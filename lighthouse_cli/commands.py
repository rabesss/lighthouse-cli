"""Command implementations — thin orchestration layer delegating to domain modules."""

from __future__ import annotations

import math
import re
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from .api import CourseNotFoundError, LighthouseClient, resolve_course_id
from .config import BASE_URL, DEFAULT_DOWNLOAD_DIR, warn_if_cookies_stale
from .display import error as _error, fmt_date as _fmt_date, format_user_error, output_json as _output_json, print_table as _print_table, safe_display_text, short as _short, utc_now_iso as _utc_now_iso
from .course_config import load as _load_course_config, semester_state as _semester_state
from .sync_engine import Mode, run_course, validate_output_root
from .assignments import download_single_attachment as _download_single_attachment
from .manifest import MAX_MANIFEST_SIZE, normalize_sha256
from .submit import cmd_submit  # noqa: F401 — re-export
from .show import cmd_grades, cmd_announcements, cmd_calendar, cmd_assignments, cmd_quizzes  # noqa: F401 — re-export
from .utils import get_enrolled_course_catalog


_ASSIGNMENT_NOT_FOUND = "Requested assignment folder was not found."
_ASSIGNMENT_LIST_INVALID = "Assignment folders have an invalid response shape."

# Content and quiz fields are server-controlled.  Keep their projections
# bounded and printable before they reach either a terminal or JSON encoder.
# The depth/node limits also make malformed JSON-shaped responses unable to
# exhaust Python's call stack or produce an unbounded result document.
# Keep this comfortably below Python 3.11's effective recursion budget in the
# shared ``output_json`` finite-number walk (each nested module contributes
# both a list and a mapping frame).
_CONTENT_MAX_DEPTH = 32
_CONTENT_MAX_NODES = 10_000
_CONTENT_MAX_TEXT = 512
_CONTENT_TRUNCATED_TITLE = "[content truncated]"
_QUIZ_RICH_TEXT_MAX_DEPTH = 16
_QUIZ_RICH_TEXT_MAX_TEXT = 4096


def _course_identifier(value: Any) -> int | None:
    """Return only a numeric course ID for an external result payload.

    Course arguments may be name substrings, so callers must keep the original
    value while resolving them. Once a resolution fails, however, copying that
    value into JSON or an error envelope can expose arbitrary input such as a
    pasted cookie. Unknown and malformed identifiers therefore become JSON
    ``null`` rather than being echoed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _positive_id(value: Any) -> int | None:
    """Coerce only positive integer-like identifiers from untrusted API data."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        with suppress(ValueError):
            number = int(candidate)
            return number if number > 0 else None
    return None


def _safe_server_text(value: Any, *, fallback: str = "", max_len: int = _CONTENT_MAX_TEXT) -> str:
    """Compatibility wrapper around the centralized display-label guard."""
    return safe_display_text(value, fallback, max_len=max_len)


def _safe_course_name(value: Any, course_id: Any) -> str:
    """Return a bounded printable course label for output surfaces."""
    identifier = _course_identifier(course_id)
    fallback = f"Course-{identifier}" if identifier is not None else "Course"
    return _safe_server_text(value, fallback=fallback)


def _safe_content_id(value: Any) -> int | None:
    """Keep only positive integer content identifiers."""
    return _positive_id(value)


def _safe_content_url(value: Any) -> str | None:
    """Keep a bounded printable topic URL, or omit malformed values."""
    safe_url = _safe_server_text(value, max_len=_CONTENT_MAX_TEXT)
    return safe_url or None


def _content_module_projection(module: dict[str, Any]) -> dict[str, Any]:
    """Project one untrusted module onto the documented JSON fields."""
    return {
        "ModuleId": _safe_content_id(module.get("ModuleId")),
        "Title": _safe_server_text(module.get("Title")),
        "Modules": [],
        "Topics": [],
    }


def _content_topic_projection(topic: dict[str, Any]) -> dict[str, Any]:
    """Project one untrusted topic onto scalar, renderer-safe fields."""
    return {
        "TopicId": _safe_content_id(topic.get("TopicId")),
        "Title": _safe_server_text(topic.get("Title")),
        "TypeIdentifier": _safe_server_text(topic.get("TypeIdentifier"), max_len=64),
        "Url": _safe_content_url(topic.get("Url")),
    }


def _content_truncated_module() -> dict[str, Any]:
    """Return a fixed marker used when a module branch is bounded."""
    return {
        "ModuleId": None,
        "Title": _CONTENT_TRUNCATED_TITLE,
        "Modules": [],
        "Topics": [],
        "Type": "truncated",
    }


def _content_truncated_topic() -> dict[str, Any]:
    """Return a fixed marker used when topic output reaches its node bound."""
    return {
        "TopicId": None,
        "Title": _CONTENT_TRUNCATED_TITLE,
        "TypeIdentifier": "truncated",
        "Url": None,
        "Type": "truncated",
    }


def _normalise_content_modules(modules: Any) -> list[dict[str, Any]]:
    """Build a bounded nested content projection without recursive calls."""
    if not isinstance(modules, list):
        return []

    projected: list[dict[str, Any]] = []
    # ``target`` always points at a list in the newly-created projection, so
    # untrusted objects never become part of the JSON result by reference.
    stack: list[tuple[str, Any, list[dict[str, Any]], int]] = [
        ("module", module, projected, 0)
        for module in reversed(modules)
    ]
    seen_modules: set[int] = set()
    node_count = 0

    def append_module_marker(target: list[dict[str, Any]]) -> None:
        nonlocal node_count
        if node_count < _CONTENT_MAX_NODES:
            target.append(_content_truncated_module())
            node_count += 1

    while stack:
        kind, current, target, depth = stack.pop()
        if kind == "topic":
            if not isinstance(current, dict):
                continue
            if node_count >= _CONTENT_MAX_NODES - 1:
                if node_count < _CONTENT_MAX_NODES:
                    target.append(_content_truncated_topic())
                    node_count += 1
                stack.clear()
                continue
            target.append(_content_topic_projection(current))
            node_count += 1
            continue
        if kind == "topics":
            if not isinstance(current, list):
                continue
            for topic in reversed(current):
                stack.append(("topic", topic, target, depth))
            continue
        if not isinstance(current, dict):
            continue
        module_key = id(current)
        if module_key in seen_modules:
            continue
        seen_modules.add(module_key)
        if node_count >= _CONTENT_MAX_NODES - 1:
            append_module_marker(target)
            stack.clear()
            continue

        output_module = _content_module_projection(current)
        target.append(output_module)
        node_count += 1

        topics = current.get("Topics", [])
        if isinstance(topics, list):
            stack.append(("topics", topics, output_module["Topics"], depth + 1))

        children = current.get("Modules", [])
        if isinstance(children, list):
            if depth >= _CONTENT_MAX_DEPTH and children:
                append_module_marker(output_module["Modules"])
            else:
                for child in reversed(children):
                    stack.append(("module", child, output_module["Modules"], depth + 1))

    return projected


def _normalise_semester_records(value: Any) -> list[dict[str, Any]]:
    """Project semester records onto safe ID, name, and code fields."""
    if not isinstance(value, list):
        return []
    semesters: list[dict[str, Any]] = []
    for record in value:
        if not isinstance(record, dict):
            continue
        org_id = _positive_id(record.get("OrgUnitId"))
        if org_id is None:
            continue
        semesters.append(
            {
                "OrgUnitId": org_id,
                "Name": _safe_server_text(record.get("Name")),
                "Code": _safe_server_text(record.get("Code")),
            }
        )
    return semesters


def _selector_error(value: Any, option: str) -> str | None:
    """Return a fixed validation message for an assignment selector.

    Click normally supplies integers for these options, but command functions
    are also called directly by integrations and tests. Keep that boundary
    strict so zero, negative, boolean, and string values cannot widen a
    folder-scoped download into an all-assignment download.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return f"--{option} must be a positive integer"
    return None


def _coerce_boolish(value: Any, *, default: bool = False) -> bool:
    """Coerce Brightspace's bool-ish fields without treating ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "y", "1", "on", "active"}:
            return True
        if normalized in {"false", "no", "n", "0", "off", "inactive", ""}:
            return False
    return default


def _scope_error_payload() -> dict[str, Any]:
    """Stable empty envelope for download/sync scope failures."""
    return {"courses": [], "also_errors": []}


def _single_error_payload(course_id: Any, *, action: str) -> dict[str, Any]:
    """Stable empty envelope for a single-course download/sync failure."""
    if action == "sync":
        return {
            "course_id": _course_identifier(course_id),
            "downloaded": [],
            "skipped": [],
            "updated": [],
            "orphaned": [],
            "errors": [],
        }
    return {
        "course_id": _course_identifier(course_id),
        "downloaded": [],
        "errors": [],
    }


def _course_list_error_payload() -> dict[str, Any]:
    """Stable empty envelope for a courses command failure."""
    return {"courses": []}


def _output_multi_course_json(sem_id: int, sem_name: str, courses_results: list[dict], also_errors: list[str]) -> None:
    _output_json({
        "semester": {
            "id": sem_id,
            "name": _safe_server_text(sem_name, fallback="Unknown Semester"),
        },
        "synced_at": _utc_now_iso(),
        "summary": {"courses_checked": len(courses_results),
                    **{k: sum(len(c.get(k, [])) for c in courses_results) for k in (
                        "downloaded", "skipped", "updated", "duplicates", "errors",
                        "assignments_downloaded", "assignment_errors",
                    )}},
        "courses": courses_results,
        "also_errors": [format_user_error(error) for error in also_errors],
    })


# ---------------------------------------------------------------------------
# Rendering funnels (human | --json) over sync_engine results
# ---------------------------------------------------------------------------

def _print_warnings(result: dict[str, Any]) -> None:
    """Emit recorded engine warnings to stderr (never stdout, even under --json)."""
    for warning in result["warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)


def _single_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Project an engine entry onto the single-course download schema."""
    return {
        "topic_id": entry.get("topic_id"),
        "filename": _safe_server_text(entry.get("filename")),
        "size": entry.get("size", 0),
        "path": _safe_server_text(entry.get("path")),
    }


def _pipeline_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Project an engine entry onto the sync/multi schema (no raw byte size)."""
    projected: dict[str, Any] = {}
    for key in ("topic_id", "size_kb"):
        if key in entry:
            projected[key] = entry[key]
    if "filename" in entry:
        projected["filename"] = _safe_server_text(entry.get("filename"))
    if "path" in entry:
        projected["path"] = _safe_server_text(entry.get("path"))
    if "sha256" in entry:
        projected["sha256"] = normalize_sha256(entry.get("sha256"))
    if "extension" in entry:
        projected["extension"] = _safe_server_text(entry.get("extension"), max_len=32)
    return projected


_ERROR_ID_FIELDS = ("topic_id", "folder_id", "file_id")
_SAFE_ERROR_TYPES = frozenset(
    {
        "path",
        "manifest_corrupt",
        "assignment_list",
        "assignment_data",
        "assignment_not_found",
        "topic_data",
    }
)


def _safe_error_identifier(value: Any) -> int | str | None:
    """Keep only positive integer-like IDs from a structured error."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        with suppress(ValueError):
            return candidate if int(candidate) > 0 else None
    return None


def _safe_orphan_topic_id(value: Any) -> str | None:
    """Keep only positive ASCII digit keys from a manifest orphan record."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not re.fullmatch(r"[0-9]+", candidate) or not candidate.strip("0"):
        return None
    return candidate


def _safe_orphan_entry(entry: Any) -> dict[str, Any]:
    """Project a manifest orphan without exposing its filename or path."""
    record = entry if isinstance(entry, dict) else {}
    size = record.get("size")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or size > MAX_MANIFEST_SIZE
    ):
        size = 0
    return {
        "topic_id": _safe_orphan_topic_id(record.get("topic_id")),
        "size": size,
        "size_kb": round(size / 1024, 1),
        "sha256": normalize_sha256(record.get("sha256", "")),
    }


def _single_error(error: dict[str, Any]) -> dict[str, Any]:
    """Project an engine error onto the single-course schema ({topic_id, error})."""
    result: dict[str, Any] = {}
    if (topic_id := _safe_error_identifier(error.get("topic_id"))) is not None:
        result["topic_id"] = topic_id
    if "error" in error:
        result["error"] = format_user_error(str(error["error"]))
    return result


def _safe_error_entries(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project structured errors onto a small, safe allowlist.

    Error records can carry server-provided filenames, titles, paths, and
    other context that is useful inside the engine but unsafe to expose in a
    machine-readable command result. Keep only positive numeric identifiers,
    known internal error types, and the already-sanitized message.
    """
    safe_entries: list[dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        entry: dict[str, Any] = {}
        for field in _ERROR_ID_FIELDS:
            if (identifier := _safe_error_identifier(error.get(field))) is not None:
                entry[field] = identifier
        error_type = error.get("type")
        if isinstance(error_type, str) and error_type in _SAFE_ERROR_TYPES:
            entry["type"] = error_type
        if "error" in error:
            entry["error"] = format_user_error(str(error["error"]))
        if entry:
            safe_entries.append(entry)
    return safe_entries


def _assignment_selector_snapshot(
    client: LighthouseClient,
    org_id: int,
    assignment_id: int,
) -> tuple[list[dict[str, Any]] | tuple[dict[str, Any], ...] | None, dict[str, str] | None]:
    """Validate and retain one assignment folder list before writing.

    Assignment selection is a read-only preflight because ``run_course`` may
    otherwise download content topics before its assignment phase discovers a
    nonexistent folder.  Return the exact validated list so the assignment
    phase cannot observe a different second snapshot.  Keep this check narrow:
    only a positive integer ``Id`` matching the requested selector is accepted
    as a folder record.
    """
    try:
        folders = client.get_dropbox_folders(org_id)
    except Exception as exc:
        return None, {"error": format_user_error(exc), "type": "assignment_list"}
    if not isinstance(folders, (list, tuple)):
        return None, {"error": _ASSIGNMENT_LIST_INVALID, "type": "assignment_list"}
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        folder_id = folder.get("Id")
        if (
            isinstance(folder_id, int)
            and not isinstance(folder_id, bool)
            and folder_id > 0
            and folder_id == assignment_id
        ):
            return folders, None
    return None, {"error": _ASSIGNMENT_NOT_FOUND, "type": "assignment_not_found"}


def _render_assignment_selector_error(
    org_id: int,
    error: dict[str, str],
    *,
    json_output: bool,
) -> int:
    """Render a safe assignment preflight error without creating a course dir."""
    safe_entries = _safe_error_entries([error])
    safe_message = safe_entries[0].get("error", "Command failed.") if safe_entries else "Command failed."
    print(f"Error: {safe_message}", file=sys.stderr)
    if json_output:
        _output_json({
            "course_id": org_id,
            "downloaded": [],
            "errors": [],
            "assignments_downloaded": [],
            "assignment_errors": safe_entries,
        })
    return 1


def _single_course_json(result: dict[str, Any], *, action: str, include_assignments: bool) -> Any:
    """Project one engine result into the single-course JSON schema."""
    if result["mode"] is Mode.PLAN:
        # Keep the historical plan-array shape for a clean dry run.  If a
        # local validation (for example, a symlinked course destination) makes
        # the plan fail, preserve the diagnostic in one command-shaped object
        # instead of returning an indistinguishable empty array.
        if not result["errors"] and not result["assignments"]["errors"]:
            return result["planned"]
        return {
            "course_id": result["org_id"],
            "course_name": _safe_course_name(result.get("course_name"), result.get("org_id")),
            "folder": str(result["dest"]),
            "planned": result["planned"],
            "errors": [_single_error(e) for e in result["errors"]]
            + _safe_error_entries(result["assignments"]["errors"]),
        }
    if result["empty"]:
        data: dict[str, Any] = {
            "course_id": result["org_id"],
            "course_name": _safe_course_name(result.get("course_name"), result.get("org_id")),
            "folder": str(result["dest"]),
            "errors": [_single_error(e) for e in result["errors"]],
        }
        if action == "sync":
            orphaned = result.get("orphaned") or []
            data.update(
                downloaded=[],
                skipped=[],
                updated=[],
                orphaned=[_safe_orphan_entry(e) for e in orphaned],
            )
        else:
            data.update(manifest=str(result["manifest_path"]), downloaded=[])
        return data

    assignments = result["assignments"]
    data: dict[str, Any] = {
        "course_id": result["org_id"],
        "course_name": _safe_course_name(result.get("course_name"), result.get("org_id")),
        "folder": str(result["dest"]),
    }
    if action == "sync":
        data.update(
            downloaded=[_pipeline_entry(e) for e in result["downloaded"]],
            skipped=[_pipeline_entry(e) for e in result["skipped"]],
            updated=[_pipeline_entry(e) for e in result["updated"]],
            orphaned=[_safe_orphan_entry(e) for e in result["orphaned"]],
            errors=[_single_error(e) for e in result["errors"]],
        )
        if include_assignments:
            data.update(assignments_downloaded=assignments["downloaded"], assignments_skipped=assignments["skipped"],
                        assignments_updated=assignments["updated"], assignment_errors=_safe_error_entries(assignments["errors"]))
    else:
        data.update(
            manifest=str(result["manifest_path"]),
            downloaded=[_single_entry(e) for e in result["downloaded"]],
            errors=[_single_error(e) for e in result["errors"]],
        )
        if include_assignments:
            data.update(assignments_downloaded=assignments["downloaded"], assignment_errors=_safe_error_entries(assignments["errors"]))
    return data


def _multi_course_json(result: dict[str, Any], *, sem_name: str, action: str) -> dict[str, Any]:
    """Project one engine result into the multi-course per-course JSON schema."""
    assignments = result["assignments"]
    course: dict[str, Any] = {
        "course_id": result["org_id"],
        "course_name": _safe_course_name(result.get("course_name"), result.get("org_id")),
        "semester": _safe_server_text(sem_name, fallback="Unknown Semester"),
        "root": str(result["dest"]),
        "manifest_total": result["manifest_total"],
        "downloaded": [_pipeline_entry(e) for e in result["downloaded"]],
        "skipped": [_pipeline_entry(e) for e in result["skipped"]],
        "updated": [_pipeline_entry(e) for e in result["updated"]],
    }
    if action == "sync":
        course["orphaned"] = [_safe_orphan_entry(e) for e in result["orphaned"]]
    course["duplicates"] = result["duplicates"]
    course["errors"] = _safe_error_entries(result["errors"])
    if action == "sync":
        course.update(assignments_downloaded=assignments["downloaded"], assignments_skipped=assignments["skipped"],
                      assignments_updated=assignments["updated"], assignment_errors=_safe_error_entries(assignments["errors"]))
    else:
        course.update(assignments_downloaded=assignments["downloaded"], assignment_errors=_safe_error_entries(assignments["errors"]))
    return course


def _multi_course_failure_json(
    course_id: int,
    *,
    root: Path,
    sem_name: str,
    action: str,
    error: Exception,
) -> dict[str, Any]:
    """Keep a failed scoped course visible in the multi-course JSON envelope."""
    course: dict[str, Any] = {
        "course_id": course_id,
        "course_name": "",
        "semester": _safe_server_text(sem_name, fallback="Unknown Semester"),
        "root": str(root),
        "manifest_total": 0,
        "downloaded": [],
        "skipped": [],
        "updated": [],
        "duplicates": [],
        "errors": [{"error": format_user_error(error)}],
        "assignments_downloaded": [],
        "assignment_errors": [],
    }
    if action == "sync":
        course.update(orphaned=[], assignments_skipped=[], assignments_updated=[])
    return course


def _render_course_human(result: dict[str, Any], *, action: str, include_assignments: bool) -> int:
    """Render one engine result as human-readable text. Returns per-course exit code."""
    if result["mode"] is Mode.PLAN:
        print(f"Would download {result['topic_count']} files to {result['dest']}/\n")
        print("\n".join(
            f"  [{t.get('topic_id')}] {_safe_server_text(t.get('title'), fallback='Untitled')}"
            for t in result["planned"]
            if isinstance(t, dict)
        ))
        for error in result["errors"]:
            if "error" in error:
                print(f"  FAILED: {format_user_error(str(error['error']))}", file=sys.stderr)
        if include_assignments:
            print("\n  (Assignment downloads not shown in dry-run)")
        return 1 if result["errors"] or result["assignments"]["errors"] else 0
    if result["empty"]:
        orphaned = result.get("orphaned") or []
        if action == "sync" and orphaned:
            print(f"No downloadable files found; {len(orphaned)} orphaned.")
        else:
            print("No downloadable files found.")
        # Uniform policy: a recorded failure (e.g. corrupt manifest surfaced
        # on an empty course) is an error-class exit even with no downloads.
        return 1 if (result["errors"] or result["assignments"]["errors"]) else 0

    assignments = result["assignments"]
    failed = 1 if (result["errors"] or assignments["errors"]) else 0
    if action == "sync":
        parts = [f"{len(result['downloaded'])} new"]
        if assignments["downloaded"]:
            parts.append(f"{len(assignments['downloaded'])} assignment new")
        if assignments["updated"]:
            parts.append(f"{len(assignments['updated'])} assignment updated")
        parts.extend([f"{len(result['updated'])} updated", f"{len(result['skipped'])} skipped", f"{len(result['orphaned'])} orphaned", f"{len(result['errors'])} errors"])
        if assignments["errors"]:
            parts.append(f"{len(assignments['errors'])} assignment errors")
        print(f"Synced {_safe_course_name(result.get('course_name'), result.get('org_id'))}: {', '.join(parts)}")
        return failed

    for i, entry in enumerate(result["downloaded"], 1):
        path = _safe_server_text(entry.get("path"), fallback="[path omitted]")
        size = entry.get("size", 0)
        if not isinstance(size, (int, float)) or isinstance(size, bool) or not math.isfinite(size):
            size = 0
        print(f"  [{i}/{result['topic_count']}] {path} ({size / 1024:.0f} KB)")
    for error in result["errors"]:
        if "topic_id" in error:
            print(f"  FAILED topic {error['topic_id']}: {format_user_error(str(error['error']))}", file=sys.stderr)
    if assignments["downloaded"]:
        print(f"\nAssignments: {len(assignments['downloaded'])} attachment(s) downloaded")
    for assignment_error in assignments["errors"]:
        if "error" in assignment_error:
            print(
                f"  FAILED assignment: {format_user_error(str(assignment_error['error']))}",
                file=sys.stderr,
            )
    print(f"\nDone: {len(result['downloaded'])}/{result['topic_count']} files downloaded to {result['dest']}")
    if assignments["errors"]:
        print(f"  {len(assignments['errors'])} assignment error(s)")
    return failed


def _run_and_render_single(
    client: LighthouseClient,
    org_id: int,
    root: Path,
    mode: Mode,
    action: str,
    types: str,
    json_output: bool,
    *,
    include_assignments: bool = False,
    assignment_id: int | None = None,
    assignment_folders: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> int:
    """Run one course through the sync engine and render it. Returns exit code."""
    try:
        run_kwargs: dict[str, Any] = {
            "mode": mode,
            "types": types,
            "include_assignments": include_assignments,
            "assignment_id": assignment_id,
        }
        if assignment_folders is not None:
            run_kwargs["assignment_folders"] = assignment_folders
        result = run_course(client, org_id, root, **run_kwargs)
    except Exception as e:
        return _error(
            e,
            json_output=json_output,
            payload=_single_error_payload(org_id, action=action),
        )

    _print_warnings(result)
    if json_output:
        _output_json(_single_course_json(result, action=action, include_assignments=include_assignments))
    else:
        _render_course_human(result, action=action, include_assignments=include_assignments)
    return 1 if (result["errors"] or result["assignments"]["errors"]) else 0


def _run_and_render_multi(
    client: LighthouseClient,
    course_ids: list[int],
    root: Path,
    mode: Mode,
    action: str,
    types: str,
    sem_id: int,
    sem_name: str,
    also_errors: list[str],
    json_output: bool,
    include_assignments: bool,
) -> int:
    """Run every scoped course through the sync engine and render the batch."""
    results: list[dict[str, Any]] = []
    failed_courses: list[dict[str, Any]] = []
    rc = 0
    for cid in course_ids:
        try:
            results.append(run_course(client, cid, root, mode=mode, types=types, include_assignments=include_assignments))
        except Exception as e:
            # In JSON mode this per-course failure is represented in the one
            # aggregate document below; printing a JSON error here would
            # violate the exactly-one-document contract.  Keep diagnostics on
            # stderr and continue collecting the remaining courses.
            rc = 1
            print(f"Error: {format_user_error(e)}", file=sys.stderr)
            failed_courses.append(
                _multi_course_failure_json(
                    cid,
                    root=root,
                    sem_name=sem_name,
                    action=action,
                    error=e,
                )
            )

    if json_output:
        courses = list(failed_courses)
        for result in results:
            # Engine warnings reach stderr in every mode — an unknown
            # --types value must never change the downloaded set silently.
            _print_warnings(result)
            if result["mode"] is Mode.PLAN:
                plan_course = {
                    "course_id": result["org_id"],
                    "course_name": _safe_course_name(result.get("course_name"), result.get("org_id")),
                    "semester": _safe_server_text(sem_name, fallback="Unknown Semester"),
                    "root": str(result["dest"]), "manifest_total": 0,
                    "planned": result["planned"], "downloaded": [], "skipped": [],
                    "updated": [], "duplicates": [],
                    "errors": _safe_error_entries(result["errors"]),
                }
                courses.append(plan_course)
                if result["errors"] or result["assignments"]["errors"]:
                    rc = 1
                continue
            courses.append(_multi_course_json(result, sem_name=sem_name, action=action))
            if result["errors"] or result["assignments"]["errors"]:
                rc = 1
        courses.sort(key=lambda course: course["course_id"])
        _output_multi_course_json(sem_id, sem_name, courses, also_errors)
        return rc

    print(f"{'Syncing' if action == 'sync' else 'Downloading'} courses from {sem_name}...\n")
    for result in results:
        _print_warnings(result)
        if _render_course_human(result, action=action, include_assignments=include_assignments) != 0:
            rc = 1
    if also_errors:
        print("\n".join(f"  Error: {format_user_error(err)}" for err in also_errors), file=sys.stderr)
    if action == "download":
        print("\nDownload complete.")
    return rc


# ---------------------------------------------------------------------------
# download / sync commands
# ---------------------------------------------------------------------------

def cmd_download(
    course_id: str | None = None,
    output_dir: str | None = None,
    dry_run: bool = False,
    json_output: bool = False,
    force: bool = False,
    types: str = "file",
    semester: str | None = None,
    also_courses: list[str] | None = None,
    include_assignments: bool = False,
    assignment_id: int | None = None,
    attachment_id: int | None = None,
) -> int:
    """Download files from courses. Without COURSE_ID, downloads all from latest semester.

    Supports --semester, --also, --include-assignments, --assignment/--attachment.
    Creates sanitized folder per course with .lighthouse.json manifest."""
    # Assignment-specific operations are intentionally single-course only.
    # Validate before constructing a client so malformed combinations cannot
    # touch credentials, make API calls, or enter a write-capable path.
    for option, value in (("assignment", assignment_id), ("attachment", attachment_id)):
        if validation_error := _selector_error(value, option):
            return _error(
                validation_error,
                json_output=json_output,
                payload=_single_error_payload(course_id, action="download"),
            )
    if attachment_id is not None and assignment_id is None:
        return _error(
            "--attachment requires --assignment",
            json_output=json_output,
            payload=_single_error_payload(course_id, action="download"),
        )
    if (assignment_id is not None or attachment_id is not None) and course_id is None:
        return _error(
            "COURSE_ID is required when using --assignment or --attachment",
            json_output=json_output,
            payload=_single_error_payload(course_id, action="download"),
        )
    if dry_run and assignment_id is not None:
        return _error(
            "--dry-run cannot be used with --assignment",
            json_output=json_output,
            payload=_single_error_payload(course_id, action="download"),
        )
    if course_id is not None and (semester is not None or also_courses):
        return _error(
            "--semester and --also are only supported when COURSE_ID is omitted",
            json_output=json_output,
            payload=_single_error_payload(course_id, action="download"),
        )

    payload = _single_error_payload(course_id, action="download") if course_id is not None else _scope_error_payload()
    try:
        root = validate_output_root(
            Path(output_dir).expanduser() if output_dir else DEFAULT_DOWNLOAD_DIR,
        )
    except Exception as e:
        return _error(e, json_output=json_output, payload=payload)
    try:
        client = LighthouseClient(read_only_auth=dry_run)
    except Exception as e:
        return _error(
            e,
            json_output=json_output,
            payload=payload,
        )
    also_courses = also_courses or []
    mode = Mode.PLAN if dry_run else (Mode.FORCE if force else Mode.DOWNLOAD)

    if course_id is not None:
        try:
            org_id = resolve_course_id(client, course_id)
        except Exception as e:
            return _error(
                e,
                json_output=json_output,
                payload=_single_error_payload(course_id, action="download"),
            )
        # The bulk assignment path must validate before fetching the content
        # TOC.  A direct attachment download already resolves the selected
        # folder through ``get_dropbox_folder_detail`` and has its own safe
        # error path; keep that legacy API shape intact here.
        assignment_folders = None
        if assignment_id is not None and attachment_id is None:
            assignment_folders, assignment_error = _assignment_selector_snapshot(
                client, org_id, assignment_id,
            )
            if assignment_error is not None:
                return _render_assignment_selector_error(
                    org_id, assignment_error, json_output=json_output,
                )
        if assignment_id is not None and attachment_id is not None:
            return _download_single_attachment(client, org_id, assignment_id, attachment_id, root, json_output)
        return _run_and_render_single(
            client, org_id, root, mode, "download", types, json_output,
            include_assignments=include_assignments or assignment_id is not None,
            assignment_id=assignment_id,
            assignment_folders=assignment_folders,
        )

    scope = _resolve_course_scope(
        client,
        semester,
        also_courses,
        "download",
        json_output=json_output,
    )
    if isinstance(scope, int):
        return scope
    course_ids, sem_name, sem_id, also_errors = scope
    return _run_and_render_multi(
        client, course_ids, root, mode, "download", types,
        sem_id, sem_name, also_errors, json_output, include_assignments,
    )


def cmd_sync(
    course_id: str | None = None,
    output_dir: str | None = None,
    json_output: bool = False,
    force: bool = False,
    types: str = "file",
    semester: str | None = None,
    also_courses: list[str] | None = None,
    include_assignments: bool = False,
) -> int:
    """Incremental sync: skip unchanged files using manifest. Same scope options as download."""
    payload = _single_error_payload(course_id, action="sync") if course_id is not None else _scope_error_payload()
    try:
        root = validate_output_root(
            Path(output_dir).expanduser() if output_dir else DEFAULT_DOWNLOAD_DIR,
        )
    except Exception as e:
        return _error(e, json_output=json_output, payload=payload)
    try:
        client = LighthouseClient()
    except Exception as e:
        return _error(
            e,
            json_output=json_output,
            payload=payload,
        )
    also_courses = also_courses or []
    mode = Mode.FORCE if force else Mode.SYNC

    if course_id is not None and (semester is not None or also_courses):
        return _error(
            "--semester and --also are only supported when COURSE_ID is omitted",
            json_output=json_output,
            payload=_single_error_payload(course_id, action="sync"),
        )

    if course_id is not None:
        try:
            org_id = resolve_course_id(client, course_id)
        except Exception as e:
            return _error(
                e,
                json_output=json_output,
                payload=_single_error_payload(course_id, action="sync"),
            )
        return _run_and_render_single(client, org_id, root, mode, "sync", types, json_output,
                                      include_assignments=include_assignments)

    scope = _resolve_course_scope(
        client,
        semester,
        also_courses,
        "sync",
        json_output=json_output,
    )
    if isinstance(scope, int):
        return scope
    course_ids, sem_name, sem_id, also_errors = scope
    return _run_and_render_multi(
        client, course_ids, root, mode, "sync", types,
        sem_id, sem_name, also_errors, json_output, include_assignments,
    )


# ---------------------------------------------------------------------------
# Scope resolution (multi-course)
# ---------------------------------------------------------------------------

def _resolve_semester(
    client: LighthouseClient,
    semester_filter: str | None,
    semester_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Resolve semester filter to a semester dict, or None if not found. Matches by OrgUnitId (numeric) or name substring."""
    if semester_records is None:
        semester_records = client.get_semesters()
    if not isinstance(semester_records, (list, tuple)):
        return None
    semesters: list[dict[str, Any]] = []
    for semester in semester_records:
        if not isinstance(semester, dict):
            continue
        semester_id = _positive_id(semester.get("OrgUnitId"))
        if semester_id is None:
            continue
        semesters.append(
            {
                "OrgUnitId": semester_id,
                "Name": _safe_server_text(semester.get("Name")),
                "Code": _safe_server_text(semester.get("Code")),
            }
        )
    if not semesters:
        return None

    if semester_filter is None:
        # Default: latest semester = highest OrgUnitId
        return max(semesters, key=lambda s: _positive_id(s.get("OrgUnitId")) or 0)

    # Try numeric OrgUnitId match
    with suppress(TypeError, ValueError):
        for s in semesters:
            if (_positive_id(s.get("OrgUnitId")) or 0) == int(semester_filter):
                return s

    # Try name substring match (case-insensitive)
    lower_filter = str(semester_filter).lower().strip()
    if exact := next((s for s in semesters if lower_filter == s["Name"].lower()), None):
        return exact
    if matches := [s for s in semesters if lower_filter in s["Name"].lower()]:
        return max(matches, key=lambda s: _positive_id(s.get("OrgUnitId")) or 0)


def _resolve_also_course(client: LighthouseClient, identifier: str) -> int:
    """Resolve an --also course identifier (name or numeric ID) to an OrgUnitId."""
    courses = get_enrolled_course_catalog(client)
    courses = [course for course in courses if isinstance(course, dict)]
    # Try numeric
    try:
        cid = int(identifier)
        if not any(_positive_id(c.get("OrgUnitId")) == cid for c in courses):
            raise CourseNotFoundError(
                f"Course '{identifier}' not found. Run: lighthouse courses"
            )
        return cid
    except ValueError:
        pass

    # Try name substring
    needle = str(identifier).lower()
    matches = [
        c for c in courses
        if isinstance(c.get("Name"), str) and needle in c["Name"].lower()
        and _positive_id(c.get("OrgUnitId")) is not None
    ]
    if len(matches) == 1:
        return _positive_id(matches[0]["OrgUnitId"]) or 0
    if len(matches) > 1:
        raise CourseNotFoundError(
            "Ambiguous match '" + identifier + "'. Multiple courses found:\n"
            + "\n".join(
                f"  {_positive_id(c.get('OrgUnitId'))} – "
                f"{_safe_server_text(c.get('Name'))}"
                for c in matches
            )
            + "\n\nUse the numeric OrgUnitId for an exact match."
        )
    raise CourseNotFoundError(
        f"Course '{identifier}' not found. Run: lighthouse courses"
    )


def _filter_courses_by_semester(
    enrollments: list[dict[str, Any]],
    semester: dict[str, Any],
    semester_filter: str | None = None,
    config: dict[str, dict[str, str]] | None = None,
) -> list[int]:
    """Filter enrollments to courses in a specific semester using course-config.json."""
    if config is None:
        config = _load_course_config()

    if not config:
        # Never widen a local write operation to every enrollment when the
        # semester mapping is absent.  Scope resolution fails closed before
        # this helper, and direct callers remain safe as well.
        return []

    # Determine the target semester label to match against config entries
    if semester_filter:
        # If the filter is a numeric OrgUnitId, the resolved semester's Name
        # is the authoritative source — use substring matching against config
        # labels (same as the no-filter path).
        try:
            int(semester_filter)
            # Numeric filter — use resolved semester Name
            target_lower = None
        except ValueError:
            # Text filter — compare directly against config labels
            target_lower = semester_filter.lower().strip()
    else:
        # No filter (latest semester) — use the API semester Name for
        # substring matching against config labels, so "AY 2024-25 | Sem II"
        # matches a config label of "Sem II".
        target_lower = None

    sem_name = (
        _safe_server_text(semester.get("Name"))
        if isinstance(semester, dict)
        else ""
    )
    sem_segments = [s.strip() for s in sem_name.lower().split("|")] if target_lower is None else []
    return [
        oid for e in enrollments
        if isinstance(e, dict)
        and isinstance(e.get("OrgUnit"), dict)
        and (oid := _positive_id(e["OrgUnit"].get("Id"))) is not None
        and (entry := config.get(str(oid)))
        and isinstance(entry, dict)
        and isinstance(entry.get("semester"), str)
        and (sem_label := entry.get("semester", "").lower().strip())
        and (sem_label == target_lower if target_lower is not None else sem_label in sem_segments)
    ]


def _resolve_course_scope(
    client: LighthouseClient,
    semester_filter: str | None,
    also_courses: list[str],
    action_label: str = "download",
    *,
    json_output: bool = False,
) -> tuple[list[int], str, int, list[str]] | int:
    """Resolve course scope for multi-course ops. Returns (ids, sem_name, sem_id, errors) or int exit code."""
    try:
        config = _load_course_config()
    except Exception as e:
        return _error(e, json_output=json_output, payload=_scope_error_payload())

    # A multi-course write must be bounded by a local semester mapping.  The
    # loader normalizes disk input, but callers/tests can inject arbitrary
    # mappings, so validate positive IDs and non-empty semester labels again.
    trusted_config: dict[str, dict[str, str]] = {}
    if isinstance(config, dict):
        for raw_id, entry in config.items():
            oid = _positive_id(raw_id)
            if oid is None or not isinstance(entry, dict):
                continue
            semester_label = entry.get("semester")
            if not isinstance(semester_label, str) or not semester_label.strip():
                continue
            raw_name = entry.get("name", "")
            trusted_config[str(oid)] = {
                "name": raw_name if isinstance(raw_name, str) else "",
                "semester": semester_label.strip(),
            }

    if not trusted_config:
        return _error(
            "No trustworthy local semester configuration found. Use an explicit "
            "COURSE_ID or run: lighthouse config courses.",
            json_output=json_output,
            payload=_scope_error_payload(),
        )

    try:
        semesters = client.get_semesters()
        enrollments = client.get_course_enrollments()
    except Exception as e:
        return _error(e, json_output=json_output, payload=_scope_error_payload())

    if not isinstance(semesters, (list, tuple)) or not semesters:
        return _error("No semesters found.", json_output=json_output, payload=_scope_error_payload())
    if not isinstance(enrollments, (list, tuple)):
        return _error(
            "Invalid course enrollment response.",
            json_output=json_output,
            payload=_scope_error_payload(),
        )

    # Pass the already-fetched records so one invocation cannot make a second
    # API request or observe a different semester snapshot.
    if (sem := _resolve_semester(client, semester_filter, list(semesters))) is None:
        return _error(
            f"No semester matching '{semester_filter}'. Run: lighthouse semesters"
            if semester_filter else "No semesters found.",
            json_output=json_output,
            payload=_scope_error_payload(),
        )

    semester_course_ids = sorted(set(_filter_courses_by_semester(
        list(enrollments), sem, semester_filter=semester_filter, config=trusted_config,
    )))

    also_errors, also_ids = [], []
    for ident in also_courses:
        try:
            also_ids.append(_resolve_also_course(client, ident))
        except CourseNotFoundError as e:
            also_errors.append(str(e))
        except Exception as e:
            return _error(e, json_output=json_output, payload=_scope_error_payload())

    all_course_ids = list(semester_course_ids)
    seen_course_ids = set(semester_course_ids)
    for cid in also_ids:
        if cid not in seen_course_ids:
            all_course_ids.append(cid)
            seen_course_ids.add(cid)

    if not all_course_ids:
        return _error(
            f"No courses to {action_label}.",
            json_output=json_output,
            payload=_scope_error_payload(),
        )

    sem_id = _positive_id(sem.get("OrgUnitId"))
    if sem_id is None:
        return _error(
            "Resolved semester has an invalid identifier.",
            json_output=json_output,
            payload=_scope_error_payload(),
        )
    sem_name = _safe_server_text(sem.get("Name")) or "Unknown Semester"
    return all_course_ids, sem_name, sem_id, also_errors


# ---------------------------------------------------------------------------
# Read-only commands
# ---------------------------------------------------------------------------

def cmd_auth_status(json_output: bool = False) -> int:
    """Check if stored cookies are valid."""
    try:
        client = LighthouseClient()
        cookies = client.cookies
    except Exception as e:
        return _error(
            e,
            json_output=json_output,
            payload={"valid": False},
        )
    if not cookies:
        return _error(
            "No cookies found. Run: lighthouse auth login",
            json_output=json_output,
            payload={"valid": False},
        )

    try:
        valid = client.check_auth()
    except Exception as e:
        return _error(
            e,
            json_output=json_output,
            payload={"valid": False},
        )
    if valid:
        if json_output:
            _output_json({"valid": True, "cookies": list(cookies.keys())})
            return 0
        print(f"Session valid. Cookies: {', '.join(cookies.keys())}")
        warn_if_cookies_stale()
        return 0
    return _error(
        "Session expired. Run: lighthouse auth login",
        json_output=json_output,
        payload={"valid": False, "cookies": list(cookies.keys())},
    )


def cmd_semesters(json_output: bool = False) -> int:
    """List all semesters."""
    try:
        client = LighthouseClient()
        semesters = client.get_semesters()
    except Exception as e:
        return _error(
            e,
            json_output=json_output,
            payload={"semesters": []},
        )

    if not isinstance(semesters, list):
        return _error(
            "Invalid semester response.",
            json_output=json_output,
            payload={"semesters": []},
        )
    semesters = _normalise_semester_records(semesters)

    if json_output:
        _output_json(semesters)
        return 0

    _print_table(
        ["ID", "Name", "Code"],
        [[str(s["OrgUnitId"]), s["Name"], s["Code"]] for s in semesters],
        title="Semesters",
    )
    return 0


def cmd_courses(
    semester: str | None = None,
    json_output: bool = False,
    tracked_only: bool = False,
) -> int:
    """List courses, optionally filtered by semester or tracked status."""
    try:
        client = LighthouseClient()
        enrolled_courses = get_enrolled_course_catalog(client)
    except Exception as e:
        return _error(
            e,
            json_output=json_output,
            payload=_course_list_error_payload(),
        )

    if not isinstance(enrolled_courses, list):
        return _error(
            "Invalid course enrollment response.",
            json_output=json_output,
            payload=_course_list_error_payload(),
        )

    try:
        config = _load_course_config()
        if not isinstance(config, dict):
            config = {}
    except Exception as e:
        return _error(
            e,
            json_output=json_output,
            payload=_course_list_error_payload(),
        )
    courses = []
    for enrolled_course in enrolled_courses:
        if not isinstance(enrolled_course, dict):
            continue
        org_id = _positive_id(enrolled_course.get("OrgUnitId"))
        if org_id is None:
            continue
        configured = config.get(str(org_id)) or {}
        if not isinstance(configured, dict):
            configured = {}
        # Keep the historical empty-string ``semester`` field for callers that
        # already consume it.  The explicit fields make it impossible for a
        # consumer to mistake an unmapped course for an inferred semester.
        courses.append({
            "OrgUnitId": org_id,
            "Name": _safe_server_text(enrolled_course.get("Name")),
            "Code": _safe_server_text(enrolled_course.get("Code")),
            "IsActive": _coerce_boolish(enrolled_course.get("IsActive", True), default=True),
            **_semester_state(configured),
        })

    if (tracked_only or semester) and not config:
        return _error(
            "No course config found. Run: lighthouse config courses",
            json_output=json_output,
            payload=_course_list_error_payload(),
        )
    if tracked_only:
        courses = [c for c in courses if str(c.get("OrgUnitId", "")) in config]
    if semester:
        if not (courses := [
            c for c in courses
            if c.get("semester", "").lower().strip() == semester.lower().strip()
        ]):
            return _error(
                f"No tracked courses mapped to semester '{semester}'.\n"
                "Run: lighthouse config courses --list to see your mappings.",
                json_output=json_output,
                payload=_course_list_error_payload(),
            )

    if json_output:
        _output_json(courses)
        return 0

    _print_table(["ID", "Name", "Semester", "Active"], [
        [str(c.get("OrgUnitId", "")), _short(c.get("Name", ""), 40), c.get("semester", "").strip() or "Unmapped", "Y" if c.get("IsActive") else "N"]
        for c in courses
    ], title=f"Courses ({len(courses)})")
    return 0

def cmd_content(course_id: str, json_output: bool = False) -> int:
    """Show content tree for a course."""
    try:
        client = LighthouseClient()
        org_id = resolve_course_id(client, course_id)
        toc = client.get_content_toc(org_id)
    except Exception as e:
        return _error(
            e,
            json_output=json_output,
            payload={"course_id": _course_identifier(course_id), "modules": []},
        )

    if not isinstance(toc, dict):
        return _error(
            "Invalid content response.",
            json_output=json_output,
            payload={"course_id": org_id, "modules": []},
        )
    modules = toc.get("Modules", [])
    if not isinstance(modules, list):
        return _error(
            "Invalid content response.",
            json_output=json_output,
            payload={"course_id": org_id, "modules": []},
        )

    if json_output:
        try:
            safe_modules = _normalise_content_modules(modules)
            _output_json({"course_id": org_id, "modules": safe_modules})
        except Exception as e:
            # The projection above is deliberately iterative, but retain the
            # command's one-document JSON contract if an unexpected encoder or
            # custom mapping failure is introduced in the future.
            return _error(
                e,
                json_output=True,
                payload={"course_id": _course_identifier(org_id), "modules": []},
            )
        return 0

    try:
        items = _walk_content_tree(modules)
    except Exception as e:
        return _error(
            e,
            payload={"course_id": _course_identifier(org_id), "modules": []},
        )
    if not items:
        print("No content found for this course.")
        return 0

    for item in items:
        indent = "  " * item["depth"]
        if item["type"] == "module":
            print(f"{indent}📁 {item['title']}")
        elif item["type"] == "topic":
            icon = {"File": "📄", "Link": "🔗"}.get(item.get("topic_type", ""), "📎")
            print(f"{indent}{icon} {item['title']}  [id:{item.get('id', '')}]")
        else:
            print(f"{indent}… {item['title']}")
    return 0


def _walk_content_tree(modules: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Flatten the nested content TOC into a list of display records.

    Each record: ``{depth, type, id, title, url}``.  The input is an
    untrusted API response, so this walk is iterative, bounded, and projects
    every field before it reaches a terminal renderer.
    """
    items: list[dict[str, Any]] = []
    if not isinstance(modules, list):
        return items

    start_depth = depth if isinstance(depth, int) and not isinstance(depth, bool) else 0
    start_depth = max(0, min(start_depth, _CONTENT_MAX_DEPTH))
    stack: list[tuple[str, Any, int]] = [
        ("module", module, start_depth)
        for module in reversed(modules)
    ]
    seen_modules: set[int] = set()
    truncated = False

    def append_truncation(marker_depth: int) -> None:
        nonlocal truncated
        if not truncated and len(items) < _CONTENT_MAX_NODES:
            items.append({
                "depth": marker_depth,
                "type": "truncated",
                "id": None,
                "title": _CONTENT_TRUNCATED_TITLE,
                "url": None,
            })
            truncated = True

    while stack:
        kind, current, current_depth = stack.pop()
        if kind == "topics":
            if not isinstance(current, list):
                continue
            for topic in reversed(current):
                stack.append(("topic", topic, current_depth))
            continue
        if kind == "topic":
            if not isinstance(current, dict):
                continue
            if len(items) >= _CONTENT_MAX_NODES - 1:
                append_truncation(current_depth)
                stack.clear()
                continue
            items.append({
                "depth": current_depth,
                "type": "topic",
                "id": _safe_content_id(current.get("TopicId")),
                "title": _safe_server_text(current.get("Title")),
                "url": _safe_content_url(current.get("Url")),
                "topic_type": _safe_server_text(
                    current.get("TypeIdentifier"), max_len=64,
                ),
            })
            continue
        if not isinstance(current, dict):
            continue
        module_key = id(current)
        if module_key in seen_modules:
            continue
        seen_modules.add(module_key)
        if len(items) >= _CONTENT_MAX_NODES - 1:
            append_truncation(current_depth)
            stack.clear()
            continue

        items.append({
            "depth": current_depth,
            "type": "module",
            "id": _safe_content_id(current.get("ModuleId")),
            "title": _safe_server_text(current.get("Title")),
            "url": None,
        })

        topics = current.get("Topics", [])
        if isinstance(topics, list):
            stack.append(("topics", topics, current_depth + 1))

        child_modules = current.get("Modules", [])
        if isinstance(child_modules, list):
            if current_depth >= _CONTENT_MAX_DEPTH and child_modules:
                append_truncation(current_depth + 1)
            else:
                for child in reversed(child_modules):
                    stack.append(("module", child, current_depth + 1))
    return items


def _safe_quiz_rich_text(value: Any) -> str:
    """Extract bounded RichText iteratively, preferring safe HTML/text data."""
    pending: list[tuple[Any, int]] = [(value, 0)]
    seen: set[int] = set()
    while pending:
        current, depth = pending.pop()
        if isinstance(current, str):
            if len(current) > _QUIZ_RICH_TEXT_MAX_TEXT:
                continue
            # Keep the historical HTML stripping behavior, but normalize the
            # resulting text before it reaches the terminal.
            stripped = " ".join(re.sub(r"<[^>]+>", "", current).split())
            safe_text = _safe_server_text(
                stripped,
                max_len=_QUIZ_RICH_TEXT_MAX_TEXT,
            )
            if safe_text:
                return safe_text
            continue
        if not isinstance(current, dict) or depth >= _QUIZ_RICH_TEXT_MAX_DEPTH:
            continue
        current_key = id(current)
        if current_key in seen:
            continue
        seen.add(current_key)
        # Push Text before Html so a valid HTML sibling is preferred while a
        # malformed/deep HTML branch can still fall back to valid Text.
        for key in ("Text", "Html"):
            child = current.get(key)
            if isinstance(child, (dict, str)):
                pending.append((child, depth + 1))
    return ""


def _safe_quiz_date(value: Any) -> str | None:
    """Return a bounded printable quiz date or ``None``."""
    safe_value = _safe_server_text(value, max_len=128)
    return safe_value or None


def _safe_quiz_scalar(value: Any, *, fallback: str = "?") -> str:
    """Render a scalar quiz value without interpolating nested objects."""
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(value)
    safe_value = _safe_server_text(value, max_len=64)
    return safe_value or fallback


def _safe_quiz_json_scalar(value: Any) -> int | float | str | None:
    """Keep a finite scalar for the allowlisted quiz JSON projection."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    safe_value = _safe_server_text(value, max_len=64)
    return safe_value or None


def _normalise_quiz_payload(quiz: dict[str, Any]) -> dict[str, Any]:
    """Project quiz JSON onto bounded scalar fields and safe RichText."""
    payload: dict[str, Any] = {
        "QuizId": _safe_content_id(quiz.get("QuizId")),
        "Name": _safe_server_text(quiz.get("Name"), fallback="Quiz") or "Quiz",
    }
    for key in (
        "IsActive",
        "Shuffle",
        "PreventMovingBackwards",
        "IsSingleSession",
        "AllowHints",
        "AutoExportToGrades",
    ):
        payload[key] = _coerce_boolish(quiz.get(key))
    for key in ("StartDate", "EndDate", "DueDate"):
        payload[key] = _safe_quiz_date(quiz.get(key))

    attempts = quiz.get("AttemptsAllowed")
    if not isinstance(attempts, dict):
        attempts = {}
    attempts_payload: dict[str, Any] = {
        "IsUnlimited": _coerce_boolish(attempts.get("IsUnlimited")),
    }
    if (number := _safe_quiz_json_scalar(attempts.get("NumberOfAttemptsAllowed"))) is not None:
        attempts_payload["NumberOfAttemptsAllowed"] = number
    payload["AttemptsAllowed"] = attempts_payload

    time_limit = quiz.get("SubmissionTimeLimit")
    if not isinstance(time_limit, dict):
        time_limit = {}
    time_payload: dict[str, Any] = {
        "IsEnforced": _coerce_boolish(time_limit.get("IsEnforced")),
    }
    if (time_value := _safe_quiz_json_scalar(time_limit.get("TimeLimitValue"))) is not None:
        time_payload["TimeLimitValue"] = time_value
    payload["SubmissionTimeLimit"] = time_payload

    for key in ("Description", "Instructions"):
        if key in quiz:
            payload[key] = _safe_quiz_rich_text(quiz.get(key))
    return payload


def cmd_quiz_detail(course_id: str, quiz_id: int, json_output: bool = False) -> int:
    """Show detailed info for a specific quiz."""
    try:
        client = LighthouseClient()
        org_id = resolve_course_id(client, course_id)
        quiz = client.get_quiz_detail(org_id, quiz_id)
    except Exception as e:
        return _error(
            e,
            json_output=json_output,
            payload={"course_id": _course_identifier(course_id), "quiz": {}},
        )

    if not isinstance(quiz, dict):
        return _error(
            "Invalid quiz response.",
            json_output=json_output,
            payload={"course_id": org_id, "quiz": {}},
        )

    if json_output:
        try:
            _output_json({"course_id": org_id, "quiz": _normalise_quiz_payload(quiz)})
        except Exception as e:
            return _error(
                e,
                json_output=True,
                payload={"course_id": _course_identifier(org_id), "quiz": {}},
            )
        return 0

    time_limit = quiz.get("SubmissionTimeLimit", {})
    if not isinstance(time_limit, dict):
        time_limit = {}

    quiz_name = _safe_server_text(quiz.get("Name"), fallback="Quiz") or "Quiz"
    quiz_identifier = _safe_content_id(quiz.get("QuizId"))
    desc_text = _safe_quiz_rich_text(quiz.get("Description", {}))
    instr_text = _safe_quiz_rich_text(quiz.get("Instructions", {}))

    print(f"\n📝 {quiz_name}\n   ID: {quiz_identifier if quiz_identifier is not None else '?'}")
    for label, key in [("Active", "IsActive"), ("Shuffle Questions", "Shuffle"), ("Prevent Moving Back", "PreventMovingBackwards"), ("Single Session", "IsSingleSession"), ("Allow Hints", "AllowHints"), ("Auto-export to Grades", "AutoExportToGrades")]:
        print(f"   {label}: {'Yes' if _coerce_boolish(quiz.get(key)) else 'No'}")
    for label, key in [("Start", "StartDate"), ("End", "EndDate"), ("Due", "DueDate")]:
        print(f"   {label}: {_fmt_date(_safe_quiz_date(quiz.get(key)))}")
    attempts = quiz.get("AttemptsAllowed", {})
    if not isinstance(attempts, dict):
        attempts = {}
    attempts_text = (
        "Unlimited"
        if _coerce_boolish(attempts.get("IsUnlimited"))
        else _safe_quiz_scalar(attempts.get("NumberOfAttemptsAllowed"))
    )
    print(f"   Attempts: {attempts_text}")
    time_text = _safe_quiz_scalar(time_limit.get("TimeLimitValue"))
    print(
        f"   Time Limit: {time_text} min"
        if _coerce_boolish(time_limit.get("IsEnforced"))
        else "   Time Limit: None"
    )
    if desc_text:
        print(f"\n   Description: {_short(desc_text, 200)}")
    if instr_text:
        print(f"   Instructions: {_short(instr_text, 200)}")

    print("\n   ⚠ Quiz questions and past attempts require instructor-level API access.")
    print(f"   View in browser: {BASE_URL}/d2l/lms/quizzing/user/quizzes_list.d2l?ou={org_id}")
    return 0
