# -*- coding: utf-8 -*-
"""阶段2:组合模拟(日线近似) + 参数敏感性 + 分组研究。

退出检查顺序映射生产 _short_term_exit:
  硬止损(日内low触发) → 首档/二档止盈(日内high触发) → 移动止盈(日内low触发)
  → 普通/保本止损(尾盘语义=收盘判定) → 5天上限(收盘)
入场近似: T+1开盘价成交; 开盘涨幅>5%或一字涨停开 → 防追高拦截放弃;
  周五不开新仓; 当日止损≥2次熔断; 3只上限; 单笔30%总资产。
"""
import itertools
import json
import time

import numpy as np
import pandas as pd

BASE = "/opt/quantpilot"
OUT = f"{BASE}/runtime/backtest_2y"
INIT_CASH = 300000.0
COMM, COMM_MIN, STAMP, TRANSFER = 2.5e-5, 5.0, 1e-3, 1e-5

t0 = time.time()


def log(m):
    print(f"[{time.time()-t0:6.1f}s] {m}", flush=True)


feats = pd.read_pickle(f"{OUT}/features.pkl")
slim = feats[["date", "code", "open", "high", "low", "close",
              "is_limit", "row_in_stock"]].copy()
del feats
cands = pd.read_pickle(f"{OUT}/candidates.pkl")

# ---------- 每股数组 ----------
by_code = {}
for code, g in slim.groupby("code", sort=False):
    by_code[code] = {
        "dates": g.date.values,
        "o": g.open.values, "h": g.high.values,
        "l": g.low.values, "c": g.close.values,
        "didx": {d: i for i, d in enumerate(g.date.values)},
    }
counts = slim.groupby("date").size()
CALENDAR = [d for d, n in counts.items() if n >= 500]
CALENDAR.sort()
log(f"交易日历 {len(CALENDAR)} 天 ({CALENDAR[0]}→{CALENDAR[-1]})")
cand_map = {}
for d, g in cands.sort_values("rank").groupby("date"):
    cand_map[d] = list(zip(g.code, g.prev_close))
log(f"候选覆盖 {len(cand_map)} 天")

THR = lambda code: 0.20 if code.startswith("30") else 0.10


def buy_fees(amount):
    return max(amount * COMM, COMM_MIN) + amount * TRANSFER


def sell_fees(amount):
    return max(amount * COMM, COMM_MIN) + amount * TRANSFER + amount * STAMP


def simulate(sl, trail, be_trig, hard, collect=False):
    """返回 (stats dict, trades list, equity series)。"""
    cash = INIT_CASH
    positions = []           # 每个持仓dict
    trades = []
    equity_curve = []
    entry_log = []           # (date, code, gap, entered_bool, reason)
    cal_set = set(CALENDAR)
    stops_today = 0
    prev_date = None

    for D in CALENDAR:
        if D < "2024-09-06" or D > "2026-09-04":
            continue
        stops_today = 0
        exits_by_pos = {}

        # ---- 先处理退出(收盘/日内) ----
        for p in positions:
            if p["qty"] <= 0:
                continue
            arr = by_code.get(p["code"])
            i = arr["didx"].get(D) if arr else None
            if i is None:
                continue
            if i <= p["bar"]:      # T+1: 入场bar当日不可卖
                continue
            o, h, l, c = arr["o"][i], arr["h"][i], arr["l"][i], arr["c"][i]
            cost = p["cost"]
            p["hw"] = max(p["hw"], h)
            hold_bars = i - p["bar"]
            done = False

            # 1) 硬止损: 日内击穿立即清仓
            if hard > 0 and l <= cost * (1 - hard):
                trig = cost * (1 - hard)
                fill = o if o <= trig else trig
                qty, amt = p["qty"], p["qty"] * fill
                fee = sell_fees(amt)
                cash += amt - fee
                p["qty"] = 0
                p["proceeds"] += amt - fee
                p["reason"] = "hard_stop"
                p["exit_date"] = D
                stops_today += 1
                done = True
            # 2) 止盈档(日内high触发)
            if not done and p["qty"] > 0:
                if p["stage"] == 0 and h >= cost * 1.15:
                    fill = max(o, cost * 1.15)
                    q = int(p["qty"] * 0.30) // 100 * 100
                    if q <= 0:
                        q = p["qty"]
                    q = min(q, p["qty"])
                    amt = q * fill
                    fee = sell_fees(amt)
                    cash += amt - fee
                    p["qty"] -= q
                    p["proceeds"] += amt - fee
                    p["stage"] = 1
                    p["reason"] = "tp1"
                    p["exit_date"] = D
                elif p["stage"] == 1 and h >= cost * 1.30:
                    fill = max(o, cost * 1.30)
                    q = int(p["qty"] * 0.30) // 100 * 100
                    if q <= 0:
                        q = p["qty"]
                    q = min(q, p["qty"])
                    amt = q * fill
                    fee = sell_fees(amt)
                    cash += amt - fee
                    p["qty"] -= q
                    p["proceeds"] += amt - fee
                    p["stage"] = 2
                    p["reason"] = "tp2"
                    p["exit_date"] = D
            # 3) 移动止盈(首档后, 日内low触发)
            if not done and p["qty"] > 0 and p["stage"] >= 1 and trail > 0:
                trig = p["hw"] * (1 - trail)
                if l <= trig:
                    fill = o if o <= trig else trig
                    amt = p["qty"] * fill
                    fee = sell_fees(amt)
                    cash += amt - fee
                    p["qty"] = 0
                    p["proceeds"] += amt - fee
                    p["reason"] = "trailing"
                    p["exit_date"] = D
                    done = True
            # 4) 普通/保本止损(尾盘语义=收盘判定)
            if not done and p["qty"] > 0 and sl > 0:
                line = cost * (1 - sl)
                if be_trig > 0 and p["hw"] / cost - 1 >= be_trig:
                    line = cost
                if c < line:
                    amt = p["qty"] * c
                    fee = sell_fees(amt)
                    cash += amt - fee
                    p["qty"] = 0
                    p["proceeds"] += amt - fee
                    p["reason"] = "breakeven_stop" if line >= cost else "stop_loss"
                    p["exit_date"] = D
                    stops_today += 1
                    done = True
            # 5) 5天上限(收盘)
            if not done and p["qty"] > 0 and hold_bars >= 5:
                amt = p["qty"] * c
                fee = sell_fees(amt)
                cash += amt - fee
                p["qty"] = 0
                p["proceeds"] += amt - fee
                p["reason"] = "max_hold"
                p["exit_date"] = D

        positions = [p for p in positions if p["qty"] > 0]

        # ---- 再处理入场(T+1开盘) ----
        if prev_date in cand_map:
            is_friday = pd.Timestamp(D).dayofweek == 4
            for code, prev_close in cand_map[prev_date]:
                if stops_today >= 2:
                    entry_log.append((D, code, None, False, "halt"))
                    continue
                if len(positions) >= 3:
                    entry_log.append((D, code, None, False, "slots"))
                    continue
                if is_friday:
                    entry_log.append((D, code, None, False, "friday"))
                    continue
                arr = by_code.get(code)
                i = arr["didx"].get(D) if arr else None
                if i is None or i == 0:
                    entry_log.append((D, code, None, False, "suspended"))
                    continue
                o = arr["o"][i]
                gap = o / prev_close - 1
                limit_px = round(prev_close * (1 + THR(code)), 2)
                if o >= limit_px - 0.005:
                    entry_log.append((D, code, gap, False, "limit_open"))
                    continue
                if gap > 0.05:
                    entry_log.append((D, code, gap, False, "chase_block"))
                    continue
                equity = cash + sum(
                    p["qty"] * (by_code[p["code"]]["o"][by_code[p["code"]]["didx"].get(D, p["bar"])]
                                if by_code[p["code"]]["didx"].get(D) is not None
                                else by_code[p["code"]]["c"][p["bar"]])
                    for p in positions)
                target = 0.30 * equity
                qty = int(target / o / 100) * 100
                while qty > 0:
                    amt = qty * o
                    if amt + buy_fees(amt) <= min(target * 1.02, cash):
                        break
                    qty -= 100
                if qty < 100:
                    entry_log.append((D, code, gap, False, "cash"))
                    continue
                amt = qty * o
                fee = buy_fees(amt)
                cash -= amt + fee
                positions.append({
                    "code": code, "qty": qty, "cost": o,
                    "buy_total": amt + fee, "proceeds": 0.0,
                    "entry_date": D, "bar": i, "hw": o, "stage": 0,
                    "reason": "", "exit_date": None,
                })
                entry_log.append((D, code, gap, True, "entered"))
        prev_date = D

        # ---- 收盘估值 ----
        eq = cash
        for p in positions:
            arr = by_code[p["code"]]
            i = arr["didx"].get(D)
            px = arr["c"][i] if i is not None else arr["c"][p["bar"]]
            eq += p["qty"] * px
        equity_curve.append((D, eq))

    # 期末强平估值
    last_D = CALENDAR[-1]
    for p in positions:
        arr = by_code[p["code"]]
        px = arr["c"][arr["didx"].get(last_D, p["bar"])]
        p["proceeds"] += p["qty"] * px - sell_fees(p["qty"] * px)
        p["qty"] = 0
        if not p["reason"]:
            p["reason"] = "eod_mark"
            p["exit_date"] = last_D

    all_positions = [p for p in positions]  # 已清零,用于统计
    # 统计: 从 equity_curve 反推 trades —— 这里直接基于 positions dict
    # (positions 列表在循环中被过滤, 改为在退出时归档)
    return None  # 占位, 实际由 simulate2 完成


# 上面的原型有缺陷(清仓后 positions 被过滤丢失统计), 正式版在此:
def simulate2(sl, trail, be_trig, hard, keep_details=False):
    cash = INIT_CASH
    open_pos = []
    closed = []
    entry_log = []
    equity_curve = []
    stops_today = 0
    prev_date = None

    def close_out(p, fill, D, reason, count_stop=False):
        nonlocal cash, stops_today
        amt = p["qty"] * fill
        fee = sell_fees(amt)
        cash += amt - fee
        p["proceeds"] += amt - fee
        p["qty"] = 0
        p["reason"] = reason
        p["exit_date"] = D
        if count_stop:
            stops_today += 1

    for D in CALENDAR:
        if D < "2024-09-06" or D > "2026-09-04":
            prev_date = D if D in cand_map else prev_date
            continue
        stops_today = 0

        for p in open_pos:
            if p["qty"] <= 0:
                continue
            arr = by_code[p["code"]]
            i = arr["didx"].get(D)
            if i is None or i <= p["bar"]:
                continue
            o, h, l, c = arr["o"][i], arr["h"][i], arr["l"][i], arr["c"][i]
            cost = p["cost"]
            p["hw"] = max(p["hw"], h)
            hold_bars = i - p["bar"]

            if hard > 0 and l <= cost * (1 - hard):
                trig = cost * (1 - hard)
                close_out(p, o if o <= trig else trig, D, "hard_stop", True)
                continue
            if p["stage"] == 0 and h >= cost * 1.15:
                fill = max(o, cost * 1.15)
                q = min(max(int(p["qty"] * 0.30) // 100 * 100, p["qty"]), p["qty"]) \
                    if int(p["qty"] * 0.30) // 100 * 100 <= 0 else min(int(p["qty"] * 0.30) // 100 * 100, p["qty"])
                amt, fee = q * fill, sell_fees(q * fill)
                cash += amt - fee
                p["qty"] -= q
                p["proceeds"] += amt - fee
                p["stage"], p["reason"], p["exit_date"] = 1, "tp1", D
            elif p["stage"] == 1 and h >= cost * 1.30:
                fill = max(o, cost * 1.30)
                q0 = int(p["qty"] * 0.30) // 100 * 100
                q = p["qty"] if q0 <= 0 else min(q0, p["qty"])
                amt, fee = q * fill, sell_fees(q * fill)
                cash += amt - fee
                p["qty"] -= q
                p["proceeds"] += amt - fee
                p["stage"], p["reason"], p["exit_date"] = 2, "tp2", D
            if p["qty"] > 0 and p["stage"] >= 1 and trail > 0:
                trig = p["hw"] * (1 - trail)
                if l <= trig:
                    close_out(p, o if o <= trig else trig, D, "trailing")
                    continue
            if p["qty"] > 0 and sl > 0:
                line = cost * (1 - sl)
                if be_trig > 0 and p["hw"] / cost - 1 >= be_trig:
                    line = cost
                if c < line:
                    close_out(p, c, D,
                              "breakeven_stop" if line >= cost else "stop_loss", True)
                    continue
            if p["qty"] > 0 and hold_bars >= 5:
                close_out(p, c, D, "max_hold")

        still = []
        for p in open_pos:
            (still if p["qty"] > 0 else closed).append(p)
        open_pos = still

        if prev_date in cand_map:
            is_friday = pd.Timestamp(D).dayofweek == 4
            for code, prev_close in cand_map[prev_date]:
                reason = None
                arr = by_code.get(code)
                i = arr["didx"].get(D) if arr else None
                gap = None
                if stops_today >= 2:
                    reason = "halt"
                elif len(open_pos) >= 3:
                    reason = "slots"
                elif is_friday:
                    reason = "friday"
                elif i is None or i == 0:
                    reason = "suspended"
                if reason is None:
                    o = arr["o"][i]
                    gap = o / prev_close - 1
                    limit_px = round(prev_close * (1 + THR(code)), 2)
                    if o >= limit_px - 0.005:
                        reason = "limit_open"
                    elif gap > 0.05:
                        reason = "chase_block"
                if reason is None:
                    equity = cash
                    for p in open_pos:
                        a = by_code[p["code"]]
                        j = a["didx"].get(D)
                        equity += p["qty"] * (a["o"][j] if j is not None
                                              else a["c"][p["bar"]])
                    target = 0.30 * equity
                    o = arr["o"][i]
                    qty = int(target / o / 100) * 100
                    while qty > 0:
                        amt = qty * o
                        if amt + buy_fees(amt) <= min(target * 1.02, cash):
                            break
                        qty -= 100
                    if qty < 100:
                        reason = "cash"
                    else:
                        amt = qty * o
                        fee = buy_fees(amt)
                        cash -= amt + fee
                        open_pos.append({
                            "code": code, "qty": qty, "cost": o,
                            "buy_total": amt + fee, "proceeds": 0.0,
                            "entry_date": D, "bar": i, "hw": o,
                            "stage": 0, "reason": "", "exit_date": None,
                        })
                        reason = "entered"
                entry_log.append((D, code, gap, reason == "entered", reason))
        prev_date = D if D in cand_map or True else prev_date

        eq = cash
        for p in open_pos:
            a = by_code[p["code"]]
            j = a["didx"].get(D)
            eq += p["qty"] * (a["c"][j] if j is not None else a["c"][p["bar"]])
        equity_curve.append((D, eq))

    last_D = CALENDAR[-1]
    for p in open_pos:
        a = by_code[p["code"]]
        px = a["c"][a["didx"].get(last_D, p["bar"])]
        p["proceeds"] += p["qty"] * px - sell_fees(p["qty"] * px)
        p["qty"] = 0
        p["reason"] = p["reason"] or "eod_mark"
        p["exit_date"] = last_D
        closed.append(p)

    eq = pd.Series(dict(equity_curve)).sort_index()
    dd = (eq / eq.cummax() - 1).min()
    rets = []
    for p in closed:
        net = p["proceeds"] - p["buy_total"]
        rets.append(net / p["buy_total"])
    rets = np.array(rets)
    wins, losses = rets[rets > 0], rets[rets <= 0]
    stats = {
        "final_equity": float(eq.iloc[-1]),
        "total_return": float(eq.iloc[-1] / INIT_CASH - 1),
        "max_drawdown": float(dd),
        "n_trades": len(rets),
        "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
        "avg_ret": float(rets.mean()) if len(rets) else 0.0,
        "median_ret": float(np.median(rets)) if len(rets) else 0.0,
        "profit_factor": float(wins.sum() / abs(losses.sum()))
        if len(losses) and losses.sum() != 0 else float("inf"),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
    }
    if keep_details:
        return stats, closed, list(entry_log), eq
    return stats


log("开始基准回测(当前生产参数)")
base_stats, base_trades, base_entries, base_eq = simulate2(
    sl=0.05, trail=0.10, be_trig=0.10, hard=0.08, keep_details=True)
log(f"基准: {json.dumps(base_stats, ensure_ascii=False)}")

trades_df = pd.DataFrame([{
    "code": p["code"], "entry_date": p["entry_date"],
    "exit_date": p["exit_date"], "cost": p["cost"],
    "net_ret": (p["proceeds"] - p["buy_total"]) / p["buy_total"],
    "reason": p["reason"],
    "hold_bars": None,
} for p in base_trades])
arr_hold = []
for p in base_trades:
    a = by_code[p["code"]]
    i0 = p["bar"]
    i1 = a["didx"].get(p["exit_date"], i0)
    arr_hold.append(i1 - i0)
trades_df["hold_bars"] = arr_hold
trades_df.to_csv(f"{OUT}/trades_base.csv", index=False)
base_eq.rename("equity").to_csv(f"{OUT}/equity_base.csv")

log("退出原因分布:")
log(str(trades_df.reason.value_counts().to_dict()))
log(f"平均持仓bar数: {trades_df.hold_bars.mean():.1f}")

# ---------- 敏感性网格 ----------
rows = []
for sl, trail, be, hard in itertools.product(
        [0.03, 0.05, 0.07], [0.08, 0.10, 0.12, 0.15], [0.0, 0.10], [0.0, 0.08]):
    s = simulate2(sl, trail, be, hard)
    rows.append({"stop_loss": sl, "trailing": trail,
                 "breakeven": be, "hard": hard, **s})
grid = pd.DataFrame(rows)
grid.to_csv(f"{OUT}/grid.csv", index=False)
log(f"网格完成 {len(grid)} 组")

# ---------- 分组研究 ----------
ent = pd.DataFrame(base_entries, columns=["date", "code", "gap", "entered", "reason"])
log("入场拦截原因分布: " + str(ent.reason.value_counts().to_dict()))

# 防追高分组: 前向收益
rows2 = []
for D, code, gap, entered, reason in base_entries:
    if gap is None:
        continue
    a = by_code.get(code)
    i = a["didx"].get(D) if a else None
    if i is None:
        continue
    o = a["o"][i]
    r1 = a["c"][i] / o - 1 if i < len(a["c"]) else np.nan
    r3 = a["c"][min(i + 2, len(a["c"]) - 1)] / o - 1
    rows2.append({"date": D, "code": code, "gap": gap, "entered": entered,
                  "reason": reason, "r1": r1, "r3": r3})
coh = pd.DataFrame(rows2)
grp = {}
for name, g in coh.groupby("reason"):
    grp[name] = {
        "n": len(g), "r1_mean": round(float(g.r1.mean()), 4),
        "r3_mean": round(float(g.r3.mean()), 4),
        "r3_win": round(float((g.r3 > 0).mean()), 4),
    }
log("防追高分组: " + json.dumps(grp, ensure_ascii=False))

# 周五效应
tr = trades_df.copy()
tr["weekday"] = pd.to_datetime(tr.entry_date).dt.dayofweek
fri = tr[tr.weekday == 4]
oth = tr[tr.weekday != 4]
log(f"周五入场: n={len(fri)} 均收益={fri.net_ret.mean() if len(fri) else 0:.4f} | "
    f"非周五: n={len(oth)} 均收益={oth.net_ret.mean():.4f}")

json.dump({"base": base_stats, "cohorts": grp,
           "friday": {"n": int(len(fri)),
                      "ret": float(fri.net_ret.mean()) if len(fri) else 0.0,
                      "other_ret": float(oth.net_ret.mean())},
           "reason_dist": trades_df.reason.value_counts().to_dict(),
           "entry_block": ent.reason.value_counts().to_dict()},
          open(f"{OUT}/results.json", "w"), ensure_ascii=False, indent=2)
log("SIM_OK")
