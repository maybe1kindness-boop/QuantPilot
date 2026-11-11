# -*- coding: utf-8 -*-
"""每日推送改造行为测试(stub, 不触网不写生产库)。"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/opt/quantpilot")
try:
    from trading_alert_notifier import TradingAlertNotifier
except ImportError:  # 公开版不含该私有模块, 置空占位
    TradingAlertNotifier = None


PASS = []

def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {detail}")


class Pos:
    def __init__(self, code, name, qty, cost, latest):
        self.code, self.name = code, name
        self.quantity, self.avg_cost, self.latest_price = qty, cost, latest


class FakeDAO:
    def __init__(self, basket):
        self.meta = {"dividend_basket_codes": basket}
        self.set_calls = []
    def get_meta(self, k):
        return self.meta.get(k)
    def set_meta(self, k, v):
        self.set_calls.append((k, v))
    def claim_retryable_meta(self, k, v):
        return True
    def get_trades(self, d):
        return []
    def get_candidates(self, d):
        return []
    def get_signals(self, d):
        return []


class FakeCal:
    @staticmethod
    def is_trading_day(d):
        return True


class FakeCfg:
    no_buy_notification_time = "15:20"


class FakeEngine:
    def __init__(self, basket):
        self.dao = FakeDAO(basket)
        self.calendar = FakeCal()
        self.config = FakeCfg()


n = TradingAlertNotifier.__new__(TradingAlertNotifier)

# ---------- 1) 拆分与篮子合计 ----------
print("== 1) _split_basket_machine ==")
engine = FakeEngine("601398,600887")
positions = [
    Pos("601398", "工商银行", 1000, 8.135, 8.20),
    Pos("600887", "伊利股份", 300, 26.877, 27.00),
    Pos("600186", "莲花控股", 7000, 12.033, 11.88),
]
qd = {
    "601398": {"daily_change_pct": 1.23, "price": 8.20, "prev_close": 8.10},
    "600887": {"daily_change_pct": 1.89, "price": 27.00, "prev_close": 26.50},
    "600186": {"daily_change_pct": -1.37, "price": 11.88, "prev_close": 12.04},
}
summary, machine = n._split_basket_machine(engine, positions, qd)
check("机器票不进篮子", [p.code for p in machine] == ["600186"])
check("篮子2行", len(summary["rows"]) == 2)
# 手算: 成本=8135+8063.1=16198.1 市值=8200+8100=16300 昨值=8100+7950=16050
check("当日盈亏=+250", abs(summary["daily_pnl"] - 250.0) < 0.01,
      f"({summary['daily_pnl']})")
check("当日%≈1.558", abs(summary["daily_pnl_pct"] - 250.0 / 16050 * 100) < 0.01,
      f"({summary['daily_pnl_pct']})")
check("累计=+101.9", abs(summary["cumulative_pnl"] - 101.9) < 0.05,
      f"({summary['cumulative_pnl']})")

# 估值缺失 → 无合计但有行
qd2 = {"601398": {"daily_change_pct": None, "price": 0, "prev_close": None},
       "600887": qd["600887"]}
s2, _ = n._split_basket_machine(engine, positions, qd2)
check("估值缺失不出合计", "daily_pnl" not in s2 and len(s2["rows"]) == 2)

# ---------- 2) 日报消息渲染 ----------
print("== 2) _daily_pnl_message ==")
report = {
    "trade_date": "2026-09-07", "valuation_complete": True,
    "daily_pnl": 312.5, "daily_pnl_pct": 0.11, "total_value": 287000,
    "positions_value": 248500, "position_count": 12, "price_note": "",
    "basket": summary,
    "positions": [{
        "code": "600186", "name": "莲花控股", "float_pnl_pct": -1.27,
        "daily_change_pct": -1.37, "cumulative_pnl_pct": -1.27,
        "cumulative_pnl": -1064.0, "latest_price": 11.88,
    }],
}
msg = TradingAlertNotifier._daily_pnl_message(report)
check("篮子段标题", "红利篮子" in msg)
check("篮子行渲染", "- 601398 工商银行：当日 +1.23%；累计 +0.80%" in msg, "")
check("合计行", "篮子合计：当日 +250.00 元" in msg)
check("机器段标题", "机器持仓" in msg)
check("机器行保留", "莲花控股" in msg)
# 无机器持仓 → 机器段省略, 篮子段仍在
report2 = dict(report, positions=[])
msg2 = TradingAlertNotifier._daily_pnl_message(report2)
check("无机器票省略机器段", "机器持仓" not in msg2 and "红利篮子" in msg2)
check("无机器票无莲花", "莲花控股" not in msg2)

# ---------- 3) 15:20 未成交明细闸门开关 ----------
print("== 3) run_no_buy_alert 闸门控制 ==")
sent = []
n._send = lambda title, message: (sent.append(title),
                                  {"status": "sent"})[1]
n._now = lambda: datetime(2026, 9, 7, 15, 21)
n.root = Path(tempfile.mkdtemp())
import logging
logging.basicConfig(level=logging.CRITICAL)

# 闸门关 → 跳过, 不推送
n._yearline_gate_state = lambda: {
    "as_of": "2026-09-04", "close": 3930.12, "ma200": 4010.72, "open": False}
eng = FakeEngine("601398")
r = n.run_no_buy_alert(engine=eng)
check("闸门关: skipped", r.get("status") == "skipped" and "年线闸门" in r.get("reason", ""))
check("闸门关: 未推送", len(sent) == 0)
check("闸门关: meta标记skipped", eng.dao.set_calls == [("paper_no_buy_notified:2026-09-07", "skipped")])

# 闸门开 → 正常推送
n._yearline_gate_state = lambda: None  # 数据缺失=放行路径
n._market_gate_for = lambda d: {"allow_open": True}  # stub: 真实运行在__init__后有缓存属性
eng2 = FakeEngine("601398")
r2 = n.run_no_buy_alert(engine=eng2)
check("闸门开: 推送了", len(sent) == 1 and sent[0] == "模拟盘未买入原因（明细）",
      f"({r2.get('status')})")
check("闸门开: 内容含原因", "候选池为空" in r2.get("message", "") or r2.get("status") == "no_buy")

print(f"\n{sum(1 for _, ok in PASS if ok)}/{len(PASS)} PASS")
sys.exit(0 if all(ok for _, ok in PASS) else 1)
