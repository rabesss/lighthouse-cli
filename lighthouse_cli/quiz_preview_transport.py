"""Low-level start, read, save and forward transitions for instructor previews.

The CLI session layer owns encrypted cursors and uncertain-outcome recovery.
Only preview pages are accepted. No student attempt can be written here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

from .api import LighthouseClient, NetworkError, _close_response
from .quiz_attempt_page import (
    MAX_PAGE_BYTES,
    PreviewPage,
    PreviewPageError,
    PreviewRefusedError,
    hidden_form,
    parse_preview_page,
    process_frame_src,
    start_frame_src,
    started_attempt,
)
from .request_protection import form_protection_from_homepage


class PreviewSaveUnknownError(NetworkError):
    def __init__(self) -> None:
        super().__init__("Answer save could not be verified. Inspect the preview before retrying.")


class PreviewStartUnknownError(NetworkError):
    def __init__(self, *, attempt_id: int | None = None, page: int | None = None) -> None:
        self.attempt_id = attempt_id
        self.page = page
        super().__init__("Preview start could not be verified. Inspect quiz attempts before starting again.")


class PreviewAdvanceUnknownError(NetworkError):
    def __init__(self) -> None:
        super().__init__("Preview navigation could not be verified. Inspect the browser before continuing.")


def _start_target(client: LighthouseClient, value: str, filename: str, course_id: int, quiz_id: int) -> str:
    url = client.canonical_url(value)
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    allowed = {"ou", "qi", "isprv", "dnb", "cfql", "fromQB", "inProgress", "cft", "d2l_body_type"}
    if (parsed.path != f"/d2l/lms/quizzing/user/attempt/{filename}"
            or set(query) - allowed
            or any(query.get(key) != [str(value)] for key, value in {"ou": course_id, "qi": quiz_id, "isprv": 1, "fromQB": 0, "inProgress": 0}.items())):
        raise NetworkError("Preview start form has an unexpected identity.")
    return url


_START_UNAVAILABLE = (
    "Preview start is not available to this account or under the current quiz restrictions. "
    "For a hidden or unavailable quiz, retry with --bypass-availability."
)
_START_NEEDS_BROWSER = "This preview requires an additional browser authorization step."
_START_PROTECTION = "Preview form protection could not be verified. Nothing was started."


def start_preview(
    client: LighthouseClient, *, course_id: int, quiz_id: int, bypass_availability: bool = False,
    on_identity: Callable[[int, int], None] | None = None,
) -> PreviewPage:
    """Start an instructor preview once; availability bypass is explicit.

    ``on_identity`` receives the validated attempt id and page as soon as the
    start callback reveals them, before the page readback, so the caller can
    seal them durably. If it raises, the start is reported as unknown with
    that identity attached.
    """
    # Reuse strict identity validation before any request.
    page_path(course_id, quiz_id, 1, 1)
    if type(bypass_availability) is not bool:
        raise ValueError("Invalid preview settings.")
    summary = "/d2l/lms/quizzing/user/quiz_summary.d2l?" + urlencode({
        "bp": int(bypass_availability), "isprv": 1, "qi": quiz_id, "ou": course_id,
    })
    body, _ = client.get_raw(summary, max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
    soup = BeautifulSoup(body, "html.parser")
    if not any(button.get_text(" ", strip=True) == "Start Quiz!" and not button.has_attr("disabled")
               for button in soup.find_all("button")):
        raise PreviewRefusedError(_START_UNAVAILABLE)
    form, fields = hidden_form(body)
    if form.select('input[type="password"]') or fields.get("hps"):
        raise PreviewRefusedError(_START_NEEDS_BROWSER)
    protection = form_protection_from_homepage(body)
    if fields.get("d2l_referrer") != protection.csrf_token:
        raise PreviewRefusedError(_START_PROTECTION)
    fields.update(d2l_action="Custom", d2l_actionparam="1", d2l_hitCode=protection.next_hit_code())
    if bypass_availability:
        fields["bypass"] = "1"
    post_url = client.canonical_url(summary + "&cfql=0&inProgress=0")
    response = None
    try:
        # The summary POST registers the preview/bypass choice. Skipping it
        # can appear to work for visible quizzes but fails for hidden ones.
        # Anything that fails from here on, a session expiry included, may
        # come after Brightspace created the pending preview state: unknown.
        response = client._request("POST", post_url, _skip_raise=True,
                                   files=[(key, (None, value)) for key, value in fields.items()],
                                   headers={"Referer": client.canonical_url(summary)})
        if response.status_code != 302:
            raise PreviewStartUnknownError()
        root_url = _start_target(client, response.headers.get("Location", ""), "quiz_start_frame_auto.d2l", course_id, quiz_id)
        _close_response(response)
        response = None
        root, _ = client.get_raw(root_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
        frame_path = _start_target(client, start_frame_src(root), "quiz_start_iframe_2_auto.d2l", course_id, quiz_id)
        frame, _ = client.get_raw(frame_path, max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers={"Referer": root_url})
        process_url = _start_target(client, process_frame_src(frame), "quiz_start_process_auto.d2l", course_id, quiz_id)
        # This legacy GET creates server state: it is deliberately not replayed.
        result, _ = client.get_raw(process_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False,
                                  headers={"Referer": client.canonical_url(frame_path)})
        attempt_id, page = started_attempt(result)
        page_path(course_id, quiz_id, attempt_id, page)
        try:
            if on_identity is not None:
                on_identity(attempt_id, page)
            return read_current_preview(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
        except Exception:  # all post-create failures are ambiguous
            raise PreviewStartUnknownError(attempt_id=attempt_id, page=page) from None
    except PreviewStartUnknownError:
        raise
    except Exception:
        raise PreviewStartUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)


def page_path(course_id: int, quiz_id: int, attempt_id: int, page: int) -> str:
    if any(type(value) is not int or not 0 < value < 10**18 for value in (course_id, quiz_id, attempt_id, page)):
        raise ValueError("Invalid preview identity.")
    return "/d2l/lms/quizzing/user/attempt/quiz_attempt_page_auto.d2l?" + urlencode({
        "ou": course_id, "qi": quiz_id, "ai": attempt_id, "pg": page,
        "isprv": 1, "d2l_body_type": 3, "fromQB": 0,
    })


def read_current_preview(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> PreviewPage:
    """Read the caller's current cursor; never infer or advance that cursor."""
    path = page_path(course_id, quiz_id, attempt_id, page)
    body, _ = client.get_raw(path, max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers={"Cache-Control": "no-cache"})
    return parse_preview_page(body, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)


def _server_page(body: bytes, *, course_id: int, quiz_id: int, attempt_id: int) -> int | None:
    """The page Brightspace reports for exactly this preview attempt, else None."""
    try:
        _, hidden = hidden_form(body)
    except PreviewPageError:
        return None
    expected = {"ou": course_id, "qi": quiz_id, "ai": attempt_id, "isprv": 1}
    if any(hidden.get(key) != str(value) for key, value in expected.items()):
        return None
    value = hidden.get("pg")
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,5}", value):
        return None
    return int(value)


def read_server_current_preview(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> PreviewPage:
    """Read-only recovery: return the attempt's server-side current page.

    Only for reconciling a start whose cursor is uncertain, never before a
    write. Observed on Brightspace (2026-09-23): for a forward-only quiz
    already on page 2, requesting page 1 returns page 2 with ``pg=2``, and a
    page beyond the cursor redirects. An all-at-once quiz has a single page.
    The reported page is used only after the complete preview identity
    (course, quiz, attempt, ``isprv=1``) matches, and the page must then
    parse strictly as that page.
    """
    path = page_path(course_id, quiz_id, attempt_id, page)
    body, _ = client.get_raw(path, max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers={"Cache-Control": "no-cache"})
    try:
        return parse_preview_page(body, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
    except PreviewPageError:
        server_page = _server_page(body, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id)
        if server_page is None or server_page == page:
            raise
        return parse_preview_page(body, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=server_page)


def save_current_preview_answer(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int,
    page: int, question_id: int, choice_id: int,
) -> PreviewPage:
    """Fetch fresh protection and form state, POST once, verify server state.

    A transport error or ambiguous response must not trigger a second POST.
    A successful HTTP status without saved/selected readback is not success.
    """
    path = page_path(course_id, quiz_id, attempt_id, page)
    homepage, _ = client.get_raw("/d2l/home", max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
    protection = form_protection_from_homepage(homepage)
    identity = {"course_id": course_id, "quiz_id": quiz_id, "attempt_id": attempt_id, "page": page}
    current = read_current_preview(client, **identity)
    fields = current.answer_fields(question_id, choice_id, protection)
    url = client.canonical_url(
        "/d2l/lms/quizzing/user/attempt/quiz_attempt_save_auto.d2l?"
        + urlencode({"d2l_body_type": 3, "ou": course_id, "fromQB": 0})
    )
    response = None
    try:
        # Any failure from here on, even a session expiry raised by the
        # request itself, is ambiguous: the server may have accepted the
        # answer before returning a login page.
        response = client._request(
            "POST", url,
            files=[(key, (None, value)) for key, value in fields.items()],
            headers={"Referer": client.canonical_url(path)},
        )
        if response.status_code != 200:
            raise PreviewSaveUnknownError()
        verified = read_current_preview(client, **identity)
        if not verified.confirms_answer(question_id, choice_id):
            raise PreviewSaveUnknownError()
        return verified
    except Exception:
        raise PreviewSaveUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)


def advance_current_preview(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> PreviewPage:
    homepage, _ = client.get_raw("/d2l/home", max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
    protection = form_protection_from_homepage(homepage)
    current = read_current_preview(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
    # advance_fields requires a Next control, so the page + 1 readback below
    # always exists. Never request a page beyond the quiz's last page: on the
    # Brightspace (observed 2026-09-23) that permanently breaks the attempt
    # (every later read redirects to /d2l/error/500).
    fields = current.advance_fields(protection)
    url = client.canonical_url("/d2l/lms/quizzing/user/attempt/quiz_attempt_save_auto.d2l?" + urlencode({
        "cfql": 0, "fromQB": 0, "d2l_body_type": 3, "ou": course_id,
    }))
    response = None
    try:
        # Treat any failure from here on, an auth failure included, as
        # unknown: the navigation may already have moved the remote cursor.
        response = client._request("POST", url, files=[(key, (None, value)) for key, value in fields.items()],
                                   headers={"Referer": client.canonical_url(page_path(course_id, quiz_id, attempt_id, page))})
        if response.status_code != 200:
            raise PreviewAdvanceUnknownError()
        return read_current_preview(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page + 1)
    except Exception:
        raise PreviewAdvanceUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)
