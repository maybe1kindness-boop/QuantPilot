# -*- coding: utf-8 -*-
"""阶段14: 5年候选池前瞻收益解剖——分年 × 年线上/下方。

r1 = 信号日收盘→次一交易日收盘; r5 = →5个交易日后收盘。
回答: "为什么22-24年选出的股票不赚钱"。
"""
import pandas as pd

OUT = "/opt/quantpilot/runtime/backtest_2y"

feats = pd.read_pickle(f"{OUT}/features_5y.pkl")[["date", "code", "close"]]
feats["date"] = feats["date"].astype(str)
cands = pd.read_pickle(f"{OUT}/candidates_5y.pkl")
cands["date"] = cands["date"].astype(str)

# 日历与个股索引
vc = feats["date"].value_counts()
CAL = sorted(d for d, n in vc.items() if n >= 500)
POS = {d: i for i, d in enumerate(CAL)}

need = set(cands["code"].astype(str))
slim = feats[feats["code"].isin(need)]
cl = {}
for code, g in slim.groupby("code", sort=False):
    dl = g["date"].tolist()
    cl[str(code)] = {d: float(c) for d, c in zip(dl, g["close"])}
del feats, slim

# 年线状态(信号日当天)
sh = pd.read_csv(f"{OUT}/sh_long.csv", parse_dates=["date"]).set_index("date")["sh"]
ma200 = sh.rolling(200).mean()
sh_al = sh.reindex(pd.to_datetime(CAL)).ffill()
bull = (sh_al > ma200.reindex(pd.to_datetime(CAL)).ffill()).fillna(True)
BULL = {d: bool(v) for d, v in zip(CAL, bull)}

rows = []
for r in cands.itertuples():
    d = str(r.date)
    code = str(r.code)
    i = POS.get(d)
    arr = cl.get(code)
    if i is None or arr is None or i + 5 >= len(CAL):
        continue
    c0 = arr.get(d)
    c1 = arr.get(CAL[i + 1])
    c5 = arr.get(CAL[i + 5])
    if not c0 or c1 is None or c5 is None:
        continue
    rows.append({
        "year": d[:4], "bull": BULL.get(d, True),
        "r1": c1 / c0 - 1, "r5": c5 / c0 - 1,
    })

df = pd.DataFrame(rows)
print(f"样本 {len(df)} 个信号(池子前瞻, 未经过机器确认)")
print("\n== 分年: 池子平均前瞻收益 ==")
g = df.groupby("year").agg(
    n=("r1", "size"), r1均=("r1", "mean"), r5均=("r5", "mean"),
    r5胜率=("r5", lambda s: (s > 0).mean()))
print((g * 1).assign(r1均=g["r1均"].map("{:+.2%}".format),
                     r5均=g["r5均"].map("{:+.2%}".format),
                     r5胜率=g["r5胜率"].map("{:.0%}".format)).to_string())

print("\n== 按年线状态(全期合并) ==")
g2 = df.groupby("bull").agg(
    n=("r1", "size"), r1均=("r1", "mean"), r5均=("r5", "mean"),
    r5胜率=("r5", lambda s: (s > 0).mean()))
print(g2.assign(r1均=g2["r1均"].map("{:+.2%}".format),
                r5均=g2["r5均"].map("{:+.2%}".format),
                r5胜率=g2["r5胜率"].map("{:.0%}".format)).to_string())

print("\n== 分年 × 年线状态: r5均值 ==")
p = df.pivot_table(index="year", columns="bull", values="r5", aggfunc="mean")
n = df.pivot_table(index="year", columns="bull", values="r5", aggfunc="size")
for col in p.columns:
    p[col] = p[col].map(lambda v: f"{v:+.2%}" if pd.notna(v) else "-")
    p[("n", col)] = n[col]
print(p.to_string())
print("\nPOND_OK")
