# -*- coding: utf-8 -*-
"""阶段10: 5年(2022-2026)日线级回测引擎 + 重叠窗口校准验证。

背景: TDX 5分钟K线深度仅到2024-08, 2022~2024.08 无分钟数据。
方案: 日线级引擎回放同一套规则(七重确认近似为"开盘价×成交溢价"),
  并在 2024-09~2026-09(有分钟数据的窗口)与分钟级结果对照, 量化误差。

近似口径(相对分钟级04):
  入场: 无逐bar七重确认, 成交价=当日开盘×溢价(溢价取分钟级实际成交/开盘的中位数);
        保留 开盘涨停放弃/涨幅上限/周五/熔断/3只/30%预算/90%上限/T+1
  退出: 优先级与04一致(hard→尾盘止损(收盘价成交)→到点平仓(开盘价成交)→
        tp1/tp2(日线high触发)→回撤10%(日线low触发)), 粒度从5分钟变一天
"""
import json
import time
from collections import Counter, defaultdict

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

P_BASE = dict(sl=0.05, trail=0.10, be=0.10, hard=0.08, tp1=0.15, tp2=0.30,
              pct1=0.30, pct2=0.30, maxhold=5, fri=True, halt=True, gain=0.05)
P_NEW6 = dict(P_BASE, hard=0.0, gain=0.07, maxhold=6)


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
        daily[str(code)] = {
            "o": g.open.values, "h": g.high.values, "l": g.low.values,
            "c": g.close.values, "didx": {d: i for i, d in enumerate(dl)}}
    cal_pos = {d: i for i, d in enumerate(CAL)}
    cand_map = defaultdict(list)
    for d, g in cands.sort_values(["date", "rank"]).groupby("date"):
        i = cal_pos.get(d)
        if i is None or i + 1 >= len(CAL):
            continue
        for r in g.itertuples():
            cand_map[CAL[i + 1]].append((r.code, float(r.prev_close), int(r.rank)))
    return daily, CAL, cand_map


def simulate_daily(daily, CAL, cand_map, start, end, P, premium, keep=False):
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

        # ---- 1) 退出(优先级与生产一致) ----
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

        # ---- 2) 入场(开盘×溢价) ----
        for code, pclose, rank in cand_map.get(D, []):
            arr = daily.get(code)
            if not arr or D not in arr["didx"]:
                continue
            if any(p["code"] == code for p in open_pos):
                continue
            o = float(arr["o"][arr["didx"][D]])
            limit_px = round(pclose * (1 + LIMIT_THR(code)), 2)
            if o >= limit_px - 0.005:
                continue
            fill = o * (1.0 + premium)
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
    }
    eqt = eqs.copy()
    eqt.index = pd.to_datetime(eqt.index)
    prev_last = INIT_CASH
    for y, seg in eqt.groupby(eqt.index.year):
        stats[f"y{y}"] = round(float(seg.iloc[-1] / prev_last - 1), 4)
        prev_last = float(seg.iloc[-1])
    stats["n_by_year"] = dict(Counter(p["edate"][:4] for p in closed))
    if keep:
        return stats, closed, eqs
    return stats


# ---------------- 1) 成交溢价标定(分钟级实际成交 vs 当日开盘) ----------------
tr = pd.read_csv(f"{OUT}/trades_min.csv", dtype={"code": str})
f2 = pd.read_pickle(f"{OUT}/features.pkl")
op = f2[f2.code.isin(set(tr.code))][["date", "code", "open"]]
del f2
m = tr.merge(op, left_on=["code", "entry"], right_on=["code", "date"])
prem = m.cost / m.open - 1.0
premium = float(prem.median())
log(f"成交溢价标定: n={len(prem)} 中位数={premium:+.4f} "
    f"p25={prem.quantile(.25):+.4f} p75={prem.quantile(.75):+.4f}")

# ---------------- 2) 重叠窗口验证(2024-09-06~2026-09-04) ----------------
MINUTE_REF = {"旧政策(基准)": {"ret": 0.7272, "maxdd": -0.1864, "n": 136},
              "新政策6天": {"ret": 1.1718, "maxdd": -0.1806, "n": 135}}
daily2, CAL2, cmap2 = build(f"{OUT}/features.pkl", f"{OUT}/candidates.pkl")
log(f"2y日历 {len(CAL2)} 天, 可交易日 {len(cmap2)}")
val = {}
for name, P in (("旧政策(基准)", P_BASE), ("新政策6天", P_NEW6)):
    s = simulate_daily(daily2, CAL2, cmap2, "2024-09-06", "2026-09-04", P, premium)
    ref = MINUTE_REF[name]
    s["vs分钟级"] = f"{(s['ret'] - ref['ret']) * 100:+.1f}pp"
    val[f"{name}(日线)"] = s
    log(f"验证 {name}: 日线 {s['ret']:+.1%}/{s['maxdd']:.1%}/n={s['n']} "
        f"vs 分钟 {ref['ret']:+.1%}/{ref['maxdd']:.1%}/n={ref['n']} "
        f"差 {(s['ret'] - ref['ret']) * 100:+.1f}pp")
del daily2, cmap2

# ---------------- 3) 5年正式回测 ----------------
daily5, CAL5, cmap5 = build(f"{OUT}/features_5y.pkl", f"{OUT}/candidates_5y.pkl")
log(f"5y日历 {len(CAL5)} 天({CAL5[0]}→{CAL5[-1]}), 可交易日 {len(cmap5)}, "
    f"信号 {sum(len(v) for v in cmap5.values())}")
five = {}
for name, P in (("旧政策(5日+盘中硬止损+5%防追高)", P_BASE),
                ("新政策(6日+尾盘硬止损+7%防追高)", P_NEW6)):
    s, trades5, eqs5 = simulate_daily(daily5, CAL5, cmap5, "2022-01-01",
                                      "2026-09-04", P, premium, keep=True)
    five[name] = s
    log(f"5y {name}: " + json.dumps(s, ensure_ascii=False))
    eqs5.rename("equity").to_csv(f"{OUT}/equity_5y_{'new6' if '新' in name else 'base'}.csv")
    pd.DataFrame([{"code": p["code"], "entry": p["edate"], "exit": p["xdate"],
                   "net_ret": (p["proceeds"] - p["buy_total"]) / p["buy_total"],
                   "reason": p["reason"]} for p in trades5]
                 ).to_csv(f"{OUT}/trades_5y_{'new6' if '新' in name else 'base'}.csv",
                          index=False)

# ---------------- 4) 上证分年对照 ----------------
idx_yearly = {}
try:
    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    if api.connect("180.153.18.170", 7709, time_out=5):
        rows = {}
        for start in (0, 800, 1600):
            page = api.get_index_bars(9, 1, "999999", start, 800)
            if not page:
                break
            for b in page:
                rows[str(b["datetime"])[:10]] = float(b["close"])
        api.disconnect()
        s = pd.Series(rows).sort_index()
        base = s[s.index < "2022-01-01"]
        ylast = s[s.index >= "2021-12-01"].groupby(
            s[s.index >= "2021-12-01"].index.str[:4]).last()
        rets = ylast.pct_change()
        if len(base):
            rets.iloc[0] = ylast.iloc[0] / base.iloc[-1] - 1
        idx_yearly = {k: round(float(v), 4) for k, v in rets.items()}
        log("上证分年: " + str(idx_yearly))
except Exception as e:
    log(f"上证获取失败(不影响回测): {e}")

json.dump({"premium": {"median": premium, "p25": float(prem.quantile(.25)),
                       "p75": float(prem.quantile(.75)), "n": int(len(prem))},
           "validation": val, "five_year": five, "index_yearly": idx_yearly},
          open(f"{OUT}/results_5y.json", "w"), ensure_ascii=False, indent=2)
log("DAILY_5Y_OK")
