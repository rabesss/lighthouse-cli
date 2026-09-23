"""Low-level start, read, save and forward transitions for instructor previews.

The CLI session layer owns encrypted cursors and uncertain-outcome recovery.
Only preview pages are accepted. No student attempt can be written here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

from .api import LighthouseClient, NetworkError, SessionExpiredError, _close_response
from .quiz_attempt_page import (
    MAX_PAGE_BYTES,
    PreviewPage,
    PreviewPageError,
    PreviewRefusedError,
    hidden_form,
    parse_preview_page,
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
    state_created = False
    start_dispatched = False
    attempt_id: int | None = None
    page: int | None = None
    try:
        # The summary POST registers the preview/bypass choice. Skipping it
        # can appear to work for visible quizzes but fails for hidden ones.
        # Mark it before dispatch because a session expiry can arrive after
        # Brightspace has already created the pending preview state.
        start_dispatched = True
        response = client._request("POST", post_url, _skip_raise=True,
                                   files=[(key, (None, value)) for key, value in fields.items()],
                                   headers={"Referer": client.canonical_url(summary)})
        if response.status_code != 302:
            raise PreviewStartUnknownError()
        root_url = _start_target(client, response.headers.get("Location", ""), "quiz_start_frame_auto.d2l", course_id, quiz_id)
        _close_response(response)
        response = None
        root, _ = client.get_raw(root_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
        candidates = [
            src
            for f in BeautifulSoup(root, "html.parser").find_all("iframe")
            if isinstance(src := f.get("src"), str)
            and urlparse(src).path.endswith("/quiz_start_iframe_2_auto.d2l")
        ]
        if len(candidates) != 1:
            raise PreviewStartUnknownError()
        frame_path = _start_target(
            client, candidates[0], "quiz_start_iframe_2_auto.d2l", course_id, quiz_id
        )
        frame, _ = client.get_raw(
            frame_path, max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers={"Referer": root_url}
        )
        frames = BeautifulSoup(frame, "html.parser").select(
            'iframe[name="hiddenFrame"], frame[name="hiddenFrame"]'
        )
        frames_src = frames[0].get("src") if len(frames) == 1 else None
        if not isinstance(frames_src, str):
            raise PreviewStartUnknownError()
        process_url = _start_target(
            client, frames_src, "quiz_start_process_auto.d2l", course_id, quiz_id
        )
        # This legacy GET creates server state: it is deliberately not replayed.
        state_created = True
        result, _ = client.get_raw(process_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False,
                                  headers={"Referer": client.canonical_url(frame_path)})
        matches: set[tuple[int, int]] = set()
        for script in BeautifulSoup(result, "html.parser").find_all("script"):
            for match in re.finditer(
                r"^\s*parent\.GoToAttemptQuizAuto\(\s*([0-9]{1,18})\s*,\s*([0-9]{1,6})\s*,\s*0\s*\)\s*;?\s*$",
                script.get_text(), re.MULTILINE,
            ):
                matches.add((int(match[1]), int(match[2])))
        if len(matches) != 1:
            raise PreviewStartUnknownError()
        attempt_id, page = matches.pop()
        try:
            page_path(course_id, quiz_id, attempt_id, page)
        except ValueError:
            raise PreviewStartUnknownError() from None
        if on_identity is not None:
            try:
                on_identity(attempt_id, page)
            except Exception:
                raise PreviewStartUnknownError(attempt_id=attempt_id, page=page) from None
        try:
            return read_current_preview(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
        except SessionExpiredError:
            raise PreviewStartUnknownError(attempt_id=attempt_id, page=page) from None
        except Exception:  # all post-create readback failures are ambiguous
            raise PreviewStartUnknownError(attempt_id=attempt_id, page=page) from None
    except PreviewStartUnknownError:
        raise
    except SessionExpiredError:
        if state_created:
            raise PreviewStartUnknownError(attempt_id=attempt_id, page=page) from None
        if start_dispatched:
            raise PreviewStartUnknownError() from None
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
    write. Observed on the trial tenant (2026-09-23): for a forward-only quiz
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
    write_dispatched = False
    try:
        # A session-expiry raised by the request itself is ambiguous: the
        # server may have accepted the answer before returning a login page.
        write_dispatched = True
        response = client._request(
            "POST", url,
            files=[(key, (None, value)) for key, value in fields.items()],
            headers={"Referer": client.canonical_url(path)},
        )
        if response.status_code != 200:
            raise PreviewSaveUnknownError()
        try:
            verified = read_current_preview(client, **identity)
        except SessionExpiredError:
            # The POST was accepted before the readback lost authentication.
            raise PreviewSaveUnknownError() from None
        if not verified.confirms_answer(question_id, choice_id):
            raise PreviewSaveUnknownError()
        return verified
    except SessionExpiredError:
        if write_dispatched:
            raise PreviewSaveUnknownError() from None
        raise
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
    # trial tenant (2026-09-23) that permanently breaks the preview attempt
    # (every later read redirects to /d2l/error/500).
    fields = current.advance_fields(protection)
    url = client.canonical_url("/d2l/lms/quizzing/user/attempt/quiz_attempt_save_auto.d2l?" + urlencode({
        "cfql": 0, "fromQB": 0, "d2l_body_type": 3, "ou": course_id,
    }))
    response = None
    write_dispatched = False
    try:
        # Treat an auth failure from this request as post-dispatch unknown;
        # the navigation may already have moved the remote cursor.
        write_dispatched = True
        response = client._request("POST", url, files=[(key, (None, value)) for key, value in fields.items()],
                                   headers={"Referer": client.canonical_url(page_path(course_id, quiz_id, attempt_id, page))})
        if response.status_code != 200:
            raise PreviewAdvanceUnknownError()
        try:
            return read_current_preview(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page + 1)
        except SessionExpiredError:
            # The navigation POST completed before the readback lost auth.
            raise PreviewAdvanceUnknownError() from None
    except SessionExpiredError:
        if write_dispatched:
            raise PreviewAdvanceUnknownError() from None
        raise
    except Exception:
        raise PreviewAdvanceUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)
