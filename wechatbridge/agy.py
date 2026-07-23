"""
agy CLI runner with per-user session isolation, output cleanup, and timeout protection.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import time

from .config import config

logger = logging.getLogger("agy_runner")

# ANSI escape code pattern
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# HTML tag pattern
HTML_TAG_RE = re.compile(r"<[^>]+>")


def extract_artifacts(text: str) -> list[tuple[str, str]]:
    """Extract (name, absolute_path) tuples from markdown file:/// links.

    Uses regex ``\\[([^\\]]+)\\](file:///([^)]+))`` to find agy-generated
    artifact references in stdout. Returns deduplicated, order-preserved list.
    """
    if not text:
        return []
    seen = set()
    result = []
    for match in re.finditer(r"\[([^\]]+)\]\(file:///([^)]+)\)", text):
        name = match.group(1).split("#")[0]
        abs_path = "/" + match.group(2).split("#")[0]
        key = (name, abs_path)
        if key not in seen:
            seen.add(key)
            result.append(key)
    if result:
        logger.debug("Extracted %d artifacts: %s", len(result), [n for n, _ in result[:3]])
    return result


def sanitize_user_id(user_id: str) -> str:
    """Convert a WeChat user ID to a filesystem-safe directory name.

    Uses a short hash suffix for uniqueness while keeping a readable prefix.
    """
    h = hashlib.sha256(user_id.encode()).hexdigest()[:12]
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", user_id)[:48]
    return f"{safe}_{h}"


def get_session_dir(user_id: str) -> str:
    """Get the per-user session directory path."""
    return os.path.join(config.session_base_dir, sanitize_user_id(user_id))


def is_first_message(session_dir: str) -> bool:
    """Check if this user has no existing conversation."""
    return not os.path.exists(os.path.join(session_dir, ".initialized"))


def mark_initialized(session_dir: str) -> None:
    """Create .initialized flag file after first message."""
    try:
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, ".initialized"), "w") as f:
            f.write("1")
    except OSError as e:
        logger.error("Failed to mark session initialized: %s", e)


def clean_output(text: str) -> str:
    """Remove ANSI escape codes and HTML tags from agy output."""
    text = ANSI_RE.sub("", text)
    text = HTML_TAG_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Per-user preference persistence
# ---------------------------------------------------------------------------

def load_prefs(user_id: str) -> dict:
    """Load per-user preferences from prefs.json.

    Returns a dict with keys: model, effort, mode, add_dirs.
    Missing keys are filled from defaults.
    """
    session_dir = get_session_dir(user_id)
    prefs_path = os.path.join(session_dir, "prefs.json")
    default_prefs = {"model": "", "effort": "", "mode": "", "add_dirs": []}
    try:
        if os.path.exists(prefs_path):
            with open(prefs_path, "r") as f:
                data = json.load(f)
            for k in default_prefs:
                if k not in data:
                    data[k] = default_prefs[k]
            return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load prefs for %s: %s", user_id, e)
    return dict(default_prefs)


def save_prefs(user_id: str, prefs: dict) -> None:
    """Save per-user preferences to prefs.json."""
    session_dir = get_session_dir(user_id)
    os.makedirs(session_dir, exist_ok=True)
    prefs_path = os.path.join(session_dir, "prefs.json")
    try:
        with open(prefs_path, "w") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("Failed to save prefs for %s: %s", user_id, e)


def ensure_user_gemini(user_id: str) -> str:
    """Ensure per-user .gemini directory with auth token and default persona.

    Creates session/.gemini/antigravity-cli/ for agy auth and conversations.
    Copies global auth token and default GEMINI.md (persona) on first use.
    Returns session_dir path (for use as HOME when running agy).
    """
    session_dir = get_session_dir(user_id)
    gemini_dir = os.path.join(session_dir, ".gemini")
    antigravity_dir = os.path.join(gemini_dir, "antigravity-cli")
    os.makedirs(antigravity_dir, exist_ok=True)

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


# Confirm gate: hardcoded dangerous keyword fallbacks (used when config.confirm_keywords is empty)
# Confirm gate: hardcoded dangerous keyword fallbacks (used when config.confirm_keywords is empty)
# 宁枉勿纵 — 自然语言危险意图词也触发确认，日常误触多一轮确认即可取消
_DANGEROUS_KEYWORDS = [
    "rm -rf /", "curl |sh", "curl | bash", "wget -o- | sh",
    "删掉", "删除", "清空", "卸载", "格式化",
]


def is_dangerous(prompt: str) -> bool:
    """Check if a prompt contains dangerous keywords.

    Uses config.confirm_keywords if non-empty, otherwise falls back to
    the hardcoded _DANGEROUS_KEYWORDS list (matching existing audit behavior).
    """
    keywords = config.confirm_keywords if config.confirm_keywords else _DANGEROUS_KEYWORDS
    lower = prompt.lower()
    for kw in keywords:
        if kw in lower:
            return True
    return False


# ---------------------------------------------------------------------------
# agy CLI execution
# ---------------------------------------------------------------------------

def parse_model_effort(model: str) -> tuple[str, str | None]:
    """Split 'gemini-3.6-flash-high' -> ('gemini-3.6-flash', 'high').

    Returns (base_model, embedded_effort) where embedded_effort is None if
    the model name does not end with -high, -medium, or -low.
    """
    for suffix in ("-high", "-medium", "-low"):
        if model.endswith(suffix):
            base = model[: -len(suffix)]
            effort = suffix[1:]  # strip leading dash
            return base, effort
    return model, None


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

    t0 = time.time()
    session_dir = ensure_user_gemini(user_id)

    # Audit logging
    logger.info("[AUDIT] user=%s prompt=%.200s", user_id, prompt)
    if is_dangerous(prompt):
        logger.warning(
            "[AUDIT] dangerous keyword in prompt from user=%s", user_id
        )

    first = is_first_message(session_dir)

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
        if d:
            cmd += ["--add-dir", d]

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
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("TOKEN","KEY","SECRET","PASSWORD","AWS","GITHUB","GITLAB","CREDENTIAL"))}
        env["HOME"] = session_dir
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
        display = clean_output(stdout_text) or "(empty response)"

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
                return clean_output(stderr_text), []

        if first:
            mark_initialized(session_dir)

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
        if process and process.pid:
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGKILL)
                logger.info("Killed process group %s", pgid)
            except (ProcessLookupError, PermissionError, OSError) as e:
                logger.warning("Failed to kill process group: %s", e)
            try:
                await process.wait()
            except Exception as e:
                logger.warning("子进程清理失败: %s", e)
        return "⏰ **处理超时** ⏰", []

    except Exception as e:
        logger.exception("Unexpected error running agy: %s", e)
        if process and process.pid:
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception as e:
                logger.warning("子进程清理失败: %s", e)
            try:
                await process.wait()
            except Exception as e:
                logger.warning("子进程清理失败: %s", e)
        return f"❌ **执行出错** ❌\n\n```\n{str(e)}\n```", []


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
    try:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("TOKEN","KEY","SECRET","PASSWORD","AWS","GITHUB","GITLAB","CREDENTIAL"))}
        env["HOME"] = session_dir
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
            return clean_output(stderr_text) if stderr_text else (
                f"❌ **终端指令执行失败** ❌"
            )

        return clean_output(stdout_text) or "(empty response)"

    except asyncio.TimeoutError:
        return "❌ **指令超时** ❌"
    except Exception as e:
        logger.exception("Subcommand error: %s", e)
        return f"❌ **执行出错** ❌\n\n```\n{str(e)}\n```"


def _cmd_help() -> str:
    """Build /help response listing all supported slash commands."""
    lines = [
        "📋 **wechatbridge 支持指令** 📋",
        "",
        "**模型控制**",
        "- `/model <名称>` — 切换模型（用 `/models` 查看可用列表）",
        "- `/models` — 查看可用模型列表",
        "",
        "**对话控制**",
        "- `/clear` 或 `/new` — 重置对话（开始新会话）",
        "- `/fast` — 开启**快速模式**（低推理开销）",
        "- `/planning` — 开启 **planning 模式**",
        "",
        "**工具**",
        "- `/add-dir <路径>` — 添加工作目录",
        "- `/agents` — 查看可用 agent",
        "",
        "**MCP & 子代理**",
        "- `/mcp` — MCP 工具使用引导",
        "- `/agent <名称> <任务>` — 调用子代理执行任务",
        "",
        "**人格**",
        "- `/persona <内容>` — 设置你专属的人格文档（支持 show / clear / reset 子命令）",
        "",
        "**其他**",
        "- `/help` — 显示本帮助",
        "",
        "提示：其他 `/` 指令（如 `/goal`、`/grill-me`、`/schedule` 等）会直接交给 agy 处理。",
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
    flag_path = os.path.join(session_dir, ".initialized")
    try:
        if os.path.exists(flag_path):
            os.remove(flag_path)
        return "✅ **对话已重置** ✅"
    except OSError as e:
        logger.error("Failed to clear session for %s: %s", user_id, e)
        return "❌ **重置失败** ❌"


def _cmd_fast(user_id: str) -> str:
    """Handle /fast: set effort=low."""
    prefs = load_prefs(user_id)
    prefs["effort"] = "low"
    save_prefs(user_id, prefs)
    return "✅ **已开启 fast 模式** ✅"


def _cmd_planning(user_id: str) -> str:
    """Handle /planning: set mode=plan."""
    prefs = load_prefs(user_id)
    prefs["mode"] = "plan"
    save_prefs(user_id, prefs)
    return "✅ **已开启 planning 模式** ✅"


def _cmd_add_dir(args: str, user_id: str) -> str:
    """Handle /add-dir <path>: add path to add_dirs list (dedup)."""
    path = args.strip()
    if not path:
        return "❌ **缺少参数** ❌\n\n`/add-dir <路径>`"
    prefs = load_prefs(user_id)
    dirs = prefs.get("add_dirs", [])
    if path not in dirs:
        dirs.append(path)
        prefs["add_dirs"] = dirs
        save_prefs(user_id, prefs)
    return f"✅ **已添加工作目录** ✅\n\n```\n{path}\n```"


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
    if output.startswith("[error]") or output.startswith("❌"):
        return "❌ **无法获取模型列表** ❌"

    models = [line.strip() for line in output.split("\n") if line.strip()]

    # Exact match
    if name in models:
        prefs = load_prefs(user_id)
        prefs["model"] = name
        # If model name carries an effort suffix, clear any stored effort
        # so run_agy doesn't pass conflicting --effort flag
        _, embedded = parse_model_effort(name)
        if embedded:
            prefs.pop("effort", None)
        save_prefs(user_id, prefs)
        return f"✅ **模型已切换** ✅\n\n`{name}`"

    # Prefix match
    prefix_matches = [m for m in models if m.startswith(name)]
    if prefix_matches:
        matched = prefix_matches[0]
        prefs = load_prefs(user_id)
        prefs["model"] = matched
        # If model name carries an effort suffix, clear any stored effort
        _, embedded = parse_model_effort(matched)
        if embedded:
            prefs.pop("effort", None)
        save_prefs(user_id, prefs)
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
        if not output or output == "Available agents:":
            output = "**Available agents**\n\n（当前没有自定义 agent。）"
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
            "ℹ️ **MCP 工具使用引导** ℹ️\n\n"
            "agy 已配置 MCP server（ctxmode / codegraph）。\n\n"
            "使用方法：用自然语言描述调用，格式为：\n"
            "> 用 call_mcp_tool 调用 `<工具名>`，参数 `<json>`\n\n"
            "示例：\n"
            "> 用 codegraph 的 search 工具搜 ctxmode"
        )

    if cmd == "/agent":
        if not config.enable_subagent:
            return "ℹ️ **该功能已禁用** ℹ️"
        if not args:
            return "❌ **缺少参数** ❌\n\n`/agent <名称> <任务>`"
        # Construct prompt and run through agy
        agent_parts = args.split(maxsplit=1)
        agent_name = agent_parts[0]
        agent_task = agent_parts[1] if len(agent_parts) > 1 else ""
        crafted = f"请用 invoke_subagent 调用 agent {agent_name} 执行任务：{agent_task}"
        logger.info("Agent subcmd: user=%s agent=%s task=%.100s", user_id, agent_name, agent_task)
        result_text, _ = await run_agy(crafted, user_id)
        return result_text

    # --- D class: passthrough to agy (return None so caller runs run_agy) ---
    return None
