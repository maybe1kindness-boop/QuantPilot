# -*- coding: utf-8 -*-
"""篮子动态调度行为测试(Stub级, 不碰真库)。"""
import json
import sys
from datetime import datetime

sys.path.insert(0, "/opt/quantpilot")
try:
    from trading_alert_notifier import TradingAlertNotifier  # noqa: E402
except ImportError:  # 公开版不含该私有模块, 置空占位
    TradingAlertNotifier  # noqa: E402 = None



class FakePos:
    def __init__(self, code, qty, available, price, avg_cost=None):
        self.code, self.quantity, self.available = code, qty, available
        self.latest_price = price
        self.avg_cost = avg_cost or price


class FakeFill:
    def __init__(self, status, qty=0, total=0.0):
        self.status, self.executed_qty, self.total = status, qty, total


class FakeAccount:
    def __init__(self, cash, positions):
        self.cash = cash
        self.positions = positions  # code -> FakePos

    def position_codes(self):
        return list(self.positions)

    def get_position(self, code):
        return self.positions.get(code)

    def update_price(self, code, price):
        if code in self.positions:
            self.positions[code].latest_price = price

    def positions_value(self):
        return sum(p.quantity * p.latest_price for p in self.positions.values())

    def total_value(self):
        return self.cash + self.positions_value()


class FakeBroker:
    def __init__(self):
        self.calls = []

    def execute_sell(self, account, quote, qty, td, msg="", side="sell",
                     **kw):
        pos = account.get_position(quote.code)
        qty = min(qty, pos.available)
        if quote.is_suspended or qty < 100:
            self.calls.append(("sell", quote.code, qty, "rejected"))
            return FakeFill("rejected")
        net = qty * quote.price * 0.998
        pos.quantity -= qty
        pos.available -= qty
        account.cash += net
        self.calls.append(("sell", quote.code, qty, "filled"))
        return FakeFill("filled", qty, net)

    def execute_buy(self, account, quote, qty, td, msg="", side="buy", **kw):
        cost = qty * quote.price * 1.0003
        if quote.is_suspended or qty < 100 or cost > account.cash:
            self.calls.append(("buy", quote.code, qty, "rejected"))
            return FakeFill("rejected")
        account.cash -= cost
        pos = account.get_position(quote.code)
        if pos is None:
            pos = FakePos(quote.code, 0, 0, quote.price)
            account.positions[quote.code] = pos
        pos.quantity += qty
        self.calls.append(("buy", quote.code, qty, "filled"))
        return FakeFill("filled", qty, cost)

    def calc_buy_quantity(self, cash, price, code):
        qty = int(cash / (price * 1.0003)) // 100 * 100
        return qty


class Quote:
    def __init__(self, code, price, suspended=False):
        self.code, self.name = code, code
        self.price = price
        self.is_suspended = suspended


class FakePoller:
    def __init__(self, quotes):
        self.quotes = quotes

    def fetch_targeted_quotes(self, codes):
        return {c: self.quotes[c] for c in codes if c in self.quotes}


class FakeDAO:
    def __init__(self, meta=None, candidates=None):
        self.meta = meta or {}
        self.candidates = candidates if candidates is not None else [{"code": "1"}]

    def get_meta(self, k, default=None):
        return self.meta.get(k, default)

    def set_meta(self, k, v):
        self.meta[k] = v

    def get_candidates(self, d):
        return self.candidates


class Cfg:
    max_positions = 3
    max_position_pct = 0.30
    add_position_pct = 0.30
    max_total_position_pct = 0.90


class FakeEngine:
    def __init__(self, account, dao, quotes):
        self.account, self.dao = account, dao
        self.config = Cfg()
        self.broker = FakeBroker()
        se = type("SE", (), {})()
        se.broker = self.broker
        self.signal_engine = se
        self._poller = FakePoller(quotes)

    def _get_screen_poller(self):
        return self._poller


def make_notifier(alert_cfg, gate):
    n = TradingAlertNotifier.__new__(TradingAlertNotifier)
    n.alert_config = alert_cfg
    n._yearline_gate_state = lambda: gate
    return n


def basket_engine(cash, weights, machine=None, candidates=None):
    """weights: [(code, qty, price)]"""
    positions = {}
    quotes = {}
    for code, qty, price in weights:
        positions[code] = FakePos(code, qty, qty, price)
        quotes[code] = Quote(code, price)
    for code, qty, price in (machine or []):
        positions[code] = FakePos(code, qty, qty, price)
        quotes[code] = Quote(code, price)
    dao = FakeDAO({"dividend_basket_codes": ",".join(
        w[0] for w in weights)}, candidates)
    acct = FakeAccount(cash, positions)
    return FakeEngine(acct, dao, quotes), acct, dao


MON = datetime(2026, 9, 7, 9, 31)      # 周一
FRI = datetime(2026, 9, 11, 9, 31)     # 周五
CLOSE = datetime(2026, 9, 7, 14, 57)
ok = []


def check(name, cond):
    ok.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


# 1 关闭开关 → 完全不动
eng, acct, dao = basket_engine(50000, [("601288", 10000, 10)])
n = make_notifier({}, {"open": True})
n._basket_schedule_tick(eng, MON)
check("1 disabled no calls", not eng.broker.calls)

# 2 闸门关 → 不预卖, 记 skipped
eng, acct, dao = basket_engine(50000, [("601288", 10000, 10)])
n = make_notifier({"basket_dynamic_parking_enabled": True}, {"open": False})
n._basket_schedule_tick(eng, MON)
log = json.loads(dao.meta["basket_schedule_log_2026-09-07"])
check("2 gate closed no sell", not eng.broker.calls)
check("2 meta skipped", log["morning"]["status"] == "skipped"
      and "闸门" in log["morning"]["reason"])

# 3 闸门开 → 按 cap_room 预卖, 超配先卖, 整手, T+1限制
#    篮子9只10000股@10 + 1只20000股@10(超配); 机器0只; 现金60万
rows = [(f"6000{i:02d}", 10000, 10) for i in range(9)] + [("600099", 20000, 10)]
eng, acct, dao = basket_engine(600000, rows)
n = make_notifier({"basket_dynamic_parking_enabled": True}, {"open": True})
n._basket_schedule_tick(eng, MON)
eq = 600000 + 9 * 100000 + 200000  # 1.7M
cap_room = 0.9 * eq - 1100000
log = json.loads(dao.meta["basket_schedule_log_2026-09-07"])["morning"]
check("3 morning done", log["status"] == "done")
check("3 target=cap_room", abs(log["target"] - cap_room) < 1)
check("3 overweight sold first", eng.broker.calls[0][1] == "600099")
sold_val = sum(q for _, c, q, s in eng.broker.calls if s == "filled")
check("3 sold <= target+lot", sold_val * 10 <= log["target"] + 1000)
check("3 all lots x100", all(q % 100 == 0 for _, _, q, s in eng.broker.calls
                             if s == "filled"))

# 4 周五 → 只备加仓需求(机器1只10万 → 3万), 不备新仓
rows = [("600100", 10000, 10)] + [(f"6001{i:02d}", 8000, 10) for i in range(9)]
eng, acct, dao = basket_engine(200000, rows, machine=[("300750", 1000, 100)])
n = make_notifier({"basket_dynamic_parking_enabled": True}, {"open": True})
n._basket_schedule_tick(eng, FRI)
log = json.loads(dao.meta["basket_schedule_log_2026-09-11"])["morning"]
mach_mv = 100000
basket_mv = 100000 + 9 * 80000
eq = 200000 + basket_mv + mach_mv
cap_room = 0.9 * eq - basket_mv - mach_mv
expect = min(mach_mv * 0.3, cap_room)
check("4 friday add-only target", abs(log["target"] - expect) < 1)

# 5 闸门开但无候选无机器 → 额度0 → skipped
eng, acct, dao = basket_engine(50000, [("601288", 10000, 10)],
                               candidates=[])
n = make_notifier({"basket_dynamic_parking_enabled": True}, {"open": True})
n._basket_schedule_tick(eng, MON)
log = json.loads(dao.meta["basket_schedule_log_2026-09-07"])["morning"]
check("5 no-cand skip", log["status"] == "skipped" and not eng.broker.calls)

# 6 尾盘归位: 现金5.5万, A低配(1万股@10=10万) B(1万股@20=20万) → A先买, 整手
eng, acct, dao = basket_engine(55000, [("600001", 10000, 10),
                                       ("600002", 10000, 20)])
n = make_notifier({"basket_dynamic_parking_enabled": True}, {"open": True})
n._basket_schedule_tick(eng, CLOSE)
log = json.loads(dao.meta["basket_schedule_log_2026-09-07"])["close"]
buys = [c for t, c, q, s in eng.broker.calls if t == "buy" and s == "filled"]
check("6 close done", log["status"] == "done")
check("6 underweight first", buys and buys[0] == "600001")
check("6 whole lots", all(q % 100 == 0 for t, c, q, s in eng.broker.calls
                          if t == "buy" and s == "filled"))
check("6 cash parked(<2.1k left)",
      acct.cash < 2100)
check("6 min amount", all(q * (10 if c == "600001" else 20) >= 2000
                          for t, c, q, s in eng.broker.calls
                          if t == "buy" and s == "filled"))

# 7 停牌腿拒绝不影响其它腿
eng, acct, dao = basket_engine(55000, [("600001", 10000, 10),
                                       ("600002", 10000, 10)])
eng._poller.quotes["600001"].is_suspended = True
n = make_notifier({"basket_dynamic_parking_enabled": True}, {"open": True})
n._basket_schedule_tick(eng, CLOSE)
log = json.loads(dao.meta["basket_schedule_log_2026-09-07"])["close"]
check("7 suspended no crash", log["status"] == "done")
check("7 only healthy leg bought",
      all(c == "600002" for t, c, q, s in eng.broker.calls
          if t == "buy" and s == "filled"))

# 8 tick 幂等: 同日第二次不再动
calls_before = len(eng.broker.calls)
n._basket_schedule_tick(eng, datetime(2026, 9, 7, 14, 58))
check("8 once per day", len(eng.broker.calls) == calls_before)

# 9 summary 渲染
n2 = make_notifier({"basket_dynamic_parking_enabled": True}, {"open": True})
eng2, acct2, dao2 = basket_engine(55000, [("600001", 10000, 10),
                                          ("600002", 10000, 20)])
dao2.meta["basket_schedule_log_2026-09-07"] = json.dumps({
    "morning": {"status": "done", "sold": 30000},
    "close": {"status": "done", "bought": 20000},
}, ensure_ascii=False)
s = n2._basket_schedule_summary(eng2, "2026-09-07")
check("9 summary text", "预卖 30,000" in s.get("text", "")
      and "归位买入 20,000" in s.get("text", ""))

fails = [k for k, v in ok if not v]
print(f"\n{len(ok) - len(fails)}/{len(ok)} PASS")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL_OK")
