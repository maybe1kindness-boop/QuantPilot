# -*- coding: utf-8 -*-
"""阶段1b: 5年(2022-2026)日线特征 + 选股规则全历史回放。

与 01_prep_screen.py 同口径(复用已缓存的 spot_snapshot.csv 股本快照),
WARMUP 提前到 2021-06, 筛选窗 2021-12-01→2026-09-04(首个可入场日 2022-01)。
features_5y.pkl 只存模拟器需要的 8 列(省内存)。
"""
import json
import os
import sqlite3
import time

import numpy as np
import pandas as pd

BASE = "/opt/quantpilot"
OUT = f"{BASE}/runtime/backtest_2y"

WARMUP = "2021-06-01"
SCREEN_START = "2021-12-01"
SCREEN_END = "2026-09-04"

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:6.1f}s] {m}", flush=True)

spot = pd.read_csv(f"{OUT}/spot_snapshot.csv", dtype={"code": str})
spot = spot.set_index("code")
name_map = spot["name"].to_dict()
shares_total = spot["shares_total"].to_dict()
shares_float = spot["shares_float"].to_dict()
is_st = {c: (("ST" in str(n)) or ("退" in str(n))) for c, n in name_map.items()}

conn = sqlite3.connect(f"file:{BASE}/runtime/data/stock_selection.db?mode=ro", uri=True)
codes_ok = [c for c in spot.index if c.startswith(("60", "00", "30"))
            and not is_st.get(c, True)]
log(f"代码过滤后 {len(codes_ok)} 只")

frames = []
for p in ("60", "00", "30"):
    chunk = [c for c in codes_ok if c[:2] == p]
    if not chunk:
        continue
    q = ("SELECT code,date,open,high,low,close,volume FROM stock_kline "
         "WHERE date>=? AND code IN (%s)" % ",".join("?" * len(chunk)))
    frames.append(pd.read_sql_query(q, conn, params=[WARMUP] + chunk))
df = pd.concat(frames, ignore_index=True)
log(f"日线行数 {len(df)}")

first_dates = pd.read_sql_query(
    "SELECT code, MIN(date) AS first_date FROM stock_kline GROUP BY code", conn)
conn.close()
first_map = first_dates.set_index("code")["first_date"].to_dict()

df = df.sort_values(["code", "date"]).reset_index(drop=True)
df["prev_close"] = df.groupby("code")["close"].shift(1)

board_thr = np.where(df.code.str.startswith("30"), 0.20, 0.10)
limit_price = np.round(df.prev_close * (1 + board_thr), 2)
df["is_limit"] = (df.close >= limit_price - 0.005) & df.prev_close.notna()

df["row_in_stock"] = df.groupby("code").cumcount()
g = df.groupby("code")
df["cnt20"] = g["is_limit"].transform(lambda s: s.rolling(20, min_periods=20).sum())
df["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
df["ma10_prev"] = g["ma10"].shift(1)
log("滚动特征完成")

pos = np.arange(len(df))
limit_pos = pd.Series(np.where(df.is_limit, pos, np.nan), index=df.index)
last_pos = limit_pos.groupby(df["code"]).ffill()
df["days_since_limit"] = pos - last_pos

df["amount_est"] = df.volume * 100 * df.close
df["shares_float"] = df.code.map(shares_float)
df["shares_total"] = df.code.map(shares_total)
df["turnover_est"] = df.volume * 100 / df.shares_float
df["mcap_est"] = df.shares_total * df.close
df["bias10"] = df.close / df.ma10 - 1
df["ma10_up"] = df.ma10 > df.ma10_prev
df["first_date"] = df.code.map(first_map)

listed_60d = pd.to_datetime(df.date) - pd.to_datetime(df.first_date) >= pd.Timedelta(days=90)
mask = (
    listed_60d
    & (df.row_in_stock >= 25)
    & (df.amount_est > 20e8)
    & (df.mcap_est < 300e8)
    & (df.cnt20 >= 2) & (df.cnt20 < 5)
    & (df.close >= df.ma10)
    & df.ma10_up.astype(bool)
    & (df.bias10 > 0.02) & (df.bias10 < 0.08)
    & (df.turnover_est > 0.10) & (df.turnover_est < 0.20)
    & (df.close < 50)
    & df.turnover_est.notna() & df.mcap_est.notna()
)
df["is_cand"] = mask
sel = df[df.is_cand & (df.date >= SCREEN_START) & (df.date <= SCREEN_END)].copy()
log(f"候选信号总数 {len(sel)}, 覆盖交易日 {sel.date.nunique()}")

sel = sel.sort_values(["date", "days_since_limit", "cnt20", "amount_est"],
                      ascending=[True, True, False, False])
sel["rank"] = sel.groupby("date").cumcount() + 1

keep_cols = ["date", "code", "open", "high", "low", "close", "prev_close",
             "is_limit", "cnt20", "days_since_limit", "ma10", "bias10",
             "amount_est", "turnover_est", "mcap_est", "rank"]
cands = sel[keep_cols]
cands.to_pickle(f"{OUT}/candidates_5y.pkl")       # 小文件先落盘
nrows = len(df)

# 大表瘦身后再写盘: date/code 转 category 释放字符串内存(上次在此 OOM)
import gc
del sel, g, limit_pos, last_pos, first_dates, frames, pos
gc.collect()
df["date"] = df["date"].astype("category")
df["code"] = df["code"].astype("category")
slim_cols = ["date", "code", "open", "high", "low", "close", "prev_close",
             "is_limit"]
slim = df[slim_cols].copy()
del df
gc.collect()
slim.to_pickle(f"{OUT}/features_5y.pkl")
del slim
gc.collect()

# 完整性回读校验(防再次截断)
chk = pd.read_pickle(f"{OUT}/features_5y.pkl")
assert len(chk) == nrows, f"features_5y 回读 {len(chk)} != {nrows}"
assert list(chk.columns) == slim_cols
chk2 = pd.read_pickle(f"{OUT}/candidates_5y.pkl")
assert len(chk2) == len(cands)
del chk, chk2
gc.collect()
log(f"回读校验通过: features {nrows} 行 / candidates {len(cands)} 行")

meta = {
    "screen_start": SCREEN_START, "screen_end": SCREEN_END,
    "n_signals": int(len(cands)), "n_days": int(cands.date.nunique()),
    "avg_per_day": round(float(len(cands) / max(1, cands.date.nunique())), 2),
    "max_per_day": int(cands.groupby("date").size().max()),
    "note": "股本/ST为当前快照近似; 幸存者偏差: 库内仅现存5183只",
}
json.dump(meta, open(f"{OUT}/meta_5y.json", "w"), ensure_ascii=False, indent=2)
log(f"完成: 信号{meta['n_signals']} 日均{meta['avg_per_day']} 峰值{meta['max_per_day']}")
log("PREP_5Y_OK")
