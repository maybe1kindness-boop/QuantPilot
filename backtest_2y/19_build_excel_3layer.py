# -*- coding: utf-8 -*-
"""阶段19: 以部署版"先3层"为主口径重出Excel。"""
import json

import pandas as pd

OUT = "/opt/quantpilot/runtime/backtest_2y"
INIT_CASH = 300000.0

s3 = json.load(open(f"{OUT}/results_3layer.json"))
s1 = json.load(open(f"{OUT}/results_cur_logic.json"))["gate_cash"]
s2 = json.load(open(f"{OUT}/results_cur_logic.json"))["gate_basket"]

eq3 = pd.read_csv(f"{OUT}/equity_3layer.csv", index_col=0, parse_dates=True)
eq1 = pd.read_csv(f"{OUT}/equity_cur_cash.csv", index_col=0, parse_dates=True)
eq = eq3.join(eq1.rename(columns={"equity": "eq_ref"}), how="left").ffill()
eq.columns = ["3层总权益", "篮子市值", "机器(闲钱现金,参考)"]
eq["日期"] = eq.index.strftime("%Y-%m-%d")
eq["3层累计收益率%"] = ((eq["3层总权益"] / INIT_CASH - 1) * 100).round(2)
eq["其中机器腿(总权益-篮子)"] = (eq["3层总权益"] - eq["篮子市值"]).round(0)
eq_out = eq[["日期", "3层总权益", "3层累计收益率%", "篮子市值",
             "其中机器腿(总权益-篮子)", "机器(闲钱现金,参考)"]].copy()
eq_out.loc[len(eq_out)] = ["2021-12-31(期初)", INIT_CASH, 0.0,
                           INIT_CASH * 0.3, INIT_CASH * 0.7, INIT_CASH]
eq_out = eq_out.sort_values("日期").reset_index(drop=True)

df = pd.read_csv(f"{OUT}/trades_3layer.csv", dtype={"代码": str})
df["代码"] = df["代码"].str.zfill(6)
years = [f"y{y}" for y in range(2022, 2027)]
final3 = float(eq3["equity"].iloc[-1])
summary = pd.DataFrame([
    ("结构(部署版'先3层')", "篮子固定30%长持(不受年线控制,每年1月换股) + 机器≤70%(年线闸门,闸门关=现金趴账) + 现金"),
    ("期初资金", f"{INIT_CASH:,.0f} 元 (2021-12-31)"),
    ("期末资金(3层合计)", f"{final3:,.0f} 元 → 总收益 {s3['ret']*100:+.1f}%"),
    ("├ 篮子腿期末", f"{s3['final_basket']:,.0f} 元 (9万→按basket_daily含每年1月换股估值)"),
    ("└ 机器腿期末", f"{s3['final_machine']:,.0f} 元 (21万现金池照常交易)"),
    ("参考: 纯机器(闲钱现金)", f"+{s1['ret']*100:.1f}% / 回撤 {s1['maxdd']*100:.1f}%"),
    ("参考: 闲钱全停篮子(已被否)", f"+{s2['ret']*100:.1f}% / 回撤 {s2['maxdd']*100:.1f}% (仅参考,非部署结构)"),
    ("最大回撤(3层)", f"{s3['maxdd']*100:.1f}% (3层比纯机器-18.3%平滑)"),
    ("机器交易笔数 / 胜率 / 盈亏比", f"{s3['n']} 笔 / {s3['win']*100:.1f}% / {s3['pf']}"),
    *[(f"分年收益(3层) {y[1:]}", f"{s3.get(y, 0)*100:+.1f}%") for y in years],
    ("上证对照", "2022 -15.1% / 2023 -3.7% / 2024 +12.7% / 2025 +18.4% / 2026 -1.0%"),
    ("年线闸门与篮子的关系", "闸门只管机器开不开新仓(上证收盘>MA200才开);篮子一直持仓,牛市熊市都在场,闸门开关从不买卖篮子"),
    ("单笔累计收益率算法", "(全部卖出净得 - 买入总成本含费) ÷ 买入总成本含费"),
    ("费用模型", "买: 佣金max(金额×0.025%,5元)+过户0.001%; 卖: 另加印花0.1%"),
    ("口径", "未回放加仓/题材门禁/外围闸门/止损反弹守卫; 候选池有幸存者偏差; 篮子选池含2026视角偏差(指数停靠版+142.6%为干净下界)"),
], columns=["指标", "数值"])

yearly = df.groupby("年份").agg(
    笔数=("代码", "size"),
    胜率=("累计收益(元)", lambda s: f"{(s > 0).mean() * 100:.0f}%"),
    盈利和=("累计收益(元)", lambda s: round(s[s > 0].sum(), 0)),
    亏损和=("累计收益(元)", lambda s: round(s[s <= 0].sum(), 0)),
    净收益=("累计收益(元)", lambda s: round(s.sum(), 0)),
).reset_index()

xlsx = f"{OUT}/backtest_3layer_2022_2026.xlsx"
with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
    summary.to_excel(w, sheet_name="汇总", index=False)
    eq_out.to_excel(w, sheet_name="权益曲线", index=False)
    yearly.to_excel(w, sheet_name="分年统计", index=False)
    df.to_excel(w, sheet_name="交易明细", index=False)
    from openpyxl.utils import get_column_letter
    for sheet, widths in {
        "汇总": [30, 60],
        "权益曲线": [18, 14, 16, 12, 20, 20],
        "分年统计": [10, 8, 8, 12, 12, 12],
        "交易明细": [8, 12, 10, 10, 14, 17, 34, 20, 14, 12, 10, 8],
    }.items():
        ws = w.sheets[sheet]
        for idx, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(idx)].width = width
print("EXCEL_OK", eq_out.iloc[-1].to_dict())
