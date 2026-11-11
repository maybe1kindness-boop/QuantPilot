"""Historical intraday bars used by the minute-level backtest.

The normal stock_kline table is daily data.  This module keeps 5-minute bars
in a separate cache so minute replay never silently falls back to daily bars.
"""
from __future__ import annotations

import logging
import io
import os
import sqlite3
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout,
)
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from utils.global_db import get_global_db
from utils.mac_power import keep_mac_awake
from utils.stock_exclusions import filter_stock_codes

logger = logging.getLogger(__name__)
_BAOSTOCK_LOCK = threading.Lock()
_INTRADAY_DOWNLOAD_LOCK = threading.Lock()
_INTRADAY_STORE_LOCK = threading.Lock()


def _intraday_worker_count() -> int:
    """Use independent processes so Baostock's global socket is not serialized."""
    try:
        configured = int(os.environ.get('QUANTPILOT_INTRADAY_WORKERS', '8'))
    except (TypeError, ValueError):
        configured = 8
    return max(4, min(configured, 12))


def _baostock_timeout() -> float:
    try:
        return max(5.0, float(os.environ.get('QUANTPILOT_BAOSTOCK_TIMEOUT', '25')))
    except (TypeError, ValueError):
        return 25.0


_EM_TIMEOUT_POOL = ThreadPoolExecutor(max_workers=1)


def call_with_timeout(func, timeout_seconds: float = 8.0, **kwargs):
    '''行情源调用的硬超时包装。

    东财被定向封锁时可能从快失败恶化为"静默丢包"挂死(其内部 requests
    无超时)。单 worker 复用池强制 8s 上限: 一旦 worker 卡死, 后续调用
    同样快速超时并直接走 TDX/腾讯备用腿; 进程按交易日重启, 卡死连接
    随进程销毁自愈。
    '''
    future = _EM_TIMEOUT_POOL.submit(func, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeout as exc:
        raise IntradayDataUnavailable(
            f'{getattr(func, "__name__", func)} '
            f'超时({timeout_seconds}s), 行情源无响应'
        ) from exc


# 2026-09-05 服务器实测可用的 TDX 行情主机(按序轮换)
_TDX_FALLBACK_HOSTS = [
    ('218.75.126.9', 7709),
    ('115.238.90.165', 7709),
]


def _tdx_hosts() -> list:
    '''环境变量主机优先, 其后接内置备用主机(去重)。'''
    hosts = [_tdx_host()]
    hosts.extend(_TDX_FALLBACK_HOSTS)
    seen, ordered = set(), []
    for item in hosts:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _tdx_host() -> tuple[str, int]:
    host = os.environ.get('QUANTPILOT_TDX_HOST', '180.153.18.170').strip()
    try:
        port = int(os.environ.get('QUANTPILOT_TDX_PORT', '7709'))
    except (TypeError, ValueError):
        port = 7709
    return host, port


def _date_chunks(start_date: str, end_date: str, days: int = 31):
    """Split a long Baostock request into bounded ranges."""
    cursor = date.fromisoformat(str(start_date)[:10])
    last = date.fromisoformat(str(end_date)[:10])
    while cursor <= last:
        chunk_end = min(last, cursor + timedelta(days=days - 1))
        yield cursor.isoformat(), chunk_end.isoformat()
        cursor = chunk_end + timedelta(days=1)


_intraday_download_status = {
    'status': 'idle',
    'phase': 'idle',
    'total_codes': 0,
    'completed_codes': 0,
    'cached_codes': 0,
    'downloaded_codes': 0,
    'failed_codes': 0,
    'current_code': None,
    'start_date': None,
    'end_date': None,
    'message': '',
    'started_at': None,
    'updated_at': None,
}


def _update_intraday_download_status(**updates) -> None:
    with _INTRADAY_DOWNLOAD_LOCK:
        _intraday_download_status.update(updates)
        _intraday_download_status['updated_at'] = datetime.now().isoformat(timespec='seconds')


def get_intraday_download_progress() -> Dict:
    with _INTRADAY_DOWNLOAD_LOCK:
        status = dict(_intraday_download_status)
    total = int(status.get('total_codes') or 0)
    completed = int(status.get('completed_codes') or 0)
    status['percent'] = round(completed / total * 100, 1) if total else 0.0
    return status


def _intraday_eligible_codes(db_manager, start_date: str) -> set[str]:
    """Return stocks with daily history in the intraday download window."""
    rows = db_manager.query(
        """
        SELECT DISTINCT b.code
        FROM stock_basic b
        JOIN stock_kline k ON k.code = b.code
        WHERE k.date >= ?
        """,
        (str(start_date)[:10],),
    )
    return set(filter_stock_codes({
        str(item['code']).strip()
        for item in rows
        if item.get('code')
    }))


def check_intraday_completeness(db_manager=None) -> Dict:
    """Compare cached 5-minute coverage with stocks active in the date window."""
    db = db_manager or get_global_db()
    result = {
        'table': IntradayDataStore.TABLE,
        'interval': '5',
        'count': 0,
        'stockCount': 0,
        'expectedStockCount': 0,
        'coveredStockCount': 0,
        'missingStockCount': 0,
        'missingCodes': [],
        'availableFrom': None,
        'availableTo': None,
        'eligibleFrom': None,
        'complete': False,
    }
    try:
        row = db.query_one(
            f"SELECT COUNT(*) AS count, COUNT(DISTINCT code) AS stock_count, "
            f"MIN(timestamp) AS available_from, MAX(timestamp) AS available_to "
            f"FROM {IntradayDataStore.TABLE} WHERE interval = ?",
            ('5',),
        )
        if row:
            result['count'] = int(row.get('count') or 0)
            result['stockCount'] = int(row.get('stock_count') or 0)
            result['availableFrom'] = row.get('available_from')
            result['availableTo'] = row.get('available_to')

        eligible_from = str(
            result['availableFrom'] or f"{datetime.now().year}-01-01"
        )[:10]
        eligible_codes = _intraday_eligible_codes(db, eligible_from)
        covered_rows = db.query(
            f"SELECT DISTINCT code FROM {IntradayDataStore.TABLE} WHERE interval = ?",
            ('5',),
        )
        covered_codes = set(filter_stock_codes({
            str(item['code']).strip()
            for item in covered_rows
            if item.get('code')
        }))
        missing_codes = sorted(eligible_codes - covered_codes)
        result['eligibleFrom'] = eligible_from
        result['expectedStockCount'] = len(eligible_codes)
        result['missingCodes'] = missing_codes[:100]
        result['missingStockCount'] = len(missing_codes)
        result['coveredStockCount'] = len(eligible_codes & covered_codes)
        result['complete'] = bool(eligible_codes) and not missing_codes
    except Exception as exc:
        # The table is created lazily by IntradayDataStore. A missing table is
        # therefore equivalent to having no cached 5-minute data yet.
        if 'no such table' not in str(exc).lower():
            logger.warning('检查5分钟K线完整性失败: %s', exc)
    return result


def start_intraday_download(db_manager=None) -> Dict:
    """Download missing 5-minute bars for the cache's shared date range."""
    db = db_manager or get_global_db()
    IntradayDataStore(db)
    status = check_intraday_completeness(db)
    if not status['expectedStockCount']:
        return {'success': False, 'message': '没有可用的股票基础资料'}
    if status['complete']:
        return {'success': False, 'message': '5分钟K线数据已经下载完整'}
    with _INTRADAY_DOWNLOAD_LOCK:
        if _intraday_download_status['status'] == 'running':
            return {'success': False, 'message': '已有5分钟K线下载任务正在运行'}

    eligible_from = str(
        status.get('availableFrom') or f"{datetime.now().year}-01-01"
    )[:10]
    eligible_codes = _intraday_eligible_codes(db, eligible_from)
    covered_rows = db.query(
        f"SELECT DISTINCT code FROM {IntradayDataStore.TABLE} WHERE interval = ?",
        ('5',),
    )
    covered_codes = {
        str(item['code']).strip()
        for item in covered_rows
        if item.get('code')
    }
    codes = sorted(eligible_codes - covered_codes)
    if not codes:
        return {'success': False, 'message': '5分钟K线数据已经下载完整'}

    # Reuse the date window already present in the cache. On a fresh database,
    # use the current calendar year so startup does not launch an unbounded job.
    start_date = str(status.get('availableFrom') or f"{datetime.now().year}-01-01")[:10]
    end_date = str(status.get('availableTo') or datetime.now().date())[:10]
    task_id = f"INTRADAY_DOWNLOAD_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    _update_intraday_download_status(
        status='running', phase='downloading', total_codes=len(codes),
        completed_codes=0, cached_codes=0, downloaded_codes=0, failed_codes=0,
        current_code=None, start_date=start_date, end_date=end_date,
        message=f'正在下载 {len(codes)} 只股票的5分钟K线',
        started_at=datetime.now().isoformat(timespec='seconds'),
    )
    thread = threading.Thread(
        target=_run_intraday_download,
        args=(db, codes, start_date, end_date),
        daemon=True,
        name='intraday-startup-download',
    )
    thread.start()
    return {
        'success': True,
        'message': f'已启动5分钟K线下载，共 {len(codes)} 只股票',
        'taskId': task_id,
    }


def _run_intraday_download(db, codes: List[str], start_date: str, end_date: str) -> None:
    with keep_mac_awake('5分钟 K 线下载'):
        _run_intraday_download_with_power(db, codes, start_date, end_date)


def _run_intraday_download_with_power(db, codes: List[str], start_date: str, end_date: str) -> None:
    store = IntradayDataStore(db)
    failed = []
    cached_count = 0
    downloaded_count = 0
    worker_count = _intraday_worker_count()
    logger.info('5分钟K线下载并发数: %s', worker_count)

    # TDX uses an independent connection per task, so threads provide real
    # network concurrency without the startup cost and session collisions of
    # multiple Baostock child processes.
    pending_codes = []
    for code in codes:
        try:
            cached = store.load(
                code, f'{start_date} 00:00:00', f'{end_date} 23:59:59', '5'
            )
            if not cached.empty:
                cached_count += 1
                _update_intraday_download_status(
                    completed_codes=cached_count, cached_codes=cached_count,
                    current_code=code,
                )
            else:
                pending_codes.append(code)
        except Exception as exc:
            failed.append(f'{code}: 本地缓存检查失败: {exc}')

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    _fetch_intraday_frame_worker,
                    (code, start_date, end_date, '5'),
                ): code
                for code in pending_codes
            }
            for future in as_completed(futures):
                code = futures[future]
                try:
                    frame, source = future.result()
                    if frame.empty:
                        raise IntradayDataUnavailable(
                            f'{code} 在指定区间没有返回5分钟数据'
                        )
                    store._store(code, frame, '5', source=source)
                    downloaded_count += 1
                except Exception as exc:
                    failed.append(f'{code}: {exc}')
                    logger.warning('5分钟K线下载失败 %s: %s', code, exc)
                _update_intraday_download_status(
                    completed_codes=cached_count + downloaded_count + len(failed),
                    cached_codes=cached_count, downloaded_codes=downloaded_count,
                    failed_codes=len(failed), current_code=code,
                )
    except Exception as exc:
        logger.exception('5分钟K线下载任务失败')
        failed.append(str(exc))

    remaining = check_intraday_completeness(db).get('missingStockCount', 0)
    if remaining:
        _update_intraday_download_status(
            status='failed', phase='failed', failed_codes=max(len(failed), int(remaining)),
            message=f'下载结束，仍有 {remaining} 只股票缺少5分钟K线', current_code=None,
        )
    else:
        _update_intraday_download_status(
            status='completed', phase='completed', message='5分钟K线下载完成', current_code=None,
        )


class IntradayDataUnavailable(RuntimeError):
    """Raised when historical intraday bars cannot be loaded or fetched."""


def _fetch_baostock_worker(args):
    """Fetch one stock in a child process with a bounded socket lifetime."""
    code, start_date, end_date, interval = args
    try:
        return IntradayDataStore._fetch_baostock(
            code, start_date, end_date, interval,
            timeout_seconds=_baostock_timeout(),
        )
    except Exception as first_error:
        # Baostock can stall when a request spans too many rows. Retry the
        # same stock in month-sized pieces so one bad large response cannot
        # hold a worker forever.
        logger.warning('%s 大区间查询失败，拆分日期重试: %s', code, first_error)
        frames = []
        for chunk_start, chunk_end in _date_chunks(start_date, end_date):
            frame = IntradayDataStore._fetch_baostock(
                code, chunk_start, chunk_end, interval,
                timeout_seconds=_baostock_timeout(),
            )
            if not frame.empty:
                frames.append(frame)
        if not frames:
            raise first_error
        return pd.concat(frames, ignore_index=True).drop_duplicates(
            subset=['timestamp']
        ).sort_values('timestamp').reset_index(drop=True)


def _fetch_intraday_frame_worker(args):
    """Fetch a complete interval frame without touching the database."""
    code, start_date, end_date, interval = args
    if str(interval) == '5':
        try:
            frame = _fetch_tdx_with_retry(code, start_date, end_date)
            if frame.empty:
                raise IntradayDataUnavailable('TDX未返回历史数据')
            return frame, 'tdx'
        except Exception as tdx_error:
            # Keep Baostock as a correctness-preserving fallback when a TDX
            # node is temporarily unavailable. Its session lock is safe here.
            try:
                return IntradayDataStore._fetch_baostock(
                    code, start_date, end_date, interval,
                    timeout_seconds=_baostock_timeout(),
                ), 'baostock'
            except Exception as baostock_error:
                raise IntradayDataUnavailable(
                    f'TDX: {tdx_error}; Baostock: {baostock_error}'
                ) from tdx_error
    return IntradayDataStore._fetch_baostock(
        code, start_date, end_date, interval,
        timeout_seconds=_baostock_timeout(),
    ), 'baostock'


def _fetch_tdx_with_retry(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    last_error = None
    for attempt in range(2):
        try:
            return IntradayDataStore._fetch_tdx(code, start_date, end_date)
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                continue
    raise last_error


class IntradayDataStore:
    TABLE = "stock_intraday_kline"

    def __init__(self, db_manager=None):
        self.db = db_manager or get_global_db()
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.db.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE} (
                code TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                interval TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                amount REAL,
                source TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (code, timestamp, interval)
            )
            """
        )
        self.db.connect().commit()

    def load(self, code: str, start_date: str, end_date: str,
             interval: str = "5") -> pd.DataFrame:
        rows = self.db.query(
            f"""SELECT timestamp, open, high, low, close, volume, amount
                FROM {self.TABLE}
                WHERE code = ? AND interval = ? AND timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp ASC""",
            (str(code).zfill(6), str(interval), start_date, end_date),
        )
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        return frame.dropna(subset=["timestamp"]).reset_index(drop=True)

    def load_or_fetch(self, code: str, start_date: str, end_date: str,
                      interval: str = "5") -> pd.DataFrame:
        cached = self.load(code, start_date, end_date, interval)
        if not cached.empty:
            return cached
        return self.fetch_and_store(code, start_date, end_date, interval)

    def fetch_realtime_and_store(
        self,
        code: str,
        interval: str = "5",
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """Fetch the provider's recent minute bars and persist the requested slice.

        This path is intentionally separate from ``fetch_and_store``.  The
        latter is for complete historical downloads, while Eastmoney's minute
        endpoint supplies a rolling recent window that is appropriate for
        intraday monitoring only.
        """
        normalized_code = str(code).zfill(6)
        errors = []

        def requested_slice(frame: Optional[pd.DataFrame]) -> pd.DataFrame:
            result = self._normalize(frame)
            if result.empty:
                return result
            if start_time is not None:
                result = result.loc[
                    result["timestamp"] >= pd.Timestamp(start_time)
                ].copy()
            if end_time is not None:
                result = result.loc[
                    result["timestamp"] <= pd.Timestamp(end_time)
                ].copy()
            return result.reset_index(drop=True)

        normalized = pd.DataFrame()
        source = ""
        try:
            import akshare as ak

            normalized = requested_slice(call_with_timeout(
                ak.stock_zh_a_hist_min_em,
                symbol=normalized_code,
                period=str(interval),
                adjust="",
            ))
            source = "akshare_realtime"
        except Exception as exc:
            errors.append(f"akshare: {exc}")

        if normalized.empty and str(interval) == "5":
            start_date = pd.Timestamp(start_time or datetime.now()).strftime("%Y-%m-%d")
            end_date = pd.Timestamp(end_time or datetime.now()).strftime("%Y-%m-%d")
            try:
                normalized = requested_slice(
                    self._fetch_tdx(normalized_code, start_date, end_date)
                )
                source = "tdx_realtime"
            except Exception as exc:
                errors.append(f"tdx: {exc}")
        if normalized.empty and str(interval) == "5":
            try:
                normalized = requested_slice(self._fetch_sina_5(normalized_code))
                source = "sina_realtime"
            except Exception as exc:
                errors.append(f"sina: {exc}")
        if normalized.empty and str(interval) == "5":
            start_date = pd.Timestamp(start_time or datetime.now()).strftime("%Y-%m-%d")
            end_date = pd.Timestamp(end_time or datetime.now()).strftime("%Y-%m-%d")
            try:
                normalized = requested_slice(
                    self._fetch_baostock(
                        normalized_code, start_date, end_date, "5",
                        timeout_seconds=_baostock_timeout(),
                    )
                )
                source = "baostock_realtime"
            except Exception as exc:
                errors.append(f"baostock: {exc}")
        if normalized.empty:
            detail = "; ".join(errors) or "行情源没有返回指定时间段"
            raise IntradayDataUnavailable(
                f"无法获取 {normalized_code} 的实时{interval}分钟数据: {detail}"
            )
        self._store(normalized_code, normalized, str(interval), source=source)
        return normalized.reset_index(drop=True)

    def fetch_and_store(self, code: str, start_date: str, end_date: str,
                        interval: str = "5") -> pd.DataFrame:
        errors = []
        if str(interval) == '5':
            try:
                frame = _fetch_tdx_with_retry(code, start_date, end_date)
                normalized = self._normalize(frame)
                if not normalized.empty:
                    self._store(code, normalized, interval, source='tdx')
                    return normalized
            except Exception as exc:
                errors.append(f"tdx: {exc}")

        if str(interval) in {"5", "15", "30", "60"}:
            try:
                frame = self._fetch_baostock(
                    code, start_date, end_date, interval,
                    timeout_seconds=_baostock_timeout(),
                )
                normalized = self._normalize(frame)
                if not normalized.empty:
                    self._store(code, normalized, interval, source="baostock")
                    return normalized
            except Exception as exc:
                errors.append(f"baostock: {exc}")

            # Eastmoney/AkShare only returns a recent rolling window for
            # 5-minute A-share history. Storing it here would falsely mark a
            # full historical download as complete, so Baostock is the only
            # accepted source for historical minute bars.
            detail = "; ".join(errors)
            raise IntradayDataUnavailable(
                f"无法获取 {str(code).zfill(6)} 的完整历史{interval}分钟数据: {detail}"
            )

        try:
            import akshare as ak
            frame = ak.stock_zh_a_hist_min_em(
                symbol=str(code).zfill(6),
                period=str(interval),
                adjust="qfq",
                start_date=self._api_datetime(start_date),
                end_date=self._api_datetime(end_date),
            )
        except Exception as exc:
            errors.append(f"akshare: {exc}")
            detail = "; ".join(errors)
            raise IntradayDataUnavailable(
                f"无法获取 {str(code).zfill(6)} 的历史{interval}分钟数据: {detail}"
            ) from exc

        normalized = self._normalize(frame)
        if normalized.empty:
            raise IntradayDataUnavailable(
                f"{str(code).zfill(6)} 在 {start_date} 至 {end_date} 没有历史{interval}分钟数据"
            )
        self._store(code, normalized, interval, source="akshare")
        return normalized

    @staticmethod
    def _fetch_tdx(code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Read complete 5-minute history by paging the TDX quote server."""
        try:
            from pytdx.hq import TdxHq_API
        except ImportError as exc:
            raise IntradayDataUnavailable('未安装 pytdx') from exc

        hosts = _tdx_hosts()
        api = TdxHq_API()
        connected = False
        for host, port in hosts:
            if api.connect(host, port, time_out=5):
                connected = True
                break
        if not connected:
            raise IntradayDataUnavailable(
                'TDX行情服务器全部连接失败: '
                + ', '.join(f'{h}:{p}' for h, p in hosts)
            )

        normalized_code = str(code).zfill(6)
        market = 1 if normalized_code.startswith(('5', '6', '688', '689')) else 0
        start_text = f'{str(start_date)[:10]} 00:00:00'
        end_text = f'{str(end_date)[:10]} 23:59:59'
        records = []
        page_size = 800
        max_pages = 128
        try:
            for page in range(max_pages):
                bars = api.get_security_bars(
                    0, market, normalized_code, page * page_size, page_size
                )
                if not bars:
                    break
                records.extend(bars)
                oldest = str(bars[0].get('datetime') or '')
                if oldest and oldest <= start_text:
                    break
                if len(bars) < page_size:
                    break
        finally:
            api.disconnect()

        if not records:
            return pd.DataFrame()
        frame = pd.DataFrame(records)
        if 'datetime' not in frame.columns:
            return pd.DataFrame()
        frame['timestamp'] = pd.to_datetime(frame['datetime'], errors='coerce')
        frame = frame[
            (frame['timestamp'] >= pd.Timestamp(start_text))
            & (frame['timestamp'] <= pd.Timestamp(end_text))
        ].copy()
        if frame.empty:
            return pd.DataFrame()
        frame = frame.rename(columns={'vol': 'volume'})
        if 'amount' not in frame.columns:
            frame['amount'] = 0.0
        for column in ('open', 'high', 'low', 'close', 'volume', 'amount'):
            frame[column] = pd.to_numeric(frame[column], errors='coerce')
        return frame[
            ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'amount']
        ].dropna(subset=['timestamp', 'open', 'high', 'low', 'close', 'volume']) \
            .drop_duplicates(subset=['timestamp']) \
            .sort_values('timestamp').reset_index(drop=True)

    @staticmethod
    def _fetch_sina_5(code: str, datalen: int = 240) -> pd.DataFrame:
        """Recent 5-minute bars from Sina's public kline endpoint.

        Fallback leg added 2026-10-05: Eastmoney blocks this host's IP for
        minute bars, TDX returns empty pages and Baostock only publishes
        same-day minute bars after the close. Sina was verified working from
        this host on 2026-09-13 (backfill script 49). Bar timestamps follow
        the store's bar-end convention (09:35..15:00) and volume arrives in
        the store's unit (手), both confirmed against existing rows.
        """
        import json as _json
        import time as _time

        import requests as _requests

        normalized_code = str(code).zfill(6)
        symbol = ("sh" if normalized_code.startswith(("60", "68")) else "sz") + normalized_code
        url = (
            "https://quotes.sina.cn/cn/api/jsonp_v2.php/var/"
            f"CN_MarketDataService.getKLineData?symbol={symbol}"
            f"&scale=5&ma=no&datalen={int(datalen)}"
        )
        headers = {
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0",
        }
        _time.sleep(0.1)
        response = _requests.get(url, headers=headers, timeout=8)
        response.raise_for_status()
        text = response.text
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            return pd.DataFrame()
        bars = _json.loads(text[start:end + 1])
        if not bars:
            return pd.DataFrame()
        frame = pd.DataFrame(bars)
        if "day" not in frame.columns:
            return pd.DataFrame()
        frame = frame.rename(columns={"day": "timestamp"})
        if "amount" not in frame.columns:
            frame["amount"] = 0.0
        for column in ("open", "high", "low", "close", "volume", "amount"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        return frame[
            ["timestamp", "open", "high", "low", "close", "volume", "amount"]
        ].dropna(subset=["timestamp", "open", "high", "low", "close", "volume"]) \
            .drop_duplicates(subset=["timestamp"]) \
            .sort_values("timestamp").reset_index(drop=True)

    @staticmethod
    def _fetch_baostock(code: str, start_date: str, end_date: str,
                        interval: str, timeout_seconds: Optional[float] = None) -> pd.DataFrame:
        import baostock as bs
        import baostock.common.context as baostock_context

        market = "sh" if str(code).zfill(6).startswith(("5", "6", "688")) else "sz"
        fields = "date,time,code,open,high,low,close,volume,amount"
        rows = []
        # The in-process caller still serializes sessions. Download workers
        # use separate processes and therefore do not share this lock.
        with _BAOSTOCK_LOCK:
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                login = bs.login()
                if login.error_code != "0":
                    raise IntradayDataUnavailable(
                        f"Baostock登录失败: {login.error_msg}"
                    )
                socket = getattr(baostock_context, 'default_socket', None)
                if socket is not None and timeout_seconds:
                    socket.settimeout(float(timeout_seconds))
                completed = False
                try:
                    result = bs.query_history_k_data_plus(
                        f"{market}.{str(code).zfill(6)}",
                        fields,
                        start_date=str(start_date)[:10],
                        end_date=str(end_date)[:10],
                        frequency=str(interval),
                        adjustflag="2",
                    )
                    if result.error_code != "0":
                        raise IntradayDataUnavailable(
                            f"Baostock查询失败: {result.error_msg}"
                        )
                    while result.next():
                        rows.append(result.get_row_data())
                    completed = True
                finally:
                    if completed:
                        bs.logout()
                    elif socket is not None:
                        try:
                            socket.close()
                        except OSError:
                            pass

        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows, columns=fields.split(","))
        frame["timestamp"] = pd.to_datetime(
            frame["time"].astype(str).str[:14],
            format="%Y%m%d%H%M%S",
            errors="coerce",
        )
        return frame.rename(columns={"成交量": "volume", "成交额": "amount"})[
            ["timestamp", "open", "high", "low", "close", "volume", "amount"]
        ]

    def _store(self, code: str, frame: pd.DataFrame, interval: str,
               source: str = "akshare") -> None:
        rows = []
        now = datetime.now().isoformat(timespec="seconds")
        for item in frame.itertuples(index=False):
            rows.append((
                str(code).zfill(6), item.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                str(interval), float(item.open), float(item.high), float(item.low),
                float(item.close), float(item.volume), float(item.amount), source, now,
            ))
        with _INTRADAY_STORE_LOCK:
            connection = self.db.connect()
            for attempt in range(5):
                try:
                    connection.executemany(
                        f"""INSERT OR REPLACE INTO {self.TABLE}
                            (code, timestamp, interval, open, high, low, close, volume, amount,
                             source, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        rows,
                    )
                    connection.commit()
                    return
                except sqlite3.OperationalError as exc:
                    connection.rollback()
                    locked = 'database is locked' in str(exc).lower()
                    if not locked or attempt == 4:
                        raise
                    time.sleep(0.1 * (2 ** attempt))

    @staticmethod
    def _api_datetime(value: str) -> str:
        text = str(value)
        return text if " " in text else f"{text} 09:30:00"

    @staticmethod
    def _normalize(frame: Optional[pd.DataFrame]) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame()
        aliases = {
            "时间": "timestamp", "日期": "timestamp", "datetime": "timestamp",
            "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
            "成交量": "volume", "成交额": "amount",
        }
        result = frame.rename(columns=aliases).copy()
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        if not required.issubset(result.columns):
            return pd.DataFrame()
        if "amount" not in result.columns:
            result["amount"] = 0.0
        result["timestamp"] = pd.to_datetime(result["timestamp"], errors="coerce")
        for column in ("open", "high", "low", "close", "volume", "amount"):
            result[column] = pd.to_numeric(result[column], errors="coerce")
        result = result.dropna(subset=["timestamp", "open", "high", "low", "close"])
        result = result.sort_values("timestamp").drop_duplicates("timestamp")
        return result[["timestamp", "open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)
