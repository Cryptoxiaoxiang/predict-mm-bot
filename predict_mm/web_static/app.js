/opt/homebrew/Library/Homebrew/cmd/shellenv.sh: line 9: /bin/ps: Operation not permitted
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

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '操作失败，请查看日志。');
  return data;
}

function setField(name, value, target = form) {
  const field = target.elements.namedItem(name);
  if (field && value !== undefined && value !== null) field.value = value;
}

function renumberMarkets() {
  [...marketsList.querySelectorAll('.market-row')].forEach((row, index) => {
    row.querySelector('.market-title').textContent = `市场 ${index + 1}`;
    row.querySelector('.remove-market').hidden = index === 0 && marketsList.children.length === 1;
  });
}

function addMarket(market = {}) {
  const row = marketTemplate.content.firstElementChild.cloneNode(true);
  row.querySelector('[data-field="market_id"]').value = market.market_id || '';
  row.querySelector('[data-field="outcome"]').value = market.outcome || 'YES';
  row.querySelector('[data-field="quote_size"]').value = market.quote_size || '1.0';
  row.querySelector('.remove-market').addEventListener('click', () => {
    if (marketsList.children.length === 1) return;
    row.remove();
    renumberMarkets();
    formDirty = true;
  });
  marketsList.append(row);
  renumberMarkets();
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

function renderOpenOrders(markets = []) {
  const list = document.querySelector('#open-orders-list');
  const summary = document.querySelector('#open-orders-summary');
  list.replaceChildren();
  const total = markets.reduce((count, market) => count + market.buy_orders + market.sell_orders, 0);
  summary.textContent = total ? `${markets.length} 个市场 · ${total} 笔挂单` : '暂无挂单';
  if (!markets.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-state';
    empty.textContent = '机器人当前没有管理中的挂单。';
    list.append(empty);
    return;
  }
  markets.forEach((market) => {
    const row = document.createElement('article');
    row.className = 'open-order-row';
    const name = document.createElement('strong');
    name.textContent = `市场 ${market.market_id} · ${market.outcome}`;
    const details = document.createElement('span');
    const labels = [`买单 ${market.buy_orders} 笔`, `卖单 ${market.sell_orders} 笔`];
    if (market.emergency_exit_orders) labels.push(`紧急卖单 ${market.emergency_exit_orders} 笔`);
    details.textContent = labels.join(' · ');
    if (market.emergency_exit_orders) details.className = 'emergency';
    row.append(name, details);
    list.append(row);
  });
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
    document.querySelector('#outcome-value').textContent = markets.length ? markets.map((market) => market.outcome).join(' / ') : '—';
    document.querySelector('#size-value').textContent = markets.length ? markets.map((market) => market.quote_size).join(' / ') : '—';
    renderOpenOrders(status.open_order_markets || []);
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
setInterval(() => { refreshStatus(); refreshLogs(); }, 2000);
