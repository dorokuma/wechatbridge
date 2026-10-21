"""codex (OpenAI Codex CLI) runner with per-user session isolation.

Mirrors grok.py's interface: run_codex(), handle_codex_slash_command(),
ensure_user_codex(), get_session_dir() — all imported from runner_common.

codex headless entry point: `codex exec --json` emits a JSONL event stream and
`codex exec resume <thread_id> <prompt>` resumes an explicit session (the thread
id is the rollout uuid written under $CODEX_HOME/sessions/YYYY/MM/DD/).
"""

import asyncio
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
import uuid

from .config import config
from .runner_common import (
    sanitize_user_id, get_session_dir, ensure_session_dir, is_first_message, mark_initialized, clear_initialized,
    clean_output, load_prefs, save_prefs, is_dangerous, parse_model_effort,
    sanitize_env, terminate_process, update_active_prefs, is_bridge_formatted_reply,
    format_error, format_cli_error, format_model_label, EMPTY_REPLY, validate_add_dir, path_is_under,
    MAX_ADD_DIRS, is_sensitive_artifact_path, _sanitize_prompt_at_paths,
)

logger = logging.getLogger("codex_runner")

# execve 单参数上限（Linux MAX_ARG_STRLEN = 128KB），留安全余量
_MAX_ARG_BYTES = 120 * 1024

# 续轮失败降级重试：仅当错误是明确的 resume / session 不存在语义才触发，
# 绝对不能包含裸 `missing`（会误伤 missing credentials / missing model 等）
# 或裸 `not found`（会误伤 file not found 等）。每条都是精确短语，由
# _is_resume_session_lost() 辅助函数逐一判定，不靠一个宽泛 OR 正则。
# 注意：不对完整 stdout JSONL 文本做正则（见 _extract_codex_error_messages）。
_RESUME_SESSION_NOT_FOUND_RES: tuple = (
    re.compile(r"\bsession not found\b", re.IGNORECASE),
    re.compile(r"\bconversation not found\b", re.IGNORECASE),
    re.compile(r"\bno such session\b", re.IGNORECASE),
    re.compile(r"\bunknown session\b", re.IGNORECASE),
    re.compile(r"\bmissing session\b", re.IGNORECASE),
    # 真实 Codex 文案（codex-rs 源码确证）：
    # - codex-rs/thread-store/src/local/read_thread.rs:
    #       "no rollout found for thread id {thread_id}"
    #   codex-rs/app-server/.../thread_processor.rs:
    #       "no rollout found for conversation id {conversation_id}"
    # - codex-rs/thread-store/src/local/update_thread_metadata.rs:
    #       "thread not found: {thread_id}"
    # 均为精确短语、低误判：不含裸 `missing` / 裸 `not found`，
    # 普通 turn error（rate limit / network / model / missing credentials /
    # file not found / permission denied）不会命中。
    re.compile(r"\bno rollout found\b", re.IGNORECASE),
    # 归档线程恢复路径（codex-rs/.../unarchive_thread.rs）：
    # "no archived rollout found for thread id {thread_id}" —— 明确说明该
    # thread 已无可恢复的 rollout，可降级为首次重试。注意：仅此精确短语；
    # `thread ... is archived` 不在此列（归档线程仍可用 `codex unarchive`
    # 恢复，不属于"无法恢复"，不得误判为会话丢失）。
    re.compile(r"\bno archived rollout found\b", re.IGNORECASE),
    re.compile(r"\bthread not found\b", re.IGNORECASE),
)


def _is_resume_session_lost(err_text: str) -> bool:
    """判定续轮失败是否源于明确的 session 不存在语义（应降级为首次重试）。

    只匹配精确短语：``session not found``、``conversation not found``、
    ``no such session``、``unknown session``、``missing session``，以及真实
    Codex 文案 ``no rollout found``（thread / conversation id）、
    ``no archived rollout found``、``thread not found``。
    不对 ``missing credentials`` / ``missing model`` / ``file not found`` /
    ``rate limit`` / ``network error`` / ``permission denied`` / ``thread ...
    is archived``（可 unarchive 恢复）触发——因此绝不使用裸 ``missing`` 或
    裸 ``not found`` 的宽正则。

    err_text 通常来自 ``_extract_codex_error_messages(stdout_text)`` 与
    stderr 的合并（结构化错误优先，避免把正常的 thread.started / 限流 /
    网络 / 模型错误误判为 resume 失败）。
    """
    if not err_text:
        return False
    text = err_text.lower()
    return any(rx.search(text) for rx in _RESUME_SESSION_NOT_FOUND_RES)

# 文件名含 thread_id 的 rollout（含压缩版 .jsonl.zst，feature 默认关闭但需前向兼容）
_ROLLOUT_RE = re.compile(r"^rollout-.*\.jsonl(\.zst)?$")


# ---------------------------------------------------------------------------
# Per-user .codex directory setup
# ---------------------------------------------------------------------------

def _host_codex_dir() -> str:
    """Host-level ~/.codex (bridge process home), not the per-user session HOME.

    The real credentials live on the machine (admin runs `codex login
    --with-api-key`), and each session links to them so refreshes propagate.
    """
    host_home = os.environ.get("WECHATBRIDGE_HOST_HOME") or os.path.expanduser("~")
    return os.path.join(host_home, ".codex")


def _session_auth_is_regular(path: str) -> bool:
    """True if path exists as a regular file (lstat, no symlink follow)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _atomic_copy_auth(src: str, dst: str) -> bool:
    """Atomically copy src -> dst (same-dir temp file + os.replace).

    The temp file lives in dirname(dst) to stay on the same filesystem.
    Never logs auth file content — only paths and the failure reason.
    """
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(dst), prefix=".auth.json.", suffix=".tmp"
        )
        with os.fdopen(fd, "wb") as fout:
            with open(src, "rb") as fin:
                shutil.copyfileobj(fin, fout)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, dst)
        tmp_path = None
        return True
    except OSError as e:
        logger.error("Atomic copy auth -> %s failed: %s", dst, e)
        return False
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _promote_session_auth(codex_dir: str) -> bool:
    """Treat a regular session auth.json as refreshed credentials.

    The CLI refreshes tokens via temp-file + rename; the rename replaces the
    session symlink with a regular file, so the new credentials (incl. the
    rotated refresh token) exist only in the session while host keeps the
    already-revoked old ones. Promote the session file back to host
    atomically, then re-link the session to the host file. The session
    file is never unlinked when the copy fails or the non-empty
    validation fails; after a successful copy the re-link step does
    unlink the session file and rebuild the symlink (the new credentials
    already live in host at that point, so no data is lost). Creates the
    host dir (0o700) when missing.
    """
    dest = os.path.join(codex_dir, "auth.json")
    if not _session_auth_is_regular(dest):
        return True
    host = os.path.join(_host_codex_dir(), "auth.json")
    host_dir = os.path.dirname(host)
    try:
        if not os.path.isdir(host_dir):
            os.makedirs(host_dir, mode=0o700, exist_ok=True)
    except OSError as e:
        logger.error("Cannot create host codex dir %s: %s", host_dir, e)
        return False
    try:
        if os.path.getsize(dest) <= 0:
            logger.error(
                "Refusing to promote empty session auth %s (host %s untouched)",
                dest,
                host,
            )
            return False
    except OSError as e:
        logger.error("Cannot stat session auth %s: %s", dest, e)
        return False
    if not _atomic_copy_auth(dest, host):
        logger.error(
            "Promote session auth failed; keeping %s (host %s untouched)", dest, host
        )
        return False
    try:
        host_real = os.path.realpath(host)
        os.unlink(dest)
        os.symlink(host_real, dest)
        logger.info("Promoted session auth.json -> %s", host_real)
        return True
    except OSError as e:
        logger.error("Re-link %s after promote failed: %s", dest, e)
        return False


def _sync_codex_auth(codex_dir: str) -> bool:
    """Make session .codex/auth.json track the host login credentials.

    CLI refresh writes $CODEX_HOME/auth.json via temp-file + rename; the
    rename replaces a session symlink with a regular file, so refreshed
    credentials (incl. the rotated refresh token) land only in the session
    while host keeps the old, already-revoked ones. A regular session
    auth.json is therefore always treated as newer credentials: promote it
    back to host atomically, then re-link the session to the host file. On
    promote failure the session file is never unlinked. Falls back to copy2
    if symlink is not possible.
    Returns True if credentials are available for the child process.
    """
    auth_src = os.path.join(_host_codex_dir(), "auth.json")
    auth_dst = os.path.join(codex_dir, "auth.json")

    # 普通文件 = CLI rename 拆掉 symlink 后留下的更新凭证，先回流 host
    if _session_auth_is_regular(auth_dst):
        if not _promote_session_auth(codex_dir):
            # promote 失败：session 文件是唯一的新凭证，绝不 unlink
            return os.path.isfile(auth_dst)

    if not os.path.isfile(auth_src):
        logger.warning("Host codex auth missing: %s", auth_src)
        return False

    try:
        src_real = os.path.realpath(auth_src)
        if os.path.islink(auth_dst) or os.path.exists(auth_dst):
            try:
                if os.path.realpath(auth_dst) == src_real and os.path.isfile(auth_dst):
                    return True
            except OSError:
                pass
            try:
                os.unlink(auth_dst)
            except OSError:
                pass

        try:
            os.symlink(src_real, auth_dst)
            logger.info("Linked session auth.json -> %s", src_real)
            return True
        except OSError as e:
            logger.warning("symlink auth.json failed (%s), falling back to copy", e)
            shutil.copy2(auth_src, auth_dst)
            os.chmod(auth_dst, 0o600)
            return True
    except OSError as e:
        logger.warning("Failed to sync auth.json into %s: %s", codex_dir, e)
        return False


def ensure_user_codex(user_id: str) -> str:
    """Ensure per-user .codex directory with auth credentials.

    Creates session/.codex/ for codex config and conversations (CODEX_HOME
    defaults to $HOME/.codex, and HOME points at session_dir when running).
    Always syncs host auth.json (symlink preferred).
    Returns session_dir path (used as HOME when running codex).
    """
    session_dir = ensure_session_dir(user_id)
    codex_dir = os.path.join(session_dir, ".codex")
    os.makedirs(codex_dir, exist_ok=True)
    try:
        os.chmod(codex_dir, 0o700)
    except OSError:
        pass

    _sync_codex_auth(codex_dir)

    return session_dir


# ---------------------------------------------------------------------------
# thread_id persistence (codex resume needs an explicit id)
# ---------------------------------------------------------------------------

def _codex_thread_id_path(session_dir: str) -> str:
    return os.path.join(session_dir, ".codex_thread_id")


def _read_codex_thread_id(session_dir: str) -> str:
    """Read persisted thread_id for resume; '' if none/invalid.

    严格校验规范 UUID 字符串（小写、8-4-4-4-12 形式）。空白 / 非法 /
    超长 / 换行污染一律返回 ''，调用方按首轮处理（无效状态等价于无会话）。
    """
    path = _codex_thread_id_path(session_dir)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        return ""
    # 长度保护：规范 UUID 字符串固定 36 字符；超长（含拼接污染）直接拒绝。
    # 换行/回车污染（多行注入）同样拒绝。
    if not raw or len(raw) > 36 or "\n" in raw or "\r" in raw:
        return ""
    try:
        u = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        return ""
    # 要求规范字符串（小写 hyphenated），拒绝无连字符 / 大括号 / 大写等变形，
    # 避免把任意 32 位十六进制串当成合法 thread_id 误续轮。
    if str(u) != raw:
        return ""
    return raw


def _write_codex_thread_id(session_dir: str, thread_id: str) -> None:
    try:
        with open(_codex_thread_id_path(session_dir), "w", encoding="utf-8") as f:
            f.write(thread_id)
    except OSError as e:
        logger.warning("Failed to write codex thread_id for %s: %s", session_dir, e)


def _delete_codex_thread_id(session_dir: str) -> None:
    path = _codex_thread_id_path(session_dir)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as e:
        logger.warning("Failed to delete codex thread_id for %s: %s", session_dir, e)


def _has_codex_session(session_dir: str, thread_id: str) -> bool:
    """Pre-check before resume: a rollout file containing thread_id exists.

    codex stores sessions under .codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
    where <uuid> == thread_id. Traverse the date buckets and look for a rollout
    file whose name contains the thread_id.
    """
    if not thread_id:
        return False
    sessions_root = os.path.join(session_dir, ".codex", "sessions")
    if not os.path.isdir(sessions_root):
        return False
    try:
        for year in os.listdir(sessions_root):
            yp = os.path.join(sessions_root, year)
            if not os.path.isdir(yp):
                continue
            for month in os.listdir(yp):
                mp = os.path.join(yp, month)
                if not os.path.isdir(mp):
                    continue
                for day in os.listdir(mp):
                    dp = os.path.join(mp, day)
                    if not os.path.isdir(dp):
                        continue
                    for fn in os.listdir(dp):
                        if _ROLLOUT_RE.match(fn) and thread_id in fn:
                            return True
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# Persona persistence (via AGENTS.md — codex auto-reads it from cwd)
# ---------------------------------------------------------------------------

def _persona_path(session_dir: str) -> str:
    # codex 的 core AGENTS.md 发现机制从 cwd 向上查找 AGENTS.md，
    # 子进程 cwd=session_dir，所以直接写在 session_dir/AGENTS.md 即可被读取。
    return os.path.join(session_dir, "AGENTS.md")


def _read_persona(session_dir: str) -> str:
    """Read persona content (AGENTS.md). Returns empty string if none."""
    path = _persona_path(session_dir)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""
    return ""


def handle_codex_persona(args: str, user_id: str) -> str:
    """Handle /persona command for codex backend.

    Stores persona text in AGENTS.md, which codex auto-reads each exec.
    Subcommands: set <content>, show, clear, reset (same as clear).
    """
    # 用户可能在首次对话前就设人格，确保会话目录存在
    ensure_session_dir(user_id)
    session_dir = get_session_dir(user_id)
    persona_path = _persona_path(session_dir)

    parts = args.strip().split(maxsplit=1)
    subcmd = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    # set or implicit content
    if subcmd == "set" and rest:
        try:
            with open(persona_path, "w", encoding="utf-8") as f:
                f.write(rest)
            return "✅ **人格文档已更新** ✅"
        except OSError as e:
            logger.error("Failed to write persona for %s: %s", user_id, e)
            return "❌ **写入人格文档失败** ❌"
    elif subcmd and subcmd not in ("show", "clear", "reset", "set"):
        try:
            with open(persona_path, "w", encoding="utf-8") as f:
                f.write(args.strip())
            return "✅ **人格文档已更新** ✅"
        except OSError as e:
            logger.error("Failed to write persona for %s: %s", user_id, e)
            return "❌ **写入人格文档失败** ❌"

    # show
    if subcmd == "show":
        if not os.path.exists(persona_path):
            return "（未设置人格文档）"
        try:
            with open(persona_path, "r", encoding="utf-8") as f:
                val = f.read()
            if len(val) > 1500:
                val = val[:1500] + "\n\n（已截断至前1500字符）"
            return val or "（空文档）"
        except OSError as e:
            logger.error("Failed to read persona for %s: %s", user_id, e)
            return "❌ **读取人格文档失败** ❌"

    # clear
    if subcmd == "clear":
        if os.path.exists(persona_path):
            try:
                os.remove(persona_path)
                return "✅ **人格文档已清除** ✅"
            except OSError as e:
                logger.error("Failed to clear persona for %s: %s", user_id, e)
                return "❌ **清除人格文档失败** ❌"
        return "ℹ️ **本就无人格文档** ℹ️"

    # reset (codex has no global default persona; same as clear)
    if subcmd == "reset":
        if os.path.exists(persona_path):
            try:
                os.remove(persona_path)
                return "✅ **人格已重置** ✅"
            except OSError as e:
                logger.error("Failed to reset persona for %s: %s", user_id, e)
                return "❌ **重置人格文档失败** ❌"
        return "ℹ️ **本就无人格文档** ℹ️"

    # empty args
    return "📋 **/persona 用法** 📋\n\n- `/persona <内容>` 设置\n- `/persona show` 查看\n- `/persona clear` 清除\n- `/persona reset` 重置"


# ---------------------------------------------------------------------------
# Command builder (pure function for testability)
# ---------------------------------------------------------------------------

def _build_codex_command(prompt: str, prefs: dict, first: bool, thread_id: str = "") -> list:
    """Build codex exec argv list. Pure function — does not execute.

    Flag mapping (grok -> codex):
      --always-approve (exec already auto-approves) -> --dangerously-bypass-approvals-and-sandbox
      --reasoning-effort                         -> -c model_reasoning_effort=<effort>
      --mode plan                                -> --sandbox read-only (approx; not full plan mode)
      -m                                         -> -m
      --continue (implicit)                      -> resume <thread_id> subcommand
      --rules (grok)                            -> AGENTS.md file (auto-read from cwd)

    Note: codex reads persona via AGENTS.md from cwd, so no argv flag is needed.
    """
    cmd = [config.codex_binary_path, "exec", "--json", "--skip-git-repo-check"]

    mode = prefs.get("mode", "")
    if mode == "plan":
        # 近似 plan mode：只读沙箱（不能写文件/跑命令），文档注明不完全等同
        # 真实 clap 枚举是 kebab-case：read-only（不是 readonly）。
        cmd += ["--sandbox", "read-only"]
    else:
        cmd += ["--dangerously-bypass-approvals-and-sandbox"]

    model = prefs.get("model", "")
    effort = prefs.get("effort", "")
    if model:
        base_model, embedded_effort = parse_model_effort(model)
        if embedded_effort and effort:
            cmd += ["-m", base_model, "-c", f"model_reasoning_effort={effort}"]
        elif embedded_effort:
            cmd += ["-m", model]
        else:
            cmd += ["-m", model]
            if effort:
                cmd += ["-c", f"model_reasoning_effort={effort}"]
    elif effort:
        cmd += ["-c", f"model_reasoning_effort={effort}"]

    # add_dirs -> each --add-dir (codex 原生支持，比 grok 的"仅记录"强)
    # add_dirs 已由 _resolve_add_dirs 通过 validate_add_dir 规范化
    for d in prefs.get("add_dirs", []) or []:
        if d:
            cmd += ["--add-dir", d]

    # Session continuation: resume is a subcommand, options must precede it
    if not first:
        cmd += ["resume", thread_id, prompt]
    else:
        cmd += [prompt]
    return cmd


# ---------------------------------------------------------------------------
# Output parser (pure function for testability)
# ---------------------------------------------------------------------------

def _collect_codex_artifact(path: str, session_dir: str, since: float, seen: set, artifacts: list) -> None:
    """Resolve a file_change path to an absolute one and append if fresh enough."""
    if not path:
        return
    if not os.path.isabs(path):
        path = os.path.join(session_dir, path)
    try:
        path = os.path.realpath(path)
    except (OSError, ValueError):
        return
    if is_sensitive_artifact_path(path, session_dir):
        return
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return  # 文件已不存在，无需回传
    # 只收录本轮运行期间新写/修改的文件
    if since and mtime < since - 2.0:
        return
    name = os.path.basename(path)
    key = (name, path)
    if key not in seen:
        seen.add(key)
        artifacts.append(key)


def _resolve_add_dirs(raw_dirs: list, session_dir: str, user_id: str = "") -> list:
    """Validate add_dirs via validate_add_dir for command building + fallback scan.

    Calls validate_add_dir once per directory to get resolved realpaths.
    Ensures consistency between the --add-dir flags passed to Codex and the
    allowed roots used by the artifact fallback scan.
    """
    validated = []
    for d in (raw_dirs or [])[:MAX_ADD_DIRS]:
        if not d:
            continue
        try:
            ok, result = validate_add_dir(d, user_id)
        except Exception as e:
            logger.warning("validate_add_dir failed for %s: %s", d, e)
            continue
        if ok and result:
            validated.append(result)
    return validated


_EXCLUDED_SCAN_NAMES = frozenset({".codex", ".git", "__pycache__", "node_modules", "venv", ".venv", "cache", ".cache"})


def _snapshot_regular_files(roots: list) -> dict:
    """Snapshot regular files (path, mtime) under allowed roots for fallback scan.

    Enforces limits: <=200 files, <=50 directories, <=2 seconds. Returns empty
    dict if any limit is exceeded (never partial results).

    ``os.walk(followlinks=False)`` naturally excludes symlink traversal;
    ``_EXCLUDED_SCAN_NAMES`` skips internal metadata dirs; only regular files
    (not dirs/symlinks/special) are collected.
    """
    snap = {}
    t_start = time.monotonic()
    file_count = 0
    dir_count = 0
    _MAX_FILES = 200
    _MAX_DIRS = 50
    _MAX_SCAN_TIME = 2.0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dir_count += 1
            if dir_count > _MAX_DIRS:
                return {}
            if time.monotonic() - t_start > _MAX_SCAN_TIME:
                return {}
            # Exclude internal metadata directories in-place
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_SCAN_NAMES]
            for fn in filenames:
                file_count += 1
                if file_count > _MAX_FILES:
                    return {}
                fp = os.path.join(dirpath, fn)
                try:
                    if not os.path.isfile(fp) or os.path.islink(fp):
                        continue
                    # Verify candidate is under one of the allowed roots
                    if not any(path_is_under(fp, r) for r in roots):
                        continue
                    snap[fp] = os.path.getmtime(fp)
                except OSError:
                    continue
    return snap


def _parse_codex_output(stdout_text: str, session_dir: str, since: float = 0.0) -> tuple:
    """Parse codex JSONL output into (display_text, artifacts, thread_id, parse_failed).

    stdout is a JSONL stream (not a single JSON). Events:
      thread.started{thread_id}            -> capture thread_id (caller persists)
      item.completed agent_message{text}   -> final reply (last one wins)
      item.completed file_change{changes}  -> artifacts (add/update paths only);\n                                      a file_change with status=="failed" is\n                                      NOT collected (failed writes must not be\n                                      returned as artifacts); missing status =\n                                      legacy/compatible event, still collected
      turn.failed{error} / error{message}  -> format_cli_error 且置 parse_failed=True
    Non-JSON lines are skipped; if nothing parses, fall back to plain text.

    ``parse_failed`` is a *structured* failure flag: it is set True ONLY when a
    ``turn.failed`` / ``error`` event is seen in the stream. It is NOT derived
    from whether ``display`` happens to start with ``❌`` — a normal agent reply
    may legitimately begin with that emoji, and must still be treated as a
    success (and have its thread_id persisted). Callers use ``parse_failed``
    (combined with a non-zero exit code) to decide success vs. failure.
    """
    if not stdout_text:
        return EMPTY_REPLY, [], "", False

    agent_texts: list = []
    artifacts: list = []
    seen: set = set()
    thread_id = ""
    display = ""
    # parse_failed: 仅由结构化错误事件（turn.failed / error）置位。
    # 不要用 display 是否以 ❌ 开头判断——正常 agent 回复也可能以 ❌ 开头，
    # 那种必须视为成功并落盘 thread_id。parse_failed 只代表"stdout 含结构化错误"。
    parse_failed = False
    parsed_any = False

    for raw in stdout_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            parsed_any = True
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue

        t = d.get("type")
        if t == "thread.started":
            tid = d.get("thread_id") or ""
            if tid:
                thread_id = tid
        elif t == "item.completed":
            item = d.get("item") or {}
            it = item.get("type")
            if it == "agent_message":
                txt = item.get("text", "")
                if txt:
                    agent_texts.append(txt)
            elif it == "file_change":
                # 真实 FileChangeItem 带 status（in_progress/completed/failed）。
                # 失败的 file_change 不得作为 artifact 收集（其 changes 里的
                # add/update 文件不应回传）。缺 status 的旧/兼容事件保持现有
                # 兼容行为（照常收集），不要一律拒绝。
                if item.get("status") == "failed":
                    continue
                for ch in item.get("changes", []) or []:
                    if isinstance(ch, dict) and ch.get("kind") in ("add", "update"):
                        _collect_codex_artifact(ch.get("path", ""), session_dir, since, seen, artifacts)
        elif t == "turn.failed":
            err = d.get("error") or {}
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            display = format_cli_error(msg, backend="codex")
            parse_failed = True
        elif t == "error":
            msg = d.get("message", "")
            display = format_cli_error(msg, backend="codex")
            parse_failed = True

    # 全部非 JSON（或空）：回退为纯文本
    if not parsed_any:
        return clean_output(stdout_text) or EMPTY_REPLY, [], "", False

    if parse_failed:
        return display, [], thread_id, True

    last = agent_texts[-1] if agent_texts else ""
    if not last:
        last = EMPTY_REPLY

    # 去掉文件里的 file:/// 链接（避免把内部路径直出给微信用户）
    last = re.sub(
        r"\[((?:[^\[\]]|\[[^\[\]]*\])+)\]\(file:///(?:[^()\s]|\([^()\s]*\))+\)",
        r"[\1]",
        last,
    )
    last = re.sub(
        r"\[([^\]]+)\]\(file:///[^)]+\)",
        r"[\1]",
        last,
    )
    return clean_output(last) or EMPTY_REPLY, artifacts, thread_id, False


def _extract_codex_error_messages(stdout_text: str) -> list:
    """从 JSONL stdout 抽取结构化错误消息，供 resume 失败判定使用。

    只取 error.message 与 turn.failed.error.message（error 可能是对象含
    message，也可能是字符串）。**不对完整 stdout 文本做正则**——否则正常的
    thread.started 或 rate limit / network / model 错误会被误匹配为 resume 失败。
    返回字符串列表（已过滤空值）。
    """
    msgs: list = []
    if not stdout_text:
        return msgs
    for raw in stdout_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if t == "turn.failed":
            err = d.get("error") or {}
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            if msg:
                msgs.append(msg)
        elif t == "error":
            msg = d.get("message", "")
            if isinstance(msg, str) and msg:
                msgs.append(msg)
    return msgs


# ---------------------------------------------------------------------------
# codex CLI execution
# ---------------------------------------------------------------------------


def _collect_fallback_artifacts(session_dir: str, snapshot_before: dict, t0: float) -> list:
    """Scan session_dir for files created or overwritten during this turn.

    Codex often emits a ``file_change`` for a helper script and then writes
    the real pdf/docx via the shell. Always merge this list with structured
    artifacts (dedupe by realpath). Scans only session_dir (never add_dirs).
    Excludes internal metadata dirs. Each candidate is verified under session_dir.
    """
    _internal_names = {".codex_thread_id"}
    fallback = []
    fallback_seen = set()
    for fp, fmtime in _snapshot_regular_files([session_dir]).items():
        prev = snapshot_before.get(fp)
        if prev is None:
            if fmtime < t0:
                continue
        elif fmtime <= prev:
            continue
        name = os.path.basename(fp)
        if name in _internal_names or name.startswith(".initialized.") or is_sensitive_artifact_path(fp, session_dir):
            continue
        key = (name, fp)
        if key not in fallback_seen:
            fallback_seen.add(key)
            fallback.append(key)
    return fallback


def _merge_codex_artifacts(*groups) -> list:
    """Dedupe (name, path) tuples by realpath, preserve first-seen order."""
    merged = []
    seen = set()
    for group in groups:
        for item in group or []:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            name, path = item
            try:
                key = os.path.realpath(path)
            except OSError:
                key = path
            if key in seen:
                continue
            seen.add(key)
            merged.append((name, path))
    return merged

async def run_codex(prompt: str, user_id: str, timeout: int = None) -> tuple:
    """Execute codex CLI for a given user message.

    Mirrors run_grok() interface.
    Returns (cleaned_display_text, list_of_(name, abs_path)_artifacts).
    """
    if timeout is None:
        timeout = config.agy_timeout

    # 空 / 全空白 prompt：在任何 session/env/subprocess 副作用前拒绝，绝不启动进程。
    if not prompt or not prompt.strip():
        logger.warning("Empty/whitespace prompt rejected for user %s", user_id)
        return format_error(
            "空的消息",
            "这条消息是空的，请发送一段文字内容。",
        ), []

    # argv 单参数上限约 128KB（MAX_ARG_STRLEN），超长 prompt 直接拒绝，避免 E2BIG
    if len(prompt.encode("utf-8", errors="replace")) > _MAX_ARG_BYTES:
        logger.warning("Prompt too large for argv from user %s", user_id)
        return format_error(
            "消息过长",
            f"这条消息太长了（超过 {_MAX_ARG_BYTES // 1024}KB），请精简或分段发送。",
        ), []

    t0 = time.time()
    session_dir = ensure_user_codex(user_id)
    codex_dir = os.path.join(session_dir, ".codex")

    # Audit logging
    logger.info("[AUDIT] user=%s prompt=%.200s", user_id, prompt)
    if is_dangerous(prompt):
        logger.warning("[AUDIT] dangerous keyword in prompt from user=%s", user_id)

    first = is_first_message(session_dir, backend="codex")

    # 防线一（预检）：续轮前检查 thread_id 文件 + 对应 rollout 存在；
    # 找不到按首轮处理（与 grok 的 _has_grok_session 对齐）。
    thread_id = ""
    if not first:
        thread_id = _read_codex_thread_id(session_dir)
        if not thread_id or not _has_codex_session(session_dir, thread_id):
            logger.info(
                "codex session missing despite .initialized.codex for %s, "
                "treating as first message",
                user_id,
            )
            first = True

    prefs = load_prefs(user_id)
    # 验证并规范化 add_dirs，确保命令构建与 fallback 扫描使用同一批目录
    validated_add_dirs = _resolve_add_dirs(prefs.get("add_dirs"), session_dir, user_id)
    prefs["add_dirs"] = validated_add_dirs
    safe_prompt = _sanitize_prompt_at_paths(prompt, session_dir, extra_roots=validated_add_dirs)
    # persona 经 AGENTS.md 由 codex 自动读取，这里无需注入 argv
    cmd = _build_codex_command(safe_prompt, prefs, first, thread_id)

    # 执行前快照现有文件，用于 shell 产物 fallback 扫描（仅 session_dir）
    snapshot_before = _snapshot_regular_files([session_dir])

    if first:
        logger.info("First message for user %s, running: codex exec ...", user_id)
    else:
        logger.info("Continuing conversation for user %s, running: codex exec resume ...", user_id)

    process = None
    try:
        env = sanitize_env(session_dir)
        # 显式覆盖服务环境的全局 CODEX_HOME：codex 默认把会话/日志/缓存写进
        # $CODEX_HOME（指向宿主 ~/.codex），这里强制指向本会话私有的
        # session_dir/.codex，确保每用户会话隔离，不串到全局目录。
        # sanitize_env 不会清除 CODEX_HOME（非敏感名），故必须显式覆盖一次；
        # 下方 retry 复用同一个 env 字典，覆盖同样生效。
        env["CODEX_HOME"] = os.path.join(session_dir, ".codex")
        env["PAGER"] = "cat"
        env["CI"] = "true"
        env["NONINTERACTIVE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        # sanitize_env 会洗掉 CODEX_API_KEY（_is_sensitive_env_name：*_KEY），
        # 若 bridge 进程本身持有该 env，则显式回注，支持 env 认证方式。
        raw_key = os.environ.get("CODEX_API_KEY")
        if raw_key:
            env["CODEX_API_KEY"] = raw_key

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=session_dir,
            env=env,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=float(timeout),
        )

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        display, artifacts, parsed_thread_id, parse_failed = _parse_codex_output(stdout_text, session_dir, since=t0)

        # 失败判定：非零退出一律视为失败（与 grok 行为对齐）；
        # 零退出但 stdout 含结构化错误事件（turn.failed / error）也算失败。
        # 注意：不再用 display 是否以 ❌ 开头来判定失败——正常 agent 回复也可能
        # 以 ❌ 开头，那种必须视为成功并落盘 thread_id（见 parse_failed 标志）。
        failed = process.returncode != 0 or parse_failed
        if process.returncode != 0:
            logger.warning(
                "codex exited with code %s for user %s: %.200s",
                process.returncode, user_id, stderr_text,
            )
            artifacts = []
            # 结构化错误（parse_failed 由 turn.failed/error 置位）已有格式化
            # display；仅当非结构化非零退出才用 stderr 兜底格式化。
            if not parse_failed:
                raw_err = stderr_text or ("" if display == EMPTY_REPLY else display) or "process exited abnormally"
                display = format_cli_error(raw_err, backend="codex")

        # 防线二（失败后降级重试）：resume 退出码非 0 且结构化错误匹配
        # 明确的 session 不存在语义时，清状态按首轮重试一次。
        if failed and not first:
            # 续轮失败：仅当错误是明确的 resume / session 不存在语义才降级为
            # 首次运行重试。使用 _is_resume_session_lost() 精确判定（不靠裸
            # `missing` / 裸 `not found` 宽正则），因此 missing credentials、
            # missing model、file not found、rate limit、network error、
            # permission denied 等普通错误不会误触发。结构化错误消息只从 JSONL
            # 的 error.message 与 turn.failed.error.message 抽取（兼容 error
            # 为字符串/对象），再与 stderr 合并；**绝不对完整 stdout 文本做
            # 正则**，否则正常的 thread.started 会被误判。
            structured_errs = _extract_codex_error_messages(stdout_text)
            raw_err_check = "\n".join([stderr_text or "", *structured_errs])
            if _is_resume_session_lost(raw_err_check):
                logger.warning(
                    "codex resume failed for %s, retrying without resume",
                    user_id,
                )
                clear_initialized(session_dir, backend="codex")
                _delete_codex_thread_id(session_dir)
                retry_cmd = _build_codex_command(safe_prompt, prefs, True, "")
                retry_process = await asyncio.create_subprocess_exec(
                    *retry_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    cwd=session_dir,
                    env=env,
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None,
                )
                try:
                    r_stdout, r_stderr = await asyncio.wait_for(
                        retry_process.communicate(),
                        timeout=float(timeout),
                    )
                except asyncio.TimeoutError:
                    await terminate_process(retry_process, graceful=True)
                    return format_error("处理超时", f"超过 {timeout} 秒未完成，已终止本次任务。"), []
                except (asyncio.CancelledError, Exception):
                    await terminate_process(retry_process, graceful=False)
                    raise

                r_stdout_text = r_stdout.decode("utf-8", errors="replace").strip()
                r_stderr_text = r_stderr.decode("utf-8", errors="replace").strip()
                r_display, r_artifacts, r_thread_id, r_parse_failed = _parse_codex_output(r_stdout_text, session_dir, since=t0)
                r_failed = retry_process.returncode != 0 or r_parse_failed
                if retry_process.returncode != 0:
                    logger.warning(
                        "codex retry exited with code %s for user %s: %.200s",
                        retry_process.returncode, user_id, r_stderr_text,
                    )
                    r_artifacts = []
                    if not r_parse_failed:
                        raw_err = r_stderr_text or ("" if r_display == EMPTY_REPLY else r_display) or "process exited abnormally"
                        r_display = format_cli_error(raw_err, backend="codex")
                if not r_failed:
                    mark_initialized(session_dir, backend="codex")
                    if r_thread_id:
                        _write_codex_thread_id(session_dir, r_thread_id)
                # Fallback: 重试成功也扫本轮新建/覆盖的文件，与 file_change 合并
                if not r_failed:
                    extra = _collect_fallback_artifacts(session_dir, snapshot_before, t0)
                    if extra:
                        before = len(r_artifacts)
                        r_artifacts = _merge_codex_artifacts(r_artifacts, extra)
                        added = len(r_artifacts) - before
                        if added:
                            logger.info(
                                "Fallback scan collected %d additional files on retry",
                                added,
                            )
                elapsed = time.time() - t0
                logger.info(
                    "codex retry done: user=%s elapsed=%.1fs artifacts=%d output=%d chars failed=%s",
                    user_id, elapsed, len(r_artifacts), len(r_display), r_failed,
                )
                return r_display, r_artifacts

        # 仅在真正的成功回复后标记已初始化并落盘 thread_id
        if first and not failed:
            mark_initialized(session_dir, backend="codex")
            if parsed_thread_id:
                _write_codex_thread_id(session_dir, parsed_thread_id)

        # Fallback: 成功就扫本轮新建/覆盖的文件，与 file_change 合并去重。
        # 不能再要求 artifacts 为空——先写出脚本再终端生成 pdf 时会漏文档。
        if not failed:
            extra = _collect_fallback_artifacts(session_dir, snapshot_before, t0)
            if extra:
                before = len(artifacts)
                artifacts = _merge_codex_artifacts(artifacts, extra)
                added = len(artifacts) - before
                if added:
                    logger.info("Fallback scan collected %d additional files", added)

        elapsed = time.time() - t0
        logger.info(
            "codex done: user=%s elapsed=%.1fs artifacts=%d output=%d chars failed=%s",
            user_id, elapsed, len(artifacts), len(display), failed,
        )
        return display, artifacts

    except asyncio.TimeoutError:
        logger.warning("codex execution timed out after %ss for user %s", timeout, user_id)
        await terminate_process(process, graceful=True)
        return format_error("处理超时", f"超过 {timeout} 秒未完成，已终止本次任务。"), []

    except asyncio.CancelledError:
        # 任务被取消（如重登录前排空）：必须杀掉子进程再传递取消
        await terminate_process(process, graceful=False)
        raise

    except Exception as e:
        logger.exception("Unexpected error running codex: %s", e)
        await terminate_process(process, graceful=False)
        return format_error(
            "执行出错",
            "这次没处理好，请稍后再试。若一直失败，请联系管理员。",
        ), []

    finally:
        # 子进程结束后立刻 harvest：CLI 可能刚用 rename 拆掉 symlink 刷新了
        # 凭证，普通文件一律回流 host（成功/非零退出/超时/取消均覆盖）
        _promote_session_auth(codex_dir)


# ---------------------------------------------------------------------------
# Slash command support
# ---------------------------------------------------------------------------

async def _run_codex_subcommand(subcmd_args: list, user_id: str) -> str:
    """Run a short codex subcommand (e.g. ``debug models``) and return cleaned output.

    Timeout is fixed at 30 seconds. Session isolation matches ``run_codex``:
    per-user ``HOME`` / ``CODEX_HOME``, optional ``CODEX_API_KEY`` refill after
    ``sanitize_env``.
    """
    session_dir = ensure_user_codex(user_id)
    codex_dir = os.path.join(session_dir, ".codex")
    cmd = [config.codex_binary_path] + list(subcmd_args)
    process = None
    try:
        env = sanitize_env(session_dir)
        env["CODEX_HOME"] = os.path.join(session_dir, ".codex")
        env["PAGER"] = "cat"
        env["CI"] = "true"
        env["NONINTERACTIVE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        raw_key = os.environ.get("CODEX_API_KEY")
        if raw_key:
            env["CODEX_API_KEY"] = raw_key

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=session_dir,
            env=env,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=30.0
        )
        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            logger.warning(
                "codex %s exited with code %s",
                " ".join(subcmd_args),
                process.returncode,
            )
            return format_cli_error(
                stderr_text or stdout_text or "终端指令执行失败",
                backend="codex",
            )

        return clean_output(stdout_text) or EMPTY_REPLY

    except asyncio.TimeoutError:
        await terminate_process(process, graceful=True)
        return format_error("查询超时", "查询超时，请稍后再试。")
    except asyncio.CancelledError:
        await terminate_process(process, graceful=False)
        raise
    except Exception as e:
        logger.exception("codex subcommand error: %s", e)
        await terminate_process(process, graceful=False)
        return format_error(
            "执行出错",
            "这次没处理好，请稍后再试。若一直失败，请联系管理员。",
        )

    finally:
        _promote_session_auth(codex_dir)


def _parse_codex_models(output: str) -> list[str]:
    """Loose-parse model slug list from ``codex debug models`` stdout.

    Accepts JSON (any shape with ``slug`` fields, or a ``models`` array of
    strings/objects) and falls back to line-oriented text (one name per line,
    optional leading bullets).
    """
    if not output or not output.strip():
        return []

    text = output.strip()
    models: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        name = (name or "").strip()
        if not name or name in seen:
            return
        # Skip obvious non-slug noise
        if name.startswith(("[", "{", "(", "<")):
            return
        if name in (EMPTY_REPLY, "(empty response)"):
            return
        seen.add(name)
        models.append(name)

    def _walk(obj, *, under_models: bool = False) -> None:
        if isinstance(obj, dict):
            slug = obj.get("slug")
            if isinstance(slug, str) and slug.strip():
                _add(slug)
            elif under_models:
                # Inside a models array: accept id/name/model when slug absent
                for key in ("id", "name", "model"):
                    val = obj.get(key)
                    if isinstance(val, str) and val.strip():
                        _add(val)
                        break
            # Prefer the conventional "models" key, then recurse the rest
            if "models" in obj:
                _walk(obj["models"], under_models=True)
            for k, v in obj.items():
                if k == "models":
                    continue
                _walk(v, under_models=False)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, str) and under_models:
                    _add(item)
                else:
                    _walk(item, under_models=under_models)

    # Try full-text JSON first, then per-line JSON (JSONL-ish).
    try:
        data = json.loads(text)
        # Top-level bare list of strings counts as a models array
        if isinstance(data, list) and data and all(isinstance(x, str) for x in data):
            _walk(data, under_models=True)
        else:
            _walk(data)
    except (json.JSONDecodeError, TypeError, ValueError):
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith(("{", "[")):
                continue
            try:
                piece = json.loads(line)
                if isinstance(piece, list) and piece and all(isinstance(x, str) for x in piece):
                    _walk(piece, under_models=True)
                else:
                    _walk(piece)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue

    if models:
        return models

    # Line-oriented fallback (only when JSON yielded nothing useful)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Strip common bullets / list markers
        for prefix in ("* ", "- ", "• ", "· "):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        # Take first token; drop trailing parenthetical notes
        token = line.split()[0] if line.split() else ""
        token = token.split("(")[0].strip().rstrip(",;:")
        if token and not token.endswith(":") and not token.lower().startswith("available"):
            _add(token)

    return models


async def _fetch_codex_model_list(user_id: str) -> list[str] | None:
    """Return live model slugs, or None if the list cannot be retrieved.

    Tries ``codex debug models`` first, then ``codex debug models --bundled``.
    Strict: empty / error / bridge-formatted failure → None (caller must not write prefs).
    """
    for args in (["debug", "models"], ["debug", "models", "--bundled"]):
        output = await _run_codex_subcommand(args, user_id)
        if not output or output.startswith("[error]") or is_bridge_formatted_reply(output):
            continue
        if output == EMPTY_REPLY:
            continue
        models = _parse_codex_models(output)
        if models:
            return models
    return None


def _builtin_codex_models_note() -> str:
    """Fallback reference list when live ``debug models`` is unavailable."""
    return (
        "📋 **codex 可用模型（参考）** 📋\n\n"
        "暂时无法从本机 `codex debug models` 拉取实时列表，以下为常见模型名，"
        "以你的 OpenAI 账户实际可用为准：\n\n"
        "- `gpt-5.1-codex`\n"
        "- `gpt-5.1`\n"
        "- `gpt-5-codex`\n"
        "- `gpt-5`\n"
        "- `gpt-4.1`\n\n"
        "请稍后重试 `/models`，或直接 `/model <名称>`（会校验实时列表）。"
    )


async def _cmd_model(args: str, user_id: str) -> str:
    """Handle /model <name>: validate against ``codex debug models``, then save.

    Strict: list fetch failure or unknown name → refuse and do not write prefs.
    Matching order (same as agy): exact match, then first prefix match.
    """
    name = args.strip()
    if not name:
        prefs = load_prefs(user_id)
        return (
            "📋 **当前模型** 📋\n\n"
            f"`{format_model_label(prefs.get('model', ''))}`\n\n"
            "用 `/models` 查看可用模型，`/model <名称>` 切换。"
        )

    models = await _fetch_codex_model_list(user_id)
    if models is None:
        return "❌ **无法获取模型列表** ❌"

    def _apply(model_name: str) -> str:
        _, embedded = parse_model_effort(model_name)
        if embedded:
            update_active_prefs(user_id, model=model_name, effort="")
        else:
            update_active_prefs(user_id, model=model_name)
        return f"✅ **模型已切换** ✅\n\n`{model_name}`"

    # Exact match
    if name in models:
        return _apply(name)

    # Prefix match (first hit, same as agy)
    prefix_matches = [m for m in models if m.startswith(name)]
    if prefix_matches:
        return _apply(prefix_matches[0])

    return f"❌ **模型不存在** ❌\n\n`{name}`"


async def _cmd_models(user_id: str) -> str:
    """List codex models via live ``debug models``; fall back to built-in note."""
    models = await _fetch_codex_model_list(user_id)
    if models is None:
        return _builtin_codex_models_note()

    lines = [
        "📋 **codex 可用模型** 📋",
        "",
        "（via `codex debug models`）",
        "",
    ]
    for m in models:
        lines.append(f"- `{m}`")
    lines.append("")
    lines.append("用 `/model <名称>` 切换（支持前缀匹配）。")
    return "\n".join(lines)


def _cmd_help() -> str:
    """Build /help response for codex backend."""
    lines = [
        "📋 **wechatbridge 支持指令 (codex)** 📋",
        "",
        "**模型控制**",
        "- `/model <名称>` — 切换模型（用 `/models` 查看可用列表）",
        "- `/models` — 查看可用模型列表",
        "- `/backend <agy|grok|codex>` — 切换助手引擎",
        "",
        "**对话控制**",
        "- `/clear` 或 `/new` — 重置对话（开始新会话）",
        "- `/fast` — 开启**快速模式**（回答更快，思考更少）",
        "- `/planning` — 开启**规划模式**（只读沙箱，近似 plan mode）",
        "",
        "**工具**",
        "- `/add-dir <路径>` — 添加工作目录（真正生效，后续带 `--add-dir`）",
        "",
        "**人格**",
        "- `/persona <内容>` — 设置你专属的人格文档（另有 show / clear / reset）",
        "",
        "**其他**",
        "- `/help` — 显示本帮助",
        "",
        "提示：其他 `/` 指令会直接交给助手处理。",
    ]
    return "\n".join(lines)


async def handle_codex_slash_command(text: str, user_id: str) -> str | None:
    """Handle /-slash commands for codex backend.

    Returns str for A/B/C classes, None for D class (passthrough to run_codex).
    """
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else text.lower()
    args = parts[1] if len(parts) > 1 else ""

    # --- B class: dangerous / rejected ---
    B_CMDS = frozenset({"/exit", "/quit", "/logout"})
    if cmd in B_CMDS:
        return "⛔ **该指令在微信端禁用** ⛔"

    # --- C class: TUI panels (not supported on WeChat) ---
    C_CMDS = frozenset({
        "/config", "/settings", "/context", "/diff", "/artifact", "/tasks",
        "/hooks", "/keybindings", "/permissions", "/statusline",
        "/copy", "/open", "/rename", "/fork", "/branch", "/rewind", "/undo",
        "/resume", "/switch", "/conversation", "/title", "/feedback",
        "/usage", "/quota", "/credits", "/skills",
    })
    if cmd in C_CMDS:
        return f"⚠️ **微信端不支持** ⚠️\n\n`{cmd}`"

    # --- A class: implemented commands ---
    if cmd == "/help":
        return _cmd_help()

    if cmd in ("/clear", "/new"):
        session_dir = get_session_dir(user_id)
        clear_initialized(session_dir, backend="codex")
        _delete_codex_thread_id(session_dir)
        prefs = load_prefs(user_id)
        if prefs.get("mode"):
            update_active_prefs(user_id, mode="")
        return "✅ **对话已重置** ✅"

    if cmd == "/fast":
        arg = args.strip().lower()
        prefs = load_prefs(user_id)
        if arg in ("off", "0", "false", "no", "normal", "default", "reset", "clear") or (not arg and prefs.get("effort") == "low"):
            update_active_prefs(user_id, effort="")
            return "✅ **已关闭快速模式** ✅（恢复默认思考强度）"
        update_active_prefs(user_id, effort="low")
        return "✅ **已开启快速模式** ✅"

    if cmd == "/planning":
        arg = args.strip().lower()
        prefs = load_prefs(user_id)
        if arg in ("off", "0", "false", "no", "normal", "default", "reset", "clear", "agent") or (not arg and prefs.get("mode") == "plan"):
            update_active_prefs(user_id, mode="")
            return "✅ **已退出规划模式** ✅"
        update_active_prefs(user_id, mode="plan")
        return "✅ **已开启规划模式** ✅（只读沙箱，近似 plan mode）"

    if cmd == "/model":
        return await _cmd_model(args, user_id)

    if cmd == "/models":
        return await _cmd_models(user_id)

    if cmd == "/add-dir":
        path = args.strip()
        if not path:
            return "❌ **缺少参数** ❌\n\n`/add-dir <路径>`"
        ok, result = validate_add_dir(path, user_id)
        if not ok:
            return f"❌ **目录不允许** ❌\n\n{result}"
        resolved = result
        prefs = load_prefs(user_id)
        dirs = prefs.get("add_dirs", [])
        if resolved not in dirs:
            if len(dirs) >= MAX_ADD_DIRS:
                return f"❌ **附加目录数量已达上限 ({MAX_ADD_DIRS})** ❌"
            dirs.append(resolved)
            prefs["add_dirs"] = dirs
            save_prefs(user_id, prefs)
        return (
            f"✅ **已添加工作目录** ✅\n\n```\n{resolved}\n```\n\n"
            "ℹ️ 后续对话会带上 `--add-dir`，codex 可读取/写入该目录。"
        )

    if cmd == "/agents":
        # codex 没有独立的 agents 概念，返回内置说明文案
        return (
            "ℹ️ **codex 的 agents** ℹ️\n\n"
            "codex 没有独立的 agents 列表；直接在对话里用自然语言描述你想要的"
            "子任务即可，codex 会自行规划执行。"
        )

    if cmd == "/persona":
        return handle_codex_persona(args, user_id)

    # /mcp
    if cmd == "/mcp":
        if not config.enable_mcp:
            return "ℹ️ **该功能已禁用** ℹ️"
        return (
            "ℹ️ **扩展工具说明** ℹ️\n\n"
            "可以直接用自然语言让助手调用已配置的扩展工具。\n\n"
            "示例：\n"
            "> 用 codegraph 的 search 搜一下 ctxmode\n"
            "> 帮我查一下这个项目里 xxx 怎么实现的"
        )

    # /agent 已上移到 main.py 统一处理（必须经过危险确认门，不能再绕过）

    # --- D class: passthrough to codex (return None so caller runs run_codex) ---
    return None
