# -*- coding: utf-8 -*-
"""阶段23: 无偷看版红利篮子重建 + 口径统一对比。

A. honest: 每年1月, 全市场(非ST快照、上市够早)按"连续3年分红年均值/上年末收盘"
   前10等权, 总收益口径(除权日分红加回)。
B. pool_tr: 原22只常识池同一规则同一口径重建——分离"池子偏差"与"价格口径偏差"。
输出 basket_daily_honest.csv / basket_daily_pool_tr.csv + 各自累计/分年。
"""
import sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

OUT = "/opt/quantpilot/runtime/backtest_2y"
DB = "file:/opt/quantpilot/runtime/data/stock_selection.db?mode=ro"
POOL = {"601398", "601939", "601288", "601988", "600036", "601166", "601088",
        "601225", "600985", "600900", "600025", "600886", "600941", "601728",
        "600050", "601857", "600938", "601006", "000895", "600887", "601333",
        "600919"}

div_y = defaultdict(lambda: defaultdict(float))
div_ex = defaultdict(lambda: defaultdict(float))
for shard in (0, 1, 2):
    df = pd.read_csv(f"{OUT}/div_full_{shard}.csv", dtype={"code": str})
    df = df.drop_duplicates(["code", "ex_date"])  # 分片重启/年度归属重叠防双算
    for r in df.itertuples():
        c = str(r.code).zfill(6)
        y = int(str(r.ex_date)[:4])
        div_y[c][y] += float(r.cash_ps)
        div_ex[c][str(r.ex_date)] += float(r.cash_ps)
print(f"分红记录: {len(div_y)} 只股票")

conn = sqlite3.connect(DB, uri=True)
names = {str(r[0]).zfill(6): str(r[1]) for r in
         conn.execute("SELECT code, name FROM stock_basic")}

dec_close = {}
for py in range(2021, 2026):
    q = ("SELECT k.code, k.close FROM stock_kline k JOIN "
         "(SELECT code, MAX(date) md FROM stock_kline WHERE date LIKE ? "
         " GROUP BY code) t ON k.code=t.code AND k.date=t.md")
    dec_close[py] = {str(r[0]).zfill(6): float(r[1]) for r in
                     conn.execute(q, (f"{py}-12%",))}
print(f"上年末收盘: {len(dec_close[2021])} 只(2021)")


def rank_top10(Y, pool=None):
    cands = []
    ys = [Y - 3, Y - 2, Y - 1]
    for code, ymap in div_y.items():
        if pool is not None and code not in pool:
            continue
        if any(ymap.get(y, 0.0) <= 0 for y in ys):
            continue
        px0 = dec_close[Y - 1].get(code, 0.0)
        if px0 <= 0:
            continue
        avg_d = sum(ymap[y] for y in ys) / 3.0
        cands.append((avg_d / px0, code))
    cands.sort(reverse=True)
    return [c for _, c in cands[:10]]


def year_returns(codes, year):
    cal = [str(d) for (d, n) in conn.execute(
        "SELECT date, COUNT(*) c FROM stock_kline WHERE date LIKE ? "
        "GROUP BY date HAVING c > 2000 ORDER BY date", (f"{year}-%",))]
    if not cal:
        return None
    prev_q = conn.execute(
        "SELECT MAX(date) FROM stock_kline WHERE date LIKE ?",
        (f"{year - 1}-12%",)).fetchone()[0]
    idx = [str(prev_q)] + cal
    px = pd.DataFrame(index=idx)
    div = pd.DataFrame(0.0, index=idx, columns=codes)
    for code in codes:
        rows = conn.execute(
            "SELECT date, close FROM stock_kline WHERE code=? AND date LIKE ?",
            (code, f"{year}-%")).fetchall()
        px[code] = pd.Series({str(d): float(c) for d, c in rows})
        for ex, cash in div_ex[code].items():
            if ex in div.index:
                div.at[ex, code] += cash
    px = px.ffill().bfill()
    r = (px + div) / px.shift(1) - 1.0
    r = r.loc[cal]
    return r.mean(axis=1)


all_picks = {}
for tag, pool in (("honest", None), ("pool_tr", POOL)):
    picks_log = []
    parts = []
    for Y in range(2022, 2027):
        top = rank_top10(Y, pool)
        picks_log.append((Y, top))
        s = year_returns(top, Y)
        if s is not None:
            parts.append(s)
    ser = pd.concat(parts).sort_index()
    ser.name = "basket"
    ser.to_csv(f"{OUT}/basket_daily_{tag}.csv", index_label="date")
    cum = float((1.0 + ser).prod() - 1.0)
    yr = ser.groupby(ser.index.str[:4]).apply(
        lambda x: float((1.0 + x).prod() - 1.0))
    print(f"\n== {tag} == 累计 {cum * 100:+.1f}%")
    print("分年:", {k: f"{v * 100:+.1f}%" for k, v in yr.items()})
    for Y, top in picks_log:
        print(f"{Y}: " + ", ".join(
            f"{c}{names.get(c, '')}" for c in top))
    all_picks[tag] = {str(Y): [f"{c}{names.get(c, '')}" for c in top]
                      for Y, top in picks_log}
import json as _json
_json.dump(all_picks, open(f"{OUT}/basket_picks.json", "w"),
           ensure_ascii=False, indent=1)
conn.close()
print("SIM23_OK")
