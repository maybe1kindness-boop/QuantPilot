# -*- coding: utf-8 -*-
"""阶段3: 为两年候选股定向补齐5分钟K线(TDX主源)。

复用模块内 _fetch_intraday_frame_worker(线程并发抓取,不碰库)
+ IntradayDataStore._store(自带锁的单点写入, INSERT OR REPLACE幂等)。
完整判定: 库内该股 interval='5' 最早<=2024-09-10 且 最晚>=2026-09-03,
不满足则整段重取(2024-09-01→2026-09-04), 覆盖式替换。
"""
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, "/opt/quantpilot")
import pandas as pd

from trading.intraday_data import (
    IntradayDataStore, _fetch_intraday_frame_worker,
)

BASE = "/opt/quantpilot"
OUT = f"{BASE}/runtime/backtest_2y"
START, END = "2024-09-01", "2026-09-04"
FULL_MIN, FULL_MAX = "2024-09-10", "2026-09-03"
WORKERS = 4

t0 = time.time()


def log(m):
    print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)


cands = pd.read_pickle(f"{OUT}/candidates.pkl")
codes = sorted(cands.code.unique())
log(f"候选股票 {len(codes)} 只, 信号 {len(cands)} 条")

# 库内已有覆盖(一次分组查询)
conn = sqlite3.connect(f"file:{BASE}/runtime/data/stock_selection.db?mode=ro", uri=True)
cov = {}
for i in range(0, len(codes), 400):
    chunk = codes[i:i + 400]
    q = ("SELECT code, MIN(substr(timestamp,1,10)), MAX(substr(timestamp,1,10)) "
         "FROM stock_intraday_kline WHERE interval='5' AND code IN (%s) "
         "GROUP BY code" % ",".join("?" * len(chunk)))
    for code, mn, mx in conn.execute(q, chunk):
        cov[code] = (mn, mx)
conn.close()

todo, done = [], []
for code in codes:
    mn, mx = cov.get(code, (None, None))
    if mn is not None and mn <= FULL_MIN and mx >= FULL_MAX:
        done.append(code)
    else:
        todo.append(code)
log(f"已完整 {len(done)} 只, 待补 {len(todo)} 只")
json.dump({"todo": todo, "done": done}, open(f"{OUT}/backfill_state.json", "w"))

store = IntradayDataStore()
ok, failed = [], []
i = 0
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    futs = {ex.submit(_fetch_intraday_frame_worker,
                      (c, START, END, "5")): c for c in todo}
    for fut in as_completed(futs):
        code = futs[fut]
        i += 1
        try:
            frame, source = fut.result()
            if frame.empty:
                raise RuntimeError("空数据")
            store._store(code, frame, "5", source=source)
            ok.append(code)
        except Exception as e:
            failed.append((code, str(e)[:80]))
        if i % 25 == 0:
            log(f"进度 {i}/{len(todo)} ok={len(ok)} fail={len(failed)} "
                f"剩余≈{(len(todo)-i)*6/WORKERS/60:.0f}分钟")

log(f"完成: 补数成功 {len(ok)}, 失败 {len(failed)}, 原本完整 {len(done)}")
if failed:
    for c, e in failed[:20]:
        log(f"  FAIL {c}: {e}")
json.dump({"ok": ok, "failed": failed, "done": done},
          open(f"{OUT}/backfill_state.json", "w"), ensure_ascii=False)
log("BACKFILL_OK")
