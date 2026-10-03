"""Learner attempt cursors: one write per step, settled by reading back or starting again."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import stat
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import NetworkError, SessionExpiredError
from lighthouse_cli.cli import cli
from lighthouse_cli.quiz_attempt_page import (
    REFUSE_LEARNER_LAST_PAGE,
    REFUSE_LEARNER_NOT_ON_PAGE,
    REFUSE_LEARNER_UNANSWERED,
    PreviewPageError,
    PreviewRefusedError,
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
)
from tests.test_quiz_attempt_page import LEARNER_BUTTONS
from tests.test_quiz_learner import ANSWERED, NEXT, body, parse, questions

SESSION = "lighthouse_cli.quiz_learner_session"
QUIZ = {"Name": "Week 3", "PreventMovingBackwards": True, "SubmissionTimeLimit": {"IsEnforced": False},
        "AttemptsAllowed": {"IsUnlimited": False, "NumberOfAttemptsAllowed": 2}}
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


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
            patch(f"{SESSION}.read_learner_summary", return_value=Mock(can_continue=False)) as summary:
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
    assert result["quiz"] == {"name": "Week 3", "forward_only": True, "attempts_allowed": 2}
    assert workflow.status() == {"mode": "learner", "status": "active", "course_id": 10, "quiz_id": 20,
                                 "attempt_id": 30, "page": 1, "operation": None}
    assert '"actor_id"' not in workflow.path.read_text()  # the cursor is sealed


def test_start_continues_the_attempt_in_progress(remote):
    workflow = LearnerWorkflow(10, 20)
    result = start(workflow, open_page(2), resumed=True)
    assert result["resumed"] is True and result["page"] == 2
    assert workflow.status()["page"] == 2


@pytest.mark.parametrize(("quiz", "message"), [
    ({**QUIZ, "SubmissionTimeLimit": {"IsEnforced": True}}, "Timed quizzes are not supported yet"),
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
    with patch(f"{SESSION}.start_learner") as started:
        with pytest.raises(LearnerWorkflowError, match=message):
            workflow.start()
    started.assert_not_called()
    summary.assert_not_called()
    assert workflow.status()["status"] == "absent"


def test_unlimited_attempts_read_as_none():
    assert quiz_info({**QUIZ, "AttemptsAllowed": {"IsUnlimited": True, "NumberOfAttemptsAllowed": None}})[
        "attempts_allowed"] is None
    assert quiz_info({**QUIZ, "AttemptsAllowed": {"IsUnlimited": True}, "Name": None}) == {
        "name": None, "forward_only": True, "attempts_allowed": None}


def test_a_refused_start_keeps_the_previous_cursor(remote):
    workflow = LearnerWorkflow(10, 20)
    with patch(f"{SESSION}.start_learner", side_effect=PreviewRefusedError(REFUSE_START_UNAVAILABLE)):
        with pytest.raises(PreviewRefusedError):
            workflow.start()
    assert workflow.status()["status"] == "absent"
    start(workflow, open_page(2), resumed=True)
    with patch(f"{SESSION}.start_learner", side_effect=PreviewRefusedError(REFUSE_START_UNAVAILABLE)):
        with pytest.raises(PreviewRefusedError):
            workflow.start()
    assert workflow.status() == {"mode": "learner", "status": "active", "course_id": 10, "quiz_id": 20,
                                 "attempt_id": 30, "page": 2, "operation": None}


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
    with patch(f"{SESSION}.read_learner_page") as read, patch(f"{SESSION}.save_learner_answers") as save:
        for action in (workflow.page, workflow.next, lambda: workflow.answer({101: "o1"})):
            with pytest.raises(LearnerWorkflowError, match="Run attempt start"):
                action()
    read.assert_not_called()
    save.assert_not_called()
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


@pytest.mark.parametrize("operation", ["start", "answer", "next"])
def test_a_known_attempt_with_an_unverified_change_is_only_continued(remote, operation):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    workflow._save({**saved(workflow), "status": "uncertain", "operation": operation})
    before = saved(workflow)
    with patch(f"{SESSION}.start_learner") as started:
        with pytest.raises(LearnerWorkflowError, match="no new attempt was started"):
            workflow.start()
    started.assert_not_called()
    assert saved(workflow) == before
    with patch(f"{SESSION}.read_learner_summary", return_value=Mock(can_continue=True)), \
            patch(f"{SESSION}.start_learner", return_value=open_page(2)) as started:
        workflow.start()
    assert started.call_args.kwargs["continue_only"] is True
    assert (workflow.status()["status"], workflow.status()["page"]) == ("active", 2)


def test_another_accounts_unverified_change_is_never_replaced(remote):
    client, state, _ = remote
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    state["actor"] = 8
    start(workflow)  # an active cursor of another account is replaced
    assert saved(workflow)["actor_id"] == 8
    workflow._save({**saved(workflow), "status": "uncertain", "operation": "next"})
    before = saved(workflow)
    state["actor"] = 7
    with patch(f"{SESSION}.start_learner") as started:
        with pytest.raises(LearnerWorkflowError, match="different signed-in account"):
            workflow.start()
    started.assert_not_called()
    client.get_quiz_detail.assert_called()
    assert saved(workflow) == before


# -- answers and Next -----------------------------------------------------------


def test_answers_are_saved_from_the_page_just_read_then_next_uses_the_readback(remote):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
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
def test_answer_and_next_is_checked_before_anything_is_saved(remote, current, answers, allow, message):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    with patch(f"{SESSION}.read_learner_page", return_value=current), \
            patch(f"{SESSION}.save_learner_answers") as save, patch(f"{SESSION}.advance_learner") as advance:
        with pytest.raises(PreviewRefusedError, match=message):
            workflow.answer(answers, advance=True, allow_unanswered=allow)
    save.assert_not_called()
    advance.assert_not_called()
    assert workflow.status()["status"] == "active"


def test_a_next_refused_after_the_save_keeps_the_saved_answers(remote):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.save_learner_answers", return_value=open_page(content=ANSWERED)) as save, \
            patch(f"{SESSION}.advance_learner", side_effect=PreviewRefusedError(REFUSE_LEARNER_LAST_PAGE)):
        with pytest.raises(PreviewRefusedError):
            workflow.answer({101: "o2"}, advance=True, allow_unanswered=True)
    save.assert_called_once()
    assert workflow.status() == {"mode": "learner", "status": "active", "course_id": 10, "quiz_id": 20,
                                 "attempt_id": 30, "page": 1, "operation": None}


def test_unanswered_questions_can_be_left_explicitly(remote):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.save_learner_answers", return_value=open_page()), \
            patch(f"{SESSION}.advance_learner", return_value=open_page(2)) as advance:
        workflow.answer({101: "o2"}, advance=True, allow_unanswered=True)
    assert advance.call_args.kwargs["allow_unanswered"] is True


def test_an_unverified_save_is_settled_by_reading_the_page(remote):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.save_learner_answers", side_effect=LearnerSaveUnknownError()):
        with pytest.raises(LearnerSaveUnknownError):
            workflow.answer({101: "o2"})
    assert (workflow.status()["status"], workflow.status()["operation"]) == ("uncertain", "answer")
    with patch(f"{SESSION}.save_learner_answers") as save, patch(f"{SESSION}.advance_learner") as advance:
        with pytest.raises(LearnerWorkflowError, match="Run attempt page"):
            workflow.answer({101: "o2"})
        with pytest.raises(LearnerWorkflowError, match="Run attempt page"):
            workflow.next()
    save.assert_not_called()
    advance.assert_not_called()
    with patch(f"{SESSION}.read_learner_page", return_value=open_page(content=ANSWERED)) as read:
        result = workflow.page()
    assert read.call_args.kwargs == {"course_id": 10, "quiz_id": 20, "attempt_id": 30, "page": 1}
    assert result["questions"][0]["saved"] is True
    assert workflow.status()["status"] == "active"


def test_an_unknown_next_needs_starting_again(remote):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    with patch(f"{SESSION}.read_learner_page", return_value=open_page(content=ANSWERED + NEXT)), \
            patch(f"{SESSION}.advance_learner", side_effect=LearnerAdvanceUnknownError()):
        with pytest.raises(LearnerAdvanceUnknownError):
            workflow.next()
    assert (workflow.status()["status"], workflow.status()["operation"]) == ("uncertain", "next")
    with patch(f"{SESSION}.read_learner_page") as read:
        with pytest.raises(LearnerWorkflowError, match="Run attempt start"):
            workflow.page()
    read.assert_not_called()
    start(workflow, open_page(2), resumed=True)
    assert workflow.status() == {"mode": "learner", "status": "active", "course_id": 10, "quiz_id": 20,
                                 "attempt_id": 30, "page": 2, "operation": None}


@pytest.mark.parametrize("failure", [PreviewRefusedError(REFUSE_LEARNER_NOT_ON_PAGE), NetworkError("no")])
def test_a_write_that_failed_before_sending_keeps_the_cursor(remote, failure):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    before = saved(workflow)
    with patch(f"{SESSION}.read_learner_page", return_value=open_page()), \
            patch(f"{SESSION}.save_learner_answers", side_effect=failure):
        with pytest.raises(type(failure)):
            workflow.answer({101: "o2"})
    assert saved(workflow) == before


def test_another_account_cannot_use_the_cursor(remote):
    _, state, _ = remote
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    state["actor"] = 8
    with patch(f"{SESSION}.read_learner_page") as read:
        with pytest.raises(LearnerWorkflowError, match="different signed-in account"):
            workflow.page()
        # Also when the cursor would otherwise ask for a restart.
        workflow._save({**saved(workflow), "status": "uncertain", "operation": "next"})
        with pytest.raises(LearnerWorkflowError, match="different signed-in account"):
            workflow.page()
    read.assert_not_called()


def test_nothing_runs_without_a_started_attempt(remote):
    workflow = LearnerWorkflow(10, 20)
    with patch(f"{SESSION}.read_learner_page") as read:
        with pytest.raises(LearnerWorkflowError, match="Run attempt start"):
            workflow.page()
    read.assert_not_called()
    assert workflow.status() == {"mode": "learner", "status": "absent", "course_id": 10, "quiz_id": 20}


@pytest.mark.parametrize("change", [
    {"mode": "preview"}, {"actor_id": 0}, {"page": 0}, {"attempt_id": None}, {"status": "uncertain"},
    {"operation": "answer"}, {"quiz_id": 21}, {"origin": "https://other.example"},
])
def test_a_tampered_cursor_is_rejected(remote, change):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    workflow._save({**saved(workflow), **change})
    with pytest.raises(LearnerWorkflowError, match="invalid"):
        workflow.status()


def test_start_replaces_an_invalid_cursor(remote):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    workflow._save({**saved(workflow), "page": 0})
    start(workflow, open_page(2), resumed=True)
    assert workflow.status()["page"] == 2


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


def test_images_are_saved_privately_with_failures_reported_per_image(remote, tmp_path):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)

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


def test_one_questions_images_go_to_a_new_private_directory(remote):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
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


def test_images_of_a_question_elsewhere_or_an_expired_session_stop(remote, tmp_path):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    with patch(f"{SESSION}.read_learner_page", return_value=image_page()), \
            patch(f"{SESSION}.read_quiz_image") as fetch:
        with pytest.raises(PreviewRefusedError, match="not on the current page"):
            workflow.images(question_id=999, directory=tmp_path)
        fetch.side_effect = SessionExpiredError("expired")
        with pytest.raises(SessionExpiredError):
            workflow.images(directory=tmp_path)


def test_a_symlinked_image_file_is_never_followed(remote, tmp_path):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
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


def test_an_image_name_taken_by_a_fifo_fails_without_blocking(remote, tmp_path):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
    os.mkfifo(tmp_path / "question-2-image-1.png")
    with patch(f"{SESSION}.read_learner_page", return_value=image_page()), \
            patch(f"{SESSION}.read_quiz_image", return_value=(PNG, "image/png")):
        result = workflow.images(question_id=102, directory=tmp_path)
    assert result["images"] == [{"question_id": 102, "image": 1, "alt": "", "error": "The image could not be saved."}]


def test_a_replaced_image_file_is_made_private(remote, tmp_path):
    workflow = LearnerWorkflow(10, 20)
    start(workflow)
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
    for name in ("start", "page", "answer", "next", "images", "status"):
        assert name in result.stdout


def test_cli_dry_run_and_declined_write_send_nothing():
    with patch("lighthouse_cli.quiz_learner_commands.LearnerWorkflow") as workflow:
        result = invoke("answer", "10", "20", "--answers", '{"101": "o2"}', "--next", "--dry-run", "--json")
        assert result.exit_code == 0
        assert json.loads(result.stdout)["options"] == {"answers": {"101": "o2"}, "advance": True,
                                                       "allow_unanswered": False}
        for args in (("start", "10", "20"), ("next", "10", "20"),
                     ("answer", "10", "20", "--answers", '{"101": "o2"}')):
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
