(function () {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const api = async (url, options = {}) => {
    const response = await fetch(url, {
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-cache' },
      ...options
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.success === false) throw new Error(payload.error || payload.message || `请求失败 (${response.status})`);
    return payload.data ?? payload;
  };
  const toast = message => {
    const node = $('#toast');
    if (!node) return;
    node.textContent = message;
    node.classList.add('show');
    clearTimeout(window.quantPilotToastTimer);
    window.quantPilotToastTimer = setTimeout(() => node.classList.remove('show'), 2400);
  };
  const refreshIcons = () => window.lucide && lucide.createIcons();
  let klineProgressTimer = null;
  let klineStartupState = null;

  const renderThemeChips = themes => {
    if (!Array.isArray(themes) || !themes.length) {
      return '<span class="theme-empty">暂无关联题材</span>';
    }
    return themes.map(theme => {
      const name = theme?.name || '';
      const hasChange = theme?.change !== null && theme?.change !== undefined && theme?.change !== '';
      const change = Number(theme?.change);
      const changeText = hasChange && Number.isFinite(change) ? `${change >= 0 ? '+' : ''}${change.toFixed(2)}%` : '';
      const title = changeText ? `${name} · 当日 ${changeText}` : name;
      return `<span class="theme-chip" title="${escapeHtml(title)}">${escapeHtml(name)}</span>`;
    }).join('');
  };

  async function hydrateStockThemes(root) {
    if (!root) return;
    const mounts = $$('[data-theme-code]', root);
    const codes = [...new Set(mounts.map(node => node.dataset.themeCode).filter(Boolean))].slice(0, 100);
    if (!codes.length) return;
    try {
      const mapping = await api('/api/quantpilot/stock-themes', {
        method: 'POST',
        body: JSON.stringify({ codes, max_themes: 5 })
      });
      mounts.forEach(node => {
        const profile = mapping[node.dataset.themeCode] || {};
        const themes = Array.isArray(profile) ? profile : (profile.themes || []);
        const industry = Array.isArray(profile) ? '' : String(profile.industry || '').trim();
        node.innerHTML = renderThemeChips(themes);
        const identity = node.closest('.result-stock, .stock-pool-identity');
        const codeNode = identity ? $('.stock-code', identity) : null;
        if (industry && industry !== '未分类' && codeNode) {
          codeNode.textContent = `${node.dataset.themeCode} · ${industry}`;
        }
        const row = node.closest('[data-search]');
        if (row) {
          if (industry && industry !== '未分类') row.dataset.sector = industry;
          row.dataset.search = `${row.dataset.search || ''} ${industry} ${themes.map(theme => theme.name).join(' ')}`.trim();
        }
      });
    } catch (error) {
      mounts.forEach(node => { node.innerHTML = '<span class="theme-empty">题材暂不可用</span>'; });
      console.warn('关联题材加载失败:', error);
    }
  }

  function formatEtaDuration(seconds) {
    const totalMinutes = Math.max(1, Math.round(Number(seconds || 0) / 60));
    const days = Math.floor(totalMinutes / 1440);
    const hours = Math.floor((totalMinutes % 1440) / 60);
    const minutes = totalMinutes % 60;
    if (days) return `${days}天${hours}小时`;
    if (hours) return `${hours}小时${minutes}分钟`;
    return `${minutes}分钟`;
  }

  function formatEtaClock(seconds) {
    const target = new Date(Date.now() + Number(seconds || 0) * 1000);
    const month = String(target.getMonth() + 1).padStart(2, '0');
    const day = String(target.getDate()).padStart(2, '0');
    const hour = String(target.getHours()).padStart(2, '0');
    const minute = String(target.getMinutes()).padStart(2, '0');
    return `${month}-${day} ${hour}:${minute}`;
  }

  function updateEtaEstimate(state, key, completed, total, startedAt) {
    if (!state.etaSamples[key]) state.etaSamples[key] = [];
    const now = Date.now();
    const samples = state.etaSamples[key];
    samples.push({ time: now, completed: Number(completed || 0) });
    const cutoff = now - 5 * 60 * 1000;
    state.etaSamples[key] = samples.filter(item => item.time >= cutoff).slice(-120);
    const recent = state.etaSamples[key];
    if (recent.length < 2 || Number(total || 0) <= Number(completed || 0)) return null;

    const first = recent[0];
    const last = recent[recent.length - 1];
    let deltaCompleted = last.completed - first.completed;
    let elapsedSeconds = (last.time - first.time) / 1000;
    if (deltaCompleted < 3 && elapsedSeconds < 30) return null;
    if (deltaCompleted <= 0 || elapsedSeconds <= 0) {
      const startTime = Date.parse(startedAt || '') || now;
      deltaCompleted = Number(completed || 0);
      elapsedSeconds = Math.max(1, (now - startTime) / 1000);
    }
    const rate = deltaCompleted / elapsedSeconds;
    if (!Number.isFinite(rate) || rate <= 0) return null;
    const remaining = Math.max(0, Number(total) - Number(completed || 0));
    const etaSeconds = remaining / rate;
    return { etaSeconds, ratePerMinute: rate * 60 };
  }

  function updateKlineEta(state, dailyStatus, intradayStatus) {
    const estimates = [];
    if (state.dailyRunning && !state.dailyDone && dailyStatus) {
      const total = Number(dailyStatus.total || state.dailyTotal || 0);
      const completed = total * Math.max(0, Math.min(100, Number(dailyStatus.progress || 0))) / 100;
      const estimate = updateEtaEstimate(state, 'daily', completed, total, dailyStatus.start_time);
      if (estimate) estimates.push(estimate);
    }
    if (state.intradayRunning && !state.intradayDone && intradayStatus) {
      const estimate = updateEtaEstimate(
        state, 'intraday', intradayStatus.completed_codes, intradayStatus.total_codes, intradayStatus.started_at,
      );
      if (estimate) estimates.push(estimate);
    }
    if (!estimates.length) return '预计完成：正在统计下载速度...';
    const etaSeconds = Math.max(...estimates.map(item => item.etaSeconds));
    const rate = estimates.reduce((sum, item) => sum + item.ratePerMinute, 0);
    return `预计完成：${formatEtaClock(etaSeconds)}（约${formatEtaDuration(etaSeconds)}后，最近速度 ${rate.toFixed(1)} 只/分钟）`;
  }

  function formatKlineStartupSummary(status, suffix = '') {
    const expected = Number(status.expectedStockCount || 0);
    const covered = Number(status.coveredStockCount || status.stockCount || 0);
    const missing = Number(status.missingStockCount || Math.max(expected - covered, 0));
    const intraday = status.intraday || {};
    const intradayExpected = Number(intraday.expectedStockCount || expected || 0);
    const intradayCovered = Number(intraday.coveredStockCount || intraday.stockCount || 0);
    const intradayMissing = Number(intraday.missingStockCount || Math.max(intradayExpected - intradayCovered, 0));
    const dailyText = expected
      ? `日线：${covered}/${expected} 只，缺少 ${missing} 只`
      : '日线：尚未准备股票基础资料';
    const intradayText = intradayExpected
      ? `5分钟线：${intradayCovered}/${intradayExpected} 只，缺少 ${intradayMissing} 只`
      : '5分钟线：尚未准备股票基础资料';
    return `${dailyText}\n${intradayText}${suffix ? `\n${suffix}` : ''}`;
  }

  function hasKlineDownloadGap(status) {
    const dailyExpected = Number(status?.expectedStockCount || 0);
    const dailyMissing = Number(status?.missingStockCount || 0);
    const intraday = status?.intraday || {};
    const intradayExpected = Number(intraday.expectedStockCount || 0);
    const intradayMissing = Number(intraday.missingStockCount || 0);
    return (dailyExpected > 0 && dailyMissing > 0)
      || (intradayExpected > 0 && intradayMissing > 0);
  }

  function ensureKlineStartupDialog(status) {
    let backdrop = $('#klineStartupModal');
    if (!backdrop) {
      backdrop = document.createElement('div');
      backdrop.id = 'klineStartupModal';
      backdrop.className = 'modal-backdrop open';
      backdrop.innerHTML = `
        <div class="modal kline-startup-modal" role="dialog" aria-modal="true" aria-labelledby="klineStartupTitle">
          <div class="panel-header">
            <div>
              <div class="panel-title" id="klineStartupTitle">行情数据未完整</div>
              <div class="panel-subtitle">部分股票还没有日线或5分钟历史 K 线，选股和回测结果可能不完整</div>
            </div>
            <button class="icon-btn" id="closeKlineStartup" title="稍后处理" aria-label="稍后处理"><i data-lucide="x"></i></button>
          </div>
          <div class="modal-body">
            <div class="kline-startup-summary" id="klineStartupSummary"></div>
            <div class="kline-startup-progress" id="klineStartupProgress" hidden>
              <div class="kline-startup-progress-track"><div class="kline-startup-progress-fill" id="klineStartupProgressFill"></div></div>
              <div class="kline-startup-progress-meta"><span id="klineStartupProgressText">准备下载</span><strong id="klineStartupProgressPercent">0%</strong></div>
              <div class="kline-startup-eta" id="klineStartupEta">预计完成：正在统计下载速度...</div>
            </div>
          </div>
          <div class="modal-actions">
            <button class="ghost-btn" id="skipKlineStartup">稍后处理</button>
            <button class="primary-btn" id="downloadKlineStartup"><i data-lucide="download"></i>下载缺失日线和5分钟线</button>
          </div>
        </div>`;
      document.body.appendChild(backdrop);
      const close = () => {
        if (klineStartupState) klineStartupState.closed = true;
        if (klineProgressTimer) window.clearTimeout(klineProgressTimer);
        backdrop.remove();
      };
      $('#closeKlineStartup', backdrop).addEventListener('click', close);
      $('#skipKlineStartup', backdrop).addEventListener('click', close);
      backdrop.addEventListener('click', event => {
        if (event.target === backdrop) close();
      });
      $('#downloadKlineStartup', backdrop).addEventListener('click', () => startKlineStartupDownload(backdrop));
      refreshIcons();
    }

    klineStartupState = {
      dailyNeeded: Number(status.missingStockCount || 0) > 0,
      intradayNeeded: Number(status.intraday?.missingStockCount || 0) > 0,
      dailyRunning: false,
      intradayRunning: false,
      dailyDone: Number(status.missingStockCount || 0) <= 0,
      intradayDone: Number(status.intraday?.missingStockCount || 0) <= 0,
      dailyTotal: Number(status.missingStockCount || 1),
      intradayTotal: Number(status.intraday?.missingStockCount || 1),
      errors: [],
      etaSamples: { daily: [], intraday: [] },
      started: false,
      closed: false,
    };
    $('#klineStartupSummary', backdrop).textContent = formatKlineStartupSummary(status);
    return backdrop;
  }

  async function startKlineStartupDownload(backdrop) {
    const state = klineStartupState;
    const button = $('#downloadKlineStartup', backdrop);
    const progress = $('#klineStartupProgress', backdrop);
    const summary = $('#klineStartupSummary', backdrop);
    if (!state || state.closed || state.starting) return;
    state.started = true;
    state.starting = true;
    button.disabled = true;
    progress.hidden = false;
    state.errors = [];
    summary.textContent = '正在启动日线和5分钟线下载任务，已有数据会保留。';
    const jobs = [];
    if (state.dailyNeeded) {
      jobs.push(api('/api/data/kline/download', {
        method: 'POST', body: JSON.stringify({})
      }).then(result => ({type: 'daily', result})));
    }
    if (state.intradayNeeded) {
      jobs.push(api('/api/data/intraday/download', {
        method: 'POST', body: JSON.stringify({})
      }).then(result => ({type: 'intraday', result})));
    }
    const results = await Promise.allSettled(jobs);
    state.starting = false;
    results.forEach(outcome => {
      if (outcome.status === 'rejected') {
        state.errors.push(outcome.reason?.message || '下载任务启动失败');
        return;
      }
      const {type, result} = outcome.value;
      if (!result.taskId) {
        state.errors.push(result.message || `${type === 'daily' ? '日线' : '5分钟线'}下载任务未创建`);
      } else if (type === 'daily') {
        state.dailyRunning = true;
      } else {
        state.intradayRunning = true;
      }
    });
    state.dailyDone = state.dailyDone || !state.dailyRunning;
    state.intradayDone = state.intradayDone || !state.intradayRunning;
    if (!state.dailyRunning && !state.intradayRunning) {
      button.disabled = false;
      summary.textContent = `下载任务启动失败：${state.errors.join('；') || '未知错误'}`;
      return;
    }
    pollKlineStartupDownload(backdrop);
  }

  async function pollKlineStartupDownload(backdrop) {
    const state = klineStartupState;
    if (!state || state.closed) return;
    let dailyPercent = state.dailyDone ? 100 : 0;
    let intradayPercent = state.intradayDone ? 100 : 0;
    let dailyStatus = null;
    let intradayStatus = null;
    try {
      if (state.dailyRunning && !state.dailyDone) {
        dailyStatus = await api('/api/data/init/progress');
        state.dailyTotal = Number(dailyStatus.total || state.dailyTotal || 1);
        dailyPercent = Math.max(0, Math.min(100, Number(dailyStatus.progress || 0)));
        if (['completed', 'failed', 'cancelled'].includes(dailyStatus.status)) {
          state.dailyDone = true;
          if (dailyStatus.status !== 'completed') {
            state.dailyError = dailyStatus.message || '日线下载失败';
            state.errors.push(state.dailyError);
          }
        }
      }
      if (state.intradayRunning && !state.intradayDone) {
        intradayStatus = await api('/api/data/intraday/progress');
        state.intradayTotal = Number(intradayStatus.total_codes || state.intradayTotal || 1);
        intradayPercent = Math.max(0, Math.min(100, Number(intradayStatus.percent || 0)));
        if (['completed', 'failed', 'cancelled'].includes(intradayStatus.status)) {
          state.intradayDone = true;
          if (intradayStatus.status !== 'completed') {
            state.intradayError = intradayStatus.message || '5分钟线下载失败';
            state.errors.push(state.intradayError);
          }
        }
      }
      const dailyWeight = state.dailyRunning ? state.dailyTotal : 0;
      const intradayWeight = state.intradayRunning ? state.intradayTotal : 0;
      const totalWeight = dailyWeight + intradayWeight;
      const percent = totalWeight
        ? (dailyPercent * dailyWeight + intradayPercent * intradayWeight) / totalWeight
        : 100;
      $('#klineStartupProgressFill', backdrop).style.width = `${percent}%`;
      $('#klineStartupProgressPercent', backdrop).textContent = `${percent.toFixed(0)}%`;
      $('#klineStartupProgressText', backdrop).textContent = `日线 ${dailyPercent.toFixed(0)}% · 5分钟线 ${intradayPercent.toFixed(0)}%`;
      $('#klineStartupEta', backdrop).textContent = updateKlineEta(state, dailyStatus, intradayStatus);
      const runningTasks = [];
      if (state.dailyRunning && !state.dailyDone) runningTasks.push('日线');
      if (state.intradayRunning && !state.intradayDone) runningTasks.push('5分钟线');
      if (state.errors.length && runningTasks.length) {
        $('#klineStartupSummary', backdrop).textContent = `${state.errors[state.errors.length - 1]}；${runningTasks.join('和')}下载仍在继续。`;
      }
      if (state.dailyDone && state.intradayDone) {
        const latest = await api('/api/data/kline/completeness');
        if (latest.complete && latest.intraday?.complete) {
          if (klineProgressTimer) window.clearTimeout(klineProgressTimer);
          $('#klineStartupProgressFill', backdrop).style.width = '100%';
          $('#klineStartupProgressPercent', backdrop).textContent = '100%';
          $('#klineStartupProgressText', backdrop).textContent = '日线和5分钟K线下载完成';
          $('#klineStartupSummary', backdrop).textContent = '下载完成，窗口即将自动关闭。';
          toast('日线和5分钟K线下载完成');
          window.setTimeout(async () => {
            backdrop.remove();
            try {
              await api('/api/app/restart', { method: 'POST', body: JSON.stringify({}) });
            } catch (error) {
              // The data is already complete; keep the current window usable
              // if a manually launched browser cannot restart the desktop shell.
              console.warn('自动重启桌面应用失败:', error);
            }
          }, 1200);
          return;
        }
        $('#klineStartupSummary', backdrop).textContent = formatKlineStartupSummary(latest, '本次下载结束后仍有数据缺失，请稍后重试。');
        $('#downloadKlineStartup', backdrop).disabled = false;
        return;
      }
    } catch (error) {
      $('#klineStartupProgressText', backdrop).textContent = `读取下载进度失败：${error.message}`;
    }
    klineProgressTimer = window.setTimeout(() => pollKlineStartupDownload(backdrop), 1000);
  }

  async function checkStartupKlineData() {
    try {
      const status = await api('/api/data/kline/completeness');
      if (hasKlineDownloadGap(status)) {
        const backdrop = ensureKlineStartupDialog(status);
        window.setTimeout(() => {
          if (klineStartupState && !klineStartupState.started && !klineStartupState.closed) {
            startKlineStartupDownload(backdrop);
          }
        }, 250);
      }
    } catch (error) {
      console.warn('启动时检查 K 线完整性失败:', error);
    }
  }
  const formatMoney = value => `¥ ${Number(value || 0).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`;
  const formatPercent = value => `${Number(value || 0) >= 0 ? '+' : ''}${Number(value || 0).toFixed(2)}%`;
  const formatTradeDateTime = value => {
    if (!value) return '--';
    const text = String(value).replace('T', ' ').trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(text) || / 00:00:00$/.test(text)) {
      return `${text.slice(0, 10)} 09:30:00`;
    }
    return text.slice(0, 19);
  };
  const backtestSellTypeNames = {
    take_profit_pullback: '止盈回撤',
    take_profit_reduce_1: '首档止盈减仓',
    take_profit_reduce_2: '二档止盈减仓',
    take_profit: '止盈',
    stop_loss: '止损',
    position_expire: '持仓期限到期',
    moving_average_break_5: '跌破5日均线',
    moving_average_break_10: '跌破10日均线',
    moving_average_break_20: '跌破20日均线',
    strategy_sell: '策略卖出',
    strategy_reduce: '策略减仓',
    backtest_end: '区间结束结算',
    end_of_backtest: '区间结束结算'
  };
  const formatBacktestSellType = value => backtestSellTypeNames[value] || value || '--';
  const formatRemainingTime = seconds => {
    if (!Number.isFinite(seconds)) return '预计剩余：计算中';
    if (seconds < 60) return `预计剩余：约 ${Math.max(1, Math.round(seconds))} 秒`;
    const minutes = Math.ceil(seconds / 60);
    return `预计剩余：约 ${minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟` : `${minutes} 分钟`}`;
  };
  const parsePercent = value => Number(String(value ?? 0).replace('%', '').replace('+', '')) || 0;
  const formatDayChange = value => `${parsePercent(value) >= 0 ? '+' : ''}${parsePercent(value).toFixed(2)}%`;
  const dayChangeClass = value => parsePercent(value) > 0 ? 'rise' : parsePercent(value) < 0 ? 'fall' : 'flat';
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function resetBacktestView() {
    ['backtestReturn', 'backtestAnnual', 'backtestWin', 'backtestDrawdown', 'backtestTrades'].forEach(id => setText(id, '--'));
    setText('backtestRunState', '待运行');
    setText('backtestStatus', '最近运行：未运行');
    const table = $('#backtestTable');
    if (table) table.innerHTML = '<tr><td colspan="10" class="stock-pool-empty">运行回测后显示真实交易记录</td></tr>';
    const equityPath = $('#backtestEquityPath');
    const benchmarkPath = $('#backtestBenchmarkPath');
    if (equityPath) equityPath.setAttribute('d', '');
    if (benchmarkPath) benchmarkPath.setAttribute('d', '');
    $$('#history-backtest [data-axis-month]').forEach(axis => { axis.textContent = '--/--'; });
    const progressPanel = ensureBacktestDownloadProgress();
    if (progressPanel) {
      progressPanel.hidden = false;
      setText('backtestDownloadText', '分钟数据下载进度');
      setText('backtestDownloadPercent', '等待回测');
      setText('backtestDownloadMeta', '点击“运行回测”后显示已完成股票数和预计剩余时间');
    }
  }

  function ensureBacktestDownloadProgress() {
    let panel = $('#backtestDownloadProgress');
    if (panel) return panel;
    const toolbar = $('#history-backtest .backtest-toolbar');
    if (!toolbar) return null;
    panel = document.createElement('div');
    panel.id = 'backtestDownloadProgress';
    panel.className = 'backtest-download-progress';
    panel.setAttribute('role', 'status');
    panel.setAttribute('aria-live', 'polite');
    panel.innerHTML = '<div class="backtest-download-heading"><span id="backtestDownloadText">正在准备分钟数据...</span><strong id="backtestDownloadPercent">0%</strong></div><div class="backtest-download-bar"><span id="backtestDownloadFill"></span></div><div id="backtestDownloadMeta" class="backtest-download-meta">正在计算需要下载的股票</div>';
    toolbar.insertAdjacentElement('afterend', panel);
    return panel;
  }

  function renderBacktestDownloadProgress(progress) {
    const panel = ensureBacktestDownloadProgress();
    if (!panel) return;
    const phase = progress?.phase || 'discovering';
    panel.hidden = phase === 'completed' || phase === 'failed';
    if (panel.hidden) return;
    const selecting = phase === 'selecting';
    const total = Number(selecting ? progress.total_days : progress.total_codes || 0);
    const completed = Number(selecting ? progress.completed_days : progress.completed_codes || 0);
    const percent = Number(progress.percent || 0);
    const current = progress.current_code ? ` · ${progress.current_code}` : '';
    const text = phase === 'idle'
      ? '分钟数据下载进度'
      : selecting
      ? '正在扫描历史交易日，定位需要分钟数据的股票'
      : phase === 'replaying'
      ? '分钟数据已就绪，正在按 5 分钟 K 线回放交易'
      : total ? `正在补齐 5 分钟历史数据${current}` : '正在计算需要下载的股票';
    setText('backtestDownloadText', text);
    setText('backtestDownloadPercent', phase === 'replaying' ? '100%' : `${percent.toFixed(1)}%`);
    const fill = $('#backtestDownloadFill');
    if (fill) fill.style.width = `${phase === 'replaying' ? 100 : percent}%`;
    const parts = total ? [`${completed}/${total} ${selecting ? '个交易日' : '只股票'}`] : ['点击“运行回测”后显示实际进度'];
    if (progress.cached_codes) parts.push(`本地命中 ${progress.cached_codes} 只`);
    if (progress.downloaded_codes) parts.push(`本次下载 ${progress.downloaded_codes} 只`);
    if (phase !== 'replaying') parts.push(formatRemainingTime(progress.estimated_remaining_seconds));
    setText('backtestDownloadMeta', parts.join(' · '));
  }

  function startBacktestProgressPolling() {
    const panel = ensureBacktestDownloadProgress();
    if (panel) panel.hidden = false;
    let stopped = false;
    const poll = async () => {
      if (stopped) return;
      try {
        const progress = await api('/api/trading/backtest/progress');
        renderBacktestDownloadProgress(progress);
      } catch (error) {
        console.warn('读取回测进度失败:', error);
      }
      if (!stopped) window.setTimeout(poll, 1000);
    };
    poll();
    return () => {
      stopped = true;
      if (panel) panel.hidden = true;
    };
  }

  function calculateAnnualizedReturn(totalReturn, startDate, endDate) {
    const start = new Date(`${startDate}T00:00:00`);
    const end = new Date(`${endDate}T00:00:00`);
    const days = Number.isFinite(start.getTime()) && Number.isFinite(end.getTime())
      ? Math.max(1, (end - start) / 86400000)
      : 365;
    const years = days / 365;
    const base = 1 + Number(totalReturn || 0) / 100;
    return base > 0 ? (Math.pow(base, 1 / years) - 1) * 100 : -100;
  }

  function renderBacktestEquityCurve(curve) {
    if (!Array.isArray(curve) || !curve.length) return;
    const values = curve.map(item => Number(item.capital)).filter(Number.isFinite);
    if (!values.length) return;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    const width = 820;
    const height = 100;
    const points = values.map((value, index) => {
      const x = values.length === 1 ? 0 : index / (values.length - 1) * width;
      const y = height - 10 - ((value - min) / span) * 78;
      return `${index === 0 ? 'M' : 'L'}${x.toFixed(2)} ${y.toFixed(2)}`;
    }).join(' ');
    const path = $('#backtestEquityPath');
    const benchmark = $('#backtestBenchmarkPath');
    if (path) path.setAttribute('d', points);
    if (benchmark) benchmark.setAttribute('d', '');

    const dates = curve.map(item => String(item.date || '').slice(0, 10)).filter(Boolean);
    const axes = $$('#history-backtest [data-axis-month]');
    axes.forEach((axis, index) => {
      if (!dates.length) return;
      const date = dates[Math.round(index * (dates.length - 1) / Math.max(1, axes.length - 1))];
      axis.textContent = date ? date.slice(5).replace('-', '/') : '--/--';
    });
  }

  function ensureBacktestTradeModal() {
    let modal = $('#backtestTradeModal');
    if (modal) return modal;
    const style = document.createElement('style');
    style.id = 'backtest-trade-modal-style';
    style.textContent = `
      .backtest-trade-modal { width: min(980px, 100%); }
      .backtest-trade-modal .modal-body { padding: 18px 20px 20px; }
      .trade-chart-summary { display:flex; flex-wrap:wrap; gap:8px 18px; color:#607169; font-size:12px; margin-bottom:12px; }
      .trade-chart-summary strong { color:#15231e; font-size:13px; }
      .trade-day-tabs { display:flex; gap:6px; overflow-x:auto; padding-bottom:8px; }
      .trade-day-tab { border:1px solid #dfe8e2; background:#fff; color:#52645b; border-radius:5px; padding:6px 10px; cursor:pointer; white-space:nowrap; font-size:12px; }
      .trade-day-tab.active { color:#087c59; border-color:#86d3b5; background:#edf9f3; font-weight:700; }
      .trade-chart-shell { position:relative; min-height:350px; border:1px solid #dfe8e2; border-radius:7px; background:#fff; overflow:hidden; }
      .trade-chart-canvas { display:block; width:100%; height:350px; }
      .trade-chart-loading, .trade-chart-empty { min-height:350px; display:grid; place-items:center; color:#75847b; font-size:13px; padding:24px; text-align:center; }
      .trade-chart-foot { display:flex; justify-content:space-between; gap:12px; margin-top:8px; color:#74827a; font-size:11px; }
      .trade-chart-legend { display:flex; gap:14px; }
      .trade-chart-legend span::before { content:''; display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:5px; background:#d84b4b; }
      .trade-chart-legend span:last-child::before { background:#1c9b78; }
      .trade-operation-list { overflow:auto; min-height:350px; }
      .trade-operation-list table { width:100%; border-collapse:collapse; font-size:12px; }
      .trade-operation-list th, .trade-operation-list td { padding:10px 12px; border-bottom:1px solid #edf2ee; text-align:left; white-space:nowrap; }
      .trade-operation-list th { color:#718078; font-weight:600; background:#f7faf8; }
      .backtest-trade-row { cursor:pointer; }
      .backtest-trade-row:hover { background:#f4faf7; }
    `;
    document.head.appendChild(style);
    modal = document.createElement('div');
    modal.id = 'backtestTradeModal';
    modal.className = 'modal-backdrop';
    modal.innerHTML = `<div class="modal backtest-trade-modal" role="dialog" aria-modal="true" aria-labelledby="backtestTradeTitle">
      <div class="panel-header"><div><div class="panel-title" id="backtestTradeTitle">交易分时图</div><div class="panel-subtitle" id="backtestTradeSubtitle">真实历史分时数据</div></div><button class="icon-btn" type="button" id="closeBacktestTradeModal" title="关闭"><i data-lucide="x"></i></button></div>
      <div class="modal-body"><div class="trade-chart-summary" id="tradeChartSummary"></div><div class="trade-day-tabs" id="tradeDayTabs"></div><div class="trade-chart-shell" id="tradeChartShell"><div class="trade-chart-loading">正在读取真实历史分时数据...</div></div><div class="trade-chart-foot"><span id="tradeChartStatus">--</span><span class="trade-chart-legend"><span>上涨</span><span>下跌</span></span></div></div>
    </div>`;
    document.body.appendChild(modal);
    $('#closeBacktestTradeModal', modal).addEventListener('click', () => modal.classList.remove('open'));
    modal.addEventListener('click', event => { if (event.target === modal) modal.classList.remove('open'); });
    refreshIcons();
    return modal;
  }

  function parseTradeTimestamp(value) {
    const text = String(value || '').replace(' ', 'T');
    const timestamp = Date.parse(text);
    return Number.isFinite(timestamp) ? timestamp : 0;
  }

  function renderTradeDayChart(modal, bars, trade, day) {
    const shell = $('#tradeChartShell', modal);
    if (!shell) return;
    shell.innerHTML = '<canvas class="trade-chart-canvas" id="tradeChartCanvas" aria-label="交易分时K线图"></canvas>';
    const canvas = $('#tradeChartCanvas', shell);
    const width = Math.max(640, Math.floor(canvas.getBoundingClientRect().width || 760));
    const height = 350;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const ctx = canvas.getContext('2d');
    ctx.scale(ratio, ratio);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, width, height);
    const pad = { left: 58, right: 18, top: 18, bottom: 34 };
    const priceBottom = height - 88;
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = priceBottom - pad.top;
    const validBars = bars.filter(item => Number(item.high) > 0 && Number(item.low) > 0);
    if (!validBars.length) {
      shell.innerHTML = '<div class="trade-chart-empty">该交易日没有可用的分时数据</div>';
      return;
    }
    const highs = validBars.map(item => Number(item.high));
    const lows = validBars.map(item => Number(item.low));
    const maxPrice = Math.max(...highs);
    const minPrice = Math.min(...lows);
    const priceSpan = maxPrice - minPrice || Math.max(maxPrice * 0.01, 0.01);
    const volumeMax = Math.max(...validBars.map(item => Number(item.volume) || 0), 1);
    const xAt = index => pad.left + (index + 0.5) / validBars.length * plotWidth;
    const yAt = price => pad.top + (maxPrice - price) / priceSpan * plotHeight;

    ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.strokeStyle = '#e9efeb';
    ctx.fillStyle = '#9a8883';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i += 1) {
      const y = pad.top + plotHeight * i / 4;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
      const price = maxPrice - priceSpan * i / 4;
      ctx.fillText(price.toFixed(2), 8, y + 4);
    }
    ctx.beginPath(); ctx.moveTo(pad.left, priceBottom + 1); ctx.lineTo(width - pad.right, priceBottom + 1); ctx.stroke();
    const candleWidth = Math.max(2, Math.min(10, plotWidth / validBars.length * 0.62));
    validBars.forEach((bar, index) => {
      const open = Number(bar.open); const close = Number(bar.close);
      const high = Number(bar.high); const low = Number(bar.low);
      const up = close >= open;
      const color = up ? '#d84b4b' : '#1c9b78';
      const x = xAt(index);
      ctx.strokeStyle = color; ctx.fillStyle = color;
      ctx.beginPath(); ctx.moveTo(x, yAt(high)); ctx.lineTo(x, yAt(low)); ctx.stroke();
      const top = Math.min(yAt(open), yAt(close));
      const bodyHeight = Math.max(1, Math.abs(yAt(open) - yAt(close)));
      ctx.fillRect(x - candleWidth / 2, top, candleWidth, bodyHeight);
      const volHeight = (Number(bar.volume) || 0) / volumeMax * 42;
      ctx.globalAlpha = 0.45; ctx.fillRect(x - candleWidth / 2, height - 42 - volHeight, candleWidth, volHeight); ctx.globalAlpha = 1;
    });

    const marker = (timestamp, label, color, priceValue) => {
      if (!timestamp) return;
      const target = parseTradeTimestamp(timestamp);
      let nearest = 0; let distance = Infinity;
      validBars.forEach((bar, index) => { const delta = Math.abs(parseTradeTimestamp(bar.timestamp) - target); if (delta < distance) { distance = delta; nearest = index; } });
      const x = xAt(nearest); const y = yAt(Number(priceValue || validBars[nearest].close));
      ctx.strokeStyle = color; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(x, pad.top); ctx.lineTo(x, priceBottom); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = '#fff'; ctx.font = 'bold 10px sans-serif'; ctx.fillText(label, x - 3, y + 3);
      ctx.fillStyle = color; ctx.font = 'bold 11px sans-serif'; ctx.fillText(label, x - 4, Math.max(12, y - 9));
    };
    if (String(trade.buy_date || '').slice(0, 10) === day) marker(trade.buy_date, 'B', '#d94b4b', Number(trade.buy_price));
    if (String(trade.sell_date || '').slice(0, 10) === day) marker(trade.sell_date, 'S', '#16835f', Number(trade.sell_price));
    const labelCount = Math.min(6, validBars.length);
    ctx.fillStyle = '#9a8883'; ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
    for (let i = 0; i < labelCount; i += 1) {
      const item = validBars[Math.round(i * (validBars.length - 1) / Math.max(1, labelCount - 1))];
      const label = String(item.timestamp || '').slice(11, 16);
      ctx.fillText(label, xAt(Math.round(i * (validBars.length - 1) / Math.max(1, labelCount - 1))) - 16, height - 12);
    }
    const intervalLabel = trade.interval === 'daily' ? '日线' : `${trade.interval || 5}分钟`;
    setText('tradeChartStatus', `${day} · ${validBars.length} 根${intervalLabel}K线`);
  }

  async function openBacktestTradeDetail(trade) {
    const modal = ensureBacktestTradeModal();
    const code = trade.stock_code || '';
    setText('backtestTradeTitle', `${trade.stock_name || code} · ${code}`);
    setText('backtestTradeSubtitle', '历史5分钟分时 · 标出买入/卖出点');
    $('.trade-chart-legend', modal).style.display = 'flex';
    setText('tradeChartSummary', `买入 ${trade.buy_date || '--'} · ${Number(trade.buy_price || 0).toFixed(2)} · ${Number(trade.quantity || 0).toLocaleString('zh-CN')}股　卖出 ${trade.sell_date || '--'} · ${trade.sell_price ? Number(trade.sell_price).toFixed(2) : '--'}　收益 ${formatPercent(trade.return_rate || trade.profit_rate || 0)}　原因 ${formatBacktestSellType(trade.sell_type || trade.trade_type)}`);
    const shell = $('#tradeChartShell', modal);
    const tabs = $('#tradeDayTabs', modal);
    if (shell) shell.innerHTML = '<div class="trade-chart-loading">正在读取真实历史5分钟分时...</div>';
    if (tabs) tabs.innerHTML = '';
    modal.classList.add('open');
    try {
      const payload = await api(`/api/trading/backtest/trades/${encodeURIComponent(trade.id)}/intraday?interval=5&trade_days=1`);
      const bars = Array.isArray(payload?.bars) ? payload.bars : [];
      const estimated = Boolean(payload?.estimated);
      setText('backtestTradeSubtitle', estimated
        ? '估算分时（基于真实日线OHLCV） · 标出买入/卖出点'
        : '真实历史5分钟分时 · 标出买入/卖出点');
      const days = [...new Set(bars.map(item => String(item.timestamp || '').slice(0, 10)).filter(Boolean))];
      if (!days.length) throw new Error('该笔交易没有可用的历史5分钟分时数据');
      const grouped = new Map(days.map(day => [day, bars.filter(item => String(item.timestamp || '').startsWith(day))]));
      let selectedDay = days[days.length - 1];
      const render = () => {
        tabs.innerHTML = days.map(day => `<button type="button" class="trade-day-tab${day === selectedDay ? ' active' : ''}" data-day="${escapeHtml(day)}">${escapeHtml(day.slice(5).replace('-', '/'))}</button>`).join('');
        $$('.trade-day-tab', tabs).forEach(button => button.addEventListener('click', () => { selectedDay = button.dataset.day; render(); renderTradeDayChart(modal, grouped.get(selectedDay) || [], trade, selectedDay); }));
        renderTradeDayChart(modal, grouped.get(selectedDay) || [], trade, selectedDay);
      };
      render();
    } catch (error) {
      if (shell) shell.innerHTML = `<div class="trade-chart-empty">${escapeHtml(error.message)}<br>请确认对应交易日已准备历史5分钟数据。</div>`;
      if (tabs) tabs.innerHTML = '';
      setText('tradeChartStatus', '历史分时不可用');
    }
  }

  const backtestOperationName = trade => {
    if (trade.sell_date) return formatBacktestSellType(trade.sell_type || trade.trade_type || 'strategy_sell');
    return String(trade.trade_type || '').toLowerCase() === 'add' ? '加仓' : '建仓';
  };

  function buildBacktestTradeSummaries(trades) {
    const groups = new Map();
    (trades || []).forEach(trade => {
      const code = String(trade.stock_code || '').padStart(6, '0');
      if (!code) return;
      if (!groups.has(code)) {
        groups.set(code, {
          code,
          name: trade.stock_name || code,
          strategy: trade.strategy_name || '策略信号',
          operations: [],
          buyRecords: [],
          sellRecords: [],
          holdDays: 0,
        });
      }
      const group = groups.get(code);
      group.holdDays = Math.max(group.holdDays, Number(trade.hold_days || 0));
      const isSell = Boolean(trade.sell_date);
      const sellAmount = Number(trade.sell_amount);
      const operation = {
        type: backtestOperationName(trade),
        time: isSell ? trade.sell_date : trade.buy_date,
        price: Number(isSell ? trade.sell_price : trade.buy_price) || 0,
        quantity: Number(trade.quantity) || 0,
        // The current backtest response does not persist sell_amount. Recover
        // the net sell proceeds from the allocated cost and realized P/L.
        amount: isSell
          ? (Number.isFinite(sellAmount) && sellAmount > 0
            ? sellAmount
            : (Number(trade.buy_amount) || 0) + (Number(trade.profit_loss) || 0))
          : (Number(trade.buy_amount) || 0),
        returnRate: isSell ? (trade.return_rate ?? trade.profit_rate) : null,
        profitLoss: isSell ? Number(trade.profit_loss || 0) : 0,
      };
      group.operations.push(operation);
      if (isSell) group.sellRecords.push(trade);
      else group.buyRecords.push(trade);
    });

    return [...groups.values()].map(group => {
      const buyAmount = group.buyRecords.reduce((sum, item) => sum + (Number(item.buy_amount) || 0), 0);
      const realizedProfit = group.sellRecords.reduce((sum, item) => sum + (Number(item.profit_loss) || 0), 0);
      const boughtQuantity = group.buyRecords.reduce((sum, item) => sum + (Number(item.quantity) || 0), 0);
      const soldQuantity = group.sellRecords.reduce((sum, item) => sum + (Number(item.quantity) || 0), 0);
      const openQuantity = Math.max(0, boughtQuantity - soldQuantity);
      const hasOpen = openQuantity > 0;

      // Buy rows remain in the API after a reduction or full liquidation. Use
      // the remaining quantity to decide whether their floating P/L is still
      // applicable, scaling each lot when it was only partially sold.
      let remainingToAllocate = soldQuantity;
      const openRecords = group.buyRecords.map(item => {
        const quantity = Math.max(0, Number(item.quantity) || 0);
        const soldFromLot = Math.min(quantity, remainingToAllocate);
        remainingToAllocate -= soldFromLot;
        return { item, remainingQuantity: quantity - soldFromLot };
      }).filter(record => record.remainingQuantity > 0);
      const currentProfit = openRecords.reduce((sum, record) => {
        const item = record.item;
        const quantity = Number(item.quantity) || 0;
        const profit = Number(item.current_profit_loss) || 0;
        return sum + (quantity > 0 ? profit * record.remainingQuantity / quantity : 0);
      }, 0);
      const totalProfit = realizedProfit + currentProfit;
      const buyTimes = group.buyRecords.map(item => String(item.buy_date || '')).filter(Boolean).sort();
      const sellTimes = group.sellRecords.map(item => String(item.sell_date || '')).filter(Boolean).sort();
      const lastOpen = openRecords[openRecords.length - 1] || {};
      const lastSell = group.sellRecords[group.sellRecords.length - 1] || {};
      const quantity = boughtQuantity;
      const avgBuyPrice = quantity > 0
        ? group.buyRecords.reduce((sum, item) => sum + (Number(item.buy_price) || 0) * (Number(item.quantity) || 0), 0) / quantity
        : 0;
      group.operations.sort((left, right) => String(left.time || '').localeCompare(String(right.time || '')));
      return {
        code: group.code,
        name: group.name,
        strategy: group.strategy,
        startTime: buyTimes[0] || '--',
        clearTime: hasOpen ? '--' : (sellTimes[sellTimes.length - 1] || '--'),
        avgBuyPrice,
        quantity,
        currentOrSellPrice: hasOpen
          ? Number(lastOpen.item?.current_price || 0)
          : Number(lastSell.sell_price || 0),
        holdDays: group.holdDays,
        totalProfit,
        totalReturn: buyAmount > 0 ? totalProfit / buyAmount * 100 : 0,
        status: hasOpen ? '持仓中' : '已清仓',
        operations: group.operations,
      };
    }).sort((left, right) => left.startTime.localeCompare(right.startTime) || left.code.localeCompare(right.code));
  }

  function openBacktestTradeSummary(summary) {
    const modal = ensureBacktestTradeModal();
    setText('backtestTradeTitle', `${summary.name} · ${summary.code}`);
    setText('backtestTradeSubtitle', '单只股票操作明细');
    $('.trade-chart-legend', modal).style.display = 'none';
    setText('tradeChartSummary', `开始建仓 ${formatTradeDateTime(summary.startTime)} · 清仓 ${summary.clearTime === '--' ? '持仓中' : formatTradeDateTime(summary.clearTime)} · 累计收益 ${formatPercent(summary.totalReturn)} · 累计盈亏 ¥ ${summary.totalProfit.toFixed(2)}`);
    const tabs = $('#tradeDayTabs', modal);
    if (tabs) tabs.innerHTML = '';
    const shell = $('#tradeChartShell', modal);
    if (shell) {
      shell.innerHTML = `<div class="trade-operation-list"><table><thead><tr><th>操作</th><th>时间</th><th>价格</th><th>数量</th><th>金额</th><th>收益</th></tr></thead><tbody>${summary.operations.map(operation => `<tr><td>${escapeHtml(operation.type)}</td><td>${escapeHtml(formatTradeDateTime(operation.time))}</td><td>¥ ${operation.price.toFixed(2)}</td><td>${operation.quantity ? operation.quantity.toLocaleString('zh-CN') : '--'}</td><td>¥ ${operation.amount.toFixed(2)}</td><td>${operation.returnRate == null ? '--' : formatPercent(operation.returnRate)}</td></tr>`).join('')}</tbody></table></div>`;
    }
    setText('tradeChartStatus', `共 ${summary.operations.length} 次操作`);
    modal.classList.add('open');
  }

  function readConditionNumber(value) {
    const match = String(value ?? '').replace(',', '.').match(/-?\d+(?:\.\d+)?/);
    return match ? Number(match[0]) : null;
  }

  const SELL_RULES_STORAGE_KEY = 'quantpilot.sellRules.v1';

  function readSellRuleControls() {
    const priceRows = $$('#sellConditionBlock .sell-price-row');
    const rowByTitle = title => priceRows.find(row => $('strong', row)?.textContent.includes(title));
    const mobileRow = rowByTitle('移动止盈');
    const fixedRow = rowByTitle('固定止损');
    const limitUpRow = rowByTitle('涨停兑现');
    const splitInputs = $$('#sellConditionBlock .sell-price-card input');
    const limitUpTrigger = $('select', limitUpRow)?.value || '';
    return {
      stop_loss: Math.abs(readConditionNumber($('input', fixedRow)?.value) ?? 5),
      trailing_profit_drawdown: Math.abs(readConditionNumber($('input', mobileRow)?.value) ?? 10),
      limit_up_profit: readConditionNumber(limitUpTrigger) ?? 20,
      limit_up_sell_pct: Math.abs(readConditionNumber($('input', limitUpRow)?.value) ?? 30),
      take_profit: Math.abs(readConditionNumber(splitInputs[0]?.value) ?? 15),
      take_profit_sell_pct: 30,
      second_take_profit: Math.abs(readConditionNumber(splitInputs[1]?.value) ?? 30),
      second_take_profit_sell_pct: 30,
      remaining_position_pct: Math.abs(readConditionNumber(splitInputs[2]?.value) ?? 40),
      initial_position_pct: Math.max(1, Math.min(100, Number($('#initialPositionPct')?.value || 30))),
      add_position_pct: Math.max(1, Math.min(100, Number($('#addPositionPct')?.value || 50)))
    };
  }

  function restoreSellRuleControls() {
    try {
      const saved = JSON.parse(localStorage.getItem(SELL_RULES_STORAGE_KEY) || 'null');
      if (!saved) return;
      const inputs = $$('#sellConditionBlock .sell-price-card input');
      if (saved.take_profit != null && inputs[0]) inputs[0].value = saved.take_profit;
      if (saved.second_take_profit != null && inputs[1]) inputs[1].value = saved.second_take_profit;
      if (saved.remaining_position_pct != null && inputs[2]) inputs[2].value = saved.remaining_position_pct;
      const priceRows = $$('#sellConditionBlock .sell-price-row');
      const rowByTitle = title => priceRows.find(row => $('strong', row)?.textContent.includes(title));
      const fixedRow = rowByTitle('固定止损');
      const mobileRow = rowByTitle('移动止盈');
      const limitUpRow = rowByTitle('涨停兑现');
      if (saved.stop_loss != null && fixedRow) $('input', fixedRow).value = `-${saved.stop_loss}%`;
      if (saved.trailing_profit_drawdown != null && mobileRow) $('input', mobileRow).value = `-${saved.trailing_profit_drawdown}%`;
      if (saved.limit_up_sell_pct != null && limitUpRow) $('input', limitUpRow).value = `${saved.limit_up_sell_pct}%`;
    } catch (_) {
      // Ignore malformed local UI state and keep the template defaults.
    }
  }

  async function restorePositionControls() {
    try {
      const payload = await api('/api/trading/backtest/configs');
      const config = payload?.configs?.[0];
      if (!config) return;
      const initial = $('#initialPositionPct');
      const add = $('#addPositionPct');
      if (initial && config.initial_position_pct != null) initial.value = config.initial_position_pct;
      if (add && config.add_position_pct != null) add.value = config.add_position_pct;
    } catch (_) {
      // Keep the page defaults when the configuration endpoint is unavailable.
    }
  }

  async function saveBacktestConditionSettings() {
    const name = $('#conditionStrategyName')?.value.trim() || '默认交易条件';
    const existingPayload = await api('/api/trading/backtest/configs');
    const existing = existingPayload?.configs?.[0] || {};
    const config = {
      config_name: name,
      score_threshold: Number(existing.score_threshold ?? 60),
      hold_period: Number(existing.hold_period ?? 10),
      stop_loss: Number(existing.stop_loss ?? -5),
      take_profit: Number(existing.take_profit ?? 15),
      initial_capital: Number(existing.initial_capital ?? 300000),
      buy_amount: Number(existing.buy_amount ?? 100000),
      max_daily_buys: Number(existing.max_daily_buys ?? 8),
      buy_point_lower: Number(existing.buy_point_lower ?? -1),
      buy_point_upper: Number(existing.buy_point_upper ?? 3)
    };

    $$('#sellConditionList .condition-row').forEach(row => {
      const enabled = $('.condition-switch', row)?.classList.contains('on');
      if (!enabled) return;
      const field = $('select', row)?.value || '';
      const value = readConditionNumber($('input', row)?.value);
      if (value == null) return;
      if (field.includes('首档止盈')) config.take_profit = Math.abs(value);
      if (field.includes('二档止盈')) config.second_take_profit = Math.abs(value);
      if (field.includes('止损')) config.stop_loss = -Math.abs(value);
      if (field.includes('持仓天数')) config.hold_period = Math.max(1, Math.round(Math.abs(value)));
    });

    const sellRules = readSellRuleControls();
    config.take_profit = sellRules.take_profit;
    config.take_profit_sell_pct = sellRules.take_profit_sell_pct;
    config.second_take_profit = sellRules.second_take_profit;
    config.second_take_profit_sell_pct = sellRules.second_take_profit_sell_pct;
    config.trailing_profit_drawdown = sellRules.trailing_profit_drawdown;
    config.limit_up_profit = sellRules.limit_up_profit;
    config.limit_up_sell_pct = sellRules.limit_up_sell_pct;
    config.remaining_position_pct = sellRules.remaining_position_pct;
    config.initial_position_pct = sellRules.initial_position_pct;
    config.add_position_pct = sellRules.add_position_pct;
    config.stop_loss = -sellRules.stop_loss;
    try { localStorage.setItem(SELL_RULES_STORAGE_KEY, JSON.stringify(sellRules)); } catch (_) {}

    $$('#buyConditionList .condition-row').forEach(row => {
      const enabled = $('.condition-switch', row)?.classList.contains('on');
      const field = $('select', row)?.value || '';
      const value = readConditionNumber($('input', row)?.value);
      if (enabled && field.includes('题材强度') && value != null) config.score_threshold = value;
    });

    await api('/api/trading/backtest/configs', { method: 'POST', body: JSON.stringify(config) });
    const status = $('#conditionStatusText');
    if (status) status.textContent = '已保存 · 刚刚';
    toast(`条件设置「${name}」已保存：首档止盈 ${config.take_profit}%、二档止盈 ${config.second_take_profit}%、止损 ${config.stop_loss}%`);
  }

  const automationConfigFieldIds = [
    'automationExternalGate', 'automationThemeEnabled',
    'automationThemeScore', 'automationOpenDelay', 'automationVwapGate',
    'automationVwapDirectDeviation', 'automationVwapRejectDeviation',
    'automationHighDistance', 'automationDailyGain',
    'automationOpenPriceGate', 'automationOpeningLowGate', 'automationMaxPositions',
    'automationStockCap', 'automationTotalCap', 'automationInitialPosition',
    'automationAddPosition', 'automationMaxHoldDays', 'automationStopLoss',
    'automationTakeProfit', 'automationTakeProfitSell', 'automationSecondTakeProfit',
    'automationSecondTakeProfitSell', 'automationTrailingDrawdown',
    'automationLimitUpProfit', 'automationLimitUpSell'
  ];

  function setAutomationConfigForm(config = {}) {
    const setValue = (id, value) => {
      const input = $(`#${id}`);
      if (input && value !== undefined && value !== null) input.value = value;
    };
    const setChecked = (id, value) => {
      const input = $(`#${id}`);
      if (input) input.checked = Boolean(value);
    };
    setChecked('automationExternalGate', config.require_external_environment);
    setChecked('automationThemeEnabled', config.theme_position_enabled);
    setValue('automationThemeScore', config.theme_min_score);
    $$('.automation-buy-window').forEach(input => {
      input.checked = (config.buy_time_windows || []).includes(input.value);
    });
    setValue('automationOpenDelay', config.open_entry_delay_minutes);
    setChecked('automationVwapGate', config.require_vwap_confirmation);
    setValue('automationVwapDirectDeviation', config.max_vwap_deviation_pct);
    setValue('automationVwapRejectDeviation', config.vwap_pullback_deviation_pct);
    setValue('automationHighDistance', config.min_intraday_high_distance_pct);
    setValue('automationDailyGain', config.max_intraday_gain_pct);
    setChecked('automationOpenPriceGate', config.require_open_price_confirmation);
    setChecked('automationOpeningLowGate', config.block_on_opening_low_break);
    setValue('automationMaxPositions', config.max_positions);
    setValue('automationStockCap', config.max_position_pct);
    setValue('automationTotalCap', config.max_total_position_pct);
    setValue('automationInitialPosition', config.initial_position_pct);
    setValue('automationAddPosition', config.add_position_pct);
    setValue('automationMaxHoldDays', config.max_hold_days);
    setValue('automationStopLoss', config.stop_loss);
    setValue('automationTakeProfit', config.take_profit);
    setValue('automationTakeProfitSell', config.take_profit_sell_pct);
    setValue('automationSecondTakeProfit', config.second_take_profit);
    setValue('automationSecondTakeProfitSell', config.second_take_profit_sell_pct);
    setValue('automationTrailingDrawdown', config.trailing_profit_drawdown);
    setValue('automationLimitUpProfit', config.limit_up_profit);
    setValue('automationLimitUpSell', config.limit_up_sell_pct);
  }

  function numberFromAutomationForm(id, fallback = 0) {
    const value = Number($(`#${id}`)?.value);
    return Number.isFinite(value) ? value : fallback;
  }

  function readAutomationConfigForm(fallback = {}) {
    const checked = id => Boolean($(`#${id}`)?.checked);
    return {
      timing_strategy: 'five_minute',
      require_external_environment: checked('automationExternalGate'),
      theme_position_enabled: checked('automationThemeEnabled'),
      theme_min_score: numberFromAutomationForm('automationThemeScore', fallback.theme_min_score),
      buy_time_windows: $$('.automation-buy-window:checked').map(input => input.value),
      open_entry_delay_minutes: numberFromAutomationForm('automationOpenDelay', fallback.open_entry_delay_minutes),
      require_vwap_confirmation: checked('automationVwapGate'),
      max_vwap_deviation_pct: numberFromAutomationForm('automationVwapDirectDeviation', fallback.max_vwap_deviation_pct),
      vwap_pullback_deviation_pct: numberFromAutomationForm('automationVwapRejectDeviation', fallback.vwap_pullback_deviation_pct),
      min_intraday_high_distance_pct: numberFromAutomationForm('automationHighDistance', fallback.min_intraday_high_distance_pct),
      max_intraday_gain_pct: numberFromAutomationForm('automationDailyGain', fallback.max_intraday_gain_pct),
      require_open_price_confirmation: checked('automationOpenPriceGate'),
      block_on_opening_low_break: checked('automationOpeningLowGate'),
      max_positions: numberFromAutomationForm('automationMaxPositions', fallback.max_positions),
      max_position_pct: numberFromAutomationForm('automationStockCap', fallback.max_position_pct),
      max_total_position_pct: numberFromAutomationForm('automationTotalCap', fallback.max_total_position_pct),
      initial_position_pct: numberFromAutomationForm('automationInitialPosition', fallback.initial_position_pct),
      add_position_pct: numberFromAutomationForm('automationAddPosition', fallback.add_position_pct),
      max_hold_days: numberFromAutomationForm('automationMaxHoldDays', fallback.max_hold_days),
      stop_loss: numberFromAutomationForm('automationStopLoss', fallback.stop_loss),
      take_profit: numberFromAutomationForm('automationTakeProfit', fallback.take_profit),
      take_profit_sell_pct: numberFromAutomationForm('automationTakeProfitSell', fallback.take_profit_sell_pct),
      second_take_profit: numberFromAutomationForm('automationSecondTakeProfit', fallback.second_take_profit),
      second_take_profit_sell_pct: numberFromAutomationForm('automationSecondTakeProfitSell', fallback.second_take_profit_sell_pct),
      trailing_profit_drawdown: numberFromAutomationForm('automationTrailingDrawdown', fallback.trailing_profit_drawdown),
      limit_up_profit: numberFromAutomationForm('automationLimitUpProfit', fallback.limit_up_profit),
      limit_up_sell_pct: numberFromAutomationForm('automationLimitUpSell', fallback.limit_up_sell_pct)
    };
  }

  async function loadAutomationConfig() {
    const config = await api('/api/quantpilot/automation-config');
    state.automationConfig = config;
    setAutomationConfigForm(config);
    renderConditionSourceComparison(state.automationDashboard || {});
    return config;
  }

  async function saveAutomationConfig() {
    const config = readAutomationConfigForm(state.automationConfig || {});
    const saved = await api('/api/quantpilot/automation-config', {
      method: 'PUT', body: JSON.stringify(config)
    });
    state.automationConfig = saved;
    setAutomationConfigForm(saved);
    const status = $('#conditionStatusText');
    if (status) status.textContent = '已同步服务器 · 刚刚';
    await loadAutomationDashboard(state.automationDashboard?.strategy?.name || '');
    renderConditionSourceComparison(state.automationDashboard || {});
    toast('买入和卖出条件已保存并同步到服务器');
  }

  const state = {
    watchlist: [],
    groups: new Set(['核心关注', '短线策略', '待观察']),
    strategies: [],
    selectedStrategies: [],
    codeStrategies: [],
    codeStrategyResults: {},
    selectedCodeStrategies: [],
    editingCodeStrategy: null,
    activeCodeStrategy: null,
    lastCodeScreen: null,
    marketOverview: null,
    overviewRange: 'YTD',
    overviewCustomStart: '',
    overviewCustomEnd: '',
    automationDashboard: null,
    automationConfig: null
  };

  const replaceHandler = id => {
    const element = document.getElementById(id);
    if (!element) return null;
    const replacement = element.cloneNode(true);
    element.replaceWith(replacement);
    return replacement;
  };

  function setText(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  }

  function renderStrategyList() {
    const list = $('.strategy-list:not(#legPerformanceCards)');
    if (!list || !state.strategies.length) return;
    list.innerHTML = state.strategies.slice(0, 8).map((strategy, index) => {
      const name = strategy.display_name || strategy.name || strategy.strategy_name || `策略 ${index + 1}`;
      const strategyKey = strategy.name || strategy.strategy_name || name;
      const winRate = Number(strategy.win_rate || strategy.metadata?.win_rate || 0);
      return `<label class="strategy-row"><input class="strategy-check" data-strategy="${escapeHtml(name)}" type="checkbox" checked><div><div class="strategy-name">${escapeHtml(name)}</div><div class="strategy-meta"><span>${winRate ? `胜率 ${winRate.toFixed(1)}%` : '可运行策略'}</span><span>${strategy.trade_count ? `${strategy.trade_count} 笔` : '待回测'}</span></div><div class="progress"><span style="width:${Math.min(winRate || 0, 100)}%"></span></div></div><div class="strategy-return">${strategy.total_return == null ? '--' : formatPercent(strategy.total_return)}<small>${strategy.sharpe_ratio ? `夏普 ${Number(strategy.sharpe_ratio).toFixed(2)}` : '待回测'}</small></div><button type="button" class="icon-btn strategy-refresh" data-refresh-strategy="${escapeHtml(strategyKey)}" title="刷新${escapeHtml(name)}筛选结果" aria-label="刷新${escapeHtml(name)}筛选结果"><i data-lucide="refresh-cw"></i></button><button type="button" class="icon-btn strategy-edit" title="编辑策略"><i data-lucide="pencil"></i></button></label>`;
    }).join('');
    state.selectedStrategies = $$('.strategy-check:checked').map(node => node.dataset.strategy);
    setText('selectedStrategyCount', state.selectedStrategies.length);
    if (!list.dataset.quantPilotBound) {
      list.dataset.quantPilotBound = 'true';
      list.addEventListener('change', () => {
        state.selectedStrategies = $$('.strategy-check:checked').map(node => node.dataset.strategy);
        setText('selectedStrategyCount', state.selectedStrategies.length);
      });
    }
  }

  function renderSignals(data) {
    const signals = Array.isArray(data?.signals) ? data.signals : [];
    const list = $('.signal-list');
    if (!list) return;
    const grouped = new Map();
    signals.forEach(signal => {
      const name = signal.strategy_name || signal.signal_type || '策略信号';
      const entry = grouped.get(name) || { count: 0, strength: 0 };
      entry.count += 1;
      entry.strength = Math.max(entry.strength, Number(signal.confidence || signal.score || signal.signal_strength || 0));
      grouped.set(name, entry);
    });
    const entries = [...grouped.entries()].sort((a, b) => b[1].strength - a[1].strength).slice(0, 5);
    setText('signalMore', '查看全部信号');
    const total = $('.signal-total strong');
    if (total) total.textContent = signals.length;
    setText('overviewSignalSubtitle', `服务器信号 · ${String(data?.generated_at || '--').replace('T', ' ').slice(0, 19)} 更新`);
    setText('overviewSignalDate', data?.today || '--');
    list.innerHTML = entries.length ? entries.map(([name, entry], index) => `<div class="signal-item"><div class="signal-icon ${index === 1 ? 'orange' : index === 2 ? 'blue' : 'green'}"><i data-lucide="${index === 1 ? 'triangle-alert' : index === 2 ? 'git-compare-arrows' : 'zap'}"></i></div><div><div class="signal-name">${escapeHtml(name)}</div><div class="signal-desc">${entry.count} 个标的 · 后端实时信号</div></div><div class="signal-num up">${entry.strength ? `${entry.strength.toFixed(0)}%` : '--'}</div></div>`).join('') : '<div class="stock-pool-empty">当前没有待确认信号</div>';
    refreshIcons();
  }

  function setSignClass(id, value) {
    const el = document.getElementById(id);
    if (!el || !Number.isFinite(value)) return;
    el.className = value > 0 ? 'up' : (value < 0 ? 'down' : '');
  }

  function renderStats(data) {
    const cards = $$('.stat-card');
    const account = data?.account || {};
    const assets = Number(account.total_value || 0);
    const profit = Number(account.pnl_pct || 0);
    const signalCount = Number(data?.counts?.signals_today || 0);
    if (cards[0]) $('.stat-value', cards[0]).textContent = formatMoney(assets);
    if (cards[0]) $('.stat-footer', cards[0]).innerHTML = `<span>现金 ${formatMoney(account.cash || 0)}</span><span>持仓市值 ${formatMoney(account.positions_value || 0)}</span>`;
    if (cards[1]) {
      const cumulativeValue = $('.stat-value', cards[1]);
      cumulativeValue.textContent = formatPercent(profit);
      cumulativeValue.className = `stat-value${profit > 0 ? ' positive' : profit < 0 ? ' negative' : ''}`;
    }
    if (cards[1]) $('.stat-footer', cards[1]).innerHTML = `<span>累计盈亏 ${formatMoney(account.pnl || 0)}</span><span>行情 ${escapeHtml(data?.latest_market_date || '--')}</span>`;
    if (cards[2]) $('.stat-value', cards[2]).textContent = `${signalCount}`;
    if (cards[2]) $('.stat-footer', cards[2]).innerHTML = `<span>今日成交 ${Number(data?.counts?.trades_today || 0)} 笔</span><span>当前持仓 ${Number(data?.counts?.positions || 0)} 只</span>`;
    if (cards[3]) $('.stat-value', cards[3]).textContent = data?.mode || '--';
    if (cards[3]) $('.stat-footer', cards[3]).innerHTML = `<span>监控状态</span><span class="tag ${account.realtime ? 'green' : 'orange'}">${account.realtime ? '运行中' : '未启动'}</span><span>回撤 ${Number(account.drawdown_pct || 0).toFixed(2)}%</span>`;
    setText('overviewHeadingNote', `服务器已返回 ${signalCount} 条今日策略信号 · 数据时间 ${String(data?.generated_at || '--').replace('T', ' ').slice(0, 19)}`);
    setText('marketServerTime', `A股 · 服务器 ${String(data?.generated_at || '--').slice(11, 16) || '--'}`);
  }

  const cumulativeTradesState = { records: [], totalPnl: 0, sortKey: null, sortDir: 'desc' };

  function cumulativeSortValue(record, key) {
    if (key === 'cumulative_pnl') return Number(record.cumulative_pnl || 0);
    if (key === 'cumulative_pnl_pct') return Number(record.cumulative_pnl_pct || 0);
    const raw = key === 'first_buy_time' ? record.first_buy_time
      : key === 'last_sell_time' ? record.last_sell_time : null;
    return raw && raw !== '--' ? String(raw) : null;
  }

  function sortCumulativeRecords() {
    const { sortKey, sortDir } = cumulativeTradesState;
    if (!sortKey) return cumulativeTradesState.records;
    const dir = sortDir === 'asc' ? 1 : -1;
    return [...cumulativeTradesState.records].sort((a, b) => {
      const va = cumulativeSortValue(a, sortKey);
      const vb = cumulativeSortValue(b, sortKey);
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      if (va === vb) return 0;
      return (va > vb ? 1 : -1) * dir;
    });
  }

  function updateCumulativeSortHeaders() {
    const table = $('#cumulativeTradeTable');
    const ths = table?.closest('table')?.querySelectorAll('th[data-sort-key]');
    (ths || []).forEach(th => {
      const active = th.dataset.sortKey === cumulativeTradesState.sortKey;
      th.classList.toggle('sorted-desc', active && cumulativeTradesState.sortDir === 'desc');
      th.classList.toggle('sorted-asc', active && cumulativeTradesState.sortDir === 'asc');
    });
  }

  function renderCumulativeTrades(data) {
    const table = $('#cumulativeTradeTable');
    if (!table) return;
    cumulativeTradesState.records = Array.isArray(data?.records) ? data.records : [];
    cumulativeTradesState.totalPnl = Number(data?.total_pnl || 0);
    const records = sortCumulativeRecords();
    const totalPnl = cumulativeTradesState.totalPnl;
    setText('cumulativePageNote', records.length
      ? `共 ${records.length} 只 · 累计盈亏 ${formatMoney(totalPnl)}（含持仓浮动盈亏，按服务器最新行情估值）`
      : '服务器尚无成交记录');
    if (!records.length) {
      table.innerHTML = '<tr><td colspan="10" class="automation-empty">暂无累计成交记录</td></tr>';
      return;
    }
    table.innerHTML = records.map(record => {
      const pnl = Number(record.cumulative_pnl || 0);
      const pnlPct = Number(record.cumulative_pnl_pct || 0);
      const pnlClass = pnl > 0 ? 'up' : (pnl < 0 ? 'down' : '');
      const firstBuy = record.first_buy_time && record.first_buy_time !== '--'
        ? escapeHtml(record.first_buy_time) : '--';
      const lastBuy = record.last_buy_time && record.last_buy_time !== '--'
        && record.last_buy_time !== record.first_buy_time
        ? `<div class="cell-sub">最近 ${escapeHtml(record.last_buy_time)}</div>` : '';
      return `<tr><td>${escapeHtml(record.code || '--')}</td><td><div class="stock-name">${escapeHtml(record.name || '--')}</div></td><td>${firstBuy}${lastBuy}</td><td>${record.buy_price != null ? `¥ ${Number(record.buy_price).toFixed(3)}` : '--'}</td><td>${Number(record.held_quantity || 0).toLocaleString('zh-CN')}</td><td>${record.sell_price != null ? `¥ ${Number(record.sell_price).toFixed(3)}` : '--'}</td><td>${record.last_sell_time && record.last_sell_time !== '--' ? escapeHtml(record.last_sell_time) : '--'}</td><td>${Number(record.sell_quantity || 0) ? Number(record.sell_quantity).toLocaleString('zh-CN') : '--'}</td><td class="${pnlClass}">${pnl >= 0 ? '+' : ''}${formatMoney(pnl)}</td><td class="${pnlClass}"><strong>${formatPercent(pnlPct)}</strong></td></tr>`;
    }).join('');
    updateCumulativeSortHeaders();
  }

  async function openCumulativeTradesPage() {
    if (typeof window.quantPilotSetActivePage === 'function') {
      window.quantPilotSetActivePage('cumulative-trades');
    } else if (location.hash !== '#cumulative-trades') {
      location.hash = 'cumulative-trades';
    }
    const table = $('#cumulativeTradeTable');
    if (table) table.innerHTML = '<tr><td colspan="10" class="automation-empty">正在读取服务器数据</td></tr>';
    setText('cumulativePageNote', '正在读取服务器累计成交记录...');
    try {
      const data = await api('/api/quantpilot/cumulative-trades');
      renderCumulativeTrades(data);
    } catch (error) {
      setText('cumulativePageNote', `服务器数据读取失败：${error.message}`);
      if (table) table.innerHTML = `<tr><td colspan="10" class="automation-empty">${escapeHtml(error.message)}</td></tr>`;
    }
  }

  function parseOverviewDate(value) {
    const text = String(value || '').slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return null;
    const date = new Date(`${text}T00:00:00`);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatOverviewDate(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  function formatOverviewAxisDate(date) {
    return `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')}`;
  }

  function overviewPeriodBounds(range, today) {
    const end = parseOverviewDate(today) || new Date();
    if (range === 'ALL') return { start: null, end };
    if (range === 'MONTH') return { start: new Date(end.getFullYear(), end.getMonth(), 1), end };
    if (range === 'YTD') return { start: new Date(end.getFullYear(), 0, 1), end };
    if (range === 'CUSTOM') {
      const start = parseOverviewDate(state.overviewCustomStart);
      const customEnd = parseOverviewDate(state.overviewCustomEnd);
      return start && customEnd && start <= customEnd ? { start, end: customEnd } : null;
    }
    return { start: null, end };
  }

  function buildOverviewReturnSeries(data, range) {
    const dailySeries = Array.isArray(data?.daily_return_series)
      ? data.daily_return_series
      : [];
    const source = (dailySeries.length ? dailySeries : (Array.isArray(data?.nav_series) ? data.nav_series : []))
      .filter(item => parseOverviewDate(item?.trade_date))
      .map(item => ({
        ...item,
        valuation_status: item?.valuation_status || (Number(item?.total_value) > 0 ? 'known' : 'unknown')
      }))
      .sort((left, right) => String(left.trade_date).localeCompare(String(right.trade_date)));
    const period = overviewPeriodBounds(range, data?.today || source[source.length - 1]?.trade_date);
    if (!source.length || !period) return { rows: [], period, baseline: null, baselineValue: null };
    const visible = source.filter(item => {
      const date = parseOverviewDate(item.trade_date);
      return (!period.start || date >= period.start) && date <= period.end;
    });
    if (!visible.length) return { rows: [], period, baseline: null, baselineValue: null };

    const initialCapital = Number(data?.account?.initial_capital || 0);
    const baseline = source.filter(item => {
      const date = parseOverviewDate(item.trade_date);
      return item.valuation_status === 'known'
        && Number(item.total_value) > 0
        && (!period.start || date <= period.start);
    }).pop();
    const baselineValue = Number(baseline?.total_value || initialCapital);
    if (!Number.isFinite(baselineValue) || baselineValue <= 0) return { rows: [], period, baseline: null, baselineValue: null };
    const rows = visible.map(item => {
      const known = item.valuation_status === 'known' && Number(item.total_value) > 0;
      return {
        ...item,
        cumulativeReturnPct: known
          ? (Number(item.total_value) / baselineValue - 1) * 100
          : null
      };
    });
    return { rows, period, baseline, baselineValue };
  }

  function setRangeStat(id, value, cls = '', title = '') {
    const node = $(`#${id}`);
    if (!node) return;
    node.textContent = value;
    node.classList.toggle('up', cls === 'up');
    node.classList.toggle('down', cls === 'down');
    if (title) node.setAttribute('title', title);
    else node.removeAttribute('title');
  }

  function hideOverviewRangeStats() {
    const strip = $('#overviewRangeStats');
    if (strip) strip.hidden = true;
    ['rangeStatReturn', 'rangeStatPnl', 'rangeStatAnnual', 'rangeStatDrawdown']
      .forEach(id => setRangeStat(id, '--'));
  }

  function renderOverviewNav(data = state.marketOverview, range = state.overviewRange) {
    if (!data) return;
    state.overviewRange = range;
    $$('.chart-tab').forEach(tab => {
      tab.classList.toggle('active', tab.dataset.range === range);
    });
    const customRange = $('#overviewDateRange');
    const startInput = $('#overviewStartDate');
    const endInput = $('#overviewEndDate');
    if (range === 'CUSTOM') {
      const defaultBounds = overviewPeriodBounds('MONTH', data.today);
      if (startInput && !startInput.value) startInput.value = formatOverviewDate(defaultBounds.start);
      if (endInput && !endInput.value) endInput.value = formatOverviewDate(defaultBounds.end);
      state.overviewCustomStart = startInput?.value || state.overviewCustomStart;
      state.overviewCustomEnd = endInput?.value || state.overviewCustomEnd;
    }
    if (customRange) customRange.hidden = range !== 'CUSTOM';
    const { rows, period, baseline, baselineValue } = buildOverviewReturnSeries(data, range);
    const line = $('#overviewEquityPath');
    const points = $('#overviewEquityPoints');
    const tooltip = $('#overviewChartTooltip');
    const empty = $('#overviewNavEmpty');
    const axis = $$('#overviewChartAxis span');
    const yAxis = $$('#overviewChartYAxis span');
    if (!rows.length) {
      if (line) line.setAttribute('d', '');
      if (points) points.replaceChildren();
      if (tooltip) tooltip.hidden = true;
      if (empty) {
        empty.hidden = false;
        empty.textContent = range === 'CUSTOM' ? '所选日期范围内暂无净值快照' : '服务器暂无净值快照';
      }
      axis.forEach(node => { node.textContent = '--/--'; });
      yAxis.forEach(node => { node.textContent = '--'; });
      setText('rangeReturn', '--');
      setText('overviewNavSubtitle', `${data.mode || '模拟盘'} · 暂无可用收益数据`);
      hideOverviewRangeStats();
      return;
    }

    const hasCumulativeReturn = item => item.cumulativeReturnPct !== null
      && item.cumulativeReturnPct !== undefined
      && Number.isFinite(Number(item.cumulativeReturnPct));
    const knownRows = rows.filter(hasCumulativeReturn);
    if (!knownRows.length) {
      if (line) line.setAttribute('d', '');
      if (points) points.replaceChildren();
      if (tooltip) tooltip.hidden = true;
      if (empty) {
        empty.hidden = false;
        empty.textContent = '所选日期范围内没有已确认的账户估值';
      }
      axis.forEach(node => { node.textContent = '--/--'; });
      yAxis.forEach(node => { node.textContent = '--'; });
      setText('rangeReturn', '--');
      setText('overviewNavSubtitle', `${data.mode || '模拟盘'} · 暂无可确认收益数据`);
      hideOverviewRangeStats();
      return;
    }

    const values = knownRows.map(item => Number(item.cumulativeReturnPct));
    let minValue = Math.min(...values);
    let maxValue = Math.max(...values);
    if (minValue === maxValue) {
      const padding = Math.max(Math.abs(minValue) * 0.005, 0.005);
      minValue -= padding;
      maxValue += padding;
    } else {
      const padding = Math.max((maxValue - minValue) * 0.08, 0.02);
      minValue -= padding;
      maxValue += padding;
    }
    const periodStart = period?.start || parseOverviewDate(rows[0].trade_date);
    const periodEnd = period?.end || parseOverviewDate(rows[rows.length - 1].trade_date);
    // 横轴按交易日序号定位(与下方日期刻度同一坐标系): 曲线铺满整幅,
    // 悬停点/光标日期/静态刻度三者对齐; 假期空档不再把曲线挤向左侧。
    const xIndex = new Map(rows.map((row, index) => [String(row.trade_date), index]));
    const xAt = item => rows.length <= 1 ? 360 : Math.max(0, Math.min(720,
      ((xIndex.get(String(item.trade_date)) ?? 0) / (rows.length - 1)) * 720));
    const yAt = value => 20 + (maxValue - value) / (maxValue - minValue) * 140;
    const segmentPath = values => values.map((item, index) => `${index ? 'L' : 'M'}${xAt(item).toFixed(1)},${yAt(Number(item.cumulativeReturnPct)).toFixed(1)}`).join(' ');
    const path = segmentPath(knownRows);
    if (line) line.setAttribute('d', path);
    if (points) {
      points.replaceChildren();
      knownRows.forEach(item => {
        const point = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        point.setAttribute('cx', xAt(item).toFixed(1));
        point.setAttribute('cy', yAt(Number(item.cumulativeReturnPct)).toFixed(1));
        point.setAttribute('r', '3.5');
        point.setAttribute('fill', '#d94b4b');
        point.setAttribute('stroke', '#ffffff');
        point.setAttribute('stroke-width', '1.5');
        point.setAttribute('tabindex', '0');
        point.setAttribute('aria-label', `${item.trade_date} 当日收益 ${item.daily_return_pct == null ? '--' : formatPercent(item.daily_return_pct)}`);
        const showTooltip = () => {
          if (!tooltip) return;
          const chartWrap = tooltip.closest('.chart-wrap');
          if (!chartWrap) return;
          tooltip.textContent = `${item.trade_date} · 当日收益 ${item.daily_return_pct == null ? '--' : formatPercent(item.daily_return_pct)}`;
          tooltip.hidden = false;
          const wrapRect = chartWrap.getBoundingClientRect();
          const pointRect = point.getBoundingClientRect();
          const halfWidth = tooltip.offsetWidth / 2 + 6;
          const centerX = pointRect.left - wrapRect.left + pointRect.width / 2;
          tooltip.style.left = `${Math.max(halfWidth, Math.min(wrapRect.width - halfWidth, centerX))}px`;
          tooltip.style.top = `${pointRect.top - wrapRect.top - 7}px`;
          const cursorAxis = $('#overviewCursorAxis');
          if (cursorAxis) {
            cursorAxis.textContent = formatOverviewAxisDate(parseOverviewDate(item.trade_date));
            cursorAxis.hidden = false;
            const halfAxisWidth = cursorAxis.offsetWidth / 2 + 6;
            cursorAxis.style.left = `${Math.max(halfAxisWidth, Math.min(wrapRect.width - halfAxisWidth, centerX))}px`;
          }
        };
        const hideTooltip = () => {
          if (tooltip) tooltip.hidden = true;
          const cursorAxis = $('#overviewCursorAxis');
          if (cursorAxis) cursorAxis.hidden = true;
        };
        point.addEventListener('mouseenter', showTooltip);
        point.addEventListener('mouseleave', hideTooltip);
        point.addEventListener('focus', showTooltip);
        point.addEventListener('blur', hideTooltip);
        points.appendChild(point);
      });
    }
    if (empty) empty.hidden = true;

    yAxis.forEach((node, index) => {
      const value = maxValue - (maxValue - minValue) * index / Math.max(1, yAxis.length - 1);
      node.textContent = formatPercent(value);
    });
    axis.forEach((node, index) => {
      const ratio = index / Math.max(1, axis.length - 1);
      const rowIndex = Math.round((rows.length - 1) * ratio);
      node.textContent = formatOverviewAxisDate(parseOverviewDate(rows[rowIndex].trade_date));
    });
    const last = Number(knownRows[knownRows.length - 1].cumulativeReturnPct);
    setText('rangeReturn', formatPercent(last));
    setText('overviewNavSubtitle', `${data.mode || '模拟盘'}区间累计收益 · ${formatOverviewDate(periodStart)} 至 ${formatOverviewDate(periodEnd)} · 已确认 ${knownRows.length}/${rows.length} 个交易日`);

    // 查询区间统计: 以区间前最后一个已确认净值为基准, 汇总区间收益四指标
    const strip = $('#overviewRangeStats');
    if (strip) strip.hidden = false;
    const lastKnownRow = knownRows[knownRows.length - 1];
    const lastValue = Number(lastKnownRow.total_value);
    const rangePnl = lastValue - baselineValue;
    setRangeStat('rangeStatReturn', formatPercent(last),
      last > 0 ? 'up' : last < 0 ? 'down' : '');
    setRangeStat('rangeStatPnl',
      `${rangePnl >= 0 ? '+' : '-'}¥ ${Math.abs(rangePnl).toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`,
      rangePnl > 0 ? 'up' : rangePnl < 0 ? 'down' : '');
    const baselineDate = baseline?.trade_date
      ? parseOverviewDate(baseline.trade_date)
      : parseOverviewDate(knownRows[0].trade_date);
    const lastDate = parseOverviewDate(lastKnownRow.trade_date);
    const elapsedDays = baselineDate && lastDate
      ? Math.round((lastDate - baselineDate) / 86400000)
      : 0;
    const annualPct = baselineValue > 0 && lastValue > 0 && elapsedDays >= 7
      ? ((lastValue / baselineValue) ** (365 / elapsedDays) - 1) * 100
      : null;
    setRangeStat('rangeStatAnnual',
      annualPct == null || !Number.isFinite(annualPct) ? '--' : formatPercent(annualPct),
      annualPct == null || !Number.isFinite(annualPct) ? '' : annualPct > 0 ? 'up' : annualPct < 0 ? 'down' : '',
      annualPct == null ? '区间跨度不足7天, 不做年化展示' : '');
    let peakValue = baselineValue > 0 ? baselineValue : -Infinity;
    let maxDrawdownPct = 0;
    knownRows.forEach(row => {
      const value = Number(row.total_value);
      if (!Number.isFinite(value) || value <= 0) return;
      if (value > peakValue) peakValue = value;
      const drawdown = (value / peakValue - 1) * 100;
      if (drawdown < maxDrawdownPct) maxDrawdownPct = drawdown;
    });
    setRangeStat('rangeStatDrawdown',
      maxDrawdownPct === 0 ? '0.00%' : formatPercent(maxDrawdownPct),
      maxDrawdownPct < 0 ? 'down' : '');
  }

  function updateResultSelection(root = $('#filterResults')) {
    if (!root) return;
    const selected = $$('.result-select:checked', root).length;
    setText('selectedResultCount', `已选 ${selected} 条`);
    const batchButton = $('#batchAddStock');
    if (batchButton) batchButton.disabled = selected === 0;
  }

  function bindResultSelection(root) {
    if (!root || root.dataset.quantPilotSelectionBound) return;
    root.dataset.quantPilotSelectionBound = 'true';
    root.addEventListener('change', event => {
      if (event.target.closest('.result-select')) updateResultSelection(root);
    });
    root.addEventListener('click', event => {
      const selectAll = event.target.closest('#selectAllResults');
      if (selectAll) {
        const rows = $$('.filter-result-row', root).filter(row => !row.hidden);
        const shouldSelect = rows.some(row => !$('.result-select', row)?.checked);
        rows.forEach(row => {
          const checkbox = $('.result-select', row);
          if (checkbox) checkbox.checked = shouldSelect;
        });
        updateResultSelection(root);
        return;
      }
      const batchAdd = event.target.closest('#batchAddStock');
      if (batchAdd && !batchAdd.disabled) {
        const group = $('#batchGroup')?.value || '待观察';
        const rows = $$('.filter-result-row', root).filter(row => $('.result-select', row)?.checked);
        batchAdd.disabled = true;
        batchAdd.innerHTML = '<i data-lucide="loader-circle"></i>加入中';
        refreshIcons();
        Promise.all(rows.map(row => api('/api/quantpilot/watchlist', {
          method: 'POST',
          body: JSON.stringify(resultToWatchlistItem(row, group))
        }))).then(async () => {
          rows.forEach(row => { const checkbox = $('.result-select', row); if (checkbox) checkbox.checked = false; });
          await loadWatchlist();
          toast(`已将 ${rows.length} 只股票加入「${group}」`);
        }).catch(error => {
          toast(`批量加入失败：${error.message}`);
        }).finally(() => {
          batchAdd.disabled = false;
          batchAdd.innerHTML = '<i data-lucide="star"></i>批量加入自选';
          updateResultSelection(root);
          refreshIcons();
        });
      }
    });
    updateResultSelection(root);
  }

  function groupOptions(selected) {
    return [...state.groups].map(group => `<option value="${escapeHtml(group)}" ${group === selected ? 'selected' : ''}>${escapeHtml(group)}</option>`).join('');
  }

  function renderWatchlist() {
    const table = $('#stockTable');
    if (!table) return;
    const rows = state.watchlist.map(item => {
      const change = Number(item.change || 0);
      const strength = Number(item.signal_strength || 0);
      const favorite = item.favorite !== false;
      return `<tr data-code="${escapeHtml(item.code)}" data-favorite="${favorite}" data-group="${escapeHtml(item.group || '待观察')}" data-strategy="${escapeHtml(item.strategy || '')}" data-search="${escapeHtml(`${item.name || ''} ${item.code || ''} ${item.industry || ''}`)}"><td><div class="stock-cell"><button class="stock-star ${favorite ? 'active' : ''}" title="${favorite ? '取消自选' : '加入自选'}"><i data-lucide="star"></i></button><div><div class="stock-name">${escapeHtml(item.name || item.code)}</div><div class="stock-code">${escapeHtml(item.code)} · ${escapeHtml(item.industry || '未分类')}</div><span class="stock-group">${escapeHtml(item.group || '待观察')}</span></div></div></td><td class="price">${Number(item.price || 0).toFixed(2)}</td><td class="${change < 0 ? 'down' : 'up'}">${formatPercent(change)}</td><td><span class="tag ${String(item.strategy || '').includes('回踩') ? 'orange' : 'green'}">${escapeHtml(item.strategy || '观察中')}</span></td><td><span class="${strength >= 70 ? 'up' : ''}">${strength ? `${strength >= 85 ? '强' : '中'} · ${strength}` : '--'}</span></td><td>${item.win_rate ? `${Number(item.win_rate).toFixed(1)}%` : '--'}</td><td class="up">${item.expected_return == null ? '--' : formatPercent(item.expected_return)}</td><td><select class="row-group" aria-label="${escapeHtml(item.name || item.code)}分组">${groupOptions(item.group || '待观察')}</select></td><td><button class="icon-btn row-detail" title="查看详情"><i data-lucide="arrow-up-right"></i></button></td></tr>`;
    }).join('');
    table.innerHTML = rows || '<tr data-search=""><td colspan="10" class="stock-pool-empty">暂无自选股，请从筛选结果加入</td></tr>';
    setText('watchCount', state.watchlist.filter(item => item.favorite !== false).length);
    setText('tableResult', `显示 ${state.watchlist.length} / ${state.watchlist.length} 只股票 · 数据来自后端`);
    refreshIcons();
    bindWatchlistEvents();
  }

  async function loadWatchlist() {
    const data = await api('/api/quantpilot/watchlist');
    state.watchlist = data.items || [];
    (data.groups || []).forEach(group => state.groups.add(group));
    renderWatchlist();
  }

  function getCodeStrategyStocks(strategy) {
    return Array.isArray(state.codeStrategyResults[strategy.name])
      ? state.codeStrategyResults[strategy.name]
      : [];
  }

  function formatStrategyTabName(name) {
    const characters = Array.from(String(name || ''));
    return characters.length > 10 ? `${characters.slice(0, 10).join('')}...` : characters.join('');
  }

  function getCodeTimeRange() {
    const mode = $('#codeTimeRange')?.value || 'all';
    if (mode === 'all') return { mode: 'all' };
    if (mode === 'custom') {
      const startDate = $('#codeStartDate')?.value || '';
      const endDate = $('#codeEndDate')?.value || '';
      if (!startDate && !endDate) throw new Error('开始日期和结束日期至少填写一个');
      if (startDate && endDate && startDate > endDate) throw new Error('开始日期不能晚于结束日期');
      return { mode: 'custom', start_date: startDate, end_date: endDate };
    }
    return { mode, days: Number(mode) };
  }

  function setCodeTimeRange(timeRange) {
    const select = $('#codeTimeRange');
    if (!select) return;
    const mode = String(timeRange?.mode || 'all');
    select.value = [...select.options].some(option => option.value === mode) ? mode : 'all';
    const custom = $('#codeTimeCustom');
    if (custom) custom.classList.toggle('visible', select.value === 'custom');
    if (select.value === 'custom') {
      if ($('#codeStartDate')) $('#codeStartDate').value = timeRange?.start_date || '';
      if ($('#codeEndDate')) $('#codeEndDate').value = timeRange?.end_date || '';
    }
  }

  function bindCodeTimeRange() {
    const select = $('#codeTimeRange');
    if (!select || select.dataset.quantPilotBound) return;
    select.dataset.quantPilotBound = 'true';
    select.addEventListener('change', () => {
      const custom = $('#codeTimeCustom');
      if (custom) custom.classList.toggle('visible', select.value === 'custom');
    });
    setCodeTimeRange({ mode: 'all' });
  }

  function renderCodeStrategyPools(activeName = state.activeCodeStrategy?.name) {
    const mount = $('#codeStrategyPoolMount');
    if (!mount) return;
    const strategies = state.codeStrategies;
    if (!strategies.length) {
      mount.innerHTML = '<div class="strategy-pool-zone"><div class="strategy-pool-heading"><div><div class="panel-title"><i data-lucide="layers-3"></i>代码策略与股票池</div><div class="panel-subtitle">保存多个策略后，可点击策略查看各自命中的股票</div></div><span class="tag blue">暂无已保存策略</span></div><div class="stock-pool-empty">暂无代码策略，请点击“新建策略”保存第一条策略。</div></div>';
      refreshIcons();
      return;
    }

    const selected = strategies.find(item => item.name === activeName) || strategies[0];
    state.activeCodeStrategy = selected;
    const strategyNames = new Set(strategies.map(item => item.name));
    state.selectedCodeStrategies = state.selectedCodeStrategies.filter(name => strategyNames.has(name));
    const stocks = getCodeStrategyStocks(selected);
    const strategyItems = strategies.map(strategy => {
      const count = getCodeStrategyStocks(strategy).length;
      const active = strategy.name === selected.name ? ' active' : '';
      const displayName = formatStrategyTabName(strategy.name);
      const checked = state.selectedCodeStrategies.includes(strategy.name) ? ' checked' : '';
      return `<div class="strategy-pool-item${active}" data-strategy-name="${escapeHtml(strategy.name)}" role="tab" aria-selected="${strategy.name === selected.name}" tabindex="0" title="${escapeHtml(strategy.name)}"><input class="strategy-pool-select" type="checkbox" data-code-strategy-select="${escapeHtml(strategy.name)}"${checked} aria-label="选择删除${escapeHtml(strategy.name)}"><span class="strategy-pool-label"><span class="strategy-pool-name">${escapeHtml(displayName)}</span><span class="strategy-pool-meta">代码策略</span></span><span class="strategy-pool-count">${count}<small>只股票</small></span><button type="button" class="icon-btn strategy-pool-edit" data-code-strategy-edit="${escapeHtml(strategy.name)}" title="编辑${escapeHtml(strategy.name)}" aria-label="编辑${escapeHtml(strategy.name)}"><i data-lucide="pencil"></i></button><button type="button" class="icon-btn strategy-pool-refresh" data-code-strategy-refresh="${escapeHtml(strategy.name)}" title="刷新${escapeHtml(strategy.name)}筛选结果" aria-label="刷新${escapeHtml(strategy.name)}筛选结果"><i data-lucide="refresh-cw"></i></button><span class="strategy-pool-updated">更新时间 · ${escapeHtml(strategy.updated_at || '已保存')}</span></div>`;
    }).join('');
    const stockCards = stocks.map(stock => {
      const change = parsePercent(stock.change);
      const signal = Array.isArray(stock.signals) ? stock.signals[0] : (stock.signals || {});
      const price = stock.price ?? stock.close ?? signal.close ?? 0;
      return `<div class="stock-pool-card"><div class="stock-pool-identity"><span class="stock-name">${escapeHtml(stock.name || stock.code)}</span><span class="stock-code">${escapeHtml(stock.code)} · ${escapeHtml(stock.industry || '未分类')}</span><span class="stock-pool-themes" data-theme-code="${escapeHtml(stock.code)}"><span class="theme-empty">题材加载中</span></span></div><span class="stock-pool-price">${Number(price || 0).toFixed(2)}</span><span class="stock-pool-change ${dayChangeClass(change)}">${formatDayChange(change)}</span></div>`;
    }).join('');
    const selectedCount = state.selectedCodeStrategies.length;
    const updatedAt = selected.updated_at ? ` · ${escapeHtml(selected.updated_at)}` : '';
    mount.innerHTML = `<div class="strategy-pool-zone"><div class="strategy-pool-heading"><div><div class="panel-title"><i data-lucide="layers-3"></i>代码策略与股票池</div><div class="panel-subtitle">点击策略只显示对应股票，可勾选后批量删除</div></div><div class="strategy-pool-actions"><label class="strategy-pool-select-all"><input id="selectAllCodeStrategies" type="checkbox"${selectedCount === strategies.length ? ' checked' : ''} aria-label="全选代码策略">全选</label><button type="button" class="ghost-btn strategy-pool-delete" id="deleteSelectedCodeStrategies"${selectedCount ? '' : ' disabled'}><i data-lucide="trash-2"></i>删除已选</button><span class="tag blue">共 ${strategies.length} 个策略</span></div></div><div class="strategy-pool-layout"><div class="strategy-pool-list">${strategyItems}</div><div class="stock-pool-preview"><div class="stock-pool-header"><strong>${escapeHtml(selected.name)} · 股票池</strong><span>${stocks.length} 只 · ${stocks.length ? `最近一次保存结果${updatedAt}` : '尚未执行该策略'}</span></div><div class="stock-pool-grid">${stockCards || '<div class="stock-pool-empty">该策略暂无执行结果</div>'}</div></div></div></div>`;
    refreshIcons();
    void hydrateStockThemes(mount);
  }

  async function deleteSelectedCodeStrategies() {
    const names = [...state.selectedCodeStrategies];
    if (!names.length) return toast('请先勾选要删除的策略');
    if (!window.confirm(`确定删除选中的 ${names.length} 个策略吗？`)) return;
    const button = $('#deleteSelectedCodeStrategies');
    if (button) {
      button.disabled = true;
      button.classList.add('loading');
    }
    try {
      await api('/api/quantpilot/code-strategies', {
        method: 'DELETE',
        body: JSON.stringify({ names })
      });
      state.codeStrategies = state.codeStrategies.filter(strategy => !names.includes(strategy.name));
      names.forEach(name => delete state.codeStrategyResults[name]);
      state.selectedCodeStrategies = [];
      state.activeCodeStrategy = state.codeStrategies[0] || null;
      renderCodeStrategyPools();
      if (state.activeCodeStrategy) loadCodeStrategyIntoEditor(state.activeCodeStrategy);
      toast(`已删除 ${names.length} 个策略`);
    } catch (error) {
      toast(`批量删除失败：${error.message}`);
    } finally {
      if (button) button.classList.remove('loading');
    }
  }

  function resetCodeStrategyModal() {
    state.editingCodeStrategy = null;
    const modal = $('#modalBackdrop .modal');
    const nameInput = $('#strategyName');
    modal?.classList.remove('strategy-code-modal');
    nameInput?.removeAttribute('readonly');
  }

  function openCodeStrategyEditor(strategy) {
    if (!strategy) return;
    state.editingCodeStrategy = strategy;
    state.activeCodeStrategy = strategy;
    const modal = $('#modalBackdrop .modal');
    const nameInput = $('#strategyName');
    const sourceInput = $('#strategyCode');
    if (!modal || !nameInput || !sourceInput) return toast('代码编辑器尚未准备好，请稍后重试');
    modal.classList.add('strategy-code-modal');
    $('#modalTitle').textContent = '编辑代码策略';
    $('#modalSubtitle').textContent = '修改策略代码，点击保存后将重新执行并更新股票池';
    nameInput.value = strategy.name || '';
    nameInput.removeAttribute('readonly');
    sourceInput.value = strategy.source || '';
    setCodeTimeRange(strategy.time_range);
    modal.closest('.modal-backdrop')?.classList.add('open');
    setTimeout(() => sourceInput.focus(), 50);
  }

  function loadCodeStrategyIntoEditor(strategy) {
    const editor = $('#codeFilter');
    if (editor) editor.value = strategy.source || '';
    const backtestSelect = $('#backtestStrategySelect');
    if (backtestSelect) backtestSelect.value = strategy.name || '';
    setCodeTimeRange(strategy.time_range);
    state.activeCodeStrategy = strategy;
    renderCodeStrategyPools(strategy.name);
  }

  async function loadCodeStrategies() {
    const data = await api('/api/quantpilot/code-strategies');
    const hadActiveStrategy = Boolean(state.activeCodeStrategy);
    state.codeStrategies = Array.isArray(data?.strategies) ? data.strategies : [];
    const backtestSelect = $('#backtestStrategySelect');
    if (backtestSelect) {
      const activeName = state.activeCodeStrategy?.name || state.codeStrategies[0]?.name || '';
      backtestSelect.innerHTML = '<option value="">选择已保存策略</option>' + state.codeStrategies
        .map(strategy => `<option value="${escapeHtml(strategy.name)}">${escapeHtml(formatStrategyTabName(strategy.name))}</option>`)
        .join('');
      backtestSelect.value = activeName;
      if (!backtestSelect.dataset.quantPilotBound) {
        backtestSelect.dataset.quantPilotBound = 'true';
        backtestSelect.addEventListener('change', event => {
          const selected = state.codeStrategies.find(strategy => strategy.name === event.target.value);
          if (selected) state.activeCodeStrategy = selected;
        });
      }
    }
    state.codeStrategies.forEach(strategy => {
      if (Array.isArray(strategy.results)) {
        state.codeStrategyResults[strategy.name] = strategy.results;
      }
    });
    renderCodeStrategyPools();
    if (!hadActiveStrategy && state.codeStrategies.length) {
      loadCodeStrategyIntoEditor(state.codeStrategies[0]);
    }
  }

  async function saveCodeStrategy() {
    const name = $('#strategyName')?.value.trim();
    const source = $('#strategyCode')?.value.trim() || $('#codeFilter')?.value.trim();
    if (!name) return toast('请先填写策略名称');
    if (!source) return toast('请先填写策略代码');
      const savedResults = state.lastCodeScreen?.source === source
      ? state.lastCodeScreen.results
      : getCodeStrategyStocks(state.activeCodeStrategy || { name: '' });
    let timeRange;
    try {
      timeRange = getCodeTimeRange();
    } catch (error) {
      return toast(error.message);
    }
    if (state.editingCodeStrategy) {
      const editingName = state.editingCodeStrategy.name;
      const button = $('#saveStrategy');
      if (button) {
        button.disabled = true;
        button.innerHTML = '<i data-lucide="loader-circle"></i>保存并执行中';
        refreshIcons();
      }
      try {
        const result = await api('/api/quantpilot/code-screen', {
          method: 'POST',
          body: JSON.stringify({
            source,
            strategy_name: name,
            previous_name: editingName,
            max_stocks: state.editingCodeStrategy.max_stocks || undefined,
            force_refresh: true,
            time_range: timeRange,
          })
        });
        const current = state.codeStrategies.find(item => item.name === editingName);
        delete state.codeStrategyResults[editingName];
        state.codeStrategyResults[name] = result.results || [];
        if (current) {
          current.name = name;
          current.source = source;
          current.results = result.results || [];
          current.time_range = timeRange;
          current.updated_at = new Date().toISOString().replace('T', ' ').slice(0, 19);
          state.activeCodeStrategy = current;
        }
        const backtestSelect = $('#backtestStrategySelect');
        const renamedOption = backtestSelect
          ? [...backtestSelect.options].find(option => option.value === editingName)
          : null;
        if (renamedOption) {
          renamedOption.value = name;
          renamedOption.textContent = formatStrategyTabName(name);
          backtestSelect.value = name;
        }
        state.lastCodeScreen = { source, results: result.results || [], strategyName: name, timeRange };
        renderCodeStrategyPools(name);
        $('#modalBackdrop')?.classList.remove('open');
        resetCodeStrategyModal();
        const incomplete = result.errors?.length
          ? `；${result.errors.length} 只未纳入：${result.errors[0]?.error || '历史数据不足'}`
          : '';
        const importing = result.import_stats?.import_running
          ? `；${result.import_stats.deferred_codes || 0} 只历史K线正在后台补充`
          : '';
        toast(`策略「${name}」已保存并重新执行，命中 ${result.count || 0} 只股票${incomplete}${importing}`);
      } catch (error) {
        toast(`策略保存并执行失败：${error.message}`);
      } finally {
        if (button) {
          button.disabled = false;
          button.innerHTML = '<i data-lucide="check"></i>保存';
          refreshIcons();
        }
      }
      return;
    }
    try {
      const record = await api('/api/quantpilot/code-strategies', {
        method: 'POST',
        body: JSON.stringify({ name, source, results: savedResults, time_range: timeRange })
      });
      state.codeStrategies = [
        ...state.codeStrategies.filter(item => item.name !== record.name),
        record,
      ];
      state.activeCodeStrategy = record;
      state.codeStrategyResults[name] = record.results || savedResults || [];
      loadCodeStrategyIntoEditor(record);
      $('#modalBackdrop')?.classList.remove('open');
      resetCodeStrategyModal();
      toast(`策略「${name}」已保存`);
    } catch (error) {
      toast(`策略保存失败：${error.message}`);
    }
  }

  async function saveWatch(item) {
    await api('/api/quantpilot/watchlist', { method: 'POST', body: JSON.stringify(item) });
    await loadWatchlist();
  }

  const parseResultValue = value => value === '' ? null : Number(String(value).replace('%', '').replace('+', ''));
  const resultToWatchlistItem = (row, group) => ({
    code: row.dataset.code,
    name: row.dataset.name,
    industry: row.dataset.sector,
    group,
    strategy: row.dataset.signal,
    signal_strength: parseResultValue(row.dataset.strength),
    win_rate: parseResultValue(row.dataset.win),
    expected_return: parseResultValue(row.dataset.return)
  });

  function renderScanResults(data) {
    const results = [];
    Object.entries(data || {}).forEach(([strategy, stocks]) => {
      if (!Array.isArray(stocks)) return;
      stocks.forEach(stock => {
      const signal = (Array.isArray(stock.signals) ? stock.signals[0] : stock.signals) || {};
        results.push({
          code: stock.code,
          name: stock.name || stock.code,
          industry: stock.industry || '未分类',
          price: stock.price ?? stock.close ?? signal.close ?? 0,
          change: stock.change ?? signal.change ?? 0,
          strategy,
          strength: stock.score ?? stock.signal_strength ?? signal.score ?? 0,
          win: stock.win_rate ?? signal.win_rate,
          expected: stock.expected_return ?? signal.expected_return
        });
      });
    });
    const unique = [...new Map(results.map(item => [item.code, item])).values()].slice(0, 20);
    const container = $('#filterResults');
    if (!container) return;
    const heading = $('.result-heading', container);
    if (heading) heading.querySelector('strong').innerHTML = `筛选结果预览 <span class="tag blue">${unique.length} 条</span>`;
    const list = $('.result-list', container);
    if (!list) return;
    list.innerHTML = unique.length ? unique.map(item => {
      return `<div class="filter-result-row" data-name="${escapeHtml(item.name)}" data-code="${escapeHtml(item.code)}" data-sector="${escapeHtml(item.industry)}" data-search="${escapeHtml(`${item.name} ${item.code} ${item.industry}`)}" data-price="${Number(item.price || 0).toFixed(2)}" data-change="${parsePercent(item.change).toFixed(2)}" data-signal="${escapeHtml(item.strategy || '代码筛选')}" data-strength="${Number(item.strength || 0)}" data-win="${item.win == null ? '' : Number(item.win).toFixed(1)}" data-return="${item.expected == null ? '' : Number(item.expected).toFixed(2)}"><input class="result-select" type="checkbox" aria-label="选择${escapeHtml(item.name)}"><div class="result-stock"><span class="stock-name">${escapeHtml(item.name)}</span><span class="stock-code">${escapeHtml(item.code)} · ${escapeHtml(item.industry)}</span><span class="result-themes" data-theme-code="${escapeHtml(item.code)}"><span class="theme-empty">题材加载中</span></span></div><span class="result-price">价格 <strong>${Number(item.price || 0).toFixed(2)}</strong></span><span class="result-change ${dayChangeClass(item.change)}">涨跌 <strong>${formatDayChange(item.change)}</strong></span><span class="result-signal">信号 <strong>${Number(item.strength || 0).toFixed(0)}</strong></span></div>`;
    }).join('') : '<div class="stock-pool-empty">没有符合条件的股票</div>';
    updateResultSelection(container);
    refreshIcons();
    void hydrateStockThemes(container);
  }

  async function bindFavorite(row) {
    const item = state.watchlist.find(entry => entry.code === row.dataset.code);
    if (!item) return;
    await api(`/api/quantpilot/watchlist/${item.code}`, { method: 'DELETE' });
    await loadWatchlist();
    toast(`${item.name || item.code} 已移出自选股`);
  }

  function bindWatchlistEvents() {
    const table = $('#stockTable');
    if (!table || table.dataset.quantPilotBound) return;
    table.dataset.quantPilotBound = 'true';
    table.addEventListener('change', async event => {
      const select = event.target.closest('.row-group');
      if (!select) return;
      const row = select.closest('tr');
      const item = state.watchlist.find(entry => entry.code === row.dataset.code);
      if (!item) return;
      try { await saveWatch({ ...item, group: select.value }); toast(`已将 ${item.name} 移入「${select.value}」`); } catch (error) { toast(error.message); }
    });
    table.addEventListener('click', async event => {
      const star = event.target.closest('.stock-star');
      if (star) {
        try { await bindFavorite(star.closest('tr')); } catch (error) { toast(error.message); }
        return;
      }
      const detail = event.target.closest('.row-detail, .stock-name');
      if (detail && typeof window.quantPilotOpenStockDetail === 'function') {
        window.quantPilotOpenStockDetail(detail.closest('tr'));
      }
    });
  }

  function renderBacktestTrades(trades) {
    const table = $('#backtestTable');
    if (!table) return;
    if (!Array.isArray(trades) || !trades.length) {
      table.innerHTML = '<tr><td colspan="10" class="stock-pool-empty">该策略在所选区间没有生成已完成交易</td></tr>';
      return;
    }

    const headerLabels = ['股票', '策略信号', '开始建仓', '建仓均价', '总买入数量', '清仓时间', '当前/清仓价', '持仓天数', '全部收益率', '结果'];
    const header = table.closest('.table-wrap')?.querySelectorAll('thead th');
    header?.forEach((cell, index) => { if (headerLabels[index]) cell.textContent = headerLabels[index]; });
    const description = table.closest('.table-wrap')?.previousElementSibling?.querySelector('div > span');
    if (description) description.textContent = '按单只股票汇总；点击股票行查看建仓、加仓、止盈和清仓操作时间';

    const summaries = buildBacktestTradeSummaries(trades);
    table.innerHTML = summaries.map(summary => `<tr data-summary-code="${escapeHtml(summary.code)}" class="backtest-trade-row"><td><div class="stock-name">${escapeHtml(summary.name)}</div><div class="stock-code">${escapeHtml(summary.code)}</div></td><td><span class="tag blue">${escapeHtml(summary.strategy)}</span></td><td>${escapeHtml(formatTradeDateTime(summary.startTime))}</td><td>¥ ${summary.avgBuyPrice.toFixed(2)}</td><td>${summary.quantity ? summary.quantity.toLocaleString('zh-CN') : '--'}</td><td>${escapeHtml(summary.clearTime === '--' ? '持仓中' : formatTradeDateTime(summary.clearTime))}</td><td>${summary.currentOrSellPrice ? `¥ ${summary.currentOrSellPrice.toFixed(2)}` : '--'}</td><td>${escapeHtml(summary.holdDays)}</td><td class="${summary.totalReturn >= 0 ? 'up' : 'down'}">${formatPercent(summary.totalReturn)}</td><td><span class="tag ${summary.status === '持仓中' ? 'blue' : (summary.totalReturn >= 0 ? 'red' : 'gray')}">${escapeHtml(summary.status)}</span></td></tr>`).join('');
    table.onclick = event => {
      const row = event.target.closest('.backtest-trade-row[data-summary-code]');
      if (!row) return;
      const summary = summaries.find(item => item.code === row.dataset.summaryCode);
      if (summary) openBacktestTradeSummary(summary);
    };
    return;

    // 后端分别保存买入和卖出记录。已平仓的买入记录不能继续显示为“持仓中”，
    // 只保留最近一次卖出之后仍未平仓的买入/加仓记录。
    const latestSellByCode = new Map();
    const settledBuyKeys = new Set();
    trades.forEach(trade => {
      if (!trade.sell_date) return;
      const code = String(trade.stock_code || '');
      const buyDate = String(trade.buy_date || '').slice(0, 10);
      settledBuyKeys.add(`${code}|${buyDate}`);
      const current = latestSellByCode.get(code);
      const candidate = { date: String(trade.sell_date), id: Number(trade.id || 0) };
      if (!current || candidate.date > current.date || (candidate.date === current.date && candidate.id > current.id)) {
        latestSellByCode.set(code, candidate);
      }
    });
    const visibleTrades = trades.filter(trade => {
      if (trade.sell_date) return true;
      const buyKey = `${String(trade.stock_code || '')}|${String(trade.buy_date || '').slice(0, 10)}`;
      if (settledBuyKeys.has(buyKey)) return false;
      const latestSell = latestSellByCode.get(String(trade.stock_code || ''));
      if (!latestSell) return true;
      const buyDate = String(trade.buy_date || '');
      const buyId = Number(trade.id || 0);
      return buyDate > latestSell.date || (buyDate === latestSell.date && buyId > latestSell.id);
    }).sort((left, right) => {
      const buyDate = String(left.buy_date || left.selection_date || '');
      const otherBuyDate = String(right.buy_date || right.selection_date || '');
      return buyDate.localeCompare(otherBuyDate) || String(left.stock_code || '').localeCompare(String(right.stock_code || ''));
    });

    const codes = new Set();
    table.innerHTML = visibleTrades.map(trade => {
      const code = trade.stock_code || '';
      const name = trade.stock_name || code || '未知股票';
      const isOpen = !trade.sell_date;
      const returnRateValue = isOpen
        ? (trade.current_return_rate ?? trade.return_rate ?? trade.profit_rate)
        : (trade.return_rate ?? trade.profit_rate);
      const returnRate = Number(returnRateValue ?? 0);
      const holdDays = trade.hold_days ?? trade.holding_days ?? '--';
      const buyPrice = Number(trade.buy_price || 0);
      const quantity = Number(trade.quantity || 0);
      const sellPrice = Number(trade.sell_price || 0);
      const currentPrice = Number(trade.current_price || 0);
      const buyDate = formatTradeDateTime(trade.buy_date);
      const sellDate = isOpen ? '持仓中' : formatTradeDateTime(trade.sell_date);
      const rawResult = trade.sell_type || trade.trade_type;
      const result = isOpen ? '持仓中' : (formatBacktestSellType(rawResult) !== '--' ? formatBacktestSellType(rawResult) : (returnRate >= 0 ? '盈利' : '亏损'));
      const resultClass = isOpen ? 'blue' : (returnRate >= 0 ? 'red' : 'gray');
      codes.add(code);
      const returnText = returnRateValue == null ? '--' : formatPercent(returnRate);
      const sellPriceText = isOpen
        ? (currentPrice ? `当前 ¥ ${currentPrice.toFixed(2)}` : '--')
        : (sellPrice ? `¥ ${sellPrice.toFixed(2)}` : '--');
      return `<tr data-trade-id="${escapeHtml(trade.id || '')}" class="backtest-trade-row"><td><div class="stock-name">${escapeHtml(name)}</div><div class="stock-code">${escapeHtml(code)}</div></td><td><span class="tag blue">${escapeHtml(trade.strategy_name || '策略信号')}</span></td><td>${escapeHtml(buyDate)}</td><td>¥ ${buyPrice.toFixed(2)}</td><td>${quantity ? quantity.toLocaleString('zh-CN') : '--'}</td><td>${escapeHtml(sellDate)}</td><td>${escapeHtml(sellPriceText)}</td><td class="${returnRate >= 0 ? 'up' : 'down'}">${returnText}</td><td><span class="tag ${resultClass}">${escapeHtml(result)}</span></td></tr>`;
    }).join('');

    table.onclick = event => {
      const row = event.target.closest('.backtest-trade-row[data-trade-id]');
      if (!row) return;
      const trade = visibleTrades.find(item => String(item.id || '') === row.dataset.tradeId);
      if (trade) openBacktestTradeDetail(trade);
    };
  }

  async function runScan() {
    const names = state.selectedStrategies.length ? state.selectedStrategies : state.strategies.slice(0, 3).map(item => item.name || item.display_name);
    if (!names.length) return toast('暂无可运行策略');
    const button = $('#runScan');
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle"></i>正在筛选';
    refreshIcons();
    try {
      const result = await api('/api/select', { method: 'POST', body: JSON.stringify({ strategies: names, logic: 'or' }) });
      renderScanResults(result);
      const rows = Object.values(result || {}).filter(value => Array.isArray(value)).flat();
      toast(`筛选完成，后端返回 ${rows.length} 条结果`);
      $('#filterResults')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (error) { toast(`筛选失败：${error.message}`); }
    button.disabled = false;
    button.innerHTML = '<i data-lucide="scan-line"></i>运行筛选';
    refreshIcons();
  }

  async function refreshStrategyResults(strategyName, button) {
    if (!strategyName || button?.disabled) return;
    if (button) {
      button.disabled = true;
      button.classList.add('loading');
      button.innerHTML = '<i data-lucide="loader-circle"></i>';
      refreshIcons();
    }
    try {
      const result = await api('/api/select', {
        method: 'POST',
        body: JSON.stringify({ strategies: [strategyName], logic: 'or' })
      });
      renderScanResults(result);
      const rows = Object.values(result || {}).filter(value => Array.isArray(value)).flat();
      toast(`策略「${strategyName}」已刷新，命中 ${rows.length} 只股票`);
      $('#filterResults')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (error) {
      toast(`策略刷新失败：${error.message}`);
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove('loading');
        button.innerHTML = '<i data-lucide="refresh-cw"></i>';
        refreshIcons();
      }
    }
  }

  async function runCodeScreen() {
    const source = $('#codeFilter')?.value.trim();
    if (!source) return toast('请先编写筛选代码');
    let timeRange;
    try {
      timeRange = getCodeTimeRange();
    } catch (error) {
      return toast(error.message);
    }
    const button = $('#runCode');
    if (!button) return;
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle"></i>执行中';
    refreshIcons();
    try {
      const strategyName = state.activeCodeStrategy?.name || '未保存代码策略';
      const result = await api('/api/quantpilot/code-screen', {
        method: 'POST',
        body: JSON.stringify({ source, strategy_name: '', time_range: timeRange })
      });
      state.lastCodeScreen = {
        source,
        results: result.results || [],
        strategyName,
        timeRange,
      };
      renderScanResults({ [strategyName]: result.results || [] });
      const incomplete = result.errors?.length
        ? `；${result.errors.length} 只未纳入：${result.errors[0]?.error || '历史数据不足'}`
        : '';
      const importing = result.import_stats?.import_running
        ? `；${result.import_stats.deferred_codes || 0} 只历史K线正在后台补充`
        : '';
      const warning = result.warnings?.length ? `；${result.warnings[0]}` : '';
      toast(`策略「${strategyName}」筛选完成，命中 ${result.count || 0} 只股票${incomplete}${importing}${warning}`);
    } catch (error) {
      toast(`代码筛选失败：${error.message}`);
    }
    button.disabled = false;
    button.innerHTML = '<i data-lucide="play"></i>运行代码';
    refreshIcons();
  }

  async function refreshCodeStrategy(strategy, button) {
    if (!strategy?.source || button?.disabled) return;
    if (button) {
      button.disabled = true;
      button.classList.add('loading');
      button.innerHTML = '<i data-lucide="loader-circle"></i>';
      refreshIcons();
    }
    try {
      const result = await api('/api/quantpilot/code-screen', {
        method: 'POST',
        body: JSON.stringify({
          source: strategy.source,
          strategy_name: strategy.name,
          max_stocks: strategy.max_stocks || undefined,
          force_refresh: true,
          time_range: strategy.time_range || { mode: 'all' },
        })
      });
      state.codeStrategyResults[strategy.name] = result.results || [];
      const current = state.codeStrategies.find(item => item.name === strategy.name);
      if (current) {
        current.results = result.results || [];
        current.time_range = strategy.time_range || { mode: 'all' };
        current.updated_at = new Date().toISOString().replace('T', ' ').slice(0, 19);
      }
      if (state.activeCodeStrategy?.name === strategy.name) setCodeTimeRange(strategy.time_range);
      renderCodeStrategyPools(strategy.name);
      const incomplete = result.errors?.length
        ? `；${result.errors.length} 只未纳入：${result.errors[0]?.error || '历史数据不足'}`
        : '';
      const importing = result.import_stats?.import_running
        ? `；${result.import_stats.deferred_codes || 0} 只历史K线正在后台补充`
        : '';
      toast(`策略「${strategy.name}」已刷新，命中 ${result.count || 0} 只股票${incomplete}${importing}`);
      $('#filterResults')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } catch (error) {
      toast(`代码策略刷新失败：${error.message}`);
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove('loading');
        button.innerHTML = '<i data-lucide="refresh-cw"></i>';
        refreshIcons();
      }
    }
  }

  async function runBacktest() {
    const strategySelect = $('#backtestStrategySelect');
    const selectedStrategyName = strategySelect?.value || state.activeCodeStrategy?.name || state.codeStrategies[0]?.name || '';
    const savedStrategy = state.codeStrategies.find(strategy => strategy.name === selectedStrategyName)
      || state.activeCodeStrategy
      || state.codeStrategies[0];
    const start = $('#backtestStart')?.value;
    const end = $('#backtestEnd')?.value;
    if (!savedStrategy || !start || !end || start >= end) return toast('请选择已保存策略并检查回测日期范围');
    const button = $('#runBacktest');
    button.disabled = true;
    button.innerHTML = '<i data-lucide="loader-circle"></i>回测中';
    refreshIcons();
    setText('backtestRunState', '运行中');
    const stopProgressPolling = startBacktestProgressPolling();
    try {
      const request = {
        strategy_name: savedStrategy.name,
        code_strategy: true,
        // 买卖成交必须来自真实历史5分钟K线；分钟数据缺失时后端会明确失败。
        intraday_backtest: true,
        intraday_interval: '5',
        start_date: start,
        end_date: end,
        // 已保存代码策略的命中结果就是买入信号，历史回测不再叠加海龟择时。
        timing_strategy: 'user_conditions'
      };
      const result = await api('/api/trading/backtest/run', { method: 'POST', body: JSON.stringify(request) });
      const granularity = result.backtest_mode === 'daily_fallback'
        ? '日线降级'
        : (result.backtest_mode === 'daily' ? '日线' : '5分钟');
      setText('backtestRunState', '已完成');
      const fields = { backtestReturn: result.total_return, backtestWin: result.win_rate, backtestDrawdown: result.max_drawdown, backtestTrades: result.total_trades };
      Object.entries(fields).forEach(([id, value]) => { if (value != null) setText(id, id === 'backtestTrades' ? value : formatPercent(value)); });
      if (result.total_return != null) {
        const observedEnd = result.equity_curve?.length
          ? (result.equity_curve[result.equity_curve.length - 1].date || end)
          : (result.exit_end_date || end);
        const annualReturn = calculateAnnualizedReturn(result.total_return, start, observedEnd);
        setText('backtestAnnual', formatPercent(annualReturn));
        setSignClass('backtestReturn', Number(result.total_return));
        setSignClass('backtestAnnual', Number(annualReturn));
      }
      setText('backtestStatus', `最近运行：${result.created_at || new Date().toLocaleString('zh-CN')} · ${granularity}`);
      renderBacktestEquityCurve(result.equity_curve);
      renderBacktestTrades(result.trades);
      const modeMessage = result.backtest_message ? `；${result.backtest_message}` : '';
      const policyText = result.position_policy || `结束日 ${result.buy_end_date || end} 后停止买入，持仓继续检查卖出条件至 ${result.exit_end_date || end}`;
      setText('backtestStatus', `最近运行：${result.created_at || new Date().toLocaleString('zh-CN')} · ${granularity} · ${policyText}`);
      toast(`回测完成：${savedStrategy.name} 已生成并测试 ${result.total_trades || 0} 笔交易；${policyText}${modeMessage}`);
    } catch (error) { setText('backtestRunState', '失败'); toast(`回测失败：${error.message}`); }
    finally { stopProgressPolling(); }
    button.disabled = false;
    button.innerHTML = '<i data-lucide="play"></i>运行回测';
    refreshIcons();
  }

  function renderExternalEnvironment(snapshot) {
    const panel = $('#externalEnvironmentStatus');
    const decision = snapshot?.opening_decision || {};
    if (!panel) return;
    const allowed = decision.allow_open === true;
    panel.dataset.state = allowed ? 'allow' : 'block';
    setText('externalEnvironmentDecision', allowed ? '允许 A 股开仓' : '禁止 A 股开仓');
    const reasons = Array.isArray(decision.reasons) ? decision.reasons : [];
    const japan = decision.japan?.change_pct;
    const korea = decision.korea?.change_pct;
    const detail = reasons[0] || `日本 ${formatPercent(japan)} · 韩国 ${formatPercent(korea)}`;
    setText('externalEnvironmentDetail', `${detail} · 快照 ${snapshot.created_at || '--'}`);
  }

  async function loadExternalEnvironment(refresh = false) {
    try {
      const snapshot = await api('/api/quantpilot/external-environment', refresh ? {
        method: 'POST', body: JSON.stringify({})
      } : {});
      renderExternalEnvironment(snapshot);
      return snapshot;
    } catch (error) {
      const panel = $('#externalEnvironmentStatus');
      if (panel) panel.dataset.state = 'block';
      setText('externalEnvironmentDecision', '禁止 A 股开仓');
      setText('externalEnvironmentDetail', `外围数据读取失败：${error.message}`);
      return null;
    }
  }

  function automationSideLabel(side) {
    return ({ buy: '买入', add: '加仓', sell: '卖出', hold: '未触发', dividend: '分红', basket_buy: '篮子买入', basket_sell: '篮子卖出' })[side] || side || '--';
  }

  function automationStatusLabel(status) {
    return ({ filled: '已成交', rejected: '已拒绝', cancelled: '已取消' })[status] || status || '--';
  }

  function renderAutomationTradeRecords() {
    const table = $('#automationTradeTable');
    if (!table) return;
    const records = (state.automationDashboard?.trade_records || [])
      .filter(record => record.status === 'filled');
    const side = $('#automationRecordSide')?.value || 'all';
    const keyword = ($('#automationRecordSearch')?.value || '').trim().toLowerCase();
    const filtered = records.filter(record => {
      if (side !== 'all' && record.side !== side) return false;
      const search = `${record.code || ''} ${record.name || ''} ${record.reason || ''}`.toLowerCase();
      return !keyword || search.includes(keyword);
    });
    setText('automationRecordNote', records.length
      ? `共 ${records.length} 条真实模拟成交 · 当前显示 ${filtered.length} 条`
      : '后端尚未保存自动成交记录');
    if (!filtered.length) {
      table.innerHTML = `<tr><td colspan="9" class="automation-empty">${records.length ? '没有符合当前筛选条件的成交记录' : '暂无自动成交记录；候选只有在次日满足买入条件后才会产生记录'}</td></tr>`;
      return;
    }
    table.innerHTML = filtered.map(record => {
      const isBuy = record.side === 'buy' || record.side === 'add';
      const statusClass = record.status === 'filled' ? 'green' : (record.status === 'rejected' ? 'orange' : 'gray');
      const amount = Number(record.amount || 0);
      const price = Number(record.price || 0);
      const quantity = Number(record.quantity || 0);
      return `<tr data-search="${escapeHtml(`${record.code || ''} ${record.name || ''} ${record.reason || ''}`)}"><td>${escapeHtml(`${record.trade_date || '--'} ${record.trade_time || ''}`.trim())}</td><td><div class="stock-name">${escapeHtml(record.name || record.code || '--')}</div><div class="stock-code">${escapeHtml(record.code || '--')}</div></td><td><span class="tag ${record.side === 'dividend' ? 'blue' : (isBuy ? 'red' : 'green')}">${escapeHtml(automationSideLabel(record.side))}</span></td><td>${escapeHtml(record.reason || '策略条件触发')}</td><td>${quantity ? quantity.toLocaleString('zh-CN') : '--'}</td><td>${price ? `¥ ${price.toFixed(2)}` : '--'}</td><td>${amount ? `¥ ${amount.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '--'}</td><td>¥ ${Number(record.fees || 0).toFixed(2)}</td><td><span class="tag ${statusClass}">${escapeHtml(automationStatusLabel(record.status))}</span></td></tr>`;
    }).join('');
  }

  function renderAutomationPositions(data) {
    const table = $('#automationPositionTable');
    if (!table) return;
    const positions = Array.isArray(data.positions) ? data.positions : [];
    const updatedAt = String(data.valuation_updated_at || data.generated_at || '').replace('T', ' ').slice(0, 19);
    setText('automationPositionNote', positions.length
      ? `共 ${positions.length} 只持仓，累计盈亏=卖出所得+余股估值−买入支出${updatedAt ? ` · 估值更新于 ${updatedAt}` : ''}`
      : '当前没有持仓');
    if (!positions.length) {
      table.innerHTML = '<tr><td colspan="7" class="automation-empty">当前没有持仓</td></tr>';
      return;
    }
    table.innerHTML = positions.map(position => {
      const quantity = Number(position.quantity || 0);
      const available = Number(position.available || 0);
      const avgCost = Number(position.avg_cost || 0);
      const latestPrice = Number(position.latest_price || 0);
      const marketValue = Number(position.market_value || latestPrice * quantity);
      const hasCum = position.cumulative_pnl != null;
      const pnl = hasCum ? Number(position.cumulative_pnl) : (latestPrice - avgCost) * quantity;
      const pnlPct = hasCum && position.cumulative_pnl_pct != null
        ? Number(position.cumulative_pnl_pct)
        : Number(position.float_pnl_pct ?? (avgCost ? pnl / (avgCost * quantity) * 100 : 0));
      const pnlClass = pnl > 0 ? 'up' : (pnl < 0 ? 'down' : '');
      const pnlText = `${pnl >= 0 ? '+' : ''}¥ ${Math.abs(pnl).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      return `<tr><td><div class="stock-name">${escapeHtml(position.name || position.code || '--')}</div><div class="stock-code">${escapeHtml(position.code || '--')}</div></td><td>${quantity.toLocaleString('zh-CN')}</td><td>${available.toLocaleString('zh-CN')}</td><td>¥ ${avgCost.toFixed(3)}</td><td>¥ ${latestPrice.toFixed(2)}</td><td>¥ ${marketValue.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td><td class="${pnlClass}"><strong>${pnlText}</strong><div>${formatPercent(pnlPct)}</div></td></tr>`;
    }).join('');
  }

  function renderAutomationPlan(data) {
    const select = $('#automationStrategySelect');
    if (select) {
      const options = data.strategy_options || [];
      select.innerHTML = options.length
        ? options.map(item => `<option value="${escapeHtml(item.name)}" ${item.name === data.strategy?.name ? 'selected' : ''}>${escapeHtml(item.name)} · ${item.result_count || 0} 只</option>`).join('')
        : '<option value="">暂无筛选策略</option>';
    }
    const candidates = data.candidates || [];
    const queued = candidates.filter(item => item.queued);
    setText('automationPlanCount', `${data.counts?.queued || 0} 只`);
    const strategyDate = data.strategy?.updated_at ? String(data.strategy.updated_at).replace('T', ' ').slice(0, 16) : '--';
    const planNote = $('#automationPlanNote');
    if (planNote) {
      planNote.textContent = data.strategy?.stale
        ? `当前筛选结果更新于 ${strategyDate}，建议先刷新筛选再加入明日计划`
        : `筛选结果 ${data.counts?.screened || 0} 只，已加入明日计划 ${data.counts?.queued || 0} 只`;
      planNote.classList.toggle('automation-stale', Boolean(data.strategy?.stale));
    }
    const list = $('#automationPlanList');
    if (!list) return;
    if (!candidates.length) {
      list.innerHTML = '<div class="automation-empty">当前策略没有筛选结果，请先到代码筛选页运行策略</div>';
      return;
    }
    list.innerHTML = candidates.slice(0, 20).map(item => {
      const themes = (item.business_themes || []).slice(0, 3).map(theme => theme.name).join('、');
      const themeText = item.theme_score == null
        ? (item.theme_reason || '题材快照不可用')
        : `真实题材 ${Number(item.theme_score).toFixed(1)} 分 · 仓位 ${Math.round(Number(item.theme_multiplier || 0) * 100)}% · ${themes || '无可计分题材'}`;
      return `<div class="automation-plan-row" data-code="${escapeHtml(item.code)}"><div class="automation-plan-stock"><strong>${escapeHtml(item.name || item.code)}</strong><span>${escapeHtml(`${item.code} · ${item.industry || '未分类'} · ${themeText}`)}</span></div><span class="tag ${item.queued ? 'green' : 'gray'}">${escapeHtml(item.status || (item.queued ? '待次日判断' : '待确认'))}</span><button class="icon-btn automation-plan-action" data-automation-action="${item.queued ? 'cancel' : 'queue'}" title="${item.queued ? '移出明日计划' : '加入明日计划'}"><i data-lucide="${item.queued ? 'x' : 'plus'}"></i></button></div>`;
    }).join('');
    if (candidates.length > 20) list.insertAdjacentHTML('beforeend', `<div class="automation-empty">另有 ${candidates.length - 20} 只候选，请在代码筛选页查看</div>`);
    refreshIcons();
  }

  function renderAutomationRules(data) {
    const rules = data.rules || {};
    const timingName = '5分钟K线确认';
    const rows = [
      ['次日买入判断', `${rules.opening_window || '09:30-09:35'} · ${timingName}`],
      ['真实题材门槛', rules.theme_position_enabled ? `评分不低于 ${rules.theme_min_score ?? '--'} 分${rules.theme_position_adjustment_enabled ? '；按评分调整仓位' : '；不折减目标仓位'}` : '未启用'],
      ['持仓数量上限', `${rules.max_positions ?? '--'} 只`],
      ['单票 / 总仓位', `${rules.max_position_pct ?? '--'}% / ${rules.max_total_position_pct ?? '--'}%`],
      ['固定止损', `亏损 ${rules.stop_loss ?? '--'}% 清仓`],
      ['分批止盈', `+${rules.take_profit ?? '--'}% 卖 ${rules.take_profit_sell_pct ?? '--'}%，+${rules.second_take_profit ?? '--'}% 再卖 ${rules.second_take_profit_sell_pct ?? '--'}%`],
      ['移动止盈', `最高价回撤 ${rules.trailing_profit_drawdown ?? '--'}% 清仓`],
      ['最长持仓', `${rules.max_hold_days || '未限制'} 个交易日`],
    ];
    const list = $('#automationRuleList');
    if (list) list.innerHTML = rows.map(row => `<div class="automation-rule-row"><span>${escapeHtml(row[0])}</span><strong>${escapeHtml(row[1])}</strong></div>`).join('');
  }

  function renderAutomationSignals(data) {
    const activities = (data.activities || []).filter(item => item.kind === 'signal');
    setText('automationSignalCount', `${activities.length} 条`);
    const list = $('#automationSignalList');
    if (!list) return;
    if (!activities.length) {
      list.innerHTML = '<div class="automation-empty">今日尚无条件判断记录；交易时段启动监控后会显示每只候选的判断结果</div>';
      return;
    }
    list.innerHTML = activities.map(item => {
      const sideClass = item.side === 'sell' ? 'green' : (item.side === 'hold' ? 'gray' : 'red');
      return `<div class="automation-signal-row"><span class="automation-signal-time">${escapeHtml(String(item.time || '--').replace('T', ' ').slice(0, 19))}</span><div class="automation-signal-main"><strong>${escapeHtml(`${item.code || '--'} · ${automationSideLabel(item.side)}`)}</strong><span>${escapeHtml(item.message || '条件检查完成')}</span></div><span class="tag ${sideClass}">${escapeHtml(item.status || '未执行')}</span></div>`;
    }).join('');
  }

  function conditionRows(rows) {
    if (!rows.length) return '<div class="automation-empty">未配置启用条件</div>';
    return rows.map(([label, value]) => (
      `<div class="condition-source-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
    )).join('');
  }

  function activeConditionLines(scope) {
    return $$('.condition-row', scope).flatMap((row) => {
      if (!$('.condition-switch', row)?.classList.contains('on')) return [];
      const values = $$('.condition-fields select, .condition-fields input', row)
        .map(element => String(element.value || '').trim())
        .filter(Boolean);
      return values.length ? [values.join(' ')] : [];
    });
  }

  function renderConditionSourceComparison(data = {}) {
    const rules = data.rules || {};
    const strategy = data.strategy || {};
    const external = data.external_environment || {};
    const webConfig = readAutomationConfigForm(state.automationConfig || rules);
    const timingName = '5分钟K线确认';
    const entryChecks = [];
    if (rules.require_vwap_confirmation) entryChecks.push('站上 VWAP');
    if (rules.require_open_price_confirmation) entryChecks.push('不低于开盘价');
    if (rules.block_on_opening_low_break) entryChecks.push('不跌破开盘首根 5 分钟低点');
    const delay = Number(rules.open_entry_delay_minutes || 0);
    const serverBuy = [
      ['候选池', strategy.name ? `${strategy.name}（${strategy.result_count ?? 0} 只）` : '未配置代码策略'],
      ['择时信号', timingName],
      ['开仓时点', delay ? `09:30 后等待 ${delay} 分钟确认` : '开盘后'],
      ['价格确认', entryChecks.length ? entryChecks.join('；') : '未启用额外价格确认'],
      ['防追高', `VWAP直接 ${rules.max_vwap_deviation_pct ?? '--'}%以内；超过 ${rules.vwap_pullback_deviation_pct ?? '--'}% 当日不追；日涨幅不超 ${rules.max_intraday_gain_pct ?? '--'}%`],
      ['外围环境', !rules.require_external_environment ? '未启用' : (external.allow_open ? '当前允许开仓' : `当前禁止开仓${external.reasons?.length ? `：${external.reasons[0]}` : ''}`)],
      ['仓位限制', `最多 ${rules.max_positions ?? '--'} 只；单票 ${rules.max_position_pct ?? '--'}%；总仓 ${rules.max_total_position_pct ?? '--'}%`],
      ['题材门槛', rules.theme_position_enabled ? `评分不低于 ${rules.theme_min_score ?? '--'} 分` : '未启用'],
      ['买入时段', (rules.buy_time_windows || []).join('；') || '未配置'],
    ];
    const serverSell = [
      ['固定止损', `亏损 ${rules.stop_loss ?? '--'}% 清仓`],
      ['首档止盈', `盈利 ${rules.take_profit ?? '--'}% 卖出 ${rules.take_profit_sell_pct ?? '--'}%`],
      ['二档止盈', `盈利 ${rules.second_take_profit ?? '--'}% 再卖 ${rules.second_take_profit_sell_pct ?? '--'}%`],
      ['移动止盈', `首档后从高点回撤 ${rules.trailing_profit_drawdown ?? '--'}% 清仓`],
      ['涨停兑现', `涨停且盈利 ${rules.limit_up_profit ?? '--'}% 卖出 ${rules.limit_up_sell_pct ?? '--'}%`],
      ['最长持仓', `${rules.max_hold_days || '未限制'} 个交易日`],
      ['择时卖出', '不使用日线择时卖出信号'],
      ['分时卖出确认', '所有卖出均需已收盘5分钟K确认走弱'],
    ];
    const webTimingName = '5分钟K线确认';
    const webEntryChecks = [];
    if (webConfig.require_vwap_confirmation) webEntryChecks.push('站上 VWAP');
    if (webConfig.require_open_price_confirmation) webEntryChecks.push('不低于开盘价');
    if (webConfig.block_on_opening_low_break) webEntryChecks.push('不跌破首根 5 分钟低点');
    const webBuy = [
      ['择时信号', webTimingName],
      ['外围环境', webConfig.require_external_environment ? '启用开仓拦截' : '未启用'],
      ['题材门槛', webConfig.theme_position_enabled ? `评分不低于 ${webConfig.theme_min_score ?? '--'} 分` : '未启用'],
      ['买入时段', (webConfig.buy_time_windows || []).join('；') || '未配置'],
      ['价格确认', webEntryChecks.join('；') || '未启用额外价格确认'],
      ['防追高', `VWAP直接 ${webConfig.max_vwap_deviation_pct ?? '--'}%以内；超过 ${webConfig.vwap_pullback_deviation_pct ?? '--'}% 当日不追；日涨幅不超 ${webConfig.max_intraday_gain_pct ?? '--'}%`],
      ['仓位限制', `最多 ${webConfig.max_positions ?? '--'} 只；单票 ${webConfig.max_position_pct ?? '--'}%；总仓 ${webConfig.max_total_position_pct ?? '--'}%`],
      ['分次建仓', `首次 ${webConfig.initial_position_pct ?? '--'}%；加仓 ${webConfig.add_position_pct ?? '--'}% / 大盘走强 ${webConfig.market_strength_add_position_pct ?? '--'}% / 底部强反转 ${webConfig.bottom_reversal_add_position_pct ?? '--'}%`],
    ];
    const webSell = [
      ['固定止损', `亏损 ${webConfig.stop_loss ?? '--'}% 清仓`],
      ['首档止盈', `盈利 ${webConfig.take_profit ?? '--'}% 卖出 ${webConfig.take_profit_sell_pct ?? '--'}%`],
      ['二档止盈', `盈利 ${webConfig.second_take_profit ?? '--'}% 再卖 ${webConfig.second_take_profit_sell_pct ?? '--'}%`],
      ['移动止盈', `首档后从高点回撤 ${webConfig.trailing_profit_drawdown ?? '--'}% 清仓`],
      ['涨停兑现', `涨停且盈利 ${webConfig.limit_up_profit ?? '--'}% 卖出 ${webConfig.limit_up_sell_pct ?? '--'}%`],
      ['最长持仓', `${webConfig.max_hold_days || '未限制'} 个交易日`],
    ];
    const serverBuyList = $('#serverBuyConditionList');
    const serverSellList = $('#serverSellConditionList');
    const webBuyList = $('#webBuyConditionList');
    const webSellList = $('#webSellConditionList');
    if (serverBuyList) serverBuyList.innerHTML = conditionRows(serverBuy);
    if (serverSellList) serverSellList.innerHTML = conditionRows(serverSell);
    if (webBuyList) webBuyList.innerHTML = conditionRows(webBuy);
    if (webSellList) webSellList.innerHTML = conditionRows(webSell);
  }

  function renderAutomationDashboard(data) {
    state.automationDashboard = data || {};
    setText('automationMode', data.mode || '--');
    setText('automationMonitoring', data.monitoring ? '运行中' : '未启动');
    setText('automationMonitoringNote', data.monitoring ? '正在监听盘中行情与条件' : '仅展示已保存记录');
    setText('automationTradesToday', `${data.counts?.trades_today || 0} 笔`);
    setText('automationTradeDate', data.today || '--');
    setText('automationQueued', `${data.counts?.queued || 0} 只`);
    setText('automationExecutionDate', `${data.execution_date || '--'} 判断`);
    setText('automationPositions', `${data.counts?.positions || 0} 只`);
    setText('automationPositionLimit', `上限 ${data.rules?.max_positions ?? '--'} 只`);
    renderAutomationPositions(data);
    renderAutomationTradeRecords();
    renderAutomationPlan(data);
    renderAutomationRules(data);
    renderAutomationSignals(data);
    renderConditionSourceComparison(data);
  }

  async function loadAutomationDashboard(strategyName = '') {
    try {
      const query = strategyName ? `?strategy_name=${encodeURIComponent(strategyName)}` : '';
      const data = await api(`/api/quantpilot/automation-dashboard${query}`);
      renderAutomationDashboard(data);
      return data;
    } catch (error) {
      const table = $('#automationTradeTable');
      if (table) table.innerHTML = `<tr><td colspan="9" class="automation-empty">自动交易记录读取失败：${escapeHtml(error.message)}</td></tr>`;
      return null;
    }
  }

  async function updateAutomationPlan(code, action) {
    const strategyName = state.automationDashboard?.strategy?.name || '';
    if (action === 'cancel') {
      await api(`/api/quantpilot/automation-plan/${encodeURIComponent(code)}`, { method: 'DELETE' });
      toast(`${code} 已移出明日交易计划`);
      await loadAutomationDashboard(strategyName);
      return;
    }
    const data = await api('/api/quantpilot/automation-plan', {
      method: 'POST',
      body: JSON.stringify({ codes: [code], strategy_name: strategyName })
    });
    renderAutomationDashboard(data);
    toast(`${code} 已加入 ${data.execution_date || '下一交易日'} 交易计划`);
  }

  async function loadMarketOverview() {
    setText('overviewHeadingNote', '正在读取服务器数据...');
    try {
      const [overviewResult, strategiesResult] = await Promise.allSettled([
        api('/api/quantpilot/market-overview'),
        api('/api/strategies')
      ]);
      if (overviewResult.status === 'rejected') throw overviewResult.reason;
      const data = overviewResult.value;
      state.marketOverview = data;
      if (strategiesResult.status === 'fulfilled') {
        const strategies = strategiesResult.value;
        state.strategies = Array.isArray(strategies) ? strategies : (strategies?.strategies || []);
        renderStrategyList();
      }
      renderStats(data);
      renderSignals(data);
      renderOverviewNav(data, state.overviewRange);
      return data;
    } catch (error) {
      setText('overviewHeadingNote', `市场总览读取失败：${error.message}`);
      setText('overviewNavSubtitle', '服务器净值读取失败');
      hideOverviewRangeStats();
      const empty = $('#overviewNavEmpty');
      if (empty) { empty.hidden = false; empty.textContent = '服务器净值读取失败'; }
      const list = $('.signal-list');
      if (list) list.innerHTML = `<div class="stock-pool-empty">服务器信号读取失败：${escapeHtml(error.message)}</div>`;
      return null;
    }
  }

  async function loadStartupData() {
    setText('serverDataStatus', '正在同步服务器数据');
    setText('serverDataDetail', '市场总览、自动交易与条件正在刷新');
    const [market, automation, external, automationConfig] = await Promise.all([
      loadMarketOverview(),
      loadAutomationDashboard(),
      loadExternalEnvironment(),
      loadAutomationConfig().catch(error => {
        console.warn('自动交易配置读取失败:', error);
        return null;
      })
    ]);
    await Promise.allSettled([loadWatchlist(), loadCodeStrategies()]);
    const synced = [market, automation].filter(Boolean).length;
    setText('serverDataStatus', synced === 2 ? '服务器数据已同步' : '部分服务器数据失败');
    setText(
      'serverDataDetail',
      synced === 2
        ? `总览与自动交易 · ${String(market.generated_at || '').replace('T', ' ').slice(0, 19)}`
        : `市场总览 ${market ? '成功' : '失败'} · 自动交易 ${automation ? '成功' : '失败'}`
    );
    return { market, automation, external, automationConfig };
  }

  async function loadData() {
    return loadStartupData();
  }

  function bindActions() {
    const refresh = replaceHandler('refreshBtn');
    refresh?.addEventListener('click', async () => {
      refresh.classList.add('loading');
      try {
        await loadStartupData();
        toast('服务器数据已刷新');
      } finally {
        refresh.classList.remove('loading');
      }
    });
    $$('.chart-tab').forEach(tab => tab.addEventListener('click', () => {
      state.overviewRange = tab.dataset.range || 'YTD';
      renderOverviewNav(state.marketOverview, state.overviewRange);
    }));
    const applyOverviewDateRange = () => {
      state.overviewCustomStart = $('#overviewStartDate')?.value || '';
      state.overviewCustomEnd = $('#overviewEndDate')?.value || '';
      state.overviewRange = 'CUSTOM';
      renderOverviewNav(state.marketOverview, state.overviewRange);
    };
    $('#overviewDateApply')?.addEventListener('click', applyOverviewDateRange);
    ['overviewStartDate', 'overviewEndDate'].forEach(id => {
      $('#' + id)?.addEventListener('change', () => {
        state.overviewCustomStart = $('#overviewStartDate')?.value || '';
        state.overviewCustomEnd = $('#overviewEndDate')?.value || '';
      });
    });
    $('[data-target="overview"]')?.addEventListener('click', () => loadMarketOverview());
    $('[data-target="automation"]')?.addEventListener('click', () => {
      Promise.all([loadAutomationDashboard(), loadExternalEnvironment()]);
    });
    $('[data-target="review"]')?.addEventListener('click', () => loadReviewCenter());
    $('#legPerformanceCards')?.addEventListener('click', ev => {
      if (ev.target.closest('[data-leg-jump]')) $('[data-target="review"]')?.click();
    });
    $('#reviewDate')?.addEventListener('change', () => loadReviewCenter());
    $$('[data-review-view]').forEach(btn => btn.addEventListener('click', () => {
      $$('[data-review-view]').forEach(b => b.classList.toggle('active', b === btn));
      const view = btn.dataset.reviewView;
      $('#reviewDayView').classList.toggle('page-hidden', view !== 'day');
      $('#reviewLegsView').classList.toggle('page-hidden', view !== 'legs');
      $('#reviewReportView').classList.toggle('page-hidden', view !== 'report');
      if (view === 'day') loadReviewCenter();
      if (view === 'legs') loadReviewLegs();
      if (view === 'report') {
        $$('[data-review-period]').forEach(b => b.classList.toggle('active', b.dataset.reviewPeriod === 'week'));
        loadReviewReport('week');
      }
    }));
    $$('[data-review-period]').forEach(btn => btn.addEventListener('click', () => {
      $$('[data-review-period]').forEach(b => b.classList.toggle('active', b === btn));
      loadReviewReport(btn.dataset.reviewPeriod);
    }));
    if (location.hash === '#review') loadReviewCenter();
    // URL hash 直达复盘中心(刷新/收藏/返回)时同样自动加载,不必再点导航。
    window.addEventListener('hashchange', () => {
      if (location.hash === '#review') loadReviewCenter();
    });
    const scan = replaceHandler('runScan');
    scan?.addEventListener('click', runScan);
    const runCode = replaceHandler('runCode');
    runCode?.addEventListener('click', runCodeScreen);
    bindCodeTimeRange();
    const saveStrategy = replaceHandler('saveStrategy');
    saveStrategy?.addEventListener('click', saveCodeStrategy);
    ['closeModal', 'cancelModal'].forEach(id => {
      document.getElementById(id)?.addEventListener('click', resetCodeStrategyModal);
    });
    const cumulativeCard = replaceHandler('cumulativeReturnCard');
    cumulativeCard?.addEventListener('click', openCumulativeTradesPage);
    $('#cumulativeTradeTable')?.closest('table')?.querySelectorAll('th[data-sort-key]').forEach(th => {
      th.addEventListener('click', () => {
        const key = th.dataset.sortKey;
        if (!key) return;
        if (cumulativeTradesState.sortKey === key) {
          cumulativeTradesState.sortDir = cumulativeTradesState.sortDir === 'desc' ? 'asc' : 'desc';
        } else {
          cumulativeTradesState.sortKey = key;
          cumulativeTradesState.sortDir = 'desc';
        }
        renderCumulativeTrades({
          records: cumulativeTradesState.records,
          total_pnl: cumulativeTradesState.totalPnl,
        });
      });
    });
    const backtest = replaceHandler('runBacktest');
    backtest?.addEventListener('click', runBacktest);
    const saveCondition = replaceHandler('saveConditionStrategy');
    saveCondition?.addEventListener('click', async () => {
      try {
        saveCondition.disabled = true;
        await saveAutomationConfig();
      } catch (error) {
        toast(`条件设置保存失败：${error.message}`);
      } finally {
        saveCondition.disabled = false;
      }
    });
    const resetCondition = replaceHandler('resetConditionStrategy');
    resetCondition?.addEventListener('click', async () => {
      try {
        resetCondition.disabled = true;
        await loadAutomationConfig();
        const status = $('#conditionStatusText');
        if (status) status.textContent = '服务器当前设置';
        toast('已恢复服务器当前设置');
      } catch (error) {
        toast(`恢复服务器设置失败：${error.message}`);
      } finally {
        resetCondition.disabled = false;
      }
    });
    const conditionSettings = $('#condition-strategy');
    ['input', 'change', 'click'].forEach(eventName => {
      conditionSettings?.addEventListener(eventName, () => {
        window.requestAnimationFrame(() => renderConditionSourceComparison(
          state.automationDashboard || {}
        ));
      });
    });
    const externalRefresh = replaceHandler('refreshExternalEnvironment');
    externalRefresh?.addEventListener('click', async () => {
      externalRefresh.classList.add('loading');
      await loadExternalEnvironment(true);
      externalRefresh.classList.remove('loading');
    });
    const automationRefresh = replaceHandler('refreshAutomation');
    automationRefresh?.addEventListener('click', async () => {
      automationRefresh.classList.add('loading');
      await loadAutomationDashboard(state.automationDashboard?.strategy?.name || '');
      await loadExternalEnvironment();
      automationRefresh.classList.remove('loading');
      toast('自动交易记录已刷新');
    });
    const automationSide = replaceHandler('automationRecordSide');
    automationSide?.addEventListener('change', renderAutomationTradeRecords);
    const automationSearch = replaceHandler('automationRecordSearch');
    automationSearch?.addEventListener('input', renderAutomationTradeRecords);
    const automationStrategy = replaceHandler('automationStrategySelect');
    automationStrategy?.addEventListener('change', () => loadAutomationDashboard(automationStrategy.value));
    const automationPlan = replaceHandler('automationPlanList');
    automationPlan?.addEventListener('click', async event => {
      const button = event.target.closest('[data-automation-action]');
      const row = button?.closest('[data-code]');
      if (!button || !row) return;
      button.disabled = true;
      try {
        await updateAutomationPlan(row.dataset.code, button.dataset.automationAction);
      } catch (error) {
        toast(`明日计划更新失败：${error.message}`);
        button.disabled = false;
      }
    });
    const risk = replaceHandler('riskBtn');
    risk?.addEventListener('click', () => $('[data-target="settings"]')?.click());
    const openCodeStrategy = replaceHandler('openCodeStrategy');
    openCodeStrategy?.addEventListener('click', () => $('[data-target="code-strategy"]')?.click());
    const addStock = replaceHandler('addStockBtn');
    addStock?.addEventListener('click', () => { $('#stockSearch')?.focus(); toast('请从筛选结果中加入自选股'); });
    replaceHandler('stockTable');
    const filterResults = replaceHandler('filterResults');
    bindResultSelection(filterResults);
    filterResults?.addEventListener('click', async event => {
      const button = event.target.closest('.add-filter-stock');
      if (!button) return;
      const row = button.closest('.filter-result-row');
      const parse = value => value === '' ? null : Number(String(value).replace('%', '').replace('+', ''));
      try {
        await saveWatch({
          code: row.dataset.code,
          name: row.dataset.name,
          industry: row.dataset.sector,
          group: $('.result-group', row).value,
          strategy: row.dataset.signal,
          signal_strength: parse(row.dataset.strength),
          win_rate: parse(row.dataset.win),
          expected_return: parse(row.dataset.return)
        });
        button.disabled = true;
        button.innerHTML = '<i data-lucide="check"></i>已加入';
        refreshIcons();
        toast(`${row.dataset.name} 已加入自选股`);
      } catch (error) { toast(`加入失败：${error.message}`); }
    });
    const strategyPool = $('#codeStrategyPoolMount');
    strategyPool?.addEventListener('click', event => {
      if (event.target.closest('.strategy-pool-select, .strategy-pool-select-all')) {
        event.stopPropagation();
        return;
      }
      if (event.target.closest('#deleteSelectedCodeStrategies')) {
        event.preventDefault();
        event.stopPropagation();
        deleteSelectedCodeStrategies();
        return;
      }
      const edit = event.target.closest('.strategy-pool-edit[data-code-strategy-edit]');
      if (edit) {
        event.preventDefault();
        event.stopPropagation();
        const strategy = state.codeStrategies.find(entry => entry.name === edit.dataset.codeStrategyEdit);
        if (strategy) openCodeStrategyEditor(strategy);
        return;
      }
      const refresh = event.target.closest('.strategy-pool-refresh[data-code-strategy-refresh]');
      if (refresh) {
        event.preventDefault();
        event.stopPropagation();
        const strategy = state.codeStrategies.find(entry => entry.name === refresh.dataset.codeStrategyRefresh);
        if (strategy) refreshCodeStrategy(strategy, refresh);
        return;
      }
      const item = event.target.closest('.strategy-pool-item[data-strategy-name]');
      if (!item) return;
      const strategy = state.codeStrategies.find(entry => entry.name === item.dataset.strategyName);
      if (strategy) loadCodeStrategyIntoEditor(strategy);
    });
    strategyPool?.addEventListener('change', event => {
      const checkbox = event.target.closest('.strategy-pool-select');
      const selectAll = event.target.closest('#selectAllCodeStrategies');
      if (selectAll) {
        state.selectedCodeStrategies = selectAll.checked
          ? state.codeStrategies.map(strategy => strategy.name)
          : [];
        renderCodeStrategyPools(state.activeCodeStrategy?.name);
        return;
      }
      if (!checkbox) return;
      const name = checkbox.dataset.codeStrategySelect;
      state.selectedCodeStrategies = checkbox.checked
        ? [...new Set([...state.selectedCodeStrategies, name])]
        : state.selectedCodeStrategies.filter(item => item !== name);
      const deleteButton = $('#deleteSelectedCodeStrategies');
      if (deleteButton) deleteButton.disabled = state.selectedCodeStrategies.length === 0;
      const allCheckbox = $('#selectAllCodeStrategies');
      if (allCheckbox) allCheckbox.checked = state.selectedCodeStrategies.length === state.codeStrategies.length;
    });
    strategyPool?.addEventListener('keydown', event => {
      const item = event.target.closest('.strategy-pool-item[data-strategy-name]');
      if (!item || (event.key !== 'Enter' && event.key !== ' ')) return;
      if (event.target.closest('.strategy-pool-refresh, .strategy-pool-edit, .strategy-pool-select')) return;
      event.preventDefault();
      const strategy = state.codeStrategies.find(entry => entry.name === item.dataset.strategyName);
      if (strategy) loadCodeStrategyIntoEditor(strategy);
    });
    const strategyList = $('.strategy-list');
    if (strategyList && !strategyList.dataset.quantPilotBound) {
      strategyList.dataset.quantPilotBound = 'true';
      strategyList.addEventListener('click', event => {
        const refresh = event.target.closest('.strategy-refresh[data-refresh-strategy]');
        if (!refresh) return;
        event.preventDefault();
        event.stopPropagation();
        refreshStrategyResults(refresh.dataset.refreshStrategy, refresh);
      });
      strategyList.addEventListener('change', () => {
      state.selectedStrategies = $$('.strategy-check:checked').map(item => item.dataset.strategy);
      setText('selectedStrategyCount', state.selectedStrategies.length);
      });
    }
    const groupButton = replaceHandler('saveGroup');
    groupButton?.addEventListener('click', async () => {
      const name = $('#groupName')?.value.trim();
      if (!name) return toast('请输入分组名称');
      state.groups.add(name);
      await api('/api/quantpilot/groups', { method: 'POST', body: JSON.stringify({ name }) });
      $('#groupModalBackdrop')?.classList.remove('open');
      renderWatchlist();
      toast(`分组「${name}」已创建`);
    });
  }

  // ---------- 复盘中心 ----------
  const fmtPct = v => (v == null ? '--' : `${(v * 100).toFixed(1)}%`);
  const fmtMoney = v => (v == null ? '--' : `¥ ${Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`);

  const renderReviewPlan = plan => {
    const cards = [];
    const machine = (plan.machine || []).map(c =>
      `<div class="backtest-metric"><label>${escapeHtml(c.code)} ${escapeHtml(c.name || '')}</label><strong>${c.strength != null ? `强度 ${Number(c.strength).toFixed(1)}` : '候选'}</strong></div>`).join('');
    cards.push(`<div class="panel review-plan-card"><div class="panel-title">机器腿计划(${(plan.machine || []).length}只)</div><div class="backtest-metrics">${machine || '<span class="automation-empty">当日无候选</span>'}</div></div>`);
    const bear = Array.isArray(plan.bear)
      ? plan.bear.map(c => `${escapeHtml(c.code)} ${escapeHtml(c.name || '')}`).join('、') || '当日无候选'
      : '数据未同步(需批准服务器快照加表)';
    cards.push(`<div class="panel review-plan-card"><div class="panel-title">熊市池腿计划</div><div>${bear}</div></div>`);
    const basket = plan.basket && Object.keys(plan.basket).length
      ? Object.entries(plan.basket).map(([k, v]) => `${escapeHtml(k)}: ${escapeHtml(JSON.stringify(v).slice(0, 80))}`).join('<br>')
      : '当日无调度记录';
    cards.push(`<div class="panel review-plan-card"><div class="panel-title">篮子调度</div><div>${basket}</div></div>`);
    const p = plan.params || {};
    cards.push(`<div class="panel review-plan-card"><div class="panel-title">参数引用(只读)</div><div class="panel-subtitle">仓位 ${fmtPct(p.initial_position_pct)} · 持仓上限 ${p.max_positions} 只 · 止损 ${fmtPct(p.stop_loss)} · 硬止损 ${fmtPct(p.hard_stop_loss)} · 止盈 ${fmtPct(p.take_profit)}/${fmtPct(p.second_take_profit)} · 最长持有 ${p.max_hold_days} 天</div></div>`);
    $('#reviewPlanCards').innerHTML = cards.join('');
  };

  const renderReviewCompare = rows => {
    const tbody = $('#reviewCompareTable');
    tbody.innerHTML = (rows || []).map(r => `<tr><td>${r.code}</td><td>${escapeHtml(r.name || '')}</td><td>${escapeHtml(r.state)}</td><td>${escapeHtml(r.detail || '--')}</td><td>${r.strength != null ? Number(r.strength).toFixed(1) : '--'}</td></tr>`).join('')
      || '<tr><td colspan="5" class="automation-empty">当日无候选记录</td></tr>';
  };

  // v2: 历史日期(非最新交易日)涨跌家数/板块仅当日可得,降级块标注"仅当日"。
  const reviewHistNote = (blockMissing, date) => (
    blockMissing && date < new Date().toISOString().slice(0, 10)
      ? ' <span class="panel-subtitle">(仅当日)</span>' : ''
  );

  const renderMarketPanel = (market, date) => {
    const indices = (market && Array.isArray(market.indices)) ? market.indices : [];
    const cards = [0, 1, 2].map(i => {
      const idx = indices[i];
      if (!idx) return '<div class="backtest-metric"><label>--</label><strong>--</strong><span>--</span></div>';
      const pct = idx.pct == null ? null : Number(idx.pct);
      const pctHtml = pct == null ? '<span>--</span>'
        : `<span class="${pct >= 0 ? 'up' : 'down'}">${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%</span>`;
      const close = idx.close == null ? '--' : Number(idx.close).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
      return `<div class="backtest-metric"><label>${escapeHtml(idx.name || '--')}</label><strong>${close}</strong>${pctHtml}</div>`;
    }).join('');
    // 量能:上证成交额 + 相对 5/20 日均值比例(>=1.15 放量 / <=0.85 缩量 / 其余 持平)。
    const shAmount = indices.filter(i => i && i.name === '上证指数').map(i => i.amount)[0];
    const ratio = (market && market.amount_ratio) || {};
    const amountTag = r => (r >= 1.15 ? '放量' : (r <= 0.85 ? '缩量' : '持平'));
    const ratioParts = [];
    if (ratio.vs5 != null) ratioParts.push(`vs5 ${Number(ratio.vs5).toFixed(2)} ${amountTag(Number(ratio.vs5))}`);
    if (ratio.vs20 != null) ratioParts.push(`vs20 ${Number(ratio.vs20).toFixed(2)} ${amountTag(Number(ratio.vs20))}`);
    const amountLine = `成交额 ${shAmount != null ? fmtMoney(shAmount) : '--'}${ratioParts.length ? `(${ratioParts.join(' · ')})` : ''}`;
    const b = market ? market.breadth : null;
    const breadthLine = !b
      ? `上涨 -- · 下跌 -- · 涨停 -- · 跌停 --${reviewHistNote(!b, date)}`
      : `上涨 <span class="up">${b.up != null ? b.up : '--'}</span> · 下跌 <span class="down">${b.down != null ? b.down : '--'}</span> · 涨停 <span class="up">${b.limit_up != null ? b.limit_up : '--'}</span> · 跌停 <span class="down">${b.limit_down != null ? b.limit_down : '--'}</span>`;
    const g = market ? market.gate : null;
    const gateLine = !g ? '年线闸门 --'
      : `年线闸门 ${g.open ? '<span class="up">开</span>' : '<span class="down">关</span>'} · 距200日线 ${g.distance_pct == null ? '--' : `${g.distance_pct >= 0 ? '+' : ''}${Number(g.distance_pct).toFixed(1)}%`} · 连续 ${g.streak_days != null ? g.streak_days : '--'} 天`;
    $('#reviewMarketPanel').innerHTML = `
      <div class="panel-header"><div><div class="panel-title">大盘环境</div><div class="panel-subtitle">${escapeHtml(date)} · 上证指数/深证成指/创业板指</div></div></div>
      <div class="backtest-metrics" style="grid-template-columns: repeat(3, minmax(0, 1fr)); padding-bottom: 5px;">${cards}</div>
      <div class="panel-body" style="padding-top: 10px; display: grid; gap: 6px;">
        <div>${amountLine}</div>
        <div>${breadthLine}</div>
        <div>${gateLine}</div>
      </div>`;
  };

  const renderIndustries = (industries, date) => {
    const industryRows = rows => (rows || []).map(r => {
      const pct = r.pct == null ? null : Number(r.pct);
      const cls = pct == null ? '' : (pct >= 0 ? 'up' : 'down');
      return `<tr><td>${escapeHtml(r.name || '--')}</td><td class="${cls}">${pct == null ? '--' : `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`}</td></tr>`;
    }).join('') || '<tr><td colspan="2" class="automation-empty">--</td></tr>';
    const note = reviewHistNote(!industries, date);
    $('#reviewIndustries').innerHTML = `
      <div class="panel-header"><div><div class="panel-title">板块强弱${note}</div><div class="panel-subtitle">行业板块当日涨跌幅</div></div></div>
      <div class="panel-body" style="display: grid; grid-template-columns: 1fr 1fr; gap: 14px;">
        <div><div class="panel-subtitle" style="margin: 0 0 5px;">涨幅前5</div><div class="table-wrap"><table class="automation-table"><tbody>${industryRows(industries && industries.top)}</tbody></table></div></div>
        <div><div class="panel-subtitle" style="margin: 0 0 5px;">跌幅前5</div><div class="table-wrap"><table class="automation-table"><tbody>${industryRows(industries && industries.bottom)}</tbody></table></div></div>
      </div>`;
  };

  const renderDaySummary = dayTrades => {
    const names = { machine: '机器腿', bear: '熊市池腿', basket: '红利篮子腿' };
    // dayTrades 为 null(端点失败/块缺失)是"数据不可用",不能误报为"无交易"。
    const unavailable = dayTrades == null;
    const legs = ['machine', 'bear', 'basket'].map(leg => {
      const t = (!unavailable && dayTrades[leg]) || {};
      const parts = [];
      if (t.buys) parts.push(`买${t.buys} ·${fmtMoney(t.buy_amount)}`);
      if (t.sells) parts.push(`卖${t.sells} ·${fmtMoney(t.sell_amount)}`);
      return `<div style="min-width: 190px;"><span class="strategy-name">${names[leg]}</span> <span>${unavailable ? '--' : (parts.length ? parts.join(' ') : '无交易')}</span></div>`;
    });
    const note = unavailable ? ' <span class="panel-subtitle">(数据不可用)</span>' : '';
    $('#reviewDaySummary').innerHTML = `
      <div class="panel-header"><div><div class="panel-title">当日账户摘要${note}</div><div class="panel-subtitle">三腿当日买卖笔数与金额(仅成交计入)</div></div></div>
      <div class="panel-body" style="display: flex; flex-wrap: wrap; gap: 10px 26px;">${legs.join('')}</div>`;
  };

  const renderReviewLegs = data => {
    $('#reviewReconNote').textContent = data.max_abs_recon_diff_pct > 0.5
      ? `分腿口径与账户净值存在偏差(最大 ${data.max_abs_recon_diff_pct.toFixed(2)}%)` : '对账通过:三腿合计与账户净值一致';
    const series = [data.pnl.machine, data.pnl.bear, data.pnl.basket, data.total_pnl];
    const ids = ['#reviewLegMachine', '#reviewLegBear', '#reviewLegBasket', '#reviewLegTotal'];
    const all = series.flat();
    const min = Math.min(0, ...all), max = Math.max(1, ...all);
    series.forEach((line, i) => {
      const n = line.length;
      const path = n < 2 ? '' : line.map((v, j) =>
        `${j === 0 ? 'M' : 'L'} ${(j / (n - 1)) * 820} ${150 - ((v - min) / (max - min)) * 140}`).join(' ');
      $(ids[i]).setAttribute('d', path);
    });
  };

  const renderReviewAttribution = data => {
    $('#reviewAttributionTable').innerHTML = (data.groups || []).map(g =>
      `<tr><td>${{machine:'机器腿',bear:'熊市池腿',basket:'红利篮子腿'}[g.leg] || escapeHtml(g.leg)}</td><td>${escapeHtml(g.env)}</td><td>${g.rounds}</td><td>${fmtPct(g.win_rate)}</td><td>${g.profit_factor == null ? '--' : g.profit_factor.toFixed(2)}</td><td class="${g.total_pnl >= 0 ? 'up' : 'down'}">${fmtMoney(g.total_pnl)}</td><td class="down">${fmtMoney(g.max_drawdown_pnl)}</td><td>${g.avg_hold_days}</td><td>${g.median_hold_days}</td></tr>`).join('')
      || '<tr><td colspan="9" class="automation-empty">该区间无已完结回合</td></tr>';
    $('#reviewOpenRounds').textContent = `持仓中回合 ${data.open_rounds.length} 个(不计入胜率)`;
  };

  const loadReviewCenter = async () => {
    const input = $('#reviewDate');
    let date = input.value;
    // 未选日期:本地镜像库即时解析最近交易日(毫秒级端点),行情源不等、计划区先行。
    if (!date) {
      try {
        const info = await api('/api/quantpilot/review/latest-date');
        date = info.latest || '';
      } catch (err) {
        console.warn(`最近交易日解析失败(退回昨日): ${err.message}`);
      }
      if (!date) date = new Date(Date.now() - 864e5).toISOString().slice(0, 10);
      input.value = date;
    }
    // 大盘快照走 akshare 可能较慢:独立并行拉取与渲染,不阻塞本地 plan/compare 的快速出块;
    // 整体失败仅本块降级为"--",不影响计划渲染,错误 toast 行为与既有口径一致。
    const marketTask = (async () => {
      let market = null;
      try {
        market = await api(`/api/quantpilot/review/market/${date}`);
      } catch (err) {
        console.warn(`大盘快照加载失败(降级为--): ${err.message}`);
      }
      renderMarketPanel(market, date);
      renderIndustries(market && market.industries, date);
      renderDaySummary(market && market.day_trades);
    })();
    try {
      const [plan, compare] = await Promise.all([
        api(`/api/quantpilot/review/plan/${date}`),
        api(`/api/quantpilot/review/plan-vs-actual/${date}`),
      ]);
      renderReviewPlan(plan);
      renderReviewCompare(compare.rows);
    } catch (err) { toast(`复盘中心加载失败: ${escapeHtml(err.message)}`); }
    await marketTask;
  };

  const loadReviewLegs = async () => {
    try { renderReviewLegs(await api('/api/quantpilot/review/legs-nav')); }
    catch (err) { toast(`分腿曲线加载失败: ${escapeHtml(err.message)}`); }
  };

  const loadReviewReport = async period => {
    try { renderReviewAttribution(await api(`/api/quantpilot/review/attribution?period=${period}`)); }
    catch (err) { toast(`归因报表加载失败: ${escapeHtml(err.message)}`); }
  };

  const renderLegPerformanceCards = async () => {
    try {
      const [nav, attr] = await Promise.all([
        api('/api/quantpilot/review/legs-nav'),
        api('/api/quantpilot/review/attribution?period=all'),
      ]);
      const byLeg = {};
      (attr.groups || []).forEach(g => {
        byLeg[g.leg] = byLeg[g.leg] || { rounds: 0, win: 0, pnl: 0 };
        byLeg[g.leg].rounds += g.rounds;
        byLeg[g.leg].win += Math.round(g.win_rate * g.rounds);
        byLeg[g.leg].pnl += g.total_pnl;
      });
      const names = { machine: '机器腿', bear: '熊市池腿', basket: '红利篮子腿' };
      $('#legPerformanceCards').innerHTML = ['machine', 'bear', 'basket'].map(leg => {
        const s = byLeg[leg] || { rounds: 0, win: 0, pnl: 0 };
        const last = (nav.pnl[leg] || []).slice(-1)[0] || 0;
        const winRate = s.rounds ? s.win / s.rounds : null;
        return `<label class="strategy-row" style="grid-template-columns: minmax(0, 1fr) auto;" data-leg-jump="${leg}"><div><div class="strategy-name">${names[leg]}</div><div class="strategy-meta"><span>${s.rounds} 回合</span><span>胜率 ${winRate == null ? '--' : fmtPct(winRate)}</span></div><div class="progress"><span style="width:${winRate == null ? 0 : Math.round(winRate * 100)}%"></span></div></div><div class="strategy-return ${last >= 0 ? 'up' : 'down'}">${last >= 0 ? '+' : ''}${fmtMoney(last)}<small>累计盈亏</small></div></label>`;
      }).join('');
    } catch (err) {
      $('#legPerformanceCards').innerHTML = `<div class="strategy-row" style="grid-template-columns: minmax(0, 1fr) auto;"><div><div class="strategy-name">加载失败</div><div class="strategy-meta">${escapeHtml(err.message)}</div></div></div>`;
    }
  };

  document.addEventListener('DOMContentLoaded', () => {
    resetBacktestView();
    bindActions();
    checkStartupKlineData();
    loadData();
    renderLegPerformanceCards();
    window.setInterval(loadWatchlist, 30000);
    window.setInterval(loadExternalEnvironment, 30000);
    // 腿卡自动刷新:配合桌面镜像定时同步,页面长开也能看到当日最新分腿数字。
    window.setInterval(renderLegPerformanceCards, 600000);
  });
})();
