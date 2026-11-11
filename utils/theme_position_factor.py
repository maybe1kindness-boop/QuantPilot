"""Translate real business-theme heat into a conservative position multiplier."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, Optional

class ThemePositionFactor:
    """Score only source-validated core-business themes without network requests."""

    MAX_SNAPSHOT_AGE_DAYS = 120
    WEIGHTS = (0.60, 0.25, 0.15)

    def __init__(self, db_manager):
        self.db_manager = db_manager

    def evaluate(self, stock_code: str, trade_date: Optional[str] = None) -> Dict[str, Any]:
        code = str(stock_code or "").strip().zfill(6)
        target_date = self._parse_date(trade_date) or date.today()
        profile = self.db_manager.query_one(
            "SELECT report_date, validation_status, validation_reason, fetched_at "
            "FROM stock_core_business WHERE stock_code = ?",
            (code,),
        ) or {}
        snapshot_date = self._parse_date(profile.get("fetched_at"))
        if snapshot_date is None:
            return self._unavailable("无主营业务校验快照")
        if snapshot_date > target_date:
            return self._unavailable("历史交易日不使用未来主营业务快照")
        age_days = (target_date - snapshot_date).days
        if age_days > self.MAX_SNAPSHOT_AGE_DAYS:
            return self._unavailable(f"主营业务快照已过期{age_days}天")
        if profile.get("validation_status") != "verified":
            return self._unavailable(str(profile.get("validation_reason") or "主营业务未通过校验"))

        rows = self.db_manager.query(
            "SELECT theme_name, confidence, evidence_json, market_aliases_json "
            "FROM stock_core_business_theme WHERE stock_code = ?",
            (code,),
        )
        total_row = self.db_manager.query_one(
            "SELECT COUNT(*) AS total FROM stock_sector WHERE sector_type = '概念'"
        ) or {}
        total = max(1, int(total_row.get("total") or 1))
        themes = []
        for row in rows:
            aliases = self._json_list(row.get("market_aliases_json"))
            market_rows = self._market_rows(aliases)
            if not market_rows:
                continue
            market = min(market_rows, key=lambda item: self._integer(item.get("rank_position"), total))
            name = str(row.get("theme_name") or "").strip()
            change = self._number(market.get("sector_change"))
            rank = self._integer(market.get("rank_position"), total)
            rank_score = self._rank_score(rank, total)
            change_score = self._change_score(change)
            confidence = max(0.0, min(1.0, self._number(row.get("confidence"))))
            score = round((rank_score * 0.65 + change_score * 0.35) * confidence, 1)
            themes.append({
                "code": str(market.get("sector_code") or ""),
                "name": name,
                "market_theme": str(market.get("sector_name") or ""),
                "change": round(change, 2),
                "rank": rank,
                "confidence": round(confidence, 2),
                "score": score,
            })
        themes.sort(key=lambda item: (-item["score"], item["rank"], item["name"]))

        if not themes:
            return {
                "available": True,
                "snapshot_date": snapshot_date.isoformat(),
                "score": 0.0,
                "multiplier": 0.30,
                "themes": [],
                "reason": "主营业务已校验，但没有可用的市场题材热度映射",
            }

        top = themes[: len(self.WEIGHTS)]
        active_weights = self.WEIGHTS[: len(top)]
        weight_sum = sum(active_weights)
        score = round(
            sum(theme["score"] * weight for theme, weight in zip(top, active_weights))
            / weight_sum,
            1,
        )
        multiplier = self._multiplier(score)
        names = "、".join(theme["name"] for theme in top)
        return {
            "available": True,
            "snapshot_date": snapshot_date.isoformat(),
            "score": score,
            "multiplier": multiplier,
            "themes": themes[:5],
            "reason": f"已校验主营题材热度{score:.1f}分({names})，仓位系数{multiplier:.0%}",
        }

    def _market_rows(self, aliases: list[str]) -> list[Dict[str, Any]]:
        aliases = [str(item).strip() for item in aliases if str(item).strip()]
        if not aliases:
            return []
        placeholders = ",".join("?" for _ in aliases)
        return self.db_manager.query(
            "SELECT sector_code, sector_name, sector_change, rank_position "
            "FROM stock_sector WHERE sector_type = '概念' "
            f"AND sector_name IN ({placeholders})",
            tuple(aliases),
        )

    @staticmethod
    def _json_list(value: Any) -> list[str]:
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def _multiplier(score: float) -> float:
        if score >= 85:
            return 1.0
        if score >= 75:
            return 0.85
        if score >= 65:
            return 0.70
        if score >= 55:
            return 0.50
        return 0.30

    @staticmethod
    def _rank_score(rank: int, total: int) -> float:
        if total <= 1:
            return 50.0
        return max(0.0, min(100.0, 100.0 * (1 - (rank - 1) / (total - 1))))

    @staticmethod
    def _change_score(change: float) -> float:
        return max(0.0, min(100.0, 50.0 + change * 12.5))

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        if isinstance(value, date):
            return value
        try:
            return datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _number(value: Any) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _integer(value: Any, default: int) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return max(1, default)

    @staticmethod
    def _unavailable(reason: str) -> Dict[str, Any]:
        return {
            "available": False,
            "snapshot_date": None,
            "score": None,
            "multiplier": 1.0,
            "themes": [],
            "reason": reason + "，沿用原仓位",
        }
