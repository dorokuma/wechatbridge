"""pig CLI runner with per-user session isolation.

Single-model Magpie backend: only ``gemini-web/gemini-4-neon`` is ever
exposed. Per-user agent state lives under ``session_dir/.pig/agent`` with a
minimized, key-free ``models.json`` (``"$MAGPIE_API_KEY"`` placeholder) and a
``settings.json`` that disables pig's internal retry so the bridge's
UpstreamGuard owns all backoff.

Mirrors the agy/grok/codex runner interface: ``run_pig()`` and
``handle_pig_slash_command()``.
"""

import asyncio
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time

from .config import config
from .runner_common import (
    MAX_ADD_DIRS,
    _AT_TOKEN_RE,
    _sanitize_prompt_at_paths,
    _classify_cli_error,
    _is_cli_binary_missing,
    _strip_file_links,
    sanitize_env,
    get_session_dir,
    ensure_session_dir,
    is_first_message,
    mark_initialized,
    clear_initialized,
    clean_output,
    load_prefs,
    save_prefs,
    is_dangerous,
    is_sensitive_artifact_path,
    path_is_under,
    terminate_process,
    _live_key_in_path_names,
    _register_active_pig_session,
    _unregister_active_pig_session,
    update_active_prefs,
    format_error,
    format_cli_error,
    format_timeout_error,
    EMPTY_REPLY,
    validate_add_dir,
    _snapshot_regular_files,
    collect_session_fallback_artifacts,
    merge_artifact_groups,
    ARTIFACT_FALLBACK_EXTENSIONS,
)
from .agy import extract_artifacts

logger = logging.getLogger("pig_runner")

# ---------------------------------------------------------------------------
# Fixed single-model / provider constants (hard-locked, never configurable)
# ---------------------------------------------------------------------------

PIG_PROVIDER_ID = "magpie"
PIG_MODEL_ID = "gemini-web/gemini-4-neon"
PIG_DEFAULT_THINKING = "high"
PIG_ALLOWED_THINKING = ("low", "medium", "high")

# execve 单参数上限（Linux MAX_ARG_STRLEN = 128KB），留安全余量
_MAX_ARG_BYTES = 120 * 1024

# Independent @file argv cap per turn (memory guard for multimodal loads)
_PIG_MAX_FILE_ARGS = 8
# Max bytes for a UTF-8 text file to ride as a @file CLI argument
_PIG_TEXT_FILE_MAX_BYTES = 64 * 1024

# Host/wrapper env vars that would redirect pig's agent dir away from the
# per-user isolation tree (PIG_USE_PI_DIRS=1 makes native pig read PI_*).
_PIG_STRIP_ENV_VARS = (
    "PIG_USE_PI_DIRS",
    "PIG_CODING_AGENT_SESSION_DIR",
    "PI_CODING_AGENT_SESSION_DIR",
)

# pig JSONL event types / fields inspected by _parse_pig_output
_PIG_WRITE_TOOLS = frozenset({"write", "edit"})

_PLAN_TOOLS = "read,grep,find,ls"
_NORMAL_TOOLS = "read,bash,edit,write"

_PLAN_MODE_PROMPT = (
    "当前为只读规划模式：先调研给出分步方案，不要执行任何文件修改或命令。"
)


# ---------------------------------------------------------------------------
# Credential resolution (never logged, never persisted to disk)
# ---------------------------------------------------------------------------

def _resolve_magpie_api_key() -> str:
    """Resolve the Magpie API key: config > WECHATBRIDGE_MAGPIE_API_KEY > MAGPIE_API_KEY.

    The value is only ever injected into the child process environment —
    never printed, logged, or written to a file (models.json keeps the
    ``$MAGPIE_API_KEY`` placeholder literal).
    """
    key = getattr(config, "magpie_api_key", "") or ""
    if not key.strip():
        key = os.environ.get("WECHATBRIDGE_MAGPIE_API_KEY", "")
    if not key.strip():
        key = os.environ.get("MAGPIE_API_KEY", "")
    return key.strip()


def _pig_has_credentials() -> bool:
    return bool(_resolve_magpie_api_key())


# ---------------------------------------------------------------------------
# Per-user .pig/agent isolation dir + minimized models.json / settings.json
# ---------------------------------------------------------------------------

def _pig_home(session_dir: str) -> str:
    return os.path.join(session_dir, ".pig")


def _pig_agent_dir(session_dir: str) -> str:
    return os.path.join(_pig_home(session_dir), "agent")


def _pig_sessions_dir(session_dir: str) -> str:
    return os.path.join(_pig_agent_dir(session_dir), "sessions")


def _check_no_link_or_hardlink(path: str) -> bool:
    """True if path is missing, a real dir, or a single-linked regular file.

    Symlinks are always refused (hijack vector). Hardlink count only applies
    to regular files — directories naturally have st_nlink >= 2.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return True  # missing — fine, caller creates it
    if stat.S_ISLNK(st.st_mode):
        return False
    if stat.S_ISDIR(st.st_mode):
        return True
    if stat.S_ISREG(st.st_mode):
        return st.st_nlink <= 1
    return False


def _atomic_write_private(path: str, payload: str) -> bool:
    """Atomically write *payload* to *path* with 0o600 (tmp + os.replace)."""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix=".tmp_pig_", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        tmp_path = None
        return True
    except OSError as e:
        logger.error("Atomic write %s failed: %s", path, e)
        return False
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _pig_models_json() -> dict:
    """Minimized models.json — exactly one model, no key material on disk."""
    return {
        "providers": {
            PIG_PROVIDER_ID: {
                "name": "Magpie",
                "baseUrl": getattr(config, "magpie_base_url", "") or "https://magpie.hotkids.eu/v1",
                "apiKey": "$MAGPIE_API_KEY",
                "api": "openai-responses",
                "models": [
                    {
                        "id": PIG_MODEL_ID,
                        "name": "gemini-4-neon",
                        "input": ["text", "image"],
                        "reasoning": True,
                        "contextWindow": 1000000,
                        "maxTokens": 8192,
                        "thinkingLevelMap": {
                            "off": None,
                            "minimal": None,
                            "low": "low",
                            "medium": "medium",
                            "high": "high",
                            "xhigh": None,
                            "max": None,
                        },
                    }
                ],
            }
        }
    }


def _pig_settings_json() -> dict:
    """Minimized settings.json — internal retry disabled; the bridge
    UpstreamGuard owns all backoff/cooldown instead of pig re-firing while
    holding the global concurrency slot."""
    timeout_ms = max(int(getattr(config, "pig_timeout", 570) or 570), 1) * 1000
    return {
        "defaultProvider": PIG_PROVIDER_ID,
        "defaultModel": PIG_MODEL_ID,
        "retry": {
            "enabled": False,
            "maxRetries": 0,
            "provider": {
                "maxRetries": 0,
                "timeoutMs": timeout_ms,
            },
        },
    }


def ensure_user_pig(session_dir: str) -> str:
    """Ensure session_dir/.pig/agent + sessions/ (0o700) and minimized
    models.json / settings.json (0o600, atomic, symlink/hardlink refused).

    Fail-closed: every level of the path (pig_home, agent_dir, sessions_dir)
    and both config files are verified to be neither symlink nor multi-link
    file BEFORE they are used. Any verification or write failure returns
    ``""`` — the caller MUST NOT spawn the CLI with an unverified agent dir
    (a symlinked dir would let pig load host auth/models/settings or
    redirect chmod 0o700 onto a foreign tree).
    """
    pig_home = _pig_home(session_dir)
    agent_dir = _pig_agent_dir(session_dir)
    sessions_dir = _pig_sessions_dir(session_dir)

    for p in (pig_home, agent_dir, sessions_dir):
        if not _check_no_link_or_hardlink(p):
            logger.error("Refusing to use pig dir (symlink/hardlink): %s", p)
            return ""
    try:
        os.makedirs(sessions_dir, exist_ok=True)
    except OSError as e:
        logger.error("Failed to create pig agent dir %s: %s", agent_dir, e)
        return ""
    # Re-verify after makedirs — a sessions_dir symlink planted before the
    # call must not be chmod'ed through (makedirs follows links happily).
    for p in (pig_home, agent_dir, sessions_dir):
        if not _check_no_link_or_hardlink(p):
            logger.error("Refusing to use pig dir (symlink appeared): %s", p)
            return ""
        try:
            os.chmod(p, 0o700)
        except OSError as e:
            logger.error("Failed to chmod pig dir %s: %s", p, e)
            return ""

    for name, payload in (
        ("models.json", json.dumps(_pig_models_json(), indent=2)),
        ("settings.json", json.dumps(_pig_settings_json(), indent=2)),
    ):
        fp = os.path.join(agent_dir, name)
        if not _check_no_link_or_hardlink(fp):
            logger.error("Refusing to write pig config (symlink/hardlink): %s", fp)
            return ""
        if not _atomic_write_private(fp, payload):
            return ""

    return agent_dir


def _apply_pig_runtime_env(env: dict, session_dir: str) -> dict:
    """Lock pig into the per-user isolation tree on both env channels.

    Native pig reads PIG_HOME + PIG_CODING_AGENT_DIR; wrapped builds that set
    PIG_USE_PI_DIRS=1 read PI_CODING_AGENT_DIR instead — set all three and
    strip every redirect var so host and wrapped installs behave identically.
    Re-injects exactly one secret (MAGPIE_API_KEY) after sanitize_env stripped
    every *KEY* from the host environment.
    """
    for k in _PIG_STRIP_ENV_VARS:
        env.pop(k, None)
    # PIG_PIGLET_* wrapper vars (name/path) also redirect agent state
    for k in [k for k in env if k.startswith("PIG_PIGLET_")]:
        env.pop(k, None)

    pig_home = _pig_home(session_dir)
    agent_dir = _pig_agent_dir(session_dir)
    env["PIG_HOME"] = pig_home
    env["PIG_CODING_AGENT_DIR"] = agent_dir
    env["PI_CODING_AGENT_DIR"] = agent_dir
    env["PI_OFFLINE"] = "1"
    env["PI_SKIP_VERSION_CHECK"] = "1"
    env.setdefault("GOMEMLIMIT", "192MiB")
    env["MAGPIE_API_KEY"] = _resolve_magpie_api_key()
    return env


# ---------------------------------------------------------------------------
# @file magic-byte detection (mirrors pig's image loader exactly)
# ---------------------------------------------------------------------------

def _u32be(b: bytes, off: int) -> int:
    return (
        b[off] * 0x1000000 + (b[off + 1] << 16)
        + (b[off + 2] << 8) + b[off + 3]
    )


def _u32le(b: bytes, off: int) -> int:
    return b[off] | b[off + 1] << 8 | b[off + 2] << 16 | b[off + 3] * 0x1000000


def _u16le(b: bytes, off: int) -> int:
    return b[off] | b[off + 1] << 8


def _is_pig_supported_image(real_path: str) -> bool:
    """True if *real_path* is a static image pig accepts as @file.

    Magic-byte check mirrors ref/PiG internal/imageprocessing/images.go
    DetectSupportedImageMimeType exactly:
      - JPEG: FF D8 FF, reject only head[3]==0xF7 (JPEG-LS) — no blind
        0xFFC7 scan (EXIF/ICC segments legitimately contain that pair).
      - PNG: first chunk must be IHDR (length==13); APNG (acTL before the
        first IDAT) is rejected — pig's image path returns "" and would
        fall through to the UTF-8 text branch producing garbage. An
        unscannable header is NOT assumed static.
      - GIF87a/89a, RIFF…WEBP.
      - BMP: "BM" + full header consistency check (isBMP): declared file
        size, pixel-data offset vs DIB header size, planes==1 and bpp in
        {1,4,8,16,24,32} — a text/binary blob that merely starts with "BM"
        is not an image.
    """
    try:
        size = os.path.getsize(real_path)
        # ref/PiG ImageTypeSniffBytes = 4100 — detection only needs the head.
        with open(real_path, "rb") as f:
            head = f.read(min(size, 4100))
    except OSError:
        return False
    if len(head) < 2:
        return False

    # JPEG: FF D8 FF; reject lossless JPEG-LS (fourth byte 0xF7)
    if head[:3] == b"\xff\xd8\xff":
        if len(head) > 3 and head[3] == 0xF7:
            return False
        return True

    # PNG: signature + mandatory IHDR first chunk (length==13), then acTL
    # must not appear before the first IDAT (mirrors isPNG/isAnimatedPNG).
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        if len(head) < 16 or _u32be(head, 8) != 13 or head[12:16] != b"IHDR":
            return False
        pos = 8
        n = len(head)
        while pos + 8 <= n:
            chunk_length = _u32be(head, pos)
            ctype = head[pos + 4:pos + 8]
            if ctype == b"acTL":
                return False
            if ctype == b"IDAT":
                return True
            nxt = pos + 8 + chunk_length + 4
            if nxt <= pos or nxt > n:
                return False
            pos = nxt
        # Header scan exhausted before IDAT — cannot prove it is static,
        # so do NOT treat it as a supported still image.
        return False

    if head[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True

    # BMP: "BM" + isBMP header consistency (ref/PiG images.go isBMP)
    if len(head) >= 26 and head[:2] == b"BM":
        declared_size = _u32le(head, 2)
        pixel_offset = _u32le(head, 10)
        dib_size = _u32le(head, 14)
        if declared_size != 0 and declared_size < 26:
            return False
        if pixel_offset < 14 + dib_size:
            return False
        if declared_size != 0 and pixel_offset >= declared_size:
            return False
        if dib_size == 12:
            planes, bpp = _u16le(head, 22), _u16le(head, 24)
        elif 40 <= dib_size <= 124:
            if len(head) < 30:
                return False
            planes, bpp = _u16le(head, 26), _u16le(head, 28)
        else:
            return False
        return planes == 1 and bpp in (1, 4, 8, 16, 24, 32)

    return False


def _is_small_utf8_text(real_path: str, max_bytes: int = _PIG_TEXT_FILE_MAX_BYTES) -> bool:
    """True if *real_path* is a small NUL-free UTF-8 text file."""
    try:
        st = os.lstat(real_path)
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or st.st_size > max_bytes:
            return False
        with open(real_path, "rb") as f:
            data = f.read(max_bytes + 1)
    except OSError:
        return False
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return True


def _resolve_at_token(raw_token: str, session_dir: str, extra_roots: list[str]) -> str:
    """Resolve a post-sanitize ``@<token>`` to a realpath if it is an allowed,
    safe regular file. Returns "" when the token is not a usable file ref."""
    from urllib.parse import unquote

    token = raw_token.rstrip(".,!?;:)>]}'\"")
    if not token:
        return ""
    candidate = unquote(token).replace("\\", "/")
    is_candidate = (
        candidate.startswith(("/", "./", "../", "~/"))
        or candidate == "~"
        or "://" in candidate
        or candidate.startswith("file:")
        or "/" in candidate
    )
    if not is_candidate:
        # Bare @token — try session-dir-local file (main.py inlines @<abs>
        # paths which always carry a slash; bare names only match local files)
        local = os.path.join(session_dir, candidate)
        if not os.path.isfile(local):
            return ""
        candidate = local
    p = candidate
    if p.startswith("file://"):
        p = p[len("file://"):]
    elif p.startswith("file:"):
        p = p[len("file:"):]
    elif "://" in p:
        return ""
    if p == "~":
        p = session_dir
    elif p.startswith("~/"):
        p = os.path.join(session_dir, p[2:])
    if not os.path.isabs(p):
        p = os.path.normpath(os.path.join(session_dir, p))
    try:
        real = os.path.realpath(p)
    except OSError:
        return ""

    roots = [session_dir] + [r for r in (extra_roots or []) if r]
    if not any(path_is_under(real, r) for r in roots):
        return ""
    if is_sensitive_artifact_path(real, session_dir):
        return ""
    try:
        st = os.lstat(real)
    except OSError:
        return ""
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size <= 0:
        return ""
    if st.st_size > getattr(config, "max_inbound_file_bytes", 20 * 1024 * 1024):
        return ""
    return real


def _split_pig_prompt_and_files(
    safe_prompt: str, session_dir: str, extra_roots: list[str]
) -> tuple[str, list[str]]:
    """Split inline ``@<path>`` mentions into independent ``@file`` argv args.

    Three-way magic-byte routing per resolved real path:
      - supported static image   -> ``@realpath`` into file_args, token removed
      - small UTF-8 text file    -> ``@realpath`` into file_args, token
                                    replaced by an inline attachment note
      - anything else (APNG, PDF, big/binary) -> never a file arg; the token
                                    is rewritten to the bare real path so the
                                    model may inspect it via read/bash tools
    Nicknames (@alice) and unresolved/unsafe tokens stay untouched.
    """
    if not safe_prompt or "@" not in safe_prompt:
        return safe_prompt, []

    file_args: list[str] = []
    seen_paths: set[str] = set()

    def _add_file_arg(real: str) -> bool:
        if len(file_args) >= _PIG_MAX_FILE_ARGS:
            return False
        if real in seen_paths:
            return True
        seen_paths.add(real)
        file_args.append("@" + real)
        return True

    def _replace(m: re.Match) -> str:
        raw_token = m.group(1)
        trailing = raw_token[len(raw_token.rstrip(".,!?;:)>]}'\"")):]
        real = _resolve_at_token(raw_token, session_dir, extra_roots)
        if not real:
            return m.group(0)
        if _is_pig_supported_image(real):
            if _add_file_arg(real):
                return "" + trailing
            return m.group(0)
        if _is_small_utf8_text(real):
            if _add_file_arg(real):
                return f"(已附文本文件 {os.path.basename(real)})" + trailing
            return m.group(0)
        # Binary/oversized: strip the @ and keep the plain real path inline so
        # pig never reads it as a @file (no U+FFFD blob in the prompt).
        return real + trailing

    new_prompt = _AT_TOKEN_RE.sub(_replace, safe_prompt)
    if file_args and not new_prompt.strip():
        new_prompt = "请描述这张图片的内容"
    return new_prompt, file_args


def _neutralize_pig_prompt_prefix(prompt_text: str) -> str:
    """Leading ``/`` / ``@`` / ``-`` would trigger pig's built-in command,
    @file, or flag parsing even after ``--`` — prepend a single space."""
    text = prompt_text if prompt_text.strip() else "你好"
    if text.startswith(("/", "@", "-")):
        return " " + text
    return text


# ---------------------------------------------------------------------------
# Runtime prompt composition (persona / plan / add_dirs → runtime_prompt.txt)
# ---------------------------------------------------------------------------

def _runtime_prompt_path(agent_dir: str) -> str:
    return os.path.join(agent_dir, "runtime_prompt.txt")


def _pig_persona_path(session_dir: str) -> str:
    return os.path.join(session_dir, "pig_persona.txt")


def _read_pig_persona_file(path: str, max_chars: int = 0) -> str:
    """Read *path* only if it is a single-linked regular file (no symlink,
    no hardlink). Opened with O_NOFOLLOW + fstat so a link planted between
    lstat and open cannot redirect the read to a host credential file.
    """
    # O_NONBLOCK keeps a FIFO planted in place of pig_persona.txt from
    # blocking the asyncio event loop forever on open()/read(); the fstat
    # regular-file check below still rejects it (returns "").
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return ""
    try:
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                return ""
            data = f.read()
    except OSError:
        return ""
    return data[:max_chars] if max_chars else data


def _read_pig_persona(session_dir: str) -> str:
    return _read_pig_persona_file(_pig_persona_path(session_dir)).strip()


def _write_runtime_prompt(agent_dir: str, session_dir: str, prefs: dict) -> str:
    """Compose the --append-system-prompt content into a fixed 0o600 file.

    Returns the file path when content was written, "" otherwise. Using a
    bridge-controlled file path (never user text) avoids pig's
    resolvePromptInput treating the argument as a host file to read.
    """
    sections: list[str] = []
    persona = _read_pig_persona(session_dir)
    if persona:
        sections.append(persona)
    if (prefs.get("mode") or "") == "plan":
        sections.append(_PLAN_MODE_PROMPT)
    add_dirs = prefs.get("add_dirs") or []
    if add_dirs:
        listing = "\n".join(f"- {d}" for d in add_dirs[:MAX_ADD_DIRS])
        sections.append(
            "已授权附加工作目录（可读写的额外根目录）：\n" + listing
        )
    content = "\n\n".join(s for s in sections if s.strip()).strip()
    path = _runtime_prompt_path(agent_dir)
    if not content:
        try:
            if os.path.lexists(path):
                os.unlink(path)
        except OSError:
            pass
        return ""
    if not _check_no_link_or_hardlink(path):
        logger.error("Refusing to write runtime prompt (symlink/hardlink): %s", path)
        return ""
    if _atomic_write_private(path, content):
        return path
    return ""


# ---------------------------------------------------------------------------
# Session lifecycle helpers
# ---------------------------------------------------------------------------

def _has_pig_session(session_dir: str) -> bool:
    """True if at least one non-empty .jsonl session file exists (safe to --continue)."""
    root = _pig_sessions_dir(session_dir)
    if not os.path.isdir(root):
        return False
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for fn in filenames:
                if not fn.endswith(".jsonl"):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.isfile(fp) and not os.path.islink(fp) and os.lstat(fp).st_size > 0:
                        return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def _clear_pig_sessions(session_dir: str) -> None:
    """Delete every .jsonl under .pig/agent/sessions and prune empty buckets.

    models.json / settings.json one level up are never touched.

    A symlinked ``sessions`` leaf — or ANY symlinked ancestor between
    *session_dir* and it (``.pig`` / ``.pig/agent``) — is never
    traversed: under User=root the link could point at a foreign
    ~/.pig/agent tree and /clear + self-heal would then wipe that host's
    session history. The trust anchor is *session_dir* itself:
    components above it (a symlinked session store path) are
    bridge-owned. Fail-closed on any trust violation.
    """
    from .runner_common import (
        _pig_session_root_active,
        _pig_sessions_tree_untrusted,
    )

    root = _pig_sessions_dir(session_dir)
    if not os.path.isdir(root) or os.path.islink(root):
        return
    # /clear of a LIVE turn must not unlink sessions mid-run: the
    # stable-key active check stays correct even if the turn's bash
    # tool swapped the ``sessions`` chain (the key never resolves the
    # attacker-controlled components).
    if _pig_session_root_active(root):
        return
    if _pig_sessions_tree_untrusted(root, user_dir=session_dir):
        logger.warning(
            "Refusing to clear pig sessions with symlink/escape ancestor: %s", root
        )
        return
    try:
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            if os.path.isfile(path):
                if name.endswith(".jsonl"):
                    try:
                        os.remove(path)
                    except OSError as e:
                        logger.warning("Failed to remove pig session %s: %s", path, e)
                continue
            if not os.path.isdir(path):
                continue
            try:
                for fn in os.listdir(path):
                    fp = os.path.join(path, fn)
                    if fn.endswith(".jsonl") and os.path.isfile(fp) and not os.path.islink(fp):
                        try:
                            os.remove(fp)
                        except OSError as e:
                            logger.warning("Failed to remove pig session %s: %s", fp, e)
            except OSError as e:
                logger.warning("Failed to list pig session dir %s: %s", path, e)
                continue
            try:
                if not os.listdir(path):
                    os.rmdir(path)
            except OSError:
                pass
    except OSError as e:
        logger.warning("Failed to clear pig sessions %s: %s", root, e)


def _pig_thinking(prefs: dict) -> str:
    """Effective --thinking level: prefs effort wins, else PIG_DEFAULT_THINKING."""
    effort = (prefs.get("effort") or "").strip().lower()
    if effort in PIG_ALLOWED_THINKING:
        return effort
    return PIG_DEFAULT_THINKING


# ---------------------------------------------------------------------------
# Command builder (pure function for testability)
# ---------------------------------------------------------------------------

def _build_pig_command(
    cli_prompt: str,
    file_args: list[str],
    prefs: dict,
    use_continue: bool,
    append_prompt_arg: str,
) -> list:
    """Build pig argv. Every flag below was verified against pig's parser."""
    is_plan = (prefs.get("mode") or "") == "plan"
    cmd = [
        config.pig_binary_path,
        "--mode", "json",
        "-p",
        "--no-stdin",
        "--no-extensions",
        "--no-mcp",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
        "--provider", PIG_PROVIDER_ID,
        "--model", PIG_MODEL_ID,
        "--thinking", _pig_thinking(prefs),
        "--tools", _PLAN_TOOLS if is_plan else _NORMAL_TOOLS,
        "--system-prompt", "",
        "--append-system-prompt", append_prompt_arg,
    ]
    if use_continue:
        cmd.append("--continue")
    cmd.append("--")
    cmd.extend(file_args)
    cmd.append(cli_prompt)
    return cmd


# ---------------------------------------------------------------------------
# --mode json JSONL output parsing
# ---------------------------------------------------------------------------

def _redact_key(text: str) -> str:
    """Force-replace the live Magpie key with [REDACTED] in display text."""
    key = _resolve_magpie_api_key()
    if key and key in text:
        return text.replace(key, "[REDACTED]")
    return text


def _parse_pig_output(
    stdout_text: str,
    stderr_text: str,
    returncode: int,
    session_dir: str,
    snapshot_before: dict,
    t0: float,
    extra_roots: list[str],
) -> tuple[str, list, bool, str]:
    """Parse pig --mode json stdout into (display, artifacts, failed, err_src).

    Joint returncode + last-assistant stopReason invariant:
      - stopReason error/aborted → failed regardless of returncode
      - returncode != 0 with no usable text → failed
      - stopReason length → keep generated text + truncation notice
    """
    last_assistant_text = ""
    last_stop_reason = ""
    last_error_message = ""
    retry_final_error = ""
    non_json_lines: list[str] = []
    pending_tool_paths: dict[str, str] = {}
    tool_written_paths: list[str] = []

    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            non_json_lines.append(line)
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")

        if etype == "auto_retry_end" and event.get("success") is False:
            retry_final_error = (event.get("finalError") or "").strip()
            continue

        if etype == "tool_execution_start":
            tool_name = event.get("toolName") or event.get("tool") or ""
            args = event.get("args") or event.get("arguments") or {}
            call_id = event.get("toolCallId") or event.get("id") or ""
            if tool_name in _PIG_WRITE_TOOLS and call_id and isinstance(args, dict):
                for key in ("path", "file_path", "target_file"):
                    val = args.get(key)
                    if isinstance(val, str) and val.strip():
                        pending_tool_paths[call_id] = val.strip()
                        break
            continue

        if etype == "tool_execution_end":
            call_id = event.get("toolCallId") or event.get("id") or ""
            if not event.get("isError") and call_id in pending_tool_paths:
                tool_written_paths.append(pending_tool_paths.pop(call_id))
            continue

        messages = []
        if etype in ("message_end", "turn_end"):
            msg = event.get("message")
            if isinstance(msg, dict):
                messages.append(msg)
        elif etype == "agent_end":
            msgs = event.get("messages")
            if isinstance(msgs, list):
                messages.extend(m for m in msgs if isinstance(m, dict))
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            stop = msg.get("stopReason") or ""
            err = (msg.get("errorMessage") or "").strip()
            text_parts = []
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str) and t:
                        text_parts.append(t)
            # Every assistant message overwrites — a final round that is
            # tool-only must NOT leave a previous toolUse narration behind
            # (mirrors print_mode.go which prints only the LAST message).
            last_assistant_text = "".join(text_parts)
            if stop:
                last_stop_reason = stop
            if err:
                last_error_message = err

    stderr_clean = clean_output(stderr_text or "")
    non_json_summary = clean_output("\n".join(non_json_lines[:5]))

    failed = False
    err_src = ""
    if last_stop_reason in ("error", "aborted"):
        failed = True
        err_src = last_error_message or retry_final_error or stderr_clean or "未知错误"
    elif returncode != 0:
        # Any non-zero exit is a failure regardless of earlier toolUse
        # transition text — never scan artifacts or mark initialized on
        # crash, and never ship half-written deliverables.
        failed = True
        err_src = (
            last_error_message or retry_final_error or stderr_clean
            or non_json_summary or "process exited abnormally"
        )

    if failed:
        return format_cli_error(err_src, backend="pig"), [], True, err_src

    # Artifacts: text links + tool-written paths + session-dir delta scan.
    # Markdown links are pre-filtered through the exact same sensitive /
    # regular-single-link / in-roots checks as tool-written paths (SF-R2-1):
    # a link into the credential tree must never consume the per-turn
    # outbound quota or reach the failure-notice channel.
    artifacts = merge_artifact_groups(
        _filter_link_artifacts(
            extract_artifacts(
                last_assistant_text,
                session_dir,
                allowed_roots=[session_dir] + [r for r in (extra_roots or []) if r],
            ),
            session_dir,
            extra_roots,
        ),
        _resolve_tool_artifacts(tool_written_paths, session_dir, extra_roots),
        collect_session_fallback_artifacts(
            session_dir,
            snapshot_before,
            t0,
            ext_whitelist=ARTIFACT_FALLBACK_EXTENSIONS,
        ),
    )

    display = clean_output(_strip_file_links(last_assistant_text))
    if last_stop_reason == "length" and display:
        display += "\n\n⚠️（输出达到单轮长度上限已截断，可发送“继续”接着输出）"
    if not display and not artifacts:
        display = EMPTY_REPLY
    display = _redact_key(display)
    return display, artifacts, False, ""


def _filter_link_artifacts(
    pairs: list, session_dir: str, extra_roots: list[str] | None = None
) -> list:
    """Pre-filter extract_artifacts (name, path) tuples to the send contract.

    extract_artifacts already realpaths each candidate and confirms it sits
    under an allowed root; this second pass applies the same sensitive /
    regular-single-link checks _resolve_tool_artifacts enforces, so a
    markdown link into the credential tree can neither consume the
    outbound quota nor reach the send path (and its failure notice).
    Filtered entries are logged with a redacted name — never the raw one.
    """
    allowed_roots = [session_dir] + [r for r in (extra_roots or []) if r]
    out = []
    for name, fp in pairs or []:
        if not fp:
            continue
        try:
            real = os.path.realpath(fp)
        except OSError:
            continue
        if not any(path_is_under(real, r) for r in allowed_roots):
            continue
        if is_sensitive_artifact_path(real, session_dir):
            logger.warning(
                "Filtered sensitive artifact link: %s", _redact_key(str(name))
            )
            continue
        try:
            st = os.lstat(real)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            continue
        out.append((os.path.basename(real), real))
    return out


def _resolve_tool_artifacts(
    tool_paths: list[str], session_dir: str, extra_roots: list[str] | None = None
) -> list:
    """Resolve tool write/edit paths into (name, realpath) artifact tuples.

    Pre-filtered to the same contract agy/codex/grok/dsh runners enforce:
    only files inside allowed roots (session_dir + approved add_dirs),
    single-hardlink regular files, and never is_sensitive_artifact_path.
    A tool writing /tmp scratch or internal configs must not consume the
    per-turn outbound artifact quota or spam failure notices.
    """
    out = []
    for fp in tool_paths:
        if not fp:
            continue
        if not os.path.isabs(fp):
            fp = os.path.join(session_dir, fp)
        out.extend(_filter_link_artifacts([(os.path.basename(fp), fp)], session_dir, extra_roots))
    return out


# ---------------------------------------------------------------------------
# pig CLI execution
# ---------------------------------------------------------------------------

async def _spawn_pig(cmd: list, env: dict, session_dir: str):
    """Spawn pig in its own process group (terminate_process kills the group)."""
    kwargs = {}
    if hasattr(os, "setsid"):
        kwargs["start_new_session"] = True
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=session_dir,
        env=env,
        **kwargs,
    )


async def run_pig(prompt: str, user_id: str, timeout: int = None) -> tuple:
    """Execute pig CLI for a given user message.

    Returns (cleaned_display_text, list_of_(name, abs_path)_artifacts).
    """
    if timeout is None:
        timeout = int(getattr(config, "pig_timeout", 570) or 570)

    if len(prompt.encode("utf-8", errors="replace")) > _MAX_ARG_BYTES:
        logger.warning("Prompt too large for argv from user %s", user_id)
        return format_error(
            "消息过长",
            f"这条消息太长了（超过 {_MAX_ARG_BYTES // 1024}KB），请精简或分段发送。",
        ), []

    t0 = time.time()
    session_dir = ensure_session_dir(user_id)

    if not _pig_has_credentials():
        logger.warning("pig credentials missing: no MAGPIE_API_KEY")
        return format_cli_error("No API key found for magpie", backend="pig"), []

    logger.info("[AUDIT] user=%s prompt=%.200s", user_id, prompt)
    if is_dangerous(prompt):
        logger.warning("[AUDIT] dangerous keyword in prompt from user=%s", user_id)

    agent_dir = ensure_user_pig(session_dir)
    if not agent_dir:
        # Fail-closed: a symlinked/hijacked .pig tree or a failed config
        # write must never reach the spawn path (key injection, runtime
        # prompt write, subprocess) — return the fixed error bubble.
        logger.error(
            "pig agent dir failed verification for user %s; refusing to run",
            user_id,
        )
        return format_error(
            "执行出错",
            "助手环境初始化失败，请稍后再试。若一直失败，请联系管理员。",
        ), []
    prefs = load_prefs(user_id)
    extra_roots = []
    for d in (prefs.get("add_dirs") or [])[:MAX_ADD_DIRS]:
        ok, resolved = validate_add_dir(d, user_id)
        if ok:
            extra_roots.append(resolved)

    safe_prompt = _sanitize_prompt_at_paths(prompt, session_dir, extra_roots)
    split_prompt, file_args = _split_pig_prompt_and_files(
        safe_prompt, session_dir, extra_roots
    )
    cli_prompt = _neutralize_pig_prompt_prefix(split_prompt)
    append_prompt_arg = _write_runtime_prompt(agent_dir, session_dir, prefs)

    first = is_first_message(session_dir, backend="pig") or not _has_pig_session(session_dir)
    snapshot_before = _snapshot_regular_files([session_dir])

    env = _apply_pig_runtime_env(sanitize_env(session_dir), session_dir)
    env["PAGER"] = "cat"
    env["CI"] = "true"
    env["NONINTERACTIVE"] = "1"

    cmd = _build_pig_command(cli_prompt, file_args, prefs, not first, append_prompt_arg)
    logger.info(
        "pig run: user=%s continue=%s file_args=%d plan=%s thinking=%s",
        user_id, not first, len(file_args),
        (prefs.get("mode") or "") == "plan", _pig_thinking(prefs),
    )

    process = None
    # Register the sessions tree as active for the whole turn so the
    # periodic TTL cleaner (worker thread) never unlinks a just-resumed
    # .jsonl while pig is still running. The registration takes the
    # per-session clean lock — which the cleaner may be holding through a
    # disk pass — so it MUST run off the event loop: a synchronous call
    # here would stall every coroutine (cancellation, long polls, other
    # users' turns) behind the cleaner's I/O.
    #
    # Cancellation window: an await on a worker future still lets the
    # coroutine be cancelled (relogin drain timeout in main.py). A pool
    # thread already started CANNOT be un-scheduled — it will eventually
    # acquire the clean lock and bump the refcount, AFTER the cancelled
    # await already aborted. Unless that key is handed back,
    # _ACTIVE_PIG_SESSIONS keeps a +1 with nobody holding it and the
    # user's history is skipped by the 30d TTL forever.
    #
    # The handoff needs no extra thread: ``_register_capturing`` itself
    # runs on the worker thread, so the worker can repay its own late +1
    # when it finds the coroutine already cancelled. ``_reg_lock`` guards
    # ONLY the two-slot state below — never the registration call, which
    # takes the per-session clean lock and may wait on a cleaner pass.
    # Three interleaves, all paired with zero extra threads:
    #   1. cancelled while still queued → the worker never runs; the
    #      except branch sets cancelled and finds no key to repay;
    #   2. cancelled while the worker runs / waits on the clean lock →
    #      the except branch sets cancelled and exits immediately; the
    #      worker later lands its +1, sees cancelled, and unregisters
    #      its own key right there;
    #   3. cancelled after the worker completed the handoff → the except
    #      branch picks up ``_reg_state["key"]`` and unregisters it.
    active_key = None
    _reg_lock = threading.Lock()
    _reg_state = {"cancelled": False, "key": None}
    _sessions_root = _pig_sessions_dir(session_dir)

    def _register_capturing():
        key = _register_active_pig_session(_sessions_root)
        with _reg_lock:
            if _reg_state["cancelled"]:
                _unregister_active_pig_session(key)
                return None
            _reg_state["key"] = key
            return key

    _reg_fut = asyncio.get_running_loop().run_in_executor(
        None, _register_capturing
    )
    try:
        active_key = await _reg_fut
    except asyncio.CancelledError:
        with _reg_lock:
            _reg_state["cancelled"] = True
            late_key = _reg_state["key"]
            _reg_state["key"] = None
        if late_key:
            _unregister_active_pig_session(late_key)
        raise

    def _pgid_of(p) -> int:
        """pgid of the child's group; start_new_session=True → pgid==pid.
        On platforms without setsid _spawn_pig omits start_new_session and
        the child shares the bridge's group — passing the pid would then
        target OUR group; return None so killpg is skipped there."""
        if p is None or not hasattr(os, "setsid"):
            return None
        return p.pid

    try:
        process = await _spawn_pig(cmd, env, session_dir)
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=float(timeout)
            )
        except asyncio.TimeoutError:
            logger.warning("pig execution timed out after %ss for user %s", timeout, user_id)
            await terminate_process(process, graceful=True, pgid=_pgid_of(process))
            return format_timeout_error(timeout, backend="pig"), []

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        display, artifacts, failed, err_src = _parse_pig_output(
            stdout_text, stderr_text, process.returncode,
            session_dir, snapshot_before, t0, extra_roots,
        )
        if process.returncode != 0 and not failed and not display:
            failed = True
            err_src = stderr_text or "process exited abnormally"
            display = format_cli_error(err_src, backend="pig")
            artifacts = []

        # Single self-heal retry: --continue failed because the recorded
        # session is missing/corrupt — clear sessions and retry fresh once.
        # Tightened: ONLY when the process actually exited non-zero AND the
        # *stderr* (not the upstream JSONL errorMessage, which may carry a
        # transient "no session available" from gemini-web) matches a local
        # session-corruption signature. An upstream transient must never
        # wipe the user's whole .pig/agent/sessions history.
        _SESSION_CORRUPT_MARKERS = (
            "no session found",
            "session not found",
            "not a valid pi session",
            "session: empty file",
            "failed to load session",
            "corrupt session",
        )
        if failed and not first and process.returncode != 0:
            stderr_lower = (stderr_text or "").lower()
            if any(m in stderr_lower for m in _SESSION_CORRUPT_MARKERS):
                logger.warning(
                    "pig --continue failed (session missing/corrupt) for %s, "
                    "clearing sessions and retrying once", user_id,
                )
                # Reap the first-attempt process group BEFORE the retry:
                # its background children (detached stdio) share the first
                # pgid, which the outer finally can no longer reach once
                # ``process`` is rebound to the retry process.
                await terminate_process(
                    process, graceful=False, pgid=_pgid_of(process)
                )
                _clear_pig_sessions(session_dir)
                clear_initialized(session_dir, backend="pig")
                retry_cmd = _build_pig_command(
                    cli_prompt, file_args, prefs, False, append_prompt_arg
                )
                retry_process = None
                try:
                    retry_process = await _spawn_pig(
                        retry_cmd, env, session_dir
                    )
                    try:
                        r_stdout, r_stderr = await asyncio.wait_for(
                            retry_process.communicate(), timeout=float(timeout)
                        )
                    except asyncio.TimeoutError:
                        await terminate_process(
                            retry_process, graceful=True, pgid=_pgid_of(retry_process)
                        )
                        return format_timeout_error(timeout, backend="pig"), []
                    r_stdout_text = r_stdout.decode("utf-8", errors="replace").strip()
                    r_stderr_text = r_stderr.decode("utf-8", errors="replace").strip()
                    r_display, r_artifacts, r_failed, _ = _parse_pig_output(
                        r_stdout_text, r_stderr_text, retry_process.returncode,
                        session_dir, snapshot_before, t0, extra_roots,
                    )
                    if retry_process.returncode != 0 and not r_failed and not r_display:
                        r_failed = True
                        r_display = format_cli_error(
                            r_stderr_text or "process exited abnormally", backend="pig"
                        )
                        r_artifacts = []
                    # Same non-empty success criterion as the normal
                    # path — an EMPTY_REPLY retry must NOT stamp the flag.
                    if not r_failed and r_display != EMPTY_REPLY:
                        mark_initialized(session_dir, backend="pig")
                    elapsed = time.time() - t0
                    logger.info(
                        "pig retry done: user=%s elapsed=%.1fs artifacts=%d failed=%s",
                        user_id, elapsed, len(r_artifacts), r_failed,
                    )
                    return r_display, r_artifacts
                finally:
                    # The group must be reaped even if the root already
                    # exited — a stray background child that survived its
                    # parent has no waitable handle otherwise. This also
                    # covers the graceful path above.
                    if retry_process is not None:
                        process = retry_process
                        await terminate_process(
                            retry_process,
                            graceful=retry_process.returncode is None,
                            pgid=_pgid_of(retry_process),
                        )

        # EMPTY_REPLY is not a successful turn — do not stamp the
        # .initialized.pig flag on an empty first response.
        if not failed and display != EMPTY_REPLY:
            mark_initialized(session_dir, backend="pig")
        elapsed = time.time() - t0
        logger.info(
            "pig done: user=%s elapsed=%.1fs artifacts=%d output=%d chars failed=%s",
            user_id, elapsed, len(artifacts), len(display), failed,
        )
        return display, artifacts

    except asyncio.CancelledError:
        await terminate_process(process, graceful=False, pgid=_pgid_of(process))
        raise
    except FileNotFoundError as e:
        await terminate_process(process, graceful=False, pgid=_pgid_of(process))
        if _is_cli_binary_missing(e, config.pig_binary_path):
            logger.error("CLI binary not found for pig: %s", e)
            return format_cli_error("command not found", backend="pig"), []
        logger.exception("Unexpected error running pig: %s", e)
        return format_error(
            "执行出错",
            "这次没处理好，请稍后再试。若一直失败，请联系管理员。",
        ), []
    except Exception as e:
        logger.exception("Unexpected error running pig: %s", e)
        await terminate_process(process, graceful=False, pgid=_pgid_of(process))
        return format_error(
            "执行出错",
            "这次没处理好，请稍后再试。若一直失败，请联系管理员。",
        ), []
    finally:
        # Group-level cleanup runs unconditionally: even when the root
        # process already exited (normal return, timeout handled above, or
        # an exception mid-path), a pig bash tool may have spawned a
        # background child that outlived its parent inside the same group.
        if process is not None:
            await terminate_process(
                process,
                graceful=process.returncode is None,
                pgid=_pgid_of(process),
            )
        _unregister_active_pig_session(active_key)


# ---------------------------------------------------------------------------
# Slash command support (single-model semantics)
# ---------------------------------------------------------------------------

_PIG_MODEL_ALIASES = frozenset({
    PIG_MODEL_ID,
    f"{PIG_PROVIDER_ID}/{PIG_MODEL_ID}",
    "gemini-4-neon",
})


def _split_model_thinking_suffix(arg: str) -> tuple[str, str]:
    """Split 'model:low' / 'model-low' → (base, thinking|'')."""
    for sep in (":", "-"):
        if sep in arg:
            base, _, tail = arg.rpartition(sep)
            if tail in PIG_ALLOWED_THINKING and base:
                return base, tail
    return arg, ""


def _cmd_pig_models(user_id: str) -> str:
    prefs = load_prefs(user_id)
    thinking = _pig_thinking(prefs)
    return (
        "📋 **可用模型 (pig)** 📋\n\n"
        f"- `{PIG_MODEL_ID}` — Provider `magpie`，上下文 1,000,000 tokens，"
        "最大输出 8,192 tokens，支持图文输入与 `low / medium / high` 思考档位\n\n"
        f"当前模型: `{PIG_MODEL_ID}` ｜ 思考档位: `{thinking}`"
    )


def _cmd_pig_model(args: str, user_id: str) -> str:
    name = args.strip()
    prefs = load_prefs(user_id)
    if not name:
        return (
            "📋 **当前模型** 📋\n\n"
            f"`{PIG_MODEL_ID}`（思考档位 `{_pig_thinking(prefs)}`）\n\n"
            f"pig 引擎仅开放这一个模型；可用 `/model {PIG_MODEL_ID}:low|medium|high` 调整思考档位。"
        )
    base, thinking = _split_model_thinking_suffix(name)
    if base in ("default", "reset") or base in _PIG_MODEL_ALIASES:
        fields = {"model": PIG_MODEL_ID}
        if thinking:
            fields["effort"] = thinking
        update_active_prefs(user_id, **fields)
        label = PIG_MODEL_ID + (f"（思考档位 `{thinking}`）" if thinking else "")
        return f"✅ **模型已切换** ✅\n\n`{label}`"
    return (
        "❌ **模型不可用** ❌\n\n"
        f"`{name}`\n\n"
        f"pig 引擎仅开放 `{PIG_MODEL_ID}`（不含 `-task` 等其它模型）。"
    )


def handle_pig_persona(args: str, user_id: str) -> str:
    """Handle /persona for pig — stored in pig_persona.txt (0o600), injected
    via runtime_prompt.txt → --append-system-prompt."""
    ensure_session_dir(user_id)
    session_dir = get_session_dir(user_id)
    persona_path = _pig_persona_path(session_dir)

    parts = args.strip().split(maxsplit=1)
    subcmd = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    def _write(content: str) -> str:
        content = content[:8000]
        try:
            fd, tmp = tempfile.mkstemp(dir=session_dir, prefix=".tmp_persona_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(tmp, 0o600)
            os.replace(tmp, persona_path)
            os.chmod(persona_path, 0o600)
            return "✅ **人格文档已更新** ✅"
        except OSError as e:
            logger.error("Failed to write pig persona for %s: %s", user_id, e)
            return "❌ **写入人格文档失败** ❌"

    if subcmd == "set" and rest:
        return _write(rest)
    if subcmd and subcmd not in ("show", "clear", "reset", "set"):
        return _write(args.strip())

    if subcmd == "show":
        if not os.path.exists(persona_path):
            return "（未设置人格文档）"
        # O_NOFOLLOW + fstat single-link check: a pig bash tool could have
        # planted a symlink/hardlink to a host credential file; never echo
        # it back. Always redact the live key before showing.
        val = _read_pig_persona_file(persona_path)
        if not val:
            return "（未设置人格文档）"
        # Redact BEFORE truncating: a key straddling the 1500-char cut
        # would otherwise echo its leading bytes to the user.
        val = _redact_key(val)
        if len(val) > 1500:
            val = val[:1500] + "\n\n（已截断至前1500字符）"
        return val or "（空文档）"

    if subcmd in ("clear", "reset"):
        if os.path.exists(persona_path):
            try:
                os.remove(persona_path)
                return "✅ **人格文档已清除** ✅" if subcmd == "clear" else "✅ **人格已重置** ✅"
            except OSError as e:
                logger.error("Failed to clear pig persona for %s: %s", user_id, e)
                return "❌ **清除人格文档失败** ❌"
        return "ℹ️ **本就无人格文档** ℹ️"

    return "📋 **/persona 用法** 📋\n\n- `/persona <内容>` 设置\n- `/persona show` 查看\n- `/persona clear` 清除\n- `/persona reset` 重置"


def _cmd_pig_help() -> str:
    lines = [
        "📋 **wechatbridge 支持指令 (pig)** 📋",
        "",
        "**模型控制**",
        f"- `/models` — 查看可用模型（仅 `{PIG_MODEL_ID}`）",
        f"- `/model {PIG_MODEL_ID}[:low|medium|high]` — 调整思考档位",
        "- `/backend <agy|grok|codex|dsh|pig>` — 切换助手引擎",
        "",
        "**对话控制**",
        "- `/clear` 或 `/new` — 重置对话（开始新会话）",
        "- `/fast` — 开启/关闭**快速模式**（`low` vs `high` 思考档位）",
        "- `/planning` — 开启/关闭**规划模式**（工具集硬切为 read,grep,find,ls）",
        "",
        "**工具**",
        "- `/add-dir <路径>` — 添加工作目录（`clear` 清空，无参查看列表）",
        "- `/agents` — 查看可用助手",
        "- `/mcp` — 扩展工具说明",
        "",
        "**人格**",
        "- `/persona <内容>` — 设置你专属的人格文档（另有 show / clear / reset）",
        "",
        "**其他**",
        "- `/help` — 显示本帮助",
        "- `/version` — 查看版本与更新状态",
    ]
    try:
        from .price import price_feature_ready
        if price_feature_ready():
            lines.append("- `/price <链接/口令/词>` — 京东/淘宝查券比价")
    except ImportError:
        pass
    lines += [
        "",
        "提示：其他 `/` 指令会直接交给助手处理。",
    ]
    return "\n".join(lines)


async def handle_pig_slash_command(text: str, user_id: str) -> str | None:
    """Handle /-slash commands for pig backend.

    Returns str for A/B/C classes, None for D class (passthrough to run_pig).
    """
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else text.lower()
    args = parts[1] if len(parts) > 1 else ""

    # --- B class: dangerous / rejected (never touch login state in bridge) ---
    B_CMDS = frozenset({"/exit", "/quit", "/logout", "/login"})
    if cmd in B_CMDS:
        return "⛔ **该指令在微信端禁用** ⛔"

    # --- C class: TUI panels (not supported on WeChat) ---
    C_CMDS = frozenset({
        "/settings", "/resume", "/tree", "/fork", "/theme", "/hotkeys",
        "/copy", "/export", "/share", "/reload", "/scoped-models",
        "/session", "/compact",
    })
    if cmd in C_CMDS:
        return (
            f"⚠️ **微信端不支持** ⚠️\n\n`{cmd}` 需要交互式终端，"
            "可用 `/new`、`/fast`、`/model` 替代。"
        )

    # --- A class: implemented commands ---
    if cmd == "/help":
        return _cmd_pig_help()

    if cmd in ("/clear", "/new"):
        session_dir = get_session_dir(user_id)
        clear_initialized(session_dir, backend="pig")
        _clear_pig_sessions(session_dir)
        prefs = load_prefs(user_id)
        if prefs.get("mode") == "plan":
            update_active_prefs(user_id, mode="")
        return "✅ **对话已重置** ✅"

    if cmd == "/fast":
        arg = args.strip().lower()
        prefs = load_prefs(user_id)
        if arg in ("off", "0", "false", "no", "normal", "default", "reset", "clear") or (not arg and prefs.get("effort") == "low"):
            update_active_prefs(user_id, effort="")
            return "✅ **已关闭快速模式** ✅（恢复默认 high 思考档位）"
        update_active_prefs(user_id, effort="low")
        return "✅ **已开启快速模式** ✅（low 思考档位）"

    if cmd == "/planning":
        arg = args.strip().lower()
        prefs = load_prefs(user_id)
        if arg in ("off", "0", "false", "no", "normal", "default", "reset", "clear", "agent") or (not arg and prefs.get("mode") == "plan"):
            update_active_prefs(user_id, mode="")
            return "✅ **已退出规划模式** ✅"
        update_active_prefs(user_id, mode="plan")
        return "✅ **已开启规划模式** ✅（工具集已限制为 read,grep,find,ls）"

    if cmd == "/model":
        return _cmd_pig_model(args, user_id)

    if cmd == "/models":
        return _cmd_pig_models(user_id)

    if cmd == "/add-dir":
        path = args.strip()
        prefs = load_prefs(user_id)
        if not path:
            dirs = prefs.get("add_dirs", []) or []
            if dirs:
                listing = "\n".join(f"- `{d}`" for d in dirs)
                return (
                    "📋 **当前工作目录** 📋\n\n"
                    f"{listing}\n\n"
                    "`/add-dir <路径>` 添加，`/add-dir clear` 清空。"
                )
            return "ℹ️ **/add-dir 用法** ℹ️\n\n`/add-dir <路径>` 添加工作目录，`/add-dir clear` 清空。"
        if path.lower() == "clear":
            prefs["add_dirs"] = []
            save_prefs(user_id, prefs)
            return "✅ **已清空工作目录** ✅"
        ok, result = validate_add_dir(path, user_id)
        if not ok:
            return f"❌ **目录不允许** ❌\n\n{result}"
        resolved = result
        dirs = prefs.get("add_dirs", [])
        if resolved not in dirs:
            if len(dirs) >= MAX_ADD_DIRS:
                return f"❌ **附加目录数量已达上限 ({MAX_ADD_DIRS})** ❌"
            dirs.append(resolved)
            prefs["add_dirs"] = dirs
            save_prefs(user_id, prefs)
        return (
            f"✅ **已记录工作目录** ✅\n\n```\n{resolved}\n```\n\n"
            "pig 的工具可读写该目录内文件。"
        )

    if cmd == "/persona":
        return handle_pig_persona(args, user_id)

    if cmd == "/agents":
        return (
            "ℹ️ **助手说明** ℹ️\n\n"
            "当前 pig 桥接运行于精简安全模式：仅内置文件/终端工具，"
            "已关闭外部子助手面板；`/agent <名称> <任务>` 仍可用（经确认门转发）。"
        )

    if cmd == "/mcp":
        if not config.enable_mcp:
            return "ℹ️ **该功能已禁用** ℹ️"
        return (
            "ℹ️ **扩展工具说明** ℹ️\n\n"
            "当前 pig 桥接运行于精简安全模式，已关闭外部 MCP 与扩展"
            "（保障隔离与低内存）；内置 read/bash/edit/write 工具可用。"
        )

    # --- D class: passthrough to pig (return None so caller runs run_pig) ---
    return None
