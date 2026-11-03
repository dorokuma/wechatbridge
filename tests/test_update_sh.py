"""Tests for deploy/update.sh install-source detection.

Runs the script with a stub ``pipx`` on PATH (and ``systemctl``/``wechatbridge``
unavailable so the tail is a no-op) and asserts the pipx subcommand chosen:

* installed from a local path (``package_or_url`` contains ``/``) -> reinstall
* installed from PyPI (no ``/``) -> upgrade
* JSON unavailable -> fall back to parsing ``pipx list`` text
* not installed -> install
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = REPO_ROOT / "deploy" / "update.sh"

# Stub pipx. PIPX_BEHAVIOR selects the canned `pipx list` output; every call is
# appended to PIPX_LOG ("arg1 arg2"). `list --json` emits ${PIPX_JSON:-invalid}.
STUB_PIPX = """\
#!/usr/bin/env bash
echo "$*" >> "$PIPX_LOG"
if [ "$1" = "list" ]; then
    if [ "$2" = "--short" ]; then
        printf '%s\\n' "$PIPX_SHORT"
        exit 0
    fi
    if [ "$2" = "--json" ]; then
        printf '%s\\n' "$PIPX_JSON"
        [ "$PIPX_JSON_OK" = "1" ] && exit 0 || exit 1
    fi
    if [ "$PIPX_INSTALLED" = "1" ]; then
        echo "venvs are in /root/.local/share/pipx/venvs"
        printf '   package wechatbridge-cli %s\\n' "$PIPX_PKG_SUFFIX"
    else
        echo "nothing has been installed with pipx"
    fi
    exit 0
fi
exit 0
"""


@pytest.fixture()
def stub_env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "pipx"
    stub.write_text(STUB_PIPX)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "pipx.log"
    # systemctl and wechatbridge are simply absent from this PATH.
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("WECHATBRIDGE_USER", "root")
    monkeypatch.setenv("PIPX_LOG", str(log))
    return log


def run_update(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    proc = subprocess.run(
        ["bash", str(UPDATE_SH)],
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(os.environ),
    )
    assert proc.returncode == 0, f"update.sh failed:\n{proc.stdout}\n{proc.stderr}"
    return proc


def test_local_path_install_uses_reinstall(stub_env, monkeypatch):
    run_update(
        monkeypatch,
        PIPX_INSTALLED="1",
        PIPX_SHORT="wechatbridge-cli",
        PIPX_JSON_OK="1",
        PIPX_JSON=(
            '{"venvs": {"wechatbridge-cli": {"metadata": {"main_package": '
            '{"package_or_url": "/root/wechatbridge"}}}}}'
        ),
        PIPX_PKG_SUFFIX="1.9.0, installed using Python 3.12.3",
    )
    calls = stub_env.read_text()
    assert "reinstall wechatbridge-cli --python python3" in calls
    assert "upgrade" not in calls
    assert "install wechatbridge-cli\n" not in calls.replace(
        "reinstall wechatbridge-cli", "")


def test_pypi_install_uses_upgrade(stub_env, monkeypatch):
    run_update(
        monkeypatch,
        PIPX_INSTALLED="1",
        PIPX_SHORT="wechatbridge-cli",
        PIPX_JSON_OK="1",
        PIPX_JSON=(
            '{"venvs": {"wechatbridge-cli": {"metadata": {"main_package": '
            '{"package_or_url": "wechatbridge-cli"}}}}}'
        ),
        PIPX_PKG_SUFFIX="1.9.0, installed using Python 3.12.3",
    )
    calls = stub_env.read_text()
    assert "upgrade wechatbridge-cli" in calls
    assert "reinstall" not in calls


def test_text_fallback_detects_local_path(stub_env, monkeypatch):
    """pipx <1.0 / no --json: path inside parentheses in `pipx list` text."""
    run_update(
        monkeypatch,
        PIPX_INSTALLED="1",
        PIPX_SHORT="wechatbridge-cli",
        PIPX_JSON_OK="0",
        PIPX_JSON="",
        PIPX_PKG_SUFFIX="1.9.0 (/root/wechatbridge), installed using Python 3.12.3",
    )
    calls = stub_env.read_text()
    assert "reinstall wechatbridge-cli --python python3" in calls
    assert "upgrade" not in calls


def test_text_fallback_pypi_uses_upgrade(stub_env, monkeypatch):
    run_update(
        monkeypatch,
        PIPX_INSTALLED="1",
        PIPX_SHORT="wechatbridge-cli",
        PIPX_JSON_OK="0",
        PIPX_JSON="",
        PIPX_PKG_SUFFIX="1.9.0, installed using Python 3.12.3",
    )
    calls = stub_env.read_text()
    assert "upgrade wechatbridge-cli" in calls
    assert "reinstall" not in calls


def test_not_installed_runs_install(stub_env, monkeypatch):
    run_update(
        monkeypatch,
        PIPX_INSTALLED="0",
        PIPX_SHORT="",
        PIPX_JSON_OK="0",
        PIPX_JSON="",
        PIPX_PKG_SUFFIX="",
    )
    calls = stub_env.read_text()
    assert "\ninstall wechatbridge-cli\n" in f"\n{calls}"
    assert "upgrade" not in calls
    assert "reinstall" not in calls
