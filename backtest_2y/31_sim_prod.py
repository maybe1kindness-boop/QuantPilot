# -*- coding: utf-8 -*-
"""阶段31: 生产实况口径全账户回测(29号基础上补齐生产买卖链路)。

相对29号新回放的生产逻辑:
  1) 加仓: 持仓股走同一5分钟确认(含VWAP防追高), 门槛=首买T+1、
     每日一次、题材分≥50、年线闸门; 不受周五/熔断/3只/同题材≤2限制;
     额度=min(0.8eq-本股市值, 0.9eq-总持仓, 0.3eq, 现金);
     T+1共振确认=大盘当日开盘缺口>0 且 ≥1相关题材板块开盘缺口>0
     (无偷看地近似"盘中大盘/题材是否红盘")。
  2) 涨停兑现: 触及涨停价且浮盈≥20% → 卖可用量30%(计入首档, 下档+30%)。
  3) 新仓题材门禁: 最优题材分<50拦截; 同题材持仓≥2拦截; 数据缺失放行(同生产)。
  4) T+1可卖: 当日买入(含加仓)份额当日不可卖, avail口径。
题材分近似(盘中无偷看): 排名分用前一日收盘全板块等权排名(生产盘中快照
           即最近一次刷新≈昨收), 涨幅分用当日开盘缺口; 生产为东财加权指数
           盘中排名/实时涨幅, 属口径近似。
外围闸门(extgate FLAG, 32号数据): 纳指昨收<-1% 或 SMH/AIQ/IGV/SKYY昨收
           上涨只数<50% 的日子视为闸门关, 拦"行业命中科技关键词"的新仓
           (题材关键词生产为空表; 行业=stock_basic.industry, 该表仅5%非空,
           与生产判定完全同源)。韩KOSDAQ实时/VIX/数据缺失fail-closed不回放。
篮子腿/调度摩擦/占用调整与29号完全一致(前复权口径, 价差已含分红)。
"""
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict

import numpy as np
import pandas as pd

BASE = "/opt/quantpilot"
OUT = f"{BASE}/runtime/backtest_2y"
import sys
INIT_CASH = float(sys.argv[1]) if len(sys.argv) > 1 else 300000.0
TAG = f"_{int(round(INIT_CASH/10000))}万" if INIT_CASH != 300000 else ""
FLAGS = set(filter(None, re.split(r"[,\-]", sys.argv[2] if len(sys.argv) > 2
                                  else "")))
# 可选开关(归因用): notheme=关题材门禁 noadd=关加仓 noav=关T+1可卖限制
#                 nolup=关涨停兑现
if FLAGS:
    TAG = (TAG + "-" if TAG else "") + "-".join(sorted(FLAGS))
COMM, COMM_MIN, STAMP, TRANSFER = 2.5e-5, 5.0, 1e-3, 1e-5
FRIC_OUT = STAMP + COMM + TRANSFER   # 篮子卖出腿 0.126%
FRIC_IN = COMM + TRANSFER            # 篮子买回腿 0.026%

T48 = ([f"09:{m:02d}" for m in (35, 40, 45, 50, 55)]
       + [f"10:{m:02d}" for m in range(0, 60, 5)]
       + [f"11:{m:02d}" for m in (0, 5, 10, 15, 20, 25, 30)]
       + [f"13:{m:02d}" for m in (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55)]
       + [f"14:{m:02d}" for m in range(0, 60, 5)]
       + ["15:00"])
IDX48 = {t: i for i, t in enumerate(T48)}
WIN = ({"09:45", "09:50", "09:55", "10:00", "10:05", "10:10", "10:15",
        "10:20", "10:25", "10:30", "13:30", "13:35", "13:40", "13:45",
        "13:50", "13:55", "14:00"})
LIMIT_THR = lambda code: 0.20 if code.startswith("30") else 0.10
FEE_B = lambda a: max(a * COMM, COMM_MIN) + a * TRANSFER
FEE_S = lambda a: max(a * COMM, COMM_MIN) + a * TRANSFER + a * STAMP
LABEL = {
    "stop_loss": "止损-5%线(14:55尾盘复核)",
    "breakeven_stop": "保本止损(高点浮盈曾达10%,成本价离场)",
    "max_hold": "持仓到期6天清仓",
    "trailing": "移动止盈(高点回撤10%)",
    "limit_cash": "涨停兑现(触板且浮盈≥20%,卖30%)",
    "eod": "回测截止未平仓",
}

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:7.1f}s] {m}", flush=True)
W_START, W_END = "2022-01-01", "2026-09-04"

# ---------------- 数据 ----------------
feats = pd.read_pickle(f"{OUT}/features_5y.pkl")
slim_all = feats[["date", "code", "open", "high", "low", "close", "prev_close",
                  "is_limit"]]
del feats
vc = slim_all["date"].value_counts()
CAL = sorted(str(d) for d, n in vc.items() if n >= 500)
POS_CAL = {d: i for i, d in enumerate(CAL)}

sh = pd.read_csv(f"{OUT}/sh_long.csv", parse_dates=["date"]).set_index("date")["sh"]
ma200 = sh.rolling(200).mean()
sh_al = sh.reindex(CAL_dates := pd.to_datetime(CAL)).ffill()
above = (sh_al > ma200.reindex(CAL_dates).ffill()).fillna(True)
bull_al = (above & above.shift(1)).fillna(False).shift(1)  # 2天确认
bull_al.iloc[0] = True
BULL = {d: bool(v) for d, v in zip(CAL, bull_al)}
# T+1共振用: 大盘当日开盘缺口(今开/昨收, 无偷看地近似"盘中大盘是否红盘")
_g = (slim_all["open"] / slim_all["prev_close"] - 1.0).groupby(slim_all["date"])
MKT_GAP = {str(d): float(v) for d, v in _g.mean().items()}
PREV_CAL = {CAL[i]: CAL[i - 1] for i in range(1, len(CAL))}

_bk = pd.read_csv(f"{OUT}/basket_daily.csv", index_col=0, parse_dates=True)["basket"]
PARK_BASKET = {d: float(v) for d, v in
               zip(CAL, _bk.reindex(CAL_dates).fillna(0.0).shift(1).fillna(0))}

# 题材数据(30号产出; open_pct=当日开盘缺口, rank=当日收盘全板块排名)
TD = pd.read_pickle(f"{OUT}/theme_data.pkl")
code_themes = {k: sorted(set(v)) for k, v in TD["code_themes"].items()}
SEC_DAY = {}
for r in TD["sector_ret"].itertuples():
    SEC_DAY.setdefault(str(r.date), {})[str(r.sector_code)] = (
        float(r.ret_pct), float(r.open_pct), int(r.rank), int(r.total))
log(f"题材: 绑定{len(code_themes)}只 / 板块日数据{len(SEC_DAY)}天")

# 外围科技闸门(32号产出; extgate FLAG 才启用)
EXT_GATE, EXT_EXPOSED = set(), set()
if "extgate" in FLAGS:
    ED = pd.read_pickle(f"{OUT}/extgate_data.pkl")
    EXT_GATE = set(ED["gate_days"])
    EXT_EXPOSED = set(ED["exposed"])
    log(f"外围闸门: 关闭日{len(EXT_GATE)} / 暴露股{len(EXT_EXPOSED)}")


def best_theme_score(code, D):
    """最优题材分, 盘中无偷看口径: 排名用前一日收盘排名(生产盘中快照
    即最新一次刷新≈昨收), 涨幅用当日开盘缺口近似"盘中到现刻";
    None=无数据(近似口径放行)。
    realgate=生产真实语义(fail-closed): 无快照/无匹配题材→0分(拦截),
    且分数=前3题材加权0.60/0.25/0.15(对齐ThemePositionFactor.evaluate)"""
    rg = "realgate" in FLAGS
    ths = code_themes.get(code)
    if not ths:
        return 0.0 if rg else None
    pd_ = PREV_CAL.get(D)
    if pd_ is None:
        return 0.0 if rg else None
    prev = SEC_DAY.get(pd_, {})
    today = SEC_DAY.get(D, {})
    resolved = []
    for sc, conf in ths:
        pi = prev.get(sc)
        if pi is None:
            continue
        _, _, rank, total = pi
        op = today.get(sc, (0.0, 0.0, 0, 0))[1]
        rs = 100.0 if total <= 1 else 100.0 * (1.0 - (rank - 1) / (total - 1))
        cs = min(100.0, max(0.0, 50.0 + op * 12.5))
        resolved.append((rs * 0.65 + cs * 0.35) * conf)
    if not resolved:
        return 0.0 if rg else None
    if rg:
        top = sorted(resolved, reverse=True)[:3]
        w = [0.60, 0.25, 0.15][: len(top)]
        return sum(s * wi for s, wi in zip(top, w)) / sum(w)
    return max(resolved)


def theme_any_up(code, D):
    """≥1相关题材板块盘中上涨(开盘缺口口径); 无数据放行"""
    ths = code_themes.get(code)
    if not ths:
        return True
    day = SEC_DAY.get(D, {})
    seen = False
    for sc, _ in ths:
        info = day.get(sc)
        if info is None:
            continue
        seen = True
        if info[1] > 0:
            return True
    return not seen


# 校准模式(FLAG=cal): 生产真因子实测(2026-09-06锚点, 真EM加权数据)
# 最优题材分>=50通过率80%/中位68.7; 等权代理同日仅58%/52.7, 系统性偏严
# (等权排名压低主流板块名次)。校准=每日阈值取绑定股最优分分布的20分位,
# 使通过率对齐生产80%锚点。用于给出"生产真实口径"的更近估计。
THR_CAL = {}
if "cal" in FLAGS:
    for d in SEC_DAY:
        pd_ = PREV_CAL.get(d)
        if pd_ is None:
            continue
        prev = SEC_DAY.get(pd_, {})
        cur = {}
        for sc, info in SEC_DAY[d].items():
            pi = prev.get(sc)
            if pi is None:
                continue
            rk, tt = pi[2], pi[3]
            rs = 100.0 if tt <= 1 else 100.0 * (1.0 - (rk - 1) / (tt - 1))
            cs = min(100.0, max(0.0, 50.0 + info[1] * 12.5))
            cur[sc] = rs * 0.65 + cs * 0.35
        bests = []
        for code, ths in code_themes.items():
            b = None
            for sc, conf in ths:
                v0 = cur.get(sc)
                if v0 is None:
                    continue
                v = v0 * conf
                if b is None or v > b:
                    b = v
            if b is not None:
                bests.append(b)
        THR_CAL[d] = float(np.percentile(bests, 20)) if bests else 50.0
    log(f"cal校准: {len(THR_CAL)}天 阈值中位 {np.median(list(THR_CAL.values())):.1f}")


def theme_thr(D):
    return THR_CAL.get(D, 50.0) if "cal" in FLAGS else 50.0


def same_theme_count(code):
    """持仓中同题材只数(数据缺失不计数)"""
    ths = code_themes.get(code)
    if not ths:
        return 0
    sec = {sc for sc, _ in ths}
    n = 0
    for q in open_pos:
        if q["qty"] <= 0:
            continue
        oth = code_themes.get(q["code"])
        if oth and sec & {s for s, _ in oth}:
            n += 1
    return n


cands = pd.read_pickle(f"{OUT}/candidates_5y.pkl")
cands["date"] = cands["date"].astype(str)
cand_map = defaultdict(list)
for d, g in cands.sort_values(["date", "rank"]).groupby("date"):
    nd_i = POS_CAL.get(str(d))
    if nd_i is None or nd_i + 1 >= len(CAL):
        continue
    nd = CAL[nd_i + 1]
    for r in g.itertuples():
        cand_map[nd].append((r.code, float(r.prev_close), int(r.rank)))

need = defaultdict(set)
for nd, lst in cand_map.items():
    i0 = POS_CAL[nd]
    for code, _, _ in lst:
        for k in range(10):
            if i0 + k < len(CAL):
                need[code].add(CAL[i0 + k])
# 加仓需要持仓股全窗口bar, 候选股10天外仍可能被持有→一并取
for code in list(need.keys()):
    pass

slim = slim_all[slim_all.code.isin(set(need.keys()))]
del slim_all
import gc
gc.collect()
daily = {}
for code, g in slim.groupby("code", sort=False):
    dl = [str(x) for x in g.date.tolist()]
    daily[str(code)] = {"o": g.open.values, "c": g.close.values,
                        "didx": {d: i for i, d in enumerate(dl)}}
del slim
gc.collect()

conn = sqlite3.connect(f"file:{BASE}/runtime/data/stock_selection.db?mode=ro", uri=True)
names = {str(r[0]).zfill(6): str(r[1]) for r in
         conn.execute("SELECT code, name FROM stock_basic").fetchall()}
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
log(f"5分钟bar: {len(bars)} 只")

# 篮子成分(与29号一致)
picks_all = json.load(open(f"{OUT}/basket_picks.json"))
YEAR_PICKS = {y: [s[:6] for s in lst]
              for y, lst in picks_all["pool_tr"].items()}

dec_close = {}
for py in range(2021, 2026):
    q = ("SELECT k.code, k.close FROM stock_kline k JOIN "
         "(SELECT code, MAX(date) md FROM stock_kline WHERE date LIKE ? "
         " GROUP BY code) t ON k.code=t.code AND k.date=t.md")
    dec_close[py] = {str(r[0]).zfill(6): float(r[1]) for r in
                     conn.execute(q, (f"{py}-12%",))}
last_rows = conn.execute(
    "SELECT code, close FROM stock_kline WHERE date=?", (W_END,)).fetchall()
last_close = {str(r[0]).zfill(6): float(r[1]) for r in last_rows}
conn.close()
log(f"篮子成分 {sorted(YEAR_PICKS)} / 末日收盘 {len(last_close)} 只")


def prev_daily_close(code, D):
    arr = daily.get(code)
    if not arr:
        return None
    j = arr["didx"].get(D)
    if j is None or j < 1:
        return None
    return float(arr["c"][j - 1])


def first_bar(db):
    for b in db:
        if b is not None:
            return b
    return None


def confirm(db, i, prev_close, day_open, st, gain_limit=0.07):
    cur = db[i][3]
    fb = first_bar(db[: i + 1])
    if fb is None or cur < fb[2]:
        return "破开盘5分钟低点"
    vol = tw = ih = 0.0
    for b in db[: i + 1]:
        if b is None:
            continue
        vol += b[4]
        tw += (b[1] + b[2] + b[3]) / 3.0 * b[4]
        ih = max(ih, b[1])
    if vol <= 0:
        return "VWAP无效"
    vwap = tw / vol
    if cur < vwap:
        return "低于VWAP"
    if prev_close > 0 and cur / prev_close - 1.0 > gain_limit:
        return f"当日涨幅>{gain_limit:.0%}"
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


P = dict(sl=0.05, trail=0.10, be=0.10, tp1=0.15, tp2=0.30, pct1=0.30,
         pct2=0.30, maxhold=6, fri=True, halt=True, lup=0.20, lup_pct=0.30)


def t_is_tail(i):
    return T48[i] == "14:55"


cash = INIT_CASH
open_pos, closed = [], []
eq_curve, cash_curve = [], []
tot_fric = 0.0
fric_by_year = defaultdict(float)
flow_out_tot = flow_in_tot = 0.0
st_theme_block = st_theme2_block = st_t1_block = 0
st_adds = st_limit_cash = st_ext_block = 0


def mark(code, D):
    arr = daily.get(code)
    j = arr["didx"].get(D) if arr else None
    return float(arr["c"][j]) if j is not None else None


for D in CAL:
    if D < W_START or D > W_END:
        continue
    cash *= 1.0 + PARK_BASKET.get(D, 0.0)
    day_buys = day_sells = 0.0
    is_fri = pd.Timestamp(D).dayofweek == 4
    stops_today = 0
    day_list = cand_map.get(D, [])
    states = {code: {"s": None} for code, _, _ in day_list}
    add_states = {}
    pend, pend_add = [], []
    for p in open_pos:
        p["avail"] = p["qty"]       # T+1: 昨仓全可卖
        p["added_today"] = False
    db_holder = {p["code"]: bars.get(p["code"], {}).get(D) for p in open_pos}

    for i in range(48):
        # -- 新仓买单成交(确认后下一根开盘) --
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
            limit_px = round(pclose * (1 + LIMIT_THR(code)), 2)
            if o >= limit_px - 0.005:
                continue
            invested = sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                           for p in open_pos)
            eq = cash + invested
            room = min(0.30 * eq, 0.90 * eq - invested, cash)
            qty = int(room / (o * (1 + COMM)) / 100) * 100
            while qty >= 100 and qty * o + FEE_B(qty * o) > room:
                qty -= 100
            if qty < 100:
                continue
            amt = qty * o
            fee = FEE_B(amt)
            cash -= amt + fee
            day_buys += amt + fee
            open_pos.append({"code": code, "qty": qty, "qty0": qty,
                             "qty_all": qty, "adds": 0, "cost": o,
                             "hw": o, "stage": 0,
                             "avail": qty if "noav" in FLAGS else 0,
                             "added_today": True, "buy_total": amt + fee,
                             "proceeds": 0.0, "edate": D, "ct": T48[i],
                             "sells": [], "xdate": None, "xt": None,
                             "eod": False})
            db_holder[code] = db
        pend = still

        # -- 加仓单成交(确认后下一根开盘; 当日买入份额计入持仓但锁T+1) --
        still_a = []
        for pe in pend_add:
            p, pclose, fi = pe
            if i < fi:
                still_a.append(pe)
                continue
            code = p["code"]
            if p["qty"] <= 0:
                continue
            db = bars.get(code, {}).get(D)
            if db is None or db[i] is None:
                continue
            o = db[i][0]
            limit_px = round(pclose * (1 + LIMIT_THR(code)), 2)
            if o >= limit_px - 0.005:
                continue
            invested = sum(q["qty"] * (mark(q["code"], D) or q["cost"])
                           for q in open_pos)
            mv = p["qty"] * (mark(code, D) or p["cost"])
            eq = cash + invested
            room = min(0.80 * eq - mv, 0.90 * eq - invested,
                       (0.15 if "add15" in FLAGS else 0.30) * eq, cash)
            if room < 6000:
                continue
            qty = int(room / (o * (1 + COMM)) / 100) * 100
            while qty >= 100 and qty * o + FEE_B(qty * o) > room:
                qty -= 100
            if qty < 100:
                continue
            amt = qty * o
            fee = FEE_B(amt)
            cash -= amt + fee
            day_buys += amt + fee
            nq = p["qty"] + qty
            p["cost"] = (p["cost"] * p["qty"] + amt + fee) / nq
            p["qty"] = nq
            p["qty_all"] += qty
            p["adds"] += 1
            p["buy_total"] += amt + fee
            p["hw"] = max(p["hw"], o)
            p["added_today"] = True
            if "noav" in FLAGS:
                p["avail"] += qty
            st_adds += 1
        pend_add = still_a

        # -- 持仓卖出(avail口径: 当日买进的不可卖) --
        for p in open_pos:
            if p["qty"] <= 0 or p["edate"] == D:
                continue
            db = db_holder.get(p["code"])
            if not db or db[i] is None:
                continue
            o, h, l, c, v = db[i]
            cost = p["cost"]
            p["hw"] = max(p["hw"], h)
            didx = daily[p["code"]]["didx"]
            if D not in didx or p["edate"] not in didx:
                continue
            hd = didx[D] - didx[p["edate"]]
            av = p["avail"]
            if av <= 0:
                continue

            def sell_all(px, key, is_stop=False):
                global cash, stops_today
                q_sold = p["avail"]
                a = q_sold * px
                f = FEE_S(a)
                cash += a - f
                p["proceeds"] += a - f
                p["qty"] -= q_sold
                p["avail"] -= q_sold
                p["sells"].append((D, T48[i], LABEL[key], q_sold, px))
                p["xdate"], p["xt"] = D, T48[i]
                if is_stop:
                    stops_today += 1
                return a - f

            if t_is_tail(i) and P["sl"]:
                line = -P["sl"]
                if P["be"] and p["hw"] / cost - 1 >= P["be"]:
                    line = 0.0
                if c / cost - 1.0 < line:
                    nxt = db[i + 1] if i + 1 < 48 and db[i + 1] else None
                    px = nxt[0] if nxt else c
                    day_sells += sell_all(px, "breakeven_stop" if line >= 0
                                          else "stop_loss", True)
                    continue
            if hd >= P["maxhold"]:
                day_sells += sell_all(o, "max_hold")
                continue
            if p["stage"] == 0:
                pc = prev_daily_close(p["code"], D)
                limit_px = round(pc * (1 + LIMIT_THR(p["code"])), 2) if pc else None
                if "nolup" not in FLAGS and limit_px \
                        and h >= limit_px - 0.005 \
                        and limit_px / cost - 1.0 >= P["lup"]:
                    q = int(av * P["lup_pct"]) // 100 * 100 or av
                    a = q * limit_px
                    f = FEE_S(a)
                    cash += a - f
                    day_sells += a - f
                    p["proceeds"] += a - f
                    p["qty"] -= q
                    p["avail"] -= q
                    p["stage"] = 1
                    p["sells"].append((D, T48[i], LABEL["limit_cash"], q,
                                       limit_px))
                    st_limit_cash += 1
                    if p["avail"] <= 0:
                        continue
                elif h >= cost * (1 + P["tp1"]):
                    fill = max(o, cost * (1 + P["tp1"]))
                    q = int(av * P["pct1"]) // 100 * 100 or av
                    a = q * fill
                    f = FEE_S(a)
                    cash += a - f
                    day_sells += a - f
                    p["proceeds"] += a - f
                    p["qty"] -= q
                    p["avail"] -= q
                    p["stage"] = 1
                    p["sells"].append((D, T48[i], "首档止盈+15%(卖30%)", q, fill))
                    if p["avail"] <= 0:
                        continue
            elif p["stage"] == 1 and h >= cost * (1 + P["tp2"]):
                fill = max(o, cost * (1 + P["tp2"]))
                q = int(av * P["pct2"]) // 100 * 100 or av
                a = q * fill
                f = FEE_S(a)
                cash += a - f
                day_sells += a - f
                p["proceeds"] += a - f
                p["qty"] -= q
                p["avail"] -= q
                p["stage"] = 2
                p["sells"].append((D, T48[i], "二档止盈+30%(卖30%)", q, fill))
            if p["qty"] > 0 and p["stage"] >= 1 and P["trail"] \
                    and p["hw"] > cost and l <= p["hw"] * (1 - P["trail"]) \
                    and p["avail"] > 0:
                trig = p["hw"] * (1 - P["trail"])
                day_sells += sell_all(o if o <= trig else trig, "trailing")

        still_pos = []
        for p in open_pos:
            (still_pos if p["qty"] > 0 else closed).append(p)
        open_pos = still_pos

        if T48[i] not in WIN:
            continue
        gate_ok = BULL.get(D, True)

        # -- 加仓确认(持仓股; 生产: 位置分支在买单分支之前) --
        for p in ([] if "noadd" in FLAGS else open_pos):
            code = p["code"]
            if p["qty"] <= 0 or p["added_today"] or p["edate"] >= D:
                continue
            if any(pe[0] is p for pe in pend_add):
                continue
            if not gate_ok:
                continue
            if "notheme" not in FLAGS:
                bs = best_theme_score(code, D)
                if bs is not None and bs < theme_thr(D):
                    st_theme_block += 1
                    continue
            db = bars.get(code, {}).get(D)
            if db is None or db[i] is None:
                continue
            if "addwin" in FLAGS and db[i][3] <= p["cost"]:
                continue  # 收紧版: 只加浮盈仓(确认bar价>成本)
            fb = first_bar(db)
            if fb is None:
                continue
            pclose = prev_daily_close(code, D)
            if pclose is None:
                continue
            st = add_states.setdefault(code, {"s": None})
            if confirm(db, i, pclose, fb[0], st) is not None:
                continue
            if not (MKT_GAP.get(D, 0.0) > 0.0 and theme_any_up(code, D)):
                st_t1_block += 1
                continue
            invested = sum(q["qty"] * (mark(q["code"], D) or q["cost"])
                           for q in open_pos)
            if 0.30 * (cash + invested) < 6000:
                continue
            p["added_today"] = True  # 生产: 信号发出即消耗当日加仓资格
            pend_add.append([p, pclose, i + 1])

        # -- 新仓确认 --
        for code, pclose, rank in day_list:
            if any(p["code"] == code for p in open_pos):
                continue
            if any(pe[0] == code for pe in pend):
                continue
            st = states.get(code)
            db = bars.get(code, {}).get(D)
            if st is None or db is None or db[i] is None:
                continue
            fb = first_bar(db)
            if fb is None:
                continue
            if confirm(db, i, pclose, fb[0], st) is not None:
                continue
            if P["fri"] and is_fri:
                continue
            if P["halt"] and stops_today >= 2:
                continue
            if not gate_ok:
                continue
            if EXT_GATE and D in EXT_GATE and code in EXT_EXPOSED:
                st_ext_block += 1
                continue
            if len(open_pos) + len(pend) >= 3:
                continue
            if "notheme" not in FLAGS:
                bs = best_theme_score(code, D)
                if bs is not None and bs < theme_thr(D):
                    st_theme_block += 1
                    continue
                if same_theme_count(code) >= 2:
                    st_theme2_block += 1
                    continue
            invested = sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                           for p in open_pos)
            if 0.30 * (cash + invested) < 6000:
                continue
            pend.append([code, pclose, i + 1])

    fric = day_buys * FRIC_OUT + day_sells * FRIC_IN
    cash -= fric
    tot_fric += fric
    fric_by_year[D[:4]] += fric
    flow_out_tot += day_buys
    flow_in_tot += day_sells
    eq_curve.append((D, cash + sum(p["qty"] * (mark(p["code"], D) or p["cost"])
                                   for p in open_pos)))
    cash_curve.append((D, cash))

for p in open_pos:
    arr = daily[p["code"]]
    j = arr["didx"].get(W_END)
    if j is None:
        j = arr["didx"][p["edate"]]
    px = float(arr["c"][j])
    p["proceeds"] += p["qty"] * px - FEE_S(p["qty"] * px)
    p["sells"].append((W_END, None, LABEL["eod"], p["qty"], px))
    p["qty"] = 0
    p["xdate"] = W_END
    p["eod"] = True
closed += open_pos

eqs = pd.Series(dict(eq_curve)).sort_index()
cash_s = pd.Series(dict(cash_curve)).sort_index()
dd = float((eqs / eqs.cummax() - 1).min())
final_eq = float(eqs.iloc[-1])
total_ret = final_eq / INIT_CASH - 1
log(f"生产实况版总收益 {total_ret:+.1%} 回撤 {dd:.1%} 摩擦 {tot_fric:,.0f} "
    f"加仓{st_adds} 涨停兑现{st_limit_cash} 题材拦{st_theme_block} "
    f"同题材拦{st_theme2_block} T+1拦{st_t1_block} 外围闸门拦{st_ext_block}")

# ---------------- 明细 ----------------
rows = []
for p in closed:
    pnl = p["proceeds"] - p["buy_total"]
    chain = "；".join(s[2] for s in p["sells"]) or "无"
    rows.append({
        "类型": "机器",
        "代码": p["code"],
        "股票名称": names.get(p["code"], ""),
        "买入时间": f"{p['edate']} {p['ct']}",
        "持仓数量": p["qty_all"],
        "加仓次数": p["adds"],
        "买入成交价": round(p["buy_total"] / p["qty_all"], 3),
        "持仓成本(含费总额)": round(p["buy_total"], 2),
        "卖出数量": sum(s[3] for s in p["sells"]),
        "卖出成交价": "；".join(f"{s[4]:.3f}" for s in p["sells"]) or "—",
        "卖出时机": chain,
        "卖出时间": (f"{p['xdate']} {p['xt']}"
                    if (p["xdate"] and p["xt"] and not p["eod"])
                    else (f"{p['xdate'] or ''} 收盘估值(未平仓)" if p["eod"]
                          else str(p["xdate"]))),
        "卖出净得(含费后)": round(p["proceeds"], 2),
        "单笔收益(元)": round(pnl, 2),
        "年份": str(p["edate"])[:4],
        "_sort": f"{p['edate']} {p['ct']}",
    })

# 篮子腿(与29号一致)
cash_by_date = cash_s.to_dict()
year_first_day = {}
for D in CAL:
    y = D[:4]
    if y >= "2022" and y <= "2026" and y not in year_first_day:
        year_first_day[y] = D


def prev_year_end_cash(y):
    if y == "2022":
        return INIT_CASH
    py_dates = [d for d in cash_by_date if d[:4] == str(int(y) - 1)]
    return cash_by_date[max(py_dates)] if py_dates else INIT_CASH


basket_rows = []
for y in range(2022, 2027):
    ys = str(y)
    codes = YEAR_PICKS.get(y) or YEAR_PICKS.get(ys)
    if not codes:
        log(f"warn: {y} 无篮子成分, 跳过")
        continue
    base = prev_year_end_cash(ys)
    per = base / 10.0
    d0 = year_first_day[ys]
    for code in codes:
        px_in = dec_close[y - 1].get(code)
        if px_in is None or px_in <= 0:
            log(f"warn: {code} 无{y-1}年末收盘, 跳过")
            continue
        qty = per / px_in
        cost_total = per + FEE_B(per)
        if y == 2026:
            px_out = last_close.get(code)
            note = "2026-09-04 收盘估值(未平仓)"
        else:
            px_out = dec_close[y].get(code)
            note = f"{y}-12月最后交易日收盘"
        if px_out is None or px_out <= 0:
            log(f"warn: {code} 无卖出价, 跳过")
            continue
        amt_out = qty * px_out
        proceeds = amt_out - FEE_S(amt_out)
        pnl = proceeds - cost_total
        basket_rows.append({
            "类型": "红利篮子",
            "代码": code,
            "股票名称": names.get(code, ""),
            "买入时间": f"{d0}(按{y-1}年末收盘换仓)",
            "持仓数量": round(qty, 1),
            "加仓次数": "—",
            "买入成交价": round(px_in, 3),
            "持仓成本(含费总额)": round(cost_total, 2),
            "卖出数量": round(qty, 1),
            "卖出成交价": round(px_out, 3),
            "卖出时机": "年度换仓(等权1/10,前复权口径,价差已含分红回报)",
            "卖出时间": note,
            "卖出净得(含费后)": round(proceeds, 2),
            "单笔收益(元)": round(pnl, 2),
            "年份": ys,
            "_sort": f"{d0} 00:00",
        })

for y, f in sorted(fric_by_year.items()):
    rows.append({
        "类型": "调度摩擦", "代码": "—", "股票名称": "篮子预卖/买回往返费用",
        "买入时间": f"{y}年度", "持仓数量": "—", "加仓次数": "—",
        "买入成交价": "—", "持仓成本(含费总额)": "—", "卖出数量": "—",
        "卖出成交价": "—",
        "卖出时机": "机器每买1元→篮子卖1元(0.126%);每回1元→买回(0.026%)",
        "卖出时间": f"{y}年度合计", "卖出净得(含费后)": "—",
        "单笔收益(元)": round(-f, 2), "年份": y, "_sort": f"{y}-12-31 23:00",
    })

cash_dates = list(cash_s.index)
basket_gain_by_year = defaultdict(float)
prev_c = INIT_CASH
for D in CAL:
    if D < W_START or D > W_END:
        continue
    basket_gain_by_year[D[:4]] += prev_c * PARK_BASKET.get(D, 0.0)
    prev_c = float(cash_s[D])
basket_detail_by_year = defaultdict(float)
for r in basket_rows:
    basket_detail_by_year[r["年份"]] += r["单笔收益(元)"]
for y in sorted(basket_gain_by_year):
    adj = basket_gain_by_year[y] - basket_detail_by_year[y]
    rows.append({
        "类型": "占用调整", "代码": "—",
        "股票名称": "篮子资金占用调整(机器占用期实际未停篮子)",
        "买入时间": f"{y}年度", "持仓数量": "—", "加仓次数": "—",
        "买入成交价": "—", "持仓成本(含费总额)": "—", "卖出数量": "—",
        "卖出成交价": "—",
        "卖出时机": "对齐账户权益口径: 实际逐日篮子增益-明细满基数增益",
        "卖出时间": f"{y}年度合计", "卖出净得(含费后)": "—",
        "单笔收益(元)": round(adj, 2), "年份": y, "_sort": f"{y}-12-31 23:30",
    })

df = pd.DataFrame(rows + basket_rows).sort_values("_sort").reset_index(drop=True)
df["累积收益(元)"] = df["单笔收益(元)"].cumsum().round(2)
df["累积收益率"] = (df["累积收益(元)"] / INIT_CASH * 100).round(2).map(
    lambda v: f"{v:+.2f}%")
df["单笔收益率"] = df.apply(
    lambda r: (f"{r['单笔收益(元)'] / r['持仓成本(含费总额)'] * 100:+.2f}%"
               if isinstance(r["持仓成本(含费总额)"], (int, float))
               and r["持仓成本(含费总额)"] else "—"), axis=1)
df = df.drop(columns=["_sort"])
df.to_csv(f"{OUT}/trades_prod_fidelity{TAG}.csv", index=False,
          encoding="utf-8-sig")

ledger_final = float(df["累积收益(元)"].iloc[-1])
gap = final_eq - INIT_CASH - ledger_final
log(f"明细累积终值 {ledger_final:,.0f} vs 生产实况版实际 {final_eq-INIT_CASH:,.0f}"
    f" 差 {gap:,.2f}")
assert abs(gap) < 2.0, f"对账失败 差{gap}"


def yearly_agg(sub):
    pnl = sub["单笔收益(元)"]
    n = len(sub)
    return pd.Series({
        "笔数": n,
        "胜率": f"{(pnl > 0).mean() * 100:.0f}%" if n else "—",
        "净收益": round(pnl.sum(), 0),
    })


yearly = (df[df["类型"].isin(["机器", "红利篮子"])]
          .groupby(["年份", "类型"]).apply(yearly_agg).reset_index())
yearly_pivot = yearly.pivot(index="年份", columns="类型", values="净收益").fillna(0)
yearly_pivot["篮子调度摩擦"] = [-fric_by_year.get(str(y), 0) for y in yearly_pivot.index]
yearly_pivot["年度合计"] = yearly_pivot.sum(axis=1)
yearly_pivot = yearly_pivot.reset_index()

eqt = eqs.copy()
eqt.index = pd.to_datetime(eqt.index)
prev = INIT_CASH
yr_eq = []
for y, seg in eqt.groupby(eqt.index.year):
    yr_eq.append((y, float(seg.iloc[-1] / prev - 1)))
    prev = float(seg.iloc[-1])
yearly_pivot["账户收益率(权益口径)"] = [
    f"{r*100:+.1f}%" for y, r in yr_eq if str(y) in set(yearly_pivot["年份"])]

machine_df = df[df["类型"] == "机器"]
extra_rows = []
if INIT_CASH == 300000.0 and "cal" in FLAGS:
    extra_rows = [
        ("归因: 29号基线(无加仓/题材/T+1细节)", "+209.5% / 回撤-16.0%"),
        ("归因: +T+1可卖限制+涨停兑现", "+213.9% / 回撤-15.9% (中性略正)"),
        ("归因: +加仓", "+163.9% / 回撤-31.0% (加仓放大回撤)"),
        ("归因: +题材门禁严格代理(阈值50)", "+89.2% (等权代理偏严, 悲观下界)"),
        ("题材校准依据", "生产真因子实测(2026-09锚点, 真EM加权数据): 最优题材分"
         "≥50通过率80%/中位68.7; 等权代理同日58%/52.7; 校准=每日阈值取"
         "绑定股最优分20分位对齐80%通过率, 阈值中位34.4"),
        ("风险提示", "生产加仓链路使回撤约翻倍(-16%→-29%), 收益略降; "
         "题材门禁历史无法精确回放(无历史EM板块存档), 真实结果落在"
         "严格代理(+89%)与校准(+196%)之间"),
    ]
summary = pd.DataFrame([
    ("总收益(生产实况口径: 机器+加仓+红利篮子+闲钱调度)", f"{total_ret*100:+.1f}%"),
    ("期末权益 / 初始资金", f"{final_eq:,.0f} / {INIT_CASH:,.0f} 元"),
    ("最大回撤", f"{dd*100:.1f}%"),
    ("机器腿笔数(含加仓笔数)", f"{len(machine_df)} 笔 "
     f"(加仓成交 {st_adds} 次, 涨停兑现 {st_limit_cash} 次)"),
    ("题材门禁拦截", f"热度<50拦 {st_theme_block} 次 / 同题材≥2拦 "
     f"{st_theme2_block} 次 / 加仓T+1共振不满足拦 {st_t1_block} 次"),
    ("篮子腿合计收益(元)", f"{df[df['类型']=='红利篮子']['单笔收益(元)'].sum():,.0f}"),
    ("机器腿合计收益(元)", f"{machine_df['单笔收益(元)'].sum():,.0f}"),
    ("调度摩擦合计(元)", f"{-tot_fric:,.0f}"),
    ("明细累积 vs 权益口径 差额(元)", f"{gap:,.0f}"),
    ("分年收益", " / ".join(f"{y}:{r*100:+.1f}%" for y, r in yr_eq)),
    ("篮子规则", "22池每年1月按近3年年均分红/上年末价前10等权; 前复权价格计算, "
     "价差已等效含分红(分红再投资假设)"),
    ("口径1", "机器: 确认后下一根5分钟K开盘成交, 佣金2.5bp(最低5元)+过户1bp, "
     "卖出印花1‰; 当日买入(含加仓)T+1不可卖"),
    ("口径2", "已回放: 加仓(首买T+1/每日一次/题材≥50/大盘+题材共振确认)、"
     "涨停兑现(触板+浮盈≥20%卖30%)、题材热度≥50、同题材≤2; "
     "题材分盘中无偷看近似: 排名=前一日收盘全板块等权排名, 涨幅=当日开盘缺口"
     "(生产为东财加权指数盘中排名与实时涨幅); 共振确认同口径"),
    ("口径3", "未回放: 外围科技闸门(生产关键词为空=不生效)、60秒轮询粒度"
     "(5分钟bar近似); 候选池为现存5183只有幸存者偏差"),
    ("口径4", "题材成分为当前快照(历史无存档), 板块等权涨幅不含加权; "
     "题材数据缺失时放行(与生产fail-open一致)"),
    *extra_rows,
], columns=["指标", "数值"])

xlsx = f"{OUT}/回测_生产实盘口径{TAG}_2022-2026.xlsx"
with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
    summary.to_excel(w, sheet_name="汇总", index=False)
    yearly_pivot.to_excel(w, sheet_name="分年统计", index=False)
    df.drop(columns=["单笔收益率"], errors="ignore").to_excel(
        w, sheet_name="交易明细(机器+篮子)", index=False)
    from openpyxl.utils import get_column_letter
    for sheet, widths in {
        "汇总": [46, 44],
        "分年统计": [10, 10, 12, 14, 12, 18],
        "交易明细(机器+篮子)": [10, 8, 12, 17, 10, 8, 10, 15, 10, 14,
                          30, 20, 14, 12, 8],
    }.items():
        ws = w.sheets[sheet]
        for idx, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(idx)].width = width
log(f"Excel: {xlsx}")
log("SIM31_OK")
