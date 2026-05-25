"""iLink protocol client — text/file messaging, long-polling, typing indicator."""

import base64
import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

WECHAT_ILINK_BASE = "https://ilinkai.weixin.qq.com"
WECHAT_MAX_LENGTH = 2000  # Max chars per WeChat message (conservative)
LONG_POLL_TIMEOUT = 35  # Seconds for getUpdates long-poll
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

logger = logging.getLogger(__name__)


# ── Exceptions ──────────────────────────────────────────────────────────────


class WeChatAuthError(Exception):
    """Token expired or invalid — need to re-authenticate."""


class WeChatAPIError(Exception):
    """API returned a non-zero ret code."""


# ── AES-128-ECB encryption ──────────────────────────────────────────────────


def aes_128_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB encrypt *data* with PKCS7 padding."""
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len] * pad_len)
    return encryptor.update(padded) + encryptor.finalize()


# ── Config ──────────────────────────────────────────────────────────────────


@dataclass
class WeChatConfig:
    """Runtime configuration for a WeChat bot account."""

    bot_token: str
    """Bearer token obtained from QR login."""

    owner_wxid: str = ""
    """Auto-detected from the first incoming message."""

    context_tokens: dict[str, str] = field(default_factory=dict)
    """Per-sender context_token mapping — {wxid: token}."""

    get_updates_buf: str = ""
    """Cursor for incremental message sync (returned by getUpdates)."""


# ── Client ──────────────────────────────────────────────────────────────────


class WeChatClient:
    """iLink protocol client.

    Implements ``send_message(chat_id, text)`` so it can be used as the
    ``bot`` parameter in ``run_agent()``.
    """

    def __init__(self, config: WeChatConfig):
        self._config = config
        self._http = httpx.AsyncClient(timeout=15)

    # ── Internal helpers ────────────────────────────────────────────────

    def _base_info(self) -> dict:
        return {"channel_version": "0.4.11", "bot_agent": "Cyrene/0.4.11"}

    def _build_headers(self) -> dict[str, str]:
        uint32 = secrets.randbits(32)
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.bot_token}",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(str(uint32).encode()).decode(),
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": "1",
        }

    async def _post(self, endpoint: str, data: dict, timeout: int = 15) -> dict:
        """POST to an iLink endpoint with auth headers and base_info.

        Raises ``WeChatAuthError`` on auth failures (ret -14/401/403).
        Raises ``WeChatAPIError`` on other non-zero ret codes.
        Returns the parsed JSON dict on success.
        """
        resp = await self._http.post(
            f"{WECHAT_ILINK_BASE}/ilink/bot/{endpoint}",
            json={**data, "base_info": self._base_info()},
            headers=self._build_headers(),
            timeout=timeout,
        )
        result = resp.json()
        ret = result.get("ret", 0)

        if ret in (-14, 401, 403):
            raise WeChatAuthError(
                f"Token expired or invalid (ret={ret}): {result.get('errmsg', '')}"
            )
        if ret != 0:
            logger.warning("WeChat API ret=%s for %s: %s", ret, endpoint, result.get("errmsg", ""))
            raise WeChatAPIError(f"API error ret={ret}: {result.get('errmsg', '')}")

        return result

    # ── Text messaging ──────────────────────────────────────────────────

    async def send_message(self, chat_id: str, text: str) -> bool:
        """Send a text message to *chat_id* (a WeChat wxid)."""
        ctx = self._config.context_tokens.get(chat_id, "")
        try:
            await self._post("sendmessage", {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": chat_id,
                    "client_id": f"cyrene-{uuid.uuid4().hex[:16]}",
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                    "context_token": ctx,
                },
            })
            return True
        except WeChatAPIError:
            logger.exception("Failed to send text message to %s", chat_id)
            return False

    # ── File / image sending (CDN upload + sendMessage) ────────────────

    async def send_file(self, chat_id: str, filepath: str, filename: str = "") -> bool:
        """Encrypt and upload a file to CDN, then send it as a WeChat message.

        Supports images (type 2) and arbitrary files (type 4).
        Falls back to a text notice on failure.

        Returns ``True`` if the file was uploaded and sent successfully,
        ``False`` if a fallback text notice was sent instead.
        """
        if not filename:
            filename = Path(filepath).name

        raw = Path(filepath).read_bytes()
        if len(raw) > MAX_FILE_SIZE:
            await self.send_message(chat_id, "文件过大（超过 50MB 限制）")
            return False

        # `getuploadurl.media_type` differs from `sendmessage.item_list[*].type`:
        # IMAGE=1, VIDEO=2, FILE=3, VOICE=4.
        ext = Path(filepath).suffix.lower()
        is_image = ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
        media_type = 1 if is_image else 3

        # Encrypt
        aes_key = secrets.token_bytes(16)
        enc_data = aes_128_ecb_encrypt(raw, aes_key)
        raw_md5 = hashlib.md5(raw).hexdigest()
        aes_key_hex = aes_key.hex()
        image_aes_key_b64 = base64.b64encode(aes_key).decode()
        file_aes_key_b64 = base64.b64encode(aes_key_hex.encode()).decode()
        filekey = f"{uuid.uuid4().hex}{ext}"

        # Get pre-signed upload URL
        try:
            upload = await self._post("getuploadurl", {
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": chat_id,
                "rawsize": len(raw),
                "rawfilemd5": raw_md5,
                "filesize": len(enc_data),
                "aeskey": aes_key_hex,
                "no_need_thumb": True,
            }, timeout=30)
        except Exception:
            logger.exception("getUploadUrl failed for %s", filepath)
            await self.send_message(chat_id, "文件上传失败，无法获取上传地址")
            return False

        # Log upload response for debugging
        logger.info("getUploadUrl: upload_full_url=%s upload_param_type=%s upload_keys=%s",
                    upload.get("upload_full_url", "")[:120],
                    type(upload.get("upload_param")).__name__,
                    list(upload.keys()))

        # Prepare CDN upload
        cdn_download_url = upload.get("upload_full_url", "")
        raw_param = upload.get("upload_param")  # str = encrypt_query_param

        # upload_param from getUploadUrl is the encrypted_query_param value
        encrypt_query_param = raw_param if isinstance(raw_param, str) else str(raw_param or "")
        # CDN upload endpoint: https://novac2c.cdn.weixin.qq.com/c2c/upload
        cdn_upload_url = f"https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param={encrypt_query_param}&filekey={filekey}"
        upload_method = "POST"
        upload_headers = {"Content-Type": "application/octet-stream"}
        logger.info("CDN upload url=%s method=%s filekey=%s", cdn_upload_url[:120], upload_method, filekey)

        # Upload encrypted file to CDN (60s timeout, bypass proxy for CDN)
        try:
            timeout = httpx.Timeout(60.0, connect=30.0)
            async with httpx.AsyncClient(timeout=timeout) as c:
                if upload_method == "POST":
                    r = await c.post(cdn_upload_url, content=enc_data, headers=upload_headers)
                else:
                    r = await c.put(cdn_upload_url, content=enc_data, headers=upload_headers)
                # Log response for debugging even on error
                logger.debug("CDN upload response: status=%s headers=%s body=%s",
                            r.status_code, dict(r.headers), r.text[:500])
                logger.info("CDN upload response: status=%s headers=%s", r.status_code, dict(r.headers))
                r.raise_for_status()
                encrypt_query_param = r.headers.get("x-encrypted-param", "")
                if not encrypt_query_param:
                    # Fallback: use the original upload_param from getUploadUrl
                    encrypt_query_param = str(upload.get("upload_param", ""))
                    logger.warning("CDN upload missing x-encrypted-param header, using upload_param fallback")
        except httpx.HTTPStatusError:
            logger.exception("CDN upload HTTP error for %s (status=%s url=%s response=%s)",
                           filepath, r.status_code, cdn_upload_url[:80], r.text[:300])
            await self.send_message(chat_id, "文件上传失败，CDN 上传出错")
            return False
        except Exception:
            logger.exception("CDN upload failed for %s (url=%s)", filepath, cdn_upload_url[:80])
            await self.send_message(chat_id, "文件上传失败，CDN 上传出错")
            return False

        if not cdn_download_url:
            # Construct download URL from CDN base + encrypt_query_param
            cdn_download_url = f"https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param={encrypt_query_param}"
            logger.info("Constructed download URL from fallback: %s", cdn_download_url[:80])

        # Build the media reference
        logger.info("Media reference: encrypt_query_param=%s cdn_download_url=%s",
                     encrypt_query_param[:40] if encrypt_query_param else "(empty)",
                     cdn_download_url[:80] if cdn_download_url else "(empty)")
        media = {
            "encrypt_query_param": encrypt_query_param,
            "aes_key": image_aes_key_b64 if is_image else file_aes_key_b64,
            "encrypt_type": 1,
        }

        if is_image:
            item = {
                "type": 2,
                "image_item": {
                    "media": {**media, "full_url": cdn_download_url},
                    "url": cdn_download_url,
                    "aeskey": aes_key_hex,
                    "file_name": filename,
                },
            }
        else:
            item = {
                "type": 4,
                "file_item": {
                    "media": {**media, "full_url": cdn_download_url},
                    "file_name": filename,
                    "md5": raw_md5,
                    "len": str(len(raw)),
                },
            }

        ctx = self._config.context_tokens.get(chat_id, "")
        try:
            send_resp = await self._post("sendmessage", {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": chat_id,
                    "client_id": f"cyrene-{uuid.uuid4().hex[:16]}",
                    "message_type": 2,
                    "message_state": 2,
                    "item_list": [item],
                    "context_token": ctx,
                },
            })
            logger.info("sendMessage file item response: %s", send_resp)
        except Exception:
            logger.exception(
                "Failed to send file message to %s (ctx_present=%s item_type=%s media_type=%s filename=%s)",
                chat_id,
                bool(ctx),
                item.get("type"),
                media_type,
                filename,
            )
            await self.send_message(chat_id, "文件发送失败")
            return False

        return True

    async def send_image(self, chat_id: str, filepath: str) -> None:
        """Convenience: send an image file."""
        await self.send_file(chat_id, filepath)

    # ── Long-poll message retrieval ─────────────────────────────────────

    async def get_updates(self) -> list[dict]:
        """Long-poll for new messages.

        Returns a list of raw message dicts (``msgs`` from the API response).
        Returns an empty list on a normal timeout.
        """
        try:
            result = await self._post(
                "getupdates",
                {"get_updates_buf": self._config.get_updates_buf},
                timeout=LONG_POLL_TIMEOUT + 5,
            )
            self._config.get_updates_buf = result.get("get_updates_buf", "")
            return result.get("msgs", [])
        except httpx.TimeoutException:
            return []

    # ── Typing indicator ─────────────────────────────────────────────────

    async def send_typing(self, user_id: str) -> bool:
        """Show a "typing…" indicator for *user_id*.

        Best-effort — returns ``False`` on failure.
        """
        try:
            cfg = await self._post("getconfig", {"ilink_user_id": user_id}, timeout=10)
            ticket = cfg.get("typing_ticket", "")
            if ticket:
                await self._post(
                    "sendtyping",
                    {"ilink_user_id": user_id, "typing_ticket": ticket, "status": 1},
                    timeout=10,
                )
            return True
        except Exception:
            return False
