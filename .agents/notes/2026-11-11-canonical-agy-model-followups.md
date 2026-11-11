# canonical agy model（v1.9.2–v1.9.4）收口 — 遗留 follow-ups

来源：feat/canonical-agy-model 批次门禁审查（reviewer/verifier 观察项）与
收口现场记录。本批已合并 main（merge commit `7617fd0`），版本 `1.9.4`。

## O1（consider，未闭环）`_AGY_MAX_SUFFIX` 死代码

- **位置**：`wechatbridge/runner_common.py:1539`
- **事实**：`_AGY_MAX_SUFFIX = "high"` 定义后无任何引用；钳位上限实际
  由 `max(allowed, key=lambda v: ("low", "medium", "high").index(v))`
  （同文件 ~L1629）直接算出。
- **建议**：删除该常量，或改为被钳位逻辑引用（作为 ceiling 的单一事
  实来源）。无行为影响，不阻断。

## O2（遗留，交下一轮）B 分支 `fix/followup-artifacts-and-secret` 未收口

- **现状**：worktree 尖端 `4726145`，6 个未提交文件：
  `extract_artifacts` 软链 realpath 校验、oauth token 内容匹配、文件
  名剥离、`deploy/update.sh` 访问控制告警条件、笔记、
  `tests/test_hardening.py`。
- **冲突面**：与本批同改 `wechatbridge/agy.py`、
  `wechatbridge/runner_common.py`、`tests/test_hardening.py`。
- **建议路线**：B 先自身提交并过 C 档门禁 → 在 B 上
  `merge --no-ff --no-commit` 本批尖端 → 联合门禁 → 收口。

## O3（已闭环，供追溯）本批门禁闭环项

已在 `3ef18dd` 落地：

- should fix：effort 被模型自带后缀覆盖时补 WARNING（返回值仍 model
  wins）。
- should fix：`deploy/wechatbridge.env.example` 回退 fail-closed 后
  的首装提示（env.example 与 README/README.zh-CN 同步）。
- consider：CHANGELOG 中 1.9.1 小节「`*` 比空值 INFO 更吵」的日志级
  别比较错误（已更正为同样走 WARNING 分支）。
- consider：`test_illegal_suffix_shapes_warn` 的 WARNING 断言改用语
  义匹配，不再耦合英文措辞/引号样式。
