# -*- coding: utf-8 -*-
"""阶段15: "熊市也有翻倍股"解剖。
①每年全市场涨幅>50%/>100%的股票数(现存股票,幸存者偏差轻微)
②这些翻倍股当年是否进过候选池(涨停+强势筛选)
③池子信号的20日前瞻收益分布(事后上限)
"""
import pandas as pd

OUT = "/opt/quantpilot/runtime/backtest_2y"

feats = pd.read_pickle(f"{OUT}/features_5y.pkl")[["date", "code", "close"]]
feats["date"] = feats["date"].astype(str)
feats["year"] = feats["date"].str[:4]

cands = pd.read_pickle(f"{OUT}/candidates_5y.pkl")
cands["date"] = cands["date"].astype(str)
cands["year"] = cands["date"].str[:4]
pond_codes = {y: set(g["code"].astype(str)) for y, g in cands.groupby("year")}

# ---- ① 年度涨幅(上年末收→本年末收) ----
first = feats.sort_values("date").groupby(["code", "year"])["close"].first()
last = feats.sort_values("date").groupby(["code", "year"])["close"].last()
yr = (last / first - 1).rename("ret").reset_index()
yr["code"] = yr["code"].astype(str)

print("== ① 全市场年度涨幅分布(现存股票) ==")
for y in map(str, range(2022, 2027)):
    seg = yr[yr["year"] == y]
    n = len(seg)
    n50 = int((seg["ret"] > 0.5).sum())
    n100 = int((seg["ret"] > 1.0).sum())
    up = int((seg["ret"] > 0).sum())
    print(f"  {y}: {n}只 上涨{up}只({up/n:.0%})  >+50%: {n50}只 "
          f"({n50/n:.1%})  翻倍: {n100}只 ({n100/n:.1%})")

print("\n== ② 翻倍股(>+50%)与候选池的交集 ==")
for y in map(str, range(2022, 2027)):
    big = set(yr[(yr["year"] == y) & (yr["ret"] > 0.5)]["code"])
    pc = pond_codes.get(y, set())
    hit = big & pc
    seg = yr[(yr["year"] == y) & (yr["ret"] > 0.5)]
    hit_ret = seg[seg["code"].isin(pc)]["ret"]
    print(f"  {y}: >+50%共{len(big)}只, 进过池子的{len(hit)}只"
          + (f", 这些当年均涨{hit_ret.mean():+.0%}" if len(hit) else ""))

# ---- ③ 池子信号 20 日前瞻 ----
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

rows = []
for r in cands.itertuples():
    d, code = str(r.date), str(r.code)
    i = POS.get(d)
    arr = cl.get(code)
    if i is None or arr is None or i + 20 >= len(CAL):
        continue
    c0, c20 = arr.get(d), arr.get(CAL[i + 20])
    if c0 and c20 is not None:
        rows.append({"year": d[:4], "r20": c20 / c0 - 1})

df = pd.DataFrame(rows)
print("\n== ③ 池子信号20日前瞻收益(事后上限视角) ==")
g = df.groupby("year")["r20"].agg(
    n="size", 均值="mean", 中位="median",
    P90=lambda s: s.quantile(0.9), 最好="max")
print(g.assign(均值=g["均值"].map("{:+.1%}".format),
               中位=g["中位"].map("{:+.1%}".format),
               P90=g["P90"].map("{:+.1%}".format),
               最好=g["最好"].map("{:+.0%}".format)).to_string())
print("\nDOUBLERS_OK")
