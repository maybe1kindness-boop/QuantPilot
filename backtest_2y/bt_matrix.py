# -*- coding: utf-8 -*-
"""按服务器当前策略(2026-10-15 生效配置)从零实现的 2022→今 全账户回测(内存安全版)。

策略口径(来源 config/config.yaml + 生产代码 + 2026-10-15 实验结论):
  机器腿(服务器选股策略): 非ST/60,00,30板/上市≥90天; 成交额>20亿, 市值<300亿,
    近20日涨停≥2且<6, 收盘站上10日线且10日线向上, 乖离(2%,8%), 换手(10%,20%),
    价<50; 强度排序。次日 09:45-10:30/13:30-14:00 窗口 5分钟VWAP确认入场
    (开盘低点不破/高于VWAP/乖离状态机/涨幅>7%不追/距日内高点≥0.5%/高于开盘价)。
  熊池腿(2026-10-15 新规): bias10≤-15% & ret20≤-20% & 2≤价<50 & 量比≥1 &
    成交额>2亿; 弹性组合分(beta60+20日振幅+60日波动率+超跌深度+低价)排序取前5;
    信号次日开盘直接买入(09:35-10:30), 仅保留开盘涨幅>7%防追高与涨停不买;
    量能确认闸已禁用。仅年线闸门关闭期间开仓。
  仓位: 机器买入不因闸门停止, 闸门只切换仓位上限——年线闸门开(上证>200日线
        连续2天)→单票30%/总仓位90%; 闸门关→机器票(机器+熊池共享)合计≤总资金30%、
        单票≤10%。持仓≤3只(篮子不占名额); 周五不开新仓; 当日止损2次停买; T+1。
  卖出: -5%止损(14:55尾盘复核, 高点浮盈曾达10%后上移保本) / +15%卖30% /
        +30%再卖30% / 浮盈回撤10%移动止盈 / 涨停浮盈≥20%兑现30% /
        持仓6天到期提前至第5日14:55尾盘清仓。
  现金停泊: 闲钱停靠红利篮子(10只等权前复权日收益), 机器出入金计往返摩擦
        (卖出腿0.126%/买回腿0.026%)。
近似口径(如实标注): 换手/市值=baostock当日实际流通换手推导; 2022-2025 部分
  日期无5分钟线, 该场景机器腿按"次日开盘价近似成交"(开盘涨幅>7%或涨停则放弃),
  持仓退出按日线OHLC近似; ST为当前名称快照。
"""
import gc
import json
import pickle
import sqlite3
import sys
import time
from collections import defaultdict

import numpy as np
import pandas as pd

BASE = "/opt/quantpilot"
OUT = f"{BASE}/runtime/backtest_current"
DB = f"{BASE}/runtime/data/stock_selection.db"
CAP_MODE = sys.argv[1] if len(sys.argv) > 1 else ""     # ""/nocap/cap15/cap20/volcap30/volband20
# 归因矩阵扩展: volfloor0/08/12=熊池量能下限; beardepth=熊池按乖离深度排序;
# nofriday=周五照常开仓; noconfirm=机器腿轻确认(窗口首根K线即买,保留防追高);
# nostop2=当日止损2次熔断关; nomax3=持仓3只上限关
BEAR_DEPTH = CAP_MODE == "beardepth"
NO_FRIDAY = CAP_MODE == "nofriday"
NO_CONFIRM = CAP_MODE == "noconfirm"
NO_STOP2 = CAP_MODE == "nostop2"
NO_MAX3 = CAP_MODE == "nomax3"
MAX_POS = 999 if NO_MAX3 else 3
NOCAP = CAP_MODE == "nocap"
SINGLE_CAP, TOTAL_CAP = {"": (0.10, 0.30), "nocap": (0.30, 0.90),
                         "cap15": (0.15, 0.45), "cap20": (0.20, 0.60),
                         "volcap30": (0.10, 0.30), "volband20": (0.10, 0.30),
                         "volfloor0": (0.10, 0.30), "volfloor08": (0.10, 0.30),
                         "volfloor12": (0.10, 0.30), "beardepth": (0.10, 0.30),
                         "nofriday": (0.10, 0.30), "noconfirm": (0.10, 0.30),
                         "nostop2": (0.10, 0.30), "nomax3": (0.10, 0.30)}[CAP_MODE]
VOL_LO, VOL_HI = {"": (1.0, np.inf), "nocap": (1.0, np.inf), "cap15": (1.0, np.inf),
                  "cap20": (1.0, np.inf), "volcap30": (1.0, 3.0),
                  "volband20": (1.0, 2.0), "volfloor0": (0.0, np.inf),
                  "volfloor08": (0.8, np.inf), "volfloor12": (1.2, np.inf),
                  "beardepth": (1.0, np.inf), "nofriday": (1.0, np.inf),
                  "noconfirm": (1.0, np.inf), "nostop2": (1.0, np.inf),
                  "nomax3": (1.0, np.inf)}[CAP_MODE]
SUFFIX = {"": "", "nocap": "_nocap", "cap15": "_cap15", "cap20": "_cap20",
          "volcap30": "_volcap30", "volband20": "_volband20",
          "volfloor0": "_volfloor0", "volfloor08": "_volfloor08",
          "volfloor12": "_volfloor12", "beardepth": "_beardepth",
          "nofriday": "_nofriday", "noconfirm": "_noconfirm",
          "nostop2": "_nostop2", "nomax3": "_nomax3"}[CAP_MODE]

INIT_CASH = 100000.0
W_START, W_END = "2022-01-01", "2026-10-15"
SCREEN_START = "2021-12-01"
LOAD_START = "2021-06-01"
COMM, COMM_MIN, STAMP, TRANSFER = 2.5e-5, 5.0, 1e-3, 1e-5
FRIC_OUT, FRIC_IN = STAMP + COMM + TRANSFER, COMM + TRANSFER
FEE_B = lambda a: max(a * COMM, COMM_MIN) + a * TRANSFER
FEE_S = lambda a: max(a * COMM, COMM_MIN) + a * TRANSFER + a * STAMP

T48 = ([f"09:{m:02d}" for m in (35, 40, 45, 50, 55)]
       + [f"10:{m:02d}" for m in range(0, 60, 5)]
       + [f"11:{m:02d}" for m in (0, 5, 10, 15, 20, 25, 30)]
       + [f"13:{m:02d}" for m in (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)]
       + [f"14:{m:02d}" for m in range(0, 60, 5)] + ["15:00"])
IDX48 = {t: i for i, t in enumerate(T48)}
WIN = {t for t in T48 if "09:45" <= t <= "10:30" or "13:30" <= t <= "14:00"}
LIMIT_THR = lambda code: 0.20 if code.startswith("30") else 0.10

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)

# ---------------- 1) 基础数据与机器腿筛选 ----------------
sh = pd.read_pickle(f"{OUT}/sh_index.pkl")
basket_ret = pd.read_pickle(f"{OUT}/basket_ret.pkl")
with open(f"{OUT}/turn_map.pkl", "rb") as f:
    turn_map = pickle.load(f)

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
names = {str(r[0]).zfill(6): str(r[1]) for r in
         conn.execute("SELECT code, name FROM stock_basic").fetchall()}
is_st = {c: (("ST" in str(n)) or ("退" in str(n))) for c, n in names.items()}
first_dates = {str(r[0]).zfill(6): str(r[1]) for r in
               conn.execute("SELECT code, MIN(date) FROM stock_kline GROUP BY code")}

frames = []
for p in ("60", "00", "30"):
    chunk = [c for c in names if c.startswith(p) and not is_st.get(c, True)]
    for i in range(0, len(chunk), 400):
        part = chunk[i:i + 400]
        q = ("SELECT code,date,open,high,low,close,volume FROM stock_kline "
             "WHERE date>=? AND date<=? AND code IN (%s)" % ",".join("?" * len(part)))
        frames.append(pd.read_sql_query(q, conn, params=[LOAD_START, W_END] + part))
df = pd.concat(frames, ignore_index=True)
del frames
gc.collect()
df["code"] = df["code"].astype(str).str.zfill(6)
df = df.sort_values(["code", "date"]).reset_index(drop=True)
g = df.groupby("code")
df["prev_close"] = g["close"].shift(1)
board_thr = np.where(df.code.str.startswith("30"), 0.20, 0.10)
df["is_limit"] = (df.close >= np.round(df.prev_close * (1 + board_thr), 2) - 0.005) \
    & df.prev_close.notna()
df["row_in_stock"] = g.cumcount()
df["cnt20"] = g["is_limit"].transform(lambda s: s.rolling(20, min_periods=20).sum())
df["ma10"] = g["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
df["ma10_prev"] = g["ma10"].shift(1)
pos = np.arange(len(df))
lp = pd.Series(np.where(df.is_limit, pos, np.nan), index=df.index)
df["days_since_limit"] = pos - lp.groupby(df["code"]).ffill()
df["amount_est"] = df.volume * 100 * df.close
df["bias10"] = df.close / df.ma10 - 1
df["ma10_up"] = df.ma10 > df.ma10_prev
df["first_date"] = df.code.map(first_dates)
log(f"日线 {len(df)} 行 / {df.code.nunique()} 只, 滚动特征完成")
del g, lp, pos
gc.collect()

listed_ok = pd.to_datetime(df.date) - pd.to_datetime(df.first_date) >= pd.Timedelta(days=90)
pre = (listed_ok & (df.row_in_stock >= 25)
       & (df.amount_est > 20e8) & (df.close < 50)
       & (df.cnt20 >= 2) & (df.cnt20 < 6)
       & (df.close >= df.ma10) & df.ma10_up.astype(bool)
       & (df.bias10 > 0.02) & (df.bias10 < 0.08))
sel = df[pre & (df.date >= SCREEN_START) & (df.date <= W_END)].copy()
n_pre = len(sel)


def turn_of(c, d):
    m = turn_map.get(c)
    return m.get(d, np.nan) if isinstance(m, dict) else np.nan


sel["turn"] = [turn_of(c, str(d)) for c, d in zip(sel.code, sel.date)]
sel["float_shares"] = np.where(sel.turn.notna() & (sel.turn > 0),
                               sel.volume * 100 / (sel.turn / 100.0), np.nan)
sel["mcap_est"] = sel.float_shares * sel.close
sel = sel[(sel.turn > 10) & (sel.turn < 20)
          & sel.mcap_est.notna() & (sel.mcap_est < 300e8)]
log(f"机器腿信号 {len(sel)} 个 / {sel.date.nunique()} 天 "
    f"(预筛 {n_pre}, 换手/市值过滤剔除 {n_pre - len(sel)})")

# 交易日历 + 信号映射
cnt_by_date = df.groupby("date")["code"].count()
CAL = sorted(str(d) for d, n in cnt_by_date.items() if n >= 500)
POS_CAL = {d: i for i, d in enumerate(CAL)}
cand_map = defaultdict(list)
for r in sel.itertuples():
    i = POS_CAL.get(str(r.date))
    if i is None or i + 1 >= len(CAL):
        continue
    cand_map[CAL[i + 1]].append((r.code, float(r.prev_close), int(r.days_since_limit)))
del sel
gc.collect()

# ---------------- 2) 轻量透视 + 熊池 + daily字典 ----------------
slim = df[["date", "code", "open", "high", "low", "close", "volume"]].copy()
del df
gc.collect()
piv = {col: slim.pivot(index="date", columns="code", values=col).sort_index()
       for col in ("open", "high", "low", "close", "volume")}
del slim
gc.collect()
log(f"透视表完成 {piv['close'].shape}")

close, high, low, vol, opn = (piv[k] for k in ("close", "high", "low", "volume", "open"))
ret1 = close.pct_change(fill_method=None)
mkt = ret1.median(axis=1)
bias10p = close / close.rolling(10).mean() - 1
ret20p = close / close.shift(20) - 1
vol_ratio = vol / vol.rolling(5).mean().shift(1)
amountp = vol * 100 * close
qualify = ((bias10p <= -0.15) & (ret20p <= -0.20)
           & (close >= 2.0) & (close < 50)
           & (vol_ratio >= VOL_LO) & (vol_ratio <= VOL_HI) & (amountp > 2e8))
amp = ((high - low) / close).rolling(20).mean()
std60 = ret1.rolling(60, min_periods=20).std()
cov = ret1.rolling(60, min_periods=30).cov(mkt)
beta = cov.div(mkt.rolling(60, min_periods=30).var(), axis=0)
depth = close / close.rolling(120, min_periods=60).max() - 1
score = (beta.rank(axis=1, pct=True).fillna(0.5) + amp.rank(axis=1, pct=True).fillna(0.5)
         + std60.rank(axis=1, pct=True).fillna(0.5)
         + (-depth).rank(axis=1, pct=True).fillna(0.5)
         + (-close).rank(axis=1, pct=True).fillna(0.5)) / 5.0
del ret1, cov, vol_ratio, amountp, amp, std60, beta, depth
gc.collect()
log("熊池特征完成")

bear_map = defaultdict(list)
for d in close.index:
    ds = str(d)
    if ds < SCREEN_START or ds > W_END:
        continue
    i = POS_CAL.get(ds)
    if i is None or i + 1 >= len(CAL):
        continue
    q = qualify.loc[d]
    valid = q.index[q.fillna(False)]
    if len(valid) == 0:
        continue
    if BEAR_DEPTH:
        s5 = (-bias10p.loc[d, valid]).nlargest(5)
    else:
        s5 = score.loc[d, valid].nlargest(5)
    nd = CAL[i + 1]
    cl = close.loc[d]
    for code in s5.index:
        pc = cl.get(code)
        if pc == pc and pc > 0:
            bear_map[nd].append((str(code), float(pc)))
log(f"熊池信号日 {len(bear_map)} 天")
del qualify, score, bias10p, ret20p, mkt
gc.collect()

daily = {}
codes_all = close.columns
for code in codes_all:
    mask = close[code].notna().values
    if not mask.any():
        continue
    dts = close.index.values[mask]
    c = close[code].values[mask]
    daily[str(code)] = {
        "didx": {d: i for i, d in enumerate(dts)},
        "o": opn[code].values[mask], "h": high[code].values[mask],
        "l": low[code].values[mask], "c": c,
        "pc": np.concatenate([[np.nan], c[:-1]]),
    }
del piv, close, high, low, vol, opn, codes_all
gc.collect()
log(f"daily字典 {len(daily)} 只")

# ---------------- 3) 闸门 / 篮子 / 5分钟bar ----------------
sh_s = sh.set_index("date")["close"].astype(float)
sh_s.index = pd.to_datetime(sh_s.index)
ma200 = sh_s.rolling(200).mean()
cal_ts = pd.to_datetime(CAL)
sh_al = sh_s.reindex(cal_ts).ffill()
ab_al = (sh_al > ma200.reindex(cal_ts).ffill()).astype("boolean").fillna(False)
gate = (ab_al & ab_al.shift(1)).shift(1).fillna(False).astype(bool)
GATE = {d: bool(v) for d, v in zip(CAL, gate)}
log(f"闸门开 {sum(GATE.values())} 天 / 共 {len(CAL)} 天 "
    f"(上证末日 {sh_s.iloc[-1]:.0f})")

BK = basket_ret.copy()
BK.index = BK.index.astype(str)
PARK = {d: float(BK.get(d, 0.0)) for d in CAL}

need = defaultdict(set)
for nd, lst in list(cand_map.items()) + list(bear_map.items()):
    i0 = POS_CAL[nd]
    for item in lst:
        code = item[0]
        for k in range(11):
            if i0 + k < len(CAL):
                need[code].add(CAL[i0 + k])
log(f"需5分钟bar: {len(need)} 只")
bars = {}
for code, dates in need.items():
    per = defaultdict(dict)
    dl = sorted(dates)
    for i in range(0, len(dl), 80):
        chunk = dl[i:i + 80]
        q = ("SELECT substr(timestamp,1,10) d, substr(timestamp,12,5) t, "
             "open, high, low, close, volume FROM stock_intraday_kline "
             "WHERE interval='5' AND code=? AND substr(timestamp,1,10) IN (%s)"
             % ",".join("?" * len(chunk)))
        for d, t, o, h, l, c, v in conn.execute(q, [code] + chunk):
            j = IDX48.get(t)
            if j is not None:
                per[d][j] = (o, h, l, c, v)
    bars[code] = {d: [bd.get(j) for j in range(48)] for d, bd in per.items()}
conn.close()
log(f"5分钟bar就绪: {sum(len(v) for v in bars.values())} 个代码-日")


def dget(code, D, k):
    arr = daily.get(code)
    if not arr:
        return None
    j = arr["didx"].get(D)
    return None if j is None else float(arr[k][j])


def confirm(db, i, pclose, day_open, st):
    """机器腿VWAP三闸(与生产同口径)。返回None=通过。"""
    cur = db[i][3]
    fb = next((b for b in db[:i + 1] if b is not None), None)
    if fb is None or cur < fb[2]:
        return "破开盘5分钟低点"
    v = w = ih = 0.0
    for b in db[:i + 1]:
        if b is None:
            continue
        v += b[4]
        w += (b[1] + b[2] + b[3]) / 3.0 * b[4]
        ih = max(ih, b[1])
    if v <= 0:
        return "VWAP无效"
    vwap = w / v
    if cur < vwap:
        return "低于VWAP"
    if pclose > 0 and cur / pclose - 1.0 > 0.07:
        return "当日涨幅>7%"
    if ih > 0 and (ih - cur) / ih < 0.005:
        return "距日内高点<0.5%"
    dev = cur / vwap - 1.0
    if st["s"] == "blocked":
        return "乖离过大已拉黑"
    if dev > 0.025:
        st["s"] = "blocked"
        return "乖离>2.5%拉黑"
    if dev > 0.015:
        st["s"] = "waiting_pullback"
        return "乖离>1.5%等回踩"
    if st["s"] == "waiting_pullback":
        if dev <= 0.005:
            st["s"] = "reclaim_ready"
            return "已回踩待重新站上"
        return "等待回踩VWAP"
    if st["s"] == "reclaim_ready" and dev < 0.002:
        return "待站上VWAP0.2%"
    if cur < day_open:
        return "低于开盘价"
    return None


LABEL = {"stop_loss": "止损-5%(14:55尾盘复核)", "breakeven_stop": "保本止损(高点浮盈≥10%)",
         "max_hold": "持仓6天到期(第5日14:55尾盘清仓)", "trailing": "移动止盈(高点回撤10%)",
         "limit_cash": "涨停兑现(浮盈≥20%卖30%)", "tp1": "首档止盈+15%(卖30%)",
         "tp2": "二档止盈+30%(卖30%)", "eod": "回测截止收盘估值(未平仓)"}

# ---------------- 4) 引擎 ----------------
cash = INIT_CASH
open_pos, closed = [], []
eq_curve, cash_curve = [], []
tot_fric = 0.0
fric_by_year = defaultdict(float)
basket_gain_by_year = defaultdict(float)
stats = defaultdict(int)

for D in CAL:
    if D < W_START or D > W_END:
        continue
    pr = PARK.get(D, 0.0)
    gain = cash * pr / (1.0 + pr)
    cash += gain
    basket_gain_by_year[D[:4]] += gain
    day_buys = day_sells = 0.0
    is_fri = pd.Timestamp(D).dayofweek == 4
    block_fri = is_fri and not NO_FRIDAY
    stops_today = 0
    gate_ok = GATE.get(D, False)
    day_cand = cand_map.get(D, [])
    day_bear = bear_map.get(D, [])
    states = {c: {"s": None} for c, _, _ in day_cand}
    pend = []
    for p in open_pos:
        p["avail"] = p["qty"]
    db_holder = {p["code"]: bars.get(p["code"], {}).get(D) for p in open_pos}

    def eq_est():
        inv = 0.0
        for q in open_pos:
            arr = daily.get(q["code"])
            di = arr["didx"].get(D) if arr else None
            pc = float(arr["c"][di - 1]) if di and di >= 1 else q["cost"]
            inv += q["qty"] * pc
        return cash + inv

    def do_buy(code, px, leg, ct_label):
        global cash, day_buys
        eq = eq_est()
        inv = eq - cash
        # 与生产 signal_engine._calc_buy_plan 同口径:
        # 闸门开: min(单票30%目标, 总仓位90%-持仓, 现金)
        # 闸门关(熊市上限注入): min(单票10%, 机器票合计30%-持仓, 现金) 机器+熊池共享额度
        if gate_ok or NOCAP:
            room = min(0.30 * eq, 0.90 * eq - inv, cash)
        else:
            room = min(SINGLE_CAP * eq, TOTAL_CAP * eq - inv, cash)
        qty = int(room / (px * (1 + COMM)) / 100) * 100
        while qty >= 100 and qty * px + FEE_B(qty * px) > room:
            qty -= 100
        if qty < 100 or room < 6000:
            return False
        amt = qty * px
        fee = FEE_B(amt)
        cash -= amt + fee
        day_buys += amt + fee
        open_pos.append({"code": code, "leg": leg, "qty": qty, "qty_all": qty,
                         "cost": px, "hw": px, "stage": 0, "avail": 0,
                         "edate": D, "et": ct_label,
                         "buy_total": amt + fee, "proceeds": 0.0,
                         "sells": [], "xdate": None, "eod": False})
        db_holder[code] = bars.get(code, {}).get(D)
        return True

    def do_sell(p, px, key, q_sold=None, ct="14:55", is_stop=False):
        global cash, day_sells, stops_today
        q_sold = p["avail"] if q_sold is None else q_sold
        a = q_sold * px
        f = FEE_S(a)
        cash += a - f
        day_sells += a - f
        p["proceeds"] += a - f
        p["qty"] -= q_sold
        p["avail"] -= q_sold
        p["sells"].append((D, ct, LABEL[key], q_sold, px))
        p["xdate"] = D
        if is_stop:
            stops_today += 1

    for i in range(48):
        # 机器腿挂单成交(确认后下一根开盘)
        still = []
        for pe in pend:
            code, pclose, fi = pe
            if i < fi:
                still.append(pe)
                continue
            db = bars.get(code, {}).get(D)
            if db is None or db[i] is None:
                continue
            o = db[i][0]
            lpx = round(pclose * (1 + LIMIT_THR(code)), 2)
            if o < lpx - 0.005:
                do_buy(code, o, "machine", T48[i])
        pend = still

        # 熊池: 开盘直接买(09:35槽; 无bar用当日开盘价)
        if T48[i] == "09:35" and not gate_ok and not block_fri \
                and (stops_today < 2 or NO_STOP2) \
                and len(open_pos) + len(pend) < MAX_POS:
            for code, pc in day_bear:
                if any(p["code"] == code for p in open_pos):
                    continue
                db = bars.get(code, {}).get(D)
                b0 = next((b for b in (db or []) if b is not None), None)
                px = b0[0] if b0 else dget(code, D, "o")
                if px is None or pc <= 0:
                    continue
                lpx = round(pc * (1 + LIMIT_THR(code)), 2)
                if px >= lpx - 0.005 or px / pc - 1.0 > 0.07:
                    continue
                if do_buy(code, px, "bear", "09:35开盘直买"):
                    stats["bear_buys"] += 1

        # 持仓退出
        for p in open_pos:
            if p["qty"] <= 0 or p["edate"] == D or p["avail"] <= 0:
                continue
            code = p["code"]
            db = db_holder.get(code)
            arr = daily.get(code, {})
            di = arr.get("didx", {}).get(D)
            if di is None or p["edate"] not in arr["didx"]:
                continue
            hd = di - arr["didx"][p["edate"]]
            cost = p["cost"]
            d_o, d_h, d_l, d_c = (float(arr["o"][di]), float(arr["h"][di]),
                                  float(arr["l"][di]), float(arr["c"][di]))
            pc_prev = arr["pc"][di]
            pc_prev = float(pc_prev) if pc_prev == pc_prev else None
            if db is None:
                # 日线近似(无5分钟线): 14:00档止盈链, 14:55档止损/到期
                if T48[i] == "14:00":
                    lpx = round(pc_prev * (1 + LIMIT_THR(code)), 2) if pc_prev else None
                    if p["stage"] == 0 and lpx and d_h >= lpx - 0.005 \
                            and lpx / cost - 1.0 >= 0.20:
                        q = int(p["avail"] * 0.3) // 100 * 100 or p["avail"]
                        do_sell(p, lpx, "limit_cash", q, "14:00(日线近似)")
                        stats["limit_cash"] += 1
                    elif p["stage"] == 0 and d_h >= cost * 1.15:
                        fill = max(d_o, cost * 1.15)
                        q = int(p["avail"] * 0.3) // 100 * 100 or p["avail"]
                        do_sell(p, fill, "tp1", q, "14:00(日线近似)")
                    elif p["stage"] == 1 and d_h >= cost * 1.30:
                        fill = max(d_o, cost * 1.30)
                        q = int(p["avail"] * 0.3) // 100 * 100 or p["avail"]
                        do_sell(p, fill, "tp2", q, "14:00(日线近似)")
                    if p["stage"] >= 1 and p["hw"] > cost and d_l <= p["hw"] * 0.90:
                        trig = p["hw"] * 0.90
                        do_sell(p, d_o if d_o <= trig else trig, "trailing",
                                ct="14:00(日线近似)")
                elif T48[i] == "14:55":
                    p["hw"] = max(p["hw"], d_h)
                    if hd >= 5:
                        do_sell(p, d_c, "max_hold", ct="14:55(日线近似)")
                        continue
                    line = 0.0 if p["hw"] / cost - 1 >= 0.10 else -0.05
                    if d_c / cost - 1.0 < line:
                        do_sell(p, d_c,
                                "breakeven_stop" if line >= 0 else "stop_loss",
                                ct="14:55(日线近似)", is_stop=True)
                continue
            b = db[i]
            if b is None:
                continue
            o, h, l, c, v = b
            p["hw"] = max(p["hw"], h)
            if T48[i] == "14:55":
                if hd >= 5:
                    nxt = db[i + 1] if i + 1 < 48 and db[i + 1] else None
                    do_sell(p, nxt[0] if nxt else c, "max_hold", ct="14:55")
                    continue
                line = 0.0 if p["hw"] / cost - 1 >= 0.10 else -0.05
                if c / cost - 1.0 < line:
                    nxt = db[i + 1] if i + 1 < 48 and db[i + 1] else None
                    do_sell(p, nxt[0] if nxt else c,
                            "breakeven_stop" if line >= 0 else "stop_loss",
                            ct="14:55", is_stop=True)
                    continue
            if hd >= 6:
                do_sell(p, o, "max_hold", ct=T48[i])
                continue
            if p["stage"] == 0:
                lpx = round(pc_prev * (1 + LIMIT_THR(code)), 2) if pc_prev else None
                if lpx and h >= lpx - 0.005 and lpx / cost - 1.0 >= 0.20:
                    q = int(p["avail"] * 0.3) // 100 * 100 or p["avail"]
                    do_sell(p, lpx, "limit_cash", q, T48[i])
                    stats["limit_cash"] += 1
                elif h >= cost * 1.15:
                    fill = max(o, cost * 1.15)
                    q = int(p["avail"] * 0.3) // 100 * 100 or p["avail"]
                    do_sell(p, fill, "tp1", q, T48[i])
            elif p["stage"] == 1 and h >= cost * 1.30:
                fill = max(o, cost * 1.30)
                q = int(p["avail"] * 0.3) // 100 * 100 or p["avail"]
                do_sell(p, fill, "tp2", q, T48[i])
            if p["qty"] > 0 and p["stage"] >= 1 and p["hw"] > cost \
                    and l <= p["hw"] * 0.90:
                trig = p["hw"] * 0.90
                do_sell(p, o if o <= trig else trig, "trailing", ct=T48[i])

        still_pos = []
        for p in open_pos:
            (still_pos if p["qty"] > 0 else closed).append(p)
        open_pos = still_pos

        if T48[i] not in WIN or block_fri or (stops_today >= 2 and not NO_STOP2):
            continue
        # 机器腿新仓确认(闸门不拦截机器买入, 只切换仓位上限; 5分钟三闸; 全日无bar→开盘近似)
        for code, pclose, _rank in day_cand:
            if any(p["code"] == code for p in open_pos) \
                    or any(pe[0] == code for pe in pend) \
                    or len(open_pos) + len(pend) >= MAX_POS:
                continue
            st = states.get(code)
            if st is None:
                continue
            db = bars.get(code, {}).get(D)
            if db is None:
                if T48[i] != "09:45":
                    continue
                o = dget(code, D, "o")
                if o is None or pclose <= 0:
                    continue
                lpx = round(pclose * (1 + LIMIT_THR(code)), 2)
                if o >= lpx - 0.005 or o / pclose - 1.0 > 0.07:
                    continue
                if do_buy(code, o, "machine", "09:45(开盘近似)"):
                    stats["machine_openfill"] += 1
                continue
            if db[i] is None:
                continue
            if NO_CONFIRM:
                # 轻确认归因: 窗口内首根可用K线即挂单(次根开盘成交),
                # 仅保留涨停不买与当日涨幅>7%防追高, 跳过VWAP状态机
                cur = db[i][3]
                lpx = round(pclose * (1 + LIMIT_THR(code)), 2)
                if cur >= lpx - 0.005 or (pclose > 0 and cur / pclose - 1.0 > 0.07):
                    continue
                pend.append([code, pclose, i + 1])
                continue
            fb = next((b for b in db if b is not None), None)
            if fb is None:
                continue
            if confirm(db, i, pclose, fb[0], st) is not None:
                continue
            pend.append([code, pclose, i + 1])

    fric = day_buys * FRIC_OUT + day_sells * FRIC_IN
    cash -= fric
    tot_fric += fric
    fric_by_year[D[:4]] += fric
    eq_curve.append((D, cash + sum(
        p["qty"] * (dget(p["code"], D, "c") or p["cost"]) for p in open_pos)))
    cash_curve.append((D, cash))

for p in open_pos:
    arr = daily[p["code"]]
    j = arr["didx"].get(W_END, arr["didx"][p["edate"]])
    px = float(arr["c"][j])
    p["proceeds"] += p["qty"] * px - FEE_S(p["qty"] * px)
    p["sells"].append((W_END, "收盘", LABEL["eod"], p["qty"], px))
    p["qty"] = 0
    p["xdate"] = W_END
    p["eod"] = True
closed += open_pos

eqs = pd.Series(dict(eq_curve)).sort_index()
dd = float((eqs / eqs.cummax() - 1).min())
final_eq = float(eqs.iloc[-1])
days = (pd.Timestamp(W_END) - pd.Timestamp(W_START)).days
ann = (final_eq / INIT_CASH) ** (365.0 / days) - 1
log(f"总收益 {final_eq/INIT_CASH-1:+.1%} 年化 {ann:+.1%} 回撤 {dd:.1%} "
    f"摩擦 {tot_fric:,.0f} 平仓笔数 {len(closed)} "
    f"(机器开近似{stats['machine_openfill']} 熊池买{stats['bear_buys']} "
    f"涨停兑现{stats['limit_cash']})")

# ---------------- 5) 明细与Excel ----------------
rows = []
for p in closed:
    pnl = p["proceeds"] - p["buy_total"]
    arr = daily[p["code"]]
    hd_total = None
    if p["sells"]:
        di_s = arr["didx"].get(p["sells"][-1][0])
        if di_s is not None:
            hd_total = di_s - arr["didx"][p["edate"]]
    rows.append({
        "类型": "机器腿" if p["leg"] == "machine" else "熊池腿",
        "代码": p["code"],
        "股票名称": names.get(p["code"], ""),
        "买入时间": f"{p['edate']} {p['et']}",
        "买入数量": p["qty_all"],
        "买入成交价": round(p["cost"], 3),
        "持仓成本(含费)": round(p["buy_total"], 2),
        "卖出时间": "；".join(f"{s[0]} {s[1]}" for s in p["sells"]),
        "卖出数量": sum(s[3] for s in p["sells"]),
        "卖出成交价": "；".join(f"{s[4]:.3f}" for s in p["sells"]),
        "持有交易日": hd_total,
        "卖出时机": "；".join(s[2] for s in p["sells"]),
        "卖出净得(含费后)": round(p["proceeds"], 2),
        "单笔收益(元)": round(pnl, 2),
        "年份": p["edate"][:4],
        "_sort": f"{p['edate']} {p['et']}",
    })
for y in sorted(basket_gain_by_year):
    rows.append({"类型": "红利篮子停靠", "代码": "—",
                 "股票名称": "闲钱停靠篮子日收益(10只等权前复权)",
                 "买入时间": f"{y}年度", "买入数量": "—", "买入成交价": "—",
                 "持仓成本(含费)": "—", "卖出时间": f"{y}年度合计", "卖出数量": "—",
                 "卖出成交价": "—", "持有交易日": "—", "卖出时机": "现金停泊腿",
                 "卖出净得(含费后)": "—", "单笔收益(元)": round(basket_gain_by_year[y], 2),
                 "年份": y, "_sort": f"{y}-12-31 22:00"})
for y, f in sorted(fric_by_year.items()):
    rows.append({"类型": "调度摩擦", "代码": "—", "股票名称": "篮子预卖/买回往返费用",
                 "买入时间": f"{y}年度", "买入数量": "—", "买入成交价": "—",
                 "持仓成本(含费)": "—", "卖出时间": f"{y}年度合计", "卖出数量": "—",
                 "卖出成交价": "—", "持有交易日": "—",
                 "卖出时机": "机器每买1元→篮子卖1元(0.126%);每回1元→买回(0.026%)",
                 "卖出净得(含费后)": "—", "单笔收益(元)": round(-f, 2),
                 "年份": y, "_sort": f"{y}-12-31 23:00"})

detail = pd.DataFrame(rows).sort_values("_sort").reset_index(drop=True)
detail["累积收益(元)"] = detail["单笔收益(元)"].cumsum().round(2)
detail["累积收益率"] = (detail["累积收益(元)"] / INIT_CASH * 100).round(2).map(
    lambda v: f"{v:+.2f}%")
detail["单笔收益率"] = detail.apply(
    lambda r: (f"{r['单笔收益(元)'] / r['持仓成本(含费)'] * 100:+.2f}%"
               if isinstance(r["持仓成本(含费)"], (int, float))
               and r["持仓成本(含费)"] else "—"), axis=1)
detail = detail.drop(columns=["_sort"])
detail.to_csv(f"{OUT}/trades_detail{SUFFIX}.csv", index=False, encoding="utf-8-sig")

nav = pd.DataFrame({"日期": eqs.index, "总资产": eqs.round(2).values})
nav["现金"] = pd.Series(dict(cash_curve)).reindex(eqs.index).round(2).values
nav["持仓市值"] = (nav["总资产"] - nav["现金"]).round(2)
nav["净值"] = (nav["总资产"] / INIT_CASH).round(4)
nav["当日盈亏"] = nav["总资产"].diff().round(2)
nav["回撤%"] = ((nav["总资产"] / nav["总资产"].cummax() - 1) * 100).round(2)
nav.to_csv(f"{OUT}/nav_daily{SUFFIX}.csv", index=False)

trades_only = detail[detail["类型"].isin(["机器腿", "熊池腿"])]
win = (trades_only["单笔收益(元)"] > 0).mean() if len(trades_only) else 0
summ_rows = [
    ("初始本金", f"{INIT_CASH:,.0f}"), ("期末总资产", f"{final_eq:,.2f}"),
    ("总收益", f"{final_eq/INIT_CASH-1:+.2%}"), ("年化收益", f"{ann:+.2%}"),
    ("最大回撤", f"{dd:.2%}"), ("回测区间", f"{W_START} → {W_END}"),
    ("交易笔数(平仓)", len(trades_only)),
    ("胜率", f"{win:.1%}" if len(trades_only) else "—"),
    ("机器腿笔数", int((trades_only["类型"] == "机器腿").sum())),
    ("熊池腿笔数", int((trades_only["类型"] == "熊池腿").sum())),
    ("其中:开盘近似成交(无5分钟线)", stats["machine_openfill"]),
    ("篮子停靠累计收益(元)", round(sum(basket_gain_by_year.values()), 2)),
    ("调度摩擦(元)", round(-tot_fric, 2)),
    ("涨停兑现次数", stats["limit_cash"]),
]
by_year = trades_only.groupby("年份")["单笔收益(元)"].agg(["size", "sum"]).round(2)
by_year.columns = ["笔数", "股票腿盈亏(元)"]
by_year["篮子+摩擦(元)"] = pd.Series(
    {y: round(basket_gain_by_year.get(y, 0) - fric_by_year.get(y, 0), 2)
     for y in by_year.index})

params = [
    ("本金", "100,000"),
    ("机器选股", "成交额>20亿/市值<300亿/20日涨停≥2且<6/站上向上的10日线/"
     "乖离2-8%/换手10-20%/价<50/强度排序"),
    ("机器入场", "次日09:45-10:30,13:30-14:00五分钟VWAP三闸(无5分钟线年份按开盘价近似)"),
    ("熊池", "bias10≤-15%&ret20≤-20%&价2-50&量比≥1&额>2亿,弹性分top5,次日开盘直接买(涨幅>7%不追)"),
    ("闸门", "上证收盘>200日线连续2天=开。闸门不拦截机器买入, 只切换仓位上限: "
     "开=单票30%/总90%; 关=机器票(机器+熊池)合计≤30%总资金, 单票≤10%"),
    ("仓位", "闸门开:≤3只/单票30%/总90%;周五不开新仓;当日止损2次停买;T+1"),
    ("卖出", "-5%止损(14:55复核,高点浮盈≥10%转保本)/+15%卖30%/+30%卖30%/回撤10%移动止盈/"
     "涨停浮盈≥20%卖30%/6天到期第5日14:55清仓"),
    ("费用", "佣金万2.5(最低5元)+过户费0.001%+印花税0.1%(卖)"),
    ("现金停泊", "闲钱停靠红利篮子(10只等权前复权);机器出入金计0.126%/0.026%往返摩擦"),
    ("数据口径", "日线=stock_selection.db全库;换手/市值=baostock当日实际流通换手推导;5分钟线覆盖2026全年/"
     "2024-25约250只/2022-23约61只,缺失日机器腿按开盘近似、退出按日线OHLC近似;ST为当前名称快照(幸存者偏差)"),
]

with pd.ExcelWriter(f"{OUT}/回测_当前策略_10万_2022-2026{SUFFIX}.xlsx", engine="openpyxl") as w:
    pd.DataFrame(summ_rows, columns=["指标", "数值"]).to_excel(
        w, sheet_name="汇总", index=False)
    by_year.reset_index().to_excel(w, sheet_name="分年度", index=False)
    detail.to_excel(w, sheet_name="交易明细", index=False)
    nav.to_excel(w, sheet_name="每日净值", index=False)
    pd.DataFrame(params, columns=["参数", "口径"]).to_excel(
        w, sheet_name="策略口径", index=False)

json.dump({"init": INIT_CASH, "final": final_eq, "ret": final_eq / INIT_CASH - 1,
           "ann": ann, "dd": dd, "fric": tot_fric, "trades": len(closed),
           "stats": dict(stats)},
          open(f"{OUT}/summary{SUFFIX}.json", "w"), ensure_ascii=False, indent=2)
log("BT_RUN_OK")
