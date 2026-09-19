"""Characterization of the observed preview DOM, using synthetic tokens only."""

from __future__ import annotations

import json
from html import escape
from unittest.mock import Mock

import pytest

from lighthouse_cli.api import LighthouseClient, SessionExpiredError
from lighthouse_cli.quiz_attempt_page import MAX_PAGE_BYTES, PreviewPageError, parse_preview_page
from lighthouse_cli.quiz_preview_transport import (
    PreviewAdvanceUnknownError,
    PreviewSaveUnknownError,
    PreviewStartUnknownError,
    advance_current_preview,
    save_current_preview_answer,
    start_preview,
)
from lighthouse_cli.request_protection import FormProtection


def question(number: int, page: int = 1, *, saved: str = "True", selected: bool = True) -> str:
    values = {
        "object-id": str(100 + number),
        "autosave-question-num": str(number),
        "autosave-page": str(page),
        "autosave-tid": str(200 + number),
        "autosave-tvid": "300",
        "autosave-is-saved": saved,
    }
    metadata = "".join(
        f'<div class="d2l-quiz-question-{key}"><input type="hidden" value="{value}"></div>'
        for key, value in values.items()
    )
    checked = "checked" if selected else ""
    return f"""<div class="d2l-quiz-question-autosave-container">{metadata}
    <div id="d2l_read_element_{number}">Question {number}: choose true.
    <fieldset><legend>Options</legend><table>
    <tr><td><input type="radio" name="tAtom{200 + number}_300" id="q{number}a" value="401" {checked}></td><td><label for="q{number}a">True</label></td></tr>
    <tr><td><input type="radio" name="tAtom{200 + number}_300" id="q{number}b" value="402"></td><td><label for="q{number}b">False</label></td></tr>
    </table></fieldset></div></div>"""


def html(questions: str, *, page: int = 1, extra: str = "") -> bytes:
    identity = {
        "ou": "10",
        "qi": "20",
        "ai": "30",
        "pg": str(page),
        "isprv": "1",
        "d2l_referrer": "SESSION_SENTINEL",
    }
    identity.update(
        {
            "d2l_controlMap": json.dumps(
                [{"hdn_resp_101": ["z_r1"], "hdn_resp_102": ["z_r2"]}, {}]
            ),
            "z_r1": "0",
            "z_r2": "0",
        }
    )
    fields = "".join(
        f'<input name="{key}" type="hidden" value="{escape(value, quote=True)}">'
        for key, value in identity.items()
    )
    return f"<form>{fields}{questions}{extra}</form>".encode()


def parse(body: bytes, page: int = 1):
    return parse_preview_page(body, course_id=10, quiz_id=20, attempt_id=30, page=page)


def test_all_at_once_exposes_only_current_visible_questions():
    page = parse(html(question(1) + question(2)))
    data = page.public_data()
    assert len(data["questions"]) == 2
    assert data["questions"][0]["text"] == "Question 1: choose true."
    assert data["questions"][0]["choices"] == [
        {"choice_id": 401, "text": "True"},
        {"choice_id": 402, "text": "False"},
    ]
    assert page.confirms_answer(101, 401)
    assert not page.confirms_answer(101, 402)
    assert not page.has_next_control
    assert "SESSION_SENTINEL" not in repr(page)
    assert "SESSION_SENTINEL" not in json.dumps(data)


def test_one_way_page_has_next_without_previous():
    page = parse(html(question(1), extra="<button>Next Page</button>"))
    assert len(page.questions) == 1
    assert page.has_next_control
    assert not page.has_previous_control
    final_page = parse(
        html(question(2, 2), page=2, extra="<button disabled>Next Page</button>"), page=2
    )
    assert not final_page.has_next_control
    assert not final_page.has_previous_control


@pytest.mark.parametrize("saved", ["False", "unknown", ""])
def test_selected_choice_without_saved_confirmation_is_not_success(saved):
    page = parse(html(question(1, saved=saved)))
    assert not page.confirms_answer(101, 401)


def test_saved_flag_without_selected_choice_is_not_success():
    assert not parse(html(question(1, selected=False))).confirms_answer(101, 401)


@pytest.mark.parametrize("field", ["ou", "qi", "ai", "pg", "isprv"])
def test_wrong_identity_or_real_learner_page_is_rejected(field):
    body = html(question(1))
    before = {"ou": "10", "qi": "20", "ai": "30", "pg": "1", "isprv": "1"}[field]
    body = body.replace(
        f'name="{field}" type="hidden" value="{before}"'.encode(),
        f'name="{field}" type="hidden" value="999"'.encode(),
    )
    with pytest.raises(PreviewPageError):
        parse(body)


@pytest.mark.parametrize(
    "body",
    [
        b"<html>Login</html>",
        html(question(1) + question(1)),
        html(question(1), extra='<input type="hidden" name="ai" value="30">'),
        b"x" * (MAX_PAGE_BYTES + 1),
    ],
)
def test_incomplete_duplicate_or_oversized_response_is_rejected(body):
    with pytest.raises(PreviewPageError) as exc:
        parse(body)
    assert "SESSION_SENTINEL" not in str(exc.value)


def test_media_is_explicitly_unsupported_instead_of_silently_lost():
    q = question(1).replace("Question 1: choose true.", 'Question 1: <img alt="diagram">')
    page = parse(html(q))
    assert page.questions[0]["kind"] == "unsupported"
    assert not page.confirms_answer(101, 401)


def test_server_html_block_attribute_supplies_question_text():
    q = question(1).replace(
        "Question 1: choose true.",
        '<d2l-html-block html="&lt;p&gt;Two plus two equals four.&lt;/p&gt;"></d2l-html-block>',
    )
    page = parse(html(q))
    assert page.questions[0]["text"] == "Two plus two equals four."
    assert page.questions[0]["supported"]


def test_media_inside_html_attribute_is_not_silently_ignored():
    q = question(1).replace(
        "Question 1: choose true.",
        '<d2l-html-block html="&lt;p&gt;Identify:&lt;img src=&quot;diagram.png&quot;&gt;&lt;/p&gt;"></d2l-html-block>',
    )
    assert not parse(html(q)).questions[0]["supported"]


def test_unhydrated_empty_prompt_is_not_actionable():
    q = question(1).replace("Question 1: choose true.", "<d2l-html-block></d2l-html-block>")
    assert not parse(html(q)).questions[0]["supported"]


def test_answer_form_resolves_response_flag_and_preserves_sibling_answer():
    page = parse(html(question(1) + question(2, selected=False)))
    protection = FormProtection("SESSION_SENTINEL", "1234567890")
    first = page.answer_fields(102, 402, protection)
    second = page.answer_fields(102, 402, protection)
    assert first["tAtom201_300"] == "401"
    assert first["tAtom202_300"] == "402"
    assert first["z_r2"] == "1"
    assert first["d2l_action"] == "Update"
    assert first["d2l_actionparam"] == "3,1,202,300,2"
    assert first["d2l_hitCode"] != second["d2l_hitCode"]
    assert "SESSION_SENTINEL" not in repr(protection)


def test_answer_form_rejects_stale_session_or_unknown_choice():
    page = parse(html(question(1)))
    with pytest.raises(PreviewPageError):
        page.answer_fields(101, 999, FormProtection("SESSION_SENTINEL", "123"))
    with pytest.raises(PreviewPageError):
        page.answer_fields(101, 401, FormProtection("DIFFERENT_SESSION", "123"))


def bootstrap() -> bytes:
    record = json.dumps(
        {
            "_type": "func",
            "N": "D2L.LP.Web.Authentication.Xsrf.Init",
            "P": ["d2l_referrer", "SESSION_SENTINEL", 1234567890],
        }
    )
    return ('<script>const graph={"1":' + json.dumps(record) + "};</script>").encode()


def test_save_transport_requires_persisted_readback_and_posts_once():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(
        side_effect=[
            (bootstrap(), {}),
            (html(question(1, selected=False, saved="False")), {}),
            (html(question(1)), {}),
        ]
    )
    response = Mock(status_code=200)
    client._request = Mock(return_value=response)
    result = save_current_preview_answer(
        client, course_id=10, quiz_id=20, attempt_id=30, page=1, question_id=101, choice_id=401
    )
    assert result.confirms_answer(101, 401)
    client._request.assert_called_once()
    assert client._request.call_args.args[0] == "POST"
    assert "isprv=1" in client._request.call_args.kwargs["headers"]["Referer"]
    fields = dict(client._request.call_args.kwargs["files"])
    assert fields["z_r1"] == (None, "1")
    response.close.assert_called_once()


def test_http_200_without_persisted_answer_is_unknown_not_success():
    client = LighthouseClient(site="trial")
    unchanged = html(question(1, selected=False, saved="False"))
    client.get_raw = Mock(side_effect=[(bootstrap(), {}), (unchanged, {}), (unchanged, {})])
    client._request = Mock(return_value=Mock(status_code=200))
    with pytest.raises(PreviewSaveUnknownError):
        save_current_preview_answer(
            client, course_id=10, quiz_id=20, attempt_id=30, page=1, question_id=101, choice_id=401
        )
    client._request.assert_called_once()


def test_save_readback_auth_expiry_is_unknown_after_post_dispatch():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(
        side_effect=[
            (bootstrap(), {}),
            (html(question(1)), {}),
            SessionExpiredError("session expired"),
        ]
    )
    client._request = Mock(return_value=Mock(status_code=200))
    with pytest.raises(PreviewSaveUnknownError):
        save_current_preview_answer(
            client, course_id=10, quiz_id=20, attempt_id=30, page=1, question_id=101, choice_id=401
        )
    client._request.assert_called_once()


def test_save_post_auth_expiry_is_unknown_after_dispatch():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(side_effect=[(bootstrap(), {}), (html(question(1)), {})])
    client._request = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(PreviewSaveUnknownError):
        save_current_preview_answer(
            client, course_id=10, quiz_id=20, attempt_id=30, page=1, question_id=101, choice_id=401
        )
    client._request.assert_called_once()


def test_advance_readback_auth_expiry_is_unknown_after_post_dispatch():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(
        side_effect=[
            (bootstrap(), {}),
            (html(question(1), extra="<button>Next Page</button>"), {}),
            SessionExpiredError("session expired"),
        ]
    )
    client._request = Mock(return_value=Mock(status_code=200))
    with pytest.raises(PreviewAdvanceUnknownError):
        advance_current_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)
    client._request.assert_called_once()


def test_advance_post_auth_expiry_is_unknown_after_dispatch():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(
        side_effect=[(bootstrap(), {}), (html(question(1), extra="<button>Next Page</button>"), {})]
    )
    client._request = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(PreviewAdvanceUnknownError):
        advance_current_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)
    client._request.assert_called_once()


def test_uncertain_post_is_not_replayed_or_echoed():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(side_effect=[(bootstrap(), {}), (html(question(1)), {})])
    client._request = Mock(side_effect=RuntimeError("cookie=SESSION_SENTINEL"))
    with pytest.raises(PreviewSaveUnknownError) as exc:
        save_current_preview_answer(
            client, course_id=10, quiz_id=20, attempt_id=30, page=1, question_id=101, choice_id=401
        )
    assert "SESSION_SENTINEL" not in str(exc.value)
    client._request.assert_called_once()


def test_start_follows_typed_callback_without_executing_scripts():
    client = LighthouseClient(site="trial")
    process = "/d2l/lms/quizzing/user/attempt/quiz_start_process_auto.d2l?ou=10&qi=20&isprv=1&fromQB=0&inProgress=0"
    root = process.replace("quiz_start_process_auto", "quiz_start_frame_auto")
    inner = process.replace("quiz_start_process_auto", "quiz_start_iframe_2_auto")
    client._request = Mock(return_value=Mock(status_code=302, headers={"Location": root}))
    client.get_raw = Mock(
        side_effect=[
            (html("", extra="<button>Start Quiz!</button>") + bootstrap(), {}),
            (f'<iframe src="{inner}"></iframe>'.encode(), {}),
            (f'<iframe name="hiddenFrame" src="{process}"></iframe>'.encode(), {}),
            (b"<script>\nparent.GoToAttemptQuizAuto( 30,1,0 );\n</script>", {}),
            (html(question(1)), {}),
        ]
    )
    page = start_preview(client, course_id=10, quiz_id=20)
    assert page.attempt_id == 30
    assert page.page == 1
    assert client.get_raw.call_count == 5
    assert client.get_raw.call_args_list[3].kwargs["_replay_safe"] is False
    client._request.assert_called_once()


def test_start_rejects_missing_button_without_creating_attempt():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(return_value=(b"<h1>Quiz Summary</h1>", {}))
    with pytest.raises(Exception, match="not available"):
        start_preview(client, course_id=10, quiz_id=20)
    client.get_raw.assert_called_once()


def test_start_readback_auth_expiry_is_unknown_after_state_creation():
    client = LighthouseClient(site="trial")
    process = "/d2l/lms/quizzing/user/attempt/quiz_start_process_auto.d2l?ou=10&qi=20&isprv=1&fromQB=0&inProgress=0"
    root = process.replace("quiz_start_process_auto", "quiz_start_frame_auto")
    inner = process.replace("quiz_start_process_auto", "quiz_start_iframe_2_auto")
    client._request = Mock(return_value=Mock(status_code=302, headers={"Location": root}))
    client.get_raw = Mock(
        side_effect=[
            (html("", extra="<button>Start Quiz!</button>") + bootstrap(), {}),
            (f'<iframe src="{inner}"></iframe>'.encode(), {}),
            (f'<iframe name="hiddenFrame" src="{process}"></iframe>'.encode(), {}),
            (b"<script>parent.GoToAttemptQuizAuto( 30,1,0 );</script>", {}),
            SessionExpiredError("session expired"),
        ]
    )
    with pytest.raises(PreviewStartUnknownError):
        start_preview(client, course_id=10, quiz_id=20)
    assert client._request.call_count == 1


def test_start_process_auth_expiry_is_unknown_after_dispatch():
    client = LighthouseClient(site="trial")
    process = "/d2l/lms/quizzing/user/attempt/quiz_start_process_auto.d2l?ou=10&qi=20&isprv=1&fromQB=0&inProgress=0"
    root = process.replace("quiz_start_process_auto", "quiz_start_frame_auto")
    inner = process.replace("quiz_start_process_auto", "quiz_start_iframe_2_auto")
    client._request = Mock(return_value=Mock(status_code=302, headers={"Location": root}))
    client.get_raw = Mock(
        side_effect=[
            (html("", extra="<button>Start Quiz!</button>") + bootstrap(), {}),
            (f'<iframe src="{inner}"></iframe>'.encode(), {}),
            (f'<iframe name="hiddenFrame" src="{process}"></iframe>'.encode(), {}),
            SessionExpiredError("session expired"),
        ]
    )
    with pytest.raises(PreviewStartUnknownError):
        start_preview(client, course_id=10, quiz_id=20)
    assert client._request.call_count == 1


def test_start_summary_post_auth_expiry_is_unknown_after_dispatch():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(
        return_value=(html("", extra="<button>Start Quiz!</button>") + bootstrap(), {})
    )
    client._request = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(PreviewStartUnknownError):
        start_preview(client, course_id=10, quiz_id=20)
    client.get_raw.assert_called_once()
    client._request.assert_called_once()


def test_ambiguous_start_does_not_retry_or_trust_script_strings():
    client = LighthouseClient(site="trial")
    process = "/d2l/lms/quizzing/user/attempt/quiz_start_process_auto.d2l?ou=10&qi=20&isprv=1&fromQB=0&inProgress=0"
    root = process.replace("quiz_start_process_auto", "quiz_start_frame_auto")
    inner = process.replace("quiz_start_process_auto", "quiz_start_iframe_2_auto")
    client._request = Mock(return_value=Mock(status_code=302, headers={"Location": root}))
    client.get_raw = Mock(
        side_effect=[
            (html("", extra="<button>Start Quiz!</button>") + bootstrap(), {}),
            (f'<iframe src="{inner}"></iframe>'.encode(), {}),
            (f'<iframe name="hiddenFrame" src="{process}"></iframe>'.encode(), {}),
            (b'<script>const text="parent.GoToAttemptQuizAuto(30,1,0)";</script>', {}),
        ]
    )
    with pytest.raises(PreviewStartUnknownError):
        start_preview(client, course_id=10, quiz_id=20)
    assert client.get_raw.call_count == 4
    client._request.assert_called_once()
