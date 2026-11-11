"""股票代码永久排除名单。

这些代码能出现在报价扫描结果中，但历史行情接口没有可用 K 线。
"""

from __future__ import annotations

from typing import Iterable, Mapping, TypeVar


MANUALLY_EXCLUDED_STOCK_CODES = frozenset({
    '000991', '001235', '002257', '002525', '002720', '300060',
    '300361', '300728', '600349', '603302', '603361', '688688',
})

# Delisted, merged, or otherwise permanently inactive securities found in the
# server market-data universe on 2026-08-20. Keeping this list separate makes
# the one-off historical cleanup auditable and prevents reinsertion by a later
# stock-list refresh.
RETIRED_STOCK_CODES = frozenset({
    '000003', '000005', '000013', '000015', '000023', '000024', '000040',
    '000046', '000047', '000150', '000405', '000406', '000412', '000413',
    '000416', '000418', '000508', '000515', '000522', '000527', '000535',
    '000540', '000542', '000549', '000556', '000562', '000569', '000578',
    '000583', '000587', '000588', '000602', '000616', '000618', '000621',
    '000627', '000638', '000653', '000658', '000660', '000666', '000667',
    '000671', '000675', '000689', '000699', '000730', '000732', '000748',
    '000763', '000765', '000769', '000780', '000787', '000805', '000817',
    '000827', '000832', '000836', '000851', '000861', '000866', '000916',
    '000918', '000956', '000961', '000971', '000976', '000982', '002002',
    '002013', '002113', '002118', '002231', '002280', '002288', '002308',
    '002325', '002341', '002435', '002502', '002503', '002504', '002505',
    '002610', '002619', '002621', '002665', '002699', '002740', '002776',
    '300108', '300116', '300117', '300186', '300262', '300495', '600001',
    '600002', '600003', '600005', '600065', '600068', '600070', '600077',
    '600083', '600092', '600102', '600112', '600122', '600139', '600181',
    '600205', '600213', '600220', '600247', '600253', '600260', '600263',
    '600270', '600277', '600286', '600290', '600296', '600297', '600311',
    '600317', '600321', '600355', '600357', '600393', '600466', '600472',
    '600485', '600553', '600565', '600591', '600607', '600625', '600627',
    '600631', '600632', '600646', '600659', '600669', '600670', '600672',
    '600677', '600680', '600700', '600705', '600709', '600723', '600752',
    '600762', '600772', '600786', '600788', '600799', '600811', '600813',
    '600823', '600832', '600836', '600837', '600840', '600842', '600852',
    '600878', '600898', '600899', '600978', '600991', '601028', '601258',
    '601268', '601299', '601989', '603056', '603388', '603555', '603963',
})

EXCLUDED_STOCK_CODES = MANUALLY_EXCLUDED_STOCK_CODES | RETIRED_STOCK_CODES

_T = TypeVar('_T')


def is_excluded_stock_code(code: object) -> bool:
    return str(code or '').strip().zfill(6) in EXCLUDED_STOCK_CODES


def filter_stock_codes(codes: Iterable[object]) -> list[str]:
    """Normalize and remove permanently excluded stock codes."""
    return [
        code
        for code in (str(item).strip().zfill(6) for item in codes)
        if code and code not in EXCLUDED_STOCK_CODES
    ]


def filter_stock_mapping(stocks: Mapping[object, _T]) -> dict[str, _T]:
    """Normalize a code-to-value mapping while removing excluded codes."""
    return {
        str(code).strip().zfill(6): value
        for code, value in stocks.items()
        if not is_excluded_stock_code(code)
    }


def purge_excluded_stock_data(db_manager) -> int:
    """Remove excluded codes from current daily, minute, and basic tables."""
    placeholders = ','.join('?' for _ in EXCLUDED_STOCK_CODES)
    deleted = 0
    for table in ('stock_kline', 'stock_intraday_kline', 'stock_basic'):
        try:
            before = db_manager.query_one(
                f'SELECT COUNT(*) AS count FROM {table} WHERE code IN ({placeholders})',
                tuple(EXCLUDED_STOCK_CODES),
            )
            db_manager.execute(
                f'DELETE FROM {table} WHERE code IN ({placeholders})',
                tuple(EXCLUDED_STOCK_CODES),
            )
            deleted += int((before or {}).get('count') or 0)
        except Exception as exc:
            # The minute table is created lazily; missing optional tables are fine.
            if 'no such table' not in str(exc).lower():
                raise
    return deleted
