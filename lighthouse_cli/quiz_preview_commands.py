"""Explicit, checkpointed instructor quiz-preview commands."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import click

from .course_read_commands import JSON_OPTION, WRITE_OPTIONS, YES_OPTION, emit, fail, id_command
from .display import format_user_error
from .quiz_attempt_page import PreviewPageError, PreviewRefusedError
from .quiz_preview_session import _UNCERTAIN, PreviewWorkflow, PreviewWorkflowError

_ID = click.IntRange(min=1, max=10**18 - 1)


@click.group()
def preview() -> None:
    """Experimental instructor quiz previews, not graded learner attempts.

    Needs an account that can preview the quiz. Supports untimed text/radio
    questions in all-at-once and one-question, no-backtracking layouts. Read
    page output before choosing answer IDs.
    """


def _command(name: str, *identifiers: str) -> Callable[[Any], Any]:
    return id_command(preview, name, "course_id", "quiz_id", *identifiers, id_type=_ID)


def _execute(operation: str, course_id: int, quiz_id: int, json_output: bool,
             yes: bool = False, dry_run: bool = False, **options: Any) -> None:
    if dry_run:
        emit({"mode": "preview", "operation": operation, "course_id": course_id,
              "quiz_id": quiz_id, "dry_run": True, "options": options}, json_output)
        return
    writes = operation not in {"page", "status", "reconcile"}
    if writes and not yes and (not sys.stdin.isatty() or not click.confirm(
        f"Run preview {operation} for course {course_id}, quiz {quiz_id}?", err=True,
    )):
        fail("Operation cancelled. Use --yes for non-interactive preview changes.", json_output, {"cancelled": True})
    try:
        workflow = PreviewWorkflow(course_id, quiz_id)
        if operation == "status":
            result = workflow.status()
        elif operation == "abandon":
            result = workflow.abandon()
        elif operation == "reconcile":
            result = workflow.reconcile(**options)
        else:
            result = workflow.run(operation, **options)
        emit(result, json_output)
    except Exception as exc:
        # These carry only fixed, local messages; anything else is sanitized.
        fixed = (PreviewWorkflowError, PreviewRefusedError, PreviewPageError, *_UNCERTAIN)
        message = str(exc) if isinstance(exc, fixed) else format_user_error(exc)
        fail(message, json_output, {"mode": "preview", "course_id": course_id, "quiz_id": quiz_id, "error": message})


@_command("start")
@click.option("--bypass-availability", is_flag=True, help="Use the instructor preview's availability-bypass option.")
@WRITE_OPTIONS
def start(course_id: int, quiz_id: int, bypass_availability: bool, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Create one preview and seal its cursor. Refuses a second active start."""
    _execute("start", course_id, quiz_id, json_output, yes, dry_run, bypass_availability=bypass_availability)


@_command("page")
@JSON_OPTION
def page(course_id: int, quiz_id: int, json_output: bool) -> None:
    """Read the current page; verify answer saves and completed submissions.

    An uncertain navigation outcome requires browser inspection before
    continuing or abandoning the preview.
    """
    _execute("page", course_id, quiz_id, json_output)


@_command("reconcile")
@click.option("--attempt-id", type=_ID, help="Bind a listed candidate attempt explicitly.")
@click.option("--confirm-no-remote-attempt", is_flag=True,
              help="Record that the browser shows no preview was created (only when none is listed).")
@JSON_OPTION
def reconcile(course_id: int, quiz_id: int, attempt_id: int | None, confirm_no_remote_attempt: bool,
              json_output: bool) -> None:
    """Resolve an uncertain start with read-only checks; never writes remotely.

    Without options, lists candidate attempts. A start already bound to an
    attempt is verified and resumed.
    """
    _execute("reconcile", course_id, quiz_id, json_output, attempt_id=attempt_id,
             confirm_no_remote_attempt=confirm_no_remote_attempt)


@_command("status")
@JSON_OPTION
def status(course_id: int, quiz_id: int, json_output: bool) -> None:
    """Read the local cursor status without contacting Brightspace."""
    _execute("status", course_id, quiz_id, json_output)


@_command("answer", "question_id", "choice_id")
@WRITE_OPTIONS
def answer(course_id: int, quiz_id: int, question_id: int, choice_id: int, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Save a current-page radio choice once and verify persisted readback."""
    _execute("answer", course_id, quiz_id, json_output, yes, dry_run, question_id=question_id, choice_id=choice_id)


@_command("next")
@WRITE_OPTIONS
def next_page(course_id: int, quiz_id: int, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Advance after all current answers are saved. No backward command exists."""
    _execute("next", course_id, quiz_id, json_output, yes, dry_run)


@_command("submit")
@click.option("--retain", is_flag=True, help="Retain this preview in the teacher's Grade Quiz area.")
@WRITE_OPTIONS
def submit(course_id: int, quiz_id: int, retain: bool, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Submit a fully answered preview and verify its completion receipt."""
    _execute("submit", course_id, quiz_id, json_output, yes, dry_run, retain=retain)


@_command("abandon")
@YES_OPTION
@JSON_OPTION
def abandon(course_id: int, quiz_id: int, yes: bool, json_output: bool) -> None:
    """Forget the local active cursor; does not delete the remote attempt.

    Inspect uncertain outcomes in the browser first. Starting another preview
    can invalidate an older unretained preview of the same quiz.
    """
    _execute("abandon", course_id, quiz_id, json_output, yes)
