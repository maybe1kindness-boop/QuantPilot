# -*- coding: utf-8 -*-
"""2026-09-06 关闭外围风险闸门(用户拍板): require_external_environment true -> false。

回测依据(32号extgate口径, 加仓/题材门禁已关的基线 +213.9%/-15.9%):
  闸门开 +177.0%/-15.2% —— 少赚37pp, 回撤无改善; 且行业表仅5%非空+题材
  关键词为空表, 生产实际拦截力≈0(8/18上线以来真实拦截0次), 属空转。
行为链: scheduler.py:809 的 require_external_environment 为 False
        -> 不调用 _market_gate_for -> 买入分支不再检查 allow_open。
不影响: 年线闸门/同题材≤2/五分钟确认等其它风控; 外围快照目录不动
        (只是不再每日09:30新采集)。
恢复: 改回 true。备份 config.yaml.before_ext_gate_off_20260906。
"""
import re
import shutil

import yaml

P = "/opt/quantpilot/config/config.yaml"
BAK = P + ".before_ext_gate_off_20260906"

shutil.copy2(P, BAK)
s = open(P, encoding="utf-8").read()
new, n = re.subn(r"^(\s*)require_external_environment:\s*\w+\s*$",
                 r"\1require_external_environment: false", s, count=1,
                 flags=re.M)
assert n == 1, f"替换了{n}处(应为1)"
open(P, "w", encoding="utf-8").write(new)

cfg = yaml.safe_load(open(P, encoding="utf-8"))


def find(d, key, path=""):
    if isinstance(d, dict):
        for k, v in d.items():
            if k == key:
                yield path + "/" + k, v
            yield from find(v, key, path + "/" + str(k))


hits = list(find(cfg, "require_external_environment"))
assert hits and all(v is False for _, v in hits), hits
kw = list(find(cfg, "external_environment_tech_industry_keywords"))
print("require_external_environment:", hits, "(闸门已关)")
print("行业关键词保留不动:", len(kw and kw[0][1] or []), "项")
print("备份:", BAK)
print("PATCH_EXT_GATE_OFF_OK")
