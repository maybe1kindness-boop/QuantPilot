# -*- coding: utf-8 -*-
"""把红利篮子10只按09-04收盘价写入模拟盘账户(30%权益, 等权)。

- pt_trade(filled, 15:00:00收盘成交) + pt_position + pt_position_lot
- pt_meta: cash 扣减; 新增 dividend_basket_codes 标记(供notifier豁免)
- 手续费: 佣金 max(金额*2.5bp, 5) + 过户费 金额*1bp; 买入无印花税
- 幂等: 已有标记则跳过
"""
import json
import shutil
import sqlite3

import baostock as bs
import pandas as pd

DB = "/opt/quantpilot/runtime/data/paper_trading.db"
BACKUP = DB + ".before_basket_20260905"
BASKET = json.load(open("/opt/quantpilot/runtime/data/dividend_basket.json",
                        encoding="utf-8"))
STOCKS = BASKET["stocks"]
TRADE_DATE, TRADE_TIME = "2026-09-04", "15:00:00"
COMM, COMM_MIN, TRANSFER = 2.5e-5, 5.0, 1e-5
TARGET_PCT = 0.30

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

if conn.execute(
        "SELECT value FROM pt_meta WHERE key='dividend_basket_codes'"
).fetchone():
    print("ALREADY_SEEDED")
    raise SystemExit(0)

shutil.copy2(DB, BACKUP)

cash = float(conn.execute(
    "SELECT value FROM pt_meta WHERE key='cash'").fetchone()["value"])
pos_val = conn.execute(
    "SELECT COALESCE(SUM(quantity*avg_cost),0) v FROM pt_position "
    "WHERE quantity > 0").fetchone()["v"]
equity = cash + pos_val
budget = equity * TARGET_PCT
per = budget / len(STOCKS)
print(f"现金 {cash:,.0f} + 持仓 {pos_val:,.0f} = 权益 {equity:,.0f}; "
      f"篮子预算 {budget:,.0f}, 每只 {per:,.0f}")

bs.login()
fills = []
for s in STOCKS:
    code = s["code"]
    rs = bs.query_history_k_data_plus(
        ("sh." if code.startswith("6") else "sz.") + code, "date,close",
        start_date="2026-09-01", end_date="2026-09-04",
        frequency="d", adjustflag="3")
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    px = float(rows[-1][1])
    qty = int(per / px / 100) * 100
    amt = qty * px
    fee = max(amt * COMM, COMM_MIN) + amt * TRANSFER
    fills.append((code, s["name"], qty, px, amt, fee))
bs.logout()

spend = sum(a + f for _, _, _, _, a, f in fills)
assert spend < cash, f"现金不足: 需{spend:,.0f} 有{cash:,.0f}"
print(f"合计买入 {sum(q for _,_,q,_,_,_ in fills)} 股, 花费 {spend:,.2f}")

for code, name, qty, px, amt, fee in fills:
    total = amt + fee
    conn.execute(
        "INSERT INTO pt_trade (trade_date, trade_time, code, name, side, "
        "quantity, price, amount, commission, stamp_tax, transfer_fee, total, "
        "status, signal_msg) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TRADE_DATE, TRADE_TIME, code, name, "buy", qty, px, amt,
         max(amt * COMM, COMM_MIN), 0.0, amt * TRANSFER, total, "filled",
         "红利篮子建仓(熊市战法长持腿; 不参与机器止损/到期, 每年1月换仓)"))
    conn.execute(
        "INSERT INTO pt_position (code, name, quantity, available, avg_cost, "
        "first_buy_date, high_water_price, exit_stage, rally_exit_stage, "
        "rally_exit_base_quantity, rally_exit_first_breakout_price, "
        "rally_exit_first_sell_price, rally_exit_second_sell_price, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(code) DO UPDATE SET quantity=quantity+?, "
        "avg_cost=(avg_cost*quantity+?)/(quantity+?), updated_at=?",
        (code, name, qty, qty, total / qty, TRADE_DATE, px, 0, 0, 0, 0, 0, 0,
         TRADE_DATE, qty, total, qty, TRADE_DATE))
    conn.execute(
        "INSERT INTO pt_position_lot (code, buy_date, quantity, price, fee, "
        "remaining) VALUES (?,?,?,?,?,?)",
        (code, TRADE_DATE, qty, px, fee, qty))
    print(f"  {code} {name}: {qty}股 × {px} = {amt:,.0f} (+费{fee:.2f})")

conn.execute(
    "INSERT OR REPLACE INTO pt_meta (key, value) VALUES ('cash', ?)",
    (str(cash - spend),))
conn.execute(
    "INSERT OR REPLACE INTO pt_meta (key, value) VALUES "
    "('dividend_basket_codes', ?)",
    (",".join(s["code"] for s in STOCKS),))
conn.commit()

chk_cash = float(conn.execute(
    "SELECT value FROM pt_meta WHERE key='cash'").fetchone()["value"])
n = conn.execute("SELECT COUNT(*) FROM pt_position WHERE quantity>0").fetchone()[0]
print(f"校验: 现金 {chk_cash:,.2f}, 持仓股数 {n}")
assert abs(cash - spend - chk_cash) < 0.01
conn.close()
print("SEED_BASKET_OK")
