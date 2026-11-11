# -*- coding: utf-8 -*-
"""阶段11: 导入本地baostock拉取的5分钟K线到服务器库。

INSERT OR REPLACE, source='baostock', interval='5'。
导入后校验: 时间戳分布/按年行数/与TDX重叠段一致性抽查。
"""
import csv
import glob
import sqlite3
import time

BASE = "/opt/quantpilot"
SRC = f"{BASE}/runtime/backtest_2y/bs_5min"

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:6.1f}s] {m}", flush=True)

files = sorted(glob.glob(f"{SRC}/*.csv"))
log(f"待导入 {len(files)} 个文件")


def fnum(x):
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


conn = sqlite3.connect(f"{BASE}/runtime/data/stock_selection.db", timeout=30)
total = 0
for k, path in enumerate(files):
    code = path.split("/")[-1][:6]
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append((code, r["timestamp"], "5", fnum(r["open"]),
                         fnum(r["high"]), fnum(r["low"]), fnum(r["close"]),
                         fnum(r["volume"]), fnum(r["amount"]), "baostock"))
    conn.executemany(
        "INSERT OR REPLACE INTO stock_intraday_kline "
        "(code, timestamp, interval, open, high, low, close, volume, amount, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    total += len(rows)
    if (k + 1) % 10 == 0:
        log(f"  {k+1}/{len(files)} 累计 {total} 行")

# ---- 校验 ----
log("按年行数(baostock源): " + str(conn.execute(
    "SELECT substr(timestamp,1,4), COUNT(*) FROM stock_intraday_kline "
    "WHERE source='baostock' GROUP BY 1").fetchall()))
log("时间戳分钟分布(抽查000001): " + str(conn.execute(
    "SELECT DISTINCT substr(timestamp,12,5) FROM stock_intraday_kline "
    "WHERE code=(SELECT code FROM stock_intraday_kline WHERE source='baostock' "
    "LIMIT 1) AND source='baostock' ORDER BY 1").fetchall()))
# 与TDX重叠抽查: 同bar的收盘价差异
chk = conn.execute(
    "SELECT a.timestamp, a.close, b.close FROM stock_intraday_kline a "
    "JOIN stock_intraday_kline b ON a.code=b.code AND a.timestamp=b.timestamp "
    "AND a.interval=b.interval "
    "WHERE a.source='baostock' AND b.source!='baostock' LIMIT 5").fetchall()
log(f"重叠抽查(时间戳, baostock, tdx): {chk}")
conn.close()
log(f"IMPORT_OK 共 {total} 行")
