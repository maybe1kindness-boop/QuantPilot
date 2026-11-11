# -*- coding: utf-8 -*-
"""阶段32: 外围科技闸门回放数据准备。

生产规则(utils/external_environment.py _opening_decision, 只列可回放的):
  1) 纳指上一美股收盘涨跌 < -1.0% -> 风险(拦)
  2) 核心题材宽度: SMH/AIQ/IGV/SKYY 上一美股收盘上涨只数占比 < 0.5 -> 拦
不可回放(不模拟, 回测口径注明): 韩国KOSDAQ实时走稳、VIX>28、数据缺失fail-closed。
拦截范围(trading_alert_notifier._external_technology_exposures):
  行业=stock_basic.industry 对 config 关键词做子串匹配(upper);
  题材关键词现为空列表=题材匹配不生效。
数据源: akshare index_us_stock_sina(.IXIC) + stock_us_daily(ETF), 新浪源。
输出 extgate_data.pkl {gate_days, exposed, yearly统计}。
校验: 用 2026-08-18 以来生产晨间快照(每日最早一枚>=09:00)对照重建结果。
"""
import glob
import json
import sqlite3
import sys
import time
from bisect import bisect_right
from collections import defaultdict

import pandas as pd

sys.path.insert(0, "/opt/quantpilot")
OUT = "/opt/quantpilot/runtime/backtest_2y"
W_START, W_END = "2022-01-01", "2026-09-06"
INDUSTRY_KEYWORDS = [
    "半导体", "芯片设计", "集成电路", "分立器件", "印制电路板", "被动元件",
    "电子化学品", "电子", "光学元件", "激光设备", "软件", "IT服务",
    "计算机设备", "通信", "机器人", "自动化", "工控设备",
]

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:6.1f}s] {m}", flush=True)

import akshare as ak  # noqa: E402

# 1) 拉美股日线(新浪源)
us_close = {}
for sym, fn in [(".IXIC", "idx")] + [(s, "etf") for s in
                                     ("SMH", "AIQ", "IGV", "SKYY")]:
    df = (ak.index_us_stock_sina(symbol=sym) if fn == "idx"
          else ak.stock_us_daily(symbol=sym))
    df["date"] = df["date"].astype(str)
    us_close[sym] = dict(zip(df["date"], df["close"].astype(float)))
    log(f"{sym}: {len(df)} 天 ({df['date'].iloc[0]}..{df['date'].iloc[-1]})")
    time.sleep(0.5)

# 2) 每个美股日的变化率(对上一美股收盘)
us_chg = {}
for sym, closes in us_close.items():
    ds = sorted(closes)
    chg = {}
    for i in range(1, len(ds)):
        prev, cur = closes[ds[i - 1]], closes[ds[i]]
        if prev > 0:
            chg[ds[i]] = cur / prev - 1.0
    us_chg[sym] = (ds, chg)

ETFS = ["SMH", "AIQ", "IGV", "SKYY"]

# 3) 对每个A股交易日 D: 上一已完成美股收盘 = 最后一个日期 <= D-1(日历)的美股日
#    (美股日 U 的收盘在北京时间 U+1 凌晨04/05点完成, 早于 D 日 09:30 开盘
#     <=> U+1 <= D <=> U <= D-1; 跨周末/美股节假日自动落到最近的美股日)
import datetime as _dt

sh = pd.read_csv(f"{OUT}/sh_long.csv", parse_dates=["date"])
cn_dates = sorted(str(d.date()) for d in sh["date"])

gate_days, by_rule = set(), defaultdict(int)
yearly = defaultdict(int)
for D in cn_dates:
    if not (W_START <= D <= W_END):
        continue
    lim = str(_dt.date.fromisoformat(D) - _dt.timedelta(days=1))
    ds, chg = us_chg[".IXIC"]
    j = bisect_right(ds, lim) - 1
    if j < 1:
        continue
    u = ds[j]
    nas = chg.get(u, 0.0)
    up = 0
    for e in ETFS:
        eds, echg = us_chg[e]
        ej = bisect_right(eds, lim) - 1
        if ej >= 1 and echg.get(eds[ej], 0.0) > 0:
            up += 1
    breadth = up / 4.0
    hit = False
    if nas < -0.01:
        by_rule["nasdaq"] += 1
        hit = True
    if breadth < 0.5:
        by_rule["breadth"] += 1
        hit = True
    if hit:
        gate_days.add(D)
        yearly[D[:4]] += 1
log(f"闸门关闭日 {len(gate_days)} 天 / {sum(1 for d in cn_dates if W_START<=d<=W_END)} 个A股日"
    f" (纳指规则{by_rule['nasdaq']} 宽度规则{by_rule['breadth']})")
log("分年关闭天数: " + " ".join(f"{y}:{n}" for y, n in sorted(yearly.items())))

# 4) 行业暴露集(stock_basic.industry 子串匹配, 与生产一致含upper)
conn = sqlite3.connect(
    "file:/opt/quantpilot/runtime/data/stock_selection.db?mode=ro", uri=True)
exposed = set()
ind_cnt = defaultdict(int)
for code, ind in conn.execute("SELECT code, industry FROM stock_basic"):
    ind = str(ind or "").strip()
    if ind and any(k.upper() in ind.upper() for k in INDUSTRY_KEYWORDS):
        exposed.add(str(code).zfill(6))
        ind_cnt[ind] += 1
cands = pd.read_pickle(f"{OUT}/candidates_5y.pkl")
cand_codes = set(cands["code"].astype(str).str.zfill(6))
log(f"行业暴露股 {len(exposed)} 只 / 候选池命中 {len(exposed & cand_codes)}"
    f" / 候选池共 {len(cand_codes)} 只")
log("暴露行业TOP: " + ", ".join(f"{k}:{v}" for k, v in
    sorted(ind_cnt.items(), key=lambda x: -x[1])[:10]))

# 5) 生产快照对照(2026-08-18 以来每日最早一枚 >=09:00 的快照)
snap_days = defaultdict(list)
for f in sorted(glob.glob(
        "/opt/quantpilot/runtime/data/external_environment/2026-*.json")):
    base = f.split("/")[-1]
    day, hhmm = base[:10], base[11:15]
    snap_days[day].append((hhmm, f))
n_match = n_korea_vix = n_data = 0
for day in sorted(snap_days):
    hhmm, f = min(snap_days[day])
    if hhmm < "0900":
        continue
    d = json.load(open(f))
    od = d.get("opening_decision") or {}
    prod_open = bool(od.get("allow_open", True))
    reasons = od.get("risk_reasons") or []
    issues = od.get("data_issues") or []
    recon = day not in gate_days
    if recon == prod_open:
        n_match += 1
        tag = "一致"
    elif issues or any("韩国" in r or "VIX" in r for r in reasons):
        n_korea_vix += 1
        tag = "差异(韩/VIX/数据, 不可回放项)"
    else:
        n_data += 1
        tag = "差异(其他!)"
    print(f"  {day} 晨间快照 prod_open={prod_open} recon_open={recon} {tag}"
          + (f" reasons={reasons}" if (not prod_open and recon) else ""))
log(f"快照对照: 一致{n_match} 韩/VIX/数据差异{n_korea_vix} 其他差异{n_data}")

pd.to_pickle({"gate_days": sorted(gate_days), "exposed": sorted(exposed),
              "yearly": dict(yearly), "by_rule": dict(by_rule)},
             f"{OUT}/extgate_data.pkl")
log("extgate_data.pkl 写入完成; SIM32_OK")
