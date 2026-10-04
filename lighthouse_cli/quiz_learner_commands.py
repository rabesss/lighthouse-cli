"""Commands for taking your own quiz attempts as a learner."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from .course_read_commands import JSON_OPTION, WRITE_OPTIONS, YES_OPTION, emit, fail, id_command
from .display import format_user_error
from .quiz_attempt_page import PreviewPageError, PreviewRefusedError
from .quiz_learner_finish import LearnerNotSubmittedError, LearnerUnansweredError
from .quiz_learner_session import UNCERTAIN, LearnerWorkflow, LearnerWorkflowError, parse_answers

_ID = click.IntRange(min=1, max=10**18 - 1)
_CONFIRMED = {"start", "answer", "next", "previous", "submit", "forget"}
_NOT_CURRENT = ("Brightspace did not return a supported current page of this attempt. If the attempt "
                "changed elsewhere, run attempt start to continue it where Brightspace has it.")


@click.group()
def attempt() -> None:
    """Take your own quiz attempts as a learner. Attempts are graded, unlike previews.

    Start (or continue) an attempt, read its page, answer the questions on
    it, move on with Next (or back with Previous, where the quiz allows it),
    and submit from the last page. Every change is read back from Brightspace
    before it is reported. On a timed quiz every page reports the time left;
    submit before it runs out.
    """


def _command(name: str, *identifiers: str) -> Callable[[Any], Any]:
    return id_command(attempt, name, "course_id", "quiz_id", *identifiers, id_type=_ID)


def _confirmed(operation: str, course_id: int, quiz_id: int) -> bool:
    if not sys.stdin.isatty():
        return False
    if operation == "start":
        prompt = (f"Start or continue your attempt at quiz {quiz_id} in course {course_id}? "
                  "A new attempt uses one of your allowed attempts and is graded. "
                  "On a timed quiz its clock starts and keeps running.")
    elif operation == "submit":
        prompt = f"Submit your attempt at quiz {quiz_id} in course {course_id}? A submitted attempt is final and graded."
    elif operation == "forget":
        prompt = (f"Forget the CLI's record of your attempt at quiz {quiz_id} in course {course_id}? "
                  "Brightspace keeps the attempt, and the next start may begin a new graded attempt.")
    else:
        prompt = f"Run attempt {operation} for course {course_id}, quiz {quiz_id}?"
    return click.confirm(prompt, err=True)


def _execute(operation: str, course_id: int, quiz_id: int, json_output: bool,
             yes: bool = False, dry_run: bool = False, **options: Any) -> None:
    try:
        if "answers" in options:
            options["answers"] = parse_answers(options["answers"])
        if dry_run:
            emit({"mode": "learner", "operation": operation, "course_id": course_id,
                  "quiz_id": quiz_id, "dry_run": True, "options": options}, json_output)
            return
        if operation in _CONFIRMED and not yes and not _confirmed(operation, course_id, quiz_id):
            fail("Operation cancelled. Use --yes for non-interactive quiz attempt changes.", json_output,
                 {"cancelled": True})
        workflow = LearnerWorkflow(course_id, quiz_id)
        emit(getattr(workflow, operation)(**options), json_output)
    except Exception as exc:
        # These carry only fixed, local messages; anything else is sanitized.
        fixed = (LearnerWorkflowError, LearnerUnansweredError, LearnerNotSubmittedError, PreviewRefusedError, *UNCERTAIN)
        if isinstance(exc, PreviewPageError):
            message = _NOT_CURRENT
        else:
            message = str(exc) if isinstance(exc, fixed) else format_user_error(exc)
        unanswered = exc.questions if isinstance(exc, LearnerUnansweredError) else None
        listed = "".join(f" Question {q['number']} (page {q['page']}) is unanswered." for q in unanswered or ())
        error: dict[str, Any] = {"mode": "learner", "course_id": course_id, "quiz_id": quiz_id, "error": message}
        if unanswered is not None:
            error["unanswered"] = unanswered
        fail(message + listed, json_output, error)


@_command("start")
@WRITE_OPTIONS
def start(course_id: int, quiz_id: int, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Continue the attempt in progress, or start a new one, and read its page.

    Once an attempt is open, until it is submitted only that attempt is
    continued, where Brightspace has it; no new attempt is started. A timed
    attempt's limit is read here, once; later commands count down from it.
    """
    _execute("start", course_id, quiz_id, json_output, yes, dry_run)


@_command("page")
@JSON_OPTION
def page(course_id: int, quiz_id: int, json_output: bool) -> None:
    """Read the current page: its questions, options, blanks and saved answers."""
    _execute("page", course_id, quiz_id, json_output)


@_command("answer")
@click.option("--answers", "answers", required=True, metavar="JSON",
              help='Question id to answer: a choice id, a list of option ids, or a list of blank texts, '
                   'e.g. \'{"101": "o2", "102": ["o5", "o7"], "103": ["4"]}\'.')
@click.option("--next", "advance", is_flag=True, help="Then move to the next page.")
@click.option("--allow-unanswered", is_flag=True, help="With --next, leave this page's empty answers unanswered.")
@WRITE_OPTIONS
def answer(course_id: int, quiz_id: int, answers: str, advance: bool, allow_unanswered: bool,
           yes: bool, dry_run: bool, json_output: bool) -> None:
    """Save answers on the current page in one request, keeping the others.

    With --next, everything Next needs is checked before anything is saved.
    """
    _execute("answer", course_id, quiz_id, json_output, yes, dry_run, answers=answers, advance=advance,
             allow_unanswered=allow_unanswered)


@_command("next")
@click.option("--allow-unanswered", is_flag=True, help="Leave this page's empty answers unanswered.")
@WRITE_OPTIONS
def next_page(course_id: int, quiz_id: int, allow_unanswered: bool, yes: bool, dry_run: bool,
              json_output: bool) -> None:
    """Move to the next page. On a forward-only quiz there is no way back."""
    _execute("next", course_id, quiz_id, json_output, yes, dry_run, allow_unanswered=allow_unanswered)


@_command("previous")
@WRITE_OPTIONS
def previous_page(course_id: int, quiz_id: int, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Move back to the previous page. The first page and forward-only quizzes have none.

    This page's answers are sent as they stand; empty ones can be filled in
    on a later visit.
    """
    _execute("previous", course_id, quiz_id, json_output, yes, dry_run)


@_command("submit")
@click.option("--allow-unanswered", is_flag=True, help="Submit even with unanswered questions in the quiz.")
@WRITE_OPTIONS
def submit(course_id: int, quiz_id: int, allow_unanswered: bool, yes: bool, dry_run: bool,
           json_output: bool) -> None:
    """Submit the attempt from its last page, once, and verify the receipt.

    Unless --allow-unanswered, nothing is submitted while any question of the
    quiz is unanswered: the page's answers are saved and the error lists the
    unanswered questions. A submitted attempt is final.
    """
    _execute("submit", course_id, quiz_id, json_output, yes, dry_run, allow_unanswered=allow_unanswered)


@_command("verify")
@JSON_OPTION
def verify(course_id: int, quiz_id: int, json_output: bool) -> None:
    """Check that the attempt was submitted, from its receipt and the submissions list.

    Settles a submission that could not be verified, or reports that the
    attempt is still in progress, or, once its time ran out, still waiting
    for Brightspace to submit it. Changes nothing on Brightspace.
    """
    _execute("verify", course_id, quiz_id, json_output)


@_command("forget")
@YES_OPTION
@JSON_OPTION
def forget(course_id: int, quiz_id: int, yes: bool, json_output: bool) -> None:
    """Drop the CLI's record of this quiz's attempt; Brightspace keeps the attempt.

    For an attempt that ended outside the CLI and cannot be verified. The
    next start may then begin a new graded attempt.
    """
    _execute("forget", course_id, quiz_id, json_output, yes)


@_command("images")
@click.option("--question", "question_id", type=_ID, help="Only this question's images.")
@click.option("--dir", "directory", type=click.Path(file_okay=False, path_type=Path),
              help="Save into this directory instead of a new temporary one.")
@JSON_OPTION
def images(course_id: int, quiz_id: int, question_id: int | None, directory: Path | None, json_output: bool) -> None:
    """Download the current page's question images to files an agent can view."""
    _execute("images", course_id, quiz_id, json_output, question_id=question_id, directory=directory)


@_command("status")
@JSON_OPTION
def status(course_id: int, quiz_id: int, json_output: bool) -> None:
    """Read the local attempt cursor without contacting Brightspace."""
    _execute("status", course_id, quiz_id, json_output)
