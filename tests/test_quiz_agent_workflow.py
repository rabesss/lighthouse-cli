"""Exercise an agent's separate CLI calls with synthetic HTTP responses."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

from click.testing import CliRunner

from lighthouse_cli.cli import cli
from tests.test_quiz_learner import (
    ALL,
    ANSWERED,
    FINISHED,
    RECEIPT,
    body,
    confirmation,
    listing,
    posted,
    questions,
    reply,
    saved_reply,
    start_client,
    summary,
)
from tests.test_quiz_learner_session import QUIZ

SESSION = "lighthouse_cli.quiz_learner_session"


def invoke(operation: str, *args: str, exit_code: int = 0):
    result = CliRunner().invoke(cli, ["student", "attempt", operation, "10", "20", *args, "--json"])
    assert result.exit_code == exit_code, result.stdout + result.stderr
    assert "SESSION_SENTINEL" not in result.stdout + result.stderr
    return json.loads(result.stdout)


def client_for_attempt():
    client = start_client(summary())
    client.get_json = Mock(return_value={"Identifier": 7})
    client.get_quiz_detail = Mock(return_value=QUIZ)
    return client


def test_agent_can_complete_and_verify_a_quiz_across_cli_calls():
    client = client_for_attempt()
    with patch(f"{SESSION}.LighthouseClient", return_value=client):
        started = invoke("start", "--yes")
        assert started["resumed"] is False
        assert {q["kind"] for q in started["questions"]} == {
            "single-choice",
            "multi-select",
            "fill-blank",
        }
        assert started["timer"] is None
        client._request.assert_called_once()

        client.get_raw.side_effect = [(body(questions()), {})]
        page = invoke("page")
        assert page["attempt_id"] == started["attempt_id"]
        assert page["has_next_control"] is False
        client._request.reset_mock()
        client._request.side_effect = [
            posted(saved_reply("101")),
            posted(saved_reply("102")),
            posted(saved_reply("103")),
        ]
        client.get_raw.side_effect = [
            (body(questions()), {}),
            (body(questions(sc=("o2",), saved=("True", "False", "False"))), {}),
            (body(questions(sc=("o2",), ms=("o11", "o13"), saved=("True", "True", "False"))), {}),
            (body(ANSWERED), {}),
        ]
        answered = invoke("answer", "--answers", json.dumps(ALL), "--yes")
        assert all(q["saved"] is True for q in answered["questions"])
        assert client._request.call_count == 3

        client._request.reset_mock()
        client.get_raw.side_effect = [(body(ANSWERED), {})]
        repeated = invoke("answer", "--answers", json.dumps(ALL), "--yes")
        assert repeated["questions"] == answered["questions"]
        client._request.assert_not_called()

        client.get_raw.side_effect = [
            (body(ANSWERED), {}),
            (confirmation(), {}),
            (listing(), {}),
            (RECEIPT, {}),
        ]
        client._request.side_effect = [posted(FINISHED), posted(reply())]
        submitted = invoke("submit", "--yes")
        assert submitted["submitted"] is True
        assert submitted["attempt_id"] == started["attempt_id"]
        assert client._request.call_count == 2

        client._request.reset_mock()
        client.get_raw.side_effect = [(listing(), {}), (RECEIPT, {})]
        assert invoke("verify") == submitted
        client._request.assert_not_called()
        assert invoke("status")["status"] == "submitted"


def test_agent_settles_a_partial_save_before_resending_remaining_answers():
    client = client_for_attempt()
    partial = body(questions(sc=("o2",), saved=("True", "False", "False")))
    answers = json.dumps({"101": "o2", "102": ["o11"]})
    first, second = posted(saved_reply("101")), posted(saved_reply("102"), status=500)
    with patch(f"{SESSION}.LighthouseClient", return_value=client):
        invoke("start", "--yes")
        client._request.reset_mock()
        client._request.side_effect = [first, second]
        client.get_raw.side_effect = [(body(questions()), {}), (partial, {})]
        assert invoke("answer", "--answers", answers, "--yes", exit_code=1)["error"]
        assert client._request.call_count == 2
        first.close.assert_called_once()
        second.close.assert_called_once()
        cursor = invoke("status")
        assert (cursor["status"], cursor["operation"]) == ("uncertain", "answer")

        client._request.reset_mock()
        client.get_raw.side_effect = [(partial, {})]
        reread = invoke("page")
        assert reread["questions"][0]["selected_choice_ids"] == ["o2"]
        assert reread["questions"][1]["selected_choice_ids"] == []
        assert invoke("status")["status"] == "active"
        client._request.assert_not_called()

        client.get_raw.side_effect = [
            (partial, {}),
            (body(questions(sc=("o2",), ms=("o11",), saved=("True", "True", "False"))), {}),
        ]
        client._request.side_effect = [posted(saved_reply("102"))]
        saved = invoke("answer", "--answers", answers, "--yes")
        assert [q["saved"] for q in saved["questions"][:2]] == [True, True]
        client._request.assert_called_once()
