const form = document.querySelector('#setup-form');
const notice = document.querySelector('#notice');

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

function setField(name, value) {
  const field = form.elements.namedItem(name);
  if (field && value !== undefined && value !== null) field.value = value;
}

async function refreshStatus() {
  try {
    const status = await request('/api/status');
    const mode = status.dry_run ? '模拟运行' : '实盘模式';
    const badge = document.querySelector('#mode-badge');
    badge.textContent = mode;
    badge.className = `badge ${status.dry_run ? 'safe' : 'live'}`;
    document.querySelector('#run-status').textContent = status.running ? '运行中' : (status.configured ? '已停止' : '等待配置');
    document.querySelector('#market-value').textContent = status.market_id || '—';
    document.querySelector('#outcome-value').textContent = status.outcome || '—';
    document.querySelector('#size-value').textContent = status.quote_size || '—';
    document.querySelector('#start-button').disabled = !status.configured || status.running;
    document.querySelector('#stop-button').disabled = !status.running;
    document.querySelector('#cancel-button').disabled = !status.configured;
    if (!document.activeElement || document.activeElement.tagName !== 'INPUT') {
      setField('market_id', status.market_id);
      setField('outcome', status.outcome);
      setField('quote_size', status.quote_size);
      setField('cancel_after_seconds', status.cancel_after_seconds);
      setField('max_position_per_market', status.max_position_per_market);
      setField('max_total_position', status.max_total_position);
      form.elements.namedItem('dry_run').checked = status.dry_run;
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

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(form));
  values.dry_run = form.elements.namedItem('dry_run').checked;
  values.is_neg_risk = form.elements.namedItem('is_neg_risk').checked;
  values.is_yield_bearing = form.elements.namedItem('is_yield_bearing').checked;
  values.api_base_url = 'https://api.predict.fun';
  try {
    const result = await request('/api/setup', {
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
  if (!confirm('确定要撤销当前配置市场的所有订单吗？')) return;
  try { showNotice((await request('/api/cancel-all', {method: 'POST'})).message); } catch (error) { showNotice(error.message, 'error'); }
});
document.querySelector('#refresh-logs').addEventListener('click', refreshLogs);

refreshStatus();
refreshLogs();
setInterval(() => { refreshStatus(); refreshLogs(); }, 2000);
