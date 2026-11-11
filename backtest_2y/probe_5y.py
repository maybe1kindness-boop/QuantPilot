# -*- coding: utf-8 -*-
"""5年回测(2022-2026)数据可行性探测: 日线深度/5分钟深度/磁盘."""
import os
import shutil
import sqlite3
import time

import pandas as pd

BASE = "/opt/quantpilot"
DB = f"{BASE}/runtime/data/stock_selection.db"

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:5.1f}s] {m}", flush=True)

du = shutil.disk_usage("/opt")
log(f"磁盘: 总 {du.total/1e9:.0f}G 可用 {du.free/1e9:.1f}G "
    f"db大小 {os.path.getsize(DB)/1e9:.2f}G")

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
log("stock_kline 总体: " + str(
    conn.execute("SELECT MIN(date), MAX(date), COUNT(DISTINCT code) "
                 "FROM stock_kline").fetchone()))
m = pd.read_sql_query(
    "SELECT substr(date,1,7) ym, COUNT(*) n, COUNT(DISTINCT code) nc "
    "FROM stock_kline GROUP BY 1", conn)
log("2021-10以来按月覆盖:\n" + m[m.ym >= "2021-10"].to_string(index=False))
conn.close()

from pytdx.hq import TdxHq_API
api = TdxHq_API()
assert api.connect("180.153.18.170", 7709, time_out=5), "TDX连接失败"
for code, mkt in (("000001", 0), ("600000", 1), ("300750", 0)):
    n_bars, earliest = 0, None
    start = 0
    for _ in range(90):                      # 最多90页=72000根≈6年
        page = api.get_security_bars(0, mkt, code, start, 800)
        if not page:
            break
        n_bars += len(page)
        earliest = page[0].get("datetime")
        if str(earliest) < "2021-11-01":
            break
        start += len(page)
    log(f"{code}: 5分钟最早 {earliest}, 共 {n_bars} 根")
api.disconnect()
log("PROBE_OK")
