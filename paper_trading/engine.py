"""QuantPilot 公开版·纯筛选引擎(由发布脚本自动提取生成,请勿手改)。

仅包含自定义选股策略子系统: 策略保存/列表/删除、用户代码筛选(screen_with_user_code)、
自然语言规则筛选与强度评分、历史K线按需装载。不包含模拟交易、策略腿、候选池、
择时工厂或任何实盘/演练链路; 筛选 API 不创建订单、不改动任何账户。
"""
from __future__ import annotations

import csv
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd
import yaml

from utils.global_db import get_global_db

from .dao import PaperTradingDAO
from .trade_calendar import TradeCalendar

logger = logging.getLogger(__name__)

# 原引擎中 Quote 来自 broker 模块(未随公开版发布); 此处仅作类型占位
Quote = Dict[str, Any]


class _ScreenPollerStub:
    """公开版不附带盘中行情轮询; 接口保持兼容, 全部为空操作。"""

    def __init__(self, engine):
        self._engine = engine

    def start_background_refresh(self, interval_sec: float = 20.0):
        pass

    def stop_background_refresh(self):
        pass


class PaperTradingEngine:
    """公开版筛选引擎: 只实现选股与自定义策略持久化所需的最小接线。"""

    def __init__(self, config_file: str = "config/config.yaml",
                 db_path: Optional[str] = None, timing_name: Optional[str] = None,
                 start_quote_refresh: bool = False):
        self.config_file = Path(config_file)
        self.khunter_config: Dict[str, Any] = {}
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                self.khunter_config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning(f"配置文件不存在: {config_file}，使用默认值")
        self.config = self.khunter_config  # 筛选路径只用 getattr(...) 带默认值读取
        self.dao = PaperTradingDAO(db_path or "runtime/data/paper_trading.db")
        self._db_manager = None
        self.calendar = TradeCalendar()
        self._screen_poller = _ScreenPollerStub(self)
        self._historical_import_lock = threading.Lock()
        self._historical_import_running = False

    def _get_screen_poller(self):
        """公开版: 无盘中行情轮询模块, 返回兼容桩。"""
        return self._screen_poller

    @property
    def db_manager(self):
        if self._db_manager is None:
            from utils.global_db import get_global_db
            self._db_manager = get_global_db()
        return self._db_manager

    def screen_with_user_code(
        self, source: str, max_stocks: Optional[int] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
        force_refresh: bool = False,
        time_range: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Run a user-provided local matcher against the cached A-share data.

        The source must define ``match_stock(df, code, name)`` and return a
        boolean or a dict containing ``matched`` (or ``match``) and an optional
        ``reason``. This is deliberately a screening-only API; it does not
        create orders or mutate the paper account.
        """
        if not isinstance(source, str) or not source.strip():
            raise ValueError("筛选代码不能为空")
        if len(source) > 100_000:
            raise ValueError("筛选代码超过 100KB 限制")

        history_window = self._normalize_screen_time_range(time_range)
        natural_rules = self._parse_natural_rules(source)
        if natural_rules is not None:
            return self._screen_natural_rules(
                natural_rules, max_stocks, progress_callback, force_refresh,
                history_window,
            )

        namespace = {
            "pd": pd,
            "datetime": datetime,
            "__name__": "khunter_user_screen",
        }
        try:
            exec(compile(source, "<desktop-screen-code>", "exec"), namespace, namespace)
        except Exception as exc:
            raise ValueError(f"代码编译或加载失败: {exc}") from exc

        matcher = namespace.get("match_stock") or namespace.get("select_stock")
        if not callable(matcher):
            raise ValueError("代码必须定义 match_stock(df, code, name) 函数")

        codes = self.db_manager.list_all_stocks()
        if max_stocks and max_stocks > 0:
            codes = codes[:max_stocks]
        as_of = self._screen_as_of_date(history_window)
        if as_of is None:
            as_of = pd.Timestamp(self._screen_required_completed_date())
        import_stats = (
            self._ensure_historical_screen_data(codes, as_of, required_bars=2)
            if as_of is not None else {}
        )
        names = (
            self.db_manager.get_all_stock_names()
            if hasattr(self.db_manager, "get_all_stock_names") else {}
        )
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        for code in codes:
            name = names.get(code, "")
            try:
                raw_df = self.db_manager.read_stock(code)
                if raw_df is None or raw_df.empty:
                    continue
                df = self._filter_screen_history(raw_df, history_window)
                if df.empty:
                    continue
                raw = matcher(df.copy(), code, name)
                matched = False
                reason = "命中自定义条件"
                extra: Dict[str, Any] = {}
                if isinstance(raw, dict):
                    matched = bool(raw.get("matched", raw.get("match", raw.get("selected", False))))
                    reason = str(raw.get("reason", reason))
                    extra = {k: v for k, v in raw.items() if k not in {"matched", "match", "selected", "reason"}}
                else:
                    matched = bool(raw)
                if matched:
                    snapshot = self._get_screen_snapshot(code, raw_df, history_window)
                    if snapshot is None:
                        continue
                    latest_price = snapshot["price"]
                    previous_close = snapshot["previous_close"]
                    change = ((latest_price - previous_close) / previous_close * 100) if previous_close else 0.0
                    row = {
                        "code": code,
                        "name": name,
                        "price": latest_price,
                        "change": round(change, 2),
                        "data_date": snapshot["data_date"],
                        "data_source": snapshot["data_source"],
                        "reason": reason,
                    }
                    row.update(extra)
                    results.append(row)
            except Exception as exc:
                errors.append({"code": str(code), "error": str(exc)})

        return {
            "results": results,
            "processed": len(codes),
            "errors": errors,
            "import_stats": import_stats,
            "data_date": (
                self._screen_as_of_date(history_window).strftime("%Y-%m-%d")
                if self._screen_as_of_date(history_window) is not None else None
            ),
            "data_source": "stock_kline",
        }

    def _parse_natural_rules(self, source: str) -> Optional[Dict[str, Any]]:
        """Parse the common Chinese rule syntax used by the desktop editor."""
        if "def " in source or "import " in source or "return " in source:
            return None
        text = source.strip().replace("，", ",").replace("；", ",").replace("、", ",")
        parts = [part.strip() for part in re.split(r"[,\n]+", text) if part.strip()]
        if not parts:
            return None
        spec: Dict[str, Any] = {
            "exclude_st": False,
            "exclude_bj": False,
            "exclude_star": False,
            "price_max": None,
            "amount_min": None,
            "market_cap_max": None,
            "turnover_min": None,
            "turnover_max": None,
            "limit_days": None,
            "limit_count": None,
            "limit_at_least": False,
            "limit_count_min_inclusive": None,
            "limit_count_min_exclusive": None,
            "limit_count_max_exclusive": None,
            "close_above_ma_days": None,
            "ma_slope_positive_days": None,
            "ma_deviation_days": None,
            "ma_deviation_min": None,
            "ma_deviation_max": None,
            "volume_ratio_min": None,
            "volume_ratio_lookback": 5,
            "volume_ratio_on_previous_day": False,
            "volume_shrink": False,
            "rank_by_strength": False,
        }
        unsupported = []
        for part in parts:
            compact = part.replace(" ", "")
            if compact.startswith("非ST") or "排除ST" in compact:
                spec["exclude_st"] = True
                continue
            if compact.startswith("非北交") or "排除北交" in compact:
                spec["exclude_bj"] = True
                continue
            if compact.startswith("非科创") or "排除科创" in compact:
                spec["exclude_star"] = True
                continue
            match = re.search(r"股价(?:<|小于|低于)([0-9]+(?:\.[0-9]+)?)", compact)
            if match:
                spec["price_max"] = float(match.group(1))
                continue
            match = re.search(r"市值(?:<|小于|低于)([0-9]+(?:\.[0-9]+)?)(万亿|亿|万)?", compact)
            if match:
                spec["market_cap_max"] = self._parse_amount_value(match.group(1), match.group(2))
                continue
            match = re.search(r"(?:今日|当日|当天|该日)?成交额(?:>|大于|超过)([0-9]+(?:\.[0-9]+)?)(万亿|亿|万)?", compact)
            if match:
                spec["amount_min"] = self._parse_amount_value(match.group(1), match.group(2))
                continue
            match = re.search(
                r"换手率(?:>|大于|超过)([0-9]+(?:\.[0-9]+)?)%?"
                r"(?:且|并且)?(?:<|＜|小于|低于)([0-9]+(?:\.[0-9]+)?)%?",
                compact,
            )
            if match:
                spec["turnover_min"] = float(match.group(1))
                spec["turnover_max"] = float(match.group(2))
                continue
            match = re.search(
                r"(?:今日|昨日|当日|当天|前一日)?成交量(?:是|为)?前([0-9]+)(?:个交易日|交易日|日)成交量的"
                r"(?:(\d+(?:\.\d+)?)倍量?|倍量)?",
                compact,
            )
            if match:
                spec["volume_ratio_lookback"] = int(match.group(1))
                spec["volume_ratio_min"] = float(match.group(2) or 2.0)
                spec["volume_ratio_on_previous_day"] = True
                continue
            if compact in {"今日缩量", "当日缩量", "当天缩量", "缩量"}:
                spec["volume_shrink"] = True
                continue
            if compact in {"强度排序", "综合强度排序", "按强度排序"}:
                spec["rank_by_strength"] = True
                continue
            match = re.search(
                r"近([0-9]+)(月|个月|个交易日|日)(?:的)?涨停(?:次数)?(?:需要)?"
                r"(?:>=|≥|大于等于|不少于)([0-9]+)次(?:且|并且)?(?:<|＜|小于|低于)([0-9]+)次",
                compact,
            )
            if match:
                period = int(match.group(1))
                unit = match.group(2)
                spec["limit_days"] = period * 20 if "月" in unit else period
                spec["limit_count_min_inclusive"] = int(match.group(3))
                spec["limit_count_max_exclusive"] = int(match.group(4))
                continue
            match = re.search(
                r"近([0-9]+)(月|个月|个交易日|日)涨停(?:次数)?(?:>|大于|超过)([0-9]+)次"
                r"(?:且|并且)?(?:<|小于|低于)([0-9]+)次",
                compact,
            )
            if match:
                period = int(match.group(1))
                unit = match.group(2)
                spec["limit_days"] = period * 20 if "月" in unit else period
                spec["limit_count_min_exclusive"] = int(match.group(3))
                spec["limit_count_max_exclusive"] = int(match.group(4))
                continue
            match = re.search(r"近([0-9]+)(月|个月|个交易日|日)(?:(至少))?([0-9]+)次涨停", compact)
            if match:
                period = int(match.group(1))
                unit = match.group(2)
                # 同花顺的“近1月”按约20个交易日计算。
                spec["limit_days"] = period * 20 if "月" in unit else period
                spec["limit_at_least"] = bool(match.group(3))
                spec["limit_count"] = int(match.group(4))
                continue
            match = re.search(
                r"(?:今日|当日|当天)?收盘价(?:要)?(?:站上|高于|大于)([0-9]+)日线",
                compact,
            )
            if match:
                spec["close_above_ma_days"] = int(match.group(1))
                continue
            match = re.search(r"([0-9]+)日线向上", compact)
            if match:
                spec["ma_slope_positive_days"] = int(match.group(1))
                continue
            match = re.search(
                r"([0-9]+)日线乖离(?:>|＞|大于|超过)([0-9]+(?:\.[0-9]+)?)%?"
                r"(?:且|并且)?(?:<|＜|小于|低于)([0-9]+(?:\.[0-9]+)?)%?",
                compact,
            )
            if match:
                spec["ma_deviation_days"] = int(match.group(1))
                spec["ma_deviation_min"] = float(match.group(2))
                spec["ma_deviation_max"] = float(match.group(3))
                continue
            unsupported.append(part)
        if unsupported:
            raise ValueError("无法识别的中文条件：" + "、".join(unsupported))
        return spec

    @staticmethod
    def _natural_strength_score(
        history_df: pd.DataFrame, limit_flags: List[bool],
        turnover: Optional[float],
    ) -> tuple[float, str]:
        """Score matched stocks for ordering only; no score is a hard screen."""
        frame = history_df.copy()
        for column in ("open", "high", "low", "close", "volume"):
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["close"]).reset_index(drop=True)
        closes = frame["close"]
        if frame.empty:
            return 0.0, "强度评分数据不足"

        score = 0.0
        details = []
        recent_limit_up = next((index for index, hit in enumerate(limit_flags) if hit), None)
        if recent_limit_up is not None:
            limit_score = max(0.0, 22.0 - recent_limit_up * 1.1)
            score += limit_score
            details.append(f"最近涨停{recent_limit_up}日")

            consecutive_limit_ups = 0
            for hit in limit_flags[recent_limit_up:]:
                if not hit:
                    break
                consecutive_limit_ups += 1
            if consecutive_limit_ups == 2:
                score += 10.0
                details.append("连板2")
            elif consecutive_limit_ups == 3:
                score += 6.0
                details.append("连板3")
            elif consecutive_limit_ups >= 4:
                score -= 3.0
                details.append(f"连板{consecutive_limit_ups}过热")

            # A recent limit-up that holds its gains for several sessions is
            # healthier than one that immediately loses the breakout level.
            if 2 <= recent_limit_up <= 5 and recent_limit_up < len(closes):
                limit_close = float(closes.iloc[recent_limit_up])
                post_limit_closes = closes.iloc[:recent_limit_up].dropna()
                if limit_close > 0 and len(post_limit_closes) >= 2:
                    pullback_pct = float(post_limit_closes.min() / limit_close - 1.0)
                    retention_pct = float(post_limit_closes.iloc[0] / limit_close - 1.0)
                    consolidation_range = float(
                        post_limit_closes.max() / post_limit_closes.min() - 1.0
                    )
                    if (
                        pullback_pct >= -0.06
                        and retention_pct >= -0.03
                        and consolidation_range <= 0.12
                    ):
                        score += 8.0
                        details.append("涨停后承接健康")
                    elif pullback_pct <= -0.10:
                        score -= 4.0
                        details.append("涨停后回撤过深")

        if len(closes) >= 11:
            ma10 = float(closes.iloc[:10].mean())
            previous_ma10 = float(closes.iloc[1:11].mean())
            if ma10 > 0 and previous_ma10 > 0:
                slope_pct = (ma10 / previous_ma10 - 1.0) * 100.0
                deviation_pct = (float(closes.iloc[0]) / ma10 - 1.0) * 100.0
                score += min(16.0, max(0.0, slope_pct * 12.0))
                if 0.0 <= deviation_pct <= 12.0:
                    score += max(0.0, 16.0 - deviation_pct * 1.3)
                details.append(f"MA10斜率{slope_pct:+.2f}%")
                details.append(f"MA10乖离{deviation_pct:+.2f}%")

        if turnover is not None:
            turnover_value = float(turnover)
            score += max(0.0, 8.0 - abs(turnover_value - 15.0) * 0.8)
            details.append(f"换手{turnover_value:.2f}%")

        if len(frame) >= 6 and "volume" in frame:
            amounts = (frame["close"] * frame["volume"]).iloc[:6]
            prior_average = float(amounts.iloc[1:].mean())
            amount_ratio = float(amounts.iloc[0] / prior_average) if prior_average > 0 else 0.0
            volume_score = min(12.0, max(0.0, (amount_ratio - 1.0) * 15.0))
            if amount_ratio > 2.0:
                volume_score -= min(8.0, (amount_ratio - 2.0) * 4.0)
            score += max(0.0, volume_score)
            details.append(f"量额比{amount_ratio:.2f}")

        if {"open", "high", "low"}.issubset(frame.columns):
            current = frame.iloc[0]
            spread = float(current["high"] - current["low"])
            if spread > 0:
                close_strength = (float(current["close"] - current["low"]) / spread)
                upper_shadow = (
                    float(current["high"] - max(current["open"], current["close"])) / spread
                )
                score += max(0.0, min(12.0, (close_strength - 0.45) * 20.0))
                score -= min(8.0, max(0.0, upper_shadow) * 10.0)
                details.append(f"收盘强度{close_strength:.2f}")

        return round(max(0.0, score), 1), "，".join(details)

    @staticmethod
    def _natural_strength_breakdown(
        history_df: pd.DataFrame, limit_flags: List[bool],
        turnover: Optional[float],
    ) -> tuple[float, List[Dict[str, Any]]]:
        """Return the strength score with each factor's actual contribution."""
        frame = history_df.copy()
        for column in ("open", "high", "low", "close", "volume"):
            if column in frame:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["close"]).reset_index(drop=True)
        if frame.empty:
            return 0.0, [{"label": "强度评分数据不足", "points": 0.0}]

        closes = frame["close"]
        score = 0.0
        details: List[Dict[str, Any]] = []

        def add(label: str, points: float) -> None:
            nonlocal score
            score += points
            details.append({"label": label, "points": round(points, 1)})

        recent_limit_up = next((index for index, hit in enumerate(limit_flags) if hit), None)
        if recent_limit_up is None:
            add("近 20 日未出现涨停", 0.0)
            add("连板（近段无涨停）", 0.0)
            add("涨停后承接（近段无涨停）", 0.0)
        else:
            limit_score = max(0.0, 22.0 - recent_limit_up * 1.1)
            add(f"最近涨停在 {recent_limit_up} 日前", limit_score)
            consecutive_limit_ups = 0
            for hit in limit_flags[recent_limit_up:]:
                if not hit:
                    break
                consecutive_limit_ups += 1
            if consecutive_limit_ups == 2:
                add("连板 2", 10.0)
            elif consecutive_limit_ups == 3:
                add("连板 3", 6.0)
            elif consecutive_limit_ups >= 4:
                add(f"连板 {consecutive_limit_ups}（过热）", -3.0)
            else:
                add("连板（不是连续涨停）", 0.0)

            if 2 <= recent_limit_up <= 5 and recent_limit_up < len(closes):
                limit_close = float(closes.iloc[recent_limit_up])
                post_limit_closes = closes.iloc[:recent_limit_up].dropna()
                if limit_close > 0 and len(post_limit_closes) >= 2:
                    pullback_pct = float(post_limit_closes.min() / limit_close - 1.0)
                    retention_pct = float(post_limit_closes.iloc[0] / limit_close - 1.0)
                    consolidation_range = float(post_limit_closes.max() / post_limit_closes.min() - 1.0)
                    if pullback_pct >= -0.06 and retention_pct >= -0.03 and consolidation_range <= 0.12:
                        add("涨停后承接健康", 8.0)
                    elif pullback_pct <= -0.10:
                        add("涨停后回撤过深", -4.0)
                    else:
                        add("涨停后承接未达加分条件", 0.0)
                else:
                    add("涨停后承接数据不足", 0.0)
            else:
                add("涨停后承接不在观察期", 0.0)

        if len(closes) >= 11:
            ma10 = float(closes.iloc[:10].mean())
            previous_ma10 = float(closes.iloc[1:11].mean())
            if ma10 > 0 and previous_ma10 > 0:
                slope_pct = (ma10 / previous_ma10 - 1.0) * 100.0
                deviation_pct = (float(closes.iloc[0]) / ma10 - 1.0) * 100.0
                slope_score = min(16.0, max(0.0, slope_pct * 12.0))
                add(f"MA10 斜率 {slope_pct:+.2f}%" + ("（已达到上限）" if slope_score >= 16.0 else ""), slope_score)
                deviation_score = max(0.0, 16.0 - deviation_pct * 1.3) if 0.0 <= deviation_pct <= 12.0 else 0.0
                add(f"MA10 乖离 {deviation_pct:+.2f}%", deviation_score)
            else:
                add("MA10 趋势数据不足", 0.0)
                add("MA10 乖离数据不足", 0.0)
        else:
            add("MA10 趋势数据不足", 0.0)
            add("MA10 乖离数据不足", 0.0)

        if turnover is None:
            add("换手率数据不足", 0.0)
        else:
            turnover_value = float(turnover)
            add(f"换手 {turnover_value:.2f}%", max(0.0, 8.0 - abs(turnover_value - 15.0) * 0.8))

        if len(frame) >= 6 and "volume" in frame:
            amounts = (frame["close"] * frame["volume"]).iloc[:6]
            prior_average = float(amounts.iloc[1:].mean())
            amount_ratio = float(amounts.iloc[0] / prior_average) if prior_average > 0 else 0.0
            volume_score = min(12.0, max(0.0, (amount_ratio - 1.0) * 15.0))
            if amount_ratio > 2.0:
                volume_score -= min(8.0, (amount_ratio - 2.0) * 4.0)
            add(f"量额比 {amount_ratio:.2f}", max(0.0, volume_score))
        else:
            add("量额比数据不足", 0.0)

        if {"open", "high", "low"}.issubset(frame.columns):
            current = frame.iloc[0]
            spread = float(current["high"] - current["low"])
            if spread > 0:
                close_strength = float(current["close"] - current["low"]) / spread
                upper_shadow = float(current["high"] - max(current["open"], current["close"])) / spread
                add(f"收盘强度 {close_strength:.2f}", max(0.0, min(12.0, (close_strength - 0.45) * 20.0)))
                add(f"上影线占日内波动约 {upper_shadow * 100:.1f}%", -min(8.0, max(0.0, upper_shadow) * 10.0))
            else:
                add("收盘强度数据不足", 0.0)
                add("上影线数据不足", 0.0)
        else:
            add("收盘强度数据不足", 0.0)
            add("上影线数据不足", 0.0)

        return round(max(0.0, score), 1), details

    @staticmethod
    def _normalize_screen_time_range(time_range: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Normalize the editor's optional historical-data window."""
        if not isinstance(time_range, dict):
            return {"mode": "all"}
        mode = str(time_range.get("mode") or "all").strip().lower()
        if mode in {"", "all"}:
            return {"mode": "all"}
        if mode == "custom":
            start = str(time_range.get("start_date") or "").strip()
            end = str(time_range.get("end_date") or "").strip()
            try:
                if start:
                    pd.Timestamp(start)
                if end:
                    pd.Timestamp(end)
            except (TypeError, ValueError) as exc:
                raise ValueError("自定义时间范围格式不正确") from exc
            if start and end and pd.Timestamp(start) > pd.Timestamp(end):
                raise ValueError("自定义时间范围的开始日期不能晚于结束日期")
            return {"mode": "custom", "start_date": start, "end_date": end}
        try:
            days = int(time_range.get("days", mode))
        except (TypeError, ValueError) as exc:
            raise ValueError("不支持的代码筛选时间范围") from exc
        if days not in {22, 66, 132, 252}:
            raise ValueError("不支持的代码筛选时间范围")
        return {"mode": mode, "days": days}

    @staticmethod
    def _filter_screen_history(df: pd.DataFrame, time_range: Dict[str, Any]) -> pd.DataFrame:
        """Keep only the selected date window while preserving source row order."""
        if not time_range or time_range.get("mode") == "all" or df.empty or "date" not in df:
            return df
        dates = pd.to_datetime(df["date"], errors="coerce")
        valid_dates = dates.dropna()
        if valid_dates.empty:
            return df.iloc[0:0]
        start = time_range.get("start_date")
        end = time_range.get("end_date")
        if time_range.get("mode") != "custom":
            ordered_dates = valid_dates.sort_values().drop_duplicates()
            end_timestamp = ordered_dates.iloc[-1]
            offset = max(0, len(ordered_dates) - int(time_range["days"]))
            start_timestamp = ordered_dates.iloc[offset]
        else:
            if start and not end:
                start_timestamp = end_timestamp = pd.Timestamp(start)
            else:
                start_timestamp = pd.Timestamp(start) if start else valid_dates.min()
                end_timestamp = pd.Timestamp(end) if end else valid_dates.max()
        return df.loc[(dates >= start_timestamp) & (dates <= end_timestamp)].copy()

    @staticmethod
    def _screen_as_of_date(time_range: Optional[Dict[str, Any]]) -> Optional[pd.Timestamp]:
        """Return the selected historical date, if the user supplied one."""
        if not isinstance(time_range, dict) or time_range.get("mode") != "custom":
            return None
        value = time_range.get("end_date") or time_range.get("start_date")
        return pd.Timestamp(value).normalize() if value else None

    def _get_screen_snapshot(
        self, code: str, df: pd.DataFrame, time_range: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Read one internally consistent daily snapshot for screening."""
        if df is None or df.empty or "date" not in df.columns:
            return None
        rows = df.copy()
        rows["_screen_date"] = pd.to_datetime(rows["date"], errors="coerce").dt.normalize()
        rows = rows.dropna(subset=["_screen_date"])
        if rows.empty:
            return None
        as_of = self._screen_as_of_date(time_range)
        if as_of is not None:
            exact_rows = rows.loc[rows["_screen_date"] == as_of]
            if exact_rows.empty:
                return None
            current = exact_rows.sort_values("_screen_date", ascending=False).iloc[0]
            previous_rows = rows.loc[rows["_screen_date"] < as_of]
            previous_rows = previous_rows.sort_values("_screen_date", ascending=False)
            previous = previous_rows.iloc[0] if not previous_rows.empty else None
        else:
            rows = rows.sort_values("_screen_date", ascending=False).reset_index(drop=True)
            if rows.empty:
                return None
            current = rows.iloc[0]
            previous = rows.iloc[1] if len(rows.index) > 1 else None

        def number(value: Any) -> float:
            parsed = pd.to_numeric(value, errors="coerce")
            return float(parsed) if pd.notna(parsed) else 0.0

        price = number(current.get("close"))
        volume = number(current.get("volume"))
        # stock_kline stores daily volume in lots; convert it to shares first.
        amount = price * volume * 100.0
        market_cap = number(current.get("market_cap"))
        if 0 < market_cap < 1_000_000:
            market_cap *= 1e8
        return {
            "price": price,
            "amount": amount,
            "market_cap": market_cap,
            "previous_close": number(previous.get("close")) if previous is not None else 0.0,
            "data_date": pd.Timestamp(current["_screen_date"]).strftime("%Y-%m-%d"),
            "data_source": "stock_kline",
        }

    def _load_historical_screen_data(
        self, codes: List[str], as_of: pd.Timestamp, lookback_days: int,
    ) -> Dict[str, pd.DataFrame]:
        """Load the small historical window needed by a dated screen in batches."""
        if not codes:
            return {}
        if not hasattr(self.db_manager, "query"):
            # Keep the engine usable with the lightweight DB doubles used by
            # the desktop tests and integrations.
            result = {}
            start_date = as_of - timedelta(days=max(60, lookback_days * 3))
            for code in codes:
                frame = self.db_manager.read_stock(code)
                if frame is None or frame.empty or "date" not in frame:
                    continue
                dates = pd.to_datetime(frame["date"], errors="coerce")
                result[str(code)] = frame.loc[
                    (dates >= start_date) & (dates <= as_of)
                ].copy()
            return result
        start_date = (as_of - timedelta(days=max(60, lookback_days * 3))).strftime("%Y-%m-%d")
        end_date = as_of.strftime("%Y-%m-%d")
        columns = "code, date, open, high, low, close, volume, market_cap, K, D, J"
        data: Dict[str, List[Dict[str, Any]]] = {}
        for start in range(0, len(codes), 900):
            batch = [str(code) for code in codes[start:start + 900]]
            placeholders = ",".join("?" for _ in batch)
            rows = self.db_manager.query(
                f"SELECT {columns} FROM stock_kline "
                f"WHERE code IN ({placeholders}) AND date >= ? AND date <= ? "
                "ORDER BY code, date DESC",
                tuple(batch) + (start_date, end_date),
            )
            for row in rows:
                data.setdefault(str(row["code"]), []).append(row)
        return {
            code: pd.DataFrame(rows).assign(date=lambda frame: pd.to_datetime(frame["date"]))
            for code, rows in data.items()
        }

    def _ensure_historical_screen_data(
        self, codes: List[str], as_of: pd.Timestamp, required_bars: int = 2,
    ) -> Dict[str, Any]:
        """Import missing real historical bars before a dated screen."""
        missing_codes = []
        is_trading_day = self.calendar.is_trading_day(as_of.date()) if getattr(self, "calendar", None) else True
        availability: Dict[str, Dict[str, Any]] = {}
        if hasattr(self.db_manager, "query"):
            for start in range(0, len(codes), 900):
                batch = [str(code) for code in codes[start:start + 900]]
                placeholders = ",".join("?" for _ in batch)
                rows = self.db_manager.query(
                    "SELECT code, COUNT(*) AS available_bars, "
                    "MAX(CASE WHEN date = ? THEN 1 ELSE 0 END) AS has_exact_date "
                    f"FROM stock_kline WHERE code IN ({placeholders}) AND date <= ? GROUP BY code",
                    (as_of.strftime("%Y-%m-%d"), *batch, as_of.strftime("%Y-%m-%d")),
                )
                availability.update({str(row["code"]): row for row in rows})
        else:
            for code in codes:
                frame = self.db_manager.read_stock(code)
                if frame is None or frame.empty or "date" not in frame:
                    continue
                dates = pd.to_datetime(frame["date"], errors="coerce").dropna().dt.normalize()
                availability[str(code)] = {
                    "available_bars": int((dates <= as_of).sum()),
                    "has_exact_date": int((dates == as_of).any()),
                }
        for code in codes:
            row = availability.get(str(code), {})
            if ((is_trading_day and not row.get("has_exact_date"))
                    or int(row.get("available_bars") or 0) < required_bars):
                missing_codes.append(str(code))

        imported_codes = 0
        source_counts: Dict[str, int] = {}
        deferred_codes = 0
        import_running = False
        if missing_codes:
            years = max(1, int((datetime.now().date() - as_of.date()).days / 365) + 2)
            if len(missing_codes) > 200:
                import_running = self._start_historical_import(missing_codes, as_of, years)
                deferred_codes = len(missing_codes)
            else:
                imported_codes, source_counts = self._import_historical_kline_codes(
                    missing_codes, years
                )

        market_cap_date = None
        market_cap_updated = 0
        if hasattr(self.db_manager, "query"):
            try:
                rows = self.db_manager.query(
                    "SELECT MAX(date) AS data_date FROM stock_kline WHERE date <= ?",
                    (as_of.strftime("%Y-%m-%d"),),
                )
                market_cap_date = rows[0].get("data_date") if rows else None
            except Exception:
                logger.debug("无法确定历史市值导入日期", exc_info=True)
        if market_cap_date:
            market_cap_updated = self._import_historical_market_caps(market_cap_date)
        return {
            "missing_codes": len(missing_codes),
            "imported_codes": imported_codes,
            "source_counts": source_counts,
            "deferred_codes": deferred_codes,
            "import_running": import_running,
            "market_cap_date": market_cap_date,
            "market_cap_updated": market_cap_updated,
        }

    def _import_historical_kline_codes(
        self, codes: List[str], years: int,
    ) -> tuple[int, Dict[str, int]]:
        """Fetch and persist real K-lines using the available source fallbacks."""
        imported_codes = 0
        source_counts: Dict[str, int] = {}
        try:
            from utils.akshare_fetcher import AKShareFetcher

            fetcher = AKShareFetcher()
            days = max(60, years * 250)
            batch_size = 100
            for start in range(0, len(codes), batch_size):
                batch = codes[start:start + batch_size]
                data, _ = fetcher.kline_fetcher._fetch_kline_tickflow_batch(
                    batch, days=days
                )
                if data:
                    source_counts["tickflow"] = source_counts.get("tickflow", 0) + len(data)
                unresolved = [code for code in batch if code not in data]
                if unresolved:
                    tencent_data = fetcher.stock_data_fetcher._fetch_stock_batch_tencent(
                        unresolved, years=years, concurrency=2
                    )
                    if tencent_data:
                        data.update(tencent_data)
                        source_counts["tencent"] = source_counts.get("tencent", 0) + len(tencent_data)
                if data:
                    fetcher.kline_fetcher._batch_update_kline_to_db(data)
                    imported_codes += len(data)
        except Exception as exc:
            logger.warning("历史筛选K线自动导入失败: %s", exc)
        return imported_codes, source_counts

    def _start_historical_import(
        self, codes: List[str], as_of: pd.Timestamp, years: int,
    ) -> bool:
        """Start one background import so the UI request remains responsive."""
        lock = getattr(self, "_historical_import_lock", None)
        if lock is None:
            self._historical_import_lock = threading.Lock()
            lock = self._historical_import_lock
        with lock:
            if getattr(self, "_historical_import_running", False):
                return True
            self._historical_import_running = True

        def worker() -> None:
            try:
                imported, sources = self._import_historical_kline_codes(codes, years)
                logger.info(
                    "历史筛选后台补数完成: 基准日=%s, 请求=%s, 成功=%s, 来源=%s",
                    as_of.strftime("%Y-%m-%d"), len(codes), imported, sources,
                )
            finally:
                with lock:
                    self._historical_import_running = False

        threading.Thread(
            target=worker,
            name="historical-screen-import",
            daemon=True,
        ).start()
        return True

    def _import_historical_market_caps(self, trade_date: str) -> int:
        """Import dated market caps from Tushare when a token is configured."""
        try:
            import tushare as ts

            token_path = self.config_file.parent / "tushare_config.json"
            with open(token_path, "r", encoding="utf-8") as handle:
                config = json.load(handle)
            token = config.get("token") or config.get("api_key")
            if not token:
                logger.warning("未配置 Tushare token，无法导入 %s 历史市值", trade_date)
                return 0
            pro = ts.pro_api(token)
            daily = pro.daily_basic(
                trade_date=str(trade_date).replace("-", ""),
                fields="ts_code,total_mv",
            )
            if daily is None or daily.empty:
                logger.warning("Tushare 未返回 %s 历史市值", trade_date)
                return 0

            updates = []
            for _, row in daily.iterrows():
                try:
                    code = str(row["ts_code"]).split(".")[0].zfill(6)
                    total_mv = float(row["total_mv"])
                    if total_mv > 0:
                        # Tushare total_mv is in 10,000 yuan; stock_kline
                        # screening normalizes stored yuan values directly.
                        updates.append((total_mv * 10000.0, code, trade_date))
                except (KeyError, TypeError, ValueError):
                    continue
            if not updates:
                return 0
            with self.db_manager.transaction():
                for market_cap, code, date_value in updates:
                    self.db_manager.execute(
                        "UPDATE stock_kline SET market_cap = ? WHERE code = ? AND date = ?",
                        (market_cap, code, date_value),
                    )
            logger.info("历史市值导入完成: 日期=%s, 更新=%s", trade_date, len(updates))
            return len(updates)
        except ModuleNotFoundError:
            logger.warning("未安装 Tushare，无法导入 %s 历史市值", trade_date)
        except FileNotFoundError:
            logger.warning("未找到 %s，跳过历史市值导入", self.config_file.parent / "tushare_config.json")
        except Exception as exc:
            logger.warning("历史市值导入失败(%s): %s", trade_date, exc)
        return 0

    def _screen_natural_rules(
        self, spec: Dict[str, Any], max_stocks: Optional[int],
        progress_callback: Optional[Callable[[str], None]] = None,
        force_refresh: bool = False,
        history_window: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        def report(message: str) -> None:
            if progress_callback:
                try:
                    progress_callback(message)
                except Exception:
                    logger.debug("筛选进度回调失败", exc_info=True)

        codes = self.db_manager.list_all_stocks()
        names = (
            self.db_manager.get_all_stock_names()
            if hasattr(self.db_manager, "get_all_stock_names") else {}
        )
        as_of = self._screen_as_of_date(history_window)
        historical_mode = as_of is not None
        poller = None
        quotes: Dict[str, Quote] = {}
        if historical_mode:
            report(f"正在读取 {as_of:%Y-%m-%d} 历史K线...")
        else:
            # No concrete historical date was selected: use the computer's
            # current market snapshot for today's price and amount.
            poller = self._get_screen_poller()
            report("正在获取实时行情...")
            quotes = poller.fetch_all(
                force=force_refresh,
                max_age=float(getattr(self.config, "screen_quote_cache_sec", 30.0)),
                fast=False,
            )
            if quotes:
                codes = sorted(set(codes) | set(quotes))
                names = {
                    **names,
                    **{
                        code: quote.name
                        for code, quote in quotes.items()
                        if getattr(quote, "name", "")
                    },
                }
        if max_stocks and max_stocks > 0:
            codes = codes[:max_stocks]
        if historical_mode:
            import_stats = self._ensure_historical_screen_data(
                codes,
                as_of,
                required_bars=max(
                    2,
                    21 if spec.get("rank_by_strength") else 0,
                    int(spec["limit_days"] or 0) + 2,
                    int(spec.get("close_above_ma_days") or 0),
                    int(spec.get("volume_ratio_lookback") or 0) + 2,
                ),
            )
            history_data = self._load_historical_screen_data(
                codes,
                as_of,
                max(
                    2,
                    21 if spec.get("rank_by_strength") else 0,
                    int(spec["limit_days"] or 0) + 2,
                    int(spec.get("close_above_ma_days") or 0),
                    int(spec.get("volume_ratio_lookback") or 0) + 2,
                ),
            )
            report(f"历史K线读取完成（基准日 {as_of:%Y-%m-%d}），正在计算筛选条件...")
        else:
            import_stats = {}
            report(f"实时行情完成（{len(quotes)} 只），正在计算筛选条件...")

        # Only the current-date path may refresh or append the current intraday
        # bar. A historical screen must remain reproducible from local history.
        if (
            spec["limit_count"] is not None
            or spec.get("limit_count_min_inclusive") is not None
            or spec.get("limit_count_min_exclusive") is not None
            or spec.get("close_above_ma_days") is not None
            or spec.get("rank_by_strength")
        ) and not historical_mode:
            refresh_codes = []
            for code in codes:
                name = names.get(code, "")
                if spec["exclude_st"] and (name.startswith(("ST", "*ST")) or "ST" in name.upper()):
                    continue
                if spec["exclude_bj"] and str(code).startswith(("4", "8")):
                    continue
                if spec["exclude_star"] and str(code).startswith(("688", "689")):
                    continue
                quote = quotes.get(code)
                if quote is None or quote.price <= 0:
                    continue
                if spec["price_max"] is not None and quote.price >= spec["price_max"]:
                    continue
                if spec["amount_min"] is not None and quote.amount <= spec["amount_min"]:
                    continue
                if spec["turnover_min"] is not None and not (
                    float(getattr(quote, "turnover", 0.0) or 0.0) > spec["turnover_min"]
                    and float(getattr(quote, "turnover", 0.0) or 0.0) < spec["turnover_max"]
                ):
                    continue
                if spec["market_cap_max"] is not None and (
                    quote.market_cap <= 0 or quote.market_cap >= spec["market_cap_max"]
                ):
                    continue
                refresh_codes.append(code)
            report(f"需要校验日线条件的股票 {len(refresh_codes)} 只，正在检查本地 K 线...")
            refresh_days = max(
                40,
                21 if spec.get("rank_by_strength") else 0,
                int(spec["limit_days"] or 0) + 10,
                int(spec.get("close_above_ma_days") or 0) + 10,
            )
            if history_window and history_window.get("mode") != "all":
                refresh_days = max(refresh_days, int(history_window.get("days") or 0) + 10)
            self._refresh_intraday_screen_history(refresh_codes, days=refresh_days)
            report("历史 K 线准备完成，正在计算筛选结果...")

        results = []
        errors = []
        warnings = []
        historical_market_cap_missing = False
        needs_history = (
            spec["limit_count"] is not None
            or spec.get("limit_count_min_inclusive") is not None
            or spec.get("limit_count_min_exclusive") is not None
            or spec.get("close_above_ma_days") is not None
            or spec.get("volume_ratio_min") is not None
            or spec.get("volume_shrink")
            or spec.get("rank_by_strength")
        )
        for code in codes:
            name = names.get(code, "")
            try:
                if spec["exclude_st"] and (name.startswith(("ST", "*ST")) or "ST" in name.upper()):
                    continue
                if spec["exclude_bj"] and str(code).startswith(("4", "8")):
                    continue
                if spec["exclude_star"] and str(code).startswith(("688", "689")):
                    continue
                quote = quotes.get(code)
                df = None
                snapshot = None
                if historical_mode:
                    df = history_data.get(str(code))
                    snapshot = self._get_screen_snapshot(code, df, history_window)
                    if snapshot is None or snapshot["price"] <= 0:
                        errors.append({"code": str(code), "error": f"{as_of:%Y-%m-%d} 缺少历史K线"})
                        continue
                    price = snapshot["price"]
                else:
                    if quote is None or quote.price <= 0:
                        continue
                    price = float(quote.price)
                checks = []
                if spec["price_max"] is not None:
                    checks.append(price < spec["price_max"])

                if spec["market_cap_max"] is not None:
                    if historical_mode:
                        market_cap = float(snapshot["market_cap"] or 0)
                        if market_cap <= 0:
                            # Do not substitute today's market cap for a
                            # historical date. If the historical field is
                            # unavailable, keep the candidate and report the
                            # skipped condition once in the response.
                            historical_market_cap_missing = True
                            market_cap = None
                    else:
                        market_cap = float(quote.market_cap or 0)
                    if market_cap is not None:
                        checks.append(market_cap > 0 and market_cap < spec["market_cap_max"])
                else:
                    market_cap = 0.0

                if spec["amount_min"] is not None:
                    if historical_mode:
                        amount = float(snapshot["amount"] or 0)
                    else:
                        amount = float(quote.amount or 0)
                    checks.append(amount > spec["amount_min"])
                else:
                    amount = 0.0

                if spec["turnover_min"] is not None:
                    turnover = (
                        float(snapshot.get("turnover") or 0.0)
                        if historical_mode else
                        float(getattr(quote, "turnover", 0.0) or 0.0)
                    )
                    checks.append(
                        turnover > float(spec["turnover_min"])
                        and turnover < float(spec["turnover_max"])
                    )
                else:
                    turnover = None

                limit_count = 0
                limit_flags = []
                volume_ratio = None
                ma_value = None
                strength_score = None
                strength_reason = ""
                strength_breakdown = []
                if needs_history:
                    # The realtime path gets price/amount from the quote
                    # snapshot, but limit-up counts still need the latest
                    # daily K-lines. Load them after the cheap quote filters
                    # so a realtime screen can evaluate both data sources.
                    if not historical_mode:
                        df = self.db_manager.read_stock(code)
                    if df is None or df.empty:
                        continue
                    if historical_mode:
                        history_df = df.copy()
                        dates = pd.to_datetime(history_df["date"], errors="coerce").dt.normalize()
                        history_df = history_df.loc[dates <= as_of].copy()
                        history_df["_screen_date"] = dates.loc[history_df.index]
                        history_df = history_df.sort_values("_screen_date", ascending=False)
                    else:
                        latest_date = pd.Timestamp(df.iloc[0]["date"]).date()
                        if latest_date < self._screen_required_completed_date():
                            errors.append({
                                "code": str(code),
                                "error": f"历史K线未更新至所需交易日（最新 {latest_date}）",
                            })
                            continue
                        intraday_df = poller.build_intraday_df(
                            code,
                            quote,
                            tail_bars=max(
                                2,
                                21 if spec.get("rank_by_strength") else 0,
                                int(spec["limit_days"] or 0) + 2,
                                int(spec.get("volume_ratio_lookback") or 0) + 2,
                            ),
                        )
                        history_df = intraday_df if intraday_df is not None and not intraday_df.empty else df
                        history_df = self._filter_screen_history(
                            history_df, history_window or {"mode": "all"}
                        )
                    if history_df.empty:
                        continue
                    volumes = pd.to_numeric(history_df.get("volume"), errors="coerce").reset_index(drop=True)
                    if spec.get("volume_ratio_min") is not None:
                        lookback = int(spec.get("volume_ratio_lookback") or 5)
                        current_volume = float(volumes.iloc[1]) if len(volumes) > 1 and pd.notna(volumes.iloc[1]) else 0.0
                        prior_volumes = volumes.iloc[2:2 + lookback].dropna()
                        average_volume = float(prior_volumes.mean()) if len(prior_volumes) == lookback else 0.0
                        volume_ratio = current_volume / average_volume if average_volume > 0 else None
                        checks.append(
                            volume_ratio is not None
                            and volume_ratio >= float(spec["volume_ratio_min"])
                        )
                    if spec.get("volume_shrink"):
                        current_volume = float(volumes.iloc[0]) if len(volumes) > 0 and pd.notna(volumes.iloc[0]) else 0.0
                        previous_volume = float(volumes.iloc[1]) if len(volumes) > 1 and pd.notna(volumes.iloc[1]) else 0.0
                        checks.append(current_volume > 0 and previous_volume > 0 and current_volume < previous_volume)
                    if (
                        spec["limit_count"] is not None
                        or spec.get("limit_count_min_inclusive") is not None
                        or spec.get("limit_count_min_exclusive") is not None
                        or spec.get("rank_by_strength")
                    ):
                        closes = pd.to_numeric(history_df["close"], errors="coerce").reset_index(drop=True)
                        limit_window = int(spec["limit_days"] or 20)
                        for idx in range(min(limit_window, len(closes) - 1)):
                            current = closes.iloc[idx]
                            previous = closes.iloc[idx + 1]
                            limit_flags.append(self._is_limit_up(code, name, current, previous))
                        limit_count = sum(limit_flags)
                        if spec["limit_count"] is not None:
                            if spec.get("limit_at_least"):
                                checks.append(limit_count >= spec["limit_count"])
                            else:
                                checks.append(limit_count == spec["limit_count"])
                    if spec.get("limit_count_min_exclusive") is not None:
                        checks.append(
                            limit_count > int(spec["limit_count_min_exclusive"])
                            and limit_count < int(spec["limit_count_max_exclusive"])
                        )
                    if spec.get("limit_count_min_inclusive") is not None:
                        checks.append(
                            limit_count >= int(spec["limit_count_min_inclusive"])
                            and limit_count < int(spec["limit_count_max_exclusive"])
                        )
                    ma_days = spec.get("close_above_ma_days")
                    if ma_days is not None:
                        closes = pd.to_numeric(history_df["close"], errors="coerce").reset_index(drop=True)
                        ma_closes = closes.iloc[:int(ma_days)].dropna()
                        ma_value = float(ma_closes.mean()) if len(ma_closes) == int(ma_days) else None
                        current_close = float(closes.iloc[0]) if len(closes) and pd.notna(closes.iloc[0]) else 0.0
                        checks.append(ma_value is not None and current_close > ma_value)
                    slope_days = spec.get("ma_slope_positive_days")
                    deviation_days = spec.get("ma_deviation_days")
                    if slope_days is not None or deviation_days is not None:
                        closes = pd.to_numeric(history_df["close"], errors="coerce").reset_index(drop=True)
                    if slope_days is not None:
                        days = int(slope_days)
                        current_ma = closes.iloc[:days].dropna()
                        previous_ma = closes.iloc[1:days + 1].dropna()
                        checks.append(
                            len(current_ma) == days and len(previous_ma) == days
                            and float(current_ma.mean()) > float(previous_ma.mean())
                        )
                    if deviation_days is not None:
                        days = int(deviation_days)
                        ma_values = closes.iloc[:days].dropna()
                        current_close = float(closes.iloc[0]) if len(closes) and pd.notna(closes.iloc[0]) else 0.0
                        ma_value = float(ma_values.mean()) if len(ma_values) == days else 0.0
                        deviation = (current_close / ma_value - 1.0) * 100.0 if ma_value > 0 else None
                        checks.append(
                            deviation is not None
                            and deviation > float(spec["ma_deviation_min"])
                            and deviation < float(spec["ma_deviation_max"])
                        )
                    if spec.get("rank_by_strength"):
                        strength_score, strength_breakdown = self._natural_strength_breakdown(
                            history_df, limit_flags, turnover
                        )
                        _, strength_reason = self._natural_strength_score(
                            history_df, limit_flags, turnover
                        )
                # Exclusion-only rules still select every stock that survived
                # the exclusions. Numeric/history rules append their checks.
                if not checks or all(checks):
                    market_cap_text = (
                        f"{market_cap / 1e8:.2f}亿"
                        if market_cap is not None else "未知"
                    )
                    result_change = (
                        ((price - snapshot["previous_close"]) / snapshot["previous_close"] * 100)
                        if historical_mode and snapshot["previous_close"] else
                        float(quote.pct or 0)
                    )
                    results.append({
                        "code": code,
                        "name": name,
                        "price": round(price, 2),
                        "change": round(result_change, 2),
                        "data_date": snapshot["data_date"] if historical_mode else None,
                        "data_source": snapshot["data_source"] if historical_mode else "realtime_quote",
                        **(
                            {
                                "strength_score": strength_score,
                                "strength_reason": strength_reason,
                                "strength_breakdown": strength_breakdown,
                            }
                            if spec.get("rank_by_strength") else {}
                        ),
                        "reason": (
                            f"命中中文规则；基准日={snapshot['data_date'] if historical_mode else '电脑当前交易日'}，"
                            f"成交额={amount / 1e8:.2f}亿，市值={market_cap_text}，近段涨停={limit_count}次"
                            + (f"，换手率={turnover:.2f}%" if turnover is not None else "")
                            + (f"，量比={volume_ratio:.2f}" if volume_ratio is not None else "")
                            + (f"，{int(spec['close_above_ma_days'])}日线={ma_value:.2f}"
                               if ma_value is not None else "")
                            + (f"，强度评分={strength_score:.1f}（{strength_reason}）"
                               if strength_score is not None else "")
                        ),
                    })
            except Exception as exc:
                errors.append({"code": str(code), "error": str(exc)})
        if historical_market_cap_missing:
            warnings.append(
                f"{as_of:%Y-%m-%d} 缺少历史市值数据，已跳过“市值<{spec['market_cap_max'] / 1e8:.0f}亿”条件"
            )
        if spec.get("rank_by_strength"):
            results.sort(key=lambda item: (-float(item.get("strength_score") or 0.0), str(item["code"])))
        return {
            "results": results,
            "processed": len(codes),
            "errors": errors,
            "warnings": warnings,
            "conditions_enforced": not historical_market_cap_missing,
            "import_stats": import_stats,
            "quote_count": len(quotes),
            "data_date": as_of.strftime("%Y-%m-%d") if historical_mode else None,
            "data_source": "stock_kline" if historical_mode else "realtime_quote",
        }

    def pause_screen_quote_refresh(self) -> None:
        """Pause expensive full-market quote refresh during historical work."""
        if self._screen_poller is not None:
            self._screen_poller.stop_background_refresh()

    def resume_screen_quote_refresh(self) -> None:
        """Resume background quote refresh after historical work completes."""
        if self._screen_poller is not None:
            self._screen_poller.start_background_refresh(interval_sec=20.0)

    def _refresh_intraday_screen_history(self, codes: List[str], days: int) -> None:
        """Refresh recent K-lines in batches before an intraday screen."""
        if not codes:
            return
        try:
            from utils.akshare_fetcher import AKShareFetcher

            latest_dates = self._get_latest_kline_dates(codes)
            target_date = self._screen_target_trade_date()
            stale_codes = []
            for code in codes:
                latest = latest_dates.get(str(code))
                try:
                    latest_date = pd.Timestamp(latest).date() if latest else None
                except (TypeError, ValueError):
                    latest_date = None
                if latest_date is None or latest_date < target_date:
                    stale_codes.append(code)
            if not stale_codes:
                logger.info("盘中筛选K线已足够新，跳过网络刷新: %s 只", len(codes))
                return

            fetcher = AKShareFetcher()
            batch_size = 100
            refreshed = 0
            for start in range(0, len(stale_codes), batch_size):
                batch = stale_codes[start:start + batch_size]
                data, _ = fetcher.kline_fetcher._fetch_kline_tickflow_batch(
                    batch, days=days
                )
                if data:
                    fetcher.kline_fetcher._batch_update_kline_to_db(data)
                refreshed += len(data)
            logger.info(
                "盘中筛选K线刷新完成: 检查=%s, 请求=%s, 成功=%s, 天数=%s",
                len(codes), len(stale_codes), refreshed, days,
            )
        except Exception as exc:
            logger.warning("盘中筛选K线刷新失败: %s", exc)

    def _screen_target_trade_date(self):
        """Return the date whose quote/history should drive the current screen."""
        now = datetime.now()
        today = now.date()
        if self.calendar.is_trading_day(today) and (
            self.calendar.is_in_trading_session(now) or now.hour >= 15
        ):
            return today
        for offset in range(1, 15):
            candidate = today - timedelta(days=offset)
            if self.calendar.is_trading_day(candidate):
                return candidate
        return today

    def _screen_required_completed_date(self):
        """Return the last completed daily bar needed for limit-up counting."""
        target = self._screen_target_trade_date()
        now = datetime.now()
        if target == now.date() and self.calendar.is_in_trading_session(now):
            for offset in range(1, 15):
                candidate = target - timedelta(days=offset)
                if self.calendar.is_trading_day(candidate):
                    return candidate
        return target

    def save_custom_screen_results(self, results: List[Dict[str, Any]]) -> str:
        """Persist the latest desktop code-screen results as a CSV file."""
        output_dir = self.config_file.parent.parent / "runtime" / "data" / "running"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"custom_screen_{datetime.now():%Y%m%d_%H%M%S}.csv"
        fields = ["code", "name", "price", "reason"]
        with open(output, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        return str(output)

    def _user_strategy_store(self) -> Path:
        path = self.config_file.parent.parent / "runtime" / "data" / "user_screen_strategies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def list_user_screen_strategies(self) -> List[Dict[str, Any]]:
        path = self._user_strategy_store()
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, list):
                return []
            return [item for item in payload if isinstance(item, dict) and item.get("name")]
        except (OSError, ValueError, TypeError):
            return []

    def save_user_screen_strategy(
        self, name: str, source: str, max_stocks: Optional[int],
        results: Optional[List[Dict[str, Any]]] = None,
        time_range: Optional[Dict[str, Any]] = None,
        previous_name: Optional[str] = None,
        backtest_overrides: Optional[Dict[str, Any]] = None,
        refresh_verification: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ValueError("策略名称不能为空")
        if len(name) > 80:
            raise ValueError("策略名称不能超过80个字符")
        if not str(source or "").strip():
            raise ValueError("策略内容不能为空")
        strategies = self.list_user_screen_strategies()
        previous_name = str(previous_name or "").strip()
        if previous_name and previous_name != name and any(
            item.get("name") == name for item in strategies
        ):
            raise ValueError(f"策略名称已存在：{name}")
        previous = next(
            (
                item for item in strategies
                if item.get("name") == (previous_name or name)
            ),
            {},
        )
        normalized_time_range = self._normalize_screen_time_range(
            time_range if time_range is not None else previous.get("time_range")
        )
        normalized_overrides = self._normalize_user_strategy_backtest_overrides(
            backtest_overrides
            if backtest_overrides is not None
            else previous.get("backtest_overrides")
        )
        record = {
            "name": name,
            "source": str(source),
            "max_stocks": int(max_stocks or 0),
            "results": results if results is not None else previous.get("results", []),
            "time_range": normalized_time_range,
            "backtest_overrides": normalized_overrides,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        if isinstance(refresh_verification, dict):
            record["refresh_verification"] = refresh_verification
        names_to_replace = {name}
        if previous_name:
            names_to_replace.add(previous_name)
        strategies = [item for item in strategies if item.get("name") not in names_to_replace]
        strategies.append(record)
        path = self._user_strategy_store()
        temp = path.with_suffix(path.suffix + ".tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(strategies, handle, ensure_ascii=False, indent=2)
        temp.replace(path)
        return record

    @staticmethod
    def _normalize_user_strategy_backtest_overrides(
        overrides: Optional[Dict[str, Any]],
    ) -> Dict[str, int]:
        """Keep only bounded, strategy-level overrides that are safe to persist."""
        if not isinstance(overrides, dict):
            return {}
        try:
            cooldown_days = int(overrides.get("reentry_cooldown_days", 0) or 0)
        except (TypeError, ValueError):
            cooldown_days = 0
        return {"reentry_cooldown_days": max(0, min(60, cooldown_days))}

    def load_user_screen_strategy(self, name: str) -> Dict[str, Any]:
        for item in self.list_user_screen_strategies():
            if item.get("name") == name:
                return item
        raise KeyError(f"找不到保存的策略: {name}")

    @staticmethod
    def _screen_results(screen: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            item for item in screen.get("results") or []
            if isinstance(item, dict)
        ]

    def delete_user_screen_strategy(self, name: str) -> None:
        strategies = [
            item for item in self.list_user_screen_strategies()
            if item.get("name") != name
        ]
        path = self._user_strategy_store()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(strategies, handle, ensure_ascii=False, indent=2)

