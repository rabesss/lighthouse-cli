"""Command implementations — thin orchestration layer delegating to domain modules."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from contextlib import suppress
from typing import Any

from .api import CourseNotFoundError, LighthouseClient, resolve_course_id
from .config import BASE_URL, DEFAULT_DOWNLOAD_DIR
from .manifest import MANIFEST_FILENAME, Manifest, ManifestCorruptError, compute_sha256
from .utils import _sanitize_filename, get_course_name as _get_course_name, resolve_course_folder_name as _resolve_course_folder_name
from .display import error as _error, output_json as _output_json, print_table as _print_table, short as _short, fmt_date as _fmt_date, utc_now_iso as _utc_now_iso
from .course_config import load as _load_course_config
from .submit import cmd_submit  # noqa: F401 — re-export
from .show import cmd_grades, cmd_announcements, cmd_calendar, cmd_assignments, cmd_quizzes  # noqa: F401 — re-export
from .assignments import (
    assignment_key as _assignment_key,
    download_single_attachment as _download_single_attachment,
    download_for_course as _download_assignments_for_course,
    sync_for_course as _sync_assignments_for_course,
)


def _entry(tid: str, name: str, path: str, content_or_entry: bytes | dict, sha: str = "") -> dict:
    """Build a sync/download entry dict. content_or_entry is bytes (content) or dict (manifest entry)."""
    if not sha and isinstance(content_or_entry, bytes):
        sha = compute_sha256(content_or_entry)
    return {"topic_id": tid, "filename": name, "path": path, "size_kb": round((len(content_or_entry) if isinstance(content_or_entry, bytes) else content_or_entry.get("size", 0)) / 1024, 1), "sha256": sha, **({"extension": Path(name).suffix.lower()} if name and "." in name else {})}

def _fetch_toc_and_name(client: LighthouseClient, org_id: int) -> tuple[dict, str]:
    """Fetch content TOC and course name. Raises on failure."""
    return client.get_content_toc(org_id), _get_course_name(client, org_id)
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

def _download_and_persist_topic(
    client: LighthouseClient,
    org_id: int,
    topic: dict,
    dest: Path,
    manifest: Manifest,
) -> tuple[bytes, str, Path]:
    """Download a topic, write to disk, update manifest. Returns (content, name, path)."""
    tid = topic.get("topic_id")
    if tid is None:
        raise ValueError(f"Topic missing 'topic_id': {topic.get('title', 'unknown')}")
    tid = str(tid)
    if topic.get("type", "").lower() == "html":
        content, sanitized_name = client.get_topic_html(org_id, int(tid))
    else:
        content, filename = client.download_topic_file(org_id, int(tid))
        sanitized_name = _sanitize_filename(filename)
    file_dest = dest / Path(topic["path"]).parent
    file_dest.mkdir(parents=True, exist_ok=True)
    filepath = file_dest / sanitized_name
    filepath.write_bytes(content)
    manifest.add_entry(tid, content=content, filename=sanitized_name, last_modified=topic.get("last_modified") or "")
    return content, sanitized_name, filepath


def _parse_type_filter(types: str) -> set[str]:
    """Parse a comma-separated content-type filter string into a validated set.

    Accepts "file", "html", or comma-separated combos. Unknown types
    produce a warning and are dropped. Returns ``{"file"}`` when nothing
    valid remains.
    """
    valid, raw = {"file", "html"}, {t.strip().lower() for t in types.split(",")}
    for u in sorted(raw - valid):
        _error(f"Unknown content type: {u}")
    return (raw & valid) or {"file"}


def _filter_topics_by_type(modules: list[dict], type_set: set[str]) -> list[dict]:
    """Flatten topic tree and keep only topics matching *type_set*.

    Returns a list of topic dicts whose ``type`` (lowercased) is present
    in *type_set* (e.g. ``{"file"}`` or ``{"file", "html"}``).
    """
    return [
        t for t in _flatten_all_topics(modules) if t.get("type", "").lower() in type_set
    ]




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


def _flatten_all_topics(modules: list[dict], prefix: str = "") -> list[dict[str, Any]]:
    """Collect all downloadable topics from the content TOC.

    Returns list of {topic_id, title, url, type, path, last_modified}.
    """
    topics: list[dict[str, Any]] = []
    for mod in modules:
        new_prefix = f"{prefix}/{mod.get('Title', '')}" if prefix else mod.get("Title", "")
        topics.extend(_flatten_all_topics(mod.get("Modules", []), new_prefix))
        for topic in mod.get("Topics", []):
            topics.append({
                "topic_id": topic.get("TopicId"), "title": topic.get("Title", ""),
                "url": topic.get("Url"), "type": topic.get("TypeIdentifier", ""),
                "path": f"{new_prefix}/{topic.get('Title', '')}",
                "last_modified": topic.get("LastModifiedDate", ""),
            })
    return topics



def cmd_auth_status(json_output: bool = False) -> int:
    """Check if stored cookies are valid."""
    client = LighthouseClient()
    cookies = client.cookies
    if not cookies:
        return _error("No cookies found. Run: lighthouse auth refresh")

    valid = client.check_auth()
    if json_output:
        _output_json({"valid": valid, "cookies": list(cookies.keys())})
        return 0

    if valid:
        print(f"Session valid. Cookies: {', '.join(cookies.keys())}")
        return 0
    return _error("Session expired. Run: lighthouse auth refresh")


def cmd_auth_refresh(cdp_port: int | None = None, json_output: bool = False) -> int:
    """Extract fresh cookies from browser and verify."""
    from .api import refresh_auth_from_browser

    try:
        cookies = refresh_auth_from_browser(cdp_port)
    except Exception as exc:
        return _error(str(exc))

    # Verify
    valid = LighthouseClient().check_auth()
    if json_output:
        _output_json({"valid": valid, "cookies": list(cookies.keys())})
        return 0 if valid else 1

    if valid:
        print(f"Auth refreshed and verified. Cookies: {', '.join(cookies.keys())}")
        return 0
    return _error("Cookies extracted but session verification failed.")


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


def cmd_download(
    course_id: str | None = None,
    topic_id: int | None = None,
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

    if assignment_id is not None and attachment_id is not None and course_id is None:
        return _error("COURSE_ID is required when using --assignment and --attachment")

    if course_id is not None:
        try:
            org_id = resolve_course_id(client, course_id)
        except Exception as e:
            return _error(str(e))
        if assignment_id is not None and attachment_id is not None:
            return _download_single_attachment(client, org_id, assignment_id, attachment_id, root, json_output)
        return _download_single_course(
            client, org_id, root, json_output, force, types, dry_run,
            include_assignments=include_assignments,
            assignment_id=assignment_id,
        )

    if not semester and not also_courses:
        return _download_multi_course(client, root, json_output, force, types, dry_run, None, [], include_assignments)

    return _download_multi_course(
        client, root, json_output, force, types, dry_run, semester, also_courses,
        include_assignments=include_assignments,
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

    if course_id is not None:
        try:
            org_id = resolve_course_id(client, course_id)
        except Exception as e:
            return _error(str(e))
        return _sync_single_course(client, org_id, root, json_output, force, types, include_assignments)

    return _sync_multi_course(
        client, root, json_output, force, types, semester, also_courses, include_assignments
    )


def _sync_single_course(
    client: LighthouseClient,
    org_id: int,
    root: Path,
    json_output: bool,
    force: bool,
    types: str,
    include_assignments: bool = False,
) -> int:
    """Sync a single course, returning exit code."""
    try:
        toc, course_name = _fetch_toc_and_name(client, org_id)
    except Exception as e:
        return _error(str(e))


    dest = root / _resolve_course_folder_name(course_name, org_id)
    manifest_path = dest / MANIFEST_FILENAME

    if force and manifest_path.exists():
        manifest_path.unlink()
    try:
        manifest = Manifest.load(manifest_path)
    except Exception as exc:
        if isinstance(exc, ManifestCorruptError):
            print(f"Warning: {exc}. Performing full sync.", file=sys.stderr)
        manifest = Manifest()

    downloadable = _filter_topics_by_type(toc.get("Modules", []), _parse_type_filter(types))

    if not downloadable and not include_assignments:
        if json_output:
            _output_json({"course_id": org_id, "course_name": course_name, "downloaded": [], "skipped": [], "updated": [], "orphaned": [], "errors": []})
        else:
            print("No downloadable files found.")
        return 0

    dest.mkdir(parents=True, exist_ok=True)

    downloaded_entries, skipped_entries, updated_entries, errors = [], [], [], []

    manifest_topic_ids = set(manifest.entries.keys())

    for topic in downloadable:
        tid = str(topic["topic_id"])
        existing = manifest.get(tid)
        manifest_topic_ids.discard(tid)

        if existing is not None:
            if existing.get("last_modified") == (topic.get("last_modified") or ""):
                skipped_entries.append(_entry(tid, existing.get("filename", ""), existing.get("filename", ""), existing))
                continue
            target_list = updated_entries
        else:
            target_list = downloaded_entries

        try:
            content, sanitized_name, filepath = _download_and_persist_topic(client, org_id, topic, dest, manifest)
            target_list.append(_entry(tid, sanitized_name, str(filepath.relative_to(dest)), content))
        except Exception as e:
            errors.append({"topic_id": tid, "error": str(e)})

    orphaned_entries_by_tid = {tid: manifest.get(tid) for tid in manifest_topic_ids if manifest.get(tid)}

    assignments_downloaded, assignments_skipped, assignments_updated, assignment_errors = [], [], [], []
    if include_assignments:
        assignments_downloaded, assignments_skipped, assignments_updated, assignment_errors = _sync_assignments_for_course(
            client, org_id, dest, manifest
        )
        for entry in assignments_skipped + assignments_updated + assignments_downloaded:
            if (key := _assignment_key(entry.get("folder_id", 0), entry.get("file_id", 0))) in orphaned_entries_by_tid:
                del orphaned_entries_by_tid[key]

    orphaned_entries = [_entry(tid, e.get("filename", ""), "", e) for tid, e in orphaned_entries_by_tid.items()]

    if downloaded_entries or updated_entries or assignments_downloaded or assignments_updated or errors or assignment_errors:
        manifest.save(manifest_path)

    if json_output:
        _output_json({
            "course_id": org_id, "course_name": course_name,
            "folder": str(dest), "downloaded": downloaded_entries,
            "skipped": skipped_entries, "updated": updated_entries,
            "orphaned": orphaned_entries, "errors": errors,
            **({"assignments_downloaded": assignments_downloaded, "assignments_skipped": assignments_skipped, "assignments_updated": assignments_updated, "assignment_errors": assignment_errors} if include_assignments else {}),
        })
    else:
        parts = [f"{len(downloaded_entries)} new"]
        if assignments_downloaded:
            parts.append(f"{len(assignments_downloaded)} assignment new")
        if assignments_updated:
            parts.append(f"{len(assignments_updated)} assignment updated")
        parts.extend([f"{len(updated_entries)} updated", f"{len(skipped_entries)} skipped", f"{len(orphaned_entries)} orphaned", f"{len(errors)} errors"])
        if assignment_errors:
            parts.append(f"{len(assignment_errors)} assignment errors")
        print(f"Synced {course_name}: {', '.join(parts)}")

    return 1 if (errors or assignment_errors) else 0


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


def _download_single_course(
    client: LighthouseClient,
    org_id: int,
    root: Path,
    json_output: bool,
    force: bool,
    types: str,
    dry_run: bool = False,
    include_assignments: bool = False,
    assignment_id: int | None = None,
) -> int:
    """Download a single course by org_id."""
    try:
        toc, course_name = _fetch_toc_and_name(client, org_id)
    except Exception as e:
        return _error(str(e))


    downloadable = _filter_topics_by_type(toc.get("Modules", []), _parse_type_filter(types))

    if not downloadable and not include_assignments:
        if json_output:
            _output_json({"course_id": org_id, "files": [], "downloaded": 0, "errors": 0})
        else:
            print("No downloadable files found.")
        return 0

    dest = root / _resolve_course_folder_name(course_name, org_id)
    manifest_path = dest / MANIFEST_FILENAME

    if force and manifest_path.exists():
        manifest_path.unlink()
    manifest = Manifest.load(manifest_path)

    if dry_run:
        print(f"Would download {len(downloadable)} files to {dest}/\n")
        print("\n".join(f"  [{t['topic_id']}] {t['title']}" for t in downloadable))
        if include_assignments:
            print("\n  (Assignment downloads not shown in dry-run)")
        return 0

    dest.mkdir(parents=True, exist_ok=True)

    downloaded, errors = [], []
    for i, topic in enumerate(downloadable, 1):
        tid = topic["topic_id"]
        try:
            content, sanitized_name, filepath = _download_and_persist_topic(client, org_id, topic, dest, manifest)
            downloaded.append({"topic_id": tid, "filename": sanitized_name, "size": len(content), "path": str(filepath.relative_to(dest))})
            if not json_output:
                print(f"  [{i}/{len(downloadable)}] {filepath.relative_to(dest)} ({len(content)/1024:.0f} KB)")
        except Exception as e:
            errors.append({"topic_id": tid, "error": str(e)})
            print(f"  [{i}/{len(downloadable)}] FAILED topic {tid}: {e}", file=sys.stderr)

    assignments_downloaded, assignment_errors = [], []
    if include_assignments and not dry_run:
        assignments_downloaded, assignment_errors = _download_assignments_for_course(
            client, org_id, dest, manifest, folder_ids=[assignment_id] if assignment_id else None
        )

    if downloaded or assignments_downloaded or errors or assignment_errors:
        manifest.save(manifest_path)

    if json_output:
        _output_json({
            "course_id": org_id, "course_name": course_name,
            "folder": str(dest), "manifest": str(manifest_path),
            "downloaded": downloaded, "errors": errors,
            **({"assignments_downloaded": assignments_downloaded, "assignment_errors": assignment_errors} if include_assignments else {}),
        })
        return 0
    if assignments_downloaded:
        print(f"\nAssignments: {len(assignments_downloaded)} attachment(s) downloaded")
    print(f"\nDone: {len(downloaded)}/{len(downloadable)} files downloaded to {dest}")
    if assignment_errors:
        print(f"  {len(assignment_errors)} assignment error(s)")
    return 1 if (errors or assignment_errors) else 0


def _download_multi_course(
    client: LighthouseClient,
    root: Path,
    json_output: bool,
    force: bool,
    types: str,
    dry_run: bool,
    semester_filter: str | None,
    also_courses: list[str],
    include_assignments: bool = False,
) -> int:
    """Download courses matching --semester filter plus --also additions."""
    scope = _resolve_course_scope(client, semester_filter, also_courses, "download")
    if isinstance(scope, int):
        return scope
    all_course_ids, sem_name, sem_id, also_errors = scope


    if json_output:
        # Structured JSON multi-course download
        rc = 0
        courses_results = []
        for cid in all_course_ids:
            try:
                toc, cname = _fetch_toc_and_name(client, cid)
            except Exception as e:
                rc = _error(str(e))
                continue

            downloadable = _filter_topics_by_type(toc.get("Modules", []), _parse_type_filter(types))

            dest = root / _resolve_course_folder_name(cname, cid)

            if dry_run:
                courses_results.append({
                    "course_id": cid, "course_name": cname,
                    "semester": sem_name, "root": str(dest),
                    "manifest_total": 0,
                    "downloaded": [], "skipped": [], "updated": [],
                    "duplicates": [], "errors": [],
                })
                continue

            dest.mkdir(parents=True, exist_ok=True)
            manifest_path = dest / MANIFEST_FILENAME
            if force and manifest_path.exists():
                manifest_path.unlink()
            manifest = Manifest.load(manifest_path)

            downloaded, errors = [], []
            sha_hashes: dict[str, list[dict]] = {}
            for topic in downloadable:
                tid = topic["topic_id"]
                try:
                    content, sanitized_name, filepath = _download_and_persist_topic(client, cid, topic, dest, manifest)
                    file_hash = compute_sha256(content)
                    sha_hashes.setdefault(file_hash, []).append({"topic_id": tid, "filename": sanitized_name})
                    downloaded.append(_entry(tid, sanitized_name, str(filepath.relative_to(dest)), content, file_hash))
                except Exception as e:
                    errors.append({"topic_id": tid, "filename": topic.get("title", ""), "error": str(e)})

            assignments_downloaded, assignment_errors = [], []
            if include_assignments:
                assignments_downloaded, assignment_errors = _download_assignments_for_course(
                    client, cid, dest, manifest
                )

            if downloaded or assignments_downloaded or errors or assignment_errors:
                manifest.save(manifest_path)


            courses_results.append({
                "course_id": cid, "course_name": cname,
                "semester": sem_name, "root": str(dest),
                "manifest_total": len(manifest),
                "downloaded": downloaded, "skipped": [], "updated": [],
                "duplicates": [{"topic_id": e["topic_id"], "filename": e["filename"], "sha256": h} for h, es in sha_hashes.items() if len(es) > 1 for e in es],
                "errors": errors,
                "assignments_downloaded": assignments_downloaded,
                "assignment_errors": assignment_errors,
            })
            if errors:
                rc = 1

        _output_multi_course_json(sem_id, sem_name, courses_results, also_errors)
        return rc

    print(f"Downloading courses from {sem_name}...\n")
    rc = 0
    for cid in all_course_ids:
        if _download_single_course(client, cid, root, False, force, types, dry_run, include_assignments) != 0:
            rc = 1
    if also_errors:
        print("\n".join(f"  Error: {err}" for err in also_errors), file=sys.stderr)
        rc = 1
    print("\nDownload complete.")
    return rc


def _sync_multi_course(
    client: LighthouseClient,
    root: Path,
    json_output: bool,
    force: bool,
    types: str,
    semester_filter: str | None,
    also_courses: list[str],
    include_assignments: bool = False,
) -> int:
    """Sync courses matching --semester filter plus --also additions."""
    scope = _resolve_course_scope(client, semester_filter, also_courses, "sync")
    if isinstance(scope, int):
        return scope
    all_course_ids, sem_name, sem_id, also_errors = scope


    if json_output:
        # Structured JSON multi-course sync
        rc = 0
        courses_results = []
        for cid in all_course_ids:
            try:
                toc, cname = _fetch_toc_and_name(client, cid)
            except Exception as e:
                rc = _error(str(e))
                continue

            downloadable = _filter_topics_by_type(toc.get("Modules", []), _parse_type_filter(types))

            dest = root / _resolve_course_folder_name(cname, cid)

            manifest_path = dest / MANIFEST_FILENAME
            if force and manifest_path.exists():
                manifest_path.unlink()
            manifest = Manifest()
            with suppress(ManifestCorruptError):
                manifest = Manifest.load(manifest_path)

            downloaded_entries, skipped_entries, updated_entries, orphaned_entries, errors = [], [], [], [], []
            sha_hashes: dict[str, list[dict]] = {}
            manifest_topic_ids = set(manifest.entries.keys())

            for topic in downloadable:
                tid = str(topic["topic_id"])
                existing = manifest.get(tid)
                manifest_topic_ids.discard(tid)

                if existing is not None:
                    if existing.get("last_modified") == (topic.get("last_modified") or ""):
                        skipped_entries.append(_entry(tid, existing.get("filename", ""), existing.get("filename", ""), existing))
                        if file_hash := existing.get("sha256", ""):
                            sha_hashes.setdefault(file_hash, []).append({"topic_id": tid, "filename": existing.get("filename", "")})
                        continue
                    target_list = updated_entries
                else:
                    target_list = downloaded_entries

                try:
                    content, sanitized_name, filepath = _download_and_persist_topic(client, cid, topic, dest, manifest)
                    file_hash = compute_sha256(content)
                    sha_hashes.setdefault(file_hash, []).append({"topic_id": tid, "filename": sanitized_name})
                    target_list.append(_entry(tid, sanitized_name, str(filepath.relative_to(dest)), content, file_hash))
                except Exception as e:
                    errors.append({"topic_id": tid, "filename": topic.get("title", ""), "error": str(e)})

            orphaned_entries = [_entry(tid, e.get("filename", ""), "", e) for tid in manifest_topic_ids if (e := manifest.get(tid))]

            assignments_downloaded, assignments_skipped, assignments_updated, assignment_errors = [], [], [], []
            if include_assignments:
                assignments_downloaded, assignments_skipped, assignments_updated, assignment_errors = _sync_assignments_for_course(
                    client, cid, dest, manifest
                )

            if downloaded_entries or updated_entries or assignments_downloaded or assignments_updated or errors or assignment_errors:
                manifest.save(manifest_path)


            courses_results.append({
                "course_id": cid, "course_name": cname,
                "semester": sem_name, "root": str(dest),
                "manifest_total": len(manifest),
                "downloaded": downloaded_entries, "skipped": skipped_entries,
                "updated": updated_entries, "orphaned": orphaned_entries,
                "duplicates": [{"topic_id": e["topic_id"], "filename": e["filename"], "sha256": h} for h, es in sha_hashes.items() if len(es) > 1 for e in es],
                "errors": errors,
                "assignments_downloaded": assignments_downloaded,
                "assignments_skipped": assignments_skipped,
                "assignments_updated": assignments_updated,
                "assignment_errors": assignment_errors,
            })
            if errors or assignment_errors:
                rc = 1

        _output_multi_course_json(sem_id, sem_name, courses_results, also_errors)
        return rc

    print(f"Syncing courses from {sem_name}...\n")
    rc = 0
    for cid in all_course_ids:
        if _sync_single_course(client, cid, root, False, force, types, include_assignments) != 0:
            rc = 1
    if also_errors:
        print("\n".join(f"  Error: {err}" for err in also_errors), file=sys.stderr)
    return rc


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


