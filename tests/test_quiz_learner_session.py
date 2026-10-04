"""Learner attempt cursors: one write per step, settled by reading back or starting again."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from functools import partial
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import NetworkError, SessionExpiredError
from lighthouse_cli.cli import cli
from lighthouse_cli.quiz_attempt_page import (
    REFUSE_LEARNER_FIRST_PAGE,
    REFUSE_LEARNER_LAST_PAGE,
    REFUSE_LEARNER_NOT_LAST_PAGE,
    REFUSE_LEARNER_NOT_ON_PAGE,
    REFUSE_LEARNER_UNANSWERED,
    PreviewPageError,
    PreviewRefusedError,
)
from lighthouse_cli.quiz_learner_finish import (
    LearnerNotSubmittedError,
    LearnerSubmitUnknownError,
    LearnerUnansweredError,
)
from lighthouse_cli.quiz_learner_session import (
    LearnerWorkflow,
    LearnerWorkflowError,
    parse_answers,
    quiz_info,
)
from lighthouse_cli.quiz_learner_transport import (
    REFUSE_IMAGE_SOURCE,
    REFUSE_START_UNAVAILABLE,
    LearnerAdvanceUnknownError,
    LearnerSaveUnknownError,
    LearnerStartUnknownError,
    LearnerTimer,
)
from tests.test_quiz_attempt_page import LEARNER_BUTTONS
from tests.test_quiz_learner import ANSWERED, NEXT, PREVIOUS, body, parse, questions

SESSION = "lighthouse_cli.quiz_learner_session"
QUIZ = {"Name": "Week 3", "PreventMovingBackwards": False, "SubmissionTimeLimit": {"IsEnforced": False},
        "AttemptsAllowed": {"IsUnlimited": False, "NumberOfAttemptsAllowed": 2}}
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
ACTIVE = {"mode": "learner", "status": "active", "course_id": 10, "quiz_id": 20, "attempt_id": 30, "page": 1,
          "operation": None, "timer": None}


def open_page(number: int = 1, *, next_control: bool = True, content: str | None = None):
    content = questions(page=number) if content is None else content
    return parse(body(content, page=number, extra=LEARNER_BUTTONS + (NEXT if next_control else "")), page=number)


@pytest.fixture
def remote():
    client = Mock()
    client.base_url = "https://lighthouse.manipal.edu"
    state = {"actor": 7}
    client.get_json.side_effect = lambda path, **kwargs: {"Identifier": state["actor"]}
    client.get_quiz_detail.return_value = dict(QUIZ)
    with patch(f"{SESSION}.LighthouseClient", return_value=client), \
            patch(f"{SESSION}.read_learner_summary", return_value=Mock(can_continue=False)) as summary, \
            patch(f"{SESSION}.read_learner_timer", return_value=None):
        yield client, state, summary


def saved(workflow):
    return workflow.store.read_artifact(workflow.path)[1]


def start(workflow, page=None, *, resumed: bool = False):
    page = open_page() if page is None else page
    with patch(f"{SESSION}.read_learner_summary", return_value=Mock(can_continue=resumed)), \
            patch(f"{SESSION}.start_learner", return_value=page) as started:
        result = workflow.start()
    started.assert_called_once()
    return result


@pytest.fixture
def workflow(remote):
    """A cursor for an attempt started on page 1."""
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    return workflow


def seal_and_open(client, *, on_identity, **kwargs):
    on_identity(30, 1)
    return open_page()


def assert_refused(workflow, message, actions=("page", "next", "submit", "images", "answer")):
    """Each action is refused with the message before anything is read or sent."""
    with patch(f"{SESSION}.start_learner") as started, patch(f"{SESSION}.read_learner_page") as read, \
            patch(f"{SESSION}.save_learner_answers") as save, patch(f"{SESSION}.advance_learner") as advance, \
            patch(f"{SESSION}.retreat_learner") as retreat, patch(f"{SESSION}.submit_learner") as submit, \
            patch(f"{SESSION}.verify_learner_submission") as verify:
        for name in actions:
            action = partial(workflow.answer, {101: "o1"}) if name == "answer" else getattr(workflow, name)
            with pytest.raises(LearnerWorkflowError, match=message):
                action()
    for request in (started, read, save, advance, retreat, submit, verify):
        request.assert_not_called()


# -- start ----------------------------------------------------------------------


def test_start_records_its_intent_first_and_reports_the_quiz(remote):
    workflow = LearnerWorkflow(10, 20)
    seen = {}
    summary = Mock(can_continue=False)

    def fake_start(client, *, on_identity, **kwargs):
        seen.update(saved(workflow))
        on_identity(30, 1)
        seen["sealed"] = saved(workflow)
        return open_page()
    with patch(f"{SESSION}.read_learner_summary", return_value=summary), \
            patch(f"{SESSION}.start_learner", side_effect=fake_start) as started:
        result = workflow.start()
    assert seen["status"] == "uncertain" and seen["operation"] == "start" and seen["attempt_id"] is None
    assert seen["sealed"]["attempt_id"] == 30 and seen["sealed"]["page"] == 1
    # The summary just read is reused rather than requested again.
    assert started.call_args.kwargs["summary"] is summary
    assert result["attempt_id"] == 30 and result["resumed"] is False
    assert result["quiz"] == {"name": "Week 3", "forward_only": False, "attempts_allowed": 2, "time_limit_minutes": None}
    assert result["timer"] is None
    assert workflow.status() == ACTIVE
    assert '"actor_id"' not in workflow.path.read_text()  # the cursor is sealed


def test_start_continues_the_attempt_in_progress(remote):
    workflow = LearnerWorkflow(10, 20)
    result = start(workflow, open_page(2), resumed=True)
    assert result["resumed"] is True and result["page"] == 2
    assert workflow.status()["page"] == 2


@pytest.mark.parametrize(("quiz", "message"), [
    ({**QUIZ, "SubmissionTimeLimit": {"IsEnforced": True}}, "could not be read"),
    ({**QUIZ, "SubmissionTimeLimit": {"IsEnforced": True, "TimeLimitValue": 0}}, "could not be read"),
    ({**QUIZ, "SubmissionTimeLimit": {"IsEnforced": True, "TimeLimitValue": 1.5}}, "could not be read"),
    ({**QUIZ, "SubmissionTimeLimit": None}, "could not be read"),
    ({**QUIZ, "PreventMovingBackwards": 1}, "could not be read"),
    ({**QUIZ, "AttemptsAllowed": None}, "could not be read"),
    ({**QUIZ, "AttemptsAllowed": {"IsUnlimited": False, "NumberOfAttemptsAllowed": None}}, "could not be read"),
    ({**QUIZ, "AttemptsAllowed": {"IsUnlimited": False, "NumberOfAttemptsAllowed": 2.0}}, "could not be read"),
    ({**QUIZ, "AttemptsAllowed": {"IsUnlimited": False, "NumberOfAttemptsAllowed": 0}}, "could not be read"),
    ([], "could not be read"),
])
def test_a_quiz_the_cli_cannot_take_is_refused_before_anything_is_sent(remote, quiz, message):
    client, _, summary = remote
    client.get_quiz_detail.return_value = quiz
    workflow = LearnerWorkflow(10, 20)
    assert_refused(workflow, message, ("start",))
    summary.assert_not_called()
    assert workflow.status()["status"] == "absent"


def test_unlimited_attempts_read_as_none():
    assert quiz_info({**QUIZ, "AttemptsAllowed": {"IsUnlimited": True, "NumberOfAttemptsAllowed": None}})[
        "attempts_allowed"] is None
    assert quiz_info({**QUIZ, "AttemptsAllowed": {"IsUnlimited": True}, "Name": None}) == {
        "name": None, "forward_only": False, "attempts_allowed": None, "time_limit_minutes": None}


def test_a_refused_start_keeps_the_previous_cursor(remote):
    workflow = LearnerWorkflow(10, 20)
    with patch(f"{SESSION}.start_learner", side_effect=PreviewRefusedError(REFUSE_START_UNAVAILABLE)):
        with pytest.raises(PreviewRefusedError):
            workflow.start()
    assert workflow.status()["status"] == "absent"
    start(workflow, open_page(2), resumed=True)
    with patch(f"{SESSION}.read_learner_summary", return_value=Mock(can_continue=True)), \
            patch(f"{SESSION}.start_learner", side_effect=PreviewRefusedError(REFUSE_START_UNAVAILABLE)):
        with pytest.raises(PreviewRefusedError):
            workflow.start()
    assert workflow.status() == {**ACTIVE, "page": 2}


def test_an_unknown_start_is_settled_only_by_starting_again(remote):
    workflow = LearnerWorkflow(10, 20)

    def unknown(client, *, on_identity, **kwargs):
        on_identity(30, 1)
        raise LearnerStartUnknownError(attempt_id=30, page=1)
    with patch(f"{SESSION}.start_learner", side_effect=unknown):
        with pytest.raises(LearnerStartUnknownError):
            workflow.start()
    assert workflow.status()["status"] == "uncertain"
    assert workflow.status()["operation"] == "start"
    assert_refused(workflow, "Run attempt start", ("page", "next", "previous", "answer"))
    start(workflow, open_page(1), resumed=True)
    assert workflow.status()["status"] == "active"


def test_an_unknown_start_without_an_identity_is_kept(remote):
    workflow = LearnerWorkflow(10, 20)
    with patch(f"{SESSION}.start_learner", side_effect=LearnerStartUnknownError()):
        with pytest.raises(LearnerStartUnknownError):
            workflow.start()
    status = workflow.status()
    assert (status["status"], status["attempt_id"], status["page"]) == ("uncertain", None, None)
    # No attempt is known to exist, so starting again may start one.
    with patch(f"{SESSION}.start_learner", return_value=open_page()) as started:
        workflow.start()
    assert started.call_args.kwargs["continue_only"] is False


@pytest.mark.parametrize("change", [
    {}, *({"status": "uncertain", "operation": operation} for operation in ("start", "answer", "next", "previous", "submit")),
])
def test_the_clis_unsubmitted_attempt_is_only_continued(workflow, change):
    workflow._save({**saved(workflow), **change})
    before = saved(workflow)
    assert_refused(workflow, "no new attempt was started", ("start",))
    assert saved(workflow) == before
    with patch(f"{SESSION}.read_learner_summary", return_value=Mock(can_continue=True)), \
            patch(f"{SESSION}.start_learner", return_value=open_page(2)) as started:
        workflow.start()
    assert started.call_args.kwargs["continue_only"] is True
    assert (workflow.status()["status"], workflow.status()["page"]) == ("active", 2)


def test_another_accounts_unverified_change_is_never_replaced(remote, workflow):
    client, state, _ = remote
    state["actor"] = 8
    start(workflow)  # an active cursor of another account is replaced
    assert saved(workflow)["actor_id"] == 8
    workflow._save({**saved(workflow), "status": "uncertain", "operation": "next"})
    before = saved(workflow)
    state["actor"] = 7
    assert_refused(workflow, "different signed-in account", ("start",))
    client.get_quiz_detail.assert_called()
    assert saved(workflow) == before


# -- answers and Next -----------------------------------------------------------


def test_answers_are_saved_from_the_page_just_read_then_next_uses_the_readback(workflow):
    current, answered, following = open_page(), open_page(content=ANSWERED), open_page(2)
    with patch(f"{SESSION}.read_learner_page", return_value=current) as read, \
            patch(f"{SESSION}.save_learner_answers", return_value=answered) as save, \
            patch(f"{SESSION}.advance_learner", return_value=following) as advance:
        result = workflow.answer({101: "o2", 102: ["o11", "o13"], 103: ["four", "six"]}, advance=True)
    read.assert_called_once()
    assert save.call_args.kwargs["current"] is current
    assert save.call_args.kwargs["answers"] == {101: "o2", 102: ["o11", "o13"], 103: ["four", "six"]}
    assert advance.call_args.kwargs["current"] is answered
    assert advance.call_args.kwargs["page"] == 1
    assert result["page"] == 2 and workflow.status()["page"] == 2


@pytest.mark.parametrize(("current", "answers", "allow", "message"), [
    (open_page(next_control=False), {101: "o2"}, False, REFUSE_LEARNER_LAST_PAGE),
    (open_page(), {101: "o2"}, False, REFUSE_LEARNER_UNANSWERED),
    (open_page(), {101: "o2", 102: ["o11"], 103: ["four", ""]}, False, REFUSE_LEARNER_UNANSWERED),
    (open_page(), {999: "o2"}, True, REFUSE_LEARNER_NOT_ON_PAGE),
])
def test_answer_and_next_is_checked_before_anything_is_saved(workflow, current, answers, allow, message):
    with patch(f"{SESSION}.read_learner_page", return_value=current), \
            patch(f"{SESSION}.save_learner_answers") as save, patch(f"{SESSION}.advance_learner") as advance:
        with pytest.raises(PreviewRefusedError, match=message):
            workflow.answer(answers, advance=True, allow_unanswered=allow)
    save.assert_not_called()
    advance.assert_not_called()
    assert workflow.status()["status"] == "active"


def test_a_next_refused_after_the_save_keeps_the_saved_answers(workflow):
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.save_learner_answers", return_value=open_page(content=ANSWERED)) as save, \
            patch(f"{SESSION}.advance_learner", side_effect=PreviewRefusedError(REFUSE_LEARNER_LAST_PAGE)):
        with pytest.raises(PreviewRefusedError):
            workflow.answer({101: "o2"}, advance=True, allow_unanswered=True)
    save.assert_called_once()
    assert workflow.status() == ACTIVE


def test_unanswered_questions_can_be_left_explicitly(workflow):
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.save_learner_answers", return_value=open_page()), \
            patch(f"{SESSION}.advance_learner", return_value=open_page(2)) as advance:
        workflow.answer({101: "o2"}, advance=True, allow_unanswered=True)
    assert advance.call_args.kwargs["allow_unanswered"] is True


def test_an_unverified_save_is_settled_by_reading_the_page(workflow):
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.save_learner_answers", side_effect=LearnerSaveUnknownError()):
        with pytest.raises(LearnerSaveUnknownError):
            workflow.answer({101: "o2"})
    assert (workflow.status()["status"], workflow.status()["operation"]) == ("uncertain", "answer")
    assert_refused(workflow, "Run attempt page", ("next", "previous", "submit", "images", "answer"))
    with patch(f"{SESSION}.read_learner_page", return_value=open_page(content=ANSWERED)) as read:
        result = workflow.page()
    assert read.call_args.kwargs == {"course_id": 10, "quiz_id": 20, "attempt_id": 30, "page": 1}
    assert result["questions"][0]["saved"] is True
    assert workflow.status()["status"] == "active"


def test_an_unknown_next_needs_starting_again(workflow):
    with patch(f"{SESSION}.read_learner_page", return_value=open_page(content=ANSWERED + NEXT)), \
            patch(f"{SESSION}.advance_learner", side_effect=LearnerAdvanceUnknownError()):
        with pytest.raises(LearnerAdvanceUnknownError):
            workflow.next()
    assert (workflow.status()["status"], workflow.status()["operation"]) == ("uncertain", "next")
    assert_refused(workflow, "Run attempt start", ("page",))
    start(workflow, open_page(2), resumed=True)
    assert workflow.status() == {**ACTIVE, "page": 2}


def back_page():
    return parse(body(questions(page=2), page=2, extra=LEARNER_BUTTONS + PREVIOUS), page=2)


def test_previous_moves_back_from_the_page_just_read_and_commits_the_readback(remote):
    workflow = LearnerWorkflow(10, 20)
    start(workflow, back_page(), resumed=True)
    current, before = back_page(), open_page(content=ANSWERED)
    seen = {}

    def retreat(client, **kwargs):
        seen.update(saved(workflow))  # the intent is durable before the write
        return before
    with patch(f"{SESSION}.read_learner_page", return_value=current) as read, \
            patch(f"{SESSION}.retreat_learner", side_effect=retreat) as retreat_call:
        result = workflow.previous()
    assert read.call_args.kwargs == {"course_id": 10, "quiz_id": 20, "attempt_id": 30, "page": 2}
    assert retreat_call.call_args.kwargs == {"course_id": 10, "quiz_id": 20, "attempt_id": 30, "page": 2, "current": current}
    assert (seen["status"], seen["operation"], seen["page"]) == ("uncertain", "previous", 2)
    assert result == {**before.public_data(), "timer": None}
    assert result["page"] == 1 and result["has_next_control"] is True and result["has_previous_control"] is False
    assert result["questions"][0]["selected_choice_ids"] == ["o2"]
    assert workflow.status() == {"mode": "learner", "status": "active", "course_id": 10, "quiz_id": 20,
                                 "attempt_id": 30, "page": 1, "operation": None, "timer": None}


def test_previous_is_refused_on_a_forward_only_quiz_whatever_the_page_shows(remote):
    client, _, _ = remote
    client.get_quiz_detail.return_value = {**QUIZ, "PreventMovingBackwards": True}
    workflow = LearnerWorkflow(10, 20)
    start(workflow, back_page(), resumed=True)
    before = saved(workflow)
    with patch(f"{SESSION}.read_learner_page", return_value=back_page()) as read, \
            patch(f"{SESSION}.retreat_learner") as retreat:
        with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FIRST_PAGE)):
            workflow.previous()
    read.assert_not_called()
    retreat.assert_not_called()
    assert saved(workflow) == before


@pytest.mark.parametrize("failure", [PreviewRefusedError(REFUSE_LEARNER_FIRST_PAGE), NetworkError("no")])
def test_a_previous_refused_or_failed_before_sending_keeps_the_cursor(remote, failure):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    before = saved(workflow)
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.retreat_learner", side_effect=failure):
        with pytest.raises(type(failure)):
            workflow.previous()
    assert saved(workflow) == before


def test_an_unknown_previous_needs_starting_again(remote):
    workflow = LearnerWorkflow(10, 20)
    start(workflow, back_page(), resumed=True)
    with patch(f"{SESSION}.read_learner_page", return_value=back_page()), \
            patch(f"{SESSION}.retreat_learner", side_effect=LearnerAdvanceUnknownError()):
        with pytest.raises(LearnerAdvanceUnknownError, match="Run attempt start"):
            workflow.previous()
    assert (workflow.status()["status"], workflow.status()["operation"]) == ("uncertain", "previous")
    with patch(f"{SESSION}.read_learner_page") as read, patch(f"{SESSION}.retreat_learner") as retreat:
        for action in (workflow.page, workflow.previous, workflow.next):
            with pytest.raises(LearnerWorkflowError, match="Run attempt start"):
                action()
    read.assert_not_called()
    retreat.assert_not_called()
    start(workflow, open_page(1), resumed=True)
    assert (workflow.status()["status"], workflow.status()["page"]) == ("active", 1)


@pytest.mark.parametrize("failure", [PreviewRefusedError(REFUSE_LEARNER_NOT_ON_PAGE), NetworkError("no")])
def test_a_write_that_failed_before_sending_keeps_the_cursor(workflow, failure):
    before = saved(workflow)
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.save_learner_answers", side_effect=failure):
        with pytest.raises(type(failure)):
            workflow.answer({101: "o2"})
    assert saved(workflow) == before


def test_another_account_cannot_use_the_cursor(remote, workflow):
    _, state, _ = remote
    state["actor"] = 8
    assert_refused(workflow, "different signed-in account", ("page",))
    # Also when the cursor would otherwise ask for a restart.
    workflow._save({**saved(workflow), "status": "uncertain", "operation": "next"})
    assert_refused(workflow, "different signed-in account", ("page",))


def test_nothing_runs_without_a_started_attempt(remote):
    workflow = LearnerWorkflow(10, 20)
    assert_refused(workflow, "Run attempt start", ("page",))
    assert workflow.status() == {"mode": "learner", "status": "absent", "course_id": 10, "quiz_id": 20}


TIMER_STATE = {"limit_seconds": 240, "ends_at": 1.0, "clock_offset": 0.0, "auto_submit": True}


@pytest.mark.parametrize("change", [
    {"mode": "preview"}, {"actor_id": 0}, {"page": 0}, {"attempt_id": None}, {"status": "uncertain"},
    {"operation": "answer"}, {"quiz_id": 21}, {"origin": "https://other.example"},
    {"status": "submitted", "operation": "submit"}, {"status": "closed"}, {"operation": "finish"},
    {"timer": 240}, {"timer": {key: value for key, value in TIMER_STATE.items() if key != "auto_submit"}},
    *({"timer": {**TIMER_STATE, **timer}} for timer in (
        {"limit_seconds": 0}, {"ends_at": "soon"}, {"auto_submit": 1}, {"ends_at": 1e18}, {"ends_at": 10**400},
        {"ends_at": -1.0}, {"ends_at": 0}, {"clock_offset": 1e18}, {"clock_offset": None}, {"grace_seconds": 60})),
])
def test_a_tampered_cursor_is_rejected(workflow, change):
    workflow._save({**saved(workflow), **change})
    with pytest.raises(LearnerWorkflowError, match="invalid"):
        workflow.status()


def test_start_replaces_an_invalid_cursor(workflow):
    workflow._save({**saved(workflow), "page": 0})
    start(workflow, open_page(2), resumed=True)
    assert workflow.status()["page"] == 2


def test_an_unreadable_cursor_is_invalid_and_replaced_by_start(workflow):
    workflow.path.write_text("not a sealed checkpoint")
    with pytest.raises(LearnerWorkflowError, match=r"invalid\. Run attempt start"):
        workflow.status()
    start(workflow, open_page(2))
    assert workflow.status()["page"] == 2


def test_forget_drops_only_the_local_record(workflow):
    workflow.path.write_text("not a sealed checkpoint")
    with patch(f"{SESSION}.LighthouseClient") as client:
        assert workflow.forget() == {"mode": "learner", "course_id": 10, "quiz_id": 20, "forgotten": True}
        assert workflow.forget()["forgotten"] is False
    client.assert_not_called()
    assert workflow.status()["status"] == "absent"


def test_one_operation_at_a_time(remote):
    workflow = LearnerWorkflow(10, 20)
    with workflow._locked():
        with pytest.raises(LearnerWorkflowError, match="Another operation"):
            LearnerWorkflow(10, 20).status()
    descriptor = os.open(workflow.lock_path, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released afterwards
    finally:
        os.close(descriptor)


# -- submit and verify ----------------------------------------------------------

RECEIPT = {"mode": "learner", "course_id": 10, "quiz_id": 20, "attempt_id": 30, "submitted": True,
           "attempt_number": 1, "score": 3.0, "out_of": 4.0}


def test_submit_records_its_intent_then_closes_the_attempt(workflow):
    last = open_page(next_control=False)
    seen = {}

    def fake_submit(client, **kwargs):
        seen.update(saved(workflow))
        return RECEIPT
    with patch(f"{SESSION}.read_learner_page", return_value=last), \
            patch(f"{SESSION}.submit_learner", side_effect=fake_submit) as submit:
        assert workflow.submit(allow_unanswered=True) == RECEIPT
    assert (seen["status"], seen["operation"]) == ("uncertain", "submit")
    assert submit.call_args.kwargs == {"course_id": 10, "quiz_id": 20, "attempt_id": 30, "page": 1,
                                       "allow_unanswered": True, "current": last}
    assert workflow.status() == {**ACTIVE, "status": "submitted"}
    assert_refused(workflow, "has been submitted")


@pytest.mark.parametrize("failure", [
    LearnerUnansweredError([{"page": 1, "question_id": 101, "number": 1}]),
    PreviewRefusedError(REFUSE_LEARNER_NOT_LAST_PAGE), NetworkError("no"),
])
def test_a_submission_stopped_before_its_final_request_keeps_the_attempt_open(workflow, failure):
    before = saved(workflow)
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.submit_learner", side_effect=failure):
        with pytest.raises(type(failure)):
            workflow.submit()
    assert saved(workflow) == before


def test_an_unverified_submission_is_settled_only_by_its_receipt(workflow):
    with patch(f"{SESSION}.read_learner_page", return_value=open_page(next_control=False)), \
            patch(f"{SESSION}.submit_learner", side_effect=LearnerSubmitUnknownError()):
        with pytest.raises(LearnerSubmitUnknownError):
            workflow.submit()
    assert (workflow.status()["status"], workflow.status()["operation"]) == ("uncertain", "submit")
    assert_refused(workflow, "Run attempt verify")
    for failure in (LearnerSubmitUnknownError(), LearnerNotSubmittedError()):
        with patch(f"{SESSION}.verify_learner_submission", side_effect=failure):
            with pytest.raises(type(failure)):
                workflow.verify()
        assert workflow.status()["status"] == "uncertain"
    with patch(f"{SESSION}.verify_learner_submission", return_value=RECEIPT) as verify:
        assert workflow.verify() == RECEIPT
    assert verify.call_args.kwargs == {"course_id": 10, "quiz_id": 20, "attempt_id": 30}
    assert (workflow.status()["status"], workflow.status()["operation"]) == ("submitted", None)


@pytest.mark.parametrize(("change", "actor"), [
    ({"status": "submitted"}, 7), ({"status": "submitted"}, 8), ({}, 8),
])
def test_other_cursors_never_restrict_a_new_attempt(remote, workflow, change, actor):
    _, state, _ = remote
    workflow._save({**saved(workflow), **change})
    state["actor"] = actor
    with patch(f"{SESSION}.start_learner", return_value=open_page()) as started:
        workflow.start()
    assert started.call_args.kwargs["continue_only"] is False
    assert workflow.status()["status"] == "active"


def test_verify_reads_only_this_accounts_started_attempt(remote):
    _, state, _ = remote
    workflow = LearnerWorkflow(10, 20)
    assert_refused(workflow, "Run attempt start", ("verify",))
    with patch(f"{SESSION}.start_learner", side_effect=LearnerStartUnknownError()):
        with pytest.raises(LearnerStartUnknownError):
            workflow.start()
    assert_refused(workflow, "Run attempt start", ("verify",))
    start(workflow)
    state["actor"] = 8
    assert_refused(workflow, "different signed-in account", ("verify",))
    state["actor"] = 7
    # An attempt submitted elsewhere, e.g. in the browser, closes the cursor too.
    with patch(f"{SESSION}.verify_learner_submission", return_value=RECEIPT):
        workflow.verify()
        assert workflow.verify() == RECEIPT  # again, once submitted
    assert workflow.status()["status"] == "submitted"


def test_a_recovery_start_that_fails_midway_still_names_the_clis_attempt(remote):
    workflow = LearnerWorkflow(10, 20)
    start(workflow, open_page(2), resumed=True)
    workflow._save({**saved(workflow), "status": "uncertain", "operation": "submit"})
    with patch(f"{SESSION}.read_learner_summary", return_value=Mock(can_continue=True)), \
            patch(f"{SESSION}.start_learner", side_effect=LearnerStartUnknownError()):
        with pytest.raises(LearnerStartUnknownError):
            workflow.start()
    assert workflow.status() == {**ACTIVE, "status": "uncertain", "page": 2, "operation": "start"}
    # Once the attempt ends, start still only continues it, and verify reads it.
    assert_refused(workflow, "no new attempt was started", ("start",))
    with patch(f"{SESSION}.verify_learner_submission", return_value=RECEIPT) as verify:
        workflow.verify()
    assert verify.call_args.kwargs["attempt_id"] == 30


def test_another_attempt_in_progress_is_not_taken_over(workflow):
    workflow._save({**saved(workflow), "status": "uncertain", "operation": "submit"})
    other = dataclasses.replace(open_page(), attempt_id=31)

    def continued(client, *, on_identity, **kwargs):
        on_identity(31, 1)
        return other
    with patch(f"{SESSION}.read_learner_summary", return_value=Mock(can_continue=True)), \
            patch(f"{SESSION}.start_learner", side_effect=continued):
        with pytest.raises(LearnerWorkflowError, match="different attempt in progress"):
            workflow.start()
    assert (workflow.status()["status"], workflow.status()["attempt_id"]) == ("uncertain", 30)
    assert_refused(workflow, "Run attempt start", ("page",))
    # Once the CLI's record is dropped, start continues the attempt in progress.
    workflow.forget()
    start(workflow, other, resumed=True)
    assert workflow.status()["attempt_id"] == 31


# -- timed attempts -------------------------------------------------------------


def timed(seconds_left: float, *, auto_submit: bool = True) -> LearnerTimer:
    return LearnerTimer(240, time.time() + seconds_left, auto_submit)


def start_timed(workflow, timer):
    with patch(f"{SESSION}.read_learner_timer", return_value=timer) as read:
        result = start(workflow)
    read.assert_called_once()
    assert read.call_args.kwargs == {"course_id": 10, "quiz_id": 20, "attempt_id": 30}
    return result


def time_up(workflow):
    state = saved(workflow)
    workflow._save({**state, "timer": {**state["timer"], "ends_at": time.time() - 1}})


def assert_waiting_for_brightspace(workflow):
    with patch(f"{SESSION}.verify_learner_submission", side_effect=LearnerNotSubmittedError()), \
            patch(f"{SESSION}.read_learner_timer", return_value=timed(-5)):
        with pytest.raises(LearnerWorkflowError, match="has not submitted this attempt yet"):
            workflow.verify()


def test_a_timed_start_keeps_its_limit_and_every_page_reports_the_time_left(remote):
    client, _, _ = remote
    client.get_quiz_detail.return_value = {**QUIZ, "SubmissionTimeLimit": {"IsEnforced": True, "TimeLimitValue": 4}}
    workflow = LearnerWorkflow(10, 20)
    result = start_timed(workflow, timed(200))
    assert result["quiz"]["time_limit_minutes"] == 4
    assert set(saved(workflow)["timer"]) == set(TIMER_STATE)
    ends_at = saved(workflow)["timer"]["ends_at"]
    assert result["timer"] == {"limit_seconds": 240, "seconds_left": result["timer"]["seconds_left"],
                               "ends_at": datetime.fromtimestamp(ends_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "auto_submit": True}
    assert 190 <= result["timer"]["seconds_left"] <= 200
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", result["timer"]["ends_at"])
    assert workflow.status()["timer"]["ends_at"] == result["timer"]["ends_at"]
    # Later commands count down from the cursor and never read the limit again.
    with patch(f"{SESSION}.read_learner_timer") as read:
        with patch(f"{SESSION}.read_learner_page", return_value=open_page()):
            assert workflow.page()["timer"]["limit_seconds"] == 240
        with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
                patch(f"{SESSION}.save_learner_answers", return_value=open_page()):
            assert workflow.answer({101: "o1", 102: ["o1"], 103: "x"}, allow_unanswered=True)["timer"]["auto_submit"]
        with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
                patch(f"{SESSION}.advance_learner", return_value=back_page()):
            assert workflow.next(allow_unanswered=True)["timer"]["limit_seconds"] == 240
        with patch(f"{SESSION}.read_learner_page", return_value=back_page()), \
                patch(f"{SESSION}.retreat_learner", return_value=open_page()):
            assert workflow.previous()["timer"]["limit_seconds"] == 240
    read.assert_not_called()


TIME_UP = "Time is up for this attempt, and the CLI sends nothing more to it"


def test_once_time_is_up_nothing_is_sent_and_verify_waits_for_brightspace(remote):
    client, _, summary = remote
    workflow = LearnerWorkflow(10, 20)
    start_timed(workflow, timed(200))
    time_up(workflow)
    assert workflow.status()["timer"]["seconds_left"] == 0
    summary.reset_mock()
    client.get_json.reset_mock()
    assert_refused(workflow, TIME_UP, ("page", "next", "previous", "submit", "images", "answer"))
    client.get_json.assert_not_called()  # not even the account check
    assert_refused(workflow, TIME_UP, ("start",))
    summary.assert_not_called()
    assert_waiting_for_brightspace(workflow)
    assert workflow.status()["status"] == "active"
    with patch(f"{SESSION}.verify_learner_submission", return_value=RECEIPT):
        assert workflow.verify() == RECEIPT
    assert workflow.status()["status"] == "submitted"
    # A new attempt has a limit of its own.
    assert 190 <= start_timed(workflow, timed(200))["timer"]["seconds_left"] <= 200


@pytest.mark.parametrize("operation", ["answer", "next", "previous", "submit"])
def test_once_time_is_up_an_unsettled_write_also_points_to_verify(remote, operation):
    workflow = LearnerWorkflow(10, 20)
    start_timed(workflow, timed(200))
    time_up(workflow)
    workflow._save({**saved(workflow), "status": "uncertain", "operation": operation})
    assert_refused(workflow, TIME_UP, ("page", "previous", "answer"))


def test_time_that_runs_out_during_a_command_stops_its_next_write(remote):
    workflow = LearnerWorkflow(10, 20)
    start_timed(workflow, timed(100))
    clock = [time.time()]

    def save(*args, **kwargs):
        clock[0] += 200  # the save outlasts the time left
        return open_page()
    with patch(f"{SESSION}.time", Mock(time=lambda: clock[0])), \
            patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.save_learner_answers", side_effect=save), \
            patch(f"{SESSION}.advance_learner") as advance:
        with pytest.raises(LearnerWorkflowError, match=TIME_UP):
            workflow.answer({101: "o2"}, advance=True, allow_unanswered=True)
    advance.assert_not_called()
    assert (saved(workflow)["status"], saved(workflow)["operation"]) == ("active", None)


def test_time_that_runs_out_before_a_page_change_stops_it(remote):
    workflow = LearnerWorkflow(10, 20)
    start_timed(workflow, timed(100))
    clock = [time.time()]

    def read(*args, **kwargs):
        clock[0] += 200  # the page read outlasts the time left
        return open_page()
    with patch(f"{SESSION}.time", Mock(time=lambda: clock[0])), \
            patch(f"{SESSION}.read_learner_page", side_effect=read), \
            patch(f"{SESSION}.retreat_learner") as retreat:
        with pytest.raises(LearnerWorkflowError, match=TIME_UP):
            workflow.previous()
    retreat.assert_not_called()
    assert (saved(workflow)["status"], saved(workflow)["operation"]) == ("active", None)


def test_the_countdown_runs_on_the_servers_clock(remote):
    workflow = LearnerWorkflow(10, 20)
    deadline = time.time() + 600 + 200  # the local clock runs 10 minutes behind the server's
    timer = start_timed(workflow, LearnerTimer(240, deadline, True, clock_offset=600))["timer"]
    assert 190 <= timer["seconds_left"] <= 200
    assert saved(workflow)["timer"]["ends_at"] == round(deadline, 3)  # reported on the server's clock
    assert timer["ends_at"] == datetime.fromtimestamp(round(deadline, 3), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize(("refreshed", "message"), [
    (-5, "has not submitted this attempt yet"), ("error", "has not submitted this attempt yet"),
    (100, "still in progress"), (None, "still in progress"),
])
def test_once_time_is_up_verify_reads_the_timer_again(remote, refreshed, message):
    workflow = LearnerWorkflow(10, 20)
    start_timed(workflow, timed(200))
    time_up(workflow)
    read = {"side_effect": NetworkError("no")} if refreshed == "error" else {
        "return_value": None if refreshed is None else timed(refreshed)}
    with patch(f"{SESSION}.verify_learner_submission", side_effect=LearnerNotSubmittedError()), \
            patch(f"{SESSION}.read_learner_timer", **read) as timer:
        with pytest.raises((LearnerWorkflowError, LearnerNotSubmittedError), match=message):
            workflow.verify()
    assert timer.call_args.kwargs == {"course_id": 10, "quiz_id": 20, "attempt_id": 30}
    if message == "still in progress":  # extra time, or a clock set right: the attempt goes on
        with patch(f"{SESSION}.read_learner_page", return_value=open_page()):
            workflow.page()


def test_without_auto_submit_the_attempt_stays_open_past_its_limit(remote):
    workflow = LearnerWorkflow(10, 20)
    start_timed(workflow, timed(-30, auto_submit=False))
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()):
        timer = workflow.page()["timer"]
    assert (timer["seconds_left"], timer["auto_submit"]) == (0, False)
    with patch(f"{SESSION}.verify_learner_submission", side_effect=LearnerNotSubmittedError()):
        with pytest.raises(LearnerNotSubmittedError):
            workflow.verify()
    # Answers are still saved, and the attempt moved on, back, on again and submitted.
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.save_learner_answers", return_value=open_page()) as save:
        workflow.answer({101: "o1"}, allow_unanswered=True)
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.advance_learner", return_value=open_page(2, next_control=False)) as advance, \
            patch(f"{SESSION}.retreat_learner", return_value=open_page()) as retreat:
        workflow.next(allow_unanswered=True)
        assert workflow.previous()["page"] == 1
        workflow.next(allow_unanswered=True)
    with patch(f"{SESSION}.read_learner_page", return_value=open_page(2, next_control=False)), \
            patch(f"{SESSION}.submit_learner", return_value=RECEIPT) as submit:
        assert workflow.submit(allow_unanswered=True) == RECEIPT
    save.assert_called_once()
    assert advance.call_count == 2
    retreat.assert_called_once()
    submit.assert_called_once()


@pytest.mark.parametrize(("failure", "raised"), [
    (PreviewPageError(), LearnerWorkflowError), (NetworkError("no"), LearnerWorkflowError),
    (SessionExpiredError("expired"), SessionExpiredError),
])
def test_a_limit_that_cannot_be_read_leaves_the_attempt_to_continue(remote, failure, raised):
    workflow = LearnerWorkflow(10, 20)
    with patch(f"{SESSION}.start_learner", side_effect=seal_and_open), \
            patch(f"{SESSION}.read_learner_timer", side_effect=failure):
        with pytest.raises(raised, match="time limit could not be read" if raised is LearnerWorkflowError else None):
            workflow.start()
    assert workflow.status() == {**ACTIVE, "status": "uncertain", "operation": "start"}
    assert_refused(workflow, "Run attempt start", ("page",))
    # Starting again only continues that attempt and reads its limit again.
    with patch(f"{SESSION}.read_learner_summary", return_value=Mock(can_continue=True)), \
            patch(f"{SESSION}.start_learner", return_value=open_page()) as again, \
            patch(f"{SESSION}.read_learner_timer", return_value=timed(200)):
        assert workflow.start()["timer"]["limit_seconds"] == 240
    assert again.call_args.kwargs["continue_only"] is True


def test_a_recovery_start_that_finds_the_time_up_refuses_the_attempt(remote):
    workflow = LearnerWorkflow(10, 20)
    with patch(f"{SESSION}.start_learner", side_effect=seal_and_open), \
            patch(f"{SESSION}.read_learner_timer", side_effect=NetworkError("no")):
        with pytest.raises(LearnerWorkflowError, match="time limit could not be read"):
            workflow.start()
    with patch(f"{SESSION}.read_learner_summary", return_value=Mock(can_continue=True)), \
            patch(f"{SESSION}.start_learner", return_value=open_page()), \
            patch(f"{SESSION}.read_learner_timer", return_value=timed(-5)):
        with pytest.raises(LearnerWorkflowError, match=TIME_UP):
            workflow.start()
    assert (saved(workflow)["status"], saved(workflow)["timer"]["limit_seconds"]) == ("active", 240)
    assert_waiting_for_brightspace(workflow)


def test_a_recovery_start_keeps_the_attempts_limit(remote):
    workflow = LearnerWorkflow(10, 20)
    start_timed(workflow, timed(200))
    timer = saved(workflow)["timer"]
    workflow._save({**saved(workflow), "status": "uncertain", "operation": "next"})
    with patch(f"{SESSION}.read_learner_summary", return_value=Mock(can_continue=True)), \
            patch(f"{SESSION}.start_learner", side_effect=LearnerStartUnknownError()):
        with pytest.raises(LearnerStartUnknownError):
            workflow.start()
    assert (saved(workflow)["operation"], saved(workflow)["timer"]) == ("start", timer)
    time_up(workflow)
    assert_waiting_for_brightspace(workflow)


def test_a_cursor_from_before_previous_still_goes_back_by_the_page(remote):
    workflow = LearnerWorkflow(10, 20)
    start(workflow, back_page(), resumed=True)
    state = saved(workflow)
    del state["forward_only"]
    workflow._save(state)
    with patch(f"{SESSION}.read_learner_page", return_value=back_page()), \
            patch(f"{SESSION}.retreat_learner", return_value=open_page()):
        assert workflow.previous()["page"] == 1
    workflow._save({**saved(workflow), "forward_only": 1})
    with pytest.raises(LearnerWorkflowError, match="invalid"):
        workflow.status()


def test_a_cursor_from_before_timed_quizzes_reads_as_untimed(workflow):
    state = saved(workflow)
    del state["timer"]
    workflow._save(state)
    assert workflow.status()["timer"] is None
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()):
        assert workflow.page()["timer"] is None

# -- images ---------------------------------------------------------------------


def image_page():
    page = open_page()
    first, second, third = page.questions
    return dataclasses.replace(page, questions=(
        {**first, "images": [{"number": 1, "src": "/content/a.png", "alt": "graph"},
                             {"number": 2, "src": None, "alt": ""}]},
        {**second, "images": [{"number": 1, "src": "/content/b.png", "alt": ""}]},
        {**third, "images": []},
    ))


def test_images_are_saved_privately_with_failures_reported_per_image(workflow, tmp_path):

    def fetch(client, src):
        if src is None:
            raise PreviewRefusedError(REFUSE_IMAGE_SOURCE)
        if src.endswith("b.png"):
            raise NetworkError("cookie=SECRET_SENTINEL")
        return PNG, "image/png"
    target = tmp_path / "shots"
    with patch(f"{SESSION}.read_learner_page", return_value=image_page()), \
            patch(f"{SESSION}.read_quiz_image", side_effect=fetch):
        result = workflow.images(directory=target)
    path = target / "question-1-image-1.png"
    assert result["directory"] == str(target) and result["page"] == 1
    assert result["images"] == [
        {"question_id": 101, "image": 1, "alt": "graph", "path": str(path), "media_type": "image/png"},
        {"question_id": 101, "image": 2, "alt": "", "error": REFUSE_IMAGE_SOURCE},
        {"question_id": 102, "image": 1, "alt": "", "error": "The image could not be downloaded."},
    ]
    assert path.read_bytes() == PNG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_one_questions_images_go_to_a_new_private_directory(workflow):
    with patch(f"{SESSION}.read_learner_page", return_value=image_page()), \
            patch(f"{SESSION}.read_quiz_image", return_value=(PNG, "image/png")) as fetch:
        result = workflow.images(question_id=102)
    fetch.assert_called_once()
    directory = result["directory"]
    try:
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
        assert [image["path"] for image in result["images"]] == [os.path.join(directory, "question-2-image-1.png")]
    finally:
        for name in os.listdir(directory):
            os.unlink(os.path.join(directory, name))
        os.rmdir(directory)


def test_images_of_a_question_elsewhere_or_an_expired_session_stop(workflow, tmp_path):
    with patch(f"{SESSION}.read_learner_page", return_value=image_page()), \
            patch(f"{SESSION}.read_quiz_image") as fetch:
        with pytest.raises(PreviewRefusedError, match="not on the current page"):
            workflow.images(question_id=999, directory=tmp_path)
        fetch.side_effect = SessionExpiredError("expired")
        with pytest.raises(SessionExpiredError):
            workflow.images(directory=tmp_path)


def test_a_symlinked_image_file_is_never_followed(workflow, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    (tmp_path / "question-1-image-1.png").symlink_to(outside)
    with patch(f"{SESSION}.read_learner_page", return_value=image_page()), \
            patch(f"{SESSION}.read_quiz_image", return_value=(PNG, "image/png")):
        result = workflow.images(directory=tmp_path)
    assert outside.read_bytes() == b"keep"
    # Reported for that image alone; the others are still saved.
    assert result["images"][0] == {"question_id": 101, "image": 1, "alt": "graph",
                                   "error": "The image could not be saved."}
    assert [image["path"] for image in result["images"][1:]] == [
        str(tmp_path / "question-1-image-2.png"), str(tmp_path / "question-2-image-1.png")]


def test_an_image_name_taken_by_a_fifo_fails_without_blocking(workflow, tmp_path):
    os.mkfifo(tmp_path / "question-2-image-1.png")
    with patch(f"{SESSION}.read_learner_page", return_value=image_page()), \
            patch(f"{SESSION}.read_quiz_image", return_value=(PNG, "image/png")):
        result = workflow.images(question_id=102, directory=tmp_path)
    assert result["images"] == [{"question_id": 102, "image": 1, "alt": "", "error": "The image could not be saved."}]


def test_a_replaced_image_file_is_made_private(workflow, tmp_path):
    path = tmp_path / "question-2-image-1.png"
    path.write_bytes(b"old")
    path.chmod(0o644)
    with patch(f"{SESSION}.read_learner_page", return_value=image_page()), \
            patch(f"{SESSION}.read_quiz_image", return_value=(PNG, "image/png")):
        workflow.images(question_id=102, directory=tmp_path)
    assert path.read_bytes() == PNG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# -- answers JSON ---------------------------------------------------------------


def test_answers_json_maps_question_ids_to_answers():
    assert parse_answers('{"101": "o2", "102": ["o11", "o13"], "103": ["four", ""], "104": []}') == {
        101: "o2", 102: ["o11", "o13"], 103: ["four", ""], 104: []}


@pytest.mark.parametrize("text", [
    "", "[]", "{}", '"101"', "{101: 1}", '{"101": 2}', '{"101": null}', '{"101": [["o1"]]}', '{"101": [1]}',
    '{"0": "o1"}', '{"0101": "o1"}', '{"-1": "o1"}', '{"1e3": "o1"}', '{" 101": "o1"}', '{"１": "o1"}',
    '{"1000000000000000000": "o1"}', '{"101": "o1", "101": "o2"}', pytest.param('{"' + "1" * 5000 + '": "o1"}', id="5000-digit-key"),
])
def test_answers_json_is_rejected_unless_well_formed(text):
    with pytest.raises(LearnerWorkflowError, match="JSON object"):
        parse_answers(text)


# -- CLI ------------------------------------------------------------------------


def invoke(*args: str):
    return CliRunner().invoke(cli, ["student", "attempt", *args])


def test_cli_lists_the_attempt_commands():
    result = invoke("--help")
    assert result.exit_code == 0
    for name in ("start", "page", "answer", "next", "previous", "submit", "verify", "forget", "images", "status"):
        assert name in result.stdout


def test_cli_dry_run_and_declined_write_send_nothing():
    with patch("lighthouse_cli.quiz_learner_commands.LearnerWorkflow") as workflow:
        result = invoke("answer", "10", "20", "--answers", '{"101": "o2"}', "--next", "--dry-run", "--json")
        assert result.exit_code == 0
        assert json.loads(result.stdout)["options"] == {"answers": {"101": "o2"}, "advance": True,
                                                       "allow_unanswered": False}
        result = invoke("previous", "10", "20", "--dry-run", "--json")
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"mode": "learner", "operation": "previous", "course_id": 10,
                                             "quiz_id": 20, "dry_run": True, "options": {}}
        for args in (("start", "10", "20"), ("next", "10", "20"), ("previous", "10", "20"), ("submit", "10", "20"),
                     ("forget", "10", "20"), ("answer", "10", "20", "--answers", '{"101": "o2"}')):
            declined = invoke(*args, "--json")
            assert declined.exit_code == 1
            assert json.loads(declined.stdout) == {"cancelled": True}
    workflow.assert_not_called()


def test_cli_rejects_malformed_answers_before_anything_runs():
    with patch("lighthouse_cli.quiz_learner_commands.LearnerWorkflow") as workflow:
        result = invoke("answer", "10", "20", "--answers", '{"x": 1}', "--yes", "--json")
    workflow.assert_not_called()
    assert result.exit_code == 1
    assert "JSON object" in json.loads(result.stdout)["error"]


def test_cli_runs_each_operation_with_its_options():
    with patch("lighthouse_cli.quiz_learner_commands.LearnerWorkflow") as workflow:
        workflow.return_value.answer.return_value = {"page": 2}
        result = invoke("answer", "10", "20", "--answers", '{"101": ["a", "b"]}', "--next", "--yes", "--json")
        assert result.exit_code == 0 and json.loads(result.stdout) == {"page": 2}
        workflow.return_value.answer.assert_called_once_with(answers={101: ["a", "b"]}, advance=True,
                                                             allow_unanswered=False)
        workflow.return_value.images.return_value = {"images": []}
        assert invoke("images", "10", "20", "--question", "101", "--json").exit_code == 0
        workflow.return_value.images.assert_called_once_with(question_id=101, directory=None)
        workflow.return_value.next.return_value = {}
        assert invoke("next", "10", "20", "--allow-unanswered", "--yes").exit_code == 0
        workflow.return_value.next.assert_called_once_with(allow_unanswered=True)
        workflow.return_value.previous.return_value = {"page": 1, "has_next_control": True, "timer": None}
        result = invoke("previous", "10", "20", "--yes", "--json")
        assert result.exit_code == 0 and json.loads(result.stdout) == {"page": 1, "has_next_control": True, "timer": None}
        workflow.return_value.previous.assert_called_once_with()
        assert invoke("previous", "10", "20", "--allow-unanswered", "--yes").exit_code == 2  # nothing to allow
        workflow.return_value.submit.return_value = RECEIPT
        result = invoke("submit", "10", "20", "--allow-unanswered", "--yes", "--json")
        assert result.exit_code == 0 and json.loads(result.stdout) == RECEIPT
        workflow.return_value.submit.assert_called_once_with(allow_unanswered=True)
        workflow.return_value.verify.return_value = RECEIPT
        assert invoke("verify", "10", "20", "--json").exit_code == 0
        workflow.return_value.verify.assert_called_once_with()
        workflow.return_value.forget.return_value = {"forgotten": True}
        assert invoke("forget", "10", "20", "--yes", "--json").exit_code == 0
        workflow.return_value.forget.assert_called_once_with()


def test_cli_errors_are_fixed_or_sanitized():
    with patch("lighthouse_cli.quiz_learner_commands.LearnerWorkflow") as workflow:
        workflow.return_value.page.side_effect = RuntimeError("cookie=SECRET_SENTINEL")
        result = invoke("page", "10", "20", "--json")
        assert result.exit_code == 1 and json.loads(result.stdout)["error"]
        assert "SECRET_SENTINEL" not in result.stdout + result.stderr
        workflow.return_value.page.side_effect = PreviewPageError()
        result = invoke("page", "10", "20", "--json")
        assert "preview" not in result.stdout and "run attempt start" in json.loads(result.stdout)["error"]
        workflow.return_value.next.side_effect = LearnerAdvanceUnknownError()
        result = invoke("next", "10", "20", "--yes", "--json")
        assert json.loads(result.stdout)["error"] == str(LearnerAdvanceUnknownError())
        workflow.return_value.previous.side_effect = PreviewRefusedError(REFUSE_LEARNER_FIRST_PAGE)
        result = invoke("previous", "10", "20", "--yes", "--json")
        assert result.exit_code == 1 and json.loads(result.stdout)["error"] == REFUSE_LEARNER_FIRST_PAGE
        workflow.return_value.verify.side_effect = LearnerSubmitUnknownError()
        assert json.loads(invoke("verify", "10", "20", "--json").stdout)["error"] == str(LearnerSubmitUnknownError())
        workflow.return_value.verify.side_effect = LearnerNotSubmittedError()
        assert json.loads(invoke("verify", "10", "20", "--json").stdout)["error"] == str(LearnerNotSubmittedError())
        unanswered = [{"page": 2, "question_id": 104, "number": 4}]
        workflow.return_value.submit.side_effect = LearnerUnansweredError(unanswered)
        # Without --json the questions are listed on stderr.
        assert "Question 4 (page 2) is unanswered." in invoke("submit", "10", "20", "--yes").stderr
        result = invoke("submit", "10", "20", "--yes", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"mode": "learner", "course_id": 10, "quiz_id": 20,
                                         "error": str(LearnerUnansweredError(unanswered)), "unanswered": unanswered}
