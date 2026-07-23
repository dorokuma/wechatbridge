# Contributing

## Versioning

This project follows [Semantic Versioning 2.0.0](https://semver.org/) starting from **1.0.0**.

Given a version number `MAJOR.MINOR.PATCH`:

- **MAJOR** — incompatible API, behaviour, or configuration changes (deleting a config key, changing message handling semantics, breaking existing deployments).
- **MINOR** — backward-compatible new features (new slash command, new message type support, new config option).
- **PATCH** — backward-compatible bug fixes.

### Rules

1. Every change must be recorded in `CHANGELOG.md` under the `[Unreleased]` section before the change is merged.
2. When releasing, rename `[Unreleased]` to `[X.Y.Z]` with the release date, then create a fresh empty `[Unreleased]` section.
3. Releases must be tagged with `vX.Y.Z` (e.g., `v1.0.0`).
4. Breaking changes MUST bump MAJOR. They must not be smuggled into MINOR or PATCH releases.
5. The `__version__` string in `wechatbridge/__init__.py` and the `version` field in `pyproject.toml` must match the current release tag.

## Development workflow

1. Fork the repository and create a feature branch from `main`.
2. Make your changes. Keep them focused — one change per branch.
3. Test locally: `python -m py_compile wechatbridge/*.py`.
4. Update `CHANGELOG.md` under `[Unreleased]`.
5. Open a pull request against `main`. Include a summary of what changed and why.

## Code style

- Python 3.10+.
- Keep functions short and focused on one task.
- Avoid adding dependencies without discussion.
- Log with context — include `user_id`, prompt summary, or error details.
