"""
agy CLI runner with per-user session isolation, output cleanup, and timeout protection.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from urllib.parse import unquote

from .config import config
from .runner_common import (
    sanitize_user_id, get_session_dir, ensure_session_dir, is_first_message, mark_initialized, clear_initialized,
    clean_output, load_prefs, save_prefs, is_dangerous, parse_model_effort,
    sanitize_env, terminate_process, update_active_prefs,
    format_error, format_cli_error, _classify_cli_error, is_bridge_formatted_reply, EMPTY_REPLY,
    ANSI_RE, HTML_TAG_RE, validate_add_dir,
)

logger = logging.getLogger("agy_runner")

# execve 单参数上限（Linux MAX_ARG_STRLEN = 128KB），留安全余量
_MAX_ARG_BYTES = 120 * 1024


def extract_artifacts(text: str) -> list[tuple[str, str]]:
    """Extract (name, absolute_path) tuples from markdown file:/// links.

    Uses regex ``\\[([^\\]]+)\\](file:///([^)]+))`` to find agy-generated
    artifact references in stdout. Returns deduplicated, order-preserved list.

    Paths and display names are URL-decoded (``urllib.parse.unquote``) so
    percent-encoded spaces / CJK (e.g. ``my%20report.pdf``, ``%E6%8A%A5%E5%91%8A.pdf``)
    resolve to real filesystem paths.
    """
    if not text:
        return []
    seen = set()
    result = []
    for match in re.finditer(r"\[([^\]]+)\]\(file:///([^)]+)\)", text):
        name = unquote(match.group(1).split("#")[0])
        path_part = unquote(match.group(2).split("#")[0])
        abs_path = path_part if path_part.startswith("/") else "/" + path_part
        key = (name, abs_path)
        if key not in seen:
            seen.add(key)
            result.append(key)
    if result:
        logger.debug("Extracted %d artifacts: %s", len(result), [n for n, _ in result[:3]])
    return result


def _is_transient_stream_error(stderr_text: str) -> str | None:
    """Check if stderr indicates a transient stream error eligible for retry.

    Returns "cascade" for cascade/response timeouts, "stream" for agent stream
    interruption, or None if not retriable.
    """
    if not stderr_text:
        return None
    cat = _classify_cli_error(stderr_text, backend="agy")
    if cat == "cascade_timeout":
        return "cascade"
    if cat == "agent_stream_interrupted":
        return "stream"
    return None


def ensure_user_gemini(user_id: str) -> str:
    """Ensure per-user .gemini directory with auth token and default persona.

    Creates session/.gemini/antigravity-cli/ for agy auth and conversations.
    Copies global auth token and default GEMINI.md (persona) on first use.
    Returns session_dir path (for use as HOME when running agy).
    """
    session_dir = ensure_session_dir(user_id)
    gemini_dir = os.path.join(session_dir, ".gemini")
    antigravity_dir = os.path.join(gemini_dir, "antigravity-cli")
    os.makedirs(antigravity_dir, exist_ok=True)
    try:
        os.chmod(gemini_dir, 0o700)
        os.chmod(antigravity_dir, 0o700)
    except OSError:
        pass

    # Copy global auth token if not yet present
    # agy standard auth token path, managed by agy CLI
    token_src = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")
    token_dst = os.path.join(antigravity_dir, "antigravity-oauth-token")
    if not os.path.exists(token_dst) and os.path.exists(token_src):
        try:
            shutil.copy(token_src, token_dst)
            os.chmod(token_dst, 0o600)
        except OSError as e:
            logger.warning("Failed to copy auth token for %s: %s", user_id, e)

    # Copy global GEMINI.md persona as default if not yet present
    # agy standard persona file path, managed by agy CLI
    agents_src = os.path.expanduser("~/.gemini/GEMINI.md")
    agents_dst = os.path.join(gemini_dir, "GEMINI.md")
    if not os.path.exists(agents_dst) and os.path.exists(agents_src):
        try:
            shutil.copy(agents_src, agents_dst)
        except OSError as e:
            logger.warning("Failed to copy default GEMINI.md for %s: %s", user_id, e)

    return session_dir


# ---------------------------------------------------------------------------
# agy CLI execution
# ---------------------------------------------------------------------------

async def run_agy(prompt: str, user_id: str, timeout: int = None) -> tuple[str, list]:
    """Execute agy CLI for a given user message.

    - Creates per-user session directory under config.session_base_dir
    - Applies per-user preferences (model, effort, mode, add_dirs) as CLI flags
    - Runs ``agy [flags] -p <prompt>`` for first message,
      ``agy [flags] -c -p <prompt>`` for subsequent messages
    - Extracts artifacts (file:/// links) from stdout and scratch diff
    - Cleans ANSI / HTML tags from display output
    - Kills process group on timeout and returns a friendly message
    - Never adds system prompts or personality instructions

    Returns:
        tuple[str, list]: (cleaned_display_text, list_of_(name, abs_path)_artifacts)
    """
    if timeout is None:
        timeout = config.agy_timeout

    # argv 单参数上限约 128KB（MAX_ARG_STRLEN），超长 prompt 直接拒绝，避免 E2BIG
    if len(prompt.encode("utf-8", errors="replace")) > _MAX_ARG_BYTES:
        logger.warning("Prompt too large for argv from user %s", user_id)
        return format_error(
            "消息过长",
            f"这条消息太长了（超过 {_MAX_ARG_BYTES // 1024}KB），请精简或分段发送。",
        ), []

    t0 = time.time()
    session_dir = ensure_user_gemini(user_id)

    # Audit logging
    logger.info("[AUDIT] user=%s prompt=%.200s", user_id, prompt)
    if is_dangerous(prompt):
        logger.warning(
            "[AUDIT] dangerous keyword in prompt from user=%s", user_id
        )

    first = is_first_message(session_dir, backend="agy")

    # Build command: agy [--model X] [--effort Y] [--mode Z] [--add-dir W ...] [-c] -p <prompt>
    # --dangerously-skip-permissions 保留：可信小圈子用户需 agy 能自动调工具，风险由服务层(root)+输入来源(可信用户)承担
    cmd = [config.agy_binary_path, "--dangerously-skip-permissions"]
    prefs = load_prefs(user_id)
    model = prefs.get("model")
    effort = prefs.get("effort")
    if model:
        base_model, embedded_effort = parse_model_effort(model)
        if embedded_effort and effort:
            # model has effort suffix AND user wants a different effort
            # -> use base model name + --effort from prefs (no conflict)
            cmd += ["--model", base_model, "--effort", effort]
        elif embedded_effort:
            # model has effort suffix, no explicit effort -> model carries it
            cmd += ["--model", model]
        else:
            # plain model name -> pass effort if set
            cmd += ["--model", model]
            if effort:
                cmd += ["--effort", effort]
    elif effort:
        cmd += ["--effort", effort]
    if prefs.get("mode"):
        cmd += ["--mode", prefs["mode"]]
    for d in prefs.get("add_dirs", []):
        if not d:
            continue
        ok, resolved = validate_add_dir(d, user_id)
        if ok:
            cmd += ["--add-dir", resolved]
        else:
            logger.warning("Skipping disallowed add_dir for %s: %s (%s)", user_id, d, resolved)

    if first:
        logger.info(
            "First message for user %s, running: agy -p ...", user_id
        )
    else:
        cmd += ["-c"]
        logger.info(
            "Continuing conversation for user %s, running: agy -c -p ...",
            user_id,
        )

    cmd += ["-p", prompt]

    process = None
    try:
        env = sanitize_env(session_dir)
        env["PAGER"] = "cat"
        env["CI"] = "true"
        env["NONINTERACTIVE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        process = await asyncio.create_subprocess_exec(

            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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

        # A: Extract artifacts from raw stdout (before clean_output!)
        artifacts = extract_artifacts(stdout_text)

        # B: (disabled) Scratch diff via before/after snapshots is removed because
        #   multi-user shared scratch causes cross-user artifact leakage.
        #   agy-generated files always produce file:/// links in stdout, so
        #   extract_artifacts (A) above is sufficient.

        # Clean display text
        display = clean_output(stdout_text) or EMPTY_REPLY

        # Strip file:/// links from display to avoid leaking server paths
        display = re.sub(
            r"\[([^\]]+)\]\(file:///[^)]+\)",
            r"[\1]",
            display,
        )

        if process.returncode != 0:
            logger.warning(
                "agy exited with code %s for user %s: %.200s",
                process.returncode,
                user_id,
                stderr_text,
            )
            if not stdout_text and stderr_text:
                err_kind = _is_transient_stream_error(stderr_text)
                if err_kind:
                    logger.warning(
                        "Transient stream error (%s) detected for user %s, retrying once automatically",
                        err_kind,
                        user_id,
                    )
                    if err_kind == "stream":
                        await asyncio.sleep(3)
                    retry_process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
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
                        # 重试进程也必须回收，否则超时后成为孤儿进程
                        logger.warning(
                            "agy retry timed out after %ss for user %s, terminating retry process",
                            timeout, user_id,
                        )
                        await terminate_process(retry_process, graceful=True)
                        if err_kind == "cascade":
                            return format_error(
                                "模型响应超时",
                                "模型响应超时，自动重试仍超时。请稍后重试或简化指令。",
                            ), []
                        return format_cli_error(stderr_text, backend="agy"), []
                    except (asyncio.CancelledError, Exception):
                        await terminate_process(retry_process, graceful=False)
                        raise
                    r_stdout_text = r_stdout.decode("utf-8", errors="replace").strip()
                    r_stderr_text = r_stderr.decode("utf-8", errors="replace").strip()
                    # Any useful stdout after retry counts as recovered (agy may exit non-zero)
                    if r_stdout_text:
                        r_artifacts = extract_artifacts(r_stdout_text)
                        r_display = clean_output(r_stdout_text) or EMPTY_REPLY
                        r_display = re.sub(r"\[([^\]]+)\]\(file:///[^)]+\)", r"[\1]", r_display)
                        if (
                            first
                            and r_display != EMPTY_REPLY
                            and not is_bridge_formatted_reply(r_display)
                        ):
                            mark_initialized(session_dir, backend="agy")
                        return r_display, r_artifacts
                    if err_kind == "cascade":
                        return format_error(
                            "模型响应超时",
                            "模型响应超时，自动重试仍失败。请稍后重试或简化指令。",
                        ), []
                    return format_cli_error(r_stderr_text or stderr_text, backend="agy"), []
                return format_cli_error(stderr_text, backend="agy"), []
            # Non-zero exit: never treat raw stdout as a normal success reply
            raw = stderr_text or stdout_text or display or "process exited abnormally"
            return format_cli_error(raw, backend="agy"), []

        # Success path only — never mark on ❌/🔔 error/throttle bubbles
        if first and display != EMPTY_REPLY and not is_bridge_formatted_reply(display):
            mark_initialized(session_dir, backend="agy")

        elapsed = time.time() - t0
        logger.info(
            "agy done: user=%s elapsed=%.1fs artifacts=%d output=%d chars",
            user_id, elapsed, len(artifacts), len(display),
        )
        return display, artifacts

    except asyncio.TimeoutError:
        logger.warning(
            "agy execution timed out after %ss for user %s",
            timeout,
            user_id,
        )
        await terminate_process(process, graceful=True)
        return format_error("处理超时", f"超过 {timeout} 秒未完成，已终止本次任务。"), []

    except asyncio.CancelledError:
        # 任务被取消（如重登录前排空）：必须杀掉子进程再传递取消
        await terminate_process(process, graceful=False)
        raise

    except Exception as e:
        logger.exception("Unexpected error running agy: %s", e)
        await terminate_process(process, graceful=False)
        return format_error(
            "执行出错",
            "这次没处理好，请稍后再试。若一直失败，请联系管理员。",
        ), []


# ---------------------------------------------------------------------------
# Slash command support — per-user preference persistence & command dispatch
# ---------------------------------------------------------------------------


async def _run_agy_subcommand(subcmd_args: list, user_id: str) -> str:
    """Run an agy subcommand (e.g., 'models', 'agents') and return cleaned output.

    Timeout is fixed at 30 seconds.
    Uses per-user session isolation matching run_agy.
    """
    session_dir = ensure_user_gemini(user_id)
    cmd = [config.agy_binary_path] + subcmd_args
    process = None
    try:
        env = sanitize_env(session_dir)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
                "agy %s exited with code %s",
                " ".join(subcmd_args),
                process.returncode,
            )
            return format_cli_error(
                stderr_text or stdout_text or "终端指令执行失败",
                backend="agy",
            )

        return clean_output(stdout_text) or EMPTY_REPLY

    except asyncio.TimeoutError:
        # 超时必须回收子进程，否则挂死的查询进程成为孤儿
        await terminate_process(process, graceful=True)
        return format_error("查询超时", "查询超时，请稍后再试。")
    except asyncio.CancelledError:
        await terminate_process(process, graceful=False)
        raise
    except Exception as e:
        logger.exception("Subcommand error: %s", e)
        await terminate_process(process, graceful=False)
        return format_error(
            "执行出错",
            "这次没处理好，请稍后再试。若一直失败，请联系管理员。",
        )


def _cmd_help() -> str:
    """Build /help response listing all supported slash commands."""
    lines = [
        "📋 **wechatbridge 支持指令 (agy)** 📋",
        "",
        "**模型控制**",
        "- `/model <名称>` — 切换模型（用 `/models` 查看可用列表）",
        "- `/models` — 查看可用模型列表",
        "- `/backend <agy|grok|codex>` — 切换助手引擎",
        "",
        "**对话控制**",
        "- `/clear` 或 `/new` — 重置对话（开始新会话）",
        "- `/fast` — 开启**快速模式**（回答更快，思考更少）",
        "- `/planning` — 开启**规划模式**（先想清楚再动手）",
        "",
        "**工具**",
        "- `/add-dir <路径>` — 添加工作目录",
        "- `/agents` — 查看可用助手",
        "",
        "**扩展工具 & 子助手**",
        "- `/mcp` — 扩展工具使用说明",
        "- `/agent <名称> <任务>` — 调用子助手执行任务",
        "",
        "**人格**",
        "- `/persona <内容>` — 设置你专属的人格文档（另有 show / clear / reset）",
        "",
        "**其他**",
        "- `/help` — 显示本帮助",
        "",
        "提示：其他 `/` 指令（如 `/goal`、`/grill-me`、`/schedule` 等）会直接交给助手处理。",
    ]
    return "\n".join(lines)


def handle_persona(args: str, user_id: str) -> str:
    """Handle /persona command: set, show, clear, reset per-user GEMINI.md.

    Subcommands:
      set <content>  — write content as user's persona document
      <content>      — same as set (no subcommand)
      show           — display current persona content
      clear          — delete persona, restore default
      reset          — re-copy global GEMINI.md, overwriting local
    """
    session_dir = get_session_dir(user_id)
    gemini_dir = os.path.join(session_dir, ".gemini")
    gemini_path = os.path.join(gemini_dir, "GEMINI.md")

    parts = args.strip().split(maxsplit=1)
    subcmd = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    # set or implicit content
    if subcmd == "set" and rest:
        os.makedirs(gemini_dir, exist_ok=True)
        try:
            with open(gemini_path, "w", encoding="utf-8") as f:
                f.write(rest)
            return "✅ **人格文档已更新** ✅"
        except OSError as e:
            logger.error("Failed to write persona for %s: %s", user_id, e)
            return "❌ **写入人格文档失败** ❌"
    elif subcmd and subcmd not in ("show", "clear", "reset", "set"):
        # No subcommand → treat whole args as content
        os.makedirs(gemini_dir, exist_ok=True)
        try:
            with open(gemini_path, "w", encoding="utf-8") as f:
                f.write(args.strip())
            return "✅ **人格文档已更新** ✅"
        except OSError as e:
            logger.error("Failed to write persona for %s: %s", user_id, e)
            return "❌ **写入人格文档失败** ❌"

    # show
    if subcmd == "show":
        if not os.path.exists(gemini_path):
            return "（未设置人格文档）"
        try:
            with open(gemini_path, "r", encoding="utf-8") as f:
                val = f.read()
            if len(val) > 1500:
                val = val[:1500] + "\n\n（已截断至前1500字符）"
            return val or "（空文档）"
        except OSError as e:
            logger.error("Failed to read persona for %s: %s", user_id, e)
            return "❌ **读取人格文档失败** ❌"

    # clear
    if subcmd == "clear":
        if os.path.exists(gemini_path):
            try:
                os.remove(gemini_path)
                return "✅ **人格文档已清除** ✅"
            except OSError as e:
                logger.error("Failed to clear persona for %s: %s", user_id, e)
                return "❌ **清除人格文档失败** ❌"
        return "ℹ️ **本就无人格文档** ℹ️"

    # reset
    if subcmd == "reset":
        # agy standard persona file path, managed by agy CLI
        agents_src = os.path.expanduser("~/.gemini/GEMINI.md")
        if not os.path.exists(agents_src):
            return "❌ **全局默认人格文档不存在** ❌"
        os.makedirs(gemini_dir, exist_ok=True)
        try:
            shutil.copy(agents_src, gemini_path)
            return "✅ **人格已重置为全局默认** ✅"
        except OSError as e:
            logger.error("Failed to reset persona for %s: %s", user_id, e)
            return "❌ **重置人格文档失败** ❌"

    # empty args
    return "📋 **/persona 用法** 📋\n\n- `/persona <内容>` 设置\n- `/persona show` 查看\n- `/persona clear` 清除\n- `/persona reset` 重置默认"


def _cmd_clear(user_id: str) -> str:
    """Handle /clear or /new: delete .initialized flag to start fresh."""
    session_dir = get_session_dir(user_id)
    clear_initialized(session_dir, backend="agy")
    return "✅ **对话已重置** ✅"


def _cmd_fast(user_id: str) -> str:
    """Handle /fast: set effort=low (scoped to current backend)."""
    update_active_prefs(user_id, effort="low")
    return "✅ **已开启快速模式** ✅"


def _cmd_planning(user_id: str) -> str:
    """Handle /planning: set mode=plan (scoped to current backend)."""
    update_active_prefs(user_id, mode="plan")
    return "✅ **已开启规划模式** ✅"


def _cmd_add_dir(args: str, user_id: str) -> str:
    """Handle /add-dir <path>: add path to add_dirs list (dedup, validated)."""
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
        dirs.append(resolved)
        prefs["add_dirs"] = dirs
        save_prefs(user_id, prefs)
    return f"✅ **已添加工作目录** ✅\n\n```\n{resolved}\n```"


async def _cmd_model(args: str, user_id: str) -> str:
    """Handle /model <name>: validate against agy models list, then save.

    Matching order:
      1. Exact match against a model name
      2. Prefix match (name is a prefix of one or more model names → first hit)
    """
    name = args.strip()
    if not name:
        return "❌ **缺少参数** ❌\n\n`/model <名称>`"

    output = await _run_agy_subcommand(["models"], user_id)
    # 认 ❌/🔔 格式化错误气泡与限流通知，勿把中文错误当模型列表 parse
    if output.startswith("[error]") or is_bridge_formatted_reply(output):
        return "❌ **无法获取模型列表** ❌"

    models = [line.strip() for line in output.split("\n") if line.strip()]

    # Exact match
    if name in models:
        # If model name carries an effort suffix, clear stored effort so
        # run_agy doesn't pass a conflicting --effort flag
        _, embedded = parse_model_effort(name)
        if embedded:
            update_active_prefs(user_id, model=name, effort="")
        else:
            update_active_prefs(user_id, model=name)
        return f"✅ **模型已切换** ✅\n\n`{name}`"

    # Prefix match
    prefix_matches = [m for m in models if m.startswith(name)]
    if prefix_matches:
        matched = prefix_matches[0]
        _, embedded = parse_model_effort(matched)
        if embedded:
            update_active_prefs(user_id, model=matched, effort="")
        else:
            update_active_prefs(user_id, model=matched)
        return f"✅ **模型已切换** ✅\n\n`{matched}`"

    return f"❌ **模型不存在** ❌\n\n`{name}`"


async def handle_slash_command(text: str, user_id: str) -> str | None:
    """Handle /-slash commands from WeChat messages.

    Parses the first whitespace-separated token as the command (lowercased),
    and the remainder as arguments.

    Classification:
      A — implemented in wechatbridge (model, clear, fast, planning, add-dir, etc.)
      B — dangerous (exit, quit, logout) → rejected with error message
      C — TUI panels (config, settings, context, ...) → inform not supported on WeChat
      D — passthrough to agy → returns None, caller runs run_agy() normally

    Returns:
        str: reply message for A/B/C classes
        None: for D class — the caller should pass the original text to run_agy()
    """
    # Parse: first whitespace token = cmd, rest = args
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else text.lower()
    args = parts[1] if len(parts) > 1 else ""

    # --- B class: dangerous / rejected ---
    B_CMDS = frozenset({"/exit", "/quit", "/logout"})
    if cmd in B_CMDS:
        return (
            "⛔ **该指令在微信端禁用** ⛔"
        )

    # --- C class: TUI panels (not supported on WeChat) ---
    C_CMDS = frozenset({
        "/config", "/settings", "/context", "/diff", "/artifact", "/tasks",
        "/hooks", "/keybindings", "/permissions", "/statusline",
        "/copy", "/open", "/rename", "/fork", "/branch", "/rewind", "/undo",
        "/resume", "/switch", "/conversation", "/title", "/feedback",
        "/usage", "/quota", "/credits", "/skills",
    })
    if cmd in C_CMDS:
        return (
            f"⚠️ **微信端不支持** ⚠️\n\n`{cmd}`"
        )

    # --- A class: implemented commands ---
    if cmd == "/help":
        return _cmd_help()

    if cmd in ("/clear", "/new"):
        return _cmd_clear(user_id)

    if cmd == "/fast":
        return _cmd_fast(user_id)

    if cmd == "/planning":
        return _cmd_planning(user_id)

    if cmd == "/model":
        return await _cmd_model(args, user_id)

    if cmd == "/add-dir":
        return _cmd_add_dir(args, user_id)

    if cmd == "/agents":
        output = await _run_agy_subcommand(["agents"], user_id)
        # Match CLI empty output; rewrite user-facing wording only
        if not output or output.strip() in ("Available agents:", "Available agents"):
            output = "**可用助手**\n\n（当前没有自定义助手。）"
        return output

    if cmd == "/models":
        return await _run_agy_subcommand(["models"], user_id)

    if cmd == "/persona":
        return handle_persona(args, user_id)

    # --- MCP & Subagent ---
    if cmd == "/mcp":
        if not config.enable_mcp:
            return "ℹ️ **该功能已禁用** ℹ️"
        return (
            "ℹ️ **扩展工具说明** ℹ️\n\n"
            "可以直接用自然语言让助手调用已配置的扩展工具（如代码检索等）。\n\n"
            "示例：\n"
            "> 用 codegraph 的 search 搜一下 ctxmode\n"
            "> 帮我查一下这个项目里 xxx 怎么实现的"
        )

    # /agent 已上移到 main.py 统一处理（必须经过危险确认门，不能再绕过）

    # --- D class: passthrough to agy (return None so caller runs run_agy) ---
    return None
