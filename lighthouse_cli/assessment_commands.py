"""Role-oriented assessment commands, independent of the legacy command module."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import click

from .course_read_commands import (
    JSON_OPTION,
    WRITE_OPTIONS,
    emit,
    fail,
    id_command,
    register_course_reads,
)
from .display import JsonOutputGroup, format_user_error

if TYPE_CHECKING:
    from .assessment_api import AssessmentAPI


def _run(course_id: int, json_output: bool, action: Callable[[AssessmentAPI], Any]) -> None:
    # Imported here, not at module level, so role --help skips the HTTP stack.
    from .api import LighthouseClient
    from .assessment_api import (
        AssessmentAPI,
        AssessmentWriteUnknownError,
        AssignmentClosedError,
        project,
    )

    client = None
    try:
        client = LighthouseClient()
        data = project(action(AssessmentAPI(client, course_id)))
        emit({"course_id": course_id, "data": data}, json_output)
    except Exception as exc:
        message = (
            "Write outcome unknown. Inspect the assessment before retrying."
            if isinstance(exc, AssessmentWriteUnknownError)
            else str(exc) if isinstance(exc, AssignmentClosedError)
            else format_user_error(exc)
        )
        fail(message, json_output, {"course_id": course_id, "data": None, "error": message})
    finally:
        if client is not None:
            client._session.close()


@click.group()
def instructor() -> None:
    """Inspect and author assessments with your account's course permissions.

    Choosing this group does not grant an instructor role or impersonate
    another user; Lighthouse enforces your role in each course.
    """


@click.group()
def student() -> None:
    """Read learner assessment details and your own submission history."""


class _LazyPreview(JsonOutputGroup):
    def _implementation(self) -> click.Group:
        from .quiz_preview_commands import preview
        return preview

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return self._implementation().get_command(ctx, cmd_name)

    def list_commands(self, ctx: click.Context) -> list[str]:
        return self._implementation().list_commands(ctx)

    def invoke(self, ctx: click.Context) -> Any:
        return self._implementation().invoke(ctx)


instructor.add_command(_LazyPreview(name="preview", help="Experimental checkpointed instructor quiz previews."))


class _LazyAttempt(_LazyPreview):
    def _implementation(self) -> click.Group:
        from .quiz_learner_commands import attempt
        return attempt


student.add_command(_LazyAttempt(name="attempt", help="Take your own graded quiz attempts as a learner."))


def _register_read(group: click.Group, name: str, resource: str, detail: bool) -> None:
    def command(course_id: int, json_output: bool, identifier: int | None = None) -> None:
        _run(course_id, json_output, lambda api: api.read(resource, identifier))

    command.__doc__ = f"Read {'one ' if detail else ''}{resource} {'details' if detail else 'records'} for a course."
    id_command(group, name, "course_id", *(("identifier",) if detail else ()))(JSON_OPTION(command))


for _group in (instructor, student):
    for _resource in ("quiz", "assignment"):
        _register_read(_group, _resource, _resource, True)
        _register_read(_group, "quizzes" if _resource == "quiz" else "assignments", _resource, False)


@id_command(instructor, "quiz-questions", "course_id", "quiz_id")
@JSON_OPTION
def quiz_questions(course_id: int, quiz_id: int, json_output: bool) -> None:
    """Read instructor question definitions, not a learner attempt page."""
    _run(course_id, json_output, lambda api: api.questions(quiz_id))


@id_command(instructor, "quiz-attempts", "course_id", "quiz_id")
@JSON_OPTION
def quiz_attempts(course_id: int, quiz_id: int, json_output: bool) -> None:
    """Read attempt summaries, scores, completion and feedback."""
    _run(course_id, json_output, lambda api: api.attempts(quiz_id))


@id_command(instructor, "submissions", "course_id", "folder_id")
@JSON_OPTION
def submissions(course_id: int, folder_id: int, json_output: bool) -> None:
    """Read course assignment submissions, their status and feedback."""
    _run(course_id, json_output, lambda api: api.submissions(folder_id, mine=False))


@id_command(student, "assignment-history", "course_id", "folder_id")
@JSON_OPTION
def assignment_history(course_id: int, folder_id: int, json_output: bool) -> None:
    """Read your submissions and published feedback for an assignment."""
    _run(course_id, json_output, lambda api: api.submissions(folder_id, mine=True))


@id_command(instructor, "classlist", "course_id")
@JSON_OPTION
def classlist(course_id: int, json_output: bool) -> None:
    """Read class members and roles; excludes email and login identifiers."""
    _run(course_id, json_output, lambda api: api.client.get_json(f"/{api.course_id}/classlist/"))


student.add_command(classlist)
register_course_reads(student, _run)
register_course_reads(instructor, _run)


def _create(course_id: int, resource: str, payload: dict[str, Any], yes: bool, dry_run: bool, json_output: bool) -> None:
    if dry_run:
        from .assessment_api import project
        emit({"course_id": course_id, "dry_run": True,
              "operation": f"create-{resource}", "data": project(payload)}, json_output)
        return
    if not yes and (not sys.stdin.isatty() or not click.confirm(
        f"Create a hidden {resource} in course {course_id}?", err=True,
    )):
        fail("Creation cancelled. Use --yes for non-interactive creation.", json_output, {"cancelled": True})
    _run(course_id, json_output, lambda api: api.write("POST", resource, payload))


def _settings(factory: Callable[[], dict[str, Any]], json_output: bool) -> dict[str, Any]:
    try:
        return factory()
    except ValueError:
        message = "Invalid assessment settings. Check the name, layout and input lengths."
        fail(message, json_output, {"data": None, "error": message})


@id_command(instructor, "quiz-create", "course_id")
@click.option("--name", required=True)
@click.option("--layout", type=click.Choice(["all", "one-way"]), default="all", show_default=True)
@click.option("--attempts", type=click.IntRange(1, 10), default=1, show_default=True)
@WRITE_OPTIONS
def quiz_create(course_id: int, name: str, layout: str, attempts: int, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Create a hidden quiz shell, with no questions or gradebook link.

    Add questions through Brightspace; the public quiz API supports reading
    question definitions but not creating them. one-way means one question
    per page and no backward navigation. all means all questions together.
    """
    from .assessment_api import quiz_payload
    payload = _settings(lambda: quiz_payload(name, layout, attempts), json_output)
    _create(course_id, "quiz", payload, yes, dry_run, json_output)


@id_command(instructor, "assignment-create", "course_id")
@click.option("--name", required=True)
@click.option("--instructions", default="")
@click.option("--submission-type", type=click.Choice(["file", "text"]), default="file", show_default=True)
@WRITE_OPTIONS
def assignment_create(course_id: int, name: str, instructions: str, submission_type: str, yes: bool, dry_run: bool, json_output: bool) -> None:
    """Create a hidden individual assignment, with no gradebook link."""
    from .assessment_api import assignment_payload
    payload = _settings(lambda: assignment_payload(name, instructions, submission_type), json_output)
    _create(course_id, "assignment", payload, yes, dry_run, json_output)
