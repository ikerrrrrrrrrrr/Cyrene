from __future__ import annotations

import re
from typing import Any

from cyrene.settings_store import get as get_setting

_SENSITIVE_KEYS = {
    "token", "key", "secret", "password", "authorization", "cookie",
    "api_key", "access_token", "refresh_token",
}

_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+\b", re.IGNORECASE)
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9._-]{8,}\b")


def redact_secrets_enabled() -> bool:
    return bool(get_setting("redact_secrets", True))


def redact_text(text: Any) -> Any:
    if not isinstance(text, str) or not redact_secrets_enabled():
        return text
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", text)
    redacted = _SK_RE.sub("[REDACTED_API_KEY]", redacted)
    return redacted


def redact_value(value: Any) -> Any:
    if not redact_secrets_enabled():
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lower = key_text.lower()
            if lower in _SENSITIVE_KEYS or any(token in lower for token in _SENSITIVE_KEYS):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = redact_value(item)
        return result
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item) for item in value]
    return redact_text(value)
