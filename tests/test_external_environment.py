import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from utils.external_environment import (
    DEFAULT_SYMBOLS,
    OPENING_SYMBOLS,
    ExternalEnvironmentSnapshotter,
    REQUIRED_OPENING_SYMBOLS,
)


class _Response:
    def __init__(self, payload=None, content=b""):
        self.payload = payload or {}
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class ExternalEnvironmentTests(unittest.TestCase):
    def test_tencent_fallback_parses_us_quote(self):
        fields = ["0"] * 35
        fields[3] = "100"
        fields[4] = "95"
        fields[5] = "97"
        fields[30] = "2026-08-18 09:30:00"
        payload = ('v_usIXIC="' + "~".join(fields) + '";').encode("gb18030")
        with patch("utils.external_environment.requests.get", return_value=_Response(content=payload)):
            result = ExternalEnvironmentSnapshotter._fetch_tencent_symbol("us_nasdaq", False)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider"], "Tencent Finance")
        self.assertEqual(result["change_pct"], 5.2632)

    def test_eastmoney_fallback_marks_proxy(self):
        response = _Response({"data": {"f43": 6746073, "f46": 6884735, "f60": 6922025}})
        with patch("utils.external_environment.requests.get", return_value=response):
            result = ExternalEnvironmentSnapshotter._fetch_eastmoney_symbol("japan_topix", True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider_symbol"], "100.N225")
        self.assertEqual(result["proxy_for"], "japan_nikkei225")

    def test_naver_korea_quote_is_direct_and_realtime(self):
        payload = {
            "closePrice": "1,234.50",
            "fluctuationsRatio": "1.25",
            "localTradedAt": "2026-08-31T10:30:00+09:00",
            "delayTime": 0,
            "marketStatus": "OPEN",
        }
        with patch(
            "utils.external_environment.requests.get",
            return_value=_Response(payload=payload),
        ):
            result = ExternalEnvironmentSnapshotter._fetch_naver_korea_symbol(
                "korea_kosdaq", True
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider_symbol"], "KOSDAQ")
        self.assertTrue(result["realtime_verified"])
        self.assertEqual(result["change_pct"], 1.25)

    def test_naver_korea_polling_quote_is_a_second_realtime_source(self):
        payload = {
            "result": {
                "time": 1788194137675,
                "pollingInterval": 70000,
                "areas": [{"datas": [{
                    "cd": "KOSPI", "nv": 682002, "cv": 3114, "cr": 0.46,
                    "ov": 661358, "hv": 682010, "lv": 654776, "ms": "OPEN",
                }]}],
            },
        }
        with patch(
            "utils.external_environment.requests.get",
            return_value=_Response(payload=payload),
        ):
            result = ExternalEnvironmentSnapshotter._fetch_naver_korea_realtime_symbol(
                "korea_kospi", True
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["provider_symbol"], "KOSPI")
        self.assertTrue(result["realtime_verified"])
        self.assertEqual(result["close"], 6820.02)

    def test_stale_korea_quote_is_not_accepted_for_five_minute_confirmation(self):
        snapshotter = ExternalEnvironmentSnapshotter(enabled=True)
        now = datetime(2026, 8, 31, 10, 0).astimezone()
        quote = {
            "status": "ok", "realtime_verified": True, "close": 100.0,
            "observed_at": "2026-08-31T09:50:00+08:00",
        }
        self.assertFalse(snapshotter._is_fresh_korea_quote(quote, now))

    def test_stale_korea_primary_quote_switches_to_fresh_backup(self):
        stale = {
            "status": "ok", "provider": "primary", "realtime_verified": True,
            "observed_at": "2026-08-31T09:00:00+08:00", "close": 100.0,
        }
        fresh = {
            "status": "ok", "provider": "backup", "realtime_verified": True,
            "observed_at": datetime.now().astimezone().isoformat(), "close": 101.0,
        }
        with patch.object(
            ExternalEnvironmentSnapshotter,
            "_fetch_naver_korea_symbol",
            return_value=stale,
        ), patch.object(
            ExternalEnvironmentSnapshotter,
            "_fetch_naver_korea_realtime_symbol",
            return_value=fresh,
        ):
            result = ExternalEnvironmentSnapshotter._fetch_symbol(
                "korea_kospi", "^KS11", intraday=True
            )
        self.assertEqual(result["provider"], "backup")

    def test_kosdaq_never_falls_back_to_kospi_proxy(self):
        data = {
            "korea_kospi": {"status": "ok", "close": 100.0},
            "korea_kosdaq": {"status": "error", "error": "source unavailable"},
        }
        ExternalEnvironmentSnapshotter._apply_market_proxies(data)
        self.assertEqual(data["korea_kosdaq"]["status"], "error")

    def test_korea_five_minute_bars_require_a_stable_rebound(self):
        snapshotter = ExternalEnvironmentSnapshotter(enabled=True)
        snapshotter._korea_five_minute_bars = {
            "korea_kospi": [
                {"timestamp": "2026-08-31T09:35:00+09:00", "close": 100.0},
                {"timestamp": "2026-08-31T09:40:00+09:00", "close": 99.0},
                {"timestamp": "2026-08-31T09:45:00+09:00", "close": 100.0},
            ],
            "korea_kosdaq": [
                {"timestamp": "2026-08-31T09:35:00+09:00", "close": 100.0},
                {"timestamp": "2026-08-31T09:40:00+09:00", "close": 101.0},
                {"timestamp": "2026-08-31T09:45:00+09:00", "close": 102.0},
            ],
        }
        confirmation = snapshotter._korea_five_minute_confirmation()
        self.assertTrue(confirmation["stable_rising"])
        self.assertEqual(confirmation["confirmed_count"], 2)

        snapshotter._korea_five_minute_bars["korea_kospi"][-1]["close"] = 98.0
        snapshotter._korea_five_minute_bars["korea_kosdaq"][-1]["close"] = 99.0
        confirmation = snapshotter._korea_five_minute_confirmation()
        self.assertFalse(confirmation["stable_rising"])

    def test_opening_gate_blocks_when_korea_five_minute_bars_are_insufficient(self):
        data = {
            name: {
                "status": "ok", "close": 101.0, "previous_close": 100.0,
                "open": 100.0, "change_pct": 1.0, "open_gap_pct": 0.0,
            }
            for name in REQUIRED_OPENING_SYMBOLS
        }
        decision = ExternalEnvironmentSnapshotter._opening_decision(data, {
            "stable_rising": False,
            "confirmed_count": 0,
            "required_count": 1,
        })
        self.assertFalse(decision["allow_open"])
        self.assertIn("韩国5分钟K线未企稳上涨", "；".join(decision["risk_reasons"]))

    def test_closed_korea_market_does_not_block_a_share_entry(self):
        data = {
            name: {
                "status": "ok", "close": 101.0, "previous_close": 100.0,
                "open": 100.0, "change_pct": 1.0, "open_gap_pct": 0.0,
            }
            for name in REQUIRED_OPENING_SYMBOLS
        }
        for name in ("korea_kospi", "korea_kosdaq"):
            data[name]["market_status"] = "CLOSE"
        decision = ExternalEnvironmentSnapshotter._opening_decision(data, {
            "market_closed": True,
            "applicable": False,
            "stable_rising": None,
        })
        self.assertTrue(decision["allow_open"])
        self.assertEqual(decision["effective_weights"], {"us": 1.0, "korea": 0.0})
        self.assertEqual(
            decision["gate_basis"],
            "previous_us_close_technology_themes_korea_closed",
        )

    def test_optional_failures_do_not_mark_required_gate_as_failed(self):
        def fake_fetch(name, symbol, intraday):
            if name in REQUIRED_OPENING_SYMBOLS:
                return {
                    "status": "ok", "close": 100.0, "previous_close": 100.0,
                    "open": 100.0, "change_pct": 0.0, "open_gap_pct": 0.0,
                }
            return {"status": "error", "error": "optional source unavailable"}

        with tempfile.TemporaryDirectory() as directory:
            snapshotter = ExternalEnvironmentSnapshotter(
                snapshot_dir=Path(directory), enabled=True
            )
            with patch.object(snapshotter, "_fetch_symbol", side_effect=fake_fetch):
                snapshot = snapshotter.collect()
        self.assertEqual(snapshot["status"], "degraded")
        self.assertNotIn("关键数据缺失", "；".join(snapshot["opening_decision"]["reasons"]))

    def test_same_market_proxy_only_applies_after_direct_failure(self):
        data = {
            "japan_nikkei225": {"status": "ok", "close": 100.0},
            "japan_topix": {"status": "error", "error": "upstream unavailable"},
        }
        ExternalEnvironmentSnapshotter._apply_market_proxies(data)
        self.assertEqual(data["japan_topix"]["status"], "ok")
        self.assertEqual(data["japan_topix"]["proxy_for"], "japan_nikkei225")

    def test_data_quality_downgrade_does_not_mask_market_risk(self):
        snapshotter = ExternalEnvironmentSnapshotter(enabled=True)
        snapshotter.fail_closed = False
        korea_ok = {
            name: {
                "status": "ok", "close": 101.0, "previous_close": 100.0,
                "open": 100.0, "change_pct": 1.0, "open_gap_pct": 0.0,
            }
            for name in ("korea_kospi", "korea_kosdaq")
        }
        incomplete = snapshotter._opening_decision(korea_ok)
        allowed = snapshotter._apply_data_quality_policy(incomplete, ["us_nasdaq"])
        self.assertTrue(allowed["allow_open"])
        self.assertTrue(allowed["degraded_data_quality"])

        risky = snapshotter._opening_decision({**korea_ok,
            "us_nasdaq": {"status": "ok", "change_pct": -2.0},
        })
        blocked = snapshotter._apply_data_quality_policy(risky, ["us_ai_etf"])
        self.assertFalse(blocked["allow_open"])
        self.assertIn("纳指跌幅超过阈值", "；".join(blocked["risk_reasons"]))

    def test_korea_core_themes_block_opening_when_not_stable(self):
        data = {
            name: {
                "status": "ok", "close": 100.0, "previous_close": 100.0,
                "open": 100.0, "change_pct": 1.0, "open_gap_pct": 0.0,
            }
            for name in REQUIRED_OPENING_SYMBOLS
        }
        data.update({
            "japan_nikkei225": {
                "status": "ok", "close": 95.0, "previous_close": 100.0,
                "open": 99.0, "change_pct": -5.0, "open_gap_pct": -1.0,
            },
            "japan_topix": {
                "status": "ok", "close": 95.0, "previous_close": 100.0,
                "open": 99.0, "change_pct": -5.0, "open_gap_pct": -1.0,
            },
            "korea_kospi": {
                "status": "ok", "close": 95.0, "previous_close": 100.0,
                "open": 99.0, "change_pct": -5.0, "open_gap_pct": -1.0,
            },
            "korea_kosdaq": {
                "status": "ok", "close": 95.0, "previous_close": 100.0,
                "open": 99.0, "change_pct": -5.0, "open_gap_pct": -1.0,
            },
        })
        decision = ExternalEnvironmentSnapshotter._opening_decision(data)
        self.assertFalse(decision["allow_open"])
        self.assertIn(
            "韩国核心题材未企稳上涨",
            "；".join(decision["risk_reasons"]),
        )
        self.assertEqual(decision["weighted_theme_score"], 0.5)

    def test_equal_us_and_korea_theme_strengths_allow_opening(self):
        data = {
            name: {
                "status": "ok", "close": 101.0, "previous_close": 100.0,
                "open": 100.0, "change_pct": 1.0, "open_gap_pct": 0.0,
            }
            for name in REQUIRED_OPENING_SYMBOLS
        }
        decision = ExternalEnvironmentSnapshotter._opening_decision(data)
        self.assertTrue(decision["allow_open"])
        self.assertTrue(decision["korea"]["stable_rising"])
        self.assertEqual(decision["weighted_theme_score"], 1.0)
        self.assertEqual(
            decision["gate_basis"],
            "us_technology_themes_50pct_plus_korea_realtime_50pct",
        )

    def test_missing_korea_data_blocks_even_when_degraded_data_is_allowed(self):
        data = {
            name: {
                "status": "ok", "close": 101.0, "previous_close": 100.0,
                "open": 100.0, "change_pct": 1.0, "open_gap_pct": 0.0,
            }
            for name in REQUIRED_OPENING_SYMBOLS
            if name != "korea_kosdaq"
        }
        snapshotter = ExternalEnvironmentSnapshotter(enabled=True)
        snapshotter.fail_closed = False
        decision = snapshotter._opening_decision(data)
        result = snapshotter._apply_data_quality_policy(decision, ["korea_kosdaq"])
        self.assertFalse(result["allow_open"])
        self.assertIn("韩国核心题材数据不足", "；".join(result["risk_reasons"]))

    def test_intraday_refresh_only_fetches_japan_and_korea(self):
        base_data = {
            name: {
                "status": "ok", "close": 100.0, "previous_close": 100.0,
                "open": 100.0, "change_pct": 1.0, "open_gap_pct": 0.0,
                "provider_symbol": symbol,
            }
            for name, symbol in DEFAULT_SYMBOLS.items()
        }
        calls = []

        def fake_fetch(name, symbol, intraday):
            calls.append((name, symbol, intraday))
            return {
                "status": "ok", "close": 95.0, "previous_close": 100.0,
                "open": 99.0, "change_pct": -5.0, "open_gap_pct": -1.0,
                "provider_symbol": symbol,
            }

        with tempfile.TemporaryDirectory() as directory:
            snapshotter = ExternalEnvironmentSnapshotter(
                snapshot_dir=Path(directory), enabled=True
            )
            with patch.object(snapshotter, "_fetch_symbol", side_effect=fake_fetch):
                snapshot = snapshotter.refresh_intraday_opening_markets({
                    "data": base_data,
                    "created_at": "2026-08-18T09:15:00+08:00",
                })

        self.assertEqual({call[0] for call in calls}, OPENING_SYMBOLS)
        self.assertTrue(all(call[2] for call in calls))
        self.assertEqual(snapshot["data"]["us_nasdaq"]["provider_symbol"], "^IXIC")
        self.assertEqual(snapshot["refreshed_symbols"], sorted(OPENING_SYMBOLS))


if __name__ == "__main__":
    unittest.main()
