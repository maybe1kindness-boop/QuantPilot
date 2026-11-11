# -*- coding: utf-8 -*-
"""2026-09-05 补丁: 最大持仓天数 5→6(回测依据 09_maxhold_sweep)。

幂等: 已是 6 则跳过; 找不到锚点则报错退出不做任何修改。
config_schema.py 默认值是 0(关闭), 与生产值不耦合, 不动。
"""
import py_compile
import shutil
import sys

import yaml

CFG = "/opt/quantpilot/config/config.yaml"
SE = "/opt/quantpilot/paper_trading/signal_engine.py"
STAMP = ".before_maxhold6_20260905"

with open(CFG, encoding="utf-8") as f:
    text = f.read()

OLD = "  max_hold_days: 5"
NEW = "  max_hold_days: 6"

if OLD in text:
    shutil.copy2(CFG, CFG + STAMP)
    text = text.replace(OLD, NEW, 1)
    with open(CFG, "w", encoding="utf-8") as f:
        f.write(text)
    print("config.yaml: max_hold_days 5→6 (备份 " + STAMP + ")")
elif NEW in text:
    print("config.yaml: 已是 6, 跳过")
else:
    sys.exit("FATAL: 找不到 max_hold_days 锚点, 不做任何修改")

# ---- 校验: yaml 可解析且关键参数未被误伤 ----
data = yaml.safe_load(open(CFG, encoding="utf-8"))
trading = data.get("paper_trading") or data
checks = {
    "max_hold_days": 6,
    "stop_loss": 0.05,
    "hard_stop_loss": 0.08,
    "take_profit": 0.15,
    "max_intraday_gain_pct": 0.07,
}
for k, want in checks.items():
    got = trading.get(k)
    assert float(got) == float(want), f"{k}={got} 应为 {want}"
print("校验通过: " + ", ".join(f"{k}={v}" for k, v in checks.items()))

# signal_engine 未改动, 编译仅作烟雾确认
py_compile.compile(SE, doraise=True)
print("PATCH_OK")
