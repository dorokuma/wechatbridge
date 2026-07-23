# WeChatBridge

[English](README.md) | [简体中文](README.zh-CN.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10+-blue.svg)

WeChatBridge 把微信机器人接到 [agy](#前置条件)——谷歌的 Antigravity CLI——让你在微信对话里就能读文件、跑命令、抓网页，并把生成的文件收回来。

```
微信(手机)  ⇄  iLink 机器人 API  ⇄  WeChatBridge  ⇄  agy CLI
                                 (本项目)           (跑工具)
```

桥长轮询 iLink 机器人 API 收微信消息，为每个用户起一个 `agy` 子进程处理，再把回复发回微信。agy 生成的文件经 CDN 回传。

## 功能

- **文本、图片、文件、语音**消息从微信转发给 agy
- **产物回传** — agy 生成的文档、图片、代码发回微信
- **按用户隔离会话** — 每个微信用户有独立的 agy 工作区
- **slash 指令**运行时控制（`/model`、`/clear`、`/fast`、`/persona` 等）
- **危险操作确认闸** — 删除 / 格式化 / `rm -rf` 等指令执行前要确认
- **白名单** — 限定指定微信 ID 才能用
- **MCP 与子代理引导**（`/mcp`、`/agent`）
- **AES-128-ECB 加密**媒体经微信 CDN 传输
- 附带 **systemd 服务文件**，自动重启

## 前置条件

- **agy**（谷歌 Antigravity CLI）已安装并登录（`agy` 在 `PATH`，或设 `AGY_BIN_PATH`）。Antigravity CLI 是谷歌的终端 agentic 编程工具——能理解代码库、经授权编辑文件、在终端跑命令，是 Gemini CLI 的官方继任者。
- 一个微信账号，配合 [ClawBot / iLink](https://ilinkai.weixin.qq.com) 机器人，扫码绑定。
- Python 3.10+。

## 安装

```bash
git clone https://github.com/dorokuma/wechatbridge.git
cd wechatbridge
pip install -r requirements.txt
```

或装成包：

```bash
pip install -e .
```

## 配置

复制示例环境变量文件并修改：

```bash
cp deploy/wechatbridge.env.example .env
```

关键变量（都有默认值）：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AGY_BIN_PATH` | `agy` | agy 可执行文件路径 |
| `WECHATBRIDGE_ALLOWED_SENDERS` | _空_ | 允许使用桥的微信 ID，逗号分隔（空 = 全开） |
| `AGY_TIMEOUT` | `180` | agy 执行超时，秒 |
| `WECHATBRIDGE_MAX_OUTBOUND_BYTES` | `104857600` | 回传微信的文件大小上限（100 MB） |

完整列表见 [`deploy/wechatbridge.env.example`](deploy/wechatbridge.env.example)。

## 运行

```bash
python -m wechatbridge
```

首次运行会打印二维码，用微信扫码绑定机器人，之后开始长轮询收消息。

## 用 systemd 部署

```bash
sudo cp deploy/wechatbridge.service /etc/systemd/system/
# 编辑 WorkingDirectory，并给 unit 加一行 EnvironmentFile=
sudo systemctl enable --now wechatbridge
```

## slash 指令

| 指令 | 作用 |
|---|---|
| `/help` | 列出支持的指令 |
| `/clear` 或 `/new` | 重置会话 |
| `/model <名称>` | 切换 agy 模型（用 `/models` 查列表） |
| `/models` | 列出可用模型 |
| `/fast` | 切换快速模式（低推理开销） |
| `/planning` | 切换 planning 模式 |
| `/add-dir <路径>` | 添加工作目录 |
| `/agents` | 列出可用 agent |
| `/persona <内容>` | 设置人格文档（支持 `show` / `clear` / `reset`） |
| `/mcp` | MCP 工具使用引导 |
| `/agent <名称> <任务>` | 调用子代理执行任务 |

其他 `/` 指令直接交给 agy 处理。

## 已知限制

- 依赖 agy —— 不是独立 agent。
- 语音准确率封顶在微信语音转文字能力，没有本地 ASR。
- 不收发视频。agy 原生不支持理解视频内容，需要第三方工具，超出本项目范围。
- 不输出原生语音气泡（没做 silk 编码）。
- 一个进程一个机器人 —— 两个微信号就跑两个实例。
- agy 以 `--dangerously-skip-permissions` 运行（自动批准所有工具调用）。请用白名单限制访问，只部署给可信用户。

## 贡献

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。项目从 1.0.0 起遵循语义化版本，每次改动登记到 [`CHANGELOG.md`](CHANGELOG.md)。

## 许可证

MIT —— 见 [`LICENSE`](LICENSE)。