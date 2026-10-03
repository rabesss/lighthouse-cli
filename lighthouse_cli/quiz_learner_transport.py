"""Start, read, save and move through the signed-in learner's own quiz attempt.

Brightspace has no learner REST route for an attempt, so these are the
legacy HTML endpoints its browser pages use, each response validated
strictly. Every write is sent once. An outcome that cannot be verified is
reported as unknown and never retried here: the caller re-reads the server.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

from .api import LighthouseClient, NetworkError, SessionExpiredError, _close_response
from .quiz_attempt_page import (
    MAX_PAGE_BYTES,
    REFUSE_NO_ANSWERS,
    LearnerPage,
    PreviewPageError,
    PreviewRefusedError,
    active_buttons,
    hidden_form,
    parse_learner_page,
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
        super().__init__("Page change could not be verified. Read the quiz state before continuing.")


REFUSE_START_UNAVAILABLE = "This quiz cannot be started or continued now (closed, not yet open or out of attempts)."
REFUSE_NOTHING_IN_PROGRESS = "No attempt of this quiz is in progress."
REFUSE_START_PASSWORD = "This quiz needs a password, which the CLI does not support."  # pragma: allowlist secret
REFUSE_START_BROWSER = "This quiz needs a browser step (such as LockDown Browser) that the CLI cannot do."
REFUSE_START_ROLE = "Brightspace does not let an impersonated role take a quiz."
REFUSE_START_PROTECTION = "Quiz form protection could not be verified. Nothing was started."
REFUSE_PAGE_PROTECTION = "Form protection could not be read from the quiz page. Nothing was sent."

_SUMMARY_FLAGS = ("isImpersonatingRole", "canTakeQuiz", "startQuiz", "continueQuiz", "hasPass")


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
    """The quiz summary's own start state. Private fields are secret-bearing."""

    course_id: int
    quiz_id: int
    can_start: bool
    can_continue: bool
    attempt_in_progress: int | None
    _flags: dict[str, bool] = field(repr=False, compare=False)
    _hidden_fields: dict[str, str] = field(repr=False, compare=False)
    _password_input: bool = field(repr=False, compare=False)
    _button: str | None = field(repr=False, compare=False)
    _protection: FormProtection | None = field(repr=False, compare=False)

    def public_data(self) -> dict[str, Any]:
        return {"course_id": self.course_id, "quiz_id": self.quiz_id, "can_start": self.can_start,
                "can_continue": self.can_continue, "attempt_in_progress": self.attempt_in_progress}


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
    if len(progress) > 1 or bool(progress) != flags["continueQuiz"]:
        raise PreviewPageError()
    try:
        protection: FormProtection | None = form_protection_from_homepage(body)
    except ValueError:
        protection = None
    takeable = flags["canTakeQuiz"] and not flags["isImpersonatingRole"]
    return LearnerSummary(
        course_id, quiz_id,
        can_start=takeable and flags["startQuiz"] and not flags["continueQuiz"],
        can_continue=takeable and flags["continueQuiz"] and not flags["startQuiz"],
        attempt_in_progress=int(progress[0]) if progress else None,
        _flags=flags, _hidden_fields=hidden,
        _password_input=bool(form.select('input[type="password"]') or soup.select('input[name="password"]')),
        _button=labels[0] if labels else None, _protection=protection,
    )


def read_learner_summary(client: LighthouseClient, *, course_id: int, quiz_id: int) -> LearnerSummary:
    body, _ = client.get_raw(_summary_path(course_id, quiz_id), max_bytes=MAX_PAGE_BYTES,
                             _replay_safe=False, headers={"Cache-Control": "no-cache"})
    return parse_learner_summary(body, course_id=course_id, quiz_id=quiz_id)


def _start_fields(summary: LearnerSummary, *, continue_only: bool) -> tuple[dict[str, str], bool]:
    """The summary form's start or continue action, or a fixed refusal."""
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


def _attempt_identity(body: bytes) -> tuple[int, int]:
    matches: set[tuple[int, int]] = set()
    for script in BeautifulSoup(body, "html.parser").find_all("script"):
        for match in re.finditer(
            r"^\s*parent\.GoToAttemptQuizAuto\(\s*([0-9]{1,18})\s*,\s*([0-9]{1,6})\s*,\s*0\s*\)\s*;?\s*$",
            script.get_text(), re.MULTILINE,
        ):
            matches.add((int(match[1]), int(match[2])))
    if len(matches) != 1:
        raise LearnerStartUnknownError()
    return matches.pop()


def _follow_start(client: LighthouseClient, location: str, course_id: int, quiz_id: int, resume: bool) -> tuple[int, int]:
    """The start frames, then the hidden process page that names the attempt."""
    root_url = _start_target(client, location, "quiz_start_frame_auto.d2l", course_id, quiz_id, resume)
    root, _ = client.get_raw(root_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
    candidates = [
        src for frame in BeautifulSoup(root, "html.parser").find_all("iframe")
        if isinstance(src := frame.get("src"), str) and urlparse(src).path.endswith("/quiz_start_iframe_2_auto.d2l")
    ]
    if len(candidates) != 1:
        raise LearnerStartUnknownError()
    frame_url = _start_target(client, candidates[0], "quiz_start_iframe_2_auto.d2l", course_id, quiz_id, resume)
    frame, _ = client.get_raw(frame_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers={"Referer": root_url})
    hidden_frames = BeautifulSoup(frame, "html.parser").select('iframe[name="hiddenFrame"], frame[name="hiddenFrame"]')
    process_src = hidden_frames[0].get("src") if len(hidden_frames) == 1 else None
    if not isinstance(process_src, str):
        raise LearnerStartUnknownError()
    process_url = _start_target(client, process_src, "quiz_start_process_auto.d2l", course_id, quiz_id, resume)
    # This GET creates or reopens the attempt: it is never replayed.
    result, _ = client.get_raw(process_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers={"Referer": frame_url})
    return _attempt_identity(result)


def start_learner(
    client: LighthouseClient, *, course_id: int, quiz_id: int, continue_only: bool = False,
    on_identity: Callable[[int, int], None] | None = None,
) -> LearnerPage:
    """Continue the attempt in progress, else start a new one (once).

    Starting a new attempt uses one of the learner's attempts, so callers
    confirm it first; ``continue_only`` refuses unless one is in progress.
    ``on_identity`` receives the attempt id and page before the readback,
    as for previews. A start whose outcome is unclear can be resolved from
    the summary, which then offers to continue that attempt.
    """
    if type(continue_only) is not bool:
        raise ValueError("Invalid quiz start settings.")
    summary_path = _summary_path(course_id, quiz_id)
    summary = read_learner_summary(client, course_id=course_id, quiz_id=quiz_id)
    fields, resume = _start_fields(summary, continue_only=continue_only)
    post_url = client.canonical_url(summary_path + "&" + urlencode({"inProgress": "true" if resume else "false"}))
    response = None
    dispatched = False
    attempt_id: int | None = None
    page: int | None = None
    try:
        dispatched = True
        response = client._request("POST", post_url, _skip_raise=True,
                                   files=[(key, (None, value)) for key, value in fields.items()],
                                   headers={"Referer": client.canonical_url(summary_path)})
        if response.status_code != 302:
            raise LearnerStartUnknownError()
        location = response.headers.get("Location", "")
        _close_response(response)
        response = None
        attempt_id, page = _follow_start(client, location, course_id, quiz_id, resume)
        try:
            learner_page_path(course_id, quiz_id, attempt_id, page)
        except ValueError:
            raise LearnerStartUnknownError() from None
        if not resume and page != 1:  # a new attempt opens on its first page
            raise LearnerStartUnknownError(attempt_id=attempt_id)
        if on_identity is not None:
            try:
                on_identity(attempt_id, page)
            except Exception:
                raise LearnerStartUnknownError(attempt_id=attempt_id, page=page) from None
        try:
            return read_learner_page(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
        except Exception:  # the attempt exists; its page is unverified
            raise LearnerStartUnknownError(attempt_id=attempt_id, page=page) from None
    except LearnerStartUnknownError:
        raise
    except SessionExpiredError:
        if dispatched:
            raise LearnerStartUnknownError(attempt_id=attempt_id, page=page) from None
        raise
    except Exception:
        raise LearnerStartUnknownError(attempt_id=attempt_id, page=page) from None
    finally:
        if response is not None:
            _close_response(response)


def read_learner_page(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> LearnerPage:
    """Read one page of the attempt; never infer or move the server's cursor.

    ``page`` must be one the server showed for this attempt (its start or
    continue page, or a Next readback): reading past the last page breaks
    the attempt.
    """
    body, _ = client.get_raw(learner_page_path(course_id, quiz_id, attempt_id, page), max_bytes=MAX_PAGE_BYTES,
                             _replay_safe=False, headers={"Cache-Control": "no-cache"})
    return parse_learner_page(body, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)


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
    dispatched = False
    try:
        dispatched = True
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
    except SessionExpiredError:
        if dispatched:
            raise LearnerSaveUnknownError() from None
        raise
    except Exception:
        raise LearnerSaveUnknownError() from None
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
    response = None
    dispatched = False
    try:
        dispatched = True
        response = _post_form(client, ATTEMPT_ROUTE + "quiz_attempt_save_auto.d2l?" + urlencode(
            {"cfql": 0, "fromQB": 0, "d2l_body_type": 3, "ou": course_id}), fields,
            learner_page_path(course_id, quiz_id, attempt_id, page))
        if response.status_code != 200:
            raise LearnerAdvanceUnknownError()
        _close_response(response)
        response = None
        return read_learner_page(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page + 1)
    except SessionExpiredError:
        if dispatched:
            raise LearnerAdvanceUnknownError() from None
        raise
    except Exception:
        raise LearnerAdvanceUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)
