const state = {
  account: 'stable',
  tab: 'overview',
  actionsPage: 1,
  positionsPage: 1,
  requestToken: 0,
};

const byId = id => document.getElementById(id);
const escapeHtml = value => String(value ?? '—').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[char]);
const money = cents => cents == null ? '—' : new Intl.NumberFormat('zh-CN', {
  style: 'currency', currency: 'CNY', maximumFractionDigits: 2,
}).format(cents / 100);
const yuan = cents => cents == null ? '—' : (cents / 100).toFixed(2) + ' 元';
const percent = value => value == null ? '—' : (Number(value) * 100).toFixed(2) + '%';
const signedPercent = value => value == null ? '—' : (value > 0 ? '+' : '') + percent(value);
const shares = value => value == null ? '—' : Number(value).toLocaleString('zh-CN') + ' 股';
const time = value => value ? new Date(value).toLocaleString('zh-CN', {
  timeZone: 'Asia/Shanghai', hour12: false, year: 'numeric', month: '2-digit',
  day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
}) : '—';
const pnlTone = value => value > 0 ? 'profit' : value < 0 ? 'loss' : '';
const statusNames = {
  watching: '观察中', triggered: '已触发', virtual_bought: '虚拟已买',
  opportunity_only: '虚拟资金不足，仅记录', expired: '已失效', blocked: '已阻断',
  funded: '预算已预留', cash_insufficient: '虚拟资金不足', held: '已有虚拟持仓',
  lot_too_small: '不足一手', open: '持仓中',
};
const reasonNames = {
  strict_crossing: '限价买入', partial_profit: '首次止盈',
  stop: '止损或退出线', expiry: '持仓到期',
};

function fact(label, value, tone = '') {
  return `<div class="fact"><span>${escapeHtml(label)}</span><strong class="${tone}">${escapeHtml(value)}</strong></div>`;
}

function empty(message) {
  return `<p class="empty">${escapeHtml(message)}</p>`;
}

function badge(value) {
  return `<span class="badge">${escapeHtml(statusNames[value] || value)}</span>`;
}

async function api(path) {
  const response = await fetch(path, {headers: {'Accept': 'application/json'}});
  if (!response.ok) throw new Error(`请求失败（${response.status}）`);
  return response.json();
}

function latestWatchPrice(watch, quotes, day) {
  const quote = quotes.find(row => row.code === watch.code && row.healthy &&
    String(row.source_time).slice(0, 10) === day);
  return quote ? yuan(quote.last_cents) : '—';
}

function renderOverview(board, quotes) {
  byId('capital').textContent = money(board.capital_cents);
  byId('available').textContent = money(board.available_budget_cents);
  byId('cash').textContent = money(board.cash_cents);
  byId('assets').textContent = money(board.total_asset_cents);
  const assetReturn = board.capital_cents > 0 ?
    board.total_asset_cents / board.capital_cents - 1 : null;
  const returnNode = byId('asset-return');
  returnNode.textContent = signedPercent(assetReturn);
  returnNode.className = 'asset-return ' + pnlTone(assetReturn);
  const missing = board.valuation_fallback_codes || [];
  const note = byId('valuation-note');
  note.hidden = missing.length === 0;
  note.textContent = missing.length ? '部分持仓缺少报价，当前本金按买入价估算。' : '';

  const watches = board.watchlist.filter(row =>
    row.status === 'watching' && row.eligibility !== 'held');
  byId('watch-date').textContent = `${board.date} · 监听 ${watches.length} 只`;
  byId('watch-list').innerHTML = watches.length ? watches.map(row => `
    <article class="data-card">
      <div class="card-heading"><div><span class="rank">${escapeHtml(row.rank)}</span><strong>${escapeHtml(row.code)}</strong></div>${badge(row.status)}</div>
      <div class="fact-grid">
        ${fact('买入限价', yuan(row.buy_limit_cents))}
        ${fact('首次止盈价', row.no_partial ? '不减仓' : yuan(row.first_target_cents))}
        ${fact('最新价', latestWatchPrice(row, quotes, board.date))}
        ${fact('单笔预算', money(row.order_budget_cents ?? board.settings.budget_cents))}
        ${fact('计划股数', shares(row.planned_shares))}
        ${fact('止盈档位', row.no_partial ? '不首次减仓' : percent(row.target_pct))}
      </div>
      <div class="card-footer">虚拟资格：${escapeHtml(statusNames[row.eligibility] || row.eligibility)}</div>
    </article>`).join('') : empty('暂无正在监听的候选股');

  byId('open-list').innerHTML = board.positions.length ? board.positions.map(row => `
    <article class="data-card">
      <div class="card-heading"><div><strong>${escapeHtml(row.code)}</strong><span class="position-id">#${escapeHtml(row.id)}</span></div>${badge(row.status)}</div>
      <div class="main-result"><span>当前盈亏</span><strong class="${pnlTone(row.current_pnl_pct)}">${escapeHtml(signedPercent(row.current_pnl_pct))}</strong></div>
      <div class="fact-grid">
        ${fact('买入时间', row.buy_time ? time(row.buy_time) : '历史记录未保存')}
        ${fact('买入价', yuan(row.entry_price_cents))}
        ${fact('剩余股数', shares(row.remaining_shares))}
        ${fact('最新价', yuan(row.latest_price_cents))}
        ${fact('首次目标', row.no_partial ? '不首次减仓' : percent(row.target_pct))}
        ${fact('初始止损', yuan(row.initial_stop_cents))}
        ${fact('移动线', row.trailing_line_cents ? yuan(row.trailing_line_cents) : '未启用')}
      </div>
    </article>`).join('') : empty('暂无当前持仓');
}

function renderActions(history) {
  byId('action-list').innerHTML = history.actions.length ? history.actions.map(row => `
    <article class="data-card">
      <div class="card-heading"><strong>${escapeHtml(row.code)}</strong><span class="side ${row.side === 'buy' ? 'buy' : 'sell'}">${row.side === 'buy' ? '买入' : '卖出'}</span></div>
      <p class="card-time">${escapeHtml(time(row.at))}</p>
      <div class="fact-grid">
        ${fact('股数', shares(row.shares))}
        ${fact('价格', yuan(row.gross_price_cents))}
        ${fact('费用', money(row.fee_cents))}
        ${fact('现金变动', money(row.cash_delta_cents))}
      </div>
      <div class="card-footer">原因：${escapeHtml(reasonNames[row.reason] || row.reason)}</div>
    </article>`).join('') : empty('暂无操作记录');
  state.actionsPage = history.page;
  byId('actions-page').textContent = `第 ${history.page} / ${history.pages} 页 · 共 ${history.total} 条`;
  document.querySelector('[data-page="actions-prev"]').disabled = history.page <= 1;
  document.querySelector('[data-page="actions-next"]').disabled = history.page >= history.pages;
}

function renderHistory(history) {
  byId('closed-list').innerHTML = history.positions.length ? history.positions.map(row => `
    <article class="data-card">
      <div class="card-heading"><strong>${escapeHtml(row.code)}</strong><span class="card-time">持仓 ${escapeHtml(row.hold_days)} 个交易日</span></div>
      <div class="main-result"><span>净盈亏</span><strong class="${pnlTone(row.closed_pnl_cents)}">${escapeHtml(money(row.closed_pnl_cents))} / ${escapeHtml(signedPercent(row.net_return_pct))}</strong></div>
      <div class="fact-grid">
        ${fact('买入时间', time(row.buy_time))}
        ${fact('买入价', yuan(row.entry_price_cents))}
      </div>
      <details class="transactions">
        <summary>查看每笔交易（${row.transactions.length}）</summary>
        <div class="transaction-list">${row.transactions.map(leg => `
          <div class="transaction">
            <div class="transaction-heading"><strong>${leg.side === 'buy' ? '买入' : '卖出 · ' + escapeHtml(reasonNames[leg.reason] || leg.reason)}</strong><span>${escapeHtml(time(leg.at))}</span></div>
            <div class="fact-grid">
              ${fact('股数', shares(leg.shares))}
              ${fact('价格', yuan(leg.gross_price_cents))}
              ${fact('费用', money(leg.fee_cents))}
              ${fact('现金变动', money(leg.cash_delta_cents))}
            </div>
          </div>`).join('')}</div>
      </details>
    </article>`).join('') : empty('暂无历史持仓');
  state.positionsPage = history.page;
  byId('positions-page').textContent = `第 ${history.page} / ${history.pages} 页 · 共 ${history.total} 笔`;
  document.querySelector('[data-page="positions-prev"]').disabled = history.page <= 1;
  document.querySelector('[data-page="positions-next"]').disabled = history.page >= history.pages;
}

async function load() {
  const token = ++state.requestToken;
  const {account, tab, actionsPage, positionsPage} = state;
  byId('refresh').disabled = true;
  byId('sync-status').textContent = '正在更新数据…';
  try {
    if (tab === 'overview') {
      const [board, quotes] = await Promise.all([
        api(`/api/virtual?account=${account}`), api('/api/quotes'),
      ]);
      if (token !== state.requestToken) return;
      renderOverview(board, quotes);
    } else if (tab === 'actions') {
      const history = await api(`/api/virtual/actions?account=${account}&page=${actionsPage}`);
      if (token !== state.requestToken) return;
      renderActions(history);
    } else {
      const history = await api(`/api/virtual/positions/history?account=${account}&page=${positionsPage}`);
      if (token !== state.requestToken) return;
      renderHistory(history);
    }
    byId('sync-status').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN', {
      timeZone: 'Asia/Shanghai', hour12: false,
    });
  } catch (error) {
    if (token !== state.requestToken) return;
    byId('sync-status').textContent = '更新失败：' + error.message + '。请点击刷新重试。';
  } finally {
    if (token === state.requestToken) byId('refresh').disabled = false;
  }
}

function showTab(tab) {
  state.tab = tab;
  document.querySelectorAll('[data-tab]').forEach(button => {
    const active = button.dataset.tab === tab;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.hidden = panel.id !== `panel-${tab}`;
  });
  load();
}

document.querySelectorAll('[data-account]').forEach(button => button.addEventListener('click', () => {
  if (button.dataset.account === state.account) return;
  state.account = button.dataset.account;
  state.actionsPage = 1;
  state.positionsPage = 1;
  document.querySelectorAll('[data-account]').forEach(item => {
    const active = item.dataset.account === state.account;
    item.classList.toggle('active', active);
    item.setAttribute('aria-pressed', String(active));
  });
  byId('watch-list').innerHTML = empty('正在获取数据…');
  byId('open-list').innerHTML = empty('正在获取数据…');
  byId('action-list').innerHTML = empty('正在获取数据…');
  byId('closed-list').innerHTML = empty('正在获取数据…');
  ['capital', 'available', 'cash', 'assets', 'asset-return', 'watch-date',
    'actions-page', 'positions-page'].forEach(id => { byId(id).textContent = '—'; });
  byId('valuation-note').hidden = true;
  showTab(state.tab);
}));

document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => {
  if (button.dataset.tab !== state.tab) showTab(button.dataset.tab);
}));

document.querySelectorAll('[data-page]').forEach(button => button.addEventListener('click', () => {
  if (button.disabled) return;
  const [kind, direction] = button.dataset.page.split('-');
  if (kind === 'actions') state.actionsPage += direction === 'next' ? 1 : -1;
  else state.positionsPage += direction === 'next' ? 1 : -1;
  load();
  window.scrollTo({top: 0, behavior: 'smooth'});
}));

byId('refresh').addEventListener('click', load);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && state.tab === 'overview') load();
});
setInterval(() => {
  if (!document.hidden && state.tab === 'overview' && !byId('refresh').disabled) load();
}, 10000);
load();
