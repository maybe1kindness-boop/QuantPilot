# -*- coding: utf-8 -*-
"""阶段8: "策略+慢牛腿"混合测试 — 固定比例每日再平衡(理论口径).

慢腿 = 鱼塘等权(小市值<300亿 & 收盘<50, 全库口径)日收益,
即 07_market_vs_pond 的同一序列; 快腿 = equity_min.csv(基准参数).
输出: 各比例分年收益/总收益/最大回撤/日收益相关系数.
注意: 鱼塘等权是纸上组合(含涨停买不进的成分), 真实可交易的
最近似工具(中证2000/国证2000 ETF)收益会低于此.
"""
import numpy as np
import pandas as pd

OUT = "/opt/quantpilot/runtime/backtest_2y"

feats = pd.read_pickle(f"{OUT}/features.pkl")
feats["ret"] = feats.close / feats.prev_close - 1.0
pond = feats[(feats.mcap_est < 300e8) & (feats.close < 50)]
dr = pond.groupby("date").ret.mean()
dr.index = pd.to_datetime(dr.index)
print(f"鱼塘日收益序列: {len(dr)} 天, 覆盖 {dr.index.min().date()}→{dr.index.max().date()}")

eq = pd.read_csv(f"{OUT}/equity_min.csv", index_col=0).iloc[:, 0]
eq.index = pd.to_datetime(eq.index)
sr = eq.pct_change()
sr.iloc[0] = eq.iloc[0] / 300000.0 - 1.0

dr_al = dr.reindex(sr.index)
print(f"对齐缺失天数: {dr_al.isna().sum()} (应为0)")
dr_al = dr_al.fillna(0.0)

rows = []


def curve_stats(name, r):
    cum = (1 + r).cumprod()
    eqv = cum * 300000.0
    dd = (eqv / eqv.cummax() - 1).min()
    row = {"组合": name, "两年总收益": round(float(cum.iloc[-1] - 1), 4),
           "最大回撤": round(float(dd), 4)}
    for y, seg in r.groupby(r.index.year):
        row[f"{y}年(部分)" if y == 2024 else f"{y}年"] = \
            round(float((1 + seg).prod() - 1), 4)
    rows.append(row)


curve_stats("纯策略(基准参数)", sr)
for w in (0.2, 0.3):
    curve_stats(f"{round((1-w)*10)}成策略+{round(w*10)}成慢腿(每日再平衡)",
                (1 - w) * sr + w * dr_al)
curve_stats("纯鱼塘等权(不可直接交易)", dr_al)

t = pd.DataFrame(rows)
print(t.to_string(index=False))
print(f"\n日收益相关系数 策略vs鱼塘: {sr.corr(dr_al):+.3f}")
t.to_csv(f"{OUT}/blend_slowleg.csv", index=False)
print("BLEND_OK")
