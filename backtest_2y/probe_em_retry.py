# -*- coding: utf-8 -*-
"""重试分钟K线:默认参数/不同股票/带重试与sleep。"""
import time

import akshare as ak

print("akshare", ak.__version__, flush=True)

for attempt, kwargs in enumerate([
    dict(symbol="000001", period="5"),
    dict(symbol="600000", period="5"),
    dict(symbol="000001", period="5",
         start_date="2024-09-01 09:30:00", end_date="2026-09-04 15:00:00"),
]):
    for i in range(3):
        try:
            df = ak.stock_zh_a_hist_min_em(**kwargs)
            print(f"OK {kwargs.get('symbol')} p={kwargs['period']} "
                  f"rows={len(df)} {df['时间'].min()} → {df['时间'].max()}", flush=True)
            break
        except Exception as e:
            print(f"attempt{i} {kwargs.get('symbol')}: {type(e).__name__} {e}", flush=True)
            time.sleep(4)
    time.sleep(2)
