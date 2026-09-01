"""Cookie and session utilities for Microsoft SSO."""

from __future__ import annotations

import re
from collections.abc import Collection
from urllib.parse import urljoin, urlparse

import requests


def _url_origin(url: str) -> tuple[str, int] | None:
    """Return a normalized HTTPS origin, or ``None`` for an unsafe URL.

    Login pages and redirects are untrusted input.  In particular, checking a
    hostname with ``in`` is not sufficient: ``login.microsoftonline.com.evil``
    and userinfo URLs can both pass a substring check.  The caller supplies the
    exact host allowlist after this parser has rejected userinfo, non-default
    ports, protocol-relative URLs, control characters, and fragments.
    """
    if not isinstance(url, str) or not url or url != url.strip():
        return None
    if url.startswith("//") or "\\" in url or any(ord(ch) < 0x20 for ch in url):
        return None
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or hostname.endswith(".")
        or parsed.netloc.endswith(":")
        or parsed.fragment
        or (port is not None and port != 443)
    ):
        return None
    return hostname.lower(), 443


def _safe_absolute_url(
    base_url: str,
    candidate: str,
    allowed_hosts: Collection[str],
) -> str:
    """Resolve a URL and require an exact HTTPS host/origin allowlist.

    Relative paths are resolved against ``base_url``.  Absolute and
    protocol-relative candidates are never allowed to change the origin unless
    that exact origin is present in ``allowed_hosts``; protocol-relative forms
    are rejected even when their host would otherwise be allowed so callers do
    not accidentally broaden a relative-path policy.

    ``ValueError`` deliberately contains no candidate URL because callers may
    be handling an upstream value that includes a token or password.
    """
    if not isinstance(base_url, str) or not isinstance(candidate, str):
        raise ValueError("unsafe URL")
    if not base_url or not candidate:
        raise ValueError("unsafe URL")
    if candidate.startswith("//"):
        raise ValueError("unsafe URL")

    base_origin = _url_origin(base_url)
    if base_origin is None:
        raise ValueError("unsafe URL")

    raw_candidate = candidate
    try:
        parsed_candidate = urlparse(raw_candidate)
    except (TypeError, ValueError):
        raise ValueError("unsafe URL") from None

    # A candidate with a scheme or netloc is absolute; all other forms are
    # path/query/fragment references resolved on the already trusted base.
    if parsed_candidate.scheme or parsed_candidate.netloc:
        resolved = raw_candidate
    else:
        if "\\" in raw_candidate or any(ord(ch) < 0x20 for ch in raw_candidate):
            raise ValueError("unsafe URL")
        resolved = urljoin(base_url, raw_candidate)

    origin = _url_origin(resolved)
    if origin is None or origin[0] not in {str(host).lower() for host in allowed_hosts}:
        raise ValueError("unsafe URL")
    return resolved


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
