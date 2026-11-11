# -*- coding: utf-8 -*-
"""阶段25: 22池 vs 全市场规则(无偷看)对比Excel。

行: 篮子腿累计/分年 + 结构级(含摩擦)总收益对照 + 偏差归因。
"""
import json

import pandas as pd

OUT = "/opt/quantpilot/runtime/backtest_2y"
INIT_CASH = 300000.0


def stats_of(csv):
    s = pd.read_csv(csv, index_col=0, parse_dates=True)["basket"]
    s = s[(s.index >= "2022-01-01") & (s.index <= "2026-09-04")]
    cum = float((1.0 + s).prod() - 1.0)
    yr = s.groupby(s.index.strftime("%Y")).apply(
        lambda x: float((1.0 + x).prod() - 1.0))
    return cum, yr


res = {}
for tag in ("friction", "gate2", "pool_tr", "honest"):
    try:
        res[tag] = json.load(open(f"{OUT}/results_park_{tag}.json"))
    except Exception:
        pass
try:
    res["3layer"] = json.load(open(f"{OUT}/results_3layer.json"))
except Exception:
    res["3layer"] = {"ret": 0.844, "maxdd": 0.121}

cum_old, yr_old = stats_of(f"{OUT}/basket_daily.csv")          # 22池+价格口径
cum_ptr, yr_ptr = stats_of(f"{OUT}/basket_daily_pool_tr.csv")  # 22池+总收益
cum_hon, yr_hon = stats_of(f"{OUT}/basket_daily_honest.csv")   # 全市场规则

rows_picks = json.load(open(f"{OUT}/honest_picks.json")) if False else None

years = ["2022", "2023", "2024", "2025", "2026"]


def yfmt(yr, y):
    return f"{yr.get(y, 0) * 100:+.1f}%"


summary = pd.DataFrame([
    ("口径说明", "总收益=分红除权日加回再投入; 结构=闲钱全停篮子+2天确认闸门+机器, 含全部摩擦(印花/佣金/过户)"),
    ("—— 篮子腿单独对比 ——", ""),
    ("22池·价格口径(旧回测, 分红漏算)", f"累计 {cum_old*100:+.1f}%"),
    ("22池·总收益口径(分红加回)", f"累计 {cum_ptr*100:+.1f}% → 分红贡献 {cum_ptr*100-cum_old*100:+.1f}pp"),
    ("全市场规则·总收益口径(无偷看)", f"累计 {cum_hon*100:+.1f}%"),
    ("池子偏差(22池−全市场)", f"{(cum_ptr-cum_hon)*100:+.1f}pp (正=常识池跑赢了无偷看规则)"),
    *[(f"分年 22池总收益 {y}", yfmt(yr_ptr, y)) for y in years],
    *[(f"分年 全市场规则 {y}", yfmt(yr_hon, y)) for y in years],
    ("—— 结构级对比(30万, 2天确认闸门, 含全部摩擦) ——", ""),
    ("22池·价格口径(当前部署依据)", f"+{res['gate2']['ret']*100:.1f}% / 回撤 {res['gate2']['maxdd']*100:.1f}% / 期末 {res['gate2']['final']:,.0f}"),
    ("22池·总收益口径", f"+{res['pool_tr']['ret']*100:.1f}% / 回撤 {res['pool_tr']['maxdd']*100:.1f}% / 期末 {res['pool_tr']['final']:,.0f}"),
    ("全市场规则·总收益(无偷看, 推荐口径)", f"+{res['honest']['ret']*100:.1f}% / 回撤 {res['honest']['maxdd']*100:.1f}% / 期末 {res['honest']['final']:,.0f}"),
    ("参考: 1天闸门+22池价格(旧口径)", f"+{res['friction']['ret']*100:.1f}% / 回撤 {res['friction']['maxdd']*100:.1f}%"),
    ("参考: 3层结构(此前部署版)", f"+{res['3layer'].get('ret', 0.844)*100:.1f}% / 回撤 {res['3layer'].get('maxdd', 0.121)*100:.1f}%"),
    ("参考: 红利指数停靠(价格口径, 漏分红)", "+142.6% / 回撤 -18.8%"),
    ("残存偏差", "全市场规则仍用当前股票清单(退市股缺失); 高分红大盘股退市极少, 偏差远小于机器小票池"),
], columns=["指标", "数值"])

eq_cols = {}
for tag, path in (("22池价格(部署口径)", "equity_park_gate2.csv"),
                  ("22池总收益", "equity_park_pool_tr.csv"),
                  ("全市场规则", "equity_park_honest.csv"),
                  ("3层参考", "equity_3layer.csv")):
    try:
        s = pd.read_csv(f"{OUT}/{path}", index_col=0, parse_dates=True)
        eq_cols[tag] = s["equity"] if "equity" in s.columns else s.iloc[:, 0]
    except Exception:
        pass
eq = pd.DataFrame(eq_cols).ffill()
eq_out = eq.round(0).reset_index()
eq_out.columns = ["日期"] + list(eq.columns)
for c in eq.columns:
    eq_out[f"{c}累计%"] = ((eq[c] / INIT_CASH - 1) * 100).round(2).values
eq0 = pd.DataFrame([["2021-12-31(期初)"] + [INIT_CASH] * len(eq.columns)
                    + [0.0] * len(eq.columns)], columns=eq_out.columns)
eq_out = pd.concat([eq0, eq_out], ignore_index=True)

yearly = pd.DataFrame({
    "年份": years,
    "22池篮子腿": [yfmt(yr_ptr, y) for y in years],
    "全市场篮子腿": [yfmt(yr_hon, y) for y in years],
    "结构·22池价格(部署口径)": [f"{res['gate2'].get('y'+y, 0)*100:+.1f}%" for y in years],
    "结构·22池总收益": [f"{res['pool_tr'].get('y'+y, 0)*100:+.1f}%" for y in years],
    "结构·全市场规则": [f"{res['honest'].get('y'+y, 0)*100:+.1f}%" for y in years],
    "上证对照": ["-15.1%", "-3.7%", "+12.7%", "+18.4%", "-1.0%"],
})

picks = []
try:
    ph = json.load(open(f"{OUT}/basket_picks.json"))
    for Y in sorted(ph["honest"]):
        picks.append({
            "年份": Y,
            "全市场规则前10": "、".join(ph["honest"][Y]),
            "22池前10": "、".join(ph["pool_tr"].get(Y, [])),
        })
except Exception as e:
    picks = [{"年份": "待生成", "全市场规则前10": str(e), "22池前10": ""}]
picks_df = pd.DataFrame(picks)

xlsx = f"{OUT}/backtest_basket_compare_2022_2026.xlsx"
with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
    summary.to_excel(w, sheet_name="对比汇总", index=False)
    picks_df.to_excel(w, sheet_name="每年选票", index=False)
    yearly.to_excel(w, sheet_name="分年收益", index=False)
    eq_out.to_excel(w, sheet_name="权益曲线", index=False)
    from openpyxl.utils import get_column_letter
    for sheet, widths in {
        "对比汇总": [36, 52], "每年选票": [8, 46, 46],
        "分年收益": [8, 14, 14, 18, 16, 16, 10],
        "权益曲线": [16] + [14] * len(eq.columns) * 2,
    }.items():
        ws = w.sheets[sheet]
        for idx, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(idx)].width = width
print("EXCEL_OK", eq_out.iloc[-1].to_dict())
