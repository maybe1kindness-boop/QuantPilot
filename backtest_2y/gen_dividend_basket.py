# -*- coding: utf-8 -*-
"""生成红利股篮子: ①2022年初信息选篮→2022-2026逐年换仓回测(无偷看)
②今日篮子(近12月分红/现价)。
数据: baostock 日线 + query_dividend_data(按除权除息日)。
"""
import time
from collections import defaultdict

import baostock as bs
import pandas as pd

CODES = {
    "601398": "工商银行", "601939": "建设银行", "601288": "农业银行",
    "601988": "中国银行", "600036": "招商银行", "601166": "兴业银行",
    "601088": "中国神华", "601225": "陕西煤业", "600985": "淮北矿业",
    "600900": "长江电力", "600025": "华能水电", "600886": "国投电力",
    "600941": "中国移动", "601728": "中国电信", "600050": "中国联通",
    "601857": "中国石油", "600938": "中国海油", "601006": "大秦铁路",
    "000895": "双汇发展", "600887": "伊利股份", "601333": "广深铁路",
    "600919": "紫金矿业",
}
BSC = {c: ("sh." if c.startswith("6") else "sz.") + c for c in CODES}

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:5.0f}s] {m}", flush=True)

bs.login()

# ---- 日线 ----
px = {}
for c, bc in BSC.items():
    rs = bs.query_history_k_data_plus(
        bc, "date,close", start_date="2018-01-01", end_date="2026-09-05",
        frequency="d", adjustflag="3")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    px[c] = pd.Series({d: float(v) for d, v in rows}, name=c)
px = pd.DataFrame(px)
px.index = pd.to_datetime(px.index)
log(f"日线 {px.index[0].date()}→{px.index[-1].date()} {px.shape}")

# ---- 分红(按除权除息日, 每股税前现金, 同日多行去重) ----
div = {}  # (code, year) -> 每股现金合计
div_dated = defaultdict(list)  # code -> [(opdate, cash)]
for c, bc in BSC.items():
    seen = set()
    for y in range(2018, 2027):
        rs = bs.query_dividend_data(code=bc, year=str(y), yearType="operate")
        fi = rs.fields.index("dividCashPsBeforeTax")
        oi = rs.fields.index("dividOperateDate")
        while rs.error_code == "0" and rs.next():
            r = rs.get_row_data()
            d = r[oi]
            try:
                v = float(r[fi] or 0)
            except (ValueError, TypeError):
                v = 0.0
            if v > 0 and d and (d, v) not in seen:
                seen.add((d, v))
                div_dated[c].append((d, v))
        time.sleep(0.05)
    for d, v in div_dated[c]:
        k = (c, int(d[:4]))
        div[k] = div.get(k, 0.0) + v
log(f"分红记录 {sum(len(v) for v in div_dated.values())} 条")


def trailing_div(code, end_date, months=36):
    """end_date 前推months个月内除权的每股现金合计(近似: 按自然年桶)。"""
    y_end = end_date.year
    n = months // 12
    tot = 0.0
    for y in range(y_end - n, y_end + 1):
        tot += div.get((code, y), 0.0)
    # 去掉end_date之后才除权的年份整桶误差可接受(年末选篮时当年多为年报未除权)
    return tot


# ---- ① 逐年无偷看回测: 每年1月首个交易日, 用过去3个完整自然年分红/上年末收盘 ----
years = [2022, 2023, 2024, 2025, 2026]
baskets = {}
daily_r = []
held = None
for y in years:
    sel_date = px.index[px.index.year == y][0]
    prev_last = px.index[px.index < sel_date][-1]
    yl = []
    for c in CODES:
        p = px[c].asof(prev_last)
        d3 = sum(div.get((c, yy), 0.0) for yy in (y - 4, y - 3, y - 2))
        if p and p > 0 and d3 > 0:
            yl.append((d3 / 3.0 / p, c))
    top = sorted(yl, reverse=True)[:10]
    basket = [c for _, c in top]
    baskets[y] = [(CODES[c], c, round(v * 100, 2)) for v, c in top]
    yr_px = px.loc[(px.index.year == y) & (px.index <= "2026-09-04"), basket]
    daily_r.append(yr_px.pct_change().fillna(0).mean(axis=1))
    log(f"{y}篮子: {baskets[y]}")

r = pd.concat(daily_r)
r.rename("basket").to_csv("basket_daily.csv", index_label="date")
v = (1 + r).cumprod()
dd = (v / v.cummax() - 1).min()
print(f"\n无偷看红利篮子(每年重选前10等权) 总收益 {v.iloc[-1]-1:+.1%} "
      f"最大回撤 {dd:+.1%}")
prev = 1.0
for yr, seg in v.groupby(v.index.year):
    print(f"  {yr}: {seg.iloc[-1]/prev-1:+.1%}")
    prev = seg.iloc[-1]

# ---- ② 今日篮子: 近12月分红/现价 ----
print("\n今日篮子(近12月除权现金/2026-09-04收盘):")
today = []
asof = px.index[-1]
cutoff = "2025-09-05"
for c in CODES:
    p = px[c].asof(asof)
    d1 = sum(v for d, v in div_dated.get(c, []) if d >= cutoff)
    if p and p > 0 and d1 > 0:
        today.append((d1 / p, c))
for v_, c in sorted(today, reverse=True):
    print(f"  {CODES[c]:<6} {c}  股息率 {v_*100:5.2f}%")

bs.logout()
print("\nBASKET_OK")
