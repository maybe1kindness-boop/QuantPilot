#!/usr/bin/env python3
"""Reliable end-of-day A-share K-line updater for the server scheduler."""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from paper_trading.trade_calendar import TradeCalendar
from utils.stock_data_fetcher import StockDataFetcher
from utils.stock_exclusions import filter_stock_codes


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "runtime" / "data" / "stock_selection.db"
STATUS_PATH = ROOT / "runtime" / "data" / "kline_daily_update.json"
FAILURE_NOTICE_DB_PATH = ROOT / "runtime" / "data" / "kline_failure_notifications.sqlite3"
LOGGER = logging.getLogger("kline_daily_update")
BATCH_SIZE = 100
BATCH_PAUSE_SECONDS = 1
BATCH_RETRY_DELAY_SECONDS = 60
BATCH_RETRIES = 2


class MarketDataSourceUnavailable(RuntimeError):
    """Raised when the primary batch data source cannot serve market data."""


def load_stock_codes(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT code FROM stock_basic WHERE code IS NOT NULL
            UNION
            SELECT code FROM stock_kline WHERE code IS NOT NULL
            ORDER BY code
            """
        ).fetchall()
    return filter_stock_codes(str(row[0]) for row in rows)


def upsert_rows(db_path: Path, rows: Iterable[tuple]) -> int:
    records = list(rows)
    if not records:
        return 0
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.executemany(
            """
            INSERT INTO stock_kline (code, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, date) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume
            """,
            records,
        )
    return len(records)


def target_coverage(db_path: Path, target_date: str, total_codes: int) -> tuple[int, float]:
    """Return the persisted code count and coverage for a target trading day."""
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT COUNT(DISTINCT code) FROM stock_kline WHERE date = ?",
            (target_date,),
        ).fetchone()
    written = int(row[0] if row else 0)
    return written, written / total_codes if total_codes else 0.0


def _rows_for_target_date(code: str, frame, target_date: str) -> list[tuple]:
    if frame is None or frame.empty:
        return []
    rows = []
    for row in frame.itertuples(index=False):
        date = str(row.date)[:10]
        if date == target_date:
            rows.append((
                code,
                date,
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                int(float(row.volume)),
            ))
    return rows


def _fetch_tickflow_batch(fetcher: StockDataFetcher, codes: list[str], target_date: str) -> list[tuple]:
    """Fetch a bounded batch and fail fast when TickFlow itself is unavailable."""
    for attempt in range(1, BATCH_RETRIES + 1):
        frames, api_ok = fetcher._fetch_stock_batch_tickflow(codes, days=10)
        if api_ok:
            return [
                row
                for code, frame in frames.items()
                for row in _rows_for_target_date(code, frame, target_date)
            ]
        if attempt < BATCH_RETRIES:
            LOGGER.warning(
                "TickFlow 第 %s 批不可用，%s 秒后重试（%s/%s）",
                len(codes),
                BATCH_RETRY_DELAY_SECONDS,
                attempt,
                BATCH_RETRIES,
            )
            time.sleep(BATCH_RETRY_DELAY_SECONDS)
    raise MarketDataSourceUnavailable(
        "TickFlow 批量行情接口不可用，已停止更新以避免触发腾讯财经 WAF"
    )


def collect_target_rows(codes: list[str], target_date: str) -> list[tuple]:
    """Fetch target-day bars through the low-request-rate TickFlow batch API."""
    fetcher = StockDataFetcher("runtime/data")
    all_rows: list[tuple] = []
    total_batches = (len(codes) + BATCH_SIZE - 1) // BATCH_SIZE
    for batch_number, start in enumerate(range(0, len(codes), BATCH_SIZE), start=1):
        batch_codes = codes[start:start + BATCH_SIZE]
        batch_rows = _fetch_tickflow_batch(fetcher, batch_codes, target_date)
        all_rows.extend(batch_rows)
        LOGGER.info(
            "TickFlow K线进度: %s/%s 批，已取得 %s 条",
            batch_number,
            total_batches,
            len(all_rows),
        )
        if batch_number < total_batches:
            time.sleep(BATCH_PAUSE_SECONDS)
    return all_rows


def _claim_failure_notification(trade_date: str) -> bool:
    """Reserve one update-failure notification per trading day."""
    now = datetime.now()
    now_text = now.isoformat(timespec="seconds")
    stale_before = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
    with sqlite3.connect(FAILURE_NOTICE_DB_PATH, timeout=30) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS kline_failure_notification ("
            "trade_date TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        cursor = connection.execute(
            "INSERT INTO kline_failure_notification(trade_date, status, updated_at) "
            "VALUES (?, 'sending', ?) ON CONFLICT(trade_date) DO UPDATE SET "
            "status = 'sending', updated_at = excluded.updated_at "
            "WHERE kline_failure_notification.status = 'failed' OR "
            "(kline_failure_notification.status = 'sending' "
            "AND kline_failure_notification.updated_at < ?)",
            (trade_date, now_text, stale_before),
        )
    return cursor.rowcount == 1


def _set_failure_notification_status(trade_date: str, status: str) -> None:
    with sqlite3.connect(FAILURE_NOTICE_DB_PATH, timeout=30) as connection:
        connection.execute(
            "UPDATE kline_failure_notification SET status = ?, updated_at = ? "
            "WHERE trade_date = ?",
            (status, datetime.now().isoformat(timespec="seconds"), trade_date),
        )


def notify_failure(message: str) -> None:
    trade_date = default_trade_date()
    if not _claim_failure_notification(trade_date):
        LOGGER.info("K线更新失败通知已发送或正在发送: %s", trade_date)
        return
    try:
        from trading_alert_notifier import TradingAlertNotifier

        delivery = TradingAlertNotifier(
            config={"trading_alerts": {"enabled": True, "provider": "pushplus"}},
            project_root=ROOT,
        )._send("QuantPilot K线更新失败", message)
        _set_failure_notification_status(
            trade_date, "sent" if delivery.get("status") == "sent" else "failed"
        )
    except Exception:
        _set_failure_notification_status(trade_date, "failed")
        LOGGER.exception("K线更新失败通知发送失败")


def write_status(payload: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def default_trade_date(now: datetime | None = None) -> str:
    """Choose the completed trading day for normal and persistent timer runs."""
    now = now or datetime.now()
    candidate = now.date()
    # A persistent systemd timer may execute after midnight.  Before the
    # after-close window, only the previous trading day's bar can be complete.
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        candidate -= timedelta(days=1)
    calendar = TradeCalendar()
    while not calendar.is_trading_day(candidate.isoformat()):
        candidate -= timedelta(days=1)
    return candidate.isoformat()


def run(
    target_date: str,
    max_workers: int,
    min_coverage: float,
    max_stocks: int | None = None,
    retry_attempts: int = 3,
    retry_delay_seconds: float = 15 * 60,
) -> dict:
    if not TradeCalendar().is_trading_day(target_date):
        return {"status": "skipped", "trade_date": target_date, "reason": "非交易日"}
    if not DB_PATH.exists():
        raise RuntimeError(f"行情数据库不存在: {DB_PATH}")

    codes = load_stock_codes(DB_PATH)
    if max_stocks:
        codes = codes[:max_stocks]
    if not codes:
        raise RuntimeError("行情库没有可更新的股票代码")
    retry_attempts = max(1, retry_attempts)
    written = 0
    coverage = 0.0
    for attempt in range(1, retry_attempts + 1):
        rows = collect_target_rows(codes, target_date)
        inserted = upsert_rows(DB_PATH, rows)
        written, coverage = target_coverage(DB_PATH, target_date, len(codes))
        LOGGER.info(
            "K线更新第 %s/%s 轮完成: 本轮写入=%s, 当日覆盖=%s/%s (%.1f%%)",
            attempt,
            retry_attempts,
            inserted,
            written,
            len(codes),
            coverage * 100,
        )
        if coverage >= min_coverage:
            break
        if attempt < retry_attempts:
            LOGGER.warning(
                "当日K线覆盖不足，%s 秒后进行第 %s 轮重试",
                retry_delay_seconds,
                attempt + 1,
            )
            time.sleep(max(0.0, retry_delay_seconds))

    result = {
        "status": "ok" if coverage >= min_coverage else "failed",
        "trade_date": target_date,
        "requested": len(codes),
        "written": written,
        "coverage": round(coverage, 4),
        "minimum_coverage": min_coverage,
        "attempts": attempt,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    if result["status"] == "failed":
        raise RuntimeError(
            f"当日K线覆盖不足: {written}/{len(codes)} ({coverage:.1%})，要求至少 {min_coverage:.0%}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="更新并校验每日A股K线")
    parser.add_argument("--trade-date", default=default_trade_date())
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--min-coverage", type=float, default=0.95)
    parser.add_argument("--max-stocks", type=int, default=None)
    parser.add_argument("--retry-attempts", type=int, default=3)
    parser.add_argument("--retry-delay-minutes", type=float, default=15)
    args = parser.parse_args()
    started_at = datetime.now().isoformat(timespec="seconds")
    try:
        result = run(
            target_date=args.trade_date,
            max_workers=max(1, min(args.workers, 4)),
            min_coverage=max(0.0, min(args.min_coverage, 1.0)),
            max_stocks=max(1, args.max_stocks) if args.max_stocks else None,
            retry_attempts=max(1, args.retry_attempts),
            retry_delay_seconds=max(0.0, args.retry_delay_minutes * 60),
        )
        result["started_at"] = started_at
        write_status(result)
        LOGGER.info("K线更新完成: %s", result)
        return 0
    except Exception as exc:
        result = {
            "status": "failed",
            "trade_date": args.trade_date,
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "reason": str(exc),
        }
        write_status(result)
        LOGGER.exception("K线更新失败")
        notify_failure(f"【{args.trade_date}】{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
