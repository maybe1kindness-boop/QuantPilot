# -*- coding: utf-8 -*-
"""补丁: 篮子调度成交即时推送(与机器成交同款)。

1) 新增 _basket_fill_push(trade_date, fill, side, action, code, quote)
2) 盘初预卖 / 尾盘归位 的每笔 filled 成交后调用
幂等: 已有 _basket_fill_push 定义则跳过。
"""
import py_compile
import shutil
import sys

SRC = "/opt/quantpilot/trading_alert_notifier.py"
BAK = SRC + ".before_basket_push_20260906"

src = open(SRC, encoding="utf-8").read()
if "_basket_fill_push" in src:
    print("ALREADY_PATCHED")
    sys.exit(0)

shutil.copyfile(SRC, BAK)

HELPER = '''    def _basket_fill_push(self, trade_date, fill, side, action, code, quote=None):
        """篮子调度成交即时推送(与机器成交同款体验; 推送失败不影响调度)。"""
        try:
            name = (self._quote_name(quote) if quote is not None else "") or code
            qty = int(getattr(fill, "executed_qty", 0) or 0)
            price = float(getattr(fill, "price", 0) or 0)
            total = float(getattr(fill, "total", 0) or 0) or qty * price
            if price <= 0 and qty > 0:
                price = total / qty
            tstr = str(getattr(fill, "fill_time", "") or "")
            lines = [f"红利篮子·{action}", f"{code} {name}"]
            if tstr:
                lines.append(f"时间 {tstr}")
            lines.append(f"价格 {price:.2f} 元 × {qty} 股")
            lines.append(f"金额 {total:,.0f} 元")
            lines.append("(闲钱停靠调度, 非机器信号)")
            self._send(f"模拟盘{side}(篮子调度)", "\\n".join(lines))
        except Exception:
            logger.exception("篮子成交推送失败")

'''

anchor_helper = "    def _basket_schedule_summary(self, engine, trade_date):"
assert src.count(anchor_helper) == 1, "helper anchor"
src = src.replace(anchor_helper, HELPER + anchor_helper)

anchor_sell = '''                    if fill.status == "filled":
                        remaining -= float(fill.total or 0.0)
                        sold += float(fill.total or 0.0)
                        fills.append(f"{p.code}x{fill.executed_qty}")
                        progressed = True'''
assert src.count(anchor_sell) == 1, "sell anchor"
src = src.replace(anchor_sell, anchor_sell.replace(
    "                        progressed = True",
    '''                        self._basket_fill_push(trade_date, fill, "卖出",
                                               "盘初预卖", p.code, quote)
                        progressed = True'''))

anchor_buy = '''                    if fill.status == "filled":
                        bought += float(fill.total or 0.0)
                        fills.append(f"{code}x{fill.executed_qty}")
                        progressed = True'''
assert src.count(anchor_buy) == 1, "buy anchor"
src = src.replace(anchor_buy, anchor_buy.replace(
    "                        progressed = True",
    '''                        self._basket_fill_push(trade_date, fill, "买入",
                                               "尾盘归位", code, quote)
                        progressed = True'''))

open(SRC, "w", encoding="utf-8").write(src)
py_compile.compile(SRC, doraise=True)
out = open(SRC, encoding="utf-8").read()
assert out.count("def _basket_fill_push") == 1
assert out.count('"盘初预卖", p.code, quote)') == 1
assert out.count('"尾盘归位", code, quote)') == 1
print("PATCH_OK")
