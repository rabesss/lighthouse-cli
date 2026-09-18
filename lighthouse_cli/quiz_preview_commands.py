"""Explicit, checkpointed instructor-preview commands for the trial tenant."""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from .display import JsonOutputCommand, format_user_error, output_json
from .quiz_preview_session import PreviewWorkflow, PreviewWorkflowError, _UNCERTAIN


_ID = click.IntRange(min=1, max=10**18 - 1)


@click.group()
@click.pass_context
def preview(ctx: click.Context) -> None:
    """Experimental trial-only quiz previews, not real learner attempts.

    Supports untimed text/radio questions in all-at-once and one-question,
    no-backtracking layouts. Read page output before choosing answer IDs.
    """
    ctx.obj = {"preview_site": ctx.parent.params.get("site", "lighthouse") if ctx.parent else "lighthouse"}


def _emit(value: dict[str, Any], structured: bool) -> None:
    if structured:
        output_json(value)
    else:
        click.echo(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def _execute(operation: str, course_id: int, quiz_id: int, json_output: bool,
             yes: bool = False, dry_run: bool = False, **options: Any) -> None:
    site = click.get_current_context().obj["preview_site"]
    if dry_run:
        _emit({"site": site, "mode": "preview", "operation": operation, "course_id": course_id,
               "quiz_id": quiz_id, "dry_run": True, "options": options}, json_output)
        return
    writes = operation not in {"page", "status"}
    if writes and not yes and (not sys.stdin.isatty() or not click.confirm(
        f"Run preview {operation} on {site}, course {course_id}, quiz {quiz_id}?", err=True,
    )):
        click.echo("Operation cancelled. Use --yes for non-interactive preview changes.", err=True)
        if json_output:
            output_json({"cancelled": True})
        raise SystemExit(1)
    try:
        workflow = PreviewWorkflow(site, course_id, quiz_id)
        if operation == "status":
            result = workflow.status()
        elif operation == "abandon":
            result = workflow.abandon()
        else:
            result = workflow.run(operation, **options)
        _emit({"site": site, **result}, json_output)
    except Exception as exc:
        message = str(exc) if isinstance(exc, (PreviewWorkflowError, *_UNCERTAIN)) else format_user_error(exc)
        click.echo(message, err=True)
        if json_output:
            output_json({"site": site, "mode": "preview", "course_id": course_id, "quiz_id": quiz_id, "error": message})
        raise SystemExit(1) from None


@preview.command("start", cls=JsonOutputCommand)
@click.argument("course_id", type=_ID)
@click.argument("quiz_id", type=_ID)
@click.option("--bypass-availability", is_flag=True, help="Use the instructor preview's availability-bypass option.")
@click.option("--yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def start(course_id: int, quiz_id: int, bypass_availability: bool, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Create one preview and seal its cursor. Refuses a second active start."""
    _execute("start", course_id, quiz_id, json_output, yes, dry_run, bypass_availability=bypass_availability)


@preview.command("page", cls=JsonOutputCommand)
@click.argument("course_id", type=_ID)
@click.argument("quiz_id", type=_ID)
@click.option("--json", "json_output", is_flag=True)
def page(course_id: int, quiz_id: int, json_output: bool) -> None:
    """Read the current page; verify answer saves and completed submissions.

    An uncertain navigation outcome requires browser inspection before
    continuing or abandoning the preview.
    """
    _execute("page", course_id, quiz_id, json_output)


@preview.command("status", cls=JsonOutputCommand)
@click.argument("course_id", type=_ID)
@click.argument("quiz_id", type=_ID)
@click.option("--json", "json_output", is_flag=True)
def status(course_id: int, quiz_id: int, json_output: bool) -> None:
    """Read the local cursor status without contacting Brightspace."""
    _execute("status", course_id, quiz_id, json_output)


@preview.command("answer", cls=JsonOutputCommand)
@click.argument("course_id", type=_ID)
@click.argument("quiz_id", type=_ID)
@click.argument("question_id", type=_ID)
@click.argument("choice_id", type=_ID)
@click.option("--yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def answer(course_id: int, quiz_id: int, question_id: int, choice_id: int, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Save a current-page radio choice once and verify persisted readback."""
    _execute("answer", course_id, quiz_id, json_output, yes, dry_run, question_id=question_id, choice_id=choice_id)


@preview.command("next", cls=JsonOutputCommand)
@click.argument("course_id", type=_ID)
@click.argument("quiz_id", type=_ID)
@click.option("--yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def next_page(course_id: int, quiz_id: int, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Advance after all current answers are saved. No backward command exists."""
    _execute("next", course_id, quiz_id, json_output, yes, dry_run)


@preview.command("submit", cls=JsonOutputCommand)
@click.argument("course_id", type=_ID)
@click.argument("quiz_id", type=_ID)
@click.option("--retain", is_flag=True, help="Retain this preview in the teacher's Grade Quiz area.")
@click.option("--yes", is_flag=True)
@click.option("--dry-run", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def submit(course_id: int, quiz_id: int, retain: bool, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Submit a fully answered preview and verify its completion receipt."""
    _execute("submit", course_id, quiz_id, json_output, yes, dry_run, retain=retain)


@preview.command("abandon", cls=JsonOutputCommand)
@click.argument("course_id", type=_ID)
@click.argument("quiz_id", type=_ID)
@click.option("--yes", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
def abandon(course_id: int, quiz_id: int, yes: bool, json_output: bool) -> None:
    """Forget the local active cursor; does not delete the remote attempt.

    Inspect uncertain outcomes in the browser first. Starting another preview
    can invalidate an older unretained preview of the same quiz.
    """
    _execute("abandon", course_id, quiz_id, json_output, yes)
