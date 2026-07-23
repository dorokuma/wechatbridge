"""
wechatbridge Configuration Module
Settings with environment variable overrides.
"""

import os
import logging

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger(__name__)


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


class AppConfig:
    # iLink base URL (no trailing slash)
    ilink_base_url: str = os.getenv("ILINK_BASE_URL", "https://ilinkai.weixin.qq.com")

    # agy binary path
    agy_binary_path: str = os.getenv("AGY_BIN_PATH", "agy")  # default assumes in PATH

    # Base directory for per-user session workspaces
    session_base_dir: str = os.getenv(
        "WECHATBRIDGE_SESSION_DIR", "/var/lib/wechatbridge/sessions"
    )

    # State file for bot_token persistence
    state_file_path: str = os.getenv(
        "WECHATBRIDGE_STATE_FILE", os.path.join(_BASE_DIR, ".ilink_state.json")
    )

    # QR code PNG save path
    qrcode_png_path: str = os.getenv(
        "WECHATBRIDGE_QRCODE_PATH", os.path.join(_BASE_DIR, "qrcode.png")
    )

    # QR code URL file path (for external access)
    qrcode_url_path: str = os.getenv(
        "WECHATBRIDGE_QRCODE_URL_FILE", os.path.join(_BASE_DIR, ".current_qrcode_url.txt")
    )

    # Timeout for agy execution (seconds)
    agy_timeout: int = _env_int("AGY_TIMEOUT", 180)

    # QR code polling timeout (seconds)
    qrcode_poll_timeout: int = _env_int("QRCODE_POLL_TIMEOUT", 180)

    # QR code poll interval (seconds)
    qrcode_poll_interval: float = _env_float("QRCODE_POLL_INTERVAL", 1.5)

    # Log level
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # iLink CDN base URL for image download
    cdn_base_url: str = os.getenv("WECHATBRIDGE_CDN_BASE", "https://novac2c.cdn.weixin.qq.com/c2c")

    # agy scratch directory (where agy writes generated files)
    agy_scratch_dir: str = os.getenv("AGY_SCRATCH_DIR", os.path.expanduser("~/.gemini/antigravity-cli/scratch"))  # agy generated artifacts scratch dir

    # Scratch file retention days (TTL cleanup)
    scratch_retention_days: int = _env_int("AGY_SCRATCH_RETENTION_DAYS", 7)

    # Maximum outbound file size (bytes) — 100 MB, Tencent OpenClaw SDK limit
    max_outbound_file_bytes: int = _env_int("WECHATBRIDGE_MAX_OUTBOUND_BYTES", 100 * 1024 * 1024)

    # CDN upload timeout (seconds)
    cdn_upload_timeout: int = _env_int("CDN_UPLOAD_TIMEOUT", 120)

    # Access control: comma-separated wxid list, empty = allow all
    allowed_senders: list = [
        s.strip()
        for s in os.getenv("WECHATBRIDGE_ALLOWED_SENDERS", "").split(",")
        if s.strip()
    ]

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


config = AppConfig()
