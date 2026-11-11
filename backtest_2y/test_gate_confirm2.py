# -*- coding: utf-8 -*-
"""年线闸门2天确认行为测试: 不联网, monkeypatch pull_closes/send_push/GATE_PATH。"""
import json
import sys
import tempfile
import os

sys.path.insert(0, "/opt/quantpilot")
import yearline_gate as yg  # noqa: E402

ok = []


def check(name, cond):
    ok.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def run_with(tail_closes, ma_hint=100.0):
    """tail_closes: 最近N天收盘序列(前面自动补200根低于均线的日线)。"""
    import pandas as pd
    base = [ma_hint * 0.9] * 200  # 长期在均线下方
    dates = [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(200)]
    dates = pd.to_datetime(dates).strftime("%Y-%m-%d").tolist()
    for k, c in enumerate(tail_closes):
        base.append(c)
        dates.append(f"2026-12-{k + 1:02d}")
    # 均值恒定: 让MA200由长期base主导, 用固定均线近似——直接喂常数序列会失真,
    # 改为: base全为100, 均线=100, 尾部给收盘值 → above判定= close>100。
    base = [100.0] * 200
    series = type("S", (), {})()
    import pandas as pd
    s = pd.Series(base + list(tail_closes))
    s.index = pd.date_range("2026-01-01", periods=len(s)).strftime("%Y-%m-%d")
    yg.pull_closes = lambda *a, **kw: s.copy()
    yg.send_push = lambda t, m: (True, "stub")
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    yg.GATE_PATH = tmp
    state = yg.update_gate()
    os.unlink(tmp)
    return state


# 1 站上1天(前一天在下方) → 关
st = run_with([99.0, 101.0])
check("1 one-day whipsaw stays closed", st["open"] is False)
check("1 confirm_days recorded", st.get("confirm_days") == 2)

# 2 连续2天站上 → 开
st = run_with([101.0, 102.0])
check("2 two consecutive opens", st["open"] is True)

# 3 开着的状态下当天跌破 → 立即关
st = run_with([102.0, 101.0, 99.5])
check("3 same-day close on break", st["open"] is False)

# 4 跌破1天又站回(前两天非连续) → 仍关
st = run_with([102.0, 99.0, 101.0])
check("4 broken streak stays closed", st["open"] is False)

# 5 恰好压线(=不算站上)
st = run_with([100.0, 100.0])
check("5 equal is not above", st["open"] is False)

fails = [k for k, v in ok if not v]
print(f"\n{len(ok) - len(fails)}/{len(ok)} PASS")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
print("ALL_OK")
