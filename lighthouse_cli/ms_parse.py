"""HTML/JSON extraction helpers for Microsoft login pages."""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup


def _extract_balanced_json_object(text: str, start: int) -> str | None:
    """Return a ``{...}`` JSON object substring starting at ``start`` (must be ``{``)."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def _extract_config_json(html: str) -> dict[str, Any] | None:
    """Extract the ``$Config`` JavaScript object from Microsoft's login page.

    Uses brace-balanced parsing because ``$Config`` contains deeply nested JSON
    (non-greedy regex stops at the first ``}`` and drops ``sFT`` / ``sCtx``).
    """
    pos = 0
    while True:
        m = re.search(r"\$Config\s*=", html[pos:])
        if not m:
            break
        match_end = pos + m.end()
        brace = html.find("{", match_end)
        if brace < 0:
            pos = match_end
            continue
        blob = _extract_balanced_json_object(html, brace)
        if blob:
            try:
                parsed = json.loads(blob)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        pos = match_end
    return None


def _extract_error_code_and_msg(html: str) -> tuple[int | None, str | None]:
    """Extract Microsoft error code and message from an error page.

    Looks for patterns like ``serverError\":"50126"`` or ``sErrTxt\":"..."``
    in the page's JavaScript or HTML.
    """
    # Try serverError in a script -- "serverError": "50126" (JSON-style)
    m = re.search(r'''serverError["']?\s*:\s*["']([0-9]+)["']''', html)
    if not m:
        # Try without the key quote: serverError": "50126"
        m = re.search(r'serverError["\'][^:]*:\s*["\']([0-9]+)["\']', html)
    code = int(m.group(1)) if m else None
    msg: str | None = None

    page_cfg = _extract_config_json(html) or {}
    cfg_code = page_cfg.get("sErrorCode") or page_cfg.get("iErrorCode")
    if cfg_code and str(cfg_code) not in ("", "0", "50058"):
        try:
            code = int(str(cfg_code))
        except ValueError:
            pass
    if page_cfg.get("pgid") == "ConvergedError":
        msg = msg or str(page_cfg.get("strServiceExceptionMessage") or page_cfg.get("strMainMessage") or "")

    # ConvergedTFA / KMSI pages often embed error.aspx?err=504 in JS -- not a real failure.
    if code == 504 and "error.aspx" in html.lower() and (
        "ConvergedTFA" in html or page_cfg.get("pgid") in ("ConvergedTFA", "CmsiInterrupt")
    ):
        code = None

    # Try sErrTxt -- flexible pattern for JSON key
    m = re.search(r'''sErrTxt["']?\s*:\s*["'](.+?)["']''', html, re.DOTALL)
    msg = m.group(1) if m else msg

    # Fallback: look for <div class="error"> text (case-insensitive)
    if not msg:
        soup = BeautifulSoup(html, "html.parser")
        for err_div in soup.find_all(
            lambda tag: tag.name == "div"
            and any(
                "error" in (
                    " ".join(tag.get(attr, [])) if attr == "class"
                    else (tag.get(attr, "") or "")
                ).lower()
                for attr in ("id", "class")
            )
        ):
            text = err_div.get_text(strip=True)
            if text:
                msg = text
                break

    return code, msg
