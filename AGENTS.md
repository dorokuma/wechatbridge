# Agent Guidelines
档位：L1

## 检查入口

唯一检查入口是 `make check`，退出码 0 即通过。当前覆盖范围：

- `python3 -m unittest discover -s tests`（与 `.github/workflows/tests.yml` 一致；需先 `pip install -e .` 装好依赖）

版本号检查由 CI 的 `version-check.yml` 单独跑 `python scripts/check_version_bump.py`，不在 `make check` 内。

提交/分支/门禁按 SYSTEM §4。

工作笔记全量入库到 `.agents/notes/`；笔记须不含凭据或未脱敏环境细节。

## 关键实现红线

- **槽位持有不变量**：`wechatbridge/main.py` 中 `_GlobalSlot.held`（`:255-311`）必须真实反映当前任务是否持有全局并发槽位 semaphore，并在退避临时释放/重获及取消清理（`:1693-1699`）时保持一致防止重复 `release`；同时 relogin 切换路径必须经 `msg_tasks` 排空等待（`:1561-1575`）完成后才关闭 `client`，重构槽位或登录切换逻辑时不得破坏这两处不变量。
- **凭据扫描双根覆盖**：当显式配置 `WECHATBRIDGE_HOST_HOME` 与服务进程 `HOME` 分离时，`wechatbridge/runner_common.py`（`:159-165`、`:232-240`）针对 `agy` OAuth token 敏感内容扫描必须同时覆盖 `_host_home()` 与 `os.path.expanduser("~")`（二者去重，与 `wechatbridge/agy.py:178` token 读取来源对齐），修改凭据扫描根集合时不可将该双根覆盖退化为单根。
## Release Policy / 发布规范

本节是本仓库发版的强制规则。仓库已有的更具体版本、构建或发布渠道规则优先，但不得降低以下底线。

1. **版本与变更记录**：遵循 Semantic Versioning，正式版本使用 `vX.Y.Z`。破坏性变更升 MAJOR，新功能升 MINOR，兼容修复升 PATCH。任何用户可见行为、运行时/API/包接口、依赖、安全性或分发产物变化，都必须在同一发版提交中更新版本元数据并写入 CHANGELOG/Release Notes；纯文档且不产生可分发产物时可免升版，但须在规则或 Release Notes 中明确说明。
2. **发布门禁**：发版前工作树必须干净，版本号、CHANGELOG 与发布说明必须一致；必须运行仓库规定的测试、构建、lint、类型检查及必要的安全检查，且全部通过。若仓库提供 `release-check`、`make release-check`、版本检查脚本或同等门禁，必须原样通过，不得绕过。
3. **发布顺序**：先提交并推送发布提交到目标分支 → 在该发布提交上创建 Tag（默认使用带注解的 `git tag -a vX.Y.Z -m "vX.Y.Z"`；仅当现有发布工具明确要求轻量 Tag 时可例外）→ 推送 Tag → 创建**非草稿** GitHub Release → 再发布 npm、PyPI、容器镜像等关联分发渠道。不得只推分支或只打 Tag 就宣称发版完成。
4. **GitHub Release 产物**：标题使用 `vX.Y.Z`，正文必须包含实质性变更摘要、升级/破坏性变更说明及验证结果；二进制、安装包、插件包等适用产物必须作为 Release assets 上传，并提供校验和（如适用）。**Tag 本身不等于完整发版，必须同时存在可访问的 GitHub Release。**
5. **发布后验收**：至少执行并确认 `git ls-remote --tags origin refs/tags/vX.Y.Z`、Tag 指向发布提交、`gh release view vX.Y.Z` 可访问且不是 draft；若发布到包仓库/镜像仓库，还必须核验对应版本、摘要或安装链接可用。
6. **不可变性**：已发布 Tag 不得复用、移动或删除，不得 force push 发布分支或 Tag，不得从未提交、未经测试或未通过门禁的树发布。发布后发现问题须通过新的 patch 版本修复，禁止覆盖历史 Release。
7. **例外必须显式化**：归档、已停用、被替代、仅文档或无可分发产物的仓库，必须在项目规则中明确写出“不发新版本”及历史 Tag/Release 的处置方式；例外不能用于绕过上述发布闭环。

### Repository-specific release contract

- 本仓具体权威流程见 `CONTRIBUTING.md` 与 `.github/workflows/release.yml`；Tag 必须触发并完成 PyPI 发布。
