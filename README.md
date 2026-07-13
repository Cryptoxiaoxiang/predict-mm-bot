# Predict.fun 自动做市机器人

这是一个为 Predict.fun 设计的自动挂单、撤单和重新报价工具。它会按设定的策略管理订单，并提供仓位与订单数量限制。

> 本工具不能保证订单永远不会成交。任何放在订单簿上的订单都有成交风险。第一次使用请尝试模拟运行，并从很小的金额开始。

## 开始前

你需要：

- Predictfun 注册链接，可以获得10%手续费折扣: https://predict.fun?ref=5BA3F
- 一台已安装 Python 3.11 或更高版本的电脑；
- 想要运行的 Predict.fun 市场的 `Market ID`；
- 只有准备真实交易时，才需要 Predict.fun API Key 和钱包私钥；网页会自动生成 JWT Token。

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

如果执行 `python3 -m venv .venv` 时提示缺少 `ensurepip` 或虚拟环境组件，请安装与当前 Python **相同版本**的 venv 包后重试。例如服务器显示的是 Python 3.12：

```bash
sudo apt update
sudo apt install -y python3.12-venv
```

Python 3.11 则对应使用 `python3.11-venv`。不要在没有确认 Python 版本的情况下盲目复制版本号。

## 在服务器上部署网页控制台（推荐）

登录服务器后，先下载项目并进入目录：

```bash
git clone https://github.com/Cryptoxiaoxiang/predict-mm-bot.git
cd predict-mm-bot
```

### 如果 `git clone` 失败

部分 VPS 网络会把公开仓库的克隆错误地显示为“仓库不存在”或出现 HTTPS 连接问题。这不一定是仓库私有、账号或 Token 的问题。可以改用 GitHub 的官方源码下载地址：

```bash
cd /root
curl -L https://codeload.github.com/Cryptoxiaoxiang/predict-mm-bot/tar.gz/refs/heads/main -o predict-mm-bot.tar.gz
tar -xzf predict-mm-bot.tar.gz
mv predict-mm-bot-main predict-mm-bot
cd predict-mm-bot
```

仓库是公开的，以上方式不需要输入 GitHub 密码或 Token。以后更新代码时，重复下载并解压到一个临时目录，再只覆盖程序文件；不要覆盖 `.env` 和 `config.toml`。

安装依赖并启动网页控制台：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m predict_mm.web
```

如果网页运行在远程服务器上，网页不会直接暴露到公网。请在自己的电脑终端建立 SSH 隧道：
在终端里输入:

```bash
ssh -L 8080:127.0.0.1:8080 用户名@服务器IP
```

然后将终端保持在打开状态,在自己电脑浏览器打开 `http://127.0.0.1:8080`。

确认网页能启动后，按 `Ctrl+C` 停止前台服务。若希望退出 SSH 后网页仍持续运行：

```bash
mkdir -p logs
nohup .venv/bin/python -m predict_mm.web > logs/web-console.log 2>&1 &
```

执行后可以关闭本地终端和 SSH 连接，网页控制台与已启动的机器人仍会在 VPS 上继续运行。查看后台日志：

```bash
tail -f logs/web-console.log
```

确认服务是否真的启动成功：

```bash
curl --max-time 5 -o /dev/null -s -w '%{http_code}\n' http://127.0.0.1:8080
```

返回 `200` 代表网页控制台可用；如果返回 `000`，请查看上一条日志命令的错误信息。

第一次打开网页时，先在“账户设置”填写 API Key 等账户信息并点“保存账户设置”；再填写第一个市场的 Market ID、交易方向和风险限制，需要更多市场时点“添加市场”，最后点“保存配置”。机器人会生成两个只保存在服务器上的配置文件：

- `.env`：账户和登录信息；
- `config.toml`：市场、挂单数量与风险限制。

这两个保存按钮互不影响：保存账户设置只修改 `.env`，保存市场配置只修改 `config.toml`。为保护密钥，网页重新打开时不会显示已保存的 API Key、JWT 或私钥；上方会显示“已保存”状态，留空后再次保存也不会清除已有密钥。

默认关闭 `dry_run`，保存后会处于实盘模式；首次保存前请确认账户信息、市场和数量。保存后可在网页上点“启动机器人”；点“停止并撤单”可安全停止。



## 命令行方式

不使用网页时，仍可通过命令行首次配置并直接启动：

```bash
python -m predict_mm.main --config config.toml
```

## 网页配置怎么填

第一次测试时，推荐这样填写：

- `API Key`：可暂时留空；
- `钱包 Private Key`：使用 EOA 实盘时填写。保存账户设置时，网页会用它在本机/VPS 内存中签署官方动态 message，并自动获取 JWT；私钥不会发送给 Predict.fun 或写入日志；
- `Predict Account Address`：留空；
- `是否使用 dry-run 模拟运行`：默认不勾选；勾选后才不会真实下单。
- `Market ID`：每个市场都必填，填入要运行的 Predict.fun 市场 ID；
- `交易 outcome`：每个市场分别选择 `YES` 或 `NO`；
- `单次挂单数量`：每个市场分别设置。首次建议使用很小的数字，例如 `1`；
- `单市场最大仓位`、`总最大仓位`：所有市场共用的风险上限，首次可设为 `2`、`5`。
- `买单被吃单后，立即以 0.01 紧急卖出`：默认勾选。买单被吃单（包括部分成交）时，机器人会撤掉该市场其余订单、停止继续报价，并以 `0.01` 的非 post-only 卖单卖出已成交数量。这可能会造成损失。

`PREDICT_ACCOUNT_ADDRESS` 是公开的钱包/交易账户地址，不是私钥。只有真实挂单时才需要填写，并且必须与私钥对应。

## 什么是模拟运行（dry-run）

`dry_run = true` 是模拟运行：机器人会计算报价、输出日志，并模拟订单的创建和撤销，不会真实下单。

`dry_run = false` 才是实盘运行：机器人会提交真实订单，并在设定时间后撤单或重新挂单。

建议先以模拟运行观察几个小时，确认市场、挂单数量和撤单节奏都符合预期，再切换实盘。

## 切换到实盘

确认模拟运行没有问题后：

1. 在网页账户设置填入 API Key 和 EOA 钱包 Private Key；保存时会自动生成 JWT；
2. 在 `config.toml` 将 `dry_run = true` 改为 `dry_run = false`；
3. 保持很小的单次挂单数量和仓位上限；
4. 启动后先到 Predict.fun 页面确认订单和撤单行为是否符合预期。

不需要填写 API Secret；Predict.fun 当前不要求该字段。

初次实盘时，如果程序提示缺少市场信息，请按提示补充市场的 `token_id` 等信息；通常机器人会自动尝试获取这些数据。

## 常用调整

在 `config.toml` 中：

- 每个 `[[markets]]` 中的 `quote_size`：该市场每次挂单的数量；
- `cancel_after_seconds`：订单保留多少秒后撤销；
- 当盘口价格逼近某笔挂单至只剩 1 个 tick 时，机器人会在下一次轮询（默认最多 2 秒）撤单，并按距离最新盘口 2 个 tick 的规则重新报价；
- 每个市场的价格 tick 会由 API 返回的 `decimalPrecision` 自动识别；例如精度为 `2` 时，机器人按 `0.01` 价格档位报价；
- `max_position_per_market`：单个市场允许的最大仓位；
- `max_total_position`：所有市场合计允许的最大仓位；
- `dry_run`：是否处于模拟运行。
- `emergency_exit_on_buy_fill`：买单被吃单后是否以 `0.01` 紧急卖出；默认 `true`。

配置修改后，停止机器人再重新启动即可生效。

## 部署排错

### 启动时出现 `/opt/homebrew/.../shellenv.sh` 和 `SyntaxError`

如果错误显示类似下面的内容，并且位置是某个 `.py` 文件的第 1 行：

```text
/opt/homebrew/Library/Homebrew/cmd/shellenv.sh: line 9: /bin/ps: Operation not permitted
SyntaxError: invalid syntax
```

这说明代码文件被一行本机终端提示文字污染了，并非 VPS、钱包或 Predict.fun API 的问题。请从 GitHub 重新下载干净源码后，仅覆盖服务器项目中的 `predict_mm/` 目录：

```bash
cd /root
mkdir -p predict-mm-repair
curl -L https://codeload.github.com/Cryptoxiaoxiang/predict-mm-bot/tar.gz/refs/heads/main -o predict-mm-repair/repo.tar.gz
tar -xzf predict-mm-repair/repo.tar.gz -C predict-mm-repair
cp -r predict-mm-repair/predict-mm-bot-main/predict_mm/. predict-mm-bot/predict_mm/
```

然后重新执行后台启动命令。维护代码时，不要把终端的完整输出直接复制到 Python 文件；只复制源代码本身。

### 网页没法打开

按顺序检查：

1. VPS 上的 `curl` 检查是否返回 `200`；
2. 本机 SSH 隧道是否仍在运行；
3. 浏览器访问的是否为 `http://127.0.0.1:8080`，而不是 VPS 的公网 IP。

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
