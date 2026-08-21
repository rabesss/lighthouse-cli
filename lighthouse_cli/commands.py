"""Command implementations — thin orchestration layer delegating to domain modules."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from contextlib import suppress
from typing import Any

from .api import CourseNotFoundError, LighthouseClient, resolve_course_id
from .config import BASE_URL, DEFAULT_DOWNLOAD_DIR, warn_if_cookies_stale
from .display import error as _error, output_json as _output_json, print_table as _print_table, short as _short, fmt_date as _fmt_date, utc_now_iso as _utc_now_iso
from .course_config import load as _load_course_config
from .sync_engine import Mode, run_course
from .assignments import download_single_attachment as _download_single_attachment
from .submit import cmd_submit  # noqa: F401 — re-export
from .show import cmd_grades, cmd_announcements, cmd_calendar, cmd_assignments, cmd_quizzes  # noqa: F401 — re-export


def _output_multi_course_json(sem_id: int, sem_name: str, courses_results: list[dict], also_errors: list[str]) -> None:
    _output_json({
        "semester": {"id": sem_id, "name": sem_name},
        "synced_at": _utc_now_iso(),
        "summary": {"courses_checked": len(courses_results),
                    **{k: sum(len(c.get(k, [])) for c in courses_results) for k in (
                        "downloaded", "skipped", "updated", "duplicates", "errors",
                        "assignments_downloaded", "assignment_errors",
                    )}},
        "courses": courses_results, "also_errors": also_errors,
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
    return {"topic_id": entry["topic_id"], "filename": entry["filename"], "size": entry["size"], "path": entry["path"]}


def _pipeline_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Project an engine entry onto the sync/multi schema (no raw byte size)."""
    projected = {k: entry[k] for k in ("topic_id", "filename", "path", "size_kb", "sha256") if k in entry}
    if "extension" in entry:
        projected["extension"] = entry["extension"]
    return projected


def _single_error(error: dict[str, Any]) -> dict[str, Any]:
    """Project an engine error onto the single-course schema ({topic_id, error})."""
    return {k: error[k] for k in ("topic_id", "error") if k in error}


def _single_course_json(result: dict[str, Any], *, action: str, include_assignments: bool) -> Any:
    """Project one engine result into the single-course JSON schema."""
    if result["mode"] is Mode.PLAN:
        return result["planned"]
    if result["empty"]:
        if action == "sync":
            return {"course_id": result["org_id"], "course_name": result["course_name"],
                    "downloaded": [], "skipped": [], "updated": [], "orphaned": [],
                    "errors": [_single_error(e) for e in result["errors"]]}
        return {"course_id": result["org_id"], "files": [], "downloaded": 0,
                "errors": len(result["errors"])}

    assignments = result["assignments"]
    data: dict[str, Any] = {"course_id": result["org_id"], "course_name": result["course_name"], "folder": str(result["dest"])}
    if action == "sync":
        data.update(
            downloaded=[_pipeline_entry(e) for e in result["downloaded"]],
            skipped=[_pipeline_entry(e) for e in result["skipped"]],
            updated=[_pipeline_entry(e) for e in result["updated"]],
            orphaned=[_pipeline_entry(e) for e in result["orphaned"]],
            errors=[_single_error(e) for e in result["errors"]],
        )
        if include_assignments:
            data.update(assignments_downloaded=assignments["downloaded"], assignments_skipped=assignments["skipped"],
                        assignments_updated=assignments["updated"], assignment_errors=assignments["errors"])
    else:
        data.update(
            manifest=str(result["manifest_path"]),
            downloaded=[_single_entry(e) for e in result["downloaded"]],
            errors=[_single_error(e) for e in result["errors"]],
        )
        if include_assignments:
            data.update(assignments_downloaded=assignments["downloaded"], assignment_errors=assignments["errors"])
    return data


def _multi_course_json(result: dict[str, Any], *, sem_name: str, action: str) -> dict[str, Any]:
    """Project one engine result into the multi-course per-course JSON schema."""
    assignments = result["assignments"]
    course: dict[str, Any] = {
        "course_id": result["org_id"], "course_name": result["course_name"],
        "semester": sem_name, "root": str(result["dest"]),
        "manifest_total": result["manifest_total"],
        "downloaded": [_pipeline_entry(e) for e in result["downloaded"]],
        "skipped": [_pipeline_entry(e) for e in result["skipped"]],
        "updated": [_pipeline_entry(e) for e in result["updated"]],
    }
    if action == "sync":
        course["orphaned"] = [_pipeline_entry(e) for e in result["orphaned"]]
    course["duplicates"] = result["duplicates"]
    course["errors"] = result["errors"]
    if action == "sync":
        course.update(assignments_downloaded=assignments["downloaded"], assignments_skipped=assignments["skipped"],
                      assignments_updated=assignments["updated"], assignment_errors=assignments["errors"])
    else:
        course.update(assignments_downloaded=assignments["downloaded"], assignment_errors=assignments["errors"])
    return course


def _render_course_human(result: dict[str, Any], *, action: str, include_assignments: bool) -> int:
    """Render one engine result as human-readable text. Returns per-course exit code."""
    if result["mode"] is Mode.PLAN:
        print(f"Would download {result['topic_count']} files to {result['dest']}/\n")
        print("\n".join(f"  [{t['topic_id']}] {t['title']}" for t in result["planned"]))
        if include_assignments:
            print("\n  (Assignment downloads not shown in dry-run)")
        return 0
    if result["empty"]:
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
        print(f"Synced {result['course_name']}: {', '.join(parts)}")
        return failed

    for i, entry in enumerate(result["downloaded"], 1):
        print(f"  [{i}/{result['topic_count']}] {entry['path']} ({entry['size'] / 1024:.0f} KB)")
    for error in result["errors"]:
        if "topic_id" in error:
            print(f"  FAILED topic {error['topic_id']}: {error['error']}", file=sys.stderr)
    if assignments["downloaded"]:
        print(f"\nAssignments: {len(assignments['downloaded'])} attachment(s) downloaded")
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
) -> int:
    """Run one course through the sync engine and render it. Returns exit code."""
    try:
        result = run_course(
            client, org_id, root, mode=mode, types=types,
            include_assignments=include_assignments, assignment_id=assignment_id,
        )
    except Exception as e:
        return _error(str(e))

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
    rc = 0
    for cid in course_ids:
        try:
            results.append(run_course(client, cid, root, mode=mode, types=types, include_assignments=include_assignments))
        except Exception as e:
            rc = _error(str(e))

    if json_output:
        courses = []
        for result in results:
            # Engine warnings reach stderr in every mode — an unknown
            # --types value must never change the downloaded set silently.
            _print_warnings(result)
            if result["mode"] is Mode.PLAN:
                courses.append({
                    "course_id": result["org_id"], "course_name": result["course_name"],
                    "semester": sem_name, "root": str(result["dest"]), "manifest_total": 0,
                    "downloaded": [], "skipped": [], "updated": [], "duplicates": [], "errors": [],
                })
                continue
            courses.append(_multi_course_json(result, sem_name=sem_name, action=action))
            if result["errors"] or result["assignments"]["errors"]:
                rc = 1
        _output_multi_course_json(sem_id, sem_name, courses, also_errors)
        return rc

    print(f"{'Syncing' if action == 'sync' else 'Downloading'} courses from {sem_name}...\n")
    for result in results:
        _print_warnings(result)
        if _render_course_human(result, action=action, include_assignments=include_assignments) != 0:
            rc = 1
    if also_errors:
        print("\n".join(f"  Error: {err}" for err in also_errors), file=sys.stderr)
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
    client = LighthouseClient()
    root = Path(output_dir).expanduser().resolve() if output_dir else DEFAULT_DOWNLOAD_DIR
    also_courses = also_courses or []
    mode = Mode.PLAN if dry_run else (Mode.FORCE if force else Mode.DOWNLOAD)

    if assignment_id is not None and attachment_id is not None and course_id is None:
        return _error("COURSE_ID is required when using --assignment and --attachment")

    if course_id is not None:
        try:
            org_id = resolve_course_id(client, course_id)
        except Exception as e:
            return _error(str(e))
        if assignment_id is not None and attachment_id is not None:
            return _download_single_attachment(client, org_id, assignment_id, attachment_id, root, json_output)
        return _run_and_render_single(
            client, org_id, root, mode, "download", types, json_output,
            include_assignments=include_assignments, assignment_id=assignment_id,
        )

    scope = _resolve_course_scope(client, semester, also_courses, "download")
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
    client = LighthouseClient()
    root = Path(output_dir).expanduser().resolve() if output_dir else DEFAULT_DOWNLOAD_DIR
    also_courses = also_courses or []
    mode = Mode.FORCE if force else Mode.SYNC

    if course_id is not None:
        try:
            org_id = resolve_course_id(client, course_id)
        except Exception as e:
            return _error(str(e))
        return _run_and_render_single(client, org_id, root, mode, "sync", types, json_output,
                                      include_assignments=include_assignments)

    scope = _resolve_course_scope(client, semester, also_courses, "sync")
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
) -> dict[str, Any] | None:
    """Resolve semester filter to a semester dict, or None if not found. Matches by OrgUnitId (numeric) or name substring."""
    if not (semesters := client.get_semesters()):
        return None

    if semester_filter is None:
        # Default: latest semester = highest OrgUnitId
        return max(semesters, key=lambda s: int(s.get("OrgUnitId", 0)))

    # Try numeric OrgUnitId match
    with suppress(ValueError):
        for s in semesters:
            if int(s.get("OrgUnitId", 0)) == int(semester_filter):
                return s

    # Try name substring match (case-insensitive)
    lower_filter = semester_filter.lower().strip()
    if exact := next((s for s in semesters if lower_filter == s.get("Name", "").lower()), None):
        return exact
    if matches := [s for s in semesters if lower_filter in s.get("Name", "").lower()]:
        return max(matches, key=lambda s: int(s.get("OrgUnitId", 0)))


def _resolve_also_course(client: LighthouseClient, identifier: str) -> int:
    """Resolve an --also course identifier (name or numeric ID) to an OrgUnitId."""
    courses = client.get_courses()
    # Try numeric
    try:
        cid = int(identifier)
        if not any(int(c.get("OrgUnitId", 0)) == cid for c in courses):
            raise CourseNotFoundError(
                f"Course '{identifier}' not found. Run: lighthouse courses"
            )
        return cid
    except ValueError:
        pass

    # Try name substring
    matches = [c for c in courses if identifier.lower() in c.get("Name", "").lower()]
    if len(matches) == 1:
        return int(matches[0]["OrgUnitId"])
    if len(matches) > 1:
        raise CourseNotFoundError(
            "Ambiguous match '" + identifier + "'. Multiple courses found:\n"
            + "\n".join(f"  {c['OrgUnitId']} – {c['Name']}" for c in matches)
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
        # No config — fall back to all enrolled courses
        return [
            int(e.get("OrgUnit", {}).get("Id", 0))
            for e in enrollments
            if int(e.get("OrgUnit", {}).get("Id", 0)) > 0
        ]

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

    sem_segments = [s.strip() for s in semester.get("Name", "").lower().strip().split("|")] if target_lower is None else []
    return [
        oid for e in enrollments
        if (oid := int(e.get("OrgUnit", {}).get("Id", 0))) > 0
        and (entry := config.get(str(oid)))
        and (sem_label := entry.get("semester", "").lower().strip())
        and (sem_label == target_lower if target_lower is not None else sem_label in sem_segments)
    ]


def _resolve_course_scope(
    client: LighthouseClient,
    semester_filter: str | None,
    also_courses: list[str],
    action_label: str = "download",
) -> tuple[list[int], str, int, list[str]] | int:
    """Resolve course scope for multi-course ops. Returns (ids, sem_name, sem_id, errors) or int exit code."""
    try:
        semesters = client.get_semesters()
        enrollments = client.get_course_enrollments()
    except Exception as e:
        return _error(str(e))

    if not semesters:
        return _error("No semesters found.")

    if (sem := _resolve_semester(client, semester_filter)) is None:
        return _error(
            f"No semester matching '{semester_filter}'. Run: lighthouse semesters"
            if semester_filter else "No semesters found."
        )



    if not (config := _load_course_config()):
        print("Warning: No course config found. All courses will be included.\nRun: lighthouse config courses to set up tracking.", file=sys.stderr)
    semester_course_ids = set(_filter_courses_by_semester(
        enrollments, sem, semester_filter=semester_filter, config=config or None,
    ))

    also_errors, also_ids = [], []
    for ident in also_courses:
        try:
            also_ids.append(_resolve_also_course(client, ident))
        except CourseNotFoundError as e:
            also_errors.append(str(e))

    if not (all_course_ids := list(semester_course_ids) + [cid for cid in also_ids if cid not in semester_course_ids]):
        return _error(f"No courses to {action_label}.")

    return all_course_ids, sem.get("Name", "Unknown Semester"), int(sem["OrgUnitId"]), also_errors


# ---------------------------------------------------------------------------
# Read-only commands
# ---------------------------------------------------------------------------

def cmd_auth_status(json_output: bool = False) -> int:
    """Check if stored cookies are valid."""
    client = LighthouseClient()
    cookies = client.cookies
    if not cookies:
        if json_output:
            _output_json({"valid": False, "error": "No cookies found. Run: lighthouse auth login"})
            return 1
        return _error("No cookies found. Run: lighthouse auth login")

    valid = client.check_auth()
    if json_output:
        _output_json({"valid": valid, "cookies": list(cookies.keys())})
        return 0

    if valid:
        print(f"Session valid. Cookies: {', '.join(cookies.keys())}")
        warn_if_cookies_stale()
        return 0
    return _error("Session expired. Run: lighthouse auth login")


def cmd_semesters(json_output: bool = False) -> int:
    """List all semesters."""
    client = LighthouseClient()
    try:
        semesters = client.get_semesters()
    except Exception as e:
        return _error(str(e))

    if json_output:
        _output_json(semesters)
        return 0

    _print_table(["ID", "Name", "Code"], [[s.get("OrgUnitId", ""), s.get("Name", ""), s.get("Code", "")] for s in semesters], title="Semesters")
    return 0


def cmd_courses(
    semester: str | None = None,
    json_output: bool = False,
    tracked_only: bool = False,
) -> int:
    """List courses, optionally filtered by semester or tracked status."""
    client = LighthouseClient()
    try:
        all_enrollments = client.get_course_enrollments()
    except Exception as e:
        return _error(str(e))

    config = _load_course_config()
    courses = [
        {
            "OrgUnitId": int(e["OrgUnit"]["Id"]), "Name": e["OrgUnit"].get("Name", ""),
            "Code": e["OrgUnit"].get("Code", ""),
            "IsActive": e.get("Access", {}).get("IsActive", True),
            "semester": (config.get(str(int(e["OrgUnit"]["Id"]))) or {}).get("semester", ""),
        }
        for e in all_enrollments
    ]

    if (tracked_only or semester) and not config:
        return _error("No course config found. Run: lighthouse config courses")
    if tracked_only:
        courses = [c for c in courses if str(c.get("OrgUnitId", "")) in config]
    if semester:
        if not (courses := [
            c for c in courses
            if c.get("semester", "").lower().strip() == semester.lower().strip()
        ]):
            return _error(
                f"No tracked courses mapped to semester '{semester}'.\n"
                "Run: lighthouse config courses --list to see your mappings."
            )

    if json_output:
        _output_json(courses)
        return 0

    _print_table(["ID", "Name", "Semester", "Active"], [
        [str(c.get("OrgUnitId", "")), _short(c.get("Name", ""), 40), c.get("semester", ""), "Y" if c.get("IsActive") else "N"]
        for c in courses
    ], title=f"Courses ({len(courses)})")
    return 0

def cmd_content(course_id: str, json_output: bool = False) -> int:
    """Show content tree for a course."""
    client = LighthouseClient()
    try:
        org_id = resolve_course_id(client, course_id)
        toc = client.get_content_toc(org_id)
    except Exception as e:
        return _error(str(e))

    modules = toc.get("Modules", [])

    if json_output:
        _output_json({"course_id": org_id, "modules": modules})
        return 0

    if not (items := _walk_content_tree(modules)):
        print("No content found for this course.")
        return 0

    for item in items:
        indent = "  " * item["depth"]
        if item["type"] == "module":
            print(f"{indent}📁 {item['title']}")
        else:
            icon = {"File": "📄", "Link": "🔗"}.get(item.get("topic_type", ""), "📎")
            print(f"{indent}{icon} {item['title']}  [id:{item.get('id', '')}]")
    return 0


def _walk_content_tree(modules: list[dict], depth: int = 0) -> list[dict[str, Any]]:
    """Flatten the nested content TOC into a list of display records.

    Each record: {depth, type, id, title, url}
    """
    items: list[dict[str, Any]] = []
    for mod in modules:
        items.append({
            "depth": depth, "type": "module",
            "id": mod.get("ModuleId"), "title": mod.get("Title", ""),
            "url": None,
        })
        items.extend(_walk_content_tree(mod.get("Modules", []), depth + 1))
        for topic in mod.get("Topics", []):
            items.append({
                "depth": depth + 1, "type": "topic",
                "id": topic.get("TopicId"), "title": topic.get("Title", ""),
                "url": topic.get("Url"), "topic_type": topic.get("TypeIdentifier", ""),
            })
    return items


def cmd_quiz_detail(course_id: str, quiz_id: int, json_output: bool = False) -> int:
    """Show detailed info for a specific quiz."""
    client = LighthouseClient()
    try:
        org_id = resolve_course_id(client, course_id)
        quiz = client.get_quiz_detail(org_id, quiz_id)
    except Exception as e:
        return _error(str(e))

    if json_output:
        _output_json({"course_id": org_id, "quiz": quiz})
        return 0

    time_limit = quiz.get("SubmissionTimeLimit", {})

    def _strip_html(field: dict) -> str:
        text = field.get("Text", {})
        raw = text.get("Html", "") or text.get("Text", "") if isinstance(text, dict) else ""
        return re.sub(r'<[^>]+>', '', raw).strip() if raw else ""

    desc_text = _strip_html(quiz.get("Description", {}))
    instr_text = _strip_html(quiz.get("Instructions", {}))

    print(f"\n📝 {quiz.get('Name', 'Quiz')}\n   ID: {quiz.get('QuizId')}")
    for label, key in [("Active", "IsActive"), ("Shuffle Questions", "Shuffle"), ("Prevent Moving Back", "PreventMovingBackwards"), ("Single Session", "IsSingleSession"), ("Allow Hints", "AllowHints"), ("Auto-export to Grades", "AutoExportToGrades")]:
        print(f"   {label}: {'Yes' if quiz.get(key) else 'No'}")
    for label, key in [("Start", "StartDate"), ("End", "EndDate"), ("Due", "DueDate")]:
        print(f"   {label}: {_fmt_date(quiz.get(key))}")
    print(f"   Attempts: {'Unlimited' if quiz.get('AttemptsAllowed', {}).get('IsUnlimited') else str(quiz.get('AttemptsAllowed', {}).get('NumberOfAttemptsAllowed', '?'))}")
    print(f"   Time Limit: {str(time_limit.get('TimeLimitValue', '?')) + ' min' if time_limit.get('IsEnforced') else 'None'}")
    if desc_text:
        print(f"\n   Description: {_short(desc_text, 200)}")
    if instr_text:
        print(f"   Instructions: {_short(instr_text, 200)}")

    print("\n   ⚠ Quiz questions and past attempts require instructor-level API access.")
    print(f"   View in browser: {BASE_URL}/d2l/lms/quizzing/user/quizzes_list.d2l?ou={org_id}")
    return 0
