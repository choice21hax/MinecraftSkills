"""Secret redaction for anything that leaves the process (stdout, logs, reports, subprocess output).

Known secrets are registered at runtime as they are loaded from the keyring or environment, so the
exact values are scrubbed. Generic token shapes are scrubbed as a second line of defence.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
_known: set[str] = set()

_PATTERNS = [
    # Modrinth PATs (mrp_...), generic bearer tokens, CurseForge keys ($2a$10$... bcrypt-like strings)
    re.compile(r"mrp_[A-Za-z0-9]{20,}"),
    re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{40,}"),
    re.compile(r"(?i)(x-api-key|authorization)(\s*[:=]\s*)(\"?)[^\s\"',}]+"),
]


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _known.add(value)


def redact(text: str) -> str:
    for secret in sorted(_known, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pat in _PATTERNS:
        if pat.groups >= 3:
            text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}", text)
        else:
            text = pat.sub(REDACTED, text)
    return text


def redact_obj[T](obj: T) -> T:
    """Recursively redact strings inside JSON-like structures. Keys named like secrets are blanked."""
    if isinstance(obj, str):
        return redact(obj)  # type: ignore[return-value]
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in {"api_key", "apikey", "token", "secret", "password"} and v:
                out[k] = REDACTED
            else:
                out[k] = redact_obj(v)
        return out  # type: ignore[return-value]
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]  # type: ignore[return-value]
    if isinstance(obj, tuple):
        return tuple(redact_obj(v) for v in obj)  # type: ignore[return-value]
    return obj
