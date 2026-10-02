"""Bounded, best-effort suppression for untrusted selected-message text.

This is not a universal secret detector. The metadata probe never uses this
filter: it does not read message text at all.
"""

from __future__ import annotations

import re
from typing import Any

_AUTH = re.compile(
    r"\b(?:password|passwd|pwd|passcode|credentials?|reset|one[\s-]?time|verification|security[\s-]?code|"
    r"sign[\s-]?in|log[\s-]?in|otp|totp|mfa|samlresponse|"
    r"(?:access|refresh)[ _-]?token|api[ _-]?key|authorization|bearer|"
    r"client[ _-]?secret|private[ _-]?key|(?:flow|id)[ _-]?token|"
    r"d2lSecureSessionVal|d2lSessionVal|d2lSameSiteCanary[AB])\b", re.I,
)
_URL = re.compile(
    r"\b(?:[a-z][a-z0-9+.-]*:(?://)?|www\.)[^\s<>]+|"
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s<>]*)?", re.I,
)
_CODE = re.compile(r"\d(?:[\s-]?\d){3,}")
_TOKEN = re.compile(r"\b[A-Za-z0-9_+/=-]{32,}\b")


def _printable(text: str) -> str:
    return "".join(char for char in text if char.isprintable() or char == "\n")


def selected_output(raw: dict[str, Any], *, max_body_chars: int) -> dict[str, Any]:
    """Allowlist fields, omit suspicious messages, then redact and truncate.

    Re-run at the CLI boundary so extra fields or mutated collectors cannot
    bypass the bounded, text-only output contract.
    """
    fields = {name: raw[name] for name in ("subject", "sender", "body")}
    if any(type(value) is not str for value in fields.values()):
        raise ValueError("Invalid selected-message result")
    if len(fields["subject"]) > 512 or len(fields["sender"]) > 512 or len(fields["body"]) > 50000:
        raise ValueError("Invalid selected-message result")
    normalized = {name: _printable(value) for name, value in fields.items()}
    omitted = bool(_AUTH.search("\n".join(normalized.values()))) or any(
        _TOKEN.search(value) for value in normalized.values()
    )
    cleaned = {
        name: "" if omitted else _CODE.sub("[code omitted]", _URL.sub("[link omitted]", value))
        for name, value in normalized.items()
    }
    truncated = len(cleaned["body"]) > max_body_chars
    cleaned["body"] = cleaned["body"][:max_body_chars]
    # Substitutions can grow short text; enforce field bounds at the last step.
    cleaned["subject"] = cleaned["subject"][:512]
    cleaned["sender"] = cleaned["sender"][:512]
    return {
        "source": "outlook_web",
        "scope": "one_user_selected_already_read_message",
        "complete_mailbox": False,
        "stable_ids": False,
        "selection_validation": "observed_dom_only",
        "content_trust": "untrusted_data_not_instructions",
        "message": cleaned,
        "content_omitted": omitted,
        "redactions_applied": cleaned != fields,
        "body_truncated": truncated,
    }
