# -*- coding: utf-8 -*-
"""行情备胎补丁行为测试。只读网络/不写生产库。"""
import sys
import time

sys.path.insert(0, "/opt/quantpilot")
PASS = []

def check(name, cond, detail=""):
    PASS.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {detail}")

# ---------- 1) call_with_timeout 硬超时 ----------
print("== 1) call_with_timeout 硬超时 ==")
from trading.intraday_data import call_with_timeout, IntradayDataUnavailable

check("正常调用透传", call_with_timeout(lambda x=1: x + 1) == 2)

def sleeper():
    time.sleep(30)
    return 1

t0 = time.time()
try:
    call_with_timeout(sleeper, timeout_seconds=1.0)
    check("超时抛异常", False)
except IntradayDataUnavailable as e:
    check("超时抛 IntradayDataUnavailable", "超时" in str(e),
          f"({time.time()-t0:.1f}s)")

# 卡死后下一次调用也应快速超时(单worker池被占)
t0 = time.time()
try:
    call_with_timeout(lambda: 2, timeout_seconds=1.0)
    check("卡死后续调用快速失败", False, "(意外成功——池已自愈?)")
except IntradayDataUnavailable:
    check("卡死后续调用快速失败", time.time() - t0 < 3,
          f"({time.time()-t0:.1f}s)")

# ---------- 2) TDX 主机轮换 ----------
print("== 2) TDX 主机轮换 ==")
import os
os.environ["QUANTPILOT_TDX_HOST"] = "127.0.0.1"  # 本机无服务, 立即拒绝
from trading.intraday_data import IntradayDataStore

t0 = time.time()
df = IntradayDataStore._fetch_tdx("600186", "2026-09-04", "2026-09-05")
check("坏主机会轮换到备用机", df is not None and len(df) == 48,
      f"(rows={0 if df is None else len(df)}, {time.time()-t0:.1f}s)")
del os.environ["QUANTPILOT_TDX_HOST"]

# ---------- 3) 新浪第三腿 ----------
print("== 3) 新浪第三腿 ==")
try:
    from paper_trading.market_data_poller import MarketDataPoller
except ImportError:  # 公开版不含该私有模块, 置空占位
    MarketDataPoller = None


class FakeDB:
    def list_all_stocks(self):
        return ["600186", "601606", "601398", "600519", "000001"]

class FakeEngine:
    db_manager = FakeDB()
    config = type("C", (), {"spot_min_interval_sec": 20})()

p = MarketDataPoller(FakeEngine())
t0 = time.time()
sq = p._fetch_sina_quotes(["600186", "601606", "601398", "600519"])
check("新浪返回4只", len(sq) == 4, f"({time.time()-t0:.1f}s)")
q = sq.get("600519")
if q:
    check("茅台字段", q.name == "贵州茅台" and abs(q.price - 1330.0) < 0.01
          and abs(q.prev_close - 1298.88) < 0.01,
          f"(px={q.price} prev={q.prev_close} open={q.open} hi={q.high})")
q2 = sq.get("600186")
if q2:
    exp_pct = (11.88 / 12.38 - 1) * 100
    check("pct计算", abs(q2.pct - exp_pct) < 0.05,
          f"(pct={q2.pct:.2f} vs {exp_pct:.2f})")

# ---------- 4) 腾讯空 → 新浪接管(_fetch_batch_fallback) ----------
print("== 4) 腾讯空时新浪接管 ==")
p._fetch_tencent_quotes = lambda codes: {}
fb = p._fetch_batch_fallback()
check("fallback落到新浪", len(fb) == 5, f"(got {len(fb)})")

# fetch_targeted_quotes 同样兜底
tq = p.fetch_targeted_quotes(["600186", "601398"])
check("targeted兜底", len(tq) == 2)

# ---------- 5) spot 超时包装存在且链路完整 ----------
print("== 5) spot 超时包装 ==")
import paper_trading.market_data_poller as mdp
check("_spot_call_with_timeout 已定义",
      hasattr(mdp, "_spot_call_with_timeout"))
t0 = time.time()
try:
    mdp._spot_call_with_timeout(time.sleep, timeout_seconds=1.0)
    check("spot超时抛 TimeoutError", False)
except TimeoutError:
    check("spot超时抛 TimeoutError", time.time() - t0 < 3,
          f"({time.time()-t0:.1f}s)")

print(f"\n{sum(1 for _, ok in PASS if ok)}/{len(PASS)} PASS")
sys.exit(0 if all(ok for _, ok in PASS) else 1)
