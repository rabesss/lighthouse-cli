"""Submit the signed-in learner's own attempt once and verify it was recorded.

Learners cannot read attempt records over REST (403), so completion is
checked on two pages instead: the receipt, and the attempt's row in the
learner's submissions list, which marks an unsubmitted attempt "In progress".
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

from .api import LighthouseClient, NetworkError, SessionExpiredError, _close_response
from .quiz_attempt_page import (
    MAX_PAGE_BYTES,
    LearnerPage,
    active_buttons,
    hidden_form,
    rpc_script,
    unanswered_questions,
)
from .quiz_learner_browser import (
    ATTEMPT_ROUTE,
    AttemptView,
    attempt_frame_path,
    check_finish_marker,
    finish_path,
    identity,
    rpc_body,
)
from .quiz_learner_transport import _document_headers, _post_page, current_learner_page
from .request_protection import FormProtection

REFUSE_QUIZ_UNANSWERED = ("The quiz has unanswered questions, so it was not submitted. "
                          "Answer them, or explicitly allow submitting with unanswered questions.")
NOT_SUBMITTED = "This attempt is still in progress, so it has not been submitted. Run attempt start to continue it."
_RECEIPT_HEADING = "Your work has been saved and submitted"


class LearnerSubmitUnknownError(NetworkError):
    def __init__(self) -> None:
        super().__init__("Quiz submission could not be verified. Check the quiz's submissions before retrying.")


class LearnerNotSubmittedError(ValueError):
    """The submissions list shows the attempt in progress."""

    def __init__(self) -> None:
        super().__init__(NOT_SUBMITTED)


class LearnerUnansweredError(ValueError):
    """The page's answers were saved, but the quiz was not submitted.

    Not a ``PreviewRefusedError``: that is raised before any write, and the
    page save has been sent. ``questions`` lists each unanswered question's
    page, id and number.
    """

    def __init__(self, questions: list[dict[str, int]]) -> None:
        self.questions = questions
        super().__init__(REFUSE_QUIZ_UNANSWERED)


def receipt_path(course_id: int, quiz_id: int, attempt_id: int) -> str:
    identity(course_id, quiz_id, attempt_id)
    return "/d2l/lms/quizzing/user/quiz_submissions_attempt.d2l?" + urlencode({
        "qi": quiz_id, "ai": attempt_id, "isInPopup": 0, "isTimeUp": 0, "isprv": 0, "dnb": 0,
        "cfql": 0, "cft": "", "fromQB": 0, "d2l_body_type": 1, "ou": course_id,
    })


def _attempt_row(body: bytes, *, course_id: int, quiz_id: int, attempt_id: int) -> Any:
    """The submissions-list row that links to exactly this attempt."""
    rows = []
    for link in BeautifulSoup(body, "html.parser").find_all("a", href=True):
        parsed = urlparse(str(link["href"]))
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.path.endswith("/quiz_submissions_attempt.d2l")
                and query.get("ai") == [str(attempt_id)] and query.get("qi") == [str(quiz_id)]
                and query.get("ou") == [str(course_id)]):
            rows.append((link, link.find_parent("tr")))
    if len(rows) != 1 or rows[0][1] is None:
        raise LearnerSubmitUnknownError()
    return rows[0]


def verify_learner_submission(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, frame_page: int | None = None,
    reload: bool = True,
) -> dict[str, Any]:
    """Read-only: whether this attempt is submitted, from its list row and receipt.

    Raises ``LearnerNotSubmittedError`` when the list shows the attempt in
    progress (its receipt is then not read), and ``LearnerSubmitUnknownError``
    unless both pages agree it was submitted. The points and percentage are
    reported only when the quiz shows them to learners. The receipt loads
    as from the attempt frame opened on ``frame_page``, when the CLI has
    opened one: as the browser loads it once the quiz is submitted, or,
    with ``reload``, as a reload of that page.
    """
    try:
        identity(course_id, quiz_id, attempt_id)
        if frame_page is None:
            receipt_headers = _document_headers(client, reload=True)
        else:
            receipt_headers = _document_headers(client, attempt_frame_path(course_id, quiz_id, attempt_id, frame_page),
                                                reload=reload)
        listing, _ = client.get_raw("/d2l/lms/quizzing/user/quiz_submissions.d2l?" + urlencode({"ou": course_id, "qi": quiz_id}),
                                    max_bytes=MAX_PAGE_BYTES, _replay_safe=False,
                                    headers=_document_headers(client, reload=True))
        link, row = _attempt_row(listing, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id)
        if "in progress" in " ".join(row.get_text(" ", strip=True).split()).casefold():
            raise LearnerNotSubmittedError()
        number = re.fullmatch(r"Attempt ([0-9]{1,6})", link.get_text(" ", strip=True))
        body, _ = client.get_raw(receipt_path(course_id, quiz_id, attempt_id), max_bytes=MAX_PAGE_BYTES,
                                 _replay_safe=False, headers=receipt_headers)
        soup = BeautifulSoup(body, "html.parser")
        if (number is None or not any(h.get_text(" ", strip=True) == _RECEIPT_HEADING for h in soup.find_all("h2"))
                or "still in progress" in soup.get_text(" ", strip=True).casefold()):
            raise LearnerSubmitUnknownError()
        grade = row.find("td", class_="d_gn")
        cell = " ".join(grade.get_text(" ", strip=True).split()) if grade is not None else ""
        score = re.match(r"([0-9]{1,9}(?:\.[0-9]{1,4})?) / ([0-9]{1,9}(?:\.[0-9]{1,4})?)(?: |$)", cell)
        # "P %" alone or "[N] / [M] - P %" (points hidden or shown); a decimal comma reads as a point.
        percent = re.fullmatch(r"(?:[0-9.,]{0,15} ?/ ?[0-9.,]{0,15} - )?([0-9]{1,4}(?:[.,][0-9]{1,4})?) %", cell)
        return {"mode": "learner", "course_id": course_id, "quiz_id": quiz_id, "attempt_id": attempt_id,
                "submitted": True, "attempt_number": int(number[1]),
                "score": float(score[1]) if score else None, "out_of": float(score[2]) if score else None,
                "percent": float(percent[1].replace(",", ".")) if percent else None}
    except (SessionExpiredError, LearnerNotSubmittedError):
        raise
    except Exception:
        raise LearnerSubmitUnknownError() from None


def _confirmation(client: LighthouseClient, protection: FormProtection, view: AttemptView,
                  referer: str) -> tuple[dict[str, str], list[dict[str, int]]]:
    """The confirmation page the finish save's reply opens, its fields and the quiz's unanswered questions."""
    path = ATTEMPT_ROUTE + "quiz_confirm_submit_auto.d2l?" + urlencode({
        "qi": view.quiz_id, "ai": view.attempt_id, "isprv": "", "drc": "", "impcf": "", "dnb": 0, "cfql": 0,
        "btlp": view.page, "fromQB": 0, "cft": "", "d2l_body_type": 3, "ou": view.course_id,
    })
    body, _ = client.get_raw(path, max_bytes=MAX_PAGE_BYTES, _replay_safe=False,
                             headers=_document_headers(client, referer))
    form, fields = hidden_form(body)
    # A learner's page has no can-be-graded checkbox (that is preview-only),
    # and a secure-browser attempt is never submitted over plain HTTP.
    if (form.select('input[type="checkbox"]')
            or active_buttons(form, {"Submit Quiz"}) != ["Submit Quiz"]
            or fields.get("d2l_referrer") != protection.csrf_token
            or fields.get("HDN_isUsingRldb") != "0"):
        raise LearnerSubmitUnknownError()
    return fields, unanswered_questions(form, quiz_id=view.quiz_id, attempt_id=view.attempt_id)


def submit_learner(
    client: LighthouseClient, view: AttemptView, *, allow_unanswered: bool = False, current: LearnerPage | None = None,
) -> dict[str, Any]:
    """Submit from the last page, once, and verify the receipt and list row.

    The confirmation page lists every unanswered question of the quiz; any
    are refused before the final request unless ``allow_unanswered`` is set.
    A refusal here sent only the page's answers, as the browser's save does.
    The final request is the attempt frame's own submission call.
    """
    if type(allow_unanswered) is not bool:
        raise ValueError("Invalid quiz submission settings.")
    current, protection = current_learner_page(client, current, view)
    fields = current.snapshot().finish_fields(protection, view.tab_id)
    # Its encoding is checked before anything is sent.
    rpc_body(view.quiz_id, view.attempt_id, rldb=False, token=protection.csrf_token, hit_code="0")
    response = None
    try:
        finish = finish_path(view.course_id)
        check_finish_marker(_post_page(client, finish, fields, view.page_path()), current)
        confirmation, unanswered = _confirmation(client, protection, view, finish)
        if unanswered and not allow_unanswered:
            raise LearnerUnansweredError(unanswered)
        # isRldbUse is the browser's Boolean() of a non-empty string.
        body = rpc_body(view.quiz_id, view.attempt_id, rldb=bool(confirmation.get("HDN_isRldbUse", "")),
                        token=protection.csrf_token, hit_code=protection.next_hit_code())
        rpc = attempt_frame_path(view.course_id, view.quiz_id, view.attempt_id, view.frame_page, rpc=True)
        response = client._request("POST", client.canonical_url(rpc), stream=True, data=body,
                                   headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": client.base_url,
                                            "Referer": client.canonical_url(view.frame_path())})
        expected = f"parent.QuizDone({view.quiz_id},{view.attempt_id},'0','0','0','gotoSv','')"
        if response.status_code != 200 or rpc_script(response.iter_content(chunk_size=8192)) != expected:
            raise LearnerSubmitUnknownError()
        _close_response(response)
        response = None
        # Still listed in progress after the final request: unknown, not
        # "not submitted", as the request was sent.
        return verify_learner_submission(client, course_id=view.course_id, quiz_id=view.quiz_id,
                                         attempt_id=view.attempt_id, frame_page=view.frame_page, reload=False)
    except (LearnerUnansweredError, LearnerSubmitUnknownError):
        raise
    except Exception:  # sent once: a session expiry is unknown too
        raise LearnerSubmitUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)
