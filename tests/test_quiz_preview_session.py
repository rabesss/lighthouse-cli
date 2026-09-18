"""Preview cursors must survive uncertainty without replaying remote writes."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.cli import cli
from lighthouse_cli.quiz_preview_session import PreviewWorkflow, PreviewWorkflowError
from lighthouse_cli.quiz_preview_transport import PreviewAdvanceUnknownError, PreviewSaveUnknownError
from lighthouse_cli.quiz_attempt_page import parse_preview_page
from tests.test_quiz_attempt_page import html, question


def page(number: int = 1, saved: bool = True):
    return parse_preview_page(html(question(number, number, saved="True" if saved else "False"), page=number),
                              course_id=10, quiz_id=20, attempt_id=30, page=number)


@pytest.fixture
def remote():
    client = Mock()
    client.base_url = "https://hetrynow.brightspace.com"
    state = {"actor": 7, "completed": None}
    def read(path, **kwargs):
        if path.endswith("users/whoami"):
            return {"Identifier": state["actor"]}
        return {"AttemptId": 30, "QuizId": 20, "UserId": 7, "Completed": state["completed"]}
    client.get_json.side_effect = read
    client.get_quiz_detail.return_value = {"PagingTypeId": 1, "PreventMovingBackwards": True,
                                         "IsSingleSession": False, "SubmissionTimeLimit": {"IsEnforced": False}}
    with patch("lighthouse_cli.quiz_preview_session.LighthouseClient", return_value=client):
        yield client, state


def start_local(workflow):
    with patch("lighthouse_cli.quiz_preview_session.start_preview", return_value=page()) as start:
        result = workflow.run("start")
    assert result["attempt_id"] == 30
    start.assert_called_once()


def test_one_active_preview_per_quiz_and_sealed_cursor(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    start_local(workflow)
    assert workflow.status()["status"] == "active"
    raw = workflow.path.read_text()
    assert '"actor_id"' not in raw
    with patch("lighthouse_cli.quiz_preview_session.start_preview") as start:
        with pytest.raises(PreviewWorkflowError, match="already exists"):
            workflow.run("start")
    start.assert_not_called()


def test_changed_account_cannot_mutate_saved_attempt(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    start_local(workflow)
    remote[1]["actor"] = 8
    with patch("lighthouse_cli.quiz_preview_session.advance_current_preview") as advance:
        with pytest.raises(PreviewWorkflowError, match="different signed-in account"):
            workflow.run("next")
    advance.assert_not_called()


def test_uncertain_save_blocks_writes_and_recovers_by_readback(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    start_local(workflow)
    with patch("lighthouse_cli.quiz_preview_session.save_current_preview_answer", side_effect=PreviewSaveUnknownError()) as save:
        with pytest.raises(PreviewSaveUnknownError):
            workflow.run("answer", question_id=101, choice_id=401)
        with pytest.raises(PreviewWorkflowError, match="uncertain"):
            workflow.run("answer", question_id=101, choice_id=401)
    save.assert_called_once()
    assert workflow.status()["status"] == "uncertain"
    with patch("lighthouse_cli.quiz_preview_session.read_current_preview", return_value=page()) as read:
        assert workflow.run("page")["page"] == 1
    read.assert_called_once()
    assert workflow.status()["status"] == "active"


def test_uncertain_advance_recovers_without_replaying_navigation(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    start_local(workflow)
    with patch("lighthouse_cli.quiz_preview_session.advance_current_preview", side_effect=PreviewAdvanceUnknownError()) as advance:
        with pytest.raises(PreviewAdvanceUnknownError):
            workflow.run("next")
    advance.assert_called_once()
    with patch("lighthouse_cli.quiz_preview_session.read_current_preview", return_value=page()) as read:
        assert workflow.run("page")["page"] == 1
    read.assert_called_once()
    assert workflow.status()["status"] == "active"
    assert workflow.status()["page"] == 1


def test_commit_failure_after_advance_never_restores_old_active_cursor(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    start_local(workflow)
    original = workflow.store.write_artifact
    calls = 0
    def fail_commit(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated full filesystem")
        return original(*args, **kwargs)
    with patch.object(workflow.store, "write_artifact", side_effect=fail_commit), \
            patch("lighthouse_cli.quiz_preview_session.advance_current_preview", return_value=page(2)) as advance:
        with pytest.raises(OSError):
            workflow.run("next")
    advance.assert_called_once()
    assert workflow.status()["status"] == "uncertain"
    with patch("lighthouse_cli.quiz_preview_session.read_current_preview", side_effect=[ValueError("old page unavailable"), page(2)]) as read:
        assert workflow.run("page")["page"] == 2
    assert [call.kwargs["page"] for call in read.call_args_list] == [1, 2]
    assert workflow.status()["page"] == 2


def test_completed_remote_attempt_is_not_submitted_again(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    start_local(workflow)
    remote[1]["completed"] = "2026-09-17T15:00:00Z"
    receipt = {"submitted": True, "receipt_verified": True, "attempt_id": 30}
    with patch("lighthouse_cli.quiz_preview_session.verify_receipt", return_value=receipt), \
            patch("lighthouse_cli.quiz_preview_session.submit_preview") as submit:
        assert workflow.run("submit") == receipt
    submit.assert_not_called()
    assert workflow.status()["status"] == "submitted"


def test_lock_contention_fails_without_waiting(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    with workflow._locked():
        with pytest.raises(PreviewWorkflowError, match="Another operation"):
            workflow.status()


def test_production_site_is_not_enabled_by_the_prototype():
    with pytest.raises(PreviewWorkflowError, match="requires --site trial"):
        PreviewWorkflow("lighthouse", 10, 20)


def test_cli_dry_run_and_declined_write_do_not_open_credentials():
    with patch("lighthouse_cli.quiz_preview_commands.PreviewWorkflow") as workflow:
        result = CliRunner().invoke(cli, ["instructor", "--site", "trial", "preview", "start", "10", "20", "--dry-run", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["dry_run"] is True
        declined = CliRunner().invoke(cli, ["instructor", "--site", "trial", "preview", "start", "10", "20", "--json"])
        assert declined.exit_code == 1
        assert json.loads(declined.stdout) == {"cancelled": True}
    workflow.assert_not_called()


def test_cli_error_is_json_only_and_secret_safe():
    with patch("lighthouse_cli.quiz_preview_commands.PreviewWorkflow", side_effect=RuntimeError("cookie=SECRET_SENTINEL")):
        result = CliRunner().invoke(cli, ["instructor", "--site", "trial", "preview", "page", "10", "20", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]
    assert "SECRET_SENTINEL" not in result.stdout + result.stderr
