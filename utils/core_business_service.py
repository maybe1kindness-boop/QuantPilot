"""Build a source-backed core-business profile for every A-share company.

This layer deliberately does not infer a company's business from industry or
concept-board membership.  It stores the F10 source text and revenue segments,
then emits a theme only when a curated business rule has textual evidence.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

import requests

from utils.global_db import get_global_db

logger = logging.getLogger(__name__)


class CoreBusinessService:
    """Persist verified core-business themes from EastMoney F10 disclosures."""

    COMPANY_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
    BUSINESS_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax"
    MAX_WORKERS = 8
    TIMEOUT_SECONDS = 12

    # `aliases` are market boards used only after the business has been verified.
    # They never participate in business extraction or validation.
    THEME_RULES = (
        ("锂电铜箔", ("锂电铜箔", "电解铜箔"), ("锂电池概念", "复合集流体", "新材料")),
        ("铜箔", ("铜箔",), ("复合集流体", "新材料")),
        ("半导体", ("半导体", "芯片", "集成电路", "晶圆", "封装测试", "功率器件"), ("半导体概念", "芯片概念", "集成电路")),
        ("PCB", ("印制电路板", "印制线路板", "pcb", "覆铜板"), ("PCB",)),
        ("被动元件", ("电容", "电阻", "电感", "被动元件"), ("被动元件",)),
        ("光模块", ("光模块", "光器件", "cpo", "光通信"), ("CPO概念", "光模块")),
        ("通信设备", ("通信设备", "通信网络", "基站", "射频"), ("5G", "通信设备")),
        ("软件", ("软件开发", "软件产品", "软件服务", "操作系统"), ("软件开发", "国产软件")),
        ("人工智能", ("人工智能", "ai大模型", "大模型", "机器学习"), ("人工智能", "AIGC")),
        ("算力", ("算力", "数据中心", "服务器", "云计算"), ("算力概念", "数据中心")),
        ("机器人", ("机器人",), ("机器人概念",)),
        ("新能源汽车", ("新能源汽车", "电动汽车", "电驱", "汽车电子"), ("新能源汽车",)),
        ("锂电池", ("锂电池", "锂离子电池", "动力电池"), ("锂电池概念", "固态电池")),
        ("电池材料", ("正极材料", "负极材料", "电解液", "隔膜", "电池材料"), ("锂电池概念", "固态电池")),
        ("固态电池", ("固态电池", "固态电解质"), ("固态电池",)),
        ("光伏", ("光伏", "太阳能电池", "硅片", "组件", "逆变器"), ("光伏概念",)),
        ("风电", ("风电", "风力发电", "风电设备", "风机"), ("风电",)),
        ("储能", ("储能", "储能系统", "储能电池"), ("储能",)),
        ("核电", ("核电", "核能", "核岛"), ("核电",)),
        ("稀土", ("稀土", "稀有金属"), ("稀土永磁", "小金属概念")),
        ("黄金", ("黄金", "金矿"), ("黄金概念",)),
        ("有色金属", ("铜矿", "铝", "锌", "钴", "镍", "铅"), ("有色金属",)),
        ("新材料", ("新材料", "高分子材料", "复合材料", "功能材料"), ("新材料",)),
        ("创新药", ("创新药", "化学药", "生物药", "抗体药", "医药研发"), ("创新药",)),
        ("医疗器械", ("医疗器械", "体外诊断", "医学影像", "医疗设备"), ("医疗器械概念",)),
        ("中药", ("中药", "中成药", "中药饮片"), ("中药概念",)),
        ("农业", ("种子", "农药", "化肥", "饲料", "农产品"), ("农业种植",)),
        ("食品饮料", ("食品", "饮料", "白酒", "乳制品", "调味品"), ("食品饮料",)),
        ("家电", ("家用电器", "空调", "冰箱", "洗衣机", "厨电"), ("家电行业",)),
        ("房地产", ("房地产开发", "物业管理", "房地产经营"), ("房地产",)),
        ("证券", ("证券经纪", "证券投资", "证券业务"), ("证券",)),
        ("银行", ("商业银行", "银行业务", "贷款业务"), ("银行",)),
        ("保险", ("保险业务", "人寿保险", "财产保险"), ("保险",)),
        ("航运", ("航运", "海运", "船舶运输"), ("航运港口",)),
        ("航空", ("航空运输", "航空业务", "机场运营"), ("航空机场",)),
        ("军工", ("军工", "国防", "航空航天", "导弹", "舰船"), ("军工",)),
    )

    def __init__(self, db_manager=None, max_workers: int = MAX_WORKERS):
        self.db = db_manager or get_global_db()
        self.max_workers = max(1, min(int(max_workers), self.MAX_WORKERS))
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.db.transaction():
            self.db.execute(
                """
            CREATE TABLE IF NOT EXISTS stock_core_business (
                stock_code TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                report_date TEXT,
                company_profile TEXT,
                business_scope TEXT,
                revenue_segments_json TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                validation_reason TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            )
                """
            )
            self.db.execute(
                """
            CREATE TABLE IF NOT EXISTS stock_core_business_theme (
                stock_code TEXT NOT NULL,
                theme_name TEXT NOT NULL,
                confidence REAL NOT NULL,
                evidence_json TEXT NOT NULL,
                market_aliases_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (stock_code, theme_name)
            )
                """
            )
            self.db.execute(
                """
            CREATE TABLE IF NOT EXISTS core_business_refresh_status (
                refresh_date TEXT PRIMARY KEY,
                stock_count INTEGER NOT NULL,
                success_count INTEGER NOT NULL,
                verified_count INTEGER NOT NULL,
                unmapped_count INTEGER NOT NULL,
                failed_count INTEGER NOT NULL,
                completed_at TEXT NOT NULL
            )
                """
            )

    @staticmethod
    def _market_code(code: str) -> str:
        return f"SH{code}" if code.startswith(("5", "6", "9")) else f"SZ{code}"

    @staticmethod
    def _normalize_code(value: Any) -> str:
        text = str(value or "").strip().upper()
        text = text.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
        text = text.removeprefix("SH").removeprefix("SZ").removeprefix("BJ")
        return text.zfill(6) if text.isdigit() else ""

    def _stock_codes(self) -> list[str]:
        rows = self.db.query("SELECT code FROM stock_basic")
        return sorted({self._normalize_code(row.get("code")) for row in rows if self._normalize_code(row.get("code"))})

    def _fetch_profile(self, code: str) -> Dict[str, Any]:
        market_code = self._market_code(code)
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://emweb.securities.eastmoney.com/PC_HSF10/",
        }
        company = requests.get(
            self.COMPANY_URL, params={"code": market_code}, headers=headers,
            timeout=self.TIMEOUT_SECONDS,
        )
        company.raise_for_status()
        business = requests.get(
            self.BUSINESS_URL, params={"code": market_code}, headers=headers,
            timeout=self.TIMEOUT_SECONDS,
        )
        business.raise_for_status()
        return {"company": company.json(), "business": business.json()}

    @staticmethod
    def _latest_segments(rows: Iterable[Dict[str, Any]]) -> tuple[Optional[str], list[Dict[str, Any]]]:
        values = [row for row in rows if isinstance(row, dict) and row.get("ITEM_NAME")]
        if not values:
            return None, []
        latest = max(str(row.get("REPORT_DATE") or "")[:10] for row in values)
        latest_rows = [row for row in values if str(row.get("REPORT_DATE") or "")[:10] == latest]
        segments = []
        for row in sorted(latest_rows, key=lambda item: float(item.get("MBI_RATIO") or 0), reverse=True):
            name = str(row.get("ITEM_NAME") or "").strip()
            ratio = float(row.get("MBI_RATIO") or 0)
            if not name or name.startswith("其他"):
                continue
            segments.append({"name": name, "revenue_ratio": round(ratio, 4)})
        return latest or None, segments[:8]

    @staticmethod
    def _match_terms(text: str, terms: Iterable[str]) -> list[str]:
        lowered = text.lower()
        return [term for term in terms if term.lower() in lowered]

    def _extract(self, code: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        company_rows = (payload.get("company") or {}).get("jbzl") or []
        company = company_rows[0] if company_rows else {}
        business = payload.get("business") or {}
        profile = str(company.get("ORG_PROFILE") or "").strip()
        scope = str(company.get("BUSINESS_SCOPE") or "").strip()
        if not scope:
            scope_rows = business.get("zyfw") or []
            scope = str((scope_rows[0] if scope_rows else {}).get("BUSINESS_SCOPE") or "").strip()
        report_date, segments = self._latest_segments(business.get("zygcfx") or [])
        segment_text = "；".join(item["name"] for item in segments)
        themes = []
        for name, terms, aliases in self.THEME_RULES:
            evidence = []
            for source, text in (("company_profile", profile), ("business_scope", scope), ("revenue_segment", segment_text)):
                matched = self._match_terms(text, terms)
                if matched:
                    evidence.append({"source": source, "terms": matched})
            if not evidence:
                continue
            # Company profile is the strongest description of actual products;
            # a revenue segment independently confirms materiality.
            sources = {item["source"] for item in evidence}
            # Registration scopes often enumerate permitted side businesses.
            # A core theme must be supported by the company profile or an
            # actual revenue segment, never by scope text alone.
            if not ({"company_profile", "revenue_segment"} & sources):
                continue
            confidence = 0.55 + 0.25 * ("company_profile" in sources) + 0.15 * ("revenue_segment" in sources) + 0.05 * ("business_scope" in sources)
            themes.append({
                "name": name,
                "confidence": round(min(1.0, confidence), 2),
                "evidence": evidence,
                "market_aliases": list(aliases),
            })
        themes.sort(key=lambda item: (-item["confidence"], item["name"]))
        source_complete = bool(profile) and bool(segments)
        if themes and source_complete:
            status, reason = "verified", "F10公司简介与主营收入构成已校验"
        elif source_complete:
            status, reason = "source_complete_unmapped", "F10主营资料完整，但尚无可验证的题材规则"
        elif profile or scope or segments:
            status, reason = "source_incomplete", "F10主营资料不完整，暂不输出核心题材"
        else:
            status, reason = "failed", "F10未返回可用主营资料"
        return {
            "stock_code": code,
            "report_date": report_date,
            "company_profile": profile,
            "business_scope": scope,
            "revenue_segments": segments,
            "themes": themes,
            "validation_status": status,
            "validation_reason": reason,
            "raw_payload": payload,
        }

    def _persist(self, item: Dict[str, Any]) -> None:
        fetched_at = datetime.now().isoformat(timespec="seconds")
        with self.db.transaction():
            self.db.execute(
                """
                INSERT INTO stock_core_business
                (stock_code, source, report_date, company_profile, business_scope,
                 revenue_segments_json, validation_status, validation_reason,
                 raw_payload_json, fetched_at)
                VALUES (?, 'eastmoney_f10', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stock_code) DO UPDATE SET
                  source=excluded.source, report_date=excluded.report_date,
                  company_profile=excluded.company_profile, business_scope=excluded.business_scope,
                  revenue_segments_json=excluded.revenue_segments_json,
                  validation_status=excluded.validation_status,
                  validation_reason=excluded.validation_reason,
                  raw_payload_json=excluded.raw_payload_json, fetched_at=excluded.fetched_at
                """,
                (
                    item["stock_code"], item["report_date"], item["company_profile"],
                    item["business_scope"], json.dumps(item["revenue_segments"], ensure_ascii=False),
                    item["validation_status"], item["validation_reason"],
                    json.dumps(item["raw_payload"], ensure_ascii=False), fetched_at,
                ),
            )
            self.db.execute("DELETE FROM stock_core_business_theme WHERE stock_code = ?", (item["stock_code"],))
            for theme in item["themes"]:
                self.db.execute(
                    """INSERT INTO stock_core_business_theme
                    (stock_code, theme_name, confidence, evidence_json, market_aliases_json, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        item["stock_code"], theme["name"], theme["confidence"],
                        json.dumps(theme["evidence"], ensure_ascii=False),
                        json.dumps(theme["market_aliases"], ensure_ascii=False), fetched_at,
                    ),
                )

    def refresh(self, codes: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        selected = list(dict.fromkeys(self._normalize_code(code) for code in (codes or self._stock_codes())))
        selected = [code for code in selected if code]
        success_count = 0
        verified_count = 0
        unmapped_count = 0
        failures: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(selected)))) as pool:
            futures = {pool.submit(self._fetch_profile, code): code for code in selected}
            for future in as_completed(futures):
                # A Future retains its HTTP result. Remove it immediately so
                # a full-market refresh does not accumulate every F10 payload.
                code = futures.pop(future)
                try:
                    item = self._extract(code, future.result())
                    self._persist(item)
                    success_count += 1
                    verified_count += item["validation_status"] == "verified"
                    unmapped_count += item["validation_status"] == "source_complete_unmapped"
                except Exception as exc:
                    failures[code] = str(exc)[:200]
        refresh_date = datetime.now().date().isoformat()
        with self.db.transaction():
            self.db.execute(
                """INSERT INTO core_business_refresh_status
            (refresh_date, stock_count, success_count, verified_count, unmapped_count, failed_count, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(refresh_date) DO UPDATE SET
              stock_count=excluded.stock_count, success_count=excluded.success_count,
              verified_count=excluded.verified_count, unmapped_count=excluded.unmapped_count,
              failed_count=excluded.failed_count, completed_at=excluded.completed_at""",
                (refresh_date, len(selected), success_count, verified_count, unmapped_count, len(failures), datetime.now().isoformat(timespec="seconds")),
            )
        return {"status": "ok" if not failures else "partial", "stock_count": len(selected), "success_count": success_count, "verified_count": verified_count, "unmapped_count": unmapped_count, "failed_count": len(failures), "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新全市场主营业务与核心题材资料")
    parser.add_argument("--codes", nargs="*", default=None)
    parser.add_argument("--workers", type=int, default=CoreBusinessService.MAX_WORKERS)
    args = parser.parse_args()
    report = CoreBusinessService(max_workers=args.workers).refresh(args.codes)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
