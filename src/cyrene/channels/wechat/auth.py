"""WeChat iLink protocol QR code authentication."""

import asyncio
import base64
import logging
import secrets
import time

import httpx

WECHAT_ILINK_BASE = "https://ilinkai.weixin.qq.com"

logger = logging.getLogger(__name__)


class WeChatAuthError(Exception):
    """WeChat authentication or token error."""


class WeChatAuth:
    """QR-code-based login for the WeChat iLink Bot API."""

    BASE = WECHAT_ILINK_BASE

    async def get_qr_code(self) -> tuple[str, str]:
        """Fetch a login QR code from the iLink API.

        Returns:
            (qrcode_id, qrcode_img_url): ``qrcode_id`` is used to poll login
            status; ``qrcode_img_url`` is a URL that can be rendered as a QR
            code image or a base64-encoded image.
        """
        uin = self._random_uin()
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.BASE}/ilink/bot/get_bot_qrcode",
                params={"bot_type": 3},
                headers={"X-WECHAT-UIN": uin},
            )
            data = resp.json()
            if data.get("ret") == 0:
                return data["qrcode"], data["qrcode_img_content"]
            raise WeChatAuthError(data.get("msg", "failed to get QR code"))

    async def poll_login(self, qrcode: str, timeout: int = 120) -> str | None:
        """Poll QR code status until confirmed or expired.

        Args:
            qrcode: The QR code identifier from ``get_qrcode``.
            timeout: Max seconds to poll before giving up.

        Returns:
            The ``bot_token`` on success, or ``None`` if the QR code expired
            or the timeout was reached.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            uin = self._random_uin()
            async with httpx.AsyncClient() as client:
                try:
                    resp = await client.get(
                        f"{self.BASE}/ilink/bot/get_qrcode_status",
                        params={"qrcode": qrcode},
                        headers={
                            "X-WECHAT-UIN": uin,
                            "iLink-App-ClientVersion": "1",
                        },
                        timeout=40,
                    )
                    data = resp.json()
                except Exception:
                    logger.debug("poll_login request failed, retrying")
                    await asyncio.sleep(2)
                    continue

            status = data.get("status")
            logger.debug("QR login status: %s", status)

            if status == "confirmed":
                return data["bot_token"]
            if status == "expired":
                return None

            await asyncio.sleep(3)

        return None

    @staticmethod
    def _random_uin() -> str:
        """Generate a random X-WECHAT-UIN header value."""
        return base64.b64encode(str(secrets.randbits(32)).encode()).decode()
