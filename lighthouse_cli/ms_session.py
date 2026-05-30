"""Cookie and session utilities for Microsoft SSO."""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import requests


def _export_session_cookies(session: requests.Session) -> list[dict[str, str]]:
    return [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain or "",
            "path": c.path or "/",
        }
        for c in session.cookies
    ]


def _import_session_cookies(session: requests.Session, cookies: list[dict[str, str]]) -> None:
    for cookie in cookies:
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain") or "",
            path=cookie.get("path") or "/",
        )


def _prune_stale_esctx_cookies(session: requests.Session) -> None:
    """Keep a single ``esctx-*`` cookie; stale values break password POST."""
    named = [c for c in session.cookies if c.name.startswith("esctx-")]
    if len(named) <= 1:
        return
    for cookie in named[:-1]:
        session.cookies.clear(cookie.domain, cookie.path, cookie.name)


def _absolute_url(base_url: str, path: str) -> str:
    """Resolve Microsoft login URLs (often tenant-relative paths)."""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if path.startswith("/"):
        return f"{origin}{path}"
    return urljoin(f"{origin}/", path)


def _tenant_id_from_ms_url(ms_url: str) -> str:
    """Extract Azure AD tenant id from a Microsoft login URL."""
    m = re.search(r"login\.microsoftonline\.com/([0-9a-f-]{36})/", ms_url, re.IGNORECASE)
    return m.group(1) if m else "common"


def _mask_phone_hint(data: str) -> str:
    digits = re.sub(r"\D", "", data)
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    if data:
        return data
    return "your phone"
