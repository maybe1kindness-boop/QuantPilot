# -*- coding: utf-8 -*-
"""给 trading_alert_notifier.py 打年线闸门补丁(幂等, 带anchor断言)。

1) 新增方法 _yearline_gate_state: 读 runtime/data/market_gate.json,
   缺失/过期(>4天)/损坏 → None(放行, 日志暴露), 不因数据故障停机。
2) 盘中轮询主循环: 计算 yearline 状态; signal.side in {buy, add} 且闸门关
   → 记 hold 原因"年线闸门关闭(熊市战法)"并跳过(持仓的 sell/reduce 不受影响)。
3) 返回 dict 增加 "yearline_gate" 便于报告观测。
"""
import re
import shutil
import sys

PATH = "/opt/quantpilot/trading_alert_notifier.py"
BACKUP = PATH + ".before_yearline_gate_20260905"

src = open(PATH, encoding="utf-8").read()

if "_yearline_gate_state" in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

assert "import json" in src, "json import 缺失"
assert re.search(r"from typing import.*Optional", src), "Optional import 缺失"

shutil.copy2(PATH, BACKUP)

M_METHOD = '''    def _yearline_gate_state(self):
        """年线闸门状态(runtime/data/market_gate.json, 每晚由
        quantpilot-yearline-gate 任务写入)。缺失/过期>4天/损坏 → None
        (放行并记日志, 不因数据故障停机); open=False 表示上证收盘跌破
        200日均线, 熊市战法停开新仓。"""
        try:
            with open("/opt/quantpilot/runtime/data/market_gate.json",
                      encoding="utf-8") as f:
                st = json.load(f)
            d = str(st.get("as_of") or "")
            close_v = st.get("close")
            ma_v = st.get("ma200")
            if not d or not close_v or not ma_v:
                logger.warning("年线闸门状态字段不全, 放行: %s", st)
                return None
            age = (self._now() - datetime.strptime(
                d, "%Y-%m-%d")).days
            if age > 4:
                logger.warning("年线闸门状态过期(%s, %d天), 放行", d, age)
                return None
            return {
                "open": bool(st.get("open")),
                "as_of": d,
                "close": float(close_v),
                "ma200": float(ma_v),
            }
        except FileNotFoundError:
            logger.warning("年线闸门状态文件缺失, 放行")
            return None
        except Exception:
            logger.exception("年线闸门状态读取失败, 放行")
            return None

    def _market_gate_for(self, trade_date: str) -> Dict[str, Any]:
'''

A1 = "    def _market_gate_for(self, trade_date: str) -> Dict[str, Any]:\n"
assert src.count(A1) == 1, f"anchor1 计数异常: {src.count(A1)}"
src = src.replace(A1, M_METHOD, 1)

A2 = """        poller = engine._get_screen_poller()
        quotes = poller.fetch_all(force=True, max_age=0)
        gate = self._market_gate_for(trade_date)
"""
assert src.count(A2) == 1, f"anchor2 计数异常: {src.count(A2)}"
src = src.replace(A2, A2 + "        yearline = self._yearline_gate_state()\n", 1)

A3 = """                if exposures:
                    continue
            alert_key = f"{trade_date}:{signal.side}:{code}"
"""
assert src.count(A3) == 1, f"anchor3 计数异常: {src.count(A3)}"
BLOCK = """                if exposures:
                    continue
            if signal.side in {"buy", "add"} and yearline is not None \\
                    and not yearline["open"]:
                if code in candidate_by_code:
                    self._record_hold_reason(
                        engine, trade_date, code, self._quote_name(quote),
                        f"年线闸门关闭(上证{yearline['close']:.2f}跌破"
                        f"MA200 {yearline['ma200']:.2f}@{yearline['as_of']}), "
                        f"熊市战法停开新仓",
                    )
                continue
            alert_key = f"{trade_date}:{signal.side}:{code}"
"""
src = src.replace(A3, BLOCK, 1)

A4 = '''            "market_gate": gate,
'''
assert src.count(A4) == 1, f"anchor4 计数异常: {src.count(A4)}"
src = src.replace(A4, '''            "market_gate": gate,
            "yearline_gate": yearline,
''', 1)

open(PATH, "w", encoding="utf-8").write(src)
print("PATCH_WRITTEN")

# 校验
s2 = open(PATH, encoding="utf-8").read()
assert s2.count("_yearline_gate_state") == 2
assert "熊市战法停开新仓" in s2
assert '"yearline_gate": yearline' in s2
assert len(s2) > len(open(BACKUP, encoding="utf-8").read())
print("CHECKS_OK")
