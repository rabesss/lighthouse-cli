"""Read-only course tools exposed by both learner and instructor navigation."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import click

from .display import JsonOutputCommand

if TYPE_CHECKING:
    from .assessment_api import AssessmentAPI


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

        command = make_command(path, collection)
        command.__doc__ = f"Read {name.replace('-', ' ')} visible to your account."
        command = click.option("--json", "json_output", is_flag=True)(command)
        for identifier in reversed(identifiers):
            command = click.argument(identifier, type=click.IntRange(min=1))(command)
        command = click.argument("course_id", type=click.IntRange(min=1))(command)
        group.command(name, cls=JsonOutputCommand)(command)
