"""Collect and persist an external-market environment snapshot.

The initial provider is Yahoo Finance's chart endpoint. It is useful for
research and monitoring; replace it with a contracted provider for live trading.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import yaml

from utils.paths import data_path

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = {
    # Japan
    "japan_nikkei225": "^N225",
    "japan_topix": "^TOPX",
    "usd_jpy": "JPY=X",
    # South Korea
    "korea_kospi": "^KS11",
    "korea_kosdaq": "^KQ11",
    "usd_krw": "KRW=X",
    # United States
    "us_sp500": "^GSPC",
    "us_nasdaq": "^IXIC",
    "us_dow": "^DJI",
    "us_russell2000": "^RUT",
    "us_vix": "^VIX",
    # US core themes: ETFs are used as stable theme proxies.
    "us_semiconductor_etf": "SMH",
    "us_ai_etf": "AIQ",
    "us_software_etf": "IGV",
    "us_cloud_etf": "SKYY",
    "us_sp500_futures": "ES=F",
    "us_nasdaq_futures": "NQ=F",
    "us_dow_futures": "YM=F",
    "dollar_index": "DX-Y.NYB",
    "usd_cnh": "CNH=X",
    "gold": "GC=F",
    "oil": "CL=F",
    "us10y": "^TNX",
}

MARKET_GROUPS = {
    "japan": ["japan_nikkei225", "japan_topix", "usd_jpy"],
    "korea": ["korea_kospi", "korea_kosdaq", "usd_krw"],
    "united_states": [
        "us_sp500", "us_nasdaq", "us_dow", "us_russell2000", "us_vix",
        "us_semiconductor_etf", "us_ai_etf", "us_software_etf", "us_cloud_etf",
        "us_sp500_futures", "us_nasdaq_futures", "us_dow_futures",
    ],
    "global": ["dollar_index", "usd_cnh", "gold", "oil", "us10y"],
}

OPENING_SYMBOLS = {
    # Japan is observation-only.  Korea's large-cap technology proxy is
    # refreshed for the technology-specific A-share entry gate.
    "korea_kosdaq"
}
CORE_THEME_SYMBOLS = {
    "us_semiconductor_etf", "us_ai_etf", "us_software_etf", "us_cloud_etf"
}
REQUIRED_OPENING_SYMBOLS = {
    # The technology gate uses the completed U.S. technology close plus the
    # live Korean large-cap technology proxy. Japan is never a gate input.
    "us_nasdaq", "korea_kosdaq", *CORE_THEME_SYMBOLS,
}

# Yahoo Finance is unavailable from some domestic network routes.  Keep it as
# a fallback, but use reachable regional sources for the data that controls
# the opening gate.
TENCENT_SYMBOLS = {
    "us_sp500": "usINX",
    "us_nasdaq": "usIXIC",
    "us_dow": "usDJI",
    "us_vix": "usVIX",
    "us_semiconductor_etf": "usSMH",
    "us_ai_etf": "usAIQ",
    "us_software_etf": "usIGV",
    "us_cloud_etf": "usSKYY",
}
EASTMONEY_SYMBOLS = {
    "japan_nikkei225": ("100.N225", None),
    # Eastmoney does not expose TOPIX/KOSDAQ directly.  The substitutes are
    # recorded in every result so the opening decision remains auditable.
    "japan_topix": ("100.N225", "japan_nikkei225"),
    "korea_kospi": ("100.KS11", None),
    "korea_kosdaq": ("100.KOSPI200", "korea_kospi200"),
}
MARKET_PROXY_SOURCES = {
    "japan_topix": "japan_nikkei225",
    "korea_kosdaq": "korea_kospi",
}
EASTMONEY_ENDPOINTS = (
    # The delayed endpoint is materially more reachable on domestic routes.
    "https://push2delay.eastmoney.com/api/qt/stock/get",
    "https://push2.eastmoney.com/api/qt/stock/get",
)

# Initial thresholds. Tune them with historical tests before live use.
OPEN_RULES = {
    "min_us_nasdaq_change_pct": -1.0,
    "min_core_theme_breadth": 0.5,
    "min_japan_open_gap_pct": -0.8,
    "min_japan_change_pct": -1.0,
    "min_korea_open_gap_pct": -0.8,
    "min_korea_change_pct": -1.0,
    "max_vix_close": 28.0,
}


def load_external_environment_config() -> Dict[str, Any]:
    config_path = Path("config/external_environment.yaml")
    try:
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}
    except Exception:
        logger.exception("外围环境配置读取失败，使用默认配置")
    return {}


class ExternalEnvironmentSnapshotter:
    """Fetch daily external-market data and store reproducible snapshots."""

    def __init__(self, snapshot_dir: Optional[Path] = None,
                 enabled: Optional[bool] = None):
        self.snapshot_dir = snapshot_dir or data_path("external_environment")
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.snapshot_dir / "snapshots.sqlite3"
        config = load_external_environment_config()
        configured_enabled = bool(config.get("enabled", True))
        self.enabled = configured_enabled if enabled is None else bool(enabled)
        self.apply_to_realtime = bool(config.get("apply_to_realtime", True))
        self.fail_closed = bool(config.get("fail_closed", True))
        try:
            self.intraday_refresh_minutes = max(
                1, int(config.get("intraday_refresh_minutes", 5))
            )
        except (TypeError, ValueError):
            self.intraday_refresh_minutes = 5

    def is_active(self) -> bool:
        return self.enabled and self.apply_to_realtime

    @staticmethod
    def _fetch_yahoo_symbol(symbol: str, intraday: bool = False) -> Dict[str, Any]:
        last_error = None
        # Yahoo is only a final fallback.  A slow or unreachable route must
        # not hold up the opening gate after regional providers have replied.
        for attempt in range(1):
            try:
                params = {
                    "range": "1d" if intraday else "10d",
                    "interval": "5m" if intraday else "1d",
                    "includePrePost": "false",
                }
                response = requests.get(
                    "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol,
                    params=params,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=(1.5, 4),
                )
                response.raise_for_status()
                result = response.json()["chart"]["result"][0]
                timestamps = result.get("timestamp", [])
                quote = result["indicators"]["quote"][0]
                closes = quote.get("close", [])
                observations = []
                for index, (timestamp, close) in enumerate(zip(timestamps, closes)):
                    if close is None:
                        continue
                    observations.append({
                        "timestamp": timestamp,
                        "close": float(close),
                        "open": quote.get("open", [None] * len(closes))[index],
                    })
                if len(observations) < 2:
                    raise RuntimeError("历史收盘数据不足两条")

                previous = observations[-2]["close"]
                latest = observations[-1]["close"]
                latest_timestamp = observations[-1]["timestamp"]
                if intraday:
                    meta = result.get("meta", {})
                    previous_close = meta.get("chartPreviousClose")
                    first_open = next(
                        (item["open"] for item in observations if item["open"] is not None),
                        None,
                    )
                    if previous_close is None or first_open is None:
                        raise RuntimeError("盘中数据缺少前收或开盘价")
                    return {
                        "status": "ok",
                        "provider": "Yahoo Finance chart API",
                        "provider_symbol": symbol,
                        "session": "intraday",
                        "observed_at": datetime.fromtimestamp(
                            latest_timestamp
                        ).astimezone().isoformat(),
                        "previous_close": float(previous_close),
                        "open": float(first_open),
                        "close": latest,
                        "open_gap_pct": round(
                            (float(first_open) / float(previous_close) - 1) * 100, 4
                        ),
                        "change_pct": round(
                            (latest / float(previous_close) - 1) * 100, 4
                        ),
                    }
                return {
                    "status": "ok",
                    "provider": "Yahoo Finance chart API",
                    "provider_symbol": symbol,
                    "session": "daily",
                    "observed_at": datetime.fromtimestamp(latest_timestamp)
                    .astimezone().isoformat(),
                    "previous_observed_at": datetime.fromtimestamp(
                        observations[-2]["timestamp"]
                    ).astimezone().isoformat(),
                    "close": float(latest),
                    "change_pct": round((latest / previous - 1) * 100, 4),
                }
            except Exception as exc:  # provider failures are recorded per symbol
                last_error = exc

        return {
            "status": "error",
            "provider": "Yahoo Finance chart API",
            "provider_symbol": symbol,
            "error": str(last_error),
        }

    @staticmethod
    def _as_float(value: Any, field: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{field}无效") from exc
        if number <= 0:
            raise RuntimeError(f"{field}无效")
        return number

    @classmethod
    def _fetch_tencent_symbol(cls, name: str, intraday: bool) -> Optional[Dict[str, Any]]:
        provider_symbol = TENCENT_SYMBOLS.get(name)
        if not provider_symbol:
            return None
        provider = "Tencent Finance"
        try:
            response = requests.get(
                "https://qt.gtimg.cn/q=" + provider_symbol,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=(1.5, 4),
            )
            response.raise_for_status()
            payload = response.content.decode("gb18030", errors="replace")
            match = re.search(r'=\"([^\"]*)\"', payload)
            if not match:
                raise RuntimeError("行情响应格式无效")
            fields = match.group(1).split("~")
            if len(fields) < 33:
                raise RuntimeError("行情字段不足")
            close = cls._as_float(fields[3], "最新价")
            previous_close = cls._as_float(fields[4], "前收")
            open_price = cls._as_float(fields[5], "开盘价")
            observed_at = fields[30].strip() if len(fields) > 30 else ""
            change_pct = (close / previous_close - 1) * 100
            return {
                "status": "ok",
                "provider": provider,
                "provider_symbol": provider_symbol,
                "session": "intraday" if intraday else "daily",
                "observed_at": observed_at or datetime.now().astimezone().isoformat(),
                "previous_close": previous_close,
                "open": open_price,
                "close": close,
                "open_gap_pct": round((open_price / previous_close - 1) * 100, 4),
                "change_pct": round(change_pct, 4),
            }
        except Exception as exc:
            return {
                "status": "error",
                "provider": provider,
                "provider_symbol": provider_symbol,
                "error": str(exc),
            }

    @classmethod
    def _fetch_eastmoney_symbol(cls, name: str, intraday: bool) -> Optional[Dict[str, Any]]:
        mapping = EASTMONEY_SYMBOLS.get(name)
        if not mapping:
            return None
        provider_symbol, proxy_for = mapping
        provider = "Eastmoney global index"
        last_error = None
        for attempt in range(3):
            try:
                response = requests.get(
                    EASTMONEY_ENDPOINTS[attempt % len(EASTMONEY_ENDPOINTS)],
                    params={
                        "secid": provider_symbol,
                        "fields": "f43,f44,f45,f46,f57,f58,f60,f169,f170",
                    },
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=(1.5, 4),
                )
                response.raise_for_status()
                data = response.json().get("data") or {}
                close = cls._as_float(data.get("f43"), "最新价") / 100
                previous_close = cls._as_float(data.get("f60"), "前收") / 100
                open_price = cls._as_float(data.get("f46"), "开盘价") / 100
                result = {
                    "status": "ok",
                    "provider": provider,
                    "provider_symbol": provider_symbol,
                    "session": "intraday" if intraday else "daily",
                    "observed_at": datetime.now().astimezone().isoformat(),
                    "previous_close": previous_close,
                    "open": open_price,
                    "close": close,
                    "open_gap_pct": round((open_price / previous_close - 1) * 100, 4),
                    "change_pct": round((close / previous_close - 1) * 100, 4),
                }
                if proxy_for:
                    result["proxy_for"] = proxy_for
                return result
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5)
        return {
            "status": "error",
            "provider": provider,
            "provider_symbol": provider_symbol,
            "error": str(last_error),
        }

    @classmethod
    def _fetch_symbol(cls, name: str, symbol: str, intraday: bool = False) -> Dict[str, Any]:
        """Fetch an index from its regional primary source, then Yahoo."""
        errors = []
        for fetcher in (cls._fetch_tencent_symbol, cls._fetch_eastmoney_symbol):
            result = fetcher(name, intraday)
            if result is None:
                continue
            if result.get("status") == "ok":
                return result
            errors.append(f"{result.get('provider')}: {result.get('error')}")

        yahoo = cls._fetch_yahoo_symbol(symbol, intraday)
        if yahoo.get("status") == "ok":
            return yahoo
        errors.append(f"{yahoo.get('provider')}: {yahoo.get('error')}")
        return {
            "status": "error",
            "provider": "fallback chain",
            "provider_symbol": symbol,
            "error": " | ".join(errors),
        }

    @staticmethod
    def _apply_market_proxies(data: Dict[str, Dict[str, Any]]) -> None:
        """Use a same-market proxy only when the named index is unavailable."""
        for target, source in MARKET_PROXY_SOURCES.items():
            target_data = data.get(target, {})
            source_data = data.get(source, {})
            if target_data.get("status") == "ok" or source_data.get("status") != "ok":
                continue
            proxy = dict(source_data)
            proxy["provider"] = f"{source_data.get('provider', 'unknown')} market proxy"
            proxy["proxy_for"] = source
            proxy["proxy_reason"] = f"{target}直连失败，使用{source}替代"
            data[target] = proxy

    @staticmethod
    def _score(data: Dict[str, Dict[str, Any]]) -> int:
        # Positive score means a simpler risk-on reading. Tune this only after
        # a historical test; it is not a trading recommendation by itself.
        direction = {
            "us_sp500": 1,
            "us_nasdaq": 1,
            "us_vix": -1,
            "usd_cnh": -1,
        }
        score = 0
        for name, factor in direction.items():
            item = data.get(name, {})
            if item.get("status") != "ok":
                continue
            adjusted_change = item["change_pct"] * factor
            score += 1 if adjusted_change > 0 else -1 if adjusted_change < 0 else 0
        return score

    @staticmethod
    def _market_pair(data: Dict[str, Dict[str, Any]], names: list[str]) -> Dict[str, float]:
        items = [data.get(name, {}) for name in names]
        valid = [item for item in items if item.get("status") == "ok"]
        if len(valid) != len(names):
            return {"available": 0.0}
        return {
            "available": 1.0,
            "open_gap_pct": sum(item["open_gap_pct"] for item in valid) / len(valid),
            "change_pct": sum(item["change_pct"] for item in valid) / len(valid),
        }

    @staticmethod
    def _korea_technology_stabilization(
        item: Dict[str, Any], rules: Dict[str, float]
    ) -> Dict[str, Any]:
        """Assess the live Korean technology proxy, never its opening gap.

        A weak open alone cannot veto a later A-share technology entry.  When
        the proxy is still below its loss threshold, it must first reclaim its
        Korean-session open and stop falling versus the prior five-minute
        refresh before the A-share gate is released.
        """
        if item.get("status") != "ok":
            return {"stable": False, "reason": "韩国科技代理实时行情不可用"}
        try:
            change = float(item.get("change_pct"))
            current = float(item.get("close"))
            opening = float(item.get("open"))
            previous = float(item.get("previous_refresh_close") or 0.0)
        except (TypeError, ValueError):
            return {"stable": False, "reason": "韩国科技代理实时行情字段无效"}
        if change >= float(rules["min_korea_change_pct"]):
            return {
                "stable": True,
                "reason": f"韩国科技代理实时涨跌{change:.2f}%，未跌破风险线",
            }
        if current >= opening and previous > 0 and current >= previous:
            return {
                "stable": True,
                "reason": (
                    f"韩国科技代理已企稳：现价{current:.2f}不低于开盘价"
                    f"{opening:.2f}，且不低于上一轮{previous:.2f}"
                ),
            }
        return {
            "stable": False,
            "reason": (
                f"韩国科技代理仍走弱：实时涨跌{change:.2f}%，"
                f"现价{current:.2f}，开盘价{opening:.2f}"
            ),
        }

    @classmethod
    def _opening_decision(cls, data: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        rules = OPEN_RULES
        data_issues = []
        risk_reasons = []
        required = sorted(REQUIRED_OPENING_SYMBOLS)
        missing = [
            name for name in required
            if data.get(name, {}).get("status") != "ok"
        ]
        if missing:
            data_issues.append("关键数据缺失: " + ", ".join(missing))

        nasdaq = data.get("us_nasdaq", {})
        if nasdaq.get("status") == "ok" and nasdaq["change_pct"] < rules["min_us_nasdaq_change_pct"]:
            risk_reasons.append(f"纳指跌幅超过阈值: {nasdaq['change_pct']:.2f}%")

        themes = [data.get(name, {}) for name in CORE_THEME_SYMBOLS]
        valid_themes = [item for item in themes if item.get("status") == "ok"]
        theme_breadth = (
            sum(item["change_pct"] > 0 for item in valid_themes) / len(valid_themes)
            if valid_themes else 0.0
        )
        if len(valid_themes) == len(themes):
            if theme_breadth < rules["min_core_theme_breadth"]:
                risk_reasons.append(f"核心题材上涨比例不足: {theme_breadth:.0%}")
        else:
            data_issues.append(
                f"核心题材数据不足: {len(valid_themes)}/{len(themes)}"
            )

        # Japan is retained in the snapshot for observation only. It has no
        # effect on the A-share technology entry decision.
        japan = cls._market_pair(data, ["japan_nikkei225", "japan_topix"])
        korea = cls._market_pair(data, ["korea_kospi", "korea_kosdaq"])
        korea_technology = data.get("korea_kosdaq", {})
        korea_stabilization = cls._korea_technology_stabilization(
            korea_technology, rules
        )
        if not korea_stabilization["stable"]:
            risk_reasons.append(korea_stabilization["reason"])

        vix = data.get("us_vix", {})
        if vix.get("status") == "ok" and vix["close"] > rules["max_vix_close"]:
            risk_reasons.append(f"VIX过高: {vix['close']:.2f}")

        return {
            "allow_open": not data_issues and not risk_reasons,
            "reasons": data_issues + risk_reasons or [
                "美韩科技环境满足开仓条件"
            ],
            "data_issues": data_issues,
            "risk_reasons": risk_reasons,
            "gate_basis": "previous_us_close_and_korea_technology_proxy",
            "theme_breadth": round(theme_breadth, 4),
            "japan": japan,
            "korea": korea,
            "korea_technology_stabilization": korea_stabilization,
            "rules": rules,
        }

    def _apply_data_quality_policy(
        self, decision: Dict[str, Any], failed_required: list[str]
    ) -> Dict[str, Any]:
        """Allow a configured data-quality downgrade without masking market risk."""
        if not failed_required or self.fail_closed:
            return decision
        if decision.get("risk_reasons"):
            return decision
        result = dict(decision)
        result["allow_open"] = True
        result["reasons"] = [
            "外围关键数据不完整，按 fail_closed=false 降级放行"
        ] + list(decision.get("data_issues") or [])
        result["degraded_data_quality"] = True
        return result

    def _fetch_symbols(self, symbols: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
        data: Dict[str, Dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(10, max(1, len(symbols)))) as executor:
            futures = {
                executor.submit(
                    self._fetch_symbol, name, symbol, name in OPENING_SYMBOLS
                ): name
                for name, symbol in symbols.items()
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    data[name] = future.result()
                except Exception as exc:
                    data[name] = {"status": "error", "error": str(exc)}
        return data

    def collect(self, symbols: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if not self.is_active():
            return {
                "created_at": datetime.now().astimezone().isoformat(),
                "source": "disabled",
                "groups": MARKET_GROUPS,
                "status": "disabled",
                "failed_items": [],
                "score": 0,
                "opening_decision": {
                    "allow_open": True,
                    "reasons": ["外围环境调用已关闭，沿用原有策略"],
                    "theme_breadth": None,
                    "japan": {"available": 0.0},
                    "korea": {"available": 0.0},
                    "rules": OPEN_RULES,
                },
                "data": {},
            }
        symbols = symbols or DEFAULT_SYMBOLS
        # The snapshot contains optional indicators as well as the opening
        # gate inputs. More workers prevent unavailable optional providers
        # from serially delaying an otherwise complete critical snapshot.
        data = self._fetch_symbols(symbols)
        self._apply_market_proxies(data)
        failed = [name for name, item in data.items() if item["status"] != "ok"]
        failed_required = [name for name in REQUIRED_OPENING_SYMBOLS if name in failed]
        opening_decision = self._apply_data_quality_policy(
            self._opening_decision(data), failed_required
        )
        return {
            "created_at": datetime.now().astimezone().isoformat(),
            "source": "Tencent Finance / Eastmoney global index / Yahoo Finance fallback",
            "groups": MARKET_GROUPS,
            "status": "failed" if failed_required else "degraded" if failed else "ok",
            "failed_items": failed,
            "score": self._score(data),
            "opening_decision": opening_decision,
            "data": data,
        }

    def refresh_intraday_opening_markets(
        self, previous_snapshot: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Refresh only the markets that keep moving during the A-share session.

        U.S. overnight inputs remain fixed until the next trading day. This
        avoids one external request per candidate while keeping the Japan and
        Korea gate current for new intraday entries.
        """
        if not self.is_active():
            return previous_snapshot
        if not isinstance(previous_snapshot.get("data"), dict):
            return self.collect()

        snapshot = deepcopy(previous_snapshot)
        data = snapshot["data"]
        refresh_symbols = {
            name: DEFAULT_SYMBOLS[name]
            for name in OPENING_SYMBOLS
        }
        refreshed = self._fetch_symbols(refresh_symbols)
        for name, item in refreshed.items():
            previous = data.get(name, {})
            if (
                item.get("status") == "ok"
                and previous.get("status") == "ok"
                and previous.get("close") is not None
            ):
                item["previous_refresh_close"] = previous["close"]
        data.update(refreshed)
        self._apply_market_proxies(data)
        failed = [name for name, item in data.items() if item.get("status") != "ok"]
        failed_required = [name for name in REQUIRED_OPENING_SYMBOLS if name in failed]
        opening_decision = self._apply_data_quality_policy(
            self._opening_decision(data), failed_required
        )

        snapshot.update({
            "created_at": datetime.now().astimezone().isoformat(),
            "source": "Tencent Finance / Eastmoney global index / Yahoo Finance fallback",
            "groups": MARKET_GROUPS,
            "status": "failed" if failed_required else "degraded" if failed else "ok",
            "failed_items": failed,
            "score": self._score(data),
            "opening_decision": opening_decision,
            "refreshed_symbols": sorted(refresh_symbols),
        })
        snapshot.pop("saved_to", None)
        return snapshot

    def save(self, snapshot: Dict[str, Any]) -> Path:
        created_at = datetime.fromisoformat(snapshot["created_at"])
        filename = created_at.strftime("%Y-%m-%d_%H%M%S.json")
        json_path = self.snapshot_dir / filename
        temp_path = json_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_path.replace(json_path)

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    score INTEGER,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO snapshots (created_at, source, score, status, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snapshot["created_at"],
                    snapshot["source"],
                    snapshot["score"],
                    snapshot["status"],
                    json.dumps(snapshot, ensure_ascii=False),
                ),
            )
        return json_path

    def collect_and_save(self) -> Dict[str, Any]:
        snapshot = self.collect()
        path = self.save(snapshot)
        snapshot["saved_to"] = str(path)
        return snapshot

    def latest(self) -> Optional[Dict[str, Any]]:
        """Return the most recently persisted snapshot without making a request."""
        if not self.db_path.exists():
            return None
        try:
            with sqlite3.connect(self.db_path) as connection:
                row = connection.execute(
                    "SELECT payload FROM snapshots ORDER BY id DESC LIMIT 1"
                ).fetchone()
            return json.loads(row[0]) if row else None
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            logger.exception("读取最新外围环境快照失败")
            return None


def main() -> int:
    parser = argparse.ArgumentParser(description="采集并保存外围环境快照")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        snapshot = ExternalEnvironmentSnapshotter().collect_and_save()
    except Exception:
        logger.exception("外围环境快照采集失败")
        return 1

    print("外围环境快照")
    print(f"状态: {snapshot['status']} | 评分: {snapshot['score']}")
    decision = snapshot["opening_decision"]
    print(
        f"A股开仓: {'允许' if decision['allow_open'] else '禁止'} | "
        f"原因: {'；'.join(decision['reasons'])}"
    )
    for group, names in snapshot["groups"].items():
        print(f"\n[{group}]")
        for name in names:
            item = snapshot["data"].get(name, {})
            if item.get("status") == "ok":
                print(
                    f"{name:22} close={item['close']:>12.4f} "
                    f"change={item['change_pct']:>8.4f}%"
                )
            else:
                print(f"{name:22} ERROR {item.get('error', 'unknown')}")

    print("\n完整 JSON:")
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0 if snapshot["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
