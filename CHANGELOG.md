# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.9.1] - 2026-11-11

### Fixed

- **agy 模型 id 不再凭空拼接**：`canonical_agy_model` 只对实测存在 effort 后缀形态的已知家族（`gemini-4-carbon`/`gemini-4-argon`/`gemini-3.8-flash`/`claude-opus-5-5`/`claude-sonnet-5-5`/`gpt-oss-120b`）折叠 `low|medium|high` 后缀；未知模型名原样透传并记 warning 说明 effort 未生效。实测不存在的档位（`xhigh`/`max`）钳到该家族最高可用档并 WARNING。
- **effort-only 恢复可达**：无 model 仅设 effort（如未设模型直接 `/fast`）时透传 `--effort`，不再静默什么都不传。
- `deploy/wechatbridge.env.example` 访问控制回退为注释态 `# WECHATBRIDGE_ALLOWED_SENDERS=`：`*` 在 `main.py` 走 WARNING 分支比空值 INFO 更吵，且 `*` 放行任何可私聊 bot 的账号，原默认注释理由不成立。

### Tests

- `TestCanonicalAgyModel` 覆盖：已知家族正常档/不可用档钳位、未知模型透传+effort 不落、已带后缀 model wins、空 model。
- `TestAgyModelFlag` 覆盖 argv：已知家族+正常/不可用档、裸已知家族 alias 透传、未知模型+effort 不折叠、effort-only 传 `--effort`、非法 effort-only 不落 flag。

## [1.9.0] - 2026-11-03

### Added

- **`runner_common.format_timeout_error(timeout, backend)`**：四个 `run_*` 的超时分支统一改用它——agy/grok/codex 在原有文案后追加「建议将问题拆小后重试，或发送 `/fast` 切换快速模式、`/new` 开启新对话」；dsh 不支持 `/fast`，提示去掉该句。
- `agy`/`grok`/`codex`/`dsh` 的 `run_*` 与 `_*_subcommand` 显式捕获 `FileNotFoundError`：记 `CLI binary not found` 错误日志后走 `command not found` 归类（提示可 `/backend` 切换引擎），不再落入通用 `Exception` 分支。
- `/help` 统一更新：`/backend` 参数补 `dsh`；`/fast`、`/planning` 说明更正为可 toggle / 可带 `on`/`off`；新增 `/version` 条目；配置好比价密钥时追加 `/price` 条目。dsh 帮助同步补充 `/fast`、`/planning`（标注暂不支持）。
- `agy`/`grok` 的 `/model` 无参时对齐 codex：显示当前模型卡片及切换示例，不再报「缺少参数」。
- 三后端 `/add-dir`：无参显示当前列表+用法（无列表时仅用法），`/add-dir clear` 清空已存目录。
- `price.py` `_safe_env_int(name, default, min_val)`：`PRICE_HTTP_TIMEOUT` 非法值或小于 1 时回退默认 15 并记 warning，不再抛 `ValueError`。
- 登录成功后自动删除二维码临时文件 `qrcode.png` 与 `.current_qrcode_url.txt`。
- 长轮询连续异常后首次恢复时记 `Poll loop recovered after N consecutive error(s)` info 日志。
- codex 零退出但 stdout 含结构化错误（`turn.failed`/`error`）时新增 `codex reported structured error (rc=0)` warning 日志（含重试分支）。
- `format_cli_error` 归类为 `unknown` 且有清洗后详情时新增 warning 日志，便于排查未识别错误。
- **产物识别/回传扩展（批次二）**：
  - `agy` 的 `extract_artifacts` 对齐 `dsh`，支持 4 种产物引用形式：`[name](file:///abs)`、`[name](/abs)`、`[name](./rel)` / `../rel`（相对路径基于 `session_dir` 解析）与裸 `file:///abs`；仅当路径解析后位于允许根（`session_dir` / `config.agy_scratch_dir` / 已验证 `--add-dir`）且文件存在时才收录，显示文本重写不变。
  - **session_dir 快照回退扫描**：把 `codex` 的有界快照工具（200 文件 / 50 子目录 / 2 秒上限）抽为 `runner_common._snapshot_regular_files`，并新增 `collect_session_fallback_artifacts` / `merge_artifact_groups` 共用工具；`run_agy` 与 `run_dsh` 在进程成功退出后对**单用户 `session_dir`**（绝不扫共享 `config.agy_scratch_dir`）做前后快照对比，把本轮新增/mtime 变新且扩展名在白名单（`.pdf .docx .xlsx .pptx .csv .png .jpg .jpeg .gif .webp .svg .zip .html`）内的文件作为链接提取的去重补充（按 realpath 去重）。每个候选经 `is_sensitive_artifact_path` + `st_nlink == 1` + `realpath` 在 `session_dir` 内三重校验；非零退出/超时/失败不扫描。
  - `send_artifacts_back` 跳过 0 字节产物（仅日志不通知）。
  - **出站数量上限**：`MAX_OUTBOUND_ARTIFACTS_PER_TURN = 10`，超出只发前 10 个并合并提示。
- **入站原文件名透传**：文件消息落盘文件名仍为 `uuid.hex[:12]+ext`（安全不变），prompt 附带清洗后的原始文件名（去控制字符/换行/`@`，截前 80 字符）；无附加文本为 `请分析这个文件 @<path> （原文件名：X）`，有附加文本为 `<text>\n\n[附件（原文件名：X）: @<path>]`。
- **消息类型/多附件提示**：消息仅含不支持类型（视频 type=5、表情包、卡片等）时回复“暂不支持该消息类型”气泡；单条消息多于 1 个媒体项时处理首个并在回复前追加“仅处理首个附件”提示。
- **同用户排队提示（默认开）**：同一用户上一条消息仍在处理时，后续消息在等锁前先发一次 `⏳ 上一条消息仍在处理中，本条已加入排队，请稍候…`。锁被占用即提示一次，进入锁后不再重复；发送失败吞掉只记 debug，绝不影响排队与锁释放。`WECHATBRIDGE_QUEUE_NOTICE=0`/`false`/`no` 关闭。
- **长任务进度提示（默认开）**：单条消息处理超过 30 秒未结束，向用户发一次 `⏳ 任务较复杂，仍在处理中，请稍候…`（每条任务只发一次）。watcher 绑在该消息自身的 task 生命周期上，完成/失败/取消均在 finally 中取消，不残留后台 task；发送失败吞掉只记 debug。`WECHATBRIDGE_PROGRESS_NOTICE=0`/`false`/`no` 关闭，阈值可用 `WECHATBRIDGE_PROGRESS_NOTICE_DELAY`（秒）调整。
- 两个开关均在 `config.py` 解析为 `config.queue_notice` / `config.progress_notice`，默认 `true`。

### Fixed

- agy 显示文本泄露服务器路径：裸 `file:///abs` URI 与 `[x](/abs)` / `[x](./rel)` 链接形式此前只剥 `[x](file:///...)`，现与 dsh 同规则只保留链接文字。
- `FileNotFoundError` 误报：session_dir/cwd 不存在或 try 内其它 ENOENT 不再被误报为「助手不可用/未安装」。
- 快照键不一致：`_snapshot_regular_files` 以 realpath 归一化 root，修复 `collect_session_fallback_artifacts` 前后键不匹配。
- `multi_media_notice` 末尾补 `\n\n`，与 `cancelled_notice` 一致。

### Tests

- `tests/test_hardening.py` 新增 `TestQueueAndProgressNotice`（7 条）：排队提示触发/置关不发/发送异常不破队、进度提示按时只发一次/置关不发/快任务不发/发送异常吞掉。锁顺序与 `_prune_user_locks`/`_user_lock_refs` 未动。
- 批次一（UX error & help）：`TestUxErrorAndHelp`——FileNotFoundError → `command not found` 归类（四后端 run_* + agy subcommand）、`format_timeout_error` 各后端文案、`format_cli_error` unknown 日志与 `command not found` 提示、`/model` `/add-dir` 无参行为、登录二维码清理、price 防御解析、poll 恢复日志。
- 批次二（artifacts & media）：`TestAgyExtractArtifactsFourModes`（agy 4 种产物引用 + 允许根/存在性门禁）、`TestSessionFallbackArtifacts`（快照回退新增/mtime/敏感排除/扩展名白名单/硬链接/软链接逃逸/共享 scratch 不扫）、`TestSendArtifactsBackZeroAndCap`（0 字节跳过 + 出站上限 10）、`TestProcessMessageMediaNotices`（不支持类型提示/空列表静默/多附件提示/原文件名透传）。
- 本轮返修：`TestAgyDisplayStripsFilePaths`（agy 显示文本剥裸 `file:///`、`[x](file:///abs)`、`[x](/abs)`、`[x](./rel)`）、`TestAgyImportWithoutYaml`（屏蔽 yaml 后 import wechatbridge.agy 成功）、`TestSnapshotRealpathKeys`（软链接父目录下快照前后键一致）、`TestUxErrorAndHelp` 增补 FNF `e.filename` 指向 session_dir 时不误报「助手不可用」（agy + grok）、`test_fallback_scan_timeout` 改为 patch `runner_common._snapshot_regular_files`、`test_multi_media_notice` 断言提示与正文间有空行。

### Changed

- `format_cli_error` 的 `command not found` 文案末尾追加「或发送 `/backend` 切换其它助手引擎」；`unknown` 兜底文案改为「这次没处理好，请稍后再试。若连续失败，可发送 `/new` 开启新对话或 `/backend` 切换引擎。」
- `_*_subcommand` 通用 `Exception` 兜底文案改为「命令执行失败，请稍后重试。」
- `README.md` / `README.zh-CN.md`：更正 `/fast`、`/planning` 的「只开不关」过时说明，补充 `/add-dir` 列表/清空用法与 `/price` 条目；环境变量表补充 `WECHATBRIDGE_QUEUE_NOTICE`、`WECHATBRIDGE_PROGRESS_NOTICE`、`WECHATBRIDGE_PROGRESS_NOTICE_DELAY`；`deploy/wechatbridge.env.example` 同步。
- `codex._snapshot_regular_files` / `_collect_fallback_artifacts` / `_merge_codex_artifacts` 改为 `runner_common` 共用实现的兼容封装（扫描目录排除集保持原 codex 语义）。

## [1.8.0] - 2026-11-03

### Changed

- **白名单默认收紧（fail-closed）**：`WECHATBRIDGE_ALLOWED_SENDERS` 为空不再表示放开所有人——此时仅 `WECHATBRIDGE_ADMINS` 中的用户可用，两者皆空则拒绝所有人；非空列表语义不变；显式设为 `*` 才恢复放开所有人（升级后如需保持全开，请设置 `WECHATBRIDGE_ALLOWED_SENDERS=*`）。启动日志会按当前生效模式打印一条提示（只打数量，不打 wxid 列表）。

### Fixed

- `user_locks` 清理的两个竞态：`_prune_user_locks()` 原先在 `asyncio.to_thread` 的工作线程里遍历/修改事件循环状态，现移回事件循环线程；并为每用户锁增加引用计数（取锁前 +1、结束后 -1），消除「锁刚释放、等待者未被唤醒时 `locked()` 为 False 被误删导致后来者拿到新锁打破串行」的空隙。
- 附件出站内容比对原先只覆盖会话目录内的凭据文件，遗漏了 dsh/grok/codex 使用的宿主级凭据文件——子进程可读取并改名复制后经会话目录作为附件发出；现已纳入比对清单，且不依赖会话目录是否存在。
- dsh 每用户会话 id 的比对路径更正为其实际存储位置（原清单项指向会话目录内不存在的路径，属于死代码）。
- 白名单中 `*` 与其它项混用时启动日志新增一条告警：`*` 只在单独配置时表示放开所有人，混用时被当作字面用户 id。
- `update.sh` 升级完成后检查目标用户的访问控制配置，未生效配置时打印醒目警告（不阻断升级）。
- codex 首轮/续轮命令的 prompt 参数前统一加 `--` 分隔，以 `-` 开头的消息不再被 codex CLI 解析为选项。
- agy 后端三处 `create_subprocess_exec` 未关闭子进程 stdin，现统一 `stdin=DEVNULL`（与 codex 一致）。
- 删除 `tests/test_hardening.py` 中部一处遗留的 `if __name__ == "__main__": unittest.main()`（保留文件末尾的一处）。

### Security

- 白名单改默认拒绝未知用户（见 Changed）；附件出站额外阻断改名/复制发出的宿主凭据内容；命令行参数注入防护（`--` 分隔）。

## [1.7.4] - 2026-11-03

### Fixed

- **比价模块审计修复（京东/大淘客对接）**：京东签名与请求按官方 SDK `areNotEmpty` 语义过滤空值参数（`access_token=""` 不再参与签名也不发送）；京东 `timestamp` 显式按 GMT+8（北京时间）生成，不再依赖服务器本地时区（网关容差仅 6–10 分钟，UTC 机器差 8 小时必然验签失败）；大淘客签名升级为官方 `signRan`（`MD5(appKey&timer&nonce&key)` 大写，业务参数不签名，不再发送旧版 `sign`）；`get-goods-details` 版本号显式指定为 `v1.2.4`；`handle_price_query` 的 `goods_id` 回退移除 `data.get("id")`（大淘客内部 id 不再被错当淘宝 id）。
- **测试凭据隔离**：`tests/test_price.py` 移除 `os.environ.setdefault` 注入，改为 `mock.patch.object` 修补模块常量，测试不再读取宿主真实 `JDUNION_*`/`DATAOKE_*` 环境变量；签名期望值同步更新为新算法。
- **发布前置测试**：`release.yml` 在校验 tag 与包版本一致之前先对 tag 检出代码运行 `python -m unittest discover -s tests`，测试失败即中止发布。

## [1.7.3] - 2026-11-01

### Added

- GitHub Actions 测试工作流（`.github/workflows/tests.yml`）：push 与 PR 时在 Python 3.10 / 3.11 / 3.12 上跑全量 unittest 套件。此前仓库只有发版与版本检查两个工作流，测试从不在 CI 执行。

### Fixed

- `TestGrokThrottlePreserve` 补上 `_grok_has_credentials` 的 mock：该检查读宿主机 `~/.grok/auth.json` / `XAI_API_KEY`，未 mock 时两个限流用例只在装有 grok 并已登录的机器上通过、干净机器必挂（与 `tests/test_grok_auth.py` 既有做法对齐）。
- `TestPerUserLockContract` 两个串行化用例改用有界泵送等待首条消息进入处理：原用例只 `sleep(0)` 一次便断言，在 Python 3.10 / 3.11 上因 `asyncio.wait_for` 多耗事件循环轮次而必挂（3.12 重写了 `wait_for` 才恰好通过），与版本无关地对齐。

## [1.7.2] - 2026-11-01

### Fixed

- **比价模块对接修复（此前从未真正跑通）**：京东联盟签名改为官方规则 `MD5(secret + 参数升序拼接 + secret)`（原实现只在末尾拼 secret，网关必然验签失败）；大淘客请求补上签名（新增 `DATAOKE_APP_SECRET` 配置，`sign = MD5(参数升序 k=v&… + &key=secret)` 大写，原实现未签名、且未保存 secret）；淘口令解析改走正确接口 `/tb-service/parse-content`（原误用转链接口 `/goods/get-privilege-link`）；商品详情参数由 `id` 改为 `goodsId`。新增 `tests/test_price.py` 以样本载荷锁定签名向量与解析契约。

## [1.7.1] - 2026-10-21

### Fixed

- **agy 会话目录产物回传与安全防护**：允许 `agy` 后端回传用户 `session_dir` 下生成的文档/图片（与 `grok`/`codex`/`dsh` 对齐），同时在全后端与 `send_artifacts_back` 增加 `is_sensitive_artifact_path`（拦截所有隐藏文件/隐藏目录、敏感凭据/配置名及后缀变体、会话凭据内容特征拷贝）与 `os.lstat` / `O_NOFOLLOW` + `os.fstat` 硬链接（`st_nlink == 1`）防逃逸检查。
- **跨后端 `@<path>` 注入与媒体配文消毒**：将 `_sanitize_prompt_at_paths` 提升至 `runner_common.py`，接入 `agy`/`grok`/`codex`/`dsh` 及图片/文件消息配文（支持 URL 解码、非 `file:` 协议拦截、敏感文件与越界链接拦截，放行会话内附件与最多 `MAX_ADD_DIRS=16` 个工作目录）。
- **agy 模型列表解析与模式切换闭环**：修复 `agy models` 制表符（`<id>\t<Display Name>`）输出导致的 `/model` 匹配与写入问题；支持 `/planning off`、`/fast off` 或无参二次调用关闭模式，并在 `/clear`、`/new` 时重置残留 `mode`；支持文件名含有一层平衡括号的产物链接提取与剥离；宿主 OAuth token 更新时自动原子刷新至会话目录。
- **iLink 与比价模块可靠性加固**：`get_updates` 对 5xx/429 状态码上抛触发主循环指数退避；优化 `_encrypt_aes_ecb` 与 `send_media` 内存峰值；`price.py` 修复京东短链域名匹配、将同步 HTTP 请求卸载至 `asyncio.to_thread`、逐跳校验电商重定向域名防 SSRF，并对 `httpx.HTTPError` 脱敏防止回传含 `appKey` 的请求 URL；修复 `deploy/update.sh` 在 `set -euo pipefail` 下因 `grep -q` 触发 `SIGPIPE` 误判未安装的问题。

## [1.7.0] - 2026-09-10

### Added

- **微信内原生比价查询**：新增 `wechatbridge/price.py`，支持京东联盟（京东短链/商品页）与大淘客（淘宝/天猫/淘口令）两个数据源。微信里直接甩京东短链、淘口令等分享码自动触发比价，回「现价/历史最低/优惠券/到手价」卡片；普通商品页 URL 透传给 CLI，可用 `/price <链接|分享码>` 强制比价。未配置 `JDUNION_APP_KEY/SECRET` 或 `DATAOKE_APP_KEY` 时优雅降级（拦截层不激活）。

### Changed

- **空回复文案明确化**：后端成功返回但无文字时，回复由「（这次没有文字回复）」改为「🔔 对话额度已用尽」+ 说明，明确提示是账号额度/用量耗尽，便于用户理解（对应 `EMPTY_REPLY` 常量）。

## [1.6.2] - 2026-09-05

### Changed

- **deploy 模板内存限补全**：单实例 unit `deploy/wechatbridge.service` 内置与多实例模板一致的内存限（`MemoryHigh=450M` + `MemoryMax=512M`）；限值仅在部署或重铺 unit 文件时生效，`update.sh` 升级不改动 unit 文件。

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
