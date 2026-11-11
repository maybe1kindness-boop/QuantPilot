# -*- coding: utf-8 -*-
"""2026-09-05 补丁: ①硬止损并入尾盘14:55复核 ②防追高涨幅上限5%→7%。

幂等: 已打过的目标块不存在时直接报错退出, 不做二次修改。
"""
import py_compile
import shutil
import sys

import yaml

BASE = "/opt/quantpilot"
SE = f"{BASE}/paper_trading/signal_engine.py"
CFG = f"{BASE}/config/config.yaml"
SCH = f"{BASE}/paper_trading/config_schema.py"
STAMP = ".before_tailstop_gain7_20260905"

# ---------- 1) signal_engine.py: 硬止损改为走 stop_loss_decider ----------
with open(SE, encoding="utf-8") as f:
    se = f.read()

OLD = """        # 闪崩硬止损：无视反弹守卫与尾盘复核，立即清仓。
        if hard_sl and pnl <= -hard_sl:
            return self._exit_signal(
                code, name, pos, f"硬止损{pnl * 100:.1f}%（闪崩保护立即执行）", "sell",
            )
"""
NEW = """        # 硬止损：达线后不再盘中立即清仓，与普通止损同样走尾盘复核
        # （盘中先观察，14:55 复核仍达线才清仓；前缀保持"硬止损"以计入当日熔断）。
        if hard_sl and pnl <= -hard_sl:
            reason = f"硬止损{pnl * 100:.1f}%"
            decision = stop_loss_decider(code) if stop_loss_decider else {"action": "sell"}
            if decision.get("action") in {"wait", "hold"}:
                return Signal(
                    code, name, "hold", 0, 0.0,
                    f"{reason}：{decision.get('reason') or '等待尾盘复核'}",
                    0.0, 0.0, "position", "stop_loss_rebound_wait",
                )
            signal = self._exit_signal(code, name, pos, reason, "sell")
            if decision.get("action") == "sell_immediately":
                signal.trade_type = "tail_stop_loss"
            return signal
"""
if OLD in se:
    shutil.copy2(SE, SE + STAMP)
    se = se.replace(OLD, NEW, 1)
    with open(SE, "w", encoding="utf-8") as f:
        f.write(se)
    print("signal_engine.py: 硬止损已并入尾盘复核")
elif "硬止损：达线后不再盘中立即清仓" in se:
    print("signal_engine.py: 已打过, 跳过")
else:
    sys.exit("FATAL: signal_engine.py 找不到硬止损锚点, 不做任何修改")

# ---------- 2) config.yaml ----------
with open(CFG, encoding="utf-8") as f:
    cfg_text = f.read()
changed = []
if "max_intraday_gain_pct: 0.05" in cfg_text:
    shutil.copy2(CFG, CFG + STAMP)
    cfg_text = cfg_text.replace(
        "  max_intraday_gain_pct: 0.05", "  max_intraday_gain_pct: 0.07", 1)
    changed.append("max_intraday_gain_pct 0.05→0.07")
if "  # 闪崩硬止损：浮亏达 8% 无视反弹守卫/尾盘复核立即清仓。" in cfg_text:
    cfg_text = cfg_text.replace(
        "  # 闪崩硬止损：浮亏达 8% 无视反弹守卫/尾盘复核立即清仓。",
        "  # 硬止损：浮亏达 8% 清仓，与普通止损一样走尾盘 14:55 复核。", 1)
    changed.append("hard_stop_loss 注释更新")
if changed:
    with open(CFG, "w", encoding="utf-8") as f:
        f.write(cfg_text)
    print("config.yaml: " + "; ".join(changed))
else:
    print("config.yaml: 已打过或无目标, 跳过")

# ---------- 3) config_schema.py 默认值/注释对齐 ----------
with open(SCH, encoding="utf-8") as f:
    sch = f.read()
if "max_intraday_gain_pct: float = 0.05" in sch:
    shutil.copy2(SCH, SCH + STAMP)
    sch = sch.replace(
        "    max_intraday_gain_pct: float = 0.05         # 当日涨幅超过该值不追",
        "    max_intraday_gain_pct: float = 0.07         # 当日涨幅超过该值不追", 1)
    sch = sch.replace(
        "    hard_stop_loss: float = 0.0             # 闪崩硬止损（<=0 关闭；绕过守卫立即清仓）",
        "    hard_stop_loss: float = 0.0             # 硬止损（<=0 关闭；达线走尾盘复核清仓）", 1)
    with open(SCH, "w", encoding="utf-8") as f:
        f.write(sch)
    print("config_schema.py: 默认值/注释已对齐")
else:
    print("config_schema.py: 已打过或无目标, 跳过")

# ---------- 4) 校验 ----------
py_compile.compile(SE, doraise=True)
py_compile.compile(SCH, doraise=True)
data = yaml.safe_load(open(CFG, encoding="utf-8"))
trading = data.get("paper_trading") or data
for path in ("paper_trading",):
    if isinstance(data.get(path), dict):
        trading = data[path]
gain = trading.get("max_intraday_gain_pct")
hard = trading.get("hard_stop_loss")
assert gain == 0.07, f"max_intraday_gain_pct={gain} 应为0.07"
assert hard == 0.08, f"hard_stop_loss={hard} 应为0.08"
with open(SE, encoding="utf-8") as f:
    final = f.read()
assert "闪崩保护立即执行" not in final, "旧硬止损块仍存在"
assert final.count("硬止损：达线后不再盘中立即清仓") == 1
assert "stop_loss_rebound_wait" in final
print("校验通过: py_compile OK, yaml OK, gain=0.07, hard=0.08, 尾盘语义已接入")
print("PATCH_OK")
