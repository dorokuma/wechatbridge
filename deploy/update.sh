#!/usr/bin/env bash
# WeChatBridge upgrade script
# Usage: sudo bash deploy/update.sh
#        WECHATBRIDGE_USER=wechatbridge      # target system user (default: current user)
#        WECHATBRIDGE_SERVICE=wechatbridge    # service name to restart (default: auto-detect)
set -euo pipefail

# --- Helper: run a command as the target user ---
run_as_target() {
    if [ "$CURRENT_USER" != "$TARGET_USER" ]; then
        sudo -u "$TARGET_USER" -H "$@"
    else
        "$@"
    fi
}

# --- Helper: run pipx as target user ---
run_pipx() {
    run_as_target pipx "$@"
}

# --- Preflight: check pipx ---
if ! command -v pipx &>/dev/null; then
    echo "ERROR: pipx not found. Install it first:"
    echo "  Debian/Ubuntu: sudo apt install pipx"
    echo "  Other:         python3 -m pip install --user pipx && python3 -m pipx ensurepath"
    exit 1
fi

# --- Determine target user ---
# Default: current user. When run as root (e.g. via sudo) and a dedicated
# 'wechatbridge' system user exists, default to that user instead — upgrading
# root's pipx copy would not affect the service.
if [ -n "${WECHATBRIDGE_USER:-}" ]; then
    TARGET_USER="$WECHATBRIDGE_USER"
elif [ "$(id -u)" -eq 0 ] && id wechatbridge &>/dev/null; then
    TARGET_USER="wechatbridge"
else
    TARGET_USER="$(whoami)"
fi
CURRENT_USER="$(whoami)"

echo "Target user: $TARGET_USER"
if [ "$CURRENT_USER" != "$TARGET_USER" ]; then
    echo "(pipx will run as '$TARGET_USER'; override with WECHATBRIDGE_USER=<user>)"
fi

# --- Helper: report the recorded install source of wechatbridge-cli ---
# wechatbridge-cli may have been installed from a local path
# (e.g. `pipx install /root/wechatbridge`); pipx then records that path as
# package_or_url and `pipx upgrade` is a no-op for it — local-path installs
# must be refreshed by `git pull` in the source dir followed by
# `pipx upgrade` (NOT `reinstall`, which would wipe runpip deps like PyYAML).
# Prefers `pipx list --json` (pipx >= 1.0); falls back to reading
# pipx_metadata.json directly. Never parses `pipx list` text output.
pipx_package_or_url() {
    local json src meta_file
    json="$(run_pipx list --json 2>/dev/null || true)"
    if [ -n "$json" ]; then
        src="$(printf '%s' "$json" | python3 -c 'import json,sys
try:
    data = json.load(sys.stdin)
    if not isinstance(data, dict):
        sys.exit(0)
    meta = ((data.get("venvs") or {}).get("wechatbridge-cli") or {}).get("metadata") or {}
    print((meta.get("main_package") or {}).get("package_or_url") or "")
except Exception:
    sys.exit(0)
' 2>/dev/null || true)"
        if [ -n "$src" ]; then
            printf '%s\n' "$src"
            return 0
        fi
    fi
    # Fallback: read pipx_metadata.json directly as the target user.
    local target_home
    target_home="$(eval echo "~${TARGET_USER}" 2>/dev/null || true)"
    for meta_file in \
        "${PIPX_HOME:+$PIPX_HOME/venvs/wechatbridge-cli/pipx_metadata.json}" \
        "${target_home:+$target_home/.local/share/pipx/venvs/wechatbridge-cli/pipx_metadata.json}" \
        "${target_home:+$target_home/.local/pipx/venvs/wechatbridge-cli/pipx_metadata.json}"
    do
        [ -n "$meta_file" ] || continue
        src="$(run_as_target python3 - "$meta_file" <<'PY' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
    print((data.get("main_package") or {}).get("package_or_url") or "")
except Exception:
    pass
PY
)"
        if [ -n "$src" ]; then
            printf '%s\n' "$src"
            return 0
        fi
    done
    return 0
}

# --- Upgrade (or install if not present) ---
echo "Upgrading WeChatBridge..."
if run_pipx list --short 2>/dev/null | awk '{print $1}' | grep -x "wechatbridge-cli" >/dev/null || run_pipx list 2>/dev/null | grep -E "(^|[[:space:]])package[[:space:]]+wechatbridge-cli([[:space:]]|,|$)|(^|[[:space:]])wechatbridge-cli([[:space:]]|$)" >/dev/null; then
    PKG_SRC="$(pipx_package_or_url || true)"
    if [[ "$PKG_SRC" == /* ]] || [[ "$PKG_SRC" == file://* ]]; then
        # Local-path install: pipx upgrade is a no-op for these. Refresh the
        # source checkout as the target user, then let pipx upgrade reinstall
        # from it — never silently skip and never use `pipx reinstall`
        # (which would wipe runpip-installed deps like PyYAML).
        LOCAL_DIR="${PKG_SRC#file://}"
        # Strip a trailing pip extras spec like /path[extra] — only when the
        # original path does not exist but the stripped dir does.
        if ! run_as_target test -d "$LOCAL_DIR" && [[ "$LOCAL_DIR" == *"["*"]" ]] && run_as_target test -d "${LOCAL_DIR%%\[*}"; then
            LOCAL_DIR="${LOCAL_DIR%%\[*}"
        fi
        if ! run_as_target test -d "$LOCAL_DIR"; then
            echo "ERROR: install source '$LOCAL_DIR' does not exist for user '$TARGET_USER'." >&2
            echo "       wechatbridge-cli was installed from a local path; cannot refresh it." >&2
            exit 1
        fi
        if ! run_as_target git -C "$LOCAL_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            echo "ERROR: install source '$LOCAL_DIR' is not a git repository." >&2
            echo "       wechatbridge-cli was installed from a local path; cannot refresh it." >&2
            exit 1
        fi
        echo "wechatbridge-cli was installed from local source: $LOCAL_DIR"
        echo "Refreshing local source with git pull --ff-only..."
        if ! run_as_target git -C "$LOCAL_DIR" pull --ff-only; then
            echo "ERROR: 'git pull --ff-only' failed in '$LOCAL_DIR'." >&2
            echo "       Refusing to run 'pipx upgrade' against a stale source." >&2
            exit 1
        fi
        run_pipx upgrade wechatbridge-cli
    else
        run_pipx upgrade wechatbridge-cli
    fi
else
    echo "WeChatBridge not yet installed under '$TARGET_USER'. Installing..."
    run_pipx install wechatbridge-cli
fi

# --- Helper: warn when the target user's env has no access control ---
# Since 1.8.0 the whitelist is fail-closed: an env file without an active
# WECHATBRIDGE_ALLOWED_SENDERS or WECHATBRIDGE_ADMINS denies everyone.
# Prints a warning only — never blocks the upgrade, never reads other values.
check_access_control_env() {
    # Resolve the target user's home the same way pipx does for them
    # (~ expansion by the target user's passwd entry).
    local target_home env_file
    target_home="$(eval echo "~${TARGET_USER}" 2>/dev/null || true)"
    if [ -z "$target_home" ]; then
        echo "(cannot resolve home for '$TARGET_USER'; skipping access-control check)"
        return 0
    fi
    env_file="$target_home/.config/wechatbridge/.env"
    if [ ! -f "$env_file" ]; then
        echo "(no $env_file found; skipping access-control check)"
        return 0
    fi
    # Active line = not commented out and value non-empty AFTER the parser's
    # quote strip: `KEY=""` / `KEY=''` / `KEY= " "` resolve to an empty
    # string in config._parse_env_file and fail-closed, so they must still
    # trigger this warning (an unquoted non-space non-# first char is valid).
    if ! grep -Eq '^[[:space:]]*WECHATBRIDGE_ALLOWED_SENDERS[[:space:]]*=[[:space:]]*["'"'"']?[[:space:]]*[^[:space:]"'"'"'#]' "$env_file" \
        && ! grep -Eq '^[[:space:]]*WECHATBRIDGE_ADMINS[[:space:]]*=[[:space:]]*["'"'"']?[[:space:]]*[^[:space:]"'"'"'#]' "$env_file"; then
        echo ""
        echo "==============================================================="
        echo "  WARNING: no active WECHATBRIDGE_ALLOWED_SENDERS or"
        echo "  WECHATBRIDGE_ADMINS found in $env_file"
        echo "  Since 1.8.0 the whitelist denies everyone by default —"
        echo "  this config rejects ALL users. Set WECHATBRIDGE_ADMINS or"
        echo "  WECHATBRIDGE_ALLOWED_SENDERS (use '*' to allow everyone)."
        echo "==============================================================="
        echo ""
    fi
}

# --- Print new version ---
echo "---"
if [ "$CURRENT_USER" != "$TARGET_USER" ]; then
    sudo -u "$TARGET_USER" -H wechatbridge --version 2>/dev/null || echo "(could not determine version)"
else
    wechatbridge --version 2>/dev/null || echo "(could not determine version)"
fi
echo "---"

# --- Restart systemd services ---
if command -v systemctl &>/dev/null; then
    SUDO_CMD=""
    if [ "$(id -u)" -ne 0 ]; then
        SUDO_CMD="sudo"
        echo "Not running as root, will use sudo for systemctl."
    fi

    # If a specific service was requested, restart only that
    if [ -n "${WECHATBRIDGE_SERVICE:-}" ]; then
        echo "Restarting service '$WECHATBRIDGE_SERVICE'..."
        $SUDO_CMD systemctl restart "$WECHATBRIDGE_SERVICE" || echo "  (service not found, skipping)"
    else
        # Restart every wechatbridge unit that exists (plain service, template
        # instances wechatbridge@*.service, and legacy names like
        # wechatbridge2.service) — enumerate loaded units instead of guessing.
        echo "Looking for wechatbridge*.service units..."
        mapfile -t UNITS < <($SUDO_CMD systemctl list-units --all 'wechatbridge*.service' --no-legend 2>/dev/null | awk '{print $1}' | sort -u || true)
        if [ ${#UNITS[@]} -gt 0 ]; then
            for svc in "${UNITS[@]}"; do
                echo "Restarting $svc..."
                $SUDO_CMD systemctl restart "$svc" || echo "  (failed to restart $svc)"
            done
        else
            echo "  No wechatbridge*.service units found."
        fi
    fi
else
    echo "systemctl not found. Please restart WeChatBridge manually."
fi

# --- Done ---
check_access_control_env

echo ""
echo "=== WeChatBridge upgrade complete ==="
echo "To watch logs:"
echo "  sudo journalctl -u wechatbridge -f"
echo ""
echo "For multi-instance setups, replace 'wechatbridge' with the specific service name."
