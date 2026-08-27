"""Read-only display commands: grades, announcements, calendar, assignments, quizzes."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .api import LighthouseClient, SessionExpiredError, resolve_course_id
from .assignments import folder_with_attachments
from .display import error as _error, fmt_date as _fmt_date, output_json as _output_json, print_table as _print_table, short as _short


# ---------------------------------------------------------------------------
# Shared helper for "one course or all courses" commands
# ---------------------------------------------------------------------------


def _for_course_or_all(
    course_id: str | None,
    single_fn: Callable[..., int | dict[str, Any]],
    json_output: bool,
    collection_key: str,
) -> int:
    """Run single_fn for one course or all courses.

    In --json mode, collects all results into a single JSON array (fixes
    concatenated-objects bug). In human mode, prints each result inline.

    When iterating all courses, uses ThreadPoolExecutor(max_workers=5) for
    parallel API calls (~5x speedup). Each worker gets its own HTTP client so
    session and cache state stay thread-local.

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
    except Exception as e:
        return _emit_command_error(
            course_id,
            collection_key,
            json_output,
            e,
            all_courses=course_id is None,
        )

    if course_id is not None:
        resolved_id: Any = _course_identifier(course_id)
        try:
            resolved_id = resolve_course_id(client, course_id)
            result = single_fn(client, resolved_id, json_output)
        except Exception as e:
            return _emit_single_error(resolved_id, collection_key, json_output, e)

        if json_output:
            payload, failed = _normalise_json_payload(result, resolved_id, collection_key)
            _output_json(payload)
            return 1 if failed else 0
        return result

    try:
        courses = client.get_courses()
    except Exception as e:
        return _emit_command_error(None, collection_key, json_output, e, all_courses=True)

    rc = 0
    results: list[dict[str, Any]] = []

    def _run_course(course: dict[str, Any]) -> Any:
        org_id = int(course["OrgUnitId"])
        return single_fn(
            LighthouseClient(),
            org_id,
            json_output,
            title=course.get("Name", ""),
        )

    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_run_course, course): course for course in courses}
            for future in as_completed(futures):
                course = futures[future]
                course_id_value = _course_identifier(course.get("OrgUnitId"))
                try:
                    result = future.result()
                except Exception as e:
                    rc = 1
                    if json_output:
                        results.append(_course_error_payload(course_id_value, collection_key, e))
                    _report_course_error(course_id_value, e)
                    continue

                if json_output:
                    payload, failed = _normalise_json_payload(
                        result,
                        course_id_value,
                        collection_key,
                    )
                    results.append(payload)
                    if failed:
                        rc = 1
                elif result:
                    rc = result
    except Exception as e:
        rc = 1
        if json_output:
            results.append(_course_error_payload(None, collection_key, e))
        else:
            return _error(_exception_message(e))

    if json_output:
        results.sort(key=_course_sort_key)
        _output_json(results)
    return rc


def _exception_message(exc: Exception) -> str:
    """Return a useful, always-string message for an exception."""
    return str(exc) or exc.__class__.__name__


def _course_identifier(value: Any) -> Any:
    """Keep numeric course identifiers numeric while tolerating malformed data."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _course_sort_key(payload: dict[str, Any]) -> tuple[int, int | str]:
    """Sort numeric course IDs numerically and malformed IDs deterministically."""
    value = payload.get("course_id")
    try:
        return 0, int(value)
    except (TypeError, ValueError):
        return 1, "" if value is None else str(value)


def _course_error_payload(
    course_id: Any,
    collection_key: str,
    error: Exception | str,
) -> dict[str, Any]:
    """Build the stable JSON shape used for a failed course fetch."""
    message = _exception_message(error) if isinstance(error, Exception) else str(error)
    return {"course_id": course_id, collection_key: [], "error": message}


def _normalise_json_payload(
    result: Any,
    course_id: Any,
    collection_key: str,
) -> tuple[dict[str, Any], bool]:
    """Ensure every JSON result has a course id, collection, and failure flag."""
    if not isinstance(result, dict):
        return (
            _course_error_payload(
                course_id,
                collection_key,
                "Command did not return a JSON payload",
            ),
            True,
        )

    payload = dict(result)
    payload.setdefault("course_id", course_id)
    if payload.get("error"):
        payload[collection_key] = []
        payload["error"] = str(payload["error"])
        return payload, True
    payload.setdefault(collection_key, [])
    return payload, False


def _report_course_error(course_id: Any, error: Exception) -> None:
    """Report a worker failure without writing human text to JSON stdout."""
    message = _exception_message(error)
    if isinstance(error, SessionExpiredError):
        print(f"Error: {message}", file=sys.stderr)
    else:
        print(f"Warning: course {course_id} failed: {message}", file=sys.stderr)


def _emit_single_error(
    course_id: Any,
    collection_key: str,
    json_output: bool,
    error: Exception,
) -> int:
    """Emit one single-course failure result in the selected output mode."""
    if json_output:
        _report_course_error(course_id, error)
        _output_json(_course_error_payload(course_id, collection_key, error))
        return 1
    return _error(_exception_message(error))


def _emit_command_error(
    course_id: Any,
    collection_key: str,
    json_output: bool,
    error: Exception,
    all_courses: bool = False,
) -> int:
    """Emit a command-level failure while preserving the JSON shape."""
    if json_output:
        _report_course_error(course_id, error)
        payload = _course_error_payload(course_id, collection_key, error)
        _output_json([payload] if all_courses else payload)
        return 1
    return _error(_exception_message(error))



def _show_with_error_handling(
    client: LighthouseClient,
    org_id: int,
    fetch_fn: Callable[[int], Any],
    data_key: str,
    json_output: bool,
    render_fn: Callable[[Any, str], Any],
    title: str | None = None,
) -> int | dict[str, Any]:
    """Fetch data with standard error handling, return JSON or render."""
    try:
        data = fetch_fn(org_id)
    except SessionExpiredError as e:
        return _fetch_error_result(org_id, data_key, json_output, e, generic_human_rc=1)
    except Exception as e:
        return _fetch_error_result(org_id, data_key, json_output, e)
    if json_output:
        return {"course_id": org_id, data_key: data}
    if data:
        render_fn(data, title or str(org_id))
    return 0


def _fetch_error_result(
    org_id: int,
    data_key: str,
    json_output: bool,
    error: Exception,
    generic_human_rc: int = 1,
) -> int | dict[str, Any]:
    """Report a fetch error and return either a JSON failure or human exit code."""
    message = _exception_message(error)
    if isinstance(error, SessionExpiredError):
        print(f"Error: {message}", file=sys.stderr)
        if json_output:
            return _course_error_payload(org_id, data_key, message)
        return 1

    print(f"Warning: failed to fetch {data_key}: {message}", file=sys.stderr)
    if json_output:
        return _course_error_payload(org_id, data_key, message)
    return generic_human_rc

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
    except SessionExpiredError as e:
        return _fetch_error_result(org_id, "grades", json_output, e, generic_human_rc=1)
    except Exception as e:
        return _fetch_error_result(org_id, "grades", json_output, e, generic_human_rc=1)

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
        return _fetch_error_result(org_id, "assignments", json_output, e)
    except Exception as e:
        return _fetch_error_result(org_id, "assignments", json_output, e)

    # Process folders into structured format
    assignments = []
    for f in folders:
        try:
            f, attachment_items = folder_with_attachments(client, org_id, f)
        except Exception as e:
            return _fetch_error_result(org_id, "assignments", json_output, e)
        # Extract attachments info
        attachments = [
            {"file_id": att.get("Id"), "file_name": att.get("FileName", ""), "size": att.get("Size", 0), "attachment_type": att.get("Type", "File")}
            for att in attachment_items
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
