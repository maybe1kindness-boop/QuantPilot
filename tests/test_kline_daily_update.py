import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import kline_daily_update
import pandas as pd


class KlineDailyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "stock_selection.db"
        with sqlite3.connect(self.db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE stock_basic (code TEXT);
                CREATE TABLE stock_kline (
                    code TEXT,
                    date TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER,
                    UNIQUE(code, date)
                );
                INSERT INTO stock_basic (code) VALUES ('000001'), ('600519');
                """
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_retries_until_target_coverage_is_available(self):
        first_pass = []
        second_pass = [
            ('000001', '2026-08-25', 1, 1, 1, 1, 1),
            ('600519', '2026-08-25', 1, 1, 1, 1, 1),
        ]
        with patch.object(kline_daily_update, 'DB_PATH', self.db_path), patch.object(
            kline_daily_update.TradeCalendar, 'is_trading_day', return_value=True
        ), patch.object(
            kline_daily_update, 'collect_target_rows', side_effect=[first_pass, second_pass]
        ), patch.object(kline_daily_update.time, 'sleep') as sleep:
            result = kline_daily_update.run(
                '2026-08-25',
                max_workers=1,
                min_coverage=0.95,
                retry_attempts=3,
                retry_delay_seconds=60,
            )

        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['written'], 2)
        sleep.assert_called_once_with(60)

    def test_collects_tickflow_data_in_batches(self):
        frame = pd.DataFrame([
            {
                'date': pd.Timestamp('2026-08-25'),
                'open': 1.0,
                'high': 2.0,
                'low': 0.5,
                'close': 1.5,
                'volume': 10,
            },
        ])
        with patch.object(
            kline_daily_update.StockDataFetcher,
            '_fetch_stock_batch_tickflow',
            return_value=({'000001': frame}, True),
        ), patch.object(kline_daily_update.time, 'sleep'):
            rows = kline_daily_update.collect_target_rows(['000001'], '2026-08-25')

        self.assertEqual(rows, [('000001', '2026-08-25', 1.0, 2.0, 0.5, 1.5, 10)])

    def test_stops_when_tickflow_is_unavailable(self):
        with patch.object(
            kline_daily_update.StockDataFetcher,
            '_fetch_stock_batch_tickflow',
            return_value=({}, False),
        ), patch.object(kline_daily_update.time, 'sleep'):
            with self.assertRaises(kline_daily_update.MarketDataSourceUnavailable):
                kline_daily_update.collect_target_rows(['000001'], '2026-08-25')


if __name__ == '__main__':
    unittest.main()
