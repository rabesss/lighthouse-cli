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
from urllib.parse import parse_qs, unquote, urldefrag, urlencode, urlparse

from bs4 import BeautifulSoup

from .api import LighthouseClient, NetworkError, _close_response
from .quiz_attempt_page import (
    MAX_PAGE_BYTES,
    REFUSE_NO_ANSWERS,
    LearnerPage,
    PreviewPageError,
    PreviewRefusedError,
    active_buttons,
    hidden_form,
    parse_learner_page,
    process_frame_src,
    start_frame_src,
    started_attempt,
)
from .request_protection import FormProtection, form_protection_from_homepage

ATTEMPT_ROUTE = "/d2l/lms/quizzing/user/attempt/"


class LearnerStartUnknownError(NetworkError):
    def __init__(self, *, attempt_id: int | None = None, page: int | None = None) -> None:
        self.attempt_id = attempt_id
        self.page = page
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


def _identity(*values: int) -> None:
    if any(type(value) is not int or not 0 < value < 10**18 for value in values):
        raise ValueError("Invalid quiz attempt identity.")


def learner_page_path(course_id: int, quiz_id: int, attempt_id: int, page: int) -> str:
    _identity(course_id, quiz_id, attempt_id, page)
    return ATTEMPT_ROUTE + "quiz_attempt_page_auto.d2l?" + urlencode({
        "ou": course_id, "isprv": "", "pg": page, "qi": quiz_id, "ai": attempt_id,
        "dnb": 0, "cfql": 0, "fromQB": 0, "d2l_body_type": 3,
    })


def _summary_path(course_id: int, quiz_id: int) -> str:
    _identity(course_id, quiz_id)
    return "/d2l/lms/quizzing/user/quiz_summary.d2l?" + urlencode({"ou": course_id, "qi": quiz_id, "cfql": 0})


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

    def public_data(self) -> dict[str, Any]:
        return {"course_id": self.course_id, "quiz_id": self.quiz_id, "can_start": self.can_start,
                "can_continue": self.can_continue, "attempt_in_progress": self.attempt_in_progress,
                "attempt_processing": self.attempt_processing}


def parse_learner_summary(body: bytes, *, course_id: int, quiz_id: int) -> LearnerSummary:
    """Read the summary page's start state as data; its scripts never run."""
    _identity(course_id, quiz_id)
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
        _button=labels[0] if labels else None, _protection=protection,
    )


def read_learner_summary(client: LighthouseClient, *, course_id: int, quiz_id: int) -> LearnerSummary:
    body, _ = client.get_raw(_summary_path(course_id, quiz_id), max_bytes=MAX_PAGE_BYTES,
                             _replay_safe=False, headers={"Cache-Control": "no-cache"})
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


def _timer_path(course_id: int, quiz_id: int, attempt_id: int) -> str:
    _identity(course_id, quiz_id, attempt_id)
    return ATTEMPT_ROUTE + "quiz_attempt_top_auto.d2l?" + urlencode({
        "ou": course_id, "isprv": "", "impcf": "", "qi": quiz_id, "ai": attempt_id,
        "dnb": 0, "cfql": 0, "fromQB": 0, "cft": "", "d2l_body_type": 3,
    })


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


def read_learner_timer(client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int) -> LearnerTimer | None:
    """The attempt's time limit from its timer frame, or ``None`` when its time is not enforced.

    The frame's start time is the server's. The response's ``Date`` gives
    the server clock's offset from the local one, so a local clock that is
    off still counts down to the server's limit.
    """
    sent = time.time()
    body, headers = client.get_raw(_timer_path(course_id, quiz_id, attempt_id), max_bytes=MAX_PAGE_BYTES,
                                   _replay_safe=False, headers={"Cache-Control": "no-cache"})
    date = _server_time(headers)
    # The Date is stamped in whole seconds after the request was sent, so the
    # server is at most this far ahead: the countdown never ends after its own.
    offset = 0.0 if date is None else date + 1 - sent
    timer = parse_learner_timer(body, quiz_id=quiz_id, attempt_id=attempt_id, now=time.time() + offset)
    return None if timer is None else dataclasses.replace(timer, clock_offset=offset)


def refuse_while_processing(summary: LearnerSummary) -> None:
    """Refuse to start or continue while Brightspace is still submitting an attempt whose time ran out."""
    if summary.attempt_processing is not None:
        raise PreviewRefusedError(REFUSE_START_PROCESSING.format(int(summary.attempt_processing)))


def _start_fields(summary: LearnerSummary, *, continue_only: bool) -> tuple[dict[str, str], bool]:
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
    fields = dict(summary._hidden_fields)
    if fields.get("hps") or fields.get("LockDownBrowserUrl", "0") != "0":
        raise PreviewRefusedError(REFUSE_START_BROWSER)
    protection = summary._protection
    if protection is None or fields.get("d2l_referrer") != protection.csrf_token:
        raise PreviewRefusedError(REFUSE_START_PROTECTION)
    fields.update(d2l_action="Custom", d2l_actionparam="1", d2l_hitCode=protection.next_hit_code())
    return fields, resume


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


def _follow_start(client: LighthouseClient, location: str, course_id: int, quiz_id: int, resume: bool) -> tuple[int, int]:
    """The start frames, then the hidden process page that names the attempt."""
    root_url = _start_target(client, location, "quiz_start_frame_auto.d2l", course_id, quiz_id, resume)
    root, _ = client.get_raw(root_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
    frame_url = _start_target(client, start_frame_src(root), "quiz_start_iframe_2_auto.d2l", course_id, quiz_id, resume)
    frame, _ = client.get_raw(frame_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers={"Referer": root_url})
    process_url = _start_target(client, process_frame_src(frame), "quiz_start_process_auto.d2l", course_id, quiz_id, resume)
    # This GET creates or reopens the attempt: it is never replayed.
    result, _ = client.get_raw(process_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers={"Referer": frame_url})
    return started_attempt(result)


def start_learner(
    client: LighthouseClient, *, course_id: int, quiz_id: int, continue_only: bool = False,
    on_identity: Callable[[int, int], None] | None = None, summary: LearnerSummary | None = None,
) -> LearnerPage:
    """Continue the attempt in progress, else start a new one (once).

    Starting a new attempt uses one of the learner's attempts, so callers
    confirm it first; ``continue_only`` refuses unless one is in progress.
    ``on_identity`` receives the attempt id and page before the readback,
    as for previews. A start whose outcome is unclear can be resolved from
    the summary, which then offers to continue that attempt. A ``summary``
    just read saves reading it again.
    """
    if type(continue_only) is not bool:
        raise ValueError("Invalid quiz start settings.")
    summary_path = _summary_path(course_id, quiz_id)
    if summary is None:
        summary = read_learner_summary(client, course_id=course_id, quiz_id=quiz_id)
    elif (summary.course_id, summary.quiz_id) != (course_id, quiz_id):
        raise ValueError("Invalid quiz start settings.")
    fields, resume = _start_fields(summary, continue_only=continue_only)
    post_url = client.canonical_url(summary_path + "&" + urlencode({"inProgress": "true" if resume else "false"}))
    response = None
    try:
        # Any failure once the POST is sent, a session expiry included, is unknown.
        response = client._request("POST", post_url, _skip_raise=True,
                                   files=[(key, (None, value)) for key, value in fields.items()],
                                   headers={"Referer": client.canonical_url(summary_path)})
        if response.status_code != 302:
            raise LearnerStartUnknownError()
        location = response.headers.get("Location", "")
        _close_response(response)
        response = None
        attempt_id, page = _follow_start(client, location, course_id, quiz_id, resume)
        learner_page_path(course_id, quiz_id, attempt_id, page)
        if not resume and page != 1:  # a new attempt opens on its first page
            raise LearnerStartUnknownError(attempt_id=attempt_id)
        try:
            if on_identity is not None:
                on_identity(attempt_id, page)
            return read_learner_page(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
        except Exception:  # the attempt exists; its page is unverified
            raise LearnerStartUnknownError(attempt_id=attempt_id, page=page) from None
    except LearnerStartUnknownError:
        raise
    except Exception:
        raise LearnerStartUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)


def read_learner_page(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> LearnerPage:
    """Read one page of the attempt; never infer or move the server's cursor.

    ``page`` must be one the server showed for this attempt (its start or
    continue page, or a Next or Previous readback): reading past the last
    page breaks the attempt.
    """
    body, _ = client.get_raw(learner_page_path(course_id, quiz_id, attempt_id, page), max_bytes=MAX_PAGE_BYTES,
                             _replay_safe=False, headers={"Cache-Control": "no-cache"})
    return parse_learner_page(body, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)


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
    client: LighthouseClient, current: LearnerPage | None, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> tuple[LearnerPage, FormProtection]:
    """The page to write from, with its protection: ``current`` if it is this page, else a fresh read.

    Passing the verified readback of the previous write saves a request.
    Only the first page is read without one, so a wrong page number is
    never requested.
    """
    identity = (course_id, quiz_id, attempt_id, page)
    if current is None:
        if page != 1:
            raise ValueError("Pass the page's verified readback.")
        current = read_learner_page(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
    elif not isinstance(current, LearnerPage) or (current.course_id, current.quiz_id, current.attempt_id, current.page) != identity:
        raise ValueError("Invalid quiz attempt identity.")
    if current._protection is None:
        raise PreviewRefusedError(REFUSE_PAGE_PROTECTION)
    return current, current._protection


def _post_form(client: LighthouseClient, url: str, fields: dict[str, str], referer: str) -> Any:
    return client._request("POST", client.canonical_url(url),
                           files=[(key, (None, value)) for key, value in fields.items()],
                           headers={"Referer": client.canonical_url(referer)})


def save_learner_answers(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
    answers: Mapping[int, object], current: LearnerPage | None = None,
) -> LearnerPage:
    """Save every given answer of one page in a single request, then verify.

    Unmentioned questions keep their current answers. Success needs a fresh
    readback with every value as sent; a session expiry or any surprise
    after the POST is unknown, and the POST is never sent twice.
    """
    if not isinstance(answers, Mapping) or not answers:
        raise PreviewRefusedError(REFUSE_NO_ANSWERS)
    current, protection = current_learner_page(client, current, course_id=course_id, quiz_id=quiz_id,
                                               attempt_id=attempt_id, page=page)
    values = current.intended(answers)
    fields = current.save_fields(values, protection)
    response = None
    try:
        response = _post_form(client, ATTEMPT_ROUTE + "quiz_attempt_save_auto.d2l?" + urlencode(
            {"d2l_body_type": 3, "ou": course_id, "fromQB": 0}), fields, learner_page_path(course_id, quiz_id, attempt_id, page))
        if response.status_code != 200:
            raise LearnerSaveUnknownError()
        _close_response(response)
        response = None
        verified = read_learner_page(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
        if not verified.confirms(values, answers.keys()):
            raise LearnerSaveUnknownError()
        return verified
    except Exception:
        raise LearnerSaveUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)


def _change_page(
    client: LighthouseClient, fields: dict[str, str], query: Mapping[str, int], *, course_id: int, quiz_id: int,
    attempt_id: int, page: int, target: int,
) -> LearnerPage:
    """Send one page change once, then read ``target`` back; any surprise after sending is unknown."""
    response = None
    try:
        response = _post_form(client, ATTEMPT_ROUTE + "quiz_attempt_save_auto.d2l?" + urlencode(query), fields,
                              learner_page_path(course_id, quiz_id, attempt_id, page))
        if response.status_code != 200:
            raise LearnerAdvanceUnknownError()
        _close_response(response)
        response = None
        return read_learner_page(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=target)
    except Exception:
        raise LearnerAdvanceUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)


def advance_learner(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
    allow_unanswered: bool = False, current: LearnerPage | None = None,
) -> LearnerPage:
    """Move forward one page, sending the page's answers as they stand.

    Only a visible, enabled Next control allows it: requesting a page past
    the last one permanently broke attempts. The next page must then read
    back. Forward-only quizzes never return, so unanswered questions on this
    page are refused unless ``allow_unanswered`` is set.
    """
    if type(allow_unanswered) is not bool:
        raise ValueError("Invalid page change settings.")
    current, protection = current_learner_page(client, current, course_id=course_id, quiz_id=quiz_id,
                                               attempt_id=attempt_id, page=page)
    fields = current.advance_fields(protection, allow_unanswered=allow_unanswered)
    return _change_page(client, fields, {"cfql": 0, "fromQB": 0, "d2l_body_type": 3, "ou": course_id},
                        course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page, target=page + 1)


def retreat_learner(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
    current: LearnerPage | None = None,
) -> LearnerPage:
    """Move back one page, sending the page's answers as they stand.

    Only a visible, enabled Previous control on a page after the first
    allows it, so no page below 1 is ever requested. The browser's Previous
    Page button posts without the ``cfql`` and ``fromQB`` that Next sends.
    The previous page must then read back.
    """
    current, protection = current_learner_page(client, current, course_id=course_id, quiz_id=quiz_id,
                                               attempt_id=attempt_id, page=page)
    fields = current.retreat_fields(protection)
    return _change_page(client, fields, {"d2l_body_type": 3, "ou": course_id},
                        course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page, target=page - 1)
