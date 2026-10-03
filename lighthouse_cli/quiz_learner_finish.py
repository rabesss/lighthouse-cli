"""Submit the signed-in learner's own attempt once and verify it was recorded.

Learners cannot read attempt records over REST (403), so completion is
checked on two pages instead: the receipt, and the attempt's row in the
learner's submissions list, which marks an unsubmitted attempt "In progress".
"""

from __future__ import annotations

import json
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
from .quiz_learner_transport import (
    ATTEMPT_ROUTE,
    current_learner_page,
    learner_page_path,
)
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
    learner_page_path(course_id, quiz_id, attempt_id, 1)
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


def verify_learner_submission(client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int) -> dict[str, Any]:
    """Read-only: whether this attempt is submitted, from its list row and receipt.

    Raises ``LearnerNotSubmittedError`` when the list shows the attempt in
    progress (its receipt is then not read), and ``LearnerSubmitUnknownError``
    unless both pages agree it was submitted. The score is reported only
    when the quiz shows it to learners.
    """
    try:
        listing, _ = client.get_raw("/d2l/lms/quizzing/user/quiz_submissions.d2l?" + urlencode({"ou": course_id, "qi": quiz_id}),
                                    max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers={"Cache-Control": "no-cache"})
        link, row = _attempt_row(listing, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id)
        if "in progress" in " ".join(row.get_text(" ", strip=True).split()).casefold():
            raise LearnerNotSubmittedError()
        number = re.fullmatch(r"Attempt ([0-9]{1,6})", link.get_text(" ", strip=True))
        body, _ = client.get_raw(receipt_path(course_id, quiz_id, attempt_id), max_bytes=MAX_PAGE_BYTES,
                                 _replay_safe=False, headers={"Cache-Control": "no-cache"})
        soup = BeautifulSoup(body, "html.parser")
        if (number is None or not any(h.get_text(" ", strip=True) == _RECEIPT_HEADING for h in soup.find_all("h2"))
                or "still in progress" in soup.get_text(" ", strip=True).casefold()):
            raise LearnerSubmitUnknownError()
        grade = row.find("td", class_="d_gn")
        score = re.match(r"([0-9]{1,9}(?:\.[0-9]{1,4})?) / ([0-9]{1,9}(?:\.[0-9]{1,4})?)(?: |$)",
                         " ".join(grade.get_text(" ", strip=True).split()) if grade is not None else "")
        return {"mode": "learner", "course_id": course_id, "quiz_id": quiz_id, "attempt_id": attempt_id,
                "submitted": True, "attempt_number": int(number[1]),
                "score": float(score[1]) if score else None, "out_of": float(score[2]) if score else None}
    except (SessionExpiredError, LearnerNotSubmittedError):
        raise
    except Exception:
        raise LearnerSubmitUnknownError() from None


def _confirmation(client: LighthouseClient, protection: FormProtection, *, course_id: int, quiz_id: int,
                  attempt_id: int, page: int) -> tuple[dict[str, str], list[dict[str, int]]]:
    """The confirmation page's fields and the quiz's unanswered questions."""
    path = ATTEMPT_ROUTE + "quiz_confirm_submit_auto.d2l?" + urlencode({
        "qi": quiz_id, "ai": attempt_id, "isprv": "", "drc": "", "impcf": "", "dnb": 0, "cfql": 0,
        "btlp": page, "fromQB": 0, "cft": "", "d2l_body_type": 3, "ou": course_id,
    })
    body, _ = client.get_raw(path, max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
    form, fields = hidden_form(body)
    # A learner's page has no can-be-graded checkbox (that is preview-only),
    # and a secure-browser attempt is never submitted over plain HTTP.
    if (form.select('input[type="checkbox"]')
            or active_buttons(form, {"Submit Quiz"}) != ["Submit Quiz"]
            or fields.get("d2l_referrer") != protection.csrf_token
            or fields.get("HDN_isUsingRldb") != "0"):
        raise LearnerSubmitUnknownError()
    return fields, unanswered_questions(form, quiz_id=quiz_id, attempt_id=attempt_id)


def submit_learner(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
    allow_unanswered: bool = False, current: LearnerPage | None = None,
) -> dict[str, Any]:
    """Submit from the last page, once, and verify the receipt and list row.

    The confirmation page lists every unanswered question of the quiz; any
    are refused before the final request unless ``allow_unanswered`` is set.
    A refusal here sent only the page's answers, as the browser's save does.
    """
    if type(allow_unanswered) is not bool:
        raise ValueError("Invalid quiz submission settings.")
    current, protection = current_learner_page(client, current, course_id=course_id, quiz_id=quiz_id,
                                               attempt_id=attempt_id, page=page)
    fields = current.finish_fields(protection)
    response = None
    try:
        save_url = ATTEMPT_ROUTE + "quiz_attempt_save_auto.d2l?" + urlencode(
            {"dnb": 0, "cfql": 0, "fromQB": 0, "d2l_body_type": 3, "ou": course_id})
        response = client._request("POST", client.canonical_url(save_url),
                                   files=[(key, (None, value)) for key, value in fields.items()],
                                   headers={"Referer": client.canonical_url(learner_page_path(course_id, quiz_id, attempt_id, page))})
        if response.status_code != 200:
            raise LearnerSubmitUnknownError()
        _close_response(response)
        response = None
        confirmation, unanswered = _confirmation(client, protection, course_id=course_id, quiz_id=quiz_id,
                                                 attempt_id=attempt_id, page=page)
        if unanswered and not allow_unanswered:
            raise LearnerUnansweredError(unanswered)
        context = {"qi": quiz_id, "ai": attempt_id, "isprv": "", "dnb": 0, "drc": "", "pg": page,
                   "cfql": 0, "rldbsv": 1, "fromQB": 0, "cft": "", "d2l_body_type": 1, "ou": course_id}
        frame = ATTEMPT_ROUTE + "quiz_attempt_iframe_auto.d2l"
        # isPreview, canBeGraded, isRldbUse (the browser's Boolean() of a
        # non-empty string), shouldAutoSubmit and cameFromTab, as observed.
        params = {"param1": str(quiz_id), "param2": str(attempt_id), "param3": False, "param4": True,
                  "param5": bool(confirmation.get("HDN_isRldbUse", "")), "param6": False, "param7": ""}
        response = client._request("POST", client.canonical_url(frame + "file?" + urlencode({**context, "d2l_rh": "rpc", "d2l_rt": "call"})),
                                   stream=True,
                                   data={"d2l_rf": "ProcessQuizSubmission", "params": json.dumps(params, separators=(",", ":")),
                                         "d2l_referrer": protection.csrf_token, "d2l_hitcode": protection.next_hit_code(),
                                         "d2l_action": "rpc"},
                                   headers={"Referer": client.canonical_url(frame + "?" + urlencode(context))})
        expected = f"parent.QuizDone({quiz_id},{attempt_id},'0','0','0','gotoSv','')"
        if response.status_code != 200 or rpc_script(response.iter_content(chunk_size=8192)) != expected:
            raise LearnerSubmitUnknownError()
        _close_response(response)
        response = None
        # Still listed in progress after the final request: unknown, not
        # "not submitted", as the request was sent.
        return verify_learner_submission(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id)
    except (LearnerUnansweredError, LearnerSubmitUnknownError):
        raise
    except Exception:  # sent once: a session expiry is unknown too
        raise LearnerSubmitUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)
