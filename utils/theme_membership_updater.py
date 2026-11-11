"""Refresh cached A-share concept constituents before the trading session.

The intraday monitor must not discover a theme from a small candidate list.
This job downloads the concept associations for every locally known A-share and
stores them locally for the trading session to consume without network calls.
"""
from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any, Dict, Iterable, Optional, Set

import requests

from utils.stock_theme_service import StockThemeService

logger = logging.getLogger(__name__)


class ThemeMembershipUpdater:
    """Maintain a complete, daily concept-board to stock mapping cache."""

    URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax"
    HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://emweb.securities.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(self, db_manager, timeout: float = 12.0, max_workers: int = 10):
        self.db = db_manager
        self.timeout = max(3.0, float(timeout))
        self.max_workers = max(1, min(int(max_workers), 16))

    def _concepts(self) -> list[Dict[str, str]]:
        rows = self.db.query(
            "SELECT sector_code, sector_name FROM stock_sector "
            "WHERE sector_type = '概念'"
        )
        concepts = []
        for row in rows:
            code = str(row.get("sector_code") or "").strip()
            name = str(row.get("sector_name") or "").strip()
            if code.startswith("BK") and StockThemeService.is_business_theme(name):
                concepts.append({"code": code, "name": name})
        return sorted(concepts, key=lambda row: row["code"])

    def _stock_codes(self) -> list[str]:
        rows = self.db.query("SELECT code FROM stock_basic")
        return sorted({
            str(row.get("code") or "").strip().zfill(6)
            for row in rows
            if str(row.get("code") or "").strip().isdigit()
        })

    @staticmethod
    def _market_code(stock_code: str) -> str:
        if stock_code.startswith(("4", "8", "92")):
            return f"BJ{stock_code}"
        return f"SH{stock_code}" if stock_code.startswith(("5", "6", "9")) else f"SZ{stock_code}"

    @staticmethod
    def _parse_themes(payload: Dict[str, Any], catalog: Set[str]) -> Set[str]:
        themes = set()
        for row in payload.get("ssbk") or []:
            raw_code = str((row or {}).get("BOARD_CODE") or "").strip().upper()
            sector_code = raw_code if raw_code.startswith("BK") else f"BK{raw_code.zfill(4)}"
            if sector_code in catalog:
                themes.add(sector_code)
        return themes

    def _fetch_stock_themes(self, stock_code: str, catalog: Set[str]) -> tuple[str, Set[str]]:
        market_code = self._market_code(stock_code)
        last_error = None
        for attempt in range(3):
            try:
                response = requests.get(
                    self.URL,
                    params={"code": market_code},
                    headers={
                        **self.HEADERS,
                        "Referer": (
                            "https://emweb.securities.eastmoney.com/PC_HSF10/"
                            f"CoreConception/Index?type=web&code={market_code}"
                        ),
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
                if "ssbk" not in payload:
                    raise RuntimeError("题材接口返回异常")
                return stock_code, self._parse_themes(payload, catalog)
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(str(last_error) if last_error else "题材接口请求失败")

    def _ensure_status_table(self) -> None:
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS theme_membership_refresh_status (
                mapping_date TEXT PRIMARY KEY,
                stock_count INTEGER NOT NULL,
                success_count INTEGER NOT NULL,
                mapped_stock_count INTEGER NOT NULL,
                mapping_count INTEGER NOT NULL,
                completed_at TEXT NOT NULL
            )
            """
        )

    def refresh(self, mapping_date: Optional[str] = None) -> Dict[str, Any]:
        mapping_date = str(mapping_date or date.today().isoformat())[:10]
        self._ensure_status_table()
        concepts = self._concepts()
        if not concepts:
            raise RuntimeError("概念板块目录为空，无法刷新题材成分")
        stock_codes = self._stock_codes()
        if not stock_codes:
            raise RuntimeError("股票目录为空，无法刷新题材成分")

        catalog = {item["code"] for item in concepts}
        fetched: Dict[str, Set[str]] = {}
        failures: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(stock_codes))) as pool:
            futures = {
                pool.submit(self._fetch_stock_themes, stock_code, catalog): stock_code
                for stock_code in stock_codes
            }
            for future in as_completed(futures):
                stock_code = futures[future]
                try:
                    code, themes = future.result()
                    fetched[code] = themes
                except Exception as exc:
                    failures[stock_code] = str(exc)[:160]

        if len(fetched) != len(stock_codes):
            raise RuntimeError(
                f"题材关联刷新覆盖不足: {len(fetched)}/{len(stock_codes)}，"
                "必须覆盖全市场股票后才写入当天缓存"
            )

        created_at = datetime.now().isoformat(timespec="seconds")
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM stock_sector_mapping WHERE mapping_date = ?", (mapping_date,)
            )
            for stock_code, themes in fetched.items():
                for sector_code in themes:
                    self.db.execute(
                        "INSERT OR IGNORE INTO stock_sector_mapping "
                        "(stock_code, sector_code, mapping_date, created_date) "
                        "VALUES (?, ?, ?, ?)",
                        (stock_code, sector_code, mapping_date, created_at),
                    )
            self.db.execute(
                """
                INSERT INTO theme_membership_refresh_status
                (mapping_date, stock_count, success_count, mapped_stock_count,
                 mapping_count, completed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(mapping_date) DO UPDATE SET
                    stock_count = excluded.stock_count,
                    success_count = excluded.success_count,
                    mapped_stock_count = excluded.mapped_stock_count,
                    mapping_count = excluded.mapping_count,
                    completed_at = excluded.completed_at
                """,
                (
                    mapping_date, len(stock_codes), len(fetched),
                    sum(1 for themes in fetched.values() if themes),
                    sum(len(themes) for themes in fetched.values()), created_at,
                ),
            )

        mapped_stock_count = sum(1 for themes in fetched.values() if themes)
        report = {
            "status": "ok",
            "mapping_date": mapping_date,
            "concept_count": len(concepts),
            "stock_count": len(stock_codes),
            "success_count": len(fetched),
            "failed_count": len(failures),
            "mapped_stock_count": mapped_stock_count,
            "mapping_count": sum(len(themes) for themes in fetched.values()),
            "full_market_covered": len(fetched) == len(stock_codes),
            "failures": failures,
        }
        logger.info("题材成分刷新完成: %s", report)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新东方财富概念板块成分缓存")
    parser.add_argument("--trade-date", default=None)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()
    from utils.global_db import get_global_db

    report = ThemeMembershipUpdater(get_global_db(), max_workers=args.workers).refresh(
        args.trade_date
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
