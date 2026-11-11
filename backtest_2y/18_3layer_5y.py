# -*- coding: utf-8 -*-
"""阶段18: 部署版"先3层"结构回测——篮子固定30%长持 + 机器(年线闸门)70%上限。

与16的区别: 无闲钱停靠。期初按30%买入篮子持有到期末(按basket_daily逐日估值,
篮子本身含每年1月规则换股), 机器只用自己的现金池; 总仓位90%上限含篮子市值
(机器可用空间=90%×总权益−篮子−机器持仓), 单仓上限=30%×总权益。
"""
import json
import sqlite3
import time
from collections import defaultdict

import numpy as np
import pandas as pd

BASE = "/opt/quantpilot"
OUT = f"{BASE}/runtime/backtest_2y"
INIT_CASH = 300000.0
BASKET_FRAC = 0.30
COMM, COMM_MIN, STAMP, TRANSFER = 2.5e-5, 5.0, 1e-3, 1e-5

T48 = ([f"09:{m:02d}" for m in (35, 40, 45, 50, 55)]
       + [f"10:{m:02d}" for m in range(0, 60, 5)]
       + [f"11:{m:02d}" for m in (0, 5, 10, 15, 20, 25, 30)]
       + [f"13:{m:02d}" for m in (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)]
       + [f"14:{m:02d}" for m in range(0, 60, 5)]
       + ["15:00"])
IDX48 = {t: i for i, t in enumerate(T48)}
WIN = ({"09:45", "09:50", "09:55", "10:00", "10:05", "10:10", "10:15",
        "10:20", "10:25", "10:30", "13:30", "13:35", "13:40", "13:45",
        "13:50", "13:55", "14:00"})
LIMIT_THR = lambda code: 0.20 if code.startswith("30") else 0.10
FEE_B = lambda a: max(a * COMM, COMM_MIN) + a * TRANSFER
FEE_S = lambda a: max(a * COMM, COMM_MIN) + a * TRANSFER + a * STAMP

LABEL = {
    "stop_loss": "止损-5%线(14:55尾盘复核)",
    "breakeven_stop": "保本止损(高点浮盈曾达10%,成本价离场)",
    "max_hold": "持仓到期6天清仓",
    "trailing": "移动止盈(高点回撤10%)",
    "eod": "回测截止未平仓",
}

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)

W_START, W_END = "2022-01-01", "2026-09-04"

feats = pd.read_pickle(f"{OUT}/features_5y.pkl")
slim_all = feats[["date", "code", "open", "high", "low", "close", "prev_close",
                  "is_limit"]]
del feats
vc = slim_all["date"].value_counts()
CAL = sorted(str(d) for d, n in vc.items() if n >= 500)
POS_CAL = {d: i for i, d in enumerate(CAL)}
log(f"日历 {len(CAL)} 天")

sh = pd.read_csv(f"{OUT}/sh_long.csv", parse_dates=["date"]).set_index("date")["sh"]
ma200 = sh.rolling(200).mean()
sh_al = sh.reindex(CAL_dates := pd.to_datetime(CAL)).ffill()
bull_al = (sh_al > ma200.reindex(CAL_dates).ffill()).fillna(True).shift(1)
bull_al.iloc[0] = True
BULL = {d: bool(v) for d, v in zip(CAL, bull_al)}

_bk = pd.read_csv(f"{OUT}/basket_daily.csv", index_col=0, parse_dates=True)["basket"]
BK_RET = {d: float(v) for d, v in
          zip(CAL, _bk.reindex(CAL_dates).fillna(0.0))}

cands = pd.read_pickle(f"{OUT}/candidates_5y.pkl")
cands["date"] = cands["date"].astype(str)
cand_map = defaultdict(list)
for d, g in cands.sort_values(["date", "rank"]).groupby("date"):
    nd_i = POS_CAL.get(str(d))
    if nd_i is None or nd_i + 1 >= len(CAL):
        continue
    nd = CAL[nd_i + 1]
    for r in g.itertuples():
        cand_map[nd].append((r.code, float(r.prev_close), int(r.rank)))

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

conn = sqlite3.connect(f"file:{BASE}/runtime/data/stock_selection.db?mode=ro", uri=True)
names = {str(r[0]).zfill(6): str(r[1]) for r in
         conn.execute("SELECT code, name FROM stock_basic").fetchall()}
bars = {}
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
conn.close()
log(f"5分钟bar: {len(bars)} 只")


def first_bar(db):
    for b in db:
        if b is not None:
            return b
    return None


def confirm(db, i, prev_close, day_open, st, gain_limit=0.07):
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


P = dict(sl=0.05, trail=0.10, be=0.10, tp1=0.15, tp2=0.30, pct1=0.30,
         pct2=0.30, maxhold=6, fri=True, halt=True)

cash = INIT_CASH * (1.0 - BASKET_FRAC)   # 机器现金池 70%
bv = INIT_CASH * BASKET_FRAC             # 篮子市值 30%
open_pos, closed = [], []
eq_curve = []


def t_is_tail(i):
    return T48[i] == "14:55"


def mark(code, D):
    arr = daily.get(code)
    j = arr["didx"].get(D) if arr else None
    return float(arr["c"][j]) if j is not None else None


for D in CAL:
    if D < W_START or D > W_END:
        continue
    bv *= 1.0 + BK_RET.get(D, 0.0)   # 篮子长持逐日估值(含每年1月规则换股)
    is_fri = pd.Timestamp(D).dayofweek == 4
    stops_today = 0
    day_list = cand_map.get(D, [])
    states = {}
    pend = []
    for code, pclose, rank in day_list:
        states[code] = {"s": None, "last": "无bar或未到时段"}
    db_holder = {p["code"]: bars.get(p["code"], {}).get(D) for p in open_pos}

    for i in range(48):
        still = []
        for pe in pend:
            code, pclose, fi = pe
            if i < fi:
                still.append(pe)
                continue
            db = bars.get(code, {}).get(D)
            if db is None or db[i] is None:
                continue
            o = db[i][0]
            limit_px = round(pclose * (1 + LIMIT_THR(code)), 2)
            if o >= limit_px - 0.005:
                continue
            invested = sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                           for p in open_pos)
            eq = cash + bv + invested
            room = min(0.30 * eq, 0.90 * eq - bv - invested, cash)
            qty = int(room / (o * (1 + COMM)) / 100) * 100
            while qty >= 100 and qty * o + FEE_B(qty * o) > room:
                qty -= 100
            if qty < 100:
                continue
            amt = qty * o
            fee = FEE_B(amt)
            cash -= amt + fee
            open_pos.append({"code": code, "qty": qty, "qty0": qty,
                             "cost": o, "hw": o, "stage": 0,
                             "buy_total": amt + fee, "proceeds": 0.0,
                             "edate": D, "ct": T48[i], "sells": [],
                             "xdate": None, "xt": None, "eod": False})
            db_holder[code] = db
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

            def sell_all(px, key, is_stop=False):
                global cash, stops_today
                q_sold = p["qty"]
                a = q_sold * px
                f = FEE_S(a)
                cash += a - f
                p["proceeds"] += a - f
                p["qty"] = 0
                p["sells"].append((D, T48[i], LABEL[key], q_sold, px))
                p["xdate"] = D
                p["xt"] = T48[i]
                if is_stop:
                    stops_today += 1

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
                p["sells"].append((D, T48[i], "首档止盈+15%(卖30%)", q, fill))
            elif p["stage"] == 1 and h >= cost * (1 + P["tp2"]):
                fill = max(o, cost * (1 + P["tp2"]))
                q = int(p["qty"] * P["pct2"]) // 100 * 100 or p["qty"]
                a = q * fill
                f = FEE_S(a)
                cash += a - f
                p["proceeds"] += a - f
                p["qty"] -= q
                p["stage"] = 2
                p["sells"].append((D, T48[i], "二档止盈+30%(卖30%)", q, fill))
            if p["qty"] > 0 and p["stage"] >= 1 and P["trail"] \
                    and p["hw"] > cost and l <= p["hw"] * (1 - P["trail"]):
                trig = p["hw"] * (1 - P["trail"])
                sell_all(o if o <= trig else trig, "trailing")
                continue

        still_pos = []
        for p in open_pos:
            (still_pos if p["qty"] > 0 else closed).append(p)
        open_pos = still_pos

        if T48[i] not in WIN:
            continue
        gate_ok = BULL.get(D, True)
        for code, pclose, rank in day_list:
            if any(p["code"] == code for p in open_pos):
                continue
            if any(pe[0] == code for pe in pend):
                continue
            st = states.get(code)
            db = bars.get(code, {}).get(D)
            if st is None or db is None or db[i] is None:
                continue
            fb = first_bar(db)
            day_open = fb[0] if fb else None
            if day_open is None:
                continue
            if confirm(db, i, pclose, day_open, st) is not None:
                continue
            if P["fri"] and is_fri:
                continue
            if P["halt"] and stops_today >= 2:
                continue
            if not gate_ok:
                st["last"] = "闸门关闭"
                continue
            if len(open_pos) + len(pend) >= 3:
                continue
            invested = sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                           for p in open_pos)
            eq = cash + bv + invested
            if 0.30 * eq < 6000:
                continue
            pend.append([code, pclose, i + 1])

    eq = cash + bv + sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                         for p in open_pos)
    eq_curve.append((D, eq, bv))

for p in open_pos:
    arr = daily[p["code"]]
    j = arr["didx"].get(W_END)
    if j is None:
        j = arr["didx"][p["edate"]]
    px = float(arr["c"][j])
    p["proceeds"] += p["qty"] * px - FEE_S(p["qty"] * px)
    p["sells"].append((W_END, None, LABEL["eod"], p["qty"], px))
    p["qty"] = 0
    p["xdate"] = W_END
    p["xt"] = None
    p["eod"] = True
closed += open_pos

eqs = pd.DataFrame(eq_curve, columns=["date", "equity", "basket"]).set_index("date")
dd = float((eqs["equity"] / eqs["equity"].cummax() - 1).min())
rets = np.array([(p["proceeds"] - p["buy_total"]) / p["buy_total"] for p in closed])
wins, losses = rets[rets > 0], rets[rets <= 0]
stats = {
    "ret": round(float(eqs["equity"].iloc[-1] / INIT_CASH - 1), 4),
    "maxdd": round(dd, 4),
    "n": len(rets),
    "win": round(float((rets > 0).mean()), 4) if len(rets) else 0.0,
    "pf": round(float(wins.sum() / abs(losses.sum())), 3)
    if len(losses) and losses.sum() != 0 else 99.0,
    "final_equity": round(float(eqs["equity"].iloc[-1]), 0),
    "final_basket": round(float(eqs["basket"].iloc[-1]), 0),
    "final_machine": round(float(eqs["equity"].iloc[-1] - eqs["basket"].iloc[-1]), 0),
}
eqt = eqs.copy()
eqt.index = pd.to_datetime(eqt.index)
prev_last = INIT_CASH
for y, seg in eqt.groupby(eqt.index.year):
    stats[f"y{y}"] = round(float(seg["equity"].iloc[-1] / prev_last - 1), 4)
    prev_last = float(seg["equity"].iloc[-1])
log(json.dumps(stats, ensure_ascii=False))
eqs.to_csv(f"{OUT}/equity_3layer.csv")

rows = []
for p in closed:
    pnl = p["proceeds"] - p["buy_total"]
    chain = "；".join(s[2] for s in p["sells"]) or "无"
    rows.append({
        "代码": p["code"], "股票名称": names.get(p["code"], ""),
        "持仓数量": p["qty0"], "买入成交价": round(p["cost"], 3),
        "买入总成本(含费)": round(p["buy_total"], 2),
        "买入时间": f"{p['edate']} {p['ct']}", "卖出时机": chain,
        "卖出时间": (f"{p['xdate']} {p['xt']}" if (p["xt"] and not p["eod"])
                    else f"{p['xdate'] or ''} 未平仓(按收盘估值)" if p["eod"]
                    else str(p["xdate"])),
        "卖出净得(含费后)": round(p["proceeds"], 2),
        "累计收益(元)": round(pnl, 2),
        "累计收益率": f"{pnl / p['buy_total'] * 100:+.2f}%",
        "年份": str(p["edate"])[:4],
    })
pd.DataFrame(rows).sort_values("买入时间").to_csv(
    f"{OUT}/trades_3layer.csv", index=False, encoding="utf-8-sig")
json.dump(stats, open(f"{OUT}/results_3layer.json", "w"),
          ensure_ascii=False, indent=2)
log("SIM18_OK")
