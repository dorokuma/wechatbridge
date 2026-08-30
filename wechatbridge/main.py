"""
wechatbridge Main Entry Point.
Active iLink client that receives WeChat messages and responds via CLI backends.
Architecture: WeChat ClawBot(iLink) <-> wechatbridge(Python) <-> agy/grok/codex CLIs
"""

import argparse
import asyncio
import base64
import contextvars
import logging
import os
import sys
import time
import uuid
from collections import OrderedDict
from io import StringIO

from . import __version__
from .config import config, ensure_runtime_dirs
from .ilink import ILinkClient
from .runner_common import (
    KNOWN_BACKENDS,
    clean_session_media,
    clear_initialized,
    classify_upstream_failure,
    format_artifact_send_failure_notice,
    format_error,
    format_model_label,
    format_oversized_artifact_notice,
    format_upstream_cooldown_notice,
    format_upstream_retry_notice,
    get_session_dir,
    is_dangerous,
    is_upstream_throttle_reply,
    load_prefs,
    path_is_under,
    save_prefs,
    switch_backend_prefs,
    upstream_guard,
    validate_add_dir,
)
from .update_check import update_check_loop, maybe_notify_admin, format_update_hint

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wechatbridge")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Pending dangerous prompt confirmations (user_id -> {prompt, expire_at, context_token})
pending_confirms: dict = {}


# Slash 处理器内部信号：该指令已完整处理（如 /agent 进入确认流程），调用方直接 return
_HANDLED = object()

# 重登录/关闭前等待在途消息任务的最长时间
_DRAIN_TIMEOUT_S = 90.0

# 后台任务强引用集合（事件循环只持弱引用，防止任务被 GC 提前回收）
_background_tasks: set = set()


def _spawn_bg(coro) -> asyncio.Task:
    """create_task + 强引用跟踪，任务结束自动移除。"""
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t


# 已见消息 ID（LRU，上限 1000）——服务端重投/重启重放时跳过重复处理
_seen_msg_ids: "OrderedDict[str, None]" = OrderedDict()
_SEEN_MSG_IDS_CAP = 1000


# 去重键字段优先级，对齐官方 WeixinMessage schema（Tencent/openclaw-weixin types.ts）：
#   message_id(数值) > client_id > item_list[0].msg_id > seq
def _msg_dedup_key(msg: dict) -> str | None:
    """从入站消息提取去重键；服务端不下发任何 id 字段时返回 None（无法安全去重）。"""
    for field in ("message_id", "client_id"):
        v = msg.get(field)
        if v:
            return f"{field}:{v}"
    items = msg.get("item_list")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        v = items[0].get("msg_id")
        if v:
            return f"item_msg_id:{v}"
    v = msg.get("seq")
    if v:
        return f"seq:{v}"
    return None


_dedup_field_announced = False


def _is_duplicate_msg(msg: dict) -> bool:
    global _dedup_field_announced
    key = _msg_dedup_key(msg)
    if not _dedup_field_announced:
        _dedup_field_announced = True
        if key:
            logger.info("消息去重已启用，使用字段: %s", key.split(":", 1)[0])
        else:
            logger.info("服务端未下发消息 id 字段，去重停用（依赖服务端游标）")
    if key is None:
        return False
    if key in _seen_msg_ids:
        return True
    _seen_msg_ids[key] = None
    _seen_msg_ids.move_to_end(key)
    while len(_seen_msg_ids) > _SEEN_MSG_IDS_CAP:
        _seen_msg_ids.popitem(last=False)
    return False


# ---------------------------------------------------------------------------
# Backend dispatcher — routes to agy, grok, or codex based on per-user preference
# ---------------------------------------------------------------------------

def _get_backend(user_id: str) -> str:
    """Get the active backend for a user (from prefs, fallback to global config)."""
    prefs = load_prefs(user_id)
    return prefs.get("backend", config.backend)


async def _run_llm(prompt: str, user_id: str) -> tuple[str, list]:
    """Dispatch prompt to the active backend's run function."""
    backend = _get_backend(user_id)
    try:
        if backend == "grok":
            from .grok import run_grok
            return await run_grok(prompt, user_id)
        elif backend == "codex":
            from .codex import run_codex
            return await run_codex(prompt, user_id)
        elif backend == "dsh":
            from .dsh import run_dsh
            return await run_dsh(prompt, user_id)
        else:
            from .agy import run_agy
            return await run_agy(prompt, user_id)
    except (ImportError, ModuleNotFoundError) as e:
        return format_error("缺少依赖", str(e)), []


# ---------------------------------------------------------------------------
# Global concurrency slot helpers
#
# C (user gap) / B (global cooldown) / A (retry backoff) must not hold the
# global semaphore while sleeping — otherwise multi-user throttle piles up
# every concurrent slot on asyncio.sleep. Slot is bound via ContextVar from
# _safe_process_message; when absent (unit tests), sleep is plain.
#
# After releasing the slot for sleep we re-acquire with a short timeout and
# limited retries. Under full slots with long tasks the user may wait a bit
# longer overall, but that is better than sleeping while holding the slot;
# final timeout surfaces the same friendly busy reply as initial fail-fast.
# ---------------------------------------------------------------------------

# Same copy as the initial fail-fast busy reply (concurrency full).
_BUSY_USER_TEXT = (
    "⏳ **现在有点忙** ⏳\n\n"
    "同时处理的消息太多，请过几秒再发。"
)


class SlotReacquireTimeout(Exception):
    """Global concurrency slot could not be re-acquired after a released sleep."""


class _GlobalSlot:
    """Tracks whether the current task holds the process-wide concurrency slot."""

    __slots__ = ("sem", "held")

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self.sem = sem
        self.held = True

    async def _reacquire(self) -> None:
        """Re-acquire the global slot with short timeout + limited retries.

        Leaves ``held=False`` and raises ``SlotReacquireTimeout`` on final
        failure so the outer ``_safe_process_message`` finally does not
        double-release a slot we no longer own.
        """
        # Defaults match fail-fast spirit; patchable via config for tests/ops.
        timeout = float(getattr(config, "slot_reacquire_timeout", 0.5) or 0.5)
        attempts = max(1, int(getattr(config, "slot_reacquire_attempts", 3) or 3))
        last_exc: BaseException | None = None
        for i in range(attempts):
            try:
                await asyncio.wait_for(self.sem.acquire(), timeout=timeout)
                self.held = True
                return
            except asyncio.TimeoutError as e:
                last_exc = e
                logger.warning(
                    "global slot re-acquire timeout attempt=%d/%d timeout=%.2fs",
                    i + 1, attempts, timeout,
                )
        # held stays False — we released before sleep and never got it back
        raise SlotReacquireTimeout(
            f"failed to re-acquire global slot after {attempts} attempt(s)"
        ) from last_exc

    async def sleep_released(self, seconds: float) -> None:
        """Sleep without occupying the global slot; re-acquire afterwards.

        Re-acquire is time-bounded (see ``_reacquire``). On timeout, raises
        ``SlotReacquireTimeout`` with ``held=False`` so callers can return a
        friendly busy reply and the outer finally will not double-release.
        """
        if seconds <= 0:
            return
        if self.held:
            self.sem.release()
            self.held = False
            try:
                await asyncio.sleep(seconds)
            finally:
                # Re-acquire even if cancelled so finally of _safe_process_message
                # does not double-release a slot we no longer own. Timeout → raise
                # with held=False (no leak, no double release).
                await self._reacquire()
        else:
            await asyncio.sleep(seconds)


_global_slot_ctx: contextvars.ContextVar = contextvars.ContextVar(
    "wechatbridge_global_slot", default=None
)


async def _guard_sleep(seconds: float) -> None:
    """Sleep; if this task holds the global slot, release it for the wait."""
    if seconds <= 0:
        return
    slot = _global_slot_ctx.get()
    if slot is not None:
        await slot.sleep_released(seconds)
    else:
        await asyncio.sleep(seconds)


async def _await_upstream_preflight(client, from_user: str, context_token: str) -> None:
    """C + B waits *before* acquiring the global concurrency slot.

    Called from _safe_process_message under the per-user lock so same-user
    stays serial, but other users can use free slots while we cool down.
    B still needs *client* to send the 🔔 cooldown notice.
    """
    # C: same-user min interval after throttle — silent, no WeChat notice
    gap = upstream_guard.user_gap_remaining(from_user)
    if gap > 0:
        logger.info(
            "upstream_user_gap user=%s wait=%.1fs (pre-slot)",
            from_user, gap,
        )
        await asyncio.sleep(gap)

    # B: process-wide cooldown — notify, then wait out remaining time
    cool = upstream_guard.global_remaining()
    if cool > 0:
        secs = max(1, int(round(cool)))
        logger.info(
            "upstream_global_cooldown user=%s wait=%.1fs (pre-slot)",
            from_user, cool,
        )
        if context_token and from_user:
            try:
                await client.send_message(
                    to_user_id=from_user,
                    text=format_upstream_cooldown_notice(secs),
                    context_token=context_token,
                    baseurl=client.state.baseurl,
                    bot_token=client.state.bot_token,
                )
            except Exception as e:
                logger.warning("发送上游冷却提示失败: %s", e)
        await asyncio.sleep(cool)


async def _run_llm_with_guard(
    client,
    from_user: str,
    context_token: str,
    prompt: str,
) -> tuple[str, list]:
    """Run LLM with process-wide upstream throttle / jitter protection.

    Covers all three backends (agy / grok / codex) because they share
    ``_run_llm`` dispatch. Behaviour (user-facing):

    - **C** per-user gap after a prior throttle: silent sleep (prefer pre-slot)
    - **B** global cooldown: 🔔 notice then sleep (prefer pre-slot)
    - **A** on short-window throttle: 🔔 retry notice + backoff, up to retry_max
    - **额度相关**: mark cooldown/gap but default 0 extra retries (no CLI spam)
    - Final throttle text is returned as-is (already 🔔 formatted)

    Sleeps use ``_guard_sleep`` so they do not occupy the global concurrency
    slot when one is held. Pure local slash commands must not call this helper.
    """
    # C/B safety net (normally already drained by _await_upstream_preflight
    # before the global slot was acquired; remaining ≈ 0 in the common path).
    # Use _guard_sleep so a rare race that re-extends cooldown does not hold
    # a concurrent slot during the wait.
    try:
        gap = upstream_guard.user_gap_remaining(from_user)
        if gap > 0:
            logger.info(
                "upstream_user_gap user=%s wait=%.1fs",
                from_user, gap,
            )
            await _guard_sleep(gap)

        cool = upstream_guard.global_remaining()
        if cool > 0:
            secs = max(1, int(round(cool)))
            logger.info(
                "upstream_global_cooldown user=%s wait=%.1fs",
                from_user, cool,
            )
            # Align with preflight B: notice failure must not abort the wait/retry path
            if context_token and from_user:
                try:
                    await client.send_message(
                        to_user_id=from_user,
                        text=format_upstream_cooldown_notice(secs),
                        context_token=context_token,
                        baseurl=client.state.baseurl,
                        bot_token=client.state.bot_token,
                    )
                except Exception as e:
                    logger.warning("发送上游冷却提示失败: %s", e)
            await _guard_sleep(cool)

        retry_max = max(0, int(getattr(config, "upstream_retry_max", 2) or 0))
        quota_retry_max = max(0, int(getattr(config, "upstream_quota_retry_max", 0) or 0))
        backoff = list(getattr(config, "upstream_backoff", None) or [2, 5, 12])
        if not backoff:
            backoff = [2, 5, 12]
        # Loop bound uses the larger budget; per-kind check decides early exit.
        total_attempts = max(retry_max, quota_retry_max) + 1
        reply: str = ""
        artifacts: list = []

        for attempt in range(total_attempts):
            reply, artifacts = await _run_llm(prompt, from_user)
            kind = classify_upstream_failure(reply)
            if kind is None:
                upstream_guard.clear_user_gap(from_user)
                return reply, artifacts

            backend = _get_backend(from_user)
            upstream_guard.mark_throttle(from_user)
            kind_retry_max = quota_retry_max if kind == "quota" else retry_max
            logger.warning(
                "upstream_throttle user=%s attempt=%d/%d backend=%s kind=%s",
                from_user, attempt + 1, kind_retry_max + 1, backend, kind,
            )

            if attempt >= kind_retry_max:
                # No more retries for this kind — surface the formatted reply
                return reply, artifacts

            # A: tell user we're retrying, then backoff (slot released while sleeping)
            retry_n = attempt + 1  # 1-based among the extra retries
            wait = float(backoff[min(attempt, len(backoff) - 1)])
            if context_token and from_user:
                try:
                    await client.send_message(
                        to_user_id=from_user,
                        text=format_upstream_retry_notice(retry_n, kind_retry_max),
                        context_token=context_token,
                        baseurl=client.state.baseurl,
                        bot_token=client.state.bot_token,
                    )
                except Exception as e:
                    logger.warning("发送上游重试提示失败: %s", e)
            logger.info(
                "upstream_retry_backoff user=%s attempt=%d/%d sleep=%.1fs backend=%s kind=%s",
                from_user, attempt + 1, kind_retry_max + 1, wait, backend, kind,
            )
            if wait > 0:
                await _guard_sleep(wait)

        return reply, artifacts
    except SlotReacquireTimeout:
        # Released slot during C/B/A sleep could not be reclaimed — same user-
        # facing semantics as concurrency-full fail-fast (do not silent-drop).
        logger.warning(
            "global slot re-acquire failed during guard user=%s; returning busy reply",
            from_user,
        )
        return _BUSY_USER_TEXT, []


async def _handle_slash(client: ILinkClient, text: str, user_id: str, context_token: str):
    """Dispatch slash command to the active backend's handler.

    /backend 和 /agent 是元指令，在这里处理，不下发给后端。

    返回值约定：
      str      — 直接作为回复文本
      tuple    — (reply, artifacts)，经确认门后的执行结果
      None     — D 类透传，调用方应把原文交给 gate_and_run
      _HANDLED — 已完整处理（如 /agent 进入确认流程），调用方直接 return
    """
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    # /backend is a meta-command — switch CLI backend per user
    if cmd == "/backend":
        return _cmd_backend(args, user_id)

    # /agent 也在这里处理：统一走 gate_and_run 确认门，不能绕开 is_dangerous
    if cmd == "/agent":
        return await _cmd_agent(client, args, user_id, context_token)

    if cmd == "/version":
        return (
            f"📦 **版本信息** 📦\n\n当前版本: `{__version__}`\n"
            f"实例: `{config.instance}`  引擎: `{_get_backend(user_id)}`"
        ) + format_update_hint()

    backend = _get_backend(user_id)
    try:
        if backend == "grok":
            from .grok import handle_grok_slash_command
            return await handle_grok_slash_command(text, user_id)
        elif backend == "codex":
            from .codex import handle_codex_slash_command
            return await handle_codex_slash_command(text, user_id)
        elif backend == "dsh":
            from .dsh import handle_dsh_slash_command
            return await handle_dsh_slash_command(text, user_id)
        else:
            from .agy import handle_slash_command
            return await handle_slash_command(text, user_id)
    except (ImportError, ModuleNotFoundError) as e:
        return format_error("缺少依赖", str(e))


def _cmd_backend(args: str, user_id: str) -> str:
    """Handle /backend <agy|grok|codex> — switch CLI backend per user.

    Switching restores that backend's remembered model/effort/mode (or empty
    project default on first visit), and resets the conversation so the new
    backend starts a fresh session.
    """
    name = args.strip().lower()
    if not name:
        prefs = load_prefs(user_id)
        current = prefs.get("backend", config.backend)
        model_label = format_model_label(prefs.get("model", ""))
        backend_list = " / ".join(f"`{b}`" for b in KNOWN_BACKENDS)
        return (
            f"📋 **当前助手引擎** 📋\n\n`{current}`\n"
            f"模型: `{model_label}`\n\n"
            f"用法: `/backend {backend_list}`"
        )
    if name not in KNOWN_BACKENDS:
        backend_list = " / ".join(f"`{b}`" for b in KNOWN_BACKENDS)
        return (
            f"❌ **未知引擎** ❌\n\n支持: {backend_list}\n\n"
            f"`/backend {'` 或 `/backend '.join(KNOWN_BACKENDS)}`"
        )
    # 切换前先探测依赖（如 dsh 依赖 PyYAML），依赖缺失时保持原 prefs
    if name == "dsh":
        try:
            from .dsh import clear_memory, clear_session_id
        except (ImportError, ModuleNotFoundError) as e:
            return format_error("缺少依赖", str(e))
    elif name == "codex":
        try:
            from .codex import _delete_codex_thread_id
        except (ImportError, ModuleNotFoundError) as e:
            return format_error("缺少依赖", str(e))

    prefs = load_prefs(user_id)
    old, new = switch_backend_prefs(prefs, name)
    save_prefs(user_id, prefs)
    model_label = format_model_label(prefs.get("model", ""))
    # Reset session so new backend starts fresh (only when actually changed)
    if old != new:
        session_dir = get_session_dir(user_id)
        clear_initialized(session_dir, backend=new)
        # codex 的续聊依赖 .codex_thread_id，切换时一并清掉避免指向旧会话
        if new == "codex":
            _delete_codex_thread_id(session_dir)
        elif new == "dsh":
            clear_memory(user_id)
            clear_session_id(user_id)
        return (
            f"✅ **助手引擎已切换** ✅\n\n"
            f"`{old}` → `{new}`\n"
            f"模型: `{model_label}`\n\n"
            "⚠️ 对话已重置，新引擎将开始新会话。"
        )
    return (
        f"📋 **当前助手引擎** 📋\n\n`{name}`（未变化）\n"
        f"模型: `{model_label}`"
    )


async def _cmd_agent(client: ILinkClient, args: str, user_id: str, context_token: str):
    """Handle /agent <名称> <任务> — 调用子助手执行任务。

    必须经过 gate_and_run 的危险确认门（历史上后端各自实现时绕过了该检查）。
    """
    if not config.enable_subagent:
        return "ℹ️ **该功能已禁用** ℹ️"
    if not args.strip():
        return "❌ **缺少参数** ❌\n\n`/agent <名称> <任务>`"
    agent_parts = args.split(maxsplit=1)
    agent_name = agent_parts[0]
    agent_task = agent_parts[1] if len(agent_parts) > 1 else ""
    # Execution path still uses the invoke_subagent wording for the backend;
    # confirmation UI shows the user's original agent/task only.
    crafted = f"请用 invoke_subagent 调用 agent {agent_name} 执行任务：{agent_task}"
    display = (
        f"调用助手「{agent_name}」执行：{agent_task}"
        if agent_task
        else f"调用助手「{agent_name}」"
    )
    logger.info("Agent subcmd: user=%s agent=%s task=%.100s", user_id, agent_name, agent_task)
    result = await gate_and_run(
        client, user_id, context_token, crafted, display_prompt=display,
    )
    if result is None:
        return _HANDLED  # 已进入危险确认流程
    return result  # (reply, artifacts)


# ---------------------------------------------------------------------------
# Image file extension detection
# ---------------------------------------------------------------------------

def _detect_image_ext(data: bytes) -> str:
    """Detect image file extension from magic bytes."""
    if data[:4] == b"\x89PNG":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpg"
    if data[:4] in (b"GIF8",):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "bin"


# ---------------------------------------------------------------------------
# QR code login flow
# ---------------------------------------------------------------------------
async def login_flow(client: ILinkClient) -> bool:
    """Perform QR code login flow.  Returns True on success."""
    qrcode_str, qrcode_url = await client.get_qrcode()

    # Save QR code PNG from URL
    if qrcode_url:
        try:
            import qrcode as qrcode_lib_png

            qr = qrcode_lib_png.QRCode(border=2)
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            im = qr.make_image()
            parent = os.path.dirname(os.path.abspath(config.qrcode_png_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            im.save(config.qrcode_png_path)
            try:
                os.chmod(config.qrcode_png_path, 0o600)
            except OSError:
                pass
            logger.info(
                "二维码图片已保存到 %s", config.qrcode_png_path
            )
        except Exception as e:
            logger.warning("保存二维码 PNG 失败: %s", e)

        # Write URL to file for external access (restrict permissions)
        try:
            parent = os.path.dirname(os.path.abspath(config.qrcode_url_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(config.qrcode_url_path, "w") as f:
                f.write(qrcode_url)
            os.chmod(config.qrcode_url_path, 0o600)
        except Exception as e:
            logger.warning("写入二维码 URL 文件失败: %s", e)

    logger.info(
        "请用手机微信扫描 %s 或下方二维码完成绑定",
        config.qrcode_png_path,
    )

    # Render ASCII QR code for terminal scanning
    try:
        import qrcode as qrcode_lib

        qr = qrcode_lib.QRCode(border=1)
        qr.add_data(qrcode_url)
        qr.make(fit=True)
        buf = StringIO()
        qr.print_ascii(out=buf)
        ascii_art = buf.getvalue()
        logger.info("ASCII 二维码:\n%s", ascii_art)
    except Exception as e:
        logger.debug("无法渲染 ASCII 二维码: %s", e)

    logger.info("等待扫码...（超时 %d 秒）", config.qrcode_poll_timeout)

    try:
        bot_token, baseurl = await client.poll_qrcode_status(
            qrcode_str,
            timeout=config.qrcode_poll_timeout,
        )
        client.state.bot_token = bot_token
        client.state.baseurl = baseurl
        client.state.bound_at = int(time.time())
        client.state.save()
        logger.info("绑定成功！bot_token 已持久化")
        return True
    except TimeoutError:
        logger.error("扫码超时，退出")
        return False


async def gate_and_run(
    client,
    from_user,
    context_token,
    prompt,
    *,
    display_prompt: str | None = None,
) -> tuple[str, list] | None:
    """Check prompt with is_dangerous; if dangerous, ask for confirmation.

    Returns (reply, artifacts) on safe prompt, None if confirmation asked.

    ``display_prompt`` (optional) is what the user sees in the confirm bubble;
    the stored/executed prompt remains ``prompt`` (execution path unchanged).
    """
    if is_dangerous(prompt):
        expire_at = time.time() + config.pending_confirm_ttl
        pending_confirms[from_user] = {
            "prompt": prompt,
            "expire_at": expire_at,
            "context_token": context_token,
        }
        shown = display_prompt if display_prompt is not None else prompt
        await client.send_message(
            to_user_id=from_user,
            text=(
                f"⚠️ **危险操作确认** ⚠️\n\n```\n{shown}\n```\n\n"
                f"- 回复 **{config.confirm_token}** → 执行\n"
                f"- 回复其他 → 取消"
            ),
            context_token=context_token,
            baseurl=client.state.baseurl,
            bot_token=client.state.bot_token,
        )
        logger.warning("[AUDIT] dangerous prompt pending confirmation: user=%s prompt=%.200s", from_user, prompt)
        return None
    return await _run_llm_with_guard(client, from_user, context_token, prompt)


async def send_artifacts_back(client, from_user, context_token, artifacts) -> None:
    """Filter artifacts: only send back those under per-user session dir.

    For agy: artifacts under .gemini/antigravity-cli/scratch (plus validated --add-dir)
    For codex: artifacts under session_dir (plus validated --add-dir)
    For grok: artifacts under session_dir (cwd where grok ran; no add-dir gate)
    """
    session_dir = get_session_dir(from_user)
    backend = _get_backend(from_user)
    add_dirs = []
    if backend == "grok":
        # grok runs with cwd=session_dir, artifacts are under session_dir
        allowed_root = session_dir
    elif backend == "codex":
        # codex runs with cwd=session_dir; file_change paths may also land in
        # user-approved --add-dir roots, so allow those too.
        allowed_root = session_dir
    elif backend == "dsh":
        # dsh runs with cwd=session_dir (per-user workspace); artifacts are
        # files the model created there. Session transcripts live at machine-wide
        # $DSH_HOME/sessions/ (outside per-user tree) and are cleaned by cutoff,
        # so they never match anyway.
        allowed_root = session_dir
    else:
        # agy writes to .gemini/antigravity-cli/scratch under session_dir
        allowed_root = os.path.join(session_dir, ".gemini", "antigravity-cli", "scratch")

    # codex and agy both support --add-dir; re-verify stored roots at send time.
    # Only keep roots that currently exist, are real directories, and still
    # resolve inside the configured allowed roots (session dir + config
    # add_dir_roots). Deleted dirs, plain files, out-of-bounds paths and
    # symlink escapes must not become artifact allow roots. Legitimate
    # directories are kept (and still re-checked against each artifact path
    # below). session_dir itself is never relaxed. grok does not use add-dir.
    if backend in ("codex", "agy"):
        prefs = load_prefs(from_user)
        for d in prefs.get("add_dirs", []) or []:
            ok, resolved = validate_add_dir(d, from_user)
            if ok:
                add_dirs.append(resolved)

    async def _notify_failure(name: str, reason: str) -> None:
        # Never echo server absolute paths to WeChat users
        await client.send_message(
            to_user_id=from_user,
            text=format_artifact_send_failure_notice(name, reason),
            context_token=context_token,
            baseurl=client.state.baseurl,
            bot_token=client.state.bot_token,
        )

    for art_name, art_path in artifacts:
        try:
            # realpath check blocks symlink escape outside allowed root
            if not path_is_under(art_path, allowed_root):
                ok_root = False
                for d in add_dirs:
                    if path_is_under(art_path, d):
                        ok_root = True
                        break
                if not ok_root:
                    logger.debug("skip non-scratch artifact: %s", art_path)
                    await _notify_failure(art_name, "skipped")
                    continue
            if not os.path.isfile(os.path.realpath(art_path)):
                logger.warning("Artifact not found: %s", art_path)
                await _notify_failure(art_name, "not_found")
                continue
            art_path = os.path.realpath(art_path)
            file_size = os.path.getsize(art_path)
            if file_size > config.max_outbound_file_bytes:
                size_mb = file_size / (1024 * 1024)
                # Never echo server absolute paths to WeChat users
                logger.info(
                    "Artifact too large (%.1f MB), kept on server: %s",
                    size_mb, art_path,
                )
                await client.send_message(
                    to_user_id=from_user,
                    text=format_oversized_artifact_notice(art_name, size_mb),
                    context_token=context_token,
                    baseurl=client.state.baseurl,
                    bot_token=client.state.bot_token,
                )
                continue
            ok = await client.send_media(
                to_user_id=from_user,
                baseurl=client.state.baseurl,
                bot_token=client.state.bot_token,
                context_token=context_token,
                path=art_path,
                caption="",
            )
            if ok:
                logger.info("Artifact sent: %s -> %s", art_name, from_user)
            else:
                logger.warning("Failed to send artifact: %s", art_name)
                await _notify_failure(art_name, "send_failed")
        except Exception as e:
            logger.exception("Error sending artifact %s: %s", art_name, e)
            try:
                await _notify_failure(art_name, "error")
            except Exception:
                logger.exception("Failed to notify user about artifact error: %s", art_name)


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------
async def process_message(client: ILinkClient, msg: dict) -> None:
    """Process a single WeChat message.

    - Image messages (type==2 item): download, AES decrypt, detect ext, save,
      then run CLI with ``prompt @path`` for image recognition.
    - Text-only messages: original logic (slash interception + run_llm).

    Image messages bypass slash command interception; the text caption (if any)
    is used as the prompt, otherwise a default prompt is used.
    """
    from_user = msg.get("from_user_id", "")
    context_token = msg.get("context_token", "")
    item_list = msg.get("item_list", [])
    logger.debug(
        "process_message: from=%s msg_type=%d items=%d",
        from_user, msg.get("message_type", 0), len(item_list),
    )

    # Extract image media and text from item_list
    text = ""
    image_media = None
    file_media = None
    file_name = ""
    voice_text = ""
    has_voice = False
    for item in item_list:
        item_type = item.get("type")
        if item_type == 1 and not text:
            text_item = item.get("text_item", {})
            text = text_item.get("text", "")
        elif item_type == 2 and image_media is None:
            image_item = item.get("image_item", {})
            media = image_item.get("media", {})
            if media.get("encrypt_query_param") or media.get("full_url"):
                image_media = media
        elif item_type == 3 and not has_voice:
            # Voice: WeChat transcribes to text server-side (voice_item.text).
            # Only passthrough the text — no silk decode / no ASR (dmit 1c965Mi).
            voice_item = item.get("voice_item", {})
            voice_text = voice_item.get("text", "") or ""
            has_voice = True
        elif item_type == 4 and file_media is None:
            fi = item.get("file_item", {})
            media = fi.get("media", {})
            if media.get("encrypt_query_param") or media.get("full_url"):
                file_media = media
                file_name = fi.get("file_name", "")

    if not context_token:
        logger.warning(
            "Message from %s has no context_token, cannot reply", from_user
        )
        return

    # ---- Whitelist check (before any processing) ----
    if config.allowed_senders and from_user not in config.allowed_senders:
        await client.send_message(
            to_user_id=from_user,
            text="⛔ **未授权用户** ⛔\n联系管理员添加白名单。",
            context_token=context_token,
            baseurl=client.state.baseurl,
            bot_token=client.state.bot_token,
        )
        logger.warning("拒绝非白名单用户: %s", from_user)
        return

    # ---- Admin notification (update available, etc.) ----
    await maybe_notify_admin(client, from_user, context_token)

    # ---- Pending dangerous prompt confirmation ----
    # 确认匹配用文本或语音转写（语音消息也可以回确认口令）
    reply_text = text.strip() or voice_text.strip()
    cancelled_notice = ""
    pending = pending_confirms.get(from_user)
    if pending:
        expired = time.time() >= pending["expire_at"]
        if not expired and reply_text.lower() == config.confirm_token.lower():
            # User confirmed → run pending prompt, send reply, return
            logger.info("[AUDIT] user=%s confirmed dangerous prompt", from_user)
            # 先删再执行：执行异常也不能让陈旧 pending 被后续消息误触发
            del pending_confirms[from_user]
            if image_media or file_media:
                logger.info("确认回复携带媒体，媒体部分被忽略 from=%s", from_user)
            reply, artifacts = await _run_llm_with_guard(
                client, from_user, context_token, pending["prompt"],
            )
            # Send reply
            success = await client.send_message(
                to_user_id=from_user,
                text=reply,
                context_token=context_token,
                baseurl=client.state.baseurl,
                bot_token=client.state.bot_token,
            )
            if success:
                logger.info("回复已发送到 %s", from_user)
            else:
                logger.warning("回复发送失败到 %s", from_user)
            # Send artifacts
            await send_artifacts_back(client, from_user, context_token, artifacts)
            return
        del pending_confirms[from_user]
        if expired:
            # Expired: don't reply, continue normal flow
            logger.info("[AUDIT] user=%s pending expired, continue normal flow", from_user)
        elif image_media or file_media:
            # 用户发来新媒体内容：取消待确认并继续处理本条消息（不静默丢弃）
            logger.info("[AUDIT] user=%s cancelled pending by sending new media", from_user)
            cancelled_notice = "🚫 已取消待确认的危险操作。\n\n"
        else:
            # User explicitly cancelled: reply cancelled, return
            logger.info("[AUDIT] user=%s cancelled dangerous prompt", from_user)
            await client.send_message(
                to_user_id=from_user,
                text="🚫 **已取消** 🚫",
                context_token=context_token,
                baseurl=client.state.baseurl,
                bot_token=client.state.bot_token,
            )
            return

    # ---- Case 1: Message contains an image ----
    artifacts = []
    reply = ""
    if image_media:
        if not image_media.get("aes_key"):
            reply = format_error("图片处理失败", "图片信息不完整，请重新发。")
            logger.warning("图片缺少 aes_key from=%s", from_user)
        else:
            try:
                # Download CDN image and AES decrypt → plaintext bytes
                plain_bytes = await client.download_and_decrypt_media(image_media)

                # Detect extension from magic bytes
                ext = _detect_image_ext(plain_bytes)

                # Save to per-user session images directory
                images_dir = os.path.join(get_session_dir(from_user), "images")
                os.makedirs(images_dir, exist_ok=True)
                try:
                    os.chmod(images_dir, 0o700)
                except OSError:
                    pass
                save_path = os.path.join(images_dir, f"{uuid.uuid4().hex[:12]}.{ext}")
                with open(save_path, "wb") as f:
                    f.write(plain_bytes)

                logger.info(
                    "图片已保存 %s (%d bytes, ext=%s)",
                    save_path, len(plain_bytes), ext,
                )

                # Build prompt: user's caption if present, else default
                prompt = text.strip() if text.strip() else "请描述这张图片的内容"
                logger.info("识图 from=%s: %s @%s", from_user, prompt, save_path)
                result = await gate_and_run(client, from_user, context_token, f"{prompt} @{save_path}")
                if result is None:
                    return
                reply, artifacts = result

            except Exception as e:
                # ilink 细节（aes_key / CDN / 字节数等）只写日志，不直出给用户
                logger.exception("图片下载/解密失败: %s", e)
                err_s = str(e)
                if isinstance(e, ValueError) and "过大" in err_s:
                    reply = format_error(
                        "文件太大了",
                        "图片太大，发不进来。请压缩后再发，或换成更小的图。",
                    )
                else:
                    reply = format_error("图片处理失败", "图片没处理好，请重新发。")

    # ---- Case 1.5: Message contains a file (non-image) ----
    elif file_media:
        if not file_media.get("aes_key"):
            reply = format_error("文件处理失败", "文件信息不完整，请重新发。")
            logger.warning("文件缺少 aes_key from=%s", from_user)
        else:
            try:
                plain_bytes = await client.download_and_decrypt_media(file_media)

                # Save to per-user session files directory
                files_dir = os.path.join(get_session_dir(from_user), "files")
                os.makedirs(files_dir, exist_ok=True)
                try:
                    os.chmod(files_dir, 0o700)
                except OSError:
                    pass
                # Preserve original extension from file_name (basename only)
                ext = os.path.splitext(os.path.basename(file_name or ""))[1]
                save_name = f"{uuid.uuid4().hex[:12]}{ext}" if ext else uuid.uuid4().hex[:12]
                save_path = os.path.join(files_dir, save_name)
                with open(save_path, "wb") as f:
                    f.write(plain_bytes)

                logger.info(
                    "文件已保存 %s (%d bytes)", save_path, len(plain_bytes),
                )

                prompt = text.strip() if text.strip() else "请分析这个文件"
                logger.info("文件分析 from=%s: %s @%s", from_user, prompt, save_path)
                result = await gate_and_run(client, from_user, context_token, f"{prompt} @{save_path}")
                if result is None:
                    return
                reply, artifacts = result

            except Exception as e:
                # ilink 细节（aes_key / CDN / 字节数等）只写日志，不直出给用户
                logger.exception("文件下载/解密失败: %s", e)
                err_s = str(e)
                if isinstance(e, ValueError) and "过大" in err_s:
                    reply = format_error(
                        "文件太大了",
                        "文件太大，发不进来。请压缩后再发，或换成更小的文件。",
                    )
                else:
                    reply = format_error("文件处理失败", "文件没处理好，请重新发。")

    # ---- Case 1.6: Voice message (text transcription passthrough) ----
    elif has_voice:
        if voice_text.strip():
            logger.info("语音转文字 from=%s: %.100s", from_user, voice_text.strip())
            result = await gate_and_run(client, from_user, context_token, voice_text.strip())
            if result is None:
                return
            reply, artifacts = result
        else:
            # WeChat failed to transcribe the voice → ask user to type.
            reply = "🤔 **没听清语音** 🤔\n\n请改成打字发。"
            logger.info("语音未识别出文字 from=%s", from_user)

    # ---- Case 2: Text-only message (original logic) ----
    else:
        if not text:
            logger.debug("Skipping non-text message from %s", from_user)
            return

        logger.info("收到消息 from=%s: %.100s", from_user, text)

        # Slash command interception
        if text.startswith("/"):
            logger.info("Slash command from=%s: %.200s", from_user, text)
            handled = await _handle_slash(client, text, from_user, context_token)
            if handled is _HANDLED:
                return
            if handled is None:
                # D class: passthrough — run CLI normally
                result = await gate_and_run(client, from_user, context_token, text)
                if result is None:
                    return
                reply, artifacts = result
            elif isinstance(handled, tuple):
                # /agent 等元指令经确认门后的执行结果
                reply, artifacts = handled
            else:
                reply = handled
        else:
            result = await gate_and_run(client, from_user, context_token, text)
            if result is None:
                return
            reply, artifacts = result

    # ---- Send reply via iLink ----
    success = await client.send_message(
        to_user_id=from_user,
        text=cancelled_notice + reply,
        context_token=context_token,
        baseurl=client.state.baseurl,
        bot_token=client.state.bot_token,
    )

    if success:
        logger.info("回复已发送到 %s", from_user)
    else:
        logger.warning("回复发送失败到 %s", from_user)

    # ---- Send artifacts back to WeChat ----
    await send_artifacts_back(client, from_user, context_token, artifacts)


# ---------------------------------------------------------------------------
# Scratch TTL cleanup
# ---------------------------------------------------------------------------

def clean_scratch():
    """Remove old global scratch files and per-user session media."""
    scratch_dir = config.agy_scratch_dir
    if os.path.isdir(scratch_dir):
        now = time.time()
        cutoff = now - config.scratch_retention_days * 86400
        try:
            for name in os.listdir(scratch_dir):
                path = os.path.join(scratch_dir, name)
                if os.path.isfile(path):
                    mtime = os.path.getmtime(path)
                    if mtime < cutoff:
                        os.remove(path)
                        logger.info(
                            "Scratch cleanup: removed %s (age %.1f days)",
                            path, (now - mtime) / 86400,
                        )
        except OSError as e:
            logger.error("Scratch cleanup error: %s", e)
    removed = clean_session_media()  # images/files + safe session temps (A+C)
    if removed:
        logger.info("Session temp cleanup: removed %d files", removed)
    _prune_user_locks()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
async def periodic_clean_scratch():
    """Run clean_scratch every 3600 seconds as a background task."""
    while True:
        try:
            await asyncio.sleep(3600)
            # 同步文件遍历放到线程池，避免阻塞事件循环卡死长轮询心跳
            await asyncio.to_thread(clean_scratch)
        except Exception as e:
            logger.exception("periodic_clean_scratch error: %s", e)


async def main_loop() -> None:
    """Main daemon loop: manages state, QR login, and message receiving."""
    ensure_runtime_dirs()
    # 同步文件遍历放到线程池，避免阻塞事件循环
    await asyncio.to_thread(clean_scratch)
    if config.update_check_enabled:
        _spawn_bg(update_check_loop())
    _spawn_bg(periodic_clean_scratch())
    while True:
        client = ILinkClient()
        # 本轮 client 的在途消息任务（强引用持有，relogin 前排空，避免拿死连接发消息）
        msg_tasks: set = set()
        try:
            state_loaded = client.state.load()

            if not state_loaded or not client.state.bot_token:
                try:
                    success = await login_flow(client)
                except Exception as e:
                    # 网络抖动/服务端异常不应直接杀死 daemon
                    logger.exception("登录流程异常，5 秒后重试: %s", e)
                    await asyncio.sleep(5)
                    continue
                if not success:
                    logger.warning("扫码超时，3 秒后重新获取二维码等待扫码")
                    await asyncio.sleep(3)
                    continue

            baseurl = client.state.baseurl
            bot_token = client.state.bot_token
            logger.info("开始长轮询 iLink 消息 (baseurl=%s)", baseurl)

            # Inner loop: long-poll for messages
            get_updates_buf = ""
            fail_delay = 0.5  # 网络异常指数退避，封顶 30s
            while True:
                try:
                    msgs, new_buf = await client.get_updates(
                        get_updates_buf, baseurl, bot_token
                    )
                    fail_delay = 0.5  # 成功一次即重置退避
                except Exception as e:
                    # Token invalidated (401/403) → break for re-login
                    logger.exception("长轮询异常: %s", e)
                    if not client.state.bot_token:
                        logger.warning("Bot token 已失效，准备重新登录")
                        break
                    # Network hiccup → 指数退避后重试
                    await asyncio.sleep(fail_delay)
                    fail_delay = min(fail_delay * 2, 30.0)
                    continue

                # Always update cursor with the server-returned value
                get_updates_buf = new_buf

                for msg in msgs:
                    msg_type = msg.get("message_type", 0)
                    if msg_type == 1:  # User message
                        if _is_duplicate_msg(msg):
                            logger.info("跳过重复投递的消息: %s", _msg_dedup_key(msg))
                            continue
                        logger.debug("inbound msg keys: %s", sorted(msg.keys()))
                        # Non-blocking async task creation: process message in background
                        t = asyncio.create_task(_safe_process_message(client, msg))
                        msg_tasks.add(t)
                        t.add_done_callback(msg_tasks.discard)
                    else:
                        logger.debug(
                            "跳过 message_type=%s", msg_type
                        )

                if not client.state.bot_token:
                    break

        except KeyboardInterrupt:
            logger.info("收到退出信号")
            raise
        finally:
            # 先排空/取消在途消息任务，再关连接，避免任务拿死 client 静默失败
            if msg_tasks:
                snapshot = set(msg_tasks)
                logger.info(
                    "等待 %d 个在途消息任务完成（最长 %.0fs）...",
                    len(snapshot), _DRAIN_TIMEOUT_S,
                )
                _, still_pending = await asyncio.wait(snapshot, timeout=_DRAIN_TIMEOUT_S)
                if still_pending:
                    logger.warning("强制取消 %d 个未完成的在途任务", len(still_pending))
                    for t in still_pending:
                        t.cancel()
                    await asyncio.gather(*still_pending, return_exceptions=True)
            await client.close()

        # Decide whether to re-login or exit
        if not client.state.bot_token:
            logger.info("Bot token 已失效，重新执行登录流程...")
            # Small delay before re-login to avoid tight loop
            await asyncio.sleep(2)
            continue  # outer loop → re-login
        else:
            # Normal exit (should not happen in steady-state)
            break


# ---------------------------------------------------------------------------
# Per-user async execution lock, global concurrency gate, background spawner
# ---------------------------------------------------------------------------
user_locks: dict = {}
_global_task_sem: asyncio.Semaphore | None = None


def _get_global_sem() -> asyncio.Semaphore:
    global _global_task_sem
    if _global_task_sem is None:
        n = max(int(config.max_concurrent_tasks), 1)
        _global_task_sem = asyncio.Semaphore(n)
    return _global_task_sem


def _prune_user_locks() -> None:
    """Drop idle per-user locks so the dict does not grow forever."""
    idle = [uid for uid, lock in user_locks.items() if not lock.locked()]
    for uid in idle:
        user_locks.pop(uid, None)


async def _safe_process_message(client: ILinkClient, msg: dict) -> None:
    """Run process_message with per-user serial queue then global concurrency.

    Order matters:
      1) Take per-user lock first — same-user messages queue here and do NOT
         hold a global slot while waiting for the previous one.
      2) C (user gap) + B (global cooldown, 🔔) wait *before* the global slot,
         so multi-user throttle sleeps do not occupy concurrent slots.
      3) Acquire global semaphore only immediately before process_message.
      4) Fail-fast (short timeout) if global slots are full; release only if held.
      5) Bind slot into ContextVar so A retry backoff inside the guard can
         temporarily release/re-acquire without deadlocking user_lock order.

    Thus one user can occupy at most one global slot at a time; cross-user
    parallelism is still capped by WECHATBRIDGE_MAX_CONCURRENT.
    """
    from_user = msg.get("from_user_id", "")
    context_token = msg.get("context_token", "")
    sem = _get_global_sem()

    # Serialize same user first (empty from_user still gets a dedicated lock key)
    if from_user not in user_locks:
        user_locks[from_user] = asyncio.Lock()

    async with user_locks[from_user]:
        # C/B before global slot — do not sleep while holding a concurrent slot
        await _await_upstream_preflight(client, from_user, context_token)

        try:
            await asyncio.wait_for(sem.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            logger.warning("并发已满，拒绝处理 from=%s", from_user)
            if context_token and from_user:
                try:
                    await client.send_message(
                        to_user_id=from_user,
                        text=_BUSY_USER_TEXT,
                        context_token=context_token,
                        baseurl=client.state.baseurl,
                        bot_token=client.state.bot_token,
                    )
                except Exception as e:
                    logger.warning("发送繁忙提示失败: %s", e)
            return

        slot = _GlobalSlot(sem)
        token = _global_slot_ctx.set(slot)
        try:
            await process_message(client, msg)
        except Exception as e:
            logger.exception("处理消息异常 (from=%s): %s", from_user, e)
        finally:
            _global_slot_ctx.reset(token)
            # Only release if this task still owns the slot (A backoff may have
            # temporarily released; sleep_released re-acquires before return).
            if slot.held:
                sem.release()
                slot.held = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(prog="wechatbridge", description="Bridge WeChat messages to agy, Grok Build, Codex, or dsh CLIs — text/image/file/voice in, CLI replies and generated files back.")
    parser.add_argument("--version", action="version", version=f"wechatbridge {__version__}")
    parser.parse_args()
    logger.info("wechatbridge v%s 启动 (backend=%s, instance=%s)", __version__, config.backend, config.instance)
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("进程退出")
    except Exception as e:
        logger.exception("未预期错误: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
