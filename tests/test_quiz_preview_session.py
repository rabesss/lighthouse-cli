"""Preview cursors must survive uncertainty without replaying remote writes."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import NetworkError
from lighthouse_cli.cli import cli
from lighthouse_cli.quiz_attempt_page import (
    REFUSE_NOT_ON_PAGE,
    PreviewPageError,
    PreviewRefusedError,
    parse_preview_page,
)
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


def _attempt(attempt_id: int, *, quiz_id: int = 20, actor_id: int = 7, completed=None,
             started: str = "2999-01-01T00:00:00Z") -> dict[str, object]:
    return {"AttemptId": attempt_id, "QuizId": quiz_id, "UserId": actor_id, "Completed": completed,
            "Started": started}


@pytest.fixture
def remote():
    client = Mock()
    client.base_url = "https://hetrynow.brightspace.com"
    state = {"actor": 7, "completed": None, "attempts": [], "records": {}}
    def read(path, **kwargs):
        if path.endswith("users/whoami"):
            return {"Identifier": state["actor"]}
        attempt_id = int(path.rstrip("/").rsplit("/", 1)[1])
        return state["records"].get(
            attempt_id, {"AttemptId": attempt_id, "QuizId": 20, "UserId": 7, "Completed": state["completed"]},
        )
    client.get_json.side_effect = read
    client._paginate_list.side_effect = lambda path: list(state["attempts"])
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


def unknown_start(workflow, *, identity=None):
    """Run a start whose outcome is unknown; optionally reveal identity first."""
    def fake_start(client, *, on_identity=None, **kwargs):
        if identity is not None:
            on_identity(*identity)
        raise PreviewStartUnknownError()
    with patch("lighthouse_cli.quiz_preview_session.start_preview", side_effect=fake_start):
        with pytest.raises(PreviewStartUnknownError):
            workflow.run("start")


def saved(workflow):
    return workflow.store.read_artifact(workflow.path)[1]


def test_start_seals_account_bound_baseline_before_dispatch(remote):
    _, state = remote
    state["attempts"] = [_attempt(5), _attempt(6, completed="2026-09-01T00:00:00Z"), _attempt(9, actor_id=8)]
    workflow = PreviewWorkflow("trial", 10, 20)
    seen = {}
    def fake_start(client, **kwargs):
        seen.update(saved(workflow))
        return page()
    with patch("lighthouse_cli.quiz_preview_session.start_preview", side_effect=fake_start):
        workflow.run("start")
    assert seen["status"] == "uncertain" and seen["operation"] == "start"
    assert seen["baseline_attempt_ids"] == [5, 6]  # own attempts only
    assert seen["start_intent_at"]


def test_start_is_refused_before_dispatch_when_listing_fails(remote):
    client, _ = remote
    client._paginate_list.side_effect = NetworkError("listing failed")
    workflow = PreviewWorkflow("trial", 10, 20)
    with patch("lighthouse_cli.quiz_preview_session.start_preview") as start:
        with pytest.raises(PreviewWorkflowError, match="nothing was started"):
            workflow.run("start")
    start.assert_not_called()
    assert workflow.status()["status"] == "absent"


def test_identity_is_sealed_before_page_readback(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow, identity=(31, 1))
    status = workflow.status()
    assert (status["status"], status["attempt_id"], status["page"]) == ("uncertain", 31, 1)
    assert status["unresolved_start"] is True


def test_identity_survives_an_interrupted_start_after_it_is_sealed(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    def interrupted(client, *, on_identity=None, **kwargs):
        on_identity(31, 1)
        raise KeyboardInterrupt  # e.g. the process is stopped during readback
    with patch("lighthouse_cli.quiz_preview_session.start_preview", side_effect=interrupted):
        with pytest.raises(KeyboardInterrupt):
            workflow.run("start")
    assert (workflow.status()["status"], workflow.status()["attempt_id"]) == ("uncertain", 31)


def test_exception_carried_identity_is_sealed_for_page_recovery(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    with patch("lighthouse_cli.quiz_preview_session.start_preview",
               side_effect=PreviewStartUnknownError(attempt_id=30, page=1)):
        with pytest.raises(PreviewStartUnknownError):
            workflow.run("start")
    assert (workflow.status()["attempt_id"], workflow.status()["page"]) == (30, 1)
    with patch("lighthouse_cli.quiz_preview_session.read_current_preview", return_value=page()) as read:
        assert workflow.run("page")["attempt_id"] == 30
    read.assert_called_once()
    assert workflow.status()["status"] == "active"


def test_unknown_start_without_identity_never_binds_from_listing(remote):
    client, state = remote
    workflow = PreviewWorkflow("trial", 10, 20)
    state["attempts"] = []
    unknown_start(workflow)
    state["attempts"] = [_attempt(31)]
    assert client._paginate_list.call_count == 1  # the pre-start baseline only
    assert workflow.status()["attempt_id"] is None
    assert workflow.status()["status"] == "uncertain"


@pytest.mark.parametrize("identity", [None, (31, 1)])
def test_unresolved_start_never_allows_a_fresh_start(remote, identity):
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow, identity=identity)
    with patch("lighthouse_cli.quiz_preview_session.start_preview") as start:
        with pytest.raises(PreviewWorkflowError, match="reconcile"):
            workflow.run("start")
    start.assert_not_called()


@pytest.mark.parametrize("identity", [None, (31, 1)])
def test_abandon_is_local_and_keeps_the_unresolved_start_guard(remote, identity):
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow, identity=identity)
    for _ in range(2):  # repeated abandonment keeps the guard
        result = workflow.abandon()
        assert result["remote_attempt_deleted"] is False and result["unresolved_start"] is True
    with patch("lighthouse_cli.quiz_preview_session.start_preview") as start:
        with pytest.raises(PreviewWorkflowError, match="reconcile"):
            workflow.run("start")
    start.assert_not_called()


def test_abandoning_a_verified_preview_still_allows_a_new_start(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    start_local(workflow)
    assert workflow.abandon()["unresolved_start"] is False
    start_local(workflow)


def test_reconcile_without_attempt_id_only_lists_candidates(remote):
    _, state = remote
    state["attempts"] = [_attempt(5)]
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow)
    before = saved(workflow)
    state["attempts"] = [
        _attempt(5),                                           # in the pre-start baseline
        _attempt(31),                                          # new, own, incomplete
        _attempt(32, completed="2999-01-01T00:05:00Z"),        # completed
        _attempt(33, actor_id=8),                              # another account
        _attempt(29, started="2000-01-01T00:00:00Z"),          # started long before this start
    ]
    result = workflow.reconcile()
    assert result["reconciled"] is False and result["baseline_available"] is True
    assert [c["attempt_id"] for c in result["candidates"]] == [31]
    assert saved(workflow) == before


def test_reconcile_with_an_empty_listing_stays_uncertain(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow)
    result = workflow.reconcile()
    assert result["candidates"] == []
    assert workflow.status()["status"] == "uncertain"  # empty does not prove absence
    with patch("lighthouse_cli.quiz_preview_session.start_preview") as start:
        with pytest.raises(PreviewWorkflowError, match="reconcile"):
            workflow.run("start")
    start.assert_not_called()


@pytest.mark.parametrize("attempt_id", [5, 33, 34])
def test_reconcile_refuses_an_attempt_that_is_not_a_candidate(remote, attempt_id):
    _, state = remote
    state["attempts"] = [_attempt(5)]
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow)
    before = saved(workflow)
    state["attempts"] = [_attempt(5), _attempt(33, actor_id=8)]
    with patch("lighthouse_cli.quiz_preview_session.read_server_current_preview") as read:
        with pytest.raises(PreviewWorkflowError, match="not changed"):
            workflow.reconcile(attempt_id)
    read.assert_not_called()
    assert saved(workflow) == before


@pytest.mark.parametrize("layout_page", [1, 2])
def test_reconcile_binds_a_chosen_candidate_at_the_server_page(remote, layout_page):
    _, state = remote
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow)
    workflow.abandon()
    state["attempts"] = [_attempt(31)]
    with patch("lighthouse_cli.quiz_preview_session.read_server_current_preview",
               return_value=page(layout_page, attempt_id=31)) as read, \
            patch("lighthouse_cli.quiz_preview_session.read_current_preview") as strict:
        result = workflow.reconcile(31)
    assert result["reconciled"] is True and result["page"] == layout_page
    read.assert_called_once()
    strict.assert_not_called()
    status = workflow.status()
    assert (status["status"], status["attempt_id"], status["page"]) == ("active", 31, layout_page)
    assert status["unresolved_start"] is False


def test_reconcile_keeps_a_bound_identity_and_cursor(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow, identity=(31, 1))
    before = saved(workflow)
    with pytest.raises(PreviewWorkflowError, match="different attempt"):
        workflow.reconcile(32)
    assert saved(workflow) == before
    with patch("lighthouse_cli.quiz_preview_session.read_current_preview",
               return_value=page(attempt_id=31)) as strict, \
            patch("lighthouse_cli.quiz_preview_session.read_server_current_preview") as server:
        assert workflow.reconcile()["attempt_id"] == 31
    strict.assert_called_once()
    assert strict.call_args.kwargs["page"] == 1
    server.assert_not_called()
    assert workflow.status()["status"] == "active"


def test_reconcile_of_a_completed_bound_attempt_verifies_the_receipt(remote):
    _, state = remote
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow, identity=(31, 1))
    state["records"][31] = _attempt(31, completed="2999-01-01T00:05:00Z")
    receipt = {"submitted": True, "receipt_verified": True, "attempt_id": 31}
    with patch("lighthouse_cli.quiz_preview_session.verify_receipt", return_value=receipt) as verify:
        assert workflow.reconcile()["submitted"] is True
    verify.assert_called_once()
    assert workflow.status()["status"] == "submitted"


@pytest.mark.parametrize("record", [_attempt(31, actor_id=8), _attempt(31, quiz_id=99)])
def test_reconcile_identity_mismatch_leaves_the_checkpoint_unchanged(remote, record):
    _, state = remote
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow)
    before = saved(workflow)
    state["attempts"] = [_attempt(31)]
    state["records"][31] = record
    with pytest.raises(PreviewWorkflowError, match="could not be verified"):
        workflow.reconcile(31)
    assert saved(workflow) == before


def test_reconcile_unverified_preview_page_leaves_the_checkpoint_unchanged(remote):
    _, state = remote
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow)
    before = saved(workflow)
    state["attempts"] = [_attempt(31)]
    with patch("lighthouse_cli.quiz_preview_session.read_server_current_preview",
               side_effect=PreviewPageError()):
        with pytest.raises(PreviewWorkflowError, match="not changed"):
            workflow.reconcile(31)
    assert saved(workflow) == before


def test_reconcile_listing_failure_leaves_the_checkpoint_unchanged(remote):
    client, _ = remote
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow)
    before = saved(workflow)
    client._paginate_list.side_effect = NetworkError("Permission denied (HTTP 403).")
    with pytest.raises(NetworkError):
        workflow.reconcile(31)
    assert saved(workflow) == before


def test_reconcile_refuses_another_signed_in_account(remote):
    _, state = remote
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow, identity=(31, 1))
    state["actor"] = 8
    with pytest.raises(PreviewWorkflowError, match="different signed-in account"):
        workflow.reconcile()


def test_reconcile_refuses_a_resolved_checkpoint(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    start_local(workflow)
    with pytest.raises(PreviewWorkflowError, match="unresolved start"):
        workflow.reconcile()


def test_reconcile_respects_the_single_writer_lock(remote):
    workflow = PreviewWorkflow("trial", 10, 20)
    unknown_start(workflow)
    with workflow._locked(), pytest.raises(PreviewWorkflowError, match="Another operation"):
        workflow.reconcile()


def test_legacy_checkpoint_without_baseline_requires_explicit_selection(remote):
    _, state = remote
    workflow = PreviewWorkflow("trial", 10, 20)
    workflow.store.write_artifact(workflow.path, metadata={}, secret={
        "version": 1, "origin": workflow.connection.origin, "mode": "preview", "actor_id": 7,
        "course_id": 10, "quiz_id": 20, "status": "uncertain", "operation": "start",
        "attempt_id": None, "page": None,
    })
    state["attempts"] = [_attempt(31, started="2000-01-01T00:00:00Z")]
    result = workflow.reconcile()
    assert result["baseline_available"] is False
    assert [c["attempt_id"] for c in result["candidates"]] == [31]
    assert workflow.status()["status"] == "uncertain"


@pytest.mark.parametrize("extra", [
    {"unresolved_start": "yes"},
    {"baseline_attempt_ids": [0]},
    {"baseline_attempt_ids": "5"},
    {"start_intent_at": "yesterday"},
])
def test_invalid_optional_checkpoint_fields_fail_closed(remote, extra):
    workflow = PreviewWorkflow("trial", 10, 20)
    workflow.store.write_artifact(workflow.path, metadata={}, secret={
        "version": 1, "origin": workflow.connection.origin, "mode": "preview", "actor_id": 7,
        "course_id": 10, "quiz_id": 20, "status": "uncertain", "operation": "start",
        "attempt_id": None, "page": None, **extra,
    })
    with pytest.raises(PreviewWorkflowError, match="invalid"):
        workflow.status()


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
            "mode": "preview", "status": "uncertain", "reconciled": False, "candidates": [],
        }
        result = CliRunner().invoke(
            cli,
            ["instructor", "--site", "trial", "preview", "reconcile", "10", "20", "--json"],
        )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["candidates"] == []
    assert result.stderr == ""
    workflow.return_value.reconcile.assert_called_once_with(attempt_id=None)
    workflow.return_value.run.assert_not_called()


def test_cli_shows_fixed_refusal_messages_verbatim():
    with patch("lighthouse_cli.quiz_preview_commands.PreviewWorkflow") as workflow:
        workflow.return_value.run.side_effect = PreviewRefusedError(REFUSE_NOT_ON_PAGE)
        result = CliRunner().invoke(
            cli,
            ["instructor", "--site", "trial", "preview", "answer", "10", "20", "1", "2", "--yes", "--json"],
        )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == REFUSE_NOT_ON_PAGE
    assert REFUSE_NOT_ON_PAGE in result.stderr
