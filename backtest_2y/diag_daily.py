# -*- coding: utf-8 -*-
"""诊断: 日线引擎为何在重叠窗口与分钟级差 -155pp。

A) 候选池全体前向收益(outcomes.csv, 以当日开盘计)
B) 日线引擎只回放"分钟级真实入场对"(D,code) → 机械正确性校验
C) B 但成交价=开盘(无溢价)
D) 全体候选, 开盘成交, 关闭全部止损 → 看退出栈是否在放血
"""
import time
from collections import defaultdict

import numpy as np
import pandas as pd

BASE = "/opt/quantpilot"
OUT = f"{BASE}/runtime/backtest_2y"
INIT_CASH = 300000.0
COMM, COMM_MIN, STAMP, TRANSFER = 2.5e-5, 5.0, 1e-3, 1e-5
LIMIT_THR = lambda code: 0.20 if code.startswith("30") else 0.10
FEE_B = lambda a: max(a * COMM, COMM_MIN) + a * TRANSFER
FEE_S = lambda a: max(a * COMM, COMM_MIN) + a * TRANSFER + a * STAMP

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:6.1f}s] {m}", flush=True)

# ---------------- A) 候选池前向 ----------------
fw = pd.read_csv(f"{OUT}/outcomes.csv", dtype={"code": str})
g = fw.groupby("cat")[["r1", "r3", "r5"]].agg(["mean", "count"]).round(4)
log("A) 候选结局分组前向(以当日开盘计):\n" + g.to_string())
log("A2) 全体候选: r1={:.4f} r3={:.4f} r5={:.4f} (n={})".format(
    fw.r1.mean(), fw.r3.mean(), fw.r5.mean(), len(fw)))

# ---------------- 引擎(带开关) ----------------
def build(feats_path, cands_path):
    cands = pd.read_pickle(cands_path)
    feats = pd.read_pickle(feats_path)
    vc = feats["date"].value_counts()
    CAL = sorted(d for d, n in vc.items() if n >= 500)
    codes = set(cands.code.unique())
    slim = feats[feats.code.isin(codes)]
    del feats
    daily = {}
    for code, g in slim.groupby("code", sort=False):
        dl = [str(x) for x in g.date.tolist()]
        daily[str(code)] = {"o": g.open.values, "h": g.high.values,
                            "l": g.low.values, "c": g.close.values,
                            "didx": {d: i for i, d in enumerate(dl)}}
    cal_pos = {d: i for i, d in enumerate(CAL)}
    cand_map = defaultdict(list)
    for d, g in cands.sort_values(["date", "rank"]).groupby("date"):
        i = cal_pos.get(d)
        if i is None or i + 1 >= len(CAL):
            continue
        for r in g.itertuples():
            cand_map[CAL[i + 1]].append((r.code, float(r.prev_close), int(r.rank)))
    return daily, CAL, cand_map


def simulate_daily(daily, CAL, cand_map, start, end, P, premium,
                   only=None, fill_mode="premium"):
    cash = INIT_CASH
    open_pos, closed = [], []
    eq_curve = []

    def close_px(code, D):
        arr = daily.get(code)
        if arr and D in arr["didx"]:
            return float(arr["c"][arr["didx"][D]])
        return None

    for D in CAL:
        if D < start or D > end:
            continue
        is_fri = pd.Timestamp(D).dayofweek == 4
        stops_today = 0
        for p in open_pos:
            if p["qty"] <= 0 or p["edate"] == D:
                continue
            arr = daily.get(p["code"])
            if not arr or D not in arr["didx"] or p["edate"] not in arr["didx"]:
                continue
            j = arr["didx"][D]
            o, h, l, c = (float(arr["o"][j]), float(arr["h"][j]),
                          float(arr["l"][j]), float(arr["c"][j]))
            cost = p["cost"]
            p["hw"] = max(p["hw"], h)
            hd = j - arr["didx"][p["edate"]]

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
            if P["sl"]:
                line = -P["sl"]
                if P["be"] and p["hw"] / cost - 1 >= P["be"]:
                    line = 0.0
                if c / cost - 1.0 < line:
                    sell_all(c, "breakeven_stop" if line >= 0 else "stop_loss", True)
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
        still = []
        for p in open_pos:
            (still if p["qty"] > 0 else closed).append(p)
        open_pos = still
        for code, pclose, rank in cand_map.get(D, []):
            if only is not None and (D, code) not in only:
                continue
            arr = daily.get(code)
            if not arr or D not in arr["didx"]:
                continue
            if any(p["code"] == code for p in open_pos):
                continue
            o = float(arr["o"][arr["didx"][D]])
            limit_px = round(pclose * (1 + LIMIT_THR(code)), 2)
            if o >= limit_px - 0.005:
                continue
            fill = o * (1.0 + premium) if fill_mode == "premium" else o
            if pclose > 0 and fill / pclose - 1.0 > P["gain"]:
                continue
            if P["fri"] and is_fri:
                continue
            if P["halt"] and stops_today >= 2:
                continue
            if len(open_pos) >= 3:
                continue
            eq = cash + sum(p["qty"] * (close_px(p["code"], D) or p["cost"])
                            for p in open_pos)
            invested = sum(p["qty"] * (close_px(p["code"], D) or p["cost"])
                           for p in open_pos)
            room = min(0.30 * eq, 0.90 * eq - invested, cash)
            qty = int(room / (fill * (1 + COMM)) / 100) * 100
            while qty >= 100 and qty * fill + FEE_B(qty * fill) > room:
                qty -= 100
            if qty < 100:
                continue
            amt = qty * fill
            fee = FEE_B(amt)
            cash -= amt + fee
            open_pos.append({"code": code, "qty": qty, "cost": fill, "hw": fill,
                             "stage": 0, "buy_total": amt + fee, "proceeds": 0.0,
                             "edate": D, "reason": "", "xdate": None})
        eq = cash + sum(p["qty"] * (close_px(p["code"], D) or p["cost"])
                        for p in open_pos)
        eq_curve.append((D, eq))
    for p in open_pos:
        px = close_px(p["code"], end) or p["cost"]
        p["proceeds"] += p["qty"] * px - FEE_S(p["qty"] * px)
        p["qty"] = 0
        p["reason"] = p["reason"] or "eod_mark"
        p["xdate"] = end
    closed += open_pos
    eqs = pd.Series(dict(eq_curve)).sort_index()
    rets = np.array([(p["proceeds"] - p["buy_total"]) / p["buy_total"]
                     for p in closed])
    wins, losses = rets[rets > 0], rets[rets <= 0]
    return {"ret": round(float(eqs.iloc[-1] / INIT_CASH - 1), 4),
            "maxdd": round(float((eqs / eqs.cummax() - 1).min()), 4),
            "n": len(rets),
            "win": round(float((rets > 0).mean()), 4) if len(rets) else 0.0,
            "avg": round(float(rets.mean()), 4) if len(rets) else 0.0,
            "pf": round(float(wins.sum() / abs(losses.sum())), 3)
            if len(losses) and losses.sum() != 0 else 99.0}


P_BASE = dict(sl=0.05, trail=0.10, be=0.10, hard=0.08, tp1=0.15, tp2=0.30,
              pct1=0.30, pct2=0.30, maxhold=5, fri=True, halt=True, gain=0.05)
PREM = 0.0161

daily, CAL, cmap = build(f"{OUT}/features.pkl", f"{OUT}/candidates.pkl")
tr = pd.read_csv(f"{OUT}/trades_min.csv", dtype={"code": str})
minute_pairs = set(zip(tr.entry, tr.code))
log(f"分钟级真实入场对: {len(minute_pairs)}")

log("B) 只放行分钟级真实入场对, 溢价成交: " + str(simulate_daily(
    daily, CAL, cmap, "2024-09-06", "2026-09-04", P_BASE, PREM, only=minute_pairs)))
log("C) 只放行分钟级真实入场对, 开盘成交: " + str(simulate_daily(
    daily, CAL, cmap, "2024-09-06", "2026-09-04", P_BASE, 0.0,
    only=minute_pairs, fill_mode="open")))
log("D) 全体候选, 开盘成交, 关全部止损(sl=0,hard=0): " + str(simulate_daily(
    daily, CAL, cmap, "2024-09-06", "2026-09-04", dict(P_BASE, sl=0.0, hard=0.0),
    0.0, fill_mode="open")))
log("E) 全体候选, 开盘成交, 基准退出: " + str(simulate_daily(
    daily, CAL, cmap, "2024-09-06", "2026-09-04", P_BASE, 0.0, fill_mode="open")))
log("DIAG_OK")
