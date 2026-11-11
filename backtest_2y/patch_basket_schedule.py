# -*- coding: utf-8 -*-
"""篮子动态调度补丁(2026-09-05 用户拍板"切"): 闲钱全停篮子结构。

盘初(~09:30): 年线闸门开且当天机器可能买/加仓 → 按最大需求预卖篮子(超配先卖)。
尾盘(14:56): 账户闲钱全部买回篮子(低配先买), 闸门开关与否都执行(=闲钱永不趴现金)。
走 broker.execute_sell/execute_buy, side=basket_sell/basket_buy,
不进机器熔断统计(signal_msg前缀不含"止损")、不算机器买入(no_buy判断只认buy/add)。
幂等: 以已存在的 _basket_schedule_tick 方法为锚。
"""
import shutil

PATH = "/opt/quantpilot/trading_alert_notifier.py"
CFG = "/opt/quantpilot/config/config.yaml"
STAMP = "before_basket_schedule_20260905"

src = open(PATH, encoding="utf-8").read()
if "_basket_schedule_tick" in src:
    print("ALREADY_PATCHED_NOTIFIER")
else:
    shutil.copy2(PATH, f"{PATH}.{STAMP}")

    METHODS = '''
    # ---------------- 篮子动态调度: 闲钱全停篮子(2026-09-05 拍板) ----------------
    def _basket_schedule_tick(self, engine, now) -> None:
        """盘初预卖机器备用金 + 尾盘闲钱归位, 每日各一次。仅 daemon 调用。"""
        if not bool(self.alert_config.get("basket_dynamic_parking_enabled", False)):
            return
        trade_date = now.strftime("%Y-%m-%d")
        state = getattr(self, "_basket_schedule_state", None) or {}
        if state.get("date") != trade_date:
            state = {"date": trade_date, "morning": False, "close": False}
            self._basket_schedule_state = state
        if not state["morning"] and now.time() < clock_time(14, 0):
            state["morning"] = True
            self._basket_morning_presell(engine, now)
            return
        if not state["close"] and clock_time(14, 56) <= now.time() < clock_time(15, 1):
            state["close"] = True
            self._basket_close_park(engine, now)

    def _basket_legs(self, engine):
        codes = {
            c.strip().zfill(6)
            for c in str(engine.dao.get_meta("dividend_basket_codes") or "").split(",")
            if c.strip().isdigit()
        }
        positions = []
        for code in sorted(codes):
            pos = engine.account.get_position(code)
            if pos is not None and pos.quantity > 0:
                positions.append(pos)
        return codes, positions

    def _basket_live_quotes(self, engine, codes):
        poller = engine._get_screen_poller()
        quotes = poller.fetch_targeted_quotes(list(codes))
        live = {}
        for code in codes:
            quote = quotes.get(code)
            if quote is None:
                continue
            price = float(self._quote_value(quote, "price") or 0)
            if price > 0:
                engine.account.update_price(code, price)
                live[code] = (quote, price)
        return live

    def _basket_machine_positions(self, engine, basket_codes):
        out = []
        for code in engine.account.position_codes():
            raw = str(code or "").strip()
            if not raw.isdigit():
                continue
            if raw.zfill(6) in basket_codes:
                continue
            pos = engine.account.get_position(code)
            if pos is not None and pos.quantity > 0:
                out.append(pos)
        return out

    def _basket_morning_presell(self, engine, now):
        trade_date = now.strftime("%Y-%m-%d")
        try:
            codes, positions = self._basket_legs(engine)
            if not positions:
                self._record_basket_schedule(engine, trade_date, "morning",
                                             {"status": "skipped", "reason": "无篮子持仓"})
                return
            yearline = self._yearline_gate_state()
            if yearline is not None and not bool(yearline.get("open")):
                self._record_basket_schedule(engine, trade_date, "morning",
                                             {"status": "skipped",
                                              "reason": "年线闸门关闭, 机器停买, 无需预卖"})
                return
            live = self._basket_live_quotes(engine, [p.code for p in positions])
            if not live:
                self._record_basket_schedule(engine, trade_date, "morning",
                                             {"status": "skipped", "reason": "篮子行情不可用"})
                return
            basket_mv = sum(p.quantity * live[p.code][1]
                            for p in positions if p.code in live)
            machine = self._basket_machine_positions(engine, codes)
            machine_mv = sum(mp.quantity * (mp.latest_price or mp.avg_cost)
                             for mp in machine)
            eq = engine.account.total_value()
            cfg = getattr(engine, "config", None)
            max_pos = int(getattr(cfg, "max_positions", 3) or 3)
            pos_pct = float(getattr(cfg, "max_position_pct", 0.30) or 0.30)
            add_pct = float(getattr(cfg, "add_position_pct", 0.30) or 0.30)
            total_pct = float(getattr(cfg, "max_total_position_pct", 0.90) or 0.90)
            free_slots = max(0, max_pos - len(machine))
            new_need = free_slots * pos_pct * eq
            if now.weekday() == 4:  # 周五禁新仓, 只备加仓
                new_need = 0.0
            if not engine.dao.get_candidates(trade_date):
                new_need = 0.0
            add_need = machine_mv * add_pct
            cap_room = max(0.0, total_pct * eq - basket_mv - machine_mv)
            presell = min(new_need + add_need, cap_room)
            if presell < 1000.0:
                self._record_basket_schedule(engine, trade_date, "morning",
                                             {"status": "skipped",
                                              "reason": f"预卖额度太小({presell:,.0f}元)"})
                return
            broker = engine.signal_engine.broker
            sold, fills = 0.0, []
            avg = basket_mv / max(1, len(live))
            ordered = sorted((p for p in positions if p.code in live),
                             key=lambda p: -p.quantity * live[p.code][1])
            remaining = presell
            for round_i in range(4):
                progressed = False
                for p in ordered:
                    if remaining <= 500.0:
                        break
                    quote, price = live[p.code]
                    mv = p.quantity * price
                    trim = max(0.0, mv - avg) if round_i == 0 else mv * 0.5
                    want_val = min(remaining, trim)
                    lots = int(want_val / price / 100) * 100
                    lots = min(lots, int(p.available or 0))
                    if lots < 100:
                        continue
                    fill = broker.execute_sell(
                        engine.account, quote, lots, trade_date,
                        "篮子调度:盘初预卖(机器当日备用金, 超配先卖)",
                        side="basket_sell",
                    )
                    if fill.status == "filled":
                        remaining -= float(fill.total or 0.0)
                        sold += float(fill.total or 0.0)
                        fills.append(f"{p.code}x{fill.executed_qty}")
                        progressed = True
                if not progressed or remaining <= 500.0:
                    break
            note = (f"预卖目标 {presell:,.0f} 元, 实卖 {sold:,.0f} 元"
                    f"({'; '.join(fills) or '无'})")
            self._record_basket_schedule(engine, trade_date, "morning",
                                         {"status": "done", "sold": round(sold, 2),
                                          "target": round(presell, 2),
                                          "detail": note})
            logger.info("篮子盘初预卖: %s", note)
        except Exception:
            logger.exception("篮子盘初预卖失败")
            self._record_basket_schedule(engine, trade_date, "morning",
                                         {"status": "error"})

    def _basket_close_park(self, engine, now):
        trade_date = now.strftime("%Y-%m-%d")
        try:
            codes, _held = self._basket_legs(engine)
            targets = sorted(codes)
            if not targets:
                self._record_basket_schedule(engine, trade_date, "close",
                                             {"status": "skipped", "reason": "无篮子清单"})
                return
            live = self._basket_live_quotes(engine, targets)
            if not live:
                self._record_basket_schedule(engine, trade_date, "close",
                                             {"status": "skipped", "reason": "篮子行情不可用"})
                return
            min_buy = float(self.alert_config.get(
                "basket_schedule_min_buy_amount", 2000) or 2000)
            broker = engine.signal_engine.broker
            bought, fills = 0.0, []
            for round_i in range(4):
                mvs = {}
                for code in live:
                    pos = engine.account.get_position(code)
                    _q, price = live[code]
                    mvs[code] = (pos.quantity if pos else 0) * price
                avg = sum(mvs.values()) / max(1, len(mvs))
                progressed = False
                for code in sorted(mvs, key=lambda c: mvs[c]):  # 低配先买
                    quote, price = live[code]
                    deficit = max(0.0, avg * 1.02 - mvs[code])
                    affordable = broker.calc_buy_quantity(
                        engine.account.cash, price, code)
                    lots = min(int(deficit / price / 100) * 100, affordable)
                    if lots * price < min_buy:
                        continue
                    fill = broker.execute_buy(
                        engine.account, quote, lots, trade_date,
                        "篮子调度:尾盘闲钱归位(低配先买)",
                        side="basket_buy",
                    )
                    if fill.status == "filled":
                        bought += float(fill.total or 0.0)
                        fills.append(f"{code}x{fill.executed_qty}")
                        progressed = True
                if not progressed or engine.account.cash < min_buy:
                    break
            note = (f"尾盘归位买入 {bought:,.0f} 元({'; '.join(fills) or '无'}), "
                    f"剩余现金 {engine.account.cash:,.0f} 元")
            self._record_basket_schedule(engine, trade_date, "close",
                                         {"status": "done",
                                          "bought": round(bought, 2),
                                          "detail": note})
            logger.info("篮子尾盘归位: %s", note)
        except Exception:
            logger.exception("篮子尾盘归位失败")
            self._record_basket_schedule(engine, trade_date, "close",
                                         {"status": "error"})

    def _record_basket_schedule(self, engine, trade_date, phase, payload):
        key = f"basket_schedule_log_{trade_date}"
        try:
            raw = engine.dao.get_meta(key)
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        data[phase] = payload
        try:
            engine.dao.set_meta(key, json.dumps(data, ensure_ascii=False))
        except Exception:
            logger.exception("篮子调度日志写入失败")

    def _basket_schedule_summary(self, engine, trade_date):
        try:
            raw = engine.dao.get_meta(f"basket_schedule_log_{trade_date}")
            if not raw:
                return {}
            data = json.loads(raw)
        except Exception:
            return {}
        parts = []
        m = data.get("morning") or {}
        c = data.get("close") or {}
        if m.get("status") == "done":
            parts.append(f"早盘预卖 {m.get('sold', 0):,.0f} 元(机器备用金)")
        elif m.get("status") == "skipped":
            parts.append(f"早盘未预卖({m.get('reason') or '未知'})")
        if c.get("status") == "done":
            parts.append(f"尾盘归位买入 {c.get('bought', 0):,.0f} 元")
        elif c.get("status") == "skipped":
            parts.append(f"尾盘未归位({c.get('reason') or '未知'})")
        return {"text": "；".join(parts)} if parts else {}

    def run_intraday_daemon(self) -> Dict[str, Any]:'''
    ANCHOR = "    def run_intraday_daemon(self) -> Dict[str, Any]:"
    assert src.count(ANCHOR) == 1, f"daemon anchor: {src.count(ANCHOR)}"
    src = src.replace(ANCHOR, METHODS, 1)

    HOOK_OLD = (
        '                except Exception:\n'
        '                    logger.exception("盘中通知轮询失败")\n'
        '                interval = max(10, self._int_config("intraday_poll_seconds", 60))'
    )
    HOOK_NEW = (
        '                except Exception:\n'
        '                    logger.exception("盘中通知轮询失败")\n'
        '                try:\n'
        '                    self._basket_schedule_tick(engine, now)\n'
        '                except Exception:\n'
        '                    logger.exception("篮子动态调度失败")\n'
        '                interval = max(10, self._int_config("intraday_poll_seconds", 60))'
    )
    assert src.count(HOOK_OLD) == 1, f"hook anchor: {src.count(HOOK_OLD)}"
    src = src.replace(HOOK_OLD, HOOK_NEW, 1)

    BASKET_KEY_OLD = '"basket": basket_report'
    n = src.count(BASKET_KEY_OLD)
    assert n >= 1, "basket key anchor missing"
    src = src.replace(
        BASKET_KEY_OLD,
        '"basket": basket_report, "basket_schedule": self._basket_schedule_summary(engine, trade_date)',
    )

    FOOTER_OLD = (
        '        positions = report.get("positions") or []\n'
        '        if positions:\n'
        '            lines.append("—— 机器持仓 ——")'
    )
    FOOTER_NEW = (
        '        schedule = report.get("basket_schedule") or {}\n'
        '        if schedule.get("text"):\n'
        '            lines.append("—— 闲钱调度（动态停靠）——")\n'
        '            lines.append(schedule["text"])\n'
        '        positions = report.get("positions") or []\n'
        '        if positions:\n'
        '            lines.append("—— 机器持仓 ——")'
    )
    assert src.count(FOOTER_OLD) == 1, f"footer anchor: {src.count(FOOTER_OLD)}"
    src = src.replace(FOOTER_OLD, FOOTER_NEW, 1)

    open(PATH, "w", encoding="utf-8").write(src)
    s = open(PATH, encoding="utf-8").read()
    assert "_basket_schedule_tick" in s
    assert '"basket_schedule": self._basket_schedule_summary' in s
    assert "闲钱调度（动态停靠）" in s
    print("NOTIFIER_PATCH_OK")

cfg = open(CFG, encoding="utf-8").read()
if "basket_dynamic_parking_enabled" not in cfg:
    shutil.copy2(CFG, f"{CFG}.{STAMP}")
    OLD = "  intraday_poll_seconds: 60\n"
    assert cfg.count(OLD) == 1, "yaml anchor"
    cfg = cfg.replace(OLD, (
        "  intraday_poll_seconds: 60\n"
        "  # 篮子动态调度(2026-09-05 拍板): 盘初预卖机器备用金(超配先卖)+尾盘闲钱归位(低配先买)。\n"
        "  basket_dynamic_parking_enabled: true\n"
        "  basket_schedule_min_buy_amount: 2000\n"
    ), 1)
    open(CFG, "w", encoding="utf-8").write(cfg)
    assert "basket_dynamic_parking_enabled: true" in open(CFG, encoding="utf-8").read()
    print("CFG_PATCH_OK")
else:
    print("ALREADY_PATCHED_CFG")
print("PATCH_DONE")
