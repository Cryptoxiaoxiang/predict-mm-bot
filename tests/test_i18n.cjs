const assert = require('node:assert/strict');
const {test} = require('node:test');
require('../predict_mm/web_static/i18n.js');
const i18n = globalThis.PredictI18n;

test('language switching translates statuses and durations in both directions', async () => {
  await i18n.setLanguage('en', {persist: false});
  assert.equal(i18n.translate('买单被吃单后立即卖出'), 'Sell immediately after a buy fill');
  assert.equal(i18n.translate('1小时 2分 3秒'), '1h 2m 3s');
  assert.equal(i18n.translate('每 2 秒刷新 · 80 笔'), 'Updates every 2 seconds · 80 orders');
  await i18n.setLanguage('zh', {persist: false});
  assert.equal(i18n.translate('买单被吃单后立即卖出'), '买单被吃单后立即卖出');
  assert.equal(i18n.translate('1小时 2分 3秒'), '1小时 2分 3秒');
});

test('market and outcome names keep their identity inside translated labels', async () => {
  await i18n.setLanguage('en', {persist: false});
  assert.equal(i18n.translate('已选择：模拟运行 · 跌'), 'Selected: 模拟运行 · 跌');
  assert.equal(i18n.translate('选择「市场」挂单'), 'Quote 市场');
  assert.equal(i18n.translate('跌 · 订单 123'), '跌 · Order 123');
  assert.equal(i18n.translate('Market ID：1511369'), 'Market ID: 1511369');
});

test('API and account notices are translated while technical details remain intact', async () => {
  await i18n.setLanguage('en', {persist: false});
  assert.equal(i18n.translate('机器人停止：Predict.fun 拒绝了 GET /v1/orders 请求（HTTP 401）：authorization error'),
    'Bot stopped: Predict.fun rejected GET /v1/orders (HTTP 401): authorization error');
  assert.equal(i18n.translate('账户设置已保存。 已使用 EOA 钱包本地签名并自动生成 JWT。'),
    'Account settings saved. JWT generated using a local EOA wallet signature.');
  assert.equal(i18n.translate('市场 2 → 单次挂单数量：必须是整数'), 'Market 2 → Order size (shares): Must be an integer');
});
