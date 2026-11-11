# -*- coding: utf-8 -*-
"""2026-09-06 关闭加仓链路(用户拍板): config add_position_pct 0.3 -> 0.0。

回测归因依据: 加仓使回撤翻倍(-18%->-29%), 收益贡献不稳定(46次样本)。
行为链: add_position_pct=0 -> _calc_buy_plan staged_room=0 -> room=0
        -> quantity<100 -> five_minute_entry_signal 返回 hold(信号层拦截,
        不需要动 notifier/scheduler); 新仓用 initial_position_pct=0.3 不受影响。
恢复: 改回 0.3 即可(备份在本文件同目录 .before_add_off_20260906)。
"""
import re
import shutil
import sys

import yaml

P = "/opt/quantpilot/config/config.yaml"
BAK = P + ".before_add_off_20260906"

shutil.copy2(P, BAK)
s = open(P, encoding="utf-8").read()
new, n = re.subn(r"^(\s*)add_position_pct:\s*[\d.]+\s*$",
                 r"\1add_position_pct: 0.0", s, count=1, flags=re.M)
assert n == 1, f"替换了{n}处(应为1)"
open(P, "w", encoding="utf-8").write(new)

cfg = yaml.safe_load(open(P, encoding="utf-8"))


def find(d, key, path=""):
    if isinstance(d, dict):
        for k, v in d.items():
            if k == key:
                yield path + "/" + k, v
            yield from find(v, key, path + "/" + str(k))


add_hits = list(find(cfg, "add_position_pct"))
init_hits = list(find(cfg, "initial_position_pct"))
assert add_hits and all(v == 0.0 for _, v in add_hits), add_hits
assert init_hits and all(abs(float(v) - 0.3) < 1e-9 for _, v in init_hits), init_hits
print("add_position_pct:", add_hits, "(已置0)")
print("initial_position_pct:", init_hits, "(新仓不受影响)")
print("备份:", BAK)
print("PATCH_ADD_OFF_OK")
