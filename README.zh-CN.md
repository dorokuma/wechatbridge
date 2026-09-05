# WeChatBridge

[English](README.md) | [简体中文](README.zh-CN.md)

![license](https://img.shields.io/badge/license-MIT-blue.svg)
![python](https://img.shields.io/badge/python-3.10+-blue.svg)

WeChatBridge 把微信机器人接到 agentic 编程 CLI（谷歌 agy / Antigravity、xAI Grok Build、OpenAI Codex，或 DeepSeek Harness 的 dsh）。你可以在微信里发文字、图片、文件、语音（仅微信侧转写文字）给**当前后端**，拿回复，并在条件满足时把部分生成文件经 CDN 发回微信。每个用户可用 `/backend` 切换后端，无需重启进程。

```
微信(手机)  ⇄  iLink 机器人 API  ⇄  WeChatBridge  ⇄  agy / grok / codex / dsh CLI
                                 (本项目)           (跑工具)
```

桥接进程本身一直在跑、长轮询 iLink。需要交给 CLI 的提示才会按需拉起一个 agy / grok / codex / dsh 子进程（单轮），**子进程**跑完即退、不常驻。许多 slash（如 `/help`、`/backend`、`/persona`）在桥内直接处理，不会起 CLI。只有桥能识别、且落在用户允许目录内的产物，才会经 CDN 回传。

## 功能

- 文本、图片、文件、语音（仅微信服务端语音转文字）交给**当前激活**的后端（`agy`、`grok`、`codex` 或 `dsh`）
- 在用户允许目录内、且被识别到的 CLI 产物可回传微信（有大小上限）；不是 CLI 碰过的每个文件都会发
- 每个微信用户独立工作区；模型 / 推理强度 / 模式按**后端**分别记忆
- 运行时切换后端：`/backend agy`、`/backend grok`、`/backend codex` 或 `/backend dsh`（真正切换时清除该后端续聊状态——agy/grok 的续聊标记与 codex 的 `thread_id`/resume 状态——下次 CLI 起新会话；磁盘上的历史文件不会立刻抹掉）
- slash 指令：模型、清会话、人格等（见下表）
- 危险提示闸门：对**明确破坏性关键词/模式**先确认再跑（不是全语义理解）
- 白名单 `WECHATBRIDGE_ALLOWED_SENDERS`（空 = 全开）
- `/mcp` 只回使用说明；`/agent` 改写成自然语言子助手提示再交给当前引擎（**不是**桥内原生扩展协议）
- 媒体走微信 CDN，AES-128-ECB 加解密
- 多实例：一套代码，用 `WECHATBRIDGE_INSTANCE` 区分进程（state / 会话 / 二维码路径由实例名派生）
- 部署模板：Linux systemd、macOS launchd、Windows 任务计划说明

## 平台支持

- **Linux** — 主力（附 systemd）
- **macOS** — 支持（附 launchd plist）
- **Windows** — 支持（附任务计划说明）

默认数据路径从 `~` 展开（如 `~/.local/share/wechatbridge/<instance>/`）。

## CLI 后端

- **agy**（默认）— 谷歌 Antigravity CLI
- **grok** — xAI Grok Build CLI
- **codex** — OpenAI Codex CLI
- **dsh** — DeepSeek Harness CLI（单轮 `headless` profile）

微信里 `/backend agy`、`/backend grok`、`/backend codex` 或 `/backend dsh` 按用户切换。各后端各自记模型 / 强度 / 模式，人格文件布局也分开。全局默认见 `WECHATBRIDGE_BACKEND`。

### dsh 后端说明

- **dsh 是什么：** DeepSeek Harness CLI（`dsh`），通过 `headless` profile 单轮执行任务。
- **启用与切换：** 可在 `.env` 中设置全局 `WECHATBRIDGE_BACKEND=dsh`，或在微信中通过 `/backend dsh` 按用户切换。
- **依赖说明（PyYAML）：** dsh 后端需要 `PyYAML` 解析 profile 插件配置。若缺少依赖，`/backend dsh` 会返回明确报错且保持原引擎偏好不变。安装方式：`pip install PyYAML` 或 `pipx inject wechatbridge-cli PyYAML`。
- **窗口记忆（默认模式）：** `headless` profile 每次调用均新建会话（`session-<uuid>`），由桥维护长期对话记忆：最近对话轮数（默认 10 轮对，最多 `WECHATBRIDGE_DSH_MEMORY_CHARS` 字符）存于 `dsh_memory.jsonl` 并自动注入每次提问。`/clear` 或 `/new` 清空该记忆以重新开始。
- **常驻会话模式（resume）：** 设置 `WECHATBRIDGE_DSH_RESUME=true` 开启真正的常驻会话模式（类似 codex resume）。要求 headless profile 挂载 `dsh-bridge-runner` 插件（`dsh_bridge_runner.py`，从环境变量读取 `DSH_BRIDGE_SESSION_ID` 与 `DSH_BRIDGE_TASK`）。上下文在同一会话内原生累积，无窗口截断。`/clear` 或 `/new` 会删除存储的会话 ID 并开启新会话。开启常驻模式时，自动跳过窗口记忆注入。
- **工作区与状态隔离：** 以 `dsh --profile headless -- <提示>` 运行，`cwd` = 每用户会话目录（`WECHATBRIDGE_SESSION_DIR/<user_id>`）。桥私有状态文件（`dsh_memory.jsonl` 与持久会话 ID）保存在 `WECHATBRIDGE_DSH_STATE_DIR`（`~/.local/share/wechatbridge/<实例名>/dsh_state/`），独立于子进程会话目录树，杜绝一级相对路径穿越（`../`）。
- **威胁模型：** 子进程以相同宿主 UID 运行，无容器沙箱隔离。虽然一级相对穿越（`../`）无法访问私有状态，但同 UID 下二级相对穿越（`../../dsh_state/<user_id>`）在文件系统层面可达且已接受。请作为可信用户工具部署。
- 图片/文件附件对 dsh 以 @绝对路径 文本并入 prompt。经 dsh v0.1.1-rc.2 源码验证：`headless` profile 不会在 CLI/运行时层面预读取或内联 @mention 文件，而是将 prompt 作为纯文本直接提交给模型，模型依赖系统提示感知 @ 路径并在需要时自行调用 `read` 等工具；桥仅拦截绝对路径与 ~ 开头的越界 mention（`@/abs`、`@~/x`、`@file://`，替换为 `[blocked-path]`），是 prompt 文本层的 best-effort 过滤，不是沙箱边界。
- 认证 / profile 为**机器级共享**（回退优先级：`WECHATBRIDGE_DSH_HOME` > `WECHATBRIDGE_HOST_HOME/.dsh` > `~/.dsh`，与 grok/codex 宿主回退模型一致）：子进程 `HOME` 指向每用户会话目录，所以桥总是显式传 `DSH_HOME`。显式设置 `WECHATBRIDGE_DSH_HOME` 时使用专用主目录并开启自动会话保留清理；未设置时回退至 `$WECHATBRIDGE_HOST_HOME/.dsh`（若设置）或 `~/.dsh`，不执行自动会话清理，由操作员自行管理。若桥以专用系统用户（如 `wechatbridge`）运行，可设置 `WECHATBRIDGE_HOST_HOME` 指向操作员主目录以复用 CLI凭证与 profile。`DSH_BIN_PATH`、`DSH_PROFILE`、`DSH_TIMEOUT` 均可配置。
- **状态：** 基于已发布的 dsh CLI 契约与测试套件（fake CLI）覆盖实现。常驻 resume 模式需用户在 dsh profile 中自行挂载 `dsh-bridge-runner` 插件，本仓库不附带插件文件。

### grok 后端说明

- 隔离：每个微信用户运行时把 `HOME` 指到自己的会话目录，对话状态在会话里；登录态跟机器走。
- 认证：每用户会话链接宿主 `~/.grok/auth.json`（fallback 为拷贝），复用宿主 `grok login`；grok CLI 用「临时文件+rename」重写 auth.json 时会把会话 symlink 拆成普通文件，桥会把这类 session 普通文件原子写回宿主（promote）并重建链接，刷新后的新凭证不会被宿主已吊销旧凭证覆盖；也可在桥进程环境设 `XAI_API_KEY`（sanitize 之后会再注入 grok 子进程）。grok-remote 客户端已登录，不等于主机 CLI 已登录。本仓库不存放任何 key / token 值。

### codex 后端说明

- 每轮以 `codex exec --json` 运行；续聊使用 `codex exec resume <thread_id> <prompt>`，thread id 按用户持久化。
- 隔离：每个微信用户运行时把 `HOME` 与 `CODEX_HOME` 指到自己的会话目录（`session_dir/.codex`），会话、日志、缓存互不串。
- 认证：每用户会话链接宿主 `~/.codex/auth.json`（fallback 为拷贝），复用宿主 `codex login`；codex CLI 用「临时文件+rename」重写 auth.json 时会把会话 symlink 拆成普通文件，桥会把这类 session 普通文件原子写回宿主（promote）并重建链接，刷新后的新凭证不会被宿主已吊销旧凭证覆盖；也可在桥进程环境设 `CODEX_API_KEY` 认证。本仓库不存放任何 key / token 值。
- **状态：** 目前没有真实 Codex 订阅或 CLI 可供实测。codex 后端基于源码研究、JSONL fixture 与测试用的 fake CLI 实现（测试通过），最终需由真实用户在真实 Codex CLI 上验收。

## 前置条件

- 至少装好并登录其中一个 CLI：
  - **agy** 在 `PATH` 中，或设 `AGY_BIN_PATH`
  - **和/或 grok** 在 `PATH` 中，或设 `GROK_BIN_PATH`
  - **和/或 codex** 在 `PATH` 中，或设 `CODEX_BIN_PATH`
  - **和/或 dsh** 在 `PATH` 中，或设 `DSH_BIN_PATH`（DeepSeek Harness，需先 `dsh login`）
  - Antigravity 是谷歌终端 agentic 编程工具（Gemini CLI 官方继任）；Grok Build 是 xAI 同类产品；Codex 是 OpenAI 终端 agentic 编程工具；dsh 是 DeepSeek Harness 的 CLI
- 一个微信账号 + [ClawBot / iLink](https://ilinkai.weixin.qq.com) 机器人（首次扫码绑定）
- Python 3.10+

## 安装

推荐使用 [pipx](https://pypa.github.io/pipx/)（需要 Python >= 3.10）：

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

如果你想从源码修改：

```bash
git clone https://github.com/dorokuma/wechatbridge.git
cd wechatbridge
pip install -e .
```

## 配置

配置加载优先级从高到低：

1. `$WECHATBRIDGE_ENV_FILE` — 显式指定路径
2. `$XDG_CONFIG_HOME/wechatbridge/<实例名>.env`（缺省 `~/.config/wechatbridge/<实例名>.env`）
3. `$XDG_CONFIG_HOME/wechatbridge/.env`（缺省 `~/.config/wechatbridge/.env`）
4. 仓库根目录 `.env` — **已废弃**（启动时会打印警告）

实例名缺省为 `default`；可通过 `WECHATBRIDGE_INSTANCE` 修改。

获取示例配置：

```bash
mkdir -p ~/.config/wechatbridge
curl -o ~/.config/wechatbridge/.env https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/wechatbridge.env.example
```

然后编辑 `~/.config/wechatbridge/.env` 修改你的配置。

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
| `WECHATBRIDGE_ALLOWED_SENDERS` | _空_ | 允许使用的微信 ID，逗号分隔（空 = 全开） |
| `AGY_TIMEOUT` | `600` | CLI 执行超时秒数（agy / grok / codex 后端） |
| `WECHATBRIDGE_MAX_OUTBOUND_BYTES` | `104857600` | 回传微信文件大小上限（100 MB） |
| `WECHATBRIDGE_MAX_INBOUND_BYTES` | `20971520` | 入站图片/文件下载后上限（20 MB） |
| `WECHATBRIDGE_MAX_CONCURRENT` | `4` | 全局同时处理上限；同用户串行且排队不占全局槽；满了回「忙」（小内存主机可调小此项以避免并发 CLI 内存峰值；见[内存限制](#内存限制)） |
| `WECHATBRIDGE_CONFIRM_TOKEN` | `y` | 危险闸门确认口令 |
| `WECHATBRIDGE_ENABLE_MCP` | `true` | 是否启用 `/mcp` 说明指令 |
| `WECHATBRIDGE_ENABLE_SUBAGENT` | `true` | 是否启用 `/agent` 提示改写指令 |
| `WECHATBRIDGE_ADMINS` | _空_ | 逗号分隔的 wxid 列表；检测到新版本时管理员会收到微信通知 |
| `WECHATBRIDGE_UPDATE_CHECK` | `true` | 启动时及每 24h 检查 PyPI 新版本；失败静默不影响运行 |
| `WECHATBRIDGE_UPDATE_CHECK_INTERVAL` | `86400` | 版本检查间隔（秒） |

完整列表见 [`deploy/wechatbridge.env.example`](deploy/wechatbridge.env.example)。

> **为什么改配置位置？** pipx 全局安装后，源码目录下的 `.env` 不再合理。XDG 基础目录布局将配置与代码分离，并且天然支持多实例。

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

或运行升级脚本（无需 clone 仓库，直接用 curl 获取）：

```bash
curl -fsSL https://raw.githubusercontent.com/dorokuma/wechatbridge/main/deploy/update.sh | sudo bash
```

脚本会自动升级 pipx 安装并重启服务。如果服务运行在专用系统用户下（如 `wechatbridge`），以 root 运行时会自动以该用户身份执行 pipx（可用 `WECHATBRIDGE_USER=<用户名>` 覆盖）。

数据存放在 `~/.local/share/wechatbridge/<实例名>/`（会话、SQLite 历史、二维码、登录态），升级**不会**影响——你的 bot 保持登录，对话不丢失。

升级 **major** 或 **minor** 版本（例如 1.2 → 1.3）前，请先查阅 [`CHANGELOG.md`](CHANGELOG.md) 中对应版本的破坏性变更和迁移步骤。

## 部署

### Linux（systemd）

首先，在 `wechatbridge` 系统用户下安装：

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

每个实例读取自己的配置文件（`~/.config/wechatbridge/bot2.env`），数据存放在各自的数据目录（`~/.local/share/wechatbridge/bot2/`）。

#### 内存限制

多实例模板（`deploy/wechatbridge@.service`）内置了 cgroup 内存防护配置：

```ini
MemoryAccounting=yes
MemoryHigh=450M
MemoryMax=512M
```

- **为什么选这些默认值？** 后端 CLI 子进程（如 `agy`）在单轮处理时 RSS 可达约 276 MB 以上，遇到识图或长会话时峰值更高。在 ~1 GB 内存的主机上，过于严格的硬限制（例如 300 MB）会引发剧烈的换页抖动、高昂的 swap 延迟以及流式传输中断（`subscriber fell behind updates`）。`450M` 软限制（`MemoryHigh`）会触发温和的内存回收，而 `512M`（`MemoryMax`）提供了硬顶 ceiling，防止整机 OOM。
- **通过 drop-in 按实例覆盖：** 无需修改基础 unit 模板即可单独调整内存限：
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
- **容量规划指引：** 默认值针对小内存主机（~1 GB RAM）调优。在余量充裕的较大主机上可适当放宽。部署多实例时注意：每个实例运行在独立的 service cgroup 中 — 需按全部运行实例最坏情况下的总上限（`N × MemoryMax`）规划内存预算。
- **与并发的交互：** 重型任务并发（例如多位用户同时发送图片）会成倍增加后端 CLI 的内存开销。在内存受限的主机上，优先降低 `WECHATBRIDGE_MAX_CONCURRENT`（如调至 `2` 或 `1`），再考虑调大内存上限。
- **Swap 配置：** 避免收紧 `MemorySwapMax` — swap 余量是重要的安全缓冲，能在承压时降速运行而非直接触发 cgroup OOM kill。
- **单实例部署：** 单实例模板 `deploy/wechatbridge.service` 现已内置与多实例模板相同的内存限（`MemoryHigh=450M` 与 `MemoryMax=512M`）。如需调整，可通过 `/etc/systemd/system/wechatbridge.service.d/` 下的 drop-in 进行覆盖，并注意将上述 `systemctl` 命令中的单元名相应替换为 `wechatbridge`。限值仅在部署或重铺 unit 文件时生效，`update.sh` 升级不改动 unit 文件。
- **平台适用性：** 这些 cgroup 内存限制仅适用于 Linux systemd 部署；macOS（launchd）与 Windows（任务计划程序）路径没有等价的按实例上限限制。

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
| `/backend <agy\|grok\|codex\|dsh>` | 按微信用户切换 CLI 后端（真切换时清除该后端续聊状态——agy/grok 标记、codex `thread_id`/resume，或 dsh 记忆/会话 ID——下次起新会话；历史文件可能仍在，靠保留策略清理） |
| `/clear` 或 `/new` | 开启新对话（agy/grok：丢弃续聊标记；codex：清空 thread_id；dsh：清空窗口记忆或常驻会话 ID） |
| `/model <名称>` | 设模型（各后端均对照实时列表校验：agy/grok 走 CLI `models`；codex 走 `codex debug models`，失败或空再试 `--bundled`；未知名或列表拉取失败则拒绝且不写 prefs；见 `/models`） |
| `/models` | 列出可用模型——agy/grok/codex 均向 CLI 实时查询（codex：`debug models`；仅在实时列表拉不到时才回退内置参考说明） |
| `/fast` | 设为低推理开销（**只开不关**，不是来回切换） |
| `/planning` | 设为 planning 模式（**只开不关**） |
| `/add-dir <路径>` | **agy：** 校验通过后后续会带 `--add-dir`。**grok：** 只记偏好，暂不传给 CLI |
| `/agents` | 列出可用助手 |
| `/persona <内容>` | 设人格（`show` / `clear` / `reset`） |
| `/version` | 显示当前版本、实例名和后端；若有新版本则显示升级提示 |
| `/mcp` | 短 **使用说明** 文案（可用 `WECHATBRIDGE_ENABLE_MCP` 关掉） |
| `/agent <名称> <任务>` | 拼成「调用子助手…」提示再跑当前引擎（可用 `WECHATBRIDGE_ENABLE_SUBAGENT` 关掉） |

其余 `/…`：有的在微信端禁用（如 `/exit`），有的是 TUI 专用会提示不支持，其余透传给当前 CLI。

`/add-dir` 只接受用户会话目录内，或 `WECHATBRIDGE_ADD_DIR_ROOTS` 列出的根路径下的目录。

## 运维与安全（桥实际管到的）

- **白名单优先。** `WECHATBRIDGE_ALLOWED_SENDERS` 为空 = 能私聊机器人的人都能用。
- **CLI 自动批准。** agy 带 `--dangerously-skip-permissions`；grok 带 `--always-approve`（planning 模式除外）；dsh 的模型工具无宿主路径约束。只适合可信用户，不是多租户沙箱。
- **危险闸门是关键词匹配**，不是完整意图识别。默认针对具体模式（如 `rm -rf /`、管道进 shell、`mkfs`、`format c:`、少量重型中文句式等）。日常里单独一个「删除」**不会**拦。可用 `WECHATBRIDGE_CONFIRM_KEYWORDS` 自定义；确认口令 `WECHATBRIDGE_CONFIRM_TOKEN`（默认 `y`），等待 `WECHATBRIDGE_PENDING_TTL`。
- **入站媒体**有大小上限（默认 20 MB）、流式下载、CDN 域名白名单；缺 `aes_key` 会明确报错。
- **出站产物**只从用户允许目录发出（agy：会话 scratch；grok：会话目录下；dsh：会话目录下），经 `realpath` 检查，且不超过 `WECHATBRIDGE_MAX_OUTBOUND_BYTES`。
- **并发：** 全局同时处理上限默认 4。同一用户串行，排队等自己上一条时**不占**全局槽；不同用户可并行，受全局上限约束（重型并发任务会成倍增加子进程内存开销；见[内存限制](#内存限制)）。
- **长回复**按字数切块（`WECHATBRIDGE_MESSAGE_CHUNK`，默认 2000）。
- **数据目录：** 默认 `~/.local/share/wechatbridge/<instance>/`（可 env 覆盖）。运行目录倾向 `0700`，token/二维码倾向 `0600`（Unix；Windows 依赖 NTFS ACL）。
- **清理：** 会话临时文件与对话历史用不同 TTL（`WECHATBRIDGE_SESSION_RETENTION_DAYS`、`WECHATBRIDGE_HISTORY_RETENTION_DAYS`）。偏好/登录信息不按此删。
- **子进程环境**会剥常见密钥类变量名，并把 `HOME`（Windows 另设 `USERPROFILE`）指到该用户会话目录。

## 已知限制

- 依赖 agy 和/或 grok 和/或 codex 和/或 dsh，本身不是独立 agent。
- **codex** 后端尚未在真实 Codex 订阅/CLI 上实测，仅经源码研究、JSONL fixture 与 fake CLI 测试验证，暂视为社区测试，待真实用户确认。
- **dsh** 后端支持窗口记忆模式（默认，最近 `WECHATBRIDGE_DSH_MEMORY_TURNS` 轮）与常驻会话模式（`WECHATBRIDGE_DSH_RESUME=true`）。逻辑已由测试套件完整覆盖；常驻 resume 模式需用户自行挂载 `dsh-bridge-runner` 插件（本仓库不附带插件文件）。
- dsh 的模型 / 强度 / 模式 / 人格 slash 指令尚未接通；`/model`、`/fast`、`/planning`、`/persona`、`/add-dir` 在 dsh 后端会返回"暂不支持"提示。
- 语音只靠微信转写；无本地 ASR；转写为空会提示改打字。
- 不收发视频；不回原生语音气泡（未做 silk 编码）。
- 一个进程绑一个微信号；多号多实例（`WECHATBRIDGE_INSTANCE`）。
- 产物回传是「允许路径内、能识别到的尽量发」，不是「CLI 在任意位置写的都回传」。
- `/mcp`、`/agent` 不在桥内实现 MCP 协议或托管子进程，只引导或改写提示给 CLI。
- 尽量加白名单，只给可信用户用。

## 贡献

见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。从 1.0.0 起语义化版本，改动记入 [`CHANGELOG.md`](CHANGELOG.md)。

## 许可证

MIT，见 [`LICENSE`](LICENSE)。
