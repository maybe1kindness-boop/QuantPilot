# -*- coding: utf-8 -*-
"""诊断: 22池 2022/2023 每只票 分红/上年末收盘 产出表, 查分红数据是否虚高/重复。"""
import sqlite3
from collections import defaultdict

import pandas as pd

OUT = "/opt/quantpilot/runtime/backtest_2y"
POOL = ["601398", "601939", "601288", "601988", "600036", "601166", "601088",
        "601225", "600985", "600900", "600025", "600886", "600941", "601728",
        "600050", "601857", "600938", "601006", "000895", "600887", "601333",
        "600919"]

div_ex = defaultdict(lambda: defaultdict(float))
dup = defaultdict(int)
for shard in (0, 1, 2):
    df = pd.read_csv(f"{OUT}/div_full_{shard}.csv", dtype={"code": str})
    for r in df.itertuples():
        c = str(r.code).zfill(6)
        key = (c, str(r.ex_date))
        dup[key] += 1
        div_ex[c][str(r.ex_date)] += float(r.cash_ps)
print("重复(code,ex_date)行数>1的:", sum(1 for v in dup.values() if v > 1))

conn = sqlite3.connect(
    "file:/opt/quantpilot/runtime/data/stock_selection.db?mode=ro", uri=True)
for Y in (2022, 2023):
    print(f"\n== {Y}年 22池各票: 上年末收盘 / 年内分红合计 / 股息率 / 价格涨跌 ==")
    prev_q = conn.execute("SELECT MAX(date) FROM stock_kline WHERE date LIKE ?",
                          (f"{Y-1}-12%",)).fetchone()[0]
    for code in POOL:
        p0 = conn.execute(
            "SELECT close FROM stock_kline WHERE code=? AND date=?",
            (code, prev_q)).fetchone()
        p1 = conn.execute(
            "SELECT close FROM stock_kline WHERE code=? AND date LIKE ? "
            "ORDER BY date DESC LIMIT 1", (code, f"{Y}-12%")).fetchone()
        div = sum(v for d, v in div_ex[code].items() if d.startswith(str(Y)))
        if not p0 or not p1:
            print(f"{code} 缺价格")
            continue
        p0, p1 = float(p0[0]), float(p1[0])
        exs = sorted(d for d in div_ex[code] if d.startswith(str(Y)))
        print(f"{code} p0={p0:.2f} p1={p1:.2f} div={div:.3f} "
              f"yield={div/p0*100:.1f}% px={(p1/p0-1)*100:+.1f}% ex={exs}")
conn.close()
