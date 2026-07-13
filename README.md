# Predict.fun 自动挂单机器人

这是一个为 Predict.fun 设计的自动挂单、撤单和重新报价工具。它会按设定的节奏管理订单，并提供仓位与订单数量限制。

> 本工具不能保证订单永远不会成交。任何放在订单簿上的订单都有成交风险。第一次使用请保持模拟运行，并从很小的金额开始。

## 开始前

你需要：

- 一台已安装 Python 3.11 或更高版本的电脑；
- 想要运行的 Predict.fun 市场的 `Market ID`；
- 只有准备真实交易时，才需要 Predict.fun API Key、JWT Token 和钱包私钥。

模拟运行不需要填写钱包私钥，也不会发送真实订单。

## 服务器准备（首次）

建议选择 Ubuntu 24.04 服务器。登录服务器后，先检查 Python 是否已经安装：

```bash
python3 --version
```

显示 `Python 3.11` 或更高版本即可继续下一步。如果提示找不到命令，请安装 Python、虚拟环境工具和 Git：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version
```

如果最后显示的版本低于 3.11，请改用 Ubuntu 24.04 服务器，或先将服务器的 Python 升级到 3.11 以上再继续。

## 在服务器上部署网页控制台（推荐）

登录服务器后，先下载项目并进入目录：

```bash
git clone https://github.com/Cryptoxiaoxiang/predict-mm-bot.git
cd predict-mm-bot
```

安装依赖并启动网页控制台：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m predict_mm.web
```

在同一台电脑的浏览器打开：

```text
http://127.0.0.1:8080
```

第一次打开网页时，先填写 Market ID、交易方向和风险限制，再点“保存配置”。网页会生成两个只保存在服务器上的配置文件：

- `.env`：账户和登录信息；
- `config.toml`：市场、挂单数量与风险限制。

默认会启用 `dry_run`（模拟运行）。保存后可在网页上点“启动机器人”；点“停止并撤单”可安全停止。

如果网页运行在远程服务器上，网页不会直接暴露到公网。请在自己的电脑终端建立 SSH 隧道：

```bash
ssh -L 8080:127.0.0.1:8080 用户名@服务器IP
```

然后在自己电脑浏览器打开 `http://127.0.0.1:8080`。

如果希望退出 SSH 后网页仍持续运行：

```bash
mkdir -p logs
nohup .venv/bin/python -m predict_mm.web > logs/web-console.log 2>&1 &
```

## 命令行方式

不使用网页时，仍可通过命令行首次配置并直接启动：

```bash
python -m predict_mm.main --config config.toml
```

## 网页配置怎么填

第一次测试时，推荐这样填写：

- `API Key`：可暂时留空；
- `JWT Token`：可暂时留空；
- `钱包 Private Key`：留空；
- `Predict Account Address`：留空；
- `是否先使用 dry-run 模拟运行`：选择 `yes`；
- `Market ID`：必填，填入你要运行的 Predict.fun 市场 ID；
- `交易 outcome`：选择 `YES` 或 `NO`；
- `单次挂单数量`、`单市场最大仓位`、`总最大仓位`：先使用很小的数字，例如 `1`、`2`、`5`。

`PREDICT_ACCOUNT_ADDRESS` 是公开的钱包/交易账户地址，不是私钥。只有真实挂单时才需要填写，并且必须与私钥对应。

## 什么是模拟运行（dry-run）

`dry_run = true` 是模拟运行：机器人会计算报价、输出日志，并模拟订单的创建和撤销，不会真实下单。

`dry_run = false` 才是实盘运行：机器人会提交真实订单，并在设定时间后撤单或重新挂单。

建议先以模拟运行观察几个小时，确认市场、挂单数量和撤单节奏都符合预期，再切换实盘。

## 切换到实盘

确认模拟运行没有问题后：

1. 在 `.env` 填入 API Key、JWT Token、钱包 Private Key 和 Predict Account Address；
2. 在 `config.toml` 将 `dry_run = true` 改为 `dry_run = false`；
3. 保持很小的单次挂单数量和仓位上限；
4. 启动后先到 Predict.fun 页面确认订单和撤单行为是否符合预期。

不需要填写 API Secret；Predict.fun 当前不要求该字段。

初次实盘时，如果程序提示缺少市场信息，请按提示补充市场的 `token_id` 等信息；通常机器人会自动尝试获取这些数据。

## 常用调整

在 `config.toml` 中：

- `quote_size`：每次挂单的数量；
- `cancel_after_seconds`：订单保留多少秒后撤销；
- `max_position_per_market`：单个市场允许的最大仓位；
- `max_total_position`：所有市场合计允许的最大仓位；
- `dry_run`：是否处于模拟运行。

配置修改后，停止机器人再重新启动即可生效。

## 长时间运行（Docker）

Docker 会启动网页控制台。首次使用时，在项目目录中先创建空配置文件：

```bash
touch .env config.toml
docker compose up -d --build
```

再通过上面的 SSH 隧道在浏览器打开控制台，完成配置并启动机器人。

查看运行日志：

```bash
docker compose logs -f
```

停止机器人：

```bash
docker compose down
```

## 账户安全

- `.env` 和 `config.toml` 不会被提交到 GitHub；不要把它们发送给任何人。
- 不要在聊天、截图或公开仓库中暴露 API Key、JWT Token 或私钥。
- 程序启动和停止时会尝试撤销它管理的开放订单；即使如此，也应在每次实盘后到 Predict.fun 页面核对开放订单。

Predict.fun 的快速撤单仅将订单从订单簿移除，不等同于链上取消；如需彻底取消，请使用 Predict.fun 官方工具完成链上取消。
