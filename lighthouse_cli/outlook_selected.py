"""Fail-closed reader for one manually selected, baseline already-read message.

DOM identity is only an in-memory guard, not a canonical Outlook message ID.
This module never clicks, changes read state, or reads authentication storage.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .outlook_content import selected_output
from .outlook_web import (
    _MAIL_HOSTS,
    OutlookWebError,
    _host,
    _raise_policy_block,
    collect_in_temporary_browser,
)

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

_LIST = re.compile(r"^Message list(?:\s|$)")
_BODY = 'div[data-test-id="mailMessageBodyContainer"]'
_UI = f':not({_BODY} *)'
_MAX_ROWS = 100
_SEND_BUTTON = re.compile(r"^Send(?:\s*\([^\n]*\))?$", re.I)


@dataclass(frozen=True)
class RowState:
    dom_id: str
    read: bool
    selected: bool


def _assert_no_compose(page: Page) -> None:
    """Reject the observed English compose/send UI without reading draft text.

    Disabled Send buttons count too. This is a conservative supported-layout
    guard, not a lock that can prevent the user's own input or mailbox changes.
    """
    if page.get_by_role("button", name=_SEND_BUTTON).count():
        raise OutlookWebError("compose_open")


@contextmanager
def _layout_stage(stage: str) -> Iterator[None]:
    """Add only a fixed stage label to unsupported-layout diagnostics."""
    try:
        yield
    except OutlookWebError as exc:
        if exc.code == "unsupported_layout" and exc.stage is None:
            raise OutlookWebError(exc.code, stage=stage) from None
        raise


def _one(locator: Locator) -> Locator:
    if locator.count() != 1 or not locator.is_visible():
        raise OutlookWebError("unsupported_layout")
    return locator


def _bounded_text(locator: Locator, limit: int) -> str:
    # Inspect only rendered DOM text. Never return HTML, URLs, attributes, or
    # an over-limit text value to the Python process.
    value = locator.evaluate(
        "(element, limit) => { const text = element.innerText; "
        "return typeof text === 'string' && text.length <= limit ? text : null; }",
        limit,
    )
    if type(value) is not str:
        raise OutlookWebError("content_too_large")
    return value


def _row_state(row: Locator) -> RowState:
    _one(row)
    dom_id = row.get_attribute("id")
    selected = row.get_attribute("aria-selected")
    if not dom_id or len(dom_id) > 512 or selected not in ("false", "true"):
        raise OutlookWebError("unsupported_layout")
    read = row.locator('button[title="Mark as unread"]').count()
    unread = row.locator('button[title="Mark as read"]').count()
    if (read, unread) not in ((1, 0), (0, 1)):
        raise OutlookWebError("unsupported_layout")
    return RowState(dom_id, read == 1, selected == "true")


def _rows(message_list: Locator) -> tuple[RowState, ...]:
    rows = _one(message_list).get_by_role("option")
    count = rows.count()
    if not 1 <= count <= _MAX_ROWS:
        raise OutlookWebError("unsupported_layout")
    result = tuple(_row_state(rows.nth(index)) for index in range(count))
    if len({row.dom_id for row in result}) != count:
        raise OutlookWebError("view_changed")
    return result


def _mail_list(page: Page) -> Locator:
    if page.is_closed():
        raise OutlookWebError("browser_closed")
    _raise_policy_block(page)
    if _host(page.url) not in _MAIL_HOSTS:
        raise OutlookWebError("view_changed")
    _assert_no_compose(page)
    return _one(page.get_by_role("listbox", name=_LIST))


def _wait_for_list(page: Page, timeout: int) -> Locator:
    deadline = time.monotonic() + timeout
    saw_mailbox = False
    while time.monotonic() < deadline:
        if page.is_closed():
            raise OutlookWebError("browser_closed")
        _raise_policy_block(page)
        if _host(page.url) in _MAIL_HOSTS:
            saw_mailbox = True
            _assert_no_compose(page)
            message_list = page.get_by_role("listbox", name=_LIST)
            if message_list.count():
                message_list = _one(message_list)
                if message_list.get_by_role("option").count():
                    return message_list
        page.wait_for_timeout(250)
    raise OutlookWebError("view_timeout" if saw_mailbox else "login_timeout")


def _baseline_pass(page: Page, message_list: Locator) -> tuple[RowState, ...]:
    _assert_no_compose(page)
    # Outlook retains an empty Reading Pane shell after clearing selection.
    # Permit that shell, but reject every observed message/header/body anchor.
    panes = page.get_by_role("main", name="Reading Pane", exact=True)
    content = (
        '[aria-label="Email message"], [aria-label$=" messages"], '
        'span[role="heading"][id$="_SUBJECT"], span[role="heading"][id$="_FROM"], '
        '[role="document"][aria-label="Message body"]'
    )
    if panes.count() > 1 or page.locator(_BODY).count():
        raise OutlookWebError("baseline_required")
    if panes.count() == 1 and _one(panes).locator(content).count():
        raise OutlookWebError("baseline_required")
    rows = _rows(message_list)
    if any(row.selected for row in rows):
        raise OutlookWebError("baseline_required")
    return rows


def _baseline(page: Page, *, login_timeout: int) -> tuple[RowState, ...]:
    with _layout_stage("baseline"):
        message_list = _wait_for_list(page, login_timeout)
        before = _baseline_pass(page, message_list)
        page.wait_for_timeout(250)
        if _baseline_pass(page, _mail_list(page)) != before:
            raise OutlookWebError("view_changed")
        return before


def _selected_row(page: Page, baseline: tuple[RowState, ...]) -> tuple[Locator, RowState] | None:
    with _layout_stage("selection"):
        message_list = _mail_list(page)
        states = _rows(message_list)
    if [(row.dom_id, row.read) for row in states] != [(row.dom_id, row.read) for row in baseline]:
        raise OutlookWebError("selection_not_eligible")
    selected = [(index, row) for index, row in enumerate(states) if row.selected]
    if not selected:
        return None
    if len(selected) != 1 or not selected[0][1].read:
        raise OutlookWebError("selection_not_eligible")
    index, state = selected[0]
    return message_list.get_by_role("option").nth(index), state


def _match_row_headers(row: Locator, subject: str, sender: str) -> None:
    leaves = row.locator("span:not(:has(*))")
    count = leaves.count()
    if not 1 <= count <= 100:
        raise OutlookWebError("unsupported_layout")
    texts = [_bounded_text(leaves.nth(index), 2048) for index in range(count)]
    if not subject or not sender or subject == sender or texts.count(subject) != 1 or texts.count(sender) != 1:
        raise OutlookWebError("unsupported_layout")


def _subject(pane: Locator) -> str:
    headings = pane.locator(f'span[role="heading"][id$="_SUBJECT"]{_UI}')
    if headings.count() != 2:
        raise OutlookWebError("unsupported_layout")
    # Locate the linked heading among the actual candidates, not descendants.
    candidates = [headings.nth(index) for index in range(2)]
    refs = [(item, item.get_attribute("aria-labelledby")) for item in candidates]
    references = [(item, ref) for item, ref in refs if ref]
    if len(references) != 1:
        raise OutlookWebError("unsupported_layout")
    empty, target_id = references[0]
    if target_id is None or len(target_id) > 512 or any(char.isspace() for char in target_id):
        raise OutlookWebError("unsupported_layout")
    targets = [item for item in candidates if item.get_attribute("id") == target_id]
    if pane.locator("[id]").evaluate_all(
        "(elements, target) => elements.filter(element => element.id === target).length", target_id,
    ) != 1:
        raise OutlookWebError("unsupported_layout")
    empty_id = empty.get_attribute("id") or ""
    if (not empty_id.startswith("MSG_") or not target_id.startswith("CONV_")
            or len(targets) != 1 or _bounded_text(empty, 512).strip()):
        raise OutlookWebError("unsupported_layout")
    return _bounded_text(targets[0], 512)


def _pane_text(page: Page, row: Locator) -> dict[str, str] | None:
    panes = page.get_by_role("main", name="Reading Pane", exact=True)
    if panes.count() == 0:
        return None
    pane = _one(panes)
    groups = pane.locator(f'[aria-label$=" messages"]{_UI}')
    if groups.count() == 0:
        return None
    group = _one(groups)
    if group.get_attribute("aria-label") != "1 messages":
        raise OutlookWebError("unsupported_layout")
    emails = group.locator(f'[aria-label="Email message"]{_UI}')
    if emails.count() == 0:
        return None
    email = _one(emails)
    if pane.locator(f'[aria-label="Email message"]{_UI}').count() != 1:
        raise OutlookWebError("unsupported_layout")
    if pane.locator(f'span[role="heading"][id$="_SUBJECT"]{_UI}').count() < 2:
        return None
    with _layout_stage("subject_header"):
        subject = _subject(pane)
    sender_heading = email.locator(f'span[role="heading"][id$="_FROM"]{_UI}')
    if sender_heading.count() == 0:
        return None
    with _layout_stage("sender_header"):
        sender = _bounded_text(_one(sender_heading), 512)
    with _layout_stage("row_headers"):
        _match_row_headers(row, subject, sender)
    with _layout_stage("body_layout"):
        body = _body_text(page, group, email)
    if body is None:
        return None
    return {"subject": subject, "sender": sender, "body": body}


def _body_text(page: Page, group: Locator, email: Locator) -> str | None:
    """Read the supported placeholder/portal body pair only."""
    bodies = group.locator(_BODY)
    if bodies.count() < 2:
        return None
    if bodies.count() != 2 or page.locator(_BODY).count() != 2:
        raise OutlookWebError("unsupported_layout")
    for index in range(2):
        documents = bodies.nth(index).get_by_role("document", name="Message body", exact=True)
        if documents.count() == 0:
            return None
        if documents.count() != 1 or bodies.nth(index).get_by_role("document").count() != 1:
            raise OutlookWebError("unsupported_layout")
    placeholder = email.locator(f':scope > {_BODY}')
    if placeholder.count() != 1 or _bounded_text(placeholder, 50000).strip():
        raise OutlookWebError("unsupported_layout")
    portal = group.locator(f'{_BODY}:not([aria-label="Email message"] {_BODY})')
    body = _bounded_text(_one(portal).get_by_role("document", name="Message body", exact=True), 50000)
    if not body.strip():
        return None
    return body


def _read_after_selection(
    page: Page, baseline: tuple[RowState, ...], *, selection_timeout: int,
) -> dict[str, str]:
    deadline = time.monotonic() + selection_timeout
    while time.monotonic() < deadline:
        selected = _selected_row(page, baseline)
        if selected is None:
            page.wait_for_timeout(250)
            continue
        row, identity = selected
        with _layout_stage("message_pane"):
            first = _pane_text(page, row)
        if first is None:
            page.wait_for_timeout(250)
            continue
        after_first = _selected_row(page, baseline)
        if after_first is None or after_first[1] != identity:
            raise OutlookWebError("view_changed")
        page.wait_for_timeout(300)
        second = _selected_row(page, baseline)
        if second is None or second[1] != identity:
            raise OutlookWebError("view_changed")
        with _layout_stage("message_pane"):
            second_text = _pane_text(page, second[0])
        final = _selected_row(page, baseline)
        if first != second_text or final is None or final[1] != identity:
            raise OutlookWebError("view_changed")
        return first
    raise OutlookWebError("selection_timeout")


def collect_selected_message(
    *, login_timeout: int = 180, selection_timeout: int = 120,
    max_body_chars: int = 8000, on_ready: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Wait for manual selection; never select a message on the user's behalf."""
    options = ((login_timeout, 30, 600), (selection_timeout, 10, 600), (max_body_chars, 1, 20000))
    if any(type(value) is not int or not low <= value <= high for value, low, high in options):
        raise OutlookWebError("invalid_options")

    def read(page: Page) -> dict[str, Any]:
        baseline = _baseline(page, login_timeout=login_timeout)
        if not any(row.read for row in baseline):
            raise OutlookWebError("selection_not_eligible")
        if on_ready is not None:
            on_ready()
        raw = _read_after_selection(page, baseline, selection_timeout=selection_timeout)
        return selected_output(raw, max_body_chars=max_body_chars)

    return collect_in_temporary_browser(read)
