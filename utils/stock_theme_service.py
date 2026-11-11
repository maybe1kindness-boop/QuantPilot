"""Resolve and cache concept themes associated with screened stocks."""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

import requests

logger = logging.getLogger(__name__)


class StockThemeService:
    """Load EastMoney concept memberships and cache them in SQLite."""

    F10_URL = (
        "https://emweb.securities.eastmoney.com/"
        "PC_HSF10/CoreConception/PageAjax"
    )
    EXCLUDED_NAMES = {
        "融资融券", "沪股通", "深股通", "富时罗素", "MSCI中国", "标准普尔",
        "HS300_", "上证50_", "上证180_", "央视50_", "深证100R", "深成500",
        "创业板综", "创业成份", "科创50", "AH股", "A股", "百元股", "低价股",
        "大盘股", "小盘股", "权重股", "大盘成长", "大盘价值", "行业龙头",
        "茅指数", "宁组合", "基金重仓", "社保重仓", "QFII重仓", "机构重仓",
        "券商金股", "趋势股", "周期股", "东方财富热股",
        "股权分散", "股权激励", "题材股", "反转股", "价值股", "成长股",
        "最近多板", "转债标的", "高股息", "破净股", "长期破净",
        "养老金", "参股新三板", "参股保险", "参股券商", "参股银行",
        "创投", "独角兽", "央国企改革", "国企改革", "地方国企改革",
        "增持回购", "股份回购", "限售股解禁", "送转预期", "高送转",
    }
    EXCLUDED_FRAGMENTS = (
        "风格", "昨日", "上证", "深证", "中证", "股通", "成份", "融资融券",
        "中报", "一季报", "年报", "预增", "预减", "预盈", "预亏", "首亏",
        "市净率", "市盈率", "大盘", "中盘", "小盘", "重仓", "持股",
        "参股", "股权", "回购", "解禁", "送转", "改革", "热股",
    )

    def __init__(self, db_manager, timeout: float = 8.0, max_workers: int = 8):
        self.db_manager = db_manager
        self.timeout = timeout
        self.max_workers = max(1, min(int(max_workers), 12))
        self._attempted_at: Dict[str, float] = {}
        self._attempt_lock = threading.Lock()

    def get_stock_themes(
        self,
        stock_codes: Iterable[str],
        max_themes: int = 5,
        force_refresh: bool = False,
    ) -> Dict[str, List[Dict[str, Any]]]:
        profiles = self.get_stock_profiles(
            stock_codes,
            max_themes=max_themes,
            force_refresh=force_refresh,
        )
        return {code: profile["themes"] for code, profile in profiles.items()}

    def get_stock_profiles(
        self,
        stock_codes: Iterable[str],
        max_themes: int = 5,
        force_refresh: bool = False,
    ) -> Dict[str, Dict[str, Any]]:
        codes = list(dict.fromkeys(self._normalize_code(code) for code in stock_codes))
        codes = [code for code in codes if code][:100]
        max_themes = max(1, min(int(max_themes), 8))
        if not codes:
            return {}

        catalog = self._load_sector_catalog()
        cached, latest_dates = self._load_cached_associations(codes)
        industries = self._load_cached_industries(codes)
        today = date.today().isoformat()
        missing = [
            code for code in codes
            if (
                force_refresh
                or latest_dates.get(code) != today
                or not cached.get(code)
                or not industries.get(code)
            )
        ]
        missing = [code for code in missing if force_refresh or self._can_attempt(code)]

        fetched: Dict[str, Optional[Dict[str, Any]]] = {}
        if missing and catalog:
            worker_count = min(self.max_workers, len(missing))
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {
                    pool.submit(self._fetch_stock_profile, code, catalog): code
                    for code in missing
                }
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        fetched[code] = future.result()
                    except Exception as exc:
                        logger.warning("获取 %s 关联题材失败: %s", code, exc)
                        fetched[code] = None
                    finally:
                        self._mark_attempt(code)

        successful = {
            code: profile for code, profile in fetched.items() if profile is not None
        }
        if successful:
            self._persist_profiles(successful, today)
            for code, profile in successful.items():
                themes = profile.get("themes") or []
                industry = str(profile.get("industry") or "").strip()
                if themes:
                    cached[code] = themes
                    latest_dates[code] = today
                if industry:
                    industries[code] = industry

        return {
            code: {
                "industry": industries.get(code) or "未分类",
                "themes": self._select_display_themes(cached.get(code, []), max_themes),
            }
            for code in codes
        }

    def _load_sector_catalog(self) -> Dict[str, Dict[str, Any]]:
        rows = self.db_manager.query(
            "SELECT sector_code, sector_name, sector_change, rank_position "
            "FROM stock_sector WHERE sector_type = '概念'"
        )
        return {
            str(row.get("sector_code") or ""): {
                "code": str(row.get("sector_code") or ""),
                "name": str(row.get("sector_name") or ""),
                "change": row.get("sector_change"),
                "rank": row.get("rank_position"),
            }
            for row in rows
            if row.get("sector_code") and row.get("sector_name")
        }

    def _load_cached_associations(
        self, codes: List[str]
    ) -> tuple[Dict[str, List[Dict[str, Any]]], Dict[str, str]]:
        placeholders = ",".join("?" for _ in codes)
        rows = self.db_manager.query(
            "SELECT m.id AS relevance_rank, m.stock_code, m.mapping_date, "
            "s.sector_code, s.sector_name, "
            "s.sector_change, s.rank_position "
            "FROM stock_sector_mapping m "
            "JOIN stock_sector s ON s.sector_code = m.sector_code "
            "JOIN (SELECT stock_code, MAX(mapping_date) AS mapping_date "
            "      FROM stock_sector_mapping "
            f"      WHERE stock_code IN ({placeholders}) GROUP BY stock_code) latest "
            "ON latest.stock_code = m.stock_code "
            "AND latest.mapping_date = m.mapping_date",
            tuple(codes),
        )
        cached: Dict[str, List[Dict[str, Any]]] = {code: [] for code in codes}
        latest_dates: Dict[str, str] = {}
        for row in rows:
            code = self._normalize_code(row.get("stock_code"))
            if not code:
                continue
            latest_dates[code] = str(row.get("mapping_date") or "")
            cached.setdefault(code, []).append({
                "code": str(row.get("sector_code") or ""),
                "name": str(row.get("sector_name") or ""),
                "change": row.get("sector_change"),
                "rank": row.get("rank_position"),
                "relevance_rank": row.get("relevance_rank"),
            })
        return cached, latest_dates

    def _load_cached_industries(self, codes: List[str]) -> Dict[str, str]:
        placeholders = ",".join("?" for _ in codes)
        try:
            rows = self.db_manager.query(
                f"SELECT code, industry FROM stock_basic WHERE code IN ({placeholders})",
                tuple(codes),
            )
        except Exception:
            return {}
        return {
            self._normalize_code(row.get("code")): str(row.get("industry") or "").strip()
            for row in rows
            if self._normalize_code(row.get("code")) and row.get("industry")
        }

    def _fetch_stock_profile(
        self, stock_code: str, catalog: Dict[str, Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        market_code = self._eastmoney_market_code(stock_code)
        response = requests.get(
            self.F10_URL,
            params={"code": market_code},
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": (
                    "https://emweb.securities.eastmoney.com/PC_HSF10/"
                    f"CoreConception/Index?type=web&code={market_code}"
                ),
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        board_rows = payload.get("ssbk", [])
        rows: List[Dict[str, Any]] = []
        seen = set()
        for item in board_rows:
            raw_code = str(item.get("BOARD_CODE") or "").strip()
            sector_code = raw_code if raw_code.startswith("BK") else f"BK{raw_code.zfill(4)}"
            if sector_code in catalog and sector_code not in seen:
                theme = dict(catalog[sector_code])
                theme["relevance_rank"] = item.get("BOARD_RANK")
                rows.append(theme)
                seen.add(sector_code)
        return {
            "industry": self._extract_industry(board_rows),
            "themes": rows,
        }

    def _persist_profiles(
        self,
        profiles: Dict[str, Dict[str, Any]],
        mapping_date: str,
    ) -> None:
        created_date = datetime.now().isoformat(timespec="seconds")
        with self.db_manager.transaction():
            for stock_code, profile in profiles.items():
                themes = profile.get("themes") or []
                industry = str(profile.get("industry") or "").strip()
                if themes:
                    self.db_manager.execute(
                        "DELETE FROM stock_sector_mapping "
                        "WHERE stock_code = ? AND mapping_date = ?",
                        (stock_code, mapping_date),
                    )
                    for theme in themes:
                        self.db_manager.execute(
                            "INSERT OR IGNORE INTO stock_sector_mapping "
                            "(stock_code, sector_code, mapping_date, created_date) "
                            "VALUES (?, ?, ?, ?)",
                            (stock_code, theme["code"], mapping_date, created_date),
                        )
                if industry:
                    self.db_manager.execute(
                        "UPDATE stock_basic SET industry = ? WHERE code = ?",
                        (industry, stock_code),
                    )

    def _select_display_themes(
        self, themes: List[Dict[str, Any]], max_themes: int
    ) -> List[Dict[str, Any]]:
        candidates = sorted(
            [theme for theme in themes if self.is_business_theme(theme.get("name"))],
            key=lambda item: (
                self._safe_rank(item.get("relevance_rank")),
                self._safe_rank(item.get("rank")),
                -self._safe_change(item.get("change")),
                str(item.get("name") or ""),
            ),
        )
        return candidates[:max_themes]

    @classmethod
    def _is_display_theme(cls, name: Any) -> bool:
        return cls.is_business_theme(name)

    @classmethod
    def is_business_theme(cls, name: Any) -> bool:
        """Return whether a concept describes business exposure, not market style."""
        text = str(name or "").strip()
        if not text or text in cls.EXCLUDED_NAMES or "_" in text:
            return False
        return not any(fragment in text for fragment in cls.EXCLUDED_FRAGMENTS)

    @staticmethod
    def _extract_industry(board_rows: List[Dict[str, Any]]) -> str:
        candidates = []
        for item in board_rows:
            # EastMoney marks formal industry boards with IS_PRECISE=0. The
            # remaining entries are concepts, regions, styles, and indexes.
            # Industry entries are the first contiguous precise group; later
            # precise entries such as QFII or financing are style labels.
            if str(item.get("IS_PRECISE") or "").strip() != "0":
                if candidates:
                    break
                continue
            name = str(item.get("BOARD_NAME") or "").strip()
            if not name or name.endswith("板块"):
                if candidates:
                    break
                continue
            candidates.append(name)
        if not candidates:
            return ""
        return re.sub(r"[ⅠⅡⅢⅣⅤ]+$", "", candidates[-1]).strip()

    def _can_attempt(self, code: str) -> bool:
        with self._attempt_lock:
            return time.time() - self._attempted_at.get(code, 0.0) > 3600

    def _mark_attempt(self, code: str) -> None:
        with self._attempt_lock:
            self._attempted_at[code] = time.time()

    @staticmethod
    def _normalize_code(code: Any) -> str:
        text = str(code or "").strip().upper()
        text = text.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
        text = text.removeprefix("SH").removeprefix("SZ").removeprefix("BJ")
        return text.zfill(6) if text.isdigit() else ""

    @staticmethod
    def _eastmoney_market_code(stock_code: str) -> str:
        if stock_code.startswith(("4", "8", "92")):
            return f"BJ{stock_code}"
        if stock_code.startswith(("5", "6", "9")):
            return f"SH{stock_code}"
        return f"SZ{stock_code}"

    @staticmethod
    def _safe_rank(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1_000_000

    @staticmethod
    def _safe_change(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
