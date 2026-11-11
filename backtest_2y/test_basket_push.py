# -*- coding: utf-8 -*-
"""行为测试: 篮子成交即时推送。"""
import sys

sys.path.insert(0, "/opt/quantpilot")
import trading_alert_notifier as tan  # noqa: E402

ok = []


def check(name, cond):
    ok.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def new_notifier(sent, raise_=False):
    n = tan.TradingAlertNotifier.__new__(tan.TradingAlertNotifier)
    def fake_send(title, message):
        if raise_:
            raise RuntimeError("push down")
        sent.append((title, message))
        return {"status": "ok"}
    n._send = fake_send
    return n


class Fill:
    code = "601088"
    executed_qty = 200
    price = 40.0
    total = 8000.0
    fill_time = "14:57:01"


class Quote:
    name = "中国神华"


# 1 正常买入推送
sent = []
n = new_notifier(sent)
n._basket_fill_push("2026-09-07", Fill(), "买入", "尾盘归位", "601088", Quote())
check("1 push sent once", len(sent) == 1)
t, m = sent[0]
check("2 title", t == "模拟盘买入(篮子调度)")
check("3 body has code/name/qty/amount",
      "601088" in m and "中国神华" in m and "200 股" in m and "8,000" in m)

# 2 推送挂了不影响调度
n2 = new_notifier([], raise_=True)
try:
    n2._basket_fill_push("2026-09-07", Fill(), "卖出", "盘初预卖", "601088", Quote())
    check("4 push failure swallowed", True)
except Exception:
    check("4 push failure swallowed", False)

# 3 卖出标题 + 无quote回退code + 缺price用总额反推
sent3 = []
n3 = new_notifier(sent3)


class FillNoPx:
    executed_qty = 100
    price = 0
    total = 3500.0


n3._basket_fill_push("2026-09-07", FillNoPx(), "卖出", "盘初预卖", "600900", None)
t3, m3 = sent3[0]
check("5 sell title", t3 == "模拟盘卖出(篮子调度)")
check("6 no-quote falls back to code", "600900" in m3)
check("7 price derived 35.00", "35.00" in m3)

# 4 源码两处调用点在位
src = open("/opt/quantpilot/trading_alert_notifier.py", encoding="utf-8").read()
check("8 presell call site", '"盘初预卖", p.code, quote)' in src)
check("9 park call site", '"尾盘归位", code, quote)' in src)
check("10 helper defined once", src.count("def _basket_fill_push") == 1)

fails = [k for k, v in ok if not v]
print(f"\n{len(ok) - len(fails)}/{len(ok)} PASS")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL_OK")
