# -*- coding: utf-8 -*-
"""年线闸门 + 红利篮子状态任务(每日 20:30 由 systemd 触发)。

1) 用 baostock 上证指数日线计算: 昨收 vs 200日均线 → runtime/data/market_gate.json
   {as_of, close, ma200, open, updated_at}; open=True 表示次日机器允许开新仓。
2) 每年1月(或文件缺失时)刷新红利篮子 → runtime/data/dividend_basket.json
   规则: 候选池22只, 取(y-4,y-3,y-2)三个除权年度的年均每股现金分红
   / 上年末收盘, 股息率前10等权(与5年回测口径一致)。
   首次生成写入用户2026-09-05批准的初始清单(近12月口径)。
原子写(.tmp+rename), 失败保留旧文件并退出码1。
"""
import datetime
import json
import os
import sys
import time

import baostock as bs
import pandas as pd

DATA_DIR = "/opt/quantpilot/runtime/data"
GATE_PATH = f"{DATA_DIR}/market_gate.json"
BASKET_PATH = f"{DATA_DIR}/dividend_basket.json"

POOL = {
    "601398": "工商银行", "601939": "建设银行", "601288": "农业银行",
    "601988": "中国银行", "600036": "招商银行", "601166": "兴业银行",
    "601088": "中国神华", "601225": "陕西煤业", "600985": "淮北矿业",
    "600900": "长江电力", "600025": "华能水电", "600886": "国投电力",
    "600941": "中国移动", "601728": "中国电信", "600050": "中国联通",
    "601857": "中国石油", "600938": "中国海油", "601006": "大秦铁路",
    "000895": "双汇发展", "600887": "伊利股份", "601333": "广深铁路",
    "600919": "紫金矿业",
}

APPROVED_INITIAL = [
    ("601166", "兴业银行", 5.85), ("600887", "伊利股份", 5.14),
    ("600036", "招商银行", 4.84), ("600919", "紫金矿业", 4.59),
    ("601006", "大秦铁路", 4.59), ("601088", "中国神华", 4.22),
    ("601857", "中国石油", 4.21), ("601728", "中国电信", 3.94),
    ("600050", "中国联通", 3.85), ("601398", "工商银行", 3.82),
]


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def atomic_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def pull_closes(bs_code, start, end, field="close"):
    rs = bs.query_history_k_data_plus(
        bs_code, "date,close", start_date=start, end_date=end,
        frequency="d", adjustflag="3")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if rs.error_code != "0":
        raise RuntimeError(f"{bs_code}: {rs.error_code} {rs.error_msg}")
    return pd.Series({d: float(c) for d, c in rows}, name=field)


def update_gate():
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=430)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    s = pull_closes("sh.000001", start, end)
    if len(s) < 200:
        raise RuntimeError(f"上证日线不足200根({len(s)})")
    ma200 = float(s.rolling(200).mean().iloc[-1])
    close = float(s.iloc[-1])
    as_of = str(s.index[-1])
    state = {
        "as_of": as_of, "close": round(close, 2), "ma200": round(ma200, 2),
        "open": bool(close > ma200),
        "rule": "上证收盘>200日均线=开; 否则停新仓(熊市战法)",
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    atomic_json(GATE_PATH, state)
    log(f"闸门: {as_of} 收盘{close:.2f} MA200 {ma200:.2f} → "
        f"{'开' if state['open'] else '关'}")
    return state


def query_dividends(code, years):
    bc = ("sh." if code.startswith("6") else "sz.") + code
    buckets = {y: 0.0 for y in years}
    seen = set()
    for y in years:
        rs = bs.query_dividend_data(code=bc, year=str(y), yearType="operate")
        fi = rs.fields.index("dividCashPsBeforeTax")
        oi = rs.fields.index("dividOperateDate")
        while rs.error_code == "0" and rs.next():
            r = rs.get_row_data()
            d = r[oi]
            try:
                v = float(r[fi] or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0 and d and (d, v) not in seen:
                seen.add((d, v))
                buckets[int(d[:4])] = buckets.get(int(d[:4]), 0.0) + v
        time.sleep(0.05)
    return buckets


def refresh_basket():
    year = datetime.date.today().year
    prev_last = f"{year - 1}-12-31"
    start_px = f"{year - 1}-06-01"
    years3 = (year - 4, year - 3, year - 2)
    ranked = []
    for code, name in POOL.items():
        try:
            s = pull_closes(("sh." if code.startswith("6") else "sz.") + code,
                            start_px, prev_last)
            p = float(s.iloc[-1]) if len(s) else 0.0
            b = query_dividends(code, years3)
            d3 = sum(b.get(y, 0.0) for y in years3)
            if p > 0 and d3 > 0:
                ranked.append((d3 / 3.0 / p, code, name))
        except Exception as e:
            log(f"  {code} {name} 拉取失败: {e}")
    ranked.sort(reverse=True)
    basket = {
        "as_of": f"{year}-01", "rule": "近3除权年度年均现金分红/上年末收盘, "
        "候选池22只取前10等权; 每年1月自动刷新",
        "year": year,
        "stocks": [{"code": c, "name": n, "yield_pct": round(v * 100, 2)}
                   for v, c, n in ranked[:10]],
        "benchmarks": [{"code": c, "name": n, "yield_pct": round(v * 100, 2)}
                       for v, c, n in ranked[10:]],
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    atomic_json(BASKET_PATH, basket)
    log(f"篮子刷新({year}): " + "、".join(
        f"{s['name']}{s['yield_pct']}" for s in basket["stocks"]))


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    bs.login()
    try:
        update_gate()
        today = datetime.date.today()
        need = not os.path.exists(BASKET_PATH)
        if not need:
            try:
                need = json.load(open(BASKET_PATH, encoding="utf-8")) \
                    .get("year") != today.year and today.month == 1
            except Exception:
                need = True
        if need:
            if not os.path.exists(BASKET_PATH):
                atomic_json(BASKET_PATH, {
                    "as_of": "2026-09-05",
                    "rule": "初始清单=近12月除权现金/现价前10(2026-09-05批准); "
                            "每年1月按'近3除权年度年均分红/上年末价'自动刷新",
                    "year": 2026,
                    "stocks": [{"code": c, "name": n, "yield_pct": y}
                               for c, n, y in APPROVED_INITIAL],
                    "updated_at": datetime.datetime.now()
                    .isoformat(timespec="seconds"),
                })
                log("篮子: 写入已批准的初始清单")
            else:
                refresh_basket()
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    log("GATE_JOB_OK")


if __name__ == "__main__":
    main()
