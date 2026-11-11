import unittest

import pandas as pd

from trading.backtest_engine import BacktestEngine


class BacktestExitRuleTests(unittest.TestCase):
    def test_trade_buy_date_sort_key_is_oldest_first(self):
        trades = [
            {'stock_code': '600002', 'buy_date': '2026-03-03'},
            {'stock_code': '600001', 'buy_date': '2026-02-10'},
            {'stock_code': '600003', 'buy_date': '2026-03-03'},
        ]

        ordered = sorted(trades, key=BacktestEngine._trade_buy_date_sort_key)

        self.assertEqual(
            [trade['stock_code'] for trade in ordered],
            ['600001', '600002', '600003'],
        )

    def test_profit_target_arms_then_sells_on_pullback(self):
        reason, protected_price = BacktestEngine._daily_profit_pullback_exit(
            buy_price=15.2736589043201,
            open_price=15.6141544849896,
            highest_price=16.5869990011884,
            take_profit_pct=8,
            stop_loss_pct=-4,
        )

        self.assertEqual(reason, "take_profit_pullback")
        self.assertAlmostEqual(protected_price, 16.4955516173, places=6)

    def test_target_is_not_armed_before_threshold(self):
        reason, protected_price = BacktestEngine._daily_profit_pullback_exit(
            buy_price=100,
            open_price=103,
            highest_price=107.9,
            take_profit_pct=8,
            stop_loss_pct=-4,
        )

        self.assertIsNone(reason)
        self.assertAlmostEqual(protected_price, 96.0)

    def test_fixed_stop_remains_active_before_profit_target(self):
        reason, protected_price = BacktestEngine._daily_profit_pullback_exit(
            buy_price=100,
            open_price=95,
            highest_price=104,
            take_profit_pct=8,
            stop_loss_pct=-4,
        )

        self.assertEqual(reason, "stop_loss")
        self.assertAlmostEqual(protected_price, 96.0)

    def test_fixed_stop_triggers_when_intraday_low_breaks_threshold(self):
        reason, protected_price = BacktestEngine._daily_profit_pullback_exit(
            buy_price=100,
            open_price=98,
            low_price=94,
            highest_price=104,
            take_profit_pct=8,
            stop_loss_pct=-4,
        )

        self.assertEqual(reason, "stop_loss")
        self.assertAlmostEqual(protected_price, 96.0)

    def test_reaching_target_does_not_sell_until_price_falls_back(self):
        reason, _ = BacktestEngine._daily_profit_pullback_exit(
            buy_price=100,
            open_price=109,
            highest_price=108,
            take_profit_pct=8,
            stop_loss_pct=-4,
        )

        self.assertIsNone(reason)

    def test_next_day_entry_rejects_one_price_board(self):
        bars = pd.DataFrame([
            {'open': 10, 'high': 10, 'low': 10, 'close': 10},
        ])

        allowed, reason = BacktestEngine._check_next_day_entry_confirmation(bars, 9)

        self.assertFalse(allowed)
        self.assertEqual(reason, '一字板')

    def test_next_day_entry_waits_for_two_stable_bars_after_high_open_fall(self):
        bars = pd.DataFrame([
            {'open': 10.5, 'high': 10.6, 'low': 10.1, 'close': 10.2},
            {'open': 10.2, 'high': 10.25, 'low': 9.9, 'close': 10.0},
            {'open': 10.0, 'high': 10.1, 'low': 9.95, 'close': 10.05},
            {'open': 10.05, 'high': 10.1, 'low': 9.96, 'close': 10.0},
        ])

        allowed, reason = BacktestEngine._check_next_day_entry_confirmation(bars, 10)

        self.assertTrue(allowed)
        self.assertEqual(reason, '高开低走后连续两个5分钟K线低点未创新低')

    def test_next_day_entry_rejects_new_low_after_high_open_fall(self):
        bars = pd.DataFrame([
            {'open': 10.5, 'high': 10.6, 'low': 10.1, 'close': 10.2},
            {'open': 10.2, 'high': 10.25, 'low': 9.9, 'close': 10.0},
            {'open': 10.0, 'high': 10.1, 'low': 9.8, 'close': 9.85},
            {'open': 9.85, 'high': 9.95, 'low': 9.85, 'close': 9.9},
        ])

        allowed, reason = BacktestEngine._check_next_day_entry_confirmation(bars, 10)

        self.assertFalse(allowed)
        self.assertEqual(reason, '高开低走后仍有5分钟K线低点创新低')

    def test_optimized_entry_rejects_large_gap(self):
        bars = pd.DataFrame([
            {'open': 10.4, 'high': 10.5, 'low': 10.2, 'close': 10.3, 'volume': 100},
            {'open': 10.3, 'high': 10.4, 'low': 10.2, 'close': 10.35, 'volume': 120},
            {'open': 10.35, 'high': 10.45, 'low': 10.3, 'close': 10.4, 'volume': 150},
        ])

        allowed, reason = BacktestEngine._check_next_day_entry_confirmation(
            bars, 10, max_gap_pct=3, min_bars=3
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, '高开超过追价限制')

    def test_risk_position_uses_atr_and_position_cap(self):
        engine = object.__new__(BacktestEngine)
        engine._user_code_history_cache = {
            '000001': pd.DataFrame([
                {'date': '2026-01-01', 'high': 11, 'low': 9, 'close': 10},
                {'date': '2026-01-02', 'high': 12, 'low': 10, 'close': 11},
                {'date': '2026-01-03', 'high': 13, 'low': 11, 'close': 12},
                {'date': '2026-01-04', 'high': 14, 'low': 12, 'close': 13},
            ])
        }

        quantity, stop_price = engine._calculate_intraday_risk_position(
            '000001', '2026-01-05', 10, 100000,
            {
                'risk_per_trade_pct': 0.75,
                'atr_stop_period': 2,
                'atr_stop_multiplier': 1.8,
                'max_atr_stop_pct': 6,
                'stop_loss': -5,
                'max_position_pct': 30,
                'use_atr_stop': True,
            },
        )

        self.assertEqual(quantity, 1200)
        self.assertAlmostEqual(stop_price, 9.4, places=6)

    def test_optimized_entry_requires_opening_range_breakout(self):
        bars = pd.DataFrame([
            {'open': 10.0, 'high': 10.4, 'low': 9.9, 'close': 10.1, 'volume': 100},
            {'open': 10.1, 'high': 10.5, 'low': 10.0, 'close': 10.2, 'volume': 110},
            {'open': 10.2, 'high': 10.45, 'low': 10.1, 'close': 10.25, 'volume': 120},
            {'open': 10.25, 'high': 10.5, 'low': 10.2, 'close': 10.3, 'volume': 130},
            {'open': 10.3, 'high': 10.45, 'low': 10.25, 'close': 10.35, 'volume': 140},
            {'open': 10.35, 'high': 10.5, 'low': 10.3, 'close': 10.4, 'volume': 150},
            {'open': 10.4, 'high': 10.9, 'low': 10.35, 'close': 10.7, 'volume': 220},
        ])

        allowed, reason = BacktestEngine._check_next_day_entry_confirmation(
            bars, 10, min_bars=6, require_opening_breakout=True,
            opening_range_bars=6,
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, '非高开')

    def test_loss_stop_cooldown_blocks_only_following_trading_days(self):
        calendar = {
            pd.Timestamp('2026-01-05').date(): 0,
            pd.Timestamp('2026-01-06').date(): 1,
            pd.Timestamp('2026-01-07').date(): 2,
            pd.Timestamp('2026-01-08').date(): 3,
        }
        stop_date = pd.Timestamp('2026-01-05').date()

        self.assertFalse(BacktestEngine._is_reentry_cooldown_active(
            stop_date, stop_date, 2, calendar
        ))
        self.assertTrue(BacktestEngine._is_reentry_cooldown_active(
            stop_date, pd.Timestamp('2026-01-07').date(), 2, calendar
        ))
        self.assertFalse(BacktestEngine._is_reentry_cooldown_active(
            stop_date, pd.Timestamp('2026-01-08').date(), 2, calendar
        ))
        self.assertFalse(BacktestEngine._is_reentry_cooldown_active(
            stop_date, pd.Timestamp('2026-01-06').date(), 0, calendar
        ))


if __name__ == "__main__":
    unittest.main()
