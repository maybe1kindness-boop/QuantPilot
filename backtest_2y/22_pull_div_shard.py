# -*- coding: utf-8 -*-
"""阶段22: 全市场分红历史拉取(分片, 可断点续传)。

抓 baostock query_dividend_data 2019-2025, 逐行记录:
  code, dividOperateDate(除权除息日), dividCashPsBeforeTax(每股税前现金分红)
用途: ①每年1月按"过去3年累计每股分红/上年末收盘"排名(无偷看选票)
     ②按除权日重建总收益日序列(分红再投入)。
用法: python 22_pull_div_shard.py <shard_id> <n_shards>
"""
import os
import sqlite3
import sys
import time

import baostock as bs

SHARD = int(sys.argv[1])
NSHARD = int(sys.argv[2])
OUT = "/opt/quantpilot/runtime/backtest_2y"
CSV = f"{OUT}/div_full_{SHARD}.csv"
LOG = f"{OUT}/div_pull_{SHARD}.log"

conn = sqlite3.connect(
    "file:/opt/quantpilot/runtime/data/stock_selection.db?mode=ro", uri=True)
rows = conn.execute("SELECT code, name FROM stock_basic").fetchall()
kl_first = {}
for code, mind in conn.execute(
        "SELECT code, MIN(date) FROM stock_kline GROUP BY code"):
    kl_first[str(code).zfill(6)] = str(mind)[:10]
conn.close()

universe = []
for c, n in rows:
    code = str(c).zfill(6)
    name = str(n or "")
    if "ST" in name.upper():
        continue
    first = kl_first.get(code)
    if first is None or first > "2022-06-30":
        continue
    universe.append(code)
universe.sort()
mine = [c for i, c in enumerate(universe) if i % NSHARD == SHARD]

done = set()
if os.path.exists(CSV):
    import csv as _csv
    with open(CSV, encoding="utf-8") as f:
        for r in _csv.reader(f):
            if r and r[0] != "code":
                done.add(r[0])
todo = [c for c in mine if c not in done]


def log(m):
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


log(f"shard{SHARD}/{NSHARD} 全市场{len(universe)} 本片{len(mine)} "
    f"已完成{len(done)} 待拉{len(todo)}")
bs.login()
t0 = time.time()
new = open(CSV, "a", encoding="utf-8")
if not done:
    new.write("code,ex_date,cash_ps\n")
    new.flush()
for k, code in enumerate(todo):
    bs_code = ("sh." if code.startswith(("6", "9", "5")) else "sz.") + code
    for y in range(2019, 2026):
        try:
            rs = bs.query_dividend_data(code=bs_code, year=str(y),
                                        yearType="operate")
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                d = dict(zip(rs.fields, row))
                cash = float(d.get("dividCashPsBeforeTax") or 0)
                ex = str(d.get("dividOperateDate") or "")[:10]
                if cash > 0 and ex[:4].isdigit():
                    new.write(f"{code},{ex},{cash}\n")
        except Exception as e:
            log(f"ERR {code} {y}: {e}")
    new.flush()
    if (k + 1) % 100 == 0:
        pace = (k + 1) / (time.time() - t0)
        left = (len(todo) - k - 1) / max(pace, 0.1) / 60
        log(f"进度 {k + 1}/{len(todo)} pace={pace:.1f}/s 剩余~{left:.0f}min")
new.close()
bs.logout()
log(f"SHARD{SHARD}_DONE")
