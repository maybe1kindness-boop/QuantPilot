import pandas as pd

OUT = "/opt/quantpilot/runtime/backtest_2y"
for tag in ("", "_pool_tr", "_honest"):
    f = f"{OUT}/basket_daily{tag}.csv" if tag else f"{OUT}/basket_daily.csv"
    s = pd.read_csv(f, index_col=0)["basket"]
    s.index = pd.to_datetime(s.index)
    cum = float((1.0 + s).prod() - 1.0)
    yr = s.groupby(s.index.strftime("%Y")).apply(lambda x: float((1.0 + x).prod() - 1.0))
    print(("old_px" if not tag else tag), f"cum={cum*100:+.1f}%",
          {k: f"{v*100:+.1f}%" for k, v in yr.items()}, "days=", len(s))
