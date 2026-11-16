# 观察项收件箱（Inbox）

- `[pattern:relogin-drain-slot-held-invariant]` | type:redline | added:2026-11-11
  `wechatbridge/main.py` 中 `_GlobalSlot.held`（`:255-311`）必须真实反映当前任务是否持有全局并发槽位 semaphore，并在退避临时释放/重获及取消清理（`:1693-1699`）时保持一致防止重复 `release`；同时 relogin 切换路径必须经 `msg_tasks` 排空等待（`:1561-1575`）完成后才关闭 `client`，重构槽位或登录切换逻辑时不得破坏这两处不变量。
- `[pattern:agy-oauth-dual-home-roots]` | type:redline | added:2026-11-11
  当显式配置 `WECHATBRIDGE_HOST_HOME` 与服务进程 `HOME` 分离时，`wechatbridge/runner_common.py`（`:159-165`、`:232-240`）针对 `agy` OAuth token 敏感内容扫描必须同时覆盖 `_host_home()` 与 `os.path.expanduser("~")`（二者去重，与 `wechatbridge/agy.py:178` token 读取来源对齐），修改凭据扫描根集合时不可将该双根覆盖退化为单根。
