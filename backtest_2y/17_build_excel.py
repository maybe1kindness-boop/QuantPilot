# -*- coding: utf-8 -*-
"""阶段17: 重出Excel, 加权益曲线sheet(30万→期末, 两个口径)。"""
import json

import pandas as pd

OUT = "/opt/quantpilot/runtime/backtest_2y"
INIT_CASH = 300000.0

res = json.load(open(f"{OUT}/results_cur_logic.json"))
s1, s2 = res["gate_cash"], res["gate_basket"]

eq1 = pd.read_csv(f"{OUT}/equity_cur_cash.csv", index_col=0, parse_dates=True)
eq2 = pd.read_csv(f"{OUT}/equity_cur_basket.csv", index_col=0, parse_dates=True)
eq = eq1.join(eq2, lsuffix="_cash", rsuffix="_basket").fillna(method="ffill")
eq.columns = ["机器(闲钱现金)", "机器+篮子停靠(回测最优结构)"]
eq["日期"] = eq.index.strftime("%Y-%m-%d")
eq["现金版累计收益率%"] = ((eq["机器(闲钱现金)"] / INIT_CASH - 1) * 100).round(2)
eq["篮子版累计收益率%"] = ((eq["机器+篮子停靠(回测最优结构)"] / INIT_CASH - 1) * 100).round(2)
eq_out = eq[["日期", "机器(闲钱现金)", "现金版累计收益率%",
             "机器+篮子停靠(回测最优结构)", "篮子版累计收益率%"]].copy()
eq_out.loc[len(eq_out)] = ["2021-12-31(期初)", INIT_CASH, 0.0, INIT_CASH, 0.0]
eq_out = eq_out.sort_values("日期").reset_index(drop=True)

df = pd.read_csv(f"{OUT}/trades_cur_logic.csv", dtype={"代码": str})
df["代码"] = df["代码"].str.zfill(6)
years = [f"y{y}" for y in range(2022, 2027)]
summary = pd.DataFrame([
    ("期初资金", f"{INIT_CASH:,.0f} 元 (2021-12-31)"),
    ("期末资金(机器,闲钱现金)", f"{eq['机器(闲钱现金)'].iloc[-1]:,.0f} 元 → 总收益 {s1['ret']*100:+.1f}%"),
    ("期末资金(机器+篮子停靠)", f"{eq['机器+篮子停靠(回测最优结构)'].iloc[-1]:,.0f} 元 → 总收益 {s2['ret']*100:+.1f}%"),
    ("最大回撤(现金版 / 篮子版)", f"{s1['maxdd']*100:.1f}% / {s2['maxdd']*100:.1f}%"),
    ("交易笔数 / 胜率 / 盈亏比", f"{s1['n']} 笔 / {s1['win']*100:.1f}% / {s1['pf']}"),
    *[(f"分年收益(现金版) {y[1:]}", f"{s1.get(y, 0)*100:+.1f}%") for y in years],
    *[(f"分年收益(篮子版) {y[1:]}", f"{s2.get(y, 0)*100:+.1f}%") for y in years],
    ("上证对照", "2022 -15.1% / 2023 -3.7% / 2024 +12.7% / 2025 +18.4% / 2026 -1.0%"),
    ("单笔累计收益率算法", "(全部卖出净得 - 买入总成本含费) ÷ 买入总成本含费"),
    ("费用模型", "买: 佣金max(金额×0.025%,5元)+过户0.001%; 卖: 另加印花0.1%"),
    ("213%算法", "机器照常交易; 每日未被占用的现金按篮子前一日收益增值(年线闸门关时空仓期也在篮子里)"),
    ("与实盘部署差异", "实盘拍板'先3层': 篮子固定30%+机器70%上限+闸门关现金趴账; 回测213%是'闲钱全停篮子'结构, 实际会介于+93.3%与+213.1%之间"),
    ("口径", "未回放加仓/题材门禁/外围闸门/止损反弹守卫; 候选池有幸存者偏差"),
], columns=["指标", "数值"])

yearly = df.groupby("年份").agg(
    笔数=("代码", "size"),
    胜率=("累计收益(元)", lambda s: f"{(s > 0).mean() * 100:.0f}%"),
    盈利和=("累计收益(元)", lambda s: round(s[s > 0].sum(), 0)),
    亏损和=("累计收益(元)", lambda s: round(s[s <= 0].sum(), 0)),
    净收益=("累计收益(元)", lambda s: round(s.sum(), 0)),
).reset_index()

xlsx = f"{OUT}/backtest_cur_logic_2022_2026.xlsx"
with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
    summary.to_excel(w, sheet_name="汇总", index=False)
    eq_out.to_excel(w, sheet_name="权益曲线", index=False)
    yearly.to_excel(w, sheet_name="分年统计", index=False)
    df.to_excel(w, sheet_name="交易明细", index=False)
    from openpyxl.utils import get_column_letter
    for sheet, widths in {
        "汇总": [30, 52],
        "权益曲线": [18, 16, 16, 24, 16],
        "分年统计": [10, 8, 8, 12, 12, 12],
        "交易明细": [8, 12, 10, 10, 14, 17, 34, 20, 14, 12, 10, 8],
    }.items():
        ws = w.sheets[sheet]
        for idx, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(idx)].width = width
print("EXCEL_OK", eq_out.iloc[-1].to_dict())
