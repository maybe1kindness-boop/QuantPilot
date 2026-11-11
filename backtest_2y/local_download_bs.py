# -*- coding: utf-8 -*-
"""本地(Mac)经 baostock 拉取 61 只候选股 2021-12→2024-08 的5分钟K线。

输出: bs_5min/{code}.csv (timestamp,open,high,low,close,volume,amount)
断点续传: 已存在且非空的 csv 跳过; 写 .tmp 完成后原子改名。
"""
import os
import time

import baostock as bs
import pandas as pd

BASE_DIR = "./backtest_2y"
OUT = f"{BASE_DIR}/bs_5min"
os.makedirs(OUT, exist_ok=True)

codes = [c for c in open(f"{BASE_DIR}/need_bs_codes.txt").read().split() if c]
edges = ["2021-12-01", "2022-04-01", "2022-08-01", "2022-12-01",
         "2023-04-01", "2023-08-01", "2023-12-01", "2024-04-01",
         "2024-08-01", "2024-08-25"]
FIELDS = "time,open,high,low,close,volume,amount"


def bs_code(c):
    return ("sh." if c.startswith("6") else "sz.") + c


def relogin():
    try:
        bs.logout()
    except Exception:
        pass
    time.sleep(1.5)
    lg = bs.login()
    return lg.error_code == "0"


assert relogin(), "baostock 登录失败"
t0 = time.time()
done = skip = fail = 0
fails = []
for i, code in enumerate(codes):
    path = f"{OUT}/{code}.csv"
    if os.path.exists(path) and os.path.getsize(path) > 100:
        skip += 1
        continue
    df = None
    for attempt in range(4):
        try:
            frames = []
            for a, b in zip(edges, edges[1:]):
                rs = bs.query_history_k_data_plus(
                    bs_code(code), FIELDS, start_date=a, end_date=b,
                    frequency="5", adjustflag="3")
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                if rs.error_code != "0":
                    raise RuntimeError(f"{rs.error_code}:{rs.error_msg}")
                if rows:
                    frames.append(pd.DataFrame(rows, columns=FIELDS.split(",")))
            df = pd.concat(frames, ignore_index=True) if frames else \
                pd.DataFrame(columns=FIELDS.split(","))
            break
        except Exception as e:
            print(f"[{time.time()-t0:5.0f}s] {code} 尝试{attempt+1} 失败: {e}",
                  flush=True)
            if not relogin():
                time.sleep(3)
    if df is None:
        fail += 1
        fails.append(code)
        continue
    if len(df):
        t = df["time"].str[:14]
        df["timestamp"] = (t.str[:4] + "-" + t.str[4:6] + "-" + t.str[6:8]
                           + " " + t.str[8:10] + ":" + t.str[10:12] + ":" + t.str[12:14])
        df = df[df.timestamp >= "2021-12-01"]
    df[["timestamp", "open", "high", "low", "close", "volume", "amount"]] \
        .to_csv(path + ".tmp", index=False)
    os.rename(path + ".tmp", path)
    done += 1
    if (done + skip) % 10 == 0 or i == len(codes) - 1:
        print(f"[{time.time()-t0:5.0f}s] 进度 {i+1}/{len(codes)} "
              f"新拉={done} 跳过={skip} 失败={fail}", flush=True)

try:
    bs.logout()
except Exception:
    pass
print(f"DOWNLOAD_OK 新拉={done} 跳过={skip} 失败={fail} {fails}", flush=True)
