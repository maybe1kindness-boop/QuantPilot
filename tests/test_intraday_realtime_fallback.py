import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from trading.intraday_data import IntradayDataStore


class RealtimeMinuteFallbackTests(unittest.TestCase):
    def test_baostock_is_used_after_akshare_and_tdx_fail(self):
        store = IntradayDataStore.__new__(IntradayDataStore)
        expected = pd.DataFrame([{
            "timestamp": "2026-08-20 09:35:00",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "volume": 1000,
            "amount": 10100,
        }])
        akshare = SimpleNamespace(
            stock_zh_a_hist_min_em=lambda **_kwargs: (_ for _ in ()).throw(
                RuntimeError("akshare unavailable")
            )
        )
        with patch.dict(sys.modules, {"akshare": akshare}), \
                patch.object(IntradayDataStore, "_fetch_tdx", side_effect=RuntimeError("tdx unavailable")), \
                patch.object(IntradayDataStore, "_fetch_baostock", return_value=expected) as baostock, \
                patch.object(store, "_store") as persist:
            result = store.fetch_realtime_and_store(
                "000001",
                start_time=datetime(2026, 8, 20, 0, 0),
                end_time=datetime(2026, 8, 20, 10, 0),
            )

        self.assertEqual(len(result), 1)
        baostock.assert_called_once()
        self.assertEqual(persist.call_args.kwargs["source"], "baostock_realtime")


if __name__ == "__main__":
    unittest.main()
