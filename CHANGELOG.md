# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.10.0] - 2026-11-19

### Added
- 新增 `pig` CLI 后端（经 Magpie 暴露 `gemini-web/gemini-4-neon` 单模型），支持每用户隔离配置与会话状态、安全脱敏配置（磁盘零明文密钥）以及 `error`/`aborted`/`length` 状态联合判定。
- 支持按文件魔数自动分流 `@file` 独立参数（静态图片与 ≤64KiB UTF-8 文本独立传参，APNG 与 PDF/大二进制文件内联纯路径），并对 `/`、`@`、`-` 开头提示词自动中性化防误触发内置命令；默认超时 570s（`PIG_TIMEOUT`）。
- `pig` 后端支持 `/models`、`/model`（含 `:low/:medium/:high` 档位切换）、`/fast`、`/planning`、`/persona`、`/add-dir`、`/clear`/`/new`、`/help`、`/agents`、`/mcp` 斜杠命令。
- 新增 `pig` 相关配置项：`PIG_BIN_PATH`、`PIG_TIMEOUT`、`WECHATBRIDGE_MAGPIE_BASE_URL`（仅允许 `https://` 或回环 `http://`）与 `WECHATBRIDGE_MAGPIE_API_KEY` / `MAGPIE_API_KEY`。

### Security
- 凭据扫描与出站脱敏全面覆盖 `.pig`/`.pi` 会话与宿主目录配置文件及内存态 `MAGPIE_API_KEY`（出站正文强制替换为 `[REDACTED]`，阻止敏感文件与密钥外带）。
- 收紧 `pig` 7 天临时目录与 30 天历史会话清理的信任校验：以用户会话目录为信任锚点逐级拒绝软链，并对在途运行与清理操作加锁互斥，防止清理过程被软链劫持或误删宿主目录。

### Fixed
- 补齐 `pig` 错误分类（鉴权失败、网关瞬断、超时及本地续聊会话损坏），并收紧续聊损坏自愈触发条件，避免上游瞬时错误误清会话历史。
- 7 天临时目录清理与 30 天历史会话清理全面覆盖 `.pig` 缓存、日志与会话文件（保留模型与参数配置）。

## [1.9.5] - 2026-11-11

### Security
- 宿主机 `agy` OAuth token 内容拦截同时覆盖 `WECHATBRIDGE_HOST_HOME` 与用户主目录，防止双目录分离时凭据被改名外带。

### Fixed
- 会话目录快照扫描超限时记录告警日志，产物提取改为基于规范真实路径去重，显示文件名剥离规则补齐更多 Bidi/零宽控制字符（如 `U+061C`）。
- 修复 `deploy/update.sh` 无法识别带引号空值访问控制配置的问题。

## [1.9.4] - 2026-11-11

### Fixed
- 当所选 `agy` 模型名自带合法档位后缀且与当前 effort 配置冲突时记录告警提示（仍以模型后缀为准）。

### Changed
- 在配置示例与中英文 README 首装说明中补充 fail-closed 访问控制配置提示。

## [1.9.3] - 2026-11-11

### Fixed
- 拦截并规范化已保存的非法 `agy` 模型档位后缀（已知家族超限后缀钳制至最高可用档、未知模型剥离后缀、无法识别后缀回退裸模型别名），避免直接下发导致执行失败。

## [1.9.2] - 2026-11-11

### Fixed
- 仅对已知支持档位后缀的 `agy` 模型家族折叠 effort 后缀，未知模型名原样透传并记录告警；仅设置 effort（未指定模型）时恢复透传 `--effort` 参数。
- 将 `deploy/wechatbridge.env.example` 中的 `WECHATBRIDGE_ALLOWED_SENDERS` 恢复为注释态默认配置，避免误放开全部访问权限。

## [1.9.1] - 2026-11-04

### Fixed
- 优化 `deploy/update.sh` 对本地路径安装的升级方式：在源目录执行 `git pull --ff-only` 后执行 `pipx upgrade`（不再强制重装清理额外依赖），并在失败时中止退出；改用结构化 JSON/元数据读取已安装来源，兼容含空格路径。

## [1.9.0] - 2026-11-03

### Added
- 统一各后端超时错误提示，在原有文案后追加拆小问题、`/new`（agy/grok/codex 另含 `/fast`）恢复建议。
- 显式捕获后端 CLI 未找到错误，提示用户可通过 `/backend` 切换其它引擎。
- 更新 `/help` 说明：补充 `dsh` 后端、`on`/`off` 开关用法、`/version` 与 `/price`（配置比价密钥时展示）。
- 对齐 `/model` 与 `/add-dir` 无参交互体验：`/model` 展示当前模型卡片与切换示例，`/add-dir` 展示当前目录列表及 `/add-dir clear` 清空用法。
- 扩展 `agy` 产物提取与回传能力：支持 `file:///`、绝对路径与相对路径 4 种引用形式，并为 `agy` 与 `dsh` 增加进程成功退出后的有界会话目录快照回退扫描（自动跳过 0 字节文件）。
- 限制单轮最多回传 10 个产物，超出时发送前 10 个并合并提示。
- 入站文件消息提示词透传清洗后的原始文件名，支持不兼容消息类型提示与多附件仅处理首个附件提示。
- 默认开启同用户消息排队提示（`WECHATBRIDGE_QUEUE_NOTICE`）与超过 30 秒的长任务进度提示（`WECHATBRIDGE_PROGRESS_NOTICE`、`WECHATBRIDGE_PROGRESS_NOTICE_DELAY`）。
- 登录成功后自动清理临时二维码文件；连续长轮询异常恢复及未识别错误均记录日志，且支持 `PRICE_HTTP_TIMEOUT` 非法值安全回退默认值 15。

### Changed
- 优化命令未找到、未知错误及子命令异常的兜底提示文案，引导用户使用 `/new` 或 `/backend` 恢复。
- 同步更新中英文 README 与环境变量示例中的命令用法及排队/进度提示配置说明。

### Fixed
- 修复 `agy` 回复文本在部分链接形式下可能泄露服务器本地路径的问题。
- 修复会话目录或工作目录缺失导致的 `ENOENT` 被误报为助手不可用或未安装的问题。
- 修复软链父目录下会话目录快照键不一致以及多附件提示缺少空行的问题。

## [1.8.0] - 2026-11-03

### Changed
- **默认收紧访问白名单（fail-closed）**：`WECHATBRIDGE_ALLOWED_SENDERS` 为空时不再放行所有人，仅允许 `WECHATBRIDGE_ADMINS` 中的用户访问（两者皆空则拒绝所有用户）；需显式设为 `*` 才放行所有人。启动日志打印当前生效访问模式与数量，当 `*` 与其它用户 ID 混用时输出告警。

### Security
- 附件出站内容校验补齐 `dsh`/`grok`/`codex` 的宿主级凭据文件及 `dsh` 实际会话 ID 存储路径，防止子进程复制或改名后外带；`codex` 命令提示词参数前增加 `--` 分隔防选项注入。

### Fixed
- 修复每用户消息锁清理时的并发竞态（将清理移回事件循环线程并引入引用计数），确保同用户请求严格串行。
- 统一关闭 `agy` 子进程标准输入，并增强 `deploy/update.sh` 升级后未配置访问控制时的告警提示。

## [1.7.4] - 2026-11-03

### Fixed
- 修复比价模块京东与大淘客接口对接问题：京东签名过滤空值参数并显式使用 GMT+8（北京时间）时间戳；大淘客升级为官方 `signRan` 签名并显式指定 `v1.2.4` 接口版本，修复商品 ID 回退误取大淘客内部 ID 的问题。

## [1.7.3] - 2026-11-01

### Added
- 新增覆盖 Python 3.10 / 3.11 / 3.12 的 GitHub Actions 单元测试工作流。

## [1.7.2] - 2026-11-01

### Fixed
- 修复比价模块对接签名与接口错误：修正京东联盟 MD5 签名拼接规则，补齐大淘客请求签名（新增 `DATAOKE_APP_SECRET` 配置），修正淘口令解析接口与商品详情参数名称。

## [1.7.1] - 2026-10-21

### Fixed
- 允许 `agy` 后端回传用户会话目录内生成的文档与图片，同时在全后端附件发送路径加固隐藏文件、敏感凭据内容与硬链接/软链接逃逸防护。
- 统一全后端及媒体配文的 `@<path>` 注入消毒（支持 URL 解码、拦截非 `file:` 协议与越界敏感路径，放行会话附件与最多 16 个已授权工作目录）。
- 修复 `agy` 模型列表制表符解析导致的 `/model` 匹配问题，支持 `/planning off`、`/fast off` 及无参切换与 `/clear`/`/new` 重置，支持含一层平衡括号的文件名链接提取，并支持宿主 OAuth token 自动原子刷新。
- 加固微信长轮询 5xx/429 指数退避、媒体加解密内存峰值、比价模块重定向 SSRF 校验与异常脱敏，以及 `deploy/update.sh` 在 `pipefail` 下的管道误判问题。

## [1.7.0] - 2026-09-10

### Added
- 新增微信内原生比价查询功能：支持京东联盟（京东短链/商品页）与大淘客（淘宝/天猫/淘口令），发送分享码自动返回价格、历史最低、优惠券与到手价信息，也支持 `/price <链接|分享码>` 手动触发；未配置比价密钥时自动降级跳过。

### Changed
- 明确后端成功返回但无文字时的回复文案为「🔔 对话额度已用尽」及说明，便于用户排查账号额度耗尽问题。

## [1.6.2] - 2026-09-05

### Changed
- 为单实例 systemd 模板 `deploy/wechatbridge.service` 补齐与多实例模板一致的内存限制（`MemoryHigh=450M`、`MemoryMax=512M`），并在中英文 README 补充内存限制与 drop-in 覆盖说明。

## [1.6.1] - 2026-09-05

### Changed
- `agy` CLI 出现流中断错误时自动退避 3 秒重试一次以恢复瞬时连接抖动，并新增「助手连接中断」中文错误分类与提示，引导用户通过 `/new` 或重试恢复。
- 将 `deploy/wechatbridge@.service` 内存限制调整为 `MemoryHigh=450M` / `MemoryMax=512M`，避免长会话或识图时触发换页卡顿与流中断。

## [1.6.0] - 2026-08-30

### Added
- 新增 `dsh` 后端常驻会话模式（`WECHATBRIDGE_DSH_RESUME`，默认关闭）：启用后按持久化会话 ID 恢复同一会话保留连续上下文，`/clear` 或 `/new` 重置会话。
- 新增 `dsh` 后端跨轮次长时记忆注入：在单轮模式下自动维护最近对话记忆（支持 `WECHATBRIDGE_DSH_MEMORY_TURNS` 与 `WECHATBRIDGE_DSH_MEMORY_CHARS` 配置），并保留危险指令确认门与策略过滤。
- 新增 `dsh` 私有状态目录隔离配置 `WECHATBRIDGE_DSH_STATE_DIR`（默认 `~/.local/share/wechatbridge/<实例名>/dsh_state`），避免记忆与会话 ID 暴露在会话工作区内。
- 新增 `PyYAML` 依赖并在 `/backend dsh` 切换时自动检测，缺失时给出安装指引。

## [1.5.0] - 2026-08-29

### Added
- 新增 DeepSeek Harness CLI（`dsh`）作为第四个可用后端（可通过 `/backend dsh` 切换）：支持单轮执行、产物链接提取回传、`DSH_HOME` 专属目录与自动清理隔离、越界 `@绝对路径` 过滤，以及 `/help`、`/clear`、`/new` 命令。

### Fixed
- 修复 `dsh` 下 `@~/...` 路径重写、含 `..` 的越界相对路径拦截，以及裸 `file:///` URI 中文结尾文件名误裁剪的问题。
- 校准 README 中关于 `dsh` 路径过滤边界与自动执行工具多租户安全警告的说明。

## [1.4.9] - 2026-08-16

### Fixed
- 修复 `grok`/`codex` CLI 在刷新 token 时原子重写断链导致宿主与会话凭据不同步（第一句正常、第二句失效）的问题：进程退出后（含超时、失败与取消）自动将更新后的会话 `auth.json` 原子回流至宿主并重建软链。

## [1.4.8] - 2026-08-15

### Fixed
- 修复 `codex` 后端在同时存在文件修改与终端命令生成文档（如 PDF/DOCX）时遗漏扫描会话目录的问题，现统一合并去重后回传。

## [1.4.7] - 2026-08-15

### Fixed
- 修复 `grok` 后端因环境变量清理剥离 `XAI_API_KEY` 导致的未登录误报，无宿主凭据且未配置 key 时提前拦截提示。
- 扩展 `grok` 文档回传识别范围（支持更多文件编辑字段及本轮会话目录有界扫描），并自动跳过内部配置目录避免回传无关文件。

## [1.4.6] - 2026-07-30

### Fixed
- 优化错误分类与中文提示：针对请求内容过大（HTTP 413 等）、上下文长度超限与参数无效错误给出明确中文提示并引导使用 `/new`。
- 错误日志仅记录脱敏后的错误分类标签，不再打印原始错误详情，防止日志泄露敏感内容。

## [1.4.5] - 2026-07-30

### Fixed
- 完善 `codex` 产物回传：保留结构化文件变更，无结构化产物时对会话目录执行有界前后差异对比生成摘要回传，并在目录无权访问或不存在时安全跳过。
- 统一 `--add-dir` 目录规范化校验并自动排除内部目录。

## [1.4.4] - 2026-07-29

### Fixed
- 修复 `agy` 与 `grok` 文档回传路径问题：`agy` 对 `file:///` 链接做 URL 解码并二次校验 `--add-dir`，`grok` 支持相对路径产物解析。
- 产物回传被跳过、文件不存在或发送失败时向用户发送仅含文件名的中文提示（不泄露服务器绝对路径），不再静默失败。
- 规范媒体类型映射：仅 JPEG/PNG/GIF/WebP/BMP 作为图片发送，其余文档、SVG、HEIC 与压缩包均作为带文件名的文件消息发送。

## [1.4.3] - 2026-07-29

### Changed
- 收紧 `codex` `/model` 切换校验：实时拉取可用模型列表进行匹配，模型不存在或拉取失败时拒绝写入配置（与 `agy`/`grok` 策略一致）。
- `codex` `/models` 优先展示实时模型列表，拉取失败时回退内置参考说明，并同步更新 `/help` 与文档描述。

## [1.4.2] - 2026-07-28

### Added
- 为所有后端新增统一的上游限流与保护机制：支持控制面或限流失败后的退避重试、全局冷却与单用户静默间隔（可通过 `WECHATBRIDGE_UPSTREAM_*` 与 `WECHATBRIDGE_SLOT_REACQUIRE_*` 环境变量配置），等待期间释放并发槽位并在重试/冷却时发送 `🔔` 提示。

### Fixed
- 细分限流与额度报错文案（区分「助手通道繁忙」「请求较多」与「额度相关」），避免短暂控制面抖动误报为用户频繁请求。
- 修复 `grok` 限流提示识别、零退出结构化限流误标记已初始化以及错误提示被二次覆盖的问题，并收紧 429/quota 误判与通知发送异常保护。

## [1.4.1] - 2026-07-27

- 更新包与模块文档说明以包含全部三个后端（agy、Grok Build、Codex），无功能行为变更。

## [1.4.0] - 2026-07-27

### Added
- **Codex backend**: added OpenAI Codex CLI as the third backend, switchable per user via `/backend codex`, supporting per-user isolated `CODEX_HOME`, persisted thread resume, and authentication via linked `~/.codex/auth.json` or `CODEX_API_KEY`.

## [1.3.5] - 2026-07-27

### Fixed
- 按后端隔离会话初始化标记（`.initialized.<backend>`）避免跨后端污染，并在 `grok --continue` 缺少有效会话时自动降级为新会话重试。

## [1.3.4] - 2026-07-27

### Changed
- Rewrote user-facing WeChat help, empty-list, status, and error messages into plain Chinese while keeping technical details in logs only.
- Acquire the per-user lock before taking a global process slot so queued messages from one user do not starve other users.

## [1.3.3] - 2026-07-26

### Added
- Added memory accounting limit (`MemoryMax=300M`) to `deploy/wechatbridge@.service` to prevent OOM on small hosts.

### Fixed
- Cleanup now removes dangling session symlinks immediately without following symlinks and handles concurrent unlink races safely.
- Reduced log noise for accepted iLink deliveries with non-zero status and prevented oversized artifact notices from leaking server paths.

## [1.3.2] - 2026-07-26

### Changed
- Deployment is now pipx-only; `deploy/update.sh` restarts all matching `wechatbridge*.service` units.

### Fixed
- Routed `/agent` through the dangerous-prompt confirmation gate so it cannot bypass safety checks.
- Fixed orphaned subprocess leaks on timeouts and shutdown, drained in-flight tasks before closing the client on re-login, and added exponential backoff on long-poll and login network errors.
- Fixed `grok` re-sending historical artifacts on every turn and ensured non-zero `grok` exits are treated as failures.
- Allowed voice confirmations for dangerous prompts, cancelled stale pending confirmations on new media, and removed pending confirmations before execution to avoid re-triggers.
- Added best-effort inbound message deduplication, moved scratch cleanup off the event loop, and fixed cleanup stalling on dangling symlinks.
- Fixed output cleaning stripping code generics like `List<String>`, made state/prefs writes atomic, prevented oversized prompts (>120KB) from crashing with `E2BIG`, and stripped additional sensitive env var patterns while hiding `x-encrypted-param` values from upload logs.

## [1.3.1] - 2026-07-26

### Fixed
- Added `wechat.com` subdomains to the CDN download allowlist to fix international media download errors, with graceful fallback to the constructed CDN URL when `full_url` is rejected but encrypted download parameters are available.

## [1.3.0] - 2026-07-26

### Added
- Added PyPI trusted-publishing release workflow, `wechatbridge --version` CLI flag, `/version` slash command, and automatic 24-hour PyPI update checks with admin notifications (`WECHATBRIDGE_ADMINS`, `WECHATBRIDGE_UPDATE_CHECK`, `WECHATBRIDGE_UPDATE_CHECK_INTERVAL`).
- Added XDG configuration file support (`~/.config/wechatbridge/<instance>.env`), deprecated env var auto-mapping, `deploy/update.sh` upgrade script, and `deploy/wechatbridge@.service` multi-instance template unit.

### Changed
- Switched official installation method to `pipx` using direct binary execution in systemd units, removed `requirements.txt`, updated dangerous-confirmation prompts to display `WECHATBRIDGE_CONFIRM_TOKEN`, and aligned bilingual READMEs and platform deployment guides.

## [1.2.2] - 2026-07-25

### Security
- Child environment sanitization now strips `*_API_KEY` and token-style variables anywhere in the name.
- `/add-dir` validates that target paths exist and remain under the session directory or `WECHATBRIDGE_ADD_DIR_ROOTS`.
- Artifact sending resolves `realpath` to prevent symlink escapes, inbound media downloads stream with a `WECHATBRIDGE_MAX_INBOUND_BYTES` (20MB) cap and CDN host allowlist, global concurrency is capped via `WECHATBRIDGE_MAX_CONCURRENT` (default 4), and session/runtime directories use `0700` permissions (`0600` for state/QR files).

### Fixed
- Automatically create parent directories for state and QR files, reply clearly when incoming media lacks `aes_key`, and split long replies losslessly (`WECHATBRIDGE_MESSAGE_CHUNK`).
- Separated temporary file cleanup (`WECHATBRIDGE_SESSION_RETENTION_DAYS`) from dialogue history expiration (`WECHATBRIDGE_HISTORY_RETENTION_DAYS`, default 30 days) as atomic units while preserving preferences and auth.
- Lowered default CLI timeout to 600s (`AGY_TIMEOUT`), refined dangerous-keyword defaults, and corrected state/session path docs in `deploy/wechatbridge.env.example`.

## [1.2.1] - 2026-07-25

### Fixed
- Preferences now store model, effort, and mode per backend so switching via `/backend` no longer passes the previous backend's model.
- Symlink per-session `auth.json` to host `~/.grok/auth.json` (with copy fallback) to avoid stale credentials, stop marking failed runs as initialized, and format `agy` non-zero stdout failures as Chinese error messages.

### Changed
- Standardized Chinese error reply headers and updated `/backend` status and switch replies to display the active model label.

## [1.2.0] - 2026-07-25

### Added
- **Grok Build backend**: added xAI Grok Build CLI as an alternative backend (`GROK_BIN_PATH`, `WECHATBRIDGE_BACKEND`), switchable per user via `/backend agy|grok`, with structured artifact extraction and `--rules` persona injection.
- **Multi-instance support**: added `WECHATBRIDGE_INSTANCE` so state, session, and QR files default under `~/.local/share/wechatbridge/<instance>/`.

## [1.0.6] - 2026-07-23

### Added
- Added macOS launchd service template (`deploy/wechatbridge.plist`), Windows deployment guide (`deploy/wechatbridge-windows.md`), and Platform Support documentation.
- Added exponential backoff with jitter retry strategy for outbound messages and media (up to 5 attempts) and `.env` configuration loading support.

### Changed
- Replaced forced `SIGKILL` with graceful `SIGTERM` + 2-second grace period for `agy` WAL cleanup, and added non-interactive environment flags (`PAGER=cat`, `CI=true`, `NONINTERACTIVE=1`, `PYTHONUNBUFFERED=1`).
- Increased default `AGY_TIMEOUT` (to 3600s) with automatic single-attempt retry on cascade timeout errors, refactored message polling into per-user serialized background tasks, and simplified user-facing prompt messages.

## [1.0.0] - 2026-07-23

First public release.

### Added
- **Text, image, file, and voice bridge** between WeChat and `agy` CLI, including artifact return via CDN upload.
- **Slash commands** (`/clear`, `/model`, `/effort`, `/mode`, `/fast`, `/models`, `/mcp`, `/agent`, `/persona`), dangerous-prompt confirmation gate, sender whitelist, per-user isolated sessions, 7-day scratch cleanup, and systemd deployment files.
