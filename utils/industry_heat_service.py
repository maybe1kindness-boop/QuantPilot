"""Refresh and persist the end-of-day industry heat ranking."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import logging
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from utils.industry_fetcher import EastMoneyIndustryRankingSource

logger = logging.getLogger(__name__)


class TongHuaShunIndustryRankingSource:
    """Fallback industry heat source with the most recently closed snapshot."""

    name = "同花顺行业汇总"

    def fetch(self) -> Optional[pd.DataFrame]:
        import akshare as ak

        summary = ak.stock_board_industry_summary_ths()
        catalog = ak.stock_board_industry_name_ths()
        if summary is None or summary.empty or catalog is None or catalog.empty:
            return None
        code_by_name = {
            str(row.get("name") or "").strip(): str(row.get("code") or "").strip()
            for _, row in catalog.iterrows()
        }
        frame = summary.copy()
        frame["行业代码"] = frame["板块"].map(code_by_name)
        frame = frame.rename(columns={"板块": "行业名称"})
        return frame


class IndustryHeatService:
    """Use five complete trading-day industry returns as the heat gate."""

    HISTORY_DAYS = 5
    MIN_COMPLETE_SNAPSHOT_SIZE = 80

    def __init__(
        self, db_manager, source=None, now_provider=datetime.now,
        sleep_fn=time.sleep, retries: int = 3,
    ):
        self.db = db_manager
        # TongHuaShun is the primary source because it also provides a daily
        # industry-index history under the same industry codes.  EastMoney is
        # retained as a fallback for the end-of-day snapshot.
        self.sources = [source] if source is not None else [
            TongHuaShunIndustryRankingSource(), EastMoneyIndustryRankingSource(),
        ]
        self.now_provider = now_provider
        self.sleep_fn = sleep_fn
        self.retries = max(1, int(retries))

    def refresh(self, trade_date: str, limit: int = 10) -> List[Dict[str, Any]]:
        self._ensure_snapshot_table()
        rows, source_name = self._fetch_rows()
        timestamp = self._timestamp_for_trade_date(trade_date)
        with self.db.transaction():
            for row in rows:
                self.db.execute(
                    """
                    INSERT OR REPLACE INTO stock_industry
                    (industry_code, industry_name, stock_count, industry_change,
                     rank_position, updated_date)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["code"], row["name"], row["stock_count"], row["change"],
                        row["rank"], timestamp,
                    ),
                )
            self._persist_snapshot(trade_date, rows, source_name, timestamp)
        return [dict(row, source=source_name) for row in rows[:max(1, int(limit))]]

    def ensure_recent_history(self, trade_date: str) -> List[str]:
        """Ensure five complete daily snapshots exist, backfilling when needed."""
        self._ensure_snapshot_table()
        dates = self._complete_snapshot_dates(trade_date)
        if len(dates) < self.HISTORY_DAYS:
            self._backfill_ths_history(trade_date)
            dates = self._complete_snapshot_dates(trade_date)
        if len(dates) < self.HISTORY_DAYS:
            raise RuntimeError(
                f"行业热度历史不完整：仅有 {len(dates)}/{self.HISTORY_DAYS} 个完整交易日快照"
            )
        return dates[-self.HISTORY_DAYS:]

    def _fetch_rows(self) -> tuple[List[Dict[str, Any]], str]:
        errors = []
        for source in self.sources:
            source_name = str(getattr(source, "name", source.__class__.__name__))
            for attempt in range(1, self.retries + 1):
                try:
                    frame = source.fetch()
                    rows = self._normalize(frame) if frame is not None else []
                    if rows:
                        return rows, source_name
                    error = "返回空数据"
                except Exception as exc:
                    error = str(exc)
                errors.append(f"{source_name} 第{attempt}次：{error}")
                logger.warning("行业热度获取失败：%s", errors[-1])
                if attempt < self.retries:
                    self.sleep_fn(attempt)
        raise RuntimeError("行业板块热度获取失败：" + "；".join(errors))

    def _timestamp_for_trade_date(self, trade_date: str) -> str:
        now = self.now_provider()
        return f"{str(trade_date)[:10]}T{now.strftime('%H:%M:%S')}"

    def current_top(self, trade_date: str, limit: int = 10) -> List[Dict[str, Any]]:
        dates = self.ensure_recent_history(trade_date)
        placeholders = ", ".join("?" for _ in dates)
        rows = self.db.query(
            f"""
            SELECT industry_code, industry_name,
                   AVG(industry_change) AS heat_score,
                   MAX(stock_count) AS stock_count,
                   AVG(rank_position) AS average_rank,
                   COUNT(DISTINCT trade_date) AS day_count,
                   MAX(trade_date) AS updated_date
            FROM industry_heat_snapshot
            WHERE trade_date IN ({placeholders})
            GROUP BY industry_code, industry_name
            HAVING COUNT(DISTINCT trade_date) = ?
            ORDER BY heat_score DESC, average_rank ASC, industry_name ASC
            LIMIT ?
            """,
            tuple(dates) + (len(dates), max(1, int(limit))),
        )
        return [
            {
                "code": str(row.get("industry_code") or ""),
                "name": str(row.get("industry_name") or ""),
                "stock_count": int(row.get("stock_count") or 0),
                "change": float(row.get("heat_score") or 0.0),
                "heat_score": float(row.get("heat_score") or 0.0),
                "rank": index,
                "average_rank": float(row.get("average_rank") or 0.0),
                "day_count": int(row.get("day_count") or 0),
                "updated_at": str(row.get("updated_date") or ""),
            }
            for index, row in enumerate(rows, 1)
            if row.get("industry_name")
        ]

    def _ensure_snapshot_table(self) -> None:
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS industry_heat_snapshot (
                trade_date TEXT NOT NULL,
                industry_code TEXT NOT NULL,
                industry_name TEXT NOT NULL,
                stock_count INTEGER NOT NULL DEFAULT 0,
                industry_change REAL NOT NULL,
                rank_position INTEGER NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, industry_code)
            )
            """
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_industry_heat_snapshot_date "
            "ON industry_heat_snapshot(trade_date)"
        )

    def _persist_snapshot(
        self, trade_date: str, rows: List[Dict[str, Any]], source_name: str,
        timestamp: str,
    ) -> None:
        for row in rows:
            self.db.execute(
                """
                INSERT OR REPLACE INTO industry_heat_snapshot
                (trade_date, industry_code, industry_name, stock_count,
                 industry_change, rank_position, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(trade_date)[:10], row["code"], row["name"],
                    row["stock_count"], row["change"], row["rank"],
                    source_name, timestamp,
                ),
            )

    def _complete_snapshot_dates(self, trade_date: str) -> List[str]:
        rows = self.db.query(
            """
            SELECT trade_date, COUNT(*) AS industry_count
            FROM industry_heat_snapshot
            WHERE trade_date <= ?
            GROUP BY trade_date
            HAVING COUNT(*) >= ?
            ORDER BY trade_date ASC
            """,
            (str(trade_date)[:10], self.MIN_COMPLETE_SNAPSHOT_SIZE),
        )
        return [str(row["trade_date"])[:10] for row in rows]

    def _backfill_ths_history(self, trade_date: str) -> None:
        """Backfill daily returns from the same THS industry index universe."""
        import akshare as ak

        end_date = str(trade_date)[:10]
        start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=20))
        catalog = ak.stock_board_industry_name_ths()
        if catalog is None or catalog.empty:
            raise RuntimeError("同花顺行业历史目录为空")
        industry_rows = [
            (str(row.get("name") or "").strip(), str(row.get("code") or "").strip())
            for _, row in catalog.iterrows()
            if str(row.get("name") or "").strip() and str(row.get("code") or "").strip()
        ]
        if len(industry_rows) < self.MIN_COMPLETE_SNAPSHOT_SIZE:
            raise RuntimeError(f"同花顺行业历史目录不足：{len(industry_rows)} 个行业")

        history: Dict[str, List[Dict[str, Any]]] = {}
        failures = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(
                    self._fetch_ths_history, name, code,
                    start_date.strftime("%Y%m%d"), end_date.replace("-", ""),
                ): (name, code)
                for name, code in industry_rows
            }
            for future in as_completed(futures):
                name, _code = futures[future]
                try:
                    for item in future.result():
                        history.setdefault(item["trade_date"], []).append(item)
                except Exception as exc:
                    failures.append(f"{name}: {exc}")
        valid_dates = [
            date for date, rows in history.items()
            if date <= end_date and len(rows) >= self.MIN_COMPLETE_SNAPSHOT_SIZE
        ]
        valid_dates.sort()
        if len(valid_dates) < self.HISTORY_DAYS:
            detail = "；".join(failures[:3]) or "返回的完整日线不足"
            raise RuntimeError(
                f"同花顺行业历史补齐失败：完整交易日 {len(valid_dates)}/{self.HISTORY_DAYS}；{detail}"
            )
        timestamp = self._timestamp_for_trade_date(end_date)
        for date in valid_dates:
            rows = history[date]
            rows.sort(key=lambda row: (-row["change"], row["name"], row["code"]))
            for rank, row in enumerate(rows, 1):
                row["rank"] = rank
            with self.db.transaction():
                self._persist_snapshot(date, rows, "同花顺行业指数历史", timestamp)

    @staticmethod
    def _fetch_ths_history(
        name: str, code: str, start_date: str, end_date: str,
    ) -> List[Dict[str, Any]]:
        import akshare as ak

        frame = ak.stock_board_industry_index_ths(
            symbol=name, start_date=start_date, end_date=end_date,
        )
        if frame is None or frame.empty:
            return []
        frame = frame.copy()
        frame["收盘价"] = pd.to_numeric(frame["收盘价"], errors="coerce")
        frame["daily_change"] = frame["收盘价"].pct_change() * 100
        rows = []
        for _, item in frame.iterrows():
            value = item.get("daily_change")
            date = pd.to_datetime(item.get("日期"), errors="coerce")
            if pd.isna(date) or pd.isna(value):
                continue
            rows.append({
                "trade_date": date.strftime("%Y-%m-%d"), "code": code,
                "name": name, "change": float(value), "stock_count": 0,
            })
        return rows

    @staticmethod
    def _normalize(frame: pd.DataFrame) -> List[Dict[str, Any]]:
        rows = []
        for _, item in frame.iterrows():
            code = str(item.get("行业代码") or item.get("code") or "").strip()
            name = str(item.get("行业名称") or item.get("板块") or "").strip()
            if not code or not name:
                continue
            rows.append({
                "code": code,
                "name": name,
                "change": IndustryHeatService._number(item.get("涨跌幅")),
                "stock_count": (
                    IndustryHeatService._integer(item.get("上涨家数"))
                    + IndustryHeatService._integer(item.get("下跌家数"))
                ),
            })
        rows.sort(key=lambda row: (-row["change"], row["name"], row["code"]))
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank
        return rows

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _integer(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0
