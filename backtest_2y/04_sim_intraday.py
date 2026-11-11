# -*- coding: utf-8 -*-
"""阶段4: 分钟级组合回测(忠实回放生产逻辑)。

入场(七重确认, 逐bar回放 scheduler._open_entry_confirmation + _vwap_chase_block_reason):
  1) 买入时段窗: bar收盘时刻∈{09:45..10:30, 13:30..14:00}(含端点)
  2) 现价>=首根bar低点(不破开盘5分钟低点)
  3) 现价>=VWAP( Σ((H+L+C)/3*V)/ΣV, 当日已收盘bar )
  4) 防追高: 当日涨幅>5%禁 / 距日内高点<0.5%禁 / 乖离>2.5%当日拉黑 /
     乖离>1.5%等回踩(<=0.5%)后须重新站上>=0.2%
  5) 现价>=开盘价
  成交: 确认bar的下一根bar开盘价(生产为60s轮询后下单); 下根开盘=涨停价放弃;
  停牌顺延到下一根有bar时刻, 当日无则放弃
防护(entry_block_reason): 周五不开新仓 / 当日止损>=2次熔断 / 持仓<3只 /
  单票预算30%总资产 / 总仓位<=90%
退出(signal_engine._short_term_exit 顺序, T+1入场日不卖):
  硬止损-8%(bar low触发, fill=trig或open) → 尾盘14:55止损(14:55bar收盘判定,
  fill=15:00bar open) → 持仓>=5交易日(当日首bar open卖) → 首档+15%卖30% →
  二档+30%卖30%(bar high触发, fill=max(open,trig)) → 高点回撤10%(stage>=1,
  hw含当bar high, low触发) ; 保本线: 高点浮盈曾>=10% → 止损线-5%上移到0
不回放(需盘中实时数据): 加仓T+1门禁 / 题材热度门禁 / 题材<=2只 /
  外围科技风险门禁。现价近似=bar收盘(生产为60s轮询实时价)。
"""
import itertools
import json
import sqlite3
import time
from collections import defaultdict

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

# ---------------- 数据 ----------------
feats = pd.read_pickle(f"{OUT}/features.pkl")
slim = feats[["date", "code", "open", "high", "low", "close", "prev_close",
              "is_limit"]].copy()
del feats
cands = pd.read_pickle(f"{OUT}/candidates.pkl")

daily = {}
for code, g in slim.groupby("code", sort=False):
    dl = g.date.tolist()
    daily[code] = {"o": g.open.values, "c": g.close.values,
                   "didx": {d: i for i, d in enumerate(dl)}}
counts = slim.groupby("date").size()
CAL = sorted(d for d, n in counts.items() if n >= 500)
POS_CAL = {d: i for i, d in enumerate(CAL)}
log(f"日历 {len(CAL)} 天 {CAL[0]}→{CAL[-1]}")

cand_map = defaultdict(list)
for d, g in cands.sort_values(["date", "rank"]).groupby("date"):
    nd_i = POS_CAL.get(d)
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
        for k in range(9):
            if i0 + k < len(CAL):
                need[code].add(CAL[i0 + k])

conn = sqlite3.connect(f"file:{BASE}/runtime/data/stock_selection.db?mode=ro", uri=True)
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
log(f"5分钟bar: {len(bars)} 只 / {sum(len(v) for v in bars.values())} 天")


def first_bar(db):
    for b in db:
        if b is not None:
            return b
    return None


def confirm(db, i, prev_close, day_open, st, gain_limit=0.05):
    """七重确认: 返回None=通过, 否则拦截原因。st=防追高状态机。"""
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


def simulate(P, keep=False):
    win_set = P.get("win") or WIN
    scale_fn = P.get("scale_fn") or (lambda D: 1.0)
    gate_fn = P.get("gate")
    cash = INIT_CASH
    open_pos, closed = [], []
    outcome = {}        # (D, code) -> (category, detail, aux)
    eq_curve = []
    entry_rows = []

    def mark(code, D):
        arr = daily[code]
        j = arr["didx"].get(D)
        return float(arr["c"][j]) if j is not None else None

    for D in CAL:
        if D < "2024-09-06" or D > "2026-09-04":
            continue
        is_fri = pd.Timestamp(D).dayofweek == 4
        stops_today = 0
        day_list = cand_map.get(D, [])
        states = {}
        pend = []       # [code, prev_close, fill_idx]

        for code, pclose, rank in day_list:
            states[code] = {"s": None, "last": "无bar或未到时段"}
        db_holder = {p["code"]: bars.get(p["code"], {}).get(D)
                     for p in open_pos}

        for i in range(48):
            # ---- 1) 挂单在下一根有bar的时刻成交(停牌顺延) ----
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
                budget = 0.30 * float(scale_fn(D))
                eq = cash + sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                                for p in open_pos)
                invested = sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                               for p in open_pos)
                room = min(budget * eq, 0.90 * eq - invested, cash)
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

            # ---- 2) 退出 ----
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

            # ---- 3) 入场确认 ----
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
                day_open = fb[0] if fb else daily[code]["o"][daily[code]["didx"][D]] \
                    if D in daily[code]["didx"] else None
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
                if 0.30 * float(scale_fn(D)) * eq < 6000:
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

    last_D = "2026-09-04"
    for p in open_pos:
        arr = daily[p["code"]]
        j = arr["didx"].get(last_D) or arr["didx"][p["edate"]]
        px = float(arr["c"][j])
        p["proceeds"] += p["qty"] * px - FEE_S(p["qty"] * px)
        p["qty"] = 0
        p["reason"] = p["reason"] or "eod_mark"
        p["xdate"] = last_D
    closed += [p for p in open_pos]
    open_pos = []

    eqs = pd.Series(dict(eq_curve)).sort_index()
    dd = float((eqs / eqs.cummax() - 1).min())
    rets = np.array([(p["proceeds"] - p["buy_total"]) / p["buy_total"]
                     for p in closed])
    wins, losses = rets[rets > 0], rets[rets <= 0]
    stats = {
        "final": round(float(eqs.iloc[-1]), 0),
        "ret": round(float(eqs.iloc[-1] / INIT_CASH - 1), 4),
        "maxdd": round(dd, 4),
        "n": len(rets),
        "win": round(float((rets > 0).mean()), 4) if len(rets) else 0.0,
        "avg": round(float(rets.mean()), 4) if len(rets) else 0.0,
        "pf": round(float(wins.sum() / abs(losses.sum())), 3)
        if len(losses) and losses.sum() != 0 else 99.0,
        "avg_win": round(float(wins.mean()), 4) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 4) if len(losses) else 0.0,
    }
    eqt = eqs.copy()
    eqt.index = pd.to_datetime(eqt.index)
    prev_last = INIT_CASH
    for y, seg in eqt.groupby(eqt.index.year):
        stats[f"y{y}"] = round(float(seg.iloc[-1] / prev_last - 1), 4)
        prev_last = float(seg.iloc[-1])
    if keep:
        return stats, closed, outcome, eqs, entry_rows
    return stats


def t_is_tail(i):
    return T48[i] == "14:55"


P_BASE = dict(sl=0.05, trail=0.10, be=0.10, hard=0.08, tp1=0.15, tp2=0.30,
              pct1=0.30, pct2=0.30, maxhold=5, fri=True, halt=True)

# ---------------- 基准 ----------------
log("基准回测(生产参数+6防护)")
S, trades, outcome, eqs, entry_rows = simulate(P_BASE, keep=True)
log("基准: " + json.dumps(S, ensure_ascii=False))
trades_df = pd.DataFrame([{
    "code": p["code"], "entry": p["edate"], "exit": p["xdate"],
    "cost": p["cost"], "ct": p["ct"], "net_ret": (p["proceeds"] - p["buy_total"]) / p["buy_total"],
    "reason": p["reason"], "stage": p["stage"],
} for p in trades])
trades_df["hold_days"] = [
    daily[p["code"]]["didx"][p["xdate"]] - daily[p["code"]]["didx"][p["edate"]]
    if p["xdate"] in daily[p["code"]]["didx"] and p["edate"] in daily[p["code"]]["didx"]
    else np.nan for p in trades]
trades_df.to_csv(f"{OUT}/trades_min.csv", index=False)
eqs.rename("equity").to_csv(f"{OUT}/equity_min.csv")
log("退出原因: " + str(trades_df.reason.value_counts().to_dict()))
log(f"持仓天数均值 {trades_df.hold_days.mean():.2f}")

# ---------------- 候选结局 + 前向收益 ----------------
fwd = []
for (D, code), (cat, det, aux) in outcome.items():
    arr = daily.get(code)
    j = arr["didx"].get(D) if arr else None
    if j is None:
        continue
    o = float(arr["o"][j])
    row = {"date": D, "code": code, "cat": cat, "det": det,
           "r1": float(arr["c"][j]) / o - 1,
           "r3": float(arr["c"][min(j + 2, len(arr["c"]) - 1)]) / o - 1,
           "r5": float(arr["c"][min(j + 4, len(arr["c"]) - 1)]) / o - 1}
    fwd.append(row)
fw = pd.DataFrame(fwd)
fw.to_csv(f"{OUT}/outcomes.csv", index=False)
log("候选结局分布: " + str(fw.cat.value_counts().to_dict()))
log("未入场原因: " + str(fw[fw.cat == 'no_entry'].det.value_counts().head(12).to_dict()))

grp = fw.groupby("cat")[["r1", "r3", "r5"]].agg(["mean", "count"])
log("分组前向收益(以当日开盘计):\n" + grp.round(4).to_string())

# 分时段
tr = trades_df.copy()
tr["slot"] = np.where(tr.ct < "10:15", "早盘09:45-10:10",
                      np.where(tr.ct <= "10:30", "晚晨10:15-10:30", "午后13:30-14:00"))
slot_stats = tr.groupby("slot").net_ret.agg(["count", "mean", lambda s: (s > 0).mean()])
slot_stats.columns = ["n", "avg_ret", "win"]
log("分时段入场:\n" + slot_stats.round(4).to_string())

# 周五效应(实际入场按星期)
tr["wd"] = pd.to_datetime(tr.entry).dt.dayofweek
log("入场星期分布: " + str(tr.wd.value_counts().to_dict()))

# ---------------- 敏感性网格 ----------------
rows = []
for sl, trail, be, hard in itertools.product(
        [0.03, 0.05, 0.07], [0.08, 0.10, 0.12, 0.15], [0.0, 0.10], [0.0, 0.08]):
    P = dict(P_BASE, sl=sl, trail=trail, be=be, hard=hard)
    s = simulate(P)
    rows.append({"sl": sl, "trail": trail, "be": be, "hard": hard, **s})
grid = pd.DataFrame(rows)
grid.to_csv(f"{OUT}/grid_min.csv", index=False)
log(f"网格 {len(grid)} 组完成")

# ---------------- 防护价值(逐项关闭) ----------------
guards = {}
guards["全部开启(基准)"] = S
WIN_NARROW = WIN - {"10:15", "10:20", "10:25", "10:30"}
guards["买入窗收窄至10:10"] = simulate(dict(P_BASE, win=WIN_NARROW))
# 新政策(2026-09-05用户拍板): 硬止损并入尾盘复核(经济上≈hard=0,
# -8%线在14:55被-5%线覆盖), 防追高涨幅上限5%→7%
guards["新政策:硬止损尾盘+防追高7%"] = simulate(dict(P_BASE, hard=0.0, gain=0.07))
guards["仅防追高7%"] = simulate(dict(P_BASE, gain=0.07))
guards["仅硬止损尾盘化"] = simulate(dict(P_BASE, hard=0.0))
guards["关周五禁新仓"] = simulate(dict(P_BASE, fri=False))
guards["关止损熔断"] = simulate(dict(P_BASE, halt=False))
guards["关硬止损"] = simulate(dict(P_BASE, hard=0.0))
guards["关保本线"] = simulate(dict(P_BASE, be=0.0))
guards["全关(旧系统)"] = simulate(dict(P_BASE, fri=False, halt=False, hard=0.0, be=0.0))
gv = pd.DataFrame(guards).T
gv.to_csv(f"{OUT}/guards_value.csv")
log("防护价值:\n" + gv.to_string())

# 月度收益
eqt = eqs.copy()
eqt.index = pd.to_datetime(eqt.index)
m = eqt.resample("ME").last().pct_change()
m.iloc[0] = eqt.resample("ME").last().iloc[0] / INIT_CASH - 1
m.rename("monthly").to_csv(f"{OUT}/monthly.csv")
log("月度收益:\n" + m.round(4).to_string())

# ---------------- 温度闸门试验(针对死水行情, 以新政策为基线) ----------------
P_NEW = dict(P_BASE, hard=0.0, gain=0.07)   # 周一起生效的新政策
dlc = slim.groupby("date").is_limit.sum()
cal_pos2 = {d: i for i, d in enumerate(CAL)}
ma20 = {}
for D in CAL:
    i = cal_pos2[D]
    win_d = CAL[max(0, i - 20):i]          # 仅用D之前的数据(无未来函数)
    ma20[D] = float(dlc.reindex(win_d).fillna(0.0).mean()) if win_d else 999.0
bt_win = [ma20[d] for d in CAL if "2024-09-06" <= d <= "2026-09-04"]
log(f"20日均涨停家数(回测窗口): min={min(bt_win):.0f} 中位={np.median(bt_win):.0f} "
    f"max={max(bt_win):.0f}; 2026-02窗口均值="
    f"{np.mean([ma20[d] for d in CAL if d < '2026-03-01' and d >= '2026-02-01']):.0f}")

thermal = {}
s_new, tr_new, _, _, _ = simulate(P_NEW, keep=True)
thermal["新政策(无闸门)"] = s_new
feb_new = sum(1 for p in tr_new if p["edate"].startswith("2026-02"))
for T in (60, 70, 80):
    sv, tv, _, _, _ = simulate(
        dict(P_NEW, gate=lambda D, T=T: ma20.get(D, 999.0) >= T), keep=True)
    feb = sum(1 for p in tv if p["edate"].startswith("2026-02"))
    sv["2026-02入场"] = f"{feb}笔(基线{feb_new})"
    thermal[f"硬闸门:20日均涨停<{T}停新仓"] = sv
sv = simulate(dict(P_NEW, scale_fn=lambda D: 1.0 if ma20.get(D, 999.0) >= 70 else 0.5))
thermal["软闸门:低温日单票30%→15%"] = sv
th = pd.DataFrame(thermal).T
cols = ["ret", "maxdd", "n", "win", "pf", "y2024", "y2025", "y2026", "2026-02入场"]
th = th[[c for c in cols if c in th.columns]]
log("温度闸门试验(基线=新政策):\n" + th.to_string())
th.to_csv(f"{OUT}/thermal_gate.csv")

json.dump({"base": S,
           "reason_dist": trades_df.reason.value_counts().to_dict(),
           "outcome": fw.cat.value_counts().to_dict(),
           "no_entry": fw[fw.cat == "no_entry"].det.value_counts().to_dict(),
           "slot": slot_stats.reset_index().to_dict("records"),
           "guards": gv.reset_index().to_dict("records")},
          open(f"{OUT}/results_min.json", "w"), ensure_ascii=False, indent=2)
log("SIM_MIN_OK")
