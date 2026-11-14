# WeChatBridge

[English](README.md) | [简体中文](README.zh-CN.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10+-blue.svg)

WeChatBridge 把你的终端编程 agent 装进口袋。你自己的 CLI 不用换——agy、grok、codex、dsh 都行——直接通过微信跟它对话，文字、图片、文件、语音都可以（语音由微信侧转写成文字）。你发出去的内容能拿回回复，生成的文件符合条件时还能经 CDN 发回微信。每个用户可用 `/backend` 切换后端，不用重启。

```
微信(手机)  ⇄  iLink 机器人 API  ⇄  WeChatBridge  ⇄  agy / grok / codex / dsh CLI
                                 (桥接本体)         (跑工具)
```

WeChatBridge 在后台常驻，不停地向微信拉新消息。你发的内容需要 AI 处理时，它才把 CLI 拉起一次，拿到答案再关掉；CLI 子进程不常驻，桥本身继续在后台收消息。

`/help`、`/backend`、`/persona` 这类指令由桥自己应答，不会起 CLI。生成的文件只有在用户允许目录内、且被桥识别到，才会回传微信。

## 快速开始

五步，从零到在微信里收到第一条回复，大约 10 分钟。

1. **装一个 CLI 后端并登录。** 推荐 [agy](https://antigravity.google/docs/cli)（谷歌 Antigravity），这是作者日常在用的后端。[grok](https://docs.x.ai/build/overview)、[codex](https://developers.openai.com/codex/cli)、[dsh](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md) 也能用，但选之前先看下面 **CLI 后端**一节的状态说明。装好后在该 CLI 里登录一次（如 `grok login`、`codex login`、`dsh login`）。

2. **装 WeChatBridge：**

   ```bash
   pipx install wechatbridge-cli
   wechatbridge --version
   ```

3. **写一份最小配置：**

   ```bash
   mkdir -p ~/.config/wechatbridge
   curl -o ~/.config/wechatbridge/.env https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/wechatbridge.env.example
   ```

   然后编辑 `~/.config/wechatbridge/.env`。首次启动用放开模式：

   ```bash
   WECHATBRIDGE_ALLOWED_SENDERS=*
   ```

   为什么用 `*`：桥默认 fail-closed。`WECHATBRIDGE_ALLOWED_SENDERS` 和 `WECHATBRIDGE_ADMINS` 都为空时会拒绝所有人，而你此时还不知道自己的 wxid。

   如果你第 1 步装的不是 `agy`，还要在 `.env` 里设全局默认后端；选 `agy` 可省略（默认就是 agy）：

   ```bash
   WECHATBRIDGE_BACKEND=grok   # 或 codex / dsh
   ```

4. **启动并扫码绑定。** ClawBot / iLink 是腾讯官方免费开放的个人号 bot 通道，不用申请、审核、资质或付费，你也不需要额外装任何东西：绑定由桥自己完成。运行 `wechatbridge`，它会在终端打印二维码，用手机微信扫码绑定。桥等待扫码的默认轮询超时是 180 秒，超时后它会自动重新获取二维码继续等，进程不退出也不用重启；只有你手动停掉进程才需要重新运行 `wechatbridge`。

5. **拿到自己的 wxid 并收紧白名单。** 在微信里给 bot 发一条消息，然后在桥日志里找到该消息的发送者 ID，长这样：`from=<id>`。把这个 ID 填回 `WECHATBRIDGE_ALLOWED_SENDERS`（替换 `*`）并重启。`*` 仅供首次调试，长期使用应收紧。

更多配置见下文**配置**一节，部署见**部署**一节。

## 功能

- 你可以发文本、图片、文件、语音（微信服务端转文字）给当前后端：`agy`、`grok`、`codex` 或 `dsh`。各后端成熟度见 **CLI 后端**一节。
- CLI 在你允许目录内生成的文件可以回传微信，有大小上限。不是它碰过的每个文件都会发。
- 每个微信用户有自己的工作区。模型、推理强度、模式按后端分别记忆。
- 运行中用 `/backend agy`、`/backend grok`、`/backend codex` 或 `/backend dsh` 切换后端。真切换会清掉该后端的续聊状态（agy/grok 的续聊标记和 codex 的 `thread_id`/resume 状态），下一轮 CLI 起新会话。磁盘上的历史文件不会立刻删。
- slash 指令：模型、清会话、人格等（见下表）。
- 危险提示闸门：按关键词列表匹配破坏性模式，命中先确认再跑。
- 白名单 `WECHATBRIDGE_ALLOWED_SENDERS`：非空 = 仅列表内用户；`*` = 全开；空 = 仅管理员，管理员也为空则拒绝所有人。
- `/mcp` 只回使用说明。`/agent` 把你的请求改写成自然语言子助手提示再交给 CLI，不是桥内原生 MCP。
- 媒体走微信 CDN，AES-128-ECB 加解密。
- 多实例：一套代码，每个进程设 `WECHATBRIDGE_INSTANCE`，state / 会话 / 二维码路径由实例名派生。
- 部署模板：Linux systemd、macOS launchd、Windows 任务计划说明。

## 平台支持

- **Linux**：主力（附 systemd unit）
- **macOS**：支持（附 launchd plist）
- **Windows**：支持（附任务计划说明）

默认数据路径从 `~` 展开（如 `~/.local/share/wechatbridge/<实例名>/`）。

## CLI 后端

agy 是作者每天用的后端。另外三个已经实现并有测试，但作者没在生产环境长期跑过。选非 agy 后端前，请先在你自己的环境里实测再依赖。

- **agy**（默认）：谷歌 Antigravity CLI。**状态：作者日常使用的后端。**
- **grok**：xAI Grok Build CLI。**状态：已实现并有测试，但作者未在生产环境长期使用——请先自行实测再依赖。**
- **codex**：OpenAI Codex CLI。**状态：已实现并有测试，但作者未在生产环境长期使用——请先自行实测再依赖。**
- **dsh**：DeepSeek Harness CLI（单轮 `headless` profile）。**状态：由 [@KOP-XIAO](https://github.com/KOP-XIAO) 在 #3 与 #4 中贡献，并由贡献者在真机上验证（macOS，含跨轮记忆）。仓库作者未在生产环境长期使用 dsh——依赖前请先自行实测。**

微信里用 `/backend agy`、`/backend grok`、`/backend codex` 或 `/backend dsh` 按用户切换。各后端各自记模型、强度、模式，人格文件也分开。全局默认用 `WECHATBRIDGE_BACKEND` 设。

### dsh 后端说明

- **状态：由 [@KOP-XIAO](https://github.com/KOP-XIAO) 在 #3 与 #4 中贡献，并由贡献者在真机上验证（macOS，含跨轮记忆）。仓库作者未在生产环境长期使用 dsh——依赖前请先自行实测。**
- **dsh 是什么：** DeepSeek Harness CLI（`dsh`），通过 `headless` profile 单轮执行任务。
- **启用与切换：** 在 `.env` 里设 `WECHATBRIDGE_BACKEND=dsh`，或在微信里发 `/backend dsh`。
- **依赖 PyYAML：** dsh 后端需要 `PyYAML` 解析 profile 插件配置。缺了它，`/backend dsh` 会提示并保持原后端不变。安装：`pip install PyYAML` 或 `pipx inject wechatbridge-cli PyYAML`。
- **窗口记忆（默认）：** `headless` 每次调用都新建会话（`session-<uuid>`），所以对话记忆由桥来维护。成功的轮次会追加写入 `dsh_memory.jsonl`；每次调用时桥读取最近几轮对话（默认 10 轮对，上限 `WECHATBRIDGE_DSH_MEMORY_CHARS` 字符）注入提示词——这个上限限的是注入时的读取窗口，不是磁盘文件大小。`/clear` 或 `/new` 清空这份记忆重新开始。
- **常驻会话模式（resume）：** 设 `WECHATBRIDGE_DSH_RESUME=true` 开启真正的常驻会话，类似 codex resume。前提是你的 headless profile 挂载了 `dsh-bridge-runner` 插件（`dsh_bridge_runner.py`，从环境变量读 `DSH_BRIDGE_SESSION_ID` 和 `DSH_BRIDGE_TASK`）。开了之后上下文在同一会话里自然累积，不做窗口截断。`/clear` 或 `/new` 删掉存下的会话 ID 开新会话。resume 模式下窗口记忆注入会跳过。
- **工作区与状态隔离：** 默认窗口模式下实际跑的是 `dsh --profile headless -- <提示>`，`cwd` 是你的每用户会话目录（`WECHATBRIDGE_SESSION_DIR/<user_id>`）。resume 模式下没有 `-- <提示>` 参数：任务通过环境变量 `DSH_BRIDGE_TASK` 传入（profile 仍可用 `DSH_PROFILE` 配置）。桥私有状态（`dsh_memory.jsonl` 和持久会话 ID）放在 `WECHATBRIDGE_DSH_STATE_DIR`（`~/.local/share/wechatbridge/<实例名>/dsh_state/`），在子进程会话目录树之外。这样一级 `../` 穿越摸不到私有状态。
- **谁能伤到谁：** 每个 dsh 子进程和桥用同一个 Unix 用户跑，中间没有容器。一级 `../` 到不了私有状态，但两级穿越（`../../dsh_state/<user_id>`）在文件系统层面仍然可达，这是有意接受的。所以只给你信任的人用。
- **附件：** 图片和文件以 `@/绝对路径` 的形式并进 prompt 文本。我查过 dsh v0.1.1-rc.2：`headless` 自己不会打开或内联 `@` 提到的文件，而是把你的 prompt 原样交给模型。模型靠系统提示知道 @ 路径，需要时自己调 `read` 工具去读。桥会把越界 mention（`@/abs`、`@~/x`、`@file://`）改写成 `[blocked-path]`。这只是改 prompt 文本，不是安全边界。
- **认证和 profile 是机器级共享。** 解析顺序：`WECHATBRIDGE_DSH_HOME` > `WECHATBRIDGE_HOST_HOME/.dsh` > `~/.dsh`，和 grok、codex 的宿主回退模型一样。子进程的 `HOME` 指向每用户会话目录，所以桥总是显式传 `DSH_HOME`。设了 `WECHATBRIDGE_DSH_HOME` 就用专用主目录并自动做会话保留清理；不设就回退到 `$WECHATBRIDGE_HOST_HOME/.dsh`（若已设）或 `~/.dsh`，不自动清理，由你自己管理。如果桥跑在专用系统用户（如 `wechatbridge`）下，把 `WECHATBRIDGE_HOST_HOME` 指到操作员主目录，CLI 凭证和 profile 才能被找到。`DSH_BIN_PATH`、`DSH_PROFILE`、`DSH_TIMEOUT` 都能配。
- **测试现状：** 和 codex 一样，本后端的测试基于 fake CLI；在此基础上，贡献者在自己机器上用真实 `dsh` CLI 验证过（macOS，含跨轮记忆 resume）。作者仍未在生产环境长期使用 dsh——依赖前请先自行实测。
- **插件文件：** resume 模式需要你在自己的 dsh profile 里挂载 `dsh-bridge-runner` 插件。本仓库不附带插件文件。

### grok 后端说明

- **状态：已实现并有测试，但作者未在生产环境长期使用——请先自行实测再依赖。**
- **隔离：** 每个微信用户运行时 `HOME` 指到自己的会话目录，对话状态留在里面；登录态跟机器走。
- **认证：** 会话目录链接到宿主 `~/.grok/auth.json`（链接失败就拷贝一份），复用你宿主的 `grok login`。grok CLI 重写 `auth.json` 用「临时文件加 rename」，会把会话里的 symlink 拆成普通文件。桥会把这类文件原子写回宿主再重建链接，刷新后的新凭证不会被宿主已吊销的旧凭证覆盖。也可以在桥进程环境里设 `XAI_API_KEY`，env 清洗之后会再注入给 grok 子进程。grok-remote 客户端已登录，不等于宿主 CLI 已登录。本仓库不存放任何 key 或 token 值。

### codex 后端说明

- **状态：已实现并有测试，但作者未在生产环境长期使用——请先自行实测再依赖。**
- 每轮跑 `codex exec --json`。续聊用 `codex exec resume <thread_id> <prompt>`，thread id 按用户持久化。
- **隔离：** 每个微信用户运行时 `HOME` 指到自己的每用户会话目录（`session_dir`），`CODEX_HOME` 指到 `session_dir/.codex`，会话、日志、缓存互不串。
- **认证：** 会话目录链接到宿主 `~/.codex/auth.json`（链接失败就拷贝一份），复用你宿主的 `codex login`。codex CLI 重写 `auth.json` 用「临时文件加 rename」，会把会话里的 symlink 拆成普通文件。桥会把这类文件原子写回宿主再重建链接，刷新后的新凭证不会被宿主已吊销的旧凭证覆盖。也可以在桥进程环境里设 `CODEX_API_KEY` 认证。本仓库不存放任何 key 或 token 值。
- **测试现状：** 我手上没有真实 Codex 订阅或 CLI 可以实测。这个后端是按源码研究、JSONL fixture 和测试里的 fake CLI 写出来的（测试通过）。最终要靠真实用户在真 CLI 上验收。

## 前置条件

- 至少装好并登录其中一个 CLI：
  - `agy` 在 `PATH` 中，或设 `AGY_BIN_PATH`
  - 和/或 `grok` 在 `PATH` 中，或设 `GROK_BIN_PATH`
  - 和/或 `codex` 在 `PATH` 中，或设 `CODEX_BIN_PATH`
  - 和/或 `dsh` 在 `PATH` 中，或设 `DSH_BIN_PATH`（DeepSeek Harness，先跑 `dsh login`）
  - Antigravity 是谷歌终端 agentic 编程工具（Gemini CLI 官方继任）。Grok Build 是 xAI 同类产品，Codex 是 OpenAI 的，dsh 是 DeepSeek Harness 的 CLI。
- 一个微信账号 + [ClawBot / iLink](https://ilinkai.weixin.qq.com) 机器人（首次扫码绑定）
- Python 3.10+

## 安装

推荐用 [pipx](https://pypa.github.io/pipx/)（需要 Python >= 3.10）：

```bash
pipx install wechatbridge-cli
```

安装后验证：

```bash
wechatbridge --version
```

### 安装 pipx

**Debian / Ubuntu：**

```bash
sudo apt install pipx
```

**其他系统（或想装最新版）：**

```bash
python3 -m pip install --user pipx && python3 -m pipx ensurepath
```

然后重新打开终端或重新加载 shell 配置文件，确保 `pipx` 在 `PATH` 中。

### 开发者

如果你想改源码：

```bash
git clone https://github.com/dorokuma/wechatbridge.git
cd wechatbridge
pip install -e .
```

## 配置

WeChatBridge 按下面的优先级加载配置，找到第一个就用：

1. `$WECHATBRIDGE_ENV_FILE`，显式指定路径
2. `$XDG_CONFIG_HOME/wechatbridge/<实例名>.env`（缺省 `~/.config/wechatbridge/<实例名>.env`）
3. `$XDG_CONFIG_HOME/wechatbridge/.env`（缺省 `~/.config/wechatbridge/.env`）
4. 仓库根目录 `.env`：**已废弃**（启动时会打印警告）

实例名缺省为 `default`，可用 `WECHATBRIDGE_INSTANCE` 修改。

获取示例配置：

```bash
mkdir -p ~/.config/wechatbridge
curl -o ~/.config/wechatbridge/.env https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/wechatbridge.env.example
```

然后编辑 `~/.config/wechatbridge/.env` 填你自己的配置。**首次安装注意**：`WECHATBRIDGE_ALLOWED_SENDERS` 和 `WECHATBRIDGE_ADMINS` 都为空时，桥会拒绝所有用户（fail-closed）。第一次运行前你没法知道自己的 wxid，所以先用 `WECHATBRIDGE_ALLOWED_SENDERS=*` 启动，给 bot 发一条消息，从日志里读发送者 ID（`from=<id>`），再把 `*` 换成该 wxid。可以顺手设 `WECHATBRIDGE_ADMINS=<你的 wxid>`，然后重启。

关键变量（都有默认值）：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `AGY_BIN_PATH` | `agy` | agy 可执行文件路径 |
| `GROK_BIN_PATH` | `grok` | grok 可执行文件路径 |
| `CODEX_BIN_PATH` | `codex` | codex 可执行文件路径 |
| `DSH_BIN_PATH` | `dsh` | dsh 可执行文件路径 |
| `DSH_PROFILE` | `headless` | dsh 单轮任务启动的 profile |
| `DSH_TIMEOUT` | `600` | dsh CLI 执行超时秒数 |
| `WECHATBRIDGE_DSH_MEMORY_TURNS` | `10` | dsh 后端：注入上下文的最近对话轮数（user+assistant 对） |
| `WECHATBRIDGE_DSH_MEMORY_CHARS` | `6000` | dsh 后端：注入记忆上下文的字符上限 |
| `WECHATBRIDGE_DSH_RESUME` | `false` | dsh 后端：常驻会话模式——每用户一个 dsh 会话，每条消息恢复同一会话（codex 式）；需在 headless profile 挂载 dsh-bridge-runner 插件；启用时跳过窗口记忆 |
| `WECHATBRIDGE_DSH_HOME` | _空_ | 传给 dsh 子进程的显式 `DSH_HOME`。显式设置 = 专用目录+自动会话清理；未设 = 回退至 `WECHATBRIDGE_HOST_HOME/.dsh` 或 `~/.dsh` 且不自动清理（优先级：`WECHATBRIDGE_DSH_HOME` > `WECHATBRIDGE_HOST_HOME/.dsh` > `~/.dsh`） |
| `WECHATBRIDGE_HOST_HOME` | _空_ | 宿主用户主目录覆盖（服务用户部署时使用；用作 grok `~/.grok`、codex `~/.codex`、dsh `~/.dsh` 的回退基准路径） |
| `WECHATBRIDGE_DSH_STATE_DIR` | _派生_ | 桥私有 dsh 状态目录（记忆 JSONL 与持久会话 ID；默认 `~/.local/share/wechatbridge/<实例名>/dsh_state`） |
| `WECHATBRIDGE_BACKEND` | `agy` | 全局默认后端（`agy` / `grok` / `codex` / `dsh`，可被 `/backend` 按用户覆盖） |
| `WECHATBRIDGE_INSTANCE` | `default` | 实例名；state / 会话 / 二维码路径由它派生 |
| `WECHATBRIDGE_ALLOWED_SENDERS` | _空_ | 允许使用的微信 ID，逗号分隔；`*` = 全开；空 = 仅 `WECHATBRIDGE_ADMINS` 成员（管理员也为空则拒绝所有人） |
| `AGY_TIMEOUT` | `600` | CLI 执行超时秒数（agy / grok / codex 后端） |
| `WECHATBRIDGE_MAX_OUTBOUND_BYTES` | `104857600` | 回传微信文件大小上限（100 MB） |
| `WECHATBRIDGE_MAX_INBOUND_BYTES` | `20971520` | 入站图片/文件下载后上限（20 MB） |
| `WECHATBRIDGE_MAX_CONCURRENT` | `4` | 全局同时处理上限；同用户串行且排队不占全局槽；满了回「忙」（小内存主机可调小此项以避免并发 CLI 内存峰值；见[内存限制](#内存限制)） |
| `WECHATBRIDGE_QUEUE_NOTICE` | `true` | 同用户上一条仍在处理时，后续消息入队前先发一次「已加入排队」提示；置 `0`/`false`/`no` 关闭 |
| `WECHATBRIDGE_PROGRESS_NOTICE` | `true` | 单条消息处理超过 `WECHATBRIDGE_PROGRESS_NOTICE_DELAY` 秒时发一次「仍在处理中」提示；置 `0`/`false`/`no` 关闭 |
| `WECHATBRIDGE_PROGRESS_NOTICE_DELAY` | `30` | 进度提示触发阈值（秒） |
| `WECHATBRIDGE_CONFIRM_TOKEN` | `y` | 危险闸门确认口令 |
| `WECHATBRIDGE_ENABLE_MCP` | `true` | 是否启用 `/mcp` 说明指令 |
| `WECHATBRIDGE_ENABLE_SUBAGENT` | `true` | 是否启用 `/agent` 提示改写指令 |
| `WECHATBRIDGE_ADMINS` | _空_ | 逗号分隔的 wxid 列表；检测到新版本时管理员会收到微信通知 |
| `WECHATBRIDGE_UPDATE_CHECK` | `true` | 启动时及每 24h 检查 PyPI 新版本；失败静默不影响运行 |
| `WECHATBRIDGE_UPDATE_CHECK_INTERVAL` | `86400` | 版本检查间隔（秒） |

完整列表见 [`deploy/wechatbridge.env.example`](deploy/wechatbridge.env.example)。

> **为什么改配置位置？** pipx 全局安装后，源码目录下的 `.env` 不再合理。XDG 基础目录布局把配置和代码分开，并且天然支持多实例。

## 运行

```bash
wechatbridge
```

首次运行会打印二维码（并在实例数据目录保存 PNG）。微信扫码绑定后开始收消息。

## 升级

```bash
pipx upgrade wechatbridge-cli
sudo systemctl restart wechatbridge
```

也可以用升级脚本。不用 clone 仓库，直接 curl 拿：

```bash
curl -fsSL https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/update.sh | sudo bash
```

脚本会升级 pipx 安装并重启服务。如果服务跑在专用系统用户（如 `wechatbridge`）下，用 root 跑会自动以该用户身份执行 pipx，可用 `WECHATBRIDGE_USER=<用户名>` 覆盖。

数据放在 `~/.local/share/wechatbridge/<实例名>/`（会话、SQLite 历史、二维码、登录态），升级不会动它。bot 保持登录，对话不丢。

升 **major** 或 **minor** 版本（如 1.2 → 1.3）前，先查 [`CHANGELOG.md`](CHANGELOG.md) 里的破坏性变更和迁移步骤。

> **升级到 1.8.0 前**：在 `.env` 里配好 `WECHATBRIDGE_ADMINS` 或 `WECHATBRIDGE_ALLOWED_SENDERS`。1.8.0 起两者都为空会拒绝所有用户。想保持全开就设 `WECHATBRIDGE_ALLOWED_SENDERS=*`。

## 部署

### Linux（systemd）

先在 `wechatbridge` 系统用户下安装：

```bash
sudo -u wechatbridge pipx install wechatbridge-cli
```

然后部署服务 unit：

```bash
sudo cp deploy/wechatbridge.service /etc/systemd/system/
sudo systemctl enable --now wechatbridge
```

**多实例：** 复制模板 `deploy/wechatbridge@.service` 并启动实例：

```bash
sudo cp deploy/wechatbridge@.service /etc/systemd/system/
sudo systemctl enable --now wechatbridge@bot2
sudo systemctl enable --now wechatbridge@bot3
```

每个实例读自己的配置文件（`~/.config/wechatbridge/bot2.env`），数据存在自己的数据目录（`~/.local/share/wechatbridge/bot2/`）。

#### 内存限制

多实例模板（`deploy/wechatbridge@.service`）内置了 cgroup 内存限制：

```ini
MemoryAccounting=yes
MemoryHigh=450M
MemoryMax=512M
```

- **为什么是这个数：** 后端 CLI 子进程（如 `agy`）单轮 RSS 能到 ~276 MB 以上，识图或长会话时峰值更高。在 ~1 GB 内存的主机上，300 MB 的硬顶会引发剧烈换页抖动、很高的 swap 延迟和流式中断（`subscriber fell behind updates`）。`450M`（`MemoryHigh`）温和回收，`512M`（`MemoryMax`）是硬顶，防止整机 OOM。
- **用 drop-in 按实例覆盖**，不改基础模板：
  ```bash
  sudo mkdir -p /etc/systemd/system/wechatbridge@bot2.service.d/
  cat << 'EOF' | sudo tee /etc/systemd/system/wechatbridge@bot2.service.d/memory.conf
  [Service]
  # 适用于 >= 2 GB 内存的主机
  MemoryHigh=600M
  MemoryMax=768M
  EOF
  sudo systemctl daemon-reload
  sudo systemctl restart wechatbridge@bot2
  ```
  查看生效中的限制：
  ```bash
  systemctl show wechatbridge@bot2 -p MemoryHigh,MemoryMax
  ```
- **容量规划：** 默认值针对小内存主机（~1 GB RAM）调优，大主机可以放宽。每个实例在自己的 service cgroup 里跑，预算要按全部运行实例最坏情况的总上限（N × MemoryMax）来留。
- **与并发的关系：** 多条重型任务并发（比如多人同时发图）会成倍放大后端 CLI 的内存开销。内存紧张的主机上，先把 `WECHATBRIDGE_MAX_CONCURRENT` 调小（到 `2` 或 `1`），再考虑调大内存上限。
- **Swap：** 不要收紧 `MemorySwapMax`。swap 余量是安全缓冲：承压时让进程变慢，而不是立刻被 cgroup OOM 杀掉。
- **单实例部署：** `deploy/wechatbridge.service` 内置了同样的限制（`MemoryHigh=450M` 和 `MemoryMax=512M`）。要调整就在 `/etc/systemd/system/wechatbridge.service.d/` 下放 drop-in，并把上面 `systemctl` 命令里的单元名换成 `wechatbridge`。限制在安装或重铺 unit 文件时生效；`update.sh` 升级不动 unit 文件。
- **平台范围：** 这些 cgroup 限制只在 Linux systemd 上存在。macOS（launchd）和 Windows（任务计划程序）没有等价的按实例上限。

### macOS（launchd）

```bash
cp deploy/wechatbridge.plist ~/Library/LaunchAgents/com.wechatbridge.plist
# 编辑 plist 里的 WorkingDirectory 和 ProgramArguments
launchctl load ~/Library/LaunchAgents/com.wechatbridge.plist
```

### Windows（任务计划程序）

见 [`deploy/wechatbridge-windows.md`](deploy/wechatbridge-windows.md)。

## slash 指令

| 指令 | 作用 |
|---|---|
| `/help` | 按当前后端列出支持指令 |
| `/backend <agy\|grok\|codex\|dsh>` | 按微信用户切换 CLI 后端（真切换时清除该后端续聊状态：agy/grok 标记、codex `thread_id`/resume，或 dsh 记忆/会话 ID，下次起新会话；历史文件可能仍在，靠保留策略清理） |
| `/clear` 或 `/new` | 开启新对话（agy/grok：丢弃续聊标记；codex：清空 thread_id；dsh：清空窗口记忆或常驻会话 ID） |
| `/model <名称>` | 设模型（各后端均对照实时列表校验：agy/grok 走 CLI `models`；codex 走 `codex debug models`，失败或空再试 `--bundled`；未知名或列表拉取失败则拒绝且不写 prefs；见 `/models`） |
| `/models` | 列出可用模型（agy/grok/codex 均向 CLI 实时查询；codex 用 `debug models`，仅在实时列表拉不到时才回退内置参考说明） |
| `/fast` | 快速模式开关：低推理开销（可带 `on`/`off`，无参再按一次关闭） |
| `/planning` | planning 模式开关（可带 `on`/`off`，无参再按一次关闭） |
| `/add-dir <路径>` | **agy/codex：** 校验通过后后续会带 `--add-dir`。**grok：** 只记偏好，暂不传给 CLI。无参查看当前列表，`/add-dir clear` 清空（dsh 暂不支持） |
| `/agents` | 列出可用助手 |
| `/persona <内容>` | 设人格（`show` / `clear` / `reset`） |
| `/price <链接/口令/词>` | 京东/淘宝查券比价（需配置 `JDUNION_*` 或 `DATAOKE_*`） |
| `/version` | 显示当前版本、实例名和后端；若有新版本则显示升级提示 |
| `/mcp` | 短**使用说明**文案（可用 `WECHATBRIDGE_ENABLE_MCP` 关掉） |
| `/agent <名称> <任务>` | 拼成「调用子助手…」提示再跑当前引擎（可用 `WECHATBRIDGE_ENABLE_SUBAGENT` 关掉） |

其余 `/…`：有的在微信端禁用（如 `/exit`），有的是 TUI 专用会提示不支持，其余透传给当前 CLI。

`/add-dir` 只接受用户会话目录内，或 `WECHATBRIDGE_ADD_DIR_ROOTS` 列出的根路径下的目录。

## 运维与安全（桥实际管到的）

- **白名单优先。** `WECHATBRIDGE_ALLOWED_SENDERS` 为空 = 仅 `WECHATBRIDGE_ADMINS` 成员可用；`*` = 全开；两者都空 = 拒绝所有人。
- **CLI 自动批准。** agy 带 `--dangerously-skip-permissions`，grok 带 `--always-approve`（planning 模式除外），dsh 的模型工具无宿主路径约束。只给你信任的人用，这不是多租户沙箱。
- **危险闸门是关键词匹配**，不识别意图。默认只盯具体模式：`rm -rf /`、管道进 shell、`mkfs`、`format c:`、少量重型中文句式等。日常一个单独的「删除」**不会**拦。列表用 `WECHATBRIDGE_CONFIRM_KEYWORDS` 自定义；确认口令 `WECHATBRIDGE_CONFIRM_TOKEN`（默认 `y`），等待时限 `WECHATBRIDGE_PENDING_TTL`。
- **入站媒体**有大小上限（默认 20 MB）、流式下载、CDN 域名白名单；缺 `aes_key` 会明确报错。
- **出站产物**只从用户允许目录发出（agy：会话 scratch；grok 和 dsh：会话目录下），经 `realpath` 检查，且不超过 `WECHATBRIDGE_MAX_OUTBOUND_BYTES`。
- **并发：** 全局处理槽上限（`WECHATBRIDGE_MAX_CONCURRENT`，默认 4）。同一用户串行，等自己上一条时**不占**全局槽；不同用户并行，受上限约束。重型并发任务会成倍放大子进程内存，见[内存限制](#内存限制)。
- **长回复**按字数切块（`WECHATBRIDGE_MESSAGE_CHUNK`，默认 2000）。
- **数据目录：** 默认 `~/.local/share/wechatbridge/<instance>/`（可 env 覆盖）。运行目录倾向 `0700`，token/二维码倾向 `0600`（Unix；Windows 依赖 NTFS ACL）。
- **清理：** 会话临时文件与对话历史用不同 TTL（`WECHATBRIDGE_SESSION_RETENTION_DAYS`、`WECHATBRIDGE_HISTORY_RETENTION_DAYS`）。偏好和登录信息不按此删。
- **子进程环境**会剥掉常见密钥类变量名，并把 `HOME`（Windows 另设 `USERPROFILE`）指到该用户会话目录。

## 已知限制

- 不是独立 agent，你需要 agy 和/或 grok 和/或 codex 和/或 dsh。
- **codex** 已实现并有测试，但我没有真实 Codex 订阅或 CLI 实测过，只按源码研究、JSONL fixture 和测试里的 fake CLI 写的（测试通过）。用之前先在你自己的环境确认。
- **dsh** 有窗口记忆（默认，最近 `WECHATBRIDGE_DSH_MEMORY_TURNS` 轮）和常驻会话（`WECHATBRIDGE_DSH_RESUME=true`）两种模式。逻辑有测试覆盖，但我也没长期跑过。resume 模式需要你自己在 dsh profile 里挂载 `dsh-bridge-runner` 插件，本仓库不附带插件文件。
- dsh 的模型/强度/模式/人格 slash 指令还没接通，`/model`、`/fast`、`/planning`、`/persona`、`/add-dir` 在 dsh 后端会回「暂不支持」。
- 语音只靠微信转写，没有本地 ASR；转写为空会提示改打字。
- 不收发视频，不回原生语音气泡（没做 silk 编码）。
- 一个进程绑一个微信号，多号要多实例（`WECHATBRIDGE_INSTANCE`）。
- 产物回传是「允许路径内、能识别到的尽量发」，不是「CLI 在哪写的都回传」。
- `/mcp`、`/agent` 不在桥内跑 MCP 协议或托管子进程，只做引导或改写提示给 CLI。
- 尽量加白名单，只给可信用户用。

## 贡献者

- [@KOP-XIAO](https://github.com/KOP-XIAO) — dsh 后端（[#3](https://github.com/dorokuma/wechatbridge/pull/3)）及其长期记忆与 resume 支持（[#4](https://github.com/dorokuma/wechatbridge/pull/4)），并在真机上验证过。

## 贡献

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。从 1.0.0 起语义化版本，改动记入 [`CHANGELOG.md`](CHANGELOG.md)。

## 许可证

MIT，见 [`LICENSE`](LICENSE)。
