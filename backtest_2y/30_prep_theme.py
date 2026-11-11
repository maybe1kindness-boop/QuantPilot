# -*- coding: utf-8 -*-
"""阶段30: 为生产级回测准备题材热度数据。

1. code→题材绑定: stock_core_business_theme(仅verified主营) 的
   market_aliases 匹配 stock_sector 概念板块名 → sector_code+confidence。
2. 板块等权日涨幅: 全部概念板块(stock_sector_mapping成分)日线收盘涨幅
   等权聚合(矩阵法), 每日在"有数据的全部板块"内降序排名(1=最强),
   近似生产用的东财加权指数排名(504概念板块内)。
3. 输出 theme_data.pkl: {sector_ret: df(date,sector_code,ret_pct,open_pct,
   rank,total) 仅保留被绑定板块, code_themes: {code: [(sector_code, confidence)]}}
口径注: 成分与置信度为当前快照; 板块涨幅为成分等权(生产为加权指数);
       ret_pct/open_pct分别为收盘口径与开盘缺口(开盘缺口供盘中判断无偷看用)。
"""
import json
import sqlite3
import sys
import time
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, "/opt/quantpilot")
OUT = "/opt/quantpilot/runtime/backtest_2y"
W_START = "2021-12-01"

t0 = time.time()
log = lambda m: print(f"[{time.time()-t0:6.1f}s] {m}", flush=True)

from utils.global_db import get_global_db

g = get_global_db()


def alias_names(raw):
    """market_aliases_json 兼容: ['名'] / [{'name':..}/{'alias':..}] / {'名':x}"""
    try:
        val = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    out = []
    if isinstance(val, list):
        for it in val:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                out.append(str(it.get("name") or it.get("alias") or ""))
    elif isinstance(val, dict):
        out.extend(str(k) for k in val)
    return [a.strip() for a in out if a and a.strip()]


# 1) 概念板块清单
types = [str(r[0]) for r in
         g.execute("SELECT DISTINCT sector_type FROM stock_sector").fetchall()]
where = "WHERE sector_type='概念'" if "概念" in types else ""
sector_rows = g.execute(
    f"SELECT sector_code, sector_name FROM stock_sector {where}").fetchall()
sector_name2code = {str(r["sector_name"]).strip(): str(r["sector_code"]).strip()
                    for r in sector_rows}
log(f"概念板块 {len(sector_name2code)} (type={types})")

# 2) verified 主营题材绑定(与生产 _market_rows 同: 别名精确匹配板块名)
biz = g.execute(
    "SELECT b.stock_code, t.theme_name, t.confidence, t.market_aliases_json "
    "FROM stock_core_business_theme t JOIN stock_core_business b "
    "ON t.stock_code=b.stock_code "
    "WHERE b.validation_status='verified'"
).fetchall()
code_themes = defaultdict(list)
used_sectors = set()
for r in biz:
    for alias in alias_names(r["market_aliases_json"]):
        sc = sector_name2code.get(alias)
        if sc:
            code_themes[str(r["stock_code"]).zfill(6)].append(
                (sc, max(0.0, min(1.0, float(r["confidence"] or 0.0)))))
            used_sectors.add(sc)
code_themes = {k: sorted(set(v)) for k, v in code_themes.items()}
_confs = [c for v in code_themes.values() for _, c in v]
log(f"verified 题材绑定 {len(code_themes)} 只票 / 用到板块 {len(used_sectors)}"
    + (f" / confidence {min(_confs):.2f}~{max(_confs):.2f}" if _confs else ""))

# 3) 全部概念板块成分
mp_rows = g.execute("SELECT stock_code, sector_code FROM stock_sector_mapping"
                    ).fetchall()
members = defaultdict(set)
for r in mp_rows:
    members[str(r["sector_code"]).strip()].add(str(r["stock_code"]).zfill(6))
all_sectors = sorted(set(members) & set(sector_name2code.values()))
codes_needed = sorted(set().union(*(members[sc] for sc in all_sectors)))
log(f"参与排名板块 {len(all_sectors)} / 成分股并集 {len(codes_needed)} 只")

# 4) 全部成分股日线 → 日涨幅矩阵(code×date)
conn = sqlite3.connect("file:/opt/quantpilot/runtime/data/stock_selection.db?mode=ro",
                       uri=True)
frames = []
for pfx in ("60", "00", "30"):
    chunk = [c for c in codes_needed if c.startswith(pfx)]
    if not chunk:
        continue
    for i in range(0, len(chunk), 900):
        part = chunk[i:i + 900]
        q = ("SELECT code, date, open, close FROM stock_kline WHERE date>=? "
             "AND code IN (%s)" % ",".join("?" * len(part)))
        frames.append(pd.read_sql_query(q, conn, params=[W_START] + part))
conn.close()
kl = pd.concat(frames, ignore_index=True)
log(f"日线行数 {len(kl)}")
kl["code"] = kl["code"].astype(str).str.zfill(6)
close_pv = kl.pivot(index="code", columns="date", values="close").sort_index()
open_pv = (kl.assign(open=kl["open"]).pivot(index="code", columns="date",
                                            values="open")
           .reindex(close_pv.index)[close_pv.columns])
dates = sorted(close_pv.columns)
close_v = close_pv.values.astype(float)
open_v = open_pv.values.astype(float)
ret = np.full_like(close_v, np.nan)
ret[:, 1:] = close_v[:, 1:] / close_v[:, :-1] - 1.0
gap = np.full_like(close_v, np.nan)         # 开盘缺口: 今开/昨收-1
gap[:, 1:] = open_v[:, 1:] / close_v[:, :-1] - 1.0
valid = ~np.isnan(ret)
ret0 = np.where(valid, ret, 0.0)
valid_g = ~np.isnan(gap)
gap0 = np.where(valid_g, gap, 0.0)

# 5) 板块等权涨幅 = 成分日涨幅均值(NaN不计入), 全板块内每日排名
code_pos = {c: i for i, c in enumerate(close_pv.index)}
M = np.zeros((len(all_sectors), len(code_pos)), dtype=np.float32)
for si, sc in enumerate(all_sectors):
    for code in members[sc]:
        j = code_pos.get(code)
        if j is not None:
            M[si, j] = 1.0
cnt = M @ valid.astype(np.float32)          # 板块×日期 有效样本数
S = (M @ ret0) / np.maximum(cnt, 1e-9)      # 板块×日期 等权涨幅
cnt_g = M @ valid_g.astype(np.float32)
S_g = (M @ gap0) / np.maximum(cnt_g, 1e-9)  # 板块×日期 等权开盘缺口
has = (cnt > 0) & (cnt_g > 0)
order = np.argsort(-S, axis=0, kind="stable")
rank = np.empty_like(order)
rows_idx = np.arange(S.shape[0])
for di in range(S.shape[1]):
    rank[order[:, di], di] = rows_idx + 1
total = has.sum(axis=0)

# 6) 只输出被绑定板块的长表(ret_pct=当日收盘口径存档; rank按当日收盘全板块排名)
sec_pos = {sc: i for i, sc in enumerate(all_sectors)}
recs = []
for sc in sorted(used_sectors):
    si = sec_pos.get(sc)
    if si is None:
        continue
    for di, d in enumerate(dates):
        if has[si, di]:
            recs.append((str(d), sc, float(S[si, di]) * 100.0,
                         float(S_g[si, di]) * 100.0,
                         int(rank[si, di]), int(total[di])))
sector_ret = pd.DataFrame(recs, columns=["date", "sector_code", "ret_pct",
                                         "open_pct", "rank", "total"])
log(f"板块日涨幅(绑定板块) {len(sector_ret)} 行 / {sector_ret['date'].nunique()} 天"
    f" / 每日参与排名板块数≈{int(sector_ret['total'].iloc[-1])}")

out = {"sector_ret": sector_ret, "code_themes": dict(code_themes)}
pd.to_pickle(out, f"{OUT}/theme_data.pkl")
log(f"theme_data.pkl 写入完成; SIM30_OK")
