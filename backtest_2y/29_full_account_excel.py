# -*- coding: utf-8 -*-
"""阶段29: 部署版全账户五年回测出Excel(机器腿+红利篮子腿+调度摩擦)。

模拟=24号逻辑(闲钱停篮子+2天闸门+调度摩擦), 机器明细=28号格式。
篮子腿还原: 每年1月按22池规则选前10等权(与basket_daily.csv同规则同池),
基数=上年末账户闲钱, 买入=上年末收盘, 卖出=本年末收盘; 库为前复权,
价差收益已等效含分红(分红再投资口径); 2026按09-04收盘估值未平仓。
累积收益=全部行按时间排序逐笔累计, 终值对账部署版权益。
"""
import json
import sqlite3
import time
from collections import defaultdict

import numpy as np
import pandas as pd

BASE = "/opt/quantpilot"
OUT = f"{BASE}/runtime/backtest_2y"
import sys
INIT_CASH = float(sys.argv[1]) if len(sys.argv) > 1 else 300000.0
TAG = f"_{int(round(INIT_CASH/10000))}万" if INIT_CASH != 300000 else ""
COMM, COMM_MIN, STAMP, TRANSFER = 2.5e-5, 5.0, 1e-3, 1e-5
FRIC_OUT = STAMP + COMM + TRANSFER   # 篮子卖出腿 0.126%
FRIC_IN = COMM + TRANSFER            # 篮子买回腿 0.026%

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

# ---------------- 数据 ----------------
feats = pd.read_pickle(f"{OUT}/features_5y.pkl")
slim_all = feats[["date", "code", "open", "high", "low", "close", "prev_close",
                  "is_limit"]]
del feats
vc = slim_all["date"].value_counts()
CAL = sorted(str(d) for d, n in vc.items() if n >= 500)
POS_CAL = {d: i for i, d in enumerate(CAL)}

sh = pd.read_csv(f"{OUT}/sh_long.csv", parse_dates=["date"]).set_index("date")["sh"]
ma200 = sh.rolling(200).mean()
sh_al = sh.reindex(CAL_dates := pd.to_datetime(CAL)).ffill()
above = (sh_al > ma200.reindex(CAL_dates).ffill()).fillna(True)
bull_al = (above & above.shift(1)).fillna(False).shift(1)  # 2天确认
bull_al.iloc[0] = True
BULL = {d: bool(v) for d, v in zip(CAL, bull_al)}

_bk = pd.read_csv(f"{OUT}/basket_daily.csv", index_col=0, parse_dates=True)["basket"]
PARK_BASKET = {d: float(v) for d, v in
               zip(CAL, _bk.reindex(CAL_dates).fillna(0.0).shift(1).fillna(0))}

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
log(f"5分钟bar: {len(bars)} 只")

# 篮子成分(23号 basket_picks.json, pool_tr=22池规则每年前10)
picks_all = json.load(open(f"{OUT}/basket_picks.json"))
YEAR_PICKS = {y: [s[:6] for s in lst]
              for y, lst in picks_all["pool_tr"].items()}

# 篮子逐股价格: 上年末收盘(买入) / 本年末收盘(卖出) / 2026-09-04收盘(估值)
dec_close = {}
for py in range(2021, 2026):
    q = ("SELECT k.code, k.close FROM stock_kline k JOIN "
         "(SELECT code, MAX(date) md FROM stock_kline WHERE date LIKE ? "
         " GROUP BY code) t ON k.code=t.code AND k.date=t.md")
    dec_close[py] = {str(r[0]).zfill(6): float(r[1]) for r in
                     conn.execute(q, (f"{py}-12%",))}
last_rows = conn.execute(
    "SELECT code, close FROM stock_kline WHERE date=?", (W_END,)).fetchall()
last_close = {str(r[0]).zfill(6): float(r[1]) for r in last_rows}
conn.close()
log(f"篮子成分 {sorted(YEAR_PICKS)} / 末日收盘 {len(last_close)} 只")


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
    vol = tw = ih = 0.0
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


def t_is_tail(i):
    return T48[i] == "14:55"


cash = INIT_CASH
open_pos, closed = [], []
eq_curve, cash_curve = [], []
tot_fric = 0.0
fric_by_year = defaultdict(float)
flow_out_tot = flow_in_tot = 0.0


def mark(code, D):
    arr = daily.get(code)
    j = arr["didx"].get(D) if arr else None
    return float(arr["c"][j]) if j is not None else None


for D in CAL:
    if D < W_START or D > W_END:
        continue
    cash *= 1.0 + PARK_BASKET.get(D, 0.0)
    day_buys = day_sells = 0.0
    is_fri = pd.Timestamp(D).dayofweek == 4
    stops_today = 0
    day_list = cand_map.get(D, [])
    states = {code: {"s": None} for code, _, _ in day_list}
    pend = []
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
            eq = cash + invested
            room = min(0.30 * eq, 0.90 * eq - invested, cash)
            qty = int(room / (o * (1 + COMM)) / 100) * 100
            while qty >= 100 and qty * o + FEE_B(qty * o) > room:
                qty -= 100
            if qty < 100:
                continue
            amt = qty * o
            fee = FEE_B(amt)
            cash -= amt + fee
            day_buys += amt + fee
            open_pos.append({"code": code, "qty": qty, "qty0": qty, "cost": o,
                             "hw": o, "stage": 0, "buy_total": amt + fee,
                             "proceeds": 0.0, "edate": D, "ct": T48[i],
                             "sells": [], "xdate": None, "xt": None, "eod": False})
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
                p["xdate"], p["xt"] = D, T48[i]
                if is_stop:
                    stops_today += 1
                return a - f

            if t_is_tail(i) and P["sl"]:
                line = -P["sl"]
                if P["be"] and p["hw"] / cost - 1 >= P["be"]:
                    line = 0.0
                if c / cost - 1.0 < line:
                    nxt = db[i + 1] if i + 1 < 48 and db[i + 1] else None
                    px = nxt[0] if nxt else c
                    day_sells += sell_all(px, "breakeven_stop" if line >= 0
                                          else "stop_loss", True)
                    continue
            if hd >= P["maxhold"]:
                day_sells += sell_all(o, "max_hold")
                continue
            if p["stage"] == 0 and h >= cost * (1 + P["tp1"]):
                fill = max(o, cost * (1 + P["tp1"]))
                q = int(p["qty"] * P["pct1"]) // 100 * 100 or p["qty"]
                a = q * fill
                f = FEE_S(a)
                cash += a - f
                day_sells += a - f
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
                day_sells += a - f
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
            if fb is None:
                continue
            if confirm(db, i, pclose, fb[0], st) is not None:
                continue
            if P["fri"] and is_fri:
                continue
            if P["halt"] and stops_today >= 2:
                continue
            if not gate_ok:
                continue
            if len(open_pos) + len(pend) >= 3:
                continue
            invested = sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                           for p in open_pos)
            if 0.30 * (cash + invested) < 6000:
                continue
            pend.append([code, pclose, i + 1])

    fric = day_buys * FRIC_OUT + day_sells * FRIC_IN
    cash -= fric
    tot_fric += fric
    fric_by_year[D[:4]] += fric
    flow_out_tot += day_buys
    flow_in_tot += day_sells
    eq_curve.append((D, cash + sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                                   for p in open_pos)))
    cash_curve.append((D, cash))

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
    p["eod"] = True
closed += open_pos

eqs = pd.Series(dict(eq_curve)).sort_index()
cash_s = pd.Series(dict(cash_curve)).sort_index()
dd = float((eqs / eqs.cummax() - 1).min())
final_eq = float(eqs.iloc[-1])
total_ret = final_eq / INIT_CASH - 1
log(f"部署版总收益 {total_ret:+.1%} 回撤 {dd:.1%} 摩擦 {tot_fric:,.0f}")

# ---------------- 明细 ----------------
rows = []
for p in closed:
    pnl = p["proceeds"] - p["buy_total"]
    chain = "；".join(s[2] for s in p["sells"]) or "无"
    rows.append({
        "类型": "机器",
        "代码": p["code"],
        "股票名称": names.get(p["code"], ""),
        "买入时间": f"{p['edate']} {p['ct']}",
        "持仓数量": p["qty0"],
        "买入成交价": round(p["cost"], 3),
        "持仓成本(含费总额)": round(p["buy_total"], 2),
        "卖出数量": sum(s[3] for s in p["sells"]),
        "卖出成交价": "；".join(f"{s[4]:.3f}" for s in p["sells"]) or "—",
        "卖出时机": chain,
        "卖出时间": (f"{p['xdate']} {p['xt']}"
                    if (p["xdate"] and p["xt"] and not p["eod"])
                    else (f"{p['xdate'] or ''} 收盘估值(未平仓)" if p["eod"]
                          else str(p["xdate"]))),
        "卖出净得(含费后)": round(p["proceeds"], 2),
        "单笔收益(元)": round(pnl, 2),
        "年份": str(p["edate"])[:4],
        "_sort": f"{p['edate']} {p['ct']}",
    })

# 篮子腿: 每年换仓(基数=上年末闲钱cash), 逐股等权
cash_by_date = cash_s.to_dict()
year_first_day = {}
for D in CAL:
    y = D[:4]
    if y >= "2022" and y <= "2026" and y not in year_first_day:
        year_first_day[y] = D


def prev_year_end_cash(y):
    """y年首个换仓日的基数 = 上一年最后交易日日终cash(2022=INIT_CASH)"""
    if y == "2022":
        return INIT_CASH
    py_dates = [d for d in cash_by_date if d[:4] == str(int(y) - 1)]
    return cash_by_date[max(py_dates)] if py_dates else INIT_CASH


basket_rows = []
for y in range(2022, 2027):
    ys = str(y)
    codes = YEAR_PICKS.get(y) or YEAR_PICKS.get(ys)
    if not codes:
        log(f"warn: {y} 无篮子成分, 跳过")
        continue
    base = prev_year_end_cash(ys)
    per = base / 10.0
    d0 = year_first_day[ys]
    for code in codes:
        px_in = dec_close[y - 1].get(code)
        if px_in is None or px_in <= 0:
            log(f"warn: {code} 无{y-1}年末收盘, 跳过")
            continue
        qty = per / px_in
        cost_total = per + FEE_B(per)
        if y == 2026:
            px_out = last_close.get(code)
            note = "2026-09-04 收盘估值(未平仓)"
        else:
            px_out = dec_close[y].get(code)
            note = f"{y}-12月最后交易日收盘"
        if px_out is None or px_out <= 0:
            log(f"warn: {code} 无卖出价, 跳过")
            continue
        amt_out = qty * px_out
        proceeds = amt_out - FEE_S(amt_out)
        pnl = proceeds - cost_total
        basket_rows.append({
            "类型": "红利篮子",
            "代码": code,
            "股票名称": names.get(code, ""),
            "买入时间": f"{d0}(按{y-1}年末收盘换仓)",
            "持仓数量": round(qty, 1),
            "买入成交价": round(px_in, 3),
            "持仓成本(含费总额)": round(cost_total, 2),
            "卖出数量": round(qty, 1),
            "卖出成交价": round(px_out, 3),
            "卖出时机": "年度换仓(等权1/10,前复权口径,价差已含分红回报)",
            "卖出时间": note,
            "卖出净得(含费后)": round(proceeds, 2),
            "单笔收益(元)": round(pnl, 2),
            "年份": ys,
            "_sort": f"{d0} 00:00",
        })

# 摩擦行(年度)
for y, f in sorted(fric_by_year.items()):
    rows.append({
        "类型": "调度摩擦", "代码": "—", "股票名称": "篮子预卖/买回往返费用",
        "买入时间": f"{y}年度", "持仓数量": "—", "买入成交价": "—",
        "持仓成本(含费总额)": "—", "卖出数量": "—", "卖出成交价": "—",
        "卖出时机": "机器每买1元→篮子卖1元(0.126%);每回1元→买回(0.026%)",
        "卖出时间": f"{y}年度合计", "卖出净得(含费后)": "—",
        "单笔收益(元)": round(-f, 2), "年份": y, "_sort": f"{y}-12-31 23:00",
    })

# 篮子占用调整行: 部署版逐日篮子增益(基数=前日cash) vs 明细"年初满基数持有"之差
# 机器占用资金期间该部分钱实际不在篮子, 明细高估, 在此对齐权益口径
cash_dates = list(cash_s.index)
basket_gain_by_year = defaultdict(float)
prev_c = INIT_CASH
for D in CAL:
    if D < W_START or D > W_END:
        continue
    basket_gain_by_year[D[:4]] += prev_c * PARK_BASKET.get(D, 0.0)
    prev_c = float(cash_s[D])
basket_detail_by_year = defaultdict(float)
for r in basket_rows:
    basket_detail_by_year[r["年份"]] += r["单笔收益(元)"]
for y in sorted(basket_gain_by_year):
    adj = basket_gain_by_year[y] - basket_detail_by_year[y]
    rows.append({
        "类型": "占用调整", "代码": "—",
        "股票名称": "篮子资金占用调整(机器占用期实际未停篮子)",
        "买入时间": f"{y}年度", "持仓数量": "—", "买入成交价": "—",
        "持仓成本(含费总额)": "—", "卖出数量": "—", "卖出成交价": "—",
        "卖出时机": "对齐账户权益口径: 实际逐日篮子增益-明细满基数增益",
        "卖出时间": f"{y}年度合计", "卖出净得(含费后)": "—",
        "单笔收益(元)": round(adj, 2), "年份": y, "_sort": f"{y}-12-31 23:30",
    })

df = pd.DataFrame(rows + basket_rows).sort_values("_sort").reset_index(drop=True)
df["累积收益(元)"] = df["单笔收益(元)"].cumsum().round(2)
df["累积收益率"] = (df["累积收益(元)"] / INIT_CASH * 100).round(2).map(
    lambda v: f"{v:+.2f}%")
df["单笔收益率"] = df.apply(
    lambda r: (f"{r['单笔收益(元)'] / r['持仓成本(含费总额)'] * 100:+.2f}%"
               if isinstance(r["持仓成本(含费总额)"], (int, float))
               and r["持仓成本(含费总额)"] else "—"), axis=1)
df = df.drop(columns=["_sort"])
df.to_csv(f"{OUT}/trades_full_account{TAG}.csv", index=False, encoding="utf-8-sig")

# 对账(调整行后应严格等于权益口径)
ledger_final = float(df["累积收益(元)"].iloc[-1])
gap = final_eq - INIT_CASH - ledger_final
log(f"明细累积终值 {ledger_final:,.0f} vs 部署版实际 {final_eq-INIT_CASH:,.0f}"
    f" 差 {gap:,.2f}")
assert abs(gap) < 2.0, f"对账失败 差{gap}"

# 分年统计
def yearly_agg(sub):
    pnl = sub["单笔收益(元)"]
    n = len(sub)
    return pd.Series({
        "笔数": n,
        "胜率": f"{(pnl > 0).mean() * 100:.0f}%" if n else "—",
        "净收益": round(pnl.sum(), 0),
    })


yearly = (df[df["类型"].isin(["机器", "红利篮子"])]
          .groupby(["年份", "类型"]).apply(yearly_agg).reset_index())
yearly_pivot = yearly.pivot(index="年份", columns="类型", values="净收益").fillna(0)
yearly_pivot["篮子调度摩擦"] = [-fric_by_year.get(str(y), 0) for y in yearly_pivot.index]
yearly_pivot["年度合计"] = yearly_pivot.sum(axis=1)
yearly_pivot = yearly_pivot.reset_index()

eqt = eqs.copy()
eqt.index = pd.to_datetime(eqt.index)
prev = INIT_CASH
yr_eq = []
for y, seg in eqt.groupby(eqt.index.year):
    yr_eq.append((y, float(seg.iloc[-1] / prev - 1)))
    prev = float(seg.iloc[-1])
yearly_pivot["账户收益率(权益口径)"] = [
    f"{r*100:+.1f}%" for y, r in yr_eq if str(y) in set(yearly_pivot["年份"])]

summary = pd.DataFrame([
    ("总收益(部署版: 机器+红利篮子+闲钱调度)", f"{total_ret*100:+.1f}%"),
    ("期末权益 / 初始资金", f"{final_eq:,.0f} / {INIT_CASH:,.0f} 元"),
    ("最大回撤", f"{dd*100:.1f}%"),
    ("机器腿笔数 / 胜率", f"{(df['类型']=='机器').sum()} 笔"),
    ("篮子腿合计收益(元)", f"{df[df['类型']=='红利篮子']['单笔收益(元)'].sum():,.0f}"),
    ("机器腿合计收益(元)", f"{df[df['类型']=='机器']['单笔收益(元)'].sum():,.0f}"),
    ("调度摩擦合计(元)", f"{-tot_fric:,.0f}"),
    ("明细累积 vs 权益口径 差额(元)", f"{gap:,.0f} (篮子基数随机器进出变动所致)"),
    ("分年收益", " / ".join(f"{y}:{r*100:+.1f}%" for y, r in yr_eq)),
    ("篮子规则", "22池每年1月按近3年年均分红/上年末价前10等权; 前复权价格计算, 价差已等效含分红(分红再投资假设)"),
    ("口径4", "前复权说明: 历史价为复权价(与行情软件实时前复权差一次除权调整, 如大秦2022末5.44 vs 软件5.34), 买卖同口径不影响收益; 真实账户红利税满1年免税"),
    ("口径1", "机器: 确认后下一根5分钟K开盘成交, 佣金2.5bp(最低5元)+过户1bp, 卖出印花1‰"),
    ("口径2", "未回放: 加仓、题材热度/同题材≤2、外围闸门、止损反弹守卫"),
    ("口径3", "候选池为现存5183只, 幸存者偏差, 早年略偏乐观"),
], columns=["指标", "数值"])

xlsx = f"{OUT}/回测_服务器规则{TAG}_2022-2026.xlsx"
with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
    summary.to_excel(w, sheet_name="汇总", index=False)
    yearly_pivot.to_excel(w, sheet_name="分年统计", index=False)
    df.drop(columns=["单笔收益率"], errors="ignore").to_excel(
        w, sheet_name="交易明细(机器+篮子)", index=False)
log(f"Excel: {xlsx}")
log("SIM29_OK")
