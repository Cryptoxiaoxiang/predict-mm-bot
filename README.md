# Predict.fun 自动做市机器人

这是一个为 Predict.fun 设计的自动挂单、撤单和重新报价工具。它会按设定的策略管理订单，并提供仓位与订单数量限制。

> 本工具不能保证订单永远不会成交。任何放在订单簿上的订单都有成交风险。第一次使用请尝试模拟运行，并从很小的金额开始。

## 开始前

你需要：

- Predictfun 注册链接，可以获得10%手续费折扣: https://predict.fun?ref=5BA3F ，为了安全最好使用一个新钱包注册使用。
- 一台已安装 Python 3.11 或更高版本的电脑；
- 只有准备真实交易时，才需要 Predict.fun API Key 和钱包私钥；网页会自动生成 JWT Token。

模拟运行不需要填写钱包私钥，也不会发送真实订单。

## 一条命令安装到服务器（推荐）

建议使用 Ubuntu 24.04 云服务器。通过 SSH 登录服务器后，复制并执行下面这一条命令：

```bash
curl -fsSL https://codeload.github.com/Cryptoxiaoxiang/predict-mm-bot/tar.gz/refs/heads/main | tar -xzO predict-mm-bot-main/install.sh > /tmp/predict-mm-install.sh && sudo bash /tmp/predict-mm-install.sh
```

如果当前登录的就是 `root` 用户，也可以去掉命令中的 `sudo`。脚本会自动完成：

- 安装 Python、虚拟环境和下载工具；
- 下载或更新机器人；
- 安装网页运行依赖；
- 创建受系统管理的后台服务；
- 启动网页控制台，并设置服务器重启后自动恢复；
- 更新时保留已有的 `.env`、`config.toml` 和日志；
- 首次改用一键安装时，自动迁移 `/root/predict-mm-bot` 中已有的账户和市场设置。

脚本只支持 Ubuntu/Debian，推荐 Ubuntu 24.04。如果检测到 Python 低于 3.11、端口被其他程序占用，或机器人仍在运行，脚本会停止并显示原因，不会强行覆盖或中断实盘机器人。

以后需要更新到 GitHub 最新版时，先在网页点击“停止并撤单”，然后再次执行同一条安装命令即可。

启动服务：
```bash
sudo systemctl start predict-mm-bot && timeout 30 bash -c 'until curl -fsS http://127.0.0.1:8080/api/status >/dev/null; do sleep 1; done' && curl -fsS -X POST http://127.0.0.1:8080/api/start
```

## 打开网页控制台

网页只监听服务器本机，不会直接暴露到公网，保证安全。安装完成后，在**自己的本地电脑**打开一个新的终端窗口并输入：

```bash
ssh -L 8080:127.0.0.1:8080 用户名@服务器IP
```

`用户名` 可以是 `root`，很多云主机服务商默认是ubuntu,自己确认，然后保持这个 SSH 窗口打开，在自己电脑的浏览器访问 `http://127.0.0.1:8080`。

关闭这个 SSH 窗口只会断开网页访问通道，VPS 上的网页服务和已经启动的机器人仍会继续运行。如果电脑重启或者休眠，要重新输入一遍上面命令打开ssh通道才能访问网页

## 服务管理与排错

查看实时日志：

```bash
sudo journalctl -u predict-mm-bot -f
```

重新启动网页服务：

```bash
sudo systemctl restart predict-mm-bot
```

重启或更新前，应先在网页点击“停止并撤单”。系统服务重启后只会恢复网页控制台，机器人不会自动开始实盘挂单。

## 账户配置

- 申请API key, 加入官方Discord 群 https://discord.gg/predictdotfun ，在Predict fun网页版点右上角头像，复制用户名下的钱包地址， 在Sport Ticket频道中提交工单，用刚刚的钱包地址申请Api Key
- 导出Pravy 的私钥，在PredictFun官网点头像->设置->导出Pravy私钥
- 将上面的钱包地址，Api key，以及Pravy私钥填入机器人控制台的账户设置中就完成了


## 命令行方式

不使用网页时，仍可通过命令行首次配置并直接启动：

```bash
python -m predict_mm.main --config config.toml
```

## 挂单设置

- 直接将Predictfun的市场的网址复制填入控制台中，点击识别网址将自动获取Market ID， 如果该市场有多个选项，将会询问用户选择
- 填完挂单信息后保存点击启动机器人就好了。


## 账户安全

- `.env` 和 `config.toml` 不会被提交到 GitHub；不要把它们发送给任何人。
- 不要在聊天、截图或公开仓库中暴露 API Key、JWT Token 或私钥。
- 程序启动和停止时会尝试撤销它管理的开放订单；即使如此，也应在每次实盘后到 Predict.fun 页面核对开放订单。
