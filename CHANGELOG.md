# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Documentation

- **README（英文/中文）**：新增 systemd 部署内存限制（`MemoryHigh`/`MemoryMax`）说明与按实例 drop-in 覆盖指引。

## [1.6.1] - 2026-09-05

### Fixed

- **agy 流中断分类与提示**：agy CLI 出现 `subscriber fell behind updates`、`connection to the agent was interrupted`、`conversation update stream failed` 等流中断时，新增 `agent_stream_interrupted` 分类与「助手连接中断」中文提示（原落入 unknown 兜底「执行失败」），引导用户发 `/new` 开始新会话或稍后再试。

### Changed

- **agy 流中断自动重试**：检测到流中断错误时自动重试一次（退避 3s 避开瞬时连接抖动），重试有输出即视为恢复；`cascade` 响应超时保持原重试行为（不退避）。
- **deploy 模板内存限调整**：`deploy/wechatbridge@.service` 内存限制由 `MemoryMax=300M` 调整为 `MemoryHigh=450M` + `MemoryMax=512M`，避免 agy 子进程识图/长会话瞬时 RSS（~276M）触顶导致深度换页卡顿与流中断。

## [1.6.0] - 2026-08-30

### Added

- **dsh 后端常驻会话模式**：新增 `WECHATBRIDGE_DSH_RESUME`（默认关）。启用后 headless profile 挂载 `dsh-bridge-runner` 插件（`dsh_bridge_runner.py`），每条消息按 `DSH_BRIDGE_SESSION_ID` **恢复**（`agents.resume`）同一 dsh 会话——上下文无限累积、类似 codex 的 `resume <thread_id>`；`/clear` 或 `/new` 删除会话 ID 开启全新会话。启用时窗口记忆注入自动跳过。
- **dsh 后端长时记忆**：headless 默认单轮每次新建会话，由桥为每用户维护长期对话记忆（默认最近 10 轮，存于每用户 `dsh_memory.jsonl`）并自动注入每次提问；`/clear`、`/new` 清空该记忆重新开始。新增 `WECHATBRIDGE_DSH_MEMORY_TURNS`（默认 10）与 `WECHATBRIDGE_DSH_MEMORY_CHARS`（默认 6000）配置。记忆条目会经 is_dangerous 与策略过滤，用户原文本身已危险时不丢上下文（刚过确认门），组装后才变危险才丢记忆回退裸 prompt；gate_and_run 确认门只看用户原文，run_dsh 对含记忆的 safe_prompt 只打 [AUDIT] 日志、不拦截。
- **dsh 私有状态目录隔离**：新增 `WECHATBRIDGE_DSH_STATE_DIR`（默认 `~/.local/share/wechatbridge/<实例名>/dsh_state`），将 dsh 窗口记忆文件（`dsh_memory.jsonl`）与持久会话 ID 独立存放于会话工作区外，消除子进程 cwd 一级相对路径穿越（`../`）。威胁模型：同 UID 无沙箱环境下，二级相对穿越（`../../dsh_state/<user_id>`）可达且已接受。
- **依赖新增与前置探测**：新增 `PyYAML` 依赖（用于解析 dsh profile 插件挂载状态）。在 `/backend dsh` 切换指令执行前增加依赖前置探测，缺失 PyYAML 时友好提示安装（`pip install PyYAML` 或 `pipx inject wechatbridge-cli PyYAML`）并保持原引擎偏好不变。

## [1.5.0] - 2026-08-29

### Added

- **dsh 后端**：新增 DeepSeek Harness CLI（`dsh`）作为第四个 CLI 后端（`/backend dsh`）。以 `dsh --profile headless -- <prompt>` 单轮运行，cwd 为每用户会话目录；从输出提取 `file:///` 与 markdown 文件链接作为可回传产物（路径字符白名单避免误吞紧邻中文）；`DSH_HOME` 显式配置时启用专用主目录及自动会话保留清理，未配置时复用宿主 `~/.dsh` 且跳过自动清理；对会话目录外的 `@绝对路径` mention 予以屏蔽拦截（替换为 `[blocked-path]`）；`/help`、`/clear`、`/new` 支持，模型/强度/人格类指令暂返回"暂不支持"。headless 每次新建会话，故为单轮模式。

### Fixed

- **dsh `@~/...` 路径重写为绝对路径**：`_sanitize_prompt_at_paths` 在映射判定通过后将 `@~` 与 `@~/...` 重写为绝对路径 `@<session_dir>/...`，使 dsh 的 `read` 工具可直接读取（dsh `read` 工具不展开 `~`），越界保留替换为 `[blocked-path]`。
- **dsh 裸相对 `..` 越界路径拦截**：将包含 `/` 且含 `..` 片段的裸相对路径（如 `@a/../../userB/x`）纳入 candidate 处理，经 `session_dir` 判定越界后替换为 `[blocked-path]`，会话界内原样保留。
- **dsh 裸 `file:///` URI 尾部 CJK 裁剪 exists 兜底**：`_parse_bare_file_uri` 在剥除尾部 CJK 注释前先校验本地路径是否存在；真实存在时整段保留不裁，避免合法中文结尾文件名（如 `photo说明`）被截断导致 not found。

### Documentation

- **README dsh 路径过滤表述与沙箱警告校准**：修正 README / README.zh-CN 中对 dsh mention 过滤的表述（明确为 prompt 文本层的 best-effort 过滤，仅拦截绝对路径与 `~` 开头等越界 mention，非沙箱边界），并将自动执行工具非多租户沙箱的安全警告适用范围扩展至 dsh（模型工具无宿主路径约束）。

### Tests

- 新增 `tests/test_dsh.py` + `tests/fake_dsh.py`：命令构造、产物提取、成功/失败/超时路径、未登录预检、slash 指令、`/backend dsh` 切换、`~` 路径展开与绝对路径重写、裸相对 `..` 越界拦截与界内保留、含 CJK 结尾文件名提取与剥除。

## [1.4.9] - 2026-08-16

### Fixed

- **凭证轮换后第二句必挂（promote 回流）**：grok/codex CLI 刷新 token 用「临时文件+rename」原子写，会把手机会话 `auth.json` 的 symlink 拆成普通文件，新凭证只落在 session，宿主仍是已吊销的旧凭证，下一句 `_sync` 又用旧凭证覆盖 → 第一句过第二句挂。修复：session 普通 `auth.json` 一律视为更新凭证，先原子写回宿主（promote），成功后再重建 symlink；promote 失败（含空文件校验失败）绝不 unlink session 文件；子进程退出后 harvest 回流，覆盖成功/非零退出/超时/取消/`--continue` 重试等所有路径。

### Tests

- 新增 `tests/test_grok_auth.py`、`tests/test_codex_auth.py`：promote 回流与重建链接、CLI rename 拆链后下一句回流、promote 失败保留 session 文件且不覆盖宿主、空文件拒绝覆盖宿主、宿主目录缺失时自动创建（0o700）、run/subcommand 退出后 harvest。

## [1.4.8] - 2026-08-15

### Fixed

- **Codex 文档回传**：成功回合不再要求「没有任何 `file_change`」才扫会话目录。先写出脚本再用终端生成 pdf/docx 时，文档会和结构化产物合并去重后回传；覆盖已有文档也会收。仍只扫 `session_dir`，不扫 `--add-dir`，认证逻辑未改。

### Tests

- `ok_shell_mixed_docs`：`file_change` 的 helper.py + 终端 `report.pdf` 都会进列表；覆盖已有 pdf 也会收。

## [1.4.7] - 2026-08-15

### Fixed

- **Grok 未登录误报与 headless 认证**：`sanitize_env` 会剥掉 `XAI_API_KEY`，grok 子进程即使桥进程配了 key 也会报 `Not signed in`。grok 后端现在把 `XAI_API_KEY` 重新注入子进程；没有宿主 `~/.grok/auth.json` 且没有 key 时不再空跑 CLI。
- **Grok 文档回传**：除 `write`/`edit`/`str_replace` 的 `file_path` 外，也认 `search_replace` 以及 `path`/`target_file`。Grok 用 `run_terminal_command` 生成的 pdf/docx 不会出现在写文件工具里，改为对本轮 `session_dir` 做有界扫描（跳过 `.grok`/`.gemini`/`.codex` 等内部树，避免把 bundled 税表 PDF 发回微信）。

### Tests

- grok `search_replace`+`path` 提取、session 扫描跳过 `.grok` bundled、`XAI_API_KEY` 回注。

## [1.4.6] - 2026-07-30

### Fixed

- **Oversized payload/request body errors**：payload/request body 过大（HTTP 413 等）时向用户提示「请求内容过大」并引导发 `/new` 开始新会话，不再显示原始英文错误。
- **Context/token 超限**：会话内容超出模型上下文窗口限制时提示「会话内容过长」并引导 `/new`，不再自动清会话或重放。
- **Generic `INVALID_ARGUMENT`**：非 payload/context 类的 `INVALID_ARGUMENT` 错误统一提示「请求参数无效」，不再透出原始英文。
- **Error logging**：`format_cli_error` 日志改为只记录 category 分类（如 `category=payload_too_large`），不再记录原始错误文本，避免日志泄露敏感信息。
- **Error classification refactor**：新增 `_classify_cli_error` 纯函数，将错误分类逻辑从用户面向文案中解耦，方便单测覆盖和后续扩展。

## [1.4.5] - 2026-07-30

### Fixed

- **Codex artifact relay**：官方 `file_change` 产物保留；成功但无结构化产物时，对私有 session 目录做有界前后快照差异（`diff --no-index -U0`），提取可读变更摘要供微信回传。
- **`add_dirs` 统一校验**：回传前对所有目录做 `realpath` 标准化并排除内部目录（`.codex`、`sessions` 等）；校验失败时不扫描目录内容。
- **快照失败不扫描**：目录不存在或无权访问时跳过差异扫描，不抛异常。

## [1.4.4] - 2026-07-29

### Fixed

- **三后端文档回传**：agy 对 `file:///` 链接做 URL 解码（空格/中文路径不再漏传）；agy 回传闸门与 codex 一样二次校验 `--add-dir`，合法目录内文档可回微信。
- **回传失败不再静默**：白名单跳过、文件不存在、发送失败、异常时向用户发中文提示（只显示文件名，不回显服务器绝对路径）；超大文件提示逻辑不变。
- **媒体类型**：仅 jpeg/png/gif/webp/bmp 走图片消息；svg/heic 等与 pdf/docx/xlsx/txt/zip 等一律走文件消息（带 `file_name`）。
- **grok 相对路径产物**：`write`/`edit`/`str_replace` 的相对 `file_path` 按 `session_dir` 解析后再提取（对齐 codex）。

### Tests

- 覆盖 unquote、agy add-dir 闸门、MIME→media_type、失败提示无路径泄露、grok 相对路径提取。

## [1.4.3] - 2026-07-29

### Fixed

- **codex `/model` 严格校验**：切换时通过本机 `codex debug models`（失败或空再试 `--bundled`）拉取模型 slug 列表；精确/前缀匹配成功才写入 prefs。未知模型 → `❌ 模型不存在` 且不写 prefs；列表拉取失败/空/超时 → `❌ 无法获取模型列表` 且不写 prefs（与 agy/grok 一致的严格策略，不再“未校验直接存名字”）。

### Changed

- **codex `/models`**：优先展示实时列表（via `debug models`）；拉取失败时才回退内置参考说明。
- **codex `/help` 与文档**：去掉「无列模型 / 未校验」旧说法，改为与 agy 类似「用 `/models` 查看」。
- **`fake_codex` 测试夹具**：支持 `debug models` / `--bundled` 子命令，供校验路径单测与集成测使用。

## [1.4.2] - 2026-07-28

### Added

- **Upstream throttle guard (all backends)**: agy / grok / codex share `_run_llm_with_guard` — limited backoff retries after control-plane / rate-limit style failures, global cooldown, and per-user silent gap after a throttle. Retry and cooldown notices use 🔔; final throttle-class errors also use 🔔. Config: `WECHATBRIDGE_UPSTREAM_RETRY_MAX`, `WECHATBRIDGE_UPSTREAM_BACKOFF`, `WECHATBRIDGE_UPSTREAM_COOLDOWN`, `WECHATBRIDGE_UPSTREAM_USER_GAP`, `WECHATBRIDGE_UPSTREAM_QUOTA_RETRY_MAX` (quota defaults to no extra retries). Documented in `deploy/wechatbridge.env.example`.
- **Concurrency-friendly waits**: user gap and global cooldown run before taking a global process slot; in-slot backoff releases the slot while sleeping and re-acquires with timeout (`WECHATBRIDGE_SLOT_REACQUIRE_TIMEOUT` / `ATTEMPTS`), falling back to the existing “现在有点忙” path if the pool stays full.

### Fixed

- **Error copy split**: `RESOURCE_EXHAUSTED` / eligibility / bare 429 map to “助手通道繁忙”, explicit rate limits to “请求较多”, real quota exhaustion to “额度相关” — no longer a single “请求过于频繁” that looks like user spam when Google control-plane eligibility flickers.
- **Grok 🔔 contract**: failure / already-formatted detection no longer requires `❌` only; bridge error bubbles (🔔 or ❌) are not re-`format_cli_error`’d into generic “执行失败”, so outer retry/cooldown still works. Zero-exit structured throttle no longer `mark_initialized`. `/model` treats 🔔/❌ bubbles as fetch failure, not a model list.
- **Stricter throttle detection**: only real bridge 🔔 bubbles count as throttle replies; bare substring `429` / vague “quota” wording tightened to reduce false positives.
- Guard notification `send_message` failures are logged and do not abort the retry path.

### Tests

- Hardening coverage for guard A/B/C, slot re-acquire, format_cli_error branches, and mocked `run_grok` throttle paths.

## [1.4.1] - 2026-07-27

### Fixed

- **Package description/docstring consistency**: the `wechatbridge` package docstring and the `runner_common.py` module docstring now list all three backends (agy, Grok Build, and Codex) instead of only agy/Grok. No behavior change.

## [1.4.0] - 2026-07-27

### Added

- **Codex backend**: third CLI backend (OpenAI Codex) alongside agy and grok, switchable per-user via `/backend codex`. Runs `codex exec --json` for single turns and `codex exec resume <thread_id> <prompt>` to continue (thread id persisted per user). Each WeChat user runs with `HOME` and `CODEX_HOME` isolated to their own session dir. Auth via a linked host `~/.codex/auth.json` (or `CODEX_API_KEY` in the bridge environment); no key or token values are committed.
  - **Verification boundary:** no real Codex subscription or CLI was available for live testing. The backend is implemented against the documented `codex exec --json` / `resume` contract and validated by the in-repo test suite using a JSONL fixture and a fake `codex` CLI (the full fake-CLI/fixture test suite passes). Real end-to-end acceptance against the actual Codex CLI is pending real-user testing.

## [1.3.5] - 2026-07-27

### Fixed

- **grok `--continue` 报 "这次没处理好"**：`.initialized` 标记改为按后端隔离（`.initialized.grok` / `.initialized.agy`），防止跨后端污染。加 `--continue` 前运行时检查 grok session 是否真实存在；若 `--continue` 因无 session 失败，自动降级为新会话重试。`_clear_initialized_if_no_history` 同步改为按后端分别清理。

## [1.3.4] - 2026-07-27

### Fixed

- **User-facing WeChat copy in plain language**: help, empty `/agents` list, busy/image/file/voice notices, backend switch labels, and error mapping rewritten into short Chinese; internal jargon (CLI/MCP/cascade, English blobs, server paths, `aes_key`/CDN detail) stays in logs only.
- **Residual “agent” wording**: confirmation UI and empty-list/help text talk about 助手; execution path still uses `invoke_subagent` / backend agent names where the CLI requires them. `/agent` danger confirm shows a user-friendly `display_prompt` without changing the crafted backend prompt.
- **Concurrency fairness**: take the per-user lock **before** acquiring a global process slot so same-user queue wait does not occupy `WECHATBRIDGE_MAX_CONCURRENT` slots and starve other users.

### Changed

- **Mandatory per-cut version bump**: any user-visible behavior/fix/feature change must raise `__version__` (one cut → one patch). Formal CHANGELOG section required; do not stack long-lived work only under `[Unreleased]`.
- `scripts/check_version_bump.py` (+ CI `version-check.yml`) fails when package paths changed since the latest `vX.Y.Z` tag but `__version__` / CHANGELOG were not advanced. Documented in `CONTRIBUTING.md`.

## [1.3.3] - 2026-07-26

### Fixed

- **Dangling session symlinks**: cleanup now unlinks broken file/dir links immediately (not only after TTL), never follows symlinks while walking scratch trees, and treats unlink races (ENOENT) as success.
- **iLink `ret=-1` + `message_id` log noise**: delivery decision centralized in `ilink_delivery_accepted`; non-zero ret with message_id is delivered at debug level, not warning.
- **Oversized artifact replies leaked server absolute paths** to WeChat; user text goes through `format_oversized_artifact_notice` (name + size only).

### Added

- `tests/test_hardening.py` — real-path probes: split fidelity, path containment, dangling/outside-symlink cleanup, async `send_artifacts_back` / `_post_sendmessage_with_retry` mocks, env sanitizer (`python -m unittest`).

### Changed

- `deploy/wechatbridge@.service` recommends `MemoryMax=300M` (with accounting) so a single busy instance cannot OOM a small host.

## [1.3.2] - 2026-07-26

### Fixed

- **`/agent` bypassed the dangerous-prompt confirm gate**: backend handlers invoked the CLI directly without `is_dangerous` checking. The command is now handled centrally in `main.py` and always routed through `gate_and_run`.
- **Orphaned subprocesses**: agy's cascade-timeout retry process was never terminated when the retry itself timed out; agy/grok subcommands (`/models`, `/agents`) also leaked their child process on the 30s timeout. All timeout/cancel/error paths now terminate the process group; `CancelledError` during shutdown kills the child before propagating.
- **Re-login raced in-flight message handlers**: `client.close()` ran while background tasks still used the connection, so replies silently failed after retry loops. In-flight tasks are now drained (up to 90s) or cancelled before the client closes, and all `create_task` calls hold strong references to prevent premature GC.
- **No backoff on long-poll network errors**: `get_updates` swallowed `RequestError` and returned empty results, making the main loop retry at full speed with log flooding. Network errors now propagate and the main loop backs off exponentially (0.5s → 30s cap).
- **Login-flow network errors killed the daemon** (only systemd `Restart=always` saved it). Errors are now caught and retried after 5s.
- **grok re-sent every historical artifact each turn**: `--continue` sessions accumulate `chat_history.jsonl`, and all past write/edit paths were re-collected every message. Only files modified during the current run are returned now.
- **grok treated non-zero exits with JSON stdout as normal replies**; aligned with agy — non-zero exit is always a failure.
- **`clean_scratch()` blocked the event loop** (sync file-tree walk in async context); now runs in a thread.
- **Voice/image/file messages could not confirm dangerous prompts**, and media sent while a confirmation was pending was silently dropped. Voice transcriptions can now reply with the confirm token; media cancels the pending confirmation and is processed normally with a notice.
- **Stale pending confirmation could be re-triggered** when the confirmed run raised before the entry was deleted; the entry is now removed before execution.
- **No inbound message dedup**: server redelivery or restart replay ran the LLM twice. Best-effort LRU dedup aligned with the official `WeixinMessage` schema (`message_id` → `client_id` → per-item `msg_id` → `seq`; no-op when the server sends none), with a one-time INFO log naming the active field.
- **Session cleanup choked on dangling symlinks** (e.g. stale venv `bin/python` links in scratch): `os.path.getmtime` followed the link and raised on every hourly run, spamming warnings and never expiring the link. Cleanup now uses `lstat` so dangling links age out by their own mtime.
- **Internal exception text leaked to WeChat users** (server paths, internal URLs). Generic agy/grok errors now return a fixed message; media download errors only pass through crafted `ValueError` reasons.
- **`clean_output` corrupted code output**: the HTML-tag regex stripped generics like `List<String>`. Now a whitelist of real HTML tags.
- **Non-atomic state/prefs writes** could leave half-written JSON on crash; both now write tmp + `os.replace`.
- Removed dead `interval` parameter from `poll_qrcode_status`; rewrote the fragile string-matching Content-Length check in media download.
- **Sensitive-env sanitizer gaps**: added `PASS`/`PWD`/`AUTH`/`CRED`/`CREDS`/`PRIVATE` segments so names like `DB_PASS` or `ANTHROPIC_AUTH` are stripped from child environments.
- **Oversized prompts crashed with E2BIG**: prompts over 120KB are rejected up front with a clear message.
- CDN upload log no longer prints the `x-encrypted-param` value (it authorizes media download); only its length is logged.

### Changed

- **Deployment is pipx-only**: running from the source checkout (`python -m wechatbridge` + `WorkingDirectory`) is deprecated. `deploy/update.sh` now enumerates all `wechatbridge*.service` units for restart (covers template instances and legacy names; removes the always-true `list-unit-files` check).
- **Release workflow hardening**: all third-party actions pinned to commit SHAs (checkout v6.0.3, setup-python v7.0.0, gh-action-pypi-publish v1.14.1, action-gh-release v2.6.2); the workflow now fails instead of publishing an empty GitHub Release when the CHANGELOG section for the tag is missing.

## [1.3.1] - 2026-07-26

### Fixed

- **CDN download allowlist now includes `wechat.com`**: international CDN domain `wechat.com` (e.g. `novac2c.cdn.wechat.com`) was missing from `_is_allowed_media_url()`, causing SSRF-protection false positives ("拒绝非微信 CDN 下载地址") for users whose iLink server returns international media URLs. Any subdomain of `wechat.com` is accepted.
- **Graceful fallback when full_url fails allowlist check**: `download_and_decrypt_media()` now logs a warning and falls back to the constructed CDN URL when `full_url` is rejected but `encrypt_query_param` is available, instead of raising a hard error. Fully blocked non-whitelist URLs without a fallback still raise `ValueError` with the rejected URL.

## [1.3.0] - 2026-07-26

### Added

- **PyPI release workflow**: tag-triggered GitHub Actions workflow that validates tag-version consistency, builds, publishes to PyPI via trusted publishing, and auto-creates a GitHub Release from CHANGELOG.
- **Built-in update check**: on startup and every 24 hours, silently checks PyPI for a newer version. New version is logged, reported to admin WeChat contacts (`WECHATBRIDGE_ADMINS`), and shown in `/version`.
- `wechatbridge --version` CLI flag prints the current package version.
- `~/.config/wechatbridge/<instance>.env` configuration location (XDG_CONFIG_HOME support). Priority: `$WECHATBRIDGE_ENV_FILE` > XDG instance file > XDG default file > repository `.env` (deprecated).
- Deprecated environment variable auto-mapping via `config._DEPRECATED_ENV`.
- `deploy/update.sh` — one-command upgrade script.
- `deploy/wechatbridge@.service` — systemd multi-instance template unit.
- `/version` slash command — displays version, instance, backend; shows upgrade hint when newer release is available.
- `WECHATBRIDGE_ADMINS`, `WECHATBRIDGE_UPDATE_CHECK`, `WECHATBRIDGE_UPDATE_CHECK_INTERVAL` environment variables.

### Changed

- **Installation method changed to pipx** as the sole official install path. The old `git clone + pip install -r requirements.txt` path is removed; developers use `pip install -e .` from a clone.
- Version single source of truth is `wechatbridge/__init__.py` (`__version__`); `pyproject.toml` uses `dynamic = ["version"]`.
- systemd unit now uses the pipx-installed binary directly (`%h/.local/bin/wechatbridge`) instead of `python -m wechatbridge` with a `WorkingDirectory`.
- Bilingual READMEs aligned with real capabilities: dual backend (agy/grok), tightened wording for danger gate, artifacts, `/mcp`/`/agent`, `/fast`/`/planning`, grok `/add-dir`; added Ops & security section.
- Dangerous-confirm WeChat prompt now shows `WECHATBRIDGE_CONFIRM_TOKEN` instead of hard-coded `y`.
- Package description (`pyproject.toml`, package docstring) mentions agy and Grok Build.
- Windows deploy notes use instance-scoped paths (`…\wechatbridge\<instance>\…`).
- Repository root `.env` fallback emits a deprecation warning on startup.
- Windows and macOS (launchd) deploy notes updated for pipx installs; `deploy/update.sh` defaults to the `wechatbridge` system user when run as root.

### Removed

- `requirements.txt` (dependencies are declared in `pyproject.toml` only).

## [1.2.2] - 2026-07-25

### Security / Hardening

- Child env sanitization now strips `*_API_KEY` / token-style names (not only vars whose names *start with* KEY/TOKEN).
- `/add-dir` validates path exists and stays under session dir or `WECHATBRIDGE_ADD_DIR_ROOTS`.
- Artifact send uses `realpath` so symlinks cannot escape the allowed root.
- Inbound media size cap (`WECHATBRIDGE_MAX_INBOUND_BYTES`, default 20MB); CDN download URL host allowlist.
- Inbound CDN download is **streamed** and aborts as soon as the size cap is exceeded (no full-body buffer when Content-Length is missing).
- Global concurrency limit (`WECHATBRIDGE_MAX_CONCURRENT`, default 4) with busy reply when full.
- Session dirs and runtime data dirs created as `0700`; QR/state files `0600`.

### Fixed

- State/QR parent directories are created automatically (avoids silent save failures on new instance).
- Images/files missing `aes_key` now reply with a clear error instead of silent drop.
- Long WeChat replies are split into chunks (`WECHATBRIDGE_MESSAGE_CHUNK`); split is lossless (`''.join(chunks) == original`).
- Session cleanup: temps (media/`.cache`/scratch) use `WECHATBRIDGE_SESSION_RETENTION_DAYS` (default = scratch TTL); **dialogue history** uses separate idle TTL `WECHATBRIDGE_HISTORY_RETENTION_DAYS` (default **30** days). History is expired as **units** (SQLite `*.db`+wal/shm together; brain/session trees by newest mtime) so partial deletes cannot corrupt an active DB. Prefs/auth kept; clears `.initialized` when no history remains. Idle user locks pruned.
- Default CLI timeout lowered to 600s (still overridable via `AGY_TIMEOUT`).
- Dangerous-keyword defaults avoid bare Chinese words like「删除」; focus on concrete destructive patterns.
- `deploy/wechatbridge.env.example` state/session path docs corrected.

## [1.2.1] - 2026-07-25

### Fixed

- **Backend switch kept the old model**: switching `/backend` only changed the backend flag; the previous backend's model (e.g. gemini) was still passed to the new CLI and failed on first message. Preferences now remember **model / effort / mode per backend**; first visit uses empty (CLI default), later visits restore the user's last choice.
- **Grok false "Not signed in"**: per-session `auth.json` was a one-shot copy of host credentials and went stale or missing while host login remained valid. Session auth now **symlinks** to host `~/.grok/auth.json` (copy fallback).
- **Error reply style**: CLI English errors were stuffed into `❌ **...** ❌` titles. Replies now use a fixed Chinese header line plus body; known cases (login, rate limit, network, timeout, bad model, etc.) get Chinese titles.
- Failed Grok/agy runs no longer mark the session as initialized (avoids `--continue` on a broken first turn).
- agy non-zero exits with English stdout are formatted as errors instead of sent as normal replies.

### Changed

- Shared helpers in `runner_common.py`: `by_backend` prefs, `switch_backend_prefs` / `update_active_prefs`, `format_error` / `format_cli_error`, `EMPTY_REPLY`.
- `/backend` status and switch replies show the active model label.
- Version bumped to 1.2.1.

## [1.2.0] - 2026-07-25

### Added

- **Grok Build backend**: support for xAI Grok Build CLI as an alternative to agy, switchable per-user at runtime via `/backend agy|grok` command.
- **Runtime backend switching**: `/backend` slash command to switch CLI backend without restarting; each backend has isolated sessions, persona, and model preferences.
- **Multi-instance support**: `WECHATBRIDGE_INSTANCE` env var; all per-instance paths (state/session/qrcode) derive from the instance name. Deploy N instances with identical service templates.
- **Instance-derived paths**: state/session/qrcode files now live under `~/.local/share/wechatbridge/<instance>/` by default.
- Grok artifact extraction via structured `chat_history.jsonl` tool_calls (write/edit file_path).
- Persona injection for grok via `--rules` flag.
- `GROK_BIN_PATH` and `WECHATBRIDGE_BACKEND` config options.

### Changed

- Refactored shared logic (session isolation, prefs, process management, dangerous detection) into `runner_common.py` module.
- `agy.py` and new `grok.py` both import from `runner_common.py`; behavior unchanged for agy users.
- `main.py` now dispatches to active backend based on per-user preference.
- Startup log shows active backend and instance name.


### Added

- `deploy/wechatbridge.plist` — macOS launchd service template.
- `deploy/wechatbridge-windows.md` — Windows deployment guide.
- Platform Support section in both `README.md` and `README.zh-CN.md`.

## [1.0.6] - 2026-07-23

### Changed

- Replaced forced `SIGKILL` process termination with a graceful `SIGTERM` 2-second grace period to allow `agy` CLI to gracefully unlock SQLite WAL database files and close Cascade session handles, preventing Cascade lock deadlocks on subsequent `-c` invocations.
- Added `PAGER=cat`, `CI=true`, `NONINTERACTIVE=1`, and `PYTHONUNBUFFERED=1` environment flags to prevent subshell commands from hanging on headless standard input reads.


### Changed

- Increased default `AGY_TIMEOUT` from 900s to 3600s (60 minutes / 1 hour) to fully support long-horizon complex programming tasks without early process termination.
- Added automatic single-attempt retry and friendly fallback formatting when encountering `timeout waiting for cascade/response` API errors from the AI engine.


### Changed

- Refactored message polling loop to spawn non-blocking background async tasks per message (`asyncio.create_task`), ensuring the `get_updates` heartbeat channel remains 100% active 24x7 without disconnecting during long AI task executions.
- Added per-user async locks (`user_locks`) to maintain message sequence ordering per user while enabling full inter-user concurrency.


### Added

- Robust exponential backoff with random jitter retry strategy for `send_message` and `send_media_message` (up to 5 attempts covering 30-60s network recovery window).
- Failure classification: retries transient network errors & 5xx server errors, fails fast on 401/403 auth errors and 4xx client errors.


### Added

- Support for loading configuration settings from `.env` file automatically on startup.
- `.env` and `.env.example` configuration file templates for project settings.

### Changed

- Increased default `AGY_TIMEOUT` from 180 seconds (3 minutes) to 900 seconds (15 minutes) for long-running AI tasks.


### Changed

- Simplified user-facing prompt messages: removed verbose explanatory suffixes, changed "用法" to "缺少参数" where appropriate, removed parenthetical notes and redundant explanations.

## [1.0.0] - 2026-07-23

First public release.

### Added

- **Text bridge** — send text from WeChat to agy CLI, receive reply.
- **Image recognition** — send images from WeChat, bridge passes to agy for description/analysis.
- **File input** — send any file (PDF, docx, code, etc.) from WeChat; bridge decrypts and feeds to agy via path reference.
- **Voice passthrough** — WeChat voice message transcriptions (`voice_item.text`) fed to agy as text; returns a "can't hear you, please type" prompt when no transcription is available.
- **Artifact return** — files generated by agy (documents, images, code) are sent back to WeChat via CDN upload as image_item or file_item.
- **Slash commands** — `/clear`, `/model`, `/effort`, `/mode`, `/fast`, `/models`, `/mcp`, `/agent`, `/persona` for runtime control.
- **Dangerous prompt confirmation gate** — suspicious prompts (delete, format, rm -rf) intercepted with a yes/no confirmation before execution.
- **Sender whitelist** — restrict access to specific WeChat IDs (empty = allow all).
- **Per-user sessions** — isolated agy workspaces per WeChat user.
- **Scratch TTL cleanup** — periodic removal of generated artifacts older than 7 days.
- **Systemd deployment** — service files for production use with auto-restart.
