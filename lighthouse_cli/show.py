"""Read-only display commands: grades, announcements, calendar, assignments, quizzes."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .api import LighthouseClient, SessionExpiredError, resolve_course_id
from .display import error as _error, fmt_date as _fmt_date, output_json as _output_json, print_table as _print_table, short as _short


# ---------------------------------------------------------------------------
# Shared helper for "one course or all courses" commands
# ---------------------------------------------------------------------------


def _for_course_or_all(
    course_id: str | None,
    single_fn: Callable[..., int | dict],
    json_output: bool,
    collection_key: str,
) -> int:
    """Run single_fn for one course or all courses.

    In --json mode, collects all results into a single JSON array (fixes
    concatenated-objects bug). In human mode, prints each result inline.

    When iterating all courses, uses ThreadPoolExecutor(max_workers=5) for
    parallel API calls (~5x speedup). ``requests.Session`` is thread-safe.

    Args:
        course_id: Course identifier (name/ID) or None for all courses.
        single_fn: callable(client, org_id, json_output, title=) -> int | dict
            Returns an int exit code when json_output=False,
            or a dict (the JSON payload) when json_output=True.
        json_output: Whether --json was passed.
        collection_key: Key name for the per-course payload (e.g. "grades",
            "announcements", "events", "quizzes").

    Returns:
        Exit code (0 or 1).
    """
    try:
        client = LighthouseClient()
        if course_id:
            result = single_fn(client, resolve_course_id(client, course_id), json_output)
            if json_output and isinstance(result, dict):
                _output_json(result)
                return 0
            return result

        courses = client.get_courses()
        rc = 0

        if json_output:
            # Parallel collection into a single JSON array
            results: list[dict] = []
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {
                    pool.submit(single_fn, client, int(c["OrgUnitId"]), True, title=c.get("Name", "")): c
                    for c in courses
                }
                for future in as_completed(futures):
                    try:
                        if (payload := future.result()) is not None:
                            results.append(payload)
                    except Exception as e:
                        print(f"Warning: skipping course: {e}", file=sys.stderr)
            # Sort by course_id for deterministic output
            results.sort(key=lambda r: r.get("course_id", 0))
            _output_json(results)
            return rc

        # Parallel fetch -- courses complete and print in non-deterministic order
        with ThreadPoolExecutor(max_workers=5) as pool:
            future_to_id: dict[Any, int] = {}
            for c in courses:
                oid = int(c["OrgUnitId"])
                f = pool.submit(single_fn, client, oid, False, title=c.get("Name", ""))
                future_to_id[f] = oid
            for f in as_completed(future_to_id):
                oid = future_to_id[f]
                try:
                    if r := f.result():
                        rc = r
                except Exception as e:
                    print(f"Warning: course {oid} failed: {e}", file=sys.stderr)
        return rc
    except Exception as e:
        return _error(str(e))



def _show_with_error_handling(
    client: LighthouseClient,
    org_id: int,
    fetch_fn: Callable,
    data_key: str,
    json_output: bool,
    render_fn: Callable,
    title: str | None = None,
) -> int | dict:
    """Fetch data with standard error handling, return JSON or render."""
    try:
        data = fetch_fn(org_id)
    except SessionExpiredError as e:
        return _error(str(e))
    except Exception as e:
        print(f"Warning: failed to fetch {data_key}: {e}", file=sys.stderr)
        if json_output:
            return {"course_id": org_id, data_key: []}
        return 0
    if json_output:
        return {"course_id": org_id, data_key: data}
    if data:
        render_fn(data, title or str(org_id))
    return 0

# ---------------------------------------------------------------------------
# Grades, announcements, calendar, quizzes — all use _for_course_or_all
# ---------------------------------------------------------------------------


def cmd_grades(course_id: str | None = None, json_output: bool = False) -> int:
    """Show grades for a course or all courses."""
    return _for_course_or_all(course_id, _show_course_grades, json_output, "grades")


def _show_course_grades(
    client: LighthouseClient,
    org_id: int,
    json_output: bool,
    title: str | None = None,
) -> int | dict:
    """Display grades for a single course.

    Returns int (exit code) when json_output=False, or dict when json_output=True.
    """
    try:
        schema = client.get_grade_schema(org_id)
        values = client.get_my_grades(org_id)
    except Exception as e:
        return _error(str(e))

    # Merge schema + values
    val_map = {str(v.get("GradeObjectIdentifier", v.get("GradeObjectId", ""))): v for v in values}
    merged = []
    for g in schema:
        v = val_map.get(str(g["Id"]), {})
        num = v.get("PointsNumerator")
        den = v.get("PointsDenominator") or g.get("MaxPoints", "–")
        merged.append({
            "name": g.get("Name", ""), "weight": g.get("Weight", ""),
            "grade": f"{num}/{den}" if num is not None and den is not None else f"–/{den}",
            "type": g.get("GradeType", ""),
        })

    if json_output:
        return {"course_id": org_id, "grades": merged}

    _print_table(["Item", "Grade", "Weight", "Type"], [[m["name"], m["grade"], str(m["weight"]), m["type"]] for m in merged], title=f"Grades – {title or str(org_id)}")
    return 0


def cmd_announcements(course_id: str | None = None, json_output: bool = False) -> int:
    """Show announcements for a course or all courses."""
    return _for_course_or_all(course_id, _show_announcements, json_output, "announcements")


def _show_announcements(
    client: LighthouseClient,
    org_id: int,
    json_output: bool,
    title: str | None = None,
) -> int | dict:
    """Display announcements for a single course."""
    def _render(announcements, t):
        print(f"\n📢 {t}")
        for a in announcements:
            print(f"  [{_fmt_date(a.get('CreatedDate'))}] {a.get('Title', '')}")
            if body := a.get("Body", {}).get("Text", ""):
                print(f"    {_short(body.strip(), 80)}")
            for att in a.get("Attachments", []):
                print(f"    📎 {att.get('FileName', '')} ({att.get('Size', 0)/1024:.0f} KB)")
    return _show_with_error_handling(client, org_id, client.get_announcements, "announcements", json_output, _render, title)


def cmd_calendar(course_id: str | None = None, json_output: bool = False) -> int:
    """Show calendar events for a course or all courses."""
    return _for_course_or_all(course_id, _show_calendar, json_output, "events")


def _show_calendar(
    client: LighthouseClient,
    org_id: int,
    json_output: bool,
    title: str | None = None,
) -> int | dict:
    """Display calendar events for a single course."""
    def _render(events, t):
        _print_table(["Date", "Title", "Course"], [
            [_fmt_date(e.get("StartDateTime")), _short(e.get("Title", ""), 40), e.get("OrgUnitName", "")]
            for e in events
        ], title=f"Calendar – {t}")
    return _show_with_error_handling(client, org_id, client.get_calendar, "events", json_output, _render, title)


def cmd_assignments(course_id: str | None = None, json_output: bool = False) -> int:
    """Show dropbox folders (assignments) for a course or all courses."""
    return _for_course_or_all(course_id, _show_course_assignments, json_output, "assignments")


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode HTML entities from text."""
    import html
    # First decode HTML entities (e.g. &amp; -> &, &lt; -> <)
    # Then strip tags
    return re.sub(r'<[^>]+>', '', html.unescape(text)).strip()


def _show_course_assignments(
    client: LighthouseClient,
    org_id: int,
    json_output: bool,
    title: str | None = None,
) -> int | dict:
    """Display dropbox folders (assignments) for a single course.

    Returns int (exit code) when json_output=False, or dict when json_output=True.
    """
    try:
        folders = client.get_dropbox_folders(org_id)
    except SessionExpiredError as e:
        return _error(str(e))
    except Exception as e:
        print(f"Warning: failed to fetch assignments: {e}", file=sys.stderr)
        if json_output:
            return {"course_id": org_id, "assignments": []}
        return 0

    # Process folders into structured format
    assignments = []
    for f in folders:
        # Extract attachments info
        attachments = [
            {"file_id": att.get("Id"), "file_name": att.get("FileName", ""), "size": att.get("Size", 0), "attachment_type": att.get("Type", "File")}
            for att in (f.get("Attachments", []) or [])
        ]



        # Availability info
        availability = f.get("Availability", {}) or {}

        assignments.append({
            "folder_id": f.get("Id") or f.get("FolderId", ""),
            "name": _strip_html(f.get("Name", "")),
            "due_date": f.get("DueDate") or f.get("Due", ""),
            "attachment_count": len(attachments), "attachments": attachments,
            "custom_instructions": (instr := f.get("CustomInstructions", "") or "") or None,
            "custom_instructions_preview": _short(_strip_html(instr), 80) if instr else None,
            "submission_type": f.get("CategoryName", "") or f.get("SubmissionType", ""),
            "availability": {"start": availability.get("StartDate"), "end": availability.get("EndDate")} if (availability.get("StartDate") or availability.get("EndDate")) else None,
        })

    if json_output:
        return {"course_id": org_id, "assignments": assignments}

    print(f"\n📋 {title or str(org_id)}")
    if not assignments:
        print("  No assignments found for this course.")
        return 0

    _print_table(["ID", "Name", "Due Date", "Attachments"], [
        [str(a["folder_id"]), _short(a["name"], 40), _fmt_date(a["due_date"]), str(a["attachment_count"])]
        for a in assignments
    ])

    for a in assignments:
        if a["custom_instructions_preview"]:
            print(f"  → [{a['folder_id']}] Instructions: {a['custom_instructions_preview']}")
        if av := a["availability"]:
            for label, key in [("Opens", "start"), ("Closes", "end")]:
                if av.get(key):
                    print(f"  → [{a['folder_id']}] {label}: {_fmt_date(av[key])}")
    return 0


def cmd_quizzes(course_id: str | None = None, json_output: bool = False) -> int:
    """Show quizzes for a course or all courses."""
    return _for_course_or_all(course_id, _show_course_quizzes, json_output, "quizzes")


def _show_course_quizzes(
    client: LighthouseClient,
    org_id: int,
    json_output: bool,
    title: str | None = None,
) -> int | dict:
    """Display quizzes for a single course."""
    def _render(quizzes, t):
        _print_table(["ID", "Name", "Start", "End"], [
            [str(q.get("QuizId", "")), _short(q.get("Name", ""), 35), _fmt_date(q.get("StartDate")), _fmt_date(q.get("EndDate"))]
            for q in quizzes
        ], title=f"Quizzes – {t}")
    return _show_with_error_handling(client, org_id, client.get_quizzes, "quizzes", json_output, _render, title)
