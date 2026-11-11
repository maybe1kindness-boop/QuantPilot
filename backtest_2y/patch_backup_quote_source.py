# -*- coding: utf-8 -*-
"""行情备胎补丁(2026-09-05 用户批准"先把行情备胎换了")。

当前生产实况: EM spot/分钟接口被定向封锁(快失败), 全市场报价已走腾讯降级、
5分钟K走 TDX。本补丁补掉三个单点:
  A. EM 若从"快失败"恶化为"静默丢包挂死"(Mac IP 已是这种), akshare 调用
     无超时会挂死线程 → 加 8s 硬超时包装(单worker复用池, 卡死后后续调用
     同样快速超时直走备用腿, 进程按日重启自愈)。
  B. 腾讯是当前唯一报价腿 → 加新浪 hq.sinajs.cn 第三腿(需 Referer)。
  C. TDX 只配一台主机 → 三台轮换(180.153.18.170/218.75.126.9/115.238.90.165,
     2026-09-05 服务器实测可用)。

幂等, anchor 断言, 备份 *.before_backup_src_20260905。
"""
import shutil
import sys

STAMP = "before_backup_src_20260905"

# ============ trading/intraday_data.py ============
P1 = "/opt/quantpilot/trading/intraday_data.py"
src = open(P1, encoding="utf-8").read()
if "call_with_timeout" not in src:
    shutil.copy2(P1, P1 + "." + STAMP)

    # A1: import 行加 TimeoutError
    OLD = "from concurrent.futures import ThreadPoolExecutor, as_completed\n"
    assert src.count(OLD) == 1, f"A1: {src.count(OLD)}"
    src = src.replace(OLD, (
        "from concurrent.futures import (\n"
        "    ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout,\n"
        ")\n"), 1)

    # A2: _tdx_host 前插超时包装 + TDX 主机列表
    OLD = "def _tdx_host() -> tuple[str, int]:"
    assert src.count(OLD) == 1, f"A2: {src.count(OLD)}"
    src = src.replace(OLD, (
        "_EM_TIMEOUT_POOL = ThreadPoolExecutor(max_workers=1)\n"
        "\n"
        "\n"
        "def call_with_timeout(func, timeout_seconds: float = 8.0, **kwargs):\n"
        "    '''行情源调用的硬超时包装。\n"
        "\n"
        "    东财被定向封锁时可能从快失败恶化为\"静默丢包\"挂死(其内部 requests\n"
        "    无超时)。单 worker 复用池强制 8s 上限: 一旦 worker 卡死, 后续调用\n"
        "    同样快速超时并直接走 TDX/腾讯备用腿; 进程按交易日重启, 卡死连接\n"
        "    随进程销毁自愈。\n"
        "    '''\n"
        "    future = _EM_TIMEOUT_POOL.submit(func, **kwargs)\n"
        "    try:\n"
        "        return future.result(timeout=timeout_seconds)\n"
        "    except FuturesTimeout as exc:\n"
        "        raise IntradayDataUnavailable(\n"
        "            f'{getattr(func, \"__name__\", func)} '\n"
        "            f'超时({timeout_seconds}s), 行情源无响应'\n"
        "        ) from exc\n"
        "\n"
        "\n"
        "# 2026-09-05 服务器实测可用的 TDX 行情主机(按序轮换)\n"
        "_TDX_FALLBACK_HOSTS = [\n"
        "    ('218.75.126.9', 7709),\n"
        "    ('115.238.90.165', 7709),\n"
        "]\n"
        "\n"
        "\n"
        "def _tdx_hosts() -> list:\n"
        "    '''环境变量主机优先, 其后接内置备用主机(去重)。'''\n"
        "    hosts = [_tdx_host()]\n"
        "    hosts.extend(_TDX_FALLBACK_HOSTS)\n"
        "    seen, ordered = set(), []\n"
        "    for item in hosts:\n"
        "        if item not in seen:\n"
        "            seen.add(item)\n"
        "            ordered.append(item)\n"
        "    return ordered\n"
        "\n"
        "\n"
        + OLD), 1)

    # A3: _fetch_tdx 单主机连接 → 主机轮换
    OLD = (
        "        host, port = _tdx_host()\n"
        "        api = TdxHq_API()\n"
        "        if not api.connect(host, port, time_out=5):\n"
        "            raise IntradayDataUnavailable(f'TDX行情服务器连接失败: {host}:{port}')\n"
    )
    assert src.count(OLD) == 1, f"A3: {src.count(OLD)}"
    src = src.replace(OLD, (
        "        hosts = _tdx_hosts()\n"
        "        api = TdxHq_API()\n"
        "        connected = False\n"
        "        for host, port in hosts:\n"
        "            if api.connect(host, port, time_out=5):\n"
        "                connected = True\n"
        "                break\n"
        "        if not connected:\n"
        "            raise IntradayDataUnavailable(\n"
        "                'TDX行情服务器全部连接失败: '\n"
        "                + ', '.join(f'{h}:{p}' for h, p in hosts)\n"
        "            )\n"
    ), 1)

    # A4: fetch_realtime_and_store 的 akshare 调用包硬超时
    OLD = (
        "            normalized = requested_slice(ak.stock_zh_a_hist_min_em(\n"
        "                symbol=normalized_code,\n"
        "                period=str(interval),\n"
        '                adjust="",\n'
        "            ))\n"
    )
    assert src.count(OLD) == 1, f"A4: {src.count(OLD)}"
    src = src.replace(OLD, (
        "            normalized = requested_slice(call_with_timeout(\n"
        "                ak.stock_zh_a_hist_min_em,\n"
        "                symbol=normalized_code,\n"
        "                period=str(interval),\n"
        '                adjust="",\n'
        "            ))\n"
    ), 1)

    open(P1, "w", encoding="utf-8").write(src)
    print("PATCH1_OK")
else:
    print("PATCH1_SKIP")

# ============ paper_trading/market_data_poller.py ============
P2 = "/opt/quantpilot/paper_trading/market_data_poller.py"
src = open(P2, encoding="utf-8").read()
if "_fetch_sina_quotes" not in src:
    shutil.copy2(P2, P2 + "." + STAMP)

    # B1: import 行加 TimeoutError
    OLD = "from concurrent.futures import ThreadPoolExecutor, as_completed\n"
    assert src.count(OLD) == 1, f"B1: {src.count(OLD)}"
    src = src.replace(OLD, (
        "from concurrent.futures import (\n"
        "    ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout,\n"
        ")\n"), 1)

    # B2: 模块级 spot 硬超时包装
    OLD = 'logger = logging.getLogger(__name__)\n'
    assert src.count(OLD) == 1, f"B2: {src.count(OLD)}"
    src = src.replace(OLD, OLD + (
        "\n"
        "_SPOT_TIMEOUT_POOL = ThreadPoolExecutor(max_workers=1)\n"
        "\n"
        "\n"
        "def _spot_call_with_timeout(func, timeout_seconds: float = 10.0, **kwargs):\n"
        '    """spot_em 硬超时: 防东财"静默丢包"式封锁把刷新线程挂死。"""\n'
        "    future = _SPOT_TIMEOUT_POOL.submit(func, **kwargs)\n"
        "    try:\n"
        "        return future.result(timeout=timeout_seconds)\n"
        "    except FuturesTimeout as exc:\n"
        "        raise TimeoutError(\n"
        "            f'{getattr(func, \"__name__\", func)} '\n"
        "            f'超时({timeout_seconds}s), 行情源无响应'\n"
        "        ) from exc\n"
    ), 1)

    # B3: spot_em 调用包超时
    OLD = "            df = ak.stock_zh_a_spot_em()\n"
    assert src.count(OLD) == 1, f"B3: {src.count(OLD)}"
    src = src.replace(
        OLD, "            df = _spot_call_with_timeout(ak.stock_zh_a_spot_em)\n", 1)

    # B4: _fetch_batch_fallback 腾讯不足半数 → 新浪腿
    OLD = (
        "        if not codes:\n"
        "            return {}\n"
        "        return self._fetch_tencent_quotes(codes)\n"
    )
    assert src.count(OLD) == 1, f"B4: {src.count(OLD)}"
    src = src.replace(OLD, (
        "        if not codes:\n"
        "            return {}\n"
        "        quotes = self._fetch_tencent_quotes(codes)\n"
        "        if len(quotes) < len(codes) // 2:\n"
        "            sina = self._fetch_sina_quotes(codes)\n"
        "            if len(sina) > len(quotes):\n"
        '                logger.warning("腾讯批量行情不足(%s/%s), 切换新浪腿",\n'
        "                               len(quotes), len(codes))\n"
        "                quotes = sina\n"
        "        return quotes\n"
    ), 1)

    # B5: fetch_targeted_quotes 腾讯空 → 新浪腿
    OLD = (
        "        quotes = self._fetch_tencent_quotes(normalized_codes)\n"
        "        if quotes:\n"
    )
    assert src.count(OLD) == 1, f"B5: {src.count(OLD)}"
    src = src.replace(OLD, (
        "        quotes = self._fetch_tencent_quotes(normalized_codes)\n"
        "        if not quotes:\n"
        "            quotes = self._fetch_sina_quotes(normalized_codes)\n"
        "        if quotes:\n"
    ), 1)

    # B6: 新浪第三腿方法, 插在"切片"分节前
    OLD = "    # ---------------- 切片 ----------------\n"
    assert src.count(OLD) == 1, f"B6: {src.count(OLD)}"
    src = src.replace(OLD, (
        '    def _fetch_sina_quotes(self, codes: List[str]) -> Dict[str, Quote]:\n'
        '        """新浪批量报价(第三腿): 腾讯也不可用时的兜底。\n'
        "\n"
        "        字段序: 0名称 1今开 2昨收 3现价 4最高 5最低 ... 8成交量(股)\n"
        "        9成交额(元)。新浪不提供市值/换手, 置 0(市值筛选按放行处理,\n"
        "        仅兜底可接受)。需 Referer 头, 否则 403。\n"
        '        """\n'
        '        batch_size = 60\n'
        '        headers = {\n'
        '            "Referer": "https://finance.sina.com.cn",\n'
        '            "User-Agent": "Mozilla/5.0",\n'
        "        }\n"
        "\n"
        "        def number(parts, index):\n"
        "            try:\n"
        '                value = parts[index] if index < len(parts) else ""\n'
        '                return float(value.replace(",", "")) if value else 0.0\n'
        "            except (AttributeError, TypeError, ValueError):\n"
        "                return 0.0\n"
        "\n"
        "        def fetch_batch(batch: List[str]) -> Dict[str, Quote]:\n"
        "            quotes: Dict[str, Quote] = {}\n"
        '            batch = [str(code).zfill(6) for code in batch]\n'
        '            query = ",".join(\n'
        '                ("sh" if code.startswith(("6", "8", "9")) else "sz") + code\n'
        "                for code in batch\n"
        "            )\n"
        "            try:\n"
        "                response = requests.get(\n"
        '                    f"https://hq.sinajs.cn/list={query}",\n'
        "                    timeout=8,\n"
        "                    headers=headers,\n"
        "                )\n"
        '                response.encoding = "gbk"\n'
        "                if response.status_code != 200:\n"
        "                    return quotes\n"
        "                for line in response.text.splitlines():\n"
        '                    if \'="\' not in line:\n'
        "                        continue\n"
        '                    key, raw = line.split(\'="\', 1)\n'
        '                    code = key.strip().rsplit("_", 1)[-1][-6:]\n'
        '                    parts = raw.strip().strip(\'";\').split(",")\n'
        "                    if len(parts) < 32 or not parts[0]:\n"
        "                        continue\n"
        "                    price = number(parts, 3)\n"
        "                    if price <= 0:\n"
        "                        continue\n"
        "                    prev_close = number(parts, 2)\n"
        "                    quotes[code] = Quote(\n"
        "                        code=code, name=parts[0], price=price,\n"
        "                        open=number(parts, 1), high=number(parts, 4),\n"
        "                        low=number(parts, 5), prev_close=prev_close,\n"
        "                        volume=number(parts, 8),\n"
        "                        pct=(price / prev_close - 1) * 100 if prev_close > 0 else 0.0,\n"
        "                        amount=number(parts, 9),\n"
        '                        is_st=parts[0].startswith(("ST", "*ST")),\n'
        "                    )\n"
        "            except Exception as exc:\n"
        '                logger.debug("新浪行情兜底批次失败: %s", exc)\n'
        "            return quotes\n"
        "\n"
        "        batches = [\n"
        "            codes[start:start + batch_size]\n"
        "            for start in range(0, len(codes), batch_size)\n"
        "        ]\n"
        "        quotes: Dict[str, Quote] = {}\n"
        "        worker_count = min(8, max(1, len(batches)))\n"
        "        with ThreadPoolExecutor(max_workers=worker_count) as pool:\n"
        "            futures = [pool.submit(fetch_batch, batch) for batch in batches]\n"
        "            for future in as_completed(futures):\n"
        "                quotes.update(future.result())\n"
        "        if quotes:\n"
        '            logger.info("新浪行情兜底完成: 成功 %s/%s 只", len(quotes), len(codes))\n'
        "        return quotes\n"
        "\n"
        + OLD), 1)

    open(P2, "w", encoding="utf-8").write(src)
    print("PATCH2_OK")
else:
    print("PATCH2_SKIP")

# 终检
s1 = open(P1, encoding="utf-8").read()
s2 = open(P2, encoding="utf-8").read()
assert "call_with_timeout(" in s1 and "_tdx_hosts()" in s1
assert "_fetch_sina_quotes" in s2 and "_spot_call_with_timeout(" in s2
print("ALL_OK")
