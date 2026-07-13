# Predict.fun Market Maker Bot

一个面向 Predict.fun / 预测市场 CLOB 的自动挂单撤单机器人骨架。

默认是 `dry_run: true`，只会模拟报价、打印计划，不会真实下单。请先用小资金和测试市场验证接口、最小下单量、tick size、撤单行为，再开启实盘。

## 什么是 dry-run / 模拟运行？

`dry-run` 可以理解成“模拟运行”或“安全模式”。

- `dry_run = true`：机器人只会读取配置、计算报价、打印日志，并在程序内部模拟“创建订单 / 撤销订单”；不会真的向 Predict.fun 发起下单请求。
- `dry_run = false`：机器人会尝试调用真实 API 下单和撤单，这才是实盘模式。

建议先保持 `dry_run = true` 跑一段时间，确认挂单价格、撤单节奏、风控限制都正常，再考虑切换到实盘。即使进入实盘，也应该先用很小的 `quote_size` 和仓位上限测试。

## 功能

- 多市场轮询
- 挂单后自动撤单并重挂
- 尽量只做 maker，不主动吃单
- 最大订单、最大库存、最大总敞口限制
- 启动 / 退出时可自动撤销开放订单
- dry-run 模拟模式
- Docker 部署
- 日志输出

## 快速开始

```bash
cp .env.example .env
cp config.example.toml config.toml
python -m venv .venv
source .venv/bin/activate
pip install -e .
python -m predict_mm.main --config config.toml
```

## Docker

```bash
cp .env.example .env
cp config.example.toml config.toml
docker compose up -d --build
docker compose logs -f
```

## 配置

编辑 `config.toml`：

```toml
dry_run = true

[[markets]]
id = "example-market-id"
enabled = true
```

编辑 `.env`：

```env
PREDICT_API_BASE_URL=https://api.predict.fun
PREDICT_API_KEY=
# 可选：实盘创建 / 撤销订单等钱包相关操作需要 JWT Token
PREDICT_JWT_TOKEN=
```

真实密钥不要提交到 GitHub。项目已在 `.gitignore` 中排除 `.env` 和 `config.toml`。

Predict.fun 官方文档当前说明：Mainnet 请求需要 API Key；创建订单、查看个人订单这类钱包相关操作还需要 JWT Token。文档没有要求 API Secret，所以本项目不再保留 API Secret 字段。

## 安全提醒

这不是“保证不成交”的系统。任何挂在盘口上的 maker 订单都有被成交的可能。开启实盘前建议：

1. 先 dry-run 至少几个小时；
2. 单笔数量设为极小；
3. 每个市场库存上限设小；
4. 确认程序异常退出时会撤单；
5. 手动检查交易所页面上的开放订单。

## 项目结构

```text
predict_mm/
  main.py          # 入口
  config.py        # 配置加载
  models.py        # 数据结构
  client.py        # Predict.fun API 适配层
  strategy.py      # 报价策略
  risk.py          # 风控
  engine.py        # 主循环
  logging.py       # 日志
tests/
```

## 下一步

拿到 Predict.fun 官方 SDK 或当前 API 文档后，主要需要完善 `predict_mm/client.py` 里的真实接口方法：

- `get_orderbook`
- `get_positions`
- `create_order`
- `cancel_order`
- `cancel_all_orders`
