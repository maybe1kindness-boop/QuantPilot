# -*- coding: utf-8 -*-
"""探测东财分钟K线历史深度:5分钟/15分钟各能回溯多远。"""
import akshare as ak

for period in ("5", "15"):
    try:
        df = ak.stock_zh_a_hist_min_em(
            symbol="000001", start_date="2024-09-01 09:30:00",
            end_date="2026-09-05 15:00:00", period=period, adjust="")
        print(f"period={period}: rows={len(df)}", flush=True)
        if len(df):
            print("  时间范围:", df["时间"].min(), "→", df["时间"].max(), flush=True)
            print("  列:", list(df.columns), flush=True)
            print("  样例:", df.iloc[0].to_dict(), flush=True)
    except Exception as e:
        print(f"period={period}: 失败 {e}", flush=True)
