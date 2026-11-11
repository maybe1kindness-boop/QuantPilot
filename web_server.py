"""
Web 服务器 - A股量化选股系统前端
"""
from trading.strategy_runner import StrategyRunner
from flask import Flask, render_template, jsonify, request, send_from_directory, make_response
from flask_socketio import SocketIO, emit
import json
import sys
import math
from pathlib import Path
from datetime import datetime
from utils.trade_date_utils import is_trading_day, get_previous_trading_day
from datetime import datetime as dt, timedelta
import pandas as pd
import numpy as np
import logging
import os
import traceback
import sqlite3
from json import JSONEncoder

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 自定义JSON编码器，处理numpy类型和NaN值
class NumpyEncoder(JSONEncoder):
    """自定义JSON编码器，处理numpy类型和NaN值"""
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            # 处理NaN和Inf值，转换为null或0
            if np.isnan(obj):
                return None  # NaN转换为null
            elif np.isinf(obj):
                return None  # Inf转换为null
            else:
                return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, pd.Timestamp):
            return obj.strftime('%Y-%m-%d %H:%M:%S')
        return super().default(obj)
    
    def encode(self, o):
        """重写encode方法，处理Python原生的float NaN和Inf"""
        result = super().encode(o)
        # 替换JSON中的NaN、Infinity和-Infinity为null
        result = result.replace('NaN', 'null')
        result = result.replace('Infinity', 'null')
        result = result.replace('-Infinity', 'null')
        return result


def clean_data_for_json(obj):
    """
    递归清理数据中的NaN和Inf值，确保可以序列化为JSON
    
    参数:
        obj: 任意Python对象（dict, list, float等）
    
    返回:
        清理后的对象，所有NaN/Inf值转换为None
    """
    if isinstance(obj, dict):
        # 递归处理字典
        return {k: clean_data_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        # 递归处理列表
        return [clean_data_for_json(item) for item in obj]
    elif isinstance(obj, float):
        # 处理Python原生float
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, np.floating):
        # 处理numpy float
        if np.isnan(obj) or np.isinf(obj):
            return None
        return float(obj)
    elif isinstance(obj, np.integer):
        # 处理numpy int
        return int(obj)
    elif isinstance(obj, np.bool_):
        # 处理numpy bool
        return bool(obj)
    elif isinstance(obj, np.ndarray):
        # 处理numpy数组
        return clean_data_for_json(obj.tolist())
    elif isinstance(obj, pd.Timestamp):
        # 处理pandas时间戳
        return obj.strftime('%Y-%m-%d %H:%M:%S')
    else:
        return obj

# 添加项目根目录到路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from utils.db_manager import DBManager
from strategy.strategy_registry import get_registry
from main import QuantSystem
import threading
from utils.selection_record_manager import SelectionRecordManager
from utils.db_initializer import init_databases_if_needed
from utils.stock_filter import StockFilter
from utils.data_collection_service import get_data_collection_service
from utils.kline_initializer import KlineInitializer
from trading.intraday_data import (
    check_intraday_completeness,
    get_intraday_download_progress,
    start_intraday_download,
)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from stock_analyzer import StockAnalyzer
from utils.strategy_name_mapper import STRATEGY_NAME_MAP, get_chinese_name
from paper_trading.watchlist import WATCHLIST_FILE, read_watchlist, write_watchlist

app = Flask(__name__,
            template_folder='web/templates',
            static_folder='web/static')

# 配置JSON编码器
app.json_encoder = NumpyEncoder


@app.after_request
def disable_api_cache(response):
    """API responses must reflect the currently running desktop engine."""
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

# 初始化SocketIO（配置长连接参数以支持长时间的回测任务）
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='threading',
    ping_timeout=3600,  # 1小时 ping 超时
    ping_interval=60,   # 60秒 ping 间隔
    max_http_buffer_size=int(1e8)  # 100MB 缓冲区
)

# ==================== 日志配置 ====================
# 使用新的日志配置模块
from utils.log_config import LogConfig, get_logger
from utils.paths import get_data_dir, get_logs_dir

# 初始化日志系统
LogConfig.setup_logging(log_dir=get_logs_dir(), log_file="app.log")

# 获取应用日志记录器
logger = get_logger(__name__)
logger.info("=" * 60)
logger.info("Web服务器启动")
logger.info("=" * 60)


# 初始化数据库（确保所有表都已创建）
logger.info("初始化数据库...")
init_databases_if_needed(get_data_dir())
logger.info("数据库初始化完成")

# 检查并添加缺失的数据库列
logger.info("检查数据库模式...")
from utils.db_migration_helper import ensure_database_schema
ensure_database_schema()
logger.info("数据库模式检查完成")

# 导入全局数据库管理器
from utils.global_db import get_global_db
from utils.stock_theme_service import StockThemeService

# 全局实例
db_manager = get_global_db()
stock_theme_service = StockThemeService(db_manager)
registry = get_registry("config/strategy_params.yaml")
# 注释掉QuantSystem初始化，避免数据库初始化错误
# quant_system = QuantSystem("config/config.yaml")
selection_record_manager = SelectionRecordManager()
stock_analyzer = StockAnalyzer()
data_collection_service = get_data_collection_service(get_data_dir())

# The web server owns one paper-trading engine for both API requests and the
# intraday scheduler. The desktop shell can still pass an engine when it has
# to bootstrap a local backend.
quantpilot_screen_engine = None
_quantpilot_engine_lock = threading.RLock()

# 初始化K线初始化器
from utils.akshare_fetcher import AKShareFetcher
akshare_fetcher = AKShareFetcher(get_data_dir())
kline_initializer = KlineInitializer(db_manager, akshare_fetcher)

# 初始化参数锁定机制
from strategy.param_lock import get_param_lock
param_lock = get_param_lock("config/strategy_params.yaml")

# 初始化参数追踪机制
from strategy.param_tracker import get_param_tracker
param_tracker = get_param_tracker("config/strategy_params.yaml")

# 加载策略
logger.info("正在加载策略...")
try:
    registry.auto_register_from_directory("strategy")
    logger.info(f"已加载 {len(registry.strategies)} 个策略")
except Exception as e:
    logger.error(f"加载策略失败: {str(e)}")
    import traceback
    logger.error(traceback.format_exc())

# 注册trading蓝图
from trading.routes import trading_bp
app.register_blueprint(trading_bp, url_prefix='/api/trading')
logger.info("已注册trading蓝图")

# 注册QuantPilot蓝图
from trading.routes import khunter_bp
from utils.strategy_config_manager import StrategyConfigManager
app.register_blueprint(khunter_bp)
logger.info("已注册QuantPilot蓝图")

# 全局更新状态
update_status = {
    'running': False,
    'progress': 0,
    'total': 0,
    'success': 0,
    'failed': 0,
    'message': '',
    'start_time': None,
    'end_time': None
}


@app.route('/')
def index():
    """主页"""
    # 无缓存头时浏览器/内嵌 WebView 会启发式缓存旧页面(内联脚本改动不生效),统一走再验证。
    response = make_response(render_template('quant-prototype.html'))
    response.headers['Cache-Control'] = 'no-cache'
    return response


QUANTPILOT_WATCHLIST_FILE = WATCHLIST_FILE
QUANTPILOT_GROUPS_FILE = Path('runtime/data/quantpilot_groups.json')
_quantpilot_watchlist_lock = threading.Lock()
_quantpilot_groups_lock = threading.Lock()


def _read_quantpilot_watchlist():
    """读取 QuantPilot 自选股；文件不存在时返回空列表。"""
    try:
        return read_watchlist()
    except Exception as exc:
        logger.warning(f'读取 QuantPilot 自选股失败: {exc}')
        return []


def _write_quantpilot_watchlist(items):
    try:
        write_watchlist(items)
    except Exception:
        raise


def _read_quantpilot_groups():
    try:
        if not QUANTPILOT_GROUPS_FILE.exists():
            return []
        with QUANTPILOT_GROUPS_FILE.open('r', encoding='utf-8') as file:
            groups = json.load(file)
        return groups if isinstance(groups, list) else []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f'读取 QuantPilot 分组失败: {exc}')
        return []


def _write_quantpilot_groups(groups):
    QUANTPILOT_GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = QUANTPILOT_GROUPS_FILE.with_name(
        f'.{QUANTPILOT_GROUPS_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp'
    )
    try:
        with temp_file.open('w', encoding='utf-8') as file:
            json.dump(sorted(set(groups)), file, ensure_ascii=False, indent=2)
        temp_file.replace(QUANTPILOT_GROUPS_FILE)
    finally:
        temp_file.unlink(missing_ok=True)


@app.route('/api/quantpilot/watchlist', methods=['GET'])
def get_quantpilot_watchlist():
    """获取 QuantPilot 自选股，并补充基础资料和最新收盘价。"""
    try:
        items = _read_quantpilot_watchlist()
        result = []
        for item in items:
            raw_code = str(item.get('code', '')).strip()
            code = raw_code.zfill(6)
            if not raw_code or not code.isdigit() or len(code) != 6:
                continue
            basic = db_manager.query_one(
                'SELECT name, industry FROM stock_basic WHERE code = ?', (code,)
            ) or {}
            quote = db_manager.query_one(
                'SELECT close, date FROM stock_kline WHERE code = ? ORDER BY date DESC LIMIT 2',
                (code,)
            )
            previous = db_manager.query_one(
                'SELECT close FROM stock_kline WHERE code = ? ORDER BY date DESC LIMIT 1 OFFSET 1',
                (code,)
            )
            price = quote.get('close') if quote else item.get('price', 0)
            previous_close = previous.get('close') if previous else None
            change = item.get('change', 0)
            if previous_close and price is not None:
                change = (float(price) - float(previous_close)) / float(previous_close) * 100
            result.append({
                **item,
                'code': code,
                'name': basic.get('name') or item.get('name') or code,
                'industry': basic.get('industry') or item.get('industry') or '未分类',
                'price': float(price or 0),
                'change': float(change or 0),
                'quote_date': quote.get('date') if quote else item.get('quote_date'),
            })
        groups = sorted(set(_read_quantpilot_groups()) | {
            item.get('group', '待观察') for item in result if item.get('group')
        })
        return jsonify({'success': True, 'data': {'items': result, 'groups': groups}})
    except Exception as exc:
        logger.exception('获取 QuantPilot 自选股失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/watchlist', methods=['POST'])
def save_quantpilot_watchlist_item():
    """新增或更新 QuantPilot 自选股。"""
    try:
        payload = request.get_json() or {}
        raw_code = str(payload.get('code', '')).strip()
        code = raw_code.zfill(6)
        if not raw_code or not code.isdigit() or len(code) != 6:
            return jsonify({'success': False, 'error': '股票代码必须是 6 位数字'}), 400
        with _quantpilot_watchlist_lock:
            items = _read_quantpilot_watchlist()
            item = next((entry for entry in items if entry.get('code') == code), None)
            if item is None:
                item = {
                    'code': code,
                    'added_at': datetime.now().strftime('%Y-%m-%d'),
                    'entry_pending': True,
                }
                items.append(item)
            item.update({
                key: payload[key]
                for key in ('name', 'industry', 'group', 'strategy', 'signal_strength', 'win_rate', 'expected_return')
                if key in payload
            })
            item['group'] = item.get('group') or '待观察'
            _write_quantpilot_watchlist(items)
        return jsonify({'success': True, 'data': item})
    except Exception as exc:
        logger.exception('保存 QuantPilot 自选股失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/watchlist/<code>', methods=['DELETE'])
def delete_quantpilot_watchlist_item(code):
    """删除 QuantPilot 自选股。"""
    code = str(code).strip().zfill(6)
    with _quantpilot_watchlist_lock:
        items = [item for item in _read_quantpilot_watchlist() if item.get('code') != code]
        _write_quantpilot_watchlist(items)
    return jsonify({'success': True, 'data': {'code': code}})


@app.route('/api/quantpilot/groups', methods=['POST'])
def create_quantpilot_group():
    """创建 QuantPilot 自选分组。分组由自选股条目的 group 字段自然维护。"""
    payload = request.get_json() or {}
    name = str(payload.get('name', '')).strip()
    if not name:
        return jsonify({'success': False, 'error': '分组名称不能为空'}), 400
    with _quantpilot_groups_lock:
        _write_quantpilot_groups(_read_quantpilot_groups() + [name])
    return jsonify({'success': True, 'data': {'name': name}})


def _get_quantpilot_screen_engine():
    """Return the server-wide paper-trading engine, creating it on first use."""
    global quantpilot_screen_engine
    with _quantpilot_engine_lock:
        if quantpilot_screen_engine is None:
            from paper_trading.engine import PaperTradingEngine
            quantpilot_screen_engine = PaperTradingEngine(
                config_file='config/config.yaml'
            )
    return quantpilot_screen_engine


def start_quantpilot_paper_trading():
    """Start the one server-owned simulated intraday trading scheduler."""
    with _quantpilot_engine_lock:
        engine = _get_quantpilot_screen_engine()
        if getattr(engine, '_server_mirror_mode', False):
            logger.info('桌面端处于服务器账本镜像模式，不启动本地模拟盘调度器')
            return engine
        message = engine.start_realtime()
        logger.info("服务端模拟盘调度器: %s", message)
        return engine


def web_paper_scheduler_enabled() -> bool:
    """Allow deployments to keep the web API separate from paper execution."""
    value = os.getenv("QUANTPILOT_WEB_PAPER_SCHEDULER", "false").strip().lower()
    return value not in {"0", "false", "no", "off"}


def web_paper_controls_enabled() -> bool:
    """Return whether this web process may alter paper-trading decisions."""
    value = os.getenv("QUANTPILOT_WEB_PAPER_CONTROLS", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


@app.before_request
def block_web_mutations():
    """The deployed Web API is a server-data viewer, never a strategy writer."""
    if web_paper_controls_enabled() or request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None

    logger.warning("拒绝 Web 写操作: %s %s", request.method, request.path)
    return jsonify({
        "success": False,
        "error": "服务器 Web 服务为只读展示；策略与消息只由服务器 worker 执行。",
    }), 403


@app.route('/api/quantpilot/automation-status', methods=['GET'])
def quantpilot_automation_status():
    """Return the runtime state of the server-owned paper-trading scheduler."""
    engine = _get_quantpilot_screen_engine()
    thread = getattr(engine, '_rt_thread', None)
    running = bool(getattr(engine, '_realtime_started', False))
    return jsonify({
        'success': True,
        'data': {
            'mode': 'paper',
            'scheduler_running': running,
            'scheduler_thread_alive': bool(thread and thread.is_alive()),
            'paper_control_mutations_enabled': web_paper_controls_enabled(),
            'poll_interval_minutes': int(engine.config.poll_interval_minutes),
            'trading_sessions': ['09:30-11:30', '13:00-15:00'],
        },
    })


@app.route('/api/quantpilot/paper-state', methods=['GET'])
def quantpilot_paper_state():
    """Return a complete simulated-ledger snapshot for the desktop SSH mirror."""
    try:
        engine = _get_quantpilot_screen_engine()
        tables = (
            'pt_position', 'pt_position_lot', 'pt_trade', 'pt_nav_snapshot',
            'pt_candidate_pool', 'pt_signal_log', 'pt_meta',
        )
        snapshot = {}
        with engine.dao._conn() as connection:
            for table in tables:
                snapshot[table] = [dict(row) for row in connection.execute(
                    f'SELECT * FROM {table}'
                ).fetchall()]
        kline_meta = db_manager.query_one(
            'SELECT MAX(date) AS latest_date FROM stock_kline') or {}
        return jsonify({'success': True, 'data': clean_data_for_json({
            'snapshot': snapshot,
            'latest_market_date': kline_meta.get('latest_date'),
        })})
    except Exception as exc:
        logger.exception('读取服务器模拟盘快照失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/cumulative-trades', methods=['GET'])
def quantpilot_cumulative_trades():
    """Per-stock cumulative buy/sell records behind the overview return card."""
    buy_sides = {'buy', 'add', 'basket_buy'}
    sell_sides = {'sell', 'basket_sell'}
    try:
        engine = _get_quantpilot_screen_engine()
        with engine.dao._conn() as connection:
            trades = [dict(row) for row in connection.execute(
                "SELECT trade_date, trade_time, trade_id, code, name, side, quantity, price, total "
                "FROM pt_trade WHERE status = 'filled' "
                "ORDER BY trade_date, trade_time, trade_id"
            ).fetchall()]
            positions = {
                str(item.get('code') or ''): item
                for item in engine.get_positions()
            }
        ledger = {}
        for trade in trades:
            side = str(trade.get('side') or '').lower()
            code = str(trade.get('code') or '')
            if not code or (side not in buy_sides and side not in sell_sides):
                continue
            entry = ledger.setdefault(code, {
                'name': '', 'first_buy_time': '', 'last_buy_time': '',
                'buy_qty': 0, 'buy_cash': 0.0, 'buy_price_sum': 0.0,
                'sell_qty': 0, 'sell_cash': 0.0, 'sell_price_sum': 0.0,
                'last_sell_time': '',
            })
            if trade.get('name'):
                entry['name'] = str(trade['name'])
            when = f"{trade.get('trade_date') or ''} {trade.get('trade_time') or ''}".strip()
            qty = int(trade.get('quantity') or 0)
            price = float(trade.get('price') or 0.0)
            total = float(trade.get('total') or 0.0)
            if side in buy_sides:
                entry['buy_qty'] += qty
                entry['buy_cash'] += total
                entry['buy_price_sum'] += price * qty
                if when:
                    if not entry['first_buy_time'] or when < entry['first_buy_time']:
                        entry['first_buy_time'] = when
                    if when > entry['last_buy_time']:
                        entry['last_buy_time'] = when
            else:
                entry['sell_qty'] += qty
                entry['sell_cash'] += total
                entry['sell_price_sum'] += price * qty
                if when and when > entry['last_sell_time']:
                    entry['last_sell_time'] = when
        records = []
        for code, entry in ledger.items():
            if not entry['buy_qty'] and not entry['sell_qty']:
                continue
            position = positions.get(code) or {}
            held_qty = int(position.get('quantity') or 0)
            latest_price = float(position.get('latest_price') or 0.0)
            stock_name = str(position.get('name') or '').strip() or entry['name'] or code
            market_value = held_qty * latest_price if latest_price > 0 else 0.0
            pnl = entry['sell_cash'] + market_value - entry['buy_cash']
            records.append({
                'code': code,
                'name': stock_name,
                'first_buy_time': entry['first_buy_time'] or '--',
                'last_buy_time': entry['last_buy_time'] or '--',
                'buy_price': round(entry['buy_price_sum'] / entry['buy_qty'], 3) if entry['buy_qty'] else None,
                'held_quantity': held_qty,
                'sell_price': round(entry['sell_price_sum'] / entry['sell_qty'], 3) if entry['sell_qty'] else None,
                'last_sell_time': entry['last_sell_time'] or '--',
                'sell_quantity': entry['sell_qty'],
                'cumulative_pnl': round(pnl, 2),
                'cumulative_pnl_pct': round(pnl / entry['buy_cash'] * 100, 2) if entry['buy_cash'] else 0.0,
            })
        records.sort(key=lambda item: item['first_buy_time'], reverse=True)
        return jsonify({'success': True, 'data': {
            'records': records,
            'total_pnl': round(sum(item['cumulative_pnl'] for item in records), 2),
            'record_count': len(records),
        }})
    except Exception as exc:
        logger.exception('读取累计成交汇总失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


def _review_common_data(engine):
    """复盘端点共用:全量成交(带腿)、净值快照、篮子清单、查价函数。"""
    from paper_trading import review
    with engine.dao._conn() as connection:
        trades = [dict(r) for r in connection.execute(
            "SELECT * FROM pt_trade ORDER BY trade_date, trade_time, trade_id").fetchall()]
        navs = [dict(r) for r in connection.execute(
            "SELECT * FROM pt_nav_snapshot ORDER BY trade_date").fetchall()]
        pool = [dict(r) for r in connection.execute(
            "SELECT * FROM pt_candidate_pool").fetchall()]
        signals = [dict(r) for r in connection.execute(
            "SELECT trade_date, generated_at, code, side, quantity, strength, message, source, acted "
            "FROM pt_signal_log ORDER BY generated_at").fetchall()]
    try:
        with engine.dao._conn() as _bear_conn:
            bear_rows = [dict(r) for r in _bear_conn.execute(
                "SELECT * FROM pt_bear_pool").fetchall()]
    except Exception:
        bear_rows = None  # 未同步(缺口①默认关)
    try:
        basket_codes = {c.strip() for c in str(
            engine.dao.get_meta("dividend_basket_codes") or "").split(",") if c.strip()}
    except Exception:
        basket_codes = set()

    def price_fn(code, date):
        row = db_manager.query_one(
            "SELECT close FROM stock_kline WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
            (str(code).zfill(6), date))
        return float(row["close"]) if row and row.get("close") else None

    classified = review.classify_legs(trades, basket_codes)
    return {"classified": classified, "navs": navs, "pool": pool, "signals": signals,
            "bear_rows": bear_rows, "basket_codes": basket_codes, "price_fn": price_fn}


# 持仓K线保鲜(桌面本地补充):本地K线镜像停在全量更新日(2026-09 冻结过整个月),
# 持仓/篮子股的复盘估值会长期用过期收盘价。用腾讯 ifzq 不复权日线对"当前持仓∪
# 红利篮子"做小范围增量补齐;复盘端点进入时异步触发,进程内 30 分钟冷却。
_HELD_KLINE_BACKFILL = {'at': 0.0}
_HELD_KLINE_BACKFILL_COOLDOWN = 1800.0


def _tx_stock_daily_raw(code):
    """腾讯 ifzq 个股日线(不复权) [(日期, 开, 收, 高, 低, 量)] 升序;失败返回 []。"""
    try:
        import requests
        sym = ('sh' if str(code).startswith('6') else 'sz') + str(code).zfill(6)
        url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
               f'?param={sym},day,,,640,')
        node = requests.get(url, timeout=10).json()['data'][sym]
        rows = [(str(r[0])[:10], float(r[1]), float(r[2]), float(r[3]),
                 float(r[4]), int(float(r[5])))
                for r in (node.get('day') or []) if len(r) >= 6]
        rows.sort()
        return rows
    except Exception:
        return []


def _tx_live_quote_prices(codes):
    """腾讯实时批量行情 {code: 最新价};失败返回 {}。用于日线末根交叉校验。"""
    try:
        import re
        import requests
        syms = ",".join(('sh' if str(c).startswith('6') else 'sz') + str(c).zfill(6)
                        for c in codes)
        text = requests.get(f'https://qt.gtimg.cn/q={syms}', timeout=10).text
        out = {}
        for m in re.finditer(r'v_(?:sh|sz)(\d{6})="([^"]+)"', text):
            try:
                out[m.group(1)] = float(m.group(2).split('~')[3])
            except (ValueError, IndexError):
                continue
        return out
    except Exception:
        return {}


def _backfill_holdings_klines(force=False):
    """全窗口改写持仓∪篮子股日线(INSERT OR REPLACE):拼接日收盘不一致且末根与实时
    行情也不吻合则跳过(防复权/异常序列);末根收盘与实时行情交叉校验(偏差>1% 跳过)。
    任何失败只记告警,估值退回库内现状。"""
    import time as _time
    if (not force and
            _time.time() - _HELD_KLINE_BACKFILL['at'] < _HELD_KLINE_BACKFILL_COOLDOWN):
        return
    _HELD_KLINE_BACKFILL['at'] = _time.time()
    try:
        engine = _get_quantpilot_screen_engine()
        codes = set()
        with engine.dao._conn() as connection:
            for row in connection.execute(
                    'SELECT code FROM pt_position WHERE quantity > 0').fetchall():
                codes.add(str(row['code']).zfill(6))
            meta = connection.execute(
                "SELECT value FROM pt_meta WHERE key='dividend_basket_codes'"
            ).fetchone()
        meta_value = dict(meta).get('value') if meta is not None else None
        if meta_value:
            codes.update(c.strip().zfill(6)
                         for c in str(meta_value).split(',') if c.strip())
        codes = sorted(codes)
        if not codes:
            return
        db_path = getattr(db_manager, 'db_path', None)
        if not db_path:
            return
        live = _tx_live_quote_prices(codes)
        inserted = 0
        latest = ''
        skipped = 0
        # 直连+with 自动提交:db_manager.execute 不落 commit,且其线程池会把
        # 未提交事务挂在已消亡线程的连接上(写锁悬挂),故此处绕开连接池。
        with sqlite3.connect(str(db_path)) as kconn:
            kconn.row_factory = sqlite3.Row
            for code in codes:
                try:
                    row = kconn.execute(
                        'SELECT MAX(date) AS d FROM stock_kline WHERE code=?',
                        (code,)).fetchone()
                    max_d = str(row['d'] or '') if row else ''
                    rows = _tx_stock_daily_raw(code)
                    if not rows:
                        continue
                    if max_d:
                        seam_fetch = next((r for r in rows if r[0] == max_d), None)
                        seam_db = kconn.execute(
                            'SELECT close AS c FROM stock_kline WHERE code=? AND date=?',
                            (code, max_d)).fetchone()
                        seam_bad = (seam_fetch is not None and seam_db
                                    and abs(seam_fetch[2] - float(seam_db['c'])) > 0.011)
                        feed_ok = (live.get(code)
                                   and abs(rows[-1][2] - live[code]) <= live[code] * 0.01)
                        if seam_bad:
                            if feed_ok:
                                # 拼接不一致但末根与实时行情吻合 → 库侧近端是坏值,照常改写自愈。
                                logger.info(
                                    '持仓K线保鲜:%s 拼接日 %s 库值 %s 与腾讯 %s 不一致,'
                                    '末根与实时吻合,按库侧坏值改写', code, max_d,
                                    seam_db['c'], seam_fetch[2])
                            else:
                                logger.warning(
                                    '持仓K线保鲜:%s 拼接日 %s 收盘不一致(库 %s vs 腾讯 %s),跳过',
                                    code, max_d, seam_db['c'], seam_fetch[2])
                                skipped += 1
                                continue
                    live_px = live.get(code)
                    if live_px and abs(rows[-1][2] - live_px) > live_px * 0.01:
                        logger.warning(
                            '持仓K线保鲜:%s 末根收盘 %s 与实时 %s 偏差>1%%(日线源脏窗口),跳过',
                            code, rows[-1][2], live_px)
                        skipped += 1
                        continue
                    # 改写窗口:库内最新日往前45自然日(含全部更新日期)——脏窗口写入的
                    # 近端坏值可被后续运行整段冲掉;更早历史仍受拼接校验保护、不重写。
                    if max_d:
                        try:
                            from datetime import datetime, timedelta
                            cut = (datetime.strptime(max_d, '%Y-%m-%d')
                                   - timedelta(days=45)).strftime('%Y-%m-%d')
                        except ValueError:
                            cut = max_d
                    else:
                        cut = ''
                    window = [r for r in rows if (not cut or r[0] >= cut)]
                    if not window:
                        continue
                    kconn.executemany(
                        'INSERT OR REPLACE INTO stock_kline'
                        '(code, date, open, high, low, close, volume)'
                        ' VALUES (?,?,?,?,?,?,?)',
                        # 拉取行结构 (日期,开,收,高,低,量) → 按列序 (开,高,低,收) 重排
                        [(code, d, o, h, low, c, v)
                         for d, o, c, h, low, v in window])
                    inserted += len(window)
                    latest = max(latest, rows[-1][0])
                except Exception as exc:
                    logger.warning('持仓K线保鲜:%s 失败: %s', code, exc)
        if inserted:
            logger.info('持仓K线保鲜:%d 只改写至 %s(跳过 %d 只),共 %d 行',
                        len(codes) - skipped, latest, skipped, inserted)
    except Exception as exc:
        logger.warning('持仓K线保鲜失败(复盘估值维持库内现状): %s', exc)


def _ensure_holdings_klines_async():
    import threading
    threading.Thread(target=_backfill_holdings_klines, daemon=True).start()


# 腾讯指数日线进程内缓存 + 当日落盘缓存:闸门/历史回放/归因共用一份。
_TX_INDEX_DAILY_CACHE = {}          # symbol -> (fetched_at, rows)
_TX_INDEX_DAILY_TTL = 600.0         # 日线序列盘中不变,10 分钟足够


def _tx_index_daily_disk_path():
    return os.path.join('runtime', 'data', 'tx_index_daily.json')


def _tx_index_daily_rows(symbol):
    """腾讯指数日线 (日期, 收盘) 升序列表;失败返回 None。

    取数层级:进程缓存 → 当日落盘缓存 → ifzq.gtimg.cn 日线单请求(640 根,
    亚秒级)→ 隔日落盘缓存兜底。akshare 的 stock_zh_index_daily_tx 需分页
    拉全量历史(30+ 次请求,桌面进程内可达分钟级),不用于请求时取数。
    其 amount 列为成交量(手)而非成交额,不得当作成交额使用。
    """
    import time as _time
    hit = _TX_INDEX_DAILY_CACHE.get(symbol)
    if hit and _time.time() - hit[0] < _TX_INDEX_DAILY_TTL:
        return hit[1]

    def _from_disk():
        try:
            with open(_tx_index_daily_disk_path(), 'r', encoding='utf-8') as f:
                return json.load(f) or {}
        except Exception:
            return {}

    entry = _from_disk().get(symbol) or {}
    today = _time.strftime('%Y-%m-%d')
    if entry.get('saved') == today and entry.get('rows'):
        rows = [(str(d)[:10], float(c)) for d, c in entry['rows']]
        _TX_INDEX_DAILY_CACHE[symbol] = (_time.time(), rows)
        return rows

    try:
        import requests
        url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
               f'?param={symbol},day,,,640,qfq')
        node = requests.get(url, timeout=10).json()['data'][symbol]
        raw = node.get('qfqday') or node.get('day') or []
        rows = sorted((str(r[0])[:10], float(r[2]))
                      for r in raw if len(r) >= 6)
    except Exception:
        rows = []
    if not rows and entry.get('rows'):
        # 网络失败:退回隔日落盘缓存(闸门/回放可用,最新一根或滞后一日)。
        rows = [(str(d)[:10], float(c)) for d, c in entry['rows']]
    if not rows:
        return None
    _TX_INDEX_DAILY_CACHE[symbol] = (_time.time(), rows)
    disk = _from_disk()
    disk[symbol] = {'saved': today,
                    'rows': [[d, c] for d, c in rows]}
    try:
        os.makedirs(os.path.dirname(_tx_index_daily_disk_path()), exist_ok=True)
        with open(_tx_index_daily_disk_path(), 'w', encoding='utf-8') as f:
            json.dump(disk, f, ensure_ascii=False)
    except Exception:
        logger.warning('腾讯指数日线落盘失败(不影响本次返回)')
    return rows


def _sh_index_daily_pairs():
    """上证指数日线 (日期, 收盘) 升序列表:sina 日线优先,腾讯日线兜底(带缓存);
    双源皆失败仅记一条告警并返回 None,闸门/环境维度随之降级。"""

    def _pairs(frame):
        try:
            rows = sorted((str(d)[:10], float(c))
                          for d, c in zip(frame['date'], frame['close']))
            return rows or None
        except Exception:
            return None

    try:
        import akshare as ak
        rows = _pairs(ak.stock_zh_index_daily(symbol='sh000001'))
        if rows:
            return rows
    except Exception:
        pass
    rows = _tx_index_daily_rows('sh000001')
    if rows:
        return rows
    logger.warning('上证指数日线 sina/腾讯双源均失败,年线闸门与环境维度将降级')
    return None


@app.route('/api/quantpilot/review/legs-nav', methods=['GET'])
def quantpilot_review_legs_nav():
    try:
        _ensure_holdings_klines_async()  # 持仓K线保鲜(冷却30分钟,异步不阻塞)
        engine = _get_quantpilot_screen_engine()
        common = _review_common_data(engine)
        from paper_trading import review
        initial = float(engine.account.initial_capital
                        if getattr(engine, "account", None) else 300000.0)
        data = review.legs_equity(common["classified"], common["navs"],
                                  common["price_fn"], initial_capital=initial)
        return jsonify({'success': True, 'data': data})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/review/attribution', methods=['GET'])
def quantpilot_review_attribution():
    from paper_trading import review
    try:
        _ensure_holdings_klines_async()  # 持仓K线保鲜(冷却30分钟,异步不阻塞)
        engine = _get_quantpilot_screen_engine()
        common = _review_common_data(engine)
        period = request.args.get('period', 'all')
        gates = {}
        sh_pairs = _sh_index_daily_pairs()
        if sh_pairs:
            gates = review.gate_states_from_closes(sh_pairs)
        rounds = review.round_trips(common["classified"])
        trading_days = [str(s.get("trade_date"))[:10] for s in common["navs"]]
        if period == 'all':
            start, end = "", ""
        else:
            from datetime import datetime, timedelta
            today = datetime.now()
            if period == 'week':
                anchor = today - timedelta(days=today.weekday())
            else:
                anchor = today.replace(day=1)
            start = anchor.strftime("%Y-%m-%d")
            end = today.strftime("%Y-%m-%d")
        data = review.attribution(rounds, gates, trading_days=trading_days,
                                  period_start=start, period_end=end)
        return jsonify({'success': True, 'data': data})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/review/plan/<date>', methods=['GET'])
def quantpilot_review_plan(date):
    try:
        engine = _get_quantpilot_screen_engine()
        common = _review_common_data(engine)
        from paper_trading import review
        import json as _json
        try:
            schedule = _json.loads(engine.dao.get_meta(
                f"basket_schedule_log_{str(date)[:10]}") or "{}")
        except Exception:
            schedule = {}
        cfg = getattr(engine, "config", None)
        config_view = {
            "initial_position_pct": getattr(cfg, "initial_position_pct", None),
            "bear_market_max_total_position_pct": 0.30,
            "max_positions": getattr(cfg, "max_positions", None),
            "stop_loss": getattr(cfg, "stop_loss", None),
            "hard_stop_loss": getattr(cfg, "hard_stop_loss", None),
            "take_profit": getattr(cfg, "take_profit", None),
            "second_take_profit": getattr(cfg, "second_take_profit", None),
            "max_hold_days": getattr(cfg, "max_hold_days", None),
        }
        data = review.plan_snapshot(str(date)[:10], common["pool"], common["signals"],
                                    schedule, config_view, bear_rows=common["bear_rows"])
        return jsonify({'success': True, 'data': data})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/review/plan-vs-actual/<date>', methods=['GET'])
def quantpilot_review_plan_vs_actual(date):
    try:
        engine = _get_quantpilot_screen_engine()
        common = _review_common_data(engine)
        from paper_trading import review
        positions = {str(p.code).zfill(6): {"code": p.code, "quantity": p.quantity}
                     for p in engine.account.positions.values()
                     if int(getattr(p, "quantity", 0) or 0) > 0}
        data = review.plan_vs_actual(str(date)[:10], common["pool"], common["signals"],
                                     common["classified"], positions)
        return jsonify({'success': True, 'data': data})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/review/latest-date', methods=['GET'])
def quantpilot_review_latest_date():
    """最近交易日(毫秒级,本地镜像库解析,零网络):复盘首屏默认日期用它,不等行情源。"""
    try:
        engine = _get_quantpilot_screen_engine()
        with engine.dao._conn() as connection:
            row = connection.execute(
                "SELECT MAX(trade_date) AS d FROM pt_nav_snapshot").fetchone()
        latest = str(row['d'] or '')[:10] if row else ''
        if not latest:
            r2 = db_manager.query_one('SELECT MAX(date) AS latest_date FROM stock_kline')
            latest = str((r2 or {}).get('latest_date') or '')[:10]
        return jsonify({'success': True, 'data': {'latest': latest or None}})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


# 大盘快照进程内缓存:同一桌面进程内重复打开复盘不重拉行情源(打开页面才算按需,无后台刷新)。
_REVIEW_MARKET_CACHE = {'key': None, 'at': 0.0, 'data': None}
_REVIEW_MARKET_CACHE_TTL = 180.0


@app.route('/api/quantpilot/review/market/<date>', methods=['GET'])
def quantpilot_review_market(date):
    """大盘视角市场快照(v2 补充):逐块独立降级,历史日期仅指数回放与闸门可回放。

    行情源(2026-10-18 起,本机 IP 被东财封禁,东财接口已全部移除):
    指数快照=新浪;指数日线(闸门/历史回放)=腾讯 ifzq(进程+落盘双层缓存);
    涨跌家数=乐咕。指数成交额序列与行业板块原为东财独有数据源,无替代源,恒降级显示缺省。
    """
    try:
        _ensure_holdings_klines_async()  # 持仓K线保鲜(冷却30分钟,异步不阻塞)
        import time as _time
        engine = _get_quantpilot_screen_engine()
        common = _review_common_data(engine)
        from paper_trading import review, market_review
        date_key = str(date)[:10]

        # date=latest:本地镜像净值快照即时解析最近交易日(零网络)。
        if date_key == "latest":
            date_key = max(
                (str(s.get('trade_date') or '')[:10] for s in common['navs']),
                default='') or date_key

        # 同一交易日 3 分钟内直接回缓存:检查必须先于一切行情源拉取,命中零等待。
        if (_REVIEW_MARKET_CACHE['key'] == date_key
                and _REVIEW_MARKET_CACHE['data'] is not None
                and _time.time() - _REVIEW_MARKET_CACHE['at'] < _REVIEW_MARKET_CACHE_TTL):
            return jsonify({'success': True, 'data': _REVIEW_MARKET_CACHE['data']})

        # 上证日线:腾讯 ifzq 单请求(亚秒级,进程+落盘双层缓存;东财接口已移除),
        # 年线闸门收盘序列与最新交易日判定共用一份。
        # 指数成交额序列块原用东财日线成交额列,无替代源,随东财移除永久降级。
        pairs = _tx_index_daily_rows('sh000001')
        if pairs is None:
            logger.warning('复盘市场快照:上证日线(腾讯)不可得,年线闸门将降级')

        # 年线闸门:上证日线本地重算;顺带取序列最新交易日。
        gate_info = None
        latest_trading_date = None
        if pairs:
            latest_trading_date = pairs[-1][0]
        if latest_trading_date is None:
            # 指数日线不可得时,退回本地镜像 K 线判定"当日/历史"(零网络依赖)。
            try:
                row = db_manager.query_one(
                    'SELECT MAX(date) AS latest_date FROM stock_kline')
                latest_trading_date = str(row.get('latest_date') or '')[:10] or None
            except Exception:
                latest_trading_date = None

        # 镜像净值不可得时(空库首次启动),退回序列解析最近交易日并复查缓存。
        if str(date)[:10] == "latest" and (date_key == "latest" or not common['navs']):
            date_key = latest_trading_date or date_key
            if (_REVIEW_MARKET_CACHE['key'] == date_key
                    and _REVIEW_MARKET_CACHE['data'] is not None
                    and _time.time() - _REVIEW_MARKET_CACHE['at'] < _REVIEW_MARKET_CACHE_TTL):
                return jsonify({'success': True, 'data': _REVIEW_MARKET_CACHE['data']})

        if pairs:
            gates = review.gate_states_from_closes(pairs)
            relevant = [p for p in pairs if p[0] <= date_key]
            # 非交易日(周末/假日)无当日状态,回放为该日所能见到的最近交易日闸门。
            gate_asof = relevant[-1][0] if relevant else None
            gate_info = (market_review.pick_gate_info(gates, relevant[-210:], gate_asof)
                         if gate_asof else None)

        # 历史日期(date < 序列最新交易日):指数回放与闸门可回放;涨跌家数仅当日。
        # 指数成交额序列与行业板块原为东财独有数据源,无替代源,恒降级。
        is_latest = True if latest_trading_date is None else date_key >= latest_trading_date

        def _num(value):
            try:
                f = float(value)
                return f if f == f else None  # NaN -> None
            except (TypeError, ValueError):
                return None

        def fetch_index_spot():
            # 新浪指数快照(东财快照接口已移除);新浪无成交额列,amount 恒缺省。
            try:
                import akshare as ak
                names = {'上证指数', '深证成指', '创业板指'}
                frame = ak.stock_zh_index_spot_sina()
                items = [{'name': str(r.get('名称') or ''),
                          'close': _num(r.get('最新价')),
                          'pct': _num(r.get('涨跌幅')),
                          'amount': _num(r.get('成交额'))}
                         for _, r in frame[frame['名称'].isin(names)].iterrows()]
                return items or None
            except Exception as exc:
                logger.warning('复盘市场快照:指数快照块降级(新浪源失败): %s', exc)
                return None

        def fetch_index_spot_replay():
            # 规格裁定回放:历史日期三大指数自腾讯日线(带缓存)取该日(或其前最近
            # 交易日)收盘与涨跌幅(= 收盘/前收 - 1,百分单位与实时快照一致;首日 → None)。
            # 腾讯 amount 列是成交量(手)而非成交额,不得充作成交额 → 恒 None。
            wanted = (('sh000001', '上证指数'), ('sz399001', '深证成指'),
                      ('sz399006', '创业板指'))

            def one(symbol, name):
                series = _tx_index_daily_rows(symbol) or []
                rows = [p for p in series if p[0] <= date_key]
                if not rows:
                    return None
                close = rows[-1][1]
                prev = rows[-2][1] if len(rows) >= 2 else None
                return {'name': name, 'close': close,
                        'pct': round((close / prev - 1.0) * 100, 4) if prev else None,
                        'amount': None}

            try:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=3) as pool:
                    items = [item for item in pool.map(lambda sn: one(*sn), wanted)
                             if item]
                return items or None
            except Exception as exc:
                logger.warning('复盘市场快照:历史指数回放块降级: %s', exc)
                return None

        def fetch_breadth():
            try:
                import akshare as ak
                legu = ak.stock_market_activity_legu()
                kv = {str(k): v for k, v in zip(legu['item'], legu['value'])}

                def pick(keyword):
                    key = next((k for k in kv if k == keyword), None) \
                        or next((k for k in kv if keyword in k), None)
                    val = _num(kv.get(key)) if key else None
                    return int(val) if val is not None else None

                return {'up': pick('上涨'), 'down': pick('下跌'),
                        'limit_up': pick('涨停'), 'limit_down': pick('跌停')}
            except Exception as exc:
                logger.warning('复盘市场快照:涨跌家数块降级: %s', exc)
                return None

        # 行业板块块原为东财独有数据源(THS 替代源在本机同样不可用),永久移除。

        # 成交额序列与行业板块块无替代数据源,不再注册;组装层按缺键降级为空。
        fetchers = {}
        if is_latest:
            fetchers['index_spot'] = fetch_index_spot
            fetchers['breadth'] = fetch_breadth
        else:
            # 规格裁定:历史日期指数与闸门可回放(指数走腾讯日线),涨跌家数仅当日。
            fetchers['index_spot'] = fetch_index_spot_replay
        # 行情源并行拉取:各块各自数秒,串行会叠加;并发后总耗时≈最慢单块。
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=4)
        try:
            futures = {name: pool.submit(fn) for name, fn in fetchers.items()}

            def _wrap(future):
                return future.result

            data = market_review.assemble_market_snapshot(
                {name: _wrap(fut) for name, fut in futures.items()}, date_key, gate_info)
        finally:
            pool.shutdown(wait=False)
        data['day_trades'] = market_review.summarize_day_trades(
            common['classified'], date_key)
        data['resolved_date'] = date_key
        _REVIEW_MARKET_CACHE.update(key=date_key, at=_time.time(), data=data)
        return jsonify({'success': True, 'data': data})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/external-environment', methods=['GET', 'POST'])
def quantpilot_external_environment():
    """Read or refresh the same external gate used by paper-trading orders."""
    try:
        engine = _get_quantpilot_screen_engine()
        snapshot = (
            engine.refresh_external_environment()
            if request.method == 'POST'
            else engine.get_external_environment()
        )
        return jsonify({'success': True, 'data': clean_data_for_json(snapshot)})
    except Exception as exc:
        logger.exception('外围环境状态获取失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


def _build_quantpilot_market_overview():
    """Build the overview from the same engine used by automatic trading."""
    engine = _get_quantpilot_screen_engine()
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    account = engine.account_overview()
    nav_series = engine.get_nav_series(days=400)
    snapshot_dates = {str(item.get('trade_date') or '')[:10] for item in nav_series}
    for snapshot in engine.dao.get_cash_only_nav_snapshots(account.get('initial_capital')):
        if snapshot['trade_date'] not in snapshot_dates:
            nav_series.append(snapshot)
    nav_series.sort(key=lambda item: str(item.get('trade_date') or ''))
    signals = engine.get_signals(today)
    trades = engine.get_trades(today)
    filled_trades = [
        trade for trade in trades
        if str(trade.get('status') or '').lower() == 'filled'
    ]

    current_nav = (
        float(account.get('total_value') or 0)
        / float(account.get('initial_capital') or 1)
    )
    if not nav_series or str(nav_series[-1].get('trade_date') or '') != today:
        nav_series.append({
            'trade_date': today,
            'cash': account.get('cash'),
            'positions_value': account.get('positions_value'),
            'total_value': account.get('total_value'),
            'nav': round(current_nav, 6),
            'drawdown': round(float(account.get('drawdown_pct') or 0) / 100, 6),
            'position_count': account.get('position_count'),
            'daily_pnl': None,
            'source': 'current_account',
        })

    # Sparse NAV snapshots are not a continuous return series. Cash-only days
    # are exact from the trade ledger; a held position without an EOD valuation
    # stays explicitly unknown for the chart to render as a gap.
    series_start = now.date() - timedelta(days=400)
    current_date = series_start
    trading_dates = []
    while current_date <= now.date():
        if engine.calendar.is_trading_day(current_date):
            trading_dates.append(current_date.isoformat())
        current_date += timedelta(days=1)
    current_nav_rows = {
        str(item.get('trade_date') or '')[:10]: item
        for item in nav_series
        if str(item.get('trade_date') or '')[:10]
    }
    daily_return_series = engine.dao.get_daily_nav_observations(
        account.get('initial_capital'), trading_dates, current_nav_rows,
    )

    latest_kline = db_manager.query_one(
        'SELECT MAX(date) AS latest_date FROM stock_kline'
    ) or {}
    # 桌面端本地日线库不随服务器每日更新(全市场更新是服务器16:00的任务),
    # 而账户本身是服务器镜像; "行情"日期优先取最近一次服务器同步带回的
    # 日线日期(26小时内视为新鲜), 否则回落本地库口径
    try:
        synced_market = json.loads(
            engine.dao.get_meta('server_latest_market_date') or '')
        synced_at = datetime.fromisoformat(
            str((synced_market or {}).get('synced_at') or ''))
        if (now - synced_at) < timedelta(hours=26):
            synced_date = str((synced_market or {}).get('date') or '')[:10]
            if synced_date:
                latest_kline = {'latest_date': synced_date}
    except (ValueError, TypeError):
        pass
    ptrade_execution = (
        engine.khunter_config.get('ptrade', {}).get('execution', {})
        if isinstance(engine.khunter_config, dict) else {}
    )
    live_enabled = bool(ptrade_execution.get('live_enabled')) and not bool(
        ptrade_execution.get('dry_run', True)
    )
    return {
        'generated_at': now.isoformat(timespec='seconds'),
        'today': today,
        'latest_market_date': latest_kline.get('latest_date'),
        'mode': '实盘' if live_enabled else '模拟盘',
        'account': account,
        'nav_series': nav_series,
        'daily_return_series': daily_return_series,
        'signals': signals,
        'counts': {
            'signals_today': len(signals),
            'trades_today': len(filled_trades),
            'positions': int(account.get('position_count') or 0),
        },
    }


@app.route('/api/quantpilot/market-overview', methods=['GET'])
def quantpilot_market_overview():
    """Return fresh market-overview data for desktop startup."""
    try:
        data = _build_quantpilot_market_overview()
        return jsonify({'success': True, 'data': clean_data_for_json(data)})
    except Exception as exc:
        logger.exception('市场总览状态获取失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


def _next_quantpilot_trade_date(engine, start_date):
    """Return the first trading day after start_date."""
    if isinstance(start_date, str):
        try:
            current = datetime.strptime(start_date[:10], '%Y-%m-%d').date()
        except ValueError:
            current = datetime.now().date()
    elif hasattr(start_date, 'year'):
        current = start_date
    else:
        current = datetime.now().date()
    for _ in range(15):
        current += timedelta(days=1)
        if engine.calendar.is_trading_day(current):
            return current.strftime('%Y-%m-%d')
    return current.strftime('%Y-%m-%d')


def _select_quantpilot_screen_strategy(engine, requested_name=''):
    strategies = engine.list_user_screen_strategies()
    requested_name = str(requested_name or '').strip()
    if requested_name:
        selected = next(
            (item for item in strategies if item.get('name') == requested_name), None
        )
        if selected is not None:
            return strategies, selected
    with_results = [item for item in strategies if item.get('results')]
    candidates = with_results or strategies
    selected = max(
        candidates,
        key=lambda item: str(item.get('updated_at') or ''),
        default={},
    )
    return strategies, selected


_FLOW_BUY_SIDES = {'buy', 'add', 'basket_buy'}
_FLOW_SELL_SIDES = {'sell', 'basket_sell'}


def _flow_totals_by_code(engine, codes):
    """每只票资金流口径的 买入总付/卖出总得/流水净持仓(与15:10推送同口径)。"""
    totals = {}
    codes = [str(code).zfill(6) for code in codes if code]
    if not codes:
        return totals
    placeholders = ','.join('?' for _ in codes)
    with engine.dao._conn() as connection:
        rows = connection.execute(
            f"SELECT code, side, quantity, total FROM pt_trade "
            f"WHERE status='filled' AND code IN ({placeholders})",
            tuple(codes),
        ).fetchall()
    for row in rows:
        code = str(row['code'] or '').zfill(6)
        side = str(row['side'] or '').lower()
        entry = totals.setdefault(
            code, {'buy_cash': 0.0, 'sell_cash': 0.0, 'held': 0})
        if side in _FLOW_BUY_SIDES:
            entry['buy_cash'] += float(row['total'] or 0.0)
            entry['held'] += int(row['quantity'] or 0)
        elif side in _FLOW_SELL_SIDES:
            entry['sell_cash'] += float(row['total'] or 0.0)
            entry['held'] -= int(row['quantity'] or 0)
    return totals


def _annotate_positions_flow_pnl(engine, positions):
    """持仓行补真实累计盈亏(卖出所得+余股估值−买入总付)及百分比对齐推送口径。

    流水净持仓与账本持仓不一致或无估值时不动该行,前端回退浮盈口径。
    """
    totals = _flow_totals_by_code(
        engine, [position.get('code') for position in positions])
    for position in positions:
        entry = totals.get(str(position.get('code') or '').zfill(6))
        if not entry or entry['buy_cash'] <= 0:
            continue
        quantity = int(position.get('quantity') or 0)
        latest_price = float(position.get('latest_price') or 0.0)
        if entry['held'] != quantity or quantity <= 0 or latest_price <= 0:
            continue
        pnl = entry['sell_cash'] + quantity * latest_price - entry['buy_cash']
        position['cumulative_pnl'] = round(pnl, 2)
        position['cumulative_pnl_pct'] = round(
            pnl / entry['buy_cash'] * 100, 2)
    return positions


def _build_quantpilot_automation_dashboard(strategy_name=''):
    engine = _get_quantpilot_screen_engine()
    now = datetime.now()
    if (engine.calendar.is_trading_day(now.date())
            and not engine.calendar.is_in_trading_session()):
        engine.refresh_position_valuation()
    strategies, selected = _select_quantpilot_screen_strategy(engine, strategy_name)
    today = now.strftime('%Y-%m-%d')
    default_execution_date = _next_quantpilot_trade_date(engine, today)
    watchlist = _read_quantpilot_watchlist()
    pending_by_code = {
        str(item.get('code', '')).zfill(6): item
        for item in watchlist
        if item.get('entry_pending')
    }

    raw_results = selected.get('results') if isinstance(selected, dict) else []
    raw_results = raw_results if isinstance(raw_results, list) else []
    result_by_code = {}
    candidates = []
    for raw in raw_results:
        code = str(raw.get('code', '')).strip().zfill(6)
        if not code.isdigit() or len(code) != 6:
            continue
        result_by_code[code] = raw
        basic = db_manager.query_one(
            'SELECT name, industry FROM stock_basic WHERE code = ?', (code,)
        ) or {}
        planned = pending_by_code.get(code)
        execution_date = (
            _next_quantpilot_trade_date(engine, planned.get('added_at'))
            if planned else default_execution_date
        )
        theme_factor = engine.theme_position_factor.evaluate(code, execution_date)
        candidates.append({
            'code': code,
            'name': basic.get('name') or raw.get('name') or code,
            'industry': basic.get('industry') or raw.get('industry') or '未分类',
            'price': float(raw.get('price') or 0),
            'change': float(raw.get('change') or 0),
            'reason': raw.get('reason') or selected.get('source') or '命中筛选条件',
            'strategy': selected.get('name') or '',
            'queued': bool(planned),
            'execution_date': execution_date,
            'status': '待次日判断' if planned else '待确认',
            'in_selected_strategy': True,
            'theme_score': theme_factor.get('score'),
            'theme_multiplier': theme_factor.get('multiplier'),
            'business_themes': theme_factor.get('themes') or [],
            'theme_reason': theme_factor.get('reason'),
        })

    for code, planned in pending_by_code.items():
        if code in result_by_code:
            continue
        basic = db_manager.query_one(
            'SELECT name, industry FROM stock_basic WHERE code = ?', (code,)
        ) or {}
        execution_date = _next_quantpilot_trade_date(engine, planned.get('added_at'))
        theme_factor = engine.theme_position_factor.evaluate(code, execution_date)
        candidates.append({
            'code': code,
            'name': basic.get('name') or planned.get('name') or code,
            'industry': basic.get('industry') or planned.get('industry') or '未分类',
            'price': float(planned.get('price') or 0),
            'change': float(planned.get('change') or 0),
            'reason': planned.get('strategy') or '已加入明日计划',
            'strategy': planned.get('strategy') or '',
            'queued': True,
            'execution_date': execution_date,
            'status': '待次日判断',
            'in_selected_strategy': False,
            'theme_score': theme_factor.get('score'),
            'theme_multiplier': theme_factor.get('multiplier'),
            'business_themes': theme_factor.get('themes') or [],
            'theme_reason': theme_factor.get('reason'),
        })

    overview = engine.account_overview()
    positions = _annotate_positions_flow_pnl(
        engine, engine.get_positions())
    trades = engine.get_trades(today)
    filled_trades = [
        trade for trade in trades
        if str(trade.get('status') or '').lower() == 'filled'
    ]
    # Rejected orders remain in the ledger for audit, but are not executions.
    recent_trades = [
        trade for trade in engine.get_trades()
        if str(trade.get('status') or '').lower() == 'filled'
    ]
    signals = engine.get_signals(today)
    activities = []
    for item in trades[:20]:
        activities.append({
            'time': item.get('trade_time') or item.get('trade_date') or '',
            'code': item.get('code'),
            'name': item.get('name'),
            'side': item.get('side'),
            'status': item.get('status'),
            'message': item.get('reject_reason') or item.get('signal_msg') or '已成交',
            'kind': 'trade',
        })
    for item in signals[:20]:
        activities.append({
            'time': item.get('generated_at') or '',
            'code': item.get('code'),
            'name': '',
            'side': item.get('side'),
            'status': '已执行' if item.get('acted') else '未执行',
            'message': item.get('message') or '条件检查完成',
            'kind': 'signal',
        })
    activities.sort(key=lambda item: str(item.get('time') or ''), reverse=True)

    external = engine.get_external_environment()
    external_decision = external.get('opening_decision', {})
    ptrade_execution = (
        engine.khunter_config.get('ptrade', {}).get('execution', {})
        if isinstance(engine.khunter_config, dict) else {}
    )
    live_enabled = bool(ptrade_execution.get('live_enabled')) and not bool(
        ptrade_execution.get('dry_run', True)
    )
    config = engine.config
    selected_updated_at = selected.get('updated_at') if selected else None
    selected_date = str(selected_updated_at or '')[:10]
    return {
        'generated_at': now.isoformat(timespec='seconds'),
        'valuation_updated_at': engine.position_valuation_updated_at(),
        'today': today,
        'execution_date': default_execution_date,
        'mode': '实盘' if live_enabled else '模拟盘',
        'monitoring': bool(overview.get('realtime')),
        'strategy': {
            'name': selected.get('name') or '',
            'source': selected.get('source') or '',
            'updated_at': selected_updated_at,
            'result_count': len(raw_results),
            'stale': bool(selected_date and selected_date != today),
        },
        'strategy_options': [
            {
                'name': item.get('name') or '',
                'updated_at': item.get('updated_at'),
                'result_count': len(item.get('results') or []),
            }
            for item in sorted(
                strategies,
                key=lambda item: str(item.get('updated_at') or ''),
                reverse=True,
            )
        ],
        'candidates': candidates,
        'counts': {
            'screened': len(raw_results),
            'queued': len(pending_by_code),
            'positions': len(positions),
            'signals_today': len(signals),
            'trades_today': len(filled_trades),
        },
        'account': overview,
        'positions': positions,
        'trade_records': [
            {
                'id': item.get('trade_id'),
                'trade_date': item.get('trade_date'),
                'trade_time': item.get('trade_time'),
                'code': item.get('code'),
                'name': item.get('name'),
                'side': item.get('side'),
                'quantity': item.get('quantity'),
                'price': item.get('price'),
                'amount': item.get('amount'),
                'fees': round(
                    float(item.get('commission') or 0)
                    + float(item.get('stamp_tax') or 0)
                    + float(item.get('transfer_fee') or 0),
                    2,
                ),
                'total': item.get('total'),
                'status': item.get('status'),
                'reason': item.get('reject_reason') or item.get('signal_msg') or '',
            }
            for item in recent_trades[:200]
        ],
        'activities': activities[:20],
        'rules': {
            'theme_position_enabled': bool(config.theme_position_enabled),
            'theme_position_adjustment_enabled': bool(
                config.theme_position_adjustment_enabled
            ),
            'theme_min_score': round(float(config.theme_min_score), 2),
            'timing_strategy': config.timing_strategy,
            'require_external_environment': bool(
                config.require_external_environment
            ),
            'buy_time_windows': list(config.buy_time_windows or []),
            'max_positions': config.max_positions,
            'max_position_pct': round(config.max_position_pct * 100, 2),
            'max_total_position_pct': round(config.max_total_position_pct * 100, 2),
            'initial_position_pct': round(config.initial_position_pct * 100, 2),
            'add_position_pct': round(config.add_position_pct * 100, 2),
            'market_strength_add_position_pct': round(
                config.market_strength_add_position_pct * 100, 2
            ),
            'bottom_reversal_add_position_pct': round(
                config.bottom_reversal_add_position_pct * 100, 2
            ),
            'max_hold_days': config.max_hold_days,
            'stop_loss': round(config.stop_loss * 100, 2),
            'take_profit': round(config.take_profit * 100, 2),
            'take_profit_sell_pct': round(config.take_profit_sell_pct * 100, 2),
            'second_take_profit': round(config.second_take_profit * 100, 2),
            'second_take_profit_sell_pct': round(
                config.second_take_profit_sell_pct * 100, 2
            ),
            'trailing_profit_drawdown': round(
                config.trailing_profit_drawdown * 100, 2
            ),
            'limit_up_profit': round(config.limit_up_profit * 100, 2),
            'limit_up_sell_pct': round(config.limit_up_sell_pct * 100, 2),
            'opening_window': '09:30-09:35',
            'open_entry_delay_minutes': config.open_entry_delay_minutes,
            'require_vwap_confirmation': bool(config.require_vwap_confirmation),
            'require_open_price_confirmation': bool(
                config.require_open_price_confirmation
            ),
            'block_on_opening_low_break': bool(
                config.block_on_opening_low_break
            ),
            'max_vwap_deviation_pct': round(config.max_vwap_deviation_pct * 100, 2),
            'vwap_pullback_deviation_pct': round(
                config.vwap_pullback_deviation_pct * 100, 2
            ),
            'vwap_pullback_near_pct': round(config.vwap_pullback_near_pct * 100, 2),
            'vwap_reclaim_min_pct': round(config.vwap_reclaim_min_pct * 100, 2),
            'min_intraday_high_distance_pct': round(
                config.min_intraday_high_distance_pct * 100, 2
            ),
            'max_intraday_gain_pct': round(config.max_intraday_gain_pct * 100, 2),
        },
        'external_environment': {
            'status': external.get('status'),
            'created_at': external.get('created_at'),
            'allow_open': external_decision.get('allow_open') is True,
            'reasons': external_decision.get('reasons') or [],
        },
    }


def _quantpilot_automation_config_snapshot(engine):
    """Return the single server-side configuration used by paper trading."""
    config = engine.config
    return {
        'timing_strategy': config.timing_strategy,
        'require_external_environment': bool(config.require_external_environment),
        'theme_position_enabled': bool(config.theme_position_enabled),
        'theme_position_adjustment_enabled': bool(
            config.theme_position_adjustment_enabled
        ),
        'theme_min_score': round(float(config.theme_min_score), 2),
        'buy_time_windows': list(config.buy_time_windows or []),
        'open_entry_delay_minutes': int(config.open_entry_delay_minutes),
        'require_vwap_confirmation': bool(config.require_vwap_confirmation),
        'require_open_price_confirmation': bool(
            config.require_open_price_confirmation
        ),
        'block_on_opening_low_break': bool(config.block_on_opening_low_break),
        'max_vwap_deviation_pct': round(
            float(config.max_vwap_deviation_pct) * 100, 2
        ),
        'vwap_pullback_deviation_pct': round(
            float(config.vwap_pullback_deviation_pct) * 100, 2
        ),
        'vwap_pullback_near_pct': round(
            float(config.vwap_pullback_near_pct) * 100, 2
        ),
        'vwap_reclaim_min_pct': round(
            float(config.vwap_reclaim_min_pct) * 100, 2
        ),
        'min_intraday_high_distance_pct': round(
            float(config.min_intraday_high_distance_pct) * 100, 2
        ),
        'max_intraday_gain_pct': round(
            float(config.max_intraday_gain_pct) * 100, 2
        ),
        'max_positions': int(config.max_positions),
        'max_position_pct': round(float(config.max_position_pct) * 100, 2),
        'max_total_position_pct': round(
            float(config.max_total_position_pct) * 100, 2
        ),
        'initial_position_pct': round(float(config.initial_position_pct) * 100, 2),
        'add_position_pct': round(float(config.add_position_pct) * 100, 2),
        'market_strength_add_position_pct': round(
            float(config.market_strength_add_position_pct) * 100, 2
        ),
        'bottom_reversal_add_position_pct': round(
            float(config.bottom_reversal_add_position_pct) * 100, 2
        ),
        'max_hold_days': int(config.max_hold_days),
        'stop_loss': round(float(config.stop_loss) * 100, 2),
        'stop_loss_rebound_guard_enabled': bool(config.stop_loss_rebound_guard_enabled),
        'stop_loss_rebound_min_market_drawdown_pct': round(
            float(config.stop_loss_rebound_min_market_drawdown_pct), 2
        ),
        'stop_loss_rebound_min_theme_drawdown_pct': round(
            float(config.stop_loss_rebound_min_theme_drawdown_pct), 2
        ),
        'stop_loss_rebound_bottom_band_pct': round(
            float(config.stop_loss_rebound_bottom_band_pct), 2
        ),
        'stop_loss_rebound_theme_market_deviation_pct': round(
            float(config.stop_loss_rebound_theme_market_deviation_pct), 2
        ),
        'stop_loss_rebound_theme_min_members': int(
            config.stop_loss_rebound_theme_min_members
        ),
        'stop_loss_rebound_min_market_quotes': int(
            config.stop_loss_rebound_min_market_quotes
        ),
        'take_profit': round(float(config.take_profit) * 100, 2),
        'take_profit_sell_pct': round(float(config.take_profit_sell_pct) * 100, 2),
        'second_take_profit': round(float(config.second_take_profit) * 100, 2),
        'second_take_profit_sell_pct': round(
            float(config.second_take_profit_sell_pct) * 100, 2
        ),
        'trailing_profit_drawdown': round(
            float(config.trailing_profit_drawdown) * 100, 2
        ),
        'limit_up_profit': round(float(config.limit_up_profit) * 100, 2),
        'limit_up_sell_pct': round(float(config.limit_up_sell_pct) * 100, 2),
    }


@app.route('/api/quantpilot/automation-dashboard', methods=['GET'])
def quantpilot_automation_dashboard():
    """Return the real next-session automation workflow state."""
    try:
        data = _build_quantpilot_automation_dashboard(
            request.args.get('strategy_name', '')
        )
        return jsonify({'success': True, 'data': clean_data_for_json(data)})
    except Exception as exc:
        logger.exception('自动交易工作台状态获取失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/automation-config', methods=['GET', 'PUT'])
def quantpilot_automation_config():
    """Read or update the configuration used by automatic paper trading."""
    try:
        engine = _get_quantpilot_screen_engine()
        if request.method == 'PUT':
            engine.update_automation_config(request.get_json() or {})
        return jsonify({
            'success': True,
            'data': clean_data_for_json(_quantpilot_automation_config_snapshot(engine)),
        })
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    except Exception as exc:
        logger.exception('自动交易配置读写失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/automation-plan', methods=['POST'])
def save_quantpilot_automation_plan():
    """Queue selected screen results for one next-session opening check."""
    try:
        payload = request.get_json() or {}
        raw_codes = payload.get('codes') or []
        codes = []
        for raw_code in raw_codes:
            code = str(raw_code).strip().zfill(6)
            if code.isdigit() and len(code) == 6 and code not in codes:
                codes.append(code)
        if not codes:
            return jsonify({'success': False, 'error': '请至少选择一只候选股票'}), 400

        engine = _get_quantpilot_screen_engine()
        _, selected = _select_quantpilot_screen_strategy(
            engine, payload.get('strategy_name', '')
        )
        result_map = {
            str(item.get('code', '')).strip().zfill(6): item
            for item in (selected.get('results') or [])
            if isinstance(item, dict)
        }
        missing = [code for code in codes if code not in result_map]
        if missing:
            return jsonify({
                'success': False,
                'error': '以下股票不在当前筛选结果中：' + ', '.join(missing),
            }), 400

        today = datetime.now().strftime('%Y-%m-%d')
        with _quantpilot_watchlist_lock:
            items = _read_quantpilot_watchlist()
            by_code = {str(item.get('code', '')).zfill(6): item for item in items}
            for code in codes:
                result = result_map[code]
                item = by_code.get(code)
                if item is None:
                    item = {'code': code, 'group': '明日交易'}
                    items.append(item)
                    by_code[code] = item
                basic = db_manager.query_one(
                    'SELECT name, industry FROM stock_basic WHERE code = ?', (code,)
                ) or {}
                item.update({
                    'name': basic.get('name') or result.get('name') or code,
                    'industry': basic.get('industry') or result.get('industry') or '未分类',
                    'price': float(result.get('price') or 0),
                    'change': float(result.get('change') or 0),
                    'group': item.get('group') or '明日交易',
                    'strategy': selected.get('name') or result.get('reason') or '',
                    'added_at': today,
                    'entry_pending': True,
                })
                item.pop('entry_decision_date', None)
                item.pop('entry_decision', None)
            _write_quantpilot_watchlist(items)

        data = _build_quantpilot_automation_dashboard(selected.get('name') or '')
        return jsonify({'success': True, 'data': clean_data_for_json(data)})
    except Exception as exc:
        logger.exception('保存明日交易计划失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/automation-plan/<code>', methods=['DELETE'])
def delete_quantpilot_automation_plan(code):
    """Cancel a pending next-session check without removing the watchlist row."""
    code = str(code).strip().zfill(6)
    with _quantpilot_watchlist_lock:
        items = _read_quantpilot_watchlist()
        found = False
        for item in items:
            if str(item.get('code', '')).zfill(6) != code:
                continue
            item['entry_pending'] = False
            item['entry_decision_date'] = datetime.now().strftime('%Y-%m-%d')
            item['entry_decision'] = '用户取消明日交易计划'
            found = True
            break
        if found:
            _write_quantpilot_watchlist(items)
    return jsonify({'success': True, 'data': {'code': code, 'cancelled': found}})


@app.route('/api/quantpilot/code-strategies', methods=['GET', 'POST', 'DELETE'])
def quantpilot_code_strategies():
    """Manage reusable user code strategies used by the QuantPilot screen."""
    try:
        engine = _get_quantpilot_screen_engine()

        if request.method == 'GET':
            return jsonify({'success': True, 'data': {
                'strategies': engine.list_user_screen_strategies(),
            }})

        payload = request.get_json() or {}
        name = str(payload.get('name', '')).strip()

        if request.method == 'DELETE':
            raw_names = payload.get('names')
            names = raw_names if isinstance(raw_names, list) else [name]
            names = [str(item).strip() for item in names if str(item).strip()]
            if not names:
                return jsonify({'success': False, 'error': '策略名称不能为空'}), 400
            for strategy_name in names:
                engine.delete_user_screen_strategy(strategy_name)
            return jsonify({'success': True, 'data': {'names': names}})

        source = str(payload.get('source', '')).strip()
        raw_results = payload.get('results')
        saved_results = clean_data_for_json(raw_results) if isinstance(raw_results, list) else None
        record = engine.save_user_screen_strategy(
            name,
            source,
            payload.get('max_stocks'),
            saved_results,
            time_range=payload.get('time_range'),
            previous_name=payload.get('previous_name'),
            backtest_overrides=payload.get('backtest_overrides'),
        )
        return jsonify({'success': True, 'data': record})
    except (ValueError, TypeError) as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    except Exception as exc:
        logger.exception('QuantPilot 代码策略管理失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/code-screen', methods=['POST'])
def quantpilot_code_screen():
    """Execute the same code-screening engine used by the desktop dialog."""
    try:
        payload = request.get_json() or {}
        source = str(payload.get('source', '')).strip()
        if not source:
            return jsonify({'success': False, 'error': '筛选代码不能为空'}), 400

        strategy_name = str(payload.get('strategy_name', '')).strip()
        screen_engine = _get_quantpilot_screen_engine()

        max_stocks = payload.get('max_stocks')
        max_stocks = int(max_stocks) if max_stocks else None
        screen = screen_engine.screen_with_user_code(
            source,
            max_stocks=max_stocks,
            force_refresh=bool(payload.get('force_refresh', False)),
            time_range=payload.get('time_range'),
        )
        results = []
        for item in screen.get('results', []):
            results.append({
                **item,
                'industry': item.get('industry') or '未分类',
                'change': item.get('change', 0),
                'score': item.get('score', 0),
                'signals': item.get('signals', []),
            })
        if strategy_name and strategy_name != '未保存代码策略':
            screen_engine.save_user_screen_strategy(
                strategy_name,
                source,
                max_stocks,
                clean_data_for_json(results),
                time_range=payload.get('time_range'),
                previous_name=payload.get('previous_name'),
            )
        return jsonify({'success': True, 'data': {
            'results': results[:500],
            'count': len(results),
            'processed': screen.get('processed', 0),
            'errors': screen.get('errors', []),
            'warnings': screen.get('warnings', []),
            'source': source,
            'strategy_name': strategy_name,
            'conditions_enforced': screen.get('conditions_enforced', True),
            'import_stats': screen.get('import_stats', {}),
            'data_date': screen.get('data_date'),
            'data_source': screen.get('data_source', 'realtime_quote'),
        }})
    except (ValueError, TypeError) as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    except Exception as exc:
        logger.exception('QuantPilot 代码筛选失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/quantpilot/stock-themes', methods=['POST'])
def quantpilot_stock_themes():
    """Return locally cached concept themes for screened stocks."""
    try:
        payload = request.get_json() or {}
        raw_codes = payload.get('codes') or []
        if not isinstance(raw_codes, list):
            return jsonify({'success': False, 'error': 'codes 必须是股票代码列表'}), 400
        codes = [str(code).strip() for code in raw_codes if str(code).strip()]
        max_themes = int(payload.get('max_themes') or 5)
        themes = stock_theme_service.get_stock_profiles(
            codes,
            max_themes=max_themes,
            force_refresh=bool(payload.get('force_refresh', False)),
        )
        return jsonify({'success': True, 'data': clean_data_for_json(themes)})
    except (TypeError, ValueError) as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    except Exception as exc:
        logger.exception('筛选股票关联题材获取失败')
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/stocks')
def get_stocks():
    """获取股票列表 - 从 stock_basic 表获取基础数据"""
    try:
        # 获取分页参数
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 500))  # 默认每页500只
        
        # 计算分页偏移
        offset = (page - 1) * per_page
        
        # 从 stock_basic 表获取总数
        total_result = db_manager.query('SELECT COUNT(*) as count FROM stock_basic')
        total = total_result[0]['count'] if total_result else 0
        
        # 从 stock_basic 表获取分页数据
        query = '''
            SELECT code, name, industry, area, market, list_date, market_cap
            FROM stock_basic
            ORDER BY code
            LIMIT ? OFFSET ?
        '''
        basic_stocks = db_manager.query(query, (per_page, offset))
        
        stock_list = []
        for stock in basic_stocks:
            # 处理 market_cap 为 None 或 NaN 的情况
            market_cap = stock.get('market_cap', 0)
            if market_cap is None or (isinstance(market_cap, float) and market_cap != market_cap):
                market_cap = 0
            
            # 单位转换：如果市值 > 10000，说明是万元单位，需要转换为亿元
            # 否则已经是亿元单位
            if market_cap > 10000:
                # 万元转亿元：除以 10000
                market_cap = market_cap / 10000
            
            # 从 stock_kline 表获取最新价格和日期
            kline_query = '''
                SELECT close, date FROM stock_kline
                WHERE code = ?
                ORDER BY date DESC
                LIMIT 1
            '''
            kline_result = db_manager.query(kline_query, (stock['code'],))
            
            latest_price = 0
            latest_date = ''
            data_count = 0
            
            if kline_result:
                latest_price = round(kline_result[0]['close'], 2)
                latest_date = kline_result[0]['date']
                
                # 获取该股票的数据条数
                count_query = 'SELECT COUNT(*) as count FROM stock_kline WHERE code = ?'
                count_result = db_manager.query(count_query, (stock['code'],))
                data_count = count_result[0]['count'] if count_result else 0
            
            stock_list.append({
                'code': stock['code'],
                'name': stock['name'],
                'latest_price': latest_price,
                'latest_date': latest_date,
                'market_cap': round(market_cap, 2),  # 总市值，单位：亿
                'data_count': data_count
            })
        
        return jsonify({
            'success': True, 
            'data': stock_list, 
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': (total + per_page - 1) // per_page
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


def get_latest_trading_date() -> str:
    """获取最近交易日（考虑收盘时间）
    
    如果今天是交易日且已收盘（15:00之后），返回今天
    否则返回上一个交易日
    """
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    
    # 判断是否已收盘（15:00之后）
    is_market_closed = now.hour >= 15
    
    if is_trading_day(today_str) and is_market_closed:
        return today_str
    else:
        return get_previous_trading_day(today_str)


@app.route('/api/dashboard/my-golden-stocks')
def get_my_golden_stocks():
    """获取我的金股 - 最近交易日的top5股票（考虑收盘时间）"""
    try:
        # 获取最近交易日
        target_date = get_latest_trading_date()
        
        # 查询该交易日的选股记录
        rows = db_manager.query("""
            SELECT stock_code, stock_name, industry, sector, score, rank_position
            FROM stock_selection_record
            WHERE selection_date = ?
            ORDER BY rank_position ASC
            LIMIT 5
        """, (target_date,))
        
        # 如果没有数据，直接返回空列表
        if not rows:
            return jsonify({
                'success': True,
                'date': target_date,
                'stocks': []
            })
        
        # 转换为字典列表
        items = []
        for row in rows:
            items.append({
                'stock_code': row['stock_code'],
                'stock_name': row['stock_name'],
                'industry': row['industry'] or '-',
                'area': row['sector'] or '-',  # 这里sector对应前端的area
                'total_score': row['score'] or 0
            })
        
        return jsonify({
            'success': True,
            'date': target_date,
            'stocks': items
        })
    except Exception as e:
        logger.error(f"获取我的金股失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/dashboard/hot-industries')
def get_hot_industries():
    """获取最热行业 - top50股票的行业分布（考虑收盘时间）"""
    try:
        # 获取最近交易日
        score_date = get_latest_trading_date()
        
        # 获取top50股票
        rows = db_manager.query("""
            SELECT industry
            FROM stock_selection_record
            WHERE selection_date = ?
            ORDER BY rank_position ASC
            LIMIT 50
        """, (score_date,))
        
        # 如果没有数据，直接返回空列表
        if not rows:
            return jsonify({
                'success': True,
                'date': score_date,
                'industries': []
            })
        
        # 统计行业分布
        industry_count = {}
        for row in rows:
            industry = row['industry'] or '未知'
            if industry in industry_count:
                industry_count[industry] += 1
            else:
                industry_count[industry] = 1
        
        # 转换为列表并排序
        industries = []
        total = len(rows)
        for industry, count in industry_count.items():
            industries.append({
                'industry': industry,
                'count': count,
                'percentage': round(count / total * 100, 2)
            })
        
        # 按股票数量排序
        industries.sort(key=lambda x: x['count'], reverse=True)
        
        return jsonify({
            'success': True,
            'date': score_date,
            'industries': industries
        })
    except Exception as e:
        logger.error(f"获取最热行业失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/dashboard/hot-areas')
def get_hot_areas():
    """获取最热板块 - top50股票的板块分布（考虑收盘时间）"""
    try:
        # 获取最近交易日
        score_date = get_latest_trading_date()
        
        # 获取top50股票
        rows = db_manager.query("""
            SELECT sector
            FROM stock_selection_record
            WHERE selection_date = ?
            ORDER BY rank_position ASC
            LIMIT 50
        """, (score_date,))
        
        # 如果没有数据，直接返回空列表
        if not rows:
            return jsonify({
                'success': True,
                'date': score_date,
                'areas': []
            })
        
        # 统计板块分布
        area_count = {}
        for row in rows:
            area = row['sector'] or '未知'
            if area in area_count:
                area_count[area] += 1
            else:
                area_count[area] = 1
        
        # 转换为列表并排序
        areas = []
        total = len(rows)
        for area, count in area_count.items():
            areas.append({
                'area': area,
                'count': count,
                'percentage': round(count / total * 100, 2)
            })
        
        # 按股票数量排序
        areas.sort(key=lambda x: x['count'], reverse=True)
        
        return jsonify({
            'success': True,
            'date': score_date,
            'areas': areas
        })
    except Exception as e:
        logger.error(f"获取最热板块失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/dashboard/industry-stocks')
def get_industry_stocks():
    """获取指定行业的股票列表 - top50"""
    try:
        # 获取参数
        industry = request.args.get('industry', '')
        limit = int(request.args.get('limit', 50))
        
        if not industry:
            return jsonify({'success': False, 'error': '行业参数不能为空'})
        
        # 获取最近的选股日期
        date_result = db_manager.query("SELECT DISTINCT selection_date FROM stock_selection_record ORDER BY selection_date DESC LIMIT 1")
        if not date_result:
            return jsonify({
                'success': True,
                'stocks': []
            })
        
        score_date = date_result[0]['selection_date']
        
        # 获取指定行业的股票，按评分排序
        rows = db_manager.query("""
            SELECT stock_code, stock_name, industry, sector, score, rank_position, selection_price
            FROM stock_selection_record
            WHERE selection_date = ? AND industry = ?
            ORDER BY score DESC
            LIMIT ?
        """, (score_date, industry, limit))
        
        # 初始化AKShareFetcher获取实时价格
        from utils.akshare_fetcher import AKShareFetcher
        akshare_fetcher = AKShareFetcher()
        
        # 转换为字典列表并计算实时数据
        stocks = []
        for row in rows:
            stock_code = row['stock_code']
            stock_name = row['stock_name']
            industry = row['industry'] or '-'
            sector = row['sector'] or '-'
            score = row['score'] or 0
            rank_position = row['rank_position'] or 0
            selection_price = row['selection_price'] or 0
            
            # 获取实时价格
            current_price = akshare_fetcher.get_stock_price(stock_code)
            
            # 计算当前收益率
            current_yield = 0.0
            if current_price and selection_price:
                current_yield = (current_price - selection_price) / selection_price * 100
            
            # 获取选入后最高价格
            highest_price = None  # 18策略评分系统已删除
            
            # 计算最高收益率
            highest_yield = 0.0
            if highest_price and selection_price:
                highest_yield = (highest_price - selection_price) / selection_price * 100
            
            stocks.append({
                'stock_code': stock_code,
                'stock_name': stock_name,
                'industry': industry,
                'sector': sector,
                'score': score,
                'rank_position': rank_position,
                'selection_price': selection_price,
                'current_price': current_price or 0,
                'current_yield': round(current_yield, 2) or 0,
                'highest_price': highest_price or 0,
                'highest_yield': round(highest_yield, 2) or 0
            })
        
        return jsonify({
            'success': True,
            'date': score_date,
            'stocks': stocks
        })
    except Exception as e:
        logger.error(f"获取行业股票失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/dashboard/area-stocks')
def get_area_stocks():
    """获取指定板块的股票列表 - top50"""
    try:
        # 获取参数
        area = request.args.get('area', '')
        limit = int(request.args.get('limit', 50))
        
        if not area:
            return jsonify({'success': False, 'error': '板块参数不能为空'})
        
        # 获取最近的选股日期
        date_result = db_manager.query("SELECT DISTINCT selection_date FROM stock_selection_record ORDER BY selection_date DESC LIMIT 1")
        if not date_result:
            return jsonify({
                'success': True,
                'stocks': []
            })
        
        score_date = date_result[0]['selection_date']
        
        # 获取指定板块的股票，按评分排序
        rows = db_manager.query("""
            SELECT stock_code, stock_name, industry, sector, score, rank_position, selection_price
            FROM stock_selection_record
            WHERE selection_date = ? AND sector = ?
            ORDER BY score DESC
            LIMIT ?
        """, (score_date, area, limit))
        
        # 初始化AKShareFetcher获取实时价格
        from utils.akshare_fetcher import AKShareFetcher
        akshare_fetcher = AKShareFetcher()
        
        # 转换为字典列表并计算实时数据
        stocks = []
        for row in rows:
            stock_code = row['stock_code']
            stock_name = row['stock_name']
            industry = row['industry'] or '-'
            sector = row['sector'] or '-'
            score = row['score'] or 0
            rank_position = row['rank_position'] or 0
            selection_price = row['selection_price'] or 0
            
            # 获取实时价格
            current_price = akshare_fetcher.get_stock_price(stock_code)
            
            # 计算当前收益率
            current_yield = 0.0
            if current_price and selection_price:
                current_yield = (current_price - selection_price) / selection_price * 100
            
            # 获取选入后最高价格
            highest_price = None  # 18策略评分系统已删除
            
            # 计算最高收益率
            highest_yield = 0.0
            if highest_price and selection_price:
                highest_yield = (highest_price - selection_price) / selection_price * 100
            
            stocks.append({
                'stock_code': stock_code,
                'stock_name': stock_name,
                'industry': industry,
                'sector': sector,
                'score': score,
                'rank_position': rank_position,
                'selection_price': selection_price,
                'current_price': current_price or 0,
                'current_yield': round(current_yield, 2) or 0,
                'highest_price': highest_price or 0,
                'highest_yield': round(highest_yield, 2) or 0
            })
        
        return jsonify({
            'success': True,
            'date': score_date,
            'stocks': stocks
        })
    except Exception as e:
        logger.error(f"获取板块股票失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/stock/<code>')
def get_stock_detail(code):
    """获取单只股票详情"""
    try:
        # 从数据库读取股票数据
        df = db_manager.read_stock(code)
        
        # 如果数据库没有数据，尝试从Tushare实时获取
        if df.empty:
            logger.info(f"数据库中无 {code} 数据，尝试从Tushare获取")
            try:
                import tushare as ts
                pro = ts.pro_api()
                # 转换代码格式：000001 -> 000001.SZ, 600000 -> 600000.SH
                if not code.endswith(('.SH', '.SZ')):
                    if code.startswith('6'):
                        code_fmt = f"{code}.SH"
                    else:
                        code_fmt = f"{code}.SZ"
                else:
                    code_fmt = code
                
                # 获取最近400个交易日的数据
                end_date = dt.now().strftime('%Y%m%d')
                start_date = (dt.now() - timedelta(days=400)).strftime('%Y%m%d')
                
                df = pro.daily(ts_code=code_fmt, start_date=start_date, end_date=end_date)
                
                if df is not None and not df.empty:
                    # 转换列名以匹配数据库格式
                    df = df.rename(columns={
                        'trade_date': 'date', 'vol': 'volume', 'pct_chg': 'pct_change'
                    })
                    # 将日期字符串转换为datetime
                    df['date'] = pd.to_datetime(df['date'], format='%Y%m%d')
                    df = df.sort_values('date')
                    df = df.reset_index(drop=True)
                    logger.info(f"从Tushare获取 {code} 数据成功，共 {len(df)} 条")
                else:
                    return jsonify({'success': False, 'error': '股票不存在或数据获取失败'})
            except Exception as e:
                logger.error(f"从Tushare获取 {code} 数据失败: {e}")
                return jsonify({'success': False, 'error': '股票不存在'})
        
        # 确保数据按日期升序排列（从早到晚）
        df = df.sort_values('date', ascending=True).reset_index(drop=True)
        
        # 计算KDJ指标
        from utils.technical import KDJ
        kdj_df = KDJ(df, n=9, m1=3, m2=3)
        
        # 转换为列表格式，返回最近100条数据
        data = []
        # 取最后100条（最新的数据）
        start_idx = max(0, len(df) - 100)
        for i in range(start_idx, len(df)):
            row = df.iloc[i]
            kdj_row = kdj_df.iloc[i]
            data.append({
                'date': row['date'].strftime('%Y-%m-%d'),
                'open': round(row['open'], 2) if pd.notna(row['open']) else None,
                'high': round(row['high'], 2) if pd.notna(row['high']) else None,
                'low': round(row['low'], 2) if pd.notna(row['low']) else None,
                'close': round(row['close'], 2) if pd.notna(row['close']) else None,
                'volume': int(row['volume']) if pd.notna(row['volume']) else 0,
                'turnover': round(row.get('turnover', 0), 2) if 'turnover' in row and pd.notna(row.get('turnover')) else 0,
                'market_cap': round(row.get('market_cap', 0) / 1e8, 2) if 'market_cap' in row and pd.notna(row.get('market_cap')) else 0,  # 总市值，单位：亿
                'K': round(kdj_row['K'], 2) if pd.notna(kdj_row['K']) else None,
                'D': round(kdj_row['D'], 2) if pd.notna(kdj_row['D']) else None,
                'J': round(kdj_row['J'], 2) if pd.notna(kdj_row['J']) else None
            })
        
        return jsonify({'success': True, 'code': code, 'data': data})
    except Exception as e:
        logger.error(f"获取股票详情失败: {e}")
        return jsonify({'success': False, 'error': str(e)})


def analyze_intersection(results):
    """
    分析多策略选股结果的交集。构建股票->策略映射，按交集数量分组
    :param results: 策略选股结果字典 {策略名: [信号列表]}
    :return: 交集分析结果
    """
    try:
        # 获取策略的中文名称映射
        import yaml
        config_file = Path("config/strategy_params.yaml")
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        
        strategies_config = config.get('strategies', {})
        strategy_display_names = {}
        for strategy_name, strategy_config in strategies_config.items():
            strategy_display_names[strategy_name] = strategy_config.get('display_name', strategy_name)
        
        # 构建股票->策略映射
        stock_strategies = {}
        for strategy_name, signals in results.items():
            # 确保 signals 是列表
            if not isinstance(signals, list):
                logger.warning(f"策略 {strategy_name} 的信号不是列表，跳过")
                continue
            
            for signal in signals:
                # 验证信号结构
                if not isinstance(signal, dict) or 'code' not in signal:
                    logger.warning(f"无效的信号结构: {signal}")
                    continue
                
                code = signal['code']
                if code not in stock_strategies:
                    stock_strategies[code] = {
                        'code': code,
                        'name': signal.get('name', '未知'),
                        'strategies': [],
                        'strategy_display_names': [],  # 存储中文名称
                        'count': 0,
                        'signals': signal.get('signals', [])  # 保存信号信息
                    }
                
                stock_strategies[code]['strategies'].append(strategy_name)
                stock_strategies[code]['strategy_display_names'].append(strategy_display_names.get(strategy_name, strategy_name))
                stock_strategies[code]['count'] += 1
        
        # 按交集数量分组
        by_count = {}
        for code, data in stock_strategies.items():
            count = data['count']
            if count not in by_count:
                by_count[count] = []
            by_count[count].append(data)
        
        # 计算统计信息
        total_strategies = len(results)
        stocks_by_strategy = {name: len(signals) if isinstance(signals, list) else 0 for name, signals in results.items()}
        multi_strategy_count = sum(len(stocks) for count, stocks in by_count.items() if count > 1)
        intersection_rate = (multi_strategy_count / len(stock_strategies)) if stock_strategies else 0
        
        return {
            'total': len(stock_strategies),
            'by_count': by_count,
            'intersection_stats': {
                'total_strategies': total_strategies,
                'stocks_by_strategy': stocks_by_strategy,
                'intersection_rate': round(intersection_rate, 2)
            }
        }
    except Exception as e:
        logger.error(f"交集分析失败: {str(e)}")
        logger.error(f"错误堆栈: {traceback.format_exc()}")
        # 返回空的分析结果而不是抛出异常
        return {
            'total': 0,
            'by_count': {},
            'intersection_stats': {
                'total_strategies': 0,
                'stocks_by_strategy': {},
                'intersection_rate': 0
            }
        }


@app.route('/api/select', methods=['GET', 'POST'])
def run_selection():
    """执行选股 - 支持GET（执行所有策略）和POST（执行指定策略）。POST请求支持OR/AND逻辑：OR（并集）任意策略选中即可；AND（交集）所有策略都选中"""
    import traceback
    
    # 获取日志记录器
    func_logger = logging.getLogger(__name__)
    
    try:
        # 记录请求开始和时间
        request_start_time = dt.now()
        func_logger.info("=" * 60)
        func_logger.info("选股请求开始")
        
        # 检查参数是否被修改，如果被修改则恢复
        is_modified, restored_params = param_lock.check_and_restore()
        if is_modified:
            func_logger.warning("⚠️  检测到参数被修改，已自动恢复")
            func_logger.warning(f"   恢复的参数: {restored_params}")
        
        # 检查参数是否有变化（用于追踪）
        is_changed, changes = param_tracker.check_changes()
        if is_changed:
            func_logger.warning("⚠️  检测到参数变化")
            for strategy_name, param_changes in changes.items():
                for param_name, change in param_changes.items():
                    func_logger.warning(f"   {strategy_name}.{param_name}: {change['old']} -> {change['new']}")
        
        strategies_to_run = None
        logic = 'or'
        end_date = None

        # 解析请求参数
        if request.method == 'POST':
            try:
                data = request.json or {}
                strategies_to_run = data.get('strategies')
                logic = data.get('logic', 'or')
                end_date = data.get('end_date')
                b1_match = data.get('b1_match', False)  # 是否启用B1完美图形匹配
                min_similarity = data.get('min_similarity', 60.0)  # 最小相似度阈值
                lookback_days = data.get('lookback_days', 25)  # 回看天数

                # 如果end_date为空，使用当前工作日期
                if not end_date:
                    today = dt.now().strftime('%Y-%m-%d')
                    end_date = today
                    func_logger.warning(f"⚠️ end_date为空，使用当前日期: {end_date}")

                func_logger.warning(f"⚠️ 请求参数 - 策略: {strategies_to_run}, 逻辑: {logic}, 结束日期: {end_date}, B1匹配: {b1_match}")
            except Exception as e:
                func_logger.error(f"解析请求参数失败: {str(e)}")
                return jsonify({'success': False, 'error': f'请求参数解析失败: {str(e)}'})
            
            # 检查策略列表是否为空
            if strategies_to_run is not None and len(strategies_to_run) == 0:
                func_logger.info("策略列表为空，返回空结果")
                return jsonify({'success': True, 'data': {}, 'time': dt.now().strftime('%Y-%m-%d %H:%M:%S')})
        
        # 加载股票数据
        try:
            func_logger.info("开始加载股票数据...")
            # 从数据库获取所有股票代码
            stock_codes = db_manager.list_all_stocks()

            # 从数据库获取所有股票名称（不再使用 stock_names.json）
            stock_names = db_manager.get_all_stock_names()
            func_logger.info(f"加载了 {len(stock_codes)} 只股票数据")
        except Exception as e:
            func_logger.error(f"加载股票数据失败: {str(e)}")
            return jsonify({'success': False, 'error': f'加载股票数据失败: {str(e)}'})
        
        # 构建股票数据字典
        try:
            func_logger.warning(f"⚠️ 开始加载股票数据, end_date={end_date}")
            stock_data = {}
            skip_count = 0
            load_start_time = dt.now()
            
            # 加载所有股票的完整数据
            for idx, code in enumerate(stock_codes):
                try:
                    # 读取完整数据，如果指定了结束日期，则只读取到该日期的数据
                    full_df = db_manager.read_stock(code, end_date=end_date)
                    if not full_df.empty and len(full_df) >= 30:
                        # 按日期降序排序（最新的在前）
                        full_df = full_df.sort_values('date', ascending=False)
                        # 从 stock_names 字典中获取股票名称
                        stock_name = stock_names.get(code, '未知')
                        stock_data[code] = (stock_name, full_df)
                except Exception as e:
                    # 跳过无法读取的股票
                    skip_count += 1
                    if skip_count <= 5:  # 只记录前5个错误
                        func_logger.debug(f"无法读取股票 {code}: {str(e)}")
                
                # 每加载500只股票输出一次进度
                if (idx + 1) % 500 == 0:
                    elapsed = (dt.now() - load_start_time).total_seconds()
                    func_logger.info(f"  加载进度: [{idx + 1}/{len(stock_codes)}] 已加载 {len(stock_data)} 只，耗时 {elapsed:.1f}秒")
            
            load_time = (dt.now() - load_start_time).total_seconds()
            func_logger.info(f"成功加载 {len(stock_data)} 只股票的K线数据，跳过 {skip_count} 只，总耗时 {load_time:.1f}秒")
        except Exception as e:
            func_logger.error(f"构建股票数据字典失败: {str(e)}")
            return jsonify({'success': False, 'error': f'构建股票数据字典失败: {str(e)}'})
        
        # 检查是否有可用的股票数据
        if not stock_data:
            func_logger.warning("没有可用的股票数据")
            return jsonify({'success': True, 'data': {}, 'time': dt.now().strftime('%Y-%m-%d %H:%M:%S')})
        
        results = {}
        
        # AND逻辑：找出被所有选中策略都选中的股票
        if logic == 'and' and strategies_to_run and len(strategies_to_run) > 1:
            try:
                func_logger.info(f"执行AND逻辑，策略数: {len(strategies_to_run)}")
                func_logger.info(f"选中策略列表: {strategies_to_run}")
                all_signals = {}
                
                strategy_idx = 0
                for strategy_name in strategies_to_run:
                    if strategy_name not in registry.strategies:
                        continue
                    
                    # 使用 get_strategy 获取最新参数的策略对象
                    strategy = registry.get_strategy(strategy_name)
                    if not strategy:
                        continue
                    
                    strategy_idx += 1
                    func_logger.info(f"[{strategy_idx}/{len(strategies_to_run)}] 开始执行策略: {strategy_name}")
                    signals = []
                    error_count = 0
                    success_count = 0
                    strategy_start_time = dt.now()
                    last_progress_time = dt.now()
                    
                    total_stocks = len(stock_data)
                    for idx, (code, (name, df)) in enumerate(stock_data.items()):
                        try:
                            result = strategy.analyze_stock(code, name, df)
                            if result:
                                success_count += 1
                                # 从 stock_names 字典中获取股票名称
                                fallback_name = stock_names.get(code, '未知')
                                signals.append({
                                    'code': result['code'],
                                    'name': result.get('name', fallback_name),
                                    'signals': result['signals']
                                })
                        except Exception as e:
                            # 跳过分析失败的股票
                            error_count += 1
                            if error_count <= 5:  # 只记录前5个错误
                                func_logger.warning(f"策略 {strategy_name} 分析股票 {code} 失败: {str(e)}")
                        
                        # 每500只股票输出一次进度
                        if (idx + 1) % 500 == 0:
                            elapsed = (dt.now() - last_progress_time).total_seconds()
                            progress = (idx + 1) / total_stocks * 100
                            func_logger.info(f"  策略 {strategy_name} 进度: [{idx + 1}/{total_stocks}] {progress:.1f}% - 选中 {len(signals)} 只，耗时 {elapsed:.1f}秒")
                            last_progress_time = dt.now()
                    
                    strategy_time = (dt.now() - strategy_start_time).total_seconds()
                    func_logger.info(f"策略 {strategy_name} 执行完成: 选中 {len(signals)} 只股票，分析成功 {success_count} 只，失败 {error_count} 只，耗时 {strategy_time:.1f}秒")
                    
                    results[strategy_name] = signals
                    
                    # 计算交集
                    if not all_signals:
                        all_signals = {s['code']: s for s in signals}
                        func_logger.info(f"第一个策略完成，当前交集数量: {len(all_signals)}")
                    else:
                        prev_count = len(all_signals)
                        all_signals = {code: s for code, s in all_signals.items() if any(sig['code'] == code for sig in signals)}
                        func_logger.info(f"交集计算完成: {prev_count} -> {len(all_signals)}")
                
                # 返回交集结果
                intersection_result = list(all_signals.values())
                results = {'_intersection': intersection_result}
                func_logger.info(f"AND逻辑执行完成，最终交集结果: {len(intersection_result)} 只股票")
            except Exception as e:
                func_logger.error(f"AND逻辑执行失败: {str(e)}")
                func_logger.error(f"错误堆栈: {traceback.format_exc()}")
                return jsonify({'success': False, 'error': f'AND逻辑执行失败: {str(e)}'})
        else:
            # OR逻辑（默认）：分别执行每个策略
            try:
                # 加载策略的中文名称映射
                import yaml
                config_file = Path("config/strategy_params.yaml")
                strategy_display_names = {}
                if config_file.exists():
                    with open(config_file, 'r', encoding='utf-8') as f:
                        config = yaml.safe_load(f) or {}
                    strategies_config = config.get('strategies', {})
                    for strategy_name, strategy_config in strategies_config.items():
                        strategy_display_names[strategy_name] = strategy_config.get('display_name', strategy_name)
                
                func_logger.info(f"执行OR逻辑，策略数: {len(registry.strategies)}")
                func_logger.info(f"指定执行的策略: {strategies_to_run}")
                
                # 优化：直接从strategies_to_run中获取策略，避免逐个跳过
                strategies_to_execute = []
                if strategies_to_run:
                    # 去重处理：保持顺序的同时去除重复策略
                    unique_strategies = list(dict.fromkeys(strategies_to_run))
                    if len(unique_strategies) < len(strategies_to_run):
                        func_logger.warning(f"检测到重复策略，已去重: {strategies_to_run} -> {unique_strategies}")
                    
                    # 只获取指定的策略
                    for strategy_name in unique_strategies:
                        if strategy_name in registry.strategies:
                            # 使用 get_strategy 获取最新参数的策略对象
                            strategy = registry.get_strategy(strategy_name)
                            if strategy:
                                strategies_to_execute.append((strategy_name, strategy))
                        else:
                            func_logger.warning(f"指定的策略不存在: {strategy_name}")
                else:
                    # 如果没有指定策略，执行所有策略
                    # 获取所有策略名称
                    all_strategy_names = list(registry.strategies.keys())
                    strategies_to_execute = [(name, registry.get_strategy(name)) for name in all_strategy_names if registry.get_strategy(name)]
                
                for strategy_name, strategy in strategies_to_execute:
                    func_logger.info(f"执行策略: {strategy_name}")
                    signals = []
                    error_count = 0
                    strategy_start_time = dt.now()
                    
                    # 获取该策略的中文名称
                    strategy_display_name = strategy_display_names.get(strategy_name, strategy_name)
                    
                    for idx, (code, (name, df)) in enumerate(stock_data.items()):
                        try:
                            result = strategy.analyze_stock(code, name, df)
                            if result:
                                # 从 stock_names 字典中获取股票名称
                                fallback_name = stock_names.get(code, '未知')
                                signals.append({
                                    'code': result['code'],
                                    'name': result.get('name', fallback_name),
                                    'signals': result['signals'],
                                    'strategy_display_name': strategy_display_name  # 添加中文名称
                                })
                        except Exception as e:
                            # 跳过分析失败的股票
                            error_count += 1
                            if error_count <= 3:  # 只记录前3个错误
                                func_logger.debug(f"策略 {strategy_name} 分析股票 {code} 失败: {str(e)}")
                        
                        # 每处理100只股票输出一次进度
                        if (idx + 1) % 100 == 0:
                            elapsed = (dt.now() - strategy_start_time).total_seconds()
                            func_logger.info(f"    {strategy_display_name} 进度: [{idx + 1}/{len(stock_data)}] 已选中 {len(signals)} 只，耗时 {elapsed:.1f}秒")
                    
                    strategy_time = (dt.now() - strategy_start_time).total_seconds()
                    results[strategy_name] = signals
                    func_logger.info(f"策略 {strategy_name} 完成 - 选中 {len(signals)} 只股票，分析失败 {error_count} 只，总耗时 {strategy_time:.1f}秒")
                
                # 计算交集分析（仅当有多个策略且都有结果时）
                if len(results) > 1:
                    # 检查是否有任何策略有结果
                    has_results = any(len(signals) > 0 for signals in results.values())
                    func_logger.info(f"多策略结果 - 总策略数: {len(results)}, 有结果: {has_results}")
                    
                    if has_results:
                        try:
                            func_logger.info("计算交集分析...")
                            intersection_analysis = analyze_intersection(results)
                            results['_intersection_analysis'] = intersection_analysis
                            func_logger.info(f"交集分析完成 - 总股票数: {intersection_analysis.get('total', 0)}")
                        except Exception as e:
                            func_logger.error(f"交集分析计算失败: {str(e)}")
                            func_logger.error(f"错误堆栈: {traceback.format_exc()}")
                            # 不返回错误，继续返回结果
            except Exception as e:
                func_logger.error(f"OR逻辑执行失败: {str(e)}")
                func_logger.error(f"错误堆栈: {traceback.format_exc()}")
                return jsonify({'success': False, 'error': f'OR逻辑执行失败: {str(e)}'})
        
        # 返回结果
        total_time = (dt.now() - request_start_time).total_seconds()
        func_logger.info(f"选股完成 - 返回结果数: {len(results)}，总耗时 {total_time:.1f}秒")
        func_logger.info("=" * 60)
        
        # 应用过滤条件
        filter_stats = {}
        try:
            func_logger.info("应用过滤条件...")
            # 直接从配置文件读取过滤配置
            import yaml
            with open('config/config.yaml', 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            filter_config = config.get('filters', {})
            stock_filter = StockFilter(filter_config)
            
            # 应用过滤 - 使用与策略分析相同的stock_data
            # 这样可以确保过滤逻辑使用与策略分析相同的数据，避免数据不一致
            filtered_results, filter_stats = stock_filter.apply_filters(results, stock_data)
            
            # 显示过滤统计
            if filter_stats.get('enabled', False):
                func_logger.info(f"过滤统计: 过滤前{filter_stats['total_before']}只 -> 过滤后{filter_stats['total_after']}只 (被过滤{filter_stats['filtered_out']}只)")
                for filter_name, count in filter_stats.get('filters_applied', {}).items():
                    if count > 0:
                        func_logger.info(f"  - {filter_name}: {count}只")
            
            # 使用过滤后的结果
            results = filtered_results
            
            # 重新计算交集分析（基于过滤后的结果）
            if len(results) > 1:
                # 检查是否有任何策略有结果
                has_results = any(len(signals) > 0 for signals in results.values() if isinstance(signals, list))
                func_logger.info(f"多策略结果（过滤后）- 总策略数: {len(results)}, 有结果: {has_results}")
                
                if has_results:
                    try:
                        func_logger.info("重新计算交集分析...")
                        intersection_analysis = analyze_intersection(results)
                        results['_intersection_analysis'] = intersection_analysis
                        func_logger.info(f"交集分析完成（过滤后）- 总股票数: {intersection_analysis.get('total', 0)}")
                    except Exception as e:
                        func_logger.error(f"交集分析计算失败: {str(e)}")
                        func_logger.error(f"错误堆栈: {traceback.format_exc()}")
                        # 不返回错误，继续返回结果
            
        except Exception as e:
            func_logger.warning(f"应用过滤条件失败: {str(e)}")
            func_logger.warning(f"错误堆栈: {traceback.format_exc()}")
        
        # 用腾讯财经实时价格替换选股结果中的close字段
        try:
            from utils.akshare_fetcher import AKShareFetcher
            fetcher = AKShareFetcher()
            # 收集所有选中股票的代码
            all_codes = set()
            for key, signals in results.items():
                if isinstance(signals, list):
                    for s in signals:
                        if isinstance(s, dict) and 'code' in s:
                            all_codes.add(s['code'])
            
            if all_codes:
                # 批量获取实时价格
                realtime_prices = fetcher.get_stock_prices_batch(list(all_codes))
                func_logger.info(f"获取实时价格: 请求{len(all_codes)}只, 成功{len(realtime_prices)}只")
                
                # 替换signals中的close字段为实时价格
                for key, signals in results.items():
                    if isinstance(signals, list):
                        for item in signals:
                            if not isinstance(item, dict):
                                continue
                            code = item.get('code', '')
                            price = realtime_prices.get(code)
                            if price is None:
                                continue
                            # 替换嵌套signals列表中的close
                            if 'signals' in item and isinstance(item['signals'], list):
                                for sig in item['signals']:
                                    if isinstance(sig, dict) and 'close' in sig:
                                        sig['close'] = round(price, 2)
        except Exception as e:
            func_logger.warning(f"获取实时价格失败，使用CSV收盘价: {str(e)}")
        
        # 不再自动保存选股结果，由前端手动触发保存
        # 清理数据中的NaN和Inf值
        cleaned_results = clean_data_for_json(results)
        cleaned_filter_stats = clean_data_for_json(filter_stats)
        
        # 将结果中的键从类名转换为中文名称
        if strategy_display_names:
            converted_results = {}
            for strategy_name, signals in cleaned_results.items():
                # 获取中文名称，如果没有则使用原名称
                display_name = strategy_display_names.get(strategy_name, strategy_name)
                converted_results[display_name] = signals
            cleaned_results = converted_results
        
        # 如果启用了B1完美图形匹配
        if b1_match:
            func_logger.info(f"启用B1完美图形匹配，最小相似度: {min_similarity}，回看天数: {lookback_days}")
            try:
                # 初始化CSV管理器
                from utils.csv_manager import CSVManager
                csv_manager = CSVManager(get_data_dir())
                
                # 初始化B1完美图形库
                from strategy.pattern_library import B1PatternLibrary
                library = B1PatternLibrary(csv_manager)
                
                # 执行B1完美图形匹配
                matched_results = []
                # 收集所有选中的股票
                all_stocks = []
                for strategy_name, signals in results.items():
                    if isinstance(signals, list):
                        all_stocks.extend(signals)
                
                # 去重
                seen_codes = set()
                unique_stocks = []
                for stock in all_stocks:
                    if stock['code'] not in seen_codes:
                        seen_codes.add(stock['code'])
                        unique_stocks.append(stock)
                
                func_logger.info(f"B1匹配 - 处理 {len(unique_stocks)} 只股票")
                
                for stock in unique_stocks:
                    code = stock['code']
                    name = stock['name']
                    
                    # 读取股票数据
                    df = csv_manager.read_stock(code)
                    if df.empty:
                        continue
                    
                    # 执行匹配
                    match_result = library.find_best_match(code, df, lookback_days=lookback_days)
                    if match_result.get('best_match'):
                        best = match_result['best_match']
                        similarity_score = best.get('similarity_score', 0)
                        
                        if similarity_score >= min_similarity:
                            # 计算基础数据
                            latest_data = df.iloc[0]
                            base_data = {
                                'price': float(latest_data['close']),
                                'change': float(latest_data.get('change', 0)),
                                'volume': float(latest_data['volume'])
                            }
                            
                            matched_results.append({
                                'code': code,
                                'name': name,
                                'similarity_score': similarity_score,
                                'matched_case': best.get('case_name', ''),
                                'matched_date': best.get('case_date', ''),
                                'matched_code': best.get('case_code', ''),
                                'base_data': base_data,
                                'signals': stock.get('signals', {}),
                                'all_matches': best.get('all_matches', [])
                            })
                
                # 按相似度排序
                matched_results.sort(key=lambda x: x['similarity_score'], reverse=True)
                func_logger.info(f"B1完美图形匹配完成 - 匹配结果数: {len(matched_results)}")
                
                # 返回匹配结果
                return jsonify({
                    'success': True,
                    'data': {
                        'matched': matched_results,
                        'count': len(matched_results)
                    },
                    'filter_stats': cleaned_filter_stats,
                    'b1_match': True,
                    'time': dt.now().strftime('%Y-%m-%d %H:%M:%S')
                })
            except Exception as e:
                func_logger.error(f"执行B1完美图形匹配失败: {str(e)}")
                func_logger.error(f"错误堆栈: {traceback.format_exc()}")
                # 匹配失败时返回原始结果
                pass
        
        return jsonify({
            'success': True,
            'data': cleaned_results,
            'filter_stats': cleaned_filter_stats,
            'b1_match': False,
            'time': dt.now().strftime('%Y-%m-%d %H:%M:%S'),
            'selection_date': end_date if end_date else dt.now().strftime('%Y-%m-%d'),  # 添加选股日期
            'strategy_display_names': strategy_display_names  # 添加策略名称映射
        })
    
    except Exception as e:
        # 捕获所有未预期的异常
        func_logger = logging.getLogger(__name__)
        error_msg = str(e)
        func_logger.error("=" * 60)
        func_logger.error(f"选股执行失败（未预期的异常）: {error_msg}")
        func_logger.error(f"错误堆栈: {traceback.format_exc()}")
        func_logger.error("=" * 60)
        
        return jsonify({
            'success': False,
            'error': f'选股执行失败: {error_msg}'
        })


@app.route('/api/save_selection', methods=['POST'])
def save_selection():
    """手动保存选股结果到数据库"""
    func_logger = logging.getLogger(__name__)
    try:
        data = request.json or {}
        # 从前端接收选股结果数据
        results = data.get('results', {})
        selection_time_str = data.get('time', '')
        end_date = data.get('end_date')

        # 解析选股时间
        try:
            selection_time = dt.strptime(selection_time_str, '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            selection_time = dt.now()

        # 收集所有选股信号和策略名称
        all_signals = []
        strategy_names = []
        
        # 构建股票代码到策略名称的映射
        stock_strategy_map = {}
        
        for strategy_name, signals in results.items():
            # 跳过特殊字段（如_intersection_analysis）
            if strategy_name.startswith('_'):
                continue
            strategy_names.append(strategy_name)
            
            # 为每只股票添加策略信息
            for signal in signals:
                code = signal.get('code')
                if code:
                    if code not in stock_strategy_map:
                        stock_strategy_map[code] = []
                    stock_strategy_map[code].append(strategy_name)
            
            all_signals.extend(signals)

        # 处理交集中的股票（被多个策略同时选中的股票）
        intersection_analysis = results.get('_intersection_analysis', {})
        if intersection_analysis:
            by_count = intersection_analysis.get('by_count', {})
            
            # 清空all_signals，重新构建
            all_signals = []
            
            # 处理所有股票（包括被1个策略选中的股票）
            for count, stocks in by_count.items():
                for stock in stocks:
                    code = stock.get('code')
                    if code:
                        # 获取该股票命中的策略列表
                        strategies = stock.get('strategies', [])
                        if not strategies:
                            # 如果没有strategies字段，从stock_strategy_map中获取
                            strategies = stock_strategy_map.get(code, [])
                        
                        # 构建信号对象
                        signal = {
                            'code': code,
                            'name': stock.get('name', '未知'),
                            'strategies': strategies,
                            'signals': stock.get('signals', [])
                        }
                        all_signals.append(signal)
                        func_logger.info(f"添加股票: {code} {stock.get('name')} - 策略: {strategies} (被{count}个策略选中)")

        # 为每只股票添加命中的策略列表
        for signal in all_signals:
            code = signal.get('code')
            if code and code in stock_strategy_map:
                signal['strategies'] = stock_strategy_map[code]

        # 检查是否有数据可保存
        if not all_signals or not strategy_names:
            return jsonify({'success': False, 'error': '没有可保存的选股结果'})

        # 调用保存方法
        save_result = selection_record_manager.save_selection_result(
            strategy_names=strategy_names,
            signals=all_signals,
            selection_time=selection_time,
            end_date=end_date
        )
        func_logger.info(f"手动保存选股结果 - {save_result}")
        return jsonify(save_result)

    except Exception as e:
        func_logger.error(f"手动保存选股结果失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/strategies/<name>')
def get_strategy_detail(name):
    """获取策略详情 - 包含参数详细信息"""
    try:
        # 从YAML文件直接读取原始参数，而不是转换后的参数
        import yaml
        config_file = Path("config/strategy_params.yaml")
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        
        # 获取策略对象
        strategy = registry.get_strategy(name)
        if not strategy:
            return jsonify({'success': False, 'error': '策略不存在'})
        
        # 从strategies键下获取策略配置
        strategies_config = config.get('strategies', {})
        
        # 尝试从YAML中获取策略配置
        # 首先使用类名作为键查找
        strategy_class_name = type(strategy).__name__
        strategy_config = strategies_config.get(strategy_class_name, {})
        
        # 如果找不到，再使用中文名称作为键查找
        if not strategy_config:
            strategy_config = strategies_config.get(name, {})
        
        if not strategy_config:
            return jsonify({'success': False, 'error': '策略不存在'})
        
        # 从YAML中获取原始参数值（不经过转换）
        original_params = strategy_config.get('params', {})
        
        # 构建详情数据
        detail = {
            'name': name,
            'display_name': strategy_config.get('display_name', name),
            'description': strategy_config.get('description', ''),
            'icon': strategy_config.get('icon', '📊'),
            'color': strategy_config.get('color', '#2563eb'),
            'param_groups': strategy_config.get('param_groups', []),
            'param_details': strategy_config.get('param_details', {}),
            'current_params': original_params  # 使用原始参数，不是转换后的
        }
        
        return jsonify({'success': True, 'data': detail})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/strategies/names', methods=['GET'])
def get_strategy_names():
    """
    获取策略名称映射（英文类名 -> 中文名称）
    复用 utils.strategy_name_mapper 模块
    
    :return: 策略名称映射字典
    """
    try:
        return jsonify({
            "success": True,
            "data": STRATEGY_NAME_MAP
        })
    except Exception as e:
        logger.error(f"获取策略名称映射失败: {str(e)}")
        return jsonify({
            "success": False,
            "error": str(e)
        })


@app.route('/api/strategies')
def get_strategies():
    """获取策略列表 - 包含中文名称和元数据，按照strategy_order.yaml中定义的顺序排列"""
    try:
        # 从YAML文件直接读取原始参数，而不是转换后的参数
        import yaml
        config_file = Path("config/strategy_params.yaml")
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        
        # 从strategies键下获取策略配置
        strategies_config = config.get('strategies', {})
        
        # 构建策略列表
        strategies = []
        
        # 使用registry中的排序信息（从strategy_order.yaml加载）
        # 按照排序顺序获取策略名称
        sorted_strategy_names = registry.list_strategies()
        
        # 按照排序顺序遍历策略
        for name in sorted_strategy_names:
            if name not in registry.strategies:
                continue
            
            # 获取策略对象用于获取元数据
            strategy = registry.strategies.get(name)
            metadata = getattr(strategy, 'metadata', {}) if strategy else {}
            
            # 尝试从YAML中获取策略配置
            # 首先使用类名作为键查找
            strategy_class_name = type(strategy).__name__
            strategy_config = strategies_config.get(strategy_class_name, {})
            
            # 如果找不到，再使用中文名称作为键查找
            if not strategy_config:
                strategy_config = strategies_config.get(name, {})
            
            # 从YAML中获取原始参数值（不经过转换）
            original_params = strategy_config.get('params', {})
            
            strategies.append({
                'name': name,
                'display_name': strategy_config.get('display_name', name),
                'description': strategy_config.get('description', ''),
                'icon': strategy_config.get('icon', '📊'),
                'color': strategy_config.get('color', '#2563eb'),
                'params': original_params  # 使用原始参数，不是转换后的
            })
        
        return jsonify({'success': True, 'strategies': strategies, 'data': strategies})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@ app.route('/api/timing-strategies')
def get_timing_strategies():
    """获取择时策略列表 - 根据配置文件存在与否决定是否包含顺势宝策略"""
    logger.info("开始获取择时策略列表")
    try:
        from utils.feature_config_checker import FeatureConfigChecker
        
        # 检查功能配置
        checker = FeatureConfigChecker()
        has_valid_config = False
        try:
            valid_files, expire_date = checker.check_config()
            has_valid_config = bool(valid_files)
            logger.info(f"功能配置检查结果: 有效文件={valid_files}, 过期日期={expire_date}, has_valid_config={has_valid_config}")
        except Exception as e:
            logger.warning(f"检查功能配置时发生异常: {e}")
        
        # 基础择时策略列表
        timing_strategies = [
            {'name': 'turtle', 'display_name': '海龟策略'},
            {'name': 'support', 'display_name': '支撑位策略'},
            {'name': 'rsi', 'display_name': 'RSI策略'},
            {'name': 'bollinger', 'display_name': '布林带策略'}
        ]
        logger.info(f"基础择时策略列表: {[s['display_name'] for s in timing_strategies]}")
        
        # 只有配置文件存在时才添加顺势宝策略
        if has_valid_config:
            timing_strategies.append({'name': 'macd_bollinger', 'display_name': '顺势宝'})
            logger.info("检测到有效配置文件，添加顺势宝策略")
        else:
            logger.info("未检测到有效配置文件，不添加顺势宝策略")
        
        return jsonify({'success': True, 'strategies': timing_strategies})
    except Exception as e:
        logger.error(f"获取择时策略列表失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/strategy/has-config')
def check_strategy_config():
    """检查策略配置文件是否存在"""
    logger.info("检查策略配置文件是否存在")
    try:
        from utils.feature_config_checker import FeatureConfigChecker
        
        checker = FeatureConfigChecker()
        valid_files, expire_date = checker.check_config()
        has_valid_config = bool(valid_files)
        
        logger.info(f"配置文件检查结果: has_valid_config={has_valid_config}, expire_date={expire_date}")
        
        return jsonify({
            'success': True,
            'has_config': has_valid_config,
            'expire_date': expire_date
        })
    except Exception as e:
        logger.error(f"检查配置文件失败: {str(e)}")
        return jsonify({
            'success': True,
            'has_config': False,
            'expire_date': None
        })


@app.route('/api/strategies/<name>/validate', methods=['POST'])
def validate_strategy_params(name):
    """验证策略参数 - 检查策略是否存在"""
    try:
        # 检查策略是否存在
        strategy = registry.strategies.get(name)
        
        if not strategy:
            return jsonify({'success': False, 'error': '策略不存在'})
        
        # 获取待验证的参数
        params = request.get_json() or {}
        
        # 在新架构中，只需检查策略存在即可
        # 参数验证由前端或策略类自身处理
        return jsonify({'success': True, 'message': '参数验证通过'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/strategies/<name>/params', methods=['POST'])
def save_strategy_params(name):
    """
    保存策略参数 - 更新策略的参数配置并持久化到文件
    只更新前端发送的参数，保留其他参数不变
    :param name: 策略名称
    :return: JSON响应
    """
    global registry
    
    try:
        # 检查策略是否存在
        if name not in registry.strategies:
            return jsonify({'success': False, 'error': '策略不存在'})
        
        # 获取待保存的参数
        params = request.get_json() or {}
        strategy = registry.strategies[name]
        
        # 将参数保存到配置文件
        import yaml
        config_path = Path("config/strategy_params.yaml")
        
        # 读取现有配置
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        
        # 确保 strategies 字段存在
        if 'strategies' not in config:
            config['strategies'] = {}
        
        # 确保该策略的配置存在
        if name not in config['strategies']:
            config['strategies'][name] = {}
        
        # 确保 params 字段存在
        if 'params' not in config['strategies'][name]:
            config['strategies'][name]['params'] = {}
        
        # 获取该策略的现有参数
        existing_params = config['strategies'][name]['params']
        
        # 只更新前端发送的参数，保留其他参数
        for param_name, param_value in params.items():
            # 获取原参数的类型进行转换
            if param_name in existing_params:
                param_type = type(existing_params[param_name])
            else:
                # 如果参数不存在，尝试从策略对象获取类型
                if param_name in strategy.params:
                    param_type = type(strategy.params[param_name])
                else:
                    param_type = type(param_value)
            
            try:
                if param_type == int:
                    existing_params[param_name] = int(param_value)
                elif param_type == float:
                    existing_params[param_name] = float(param_value)
                else:
                    existing_params[param_name] = param_value
            except (ValueError, TypeError):
                return jsonify({'success': False, 'error': f'参数{param_name}类型转换失败'})
        
        # 写回配置文件
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        
        # 记录参数保存
        func_logger = logging.getLogger(__name__)
        func_logger.info(f"参数已保存: {name}")
        func_logger.info(f"保存的参数: {params}")
        
        # 重新加载策略参数 - 使用global声明确保更新全局registry
        registry = get_registry("config/strategy_params.yaml")
        registry.auto_register_from_directory("strategy")
        
        return jsonify({'success': True, 'message': '参数保存成功'})
    except Exception as e:
        import traceback
        func_logger = logging.getLogger(__name__)
        func_logger.error(f"保存策略参数失败: {str(e)}")
        func_logger.error(f"错误堆栈: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/stats')
def get_stats():
    """获取系统统计信息"""
    try:
        # 从数据库获取所有股票代码
        stocks = db_manager.list_all_stocks()
        
        # 获取K线数据的最新日期（表示数据更新到了哪一天）
        # 使用SQL直接查询所有股票的最新日期
        sql = "SELECT MAX(date) as latest_date FROM stock_kline"
        result = db_manager.query_one(sql)
        latest_date = result['latest_date'] if result and result['latest_date'] else '-'
        
        return jsonify({
            'success': True,
            'data': {
                'total_stocks': len(stocks),
                'latest_date': latest_date,
                'strategies': len(registry.strategies)
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/config', methods=['GET'])
def get_config():
    """获取配置"""
    try:
        config_file = Path("config/strategy_params.yaml")
        if config_file.exists():
            import yaml
            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            return jsonify({'success': True, 'data': config})
        return jsonify({'success': False, 'error': '配置文件不存在'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/config', methods=['POST'])
def update_config():
    """
    更新配置 - 只更新指定的参数，保留其他参数
    """
    try:
        import yaml
        from pathlib import Path
        
        # 获取前端发送的配置更新
        update_data = request.json or {}
        
        # 读取现有配置
        config_file = Path("config/strategy_params.yaml")
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
        
        # 确保 strategies 字段存在
        if 'strategies' not in config:
            config['strategies'] = {}
        
        # 更新指定策略的参数
        # update_data 格式: {strategy_name: {param_name: value, ...}, ...}
        for strategy_name, params_update in update_data.items():
            if strategy_name not in config['strategies']:
                config['strategies'][strategy_name] = {}
            
            # 获取该策略的现有配置
            strategy_config = config['strategies'][strategy_name]
            
            # 确保 params 字段存在
            if 'params' not in strategy_config:
                strategy_config['params'] = {}
            
            # 只更新指定的参数，保留其他参数
            for param_name, param_value in params_update.items():
                strategy_config['params'][param_name] = param_value
        
        # 写回配置文件
        with open(config_file, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        
        # 记录配置更新
        func_logger = logging.getLogger(__name__)
        func_logger.info(f"配置已更新")
        func_logger.info(f"更新的数据: {update_data}")
        
        # 重新加载策略
        global registry
        registry = get_registry("config/strategy_params.yaml")
        registry.auto_register_from_directory("strategy")
        
        return jsonify({'success': True, 'message': '配置更新成功'})
    except Exception as e:
        import traceback
        func_logger = logging.getLogger(__name__)
        func_logger.error(f"更新配置失败: {str(e)}")
        func_logger.error(f"错误堆栈: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)})


def emit_update_progress():
    """通过WebSocket发送更新进度"""
    socketio.emit('update_progress', {
        'running': update_status['running'],
        'progress': update_status['progress'],
        'total': update_status['total'],
        'success': update_status['success'],
        'failed': update_status['failed'],
        'message': update_status['message'],
        'start_time': update_status['start_time'],
        'end_time': update_status['end_time']
    }, namespace='/')


def emit_init_progress():
    """通过WebSocket发送初始化进度
    
    改进点：
    - 添加连接状态检查
    - 添加详细的日志记录
    - 添加异常处理
    """
    try:
        # 检查 socketio 是否可用
        if not socketio:
            logger.warning("socketio 不可用，无法发送初始化进度")
            return
        
        # 获取初始化进度
        progress = data_collection_service.get_init_progress()
        
        # 发送进度到所有连接的客户端
        socketio.emit('init_progress', progress, namespace='/')
        
        # 记录发送成功的日志（仅在进度变化时记录，避免日志过多）
        if progress.get('progress', 0) % 10 == 0 or progress.get('status') in ['completed', 'failed']:
            logger.debug(f"已发送初始化进度: {progress.get('progress', 0)}% - {progress.get('status', 'unknown')}")
    
    except Exception as e:
        logger.error(f"发送初始化进度失败: {str(e)}", exc_info=True)


@app.route('/api/update', methods=['POST'])
def trigger_update():
    """触发数据更新"""
    global update_status
    
    # 检查是否已有更新在运行
    if update_status['running']:
        return jsonify({'success': False, 'error': '已有更新任务在运行中'})
    
    # 获取参数
    max_stocks = request.json.get('max_stocks') if request.json else None
    
    # 在后台线程中执行更新
    def update_thread():
        global update_status
        try:
            update_status['running'] = True
            update_status['start_time'] = dt.now().strftime('%Y-%m-%d %H:%M:%S')
            update_status['message'] = '正在更新数据...'
            emit_update_progress()
            
            # 执行更新
            quant_system.update_data(max_stocks=max_stocks)
            
            update_status['success'] += 1
            update_status['message'] = '数据更新完成'
            update_status['end_time'] = dt.now().strftime('%Y-%m-%d %H:%M:%S')
            emit_update_progress()
        except Exception as e:
            update_status['failed'] += 1
            update_status['message'] = f'更新失败: {str(e)}'
            update_status['end_time'] = dt.now().strftime('%Y-%m-%d %H:%M:%S')
            emit_update_progress()
        finally:
            update_status['running'] = False
            emit_update_progress()
    
    # 启动后台线程
    thread = threading.Thread(target=update_thread, daemon=True)
    thread.start()
    
    return jsonify({
        'success': True,
        'message': '数据更新已启动',
        'status': update_status
    })


@app.route('/api/update/status', methods=['GET'])
def get_update_status():
    """获取更新状态"""
    return jsonify({
        'success': True,
        'status': update_status
    })





@app.route('/api/selection-history', methods=['GET'])
def get_selection_history():
    """
    查询选股历史
    
    参数：
        strategy_name: 策略名称（可选）
        start_date: 开始日期 YYYY-MM-DD（可选）
        end_date: 结束日期 YYYY-MM-DD（可选）
        stock_code: 股票代码（可选）
        page: 分页页码，默认1
        limit: 每页数量，默认20
    
    返回：
        {
            'success': True,
            'total': 100,
            'page': 1,
            'limit': 20,
            'data': [...]
        }
    """
    try:
        # 获取查询参数
        strategy_name = request.args.get('strategy_name', '')
        start_date = request.args.get('start_date', '')
        end_date = request.args.get('end_date', '')
        stock_code = request.args.get('stock_code', '')
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        
        # 构建筛选条件
        filters = {}
        if strategy_name:
            filters['strategy_name'] = strategy_name
        if start_date:
            filters['start_date'] = start_date
        if end_date:
            filters['end_date'] = end_date
        if stock_code:
            filters['stock_code'] = stock_code
        
        # 查询选股历史
        result = selection_record_manager.get_selection_history(
            filters=filters,
            page=page,
            limit=limit
        )
        
        # 转换 numpy 类型为 Python 原生类型
        if result.get('success') and result.get('data'):
            for record in result['data']:
                for key, value in record.items():
                    # 将 numpy 类型转换为 Python 原生类型
                    if hasattr(value, 'item'):
                        record[key] = value.item()
        
        return jsonify(result)
    
    except Exception as e:
        logger.error(f"查询选股历史失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


# ==================== 股票分析相关路由 ====================




@app.route('/api/analyze-stock', methods=['POST'])
def analyze_stock():
    """
    分析股票
    
    参数：
        stock_code: 股票代码
        period: 分析周期
    
    返回：
        {
            'success': True,
            'data': 分析结果
        }
    """
    try:
        # 获取请求参数
        data = request.json or {}
        stock_code = data.get('stock_code', '')
        period = data.get('period', '30d')
        
        if not stock_code:
            return jsonify({'success': False, 'message': '股票代码不能为空'})
        
        # 分析股票
        analysis_result = stock_analyzer.analyze(stock_code, period=period)
        
        if not analysis_result:
            return jsonify({'success': False, 'message': '分析失败'})
        
        # 转换numpy类型为Python原生类型，同时清理NaN/Infinity
        def convert_numpy_types(obj):
            """递归转换numpy类型，将NaN/Infinity替换为None"""
            if isinstance(obj, dict):
                return {k: convert_numpy_types(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_numpy_types(item) for item in obj]
            # 先检查float类型的NaN/Infinity（含numpy.floating）
            elif isinstance(obj, float):
                if math.isnan(obj) or math.isinf(obj):
                    return None
                return obj
            elif hasattr(obj, 'item'):
                # numpy标量类型，先转为Python原生类型再检查NaN
                val = obj.item()
                if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                    return None
                return val
            elif isinstance(obj, np.ndarray):
                return convert_numpy_types(obj.tolist())
            elif isinstance(obj, pd.Timestamp):
                return obj.strftime('%Y-%m-%d %H:%M:%S')
            # 使用hasattr检查其他numpy类型
            elif hasattr(obj, 'dtype'):
                val = obj.item()
                if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                    return None
                return val
            else:
                return obj
        
        # 转换分析结果
        analysis_result = convert_numpy_types(analysis_result)
        
        # 使用json.dumps并指定default参数来处理所有numpy类型
        import json
        def default_handler(obj):
            """处理json.dumps无法序列化的类型"""
            if isinstance(obj, np.integer):
                return int(obj)
            elif isinstance(obj, np.floating):
                # 检查NaN/Infinity
                val = float(obj)
                if math.isnan(val) or math.isinf(val):
                    return None
                return val
            elif isinstance(obj, np.ndarray):
                return convert_numpy_types(obj.tolist())
            elif isinstance(obj, pd.Timestamp):
                return obj.strftime('%Y-%m-%d %H:%M:%S')
            else:
                return obj
        
        response_data = {
            'success': True,
            'data': analysis_result
        }
        json_str = json.dumps(response_data, default=default_handler)
        return app.response_class(
            response=json_str,
            mimetype='application/json'
        )
        
    except Exception as e:
        logger.error(f"分析股票失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/analysis-history')
def get_analysis_history():
    """
    获取分析历史
    
    返回：
        {
            'success': True,
            'data': 分析历史列表
        }
    """
    try:
        # 这里简化处理，实际应该从数据库获取
        # 暂时返回模拟数据
        history = [
            {
                'id': 1,
                'stock_code': '600519',
                'stock_name': '贵州茅台',
                'analysis_time': '2026-03-23 10:00:00',
                'rating': '买入'
            },
            {
                'id': 2,
                'stock_code': '000858',
                'stock_name': '五粮液',
                'analysis_time': '2026-03-22 15:30:00',
                'rating': '中性'
            }
        ]
        
        return jsonify({
            'success': True,
            'data': history
        })
        
    except Exception as e:
        logger.error(f"获取分析历史失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/export-report')
def export_report():
    """
    导出分析报告
    
    参数：
        stock_code: 股票代码
    
    返回：
        报告文件
    """
    try:
        stock_code = request.args.get('stock_code', '')
        
        if not stock_code:
            return jsonify({'success': False, 'message': '股票代码不能为空'})
        
        # 生成报告
        report_content, report_path = stock_analyzer.generate_report(stock_code)
        
        # 返回报告文件
        return send_from_directory(
            directory=str(Path(report_path).parent),
            path=Path(report_path).name,
            as_attachment=True
        )
        
    except Exception as e:
        logger.error(f"导出报告失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/report/<int:report_id>')
def get_report(report_id):
    """
    获取分析报告
    
    参数：
        report_id: 报告ID
    
    返回：
        报告内容
    """
    try:
        # 这里简化处理，实际应该根据ID获取报告
        # 暂时返回模拟数据
        return jsonify({
            'success': True,
            'message': '报告获取功能暂未实现'
        })
        
    except Exception as e:
        logger.error(f"获取报告失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


# ==================== K线初始化 API ====================

@app.route('/api/data/kline/init', methods=['POST'])
def init_kline_data():
    """
    手动触发K线数据初始化
    
    请求参数：
        stock_codes: 股票代码列表（可选，不提供则使用全部）
        years: 历史年份数（可选，默认3年）
        batch_size: 每批处理的股票数（可选，默认100）
    
    返回：
        初始化任务信息
    """
    # 获取请求参数
    try:
        data = request.json or {}
        stock_codes = data.get('stock_codes')
        years = data.get('years', 3)
        batch_size = data.get('batch_size', 100)
        
        logger.info(f"开始K线初始化: 股票数={len(stock_codes) if stock_codes else '全部'}, 年份={years}")
        
        # 先检查是否可以初始化（不启动线程）
        result = kline_initializer._check_kline_initialized()
        if result:
            return jsonify({
                'success': False,
                'message': 'K线数据初始化已经完成，无需再次初始化'
            })
        
        # 在后台线程中执行初始化
        def run_init():
            kline_initializer.initialize_kline_data(stock_codes, years, batch_size)
        
        # 启动后台线程
        init_thread = threading.Thread(target=run_init, daemon=True)
        init_thread.start()
        
        # 返回任务信息
        return jsonify({
            'success': True,
            'task_id': kline_initializer.progress['task_id'],
            'message': 'K线初始化已启动',
            'total_stocks': len(stock_codes) if stock_codes else '全部',
            'estimated_time': '30-60分钟'
        })
    
    except Exception as e:
        logger.error(f"启动K线初始化失败: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/data/kline/init/progress')
def get_kline_init_progress():
    """
    获取K线初始化进度
    
    查询参数：
        task_id: 任务ID（可选）
    
    返回：
        初始化进度信息
    """
    # 获取进度信息
    progress = kline_initializer.get_progress()
    
    return jsonify({
        'success': True,
        'data': progress
    })


# ==================== 数据采集 API ====================

@app.route('/api/data/init/config')
def get_init_config():
    """
    获取数据初始化配置
    
    返回：
        初始化配置信息
    """
    try:
        config = data_collection_service.get_init_config()
        return jsonify({
            'success': True,
            'data': config
        })
    except Exception as e:
        logger.error(f"获取初始化配置失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/init/start', methods=['POST'])
def start_initialization():
    """
    开始数据初始化
    
    请求体：
        {
            'type': 'full|structure_only|custom',
            'options': {...}
        }
    
    返回：
        初始化任务信息
    """
    try:
        data = request.get_json()
        init_type = data.get('type', 'full')
        options = data.get('options', {})
        
        # 启动初始化任务
        result = data_collection_service.start_initialization(init_type, options)
        
        return jsonify({
            'success': result['success'],
            'message': result['message'],
            'taskId': result.get('taskId')
        })
    except Exception as e:
        logger.error(f"启动初始化失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/init/progress')
def get_init_progress():
    """
    获取数据初始化进度
    
    返回：
        初始化进度信息
    """
    try:
        progress = data_collection_service.get_init_progress()
        # 返回测试期望的格式
        return jsonify({
            'success': True,
            'data': progress
        })
    except Exception as e:
        logger.error(f"获取初始化进度失败: {str(e)}")
        return jsonify({
            'success': False,
            'data': {
                'status': 'failed',
                'message': str(e),
                'logs': []
            }
        })


@app.route('/api/data/init/cancel', methods=['POST'])
def cancel_initialization():
    """
    取消数据初始化
    
    返回：
        取消结果
    """
    try:
        result = data_collection_service.cancel_initialization()
        return jsonify({
            'success': result['success'],
            'message': result['message']
        })
    except Exception as e:
        logger.error(f"取消初始化失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/init/pause', methods=['POST'])
def pause_initialization():
    """
    暂停数据初始化
    
    返回：
        暂停结果
    """
    try:
        result = data_collection_service.pause_initialization()
        return jsonify({
            'success': result['success'],
            'message': result['message']
        })
    except Exception as e:
        logger.error(f"暂停初始化失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/init/resume', methods=['POST'])
def resume_initialization():
    """
    恢复数据初始化
    
    返回：
        恢复结果
    """
    try:
        result = data_collection_service.resume_initialization()
        return jsonify({
            'success': result['success'],
            'message': result['message']
        })
    except Exception as e:
        logger.error(f"恢复初始化失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/check')
def check_data_completeness():
    """
    检查数据完整性
    
    返回：
        各数据表的完整性信息
    """
    try:
        result = data_collection_service.check_data_completeness()
        return jsonify(result)
    except Exception as e:
        logger.error(f"检查数据完整性失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/kline/completeness')
def check_kline_completeness():
    """返回桌面启动时使用的 K 线完整性状态。"""
    try:
        result = data_collection_service.check_data_completeness()
        if not result.get('success'):
            return jsonify(result)
        intraday = check_intraday_completeness(db_manager)
        return jsonify({
            'success': True,
            'data': {**result['data']['kline'], 'intraday': intraday},
            'lastUpdate': result.get('lastUpdate'),
        })
    except Exception as exc:
        logger.error(f"检查 K 线完整性失败: {exc}")
        return jsonify({'success': False, 'message': str(exc)})


@app.route('/api/data/kline/download', methods=['POST'])
def download_missing_kline():
    """启动缺失历史 K 线下载，不删除已有数据。"""
    try:
        result = data_collection_service.start_kline_download()
        return jsonify(result)
    except Exception as exc:
        logger.error(f"启动缺失 K 线下载失败: {exc}")
        return jsonify({'success': False, 'message': str(exc)})


@app.route('/api/data/intraday/download', methods=['POST'])
def download_missing_intraday():
    """启动缺失的5分钟历史 K 线下载。"""
    try:
        return jsonify(start_intraday_download(db_manager))
    except Exception as exc:
        logger.error(f"启动5分钟 K 线下载失败: {exc}")
        return jsonify({'success': False, 'message': str(exc)})


@app.route('/api/data/intraday/progress')
def get_intraday_progress():
    """返回启动阶段5分钟 K 线下载进度。"""
    return jsonify({'success': True, 'data': get_intraday_download_progress()})


@app.route('/api/app/restart', methods=['POST'])
def restart_application():
    """Restart the desktop process after all required historical data is ready."""
    try:
        completeness = data_collection_service.check_data_completeness()
        intraday = check_intraday_completeness(db_manager)
        daily_complete = bool(
            completeness.get('success')
            and completeness.get('data', {}).get('kline', {}).get('complete')
        )
        if not daily_complete or not intraday.get('complete'):
            return jsonify({
                'success': False,
                'message': '行情数据仍未完整，暂不重启应用',
            }), 409

        def relaunch():
            threading.Event().wait(1.2)
            logger.info('日线和5分钟K线已完整，正在自动重启桌面应用')
            os.execv(sys.executable, [sys.executable, *sys.argv])

        threading.Thread(
            target=relaunch,
            name='quantpilot-auto-restart',
            daemon=True,
        ).start()
        return jsonify({'success': True, 'message': '数据已完整，应用即将重启'})
    except Exception as exc:
        logger.exception('自动重启应用失败')
        return jsonify({'success': False, 'message': str(exc)}), 500


@app.route('/api/data/status')
def get_data_status():
    """
    获取数据状态摘要
    
    返回：
        数据状态信息
    """
    try:
        status = data_collection_service.get_data_status()
        return jsonify({
            'success': True,
            'data': status
        })
    except Exception as e:
        logger.error(f"获取数据状态失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/reinit', methods=['POST'])
def start_reinit():
    """
    强制重新初始化数据
    
    请求体（可选）：
        {
            'stockCount': 2000,
            'klineDays': 250
        }
    
    返回：
        任务信息
    """
    try:
        data = request.get_json() or {}
        stock_count = data.get('stockCount')
        kline_days = data.get('klineDays')
        
        result = data_collection_service.start_reinit(stock_count, kline_days)
        return jsonify(result)
    except Exception as e:
        logger.error(f"启动重新初始化失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/update/config')
def get_update_config():
    """
    获取数据更新配置
    
    返回：
        更新配置信息
    """
    try:
        config = data_collection_service.get_update_config()
        return jsonify({
            'success': True,
            'data': config
        })
    except Exception as e:
        logger.error(f"获取更新配置失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/update/start', methods=['POST'])
def start_update():
    """
    开始数据更新
    
    请求体：
        {
            'updateTypes': ['basic_data', 'history_data', ...]
        }
    
    返回：
        更新任务信息
    """
    try:
        data = request.get_json()
        update_types = data.get('updateTypes', None)
        
        # 启动更新任务
        result = data_collection_service.start_update(update_types)
        
        return jsonify({
            'success': result['success'],
            'message': result['message'],
            'taskId': result.get('taskId')
        })
    except Exception as e:
        logger.error(f"启动更新失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/update/progress')
def get_update_progress():
    """
    获取数据更新进度
    
    返回：
        更新进度信息
    """
    try:
        progress = data_collection_service.get_update_progress()
        return jsonify({
            'success': True,
            'data': progress
        })
    except Exception as e:
        logger.error(f"获取更新进度失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/update/cancel', methods=['POST'])
def cancel_update():
    """
    取消数据更新
    
    返回：
        取消结果
    """
    try:
        result = data_collection_service.cancel_update()
        return jsonify({
            'success': result['success'],
            'message': result['message']
        })
    except Exception as e:
        logger.error(f"取消更新失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/update/pause', methods=['POST'])
def pause_update():
    """
    暂停数据更新
    
    返回：
        暂停结果
    """
    try:
        result = data_collection_service.pause_update()
        return jsonify({
            'success': result['success'],
            'message': result['message']
        })
    except Exception as e:
        logger.error(f"暂停更新失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/update/resume', methods=['POST'])
def resume_update():
    """
    恢复数据更新
    
    返回：
        恢复结果
    """
    try:
        result = data_collection_service.resume_update()
        return jsonify({
            'success': result['success'],
            'message': result['message']
        })
    except Exception as e:
        logger.error(f"恢复更新失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/update/last-update-time')
def get_last_update_time():
    """
    获取上次更新时间
    
    返回：
        {
            'success': bool,
            'data': {
                'lastUpdateTime': '2026-04-02 15:30:00',
                'lastUpdateDate': '2026-04-02'
            }
        }
    """
    try:
        # 获取交易时间验证器
        from utils.trading_time_validator import TradingTimeValidator
        from utils.db_manager import DBManager
        
        # 创建数据库管理器
        from utils.global_db import get_global_db
        db_manager = get_global_db()
        validator = TradingTimeValidator(db_manager)
        
        # 获取上次更新日期
        last_update_date = validator.get_last_update_date()
        
        if not last_update_date:
            # 如果没有更新记录，返回默认值
            return jsonify({
                'success': True,
                'data': {
                    'lastUpdateTime': '未更新',
                    'lastUpdateDate': ''
                }
            })
        
        # 查询该日期的更新时间
        sql = "SELECT update_time FROM update_log WHERE update_date = ?"
        result = db_manager.query_one(sql, (last_update_date,))
        
        if result:
            last_update_time = result['update_time']
        else:
            last_update_time = f"{last_update_date} 00:00:00"
        
        return jsonify({
            'success': True,
            'data': {
                'lastUpdateTime': last_update_time,
                'lastUpdateDate': last_update_date
            }
        })
    
    except Exception as e:
        logger.error(f"获取上次更新时间失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/update/rebuild-recent-exdividend', methods=['POST'])
def rebuild_recent_exdividend():
    """
    重建近期（默认2个月内）发生除权的股票历史数据
    
    请求体：
        {
            'months': 2  # 可选，默认2个月
        }
    
    返回：
        {
            'success': bool,
            'message': str,
            'data': {
                'detectedCount': 检测到的除权股票数量,
                'rebuiltCount': 成功重建的股票数量,
                'stocks': ['股票代码列表']
            }
        }
    """
    try:
        data = request.get_json()
        months = data.get('months', 2)
        
        # 获取KlineUpdater实例
        from utils.kline_updater import KlineUpdater
        from utils.global_db import get_global_db
        from utils.stock_data_fetcher import StockDataFetcher
        
        # 检查 Tushare 配置是否存在，没有则跳过除权重建
        tushare_config_path = Path('config/tushare_config.json')
        if not tushare_config_path.exists():
            logger.info("未找到 Tushare 配置文件，跳过除权重建")
            return jsonify({
                'success': True,
                'message': '未配置 Tushare，跳过除权重建',
                'data': {'detectedCount': 0, 'rebuiltCount': 0, 'stocks': []}
            })

        fetcher = StockDataFetcher()
        kline_updater = KlineUpdater(get_global_db(), fetcher)
        
        # 获取当前日期
        from datetime import datetime, timedelta
        end_date = datetime.now().strftime('%Y%m%d')
        
        # 计算开始日期（2个月前）
        start_date = (datetime.now() - timedelta(days=months * 30)).strftime('%Y%m%d')
        
        # 获取所有股票代码
        stock_codes = fetcher.get_all_stock_codes()
        
        # 调用现有的除权检测和重建方法
        result = kline_updater.check_exdividend_and_rebuild(stock_codes, end_date, start_date)
        
        return jsonify({
            'success': True,
            'message': f"成功检测到 {len(result.get('exdividend_stocks', []))} 只除权股票，已重建 {len(result.get('rebuilt_stocks', []))} 只",
            'data': {
                'detectedCount': len(result.get('exdividend_stocks', [])),
                'rebuiltCount': len(result.get('rebuilt_stocks', [])),
                'stocks': result.get('rebuilt_stocks', []),
                'startDate': start_date,
                'endDate': end_date
            }
        })
    
    except Exception as e:
        logger.error(f"重建近期除权股票失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/tables/info')
def get_tables_info():
    """
    获取新增表的信息
    
    返回：
        新增表的信息
    """
    try:
        info = data_collection_service.get_tables_info()
        return jsonify({
            'success': True,
            'data': info
        })
    except Exception as e:
        logger.error(f"获取表信息失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/data/tables/stats')
def get_tables_stats():
    """
    获取表数据统计
    
    返回：
        表数据统计信息
    """
    try:
        stats = data_collection_service.get_tables_stats()
        return jsonify({
            'success': True,
            'data': stats
        })
    except Exception as e:
        logger.error(f"获取表统计失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


# ==================== 市场温度计 API ====================

@app.route('/api/market-temperature/calculate', methods=['POST'])
def calculate_market_temperature():
    """
    计算市场温度
    
    请求参数：
        trade_date: 交易日期（YYYYMMDD格式），可选，默认为今日
        use_cache: 是否使用缓存，默认True
    
    返回：
        市场温度数据，包含：
        - trade_date: 交易日期
        - temperature: 综合温度值（0-100）
        - status: 市场状态
        - position_ratio: 仓位系数
        - action: 狩猎场执行规则
        - 各维度得分和原始数据
    """
    try:
        data = request.get_json() or {}
        trade_date = data.get('trade_date')
        use_cache = data.get('use_cache', True)
        
        # 如果未指定日期，使用今日
        if not trade_date:
            from datetime import date
            trade_date = date.today().strftime('%Y%m%d')
        
        # 调用温度计算器
        from utils.market_temperature import MarketTemperature, DataNotAvailableError
        mt = MarketTemperature()
        result = mt.calculate(trade_date, use_cache=use_cache)
        
        return jsonify({
            'success': True,
            'data': clean_data_for_json(result)
        })
    except DataNotAvailableError as e:
        # 数据不可用（非交易日或API无数据）
        logger.info(f"市场温度数据不可用: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e),
            'error_type': 'data_not_available'
        })
    except Exception as e:
        logger.error(f"计算市场温度失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e),
            'error_type': 'unknown_error'
        })


@app.route('/api/market-temperature/query', methods=['GET'])
def query_market_temperature():
    """
    查询市场温度数据
    
    请求参数：
        trade_date: 交易日期（YYYYMMDD格式）
    
    返回：
        市场温度数据
    """
    try:
        trade_date = request.args.get('trade_date')
        
        if not trade_date:
            return jsonify({
                'success': False,
                'message': '缺少trade_date参数'
            })
        
        from trading.market_temperature_dao import MarketTemperatureDAO
        dao = MarketTemperatureDAO()
        result = dao.query_by_date(trade_date)
        
        if result:
            return jsonify({
                'success': True,
                'data': clean_data_for_json(result)
            })
        else:
            return jsonify({
                'success': False,
                'message': f'未找到日期{trade_date}的温度数据'
            })
    except Exception as e:
        logger.error(f"查询市场温度失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/market-temperature/latest', methods=['GET'])
def get_latest_market_temperature():
    """
    获取最新的市场温度数据
    
    返回：
        最新市场温度数据
    """
    try:
        from trading.market_temperature_dao import MarketTemperatureDAO
        dao = MarketTemperatureDAO()
        result = dao.get_latest()
        
        if result:
            return jsonify({
                'success': True,
                'data': clean_data_for_json(result)
            })
        else:
            return jsonify({
                'success': False,
                'message': '暂无温度数据'
            })
    except Exception as e:
        logger.error(f"获取最新市场温度失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/market-temperature/trend', methods=['GET'])
def get_market_temperature_trend():
    """
    获取市场温度趋势
    
    请求参数：
        days: 天数，默认5天
    
    返回：
        趋势数据，包含：
        - trend: 温度趋势列表
        - avg_temperature: 平均温度
        - max_temperature: 最高温度
        - min_temperature: 最低温度
        - latest_status: 最新状态
        - latest_temperature: 最新温度
        - latest_trade_date: 最新交易日
    """
    try:
        days = int(request.args.get('days', 5))
        
        from trading.market_temperature_dao import MarketTemperatureDAO
        dao = MarketTemperatureDAO()
        result = dao.get_trend(days)
        
        return jsonify({
            'success': True,
            'data': clean_data_for_json(result)
        })
    except Exception as e:
        logger.error(f"获取温度趋势失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/market-temperature/position-ratio', methods=['GET'])
def get_market_temperature_position_ratio():
    """
    获取指定日期的仓位系数
    
    请求参数：
        trade_date: 交易日期（YYYYMMDD格式），可选，默认为今日
    
    返回：
        仓位系数数据
    """
    try:
        trade_date = request.args.get('trade_date')
        
        # 如果未指定日期，使用今日
        if not trade_date:
            from datetime import date
            trade_date = date.today().strftime('%Y%m%d')
        
        # 获取温度数据（只返回已有数据，不会自动生成）
        from utils.market_temperature import MarketTemperature, DataNotAvailableError
        mt = MarketTemperature()
        result = mt.calculate(trade_date, use_cache=True)
        
        return jsonify({
            'success': True,
            'data': clean_data_for_json({
                'trade_date': result.get('trade_date'),
                'position_ratio': result.get('position_ratio'),
                'temperature': result.get('temperature'),
                'status': result.get('status'),
                'action': result.get('action')
            })
        })
    except DataNotAvailableError as e:
        logger.info(f"仓位系数数据不可用: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e),
            'error_type': 'data_not_available'
        })
    except Exception as e:
        logger.error(f"获取仓位系数失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/money-flow/select', methods=['POST'])
def money_flow_select():
    """
    持续资金流入选股
    
    请求参数（JSON）：
        days: 连续天数，默认10
        min_net_amount: 最小日均净流入(万元)，默认0
        end_date: 结束日期（YYYYMMDD），默认今日
    
    返回：
        选股结果列表
    """
    try:
        data = request.json or {}
        days = data.get('days', 10)
        min_net_amount = data.get('min_net_amount', 0)
        end_date = data.get('end_date')
        
        logger.info(f"执行资金流向选股: days={days}, min_net_amount={min_net_amount}, end_date={end_date}")
        
        from trading.money_flow_dao import MoneyFlowDAO
        dao = MoneyFlowDAO()
        results = dao.select_continuous_inflow_stocks(
            end_date=end_date,
            days=days,
            min_net_amount=min_net_amount
        )
        
        return jsonify({
            'success': True,
            'data': results,
            'total': len(results),
            'params': {
                'days': days,
                'min_net_amount': min_net_amount
            }
        })
        
    except Exception as e:
        logger.error(f"资金流向选股失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/backtest/constraints', methods=['GET'])
def get_backtest_constraints():
    """
    获取回测期间的温度约束预览
    
    请求参数：
        start_date: 回测开始日期（YYYYMMDD格式）
        end_date: 回测结束日期（YYYYMMDD格式）
        mode: 约束模式，count/position/both，默认both
    
    返回：
        批量约束结果，包含每日约束详情和汇总统计
    """
    try:
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        mode = request.args.get('mode', 'both')
        
        if not start_date or not end_date:
            return jsonify({
                'success': False,
                'message': '缺少start_date或end_date参数'
            })
        
        from trading.backtest_temp_constraint import BacktestTempConstraint
        constraint = BacktestTempConstraint()
        
        # 生成日期范围内的交易日列表
        trade_dates = _generate_trade_dates(start_date, end_date)
        
        result = constraint.get_batch_constraints(trade_dates, mode)
        
        return jsonify({
            'success': True,
            'data': clean_data_for_json(result)
        })
    except Exception as e:
        logger.error(f"获取回测约束失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


@app.route('/api/backtest/constraint', methods=['GET'])
def get_backtest_constraint():
    """
    获取单日回测温度约束
    
    请求参数：
        trade_date: 交易日期（YYYYMMDD格式）
        mode: 约束模式，count/position/both，默认both
    
    返回：
        单日约束详情
    """
    try:
        trade_date = request.args.get('trade_date')
        mode = request.args.get('mode', 'both')
        
        if not trade_date:
            return jsonify({
                'success': False,
                'message': '缺少trade_date参数'
            })
        
        from trading.market_temperature_dao import MarketTemperatureDAO
        from trading.backtest_temp_constraint import BacktestTempConstraint
        
        dao = MarketTemperatureDAO()
        temp_data = dao.query_by_date(trade_date)
        
        if not temp_data:
            return jsonify({
                'success': False,
                'message': f'未找到日期{trade_date}的温度数据'
            })
        
        constraint = BacktestTempConstraint(dao)
        result = constraint.get_constraint(temp_data['temperature'], mode)
        result['trade_date'] = trade_date
        result['temperature'] = temp_data['temperature']
        result['status'] = temp_data['status']
        
        return jsonify({
            'success': True,
            'data': clean_data_for_json(result)
        })
    except Exception as e:
        logger.error(f"获取单日约束失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        })


def _generate_trade_dates(start_date: str, end_date: str) -> list:
    """
    生成日期范围内的交易日列表（工作日）
    
    Args:
        start_date: 开始日期（YYYYMMDD）
        end_date: 结束日期（YYYYMMDD）
    
    Returns:
        交易日列表
    """
    from datetime import datetime, timedelta, timedelta
    
    start = dt.strptime(start_date, '%Y%m%d')
    end = dt.strptime(end_date, '%Y%m%d')
    
    dates = []
    current = start
    
    while current <= end:
        # 只包含周一到周五
        if current.weekday() < 5:
            dates.append(current.strftime('%Y%m%d'))
        current += timedelta(days=1)
    
    return dates


# ==================== 策略运行相关路由 ====================

# 策略运行器延迟初始化（按需加载）
strategy_runner = None

# 策略运行锁（防止并发执行）
import threading
_strategy_run_lock = threading.Lock()

def get_strategy_runner(auto_init=False):
    """获取策略运行器实例（延迟初始化）
    
    Args:
        auto_init: 是否自动初始化，默认False（禁止自动初始化）
    
    Returns:
        策略运行器实例，如果未初始化且auto_init=False则返回None
    """
    global strategy_runner
    if strategy_runner is None and auto_init:
        try:
            from trading.strategy_runner import StrategyRunner
            logger.info("开始初始化策略运行器...")
            strategy_runner = StrategyRunner()
            logger.info("策略运行器初始化成功")
        except Exception as e:
            logger.error(f"策略运行器初始化失败: {str(e)}")
            import traceback
            logger.error(f"初始化错误堆栈: {traceback.format_exc()}")
            strategy_runner = None
    return strategy_runner





@app.route('/api/strategy/run-batch', methods=['POST'])
def run_strategy_batch():
    """
    批量运行策略（所有策略执行完成后统一保存文件）
    
    参数：
        tasks: 任务列表，每个任务包含：
            - selection_strategy: 选股策略名称
            - timing_strategy: 择时策略名称
    
    返回：
        {"status": "success", "message": "批量策略运行完成", "results": [...]}
    """
    try:
        # 不自动初始化策略运行器，需要手动调用初始化接口
        runner = get_strategy_runner(auto_init=False)
        if not runner:
            return jsonify({"status": "failed", "message": "策略运行器未初始化，请先调用初始化接口"})
        
        data = request.json or {}
        tasks = data.get('tasks', [])
        
        if not tasks:
            return jsonify({"status": "failed", "message": "没有任务需要执行"})
        
        logger.info(f"批量执行 {len(tasks)} 个策略任务")
        
        # 获取配置参数
        config = data.get('config', {})
        if not config:
            backtest_config = runner._get_backtest_config()
            if backtest_config:
                config = backtest_config
        
        # 检查是否有任务使用海龟策略，从配置文件读取海龟策略参数（与 run_strategy 保持一致）
        has_turtle = any(task.get('timing_strategy') == 'turtle' for task in tasks)
        if has_turtle:
            try:
                config_manager = StrategyConfigManager()
                turtle_config = config_manager.get_strategy_config('TurtleStrategy')
                turtle_params = turtle_config.get('params', {})
                logger.info(f"从配置文件读取海龟策略参数: n_entry={turtle_params.get('n_entry')}, "
                           f"n_exit={turtle_params.get('n_exit')}, atr_period={turtle_params.get('atr_period')}")
                # 将海龟策略参数添加到config中
                config['n_entry'] = turtle_params.get('n_entry')
                config['n_exit'] = turtle_params.get('n_exit')
                config['atr_period'] = turtle_params.get('atr_period')
                config['entry_atr'] = turtle_params.get('entry_atr')
                config['add_atr'] = turtle_params.get('add_atr')
                config['exit_atr'] = turtle_params.get('exit_atr')
                config['base_position_amount'] = turtle_params.get('base_position_amount')
            except Exception as e:
                logger.warning(f"读取海龟策略配置失败，使用默认值: {str(e)}")
                # 使用默认值
                config['n_entry'] = 20
                config['n_exit'] = 10
                config['atr_period'] = 20
                config['entry_atr'] = 0.02
                config['add_atr'] = 0.5
                config['exit_atr'] = 2.0
                config['base_position_amount'] = 20000
        
        # 执行批量任务
        results = runner.run_strategies_batch(tasks, config)
        
        if results.get('status') == 'success':
            # 获取择时策略（所有任务使用相同的择时策略）
            timing_strategy = tasks[0].get('timing_strategy', 'support') if tasks else 'support'
            runner.save_task_record({
                'strategies': [task.get('selection_strategy', '') for task in tasks],
                'timing_strategy': timing_strategy,
                'initial_capital': config.get('initial_capital', 300000),
                'mode': 'realtime'
            })
        
        return jsonify(results)
    except Exception as e:
        logger.error(f"批量运行策略失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({"status": "failed", "message": str(e)})


@app.route('/api/strategy/initialize', methods=['POST'])
def initialize_strategy_runner():
    """
    手动初始化策略运行器
    
    返回：
        {"success": true, "message": "策略运行器初始化成功"}
    """
    try:
        global strategy_runner
        if strategy_runner is not None:
            return jsonify({"success": True, "message": "策略运行器已经初始化"})
        
        from trading.strategy_runner import StrategyRunner
        logger.info("手动初始化策略运行器...")
        strategy_runner = StrategyRunner()
        logger.info("策略运行器初始化成功")
        
        return jsonify({"success": True, "message": "策略运行器初始化成功"})
    except Exception as e:
        logger.error(f"策略运行器初始化失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({"success": False, "message": f"策略运行器初始化失败: {str(e)}"})


@app.route('/api/strategy/status')
def get_strategy_status():
    """
    获取策略运行状态
    
    返回：
        {"success": true, "data": {"date": "2026-04-24", "status": "completed", "strategy": "海龟策略"}}
    """
    try:
        # 不自动初始化策略运行器，只在有需要时才初始化
        runner = get_strategy_runner(auto_init=False)
        if not runner:
            return jsonify({"success": True, "data": {"date": "", "status": "not_initialized", "strategy": "",
                                                     "running": False, "selected_stocks": 0, "today_trades": 0, "last_run": "从未"}})
        
        # 获取当前工作日期
        working_date = runner.get_working_date()
        
        # 初始化当日数据（自动从最近有数据的交易日继承）
        runner.initialize_daily_data(working_date)
        
        # 检查是否已处理
        processed = runner.check_if_processed(working_date)
        
        # 检查是否正在运行（通过检查锁状态）
        running = False
        if _strategy_run_lock.locked():
            running = True
        
        # 获取今日选股数量（从股票池文件获取）
        selected_stocks = 0
        pool_file = runner.running_dir / "buy_candidate_pool.json"
        if pool_file.exists():
            try:
                with open(pool_file, 'r', encoding='utf-8') as f:
                    pool_data = json.load(f)
                    selected_stocks = len(pool_data.get('pool', []))
            except Exception as e:
                logger.warning(f"读取股票池文件失败: {str(e)}")
        
        # 获取今日交易笔数（从交易记录文件获取）
        today_trades = 0
        trades_file = runner.running_dir / f"trades_{working_date}.json"
        if trades_file.exists():
            try:
                with open(trades_file, 'r', encoding='utf-8') as f:
                    trades_data = json.load(f)
                    today_trades = len(trades_data)
            except Exception as e:
                logger.warning(f"读取交易记录文件失败: {str(e)}")
        
        # 获取最后运行时间（从任务历史获取）
        last_run = "从未"
        task_history = runner.get_task_history(limit=1)
        if task_history:
            last_run = task_history[0].get('timestamp', '从未')
        
        # 构建状态数据
        status_data = {
            "date": working_date,
            "status": "completed" if processed else "pending",
            "strategy": "",
            "running": running,
            "selected_stocks": selected_stocks,
            "today_trades": today_trades,
            "last_run": last_run
        }
        
        return jsonify({"success": True, "data": status_data})
    except Exception as e:
        logger.error(f"获取策略运行状态失败: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/portfolio')
def get_portfolio():
    """
    获取持仓信息
    
    返回：
        {"success": true, "data": {"positions": {...}, "initial_cash": 300000}}
    """
    try:
        # 延迟初始化策略运行器（仅在未初始化时才初始化）
        runner = get_strategy_runner()
        if not runner:
            logger.info("策略运行器未初始化，进行初始化...")
            from trading.strategy_runner import StrategyRunner
            runner = StrategyRunner()
        
        # 获取当前工作日期（用于信号）和当日日期（用于持仓）
        working_date = runner.get_working_date()
        today = dt.now().strftime('%Y-%m-%d')
        
        # 初始化当日数据（自动从最近有数据的交易日继承）
        runner.initialize_daily_data(working_date)
        
        # 【自动模式】在读取 portfolio 前，检查 PTrade 反馈是否更新
        # 若 PTrade 反馈文件比本地 portfolio 更新，自动同步
        runner.sync_portfolio_from_ptrade(today)
        
        # 加载持仓信息（使用当日日期，而非前一交易日）
        portfolio_file = runner.running_dir / f"portfolio_{today}.json"
        # 先读取文件数据，获取资金和持仓
        file_data = {}
        if portfolio_file.exists():
            try:
                with open(portfolio_file, 'r', encoding='utf-8') as f:
                    file_data = json.load(f)
            except Exception as e:
                logger.warning(f"读取持仓文件失败: {str(e)}")
        
        # 再调用 _load_portfolio 恢复策略运行器中的资金和持仓
        portfolio_result = runner._load_portfolio(str(portfolio_file))
        positions = portfolio_result.get('positions', {})
        
        # 更新内存中的持仓，确保执行信号时可以找到
        runner.portfolio = positions
        
        # 计算统计信息
        positions_count = len(positions) if positions else 0
        
        # 先从文件中获取资金，如果没有就用默认
        available_cash = file_data.get('cash', 300000)
        initial_capital = file_data.get('initial_capital', 300000)
        
        # 转换为列表格式（同时更新价格和计算总资产）
        positions_list = []
        total_value = 0  # 用新价格计算的持仓总市值
        
        if positions and isinstance(positions, dict):
            for stock_code, pos in positions.items():
                if isinstance(pos, dict):
                    # 获取成本价（优先buy_price，兼容cost_price）
                    cost_price = round(pos.get('buy_price', pos.get('cost_price', 0)), 2)
                    
                    # 获取最新价格（从数据库获取working_date的收盘价）
                    current_price = pos.get('current_price', 0)
                    try:
                        df_price = runner.db_manager.read_stock(stock_code)
                        if df_price is not None and not df_price.empty:
                            # 查找working_date对应的行
                            price_row = df_price[df_price['date'] == working_date]
                            if not price_row.empty:
                                current_price = float(price_row['close'].values[0])
                            else:
                                # 如果没有working_date的数据，取最后一行
                                current_price = float(df_price['close'].values[-1])
                    except Exception as e:
                        logger.debug(f"获取股票 {stock_code} 价格失败: {e}")
                    
                    quantity = pos.get('quantity', 0)
                    
                    # 计算盈亏
                    profit_loss = (current_price - cost_price) * quantity
                    profit_loss_percent = ((current_price - cost_price) / cost_price * 100 if cost_price > 0 else 0)
                    
                    # 计算止损止盈价格（止损5%，止盈15%）
                    stop_loss_price = cost_price * 0.95
                    take_profit_price = cost_price * 1.15
                    
                    # 累加持仓市值（用新价格）
                    total_value += quantity * current_price
                    
                    positions_list.append({
                        'id': pos.get('id', stock_code),
                        'stock_code': stock_code,
                        'stock_name': pos.get('stock_name', ''),
                        'quantity': quantity,
                        'cost_price': cost_price,
                        'current_price': current_price,
                        'stop_loss_price': stop_loss_price,
                        'take_profit_price': take_profit_price,
                        'profit_loss': pos.get('profit_loss', profit_loss),
                        'profit_loss_percent': pos.get('profit_loss_percent', profit_loss_percent),
                        'hold_days': pos.get('hold_days', pos.get('holding_days', 0))
                    })
        
        # 计算总资产和盈亏率（用新价格计算）
        total_assets = available_cash + total_value
        total_profit_percent = ((total_assets - initial_capital) / initial_capital) * 100
        
        # 获取当前运行模式（手动/自动），前端据此控制按钮显隐
        run_mode = getattr(runner, 'run_mode', 'manual')
        
        # 返回持仓信息和统计数据
        return jsonify({
            "success": True, 
            "data": {
                "positions": positions_list,
                "positions_count": positions_count,
                "available_cash": available_cash,
                "total_assets": total_assets,
                "total_profit_percent": total_profit_percent,
                "initial_cash": 300000,
                "date": working_date,
                "run_mode": run_mode
            }
        })
    except Exception as e:
        logger.error(f"获取持仓信息失败: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/portfolio/sell', methods=['POST'])
def sell_position():
    """
    卖出持仓中的股票（按现价执行）
    
    参数：
        stock_code: 股票代码
        
    返回：
        {"success": true, "data": {...}} 或 {"success": false, "error": "..."}
    """
    try:
        data = request.get_json()
        stock_code = data.get('stock_code')
        
        if not stock_code:
            return jsonify({"success": False, "error": "股票代码不能为空"})
        
        # 获取策略运行器
        runner = get_strategy_runner()
        if not runner:
            logger.info("策略运行器未初始化，进行初始化...")
            from trading.strategy_runner import StrategyRunner
            runner = StrategyRunner()
        
        # 自动模式下禁止手动卖出，以 PTrade 实际持仓为准
        run_mode = getattr(runner, 'run_mode', 'manual')
        if run_mode == 'auto':
            return jsonify({"success": False, "error": "自动模式下请通过 PTrade 操作，不可手动卖出"})
        
        # 获取当前工作日期
        working_date = runner.get_working_date()
        
        # 加载持仓
        portfolio_file = runner.running_dir / f"portfolio_{working_date}.json"
        if portfolio_file.exists():
            portfolio_result = runner._load_portfolio(str(portfolio_file))
            runner.portfolio = portfolio_result.get('positions', {})
        else:
            return jsonify({"success": False, "error": "持仓文件不存在"})
        
        # 检查是否持有该股票
        if stock_code not in runner.portfolio:
            return jsonify({"success": False, "error": f"未持有股票: {stock_code}"})
        
        position = runner.portfolio[stock_code]
        quantity = position.get('quantity', 0)
        
        if quantity <= 0:
            return jsonify({"success": False, "error": "持仓数量为0"})
        
        # 获取当前价格
        current_price = position.get('current_price', 0)
        try:
            df_price = runner.db_manager.read_stock(stock_code)
            if df_price is not None and not df_price.empty:
                price_row = df_price[df_price['date'] == working_date]
                if not price_row.empty:
                    current_price = float(price_row['close'].values[0])
                else:
                    current_price = float(df_price['close'].values[-1])
        except Exception as e:
            logger.debug(f"获取股票 {stock_code} 价格失败: {e}")
        
        if current_price <= 0:
            return jsonify({"success": False, "error": "无法获取当前价格"})
        
        # 创建卖出信号
        signal_id = f"manual_sell_{stock_code}_{working_date}"
        signal = {
            'id': signal_id,
            'date': working_date,
            'stock_code': stock_code,
            'stock_name': position.get('stock_name', ''),
            'signal_type': 'sell',
            'sell_type': 'manual',
            'quantity': quantity,
            'price': current_price,
            'amount': quantity * current_price,
            'profit_rate': position.get('profit_rate', 0),
            'reason': '手动卖出',
            'strategy_name': position.get('strategy_name', 'N/A'),
            'timing_strategy': 'manual',
            'executed': False,
            'executed_date': None
        }
        
        # 添加到信号列表
        runner.signals.append(signal)
        
        # 执行卖出
        result = runner.execute_signal(signal_id)
        
        if result.get('success'):
            # 保存持仓（_save_portfolio只需要传入持仓字典）
            runner._save_portfolio(runner.portfolio, str(portfolio_file))
            logger.info(f"手动卖出成功: {stock_code} x {quantity} @ ¥{current_price:.2f}")
        
        return jsonify(result)
        
    except Exception as e:
        logger.error(f"卖出持仓失败: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/task/history')
def get_task_history():
    """
    获取任务历史记录
    
    参数：
        limit: 返回记录数量（默认10条）
    
    返回：
        {"success": true, "data": {"history": [...]}}
    """
    try:
        # 延迟初始化策略运行器
        runner = get_strategy_runner()
        if not runner:
            return jsonify({"success": True, "data": {"history": []}})
        
        limit = request.args.get('limit', 10, type=int)
        history = runner.get_task_history(limit)
        
        return jsonify({"success": True, "data": {"history": history}})
    except Exception as e:
        logger.error(f"获取任务历史失败: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/task/last')
def get_last_task():
    """
    获取上次运行的任务配置
    
    返回：
        {"success": true, "data": {"strategies": [...], "initial_capital": 300000}}
    """
    try:
        # 延迟初始化策略运行器
        runner: StrategyRunner | None = get_strategy_runner()
        if not runner:
            # 没有策略运行器，返回空任务列表
            return jsonify({
                "success": True, 
                "data": {
                    "strategies": [],
                    "initial_capital": 300000,
                    "mode": 'realtime'
                }
            })
        
        last_task = runner.get_last_task()
        
        # 如果没有上次任务，返回空列表
        if not last_task:
            return jsonify({
                "success": True, 
                "data": {
                    "strategies": [],
                    "initial_capital": 300000,
                    "mode": 'realtime'
                }
            })
        
        return jsonify({"success": True, "data": last_task})
    except Exception as e:
        logger.error(f"获取上次任务失败: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/task/save', methods=['POST'])
def save_task():
    """
    保存任务运行记录
    
    参数：
        strategies: 策略列表
        initial_capital: 初始资金
        mode: 运行模式
    
    返回：
        {"success": true, "message": "任务记录已保存"}
    """
    try:
        # 延迟初始化策略运行器
        runner = get_strategy_runner()
        if not runner:
            return jsonify({"success": False, "message": "策略运行器未初始化"})
        
        data = request.json or {}
        
        result = runner.save_task_record(data)
        
        if result.get('success'):
            return jsonify({"success": True, "message": "任务记录已保存"})
        else:
            return jsonify({"success": False, "error": result.get('error', '保存失败')})
    except Exception as e:
        logger.error(f"保存任务记录失败: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/signals')
def get_signals():
    """
    获取信号列表
    
    返回：
        {"success": true, "data": {"signals": [...]}}
    """
    try:
        # 初始化策略运行器
        runner = get_strategy_runner(auto_init=True)
        if not runner:
            return jsonify({"success": True, "data": {"signals": []}})
        
        # 获取当前工作日期
        working_date = runner.get_working_date()
        
        # 初始化当日数据（自动从最近有数据的交易日继承）
        runner.initialize_daily_data(working_date)
        
        # 加载信号历史
        signals_file = runner.running_dir / f"signals_{working_date}.json"
        signals = runner._load_signals(str(signals_file))
        
        # 更新内存中的信号，确保执行时可以找到
        runner.signals = signals
        
        # 转换策略名称为中文
        for signal in signals:
            if 'strategy_name' in signal:
                signal['strategy_name'] = get_chinese_name(signal['strategy_name'])
        
        # 获取当前运行模式，前端据此控制操作按钮显隐
        run_mode = getattr(runner, 'run_mode', 'manual')
        
        return jsonify({"success": True, "data": {"signals": signals, "date": working_date, "run_mode": run_mode}})
    except Exception as e:
        logger.error(f"获取信号列表失败: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/stock-pool')
def get_stock_pool():
    """
    获取股票池数据
    
    返回：
        {"success": true, "data": {"pool": [...]}}
    """
    try:
        # 初始化策略运行器
        runner = get_strategy_runner(auto_init=True)
        if not runner:
            return jsonify({"success": True, "data": {"pool": []}})
        
        from utils.trade_date_utils import get_trading_days
        
        # 获取当前工作日期
        working_date = runner.get_working_date()
        
        # 初始化当日数据（自动从最近有数据的交易日继承）
        runner.initialize_daily_data(working_date)
        
        # 加载股票池数据
        pool_file = runner.running_dir / f"buy_candidate_pool.json"
        if pool_file.exists():
            with open(pool_file, 'r', encoding='utf-8') as f:
                pool_data = json.load(f)
                pool = pool_data.get('pool', [])
        else:
            pool = []
        
        # 【优化】只调用一次获取交易日列表，避免对每只股票重复查询
        # 获取历史交易日（过去60天足够了）
        from datetime import datetime, timedelta, timedelta
        hist_start = (dt.strptime(working_date, '%Y-%m-%d') - timedelta(days=60)).strftime('%Y-%m-%d')
        all_trading_days = get_trading_days(hist_start, working_date)
        trading_days_set = set(all_trading_days)  # 用集合加速查找
        
        # 转换格式
        pool_list = []
        for item in pool:
            stock = item.get('stock', {})
            added_date = item.get('added_date', working_date)
            stock_code = stock.get('stock_code', '')
            # 【优化】用集合快速计算入池天数（交易日数量）
            if added_date in trading_days_set:
                days_in_pool = len([d for d in all_trading_days if d >= added_date])
            else:
                days_in_pool = 0
            
            # 获取最新价格（从数据库获取working_date的收盘价）
            current_price = stock.get('signal', {}).get('close', 0)
            try:
                df_price = runner.db_manager.read_stock(stock_code)
                if df_price is not None and not df_price.empty:
                    # 查找working_date对应的行
                    price_row = df_price[df_price['date'] == working_date]
                    if not price_row.empty:
                        current_price = float(price_row['close'].values[0])
                    else:
                        # 如果没有working_date的数据，取最后一行
                        current_price = float(df_price['close'].values[-1])
            except Exception as e:
                logger.debug(f"获取股票 {stock_code} 价格失败: {e}")
            
            # 检查冷却状态
            is_cooling = item.get('is_cooling', False)
            cool_down_end = item.get('cool_down_end', None)
            
            # 确定状态显示
            status = 'candidate'
            status_text = '候选'
            if is_cooling and cool_down_end:
                status = 'cooling'
                status_text = f'冷却中(至{cool_down_end})'
            
            pool_list.append({
                'stock_code': stock_code,
                'stock_name': stock.get('stock_name', ''),
                'score': stock.get('score', 0),
                'status': status,
                'status_text': status_text,
                'is_cooling': is_cooling,
                'cool_down_end': cool_down_end,
                'days_in_pool': days_in_pool,
                'current_price': current_price,
                'support_level': item.get('support_level', 0),
                'strategy_name': get_chinese_name(item.get('strategy_name', ''))
            })
        
        return jsonify({"success": True, "data": {"pool": pool_list, "date": working_date}})
    except Exception as e:
        logger.error(f"获取股票池失败: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/signals/<signal_id>/execute', methods=['POST'])
def execute_signal(signal_id):
    """
    执行指定的信号
    
    参数：
        signal_id: 信号ID
    
    返回：
        {"success": true, "message": "信号执行成功"}
    """
    import traceback
    try:
        # 添加详细日志追踪请求
        logger.info(f"【路由层】接收到执行信号请求: {signal_id}")
        
        # 手动触发时才初始化策略运行器
        runner = get_strategy_runner(auto_init=True)
        if not runner:
            logger.error(f"【路由层】策略运行器未初始化")
            return jsonify({"success": False, "message": "策略运行器未初始化"})
        
        # 自动模式下禁止手动执行信号，信号由 PTrade 自动读取
        run_mode = getattr(runner, 'run_mode', 'manual')
        if run_mode == 'auto':
            return jsonify({"success": False, "message": "自动模式下由 PTrade 自动执行，不可手动操作"})

        logger.info(f"【路由层】开始执行信号: {signal_id}")

        # 调用策略运行器执行信号
        result = runner.execute_signal(signal_id)
        
        if result.get('success'):
            logger.info(f"【路由层】信号执行成功: {signal_id}")
            return jsonify({"success": True, "message": "信号执行成功"})
        else:
            logger.warning(f"【路由层】信号执行失败: {signal_id}, 错误: {result.get('error')}")
            return jsonify({"success": False, "message": result.get('error', '执行失败')})
    except Exception as e:
        logger.error(f"【路由层】执行信号异常: {signal_id}, 错误: {str(e)}")
        logger.error(f"【路由层】异常堆栈: {traceback.format_exc()}")
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/signals/<signal_id>/ignore', methods=['POST'])
def ignore_signal(signal_id):
    """
    忽略指定的信号
    
    参数：
        signal_id: 信号ID
    
    返回：
        {"success": true, "message": "信号已忽略"}
    """
    try:
        # 手动触发时才初始化策略运行器
        runner = get_strategy_runner(auto_init=True)
        if not runner:
            return jsonify({"success": False, "message": "策略运行器未初始化"})
        
        # 自动模式下禁止手动忽略信号
        run_mode = getattr(runner, 'run_mode', 'manual')
        if run_mode == 'auto':
            return jsonify({"success": False, "message": "自动模式下由 PTrade 自动处理，不可手动操作"})

        logger.info(f"忽略信号: {signal_id}")

        # 调用策略运行器忽略信号
        result = runner.ignore_signal(signal_id)
        
        if result.get('success'):
            return jsonify({"success": True, "message": "信号已忽略"})
        else:
            return jsonify({"success": False, "message": result.get('error', '忽略失败')})
    except Exception as e:
        logger.error(f"忽略信号失败: {str(e)}")
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/signals/execute_pending', methods=['POST'])
def execute_pending_signals():
    """
    执行所有待处理的信号（T+1日盘中调用）
    
    参数：
        trade_date: 交易日期（可选，默认使用当前工作日期）
    
    返回：
        {"success": true, "message": "信号执行完成", "data": {...}}
    """
    try:
        runner = get_strategy_runner(auto_init=True)
        if not runner:
            return jsonify({"success": False, "message": "策略运行器未初始化"})
        
        data = request.json or {}
        trade_date = data.get('trade_date')
        
        logger.info(f"执行所有待处理信号: {trade_date or '当前工作日期'}")
        
        result = runner.execute_pending_signals(trade_date)
        
        if result.get('success'):
            return jsonify({
                "success": True,
                "message": result.get('message', '信号执行完成'),
                "data": result.get('data', {})
            })
        else:
            return jsonify({"success": False, "message": result.get('error', '执行失败')})
    except Exception as e:
        logger.error(f"执行待处理信号失败: {str(e)}")
        return jsonify({"success": False, "message": str(e)})


@app.route('/api/trades/execute', methods=['POST'])
def execute_trade():
    """
    执行信号
    
    参数：
        signal_id: 信号ID
    
    返回：
        {"success": true, "message": "信号执行成功"}
    """
    try:
        if not strategy_runner:
            return jsonify({"success": False, "message": "策略运行器未初始化"})
        
        # 获取请求参数
        data = request.json or {}
        signal_id = data.get('signal_id')
        
        if not signal_id:
            return jsonify({"success": False, "error": "信号ID不能为空"})
        
        # 这里简化处理，实际应该根据信号ID找到对应的信号并执行
        # 暂时返回成功
        logger.info(f"执行信号: {signal_id}")
        
        return jsonify({"success": True, "message": "信号执行成功"})
    except Exception as e:
        logger.error(f"执行信号失败: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/trades/ignore', methods=['POST'])
def ignore_trade():
    """
    忽略信号
    
    参数：
        signal_id: 信号ID
    
    返回：
        {"success": true, "message": "信号已忽略"}
    """
    try:
        if not strategy_runner:
            return jsonify({"success": False, "message": "策略运行器未初始化"})
        
        # 获取请求参数
        data = request.json or {}
        signal_id = data.get('signal_id')
        
        if not signal_id:
            return jsonify({"success": False, "error": "信号ID不能为空"})
        
        # 这里简化处理，实际应该根据信号ID找到对应的信号并标记为忽略
        # 暂时返回成功
        logger.info(f"忽略信号: {signal_id}")
        
        return jsonify({"success": True, "message": "信号已忽略"})
    except Exception as e:
        logger.error(f"忽略信号失败: {str(e)}")
        return jsonify({"success": False, "error": str(e)})


def configure_quantpilot_engine(engine):
    """Use the engine supplied by a desktop process that bootstraps this server."""
    global quantpilot_screen_engine
    with _quantpilot_engine_lock:
        quantpilot_screen_engine = engine


# 桌面镜像定时同步:交易日历按日缓存(底层 is_trading_day 可能耗网络,分钟级循环
# 内不重复解析)。
_TRADING_DAY_CACHE = {}


def _cached_is_trading_day(date_str: str) -> bool:
    if date_str not in _TRADING_DAY_CACHE:
        try:
            _TRADING_DAY_CACHE[date_str] = bool(is_trading_day(date_str))
        except Exception:
            try:
                _TRADING_DAY_CACHE[date_str] = datetime.strptime(
                    date_str, '%Y-%m-%d').weekday() < 5
            except ValueError:
                _TRADING_DAY_CACHE[date_str] = False
    return _TRADING_DAY_CACHE[date_str]


def _desktop_mirror_sync_once():
    """定时循环注入的同步动作:拉服务器账本,成功后顺带触发持仓K线保鲜。"""
    engine = _get_quantpilot_screen_engine()
    result = engine.sync_server_paper_state() or {}
    if result.get('status') == 'synced':
        _ensure_holdings_klines_async()
    return result


def _start_desktop_mirror_sync_loop():
    """桌面专属:应用常开时按交易日节奏自动拉服务器最新账本,无需手动重启。

    仅当本进程处于"服务器账本镜像"形态才启动(该标记只在桌面成功同步过服务器
    快照后存在;服务器部署的 Web 进程永不满足,不会对自己发起同步)。启动时
    同步暂时失败也一样启动循环——否则降级只读的桌面将永远停在过期账本上,
    没有自动恢复路径;身份私钥只存在于桌面,仍是服务器进程的天然隔离条件。
    """
    try:
        engine = _get_quantpilot_screen_engine()
        if not getattr(engine, '_server_mirror_mode', False):
            settings = (
                engine.khunter_config.get('paper_trading', {}).get('server_sync', {})
                if isinstance(engine.khunter_config, dict) else {}
            ) or {}
            identity = Path(str(settings.get('identity_file') or '')).expanduser()
            if not (settings.get('enabled') and identity.is_file()):
                return
        from paper_trading.mirror_sync import start_mirror_sync_loop
        start_mirror_sync_loop(_desktop_mirror_sync_once, _cached_is_trading_day)
    except Exception:
        logger.exception('镜像定时同步线程启动失败(启动时的一次性同步不受影响)')


def run_web_server(host='0.0.0.0', port=5000, debug=False):
    """启动Web服务器"""
    # 初始化日志系统
    from utils.log_config import LogConfig
    from utils.paths import get_logs_dir
    LogConfig.setup_logging(log_dir=get_logs_dir())

    if web_paper_scheduler_enabled():
        start_quantpilot_paper_trading()
    else:
        logger.info("Web 模拟盘调度器已禁用；由独立盘中服务执行模拟交易与通知")

    _start_desktop_mirror_sync_loop()

    # 打印所有注册的路由
    print("\n注册的路由:")
    for rule in app.url_map.iter_rules():
        print(f"  {rule}")
    
    print(f"\n启动Web服务器: http://{host}:{port}")
    # 启动 socketio 服务器
    socketio.run(
        app, 
        host=host, 
        port=port, 
        debug=debug,
        allow_unsafe_werkzeug=True
    )


# ==================== 风控模块API ====================

@app.route('/api/risk/status')
def get_risk_status():
    """
    获取当日风控状态
    
    查询参数：
        date: 日期（可选），格式YYYY-MM-DD，默认为当日
        force_refresh: 是否强制刷新（可选），true/false
    
    返回：
        {
            "success": true,
            "data": {
                "date": "2026-05-14",
                "var_1d": -0.04,
                "var_5d": -0.09,
                "es_1d": -0.05,
                "risk_level": "注意",
                "position_limit": 0.7,
                "stop_loss_multiplier": 1.5,
                "score_extra": 5,
                "strategy_enabled": true,
                "liquidate": false
            }
        }
    """
    try:
        from utils.risk_controller import get_risk_controller
        
        # 获取查询参数
        date = request.args.get('date')
        force_refresh = request.args.get('force_refresh', 'false').lower() == 'true'
        
        # 获取风控控制器
        controller = get_risk_controller()
        
        # 获取风控状态
        risk_status = controller.get_risk_status(date, force_refresh)
        
        if risk_status is None:
            return jsonify({
                'success': False,
                'message': '获取风控状态失败'
            }), 500
        
        return jsonify({
            'success': True,
            'data': clean_data_for_json(risk_status.to_dict())
        })
        
    except Exception as e:
        logger.error(f"获取风控状态失败: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'message': f'获取风控状态失败: {str(e)}'
        }), 500


@app.route('/api/risk/history')
def get_risk_history():
    """
    获取历史风控状态
    
    查询参数：
        days: 天数（可选），默认30天
    
    返回：
        {
            "success": true,
            "data": [
                {
                    "date": "2026-05-14",
                    "var_1d": -0.04,
                    "risk_level": "注意",
                    ...
                },
                ...
            ]
        }
    """
    try:
        from utils.risk_controller import get_risk_controller
        
        # 获取查询参数
        days = request.args.get('days', 30, type=int)
        
        # 获取风控控制器
        controller = get_risk_controller()
        
        # 获取历史风控状态
        history = controller.get_risk_history(days)
        
        # 转换为字典列表
        history_data = [status.to_dict() for status in history]
        
        return jsonify({
            'success': True,
            'data': clean_data_for_json(history_data)
        })
        
    except Exception as e:
        logger.error(f"获取风控历史失败: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'message': f'获取风控历史失败: {str(e)}'
        }), 500


@app.route('/api/risk/config', methods=['GET'])
def get_risk_config():
    """
    获取风控配置
    
    返回：
        {
            "success": true,
            "data": {
                "risk": {...},
                "evt": {...},
                "cache": {...}
            }
        }
    """
    try:
        from utils.risk_controller import get_risk_controller
        
        # 获取风控控制器
        controller = get_risk_controller()
        
        # 获取配置
        config = controller.get_risk_config()
        
        return jsonify({
            'success': True,
            'data': clean_data_for_json(config)
        })
        
    except Exception as e:
        logger.error(f"获取风控配置失败: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'message': f'获取风控配置失败: {str(e)}'
        }), 500


@app.route('/api/risk/config', methods=['POST'])
def update_risk_config():
    """
    更新风控配置
    
    请求体：
        {
            "risk": {...},
            "evt": {...},
            "cache": {...}
        }
    
    返回：
        {
            "success": true,
            "message": "配置更新成功"
        }
    """
    try:
        from utils.risk_controller import get_risk_controller
        
        # 获取请求体
        new_config = request.get_json()
        
        if not new_config:
            return jsonify({
                'success': False,
                'message': '请求体为空'
            }), 400
        
        # 获取风控控制器
        controller = get_risk_controller()
        
        # 更新配置
        success = controller.update_risk_config(new_config)
        
        if success:
            return jsonify({
                'success': True,
                'message': '配置更新成功'
            })
        else:
            return jsonify({
                'success': False,
                'message': '配置更新失败'
            }), 400
        
    except Exception as e:
        logger.error(f"更新风控配置失败: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'message': f'更新风控配置失败: {str(e)}'
        }), 500


if __name__ == '__main__':
    run_web_server(debug=False, port=5001)
