# -*- coding: utf-8 -*-
"""阶段1:数据准备 + 选股规则全历史回放(近2年)。

产出:
  features.pkl  每股每bar特征(含涨停标记/MA10/乖离/成交额估算/换手估算/市值估算)
  candidates.pkl 每日候选(date, code, rank...)
  meta.json     口径与统计
口径近似(与生产差异):
  成交额 = volume(手)×100×close      换手率 = volume×100/流通股本(现值)
  总市值 = 总股本(现值)×close        ST/退市用当前名称, 北交/科创按代码前缀排除
  涨停判定 = close ≥ round(prev_close×(1+阈值),2)-0.005 (主板10%/创业板20%)
"""
import json
import os
import sqlite3
import time

import numpy as np
import pandas as pd

BASE = "/opt/quantpilot"
OUT = f"{BASE}/runtime/backtest_2y"
os.makedirs(OUT, exist_ok=True)

WARMUP = "2024-06-01"     # 预热(MA10+20日涨停窗口)
SCREEN_START = "2024-09-05"
SCREEN_END = "2026-09-04"

t0 = time.time()


def log(msg):
    print(f"[{time.time()-t0:6.1f}s] {msg}", flush=True)


# ---------- 1) 全市场快照: 名称来自stock_basic, 股本来自TDX金融接口 ----------
# (东财spot接口从本服务器被屏蔽, 改用TDX; 均为当前静态值, 与生产口径一致)
spot_path = f"{OUT}/spot_snapshot.csv"
if os.path.exists(spot_path):
    spot = pd.read_csv(spot_path, dtype={"code": str})
    log(f"快照缓存 {len(spot)} 行")
else:
    conn = sqlite3.connect(
        f"file:{BASE}/runtime/data/stock_selection.db?mode=ro", uri=True)
    basic = pd.read_sql_query(
        "SELECT code, name FROM stock_basic", conn)
    conn.close()
    basic["code"] = basic["code"].astype(str).str.zfill(6)

    from pytdx.hq import TdxHq_API
    api = TdxHq_API()
    host, port = "180.153.18.170", 7709
    assert api.connect(host, port, time_out=5), "TDX连接失败"
    rows, fails = [], 0
    todo = [c for c in basic.code if c.startswith(("60", "00", "30"))]
    log(f"TDX拉股本 {len(todo)} 只...")
    for k, code in enumerate(todo):
        try:
            fi = api.get_finance_info(1 if code.startswith("6") else 0, code)
            if fi:
                rows.append({"code": code,
                             "shares_float": fi.get("liutongguben"),
                             "shares_total": fi.get("zongguben")})
                continue
        except Exception:
            pass
        fails += 1
        if fails % 200 == 1:
            try:
                api.disconnect()
            except Exception:
                pass
            api = TdxHq_API()
            api.connect(host, port, time_out=5)
        if k % 1000 == 999:
            log(f"  {k+1}/{len(todo)} fails={fails}")
    api.disconnect()
    sh = pd.DataFrame(rows)
    spot = basic.merge(sh, on="code", how="left")
    spot.to_csv(spot_path, index=False)
    log(f"快照完成 {len(spot)} 行, 股本缺失 {spot.shares_total.isna().sum()}, 失败 {fails}")

spot = spot.set_index("code")
name_map = spot["name"].to_dict()
shares_total = spot["shares_total"].to_dict()
shares_float = spot["shares_float"].to_dict()
is_st = {c: (("ST" in str(n)) or ("退" in str(n))) for c, n in name_map.items()}

# ---------- 2) 日线数据(仅主板+创业板) ----------
conn = sqlite3.connect(f"file:{BASE}/runtime/data/stock_selection.db?mode=ro", uri=True)
codes_ok = [c for c in spot.index if c.startswith(("60", "00", "30"))
            and not is_st.get(c, True)]
log(f"代码过滤后 {len(codes_ok)} 只")

frames = []
for prefix_len, chunk in [(p, [c for c in codes_ok if c[:2] == p]) for p in ("60", "00", "30")]:
    if not chunk:
        continue
    q = ("SELECT code,date,open,high,low,close,volume FROM stock_kline "
         "WHERE date>=? AND code IN (%s)" % ",".join("?" * len(chunk)))
    frames.append(pd.read_sql_query(q, conn, params=[WARMUP] + chunk))
df = pd.concat(frames, ignore_index=True)
log(f"日线行数 {len(df)}")

# 量纲交叉验证: 用5分钟表的成交额核对
try:
    chk = conn.execute(
        "SELECT SUM(amount) FROM stock_intraday_kline WHERE code='000001' "
        "AND substr(timestamp,1,10)='2026-01-06' AND interval='5'").fetchone()[0]
    d_row = df[(df.code == "000001") & (df.date == "2026-01-06")]
    if chk and len(d_row):
        est = float(d_row.volume.iloc[0]) * 100 * float(d_row.close.iloc[0])
        log(f"量纲验证 5min实际={chk/1e8:.2f}亿 vs 日线估算={est/1e8:.2f}亿 比值={chk/est:.3f}")
except Exception as e:
    log(f"量纲验证跳过: {e}")

# 每只股票全历史首个日期(识别次新)
first_dates = pd.read_sql_query(
    "SELECT code, MIN(date) AS first_date FROM stock_kline GROUP BY code", conn)
conn.close()
first_map = first_dates.set_index("code")["first_date"].to_dict()

df = df.sort_values(["code", "date"]).reset_index(drop=True)
df["prev_close"] = df.groupby("code")["close"].shift(1)

# ---------- 3) 涨停判定(价格感知舍入) ----------
board_thr = np.where(df.code.str.startswith("30"), 0.20, 0.10)
limit_price = np.round(df.prev_close * (1 + board_thr), 2)
df["is_limit"] = (df.close >= limit_price - 0.005) & df.prev_close.notna()

# 上市满60交易日(按全历史首日至今的自然日粗略+窗口内bar数双保险)
df["row_in_stock"] = df.groupby("code").cumcount()
g = df.groupby("code")
df["cnt20"] = g["is_limit"].transform(lambda s: s.rolling(20, min_periods=20).sum())
df["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
df["ma10_prev"] = g["ma10"].shift(1)
log("滚动特征完成")

# 距最近一次涨停的bar数
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

# ---------- 4) 选股条件(在T日收盘计算) ----------
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

# 强度排序代理: 最近涨停越近越强, 其次20日涨停数, 再次成交额
sel = sel.sort_values(["date", "days_since_limit", "cnt20", "amount_est"],
                      ascending=[True, True, False, False])
sel["rank"] = sel.groupby("date").cumcount() + 1

keep_cols = ["date", "code", "open", "high", "low", "close", "prev_close",
             "is_limit", "cnt20", "days_since_limit", "ma10", "bias10",
             "amount_est", "turnover_est", "mcap_est", "rank"]
cands = sel[keep_cols]
df[keep_cols[:-1]].to_pickle(f"{OUT}/features.pkl")
cands.to_pickle(f"{OUT}/candidates.pkl")

meta = {
    "screen_start": SCREEN_START, "screen_end": SCREEN_END,
    "n_signals": int(len(cands)), "n_days": int(cands.date.nunique()),
    "avg_per_day": round(float(len(cands) / max(1, cands.date.nunique())), 2),
    "max_per_day": int(cands.groupby("date").size().max()),
    "amount_rule": "volume(手)×100×close", "turnover_rule": "vol/float_shares(now)",
    "mcap_rule": "total_shares(now)×close",
    "board_rule": "60/00=10%, 30=20%; 排除科创688/北交/ST(当前名称)",
    "n_codes": int(df.code.nunique()),
}
json.dump(meta, open(f"{OUT}/meta.json", "w"), ensure_ascii=False, indent=2)
log(f"完成: 信号{meta['n_signals']} 日均{meta['avg_per_day']} 峰值{meta['max_per_day']}")
log("PREP_OK")
