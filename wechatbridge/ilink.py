"""
iLink protocol client for WeChat ClawBot.
Handles QR code login, long polling message receiving, and message sending.
Picture download and AES-128-ECB decryption for iLink CDN images.
"""

import asyncio
import base64
import json
import logging
import hashlib
import mimetypes
import os
import random
import stat
import time
import uuid
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx

from .config import config

logger = logging.getLogger("ilink")

ILINK_BASE = config.ilink_base_url.rstrip("/")

# WeChat image_item handles these well; other image/* (svg/heic/…) go as FILE.
_DISPLAYABLE_IMAGE_MIMES = frozenset({
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
})

MEDIA_IMAGE = 1
MEDIA_FILE = 3


def media_type_for_path(path: str) -> int:
    """Return MEDIA_IMAGE or MEDIA_FILE for a local path based on guessed MIME.

    Only common displayable raster images use IMAGE; everything else
    (pdf/doc/xlsx/txt/zip, svg/heic, unknown) uses FILE so file_name is kept.
    """
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if mime in _DISPLAYABLE_IMAGE_MIMES:
        return MEDIA_IMAGE
    return MEDIA_FILE


def ilink_delivery_accepted(ret, message_id) -> bool:
    """Whether an iLink sendmessage JSON body means the message was accepted.

    The server often returns ``ret != 0`` (commonly ``-1``) **together with** a
    non-empty ``message_id`` when delivery succeeded. Treat as failure only when
    there is no message id to prove acceptance. ``ret == 0`` always accepts.
    """
    if ret == 0:
        return True
    if message_id is None:
        return False
    if isinstance(message_id, str):
        return bool(message_id.strip())
    return bool(message_id)


def _is_allowed_media_url(url: str) -> bool:
    """Only allow downloads from WeChat CDN hosts or the configured CDN base host."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host == "weixin.qq.com" or host.endswith(".weixin.qq.com"):
        return True
    if host == "wechat.com" or host.endswith(".wechat.com"):
        return True
    if host == "qq.com" or host.endswith(".qq.com"):
        # WeChat CDN commonly uses *.cdn.weixin.qq.com / *.qq.com
        if "weixin" in host or "cdn" in host or "novac" in host:
            return True
    try:
        base_host = (urlparse(config.cdn_base_url).hostname or "").lower()
    except Exception:
        base_host = ""
    if base_host and host == base_host:
        return True
    return False

# iLink app identity (mirrors official @tencent-weixin/openclaw-weixin package.json)
ILINK_APP_ID = "bot"
# ClientVersion encoding: (major<<16)|(minor<<8)|patch; "2.4.6" -> 132102
ILINK_APP_CLIENT_VERSION = "132102"
CHANNEL_VERSION = "2.4.6"
BOT_AGENT = "OpenClaw"


def _build_base_info() -> Dict[str, str]:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def _make_wechat_uin_header() -> str:
    """Generate X-WECHAT-UIN header: base64(str(random_uint32))."""
    uin = random.randint(0, 0xFFFFFFFF)
    uin_str = str(uin)
    return base64.b64encode(uin_str.encode()).decode()


def _common_headers(has_auth: bool = False, bot_token: str = "") -> Dict[str, str]:
    """Build common request headers per iLink spec."""
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _make_wechat_uin_header(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    if has_auth and bot_token:
        headers["Authorization"] = f"Bearer {bot_token}"
    return headers


# ---------------------------------------------------------------------------
# Image decryption helpers
# ---------------------------------------------------------------------------


def _parse_aes_key(aes_key_b64: str) -> bytes:
    """Parse base64-encoded AES key from iLink CDN media.

    Supports two formats:
      - 16 raw bytes → direct AES-128 key
      - 32 ASCII hex chars → hex-decode into 16 bytes
    """
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            s = decoded.decode("ascii")
            if all(c in "0123456789abcdefABCDEF" for c in s):
                return bytes.fromhex(s)
        except Exception:
            pass
    raise ValueError(
        f"aes_key 解码后 {len(decoded)} 字节，既非 16 字节原始 key 也非 32 字节 hex 串"
    )


def _decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt AES-128-ECB ciphertext and remove PKCS7 padding."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


# ---------------------------------------------------------------------------
# AES encryption helpers (symmetric to _decrypt_aes_ecb above)
# ---------------------------------------------------------------------------


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    """PKCS7 pad input data to a multiple of block_size."""
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt plaintext with AES-128-ECB (PKCS7 padded) without duplicating full buffer."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    tail_offset = (len(plaintext) // 16) * 16
    pad_len = 16 - (len(plaintext) - tail_offset)
    tail_padded = plaintext[tail_offset:] + bytes([pad_len] * pad_len)
    if tail_offset:
        return (
            encryptor.update(memoryview(plaintext)[:tail_offset])
            + encryptor.update(tail_padded)
            + encryptor.finalize()
        )
    return encryptor.update(tail_padded) + encryptor.finalize()


def _aes_padded_size(rawsize: int) -> int:
    """Compute the AES-ECB padded ciphertext size for a given raw size."""
    return ((rawsize + 1 + 15) // 16) * 16


# ---------------------------------------------------------------------------
# iLink state persistence
# ---------------------------------------------------------------------------


class ILinkState:
    """Persistent state for bot login (bot_token + baseurl)."""

    def __init__(self, path: str = config.state_file_path):
        self.path = path
        self.bot_token: str = ""
        self.baseurl: str = ""
        self.bound_at: int = 0

    def load(self) -> bool:
        """Load state from file. Returns True if valid state loaded."""
        logger.debug("Loading iLink state from %s", self.path)
        if not os.path.exists(self.path):
            logger.info("No iLink state file found, need to login")
            return False
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
            self.bot_token = data.get("bot_token", "")
            self.baseurl = data.get("baseurl", "")
            self.bound_at = data.get("bound_at", 0)
            if not self.bot_token or not self.baseurl:
                logger.warning("Incomplete iLink state file, need to re-login")
                return False
            logger.info(
                "Loaded iLink state (bot_token=%s..., baseurl=%s)",
                self.bot_token[:8] if len(self.bot_token) > 8 else "?",
                self.baseurl,
            )
            return True
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load iLink state: %s", e)
            return False

    def save(self) -> None:
        """Persist state to file with restricted permissions."""
        data = {
            "bot_token": self.bot_token,
            "baseurl": self.baseurl,
            "bound_at": self.bound_at or int(time.time()),
        }
        logger.info(
            "Saving iLink state (bot_token=%s..., baseurl=%s)",
            self.bot_token[:8] if len(self.bot_token) > 8 else "?",
            self.baseurl,
        )
        try:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
                try:
                    os.chmod(parent, 0o700)
                except OSError:
                    pass
            # tmp + os.replace 原子写，避免崩溃留下半截 state 文件
            tmp_path = self.path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
        except OSError as e:
            logger.error("Failed to save iLink state: %s", e)

    def clear(self) -> None:
        """Remove state file (used on token expiration)."""
        self.bot_token = ""
        self.baseurl = ""
        self.bound_at = 0
        if os.path.exists(self.path):
            os.remove(self.path)
            logger.info("Cleared iLink state file")


# ---------------------------------------------------------------------------
# iLink API client
# ---------------------------------------------------------------------------


class ILinkClient:
    """iLink API client for WeChat ClawBot."""

    def __init__(self):
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
            follow_redirects=True,
        )
        self.state = ILinkState()

    async def close(self):
        await self.http_client.aclose()

    # ---- QR Code Login (no Authorization required) ----

    async def get_qrcode(self) -> Tuple[str, str]:
        """
        Request a QR code for WeChat binding.
        Returns: (qrcode_str, qrcode_url)
        """
        logger.info("Requesting QR code from iLink...")
        headers = _common_headers(has_auth=False)
        url = f"{ILINK_BASE}/ilink/bot/get_bot_qrcode?bot_type=3"

        response = await self.http_client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

        qrcode_str = data.get("qrcode", "")
        qrcode_img_content = data.get("qrcode_img_content", "")

        if not qrcode_str:
            raise RuntimeError(
                f"get_bot_qrcode response missing 'qrcode': {data}"
            )

        logger.info("QR code received successfully")
        return qrcode_str, qrcode_img_content

    async def poll_qrcode_status(
        self, qrcode_str: str, timeout: int = 180
    ) -> Tuple[str, str]:
        """
        Poll QR code status until confirmed or timeout.
        Returns: (bot_token, baseurl)
        Raises TimeoutError on timeout.
        """
        logger.info("Polling QR code status with backoff...")
        url = f"{ILINK_BASE}/ilink/bot/get_qrcode_status"
        headers = _common_headers(has_auth=False)

        backoff = config.qrcode_poll_interval
        start_time = time.monotonic()
        while (time.monotonic() - start_time) < timeout:
            params = {"qrcode": qrcode_str}
            try:
                response = await self.http_client.get(
                    url, headers=headers, params=params
                )
                response.raise_for_status()
                data = response.json()

                status = data.get("status", "waiting")
                logger.info("QR code status: %s", status)

                if status == "confirmed":
                    bot_token = data.get("bot_token", "")
                    baseurl = data.get("baseurl", "")
                    if not bot_token:
                        logger.warning(
                            "Status confirmed but no bot_token in response"
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2 * random.uniform(0.5, 1.5), 30.0)
                        continue
                    logger.info("QR code confirmed! Bot token received.")
                    return bot_token, baseurl

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2 * random.uniform(0.5, 1.5), 30.0)

            except httpx.HTTPStatusError as e:
                logger.warning("HTTP error polling QR code status: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2 * random.uniform(0.5, 1.5), 30.0)
            except httpx.RequestError as e:
                logger.warning("Request error polling QR code status: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2 * random.uniform(0.5, 1.5), 30.0)

        raise TimeoutError(
            f"QR code polling timed out after {timeout}s"
        )

    # ---- Message Operations (Authorization required) ----

    async def get_updates(
        self, buf: str, baseurl: str, bot_token: str
    ) -> Tuple[List[Dict], str]:
        """
        Long polling get updates from iLink.
        Returns: (list_of_messages, new_buf)
        Retains old buf on error.
        """
        url = f"{baseurl}/ilink/bot/getupdates"
        headers = _common_headers(has_auth=True, bot_token=bot_token)
        body = {
            "get_updates_buf": buf,
            "base_info": _build_base_info(),
        }

        try:
            response = await self.http_client.post(
                url, headers=headers, json=body
            )
            response.raise_for_status()
            data = response.json()

            ret = data.get("ret", -1)
            msgs = data.get("msgs", [])
            new_buf = data.get("get_updates_buf", buf)
            if ret != 0 and not msgs:
                if ret == -1:
                    logger.debug("getupdates empty poll (ret=-1, no msgs)")
                else:
                    logger.warning("getupdates returned ret=%s, keys=%s", ret, list(data.keys()))
                return [], buf
            if ret != 0:
                logger.info("getupdates ret=%s but has %d msgs, processing", ret, len(msgs))
            return msgs, new_buf

        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                logger.warning("Bot token rejected (401/403), clearing state")
                self.state.clear()
                raise  # Let caller re-login
            # 5xx / 429 等 HTTP 状态错误同样上抛，由主循环统一做指数退避（避免原地满速重试）
            logger.warning("HTTP error in get_updates: status=%s", e.response.status_code)
            raise
        except httpx.RequestError as e:
            # 网络层错误上抛，由主循环做指数退避（避免原地满速重试+日志洪泛）
            logger.warning("Network error in get_updates: %s", e)
            raise
        except Exception as e:
            logger.exception("Unexpected error in get_updates: %s", e)
            raise

    # ---- Media upload and sending ----

    async def get_upload_url(
        self,
        to_user_id: str,
        baseurl: str,
        bot_token: str,
        media_type: int,
        filekey: str,
        rawsize: int,
        rawfilemd5: str,
        filesize: int,
        aeskey_hex: str,
    ) -> Tuple[str, str]:
        """Obtain a CDN upload URL for encrypted media.

        POSTs to {baseurl}/ilink/bot/getuploadurl with media metadata.
        Returns (upload_param, upload_full_url).

        Reference: hermes weixin.py _get_upload_url ~L518-548
        """
        url = f"{baseurl}/ilink/bot/getuploadurl"
        headers = _common_headers(has_auth=True, bot_token=bot_token)
        body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aeskey_hex,
        }

        try:
            response = await self.http_client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            upload_param = str(data.get("upload_param", "") or "")
            upload_full_url = str(data.get("upload_full_url", "") or "")
            if not upload_param and not upload_full_url:
                logger.warning(
                    "getUploadUrl returned neither upload_param nor upload_full_url: %s",
                    data,
                )
            logger.debug(
                "getUploadUrl success: upload_param=%.80s upload_full_url=%.200s",
                upload_param, upload_full_url,
            )
            return upload_param, upload_full_url
        except httpx.HTTPStatusError as e:
            logger.error("HTTP error in get_upload_url: %s", e)
            raise
        except httpx.RequestError as e:
            logger.error("Network error in get_upload_url: %s", e)
            raise

    async def upload_ciphertext(self, upload_url: str, ciphertext: bytes) -> str:
        """Upload encrypted media bytes to the CDN via POST.

        The CDN expects POST with Content-Type application/octet-stream.
        Returns the x-encrypted-param header value for use in sendmessage.

        Reference: hermes weixin.py _upload_ciphertext ~L550-574
        """
        headers = {"Content-Type": "application/octet-stream"}
        try:
            t0 = time.time()
            response = await self.http_client.post(
                upload_url,
                content=ciphertext,
                headers=headers,
                timeout=config.cdn_upload_timeout,
            )
            response.raise_for_status()
            encrypted_param = response.headers.get("x-encrypted-param")
            if not encrypted_param:
                raise RuntimeError(
                    f"CDN upload missing x-encrypted-param header: {response.text[:200]}"
                )
            elapsed = time.time() - t0
            logger.info(
                "CDN upload done: %d bytes in %.3fs x-encrypted-param(len=%d)",
                len(ciphertext), elapsed, len(encrypted_param),
            )
            return encrypted_param
        except httpx.TimeoutException:
            logger.error("CDN upload timed out after %ss", config.cdn_upload_timeout)
            raise
        except httpx.HTTPStatusError as e:
            logger.error("CDN upload HTTP %s: %s", e.response.status_code, e)
            raise

    async def send_media(
        self,
        to_user_id: str,
        baseurl: str,
        bot_token: str,
        context_token: str,
        path: str,
        caption: str = "",
    ) -> bool:
        """Encrypt and send a media file (image or file) via WeChat iLink.

        1. Reads the file, computes rawsize/rawfilemd5/filekey/aes_key.
        2. Encrypts plaintext with AES-128-ECB.
        3. Gets CDN upload URL via get_upload_url.
        4. Uploads ciphertext via upload_ciphertext.
        5. Builds media item (image_item or file_item depending on mime type).
        6. If caption provided, sends text message first.
        7. Sends media message.

        Returns True if the media message was sent successfully.

        Reference: hermes weixin.py _send_media ~L2110-2170
        """
        logger.info("send_media: %s for user %s", path, to_user_id)
        max_out = max(int(config.max_outbound_file_bytes), 1)
        try:
            open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, open_flags)
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode):
                    logger.warning("Refusing non-regular file in send_media: %s", path)
                    return False
                if st.st_nlink > 1:
                    logger.warning(
                        "Refusing hard-linked file in send_media (nlink=%d): %s",
                        st.st_nlink, path,
                    )
                    return False
                if st.st_size > max_out:
                    logger.warning(
                        "Refusing oversized file in send_media (%d > %d): %s",
                        st.st_size, max_out, path,
                    )
                    return False
                plaintext = f.read(max_out + 1)
            if len(plaintext) > max_out:
                logger.warning("File grew beyond max_outbound_file_bytes during read: %s", path)
                return False
        except OSError as e:
            logger.error("Cannot read file %s: %s", path, e)
            return False

        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        filekey = os.urandom(16).hex()
        aes_key = os.urandom(16)
        logger.debug(
            "send_media: rawsize=%d rawfilemd5=%s filekey=%s aes_key=%.8s...",
            rawsize, rawfilemd5, filekey, aes_key.hex(),
        )
        ciphertext = _encrypt_aes_ecb(plaintext, aes_key)
        del plaintext
        filesize = len(ciphertext)

        # Determine media_type from mime (only common rasters → IMAGE)
        media_type = media_type_for_path(path)

        # Get CDN upload URL
        upload_param, upload_full_url = await self.get_upload_url(
            to_user_id=to_user_id,
            baseurl=baseurl,
            bot_token=bot_token,
            media_type=media_type,
            filekey=filekey,
            rawsize=rawsize,
            rawfilemd5=rawfilemd5,
            filesize=filesize,
            aeskey_hex=aes_key.hex(),
        )

        # Prefer upload_full_url (direct CDN), fall back to constructed CDN URL
        # from upload_param (reference: weixin.py _cdn_upload_url).
        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = (
                f"{config.cdn_base_url}/upload"
                f"?encrypted_query_param={quote(upload_param, safe='')}"
                f"&filekey={quote(filekey, safe='')}"
            )
        else:
            raise RuntimeError(
                "getUploadUrl returned neither upload_param nor upload_full_url"
            )

        # Upload ciphertext
        encrypt_query_param = await self.upload_ciphertext(
            upload_url=upload_url, ciphertext=ciphertext
        )

        # Build media item
        # Key encoding: base64(aes_key.hex()) — NOT base64(raw_bytes).
        # Sending base64(raw_bytes) causes grey-box images on receiver side.
        aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode()

        filename = os.path.basename(path)
        if media_type == 1:  # image
            item = {
                "type": 2,  # ITEM_IMAGE
                "image_item": {
                    "media": {
                        "encrypt_query_param": encrypt_query_param,
                        "aes_key": aes_key_for_api,
                        "encrypt_type": 1,
                    },
                    "mid_size": filesize,
                },
            }
        else:  # file
            item = {
                "type": 4,  # ITEM_FILE
                "file_item": {
                    "media": {
                        "encrypt_query_param": encrypt_query_param,
                        "aes_key": aes_key_for_api,
                        "encrypt_type": 1,
                    },
                    "file_name": filename,
                    "len": str(rawsize),
                },
            }

        if caption:
            caption_ok = await self.send_message(
                to_user_id=to_user_id,
                text=caption,
                context_token=context_token,
                baseurl=baseurl,
                bot_token=bot_token,
            )
            if not caption_ok:
                logger.warning("send_media: caption send failed, continuing with media")

        return await self.send_media_message(
            to_user_id=to_user_id,
            baseurl=baseurl,
            bot_token=bot_token,
            context_token=context_token,
            item=item,
        )

    async def _post_sendmessage_with_retry(
        self,
        url: str,
        headers: dict,
        body: dict,
        to_user_id: str,
        log_label: str = "sendmessage",
        max_attempts: int = 5,
    ) -> bool:
        """Helper to POST to /sendmessage with exponential backoff + jitter retry strategy.

        - Retries on network errors (httpx.RequestError) and 5xx HTTP status errors.
        - Fails fast on 401/403 (clears auth state) and 4xx status errors.
        - Retries up to max_attempts with exponential backoff + random jitter.
        """
        attempt = 0
        base_delay = 1.0
        while attempt < max_attempts:
            attempt += 1
            try:
                response = await self.http_client.post(
                    url, headers=headers, json=body
                )
                response.raise_for_status()
                data = response.json()

                ret = data.get("ret", -1)
                message_id = data.get("message_id", "")
                if not ilink_delivery_accepted(ret, message_id):
                    logger.warning(
                        "%s failed ret=%s for %s (attempt %d/%d): %s",
                        log_label, ret, to_user_id, attempt, max_attempts, data,
                    )
                    if attempt < max_attempts:
                        delay = min(base_delay * (2 ** (attempt - 1)) * random.uniform(0.7, 1.3), 30.0)
                        await asyncio.sleep(delay)
                        continue
                    return False

                if ret != 0:
                    # Documented API quirk — delivered despite non-zero ret
                    logger.debug(
                        "%s ret=%s with message_id=%s (iLink quirk, delivered)",
                        log_label, ret, message_id,
                    )
                logger.info(
                    "%s sent to %s (attempt %d/%d): msg_id=%s ret=%s",
                    log_label, to_user_id, attempt, max_attempts,
                    message_id, ret,
                )
                return True

            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    logger.warning(
                        "Bot token rejected during %s (401/403), clearing state", log_label
                    )
                    self.state.clear()
                    return False
                elif e.response.status_code >= 500:
                    logger.warning(
                        "HTTP 5xx server error during %s (attempt %d/%d): %s",
                        log_label, attempt, max_attempts, e,
                    )
                else:
                    logger.warning(
                        "HTTP client error %s during %s: %s",
                        e.response.status_code, log_label, e,
                    )
                    return False

            except httpx.RequestError as e:
                logger.warning(
                    "Network error during %s (attempt %d/%d): %s",
                    log_label, attempt, max_attempts, e,
                )

            except Exception as e:
                logger.exception(
                    "Unexpected error during %s (attempt %d/%d): %s",
                    log_label, attempt, max_attempts, e,
                )

            if attempt < max_attempts:
                delay = min(base_delay * (2 ** (attempt - 1)) * random.uniform(0.7, 1.3), 30.0)
                logger.info("Retrying %s in %.2fs...", log_label, delay)
                await asyncio.sleep(delay)

        logger.error(
            "Failed to send %s to %s after %d attempts",
            log_label, to_user_id, max_attempts,
        )
        return False

    async def send_media_message(
        self,
        to_user_id: str,
        baseurl: str,
        bot_token: str,
        context_token: str,
        item: dict,
    ) -> bool:
        """Send a media message (image/file/voice/video) via iLink sendmessage."""
        url = f"{baseurl}/ilink/bot/sendmessage"
        headers = _common_headers(has_auth=True, bot_token=bot_token)
        client_id = f"wechatbridge-{uuid.uuid4().hex[:16]}"
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [item],
            },
            "base_info": _build_base_info(),
        }
        return await self._post_sendmessage_with_retry(
            url=url, headers=headers, body=body, to_user_id=to_user_id, log_label="send_media_message"
        )

    async def send_message(
        self,
        to_user_id: str,
        text: str,
        context_token: str,
        baseurl: str,
        bot_token: str,
    ) -> bool:
        """Send a text message reply via iLink with automatic retries.

        Long texts are split into chunks (config.message_chunk_chars) so WeChat
        length limits do not silently drop the whole reply.
        """
        from .runner_common import split_message_chunks

        chunks = split_message_chunks(text, config.message_chunk_chars)
        url = f"{baseurl}/ilink/bot/sendmessage"
        headers = _common_headers(has_auth=True, bot_token=bot_token)
        all_ok = True
        for idx, chunk in enumerate(chunks):
            client_id = f"wechatbridge-{uuid.uuid4().hex[:16]}"
            body = {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [
                        {"type": 1, "text_item": {"text": chunk}}
                    ],
                },
                "base_info": _build_base_info(),
            }
            label = "send_message" if len(chunks) == 1 else f"send_message[{idx + 1}/{len(chunks)}]"
            ok = await self._post_sendmessage_with_retry(
                url=url, headers=headers, body=body, to_user_id=to_user_id, log_label=label
            )
            if not ok:
                all_ok = False
        return all_ok


    # ---- Image download and decryption ----

    async def download_and_decrypt_media(self, media: dict) -> bytes:
        """Download a CDN-encrypted media file (image or file), AES-128-ECB decrypt it,
        and return plaintext bytes.

        This method handles both image_item.media (type=2) and file_item.media
        (type=4) — the media dict structure is identical for all encrypted media
        types in iLink.

        Args:
            media: Media dict from iLink message item_list[].image_item.media
                   or file_item.media. Expected keys: encrypt_query_param,
                   aes_key, full_url (optional).

        Returns:
            Decrypted plaintext bytes (image or file content).

        Raises:
            ValueError: Missing required fields or invalid AES key.
            httpx.HTTPError: Download failure.
        """
        encrypt_query_param = media.get("encrypt_query_param", "")
        aes_key_b64 = media.get("aes_key", "")
        full_url = media.get("full_url", "")

        if not aes_key_b64 or (not encrypt_query_param and not full_url):
            raise ValueError(
                "media 缺少 aes_key 或 encrypt_query_param/full_url"
            )

        # Parse AES key
        key = _parse_aes_key(aes_key_b64)

        # Build download URL
        if full_url:
            if _is_allowed_media_url(full_url):
                url = full_url
            elif encrypt_query_param:
                logger.warning(
                    "CDN full_url %s 不在白名单内，回退到构造URL", full_url[:120]
                )
                url = f"{config.cdn_base_url}/download?encrypted_query_param={quote(encrypt_query_param)}"
            else:
                raise ValueError(f"拒绝非微信 CDN 下载地址: {full_url[:120]}")
        else:
            url = f"{config.cdn_base_url}/download?encrypted_query_param={quote(encrypt_query_param)}"

        if not _is_allowed_media_url(url):
            raise ValueError(f"拒绝非微信 CDN 下载地址: {url[:120]}")

        # Stream download; abort as soon as size exceeds cap (even without Content-Length)
        max_in = max(int(config.max_inbound_file_bytes), 1)
        logger.info("Downloading encrypted media from CDN: %s", url[:80])
        t0 = time.time()
        buf = bytearray()
        async with self.http_client.stream("GET", url, timeout=30.0) as resp:
            resp.raise_for_status()
            cl = resp.headers.get("content-length")
            if cl is not None:
                try:
                    declared = int(cl)
                except ValueError:
                    declared = None  # 非法 Content-Length 头忽略，由流式上限兜底
                if declared is not None and declared > max_in:
                    raise ValueError(
                        f"入站文件过大（Content-Length {cl} > {max_in} 字节）"
                    )
            async for piece in resp.aiter_bytes():
                if not piece:
                    continue
                if len(buf) + len(piece) > max_in:
                    raise ValueError(
                        f"入站文件过大（>{max_in} 字节，已中止下载）"
                    )
                buf.extend(piece)
        encrypted = bytes(buf)
        if len(encrypted) > max_in:
            raise ValueError(
                f"入站文件过大（{len(encrypted)} > {max_in} 字节）"
            )
        elapsed = time.time() - t0
        logger.debug("CDN download: %d bytes in %.3fs", len(encrypted), elapsed)

        # AES-128-ECB decrypt
        plaintext = _decrypt_aes_ecb(encrypted, key)
        if len(plaintext) > max_in:
            raise ValueError(
                f"入站解密后文件过大（{len(plaintext)} > {max_in} 字节）"
            )
        logger.info("Media decrypted: %d bytes -> %d bytes", len(encrypted), len(plaintext))
        return plaintext
