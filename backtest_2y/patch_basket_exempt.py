# -*- coding: utf-8 -*-
"""notifier 补丁2: 红利篮子票豁免机器信号循环(长持腿, 不止损/不加仓/不到期)。

篮子票仍在账户里(占预算、进净值), 但不进 codes 评估循环;
priority 过滤仍用完整 positions, 防止机器把篮子票当候选再买一遍。
幂等, anchor 断言。
"""
import shutil
import sys

PATH = "/opt/quantpilot/trading_alert_notifier.py"
BACKUP = PATH + ".before_basket_exempt_20260905"

src = open(PATH, encoding="utf-8").read()
if "dividend_basket_codes" in src:
    print("ALREADY_PATCHED")
    sys.exit(0)
shutil.copy2(PATH, BACKUP)

A1 = """        for code in engine.account.position_codes():
            raw_code = str(code or "").strip()
            if raw_code.isdigit():
                positions.add(raw_code.zfill(6))
"""
assert src.count(A1) == 1, f"anchor1 计数异常: {src.count(A1)}"
src = src.replace(A1, A1 + """        basket_codes = {
            c.strip().zfill(6)
            for c in str(engine.dao.get_meta("dividend_basket_codes") or "").split(",")
            if c.strip().isdigit()
        }
        # 红利篮子=长持腿: 不进机器信号循环(不被止损/到期/加仓),
        # 但仍占用账户持仓与预算(priority过滤仍用完整positions防重复买)。
        machine_positions = positions - basket_codes
""", 1)

A2 = """        codes = (
            sorted(positions)
"""
assert src.count(A2) == 1, f"anchor2 计数异常: {src.count(A2)}"
src = src.replace(A2, """        codes = (
            sorted(machine_positions)
""", 1)

open(PATH, "w", encoding="utf-8").write(src)
s2 = open(PATH, encoding="utf-8").read()
assert s2.count("dividend_basket_codes") == 1
assert "sorted(machine_positions)" in s2
print("PATCH_OK")
