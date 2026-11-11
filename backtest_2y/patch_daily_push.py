# -*- coding: utf-8 -*-
"""每日推送改造(2026-09-05 用户指令):
①15:10 收益日报加"红利篮子"段: 每只当日涨跌+累计涨跌+篮子合计;
  机器持仓单独一段, 无机器持仓时整段省略(篮子段照常推)。
②15:20 未成交明细(模拟盘未买入原因): 年线闸门关闭(上证未收复200日均线)
  时不推送; 收复后才推送。
③_refresh_daily_pnl_prices 的 quote_details 补 price/prev_close 供篮子合计。

幂等, anchor 断言, 备份 .before_daily_push_20260905。
"""
import shutil

PATH = "/opt/quantpilot/trading_alert_notifier.py"
BACKUP = PATH + ".before_daily_push_20260905"

src = open(PATH, encoding="utf-8").read()
if "_split_basket_machine" in src:
    print("ALREADY_PATCHED")
    raise SystemExit(0)
shutil.copy2(PATH, BACKUP)

# ---- A: quote_details 补 price/prev_close ----
OLD = (
    '                quote_details[position.code] = {\n'
    '                    "daily_change_pct": round(daily_change_pct, 2)\n'
    '                    if daily_change_pct is not None else None,\n'
    "                }\n"
)
assert src.count(OLD) == 1, f"A: {src.count(OLD)}"
src = src.replace(OLD, (
    '                quote_details[position.code] = {\n'
    '                    "daily_change_pct": round(daily_change_pct, 2)\n'
    '                    if daily_change_pct is not None else None,\n'
    '                    "price": round(price, 3),\n'
    '                    "prev_close": round(prev_close, 3) if prev_close > 0 else None,\n'
    "                }\n"
), 1)

# ---- B: 篮子/机器拆分助手, 插在 _position_cumulative_return 前 ----
OLD = (
    "    @staticmethod\n"
    '    def _position_cumulative_return(position: Any) -> Dict[str, Optional[float]]:\n'
)
assert src.count(OLD) == 1, f"B: {src.count(OLD)}"
src = src.replace(OLD, (
    '    def _split_basket_machine(self, engine, positions, quote_details):\n'
    '        """持仓拆分为 红利篮子(长持腿) 与 机器持仓, 并算篮子日/累计汇总。\n'
    "\n"
    '        篮子标记来自 pt_meta.dividend_basket_codes; 任一篮子票收盘估值\n'
    '        缺失时不出合计(单行照常显示, 机器段与旧逻辑无关)。\n'
    '        """\n'
    "        basket_codes = {\n"
    '            c.strip().zfill(6)\n'
    '            for c in str(engine.dao.get_meta("dividend_basket_codes") or "").split(",")\n'
    "            if c.strip().isdigit()\n"
    "        }\n"
    "        rows = []\n"
    "        machine = []\n"
    "        cost_value = market_value = prior_value = 0.0\n"
    "        complete = True\n"
    "        for position in positions:\n"
    "            if position.code not in basket_codes:\n"
    "                machine.append(position)\n"
    "                continue\n"
    '            quantity = int(getattr(position, "quantity", 0) or 0)\n'
    '            avg_cost = float(getattr(position, "avg_cost", 0) or 0)\n'
    "            cumulative = self._position_cumulative_return(position)\n"
    '            detail = quote_details.get(position.code) or {}\n'
    '            price = float(cumulative.get("latest_price") or 0.0)\n'
    '            prev_close = float(detail.get("prev_close") or 0.0)\n'
    "            rows.append({\n"
    '                "code": position.code,\n'
    '                "name": position.name,\n'
    '                "daily_change_pct": detail.get("daily_change_pct"),\n'
    '                "latest_price": cumulative.get("latest_price"),\n'
    '                "cumulative_pnl": cumulative.get("cumulative_pnl"),\n'
    '                "cumulative_pnl_pct": cumulative.get("cumulative_pnl_pct"),\n'
    "            })\n"
    "            if price > 0 and avg_cost > 0:\n"
    "                cost_value += quantity * avg_cost\n"
    "                market_value += quantity * price\n"
    "            else:\n"
    "                complete = False\n"
    "            if price > 0 and prev_close > 0:\n"
    "                prior_value += quantity * prev_close\n"
    "            else:\n"
    "                complete = False\n"
    '        summary = {"rows": rows}\n'
    "        if complete and cost_value > 0 and prior_value > 0:\n"
    "            daily_pnl = market_value - prior_value\n"
    "            summary.update({\n"
    '                "daily_pnl": round(daily_pnl, 2),\n'
    '                "daily_pnl_pct": round(daily_pnl / prior_value * 100, 2),\n'
    '                "cumulative_pnl": round(market_value - cost_value, 2),\n'
    '                "cumulative_pnl_pct": round(\n'
    "                    (market_value - cost_value) / cost_value * 100, 2),\n"
    "            })\n"
    "        return summary, machine\n"
    "\n"
    + OLD), 1)

# ---- C: 整体替换 _daily_pnl_message(按方法边界定位, 不逐字匹配旧体) ----
i0 = src.index("    def _daily_pnl_message")
i0 = src.rindex("    @staticmethod", 0, i0)
i1 = src.index("    def run_daily_pnl_alert", i0)
NEW_C = (
    '    @staticmethod\n'
    '    def _daily_pnl_message(report: Dict[str, Any]) -> str:\n'
    '        lines = [f"【模拟盘收益｜{report[\'trade_date\']} 15:10】"]\n'
    '        if report.get("valuation_complete", True):\n'
    "            lines.extend([\n"
    '                f"账户当日收益：{report[\'daily_pnl\']:+,.2f} 元（{report[\'daily_pnl_pct\']:+.2f}%）",\n'
    '                f"账户总资产：{report[\'total_value\']:,.2f} 元",\n'
    '                f"当前持仓市值：{report[\'positions_value\']:,.2f} 元（{report[\'position_count\']} 只）",\n'
    "            ])\n"
    "        else:\n"
    '            lines.append("持仓收盘行情未完整获取，本次不计算当日盈亏或账户总资产。")\n'
    '        basket = report.get("basket") or {}\n'
    '        basket_rows = basket.get("rows") or []\n'
    "        if basket_rows:\n"
    '            lines.append("—— 红利篮子（熊市长持腿）——")\n'
    "            for row in basket_rows:\n"
    '                daily = row.get("daily_change_pct")\n'
    '                cumulative = row.get("cumulative_pnl_pct")\n'
    "                if daily is not None and cumulative is not None:\n"
    "                    lines.append(\n"
    '                        f"- {row[\'code\']} {row[\'name\']}："\n'
    '                        f"当日 {daily:+.2f}%；累计 {cumulative:+.2f}%"\n'
    "                    )\n"
    "                else:\n"
    "                    lines.append(\n"
    '                        f"- {row[\'code\']} {row[\'name\']}：收盘估值未获取"\n'
    "                    )\n"
    '            if basket.get("daily_pnl") is not None:\n'
    "                lines.append(\n"
    '                    f"篮子合计：当日 {basket[\'daily_pnl\']:+,.2f} 元"\n'
    '                    f"（{basket[\'daily_pnl_pct\']:+.2f}%）；"\n'
    '                    f"累计 {basket[\'cumulative_pnl\']:+,.2f} 元"\n'
    '                    f"（{basket[\'cumulative_pnl_pct\']:+.2f}%）"\n'
    "                )\n"
    '        positions = report.get("positions") or []\n'
    "        if positions:\n"
    '            lines.append("—— 机器持仓 ——")\n'
    "        for position in positions:\n"
    '            if position.get("float_pnl_pct") is None:\n'
    '                lines.append(f"- {position[\'code\']} {position[\'name\']}：收盘估值未获取")\n'
    '            elif position.get("cumulative_pnl_pct") is None:\n'
    '                daily_change_pct = position.get("daily_change_pct")\n'
    "                daily_text = (\n"
    '                    f"{daily_change_pct:+.2f}%"\n'
    '                    if daily_change_pct is not None else "未获取"\n'
    "                )\n"
    "                lines.append(\n"
    '                    f"- {position[\'code\']} {position[\'name\']}："\n'
    '                    f"当日涨跌 {daily_text}；累计持仓涨跌未获取"\n'
    "                )\n"
    '            elif position.get("daily_change_pct") is None:\n'
    "                lines.append(\n"
    '                    f"- {position[\'code\']} {position[\'name\']}："\n'
    '                    f"当日涨跌幅未获取；累计持仓涨跌 {position[\'cumulative_pnl_pct\']:+.2f}%"\n'
    "                )\n"
    "            else:\n"
    "                lines.append(\n"
    '                    f"- {position[\'code\']} {position[\'name\']}："\n'
    '                    f"当日涨跌 {position[\'daily_change_pct\']:+.2f}%；"\n'
    '                    f"累计持仓涨跌 {position[\'cumulative_pnl_pct\']:+.2f}%"\n'
    "                )\n"
    '        if report.get("price_note"):\n'
    '            lines.append(f"估值说明：{report[\'price_note\']}")\n'
    '        lines.append("以上为模拟盘估值，未提交任何真实券商订单。")\n'
    '        return "\\n".join(lines)\n'
    "\n"
)
src = src[:i0] + NEW_C + src[i1:]

# ---- D1: 估值完成后做篮子/机器拆分 ----
OLD = "        valuation_complete = not missing_codes\n"
assert src.count(OLD) == 1, f"D1: {src.count(OLD)}"
src = src.replace(OLD, (
    "        valuation_complete = not missing_codes\n"
    "        basket_report, machine_positions = self._split_basket_machine(\n"
    "            engine, positions, quote_details)\n"
), 1)

# ---- D2: 不完整分支的 positions 列表 → 仅机器 + basket 字段 ----
OLD = (
    "                    for position in positions\n"
    "                ],\n"
    "            }\n"
)
assert src.count(OLD) == 1, f"D2: {src.count(OLD)}"
src = src.replace(OLD, (
    "                    for position in machine_positions\n"
    "                ],\n"
    '                "basket": basket_report,\n'
    "            }\n"
), 1)

# ---- D3: 完整分支同样处理 ----
OLD = (
    "                for position in positions\n"
    "            ],\n"
    "        }\n"
)
assert src.count(OLD) == 1, f"D3: {src.count(OLD)}"
src = src.replace(OLD, (
    "                for position in machine_positions\n"
    "            ],\n"
    '            "basket": basket_report,\n'
    "        }\n"
), 1)

# ---- E: 15:20 未成交明细, 年线闸门关闭时跳过 ----
OLD = (
    "        report = self._build_no_buy_report(engine, trade_date, now)\n"
    '        if report["status"] != "no_buy":\n'
)
assert src.count(OLD) == 1, f"E: {src.count(OLD)}"
src = src.replace(OLD, (
    "        yearline = self._yearline_gate_state()\n"
    '        if yearline is not None and not yearline["open"]:\n'
    "            engine.dao.set_meta(key, \"skipped\")\n"
    "            return {\n"
    '                "status": "skipped",\n'
    '                "reason": "年线闸门关闭(上证未收复200日均线), 机器停开新仓, 未成交明细不推送",\n'
    '                "trade_date": trade_date,\n'
    "            }\n"
    + OLD
), 1)

open(PATH, "w", encoding="utf-8").write(src)
s = open(PATH, encoding="utf-8").read()
assert s.count("_split_basket_machine") == 2  # 定义+调用
assert '"basket": basket_report' in s
assert "未成交明细不推送" in s
assert '"prev_close": round(prev_close, 3)' in s
print("PATCH_OK")
