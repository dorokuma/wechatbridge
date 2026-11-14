# Agent Guidelines
档位：L1

## 检查入口

唯一检查入口是 `make check`，退出码 0 即通过。当前覆盖范围：

- `python3 -m unittest discover -s tests`（与 `.github/workflows/tests.yml` 一致；需先 `pip install -e .` 装好依赖）

版本号检查由 CI 的 `version-check.yml` 单独跑 `python scripts/check_version_bump.py`，不在 `make check` 内。

提交/分支/门禁按 SYSTEM §4。
