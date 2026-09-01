"""Read-only display commands: grades, announcements, calendar, assignments, quizzes."""

from __future__ import annotations

import html
import math
import re
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, local
from typing import Any

from .api import LighthouseClient, SessionExpiredError, resolve_course_id
from .assignments import (
    _AssignmentDataError,
    folder_with_attachments,
    safe_assignment_folder_name,
    safe_attachment_filename,
)
from .display import error as _error, fmt_date as _fmt_date, format_user_error, output_json as _output_json, print_table as _print_table, safe_display_text, short as _short
from .utils import _course_identifier, get_enrolled_course_catalog


# ---------------------------------------------------------------------------
# Shared helper for "one course or all courses" commands
# ---------------------------------------------------------------------------

# Keep all-course reads useful for normal rosters while preventing an
# accidentally broad request from fanning out without a bound.  This is a
# command-level safety limit: the enrollment catalog is fetched once so the
# command can count and validate the scope before starting any per-course API
# work.  A caller can still request an individual course by supplying
# ``COURSE_ID``.
MAX_ALL_COURSES = 100
MAX_WORKERS = 5
_MAX_DISPLAY_TEXT_LENGTH = 512
_MAX_RICH_TEXT_LENGTH = 4096
_MAX_ANNOUNCEMENT_ATTACHMENT_SIZE = (1 << 63) - 1

def _close_client(client: Any) -> None:
    """Close a client when its implementation exposes an explicit close hook.

    ``LighthouseClient`` historically relied on the session being reclaimed
    with the client, while test doubles and newer client implementations may
    expose ``close``.  Supporting both keeps this helper compatible and makes
    worker-session cleanup best effort without allowing cleanup failures to
    mask the command result.
    """
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        # Closing a requests session is not part of the command's result.  A
        # failed cleanup must never replace a useful API/error result.
        return


def _all_course_limit_message(course_count: int) -> str:
    """Return the fixed, safe diagnostic for an over-budget all-course read."""
    return (
        f"All-course reads are limited to {MAX_ALL_COURSES} courses; "
        f"found {course_count}. Specify COURSE_ID to narrow the request."
    )


def _emit_all_course_limit_error(
    collection_key: str,
    json_output: bool,
    course_count: int,
) -> int:
    """Emit one deterministic, secret-safe all-course scope error."""
    message = _all_course_limit_message(course_count)
    print(f"Error: {message}", file=sys.stderr)
    if json_output:
        _output_json([{
            "course_id": None,
            collection_key: [],
            "error": message,
        }])
    return 1


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
    parallel API calls (~5x speedup). Each worker gets one HTTP client that is
    reused for its sequential tasks, so sessions and cache state stay
    thread-local without constructing one client per course. All-course
    scopes larger than ``MAX_ALL_COURSES`` fail before any per-course request.

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
        finally:
            _close_client(client)

        if json_output:
            payload, failed = _normalise_json_payload(result, resolved_id, collection_key)
            _output_json(payload)
            return 1 if failed else 0
        return result

    try:
        courses = get_enrolled_course_catalog(client)
    except Exception as e:
        return _emit_command_error(None, collection_key, json_output, e, all_courses=True)
    finally:
        _close_client(client)

    if len(courses) > MAX_ALL_COURSES:
        return _emit_all_course_limit_error(collection_key, json_output, len(courses))

    rc = 0
    results: list[dict[str, Any]] = []
    worker_local = local()
    worker_clients: list[Any] = []
    worker_clients_lock = Lock()

    def _worker_client() -> Any:
        """Get the one client assigned to the current executor worker."""
        if hasattr(worker_local, "client_error"):
            raise worker_local.client_error
        worker_client = getattr(worker_local, "client", None)
        if worker_client is None:
            try:
                worker_client = LighthouseClient()
            except Exception as e:
                # Avoid retrying construction for every queued course if a
                # worker cannot initialize its session.  The same failure is
                # surfaced for the remaining tasks assigned to that thread.
                worker_local.client_error = e
                raise
            worker_local.client = worker_client
            with worker_clients_lock:
                worker_clients.append(worker_client)
        return worker_client

    def _run_course(course: dict[str, Any]) -> Any:
        org_id = int(course["OrgUnitId"])
        return single_fn(
            _worker_client(),
            org_id,
            json_output,
            title=_safe_course_label(course.get("Name"), org_id),
        )

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
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
            return _error(e)
    finally:
        for worker_client in worker_clients:
            _close_client(worker_client)

    if json_output:
        results.sort(key=_course_sort_key)
        _output_json(results)
    return rc


def _exception_message(exc: Exception) -> str:
    """Return a concise, secret-safe message for an exception."""
    return format_user_error(exc)


def _safe_course_label(value: Any, course_id: Any) -> str:
    """Return a bounded printable course label or an opaque fixed fallback."""
    identifier = _course_identifier(course_id)
    fallback = f"Course-{identifier}" if identifier is not None else "Course"
    return safe_display_text(value, fallback, max_len=_MAX_DISPLAY_TEXT_LENGTH)


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
    message = _exception_message(error) if isinstance(error, Exception) else format_user_error(str(error))
    return {"course_id": _course_identifier(course_id), collection_key: [], "error": message}


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
    payload["course_id"] = _course_identifier(payload.get("course_id", course_id))
    for label_key in ("course_name", "title"):
        if label_key in payload:
            payload[label_key] = _safe_course_label(
                payload.get(label_key),
                payload["course_id"],
            )
    if payload.get("error"):
        payload[collection_key] = []
        payload["error"] = format_user_error(str(payload["error"]))
        return payload, True
    payload.setdefault(collection_key, [])
    return payload, False


def _report_course_error(course_id: Any, error: Exception) -> None:
    """Report a worker failure without writing human text to JSON stdout."""
    message = _exception_message(error)
    if isinstance(error, SessionExpiredError):
        print(f"Error: {message}", file=sys.stderr)
    else:
        print(
            f"Warning: course {_course_identifier(course_id)} failed: {message}",
            file=sys.stderr,
        )


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
    return _error(error)


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
    return _error(error)



def _show_with_error_handling(
    org_id: int,
    fetch_fn: Callable[[int], Any],
    data_key: str,
    json_output: bool,
    render_fn: Callable[[Any, str], Any],
    title: str | None = None,
    empty_message: str | None = None,
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
    elif empty_message and title is None:
        print(empty_message)
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
            return _course_error_payload(org_id, data_key, error)
        return 1

    print(f"Warning: failed to fetch {data_key}: {message}", file=sys.stderr)
    if json_output:
        return _course_error_payload(org_id, data_key, error)
    return generic_human_rc


def _safe_announcement_text(value: Any) -> str:
    """Keep bounded printable API text, never arbitrary object reprs."""
    return safe_display_text(value, max_len=_MAX_DISPLAY_TEXT_LENGTH)


def _safe_rich_text(value: Any) -> str:
    """Keep bounded printable RichText content without secret-shaped text."""
    return safe_display_text(value, max_len=_MAX_RICH_TEXT_LENGTH)


def _normalise_announcement_attachment(value: Any) -> dict[str, Any] | None:
    """Project one announcement attachment onto strict scalar fields."""
    if not isinstance(value, dict):
        return None
    attachment_id = _positive_projection_id(value.get("Id"))
    if attachment_id is None:
        return None

    file_name = value.get("FileName", "")
    if not isinstance(file_name, str):
        return None
    safe_file_name = _safe_announcement_text(file_name)
    if file_name and not safe_file_name:
        return None

    size = value.get("Size", 0)
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size > _MAX_ANNOUNCEMENT_ATTACHMENT_SIZE
    ):
        return None

    attachment_type = value.get("Type", "File")
    if not isinstance(attachment_type, str):
        return None
    safe_type = _safe_announcement_text(attachment_type)
    if attachment_type and not safe_type:
        return None

    return {
        "Id": attachment_id,
        "FileName": safe_file_name,
        "Size": size,
        "Type": safe_type,
    }


def _normalise_announcement(value: dict[str, Any]) -> dict[str, Any]:
    """Project an announcement without serializing unknown upstream fields."""
    projected: dict[str, Any] = {}

    if "Id" in value:
        projected["Id"] = _positive_projection_id(value.get("Id"))
    if "Title" in value:
        projected["Title"] = _safe_announcement_text(value.get("Title"))
    if "Body" in value:
        body = _rich_text_string(value.get("Body"))
        projected["Body"] = _safe_rich_text(body)
    if "CreatedDate" in value:
        projected["CreatedDate"] = _safe_announcement_text(value.get("CreatedDate")) or None
    if "Attachments" in value:
        raw_attachments = value.get("Attachments")
        attachments: list[dict[str, Any]] = []
        if isinstance(raw_attachments, list):
            for raw_attachment in raw_attachments:
                if attachment := _normalise_announcement_attachment(raw_attachment):
                    attachments.append(attachment)
        projected["Attachments"] = attachments
    return projected


def _normalise_announcements(value: Any) -> list[dict[str, Any]]:
    """Validate and project announcement records before output/rendering."""
    if not isinstance(value, list):
        return []
    announcements: list[dict[str, Any]] = []
    for record in value:
        if not isinstance(record, dict) or not record:
            continue
        projected = _normalise_announcement(record)
        if projected:
            announcements.append(projected)
    return announcements


def _safe_calendar_identifier(value: Any) -> int | str | None:
    """Normalize a scalar calendar identifier without accepting path-like data."""
    identifier = _positive_projection_id(value)
    if identifier is not None:
        return identifier
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", candidate):
        return candidate
    return None


def _normalise_calendar_event(value: dict[str, Any]) -> dict[str, Any]:
    """Project one calendar event onto the fields used by the read command."""
    projected: dict[str, Any] = {}
    for key in ("CalendarEventId", "Id"):
        if key in value:
            if identifier := _safe_calendar_identifier(value.get(key)):
                projected[key] = identifier
    for key in ("Title", "OrgUnitName", "StartDateTime", "EndDateTime"):
        if key in value:
            projected[key] = _safe_announcement_text(value.get(key))
    return projected


def _normalise_calendar_events(value: Any) -> list[dict[str, Any]]:
    """Validate and project calendar records before output/rendering."""
    if not isinstance(value, list):
        return []
    events: list[dict[str, Any]] = []
    for record in value:
        if not isinstance(record, dict) or not record:
            continue
        projected = _normalise_calendar_event(record)
        if projected:
            events.append(projected)
    return events


def _normalise_quiz(value: dict[str, Any]) -> dict[str, Any]:
    """Project one quiz onto strict identifiers and printable scalar fields."""
    projected: dict[str, Any] = {}
    if "QuizId" in value:
        if quiz_id := _positive_projection_id(value.get("QuizId")):
            projected["QuizId"] = quiz_id
    for key in ("Name", "StartDate", "EndDate"):
        if key in value:
            projected[key] = _safe_announcement_text(value.get(key))
    if "IsActive" in value and isinstance(value.get("IsActive"), bool):
        projected["IsActive"] = value["IsActive"]
    return projected


def _normalise_quizzes(value: Any) -> list[dict[str, Any]]:
    """Validate and project quiz records before output/rendering."""
    if not isinstance(value, list):
        return []
    quizzes: list[dict[str, Any]] = []
    for record in value:
        if not isinstance(record, dict) or not record:
            continue
        projected = _normalise_quiz(record)
        if projected:
            quizzes.append(projected)
    return quizzes


def _safe_grade_number(value: Any) -> int | float | None:
    """Normalize a finite numeric grade field and reject nested/raw values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", candidate):
            return None
        try:
            number = float(candidate)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() else number
    return None


def _safe_grade_scalar(value: Any) -> str | int | float | None:
    """Keep printable text or finite numeric grade metadata only."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, str) and value.isprintable():
        return value
    return None


def _normalise_grade_schema(value: Any) -> list[dict[str, Any]]:
    """Project grade schema records onto safe merge/display fields."""
    if not isinstance(value, list):
        return []
    schema: list[dict[str, Any]] = []
    for record in value:
        if not isinstance(record, dict):
            continue
        grade_id = _positive_projection_id(record.get("Id"))
        if grade_id is None:
            continue
        weight = _safe_grade_scalar(record.get("Weight"))
        schema.append({
            "id": grade_id,
            "name": _safe_announcement_text(record.get("Name")),
            "weight": weight if weight is not None else "",
            "type": _safe_announcement_text(record.get("GradeType")),
            "max_points": _safe_grade_number(record.get("MaxPoints")),
        })
    return schema


def _normalise_grade_values(value: Any) -> dict[int, dict[str, int | float | None]]:
    """Project grade value records keyed by positive grade-object identifier."""
    if not isinstance(value, list):
        return {}
    values: dict[int, dict[str, int | float | None]] = {}
    for record in value:
        if not isinstance(record, dict):
            continue
        raw_id = record.get("GradeObjectIdentifier")
        if raw_id is None:
            raw_id = record.get("GradeObjectId")
        grade_id = _positive_projection_id(raw_id)
        if grade_id is None:
            continue
        values[grade_id] = {
            "numerator": _safe_grade_number(record.get("PointsNumerator")),
            "denominator": _safe_grade_number(record.get("PointsDenominator")),
        }
    return values


def _display_text(value: Any) -> str:
    """Return a renderer-safe text field without echoing malformed objects."""
    return safe_display_text(value)


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
        schema = _normalise_grade_schema(client.get_grade_schema(org_id))
        values = _normalise_grade_values(client.get_my_grades(org_id))
    except SessionExpiredError as e:
        return _fetch_error_result(org_id, "grades", json_output, e, generic_human_rc=1)
    except Exception as e:
        return _fetch_error_result(org_id, "grades", json_output, e, generic_human_rc=1)

    # Merge schema + values
    merged = []
    for g in schema:
        value = values.get(g["id"], {})
        num = value.get("numerator")
        den = value.get("denominator")
        if den is None or den == 0:
            den = g["max_points"]
        display_denominator = den if den is not None else "–"
        merged.append({
            "name": g["name"], "weight": g["weight"],
            "grade": f"{num}/{display_denominator}" if num is not None else f"–/{display_denominator}",
            "type": g["type"],
        })

    if json_output:
        return {"course_id": org_id, "grades": merged}

    if not merged:
        if title is None:
            print("No grades found for this course.")
        return 0

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
    def _fetch(org_unit_id: int) -> list[dict[str, Any]]:
        return _normalise_announcements(client.get_announcements(org_unit_id))

    def _render(announcements, t):
        print(f"\n📢 {t}")
        for a in announcements:
            print(f"  [{_fmt_date(a.get('CreatedDate'))}] {a.get('Title', '')}")
            body = _rich_text_string(a.get("Body"))
            if body:
                print(f"    {_short(_strip_html(body), 80)}")
            raw_attachments = a.get("Attachments", [])
            if isinstance(raw_attachments, list):
                for att in raw_attachments:
                    if not isinstance(att, dict):
                        continue
                    size = att.get("Size", 0)
                    if not isinstance(size, (int, float)) or isinstance(size, bool):
                        size = 0
                    print(f"    📎 {att.get('FileName', '')} ({size / 1024:.0f} KB)")
    return _show_with_error_handling(
        org_id,
        _fetch,
        "announcements",
        json_output,
        _render,
        title,
        "No announcements found for this course.",
    )


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
    def _fetch(org_unit_id: int) -> list[dict[str, Any]]:
        return _normalise_calendar_events(client.get_calendar(org_unit_id))

    def _render(events, t):
        _print_table(["Date", "Title", "Course"], [
            [_fmt_date(e.get("StartDateTime")), _short(_display_text(e.get("Title", "")), 40), e.get("OrgUnitName", "")]
            for e in events
        ], title=f"Calendar – {t}")
    return _show_with_error_handling(
        org_id,
        _fetch,
        "events",
        json_output,
        _render,
        title,
        "No calendar events found for this course.",
    )


def cmd_assignments(course_id: str | None = None, json_output: bool = False) -> int:
    """Show dropbox folders (assignments) for a course or all courses."""
    return _for_course_or_all(course_id, _show_course_assignments, json_output, "assignments")


_RICH_TEXT_MAX_DEPTH = 64


def _rich_text_string(value: Any) -> str | None:
    """Extract a string from Brightspace's RichText shapes.

    Brightspace normally returns ``{"Text": ..., "Html": ...}``, but nested
    ``Text``/``Html`` objects and plain strings also occur across endpoint
    versions. Walk those shapes iteratively so malformed upstream data cannot
    trigger recursion failures or be rendered through ``str(dict)``. The
    bounded walk also keeps a cyclic response from hanging the command.
    """
    pending: list[tuple[Any, int]] = [(value, 0)]
    visited: set[int] = set()
    while pending:
        current, depth = pending.pop()
        if isinstance(current, str):
            return current
        if not isinstance(current, dict) or depth >= _RICH_TEXT_MAX_DEPTH:
            continue

        current_id = id(current)
        if current_id in visited:
            continue
        visited.add(current_id)

        # Push Text first and Html second so the preferred Html branch is
        # visited first while still allowing a malformed/deep Html branch to
        # fall back to a valid Text sibling.
        text_value = current.get("Text")
        if isinstance(text_value, dict) or isinstance(text_value, str):
            pending.append((text_value, depth + 1))

        html_value = current.get("Html")
        if isinstance(html_value, dict) or (
            isinstance(html_value, str) and html_value
        ):
            pending.append((html_value, depth + 1))
    return None


def _strip_html(value: Any) -> str:
    """Remove HTML tags and decode HTML entities from a safe text value."""
    text = _rich_text_string(value)
    if not text or len(text) > _MAX_RICH_TEXT_LENGTH:
        return ""
    # Strip markup before decoding so encoded literal angle brackets stay text.
    stripped = re.sub(r"<[^>]+>", "", text)
    stripped = re.sub(r"[\r\n\t\f\v]+", " ", stripped)
    decoded = html.unescape(stripped).strip()
    if any(
        character in "\r\n\t\f\v"
        or (not character.isprintable() and not character.isspace())
        for character in decoded
    ):
        return ""
    normalized = " ".join(decoded.split())
    return safe_display_text(normalized, max_len=_MAX_RICH_TEXT_LENGTH)


def _positive_projection_id(value: Any) -> int | None:
    """Normalize a positive API identifier without accepting path-like text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not re.fullmatch(r"[0-9]+", normalized):
        return None
    try:
        identifier = int(normalized)
    except (TypeError, ValueError):
        return None
    return identifier if identifier > 0 else None


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

    if not isinstance(folders, list):
        return _fetch_error_result(
            org_id,
            "assignments",
            json_output,
            ValueError("Assignment folders have an invalid response shape."),
        )

    # Process folders into structured format
    assignments = []
    seen_folder_ids: set[int] = set()
    for f in folders:
        if not isinstance(f, dict):
            # Brightspace occasionally includes null/placeholder entries in a
            # folder list.  Preserve valid siblings instead of dropping the
            # entire course view on one malformed record.
            continue

        # Validate and canonicalize the folder id before the detail helper can
        # interpolate it into a request path.  Strings made only of decimal
        # digits remain compatible with lightweight API doubles; all other
        # strings (including traversal and credential-shaped sentinels),
        # booleans, floats, and non-positive values are skipped silently.
        folder_id = _positive_projection_id(f.get("Id", f.get("FolderId")))
        if folder_id is None:
            print("Warning: skipped malformed assignment folder.", file=sys.stderr)
            continue
        if folder_id in seen_folder_ids:
            continue
        folder_input = dict(f)
        folder_input["Id"] = folder_id
        try:
            f, attachment_items = folder_with_attachments(client, org_id, folder_input)
        except _AssignmentDataError as e:
            print(
                f"Warning: skipped malformed assignment folder: {format_user_error(e)}",
                file=sys.stderr,
            )
            continue
        except Exception as e:
            # Required detail fetches are part of the assignment collection.
            # Returning success after dropping one would make an incomplete
            # response indistinguishable from a complete one to automation.
            return _fetch_error_result(org_id, "assignments", json_output, e)
        if not isinstance(f, dict):
            print("Warning: skipped malformed assignment folder.", file=sys.stderr)
            continue
        folder_id = _positive_projection_id(f.get("Id"))
        if folder_id is None:
            print("Warning: skipped malformed assignment folder.", file=sys.stderr)
            continue
        if not isinstance(attachment_items, list):
            print("Warning: skipped malformed assignment folder.", file=sys.stderr)
            continue
        seen_folder_ids.add(folder_id)
        # Extract attachments info
        attachments = []
        for att in attachment_items:
            if not isinstance(att, dict):
                continue
            file_id = _positive_projection_id(att.get("Id"))
            if file_id is None:
                continue
            raw_file_name = att.get("FileName", "")
            size = att.get("Size", 0)
            attachment_type = att.get("Type", "File")
            file_name = safe_attachment_filename(
                raw_file_name,
                file_id,
                fallback=False,
            )
            if (
                isinstance(size, bool)
                or not isinstance(size, (int, float))
                or size < 0
                or (isinstance(size, float) and not math.isfinite(size))
                or size > _MAX_ANNOUNCEMENT_ATTACHMENT_SIZE
            ):
                size = 0
            attachment_type = _safe_announcement_text(attachment_type)
            if not attachment_type:
                attachment_type = "File"
            attachments.append({
                "file_id": file_id,
                "file_name": file_name,
                "size": size,
                "attachment_type": attachment_type,
            })



        # Availability info
        availability = f.get("Availability")
        if not isinstance(availability, dict):
            availability = {}
        instructions = _safe_rich_text(
            _rich_text_string(f.get("CustomInstructions"))
        )
        instructions_preview = _short(_strip_html(instructions), 80) if instructions else None
        if not instructions_preview:
            instructions_preview = None

        due_date = _safe_announcement_text(f.get("DueDate"))
        if not due_date:
            due_date = _safe_announcement_text(f.get("Due"))
        category_name = _safe_announcement_text(f.get("CategoryName"))
        if not category_name:
            category_name = _safe_announcement_text(f.get("SubmissionType"))
        start_date = _safe_announcement_text(availability.get("StartDate")) or None
        end_date = _safe_announcement_text(availability.get("EndDate")) or None
        assignments.append({
            "folder_id": folder_id,
            "name": safe_assignment_folder_name(
                _strip_html(f.get("Name", "")),
                folder_id,
                fallback=False,
            ),
            "due_date": due_date,
            "attachment_count": len(attachments), "attachments": attachments,
            "custom_instructions": instructions or None,
            "custom_instructions_preview": instructions_preview,
            "submission_type": category_name,
            "availability": {"start": start_date, "end": end_date} if (start_date or end_date) else None,
        })

    if json_output:
        return {"course_id": org_id, "assignments": assignments}

    if not assignments:
        if title is None:
            print(f"\n📋 {title or str(org_id)}")
            print("  No assignments found for this course.")
        return 0

    print(f"\n📋 {title or str(org_id)}")

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
    def _fetch(org_unit_id: int) -> list[dict[str, Any]]:
        return _normalise_quizzes(client.get_quizzes(org_unit_id))

    def _render(quizzes, t):
        _print_table(["ID", "Name", "Start", "End"], [
            [str(q.get("QuizId", "")), _short(_display_text(q.get("Name", "")), 35), _fmt_date(q.get("StartDate")), _fmt_date(q.get("EndDate"))]
            for q in quizzes
        ], title=f"Quizzes – {t}")
    return _show_with_error_handling(
        org_id,
        _fetch,
        "quizzes",
        json_output,
        _render,
        title,
        "No quizzes found for this course.",
    )
