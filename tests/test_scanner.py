import datetime as dt
import http.server
import json
import os
import threading
import unittest
from unittest import mock

from scanner import brief, market_hours as mh, notify, run, signals, sources

UTC = dt.timezone.utc


def chart(closes, volumes, price=None, **meta):
    bars = [{"ts": i, "close": c, "volume": v} for i, (c, v) in enumerate(zip(closes, volumes))]
    return {
        "meta": {
            "regularMarketPrice": price or closes[-1],
            "instrumentType": "EQUITY",
            "fullExchangeName": "NasdaqGS",
            "fiftyTwoWeekHigh": 100.0,
            "regularMarketDayHigh": price or closes[-1],
            **meta,
        },
        "bars": bars,
    }


def metrics(**overrides):
    base = {
        "symbol": "AAA", "name": "Aaa Inc", "instrument_type": "EQUITY", "exchange": "NasdaqGS",
        "price": 20.0, "day_change_pct": 0.0, "relative_volume": 1.0, "avg_dollar_volume": 50000000,
        "momentum_20d_pct": 0.0, "above_ma20_pct": 0.0, "daily_volatility_pct": 2.0,
        "pct_below_52w_high": 40.0, "pct_below_day_high": 0.0, "market_time": None,
    }
    return {**base, **overrides}


CONFIG = {
    "alerts": {"min_score": 55, "min_confirming_categories": 2, "max_per_day": 2, "cooldown_hours": 24, "rescore_bypass_points": 15},
    "universe": {"min_price_usd": 5.0, "min_avg_dollar_volume_usd": 10000000, "allowed_exchange_keywords": ["nasdaq", "nyse"]},
    "scorecard": {"horizons_trading_days": [1, 5, 20]},
}
NOW = dt.datetime(2026, 11, 4, 15, 0, tzinfo=UTC)


def strong(symbol="AAA", score=70, **overrides):
    return {
        "symbol": symbol, "score": score, "price": 20.0, "day_change_pct": 5.0,
        "categories": {"attention": 20, "tape": 30, "insider": 0},
        "reasons": ["Volume 3.0x normal pace"], "flags": [], "market_time": int(NOW.timestamp()),
        **overrides,
    }


class MarketHours(unittest.TestCase):
    def test_eastern_time_follows_dst(self):
        self.assertEqual(mh.to_eastern(dt.datetime(2026, 7, 1, 14, 0, tzinfo=UTC)).hour, 10)
        self.assertEqual(mh.to_eastern(dt.datetime(2026, 11, 4, 15, 0, tzinfo=UTC)).hour, 10)
        # DST ended at 06:00 UTC on Sunday November 1, 2026.
        self.assertEqual(mh.to_eastern(dt.datetime(2026, 11, 1, 5, 59, tzinfo=UTC)).hour, 1)
        self.assertEqual(mh.to_eastern(dt.datetime(2026, 11, 1, 6, 0, tzinfo=UTC)).hour, 1)

    def test_session_boundaries(self):
        self.assertFalse(mh.is_open(dt.datetime(2026, 11, 4, 9, 29)))
        self.assertTrue(mh.is_open(dt.datetime(2026, 11, 4, 9, 30)))
        self.assertTrue(mh.is_open(dt.datetime(2026, 11, 4, 15, 59)))
        self.assertFalse(mh.is_open(dt.datetime(2026, 11, 4, 16, 0)))

    def test_weekends_holidays_and_early_closes(self):
        self.assertFalse(mh.is_open(dt.datetime(2026, 11, 7, 12, 0)))  # Saturday
        self.assertFalse(mh.is_open(dt.datetime(2026, 11, 26, 12, 0)))  # Thanksgiving
        self.assertTrue(mh.is_open(dt.datetime(2026, 11, 27, 12, 59)))
        self.assertFalse(mh.is_open(dt.datetime(2026, 11, 27, 13, 0)))  # Day after Thanksgiving


class Metrics(unittest.TestCase):
    def test_relative_volume_scales_to_session_pace(self):
        data = chart([10.0] * 25 + [11.0], [1000] * 25 + [500])
        self.assertEqual(signals.build_metrics("AAA", data, 0.25)["relative_volume"], 2.0)

    def test_early_session_pace_is_floored(self):
        data = chart([10.0] * 25 + [11.0], [1000] * 25 + [150])
        self.assertEqual(signals.build_metrics("AAA", data, 0.01)["relative_volume"], 1.0)

    def test_day_change_uses_previous_close(self):
        data = chart([10.0] * 24 + [20.0, 22.0], [1000] * 26)
        self.assertEqual(signals.build_metrics("AAA", data, 1.0)["day_change_pct"], 10.0)

    def test_short_history_is_rejected(self):
        self.assertIsNone(signals.build_metrics("AAA", chart([10.0] * 10, [1000] * 10), 1.0))

    def test_gaps_in_history_are_ignored(self):
        data = chart([10.0] * 12 + [None] + [10.0] * 12 + [11.0], [1000] * 12 + [None] + [1000] * 13)
        self.assertEqual(signals.build_metrics("AAA", data, 1.0)["day_change_pct"], 10.0)


class Universe(unittest.TestCase):
    def test_filters(self):
        universe = CONFIG["universe"]
        self.assertTrue(signals.passes_universe(metrics(), universe))
        self.assertFalse(signals.passes_universe(metrics(instrument_type="ETF"), universe))
        self.assertFalse(signals.passes_universe(metrics(price=1.0), universe))
        self.assertFalse(signals.passes_universe(metrics(avg_dollar_volume=1000), universe))
        self.assertFalse(signals.passes_universe(metrics(exchange="Other OTC"), universe))


class Scoring(unittest.TestCase):
    def test_quiet_stock_scores_zero(self):
        self.assertEqual(signals.score_signal(metrics(), {})["score"], 0)

    def test_hype_alone_lacks_confirmation(self):
        crowd = {"reddit_rank": 1, "reddit_mentions": 500, "reddit_mentions_24h_ago": 50, "stocktwits": True}
        signal = signals.score_signal(metrics(), crowd)
        self.assertEqual(signals.confirming_categories(signal), 1)
        self.assertTrue(any("without matching volume" in f for f in signal["flags"]))

    def test_small_mention_counts_are_not_a_surge(self):
        signal = signals.score_signal(metrics(), {"reddit_mentions": 6, "reddit_mentions_24h_ago": 1})
        self.assertEqual(signal["categories"]["attention"], 0)

    def test_price_with_crowd_and_insiders_confirms(self):
        crowd = {"reddit_rank": 3, "reddit_mentions": 200, "reddit_mentions_24h_ago": 50}
        signal = signals.score_signal(metrics(relative_volume=3.0, day_change_pct=8.0), crowd, {"buyers": 2, "buy_value": 500000})
        self.assertEqual(signal["score"], 25 + 32 + 20)
        self.assertEqual(signals.confirming_categories(signal), 3)

    def test_falling_stock_loses_points_and_is_flagged(self):
        signal = signals.score_signal(metrics(relative_volume=2.0, day_change_pct=-8.0), {})
        self.assertEqual(signal["categories"]["tape"], 4)
        self.assertTrue(any(f.startswith("Falling") for f in signal["flags"]))

    def test_earnings_flag(self):
        quote = {"earnings_ts": 1000 + 86400}
        self.assertTrue(any("Earnings within" in f for f in signals.score_signal(metrics(), {}, quote=quote, now_ts=1000)["flags"]))
        self.assertTrue(any("unknown" in f for f in signals.score_signal(metrics(), {}, now_ts=1000)["flags"]))


class AlertSelection(unittest.TestCase):
    def state(self, **overrides):
        return {**run.new_state(), **overrides}

    def pick(self, results, state):
        return [s["symbol"] for s in run.select_alerts(results, state, CONFIG, NOW, "2026-11-04")]

    def test_threshold_and_confirmation(self):
        weak = strong("WEAK", score=40)
        solo = strong("SOLO", categories={"attention": 35, "tape": 9, "insider": 0}, score=60)
        self.assertEqual(self.pick([strong(), solo, weak], self.state()), ["AAA"])

    def test_cooldown_and_rising_score_bypass(self):
        recent = {"AAA": {"ts": int(NOW.timestamp()) - 3600, "score": 65}}
        self.assertEqual(self.pick([strong(score=70)], self.state(last_alert=recent)), [])
        self.assertEqual(self.pick([strong(score=80)], self.state(last_alert=recent)), ["AAA"])

    def test_cooldown_expires(self):
        old = {"AAA": {"ts": int(NOW.timestamp()) - 25 * 3600, "score": 65}}
        self.assertEqual(self.pick([strong(score=70)], self.state(last_alert=old)), ["AAA"])

    def test_daily_cap_counts_earlier_alerts(self):
        alerts = [{"symbol": "OLD", "date": "2026-11-04"}]
        self.assertEqual(self.pick([strong("A"), strong("B"), strong("C")], self.state(alerts=alerts)), ["A"])

    def test_old_quote_does_not_alert(self):
        stale = strong(market_time=int(NOW.timestamp()) - 2 * 3600)
        self.assertEqual(self.pick([stale], self.state()), [])


class Scorecard(unittest.TestCase):
    def test_horizon_returns_lock_once(self):
        alert = {"symbol": "AAA", "date": "2026-11-03", "price": 10.0, "spy_price": 100.0, "returns": {}}
        state = {"alerts": [alert]}
        eastern = dt.datetime(2026, 11, 4, 10, 0)
        run.update_scorecard(state, {"AAA": chart([11.0], [1]), "SPY": chart([101.0], [1])}, CONFIG, eastern)
        self.assertEqual(alert["returns"]["1"], {"stock": 10.0, "spy": 1.0})
        run.update_scorecard(state, {"AAA": chart([5.0], [1]), "SPY": chart([90.0], [1])}, CONFIG, eastern)
        self.assertEqual(alert["returns"]["1"], {"stock": 10.0, "spy": 1.0})
        self.assertEqual(alert["current_return_pct"], -50.0)

    def test_summary(self):
        alerts = [
            {"returns": {"1": {"stock": 10.0, "spy": 1.0}}},
            {"returns": {"1": {"stock": -20.0, "spy": 1.0}}},
        ]
        row = run.scorecard_summary(alerts, [1, 5])
        self.assertEqual(row[0]["avg_return_pct"], -5.0)
        self.assertEqual(row[0]["beat_spy_pct"], 50)
        self.assertEqual(row[0]["worst_pct"], -20.0)
        self.assertEqual(row[1]["count"], 0)


class Handler(http.server.BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Handler.received.append(json.loads(body))
        self.send_response(Handler.status)
        self.end_headers()

    def log_message(self, *args):
        pass


class Notifications(unittest.TestCase):
    def setUp(self):
        Handler.received = []
        Handler.status = 200
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        patcher = mock.patch.dict(os.environ, {
            "NTFY_TOPIC": "test-topic",
            "NTFY_SERVER": f"http://127.0.0.1:{self.server.server_port}",
            "DASHBOARD_URL": "https://example.test/radar/",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def cycle(self, state, results):
        health = {}
        charts = {"SPY": chart([100.0], [1])}
        run.send_notifications(results, charts, state, CONFIG, NOW, dt.datetime(2026, 11, 4, 10, 0), health)
        return health

    def test_digest_then_alert_and_no_repeat(self):
        state = run.new_state()
        self.assertEqual(self.cycle(state, [strong()])["notifications"], "ok")
        self.assertEqual(len(Handler.received), 2)
        self.assertEqual(Handler.received[0]["topic"], "test-topic")
        alert = Handler.received[1]
        self.assertEqual(alert["click"], "https://example.test/radar/#AAA")
        self.assertIn("not advice", alert["message"])
        self.assertEqual(state["alerts"][0]["spy_price"], 100.0)
        self.cycle(state, [strong()])
        self.assertEqual(len(Handler.received), 2)

    def test_failed_send_is_retried_next_run(self):
        Handler.status = 500
        state = run.new_state()
        self.assertIn("failed", self.cycle(state, [strong()])["notifications"])
        self.assertIsNone(state["digest_day"])
        self.assertEqual(state["alerts"], [])
        Handler.status = 200
        Handler.received = []
        self.cycle(state, [strong()])
        self.assertEqual(len(Handler.received), 2)

    def test_empty_scan_warns_once_a_day(self):
        state = run.new_state()
        self.cycle(state, [])
        self.cycle(state, [])
        self.assertEqual([m["title"] for m in Handler.received], ["Radar has no data"])

    def test_unicode_survives(self):
        notify.send("Test – dash", "emoji 📈")
        self.assertEqual(Handler.received[0]["message"], "emoji 📈")


class Form4(unittest.TestCase):
    def test_counts_only_open_market_trades(self):
        xml = b"""<ownershipDocument>
        <reportingOwner><reportingOwnerId><rptOwnerCik>123</rptOwnerCik></reportingOwnerId></reportingOwner>
        <nonDerivativeTable>
        <nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>1000</value></transactionShares><transactionPricePerShare><value>50</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
        <nonDerivativeTransaction><transactionCoding><transactionCode>S</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>10</value></transactionShares><transactionPricePerShare><value>50</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
        <nonDerivativeTransaction><transactionCoding><transactionCode>A</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>99999</value></transactionShares><transactionPricePerShare><value>0</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
        </nonDerivativeTable></ownershipDocument>"""
        self.assertEqual(sources.parse_form4(xml), ("123", 50000.0, 500.0))


class BriefSlots(unittest.TestCase):
    SLOTS = ["09:00", "12:30"]

    def test_runs_once_inside_window(self):
        now = dt.datetime(2026, 11, 4, 9, 15)
        key = brief.due_slot(now, self.SLOTS, 90, None)
        self.assertEqual(key, "2026-11-04 09:00")
        self.assertIsNone(brief.due_slot(now, self.SLOTS, 90, key))

    def test_skips_outside_window(self):
        self.assertIsNone(brief.due_slot(dt.datetime(2026, 11, 4, 11, 0), self.SLOTS, 90, None))


if __name__ == "__main__":
    unittest.main()
