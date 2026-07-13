const form = document.querySelector('#setup-form');
const accountForm = document.querySelector('#account-form');
const notice = document.querySelector('#notice');
const marketsList = document.querySelector('#markets-list');
const marketTemplate = document.querySelector('#market-template');
let formDirty = false;

function showNotice(message, kind = 'success') {
  notice.hidden = false;
  notice.className = `notice ${kind}`;
  notice.textContent = message;
}

function isPredictMarketUrl(value) {
  try {
    const url = new URL(value);
    if (!['predict.fun', 'www.predict.fun'].includes(url.hostname.toLowerCase())) return false;
    const parts = url.pathname.split('/').filter(Boolean);
    const marketIndex = parts.indexOf('market');
    return marketIndex >= 0 && Boolean(parts[marketIndex + 1]);
  } catch (_) {
    return false;
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.text();
  let data = {};
  try {
    data = body ? JSON.parse(body) : {};
  } catch (_) {
    if (!response.ok) throw new Error(`服务器错误（${response.status}），请查看运行日志。`);
    throw new Error('服务器返回了无法识别的数据。');
  }
  if (!response.ok) throw new Error(data.detail || `操作失败（${response.status}），请查看日志。`);
  return data;
}

function setField(name, value, target = form) {
  const field = target.elements.namedItem(name);
  if (field && value !== undefined && value !== null) field.value = value;
}

function setOutcomeOptions(row, outcomes, selected = '') {
  const select = row.querySelector('[data-field="outcome"]');
  const canonicalOutcome = (value) => {
    const text = String(value || '').trim();
    const normalized = text.toUpperCase();
    if (normalized === 'YES') return 'Yes';
    if (normalized === 'NO') return 'No';
    if (['YES_NO', 'YES&NO', 'YES AND NO'].includes(normalized)) return 'YES_NO';
    return text;
  };
  const values = [...new Set((outcomes || []).map(canonicalOutcome).filter(Boolean))];
  if (!values.length) values.push('Yes', 'No');
  const hasYes = values.includes('Yes');
  const hasNo = values.includes('No');
  if (hasYes && hasNo && !values.includes('YES_NO')) values.push('YES_NO');
  const selectedValue = canonicalOutcome(selected);
  if (selectedValue && !values.includes(selectedValue)) values.push(selectedValue);
  select.replaceChildren(...values.map((value) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value === 'YES_NO' ? 'Yes & No（双向）' : value;
    return option;
  }));
  select.value = selectedValue && values.includes(selectedValue) ? selectedValue : values[0];
}

function renumberMarkets() {
  [...marketsList.querySelectorAll('.market-row')].forEach((row, index) => {
    row.querySelector('.market-title').textContent = `市场 ${index + 1}`;
    row.querySelector('.remove-market').hidden = index === 0 && marketsList.children.length === 1;
  });
}

function addMarket(market = {}) {
  const row = marketTemplate.content.firstElementChild.cloneNode(true);
  const marketIdInput = row.querySelector('[data-field="market_id"]');
  marketIdInput.value = market.market_id || '';
  setOutcomeOptions(row, market.outcomes || ['YES', 'NO'], market.outcome || 'YES');
  row.querySelector('[data-field="quote_size"]').value = market.quote_size || '1.0';
  row.querySelector('.remove-market').addEventListener('click', () => {
    if (marketsList.children.length === 1) return;
    row.remove();
    renumberMarkets();
    formDirty = true;
  });
  const resolveButton = row.querySelector('.resolve-market');
  resolveButton.addEventListener('click', () => resolveMarketUrl(row));
  marketIdInput.addEventListener('change', () => {
    if (isPredictMarketUrl(marketIdInput.value.trim())) resolveMarketUrl(row);
  });
  marketsList.append(row);
  renumberMarkets();
}

function renderMarketLookup(row, result) {
  const container = row.querySelector('[data-field="market_lookup"]');
  container.replaceChildren();
  container.hidden = false;
  const message = document.createElement('p');
  message.textContent = result.message;
  container.append(message);
  (result.matches || []).forEach((market) => {
    const group = document.createElement('div');
    group.className = 'market-match-group';
    const title = document.createElement('p');
    const category = market.category_title ? `${market.category_title} · ` : '';
    title.textContent = `${category}${market.question} · ID ${market.id}${market.trading_status ? ` · ${market.trading_status}` : ''}`;
    group.append(title);
    const outcomes = market.outcomes?.length ? market.outcomes : ['YES', 'NO'];
    outcomes.forEach((outcome) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'market-match';
      button.textContent = `选择「${outcome}」挂单`;
      button.addEventListener('click', () => {
        row.querySelector('[data-field="market_id"]').value = market.id;
        setOutcomeOptions(row, outcomes, outcome);
        container.hidden = true;
        formDirty = true;
        showNotice(`已选择 ${market.question} · ${outcome}（Market ID：${market.id}）`);
      });
      group.append(button);
    });
    const hasYes = outcomes.some((outcome) => outcome.toUpperCase() === 'YES');
    const hasNo = outcomes.some((outcome) => outcome.toUpperCase() === 'NO');
    if (hasYes && hasNo) {
      const bothButton = document.createElement('button');
      bothButton.type = 'button';
      bothButton.className = 'market-match';
      bothButton.textContent = '选择「Yes & No」双向挂单';
      bothButton.addEventListener('click', () => {
        row.querySelector('[data-field="market_id"]').value = market.id;
        setOutcomeOptions(row, outcomes, 'YES_NO');
        container.hidden = true;
        formDirty = true;
        showNotice(`已选择 ${market.question} · Yes & No 双向挂单（Market ID：${market.id}）`);
      });
      group.append(bothButton);
    }
    container.append(group);
  });
}

async function resolveMarketUrl(row) {
  const input = row.querySelector('[data-field="market_id"]');
  const value = input.value.trim();
  if (!isPredictMarketUrl(value)) {
    showNotice('请先粘贴完整的 Predict.fun 市场网址。数字 Market ID 无需识别。', 'error');
    return;
  }
  const button = row.querySelector('.resolve-market');
  button.disabled = true;
  button.textContent = '识别中…';
  try {
    const result = await request('/api/resolve-market', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({market_url: value}),
    });
    if (result.market_id) {
      input.value = result.market_id;
      formDirty = true;
      showNotice(result.message);
    } else {
      renderMarketLookup(row, result);
    }
  } catch (error) {
    showNotice(error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = '识别网址';
  }
}

function renderMarkets(markets = []) {
  marketsList.replaceChildren();
  (markets.length ? markets : [{}]).forEach(addMarket);
}

function collectMarkets() {
  return [...marketsList.querySelectorAll('.market-row')].map((row) => ({
    market_id: row.querySelector('[data-field="market_id"]').value.trim(),
    outcome: row.querySelector('[data-field="outcome"]').value,
    quote_size: row.querySelector('[data-field="quote_size"]').value.trim(),
  }));
}

function renderOpenOrders(orders = []) {
  const list = document.querySelector('#open-orders-list');
  const summary = document.querySelector('#open-orders-summary');
  list.replaceChildren();
  summary.textContent = orders.length ? `${orders.length} 笔挂单` : '暂无挂单';
  if (!orders.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-state';
    empty.textContent = '机器人当前没有管理中的挂单。';
    list.append(empty);
    return;
  }
  orders.forEach((order) => {
    const row = document.createElement('article');
    row.className = 'open-order-row';
    const name = document.createElement('strong');
    const side = order.side === 'buy' ? '买入' : '卖出';
    name.textContent = `市场 ${order.market_id} · ${side} ${order.outcome}`;
    const details = document.createElement('span');
    const labels = [`价格 ${order.price}`, `数量 ${order.size}`, `订单 ${order.order_id}`];
    if (order.is_emergency_exit) labels.push('紧急卖单');
    details.textContent = labels.join(' · ');
    if (order.is_emergency_exit) details.className = 'emergency';
    row.append(name, details);
    list.append(row);
  });
}

function formatBalance(value) {
  const balance = Number(value);
  if (!Number.isFinite(balance)) return String(value);
  return balance.toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 4});
}

async function refreshBalance() {
  const value = document.querySelector('#balance-value');
  const note = document.querySelector('#balance-note');
  try {
    const balance = await request('/api/balance');
    value.textContent = `${formatBalance(balance.balance)} ${balance.asset}`;
    note.textContent = '链上余额 · 每 30 秒刷新';
    value.title = balance.account_address;
  } catch (error) {
    value.textContent = '余额不可用';
    note.textContent = error.message;
    value.removeAttribute('title');
  }
}

async function refreshStatus() {
  try {
    const status = await request('/api/status');
    const mode = status.dry_run ? '模拟运行' : '实盘模式';
    const badge = document.querySelector('#mode-badge');
    badge.textContent = mode;
    badge.className = `badge ${status.dry_run ? 'safe' : 'live'}`;
    document.querySelector('#run-status').textContent = status.running ? '运行中' : (status.configured ? '已停止' : '等待配置');
    const markets = status.markets || [];
    document.querySelector('#market-value').textContent = markets.length ? `${markets.length} 个市场` : '—';
    renderOpenOrders(status.open_orders || []);
    document.querySelector('#start-button').disabled = !status.configured || status.running;
    document.querySelector('#stop-button').disabled = !status.running;
    document.querySelector('#cancel-button').disabled = !status.configured;
    const isEditing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName);
    if (!formDirty && !isEditing) {
      if (markets.length) renderMarkets(markets);
      setField('cancel_after_seconds', status.cancel_after_seconds);
      setField('max_position_per_market', status.max_position_per_market);
      setField('max_total_position', status.max_total_position);
      form.elements.namedItem('dry_run').checked = status.dry_run;
      form.elements.namedItem('emergency_exit_on_buy_fill').checked = status.emergency_exit_on_buy_fill;
    }
    if (!isEditing) {
      setField('predict_account_address', status.account_address, accountForm);
      setField('log_level', status.log_level, accountForm);
    }
    document.querySelector('#secret-status').textContent = [
      status.api_key_set && 'API Key 已保存',
      status.jwt_token_set && 'JWT 已保存',
      status.private_key_set && '私钥已保存',
    ].filter(Boolean).join(' · ') || '尚未保存实盘密钥';
    if (status.last_error) showNotice(`机器人停止：${status.last_error}`, 'error');
  } catch (error) {
    showNotice(error.message, 'error');
  }
}

async function refreshLogs() {
  try {
    const { lines } = await request('/api/logs');
    const logs = document.querySelector('#logs');
    logs.textContent = lines.length ? lines.join('\n') : '暂无运行日志。';
    logs.scrollTop = logs.scrollHeight;
  } catch (error) {
    showNotice(error.message, 'error');
  }
}

document.querySelector('#add-market-button').addEventListener('click', () => {
  addMarket();
  formDirty = true;
});
form.addEventListener('input', () => { formDirty = true; });
form.addEventListener('change', () => { formDirty = true; });
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(form));
  values.markets = collectMarkets();
  values.dry_run = form.elements.namedItem('dry_run').checked;
  values.emergency_exit_on_buy_fill = form.elements.namedItem('emergency_exit_on_buy_fill').checked;
  try {
    const result = await request('/api/setup', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values),
    });
    formDirty = false;
    showNotice(result.message);
    await refreshStatus();
  } catch (error) {
    showNotice(error.message, 'error');
  }
});

accountForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(accountForm));
  try {
    const result = await request('/api/account', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values),
    });
    ['api_key', 'private_key'].forEach((name) => {
      accountForm.elements.namedItem(name).value = '';
    });
    showNotice(result.message);
    await refreshStatus();
  } catch (error) {
    showNotice(error.message, 'error');
  }
});

document.querySelector('#start-button').addEventListener('click', async () => {
  try { showNotice((await request('/api/start', {method: 'POST'})).message); await refreshStatus(); } catch (error) { showNotice(error.message, 'error'); }
});
document.querySelector('#stop-button').addEventListener('click', async () => {
  try { showNotice((await request('/api/stop', {method: 'POST'})).message); await refreshStatus(); } catch (error) { showNotice(error.message, 'error'); }
});
document.querySelector('#cancel-button').addEventListener('click', async () => {
  if (!confirm('确定要撤销所有已配置市场的订单吗？')) return;
  try { showNotice((await request('/api/cancel-all', {method: 'POST'})).message); } catch (error) { showNotice(error.message, 'error'); }
});
document.querySelector('#refresh-logs').addEventListener('click', refreshLogs);

renderMarkets();
refreshStatus();
refreshLogs();
refreshBalance();
setInterval(() => { refreshStatus(); refreshLogs(); }, 2000);
setInterval(refreshBalance, 30000);