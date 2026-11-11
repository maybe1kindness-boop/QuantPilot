"""模拟盘专用 SQLite 持久化层。

独立库文件（默认 ``data/paper_trading.db``），不与 QuantPilot 行情库混用。
所有表带 trade_date 便于回溯；主键/唯一约束保证幂等。

表清单：
  pt_position        当前持仓汇总（单账户单票一行）
  pt_position_lot    分批建仓明细（支持 T+1 与 FIFO 卖出）
  pt_trade           交易记录（含成交与拒单）
  pt_nav_snapshot    每日净值快照
  pt_candidate_pool  当日候选池
  pt_signal_log      盘中信号日志（含未成交）
  pt_meta            元数据（初始资金/暂停标志/最后轮询时间等）
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


SCHEMA = """
CREATE TABLE IF NOT EXISTS pt_position (
    code TEXT PRIMARY KEY,
    name TEXT,
    quantity INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0,
    first_buy_date TEXT,
    high_water_price REAL NOT NULL DEFAULT 0,
    exit_stage INTEGER NOT NULL DEFAULT 0,
    rally_exit_stage INTEGER NOT NULL DEFAULT 0,
    rally_exit_base_quantity INTEGER NOT NULL DEFAULT 0,
    rally_exit_first_breakout_price REAL NOT NULL DEFAULT 0,
    rally_exit_first_sell_price REAL NOT NULL DEFAULT 0,
    rally_exit_second_sell_price REAL NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pt_position_lot (
    lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    buy_date TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    remaining INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lot_code_date ON pt_position_lot(code, buy_date, lot_id);
CREATE TABLE IF NOT EXISTS pt_trade (
    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    trade_time TEXT,
    code TEXT NOT NULL,
    name TEXT,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    commission REAL NOT NULL DEFAULT 0,
    stamp_tax REAL NOT NULL DEFAULT 0,
    transfer_fee REAL NOT NULL DEFAULT 0,
    total REAL NOT NULL,
    status TEXT NOT NULL,
    reject_reason TEXT,
    signal_msg TEXT
);
CREATE INDEX IF NOT EXISTS idx_trade_date_code ON pt_trade(trade_date, code);
CREATE TABLE IF NOT EXISTS pt_nav_snapshot (
    trade_date TEXT PRIMARY KEY,
    cash REAL, positions_value REAL, total_value REAL,
    nav REAL, drawdown REAL, position_count INTEGER, daily_pnl REAL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS pt_candidate_pool (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    hit_strategies TEXT,
    close_at_select REAL,
    support REAL,
    resistance REAL,
    PRIMARY KEY (trade_date, code)
);
CREATE TABLE IF NOT EXISTS pt_bear_pool (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    bias10 REAL,
    ret20 REAL,
    close_at_select REAL,
    amount REAL,
    turnover REAL,
    market_cap REAL,
    PRIMARY KEY (trade_date, code)
);
CREATE TABLE IF NOT EXISTS pt_signal_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT,
    trade_date TEXT,
    code TEXT, side TEXT, quantity INTEGER,
    strength REAL, message TEXT, source TEXT, acted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signal_date ON pt_signal_log(trade_date, generated_at);
CREATE TABLE IF NOT EXISTS pt_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS pt_watchlist_group (
    group_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pt_watchlist_item (
    group_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    price REAL,
    reason TEXT,
    added_at TEXT NOT NULL,
    PRIMARY KEY (group_id, code),
    FOREIGN KEY (group_id) REFERENCES pt_watchlist_group(group_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_watchlist_item_group ON pt_watchlist_item(group_id);
"""


class PaperTradingDAO:
    """线程安全的 SQLite 访问层（每次操作借用独立连接）。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self.init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._lock, self._conn() as c:
            c.executescript(SCHEMA)
            columns = {row["name"] for row in c.execute("PRAGMA table_info(pt_position)")}
            migrations = {
                "high_water_price": "ALTER TABLE pt_position ADD COLUMN high_water_price REAL NOT NULL DEFAULT 0",
                "exit_stage": "ALTER TABLE pt_position ADD COLUMN exit_stage INTEGER NOT NULL DEFAULT 0",
                "rally_exit_stage": "ALTER TABLE pt_position ADD COLUMN rally_exit_stage INTEGER NOT NULL DEFAULT 0",
                "rally_exit_base_quantity": "ALTER TABLE pt_position ADD COLUMN rally_exit_base_quantity INTEGER NOT NULL DEFAULT 0",
                "rally_exit_first_breakout_price": "ALTER TABLE pt_position ADD COLUMN rally_exit_first_breakout_price REAL NOT NULL DEFAULT 0",
                "rally_exit_first_sell_price": "ALTER TABLE pt_position ADD COLUMN rally_exit_first_sell_price REAL NOT NULL DEFAULT 0",
                "rally_exit_second_sell_price": "ALTER TABLE pt_position ADD COLUMN rally_exit_second_sell_price REAL NOT NULL DEFAULT 0",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    c.execute(statement)

    def replace_server_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Replace the mirrored trading ledger with one server snapshot.

        Watchlist tables remain local to the desktop.  The remaining tables are
        a read-only mirror of the server-owned paper account, so deleting and
        inserting them in one transaction prevents a partially refreshed UI.
        (桌面专属方法:服务器版 dao 从未包含它;2026-10-18 服务器同步时误覆盖,自备份恢复。)
        """
        tables = (
            "pt_position", "pt_position_lot", "pt_trade", "pt_nav_snapshot",
            "pt_candidate_pool", "pt_signal_log", "pt_meta",
        )
        if not isinstance(snapshot, dict):
            raise ValueError("服务器模拟盘快照格式无效")

        normalized = {}
        for table in tables:
            rows = snapshot.get(table, [])
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError(f"服务器快照中的 {table} 格式无效")
            normalized[table] = rows

        delete_order = (
            "pt_candidate_pool", "pt_signal_log", "pt_nav_snapshot", "pt_trade",
            "pt_position_lot", "pt_position", "pt_meta",
        )
        with self._lock, self._conn() as c:
            for table in delete_order:
                c.execute(f"DELETE FROM {table}")
            for table in tables:
                rows = normalized[table]
                if not rows:
                    continue
                columns = [row["name"] for row in c.execute(f"PRAGMA table_info({table})")]
                for row in rows:
                    values = {key: row[key] for key in columns if key in row}
                    if not values:
                        continue
                    names = list(values)
                    placeholders = ",".join(f":{name}" for name in names)
                    c.execute(
                        f"INSERT INTO {table} ({','.join(names)}) VALUES ({placeholders})",
                        values,
                    )

    # ---------------- 元数据 ----------------
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT value FROM pt_meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO pt_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def claim_retryable_meta(self, key: str, value: Any) -> bool:
        """Claim a once-per-day task, allowing only a previous failed attempt."""
        with self._lock, self._conn() as c:
            cursor = c.execute(
                "INSERT INTO pt_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value "
                "WHERE pt_meta.value = 'failed'",
                (key, str(value)),
            )
        return cursor.rowcount == 1

    # ---------------- 自选股分组 ----------------
    def list_watchlist_groups(self) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT g.group_id, g.name, g.created_at, "
                "COUNT(i.code) AS item_count "
                "FROM pt_watchlist_group g "
                "LEFT JOIN pt_watchlist_item i ON i.group_id = g.group_id "
                "GROUP BY g.group_id ORDER BY g.name"
            ).fetchall()
            return [dict(r) for r in rows]

    def add_watchlist_items(self, group_name: str,
                            items: List[Dict[str, Any]]) -> int:
        name = str(group_name or "").strip()
        if not name:
            raise ValueError("自选分组名称不能为空")
        if not items:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO pt_watchlist_group(name, created_at) VALUES(?, ?) "
                "ON CONFLICT(name) DO NOTHING",
                (name, now),
            )
            group = c.execute(
                "SELECT group_id FROM pt_watchlist_group WHERE name=?", (name,)
            ).fetchone()
            if not group:
                raise RuntimeError(f"无法创建自选分组: {name}")
            group_id = group["group_id"]
            for item in items:
                c.execute(
                    "INSERT INTO pt_watchlist_item "
                    "(group_id, code, name, price, reason, added_at) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(group_id, code) DO UPDATE SET "
                    "name=excluded.name, price=excluded.price, "
                    "reason=excluded.reason, added_at=excluded.added_at",
                    (group_id, str(item.get("code", "")), item.get("name", ""),
                     item.get("price"), item.get("reason", ""), now),
                )
            return len(items)

    def get_watchlist_items(self, group_name: str) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT i.code, i.name, i.price, i.reason, i.added_at "
                "FROM pt_watchlist_item i "
                "JOIN pt_watchlist_group g ON g.group_id=i.group_id "
                "WHERE g.name=? ORDER BY i.code",
                (str(group_name),),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------------- 持仓汇总 ----------------
    def get_positions(self) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM pt_position").fetchall()
            return [dict(r) for r in rows]

    def reconcile_positions_with_lots(self) -> List[Dict[str, Any]]:
        """Make position summaries agree with the authoritative FIFO lots."""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT p.*, COUNT(l.lot_id) AS lot_count, "
                "COALESCE(SUM(l.remaining), 0) AS open_quantity, "
                "COALESCE(SUM(CASE WHEN l.quantity > 0 "
                "THEN l.remaining * (l.price + l.fee / l.quantity) "
                "ELSE 0 END), 0) AS open_cost "
                "FROM pt_position p "
                "LEFT JOIN pt_position_lot l ON l.code = p.code "
                "GROUP BY p.code"
            ).fetchall()
            corrected = []
            for row in rows:
                # Rows without lots come from the legacy pre-lot ledger.
                if not row["lot_count"]:
                    continue
                expected_quantity = int(row["open_quantity"] or 0)
                actual_quantity = int(row["quantity"] or 0)
                if actual_quantity == expected_quantity:
                    continue
                expected_cost = (
                    float(row["open_cost"] or 0) / expected_quantity
                    if expected_quantity else 0.0
                )
                c.execute(
                    "UPDATE pt_position SET quantity=?, available=?, avg_cost=?, "
                    "updated_at=? WHERE code=?",
                    (
                        expected_quantity,
                        min(max(0, int(row["available"] or 0)), expected_quantity),
                        expected_cost,
                        datetime.now().isoformat(timespec="seconds"),
                        row["code"],
                    ),
                )
                corrected.append({
                    "code": row["code"],
                    "previous_quantity": actual_quantity,
                    "quantity": expected_quantity,
                })
            return corrected

    def upsert_position(self, p: Dict[str, Any]) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO pt_position(code,name,quantity,available,avg_cost,first_buy_date,"
                "high_water_price,exit_stage,rally_exit_stage,rally_exit_base_quantity,"
                "rally_exit_first_breakout_price,rally_exit_first_sell_price,"
                "rally_exit_second_sell_price,updated_at) "
                "VALUES(:code,:name,:quantity,:available,:avg_cost,:first_buy_date,"
                ":high_water_price,:exit_stage,:rally_exit_stage,:rally_exit_base_quantity,"
                ":rally_exit_first_breakout_price,:rally_exit_first_sell_price,"
                ":rally_exit_second_sell_price,:updated_at) "
                "ON CONFLICT(code) DO UPDATE SET "
                "name=excluded.name,quantity=excluded.quantity,available=excluded.available,"
                "avg_cost=excluded.avg_cost,first_buy_date=excluded.first_buy_date,"
                "high_water_price=excluded.high_water_price,exit_stage=excluded.exit_stage,"
                "rally_exit_stage=excluded.rally_exit_stage,"
                "rally_exit_base_quantity=excluded.rally_exit_base_quantity,"
                "rally_exit_first_breakout_price=excluded.rally_exit_first_breakout_price,"
                "rally_exit_first_sell_price=excluded.rally_exit_first_sell_price,"
                "rally_exit_second_sell_price=excluded.rally_exit_second_sell_price,"
                "updated_at=excluded.updated_at",
                {"high_water_price": 0.0, "exit_stage": 0, "rally_exit_stage": 0,
                 "rally_exit_base_quantity": 0, "rally_exit_first_breakout_price": 0.0,
                 "rally_exit_first_sell_price": 0.0,
                 "rally_exit_second_sell_price": 0.0, **p,
                 "updated_at": datetime.now().isoformat(timespec="seconds")},
            )

    def delete_position(self, code: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM pt_position WHERE code=?", (code,))

    # ---------------- 分批 Lot ----------------
    def add_lot(self, code: str, buy_date: str, quantity: int,
                price: float, fee: float) -> int:
        """新增一批建仓，返回 lot_id。"""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO pt_position_lot(code,buy_date,quantity,price,fee,remaining) "
                "VALUES(?,?,?,?,?,?)",
                (code, buy_date, quantity, price, fee, quantity),
            )
            return cur.lastrowid

    def get_open_lots(self, code: str) -> List[Dict[str, Any]]:
        """返回某只股票所有 remaining>0 的批次，按 FIFO 排序。"""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pt_position_lot WHERE code=? AND remaining>0 "
                "ORDER BY buy_date ASC, lot_id ASC",
                (code,),
            ).fetchall()
            return [dict(r) for r in rows]

    def reduce_lots(self, code: str, qty: int) -> List[Dict[str, Any]]:
        """FIFO 卖出 qty 股，返回卖出的批次明细 [{lot_id, sell_qty, price, fee_share}]。

        在一个事务内完成扣减；若可卖不足则抛 ValueError。
        """
        if qty <= 0:
            return []
        with self._lock, self._conn() as c:
            lots = c.execute(
                "SELECT * FROM pt_position_lot WHERE code=? AND remaining>0 "
                "ORDER BY buy_date ASC, lot_id ASC",
                (code,),
            ).fetchall()
            total = sum(r["remaining"] for r in lots)
            if total < qty:
                raise ValueError(f"{code} 可卖不足: 需 {qty}, 有 {total}")
            sold, out = qty, []
            for r in lots:
                if sold <= 0:
                    break
                take = min(r["remaining"], sold)
                new_rem = r["remaining"] - take
                c.execute("UPDATE pt_position_lot SET remaining=? WHERE lot_id=?",
                          (new_rem, r["lot_id"]))
                # 按比例分摊该批费用
                fee_share = r["fee"] * take / r["quantity"] if r["quantity"] else 0
                out.append({"lot_id": r["lot_id"], "sell_qty": take,
                            "buy_price": r["price"], "fee_share": fee_share})
                sold -= take
            return out

    def mark_all_lots_available(self, code: Optional[str] = None) -> None:
        """T+1：把指定股票（或全部）的 lot 标记为可卖（仅更新 pt_position.available）。"""
        # available 的语义见 PaperAccount.mark_available_for_new_day
        pass

    # ---------------- 交易记录 ----------------
    def insert_trade(self, t: Dict[str, Any]) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO pt_trade(trade_date,trade_time,code,name,side,quantity,price,amount,"
                "commission,stamp_tax,transfer_fee,total,status,reject_reason,signal_msg) "
                "VALUES(:trade_date,:trade_time,:code,:name,:side,:quantity,:price,:amount,"
                ":commission,:stamp_tax,:transfer_fee,:total,:status,:reject_reason,:signal_msg)",
                t,
            )
            return cur.lastrowid

    def get_trades(self, trade_date: Optional[str] = None,
                   limit: int = 500, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return trade-ledger rows, optionally restricted to one order status."""
        with self._lock, self._conn() as c:
            if trade_date and status:
                rows = c.execute(
                    "SELECT * FROM pt_trade WHERE trade_date=? AND status=? "
                    "ORDER BY trade_id DESC LIMIT ?",
                    (trade_date, status, limit),
                ).fetchall()
            elif trade_date:
                rows = c.execute(
                    "SELECT * FROM pt_trade WHERE trade_date=? ORDER BY trade_id DESC LIMIT ?",
                    (trade_date, limit),
                ).fetchall()
            elif status:
                rows = c.execute(
                    "SELECT * FROM pt_trade WHERE status=? ORDER BY trade_id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM pt_trade ORDER BY trade_id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    def get_cash_only_nav_snapshots(self, initial_capital: float) -> List[Dict[str, Any]]:
        """Recover exact end-of-day values for dates closed with no holdings.

        A historical day with open positions cannot be valued safely without its
        closing quotes, so it is intentionally excluded rather than estimated.
        """
        capital = float(initial_capital or 0)
        if capital <= 0:
            return []
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT trade_date, code, side, quantity, total FROM pt_trade "
                "WHERE status = 'filled' ORDER BY trade_date ASC, trade_id ASC"
            ).fetchall()

        cash = capital
        quantities: Dict[str, int] = {}
        snapshots: List[Dict[str, Any]] = []
        current_date = None
        for row in rows:
            trade_date = str(row["trade_date"] or "")[:10]
            if not trade_date:
                continue
            if current_date is not None and trade_date != current_date:
                if not any(quantity > 0 for quantity in quantities.values()):
                    snapshots.append({
                        "trade_date": current_date,
                        "cash": round(cash, 2),
                        "positions_value": 0.0,
                        "total_value": round(cash, 2),
                        "nav": cash / capital,
                        "drawdown": None,
                        "position_count": 0,
                        "daily_pnl": None,
                        "source": "trade_ledger",
                    })
            current_date = trade_date
            code = str(row["code"] or "")
            quantity = int(row["quantity"] or 0)
            total = float(row["total"] or 0)
            if row["side"] in ("buy", "add", "basket_buy"):
                cash -= total
                quantities[code] = quantities.get(code, 0) + quantity
            elif row["side"] in ("sell", "basket_sell"):
                cash += total
                quantities[code] = quantities.get(code, 0) - quantity

        if current_date is not None and not any(quantity > 0 for quantity in quantities.values()):
            snapshots.append({
                "trade_date": current_date,
                "cash": round(cash, 2),
                "positions_value": 0.0,
                "total_value": round(cash, 2),
                "nav": cash / capital,
                "drawdown": None,
                "position_count": 0,
                "daily_pnl": None,
                "source": "trade_ledger",
            })
        return snapshots

    def get_daily_nav_observations(
            self,
            initial_capital: float,
            trading_dates: List[str],
            extra_nav: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Build daily values without estimating a missing position valuation.

        Filled trades reconstruct cash exactly whenever the account is flat.
        They cannot value an open position, so such a date remains unknown until
        it has a persisted NAV snapshot. ``extra_nav`` can add today's account
        valuation supplied by the running engine.
        """
        capital = float(initial_capital or 0)
        dates = sorted({str(value or "")[:10] for value in trading_dates if value})
        if capital <= 0 or not dates:
            return []

        with self._lock, self._conn() as c:
            trades = c.execute(
                "SELECT trade_date, code, side, quantity, total FROM pt_trade "
                "WHERE status = 'filled' ORDER BY trade_date ASC, trade_id ASC"
            ).fetchall()
            snapshots = c.execute(
                "SELECT * FROM pt_nav_snapshot ORDER BY trade_date ASC"
            ).fetchall()

        nav_by_date = {
            str(row["trade_date"] or "")[:10]: dict(row)
            for row in snapshots
            if str(row["trade_date"] or "")[:10]
        }
        for trade_date, nav in (extra_nav or {}).items():
            normalized_date = str(trade_date or "")[:10]
            if normalized_date and isinstance(nav, dict):
                nav_by_date[normalized_date] = dict(nav)

        ordered_trades = [
            trade for trade in trades
            if str(trade["trade_date"] or "")[:10]
        ]

        cash = capital
        quantities: Dict[str, int] = {}
        previous_known_value: Optional[float] = None
        previous_was_known = False
        observations: List[Dict[str, Any]] = []
        trade_index = 0
        for trade_date in dates:
            # Include ledger history before the requested range as well, so an
            # old open position cannot be mistaken for a later cash-only day.
            while trade_index < len(ordered_trades) and str(
                    ordered_trades[trade_index]["trade_date"] or "")[:10] <= trade_date:
                trade = ordered_trades[trade_index]
                code = str(trade["code"] or "")
                quantity = int(trade["quantity"] or 0)
                total = float(trade["total"] or 0)
                if trade["side"] in ("buy", "add", "basket_buy"):
                    cash -= total
                    quantities[code] = quantities.get(code, 0) + quantity
                elif trade["side"] in ("sell", "basket_sell"):
                    cash += total
                    quantities[code] = quantities.get(code, 0) - quantity
                trade_index += 1

            nav = nav_by_date.get(trade_date)
            snapshot_value = float((nav or {}).get("total_value") or 0)
            has_open_positions = any(quantity > 0 for quantity in quantities.values())
            open_position_count = sum(
                1 for quantity in quantities.values() if quantity > 0
            )
            snapshot_position_count = (nav or {}).get("position_count")
            snapshot_matches_ledger = (
                snapshot_position_count is None
                or int(snapshot_position_count) == open_position_count
            )
            if snapshot_value > 0 and snapshot_matches_ledger:
                value = snapshot_value
                source = (nav or {}).get("source") or "nav_snapshot"
                known = True
            elif not has_open_positions:
                value = cash
                source = "cash_ledger"
                known = True
            else:
                value = None
                source = "unvalued_positions"
                known = False

            daily_return_pct = None
            if known and previous_was_known and previous_known_value and previous_known_value > 0:
                daily_return_pct = (value / previous_known_value - 1) * 100
            elif known and previous_known_value is None:
                daily_return_pct = 0.0
            if daily_return_pct is None and known:
                # 前一日估值缺失(如数据保护留空的缺口日)时,退回用快照
                # 自带的 daily_pnl 反推当日收益,分母=当日总值-当日盈亏。
                snapshot_pnl = (nav or {}).get("daily_pnl")
                if snapshot_pnl is not None:
                    base = value - float(snapshot_pnl)
                    if base > 0:
                        daily_return_pct = float(snapshot_pnl) / base * 100

            observations.append({
                "trade_date": trade_date,
                "total_value": round(value, 2) if known else None,
                "daily_return_pct": round(daily_return_pct, 6)
                if daily_return_pct is not None else None,
                "valuation_status": "known" if known else "unknown",
                "source": source,
            })
            if known:
                previous_known_value = value
            previous_was_known = known
        return observations

    # ---------------- 净值快照 ----------------
    def upsert_nav(self, nav: Dict[str, Any]) -> None:
        nav = {**nav, "updated_at": datetime.now().isoformat(timespec="seconds")}
        cols = ["trade_date", "cash", "positions_value", "total_value", "nav",
                "drawdown", "position_count", "daily_pnl", "updated_at"]
        placeholders = ",".join(f":{c}" for c in cols)
        with self._lock, self._conn() as c:
            c.execute(
                f"INSERT INTO pt_nav_snapshot({','.join(cols)}) VALUES({placeholders}) "
                "ON CONFLICT(trade_date) DO UPDATE SET "
                "cash=excluded.cash,positions_value=excluded.positions_value,"
                "total_value=excluded.total_value,nav=excluded.nav,drawdown=excluded.drawdown,"
                "position_count=excluded.position_count,daily_pnl=excluded.daily_pnl,updated_at=excluded.updated_at",
                {c: nav.get(c) for c in cols},
            )

    def get_nav_series(self, days: int = 90) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pt_nav_snapshot ORDER BY trade_date DESC LIMIT ?", (days,)
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    # ---------------- 候选池 ----------------
    def replace_candidates(self, trade_date: str,
                           candidates: List[Dict[str, Any]]) -> int:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM pt_candidate_pool WHERE trade_date=?", (trade_date,))
            for cand in candidates:
                c.execute(
                    "INSERT OR IGNORE INTO pt_candidate_pool"
                    "(trade_date,code,name,hit_strategies,close_at_select,support,resistance) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (trade_date, cand.get("code"), cand.get("name"),
                     cand.get("hit_strategies"), cand.get("close_at_select"),
                     cand.get("support"), cand.get("resistance")),
                )
            return len(candidates)

    def get_candidates(self, trade_date: str) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pt_candidate_pool WHERE trade_date=? ORDER BY rowid ASC",
                (trade_date,),
            ).fetchall()
            return [dict(r) for r in rows]

    def replace_bear_candidates(self, trade_date: str,
                                candidates: List[Dict[str, Any]]) -> int:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM pt_bear_pool WHERE trade_date=?", (trade_date,))
            for cand in candidates:
                c.execute(
                    "INSERT OR IGNORE INTO pt_bear_pool"
                    "(trade_date,code,name,bias10,ret20,close_at_select,"
                    "amount,turnover,market_cap) VALUES(?,?,?,?,?,?,?,?,?)",
                    (trade_date, cand.get("code"), cand.get("name"),
                     cand.get("bias10"), cand.get("ret20"),
                     cand.get("close_at_select"), cand.get("amount"),
                     cand.get("turnover"), cand.get("market_cap")),
                )
            return len(candidates)

    def get_bear_candidates(self, trade_date: str) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM pt_bear_pool WHERE trade_date=? ORDER BY rowid ASC",
                (trade_date,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------------- 信号日志 ----------------
    def insert_signal(self, s: Dict[str, Any]) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO pt_signal_log(generated_at,trade_date,code,side,quantity,"
                "strength,message,source,acted) VALUES(?,?,?,?,?,?,?,?,0)",
                (s.get("generated_at"), s.get("trade_date"), s.get("code"),
                 s.get("side"), s.get("quantity"), s.get("strength"),
                 s.get("message"), s.get("source")),
            )
            return cur.lastrowid

    def get_signals(self, trade_date: Optional[str] = None,
                    limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock, self._conn() as c:
            if trade_date:
                rows = c.execute(
                    "SELECT * FROM pt_signal_log WHERE trade_date=? "
                    "ORDER BY id DESC LIMIT ?", (trade_date, limit)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM pt_signal_log ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(r) for r in rows]

    def latest_signal_id(self) -> Optional[int]:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT MAX(id) AS m FROM pt_signal_log").fetchone()
            return row["m"] if row and row["m"] is not None else None
