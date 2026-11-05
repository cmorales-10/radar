"""Turns raw market data into metrics, a transparent score, and risk flags. No network.

The score ranks how much unusual activity a stock shows. It is not a forecast and it
has not been shown to predict returns: the scorecard in run.py exists to measure that.
"""
import statistics

MAX_ATTENTION = 35
MAX_TAPE = 45
MAX_INSIDER = 20


def build_metrics(symbol, chart, session_fraction):
    """Metrics from one Yahoo chart result, or None when there is too little history."""
    meta = chart["meta"]
    bars = chart["bars"]
    if len(bars) < 21:
        return None
    price = meta.get("regularMarketPrice") or bars[-1]["close"]
    if not price:
        return None

    prior_closes = [b["close"] for b in bars[:-1] if b["close"]]
    prior_volumes = [b["volume"] for b in bars[:-1] if b["volume"]]
    if len(prior_closes) < 20 or len(prior_volumes) < 20:
        return None
    prev_close = prior_closes[-1]
    avg_volume = sum(prior_volumes[-20:]) / 20
    volume = bars[-1]["volume"] or 0
    # Volume is heavy near the open, so very early readings are floored to limit false spikes.
    pace = max(session_fraction, 0.15)
    returns = [b / a - 1 for a, b in zip(prior_closes[-21:], prior_closes[-20:])]

    return {
        "symbol": symbol,
        "name": meta.get("shortName") or meta.get("longName") or symbol,
        "instrument_type": meta.get("instrumentType"),
        "exchange": meta.get("fullExchangeName") or "",
        "price": round(price, 2),
        "day_change_pct": round((price / prev_close - 1) * 100, 2),
        "relative_volume": round(volume / pace / avg_volume, 2) if avg_volume else 0,
        "avg_dollar_volume": round(avg_volume * prev_close),
        "momentum_20d_pct": round((price / prior_closes[-20] - 1) * 100, 2),
        "above_ma20_pct": round((price / (sum(prior_closes[-20:]) / 20) - 1) * 100, 2),
        "daily_volatility_pct": round(statistics.pstdev(returns) * 100, 2),
        "pct_below_52w_high": round((1 - price / meta["fiftyTwoWeekHigh"]) * 100, 2) if meta.get("fiftyTwoWeekHigh") else None,
        "pct_below_day_high": round((1 - price / meta["regularMarketDayHigh"]) * 100, 2) if meta.get("regularMarketDayHigh") else None,
        "market_time": meta.get("regularMarketTime"),
    }


def passes_universe(metrics, universe):
    if metrics["instrument_type"] != "EQUITY":
        return False
    if metrics["price"] < universe["min_price_usd"]:
        return False
    if metrics["avg_dollar_volume"] < universe["min_avg_dollar_volume_usd"]:
        return False
    exchange = metrics["exchange"].lower()
    return any(key in exchange for key in universe["allowed_exchange_keywords"])


def _attention(crowd):
    points = 0
    reasons = []
    rank = crowd.get("reddit_rank")
    mentions = crowd.get("reddit_mentions") or 0
    before = crowd.get("reddit_mentions_24h_ago") or 0
    if rank:
        points += 12 if rank <= 5 else 8 if rank <= 20 else 4 if rank <= 50 else 0
        if rank <= 50:
            reasons.append(f"#{rank} on Reddit ({mentions} mentions/24h)")
    # A minimum count stops a jump from 2 mentions to 6 reading as a surge.
    if mentions >= 20:
        ratio = mentions / max(before, 1)
        if ratio >= 1.5:
            points += 13 if ratio >= 2 else 7
            reasons.append(f"Reddit mentions up {ratio:.1f}x in 24h")
    if crowd.get("stocktwits"):
        points += 6
        reasons.append("Trending on Stocktwits")
    if crowd.get("yahoo_trending"):
        points += 4
        reasons.append("Trending on Yahoo Finance")
    if crowd.get("yahoo_gainer"):
        reasons.append("On Yahoo's top gainers list")
    if crowd.get("yahoo_active"):
        reasons.append("On Yahoo's most active list")
    return min(points, MAX_ATTENTION), reasons


def _tape(m):
    points = 0
    reasons = []
    rvol = m["relative_volume"]
    if rvol >= 1.5:
        points += 20 if rvol >= 3 else 14 if rvol >= 2 else 7
        reasons.append(f"Volume {rvol:.1f}x normal pace")
    day = m["day_change_pct"]
    if day >= 3:
        points += 12 if day >= 7 else 8
        reasons.append(f"Up {day:.1f}% today")
    elif day <= -5:
        points -= 10
    momentum = m["momentum_20d_pct"]
    if momentum >= 10:
        points += 8 if momentum >= 25 else 5
        reasons.append(f"Up {momentum:.0f}% over 20 sessions")
    elif momentum <= -15:
        points -= 5
    below_high = m["pct_below_52w_high"]
    if below_high is not None and below_high <= 5:
        points += 5
        reasons.append(f"Within {below_high:.1f}% of its 52-week high")
    return max(min(points, MAX_TAPE), 0), reasons


def _insider(insider):
    if not insider:
        return 0, []
    buyers = insider.get("buyers", 0)
    if not buyers:
        return 0, []
    value = insider.get("buy_value", 0)
    label = "insider" if buyers == 1 else "insiders"
    return (MAX_INSIDER if buyers >= 2 else 12), [f"{buyers} {label} bought ${value:,.0f} on the open market (SEC Form 4)"]


def _risk_flags(m, crowd, insider, quote, now_ts):
    flags = []
    earnings = (quote or {}).get("earnings_ts")
    if earnings and now_ts:
        days = (earnings - now_ts) / 86400
        if 0 <= days <= 3:
            flags.append("Earnings within about 3 days: price can gap either way")
        elif -1 <= days < 0:
            flags.append("Just reported earnings: the move may already be priced in")
    if m["above_ma20_pct"] >= 25:
        flags.append(f"Stretched: {m['above_ma20_pct']:.0f}% above its 20-day average")
    if m["daily_volatility_pct"] >= 6:
        flags.append(f"Very volatile: typical daily swing {m['daily_volatility_pct']:.1f}%")
    if (crowd.get("reddit_mentions") or 0) >= 20 and m["relative_volume"] < 1.2:
        flags.append("Lots of chatter without matching volume")
    if (m["pct_below_day_high"] or 0) >= 5 and m["day_change_pct"] > 0:
        flags.append(f"Fading: {m['pct_below_day_high']:.1f}% below today's high")
    if m["day_change_pct"] <= -5:
        flags.append(f"Falling: down {abs(m['day_change_pct']):.1f}% today")
    if insider and insider.get("sell_value", 0) >= 1000000:
        flags.append(f"Insiders sold ${insider['sell_value']:,.0f} recently")
    cap = (quote or {}).get("market_cap")
    if cap and cap < 1000000000:
        flags.append("Small company: under $1B market cap")
    if not quote or not quote.get("earnings_ts"):
        flags.append("Earnings date unknown: check before relying on this")
    return flags


def score_signal(m, crowd, insider=None, quote=None, now_ts=None):
    attention, attention_reasons = _attention(crowd)
    tape, tape_reasons = _tape(m)
    insider_points, insider_reasons = _insider(insider)
    total = attention + tape + insider_points
    return {
        **m,
        "score": max(min(total, 100), 0),
        "categories": {"attention": attention, "tape": tape, "insider": insider_points},
        "reasons": tape_reasons + attention_reasons + insider_reasons,
        "flags": _risk_flags(m, crowd, insider, quote, now_ts),
        "market_cap": (quote or {}).get("market_cap"),
        "earnings_ts": (quote or {}).get("earnings_ts"),
    }


def confirming_categories(signal):
    """Independent kinds of evidence, each needing at least 10 points."""
    return sum(1 for v in signal["categories"].values() if v >= 10)
