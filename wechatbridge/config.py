"""
wechatbridge Configuration Module
Settings with environment variable overrides.
"""

import os
import logging

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger(__name__)


def _parse_env_file(path: str) -> None:
    """Parse a .env file and load variables into os.environ (does not override existing)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
    except Exception as e:
        logger.warning("Failed to load .env file %s: %s", path, e)


def _load_env_file():
    """Automatically load .env file if present (precedence: explicit > XDG per-instance > XDG shared > repo root)."""
    # 1. WECHATBRIDGE_ENV_FILE (explicit, highest priority)
    env_path = os.getenv("WECHATBRIDGE_ENV_FILE")
    if env_path:
        if os.path.exists(env_path):
            logger.info("加载 .env 文件: %s (由 WECHATBRIDGE_ENV_FILE 指定)", env_path)
            _parse_env_file(env_path)
        else:
            logger.warning("WECHATBRIDGE_ENV_FILE 指定的 .env 文件不存在: %s", env_path)
        return

    # 2. XDG_CONFIG_HOME/wechatbridge/<instance>.env
    xdg_config = os.getenv("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    instance = os.getenv("WECHATBRIDGE_INSTANCE", "default")
    env_path = os.path.join(xdg_config, "wechatbridge", f"{instance}.env")
    if os.path.exists(env_path):
        logger.info("加载 .env 文件: %s", env_path)
        _parse_env_file(env_path)
        return

    # 3. XDG_CONFIG_HOME/wechatbridge/.env (instance-independent shared)
    env_path = os.path.join(xdg_config, "wechatbridge", ".env")
    if os.path.exists(env_path):
        logger.info("加载 .env 文件: %s", env_path)
        _parse_env_file(env_path)
        return

    # 4. (Deprecated) Package parent directory — repo root .env
    env_path = os.path.join(os.path.dirname(_BASE_DIR), ".env")
    if os.path.exists(env_path):
        logger.warning(
            "已废弃: .env 文件位于 %s，请迁移到 %s",
            env_path,
            os.path.join(xdg_config, "wechatbridge", f"{instance}.env"),
        )
        _parse_env_file(env_path)


_DEPRECATED_ENV: dict = {}  # "OLD_NAME": "NEW_NAME" — when old is set but new isn't, copy value and warn


def _apply_deprecated_env():
    """Apply deprecated env var mappings: copy old value to new name."""
    for old, new in _DEPRECATED_ENV.items():
        old_val = os.getenv(old)
        if old_val is not None and os.getenv(new) is None:
            os.environ[new] = old_val
            logger.warning("环境变量 %s 已废弃，请改用 %s（当前值已自动复制）", old, new)


_load_env_file()
_apply_deprecated_env()


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to %d", name, val, default)
        return default


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        logger.warning("Invalid %s=%r, falling back to %f", name, val, default)
        return default


def _env_int_list(name: str, default: str) -> list:
    """Parse comma-separated positive integers from env (e.g. backoff seconds)."""
    raw = os.getenv(name, default)
    out: list = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            logger.warning("Invalid %s item %r, skipping", name, part)
            continue
        if n < 0:
            logger.warning("Negative %s item %r, skipping", name, part)
            continue
        out.append(n)
    if out:
        return out
    # Fallback to default list if env was empty/invalid
    fallback: list = []
    for part in default.split(","):
        part = part.strip()
        if part:
            fallback.append(int(part))
    return fallback


# ---------------------------------------------------------------------------
# Instance identity — all per-instance paths derive from this
# ---------------------------------------------------------------------------
_instance = os.getenv("WECHATBRIDGE_INSTANCE", "default")
_instance_data_dir = os.path.join(
    os.path.expanduser("~"), ".local", "share", "wechatbridge", _instance
)


class AppConfig:
    # iLink base URL (no trailing slash)
    ilink_base_url: str = os.getenv("ILINK_BASE_URL", "https://ilinkai.weixin.qq.com")

    # Active CLI backend: "agy", "grok", "codex", or "dsh" (global default, can be overridden per-user via /backend)
    backend: str = os.getenv("WECHATBRIDGE_BACKEND", "agy").lower()
    if backend not in ("agy", "grok", "codex", "dsh"):
        logger.warning("Unknown backend %r, falling back to 'agy'", backend)
        backend = "agy"

    # agy CLI binary path
    agy_binary_path: str = os.getenv("AGY_BIN_PATH", "agy")  # default assumes in PATH

    # grok CLI binary path
    grok_binary_path: str = os.getenv("GROK_BIN_PATH", "grok")  # default assumes in PATH

    # codex CLI binary path
    codex_binary_path: str = os.getenv("CODEX_BIN_PATH", "codex")  # default assumes in PATH

    # dsh (DeepSeek Harness) CLI binary path
    dsh_binary_path: str = os.getenv("DSH_BIN_PATH", "dsh")  # default assumes in PATH

    # dsh profile booted for one-shot tasks. The headless profile answers a
    # single task, prints the final assistant message, and exits.
    dsh_profile: str = os.getenv("DSH_PROFILE", "headless")

    # dsh execution timeout (seconds)
    dsh_timeout: int = _env_int("DSH_TIMEOUT", 600)

    # Explicit DSH_HOME passed to the dsh child process. Empty resolves via
    # precedence: WECHATBRIDGE_DSH_HOME > WECHATBRIDGE_HOST_HOME/.dsh > ~/.dsh.
    # Profiles and credentials are shared host-wide (same model as the grok
    # backend's machine-wide login).
    dsh_home: str = os.getenv("WECHATBRIDGE_DSH_HOME", "")

    # Bridge-managed long-term memory for the dsh backend. The headless
    # profile always starts a fresh session, so the bridge injects the user's
    # recent conversation history into every prompt to keep continuity.
    # Number of recent turns (user+assistant pairs) injected as context.
    dsh_memory_turns: int = _env_int("WECHATBRIDGE_DSH_MEMORY_TURNS", 10)
    # Max characters of injected memory context (older turns are dropped first).
    dsh_memory_chars: int = _env_int("WECHATBRIDGE_DSH_MEMORY_CHARS", 6000)

    # True persistent-session mode for the dsh backend (codex-style resume).
    # Requires the headless profile to mount the dsh-bridge-runner plugin
    # (reads DSH_BRIDGE_SESSION_ID / DSH_BRIDGE_TASK from the environment).
    # Each WeChat user keeps one dsh session id; every message RESUMES the same
    # session, so context accumulates without a window. /clear starts a fresh
    # session. When enabled, the windowed memory injection above is skipped.
    dsh_resume: bool = os.getenv("WECHATBRIDGE_DSH_RESUME", "false").lower() == "true"

    # Instance name (for multi-instance deployments)
    instance: str = _instance

    # Per-instance paths (derived from instance, can be overridden by explicit env vars)
    session_base_dir: str = os.getenv(
        "WECHATBRIDGE_SESSION_DIR",
        os.path.join(_instance_data_dir, "sessions"),
    )

    # Bridge-private dsh state directory (kept outside session_base_dir for isolation)
    dsh_state_dir: str = os.getenv(
        "WECHATBRIDGE_DSH_STATE_DIR",
        os.path.join(_instance_data_dir, "dsh_state"),
    )

    state_file_path: str = os.getenv(
        "WECHATBRIDGE_STATE_FILE",
        os.path.join(_instance_data_dir, ".ilink_state.json"),
    )

    qrcode_png_path: str = os.getenv(
        "WECHATBRIDGE_QRCODE_PATH",
        os.path.join(_instance_data_dir, "qrcode.png"),
    )

    qrcode_url_path: str = os.getenv(
        "WECHATBRIDGE_QRCODE_URL_FILE",
        os.path.join(_instance_data_dir, ".current_qrcode_url.txt"),
    )

    # Timeout for CLI execution (seconds) — default 600s; override via AGY_TIMEOUT
    agy_timeout: int = _env_int("AGY_TIMEOUT", 600)

    # QR code polling timeout (seconds)
    qrcode_poll_timeout: int = _env_int("QRCODE_POLL_TIMEOUT", 180)

    # QR code poll interval (seconds)
    qrcode_poll_interval: float = _env_float("QRCODE_POLL_INTERVAL", 1.5)

    # Log level
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # iLink CDN base URL for image download
    cdn_base_url: str = os.getenv("WECHATBRIDGE_CDN_BASE", "https://novac2c.cdn.weixin.qq.com/c2c")

    # agy scratch directory (where agy writes generated files)
    agy_scratch_dir: str = os.getenv("AGY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity-cli/scratch"))

    # Global agy scratch retention days (TTL cleanup)
    scratch_retention_days: int = _env_int("AGY_SCRATCH_RETENTION_DAYS", 7)

    # Per-session temp cleanup (media, .cache, scratch/logs) — not dialogue history.
    # Defaults to the same value as scratch_retention_days.
    session_retention_days: int = _env_int(
        "WECHATBRIDGE_SESSION_RETENTION_DAYS",
        _env_int("AGY_SCRATCH_RETENTION_DAYS", 7),
    )

    # Dialogue history idle TTL: conversations/brain/grok sessions untouched this
    # many days are deleted. Active chats (files still updated) are kept.
    history_retention_days: int = _env_int(
        "WECHATBRIDGE_HISTORY_RETENTION_DAYS", 30
    )

    # Maximum outbound file size (bytes) — 100 MB, Tencent OpenClaw SDK limit
    max_outbound_file_bytes: int = _env_int("WECHATBRIDGE_MAX_OUTBOUND_BYTES", 100 * 1024 * 1024)

    # Maximum inbound image/file size after download (bytes) — default 20 MB
    max_inbound_file_bytes: int = _env_int("WECHATBRIDGE_MAX_INBOUND_BYTES", 20 * 1024 * 1024)

    # Global concurrent process_message slots. Same user serializes first and
    # does not hold a slot while waiting on their previous message; extras get a busy reply.
    max_concurrent_tasks: int = _env_int("WECHATBRIDGE_MAX_CONCURRENT", 4)

    # WeChat text chunk size (characters) when splitting long replies
    message_chunk_chars: int = _env_int("WECHATBRIDGE_MESSAGE_CHUNK", 2000)

    # Extra roots allowed for /add-dir (comma-separated absolute paths).
    # Session dir is always allowed. Empty = only session dir (and its children).
    add_dir_roots: list = [
        os.path.expanduser(s.strip())
        for s in os.getenv("WECHATBRIDGE_ADD_DIR_ROOTS", "").split(",")
        if s.strip()
    ]

    # CDN upload timeout (seconds)
    cdn_upload_timeout: int = _env_int("CDN_UPLOAD_TIMEOUT", 120)

    # Access control: comma-separated wxid list, empty = allow all
    allowed_senders: list = [
        s.strip()
        for s in os.getenv("WECHATBRIDGE_ALLOWED_SENDERS", "").split(",")
        if s.strip()
    ]

    # Admin users: comma-separated wxid list, receive update notifications etc.
    admin_users: list = [
        s.strip()
        for s in os.getenv("WECHATBRIDGE_ADMINS", "").split(",")
        if s.strip()
    ]

    # Periodic update check to PyPI
    update_check_enabled: bool = os.getenv("WECHATBRIDGE_UPDATE_CHECK", "true").lower() == "true"
    update_check_interval: int = _env_int("WECHATBRIDGE_UPDATE_CHECK_INTERVAL", 86400)

    # Enable /mcp slash command (agy MCP tool guidance)
    enable_mcp: bool = os.getenv("WECHATBRIDGE_ENABLE_MCP", "true").lower() == "true"

    # Enable /agent slash command (subagent invocation)
    enable_subagent: bool = os.getenv("WECHATBRIDGE_ENABLE_SUBAGENT", "true").lower() == "true"

    # Confirm gate: dangerous prompt confirmation (empty = fallback to hardcoded list)
    confirm_keywords: list = [
        kw.strip()
        for kw in os.getenv("WECHATBRIDGE_CONFIRM_KEYWORDS", "").split(",")
        if kw.strip()
    ]
    # TTL for pending confirmations (seconds)
    pending_confirm_ttl: int = _env_int("WECHATBRIDGE_PENDING_TTL", 300)
    # Confirmation keyword users must reply to execute dangerous prompt
    confirm_token: str = os.getenv("WECHATBRIDGE_CONFIRM_TOKEN", "y")

    # Upstream throttle guard (process-wide, covers agy/grok/codex)
    # Extra retries after the first attempt for short-window throttle (default 2 → 3 total)
    upstream_retry_max: int = _env_int("WECHATBRIDGE_UPSTREAM_RETRY_MAX", 2)
    # Extra retries for 额度相关 (default 0 — mark cooldown/gap but do not spam CLI)
    upstream_quota_retry_max: int = _env_int("WECHATBRIDGE_UPSTREAM_QUOTA_RETRY_MAX", 0)
    # Per-retry backoff seconds (indexed by failed attempt 0,1,2,…)
    upstream_backoff: list = _env_int_list("WECHATBRIDGE_UPSTREAM_BACKOFF", "2,5,12")
    # Global cooldown after any throttle signal (seconds)
    upstream_cooldown: int = _env_int("WECHATBRIDGE_UPSTREAM_COOLDOWN", 20)
    # Per-user silent gap after throttle (seconds); no WeChat notice while waiting
    upstream_user_gap: int = _env_int("WECHATBRIDGE_UPSTREAM_USER_GAP", 10)

    # After A/B/C released-sleep, re-acquire the global concurrency slot with a
    # short timeout (seconds) and limited attempts. Better than sleeping while
    # holding the slot; on final timeout the user gets the same busy reply as
    # initial fail-fast concurrency full.
    slot_reacquire_timeout: float = _env_float("WECHATBRIDGE_SLOT_REACQUIRE_TIMEOUT", 0.5)
    slot_reacquire_attempts: int = _env_int("WECHATBRIDGE_SLOT_REACQUIRE_ATTEMPTS", 3)


config = AppConfig()


def ensure_runtime_dirs() -> None:
    """Create instance data / session / state / qrcode parent dirs with tight perms."""
    paths = {
        _instance_data_dir,
        config.session_base_dir,
        os.path.dirname(os.path.abspath(config.state_file_path)) or ".",
        os.path.dirname(os.path.abspath(config.qrcode_png_path)) or ".",
        os.path.dirname(os.path.abspath(config.qrcode_url_path)) or ".",
    }
    for path in paths:
        if not path or path == ".":
            continue
        try:
            os.makedirs(path, exist_ok=True)
            os.chmod(path, 0o700)
        except OSError as e:
            logger.warning("Failed to ensure runtime dir %s: %s", path, e)


def _normalized_dsh_home() -> tuple[bool, str]:
    """Single normalization helper for DSH_HOME.

    Returns:
        tuple[bool, str]: (is_explicit, normalized_abs_path)
    """
    raw = getattr(config, "dsh_home", "")
    val = raw.strip() if raw else ""
    if not val:
        host_home = os.environ.get("WECHATBRIDGE_HOST_HOME") or os.path.expanduser("~")
        return False, os.path.abspath(os.path.join(host_home, ".dsh"))
    return True, os.path.abspath(os.path.expanduser(val))


def host_dsh_home() -> str:
    """Machine-wide DeepSeek Harness home used by dsh child process and session cleanup.

    Precedence: ``WECHATBRIDGE_DSH_HOME`` (config.dsh_home) > ``WECHATBRIDGE_HOST_HOME/.dsh`` > ``~/.dsh``.
    """
    return _normalized_dsh_home()[1]


def is_dsh_home_explicit() -> bool:
    """True when WECHATBRIDGE_DSH_HOME (config.dsh_home) was explicitly configured."""
    return _normalized_dsh_home()[0]

