# -*- coding: utf-8 -*-
"""服务器侧 baostock 连通性测试(含安装)。"""
import subprocess
import sys

r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "baostock"], capture_output=True, text=True, timeout=300)
print("pip:", r.returncode, (r.stderr or "").strip()[-200:])

try:
    import baostock as bs
except Exception as e:
    print("IMPORT_FAIL:", e)
    sys.exit(1)

lg = bs.login()
print("login:", lg.error_code, lg.error_msg)
rs = bs.query_history_k_data_plus(
    "sz.000001", "time,close", start_date="2022-01-04",
    end_date="2022-01-05", frequency="5", adjustflag="3")
n = 0
while rs.error_code == "0" and rs.next():
    rs.get_row_data()
    n += 1
print("bars(2天应约96):", n, "err:", rs.error_msg)
bs.logout()
print("BS_SERVER_OK" if n > 0 else "BS_SERVER_FAIL")
