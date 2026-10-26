"""Start, read, save and move through the signed-in learner's own quiz attempt.

Brightspace has no learner REST route for an attempt, so these are the
legacy HTML endpoints its browser pages use, each response validated
strictly. Every write is sent once. An outcome that cannot be verified is
reported as unknown and never retried here: the caller re-reads the server.
"""

from __future__ import annotations

import dataclasses
import posixpath
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urldefrag, urlparse

from bs4 import BeautifulSoup

from .api import LighthouseClient, NetworkError, _close_response
from .quiz_attempt_page import (
    MAX_PAGE_BYTES,
    REFUSE_NO_ANSWERS,
    LearnerPage,
    PageSnapshot,
    PreviewPageError,
    PreviewRefusedError,
    active_buttons,
    browser_form,
    hidden_form,
    parse_learner_page,
    process_frame_src,
    start_frame_src,
    started_attempt,
)
from .quiz_learner_browser import (
    ATTEMPT_ROUTE,
    AttemptView,
    PageChange,
    attempt_frame_path,
    autosave_marker,
    autosave_path,
    check_attempt_frame,
    check_reload_marker,
    identity,
    learner_page_path,
    plan_page_change,
    show_incorrect_only,
    summary_path,
    tab_id_of,
    timer_path,
    valid_tab_id,
)
from .request_protection import FormProtection, form_protection_from_homepage


class LearnerStartUnknownError(NetworkError):
    """``timer`` is set when the attempt opened but its timer frame could not be read."""

    def __init__(self, *, attempt_id: int | None = None, page: int | None = None, timer: bool = False) -> None:
        self.attempt_id = attempt_id
        self.page = page
        self.timer = timer
        super().__init__("Quiz start could not be verified. Read the quiz state before starting again.")


class LearnerSaveUnknownError(NetworkError):
    def __init__(self) -> None:
        super().__init__("Answer save could not be verified. Read the page before retrying.")


class LearnerAdvanceUnknownError(NetworkError):
    def __init__(self) -> None:
        super().__init__("Page change could not be verified. Run attempt start to reopen the attempt where Brightspace has it.")


REFUSE_START_UNAVAILABLE = "This quiz cannot be started or continued now (closed, not yet open or out of attempts)."
REFUSE_NOTHING_IN_PROGRESS = "No attempt of this quiz is in progress."
REFUSE_START_PASSWORD = "This quiz needs a password, which the CLI does not support."  # pragma: allowlist secret
REFUSE_START_BROWSER = "This quiz needs a browser step (such as LockDown Browser) that the CLI cannot do."
REFUSE_START_ROLE = "Brightspace does not let an impersonated role take a quiz."
REFUSE_START_PROTECTION = "Quiz form protection could not be verified. Nothing was started."
# Filled in with the attempt's number alone, an integer.
REFUSE_START_PROCESSING = ("Brightspace is still submitting attempt {} after its time ran out, so nothing was started. "
                           "Run attempt start again in a few minutes, or attempt verify if the CLI was taking that attempt.")
REFUSE_PAGE_PROTECTION = "Form protection could not be read from the quiz page. Nothing was sent."
REFUSE_IMAGE_SOURCE = "This image is not stored on Brightspace, so the CLI does not download it."
REFUSE_IMAGE_ROUTE = "This image address is a Brightspace page, not a file, so the CLI does not request it."
# Pages a request can change: the quiz pages themselves (one past the last
# page breaks the attempt) and signing out. Legacy ``.d2l`` pages anywhere
# are refused too. Paths are case-insensitive.
_ACTION_ROUTES = ("/d2l/lms/quizzing/", "/d2l/logout")

_SUMMARY_FLAGS = ("isImpersonatingRole", "canTakeQuiz", "startQuiz", "continueQuiz", "hasPass")
# The timer frame's script variables, each declared once on its own line.
_TIMER_VARS = {
    "quizId": "[0-9]{1,18}", "attemptTimeLoggingQuizId": "[0-9]{1,18}",
    "attemptTimeLoggingAttemptId": "[0-9]{1,18}", "isPreview": "true|false",
    "timeStartedTicks": "[0-9]{1,19}", "timeLimit": "[0-9]{1,9}", "enforceTimeLimit": "true|false",
    "hasAutoSubmit": "true|false", "timeExceeded": "true|false",
}
# .NET ticks (100 ns since 0001-01-01 UTC) at the Unix epoch.
_UNIX_EPOCH_TICKS = 621_355_968_000_000_000
# How far ahead of the server's clock an attempt's start may be, and the
# earliest it may be (2000-01-01): an older one is no real start.
_START_SKEW_SECONDS = 300
_EARLIEST_START = 946_684_800
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# Raster formats an agent can view, by their leading bytes.
_IMAGE_SIGNATURES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
                     (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def _form_headers(client: LighthouseClient, referer: str) -> dict[str, str]:
    """A page form's headers, as the browser sends them."""
    return {"Origin": client.base_url, "Upgrade-Insecure-Requests": "1", "Cache-Control": "max-age=0",
            "Referer": client.canonical_url(referer)}


def _document_headers(client: LighthouseClient, referer: str | None = None, *, reload: bool = False) -> dict[str, str]:
    """A page load's headers: a navigation's, or a reload's, which the CLI makes where a browser would not ask again."""
    headers = {"Upgrade-Insecure-Requests": "1"}
    if reload:
        headers["Cache-Control"] = "max-age=0"
    if referer is not None:
        headers["Referer"] = client.canonical_url(referer)
    return headers


def _get_page(client: LighthouseClient, path: str, headers: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    return client.get_raw(path, max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers=headers)


def _post_page(client: LighthouseClient, path: str, fields: list[tuple[str, str]], referer: str) -> bytes:
    """Post a page's form once, as its script does, and read the reply its save frame loads."""
    response = client._request("POST", client.canonical_url(path), _skip_raise=True, stream=True,
                               files=[(key, (None, value)) for key, value in fields],
                               headers=_form_headers(client, referer))
    try:
        length = response.headers.get("Content-Length")
        if response.status_code != 200 or (isinstance(length, str) and length.isascii() and length.isdecimal()
                                           and int(length) > MAX_PAGE_BYTES):
            raise NetworkError("The quiz page's reply could not be read.")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) > MAX_PAGE_BYTES:
                raise NetworkError("The quiz page's reply could not be read.")
        return bytes(body)
    finally:
        _close_response(response)


@dataclass(frozen=True)
class LearnerSummary:
    """The quiz summary's own start state. Private fields are secret-bearing.

    ``attempt_processing`` is the number of an attempt Brightspace is still
    submitting after its time ran out. Until it has, the summary flags
    Continue, yet that attempt cannot be continued and no new one started.
    """

    course_id: int
    quiz_id: int
    can_start: bool
    can_continue: bool
    attempt_in_progress: int | None
    attempt_processing: int | None
    _flags: dict[str, bool] = field(repr=False, compare=False)
    _hidden_fields: dict[str, str] = field(repr=False, compare=False)
    _password_input: bool = field(repr=False, compare=False)
    _button: str | None = field(repr=False, compare=False)
    _protection: FormProtection | None = field(repr=False, compare=False)
    _body: bytes = field(default=b"", repr=False, compare=False)

    def public_data(self) -> dict[str, Any]:
        return {"course_id": self.course_id, "quiz_id": self.quiz_id, "can_start": self.can_start,
                "can_continue": self.can_continue, "attempt_in_progress": self.attempt_in_progress,
                "attempt_processing": self.attempt_processing}


def parse_learner_summary(body: bytes, *, course_id: int, quiz_id: int) -> LearnerSummary:
    """Read the summary page's start state as data; its scripts never run."""
    identity(course_id, quiz_id)
    form, hidden = hidden_form(body)
    soup = BeautifulSoup(body, "html.parser")
    scripts = "\n".join(script.get_text() for script in soup.find_all("script"))
    flags: dict[str, bool] = {}
    for name in _SUMMARY_FLAGS:
        values = re.findall(rf"\bvar\s+{name}\s*=\s*(true|false)\s*;", scripts)
        if len(values) != 1:
            raise PreviewPageError()
        flags[name] = values[0] == "true"
    labels = active_buttons(soup, {"Start Quiz!", "Continue Quiz..."})
    if len(labels) > 1:
        raise PreviewPageError()
    text = " ".join(soup.get_text(" ", strip=True).split())
    progress = re.findall(r"\(Attempt ([0-9]{1,6}) in progress\)", text)
    processing = re.findall(r"\(Attempt ([0-9]{1,6}) is being processed\)", text)
    # continueQuiz names one attempt: in progress, or still being submitted
    # after its time ran out, which nothing on the page may start or continue.
    if len(progress) + len(processing) > 1 or bool(progress or processing) != flags["continueQuiz"]:
        raise PreviewPageError()
    if processing and (flags["startQuiz"] or labels):
        raise PreviewPageError()
    try:
        protection: FormProtection | None = form_protection_from_homepage(body)
    except ValueError:
        protection = None
    takeable = flags["canTakeQuiz"] and not flags["isImpersonatingRole"]
    return LearnerSummary(
        course_id, quiz_id,
        can_start=takeable and flags["startQuiz"] and not flags["continueQuiz"],
        can_continue=takeable and flags["continueQuiz"] and not flags["startQuiz"] and not processing,
        attempt_in_progress=int(progress[0]) if progress else None,
        attempt_processing=int(processing[0]) if processing else None,
        _flags=flags, _hidden_fields=hidden,
        _password_input=bool(form.select('input[type="password"]') or soup.select('input[name="password"]')),
        _button=labels[0] if labels else None, _protection=protection, _body=body,
    )


def read_learner_summary(client: LighthouseClient, *, course_id: int, quiz_id: int) -> LearnerSummary:
    body, _ = _get_page(client, summary_path(course_id, quiz_id), _document_headers(client, reload=True))
    return parse_learner_summary(body, course_id=course_id, quiz_id=quiz_id)


@dataclass(frozen=True)
class LearnerTimer:
    """An attempt's enforced time limit.

    ``ends_at`` is a Unix time on the server's clock, which runs
    ``clock_offset`` seconds ahead of the local one.
    """

    limit_seconds: int
    ends_at: float
    auto_submit: bool
    clock_offset: float = 0.0


def parse_learner_timer(body: bytes, *, quiz_id: int, attempt_id: int, now: float) -> LearnerTimer | None:
    """The timer frame's limit as data, or ``None`` when the attempt's time is not enforced.

    ``now`` is the server's time of the response, so ``ends_at`` is on the
    server's clock. An attempt the server marks over time ends by ``now``.
    """
    scripts = "\n".join(script.get_text() for script in BeautifulSoup(body, "html.parser").find_all("script"))
    values: dict[str, str] = {}
    for name, pattern in _TIMER_VARS.items():
        found = re.findall(rf"^[ \t]*var\s+{name}\s*=\s*({pattern})\s*;[ \t\r]*$", scripts, re.MULTILINE)
        if len(found) != 1:
            raise PreviewPageError()
        values[name] = found[0]
    identity = (values["quizId"], values["attemptTimeLoggingQuizId"], values["attemptTimeLoggingAttemptId"])
    if identity != (str(quiz_id), str(quiz_id), str(attempt_id)) or values["isPreview"] != "false":
        raise PreviewPageError()
    if values["enforceTimeLimit"] == "false":
        return None
    limit = int(values["timeLimit"])
    started = (int(values["timeStartedTicks"]) - _UNIX_EPOCH_TICKS) / 10**7
    if limit < 1 or not _EARLIEST_START <= started <= now + _START_SKEW_SECONDS:
        raise PreviewPageError()
    ends_at = started + limit
    if values["timeExceeded"] == "true":
        ends_at = min(ends_at, now)
    return LearnerTimer(limit, ends_at, values["hasAutoSubmit"] == "true")


def _server_time(headers: Mapping[str, str]) -> float | None:
    """The response's ``Date``, or ``None`` without a valid one (a time without a zone is not one)."""
    value = next((str(value) for key, value in headers.items() if key.lower() == "date"), "")
    try:
        date = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return None if date.tzinfo is None else date.timestamp()


def _timer(body: bytes, headers: Mapping[str, str], sent: float, *, quiz_id: int, attempt_id: int) -> LearnerTimer | None:
    """The timer frame's limit, with the server clock's offset from the local one.

    The frame's start time is the server's. The response's ``Date`` gives
    the offset, so a local clock that is off still counts down to the
    server's limit. ``sent`` is the local time just before the request.
    """
    date = _server_time(headers)
    # The Date is stamped in whole seconds after the request was sent, so the
    # server is at most this far ahead: the countdown never ends after its own.
    offset = 0.0 if date is None else date + 1 - sent
    timer = parse_learner_timer(body, quiz_id=quiz_id, attempt_id=attempt_id, now=time.time() + offset)
    return None if timer is None else dataclasses.replace(timer, clock_offset=offset)


def read_learner_timer(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, frame_page: int | None = None,
) -> LearnerTimer | None:
    """The attempt's time limit from its timer frame, or ``None`` when its time is not enforced.

    The frame is reloaded as the attempt frame opened on ``frame_page``
    loads it, when the CLI has opened one.
    """
    referer = None if frame_page is None else attempt_frame_path(course_id, quiz_id, attempt_id, frame_page)
    sent = time.time()
    body, headers = _get_page(client, timer_path(course_id, quiz_id, attempt_id),
                              _document_headers(client, referer, reload=True))
    return _timer(body, headers, sent, quiz_id=quiz_id, attempt_id=attempt_id)


def refuse_while_processing(summary: LearnerSummary) -> None:
    """Refuse to start or continue while Brightspace is still submitting an attempt whose time ran out."""
    if summary.attempt_processing is not None:
        raise PreviewRefusedError(REFUSE_START_PROCESSING.format(int(summary.attempt_processing)))


def _start_fields(summary: LearnerSummary, *, continue_only: bool) -> tuple[list[tuple[str, str]], bool]:
    """The summary form's start or continue action, or a fixed refusal."""
    refuse_while_processing(summary)
    flags = summary._flags
    if flags["isImpersonatingRole"]:
        raise PreviewRefusedError(REFUSE_START_ROLE)
    if flags["hasPass"] or summary._password_input:
        raise PreviewRefusedError(REFUSE_START_PASSWORD)
    if not (summary.can_start or summary.can_continue):
        raise PreviewRefusedError(REFUSE_START_UNAVAILABLE)
    resume = summary.can_continue
    if continue_only and not resume:
        raise PreviewRefusedError(REFUSE_NOTHING_IN_PROGRESS)
    if summary._button != ("Continue Quiz..." if resume else "Start Quiz!"):
        raise PreviewRefusedError(REFUSE_START_UNAVAILABLE)
    fields = summary._hidden_fields
    if fields.get("hps") or fields.get("LockDownBrowserUrl", "0") != "0":
        raise PreviewRefusedError(REFUSE_START_BROWSER)
    protection = summary._protection
    if protection is None or fields.get("d2l_referrer") != protection.csrf_token:
        raise PreviewRefusedError(REFUSE_START_PROTECTION)
    return browser_form(summary._body, protection, action=("Custom", "1"), tab_id=None), resume


def _start_target(client: LighthouseClient, value: str, filename: str, course_id: int, quiz_id: int, resume: bool) -> str:
    url = client.canonical_url(value)
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    allowed = {"ou", "qi", "isprv", "dnb", "cfql", "fromQB", "inProgress", "cft", "d2l_body_type"}
    expected = {"ou": str(course_id), "qi": str(quiz_id), "isprv": "", "fromQB": "0", "inProgress": str(int(resume))}
    if (parsed.path != ATTEMPT_ROUTE + filename or set(query) - allowed
            or any(query.get(key) != [value] for key, value in expected.items())):
        raise NetworkError("Quiz start form has an unexpected identity.")
    return url


def _follow_start(
    client: LighthouseClient, location: str, course_id: int, quiz_id: int, resume: bool, referer: str,
) -> tuple[int, int, bytes, str]:
    """The start frames, then the hidden process page that names the attempt, and the start frame's URL."""
    root_url = _start_target(client, location, "quiz_start_frame_auto.d2l", course_id, quiz_id, resume)
    # The browser follows the start's redirect as a reload of the summary.
    root, _ = _get_page(client, root_url, _document_headers(client, referer, reload=True))
    frame_url = _start_target(client, start_frame_src(root), "quiz_start_iframe_2_auto.d2l", course_id, quiz_id, resume)
    frame, _ = _get_page(client, frame_url, _document_headers(client, root_url))
    process_url = _start_target(client, process_frame_src(frame), "quiz_start_process_auto.d2l", course_id, quiz_id, resume)
    # This GET creates or reopens the attempt: it is never replayed.
    result, _ = _get_page(client, process_url, _document_headers(client, frame_url))
    attempt_id, page = started_attempt(result)
    return attempt_id, page, result, frame_url


@dataclass(frozen=True)
class LearnerStart:
    """A started or continued attempt: its page, the attempt frame showing it, and its time limit."""

    page: LearnerPage
    view: AttemptView
    timer: LearnerTimer | None


def start_learner(
    client: LighthouseClient, *, course_id: int, quiz_id: int, continue_only: bool = False, known_tab_id: str = "",
    on_identity: Callable[[int, int, str], None] | None = None, summary: LearnerSummary | None = None,
) -> LearnerStart:
    """Continue the attempt in progress, else start a new one (once), and open it as the browser does.

    Starting a new attempt uses one of the learner's attempts, so callers
    confirm it first; ``continue_only`` refuses unless one is in progress.
    ``on_identity`` receives the attempt id, page and browser tab id before
    the attempt frame is read, as for previews. The process page sets a new
    tab id; a continued attempt whose page sets none keeps ``known_tab_id``.
    A start whose outcome is unclear can be resolved from the summary, which
    then offers to continue that attempt. A ``summary`` just read saves
    reading it again.
    """
    if type(continue_only) is not bool or not valid_tab_id(known_tab_id):
        raise ValueError("Invalid quiz start settings.")
    path = summary_path(course_id, quiz_id)
    if summary is None:
        summary = read_learner_summary(client, course_id=course_id, quiz_id=quiz_id)
    elif (summary.course_id, summary.quiz_id) != (course_id, quiz_id):
        raise ValueError("Invalid quiz start settings.")
    fields, resume = _start_fields(summary, continue_only=continue_only)
    post_url = client.canonical_url(path + "&cfql=0&inProgress=" + ("true" if resume else "false"))
    response = None
    try:
        # Any failure once the POST is sent, a session expiry included, is
        # unknown. The redirect's body is never read.
        response = client._request("POST", post_url, _skip_raise=True, stream=True,
                                   files=[(key, (None, value)) for key, value in fields],
                                   headers=_form_headers(client, path))
        if response.status_code != 302:
            raise LearnerStartUnknownError()
        location = response.headers.get("Location", "")
        _close_response(response)
        response = None
        attempt_id, page, process, start_frame = _follow_start(client, location, course_id, quiz_id, resume, path)
        learner_page_path(course_id, quiz_id, attempt_id, page)
        if not resume and page != 1:  # a new attempt opens on its first page
            raise LearnerStartUnknownError(attempt_id=attempt_id)
        try:
            tab_id = tab_id_of(process)
        except PreviewPageError:
            tab_id = None
            unclear = True
        else:
            unclear = False
        if tab_id is None:
            tab_id = known_tab_id if resume else ""
        try:
            if on_identity is not None:
                on_identity(attempt_id, page, tab_id)
            if unclear:
                raise LearnerStartUnknownError(attempt_id=attempt_id, page=page)
            return _open_attempt(client, course_id, quiz_id, attempt_id, page, tab_id, start_frame)
        except LearnerStartUnknownError:
            raise
        except Exception:  # the attempt exists; its page is unverified
            raise LearnerStartUnknownError(attempt_id=attempt_id, page=page) from None
    except LearnerStartUnknownError:
        raise
    except Exception:
        raise LearnerStartUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)


def _open_attempt(
    client: LighthouseClient, course_id: int, quiz_id: int, attempt_id: int, page: int, tab_id: str, start_frame: str,
) -> LearnerStart:
    """Load the attempt frame, its timer header and its page, as the process page's callback does."""
    frame_path = attempt_frame_path(course_id, quiz_id, attempt_id, page)
    frame, _ = _get_page(client, frame_path, _document_headers(client, start_frame))
    check_attempt_frame(frame, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
    try:
        sent = time.time()
        header, headers = _get_page(client, timer_path(course_id, quiz_id, attempt_id), _document_headers(client, frame_path))
        timer = _timer(header, headers, sent, quiz_id=quiz_id, attempt_id=attempt_id)
        view = AttemptView(course_id, quiz_id, attempt_id, page, tab_id, show_incorrect_only(header))
    except Exception:
        raise LearnerStartUnknownError(attempt_id=attempt_id, page=page, timer=True) from None
    return LearnerStart(_read_page(client, view, reload=False), view, timer)


def _read_page(client: LighthouseClient, view: AttemptView, *, reload: bool) -> LearnerPage:
    body, _ = _get_page(client, view.page_path(), _document_headers(client, view.referer(), reload=reload))
    return parse_learner_page(body, course_id=view.course_id, quiz_id=view.quiz_id, attempt_id=view.attempt_id,
                              page=view.page)


def read_learner_page(client: LighthouseClient, view: AttemptView) -> LearnerPage:
    """Reload the attempt frame's page, as the browser reloads it; never infer or move the server's cursor.

    ``view`` must show a page the server showed for this attempt (its start
    or continue page, or a Next or Previous readback): reading past the
    last page breaks the attempt.
    """
    return _read_page(client, view, reload=True)


def read_quiz_image(client: LighthouseClient, src: str) -> tuple[bytes, str]:
    """Download one question image (an ``images`` entry's ``src``) and its media type.

    Only images on the LMS itself are fetched, so the session never goes to
    another site. The bytes must be PNG, JPEG, GIF or WebP whatever the
    server's content type says.
    """
    if not isinstance(src, str) or not src or len(src) > 2048 or "\\" in src or not src.isprintable():
        raise PreviewRefusedError(REFUSE_IMAGE_SOURCE)
    try:
        # A browser never sends the fragment.
        parsed = urlparse(urldefrag(src).url)
        same_site = (parsed.scheme.lower() == "https" and parsed.hostname == urlparse(client.base_url).hostname
                     and parsed.username is None and parsed.password is None and parsed.port in (None, 443))
    except ValueError:
        raise PreviewRefusedError(REFUSE_IMAGE_SOURCE) from None
    if not (same_site or (not parsed.scheme and not parsed.netloc and src.startswith("/"))):
        raise PreviewRefusedError(REFUSE_IMAGE_SOURCE)
    path = "/" + posixpath.normpath(unquote(parsed.path)).lstrip("/").casefold() + "/"
    if path.startswith(_ACTION_ROUTES) or any(segment.endswith(".d2l") for segment in path.split("/")):
        raise PreviewRefusedError(REFUSE_IMAGE_ROUTE)
    url = client.base_url + parsed._replace(scheme="", netloc="").geturl()
    # Sent once: if the address is a page after all, it is not repeated.
    body, headers = client.get_raw(url, max_bytes=MAX_IMAGE_BYTES, _replay_safe=False)
    content_type = next((str(value) for key, value in headers.items() if key.lower() == "content-type"), "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not (media_type.startswith("image/") or media_type == "application/octet-stream"):
        raise NetworkError("The server did not return an image.")
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return body, "image/webp"
    for signature, sniffed in _IMAGE_SIGNATURES:
        if body.startswith(signature):
            return body, sniffed
    raise NetworkError("The image is not PNG, JPEG, GIF or WebP.")


def current_learner_page(
    client: LighthouseClient, current: LearnerPage | None, view: AttemptView,
) -> tuple[LearnerPage, FormProtection]:
    """The page to write from, with its protection: ``current`` if it is the view's page, else a fresh read.

    Passing the verified readback of the previous write saves a request.
    Only the first page is read without one, so a wrong page number is
    never requested.
    """
    if not isinstance(view, AttemptView):
        raise ValueError("Invalid quiz attempt view.")
    if current is None:
        if view.page != 1:
            raise ValueError("Pass the page's verified readback.")
        current = read_learner_page(client, view)
    elif not isinstance(current, LearnerPage) or (current.course_id, current.quiz_id, current.attempt_id, current.page) != (
            view.course_id, view.quiz_id, view.attempt_id, view.page):
        raise ValueError("Invalid quiz attempt identity.")
    if current._protection is None:
        raise PreviewRefusedError(REFUSE_PAGE_PROTECTION)
    return current, current._protection


def _saved_marks(value: tuple[str, ...], question_id: int) -> tuple[tuple[tuple[int, ...], tuple[int, ...]], ...]:
    """The saved and unsaved ids an autosave of ``value`` may be answered with: saved, or for a cleared answer unsaved."""
    if any(value):
        return (((question_id,), ()),)
    return (((), (question_id,)), ((), ()))


def save_learner_answers(
    client: LighthouseClient, view: AttemptView, *, answers: Mapping[int, object], current: LearnerPage | None = None,
    before_post: Callable[[], None] | None = None,
) -> tuple[LearnerPage, PageSnapshot]:
    """Save the given answers as the browser autosaves them: one request per changed question, each verified.

    Unmentioned questions keep their current answers, and an answer that is
    already saved as given is not sent again. Each save must be answered as
    the browser's page expects, then read back with every value as sent. A
    session expiry or any surprise after the first POST is unknown, and no
    POST is sent twice. ``before_post`` runs before each one and may refuse
    it. Returns the last readback and the page as the browser holds it,
    which the next form on this page is built from.
    """
    if not isinstance(answers, Mapping) or not answers:
        raise PreviewRefusedError(REFUSE_NO_ANSWERS)
    current, protection = current_learner_page(client, current, view)
    values = current.intended(answers)
    snapshot = current.snapshot()
    saved = {q["question_id"]: q["saved"] is True for q in current.questions}
    order = [qid for qid in values if qid in answers
             and (values[qid] != snapshot.values[qid] or (any(values[qid]) and not saved[qid]))]
    # Every form is built first with a stand-in protection, so a page the CLI
    # cannot send is refused before anything is sent.
    probe, scratch = snapshot, FormProtection(protection.csrf_token, protection.hit_code_seed)
    for qid in order:
        probe = probe.with_value(qid, values[qid])
        probe.autosave_fields(qid, scratch, view.tab_id)
        probe = probe.update_saved(*_saved_marks(values[qid], qid)[0])
    readback = current
    sent: list[int] = []
    for qid in order:
        if before_post is not None:
            before_post()
        snapshot = snapshot.with_value(qid, values[qid])
        fields = snapshot.autosave_fields(qid, protection, view.tab_id)
        try:
            marks = autosave_marker(_post_page(client, autosave_path(view.course_id), fields, view.page_path()))
            if marks not in _saved_marks(values[qid], qid):
                raise LearnerSaveUnknownError()
            snapshot = snapshot.update_saved(*marks)
            sent.append(qid)
            readback = read_learner_page(client, view)
            if not (readback.confirms(snapshot.values, sent) and readback.same_questions(current)
                    and readback.flags() == snapshot.flags):
                raise LearnerSaveUnknownError()
        except Exception:
            raise LearnerSaveUnknownError() from None
    return readback, snapshot


def _change_page(
    client: LighthouseClient, view: AttemptView, change: PageChange, fields: list[tuple[str, str]],
) -> tuple[LearnerPage, AttemptView]:
    """Send one page change once, then load its page as the reply has the browser do; any surprise after sending is unknown."""
    try:
        check_reload_marker(_post_page(client, change.post_path, fields, view.page_path()), change.target)
        return _read_page(client, change.view, reload=False), change.view
    except Exception:
        raise LearnerAdvanceUnknownError() from None


def _from_page(
    client: LighthouseClient, view: AttemptView, current: LearnerPage | None, snapshot: PageSnapshot | None,
) -> tuple[PageSnapshot, FormProtection]:
    """The page as the browser holds it, which a page change is built from, and its protection."""
    if snapshot is None:
        current, _ = current_learner_page(client, current, view)
        snapshot = current.snapshot()
    elif not isinstance(snapshot, PageSnapshot):
        raise ValueError("Invalid quiz page.")
    page = snapshot.page
    if (page.course_id, page.quiz_id, page.attempt_id, page.page) != (view.course_id, view.quiz_id, view.attempt_id, view.page):
        raise ValueError("Invalid quiz attempt identity.")
    if page._protection is None:
        raise PreviewRefusedError(REFUSE_PAGE_PROTECTION)
    return snapshot, page._protection


def advance_learner(
    client: LighthouseClient, view: AttemptView, *, allow_unanswered: bool = False, forward_only: bool | None = None,
    current: LearnerPage | None = None, snapshot: PageSnapshot | None = None,
) -> tuple[LearnerPage, AttemptView]:
    """Move forward one page, sending the page's answers as they stand.

    Only a visible, enabled Next control allows it: requesting a page past
    the last one permanently broke attempts. Its handler must be one the
    CLI sends exactly, agreeing with ``forward_only`` (the quiz setting)
    when that is known. The next page must then read back. Forward-only
    quizzes never return, so unanswered questions on this page are refused
    unless ``allow_unanswered`` is set. ``snapshot`` is the page as just
    saved, when it was.
    """
    if type(allow_unanswered) is not bool or (forward_only is not None and type(forward_only) is not bool):
        raise ValueError("Invalid page change settings.")
    snapshot, protection = _from_page(client, view, current, snapshot)
    change = plan_page_change(snapshot.page, view, forward=True, forward_only=forward_only, origin=client.base_url)
    fields = snapshot.advance_fields(protection, view.tab_id, allow_unanswered=allow_unanswered)
    return _change_page(client, view, change, fields)


def retreat_learner(
    client: LighthouseClient, view: AttemptView, *, current: LearnerPage | None = None,
) -> tuple[LearnerPage, AttemptView]:
    """Move back one page, sending the page's answers as they stand.

    Only a visible, enabled Previous control on a page after the first
    allows it, so no page below 1 is ever requested. The previous page must
    then read back.
    """
    snapshot, protection = _from_page(client, view, current, None)
    change = plan_page_change(snapshot.page, view, forward=False, forward_only=None, origin=client.base_url)
    fields = snapshot.retreat_fields(protection, view.tab_id)
    return _change_page(client, view, change, fields)
