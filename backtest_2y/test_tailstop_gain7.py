# -*- coding: utf-8 -*-
"""补丁行为级验证: 硬止损尾盘复核语义 + 防追高7%。"""
import sys
from types import SimpleNamespace

sys.path.insert(0, "/opt/quantpilot")
import pandas as pd
import yaml

try:
    from paper_trading.signal_engine import SignalEngine
except ImportError:  # 公开版不含该私有模块, 置空占位
    SignalEngine = None



class FakeDAO:
    def upsert_position(self, *a, **k):
        pass

    def get_meta(self, k):
        return ""

    def set_meta(self, k, v):
        pass


class FakeAccount:
    def _pos_row(self, pos):
        return {}

    def get_position(self, code):
        return None


class FakeBroker:
    def is_limit_up_locked(self, quote):
        return False


class FakePos:
    def __init__(self, cost, hw, pnl):
        self.avg_cost = cost
        self.high_water_price = hw
        self._pnl = pnl
        self.available = 1000
        self.quantity = 1000
        self.first_buy_date = "2026-09-02"
        self.exit_stage = 0

    def float_pnl_pct(self):
        return self._pnl


CFG = SimpleNamespace(
    hard_stop_loss=0.08, breakeven_stop_after_high_pct=0.10, stop_loss=0.05,
    max_hold_days=5, take_profit=0.15, take_profit_sell_pct=0.30,
    second_take_profit=0.30, second_take_profit_sell_pct=0.30,
    trailing_profit_drawdown=0.10, limit_up_profit=0.20,
    limit_up_sell_pct=0.30, rally_exit_enabled=False,
)

engine = SignalEngine.__new__(SignalEngine)
engine.config = CFG
engine.dao = FakeDAO()
engine.account = FakeAccount()
engine.broker = FakeBroker()

df = pd.DataFrame({"date": ["2026-09-02", "2026-09-03", "2026-09-04"]})
WAIT = lambda c: {"action": "wait",
                  "reason": "止损仅在尾盘14:55复核，盘中继续观察"}
SELL = lambda c: {"action": "sell_immediately", "reason": "尾盘复核后执行清仓"}

ok = 0

# T1: 盘中浮亏-9%(超硬止损线), 尾盘复核前 → hold等待
s = engine._short_term_exit("600000", "x", FakePos(10.0, 10.0, -0.09), df,
                            SimpleNamespace(price=9.1, high=9.2), "2026-09-04", WAIT)
assert s.side == "hold" and s.trade_type == "stop_loss_rebound_wait" \
    and s.message.startswith("硬止损-9"), f"T1失败: {s.side} {s.message}"
print("T1 盘中-9%: hold等待尾盘复核 ✓ ", s.message[:50])
ok += 1

# T2: 14:55复核后仍-9% → 立即清仓, trade_type=tail_stop_loss
s = engine._short_term_exit("600000", "x", FakePos(10.0, 10.0, -0.09), df,
                            SimpleNamespace(price=9.1, high=9.2), "2026-09-04", SELL)
assert s.side == "sell" and s.trade_type == "tail_stop_loss" \
    and s.message.startswith("硬止损-9"), f"T2失败: {s.side} {s.trade_type} {s.message}"
print("T2 尾盘复核-9%: 清仓 ✓", s.message[:40])
ok += 1

# T3: -6%(未到硬线,超普通止损线) → 走普通止损分支(不受本次补丁影响)
s = engine._short_term_exit("600000", "x", FakePos(10.0, 10.0, -0.06), df,
                            SimpleNamespace(price=9.4, high=9.5), "2026-09-04", WAIT)
assert s.side == "hold" and s.message.startswith("止损-6"), f"T3失败: {s.message}"
print("T3 盘中-6%: 普通止损语义不变 ✓", s.message[:40])
ok += 1

# T4: 配置接线: yaml实际值
data = yaml.safe_load(open("/opt/quantpilot/config/config.yaml", encoding="utf-8"))
trading = data.get("paper_trading", data)
assert float(trading["max_intraday_gain_pct"]) == 0.07, trading["max_intraday_gain_pct"]
assert float(trading["hard_stop_loss"]) == 0.08
print("T4 配置: max_intraday_gain_pct=0.07, hard_stop_loss=0.08 ✓")
ok += 1

# T5: 防追高7%行为: 涨6%不再被涨幅上限拦截
try:
    from paper_trading.scheduler import IntradayScheduler
except ImportError:  # 公开版不含该私有模块, 置空占位
    IntradayScheduler = None

sch = IntradayScheduler.__new__(IntradayScheduler)
sch.engine = SimpleNamespace(config=SimpleNamespace(
    max_intraday_gain_pct=0.07, min_intraday_high_distance_pct=0.005,
    max_vwap_deviation_pct=0.015, vwap_pullback_deviation_pct=0.025,
    vwap_pullback_near_pct=0.005, vwap_reclaim_min_pct=0.002), dao=FakeDAO())
r = sch._vwap_chase_block_reason(
    "600000", SimpleNamespace(price=10.6, prev_close=10.0, high=10.7),
    "2026-09-04", vwap=10.55, intraday_high=10.7)
assert r is None, f"T5失败: 6%涨幅被拦截: {r}"
print("T5 涨6%(上限7%): 不再拦截 ✓")
ok += 1

# T6: 涨8.5%仍被拦截(超过7%)
r = sch._vwap_chase_block_reason(
    "600000", SimpleNamespace(price=10.85, prev_close=10.0, high=10.9),
    "2026-09-04", vwap=10.80, intraday_high=10.9)
assert r and "超过7.00%" in r, f"T6失败: {r}"
print("T6 涨8.5%: 拦截 ✓", r)
ok += 1

print(f"ALL_{ok}_TESTS_PASSED")
