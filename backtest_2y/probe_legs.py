# -*- coding: utf-8 -*-
"""探测 baostock 可用的慢腿标的: 小盘指数/红利/黄金/国债。"""
import baostock as bs

CANDS = [
    ("sz.399303", "国证2000"),
    ("sh.000852", "中证1000"),
    ("sh.000922", "中证红利(价格)"),
    ("sh.510880", "红利ETF"),
    ("sh.518880", "黄金ETF"),
    ("sz.159934", "易方达黄金ETF"),
    ("sh.511260", "十年国债ETF"),
    ("sh.511010", "国债ETF"),
    ("sh.000001", "上证指数(对照)"),
]

bs.login()
for code, name in CANDS:
    rs = bs.query_history_k_data_plus(
        code, "date,close", start_date="2021-12-01", end_date="2022-01-10",
        frequency="d", adjustflag="3")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    first = rows[0][0] if rows else "-"
    print(f"{code} {name}: {len(rows)}根 首日={first} err={rs.error_msg}")
bs.logout()
