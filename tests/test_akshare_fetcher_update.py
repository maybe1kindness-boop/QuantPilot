import unittest
from types import SimpleNamespace

from utils.akshare_fetcher import AKShareFetcher


class AkShareFetcherUpdateTests(unittest.TestCase):
    def test_daily_update_uses_existing_batch_fetch_and_writer(self):
        fetcher = AKShareFetcher.__new__(AKShareFetcher)
        calls = []
        fetcher.db_manager = SimpleNamespace(
            list_all_stocks=lambda: ["000001", "600000", "300001"]
        )

        class FakeKlineFetcher:
            def _fetch_kline_batch(self, codes, days, use_concurrent):
                calls.append(("fetch", codes, days, use_concurrent))
                return {"000001": [1, 2], "600000": [3]}

            def _batch_update_kline_to_db(self, data):
                calls.append(("write", data))
                return 2, 1

        fetcher.kline_fetcher = FakeKlineFetcher()
        result = fetcher.daily_update(max_stocks=2, days=5)

        self.assertEqual(result, {
            "requested": 2,
            "updated": 2,
            "failed": 1,
            "records": 3,
        })
        self.assertEqual(calls[0], ("fetch", ["000001", "600000"], 5, False))
        self.assertEqual(calls[1][0], "write")


if __name__ == "__main__":
    unittest.main()
