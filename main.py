"""
wechatbridge Main Entry Point.
Active iLink client that receives WeChat messages and responds via agy CLI.
Architecture: WeChat ClawBot(iLink) <-> wechatbridge(Python) <-> agy CLI
"""

import asyncio
import base64
import logging
import os
import sys
import time
import uuid
from io import StringIO

from config import config
from ilink import ILinkClient
from agy_runner import run_agy, handle_slash_command, get_session_dir, is_dangerous

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
            im.save(config.qrcode_png_path)
            logger.info(
                "二维码图片已保存到 %s", config.qrcode_png_path
            )
        except Exception as e:
            logger.warning("保存二维码 PNG 失败: %s", e)

        # Write URL to file for external access
        try:
            with open(config.qrcode_url_path, "w") as f:
                f.write(qrcode_url)
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
            interval=config.qrcode_poll_interval,
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


async def gate_and_run(client, from_user, context_token, prompt) -> tuple[str, list] | None:
    """Check prompt with is_dangerous; if dangerous, ask for confirmation.

    Returns (reply, artifacts) on safe prompt, None if confirmation asked.
    """
    if is_dangerous(prompt):
        expire_at = time.time() + config.pending_confirm_ttl
        pending_confirms[from_user] = {
            "prompt": prompt,
            "expire_at": expire_at,
            "context_token": context_token,
        }
        await client.send_message(
            to_user_id=from_user,
            text=f"⚠️ **危险操作确认** ⚠️\n\n```\n{prompt}\n```\n\n- 回复 **y** → 执行\n- 回复其他 → 取消",
            context_token=context_token,
            baseurl=client.state.baseurl,
            bot_token=client.state.bot_token,
        )
        logger.warning("[AUDIT] dangerous prompt pending confirmation: user=%s prompt=%.200s", from_user, prompt)
        return None
    return await run_agy(prompt, from_user)


async def send_artifacts_back(client, from_user, context_token, artifacts) -> None:
    """Filter artifacts: only send back those under per-user session scratch dir."""
    user_scratch = os.path.join(get_session_dir(from_user), '.gemini', 'antigravity-cli', 'scratch')
    scratch_prefix = os.path.abspath(user_scratch) + os.sep
    for art_name, art_path in artifacts:
        try:
            if not os.path.abspath(art_path).startswith(scratch_prefix):
                logger.debug("skip non-scratch artifact: %s", art_path)
                continue
            if not os.path.isfile(art_path):
                logger.warning("Artifact not found: %s", art_path)
                continue
            file_size = os.path.getsize(art_path)
            if file_size > config.max_outbound_file_bytes:
                size_mb = file_size / (1024 * 1024)
                await client.send_message(
                    to_user_id=from_user,
                    text=f"⚠️ **文件过大** ⚠️\n\n`{art_name}` {size_mb:.1f} MB\n已存：`{art_path}`",
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
        except Exception as e:
            logger.exception("Error sending artifact %s: %s", art_name, e)


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------
async def process_message(client: ILinkClient, msg: dict) -> None:
    """Process a single WeChat message.

    - Image messages (type==2 item): download, AES decrypt, detect ext, save,
      then run agy with ``prompt @path`` for image recognition.
    - Text-only messages: original logic (slash interception + run_agy).

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

    # ---- Pending dangerous prompt confirmation ----
    pending = pending_confirms.get(from_user)
    if pending:
        expired = time.time() >= pending["expire_at"]
        if not expired and text.strip().lower() == config.confirm_token.lower():
            # User confirmed → run pending prompt, send reply, return
            logger.info("[AUDIT] user=%s confirmed dangerous prompt", from_user)
            reply, artifacts = await run_agy(pending["prompt"], from_user)
            del pending_confirms[from_user]
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
    if image_media and image_media.get("aes_key"):
        try:
            # Download CDN image and AES decrypt → plaintext bytes
            plain_bytes = await client.download_and_decrypt_media(image_media)

            # Detect extension from magic bytes
            ext = _detect_image_ext(plain_bytes)

            # Save to per-user session images directory
            images_dir = os.path.join(get_session_dir(from_user), "images")
            os.makedirs(images_dir, exist_ok=True)
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
            logger.exception("图片下载/解密失败: %s", e)
            reply = f"❌ **图片下载或解密失败** ❌\n\n```\n{e}\n```"

    # ---- Case 1.5: Message contains a file (non-image) ----
    elif file_media and file_media.get("aes_key"):
        try:
            plain_bytes = await client.download_and_decrypt_media(file_media)

            # Save to per-user session files directory
            files_dir = os.path.join(get_session_dir(from_user), "files")
            os.makedirs(files_dir, exist_ok=True)
            # Preserve original extension from file_name
            ext = os.path.splitext(file_name)[1] if file_name else ""
            save_name = f"{uuid.uuid4().hex[:12]}{ext}" if ext else (file_name or uuid.uuid4().hex[:12])
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
            logger.exception("文件下载/解密失败: %s", e)
            reply = f"❌ **文件下载或解密失败** ❌\n\n```\n{e}\n```"

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
            reply = "🤔 **听不清，请打字** 🤔"
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
            reply = await handle_slash_command(text, from_user)
            if reply is None:
                # D class: passthrough — run agy normally
                result = await gate_and_run(client, from_user, context_token, text)
                if result is None:
                    return
                reply, artifacts = result
        else:
            result = await gate_and_run(client, from_user, context_token, text)
            if result is None:
                return
            reply, artifacts = result

    # ---- Send reply via iLink ----
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

    # ---- Send artifacts back to WeChat ----
    await send_artifacts_back(client, from_user, context_token, artifacts)


# ---------------------------------------------------------------------------
# Scratch TTL cleanup
# ---------------------------------------------------------------------------

def clean_scratch():
    """Remove scratch files older than scratch_retention_days."""
    scratch_dir = config.agy_scratch_dir
    if not os.path.isdir(scratch_dir):
        return
    now = time.time()
    cutoff = now - config.scratch_retention_days * 86400
    try:
        for name in os.listdir(scratch_dir):
            path = os.path.join(scratch_dir, name)
            if os.path.isfile(path):
                mtime = os.path.getmtime(path)
                if mtime < cutoff:
                    os.remove(path)
                    logger.info("Scratch cleanup: removed %s (age %.1f days)", path, (now - mtime) / 86400)
    except OSError as e:
        logger.error("Scratch cleanup error: %s", e)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
async def periodic_clean_scratch():
    """Run clean_scratch every 3600 seconds as a background task."""
    while True:
        try:
            await asyncio.sleep(3600)
            clean_scratch()
        except Exception as e:
            logger.exception("periodic_clean_scratch error: %s", e)


async def main_loop():
    """Outer loop: login → long-poll messages → re-login on token expiry."""
    clean_scratch()
    asyncio.create_task(periodic_clean_scratch())
# ---------------------------------------------------------------------------
# Per-user async execution lock and background task spawner
# ---------------------------------------------------------------------------
user_locks: dict = {}


async def _safe_process_message(client: ILinkClient, msg: dict) -> None:
    """Run process_message inside a per-user lock as a background task.

    This ensures the main get_updates long-polling loop is NEVER blocked,
    keeping WeChat heartbeats 100% active while ensuring per-user message ordering.
    """
    from_user = msg.get("from_user_id", "")
    if from_user not in user_locks:
        user_locks[from_user] = asyncio.Lock()

    async with user_locks[from_user]:
        try:
            await process_message(client, msg)
        except Exception as e:
            logger.exception("处理消息异常 (from=%s): %s", from_user, e)


async def main_loop() -> None:
    """Main daemon loop: manages state, QR login, and message receiving."""
    while True:
        client = ILinkClient()
        try:
            state_loaded = client.state.load()

            if not state_loaded or not client.state.bot_token:
                success = await login_flow(client)
                if not success:
                    logger.warning("扫码超时，3 秒后重新获取二维码等待扫码")
                    await asyncio.sleep(3)
                    continue

            baseurl = client.state.baseurl
            bot_token = client.state.bot_token
            logger.info("开始长轮询 iLink 消息 (baseurl=%s)", baseurl)

            # Inner loop: long-poll for messages
            get_updates_buf = ""
            while True:
                try:
                    msgs, new_buf = await client.get_updates(
                        get_updates_buf, baseurl, bot_token
                    )
                except Exception as e:
                    # Token invalidated (401/403) → break for re-login
                    logger.exception("长轮询异常: %s", e)
                    if not client.state.bot_token:
                        logger.warning("Bot token 已失效，准备重新登录")
                        break
                    # Network hiccup → short delay and retry
                    await asyncio.sleep(0.5)
                    continue

                # Always update cursor with the server-returned value
                get_updates_buf = new_buf

                for msg in msgs:
                    msg_type = msg.get("message_type", 0)
                    if msg_type == 1:  # User message
                        # Non-blocking async task creation: process message in background
                        asyncio.create_task(_safe_process_message(client, msg))
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
# Entry point
# ---------------------------------------------------------------------------
def main():
    logger.info("wechatbridge 启动")
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("进程退出")
    except Exception as e:
        logger.exception("未预期错误: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
