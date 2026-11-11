# -*- coding: utf-8 -*-
"""阶段12: 5年"稳定化"混配分析。

策略腿 = equity_5ymin_new6.csv(新政策5年分钟级回测日曲线)
慢腿候选: 国证2000指数(baostock) / 黄金ETF518880(新浪) / 中证红利价格指数(baostock)
混配方式: 每日再平衡 + 季度再平衡两种口径。
"""
import json
import os
import re
import subprocess

import baostock as bs
import numpy as np
import pandas as pd

BASE = "./backtest_2y"
S, E = "2021-12-01", "2026-09-05"
CACHE = f"{BASE}/legs_5y.csv"

# ---------- 拉腿 ----------
if os.path.exists(CACHE):
    legs = pd.read_csv(CACHE, index_col=0, parse_dates=True)
else:
    frames = {}
    bs.login()
    for code, name in (("sz.399303", "gz2000"), ("sh.000922", "dividend"),
                       ("sh.000001", "shanghai")):
        rs = bs.query_history_k_data_plus(
            code, "date,close", start_date=S, end_date=E,
            frequency="d", adjustflag="3")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        frames[name] = pd.Series(
            {d: float(c) for d, c in rows}, name=name)
    bs.logout()

    # 黄金ETF 518880 走新浪
    out = subprocess.run(
        ["curl", "-s", "--max-time", "30",
         "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/"
         "CN_MarketDataService.getKLineData?symbol=sh518880&scale=240"
         "&ma=no&datalen=1250"], capture_output=True, text=True).stdout
    m = re.search(r"var _=\((.*)\);", out, re.S)
    assert m, "新浪黄金ETF解析失败"
    data = json.loads(m.group(1))
    frames["gold"] = pd.Series(
        {r["day"]: float(r["close"]) for r in data}, name="gold")

    legs = pd.DataFrame(frames)
    legs.index = pd.to_datetime(legs.index)
    legs = legs.sort_index().loc["2021-12-30":]
    legs.to_csv(CACHE)

legs = legs.dropna()
print(f"腿数据 {legs.index[0].date()} → {legs.index[-1].date()}, "
      f"{len(legs)} 天: {list(legs.columns)}")

# ---------- 策略腿 ----------
eq = pd.read_csv(f"{BASE}/equity_5ymin_new6.csv", index_col=0).iloc[:, 0]
eq.index = pd.to_datetime(eq.index)
r_strat = eq.pct_change().fillna(0)

# 对齐(同一交易日历)
idx = r_strat.index
legs_al = legs.reindex(idx).ffill()
print("对齐后缺失天数:", int(legs_al.isna().sum().sum()))

rets = pd.DataFrame({"strat": r_strat.values}, index=idx)
for c in ("gz2000", "gold", "dividend", "shanghai"):
    rets[c] = legs_al[c].pct_change().fillna(0).values

print("\n各腿单独 2022→2026-09-04:")
solo = (1 + rets).prod() - 1
print(solo.round(3).to_string())
print("\n与策略腿日收益相关性:")
print(rets.corr()["strat"].round(3).to_string())


def yearly(r):
    v = (1 + r).cumprod()
    ye = v.resample("YE").last()
    ye.iloc[0] = v.resample("YE").last().iloc[0]  # 首年含起始日
    prev = pd.Series([1.0], index=[v.index[0] - pd.Timedelta(days=1)])
    base = pd.concat([prev, ye])
    return (base.pct_change().dropna() * 100).round(1)


def stats(r, rebal="D"):
    v = (1 + r).cumprod()
    tot = v.iloc[-1] - 1
    dd = (v / v.cummax() - 1).min()
    y = yearly(r)
    return tot, dd, y, v


def blend(weights, rebal="QE"):
    """weights: {腿: 权重}; rebal='D'每日 / 'QE'季度初。"""
    cols = list(weights)
    w = np.array([weights[c] for c in cols])
    R = rets[cols].values
    n = len(R)
    if rebal == "D":
        return pd.Series(R @ w, index=idx)
    V = np.tile(w, (1, 1)).ravel().astype(float)
    Vl = w.copy()
    port = np.empty(n)
    cur_q = (idx[0].year, (idx[0].month - 1) // 3)
    for i in range(n):
        q = (idx[i].year, (idx[i].month - 1) // 3)
        if q != cur_q:
            tot = Vl.sum()
            Vl = w * tot
            cur_q = q
        Vl = Vl * (1 + R[i])
        port[i] = Vl.sum()
    return pd.Series(port / port[0] - 1, index=idx).pct_change().fillna(0)


CONFIGS = [
    ("A 纯策略(对照)", {"strat": 1.0}),
    ("B 80策略+20国证2000", {"strat": 0.8, "gz2000": 0.2}),
    ("C 80策略+20黄金", {"strat": 0.8, "gold": 0.2}),
    ("D 80策略+20红利(价格)", {"strat": 0.8, "dividend": 0.2}),
    ("E 80策略+10黄金+10国证2000", {"strat": 0.8, "gold": 0.1, "gz2000": 0.1}),
    ("F 80策略+10黄金+10红利", {"strat": 0.8, "gold": 0.1, "dividend": 0.1}),
    ("G 70策略+15黄金+15国证2000", {"strat": 0.7, "gold": 0.15, "gz2000": 0.15}),
    ("H 70策略+15黄金+15红利", {"strat": 0.7, "gold": 0.15, "dividend": 0.15}),
    ("I 70策略+10金+10国2000+10红利",
     {"strat": 0.7, "gold": 0.1, "gz2000": 0.1, "dividend": 0.1}),
]

print("\n" + "=" * 90)
print(f"{'配置':<28} {'总收益':>8} {'最大回撤':>8} {'最差年':>7} "
      f"{'2022':>7} {'2023':>7} {'2024':>7} {'2025':>7} {'2026':>7}")
print("=" * 90)
rows = []
for name, w_ in CONFIGS:
    for rebal in ("D", "QE"):
        r = blend(w_, rebal)
        tot, dd, y, _ = stats(r)
        tag = "日再平衡" if rebal == "D" else "季再平衡"
        yv = {str(k.year): v for k, v in y.items()}
        worst = min(yv.values())
        print(f"{name+'['+tag+']':<30} {tot:>+7.1%} {dd:>+7.1%} "
              f"{worst:>+6.1f} " + " ".join(
                  f"{yv.get(str(yr), float('nan')):>+7.1f}"
                  for yr in range(2022, 2027)))
        rows.append({"config": name, "rebal": tag, "total": round(tot, 4),
                     "maxdd": round(dd, 4), "worst_year": worst,
                     **{f"y{yr}": yv.get(str(yr)) for yr in range(2022, 2027)}})
pd.DataFrame(rows).to_csv(f"{BASE}/blend_5y.csv", index=False)
print("\nBLEND_5Y_OK")
