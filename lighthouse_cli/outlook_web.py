"""Metadata-only Outlook capability probe in a temporary Playwright browser.

This adapter never imports browser sessions, reads cookies/storage, or operates
mail actions. The user completes sign-in in the new window. Only structural
metadata for currently rendered message-list rows is returned; raw row text is
never read or returned. Virtualization prevents completeness or stable-ID claims.
"""

from __future__ import annotations

import re
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

OUTLOOK_URL = "https://outlook.office.com/mail/"
_MAIL_HOSTS = frozenset({"outlook.office.com", "outlook.office365.com"})
_LOGIN_HOSTS = frozenset({"login.microsoftonline.com", "login.live.com"})
_MESSAGE_LIST = re.compile(r"^Message list No conversations selected$")
_ERRORS = {
    "search_not_supported": "Outlook search is not supported yet because result freshness cannot be verified. No browser was opened.",
    "invalid_options": "Invalid Outlook options. See lighthouse outlook probe --help.",
    "dependency_missing": "Playwright is required. Install lighthouse-cli with the auth extra, then run playwright install chromium.",
    "browser_unavailable": "The temporary browser could not start. Install Chromium with playwright install chromium and use a graphical session.",
    "browser_closed": "The temporary browser was closed before the Outlook read completed.",
    "browser_error": "The temporary Outlook browser could not be read. No results were returned.",
    "navigation_timeout": "Outlook did not load in time. Check connectivity before trying again.",
    "login_timeout": "Sign-in did not finish in time. Complete sign-in and MFA yourself in the temporary window during a new invocation. No session was saved.",
    "view_timeout": "Outlook's unselected message list did not become available in time. No results were returned.",
    "empty_or_loading": "No message rows became available. The view may be empty, loading, or unsupported; no mailbox completeness is inferred.",
    "unsupported_layout": "Outlook's message-list layout is not supported. No results were returned.",
    "view_changed": "Outlook's message list changed while being read. No results were returned.",
    "admin_approval_required": "Microsoft requires administrator approval. Ask IT to review the requested application permissions; the adapter stopped.",
    "consent_required": "Microsoft requires application consent. Review the request through your organization's normal sign-in flow; contact IT if administrator approval is required. The adapter stopped.",
    "consent_incomplete": "Microsoft reports incomplete consent. If administrator review was requested, wait for that review. The adapter stopped.",
    "access_blocked": "Microsoft blocked this sign-in under an organizational access or security policy. Contact IT to review the requirement; the adapter stopped.",
    "registration_blocked": "Microsoft blocked MFA registration for this sign-in. Use the organization's allowed registration process or contact IT; the adapter stopped.",
}
_POLICY_CODES = {
    "admin_approval_required": "90094",
    "consent_required": "65001",
    "consent_incomplete": "65004",
    "access_blocked": "50131|53000|53001|53002|53003",
    "registration_blocked": "53004",
}


class OutlookWebError(Exception):
    """Fixed diagnostic only; never attach upstream text, URLs, or page data."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERRORS else "browser_error"
        super().__init__(_ERRORS[self.code])


def _host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return ""
        return parsed.hostname or ""
    except ValueError:
        return ""


def _raise_policy_block(page: Page) -> None:
    """Inspect fixed login-page labels only, never mailbox text or auth fields."""
    if _host(page.url) not in _LOGIN_HOSTS:
        return
    for category, codes in _POLICY_CODES.items():
        if page.get_by_text(re.compile(rf"\b(?:AADSTS|Error\s+Code\s*:\s*)(?:{codes})\b", re.I)).first.is_visible():
            raise OutlookWebError(category)
    if page.get_by_text(re.compile(r"^(Need admin approval|Admin approval required|Approval required)$", re.I)).first.is_visible():
        raise OutlookWebError("admin_approval_required")
    if page.get_by_text(re.compile(r"^You can['’]t get there from here$", re.I)).first.is_visible():
        raise OutlookWebError("access_blocked")


def _wait_for_message_list(page: Page, *, timeout: int) -> Locator:
    deadline = time.monotonic() + timeout
    saw_mailbox = False
    while time.monotonic() < deadline:
        if page.is_closed():
            raise OutlookWebError("browser_closed")
        _raise_policy_block(page)
        if _host(page.url) in _MAIL_HOSTS:
            saw_mailbox = True
            message_list = page.get_by_role("listbox", name=_MESSAGE_LIST)
            count = message_list.count()
            if count > 1:
                raise OutlookWebError("unsupported_layout")
            if count == 1 and message_list.is_visible():
                return message_list
        page.wait_for_timeout(250)
    raise OutlookWebError("view_timeout" if saw_mailbox else "login_timeout")


def _row_snapshot(row: Locator) -> tuple[bool, str]:
    """Read structural metadata only, never raw labels or message previews."""
    if not row.is_visible():
        raise OutlookWebError("view_changed")
    if row.get_attribute("aria-selected") != "false":
        raise OutlookWebError("unsupported_layout")
    dom_id = row.get_attribute("id")
    if not dom_id or len(dom_id) > 512:
        raise OutlookWebError("unsupported_layout")
    unread = row.locator('button[title="Mark as read"]').count() > 0
    read = row.locator('button[title="Mark as unread"]').count() > 0
    if unread == read:
        raise OutlookWebError("unsupported_layout")
    return unread, dom_id


def _read_rows(message_list: Locator, *, limit: int) -> dict[str, Any]:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    rows = message_list.get_by_role("option")
    try:
        rows.first.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeoutError:
        raise OutlookWebError("empty_or_loading") from None
    count = rows.count()
    if count == 0:
        raise OutlookWebError("view_changed")
    snapshots = [_row_snapshot(rows.nth(index)) for index in range(min(count, limit))]
    if len({dom_id for _unread, dom_id in snapshots}) != len(snapshots):
        raise OutlookWebError("view_changed")
    if rows.count() != count or any(
        _row_snapshot(rows.nth(index)) != snapshot
        for index, snapshot in enumerate(snapshots)
    ):
        raise OutlookWebError("view_changed")
    result = [
        {"position": index + 1, "rendered_text": "", "unread": unread, "text_omitted": True}
        for index, (unread, _dom_id) in enumerate(snapshots)
    ]
    return {
        "source": "outlook_web",
        "coverage": "rendered_rows_only",
        "complete_mailbox": False,
        "stable_ids": False,
        "text_included": False,
        "rows": result,
        "limit_reached": len(result) >= limit,
    }


def _validate_options(search: str | None, limit: int, login_timeout: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise OutlookWebError("invalid_options")
    if type(login_timeout) is not int or not 30 <= login_timeout <= 600:
        raise OutlookWebError("invalid_options")
    if search is not None and (
        not isinstance(search, str) or not search.strip() or len(search) > 512 or not search.isprintable()
    ):
        raise OutlookWebError("invalid_options")
    if search is not None:
        raise OutlookWebError("search_not_supported")


def collect_outlook_rows(
    *, search: str | None = None, limit: int = 25, login_timeout: int = 180,
) -> dict[str, Any]:
    """Launch an ephemeral headed browser; wait for the user, then read rows.

    No browser/profile connection argument is accepted. No persistent context,
    storage state, credentials, download handling, or mailbox action is used.
    """
    _validate_options(search, limit, login_timeout)
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise OutlookWebError("dependency_missing") from None

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=False)
            except Exception:
                raise OutlookWebError("browser_unavailable") from None
            try:
                context = browser.new_context(locale="en-US", accept_downloads=False)
                try:
                    page = context.new_page()
                    page.set_default_timeout(5000)
                    try:
                        page.goto(OUTLOOK_URL, wait_until="domcontentloaded", timeout=30000)
                    except PlaywrightTimeoutError:
                        raise OutlookWebError("navigation_timeout") from None
                    message_list = _wait_for_message_list(page, timeout=login_timeout)
                    result = _read_rows(message_list, limit=limit)
                    result["scope"] = "current_view"
                    return result
                finally:
                    with suppress(Exception):
                        context.close()
            finally:
                with suppress(Exception):
                    browser.close()
    except OutlookWebError:
        raise
    except Exception:
        raise OutlookWebError("browser_error") from None
