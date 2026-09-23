"""Preview cursors must survive uncertainty without replaying remote writes."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.cli import cli
from lighthouse_cli.quiz_attempt_page import parse_preview_page
from lighthouse_cli.quiz_preview_session import PreviewWorkflow, PreviewWorkflowError
from lighthouse_cli.quiz_preview_transport import (
    PreviewAdvanceUnknownError,
    PreviewSaveUnknownError,
    PreviewStartUnknownError,
)
from tests.test_quiz_attempt_page import html, question


def page(number: int = 1, saved: bool = True, attempt_id: int = 30):
    body = html(question(number, number, saved="True" if saved else "False"), page=number)
    if attempt_id != 30:
        body = body.replace(b'name="ai" type="hidden" value="30"', f'name="ai" type="hidden" value="{attempt_id}"'.encode())
    return parse_preview_page(body,
                              course_id=10, quiz_id=20, attempt_id=attempt_id, page=number)


def _attempt(attempt_id: int, *, quiz_id: int = 20, actor_id: int = 7, completed=None) -> dict[str, object]:
    return {"AttemptId": attempt_id, "QuizId": quiz_id, "UserId": actor_id, "Completed": completed}


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


def test_start_readback_uncertainty_is_durable_and_blocks_restart(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    with patch("lighthouse_cli.quiz_preview_session.start_preview", side_effect=PreviewStartUnknownError()):
        with pytest.raises(PreviewStartUnknownError):
            workflow.run("start")
    assert workflow.status()["status"] == "uncertain"
    with patch("lighthouse_cli.quiz_preview_session.start_preview") as start:
        with pytest.raises(PreviewWorkflowError, match="reconcile"):
            workflow.run("start")
    start.assert_not_called()


def test_typed_start_callback_identity_is_sealed_for_page_recovery(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    with patch(
        "lighthouse_cli.quiz_preview_session.start_preview",
        side_effect=PreviewStartUnknownError(attempt_id=30, page=1),
    ):
        with pytest.raises(PreviewStartUnknownError):
            workflow.run("start")
    assert workflow.status()["status"] == "uncertain"
    assert workflow.status()["attempt_id"] == 30
    assert workflow.status()["page"] == 1
    with patch("lighthouse_cli.quiz_preview_session.read_current_preview", return_value=page()) as read:
        assert workflow.run("page")["attempt_id"] == 30
    read.assert_called_once()
    assert workflow.status()["status"] == "active"


def test_unknown_start_without_typed_identity_never_guesses_from_listing(remote):
    client, _ = remote
    client._paginate_list.return_value = [{"AttemptId": 31}]
    workflow = PreviewWorkflow("trial", 10, 20)
    with patch("lighthouse_cli.quiz_preview_session.start_preview", side_effect=PreviewStartUnknownError()):
        with pytest.raises(PreviewStartUnknownError):
            workflow.run("start")
    client._paginate_list.assert_not_called()
    assert workflow.status()["status"] == "uncertain"
    assert workflow.status()["attempt_id"] is None


def test_uncertain_start_never_allows_a_fresh_retry(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    with patch("lighthouse_cli.quiz_preview_session.start_preview", side_effect=PreviewStartUnknownError()):
        with pytest.raises(PreviewStartUnknownError):
            workflow.run("start")
    with patch("lighthouse_cli.quiz_preview_session.start_preview") as start:
        with pytest.raises(PreviewWorkflowError, match="starting again is blocked"):
            workflow.run("start")
    start.assert_not_called()


def test_explicit_reconcile_binds_only_operator_selected_attempt(remote):
    client, _ = remote
    client.get_json.side_effect = lambda path, **kwargs: (
        {"Identifier": 7} if path.endswith("users/whoami")
        else {"AttemptId": 31, "QuizId": 20, "UserId": 7, "Completed": None}
    )
    workflow = PreviewWorkflow("trial", 10, 20)
    state = {
        "version": 1, "origin": workflow.connection.origin, "mode": "preview", "actor_id": 7,
        "course_id": 10, "quiz_id": 20, "status": "uncertain", "operation": "start",
        "attempt_id": None, "page": None,
    }
    with patch.object(workflow, "_load", return_value=state), patch.object(workflow, "_save"):
        with patch("lighthouse_cli.quiz_preview_session.read_current_preview", return_value=page(attempt_id=31)):
            result = workflow.reconcile(31)
    assert result["attempt_id"] == 31


@pytest.mark.parametrize(
    "record, message",
    [
        (_attempt(31, actor_id=8), "identity"),
        (_attempt(31, completed="2026-09-18T12:00:00Z"), "completed"),
        (_attempt(31, quiz_id=99), "identity"),
    ],
)
def test_explicit_reconcile_verifies_actor_quiz_and_completion(remote, record, message):
    client, _ = remote
    client.get_json.side_effect = lambda path, **kwargs: (
        {"Identifier": 7} if path.endswith("users/whoami") else record
    )
    workflow = PreviewWorkflow("trial", 10, 20)
    with patch.object(workflow, "_load", return_value={
        "version": 1, "origin": workflow.connection.origin, "mode": "preview", "actor_id": 7,
        "course_id": 10, "quiz_id": 20, "status": "uncertain", "operation": "start",
        "attempt_id": None, "page": None,
    }), patch.object(workflow, "_save"), pytest.raises(PreviewWorkflowError, match=message):
        workflow.reconcile(31)


def test_reconcile_refuses_non_uncertain_status(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    start_local(workflow)
    with pytest.raises(PreviewWorkflowError, match="uncertain"):
        workflow.reconcile()


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


def test_uncertain_advance_stays_blocked_without_authoritative_cursor(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    start_local(workflow)
    with patch("lighthouse_cli.quiz_preview_session.advance_current_preview", side_effect=PreviewAdvanceUnknownError()) as advance:
        with pytest.raises(PreviewAdvanceUnknownError):
            workflow.run("next")
    advance.assert_called_once()
    with pytest.raises(PreviewWorkflowError, match="Navigation outcome is uncertain"):
        workflow.run("page")
    assert workflow.status()["status"] == "uncertain"
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
    with pytest.raises(PreviewWorkflowError, match="Navigation outcome is uncertain"):
        workflow.run("page")
    assert workflow.status()["page"] == 1


def test_completed_remote_attempt_is_not_submitted_again(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    start_local(workflow)
    remote[1]["completed"] = "2026-09-17T15:00:00Z"
    receipt = {"submitted": True, "receipt_verified": True, "attempt_id": 30}
    with patch("lighthouse_cli.quiz_preview_session.verify_receipt", return_value=receipt), \
            patch("lighthouse_cli.quiz_preview_session.submit_preview") as submit:
        assert workflow.run("submit") == {**receipt, "retained_for_grading": False}
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


def test_cli_reconcile_is_read_only_and_keeps_json_on_stdout():
    with patch("lighthouse_cli.quiz_preview_commands.PreviewWorkflow") as workflow:
        workflow.return_value.reconcile.return_value = {
            "mode": "preview", "status": "absent", "fresh_start_allowed": True,
        }
        result = CliRunner().invoke(
            cli,
            ["instructor", "--site", "trial", "preview", "reconcile", "10", "20", "--json"],
        )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["fresh_start_allowed"] is True
    assert result.stderr == ""
    workflow.return_value.reconcile.assert_called_once_with(attempt_id=None)
