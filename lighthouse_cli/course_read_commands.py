"""Read-only course tools exposed by both learner and instructor navigation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click

from .assessment_api import AssessmentAPI
from .display import JsonOutputCommand

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
        cmd = click.option("--json", "json_output", is_flag=True)(cmd)
        for identifier in reversed(identifiers):
            cmd = click.argument(identifier, type=click.IntRange(min=1))(cmd)
        cmd = click.argument("course_id", type=click.IntRange(min=1))(cmd)
        group.command(name, cls=JsonOutputCommand)(cmd)
