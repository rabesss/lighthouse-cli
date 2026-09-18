"""Verified manual submission of a completed instructor preview.

The legacy RPC returns JavaScript. We validate one known completion callback
as data, and never execute the response. A separate receipt GET is required.
"""

from __future__ import annotations

import json
import re
import math
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from .api import LighthouseClient, NetworkError, _close_response
from .quiz_attempt_page import MAX_PAGE_BYTES, PreviewPageError, hidden_form
from .quiz_preview_transport import page_path, read_current_preview
from .request_protection import form_protection_from_homepage


class PreviewSubmitUnknownError(NetworkError):
    def __init__(self) -> None:
        super().__init__("Preview submission could not be verified. Inspect the result before retrying.")


def receipt_path(course_id: int, quiz_id: int, attempt_id: int) -> str:
    page_path(course_id, quiz_id, attempt_id, 1)
    return "/d2l/lms/quizzing/user/quiz_submissions_attempt.d2l?" + urlencode({
        "qi": quiz_id, "ai": attempt_id, "isprv": 1, "ou": course_id, "d2l_body_type": 1,
    })


def verify_receipt(client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, actor_id: int | None = None) -> dict[str, Any]:
    try:
        body, _ = client.get_raw(receipt_path(course_id, quiz_id, attempt_id), max_bytes=MAX_PAGE_BYTES,
                                 _replay_safe=False, headers={"Cache-Control": "no-cache"})
        soup = BeautifulSoup(body, "html.parser")
        if not any(h.get_text(" ", strip=True) == "Your work has been saved and submitted" for h in soup.find_all("h2")):
            raise PreviewSubmitUnknownError()
        # Do not mistake a quiz title or reflected text for a receipt. Require
        # the independent attempt record to confirm identity and completion.
        detail = client.get_json(f"/{course_id}/quizzes/{quiz_id}/attempts/{attempt_id}", _replay_safe=False)
        if (not isinstance(detail, dict) or type(detail.get("AttemptId")) is not int or detail["AttemptId"] != attempt_id
                or type(detail.get("QuizId")) is not int or detail["QuizId"] != quiz_id
                or (actor_id is not None and (type(detail.get("UserId")) is not int or detail["UserId"] != actor_id))):
            raise PreviewSubmitUnknownError()
        completed = detail.get("Completed")
        if not isinstance(completed, str) or len(completed) > 64:
            raise PreviewSubmitUnknownError()
        timestamp = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise PreviewSubmitUnknownError()
        score = detail.get("Score")
        if type(score) not in {int, float} or not abs(score) < 1e12 or not math.isfinite(score):
            score = None
        return {"mode": "preview", "course_id": course_id, "quiz_id": quiz_id,
                "attempt_id": attempt_id, "submitted": True, "receipt_verified": True,
                "completed_at": timestamp.isoformat(), "score": score}
    except Exception:
        raise PreviewSubmitUnknownError() from None


def _rpc_result(response: Any, quiz_id: int, attempt_id: int) -> None:
    if response.status_code != 200:
        raise PreviewSubmitUnknownError()
    data = bytearray()
    for chunk in response.iter_content(chunk_size=8192):
        if not isinstance(chunk, bytes) or len(data) + len(chunk) > 65536:
            raise PreviewSubmitUnknownError()
        data.extend(chunk)
    raw = bytes(data).decode("utf-8").removeprefix("while(true){}")
    reply = json.loads(raw)
    if (not isinstance(reply, dict) or type(reply.get("ResponseType")) is not int
            or reply["ResponseType"] != 0 or reply.get("IsResultMin") is not False
            or reply.get("RedirectUrl") != "" or not isinstance(reply.get("Result"), str)):
        raise PreviewSubmitUnknownError()
    expected = f"parent.QuizDone({quiz_id},{attempt_id},'1','0','0','gotoSv','')"
    actual = re.sub(r"\s+", "", reply["Result"]).removesuffix(";")
    if actual != expected:
        raise PreviewSubmitUnknownError()


def submit_preview(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int,
    page: int, retain: bool = False, actor_id: int | None = None,
) -> dict[str, Any]:
    if type(retain) is not bool:
        raise ValueError("Invalid preview submission settings.")
    current = read_current_preview(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
    if not current.ready_to_leave() or current.has_next_control:
        raise PreviewPageError()
    home, _ = client.get_raw("/d2l/home", max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
    protection = form_protection_from_homepage(home)
    first = current.questions[0]
    fields = current.answer_fields(first["question_id"], first["selected_choice_ids"][0], protection)
    fields["d2l_actionparam"] = f"5,{page}"
    response = None
    try:
        save_url = client.canonical_url("/d2l/lms/quizzing/user/attempt/quiz_attempt_save_auto.d2l?" + urlencode({
            "cfql": 0, "fromQB": 0, "d2l_body_type": 3, "ou": course_id,
        }))
        response = client._request("POST", save_url,
                                   files=[(key, (None, value)) for key, value in fields.items()],
                                   headers={"Referer": client.canonical_url(page_path(course_id, quiz_id, attempt_id, page))})
        if response.status_code != 200:
            raise PreviewSubmitUnknownError()
        _close_response(response)
        response = None
        confirmation_path = "/d2l/lms/quizzing/user/attempt/quiz_confirm_submit_auto.d2l?" + urlencode({
            "qi": quiz_id, "ai": attempt_id, "isprv": 1, "btlp": page, "ou": course_id, "d2l_body_type": 3,
        })
        confirmation, _ = client.get_raw(confirmation_path, max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
        form, confirmation_fields = hidden_form(confirmation)
        if (not form.select('input[name="attemptCanBeGraded"][type="checkbox"]')
                or not any(b.get_text(" ", strip=True) == "Submit Quiz" and not b.has_attr("disabled") for b in form.find_all("button"))
                or confirmation_fields.get("d2l_referrer") != protection.csrf_token
                or confirmation_fields.get("HDN_isUsingRldb") != "0"):
            raise PreviewSubmitUnknownError()
        # Match the observed legacy JavaScript Boolean(string) conversion.
        # This is distinct from HDN_isUsingRldb, which must be zero above.
        integration_flag = bool(confirmation_fields.get("HDN_isRldbUse", ""))
        context = {"qi": quiz_id, "ai": attempt_id, "isprv": 1, "dnb": 0, "drc": "", "pg": 1,
                   "cfql": 0, "rldbsv": 1, "fromQB": 0, "cft": "", "d2l_body_type": 1, "ou": course_id}
        parent_path = "/d2l/lms/quizzing/user/attempt/quiz_attempt_iframe_auto.d2l"
        rpc_url = client.canonical_url(parent_path + "file?" + urlencode({**context, "d2l_rh": "rpc", "d2l_rt": "call"}))
        params = {"param1": str(quiz_id), "param2": str(attempt_id), "param3": True,
                  "param4": retain, "param5": integration_flag, "param6": False, "param7": ""}
        response = client._request("POST", rpc_url, stream=True,
                                   data={"d2l_rf": "ProcessQuizSubmission", "params": json.dumps(params),
                                         "d2l_referrer": protection.csrf_token, "d2l_hitcode": protection.next_hit_code(),
                                         "d2l_action": "rpc"},
                                   headers={"Referer": client.canonical_url(parent_path + "?" + urlencode(context))})
        _rpc_result(response, quiz_id, attempt_id)
        _close_response(response)
        response = None
        result = verify_receipt(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, actor_id=actor_id)
        result["retained_for_grading"] = retain
        return result
    except Exception:
        raise PreviewSubmitUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)
