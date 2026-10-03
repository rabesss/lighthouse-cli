"""Read-only course tools exposed by both learner and instructor navigation.

Also holds the Click plumbing shared by the role-oriented command modules.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NoReturn

import click

from .display import JsonOutputCommand, output_json

if TYPE_CHECKING:
    from .assessment_api import AssessmentAPI

POSITIVE_ID = click.IntRange(min=1)
JSON_OPTION = click.option("--json", "json_output", is_flag=True)
YES_OPTION = click.option("--yes", is_flag=True)
DRY_RUN_OPTION = click.option("--dry-run", is_flag=True)


def params(*decorators: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Combine Click decorators as if stacked in the order given (first is outermost)."""
    def apply(target: Any) -> Any:
        for decorator in reversed(decorators):
            target = decorator(target)
        return target
    return apply


WRITE_OPTIONS = params(YES_OPTION, DRY_RUN_OPTION, JSON_OPTION)


def id_command(
    group: click.Group, name: str, *identifiers: str, id_type: click.IntRange = POSITIVE_ID,
) -> Callable[[Any], Any]:
    """Register a JSON-aware command whose leading arguments are positive integer IDs."""
    return params(
        group.command(name, cls=JsonOutputCommand),
        *(click.argument(identifier, type=id_type) for identifier in identifiers),
    )


def emit(data: Any, json_output: bool) -> None:
    if json_output:
        output_json(data)
    else:
        click.echo(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False))


def fail(message: str, json_output: bool, document: dict[str, Any]) -> NoReturn:
    """Print *message* to stderr, then *document* as JSON if requested, and exit 1."""
    click.echo(message, err=True)
    if json_output:
        output_json(document)
    raise SystemExit(1) from None


# Exact routes from the D2L developer reference. IDs are typed Click arguments,
# not user-provided path fragments. False means a single JSON object.
_READS = (
    ("my-sections", "/d2l/api/lp/1.47/{course_id}/sections/mysections/", (), True),
    ("group-categories", "/d2l/api/lp/1.47/{course_id}/groupcategories/", (), True),
    ("groups", "/d2l/api/lp/1.47/{course_id}/groupcategories/{category_id}/groups/", ("category_id",), True),
    ("surveys", "surveys/", (), True),
    ("survey", "surveys/{survey_id}", ("survey_id",), False),
    ("checklists", "checklists/", (), True),
    ("checklist", "checklists/{checklist_id}", ("checklist_id",), False),
    ("checklist-items", "checklists/{checklist_id}/items/", ("checklist_id",), True),
    ("forums", "discussions/forums/", (), True),
    ("topics", "discussions/forums/{forum_id}/topics/", ("forum_id",), True),
    ("posts", "discussions/forums/{forum_id}/topics/{topic_id}/posts/", ("forum_id", "topic_id"), True),
    ("post", "discussions/forums/{forum_id}/topics/{topic_id}/posts/{post_id}", ("forum_id", "topic_id", "post_id"), False),
)


def register_course_reads(
    group: click.Group,
    run: Callable[[int, bool, Callable[[AssessmentAPI], Any]], None],
) -> None:
    for name, path, identifiers, collection in _READS:
        def make_command(path: str, collection: bool) -> Callable[..., None]:
            def command(course_id: int, json_output: bool, **ids: int) -> None:
                def fetch(api: AssessmentAPI) -> Any:
                    formatted = path.format(course_id=api.course_id, **ids)
                    route = formatted if path.startswith("/d2l/") else f"/{api.course_id}/" + formatted
                    return api.client._paginate_list(route) if collection else api.client.get_json(route)
                run(course_id, json_output, fetch)
            return command

        cmd = make_command(path, collection)
        cmd.__doc__ = f"Read {name.replace('-', ' ')} visible to your account."
        id_command(group, name, "course_id", *identifiers)(JSON_OPTION(cmd))
