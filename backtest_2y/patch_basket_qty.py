# -*- coding: utf-8 -*-
"""篮子推送行加持仓数量(股)。幂等+anchor。"""
import shutil

PATH = "/opt/quantpilot/trading_alert_notifier.py"
BACKUP = PATH + ".before_basket_qty_20260905"

src = open(PATH, encoding="utf-8").read()
if '"quantity": quantity,' in src:
    print("ALREADY_PATCHED")
    raise SystemExit(0)
shutil.copy2(PATH, BACKUP)

# A: 行字典补 quantity
OLD = (
    '                "name": position.name,\n'
    '                "avg_cost": round(avg_cost, 3),\n'
)
assert src.count(OLD) == 1, f"A: {src.count(OLD)}"
src = src.replace(OLD, (
    '                "name": position.name,\n'
    '                "quantity": quantity,\n'
    '                "avg_cost": round(avg_cost, 3),\n'
), 1)

# B: 渲染行加 数量
OLD = (
    "                        f\"- {row['code']} {row['name']}：\"\n"
    "                        f\"成本 {row.get('avg_cost') or 0:.2f}｜\"\n"
)
assert src.count(OLD) == 1, f"B: {src.count(OLD)}"
src = src.replace(OLD, (
    "                        f\"- {row['code']} {row['name']}：\"\n"
    "                        f\"{row.get('quantity') or 0}股｜\"\n"
    "                        f\"成本 {row.get('avg_cost') or 0:.2f}｜\"\n"
), 1)

open(PATH, "w", encoding="utf-8").write(src)
s = open(PATH, encoding="utf-8").read()
assert '"quantity": quantity,' in s
assert "{row.get('quantity') or 0}股" in s
print("PATCH_OK")
