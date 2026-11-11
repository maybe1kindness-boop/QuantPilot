# -*- coding: utf-8 -*-
"""阶段33: 红利篮子"利润落袋"机制回测(2026-11-05 议题)。

问题: 篮子是无择时的"全资金停车场"。熊市里红利攒的浮盈, 在熊转牛过渡期
(钱从红利流向成长、但 MA200 闸门因滞后还没开)被完整回吐, 现有规则全程无动作
——年线禁卖令拦的是亏本卖, 不拦"浮盈归零"。本脚本量化:

一、诊断:
  (a) 篮子净值自高水印回吐≥3%的episode(深度/当时闸门状态/修复天数);
  (b) 每次"闸门重开日"回看: 关闭期间篮子峰值→重开日回吐多少、同期上证自低点
      反弹多少; 并模拟"6%移动止盈"在区间内会何时出场、比等到重开多保住多少
      (直接量化'过渡期处理'的价值);
  (c) 联网取最新行情: 2026年9月后当前段(指数口径)的回吐与闸门现状。

二、机制变体(只看篮子自身价格净值; t日收盘信号→t+1日收盘执行; 摩擦卖0.126%/买0.026%):
  V0 纯停泊(现状): 永远满仓
  V1 移动止盈X%: 自高水印回撤X%全卖持币; 净值站回MA_n 买回(无保本底线)
  V2 落袋保本型: 底线=max(成本, 高水印*(1-X)), 浮盈达T后武装, 只在仍有浮盈时卖
  V4 闸门版(仅篮子侧): 闸门关=停泊篮子, 开=持币(不含机器腿对现金的再利用, 参考13号)

数据: basket_daily.csv(无偷看前10等权篮子日收益率, 价格口径未含分红, 各变体同口径
      内部可比; 含分红只会更利好多持有的V0)、sh_long.csv(上证2021起, 生产口径闸门:
      连续2日收上MA200才开、跌破1日即关; 在完整序列上算好再对齐)。
"""
import json
import os
import subprocess

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
FRIC_OUT, FRIC_IN = 1.26e-3, 0.26e-3  # 卖/买 一腿摩擦

bd = pd.read_csv(f"{BASE}/basket_daily.csv", parse_dates=["date"]).set_index("date")
r = bd["basket"].astype(float)
nav = (1 + r).cumprod()  # 篮子价格净值(未含分红)

sh = pd.read_csv(f"{BASE}/sh_long.csv", parse_dates=["date"]).set_index("date")["sh"]


def gate_series(s):
    """生产口径年线闸门: 连续2日收上MA200才开, 跌破1日即关, 其余维持前态。"""
    ma = s.rolling(200).mean()
    above = (s >= ma)
    out = {}
    prev_state, last_above = None, False
    for i, d in enumerate(s.index):
        if pd.isna(ma.iloc[i]):
            out[d] = np.nan
            last_above = False
            continue
        if not above.iloc[i]:
            out[d] = False
        elif last_above:  # 今日收上且昨日也收上 → 开
            out[d] = True
        else:  # 今日收上但昨日没有 → 维持前态
            out[d] = prev_state if prev_state is not None else False
        prev_state = out[d]
        last_above = bool(above.iloc[i])
    return pd.Series(out)


gate = gate_series(sh).reindex(nav.index)
sh_al = sh.reindex(nav.index).ffill()


def yearly(v):
    y, prev = {}, 1.0
    for yr, seg in v.groupby(v.index.year):
        y[yr] = seg.iloc[-1] / prev - 1
        prev = seg.iloc[-1]
    return y


def maxdd(v):
    return (v / v.cummax() - 1).min()


# ============ 一(a) 高水印回吐 episode ============
print("=" * 100)
print("一(a) 篮子自高水印回吐≥3% 的 episode(价格口径, 未含分红)")
print(f"{'峰值日':<12}{'回撤底日':<12}{'深度':>7}{'底日闸门':>9}{'修复日':<12}{'修复天数':>8}")
print("-" * 100)
hwm, peak_i = nav.iloc[0], 0
ep_open = False
trough_i, trough_v = 0, np.inf
for i in range(1, len(nav)):
    if nav.iloc[i] >= hwm:
        if ep_open:
            g = gate.iloc[trough_i]
            gs = "" if pd.isna(g) else ("开" if g else "关")
            print(f"{str(nav.index[peak_i].date()):<12}{str(nav.index[trough_i].date()):<12}"
                  f"{trough_v / hwm - 1:>7.1%}{gs:>9}{str(nav.index[i].date()):<12}{i - trough_i:>8d}")
        hwm, peak_i = nav.iloc[i], i
        ep_open, trough_v = False, np.inf
    elif not ep_open and nav.iloc[i] <= hwm * 0.97:
        ep_open, trough_i, trough_v = True, i, nav.iloc[i]
    elif ep_open and nav.iloc[i] < trough_v:
        trough_i, trough_v = i, nav.iloc[i]
if ep_open:
    g = gate.iloc[trough_i]
    gs = "" if pd.isna(g) else ("开" if g else "关")
    print(f"{str(nav.index[peak_i].date()):<12}{str(nav.index[trough_i].date()):<12}"
          f"{trough_v / hwm - 1:>7.1%}{gs:>9}{'未修复':<12}{'-':>8}")

# ============ 一(b) 闸门重开日回看 ============
print()
print("=" * 100)
print("一(b) 每次【闸门重开日】回看: 关闭期间攒的浮盈到重开时还剩多少; 6%移动止盈能早走多少")
print(f"{'关闭区间':<26}{'重开日':<12}{'篮子峰值日':<12}{'峰值→重开回吐':>13}"
      f"{'同期上证自低点':>13}{'6%止盈出场日':<13}{'比等到重开多保住':>15}")
print("-" * 100)
saved_all = []
for i in range(1, len(gate)):
    g_i, g_prev = gate.iloc[i], gate.iloc[i - 1]
    if pd.isna(g_i) or pd.isna(g_prev) or not (g_i and not g_prev):
        continue
    j = i - 1
    while j > 0 and not pd.isna(gate.iloc[j]) and not gate.iloc[j]:
        j -= 1
    lo_i, hi_i = j + 1, i  # 关闭区间含重开日当天
    seg = nav.iloc[lo_i:hi_i + 1]
    p = seg.idxmax()
    sh_seg = sh_al.iloc[lo_i:hi_i + 1]
    lo = sh_seg.idxmin()
    # 区间内模拟6%移动止盈(从区间运行峰值起算)
    pk, exit_d, exit_px = -np.inf, None, None
    for k in range(lo_i, hi_i + 1):
        pk = max(pk, nav.iloc[k])
        if exit_d is None and k > lo_i and nav.iloc[k] <= pk * 0.94:
            exit_d, exit_px = nav.index[k], nav.iloc[k]
    if exit_d is not None and exit_d < nav.index[i]:
        saved = exit_px / nav.iloc[i] - 1
        saved_all.append(saved)
        ex_s, saved_s = str(exit_d.date()), f"{saved:+.1%}"
    else:
        ex_s, saved_s = "-", "-"
    print(f"{str(nav.index[lo_i].date())}~{str(nav.index[i - 1].date()):<11}"
          f"{str(nav.index[i].date()):<12}{str(p.date()):<12}"
          f"{nav.iloc[i] / nav.loc[p] - 1:>13.1%}{sh_al.iloc[i] / sh_al.loc[lo] - 1:>13.1%}"
          f"{ex_s:<13}{saved_s:>15}")
seg_r = nav.pct_change().fillna(0)
mask_c = ~pd.isna(gate) & (gate != True)
mask_o = ~pd.isna(gate) & (gate == True)
if mask_c.any():
    print(f"[闸门关 {int(mask_c.sum())} 天/开 {int(mask_o.sum())} 天: 篮子在关闭期区间收益 "
          f"{(1 + seg_r[mask_c]).prod() - 1:+.1%}, 开启期 {(1 + seg_r[mask_o]).prod() - 1:+.1%}]")
    if saved_all:
        print(f"[6%移动止盈 vs 等到重开: {len(saved_all)} 次区间触发, 平均多保住 "
              f"{np.mean(saved_all):+.1%}, 最差 {min(saved_all):+.1%}]")

# ============ 二、机制变体 ============
print()
print("=" * 100)
print("二、落袋机制变体(t日收盘信号→t+1日收盘执行, 含摩擦; 价格口径全体一致)")
print(f"{'配置':<34}{'总收益':>9}{'最大回撤':>9}{'2022':>8}{'2023':>8}"
      f"{'2024':>8}{'2025':>8}{'2026':>8}{'次数':>5}{'摩擦':>8}")
print("-" * 100)


def run_variant(mode, X=None, T=None, ma_n=20, verbose=False):
    manav = nav.rolling(ma_n).mean()
    v = [1.0]
    in_pos, hwm_v, cost = True, nav.iloc[0], nav.iloc[0]
    floor_on, pending, fric = False, None, 0.0
    exits = []
    for t in range(1, len(nav)):
        if in_pos:
            v.append(v[-1] * (1 + r.iloc[t]))
        else:
            v.append(v[-1])
        if pending == "exit" and in_pos:
            v[-1] *= 1 - FRIC_OUT
            fric += FRIC_OUT
            in_pos, floor_on = False, False
            exits.append((nav.index[t], nav.iloc[t], cost))
            pending = None
            continue
        if pending == "enter" and not in_pos:
            v[-1] *= 1 - FRIC_IN
            fric += FRIC_IN
            in_pos, hwm_v, cost = True, nav.iloc[t], nav.iloc[t]
            pending = None
            continue
        if mode == "V4":
            want = pd.isna(gate.iloc[t]) or (gate.iloc[t] == False)  # 闸门关(或未知)才停泊
            if in_pos and not want:
                pending = "exit"
            elif not in_pos and want:
                pending = "enter"
            continue
        if in_pos:
            hwm_v = max(hwm_v, nav.iloc[t])
            sig = False
            if mode == "V1" and nav.iloc[t] <= hwm_v * (1 - X):
                sig = True
            if mode == "V2":
                if not floor_on and nav.iloc[t] >= cost * (1 + T):
                    floor_on = True
                if floor_on and nav.iloc[t] <= max(cost, hwm_v * (1 - X)):
                    sig = True
            if sig:
                pending = "exit"
        else:
            if t >= ma_n and nav.iloc[t] > manav.iloc[t]:
                pending = "enter"
    curve = pd.Series(v, index=nav.index)
    y = yearly(curve)
    line = " ".join(f"{y.get(yr, float('nan')):>+8.1%}" for yr in range(2022, 2027))
    if mode == "V0":
        name = "V0 纯停泊(现状)"
    elif mode == "V1":
        name = f"V1 移动止盈{X:.0%} 重入MA{ma_n}"
    elif mode == "V2":
        name = f"V2 落袋 回撤{X:.0%} 启动+{T:.0%} 重入MA{ma_n}"
    else:
        name = "V4 闸门开持币/关停泊(仅篮子侧)"
    print(f"{name:<34}{curve.iloc[-1] - 1:>+9.1%}{maxdd(curve):>+9.1%}{line}"
          f"{len(exits):>5}{fric:>8.1%}")
    if verbose:
        for d, px, c in exits:
            after = nav.loc[d:]
            print(f"    卖出 {d.date()} 锁定 {px / c - 1:+6.1%}, 其后最低又至 "
                  f"{after.min() / px - 1:+6.1%}")
    return curve


run_variant("V0")
for X in (0.04, 0.06, 0.08):
    run_variant("V1", X=X, ma_n=20)
for X in (0.04, 0.06):
    for T in (0.05, 0.08):
        run_variant("V2", X=X, T=T, ma_n=20)
run_variant("V2", X=0.06, T=0.05, ma_n=60)
run_variant("V4")

print()
print("---- 明细: V2 回撤6% 启动+5% 重入MA20 的每笔落袋 ----")
run_variant("V2", X=0.06, T=0.05, ma_n=20, verbose=True)

# ============ 三、固定目标止盈变体 ============
print()
print("=" * 100)
print("三、固定目标止盈(涨到+X%卖): 全卖→MA20接回 / 全卖→回落8%接回 / 半仓落袋其余持有")


def run_tp(X, mode):
    manav = nav.rolling(20).mean()
    if mode == "half":
        cash, pos, cost, done = 0.0, 1.0, nav.iloc[0], False
        vals = [1.0]
        for t in range(1, len(nav)):
            pos *= 1 + r.iloc[t]
            if not done and nav.iloc[t] >= cost * (1 + X):
                cash += pos * 0.5 * (1 - FRIC_OUT)
                pos *= 0.5
                done = True
            vals.append(cash + pos)
        curve = pd.Series(vals, index=nav.index)
        return curve, 1, 0.0
    v, in_pos, cost, exit_px = 1.0, True, nav.iloc[0], None
    pending, n_tp, fric = None, 0, 0.0
    vals = [1.0]
    for t in range(1, len(nav)):
        if in_pos:
            v *= 1 + r.iloc[t]
        vals.append(v)
        if pending == "exit" and in_pos:
            v *= 1 - FRIC_OUT
            vals[-1] = v
            fric += FRIC_OUT
            in_pos, exit_px, pending = False, nav.iloc[t], None
            continue
        if pending == "enter" and not in_pos:
            v *= 1 - FRIC_IN
            vals[-1] = v
            fric += FRIC_IN
            in_pos, cost, pending = True, nav.iloc[t], None
            continue
        if in_pos:
            if nav.iloc[t] >= cost * (1 + X):
                pending = "exit"
                n_tp += 1
        else:
            if mode == "full_ma20":
                ok = nav.iloc[t] > manav.iloc[t]
            else:  # full_limit: 等回落8%接回
                ok = exit_px is not None and nav.iloc[t] <= exit_px * 0.92
            if ok:
                pending = "enter"
    return pd.Series(vals, index=nav.index), n_tp, fric


print(f"{'配置':<34}{'总收益':>9}{'最大回撤':>9}{'2022':>8}{'2023':>8}"
      f"{'2024':>8}{'2025':>8}{'2026':>8}{'次数':>5}{'摩擦':>8}")
print("-" * 100)
v0 = run_variant("V0")
for X in (0.10, 0.15, 0.20):
    for mode, mn in (("full_ma20", "全卖MA20接回"), ("full_limit", "全卖等回落8%接回"),
                     ("half", "半仓落袋其余持有")):
        curve, n_tp, fric = run_tp(X, mode)
        y = yearly(curve)
        line = " ".join(f"{y.get(yr, float('nan')):>+8.1%}" for yr in range(2022, 2027))
        print(f"{f'止盈+{X:.0%} {mn}':<34}{curve.iloc[-1] - 1:>+9.1%}{maxdd(curve):>+9.1%}"
              f"{line}{n_tp:>5}{fric:>8.1%}")

# ============ 一(c) 当前段(curl取腾讯ifzq, 指数口径) ============
print()
print("=" * 100)
print("一(c) 2026年9月后当前段(000922红利指数+上证, 指数口径近似)")


def fetch(code, start="2025-01-01", count=800):
    # 注意: 接口返回[start,end]内最后count根; count>800会param error
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
           f"{code},day,{start},2026-11-06,{count},")
    try:
        raw = subprocess.run(["curl", "-s", url], capture_output=True,
                             text=True, timeout=20).stdout
        j = json.loads(raw)
        rows = j["data"][code].get("day")
        if rows:
            return pd.Series({pd.Timestamp(x[0]): float(x[2]) for x in rows}).sort_index()
    except Exception as e:
        print(f"  [fetch {code} 失败: {type(e).__name__}: {e}]")
    return None


try:
    sh_new = fetch("sh000001")
    dv_new = fetch("sh000922")
    if sh_new is not None and dv_new is not None:
        sh_ext = pd.concat([sh[sh.index < sh_new.index[0]], sh_new])
        sh_ext = sh_ext[~sh_ext.index.duplicated()]
        g_ext = gate_series(sh_ext)
        gs = g_ext.iloc[-1]
        yesterday = g_ext.iloc[-2]
        dv26 = dv_new.loc["2026-01-01":]
        pk = dv26.idxmax()
        s26 = sh_new.loc["2026-06-01":]
        lo = s26.idxmin()
        print(f"  000922 2026高点 {pk.date()} {dv26.max():.0f} → 最新 {dv_new.iloc[-1]:.0f}, "
              f"回吐 {dv_new.iloc[-1] / dv26.max() - 1:+.1%}")
        print(f"  上证自 {lo.date()} 低点 {s26.min():.0f} → 最新 {sh_new.iloc[-1]:.0f}, "
              f"反弹 {sh_new.iloc[-1] / s26.min() - 1:+.1%}")
        ma_ext = sh_ext.rolling(200).mean()
        print(f"  上证现值 {sh_ext.iloc[-1]:.0f} vs MA200 {ma_ext.iloc[-1]:.0f} "
              f"({sh_ext.iloc[-1] / ma_ext.iloc[-1] - 1:+.1%}), 昨日闸门={'开' if yesterday else '关'} "
              f"→ 今日生产口径闸门: {'开' if gs else '关'}")
        if pk < lo:
            print(f"  → 红利高点({pk.date()})早于大盘低点({lo.date()}): 大盘反弹期间红利 "
                  f"{dv_new.iloc[-1] / dv26.loc[pk] - 1:+.1%}, 正是'闸门未开、钱已流走'的过渡期")
    else:
        print("  [行情未取到, 跳过当前段诊断]")
except Exception as e:
    print(f"  [当前段诊断异常: {type(e).__name__}: {e}]")

print("\nLOCK_OK")
