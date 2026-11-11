# -*- coding: utf-8 -*-
"""年线闸门2天确认版(2026-09-05 拍板): 连续2天收盘>MA200才开, 任一天跌破即关。

等价回测口径: T晚计算 above(T)&above(T-1), 次日生效 = 回测 BULL(D)=above(D-1)&above(D-2)。
回测对照: 1天版+200.5% → 2天版+209.5%(2024 +39.4%→+43.5%, 切换~8→~7次/年)。
"""
import shutil

PATH = "/opt/quantpilot/yearline_gate.py"
BACKUP = PATH + ".before_gate2_20260905"

src = open(PATH, encoding="utf-8").read()
if '"confirm_days": 2' in src:
    print("ALREADY_PATCHED")
    raise SystemExit(0)
shutil.copy2(PATH, BACKUP)

OLD = '''    ma200 = float(s.rolling(200).mean().iloc[-1])
    close = float(s.iloc[-1])
    as_of = str(s.index[-1])
    is_open = bool(close > ma200)'''
NEW = '''    ma_series = s.rolling(200).mean()
    ma200 = float(ma_series.iloc[-1])
    close = float(s.iloc[-1])
    as_of = str(s.index[-1])
    # 2天确认(防1日假突破): 最近两天收盘都站上各自MA200才开; 当天跌破即关。
    if len(s) >= 201:
        prev_close = float(s.iloc[-2])
        prev_ma200 = float(ma_series.iloc[-2])
        cur_above = bool(close > ma200)
        prev_above = bool(prev_close > prev_ma200)
        confirm_days = 2
    else:
        prev_close = prev_ma200 = 0.0
        cur_above = prev_above = bool(close > ma200)
        confirm_days = 1
    is_open = bool(cur_above and prev_above)'''
assert src.count(OLD) == 1, f"A: {src.count(OLD)}"
src = src.replace(OLD, NEW, 1)

OLD = '''        "as_of": as_of, "close": round(close, 2), "ma200": round(ma200, 2),
        "open": is_open,
        "rule": "上证收盘>200日均线=开; 否则停新仓(熊市战法)",'''
NEW = '''        "as_of": as_of, "close": round(close, 2), "ma200": round(ma200, 2),
        "open": is_open,
        "prev_close": round(prev_close, 2), "prev_ma200": round(prev_ma200, 2),
        "confirm_days": confirm_days,
        "rule": "上证连续2天收盘>200日均线=开; 任一天跌破即关(2天确认防假突破)",'''
assert src.count(OLD) == 1, f"B: {src.count(OLD)}"
src = src.replace(OLD, NEW, 1)

OLD = '''        msg = (f"**{as_of}** 收盘 **{close:.2f}** > 200日均线 **{ma200:.2f}**"
               f"(超出均线 {pct:.2f}%)\\n\\n"'''
NEW = '''        msg = (f"**{as_of}** 收盘 **{close:.2f}** > 200日均线 **{ma200:.2f}**"
               f"(超出均线 {pct:.2f}%, 已连续2天站上)\\n\\n"'''
assert src.count(OLD) == 1, f"C: {src.count(OLD)}"
src = src.replace(OLD, NEW, 1)

OLD = '''    log(f"闸门: {as_of} 收盘{close:.2f} MA200 {ma200:.2f} → "
        f"{'开' if state['open'] else '关'}")'''
NEW = '''    log(f"闸门: {as_of} 收盘{close:.2f} MA200 {ma200:.2f} "
        f"前收{prev_close:.2f} 前MA{prev_ma200:.2f} → "
        f"{'开' if state['open'] else '关'}(2天确认)")'''
assert src.count(OLD) == 1, f"D: {src.count(OLD)}"
src = src.replace(OLD, NEW, 1)

open(PATH, "w", encoding="utf-8").write(src)
s = open(PATH, encoding="utf-8").read()
assert '"confirm_days": 2' in s
assert "连续2天" in s
print("GATE2_PATCH_OK")
