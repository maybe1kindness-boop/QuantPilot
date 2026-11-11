# -*- coding: utf-8 -*-
"""2026-09-06 关闭题材热度门禁(用户拍板): theme_min_score 50.0 -> 0.0。

回测依据(真实语义 fail-closed+加权top3, 加仓已关口径):
  门禁开 +104.6%/回撤-15.5% vs 门禁关 +213.9%/-15.9% —— 收益减半、回撤无改善;
  且 32% 候选信号无 F10 主营快照被永久拦截(数据覆盖税),
  core-business-refresh.timer 停摆+快照120天过期会持续收紧可买范围。
行为链: threshold<=0 -> _theme_entry_block_reason 直接 return None(信号层放行)。
不影响: 同题材≤2(max_positions_per_theme=2, 独立开关fail-open)、
        买入消息里的题材热度文案(_calc_buy_plan 仍会 evaluate, 仅展示)。
恢复: 改回 50.0。备份 config.yaml.before_theme_gate_off_20260906。
"""
import re
import shutil

import yaml

P = "/opt/quantpilot/config/config.yaml"
BAK = P + ".before_theme_gate_off_20260906"

shutil.copy2(P, BAK)
s = open(P, encoding="utf-8").read()
new, n = re.subn(r"^(\s*)theme_min_score:\s*[\d.]+\s*$",
                 r"\1theme_min_score: 0.0", s, count=1, flags=re.M)
assert n == 1, f"替换了{n}处(应为1)"
open(P, "w", encoding="utf-8").write(new)

cfg = yaml.safe_load(open(P, encoding="utf-8"))


def find(d, key, path=""):
    if isinstance(d, dict):
        for k, v in d.items():
            if k == key:
                yield path + "/" + k, v
            yield from find(v, key, path + "/" + str(k))


score = list(find(cfg, "theme_min_score"))
tmax = list(find(cfg, "max_positions_per_theme"))
assert score and all(float(v) == 0.0 for _, v in score), score
assert tmax and all(int(v) == 2 for _, v in tmax), tmax
print("theme_min_score:", score, "(门禁已关)")
print("max_positions_per_theme:", tmax, "(同题材≤2保留,独立开关)")
print("备份:", BAK)
print("PATCH_THEME_GATE_OFF_OK")
