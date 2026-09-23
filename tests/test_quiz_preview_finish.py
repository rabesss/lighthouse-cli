"""Final submission requires a strict RPC acknowledgement and a real receipt."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from lighthouse_cli.api import LighthouseClient, SessionExpiredError
from lighthouse_cli.quiz_attempt_page import PreviewRefusedError
from lighthouse_cli.quiz_preview_finish import (
    PreviewSubmitUnknownError,
    submit_preview,
    verify_receipt,
)
from tests.test_quiz_attempt_page import bootstrap, html, question


def client_for_submit(*, rpc_result: str | None = None, secure_browser: str = "0"):
    client = LighthouseClient(site="trial")
    confirmation = f'''<form><input type="hidden" name="d2l_referrer" value="SESSION_SENTINEL">
    <input type="hidden" name="HDN_isRldbUse" value="False">
    <input type="hidden" name="HDN_isUsingRldb" value="{secure_browser}">
    <input type="checkbox" name="attemptCanBeGraded"><button>Submit Quiz</button></form>'''.encode()
    client.get_raw = Mock(side_effect=[(html(question(1)), {}), (bootstrap(), {}), (confirmation, {}),
                                      (b'<h2>Your work has been saved and submitted</h2>', {})])
    client.get_json = Mock(return_value={"AttemptId": 30, "QuizId": 20, "UserId": 7, "Completed": "2026-09-17T15:00:00Z", "Score": 1})
    reply = {"ResponseType": 0, "IsResultMin": False, "Result": rpc_result or "parent.QuizDone(20,30,'1','0', '0', 'gotoSv', '')", "RedirectUrl": "", "MessageArea": {}}
    prep = Mock(status_code=200)
    rpc = Mock(status_code=200)
    rpc.iter_content.return_value = [("while(true){}"+json.dumps(reply)).encode()]
    client._request = Mock(side_effect=[prep, rpc])
    return client, prep, rpc


def test_submit_is_preview_only_and_verifies_independent_receipt():
    client, prep, rpc = client_for_submit()
    result = submit_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1, retain=True, actor_id=7)
    assert result["submitted"] and result["receipt_verified"] and result["retained_for_grading"]
    rpc_call = client._request.call_args_list[1]
    # Brightspace rejects the RPC unless params is compact JSON (browser form).
    assert rpc_call.kwargs["data"]["params"] == (
        '{"param1":"20","param2":"30","param3":true,"param4":true,"param5":true,"param6":false,"param7":""}'
    )
    assert client._request.call_count == 2  # one preparatory save, one final RPC
    prep.close.assert_called_once()
    rpc.close.assert_called_once()


def test_submit_rpc_context_names_the_current_page():
    client, _, _ = client_for_submit()
    responses = list(client.get_raw.side_effect)
    client.get_raw.side_effect = [(html(question(2, page=2), page=2), {}), *responses[1:]]
    submit_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=2)
    url = client._request.call_args_list[1].args[1]
    assert "quiz_attempt_iframe_auto.d2lfile?" in url
    assert "&pg=2&" in url


@pytest.mark.parametrize("body, message", [
    (html(question(1, saved="False")), "Answer and save every question"),
    (html(question(1), extra="<button>Next Page</button>"), "Move to the last page"),
])
def test_submit_refuses_before_any_write(body, message):
    client, _, _ = client_for_submit()
    client.get_raw.side_effect = [(body, {})]
    with pytest.raises(PreviewRefusedError, match=message):
        submit_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)
    client._request.assert_not_called()


def test_secure_browser_attempt_is_not_submitted_by_http_client():
    client, _, _ = client_for_submit(secure_browser="1")
    with pytest.raises(PreviewSubmitUnknownError):
        submit_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)
    assert client._request.call_count == 1  # never reached final RPC


@pytest.mark.parametrize("result", ["parent.QuizDone(20,999,'1','0','0','gotoSv','')", "parent.QuizDone(20,30,'0','0','0','gotoSv','')", "parent.QuizDone(20,30,'1','0','0','gotoSv','');evil()"])
def test_wrong_identity_mode_or_extra_script_is_not_executed_or_accepted(result):
    client, _, rpc = client_for_submit(rpc_result=result)
    with pytest.raises(PreviewSubmitUnknownError):
        submit_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)
    assert client._request.call_count == 2
    rpc.close.assert_called_once()


def test_success_heading_without_completed_attempt_record_is_not_a_receipt():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(return_value=(b'<h2>Your work has been saved and submitted</h2>', {}))
    client.get_json = Mock(return_value={"AttemptId": 30, "QuizId": 20, "UserId": 7, "Completed": None})
    with pytest.raises(PreviewSubmitUnknownError):
        verify_receipt(client, course_id=10, quiz_id=20, attempt_id=30, actor_id=7)


def test_completed_attempt_record_verifies_localized_receipt_heading():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(return_value=(b"<h2>Arbeit gespeichert</h2>", {}))
    client.get_json = Mock(return_value={"AttemptId": 30, "QuizId": 20, "UserId": 7, "Completed": "2026-09-17T15:00:00Z", "Score": 1})
    result = verify_receipt(client, course_id=10, quiz_id=20, attempt_id=30, actor_id=7)
    assert result["receipt_verified"] is True
    assert result["receipt_heading_verified"] is False


def test_receipt_session_expiry_is_not_masked_as_unknown_submission():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(SessionExpiredError):
        verify_receipt(client, course_id=10, quiz_id=20, attempt_id=30, actor_id=7)


def test_submit_receipt_auth_expiry_is_unknown_after_write_dispatch():
    client, _, _ = client_for_submit()
    calls = list(client.get_raw.side_effect)
    client.get_raw = Mock(side_effect=[calls[0], calls[1], calls[2], SessionExpiredError("session expired")])
    with pytest.raises(PreviewSubmitUnknownError):
        submit_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)


def test_submit_preparatory_post_auth_expiry_is_unknown_after_dispatch():
    client, _, _ = client_for_submit()
    client._request = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(PreviewSubmitUnknownError):
        submit_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)
    client._request.assert_called_once()


def test_submit_rpc_auth_expiry_is_unknown_after_dispatch():
    client, prep, _ = client_for_submit()
    client._request = Mock(side_effect=[prep, SessionExpiredError("session expired")])
    with pytest.raises(PreviewSubmitUnknownError):
        submit_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)
    assert client._request.call_count == 2
