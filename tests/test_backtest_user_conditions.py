import unittest
from unittest.mock import Mock, patch

from trading.backtest_engine import BacktestEngine


class BacktestUserConditionTests(unittest.TestCase):
    def test_saved_code_strategy_does_not_create_timing_strategy(self):
        engine = object.__new__(BacktestEngine)
        engine.stock_data_cache = {}
        engine.stock_name_cache = {}
        engine.stock_filtered_cache = {}
        engine._current_market_cap_cache = {}
        engine.buy_candidate_pool = []
        engine._run_intraday_backtest = Mock(
            return_value={'timing_strategy': {'name': 'user_conditions'}}
        )
        config = {
            'start_date': '2025-01-02',
            'end_date': '2025-01-03',
            'intraday_backtest': True,
            'timing_strategy': 'turtle',
            'user_code_source': 'def match_stock(df):\n    return True',
        }

        with patch('trading.backtest_engine.TimingStrategyFactory.create_strategy') as create, \
                patch('trading.backtest_engine.sleep_preventer.start'), \
                patch('trading.backtest_engine.sleep_preventer.stop'):
            result = engine.run_backtest('用户策略', config)

        create.assert_not_called()
        self.assertIsNone(engine.timing_strategy)
        self.assertEqual(engine.timing_strategy_name, 'user_conditions')
        self.assertEqual(result['timing_strategy']['name'], 'user_conditions')


if __name__ == '__main__':
    unittest.main()
