# -*- coding: utf-8 -*-
"""阶段4b: 2022-2026 五年分钟级组合回测(真5分钟bar回放)。

引擎与 04_sim_intraday.py 逐行一致(七重确认+6防护+退出栈),
数据: features_5y/candidates_5y + 库内5分钟bar(2024-09后=TDX源,
2021-12~2024-08 = baostock源[由本地Mac拉取导入])。
跑两套参数: 旧政策基准 / 周一新政策(尾盘硬止损+7%防追高+6天)。
"""
import json
import sqlite3
import time
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

BASE = "/opt/quantpilot"
OUT = f"{BASE}/runtime/backtest_2y"
INIT_CASH = 300000.0
COMM, COMM_MIN, STAMP, TRANSFER = 2.5e-5, 5.0, 1e-3, 1e-5

T48 = ([f"09:{m:02d}" for m in (35, 40, 45, 50, 55)]
       + [f"10:{m:02d}" for m in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)]
       + [f"11:{m:02d}" for m in (0, 5, 10, 15, 20, 25, 30)]
       + [f"13:{m:02d}" for m in (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)]
       + [f"14:{m:02d}" for m in (0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)]
       + ["15:00"])
IDX48 = {t: i for i, t in enumerate(T48)}
WIN = ({"09:45", "09:50", "09:55", "10:00", "10:05", "10:10", "10:15",
        "10:20", "10:25", "10:30", "13:30", "13:35", "13:40", "13:45",
        "13:50", "13:55", "14:00"})
LIMIT_THR = lambda code: 0.20 if code.startswith("30") else 0.10
FEE_B = lambda a: max(a * COMM, COMM_MIN) + a * TRANSFER
FEE_S = lambda a: max(a * COMM, COMM_MIN) + a * TRANSFER + a * STAMP

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)

W_START, W_END = "2022-01-01", "2026-09-04"

# ---------------- 数据 ----------------
feats = pd.read_pickle(f"{OUT}/features_5y.pkl")
slim_all = feats[["date", "code", "open", "high", "low", "close", "prev_close",
                  "is_limit"]]
del feats
vc = slim_all["date"].value_counts()
CAL = sorted(str(d) for d, n in vc.items() if n >= 500)
POS_CAL = {d: i for i, d in enumerate(CAL)}
log(f"日历 {len(CAL)} 天 {CAL[0]}→{CAL[-1]}")

cands = pd.read_pickle(f"{OUT}/candidates_5y.pkl")
cand_map = defaultdict(list)
for d, g in cands.sort_values(["date", "rank"]).groupby("date"):
    nd_i = POS_CAL.get(str(d))
    if nd_i is None or nd_i + 1 >= len(CAL):
        continue
    nd = CAL[nd_i + 1]
    for r in g.itertuples():
        cand_map[nd].append((r.code, float(r.prev_close), int(r.rank)))
log(f"可交易日 {len(cand_map)}, 信号 {sum(len(v) for v in cand_map.values())}")

need = defaultdict(set)
for nd, lst in cand_map.items():
    i0 = POS_CAL[nd]
    for code, _, _ in lst:
        for k in range(10):
            if i0 + k < len(CAL):
                need[code].add(CAL[i0 + k])

slim = slim_all[slim_all.code.isin(set(need.keys()))]
del slim_all
import gc
gc.collect()
daily = {}
for code, g in slim.groupby("code", sort=False):
    dl = [str(x) for x in g.date.tolist()]
    daily[str(code)] = {"o": g.open.values, "c": g.close.values,
                        "didx": {d: i for i, d in enumerate(dl)}}
del slim
gc.collect()
log(f"daily覆盖 {len(daily)} 只候选股")

conn = sqlite3.connect(f"file:{BASE}/runtime/data/stock_selection.db?mode=ro", uri=True)
bars = {}
miss_days = 0
for code, dates in need.items():
    per = defaultdict(dict)
    dl = sorted(dates)
    for i in range(0, len(dl), 80):
        chunk = dl[i:i + 80]
        q = ("SELECT substr(timestamp,1,10) d, substr(timestamp,12,5) t, "
             "open, high, low, close, volume FROM stock_intraday_kline "
             "WHERE interval='5' AND code=? AND substr(timestamp,1,10) IN (%s)"
             % ",".join("?" * len(chunk)))
        for d, t, o, h, l, c, v in conn.execute(q, [code] + chunk):
            j = IDX48.get(t)
            if j is not None:
                per[d][j] = (o, h, l, c, v)
    bars[code] = {d: [bd.get(j) for j in range(48)] for d, bd in per.items()}
    miss_days += len(need[code]) - len(per)
conn.close()
log(f"5分钟bar: {len(bars)} 只 / {sum(len(v) for v in bars.values())} 天, "
    f"缺bar天数 {miss_days}")


def first_bar(db):
    for b in db:
        if b is not None:
            return b
    return None


def confirm(db, i, prev_close, day_open, st, gain_limit=0.05):
    cur = db[i][3]
    fb = first_bar(db[: i + 1])
    if fb is None or cur < fb[2]:
        return "破开盘5分钟低点"
    vol = 0.0
    tw = 0.0
    ih = 0.0
    for b in db[: i + 1]:
        if b is None:
            continue
        vol += b[4]
        tw += (b[1] + b[2] + b[3]) / 3.0 * b[4]
        ih = max(ih, b[1])
    if vol <= 0:
        return "VWAP无效"
    vwap = tw / vol
    if cur < vwap:
        return "低于VWAP"
    if prev_close > 0 and cur / prev_close - 1.0 > gain_limit:
        return f"当日涨幅>{gain_limit:.0%}"
    if ih > 0 and (ih - cur) / ih < 0.005:
        return "距日内高点<0.5%"
    dev = cur / vwap - 1.0
    if st["s"] == "blocked":
        return "乖离过大已拉黑"
    if dev > 0.025:
        st["s"] = "blocked"
        return "乖离>2.5%拉黑"
    if dev > 0.015:
        st["s"] = "waiting_pullback"
        return "乖离>1.5%等回踩"
    if st["s"] == "waiting_pullback":
        if dev <= 0.005:
            st["s"] = "reclaim_ready"
            return "已回踩待重新站上"
        return "等待回踩VWAP"
    if st["s"] == "reclaim_ready" and dev < 0.002:
        return "待站上VWAP0.2%"
    if cur < day_open:
        return "低于开盘价"
    return None


def t_is_tail(i):
    return T48[i] == "14:55"


def simulate(P, keep=False):
    win_set = P.get("win") or WIN
    gate_fn = P.get("gate")
    cash = INIT_CASH
    open_pos, closed = [], []
    outcome = {}
    eq_curve = []

    def mark(code, D):
        arr = daily.get(code)
        j = arr["didx"].get(D) if arr else None
        return float(arr["c"][j]) if j is not None else None

    for D in CAL:
        if D < W_START or D > W_END:
            continue
        is_fri = pd.Timestamp(D).dayofweek == 4
        stops_today = 0
        day_list = cand_map.get(D, [])
        states = {}
        pend = []

        for code, pclose, rank in day_list:
            states[code] = {"s": None, "last": "无bar或未到时段"}
        db_holder = {p["code"]: bars.get(p["code"], {}).get(D)
                     for p in open_pos}

        for i in range(48):
            still = []
            for pe in pend:
                code, pclose, fi = pe
                if i < fi:
                    still.append(pe)
                    continue
                db = bars.get(code, {}).get(D)
                if db is None:
                    outcome[(D, code)] = ("fill_fail", "停牌无bar", None)
                    continue
                if db[i] is None:
                    if i < 47:
                        still.append(pe)
                    else:
                        outcome[(D, code)] = ("fill_fail", "日内无bar", None)
                    continue
                o = db[i][0]
                limit_px = round(pclose * (1 + LIMIT_THR(code)), 2)
                if o >= limit_px - 0.005:
                    outcome[(D, code)] = ("fill_fail", "开盘涨停", None)
                    continue
                eq = cash + sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                                for p in open_pos)
                invested = sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                               for p in open_pos)
                room = min(0.30 * eq, 0.90 * eq - invested, cash)
                qty = int(room / (o * (1 + COMM)) / 100) * 100
                while qty >= 100 and qty * o + FEE_B(qty * o) > room:
                    qty -= 100
                if qty < 100:
                    outcome[(D, code)] = ("blocked", "资金不足", None)
                    continue
                amt = qty * o
                fee = FEE_B(amt)
                cash -= amt + fee
                open_pos.append({"code": code, "qty": qty, "cost": o, "hw": o,
                                 "stage": 0, "buy_total": amt + fee,
                                 "proceeds": 0.0, "edate": D, "ct": T48[i],
                                 "reason": "", "xdate": None})
                db_holder[code] = db
                outcome[(D, code)] = ("entered", T48[i], o)
            pend = still

            for p in open_pos:
                if p["qty"] <= 0 or p["edate"] == D:
                    continue
                db = db_holder.get(p["code"])
                if not db or db[i] is None:
                    continue
                o, h, l, c, v = db[i]
                cost = p["cost"]
                p["hw"] = max(p["hw"], h)
                didx = daily[p["code"]]["didx"]
                if D not in didx or p["edate"] not in didx:
                    continue
                hd = didx[D] - didx[p["edate"]]

                def sell_all(px, reason, is_stop=False):
                    nonlocal cash, stops_today
                    a = p["qty"] * px
                    f = FEE_S(a)
                    cash += a - f
                    p["proceeds"] += a - f
                    p["qty"] = 0
                    p["reason"] = reason
                    p["xdate"] = D
                    if is_stop:
                        stops_today += 1

                if P["hard"] and l <= cost * (1 - P["hard"]):
                    trig = cost * (1 - P["hard"])
                    sell_all(o if o <= trig else trig, "hard_stop", True)
                    continue
                if t_is_tail(i) and P["sl"]:
                    line = -P["sl"]
                    if P["be"] and p["hw"] / cost - 1 >= P["be"]:
                        line = 0.0
                    if c / cost - 1.0 < line:
                        nxt = db[i + 1] if i + 1 < 48 and db[i + 1] else None
                        px = nxt[0] if nxt else c
                        sell_all(px, "breakeven_stop" if line >= 0 else "stop_loss", True)
                        continue
                if hd >= P["maxhold"]:
                    sell_all(o, "max_hold")
                    continue
                if p["stage"] == 0 and h >= cost * (1 + P["tp1"]):
                    fill = max(o, cost * (1 + P["tp1"]))
                    q = int(p["qty"] * P["pct1"]) // 100 * 100 or p["qty"]
                    a = q * fill
                    f = FEE_S(a)
                    cash += a - f
                    p["proceeds"] += a - f
                    p["qty"] -= q
                    p["stage"] = 1
                elif p["stage"] == 1 and h >= cost * (1 + P["tp2"]):
                    fill = max(o, cost * (1 + P["tp2"]))
                    q = int(p["qty"] * P["pct2"]) // 100 * 100 or p["qty"]
                    a = q * fill
                    f = FEE_S(a)
                    cash += a - f
                    p["proceeds"] += a - f
                    p["qty"] -= q
                    p["stage"] = 2
                if p["qty"] > 0 and p["stage"] >= 1 and P["trail"] \
                        and p["hw"] > cost and l <= p["hw"] * (1 - P["trail"]):
                    trig = p["hw"] * (1 - P["trail"])
                    sell_all(o if o <= trig else trig, "trailing")
                    continue

            still_pos = []
            for p in open_pos:
                (still_pos if p["qty"] > 0 else closed).append(p)
            open_pos = still_pos

            if T48[i] not in win_set:
                continue
            for code, pclose, rank in day_list:
                if (D, code) in outcome:
                    continue
                if any(p["code"] == code for p in open_pos):
                    continue
                if any(pe[0] == code for pe in pend):
                    continue
                st = states.get(code)
                db = bars.get(code, {}).get(D)
                if st is None:
                    continue
                if db is None:
                    st["last"] = "停牌无bar"
                    continue
                if db[i] is None:
                    continue
                fb = first_bar(db)
                day_open = fb[0] if fb else None
                if day_open is None:
                    continue
                reason = confirm(db, i, pclose, day_open, st, P.get("gain", 0.05))
                if reason is not None:
                    st["last"] = reason
                    continue
                if P["fri"] and is_fri:
                    st["last"] = "周五禁新仓"
                    continue
                if P["halt"] and stops_today >= 2:
                    st["last"] = "当日止损熔断"
                    continue
                if gate_fn is not None and not gate_fn(D):
                    st["last"] = "温度闸门(死水行情停新仓)"
                    continue
                if len(open_pos) + len(pend) >= 3:
                    st["last"] = "仓位已满3只"
                    continue
                eq = cash + sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                                for p in open_pos)
                if 0.30 * eq < 6000:
                    st["last"] = "资金不足"
                    continue
                pend.append([code, pclose, i + 1])
                st["last"] = "已确认待成交"

        for code, pclose, rank in day_list:
            if (D, code) not in outcome:
                outcome[(D, code)] = ("no_entry", states[code]["last"], None)
        eq = cash + sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                        for p in open_pos)
        eq_curve.append((D, eq))

    for p in open_pos:
        arr = daily[p["code"]]
        j = arr["didx"].get(W_END) or arr["didx"][p["edate"]]
        px = float(arr["c"][j])
        p["proceeds"] += p["qty"] * px - FEE_S(p["qty"] * px)
        p["qty"] = 0
        p["reason"] = p["reason"] or "eod_mark"
        p["xdate"] = W_END
    closed += open_pos

    eqs = pd.Series(dict(eq_curve)).sort_index()
    dd = float((eqs / eqs.cummax() - 1).min())
    rets = np.array([(p["proceeds"] - p["buy_total"]) / p["buy_total"]
                     for p in closed])
    wins, losses = rets[rets > 0], rets[rets <= 0]
    stats = {
        "ret": round(float(eqs.iloc[-1] / INIT_CASH - 1), 4),
        "maxdd": round(dd, 4),
        "n": len(rets),
        "win": round(float((rets > 0).mean()), 4) if len(rets) else 0.0,
        "pf": round(float(wins.sum() / abs(losses.sum())), 3)
        if len(losses) and losses.sum() != 0 else 99.0,
        "n_by_year": dict(Counter(p["edate"][:4] for p in closed)),
    }
    eqt = eqs.copy()
    eqt.index = pd.to_datetime(eqt.index)
    prev_last = INIT_CASH
    for y, seg in eqt.groupby(eqt.index.year):
        stats[f"y{y}"] = round(float(seg.iloc[-1] / prev_last - 1), 4)
        prev_last = float(seg.iloc[-1])
    if keep:
        return stats, closed, outcome, eqs
    return stats


P_BASE = dict(sl=0.05, trail=0.10, be=0.10, hard=0.08, tp1=0.15, tp2=0.30,
              pct1=0.30, pct2=0.30, maxhold=5, fri=True, halt=True)
P_NEW6 = dict(P_BASE, hard=0.0, gain=0.07, maxhold=6)

IDX_YEARLY = {"2022": -0.1513, "2023": -0.0370, "2024": 0.1267,
              "2025": 0.1841, "2026": -0.0098}

res = {}
for name, P, tag in (("旧政策(5天+盘中硬止损+5%防追高)", P_BASE, "base"),
                     ("新政策(6天+尾盘硬止损+7%防追高)", P_NEW6, "new6")):
    s, trades, outcome, eqs = simulate(P, keep=True)
    res[name] = s
    log(f"{name}: " + json.dumps(s, ensure_ascii=False))
    reasons = pd.Series([p["reason"] for p in trades]).value_counts()
    log(f"  退出原因: {reasons.to_dict()}")
    oc = pd.Series([v[0] for v in outcome.values()]).value_counts()
    log(f"  候选结局: {oc.to_dict()}")
    no_entry = pd.Series([v[1] for v in outcome.values() if v[0] == "no_entry"]) \
        .value_counts().head(8)
    log(f"  未入场原因: {no_entry.to_dict()}")
    eqs.rename("equity").to_csv(f"{OUT}/equity_5ymin_{tag}.csv")
    pd.DataFrame([{"code": p["code"], "entry": p["edate"], "exit": p["xdate"],
                   "ct": p["ct"], "net_ret": (p["proceeds"] - p["buy_total"]) / p["buy_total"],
                   "reason": p["reason"]} for p in trades]
                 ).to_csv(f"{OUT}/trades_5ymin_{tag}.csv", index=False)
    if tag == "new6":
        by_year = pd.Series([(p["edate"][:4],
                              (p["proceeds"] - p["buy_total"]) / p["buy_total"])
                             for p in trades])
        yr = by_year.reset_index().groupby(0)[1].agg(["count", "mean"])
        log("  新政策分年笔均:\n" + yr.round(4).to_string())

log("上证分年对照: " + str(IDX_YEARLY))
json.dump(res, open(f"{OUT}/results_5ymin.json", "w"), ensure_ascii=False, indent=2)
log("SIM_5Y_OK")
