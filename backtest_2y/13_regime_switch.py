# -*- coding: utf-8 -*-
"""阶段13: "熊市战法/牛市战法"切换 vs 静态分层, 5年对比。

方案A: 大盘牛熊切换 — 上证收于MA200上=脉冲满配, 下=红利满配(及50/50变体)
方案A2: 同上但用国证2000的MA200(小盘趋势)
方案B: 信号温度切换 — 近20交易日候选信号数>=阈值=脉冲满配, 否则6成脉冲4成红利
对照:  静态8/2、纯策略
全部用t-1日状态决策, 日频执行。
"""
import numpy as np
import pandas as pd

BASE = "./backtest_2y"
DIV_DRIFT = 0.000217  # 红利价格指数+年化5.5%分红近似

eq = pd.read_csv(f"{BASE}/equity_5ymin_new6.csv", index_col=0).iloc[:, 0]
eq.index = pd.to_datetime(eq.index)
r_strat = eq.pct_change().fillna(0)
idx = r_strat.index

legs = pd.read_csv(f"{BASE}/legs_5y.csv", index_col=0, parse_dates=True)
r_div = (legs["dividend"].reindex(idx).ffill().pct_change().fillna(0)
         + DIV_DRIFT)

sh = pd.read_csv(f"{BASE}/sh_long.csv", parse_dates=["date"]).set_index("date")["sh"]
ma200_sh = sh.rolling(200).mean()
gz = legs["gz2000"].copy()
gz_long = gz.reindex(
    pd.date_range(gz.index[0], gz.index[-1], freq="B")).ffill()
# 国证2000预热: 用与上证的比值回溯不可行, 直接从legs起点算MA200,
# 2022年内前段MA未就绪时默认"牛"(保守: 不关机器)
ma200_gz = gz_long.rolling(200).mean()

cand = pd.read_pickle(f"{BASE}/candidates_5y.pkl")
sig = cand.groupby("date").size()
sig = sig.reindex(idx).fillna(0)
temp20 = sig.rolling(20).sum()


def run(w_series):
    w = w_series.shift(1).fillna(w_series.iloc[0]).values  # t-1决策
    r = w * r_strat.values + (1 - w) * r_div.values
    s = pd.Series(r, index=idx)
    v = (1 + s).cumprod()
    tot, dd = v.iloc[-1] - 1, (v / v.cummax() - 1).min()
    y = {}
    prev = 1.0
    for yr, seg in v.groupby(v.index.year):
        y[yr] = seg.iloc[-1] / prev - 1
        prev = seg.iloc[-1]
    return tot, dd, y


def show(name, w):
    tot, dd, y = run(w)
    line = " ".join(f"{y.get(yr, float('nan')):>+7.1%}" for yr in range(2022, 2027))
    print(f"{name:<26} {tot:>+7.1%} {dd:>+7.1%} {line}")


sh_al = sh.reindex(idx).ffill()
bull_sh = (sh_al > ma200_sh.reindex(idx).ffill()).fillna(True)
gz_al = gz_long.reindex(idx).ffill()
bull_gz = (gz_al > ma200_gz.reindex(idx).ffill()).fillna(True)

print(f"{'配置':<26} {'总收益':>8} {'回撤':>8}  {'2022':>7} {'2023':>7} "
      f"{'2024':>7} {'2025':>7} {'2026':>7}")
print("=" * 88)
show("纯策略", pd.Series(1.0, index=idx))
show("静态8/2", pd.Series(0.8, index=idx))
print("-" * 88)
print(f"[上证MA200: 牛天数占比 {bull_sh.mean():.0%}]")
show("A 大盘牛熊: 牛=脉冲/熊=红利",
     bull_sh.astype(float))
show("A 大盘牛熊: 牛=脉冲/熊=五五",
     bull_sh.astype(float) * 0.5 + 0.5)
print(f"[国证2000MA200: 牛天数占比 {bull_gz.mean():.0%}]")
show("A2 小盘牛熊: 牛=脉冲/熊=红利",
     bull_gz.astype(float))
show("A2 小盘牛熊: 牛=脉冲/熊=五五",
     bull_gz.astype(float) * 0.5 + 0.5)
print("-" * 88)
t = temp20.fillna(0)
print(f"[20日信号数: 中位数 {t.median():.0f}, 均值 {t.mean():.1f}, "
      f"P25/P75 = {t.quantile(.25):.0f}/{t.quantile(.75):.0f}]")
for thr in (2, 3, 5):
    show(f"B 信号温度≥{thr}: 满配/否则64",
         (t >= thr).astype(float) * 0.4 + 0.6)

# 各方案在2026年关键节点的机器开关状态
print("\n2026年机器满配天数占比:")
for name, w in (("A 上证牛熊", bull_sh.astype(float)),
                ("A2 小盘牛熊", bull_gz.astype(float)),
                ("B 信号温度≥3", (t >= 3).astype(float))):
    seg = w.loc["2026"]
    print(f"  {name}: {seg.mean():.0%}")

print("\nREGIME_OK")
