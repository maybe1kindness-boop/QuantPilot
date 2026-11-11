# -*- coding: utf-8 -*-
"""篮子推送行加 买入成本价 与 最新收盘价(用户2026-09-05要求)。幂等+anchor。"""
import shutil

PATH = "/opt/quantpilot/trading_alert_notifier.py"
BACKUP = PATH + ".before_basket_price_cols_20260905"

src = open(PATH, encoding="utf-8").read()
if '"avg_cost": round(avg_cost, 3),' in src:
    print("ALREADY_PATCHED")
    raise SystemExit(0)
shutil.copy2(PATH, BACKUP)

# A: _split_basket_machine 的行字典补 avg_cost
OLD = (
    '                "name": position.name,\n'
    '                "daily_change_pct": detail.get("daily_change_pct"),\n'
)
assert src.count(OLD) == 1, f"A: {src.count(OLD)}"
src = src.replace(OLD, (
    '                "name": position.name,\n'
    '                "avg_cost": round(avg_cost, 3),\n'
    '                "daily_change_pct": detail.get("daily_change_pct"),\n'
), 1)

# B: 渲染行加 成本/收盘
OLD = (
    "                    lines.append(\n"
    "                        f\"- {row['code']} {row['name']}：\"\n"
    "                        f\"当日 {daily:+.2f}%；累计 {cumulative:+.2f}%\"\n"
    "                    )\n"
)
assert src.count(OLD) == 1, f"B: {src.count(OLD)}"
src = src.replace(OLD, (
    "                    lines.append(\n"
    "                        f\"- {row['code']} {row['name']}：\"\n"
    "                        f\"成本 {row.get('avg_cost') or 0:.2f}｜\"\n"
    "                        f\"收盘 {row.get('latest_price') or 0:.2f}｜\"\n"
    "                        f\"当日 {daily:+.2f}%；累计 {cumulative:+.2f}%\"\n"
    "                    )\n"
), 1)

open(PATH, "w", encoding="utf-8").write(src)
s = open(PATH, encoding="utf-8").read()
assert '"avg_cost": round(avg_cost, 3),' in s
assert "收盘 {row.get('latest_price') or 0:.2f}" in s
print("PATCH_OK")
