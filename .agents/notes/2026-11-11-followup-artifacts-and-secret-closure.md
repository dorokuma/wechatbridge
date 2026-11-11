# `fix/followup-artifacts-and-secret`（v1.9.5）收口 — consider/观察项登记

来源：本批 C 档门禁三审（verifier 双快照隔离实测 / reviewer / oracle）的
consider 与观察项，收口现场记录。本批已合并 main（merge commit
`eae9838`），版本 `1.9.5`，矩阵与结论均经本轮实测复核。

## 闭环结论（先于 consider 项）

- **O1（已闭环）显示名剥离**：`wechatbridge/main.py:80`
  `_DISPLAY_FILENAME_STRIP_CHARS` 已覆盖零宽 U+200B–U+200F、双向控制符
  U+202A–U+202E / U+2066–U+2069、ALM U+061C、BOM U+FEFF、半角 `@` 与全角
  `＠` U+FF20，另加 `<0x20` 与 `0x7F`；回归
  `test_sanitize_display_filename_strips_invisible_and_bidi` 覆盖。
  仅视觉欺骗面，落盘名仍走 `uuid.hex[:12]+ext`。
- **O2（已闭环）快照/回退扫描超限 WARNING**：
  `runner_common._snapshot_regular_files._abort` 记 WARNING（限额类别 +
  目录，不记文件名/内容）；`test_snapshot_limit_aborts_with_warning` 断言
  已收紧为精确匹配三类限额 `"file-count"` / `"directory-count"` /
  `"scan-time"`。
- **O4（已闭环，原始前提 refuted-with-evidence）**：旧 O4「extract 层放行
  越界软链、与 `send_artifacts_back` 不一致」的前提经核
  `runner_common.path_is_under`（`runner_common.py:85-95`，`path` 与
  `root` **两侧均 `os.path.realpath`**）**不成立**——基线早已拒绝越界软链。
  实际闭环的是三点：① 发射**规范 `realpath`**（`agy.py:110-117`，与
  `send_artifacts_back` 校验同形）；② `seen.add(abs_path)` 按真实目标去重
  （同一目标的多个软链别名只提取一次）；③ 显式
  `os.path.exists(real_path)` 针对解析后路径判定。
  `test_escape_symlink_filtered_at_extract` 是**安全不退化**断言（基线本已
  拒绝越界软链，返回 `[]`），**不是**新增防御。
- **O3 / O5（维持现状，继续跟踪）**：O3 共享
  `config.agy_scratch_dir` 作为 agy 链接白名单根（v1.8.0 已接受的残余
  风险）；O5 relogin 排空对 `slot.held` 标志语义的隐式依赖（实现正确、重构
  易破）。

## reviewer 的 4 条 consider（不阻断）

### [pattern:update-sh-env-value-regex-divergence]

`deploy/update.sh:147` `check_access_control_env` 对 `K=#value` 形态的
语义分歧。

- **事实**：`config._parse_env_file`（`wechatbridge/config.py:13-25`）只在
  **整行**（strip 后）以 `#` 开头时视为注释，故
  `WECHATBRIDGE_ADMINS=#value` 得非空值 `"#value"`（运行时视为已配置）；
  而 update.sh 的正则要求 `=` 后首个非空白/非引号字符落在
  `[^[:space:]"'#]`，把 `#` 排除在首字符外 → **正则不命中**。
- **实测（2026-11-11）**：`WECHATBRIDGE_ADMINS=#value` → `grep -Eq` 不命中
  → 脚本打出「rejects ALL users」**多余告警**；`=abc` 命中不告警；
  `=""` 不命中并告警（后者为设计意图）。即机制层面是「正则不识别该值」，
  可观测效果为**多一次告警**（噪音），并非漏报。
- **影响**：仅升级脚本的告警精度，不影响桥进程鉴权本身。

### [pattern:log-safe-c1-control-chars]

`runner_common.py:395` `_log_safe` 只把 `<0x20` 与 `0x7F` 换成 `?`，未剥
C1 控制符（0x80–0x9F）。

- **影响**：仅**本机**服务端 warning 日志；日志不进 commit、不外发，属理论
  隐患级（部分终端会把 C1 当 CSI 前导）。

### [pattern:display-filename-at-equivalent-contract]

`main.py:88-92` `_sanitize_display_filename` docstring 承诺
「remove EVERY '@'-equivalent」，但实现只含 ASCII `@` 与全角 `＠`
U+FF20，未含小型商用符号 `﹫` U+FE6B 等罕见变体。

- **建议**：措辞收紧为枚举式（指向 `_DISPLAY_FILENAME_STRIP_CHARS`），
  而非「EVERY」。

### [pattern:scan-time-monotonic-coupling]

`tests/test_hardening.py:4672` scan-time 断言通过
`patch(rc.time, "monotonic", side_effect=next(ticks))` 依赖
`time.monotonic` 的**调用次数/顺序**；扫描重构新增一次取样即会破测。

- **建议**：后续如改动扫描路径，预先核对 ticks 数量或改为边界注入。

## oracle 的 4 条观察项（不阻断）

### [pattern:display-filename-invisible-charset-gap]

显示名剥离集缺 Unicode 默认隐形字符 U+115F / U+1160（Hangul Jamo fill）、
U+3164、U+FFA0、U+2800（Braille blank）与 C1 控制符 0x80–0x9F。
经核 `_DISPLAY_FILENAME_STRIP_CHARS` 现状确实不含上述码位。

### [pattern:log-safe-surrogateescape]

`_log_safe` 不处理 `surrogateescape` 坏字节：代理对字符
（`\udc80`–`\udcff`）`ord()` ≥ 0x20 且 ≠ 0x7F，原样进入日志行；写入时可能
再次编码失败或产出替换字符。仅本机日志面。

### [pattern:secret-content-whole-file-match]

`runner_common._matches_session_secret_content` 的 token/凭据匹配粒度为
**整文件子串**（凭据读取上限 64KiB，候选上限 256KiB）。

- **边界（docstring 已固化）**：只拦「整文件逐字复制 / 改名 / 拼接」；字段级
  单独外泄、编码或重排版变体、超界截断均不在威胁模型内。
- 与既有 `.grok` / `.codex`（及宿主 dsh `.credentials.yaml`）凭据契约一致，
  本批未改变该契约。

### [pattern:home-root-expanduser-pwent-drift]

systemd 服务下 `HOME` 非真实 home 时，`os.path.expanduser("~")` 与
passwd(`pwent`) 解析出的宿主 home 两个根不齐，可能使
`_matches_session_secret_content` 的宿主凭据根漏挂一端。

- **性质**：理论边缘场景；`WECHATBRIDGE_HOST_HOME` 显式设置时不受影响。

## [pattern:content-match-residual-surface]

残余面小结：`_matches_session_secret_content` 是**整文件子串**匹配，不做
字段级/编码级检测；本批未改变该契约（详见
`[pattern:secret-content-whole-file-match]`）。同类残余面：
`_log_safe` 的 C1 与 surrogateescape 缺口（`[pattern:log-safe-c1-control-chars]` /
`[pattern:log-safe-surrogateescape]`）同样只影响本机日志。
