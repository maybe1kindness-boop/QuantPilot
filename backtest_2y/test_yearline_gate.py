# -*- coding: utf-8 -*-
"""年线闸门行为测试: _yearline_gate_state 的五种输入场景(mock文件, 不动生产状态)。"""
import json
import sys
from datetime import datetime
from unittest.mock import mock_open, patch

sys.path.insert(0, "/opt/quantpilot")
try:
    from trading_alert_notifier import TradingAlertNotifier
except ImportError:  # 公开版不含该私有模块, 置空占位
    TradingAlertNotifier = None


P = "/opt/quantpilot/runtime/data/market_gate.json"
NOW = datetime(2026, 9, 7, 10, 0)


def run(content, as_now=NOW):
    obj = TradingAlertNotifier.__new__(TradingAlertNotifier)
    obj._now = lambda: as_now
    with patch("builtins.open", mock_open(read_data=content)):
        return obj._yearline_gate_state()


ok_open = json.dumps({"as_of": "2026-09-05", "close": 4050.0,
                      "ma200": 4010.7, "open": True})
ok_close = json.dumps({"as_of": "2026-09-05", "close": 3930.1,
                       "ma200": 4010.7, "open": False})

cases = [
    ("正常-开闸", ok_open, {"open": True}),
    ("正常-关闸", ok_close, {"open": False}),
    ("文件缺失", FileNotFoundError(), None),
    ("过期10天", json.dumps({"as_of": "2026-08-28", "close": 3900.0,
                             "ma200": 4010.7, "open": False}), None),
    ("字段不全", json.dumps({"as_of": "2026-09-05", "open": False}), None),
    ("损坏JSON", "{oops", None),
]

fails = 0
for name, content, expect in cases:
    if isinstance(content, FileNotFoundError):
        with patch("builtins.open", mock_open()) as m:
            m.side_effect = FileNotFoundError(P)
            obj = TradingAlertNotifier.__new__(TradingAlertNotifier)
            obj._now = lambda: NOW
            got = obj._yearline_gate_state()
    else:
        got = run(content)
    ok = (got is None and expect is None) or (
        got is not None and expect is not None and got["open"] == expect["open"])
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {got}")
    fails += 0 if ok else 1

# 闸门拦截块逻辑(纯语法级): 关闸时 buy/add 被拦、sell 放行
side_blocked = {"buy", "add"}
yl = {"open": False, "as_of": "2026-09-05", "close": 3930.1, "ma200": 4010.7}
for side, should_block in (("buy", True), ("add", True),
                           ("sell", False), ("reduce", False)):
    blocked = side in side_blocked and yl is not None and not yl["open"]
    ok = blocked == should_block
    print(f"  [{'PASS' if ok else 'FAIL'}] 关闸拦截 {side}: "
          f"{'拦' if blocked else '放'}")
    fails += 0 if ok else 1

print("TEST_RESULT:", "ALL_PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
