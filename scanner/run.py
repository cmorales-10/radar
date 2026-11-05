"""One scan cycle: collect, score, track past alerts, notify, and write the dashboard data.

Run from the radar folder: py -m scanner.run
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import traceback

from . import brief, market_hours, notify, signals, sources

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
STATE = ROOT / "data" / "state.json"
SITE_DATA = ROOT / "site" / "data.json"
BENCHMARK = "SPY"
MAX_ALERT_HISTORY = 300
MAX_QUOTE_AGE_SECONDS = 3600


def _load(path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _save(path, data, indent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")


def new_state():
    return {
        "alerts": [],
        "last_alert": {},
        "insider_cache": {},
        "latest": [],
        "latest_at": None,
        "digest_day": None,
        "failure_notice_day": None,
        "brief": None,
        "brief_slot": None,
    }


def _attempt(health, name, function, default):
    """Call one data source, recording failure without stopping the scan."""
    try:
        result = function()
        health[name] = "ok"
        return result
    except Exception as error:
        health[name] = f"failed: {type(error).__name__}"
        return default


def collect_candidates(limit, health):
    crowd = {}

    def tag(symbol):
        return crowd.setdefault(symbol, {})

    mentions = _attempt(health, "reddit", sources.reddit_mentions, {})
    for symbol, fields in mentions.items():
        if (fields.get("reddit_rank") or 999) <= 50:
            tag(symbol).update(fields)
    for symbol in _attempt(health, "stocktwits", sources.stocktwits_trending, set()):
        tag(symbol)["stocktwits"] = True
    for symbol in _attempt(health, "yahoo_trending", sources.yahoo_trending, set()):
        tag(symbol)["yahoo_trending"] = True

    quotes = {}
    for screen, key in (("day_gainers", "yahoo_gainer"), ("most_actives", "yahoo_active")):
        listed = _attempt(health, f"yahoo_{screen}", lambda s=screen: sources.yahoo_screener(s), {})
        quotes.update(listed)
        for symbol in listed:
            tag(symbol)[key] = True

    # Names seen in more places come first, so a cap only drops the weakest.
    ranked = sorted(crowd, key=lambda s: (-len(crowd[s]), crowd[s].get("reddit_rank") or 999))
    return {s: crowd[s] for s in ranked[:limit]}, quotes


def _insider_for(symbol, ciks, state, config, now):
    settings = config["enrichment"]
    cached = state["insider_cache"].get(symbol)
    if cached and now.timestamp() - cached["fetched"] < settings["insider_cache_hours"] * 3600:
        return cached["data"]
    cik = ciks.get(symbol)
    if not cik:
        return None
    since = (now - dt.timedelta(days=settings["insider_lookback_days"])).date().isoformat()
    data = sources.insider_activity(cik, since, settings["insider_min_purchase_usd"])
    state["insider_cache"][symbol] = {"fetched": int(now.timestamp()), "data": data}
    return data


def scan(state, config, now, eastern, health):
    crowd, quotes = collect_candidates(config["universe"]["max_candidates"], health)
    symbols = list(crowd)
    charts, failed = sources.yahoo_charts(symbols + [BENCHMARK] + _tracked_symbols(state, config, eastern))
    health["yahoo_charts"] = "ok" if charts else "failed: no charts returned"
    if failed:
        health["yahoo_charts_missing"] = len(failed)
    quotes.update(_attempt(health, "yahoo_quotes", lambda: sources.yahoo_quotes(symbols), {}))

    fraction = market_hours.session_fraction(eastern)
    now_ts = int(now.timestamp())
    scored = []
    for symbol in symbols:
        if symbol not in charts:
            continue
        metrics = signals.build_metrics(symbol, charts[symbol], fraction)
        if metrics and signals.passes_universe(metrics, config["universe"]):
            scored.append(signals.score_signal(metrics, crowd[symbol], quote=quotes.get(symbol), now_ts=now_ts))
    scored.sort(key=lambda s: s["score"], reverse=True)

    # Filings and headlines cost more requests, so only the leaders get them.
    leaders = scored[:config["enrichment"]["top_n"]]
    ciks = _attempt(health, "sec", sources.sec_ciks, {})
    results = []
    for signal in scored:
        symbol = signal["symbol"]
        insider = None
        news = []
        if signal in leaders:
            try:
                insider = _insider_for(symbol, ciks, state, config, now)
            except Exception:
                health["sec"] = "partial"
            try:
                news = sources.headlines(symbol, now, config["enrichment"]["headline_max_age_days"])
            except Exception:
                health["headlines"] = "partial"
        metrics = {k: signal[k] for k in signal if k not in ("score", "categories", "reasons", "flags", "market_cap", "earnings_ts")}
        final = signals.score_signal(metrics, crowd[symbol], insider, quotes.get(symbol), now_ts)
        final["headlines"] = news
        results.append(final)
    results.sort(key=lambda s: s["score"], reverse=True)
    health.setdefault("headlines", "ok")
    return results, charts


def _tracked_symbols(state, config, eastern):
    longest = max(config["scorecard"]["horizons_trading_days"])
    return sorted({a["symbol"] for a in state["alerts"] if _age(a, eastern) <= longest})


def _age(alert, eastern):
    return market_hours.trading_days_between(dt.date.fromisoformat(alert["date"]), eastern.date())


def _last_price(chart):
    price = chart["meta"].get("regularMarketPrice")
    if price:
        return price
    closes = [b["close"] for b in chart["bars"] if b["close"]]
    return closes[-1] if closes else None


def update_scorecard(state, charts, config, eastern):
    """Record each alert's return, and SPY's, the first time it reaches each horizon."""
    spy = _last_price(charts[BENCHMARK]) if BENCHMARK in charts else None
    for alert in state["alerts"]:
        chart = charts.get(alert["symbol"])
        price = _last_price(chart) if chart else None
        if not price or not spy or not alert.get("spy_price"):
            continue
        age = _age(alert, eastern)
        alert["current_return_pct"] = round((price / alert["price"] - 1) * 100, 2)
        alert["current_spy_return_pct"] = round((spy / alert["spy_price"] - 1) * 100, 2)
        for horizon in config["scorecard"]["horizons_trading_days"]:
            key = str(horizon)
            if age >= horizon and key not in alert["returns"]:
                alert["returns"][key] = {
                    "stock": alert["current_return_pct"],
                    "spy": alert["current_spy_return_pct"],
                }


def scorecard_summary(alerts, horizons):
    summary = []
    for horizon in horizons:
        rows = [a["returns"][str(horizon)] for a in alerts if str(horizon) in a["returns"]]
        if not rows:
            summary.append({"horizon": horizon, "count": 0})
            continue
        stock = [r["stock"] for r in rows]
        spy = [r["spy"] for r in rows]
        summary.append({
            "horizon": horizon,
            "count": len(rows),
            "avg_return_pct": round(sum(stock) / len(rows), 2),
            "avg_spy_return_pct": round(sum(spy) / len(rows), 2),
            "beat_spy_pct": round(100 * sum(1 for r in rows if r["stock"] > r["spy"]) / len(rows)),
            "positive_pct": round(100 * sum(1 for r in rows if r["stock"] > 0) / len(rows)),
            "worst_pct": min(stock),
            "best_pct": max(stock),
        })
    return summary


def select_alerts(results, state, config, now, today):
    settings = config["alerts"]
    sent_today = sum(1 for a in state["alerts"] if a["date"] == today)
    picks = []
    for signal in results:
        if sent_today + len(picks) >= settings["max_per_day"]:
            break
        if signal["score"] < settings["min_score"]:
            break  # Results are sorted by score.
        if signals.confirming_categories(signal) < settings["min_confirming_categories"]:
            continue
        # A halted stock or a lagging feed would alert on an old price.
        if signal.get("market_time") and now.timestamp() - signal["market_time"] > MAX_QUOTE_AGE_SECONDS:
            continue
        previous = state["last_alert"].get(signal["symbol"])
        if previous:
            hours = (now.timestamp() - previous["ts"]) / 3600
            risen = signal["score"] >= previous["score"] + settings["rescore_bypass_points"]
            if hours < settings["cooldown_hours"] and not risen:
                continue
        picks.append(signal)
    return picks


def _click_url(fragment=""):
    base = os.environ.get("DASHBOARD_URL", "").strip()
    if not base:
        return None
    return f"{base}#{fragment}" if fragment else base


def send_notifications(results, charts, state, config, now, eastern, health):
    """Only runs while the market is open. State changes only after a send succeeds."""
    today = eastern.date().isoformat()
    if not notify.configured():
        health["notifications"] = "not configured"
        return
    try:
        if not results and state.get("failure_notice_day") != today:
            notify.send("Radar has no data", "The scan found no usable market data. Open the dashboard to see which source failed.", _click_url(), ["warning"])
            state["failure_notice_day"] = today

        if results and state.get("digest_day") != today:
            lines = [f"{s['symbol']} ({s['score']}): {s['reasons'][0] if s['reasons'] else 'on watch'}" for s in results[:3]]
            lines.append("Research signals, not advice.")
            notify.send("Market open: top of the radar", "\n".join(lines), _click_url(), ["bar_chart"])
            state["digest_day"] = today

        spy = _last_price(charts[BENCHMARK]) if BENCHMARK in charts else None
        for signal in select_alerts(results, state, config, now, today):
            notify.send(notify.signal_title(signal), notify.signal_message(signal), _click_url(signal["symbol"]), ["mag"], priority=4)
            state["last_alert"][signal["symbol"]] = {"ts": int(now.timestamp()), "score": signal["score"]}
            state["alerts"].append({
                "symbol": signal["symbol"],
                "date": today,
                "ts": int(now.timestamp()),
                "score": signal["score"],
                "price": signal["price"],
                "spy_price": spy,
                "reasons": signal["reasons"],
                "flags": signal["flags"],
                "returns": {},
            })
        health["notifications"] = "ok"
    except Exception as error:
        health["notifications"] = f"failed: {type(error).__name__}"


def maybe_write_brief(results, state, config, eastern, health):
    settings = config["brief"]
    if not brief.configured():
        health["brief"] = "off"
        return
    if not results or not market_hours.is_trading_day(eastern.date()):
        return
    slot = brief.due_slot(eastern, settings["slots_et"], settings["slot_window_minutes"], state.get("brief_slot"))
    if not slot:
        return
    try:
        text = brief.generate(results, settings["model"], settings["max_tokens"])
        state["brief_slot"] = slot
        if text:
            state["brief"] = {"text": text, "slot": slot}
        health["brief"] = "ok" if text else "declined"
    except Exception as error:
        health["brief"] = f"failed: {type(error).__name__}"


def build_site_data(state, config, now, eastern, health, stale):
    horizons = config["scorecard"]["horizons_trading_days"]
    return {
        "generated_at": int(now.timestamp()),
        "latest_at": state["latest_at"],
        "market_open": market_hours.is_open(eastern),
        "stale": stale,
        "min_alert_score": config["alerts"]["min_score"],
        "signals": state["latest"][:config["dashboard_rows"]],
        "alerts": list(reversed(state["alerts"][-50:])),
        "scorecard": scorecard_summary(state["alerts"], horizons),
        "brief": state.get("brief"),
        "health": health,
    }


def run_cycle(now=None, send=True):
    now = now or dt.datetime.now(dt.timezone.utc)
    eastern = market_hours.to_eastern(now)
    config = _load(CONFIG)
    state = {**new_state(), **_load(STATE, {})}
    health = {}

    results, charts = scan(state, config, now, eastern, health)
    stale = not results
    if results:
        state["latest"] = results
        state["latest_at"] = int(now.timestamp())
    update_scorecard(state, charts, config, eastern)

    if send and market_hours.is_open(eastern):
        send_notifications(results, charts, state, config, now, eastern, health)
    elif not send:
        health["notifications"] = "skipped (dry run)"
    else:
        health["notifications"] = "market closed"
    maybe_write_brief(results, state, config, eastern, health)

    state["alerts"] = state["alerts"][-MAX_ALERT_HISTORY:]
    _save(STATE, state, indent=1)
    _save(SITE_DATA, build_site_data(state, config, now, eastern, health, stale))
    print(json.dumps({"signals": len(results), "open": market_hours.is_open(eastern), "health": health}, indent=2))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Scan and write data without sending notifications")
    parser.add_argument("--test-notify", action="store_true", help="Send one test notification and exit")
    args = parser.parse_args()
    if args.test_notify:
        if not notify.configured():
            print("Set NTFY_TOPIC first.", file=sys.stderr)
            return 1
        notify.send("Radar test", "Notifications are working.", _click_url(), ["white_check_mark"])
        print("Sent.")
        return 0
    try:
        run_cycle(send=not args.dry_run)
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
