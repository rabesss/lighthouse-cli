"""Command implementations for lighthouse-cli.

Each function corresponds to a CLI subcommand and handles data fetching,
formatting, and output. Output goes through a display helper that supports
both human-readable (rich tables / plain text) and --json mode.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .api import (
    BASE_URL,
    DEFAULT_DOWNLOAD_DIR,
    CourseNotFoundError,
    LighthouseClient,
    NetworkError,
    SessionExpiredError,
    resolve_course_id,
)
from .auth import cmd_auth_login as _cmd_auth_login
from .manifest import MANIFEST_FILENAME, Manifest, ManifestCorruptError, compute_sha256
from .utils import _sanitize_filename

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

# Cache rich imports at module level to avoid re-import per table render.
_RICH_CACHE: tuple | None = None
_RICH_CHECKED: bool = False


def _try_rich():
    """Import rich if available, return (Table, console) or None. Cached."""
    global _RICH_CACHE, _RICH_CHECKED
    if not _RICH_CHECKED:
        _RICH_CHECKED = True
        try:
            from rich.console import Console
            from rich.table import Table
            _RICH_CACHE = (Table, Console())
        except ImportError:
            _RICH_CACHE = None
    return _RICH_CACHE


def _print_table(
    columns: list[str],
    rows: list[list[str]],
    title: str = "",
) -> None:
    """Print a table using rich if available, else plain aligned text."""
    rich = _try_rich()
    if rich:
        Table, console = rich
        table = Table(title=title, show_lines=False, pad_edge=False)
        for col in columns:
            table.add_column(col, overflow="ellipsis")
        for row in rows:
            table.add_row(*row)
        console.print(table)
        return

    # Plain-text fallback: columnar alignment
    widths = [len(c) for c in columns]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    if title:
        print(f"\n{title}")
    print(fmt.format(*columns))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*row))


def _output_json(data: Any) -> None:
    """Print raw JSON to stdout (for --json mode / agent consumption)."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _error(msg: str) -> None:
    """Print error message to stderr."""
    print(f"Error: {msg}", file=sys.stderr)


def _short(text: str, max_len: int = 50) -> str:
    """Truncate text with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _fmt_date(date_str: str | None) -> str:
    """Format an ISO date string to something compact."""
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(date_str)[:16]


def _utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 string (e.g. '2026-05-10T14:30:00Z')."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Generic "single course or all courses" dispatch helper
# ---------------------------------------------------------------------------

def _for_course_or_all(
    client: LighthouseClient,
    course_id: str | None,
    single_fn,
    json_output: bool,
    collection_key: str,
):
    """Run single_fn for one course or all courses.

    In --json mode, collects all results into a single JSON array (fixes
    concatenated-objects bug). In human mode, prints each result inline.

    When iterating all courses, uses ThreadPoolExecutor(max_workers=5) for
    parallel API calls (~5x speedup). ``requests.Session`` is thread-safe.

    Args:
        client: LighthouseClient instance.
        course_id: Course identifier (name/ID) or None for all courses.
        single_fn: callable(client, org_id, json_output, title=) -> dict | None
            Must return a dict (the JSON payload) when json_output=True,
            or an int exit-code when json_output=False.
        json_output: Whether --json was passed.
        collection_key: Key name for the per-course payload (e.g. "grades",
            "announcements", "events", "quizzes").

    Returns:
        Exit code (0 or 1).
    """
    if course_id:
        org_id = resolve_course_id(client, course_id)
        result = single_fn(client, org_id, json_output)
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
                    payload = future.result()
                    if payload is not None:
                        results.append(payload)
                except Exception:
                    pass  # skip courses with errors
        # Sort by course_id for deterministic output
        results.sort(key=lambda r: r.get("course_id", 0))
        _output_json(results)
        return rc
    else:
        # Parallel fetch, then sequential display (preserves readable order)
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(single_fn, client, int(c["OrgUnitId"]), False, title=c.get("Name", "")): c
                for c in courses
            }
            # Collect results in submission order for readable output
            ordered = []
            for c in courses:
                oid = int(c["OrgUnitId"])
                name = c.get("Name", "")
                # Find the matching future
                for f, fc in futures.items():
                    if int(fc["OrgUnitId"]) == oid:
                        try:
                            r = f.result()
                            if r:
                                rc = r
                        except Exception:
                            pass
                        break
        return rc


# ---------------------------------------------------------------------------
# Content tree helpers
# ---------------------------------------------------------------------------

def _walk_content_tree(modules: list[dict], depth: int = 0) -> list[dict[str, Any]]:
    """Flatten the nested content TOC into a list of display records.

    Each record: {depth, type, id, title, url}
    """
    items: list[dict[str, Any]] = []
    for mod in modules:
        items.append({
            "depth": depth,
            "type": "module",
            "id": mod.get("ModuleId"),
            "title": mod.get("Title", ""),
            "url": None,
        })
        items.extend(_walk_content_tree(mod.get("Modules", []), depth + 1))
        for topic in mod.get("Topics", []):
            items.append({
                "depth": depth + 1,
                "type": "topic",
                "id": topic.get("TopicId"),
                "title": topic.get("Title", ""),
                "url": topic.get("Url"),
                "topic_type": topic.get("TypeIdentifier", ""),
            })
    return items


def _flatten_all_topics(modules: list[dict], prefix: str = "") -> list[dict[str, Any]]:
    """Collect all downloadable topics from the content TOC.

    Returns list of {topic_id, title, url, type, path, last_modified}.
    """
    topics: list[dict[str, Any]] = []
    for mod in modules:
        mod_title = mod.get("Title", "")
        new_prefix = f"{prefix}/{mod_title}" if prefix else mod_title
        topics.extend(_flatten_all_topics(mod.get("Modules", []), new_prefix))
        for topic in mod.get("Topics", []):
            topics.append({
                "topic_id": topic.get("TopicId"),
                "title": topic.get("Title", ""),
                "url": topic.get("Url"),
                "type": topic.get("TypeIdentifier", ""),
                "path": f"{new_prefix}/{topic.get('Title', '')}",
                "last_modified": topic.get("LastModifiedDate", ""),
            })
    return topics


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

def cmd_auth_status(json_output: bool = False) -> int:
    """Check if stored cookies are valid."""
    client = LighthouseClient()
    cookies = client.cookies
    if not cookies:
        _error("No cookies found. Run: lighthouse auth refresh")
        return 1

    valid = client.check_auth()
    if json_output:
        _output_json({"valid": valid, "cookies": list(cookies.keys())})
        return 0

    if valid:
        print(f"Session valid. Cookies: {', '.join(cookies.keys())}")
        return 0
    else:
        _error("Session expired. Run: lighthouse auth refresh")
        return 1


def cmd_auth_refresh(cdp_port: int | None = None, json_output: bool = False) -> int:
    """Extract fresh cookies from browser and verify."""
    from .api import refresh_auth_from_browser

    try:
        cookies = refresh_auth_from_browser(cdp_port)
    except Exception as exc:
        _error(str(exc))
        return 1

    # Verify
    client = LighthouseClient()
    valid = client.check_auth()
    if json_output:
        _output_json({"valid": valid, "cookies": list(cookies.keys())})
        return 0 if valid else 1

    if valid:
        print(f"Auth refreshed and verified. Cookies: {', '.join(cookies.keys())}")
        return 0
    else:
        _error("Cookies extracted but session verification failed.")
        return 1


def cmd_auth_login(
    username: str | None = None,
    password: str | None = None,
    totp: str | None = None,
    totp_stdin: bool = False,
    save_credentials: bool = False,
    json_output: bool = False,
) -> int:
    """Headless browser login via Microsoft SSO with 2FA.

    Supports flexible credential input:
    - --user/--pass flags (highest priority)
    - LIGHTHOUSE_USERNAME/PASSWORD env vars
    - Stored encrypted credentials (from --save-credentials on previous run)
    - Interactive prompts (if TTY)

    Supports flexible 2FA input:
    - --totp <code> flag
    - --totp - (read from stdin pipe)
    - Interactive prompt (if TTY)
    """
    return _cmd_auth_login(
        username=username,
        password=password,
        totp_code=totp,
        totp_stdin=totp_stdin,
        save_credentials=save_credentials,
        json_output=json_output,
    )


def cmd_semesters(json_output: bool = False) -> int:
    """List all semesters."""
    client = LighthouseClient()
    try:
        semesters = client.get_semesters()
    except (SessionExpiredError, Exception) as e:
        _error(str(e))
        return 1

    if json_output:
        _output_json(semesters)
        return 0

    rows = [[s.get("OrgUnitId", ""), s.get("Name", ""), s.get("Code", "")] for s in semesters]
    _print_table(["ID", "Name", "Code"], rows, title="Semesters")
    return 0


def cmd_courses(
    semester: str | None = None,
    json_output: bool = False,
) -> int:
    """List courses, optionally filtered by semester."""
    client = LighthouseClient()
    try:
        # Use enrollments API for the full course list (30 courses)
        # instead of mycourses widget API (only 20 courses, missing labs/Sem IV)
        all_enrollments = client.get_course_enrollments()
        # Only fetch semesters when filtering (they rarely change)
        semesters = client.get_semesters() if semester else []
    except SessionExpiredError as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1

    # Normalize to the same format as mycourses API
    courses = [
        {
            "OrgUnitId": int(e["OrgUnit"]["Id"]),
            "Name": e["OrgUnit"].get("Name", ""),
            "Code": e["OrgUnit"].get("Code", ""),
            "IsActive": e.get("Access", {}).get("IsActive", True),
        }
        for e in all_enrollments
    ]

    # Build semester lookup
    sem_map = {s["OrgUnitId"]: s.get("Name", "") for s in semesters}

    # Filter by semester if given
    if semester:
        # Match by semester OrgUnitId or name substring
        matching_sems: list[dict[str, Any]] = []
        for s in semesters:
            sname = s.get("Name", "")
            if semester == s["OrgUnitId"] or semester.lower() == sname.lower():
                matching_sems.append(s)
            # Also match roman numeral or segment exactly
            elif semester.lower().strip() in [x.strip().lower() for x in sname.split("|")]:
                matching_sems.append(s)

        if not matching_sems:
            _error(f"No semester matching '{semester}'. Run: lighthouse semesters")
            return 1

        # Map courses to semesters using course-code year suffix matching.
        #
        # Semester codes use abbreviated years ("2024-25") while course codes
        # use full years ("2024-2025"). We normalize both to full form for matching.
        #
        # Ambiguity resolution for semesters sharing the same year range
        # (e.g. Sem III and Sem IV both have year "2025-2026"):
        #   - Sem IV courses embed "_IV_" in their code before the year suffix
        #     e.g. "009_MAT 2223_902_IV_2025-2026"
        #   - Sem III courses have just the year suffix, no "_IV_"
        #     e.g. "009_COURSE 2125_2025-2026"

        def _normalize_year(year_str: str) -> str:
            """Normalize abbreviated year to full form."""
            if "-" not in year_str:
                if len(year_str) == 2:
                    return "20" + year_str
                return year_str
            parts = year_str.split("-")
            if len(parts) == 2 and len(parts[1]) == 2:
                parts[1] = parts[0][:2] + parts[1]
            return "-".join(parts)

        def _extract_semester_key(sem: dict) -> tuple[str, str]:
            """Extract (normalized_year, roman_numeral) from a semester."""
            name = sem.get("Name", "")
            code = sem.get("Code", "")
            roman = ""
            rm = re.search(r'\bSem\s+([IVX]+)\b', name, re.IGNORECASE)
            if rm:
                roman = rm.group(1).upper()
            code_parts = code.split("_")
            year_part = code_parts[-1] if code_parts else ""
            return _normalize_year(year_part), roman

        sem_keys = [(s, *_extract_semester_key(s)) for s in matching_sems]

        # Check if year ranges are ambiguous across ALL semesters (not just matched ones)
        all_sem_keys = [(s, *_extract_semester_key(s)) for s in semesters]
        year_groups: dict[str, list[tuple]] = {}
        for s, yr, rn in all_sem_keys:
            year_groups.setdefault(yr, []).append((s, yr, rn))

        def _course_matches_year(code: str, year: str) -> bool:
            parts = code.rsplit("_", 1)
            if len(parts) < 2:
                return False
            course_year = _normalize_year(parts[-1])
            return course_year == year

        filtered = []
        for c in courses:
            code = str(c.get("Code", ""))
            for sem, yr, roman in sem_keys:
                group = year_groups[yr]
                if len(group) == 1:
                    if _course_matches_year(code, yr):
                        filtered.append(c)
                        break
                else:
                    code_upper = code.upper()
                    has_year = _course_matches_year(code, yr)
                    if not has_year:
                        continue
                    if roman == "IV":
                        if "_IV_" in code_upper:
                            filtered.append(c)
                            break
                    elif roman == "III":
                        if "_IV_" not in code_upper:
                            filtered.append(c)
                            break
                    else:
                        filtered.append(c)
                        break
        courses = filtered

        # Tag matched courses with semester name
        for c in courses:
            c["semester"] = matching_sems[0].get("Name", "") if len(matching_sems) == 1 else sem_map.get(c.get("OrgUnitId"), "")
    else:
        # Tag all courses with semester when no filter
        # (reverse-lookup: try to match course code to semester year ranges)
        if sem_map:
            for c in courses:
                c["semester"] = sem_map.get(c.get("OrgUnitId"), "")

    if json_output:
        _output_json(courses)
        return 0

    rows = []
    for c in courses:
        sem_col = c.get("semester", "")
        rows.append([
            str(c.get("OrgUnitId", "")),
            _short(c.get("Name", ""), 40),
            sem_col,
            c.get("IsActive") and "Y" or "N",
        ])
    _print_table(["ID", "Name", "Semester", "Active"], rows, title=f"Courses ({len(rows)})")
    return 0


def cmd_content(course_id: str, json_output: bool = False) -> int:
    """Show content tree for a course."""
    client = LighthouseClient()
    try:
        org_id = resolve_course_id(client, course_id)
        toc = client.get_content_toc(org_id)
    except (SessionExpiredError, CourseNotFoundError) as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1

    modules = toc.get("Modules", [])

    if json_output:
        # Wrap with course_id for agent discoverability
        _output_json({"course_id": org_id, "modules": modules})
        return 0

    # Render tree
    items = _walk_content_tree(modules)
    if not items:
        print("No content found for this course.")
        return 0

    for item in items:
        indent = "  " * item["depth"]
        if item["type"] == "module":
            print(f"{indent}📁 {item['title']}")
        else:
            icon = {"File": "📄", "Link": "🔗"}.get(item.get("topic_type", ""), "📎")
            tid = item.get("id", "")
            print(f"{indent}{icon} {item['title']}  [id:{tid}]")
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
    """Download files from a course.

    Creates a folder named after the D2L course Name (sanitized), not the OrgUnitId.
    Writes .lighthouse.json manifest to the course folder after download.
    Without COURSE_ID, downloads all courses from the latest semester.

    Multi-course scope:
      - No args: all courses from latest semester (highest OrgUnitId)
      - --semester: filter to specific semester (name substring or ID)
      - --also: add ad-hoc courses outside semester scope
      - Single course: positional name substring or numeric OrgUnitId

    Assignment options:
      --include-assignments: Download attachments from all dropbox folders
      --assignment: Download a specific dropbox folder by ID
      --attachment: Download a specific attachment (requires --assignment)
    """
    client = LighthouseClient()
    root = Path(output_dir).expanduser().resolve() if output_dir else DEFAULT_DOWNLOAD_DIR
    also_courses = also_courses or []

    # Single attachment download via --assignment + --attachment
    if assignment_id is not None and attachment_id is not None:
        if course_id is None:
            _error("COURSE_ID is required when using --assignment and --attachment")
            return 1
        try:
            org_id = resolve_course_id(client, course_id)
        except (SessionExpiredError, CourseNotFoundError) as e:
            _error(str(e))
            return 1
        return _download_single_attachment(
            client, org_id, assignment_id, attachment_id, root, json_output
        )

    if course_id is None and not semester and not also_courses:
        # No course_id: download all courses from latest semester
        return _download_multi_course(client, root, json_output, force, types, dry_run, None, [], include_assignments)

    # Single course by name or ID
    if course_id is not None:
        try:
            org_id = resolve_course_id(client, course_id)
        except (SessionExpiredError, CourseNotFoundError) as e:
            _error(str(e))
            return 1

        return _download_single_course(
            client, org_id, root, json_output, force, types, dry_run,
            include_assignments=include_assignments,
            assignment_id=assignment_id,
        )

    # Multi-course scope with --semester and/or --also
    return _download_multi_course(
        client, root, json_output, force, types, dry_run, semester, also_courses,
        include_assignments=include_assignments,
    )


# ---------------------------------------------------------------------------
# Sync command — incremental download with manifest
# ---------------------------------------------------------------------------

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
    """Incremental sync: read manifest, compare TOC LastModifiedDate, skip unchanged.

    Sync is idempotent — running twice with no remote changes produces no downloads.
    Multi-course scope options:
      - No args: sync all courses from latest semester
      --semester: filter to specific semester (name or ID)
      - --also: add ad-hoc courses outside semester scope
    """
    client = LighthouseClient()
    root = Path(output_dir).expanduser().resolve() if output_dir else DEFAULT_DOWNLOAD_DIR
    also_courses = also_courses or []

    if course_id is not None:
        try:
            org_id = resolve_course_id(client, course_id)
        except (SessionExpiredError, CourseNotFoundError) as e:
            _error(str(e))
            return 1
        return _sync_single_course(client, org_id, root, json_output, force, types, include_assignments)

    # Multi-course scope
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
        toc = client.get_content_toc(org_id)
        course_name = _get_course_name(client, org_id)
    except SessionExpiredError as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1

    type_set = {t.strip().lower() for t in types.split(",")}
    valid_types = {"file", "html"}
    invalid_types = type_set - valid_types
    if invalid_types:
        for it in invalid_types:
            _error(f"Unknown content type: {it}")
        type_set = type_set & valid_types
    if not type_set:
        type_set = {"file"}

    folder_name = _resolve_course_folder_name(course_name, org_id)
    dest = root / folder_name
    manifest_path = dest / MANIFEST_FILENAME

    # Load manifest (may be absent or corrupt)
    if force and manifest_path.exists():
        manifest_path.unlink()
    try:
        manifest = Manifest.load(manifest_path)
    except ManifestCorruptError as exc:
        print(f"Warning: {exc}. Performing full sync.", file=sys.stderr)
        manifest = Manifest()
    except Exception:
        manifest = Manifest()

    all_topics = _flatten_all_topics(toc.get("Modules", []))
    downloadable = []
    for t in all_topics:
        type_id = t.get("type", "").lower()
        if type_id == "file" and "file" in type_set:
            downloadable.append(t)
        elif type_id == "html" and "html" in type_set:
            downloadable.append(t)

    if not downloadable and not include_assignments:
        if json_output:
            _output_json({"course_id": org_id, "course_name": course_name, "downloaded": [], "skipped": [], "updated": [], "orphaned": [], "errors": []})
        else:
            print("No downloadable files found.")
        return 0

    dest.mkdir(parents=True, exist_ok=True)

    downloaded_entries = []
    skipped_entries = []
    updated_entries = []
    errors = []

    # Track all topic_ids in manifest before we start
    manifest_topic_ids = set(manifest.entries.keys())

    for topic in downloadable:
        tid = str(topic["topic_id"])
        last_mod = topic.get("last_modified") or ""
        existing = manifest.get(tid)
        manifest_topic_ids.discard(tid)  # mark as seen in TOC

        if existing is not None:
            # Check if unchanged
            if existing.get("last_modified") == last_mod:
                skipped_entries.append({"topic_id": tid, "filename": existing.get("filename", ""), "path": existing.get("filename", ""), "size_kb": round(existing.get("size", 0) / 1024, 1), "sha256": existing.get("sha256", "")})
                continue
            # Changed — re-download
            try:
                topic_type = topic.get("type", "").lower()
                if topic_type == "html":
                    content, sanitized_name = client.get_topic_html(org_id, int(tid))
                else:
                    content, filename = client.download_topic_file(org_id, int(tid))
                    sanitized_name = _sanitize_filename(filename)
                rel_path = Path(topic["path"]).parent
                file_dest = dest / rel_path
                file_dest.mkdir(parents=True, exist_ok=True)
                filepath = file_dest / sanitized_name
                filepath.write_bytes(content)
                manifest.add_entry(tid, content=content, filename=sanitized_name, last_modified=last_mod)
                updated_entries.append({"topic_id": tid, "filename": sanitized_name, "path": str(filepath.relative_to(dest)), "size_kb": round(len(content) / 1024, 1), "sha256": manifest.get(tid).get("sha256", "")})
            except Exception as e:
                errors.append({"topic_id": tid, "error": str(e)})
            continue

        # New topic
        try:
            topic_type = topic.get("type", "").lower()
            if topic_type == "html":
                content, sanitized_name = client.get_topic_html(org_id, int(tid))
            else:
                content, filename = client.download_topic_file(org_id, int(tid))
                sanitized_name = _sanitize_filename(filename)
            rel_path = Path(topic["path"]).parent
            file_dest = dest / rel_path
            file_dest.mkdir(parents=True, exist_ok=True)
            filepath = file_dest / sanitized_name
            filepath.write_bytes(content)
            manifest.add_entry(tid, content=content, filename=sanitized_name, last_modified=last_mod)
            downloaded_entries.append({"topic_id": tid, "filename": sanitized_name, "path": str(filepath.relative_to(dest)), "size_kb": round(len(content) / 1024, 1), "sha256": manifest.get(tid).get("sha256", "")})
        except Exception as e:
            errors.append({"topic_id": tid, "error": str(e)})

    # Remaining topic_ids in manifest are orphaned (in manifest but not in TOC)
    orphaned_entries_by_tid: dict[str, dict] = {}
    for tid in manifest_topic_ids:
        entry = manifest.get(tid)
        if entry:
            orphaned_entries_by_tid[tid] = entry

    # Sync assignment attachments if requested
    assignments_downloaded = []
    assignments_skipped = []
    assignments_updated = []
    assignment_errors = []
    if include_assignments:
        assignments_downloaded, assignments_skipped, assignments_updated, assignment_errors = _sync_assignments_for_course(
            client, org_id, dest, manifest
        )
        # Remove assignment keys from orphaned dict since they were processed above
        for entry in assignments_skipped + assignments_updated + assignments_downloaded:
            key = _assignment_key(entry.get("folder_id", 0), entry.get("file_id", 0))
            if key in orphaned_entries_by_tid:
                del orphaned_entries_by_tid[key]

    # Build orphaned list (excludes assignment entries that were processed above)
    orphaned_entries = [
        {"topic_id": tid, "filename": orphaned_entries_by_tid[tid].get("filename", ""),
         "size_kb": round(orphaned_entries_by_tid[tid].get("size", 0) / 1024, 1),
         "sha256": orphaned_entries_by_tid[tid].get("sha256", "")}
        for tid in orphaned_entries_by_tid
    ]

    if downloaded_entries or updated_entries or assignments_downloaded or assignments_updated or errors or assignment_errors:
        manifest.save(manifest_path)

    if json_output:
        result = {
            "course_id": org_id,
            "course_name": course_name,
            "folder": str(dest),
            "downloaded": downloaded_entries,
            "skipped": skipped_entries,
            "updated": updated_entries,
            "orphaned": orphaned_entries,
            "errors": errors,
        }
        if include_assignments:
            result["assignments_downloaded"] = assignments_downloaded
            result["assignments_skipped"] = assignments_skipped
            result["assignments_updated"] = assignments_updated
            result["assignment_errors"] = assignment_errors
        _output_json(result)
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
    """Resolve a semester filter to a semester dict.

    If semester_filter is None, returns the latest semester (highest OrgUnitId).
    If semester_filter is a numeric string, matches by OrgUnitId.
    If semester_filter is a name, matches by substring (case-insensitive).
    Returns None if no match found (caller should emit error).
    """
    semesters = client.get_semesters()
    if not semesters:
        return None

    if semester_filter is None:
        # Default: latest semester = highest OrgUnitId
        return max(semesters, key=lambda s: int(s.get("OrgUnitId", 0)))

    # Try numeric OrgUnitId match
    try:
        sem_id = int(semester_filter)
        for s in semesters:
            if int(s.get("OrgUnitId", 0)) == sem_id:
                return s
    except ValueError:
        pass

    # Try name substring match (case-insensitive)
    # Prefer exact matches over partial matches to avoid "Sem I" matching "Sem II"
    lower_filter = semester_filter.lower().strip()
    matches = []
    for s in semesters:
        sname = s.get("Name", "")
        sname_lower = sname.lower()
        # Exact match (after stripping whitespace)
        if lower_filter == sname_lower:
            return s
        # Partial match (filter is substring of name)
        if lower_filter in sname_lower:
            matches.append(s)

    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        # Ambiguous — return the one with highest OrgUnitId
        return max(matches, key=lambda s: int(s.get("OrgUnitId", 0)))

    return None


def _resolve_also_course(
    client: LighthouseClient,
    identifier: str,
) -> int:
    """Resolve an --also course identifier to an OrgUnitId.

    Raises CourseNotFoundError if not found or if multiple courses match
    (ambiguous), matching the behavior of resolve_course_id().
    """
    courses = client.get_courses()
    # Try numeric
    try:
        cid = int(identifier)
        # Verify it exists in our course list
        for c in courses:
            if int(c.get("OrgUnitId", 0)) == cid:
                return cid
        raise CourseNotFoundError(
            f"Course '{identifier}' not found. Run: lighthouse courses"
        )
    except ValueError:
        pass

    # Try name substring
    lower_id = identifier.lower()
    matches = [
        c for c in courses
        if lower_id in c.get("Name", "").lower()
    ]
    if len(matches) == 1:
        return int(matches[0]["OrgUnitId"])
    if len(matches) > 1:
        names = [f"  {c['OrgUnitId']} – {c['Name']}" for c in matches]
        raise CourseNotFoundError(
            f"Ambiguous match '{identifier}'. Multiple courses found:\n"
            + "\n".join(names)
            + "\n\nUse the numeric OrgUnitId for an exact match."
        )
    raise CourseNotFoundError(
        f"Course '{identifier}' not found. Run: lighthouse courses"
    )


def _filter_courses_by_semester(
    enrollments: list[dict[str, Any]],
    semester: dict[str, Any],
) -> list[int]:
    """Filter enrollments to courses belonging to a specific semester.

    Matches by year suffix in course code (e.g. courses ending in "2025-2026").
    When multiple semesters share the same year (Sem III and Sem IV both
    "2025-2026"), uses _IV_ pattern to disambiguate: Sem IV courses have
    "_IV_" before the year in their course code.

    If semester has no year in its code (e.g. "S1", "S2"), matches by
    substring: course code must contain the semester code.
    """
    sem_code = semester.get("Code", "")
    sem_name = semester.get("Name", "")

    # Extract year from semester code (last segment after _)
    def _extract_year(code: str) -> str:
        if "_" not in code:
            return ""
        year_part = code.rsplit("_", 1)[-1]
        # Normalize abbreviated year to full form (e.g. "2025-2026")
        if "-" in year_part:
            y_parts = year_part.split("-")
            if len(y_parts) == 2 and len(y_parts[1]) == 2:
                y_parts[1] = y_parts[0][:2] + y_parts[1]
                return "-".join(y_parts)
        return year_part

    sem_year = _extract_year(sem_code)

    # Extract Roman numeral from semester name for disambiguation
    roman = ""
    rm = re.search(r'\bSem\s+([IVX]+)\b', sem_name, re.IGNORECASE)
    if rm:
        roman = rm.group(1).upper()

    result_ids: list[int] = []

    for e in enrollments:
        oid = int(e.get("OrgUnit", {}).get("Id", 0))
        if oid <= 0:
            continue

        course_code = str(e.get("OrgUnit", {}).get("Code", ""))

        if sem_year:
            # Year-based matching
            course_year = _extract_year(course_code.upper())
            if course_year != sem_year:
                continue
            # Year matches — apply Roman numeral disambiguation
            if roman == "IV":
                if "_IV_" in course_code.upper():
                    result_ids.append(oid)
            elif roman == "III":
                if "_IV_" not in course_code.upper():
                    result_ids.append(oid)
            elif roman == "II":
                # Match courses with _II_ pattern in code (Sem II)
                if "_II_" in course_code.upper() and "_III_" not in course_code.upper() and "_IV_" not in course_code.upper():
                    result_ids.append(oid)
            elif roman == "I":
                # Match courses with _I_ but not _II_ (Sem I only)
                if "_I_" in course_code.upper() and "_II_" not in course_code.upper() and "_III_" not in course_code.upper() and "_IV_" not in course_code.upper():
                    result_ids.append(oid)
            else:
                result_ids.append(oid)
        elif not sem_code:
            # No year in semester code and no code — include all courses
            result_ids.append(oid)
        else:
            # No year in semester code but has a code pattern — use substring matching
            if sem_code.upper() in course_code.upper():
                result_ids.append(oid)

    return result_ids


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
        toc = client.get_content_toc(org_id)
        course_name = _get_course_name(client, org_id)
    except SessionExpiredError as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1

    type_set = {t.strip().lower() for t in types.split(",")}
    valid_types = {"file", "html"}
    invalid_types = type_set - valid_types
    if invalid_types:
        for it in invalid_types:
            _error(f"Unknown content type: {it}")
        type_set = type_set & valid_types
    if not type_set:
        type_set = {"file"}

    all_topics = _flatten_all_topics(toc.get("Modules", []))
    downloadable = []
    for t in all_topics:
        type_id = t.get("type", "").lower()
        if type_id == "file" and "file" in type_set:
            downloadable.append(t)
        elif type_id == "html" and "html" in type_set:
            downloadable.append(t)

    if not downloadable and not include_assignments:
        if json_output:
            _output_json({"course_id": org_id, "files": [], "downloaded": 0, "errors": 0})
        else:
            print("No downloadable files found.")
        return 0

    folder_name = _resolve_course_folder_name(course_name, org_id)
    dest = root / folder_name
    manifest_path = dest / MANIFEST_FILENAME

    if force and manifest_path.exists():
        manifest_path.unlink()
    manifest = Manifest.load(manifest_path)

    if dry_run:
        plan = [
            {"topic_id": t["topic_id"], "title": t["title"], "path": t["path"]}
            for t in downloadable
        ]
        print(f"Would download {len(plan)} files to {dest}/\n")
        for t in plan:
            print(f"  [{t['topic_id']}] {t['title']}")
        if include_assignments:
            print("\n  (Assignment downloads not shown in dry-run)")
        return 0

    dest.mkdir(parents=True, exist_ok=True)

    downloaded = []
    errors = []
    for i, topic in enumerate(downloadable, 1):
        tid = topic["topic_id"]
        topic_type = topic.get("type", "").lower()
        try:
            if topic_type == "html":
                content, sanitized_name = client.get_topic_html(org_id, tid)
            else:
                content, filename = client.download_topic_file(org_id, tid)
                sanitized_name = _sanitize_filename(filename)
            rel_path = Path(topic["path"]).parent
            file_dest = dest / rel_path
            file_dest.mkdir(parents=True, exist_ok=True)
            filepath = file_dest / sanitized_name
            filepath.write_bytes(content)
            last_mod = topic.get("last_modified") or ""
            manifest.add_entry(str(tid), content=content, filename=sanitized_name, last_modified=last_mod)
            downloaded.append({"topic_id": tid, "filename": sanitized_name, "size": len(content), "path": str(filepath.relative_to(dest))})
            if not json_output:
                print(f"  [{i}/{len(downloadable)}] {filepath.relative_to(dest)} ({len(content)/1024:.0f} KB)")
        except Exception as e:
            errors.append({"topic_id": tid, "error": str(e)})
            print(f"  [{i}/{len(downloadable)}] FAILED topic {tid}: {e}", file=sys.stderr)

    # Download assignment attachments if requested
    assignments_downloaded = []
    assignment_errors = []
    if include_assignments and not dry_run:
        assignments_downloaded, assignment_errors = _download_assignments_for_course(
            client, org_id, dest, manifest, folder_ids=[assignment_id] if assignment_id else None
        )

    if downloaded or assignments_downloaded or errors or assignment_errors:
        manifest.save(manifest_path)

    if json_output:
        result_data = {
            "course_id": org_id,
            "course_name": course_name,
            "folder": str(dest),
            "manifest": str(manifest_path),
            "downloaded": downloaded,
            "errors": errors,
        }
        if include_assignments:
            result_data["assignments_downloaded"] = assignments_downloaded
            result_data["assignment_errors"] = assignment_errors
        _output_json(result_data)
        return 0  # JSON was already output
    else:
        total_content = len(downloaded)
        total_assign = len(assignments_downloaded)
        total = total_content + total_assign
        if total_assign > 0:
            print(f"\nAssignments: {total_assign} attachment(s) downloaded")
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
    try:
        semesters = client.get_semesters()
        enrollments = client.get_course_enrollments()
    except SessionExpiredError as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1

    if not semesters:
        _error("No semesters found.")
        return 1

    # Resolve semester
    sem = _resolve_semester(client, semester_filter)
    if sem is None:
        if semester_filter:
            _error(f"No semester matching '{semester_filter}'. Run: lighthouse semesters")
        else:
            _error("No semesters found.")
        return 1

    sem_name = sem.get("Name", "Unknown Semester")

    # Filter enrollments to semester
    semester_course_ids = set(_filter_courses_by_semester(enrollments, sem))

    # Collect --also courses
    also_ids: list[int] = []
    also_errors: list[str] = []
    for ident in also_courses:
        try:
            resolved = _resolve_also_course(client, ident)
            also_ids.append(resolved)
        except CourseNotFoundError as e:
            also_errors.append(str(e))

    # Deduplicate: remove --also courses already in semester scope
    unique_also_ids = [cid for cid in also_ids if cid not in semester_course_ids]

    # Final course list
    all_course_ids = list(semester_course_ids) + unique_also_ids
    if not all_course_ids:
        _error("No courses to download.")
        return 1

    if json_output:
        # Structured JSON multi-course download
        rc = 0
        courses_results = []
        for cid in all_course_ids:
            try:
                toc = client.get_content_toc(cid)
                cname = _get_course_name(client, cid)
            except SessionExpiredError as e:
                _error(str(e))
                rc = 1
                continue
            except Exception as e:
                _error(str(e))
                rc = 1
                continue

            type_set = {t.strip().lower() for t in types.split(",")}
            valid_types = {"file", "html"}
            type_set = (type_set & valid_types) or {"file"}

            all_topics = _flatten_all_topics(toc.get("Modules", []))
            downloadable = []
            for t in all_topics:
                type_id = t.get("type", "").lower()
                if type_id == "file" and "file" in type_set:
                    downloadable.append(t)
                elif type_id == "html" and "html" in type_set:
                    downloadable.append(t)

            folder_name = _resolve_course_folder_name(cname, cid)
            dest = root / folder_name

            if dry_run:
                courses_results.append({
                    "course_id": cid,
                    "course_name": cname,
                    "semester": sem_name,
                    "root": str(dest),
                    "manifest_total": 0,
                    "downloaded": [],
                    "skipped": [],
                    "updated": [],
                    "duplicates": [],
                    "errors": [],
                })
                continue

            dest.mkdir(parents=True, exist_ok=True)
            manifest_path = dest / MANIFEST_FILENAME
            if force and manifest_path.exists():
                manifest_path.unlink()
            manifest = Manifest.load(manifest_path)

            downloaded = []
            errors = []
            sha_hashes: dict[str, list[dict]] = {}
            for topic in downloadable:
                tid = topic["topic_id"]
                topic_type = topic.get("type", "").lower()
                try:
                    if topic_type == "html":
                        content, sanitized_name = client.get_topic_html(cid, tid)
                    else:
                        content, filename = client.download_topic_file(cid, tid)
                        sanitized_name = _sanitize_filename(filename)
                    rel_path = Path(topic["path"]).parent
                    file_dest = dest / rel_path
                    file_dest.mkdir(parents=True, exist_ok=True)
                    filepath = file_dest / sanitized_name
                    filepath.write_bytes(content)
                    last_mod = topic.get("last_modified") or ""
                    file_hash = compute_sha256(content)
                    manifest.add_entry(str(tid), content=content, filename=sanitized_name, last_modified=last_mod)
                    sha_hashes.setdefault(file_hash, []).append({"topic_id": tid, "filename": sanitized_name})
                    downloaded.append({
                        "topic_id": tid,
                        "filename": sanitized_name,
                        "extension": Path(sanitized_name).suffix.lower(),
                        "path": str(filepath.relative_to(dest)),
                        "size_kb": round(len(content) / 1024, 1),
                        "sha256": file_hash,
                    })
                except Exception as e:
                    errors.append({"topic_id": tid, "filename": topic.get("title", ""), "error": str(e)})

            # Download assignment attachments if requested
            assignments_downloaded = []
            assignment_errors = []
            if include_assignments:
                assignments_downloaded, assignment_errors = _download_assignments_for_course(
                    client, cid, dest, manifest
                )

            if downloaded or assignments_downloaded or errors or assignment_errors:
                manifest.save(manifest_path)

            duplicates = []
            for file_hash, entries in sha_hashes.items():
                if len(entries) > 1:
                    for entry in entries:
                        duplicates.append({"topic_id": entry["topic_id"], "filename": entry["filename"], "sha256": file_hash})

            courses_results.append({
                "course_id": cid,
                "course_name": cname,
                "semester": sem_name,
                "root": str(dest),
                "manifest_total": len(manifest),
                "downloaded": downloaded,
                "skipped": [],
                "updated": [],
                "duplicates": duplicates,
                "errors": errors,
                "assignments_downloaded": assignments_downloaded,
                "assignment_errors": assignment_errors,
            })
            if errors:
                rc = 1

        summary = {
            "courses_checked": len(courses_results),
            "downloaded": sum(len(c["downloaded"]) for c in courses_results),
            "skipped": sum(len(c["skipped"]) for c in courses_results),
            "updated": sum(len(c["updated"]) for c in courses_results),
            "duplicates": sum(len(c["duplicates"]) for c in courses_results),
            "errors": sum(len(c["errors"]) for c in courses_results),
        }
        _output_json({
            "semester": {"id": int(sem["OrgUnitId"]), "name": sem_name},
            "synced_at": _utc_now_iso(),
            "summary": summary,
            "courses": courses_results,
            "also_errors": also_errors,
        })
        return rc

    # Human-readable multi-course download: delegate to _download_single_course
    print(f"Downloading courses from {sem_name}...\n")
    rc = 0
    for cid in all_course_ids:
        r = _download_single_course(client, cid, root, False, force, types, dry_run, include_assignments)
        if r != 0:
            rc = 1
    if also_errors:
        for err in also_errors:
            print(f"  Error: {err}", file=sys.stderr)
    if also_errors:
        rc = 1
    print(f"\nDownload complete.")
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
    try:
        semesters = client.get_semesters()
        enrollments = client.get_course_enrollments()
    except SessionExpiredError as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1

    if not semesters:
        _error("No semesters found.")
        return 1

    # Resolve semester
    sem = _resolve_semester(client, semester_filter)
    if sem is None:
        if semester_filter:
            _error(f"No semester matching '{semester_filter}'. Run: lighthouse semesters")
        else:
            _error("No semesters found.")
        return 1

    sem_name = sem.get("Name", "Unknown Semester")

    # Filter enrollments to semester
    semester_course_ids = set(_filter_courses_by_semester(enrollments, sem))

    # Collect --also courses
    also_ids: list[int] = []
    also_errors: list[str] = []
    for ident in also_courses:
        try:
            resolved = _resolve_also_course(client, ident)
            also_ids.append(resolved)
        except CourseNotFoundError as e:
            also_errors.append(str(e))

    # Deduplicate: remove --also courses already in semester scope
    unique_also_ids = [cid for cid in also_ids if cid not in semester_course_ids]

    # Final course list
    all_course_ids = list(semester_course_ids) + unique_also_ids
    if not all_course_ids:
        _error("No courses to sync.")
        return 1

    if json_output:
        # Structured JSON multi-course sync
        rc = 0
        courses_results = []
        for cid in all_course_ids:
            try:
                toc = client.get_content_toc(cid)
                cname = _get_course_name(client, cid)
            except SessionExpiredError as e:
                _error(str(e))
                rc = 1
                continue
            except Exception as e:
                _error(str(e))
                rc = 1
                continue

            type_set = {t.strip().lower() for t in types.split(",")}
            valid_types = {"file", "html"}
            type_set = (type_set & valid_types) or {"file"}

            all_topics = _flatten_all_topics(toc.get("Modules", []))
            downloadable = []
            for t in all_topics:
                type_id = t.get("type", "").lower()
                if type_id == "file" and "file" in type_set:
                    downloadable.append(t)
                elif type_id == "html" and "html" in type_set:
                    downloadable.append(t)

            folder_name = _resolve_course_folder_name(cname, cid)
            dest = root / folder_name

            manifest_path = dest / MANIFEST_FILENAME
            if force and manifest_path.exists():
                manifest_path.unlink()
            try:
                manifest = Manifest.load(manifest_path)
            except ManifestCorruptError:
                manifest = Manifest()

            downloaded_entries = []
            skipped_entries = []
            updated_entries = []
            orphaned_entries = []
            errors = []
            sha_hashes: dict[str, list[dict]] = {}
            manifest_topic_ids = set(manifest.entries.keys())

            for topic in downloadable:
                tid = str(topic["topic_id"])
                last_mod = topic.get("last_modified") or ""
                existing = manifest.get(tid)
                manifest_topic_ids.discard(tid)

                if existing is not None:
                    if existing.get("last_modified") == last_mod:
                        skipped_entries.append({
                            "topic_id": tid,
                            "filename": existing.get("filename", ""),
                            "path": existing.get("filename", ""),
                            "size_kb": round(existing.get("size", 0) / 1024, 1),
                            "sha256": existing.get("sha256", ""),
                        })
                        file_hash = existing.get("sha256", "")
                        if file_hash:
                            sha_hashes.setdefault(file_hash, []).append({"topic_id": tid, "filename": existing.get("filename", "")})
                        continue
                    try:
                        topic_type = topic.get("type", "").lower()
                        if topic_type == "html":
                            content, sanitized_name = client.get_topic_html(cid, int(tid))
                        else:
                            content, filename = client.download_topic_file(cid, int(tid))
                            sanitized_name = _sanitize_filename(filename)
                        rel_path = Path(topic["path"]).parent
                        file_dest = dest / rel_path
                        file_dest.mkdir(parents=True, exist_ok=True)
                        filepath = file_dest / sanitized_name
                        filepath.write_bytes(content)
                        file_hash = compute_sha256(content)
                        manifest.add_entry(tid, content=content, filename=sanitized_name, last_modified=last_mod)
                        sha_hashes.setdefault(file_hash, []).append({"topic_id": tid, "filename": sanitized_name})
                        updated_entries.append({
                            "topic_id": tid,
                            "filename": sanitized_name,
                            "extension": Path(sanitized_name).suffix.lower(),
                            "path": str(filepath.relative_to(dest)),
                            "size_kb": round(len(content) / 1024, 1),
                            "sha256": file_hash,
                        })
                    except Exception as e:
                        errors.append({"topic_id": tid, "filename": topic.get("title", ""), "error": str(e)})
                    continue

                try:
                    topic_type = topic.get("type", "").lower()
                    if topic_type == "html":
                        content, sanitized_name = client.get_topic_html(cid, int(tid))
                    else:
                        content, filename = client.download_topic_file(cid, int(tid))
                        sanitized_name = _sanitize_filename(filename)
                    rel_path = Path(topic["path"]).parent
                    file_dest = dest / rel_path
                    file_dest.mkdir(parents=True, exist_ok=True)
                    filepath = file_dest / sanitized_name
                    filepath.write_bytes(content)
                    file_hash = compute_sha256(content)
                    manifest.add_entry(tid, content=content, filename=sanitized_name, last_modified=last_mod)
                    sha_hashes.setdefault(file_hash, []).append({"topic_id": tid, "filename": sanitized_name})
                    downloaded_entries.append({
                        "topic_id": tid,
                        "filename": sanitized_name,
                        "extension": Path(sanitized_name).suffix.lower(),
                        "path": str(filepath.relative_to(dest)),
                        "size_kb": round(len(content) / 1024, 1),
                        "sha256": file_hash,
                    })
                except Exception as e:
                    errors.append({"topic_id": tid, "filename": topic.get("title", ""), "error": str(e)})

            for tid in manifest_topic_ids:
                entry = manifest.get(tid)
                if entry:
                    orphaned_entries.append({
                        "topic_id": tid,
                        "filename": entry.get("filename", ""),
                        "size_kb": round(entry.get("size", 0) / 1024, 1),
                        "sha256": entry.get("sha256", ""),
                    })

            # Sync assignment attachments if requested
            assignments_downloaded = []
            assignments_skipped = []
            assignments_updated = []
            assignment_errors = []
            if include_assignments:
                assignments_downloaded, assignments_skipped, assignments_updated, assignment_errors = _sync_assignments_for_course(
                    client, cid, dest, manifest
                )

            if downloaded_entries or updated_entries or assignments_downloaded or assignments_updated or errors or assignment_errors:
                manifest.save(manifest_path)

            duplicates = []
            for file_hash, entries in sha_hashes.items():
                if len(entries) > 1:
                    for entry in entries:
                        duplicates.append({"topic_id": entry["topic_id"], "filename": entry["filename"], "sha256": file_hash})

            courses_results.append({
                "course_id": cid,
                "course_name": cname,
                "semester": sem_name,
                "root": str(dest),
                "manifest_total": len(manifest),
                "downloaded": downloaded_entries,
                "skipped": skipped_entries,
                "updated": updated_entries,
                "duplicates": duplicates,
                "errors": errors,
                "assignments_downloaded": assignments_downloaded,
                "assignments_skipped": assignments_skipped,
                "assignments_updated": assignments_updated,
                "assignment_errors": assignment_errors,
            })
            if errors or assignment_errors:
                rc = 1

        summary = {
            "courses_checked": len(courses_results),
            "downloaded": sum(len(c["downloaded"]) for c in courses_results),
            "skipped": sum(len(c["skipped"]) for c in courses_results),
            "updated": sum(len(c["updated"]) for c in courses_results),
            "duplicates": sum(len(c["duplicates"]) for c in courses_results),
            "errors": sum(len(c["errors"]) for c in courses_results),
            "assignments_downloaded": sum(len(c.get("assignments_downloaded", [])) for c in courses_results),
            "assignment_errors": sum(len(c.get("assignment_errors", [])) for c in courses_results),
        }
        _output_json({
            "semester": {"id": int(sem["OrgUnitId"]), "name": sem_name},
            "synced_at": _utc_now_iso(),
            "summary": summary,
            "courses": courses_results,
            "also_errors": also_errors,
        })
        return rc

    # Human-readable sync output
    print(f"Syncing courses from {sem_name}...\n")
    rc = 0
    for cid in all_course_ids:
        r = _sync_single_course(client, cid, root, False, force, types, include_assignments)
        if r != 0:
            rc = 1
    if also_errors:
        for err in also_errors:
            print(f"  Error: {err}", file=sys.stderr)
    return rc


def _get_course_name(client: LighthouseClient, org_id: int) -> str:
    """Get the D2L course Name for an org unit."""
    courses = client.get_courses()
    for c in courses:
        if int(c.get("OrgUnitId", 0)) == org_id:
            return c.get("Name", f"Course-{org_id}")
    return f"Course-{org_id}"


# ---------------------------------------------------------------------------
# Assignment attachment download helpers
# ---------------------------------------------------------------------------

def _assignment_key(folder_id: int, file_id: int) -> str:
    """Generate a namespaced manifest key for an assignment attachment."""
    return f"assignment_{folder_id}_{file_id}"


def _disambiguate_filename(dest_dir: Path, filename: str) -> Path:
    """Return a Path with disambiguation suffix if filename already exists."""
    filepath = dest_dir / filename
    if not filepath.exists():
        return filepath
    # Split name and extension
    name = filepath.stem
    ext = filepath.suffix
    counter = 1
    while True:
        new_path = dest_dir / f"{name}_{counter}{ext}"
        if not new_path.exists():
            return new_path
        counter += 1


def _download_single_attachment(
    client: LighthouseClient,
    org_id: int,
    folder_id: int,
    attachment_id: int,
    root: Path,
    json_output: bool,
) -> int:
    """Download a single assignment attachment by folder and file ID.

    Returns exit code (0 on success, 1 on error).
    """
    try:
        course_name = _get_course_name(client, org_id)
        folder_detail = client.get_dropbox_folder_detail(org_id, folder_id)
    except SessionExpiredError as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1

    folder_name = folder_detail.get("Name", f"Folder-{folder_id}")
    folder_name_sanitized = _sanitize_filename(folder_name)

    try:
        content, filename = client.download_attachment(org_id, folder_id, attachment_id)
        if not filename:
            filename = f"attachment_{attachment_id}"
        sanitized_name = _sanitize_filename(filename)
    except Exception as e:
        _error(f"FAILED attachment {attachment_id}: {e}")
        return 1

    folder_name = _resolve_course_folder_name(course_name, org_id)
    dest = root / folder_name
    assignments_dir = dest / "Assignments" / folder_name_sanitized
    assignments_dir.mkdir(parents=True, exist_ok=True)
    filepath = _disambiguate_filename(assignments_dir, sanitized_name)
    filepath.write_bytes(content)

    manifest_path = dest / MANIFEST_FILENAME
    manifest = Manifest.load(manifest_path)
    key = _assignment_key(folder_id, attachment_id)
    manifest.add_entry(key, content=content, filename=sanitized_name, last_modified="")
    manifest.save(manifest_path)

    if json_output:
        _output_json({
            "course_id": org_id,
            "folder_id": folder_id,
            "file_id": attachment_id,
            "path": str(filepath),
            "size_kb": round(len(content) / 1024, 1),
            "filename": sanitized_name,
        })
    else:
        print(f"Downloaded: {filepath} ({len(content)/1024:.1f} KB)")
    return 0


def _download_assignments_for_course(
    client: LighthouseClient,
    org_id: int,
    dest: Path,
    manifest: Manifest,
    folder_ids: list[int] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Download all assignment attachments for a course.

    Returns (downloaded_entries, errors).
    """
    try:
        all_folders = client.get_dropbox_folders(org_id)
    except Exception as e:
        return [], [{"error": str(e), "type": "assignment_list"}]

    # Filter to specific folder IDs if given
    if folder_ids is not None:
        folders = [f for f in all_folders if f.get("Id") in folder_ids]
    else:
        folders = all_folders

    downloaded_entries = []
    errors = []

    for folder in folders:
        folder_id = folder.get("Id")
        if not folder_id:
            continue

        folder_name = _sanitize_filename(folder.get("Name", f"Folder-{folder_id}"))
        attachments = folder.get("Attachments", []) or []

        for att in attachments:
            att_id = att.get("Id")
            att_type = att.get("Type", "File")
            if att_type != "File" or not att_id:
                continue  # Skip link-type attachments

            att_key = _assignment_key(folder_id, att_id)
            existing = manifest.get(att_key)
            if existing is not None:
                # Already in manifest, skip
                continue

            try:
                content, filename = client.download_attachment(org_id, folder_id, att_id)
                if not filename:
                    filename = f"attachment_{att_id}"
                sanitized_name = _sanitize_filename(filename)
            except Exception as e:
                errors.append({"folder_id": folder_id, "file_id": att_id, "error": str(e)})
                print(f"  FAILED attachment {att_id}: {e}", file=sys.stderr)
                continue

            assignments_dir = dest / "Assignments" / folder_name
            assignments_dir.mkdir(parents=True, exist_ok=True)
            filepath = _disambiguate_filename(assignments_dir, sanitized_name)
            filepath.write_bytes(content)
            manifest.add_entry(att_key, content=content, filename=sanitized_name, last_modified="")
            downloaded_entries.append({
                "file_id": att_id,
                "folder_id": folder_id,
                "filename": sanitized_name,
                "path": str(filepath.relative_to(dest)),
                "size_kb": round(len(content) / 1024, 1),
            })

    return downloaded_entries, errors


def _sync_assignments_for_course(
    client: LighthouseClient,
    org_id: int,
    dest: Path,
    manifest: Manifest,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Sync assignment attachments for a course (detect new/updated).

    Returns (downloaded_entries, skipped_entries, updated_entries, errors).
    """
    try:
        all_folders = client.get_dropbox_folders(org_id)
    except Exception as e:
        return [], [], [], [{"error": str(e), "type": "assignment_list"}]

    downloaded_entries = []
    skipped_entries = []
    updated_entries = []
    errors = []

    for folder in all_folders:
        folder_id = folder.get("Id")
        if not folder_id:
            continue

        folder_name = _sanitize_filename(folder.get("Name", f"Folder-{folder_id}"))

        # Get full folder details including attachments
        try:
            folder_detail = client.get_dropbox_folder_detail(org_id, folder_id)
        except Exception as e:
            errors.append({"folder_id": folder_id, "error": str(e)})
            continue

        attachments = folder_detail.get("Attachments", []) or []

        for att in attachments:
            att_id = att.get("Id")
            att_type = att.get("Type", "File")
            if att_type != "File" or not att_id:
                continue

            att_key = _assignment_key(folder_id, att_id)
            existing = manifest.get(att_key)
            att_size = att.get("Size", 0)

            if existing is not None:
                # Check if size changed (update detection)
                if existing.get("size") == att_size:
                    skipped_entries.append({
                        "file_id": att_id,
                        "folder_id": folder_id,
                        "filename": existing.get("filename", ""),
                    })
                    continue
                # Size changed — re-download (update)
                try:
                    content, filename = client.download_attachment(org_id, folder_id, att_id)
                    if not filename:
                        filename = f"attachment_{att_id}"
                    sanitized_name = _sanitize_filename(filename)
                except Exception as e:
                    errors.append({"folder_id": folder_id, "file_id": att_id, "error": str(e)})
                    continue
                assignments_dir = dest / "Assignments" / folder_name
                assignments_dir.mkdir(parents=True, exist_ok=True)
                filepath = _disambiguate_filename(assignments_dir, sanitized_name)
                filepath.write_bytes(content)
                manifest.add_entry(att_key, content=content, filename=sanitized_name, last_modified="")
                updated_entries.append({
                    "file_id": att_id,
                    "folder_id": folder_id,
                    "filename": sanitized_name,
                    "path": str(filepath.relative_to(dest)),
                    "size_kb": round(len(content) / 1024, 1),
                })
            else:
                # New attachment
                try:
                    content, filename = client.download_attachment(org_id, folder_id, att_id)
                    if not filename:
                        filename = f"attachment_{att_id}"
                    sanitized_name = _sanitize_filename(filename)
                except Exception as e:
                    errors.append({"folder_id": folder_id, "file_id": att_id, "error": str(e)})
                    continue
                assignments_dir = dest / "Assignments" / folder_name
                assignments_dir.mkdir(parents=True, exist_ok=True)
                filepath = _disambiguate_filename(assignments_dir, sanitized_name)
                filepath.write_bytes(content)
                manifest.add_entry(att_key, content=content, filename=sanitized_name, last_modified="")
                downloaded_entries.append({
                    "file_id": att_id,
                    "folder_id": folder_id,
                    "filename": sanitized_name,
                    "path": str(filepath.relative_to(dest)),
                    "size_kb": round(len(content) / 1024, 1),
                })

    return downloaded_entries, skipped_entries, updated_entries, errors


# ---------------------------------------------------------------------------
# Grades, announcements, calendar, quizzes — all use _for_course_or_all
# ---------------------------------------------------------------------------

def cmd_grades(
    course_id: str | None = None,
    json_output: bool = False,
) -> int:
    """Show grades for a course or all courses."""
    client = LighthouseClient()
    try:
        return _for_course_or_all(client, course_id, _show_course_grades, json_output, "grades")
    except (SessionExpiredError, CourseNotFoundError) as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1


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
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1

    # Merge schema + values
    val_map = {str(v.get("GradeObjectIdentifier", v.get("GradeObjectId", ""))): v for v in values}
    merged = []
    for g in schema:
        gid = str(g["Id"])
        v = val_map.get(gid, {})
        num = v.get("PointsNumerator")
        den = v.get("PointsDenominator") or g.get("MaxPoints", "–")
        if num is not None and den is not None:
            grade_str = f"{num}/{den}"
        else:
            grade_str = f"–/{den}"
        merged.append({
            "name": g.get("Name", ""),
            "grade": grade_str,
            "weight": g.get("Weight", ""),
            "type": g.get("GradeType", ""),
        })

    payload = {"course_id": org_id, "grades": merged}

    if json_output:
        return payload

    label = title or str(org_id)
    rows = [[m["name"], m["grade"], str(m["weight"]), m["type"]] for m in merged]
    _print_table(["Item", "Grade", "Weight", "Type"], rows, title=f"Grades – {label}")
    return 0


def cmd_announcements(
    course_id: str | None = None,
    json_output: bool = False,
) -> int:
    """Show announcements for a course or all courses."""
    client = LighthouseClient()
    try:
        return _for_course_or_all(client, course_id, _show_announcements, json_output, "announcements")
    except (SessionExpiredError, CourseNotFoundError) as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1


def _show_announcements(
    client: LighthouseClient,
    org_id: int,
    json_output: bool,
    title: str | None = None,
) -> int | dict:
    """Display announcements for a single course."""
    try:
        announcements = client.get_announcements(org_id)
    except SessionExpiredError as e:
        _error(str(e))
        return 1
    except Exception:
        if json_output:
            return {"course_id": org_id, "announcements": []}
        return 0

    payload = {"course_id": org_id, "announcements": announcements}

    if json_output:
        return payload

    if not announcements:
        return 0

    label = title or str(org_id)
    print(f"\n📢 {label}")
    for a in announcements:
        date = _fmt_date(a.get("CreatedDate"))
        print(f"  [{date}] {a.get('Title', '')}")
        body = a.get("Body", {}).get("Text", "")
        if body:
            print(f"    {_short(body.strip(), 80)}")
        attachments = a.get("Attachments", [])
        for att in attachments:
            print(f"    📎 {att.get('FileName', '')} ({att.get('Size', 0)/1024:.0f} KB)")
    return 0


def cmd_calendar(
    course_id: str | None = None,
    json_output: bool = False,
) -> int:
    """Show calendar events for a course or all courses."""
    client = LighthouseClient()
    try:
        return _for_course_or_all(client, course_id, _show_calendar, json_output, "events")
    except (SessionExpiredError, CourseNotFoundError) as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1


def _show_calendar(
    client: LighthouseClient,
    org_id: int,
    json_output: bool,
    title: str | None = None,
) -> int | dict:
    """Display calendar events for a single course."""
    try:
        events = client.get_calendar(org_id)
    except SessionExpiredError as e:
        _error(str(e))
        return 1
    except Exception:
        if json_output:
            return {"course_id": org_id, "events": []}
        return 0

    payload = {"course_id": org_id, "events": events}

    if json_output:
        return payload

    if not events:
        return 0

    label = title or str(org_id)
    rows = []
    for e in events:
        start = _fmt_date(e.get("StartDateTime"))
        rows.append([start, _short(e.get("Title", ""), 40), e.get("OrgUnitName", "")])

    _print_table(["Date", "Title", "Course"], rows, title=f"Calendar – {label}")
    return 0


def cmd_assignments(
    course_id: str | None = None,
    json_output: bool = False,
) -> int:
    """Show dropbox folders (assignments) for a course or all courses."""
    client = LighthouseClient()
    try:
        return _for_course_or_all(client, course_id, _show_course_assignments, json_output, "assignments")
    except (SessionExpiredError, CourseNotFoundError) as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode HTML entities from text."""
    import html
    # First decode HTML entities (e.g. &amp; -> &, &lt; -> <)
    decoded = html.unescape(text)
    # Then strip tags
    return re.sub(r'<[^>]+>', '', decoded).strip()


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
        _error(str(e))
        return 1
    except Exception:
        if json_output:
            return {"course_id": org_id, "assignments": []}
        return 0

    # Process folders into structured format
    assignments = []
    for f in folders:
        # Extract attachments info
        attachments_raw = f.get("Attachments", []) or []
        attachments = []
        for att in attachments_raw:
            att_type = att.get("Type", "File")
            attachments.append({
                "file_id": att.get("Id"),
                "file_name": att.get("FileName", ""),
                "size": att.get("Size", 0),
                "attachment_type": att_type,  # "File" or "Link"
            })

        # Custom instructions (may contain HTML)
        instructions = f.get("CustomInstructions", "") or ""
        instructions_plain = _strip_html(instructions) if instructions else ""

        # Availability info
        availability = f.get("Availability", {}) or {}
        avail_info = None
        if availability:
            start = availability.get("StartDate")
            end = availability.get("EndDate")
            if start or end:
                avail_info = {"start": start, "end": end}

        # Due date
        due_date = f.get("DueDate") or f.get("Due", "")

        # Submission type
        submission_type = f.get("CategoryName", "") or f.get("SubmissionType", "")

        folder_id = f.get("Id") or f.get("FolderId", "")

        assignments.append({
            "folder_id": folder_id,
            "name": _strip_html(f.get("Name", "")),
            "due_date": due_date,
            "attachment_count": len(attachments),
            "attachments": attachments,
            "custom_instructions": instructions if instructions else None,
            "custom_instructions_preview": _short(instructions_plain, 80) if instructions_plain else None,
            "submission_type": submission_type,
            "availability": avail_info,
        })

    payload = {"course_id": org_id, "assignments": assignments}

    if json_output:
        return payload

    if not assignments:
        label = title or str(org_id)
        print(f"\n📋 {label}")
        print("  No assignments found for this course.")
        return 0

    label = title or str(org_id)
    print(f"\n📋 {label}")
    # Table: ID, Name, Due Date, Attachments
    rows = []
    for a in assignments:
        due = _fmt_date(a["due_date"])
        count = a["attachment_count"]
        avail_note = ""
        if a["availability"]:
            avail = a["availability"]
            if avail.get("start"):
                avail_note = f" (opens {_fmt_date(avail['start'])})"
            elif avail.get("end"):
                avail_note = f" (closed {_fmt_date(avail['end'])})"
        name = _short(a["name"], 40)
        rows.append([str(a["folder_id"]), name, due, str(count)])
    _print_table(["ID", "Name", "Due Date", "Attachments"], rows)

    # Preview custom instructions if any
    for a in assignments:
        if a["custom_instructions_preview"]:
            print(f"  → [{a['folder_id']}] Instructions: {a['custom_instructions_preview']}")

    # Show availability notes
    for a in assignments:
        if a["availability"]:
            avail = a["availability"]
            if avail.get("start"):
                print(f"  → [{a['folder_id']}] Opens: {_fmt_date(avail['start'])}")
            if avail.get("end"):
                print(f"  → [{a['folder_id']}] Closes: {_fmt_date(avail['end'])}")

    return 0


def cmd_quizzes(
    client: LighthouseClient,
    org_id: int,
    json_output: bool,
    title: str | None = None,
) -> int | dict:
    """Display quizzes for a single course."""
    try:
        quizzes = client.get_quizzes(org_id)
    except SessionExpiredError as e:
        _error(str(e))
        return 1
    except Exception:
        if json_output:
            return {"course_id": org_id, "quizzes": []}
        return 0

    payload = {"course_id": org_id, "quizzes": quizzes}

    if json_output:
        return payload

    if not quizzes:
        return 0

    label = title or str(org_id)
    rows = []
    for q in quizzes:
        start = _fmt_date(q.get("StartDate"))
        end = _fmt_date(q.get("EndDate"))
        rows.append([str(q.get("QuizId", "")), _short(q.get("Name", ""), 35), start, end])

    _print_table(["ID", "Name", "Start", "End"], rows, title=f"Quizzes – {label}")
    return 0


def cmd_submit(
    course_id: str,
    folder_id: str,
    file_path: str,
    yes: bool = False,
    json_output: bool = False,
) -> int:
    """Submit a file to a dropbox folder.

    COURSE_ID is the course identifier (name substring or numeric OrgUnitId).
    FOLDER_ID is the dropbox folder identifier (numeric ID or name substring).

    Prompts for confirmation before submitting (unless --yes is set).
    Shows course name, folder name, and file path before submitting.

    On success, prints JSON with submission details (submission_id, folder_id,
    folder_name, course_id, course_name, file, submitted_at).

    Non-interactive / agent-friendly: --yes + --json = only JSON on stdout.
    """
    client = LighthouseClient()

    # Resolve course ID (numeric or name substring)
    try:
        org_id = resolve_course_id(client, course_id)
    except CourseNotFoundError as e:
        _error(str(e))
        return 1
    except SessionExpiredError as e:
        _error(str(e))
        return 1

    # Get course name for display
    try:
        course_name = _get_course_name(client, org_id)
    except SessionExpiredError as e:
        _error(str(e))
        return 1

    # Resolve folder ID (numeric or name substring)
    try:
        folder_id_int = _resolve_folder_id(client, org_id, folder_id)
    except (FileNotFoundError, ValueError) as e:
        _error(str(e))
        return 1
    except SessionExpiredError as e:
        _error(str(e))
        return 1
    folder_name = _get_folder_name(client, org_id, folder_id_int)

    # Check file exists (fail fast before API call)
    file_path_obj = Path(file_path).expanduser().resolve()
    if not file_path_obj.exists():
        _error(f"File not found: {file_path}")
        return 1

    # Read file bytes
    try:
        file_bytes = file_path_obj.read_bytes()
    except OSError as e:
        _error(f"Could not read file: {e}")
        return 1

    filename = file_path_obj.name

    # Confirmation prompt (skip with --yes or non-TTY)
    if not yes:
        if not sys.stdout.isatty():
            _error("Refusing to submit without --yes in non-interactive mode.")
            _error("Use --yes flag to confirm submission in non-interactive/agent context.")
            return 1

        print(f"Submit to '{folder_name}' in '{course_name}'?")
        print(f"  File: {file_path_obj}")
        response = input("Confirm [y/N]: ").strip().lower()
        if response not in ("y", "yes"):
            print("Submission cancelled.")
            return 0

    # Make the submission
    try:
        result = client.submit_file(
            org_unit_id=org_id,
            folder_id=folder_id_int,
            file_bytes=file_bytes,
            filename=filename,
            description=f"Submitted via lighthouse-cli: {filename}",
        )
    except SessionExpiredError as e:
        _error(str(e))
        return 1
    except PermissionError as e:
        _error(str(e))
        return 1
    except FileNotFoundError as e:
        _error(str(e))
        _error("Run: lighthouse assignments")
        return 1
    except ValueError as e:
        _error(str(e))
        return 1

    # Build output
    submitted_at = result.get("submittedAt", _utc_now_iso())
    submission_id = result.get("submissionId", 0)
    file_info = {
        "name": filename,
        "size_bytes": len(file_bytes),
    }

    if json_output:
        _output_json({
            "submission_id": submission_id,
            "folder_id": folder_id_int,
            "folder_name": folder_name,
            "course_id": org_id,
            "course_name": course_name,
            "file": file_info,
            "submitted_at": submitted_at,
        })
    else:
        print(f"Submitted successfully!")
        print(f"  Submission ID: {submission_id}")
        print(f"  Folder: {folder_name}")
        print(f"  Course: {course_name}")
        print(f"  File: {filename}")
        print(f"  Submitted at: {submitted_at}")

    return 0


def _resolve_folder_id(client: LighthouseClient, org_id: int, identifier: str) -> int:
    """Resolve a folder identifier (numeric ID or name substring) to an int folder ID.

    Raises FileNotFoundError if zero matches (with suggestions to run assignments).
    Raises ValueError if multiple matches (ambiguous).
    """
    # Try numeric first
    try:
        fid = int(identifier)
        # Verify it exists
        folders = client.get_dropbox_folders(org_id)
        for f in folders:
            if f.get("Id") == fid:
                return fid
        raise FileNotFoundError(
            f"Folder '{identifier}' not found in course {org_id}. "
            "Run: lighthouse assignments"
        )
    except ValueError:
        pass

    # Name substring match
    folders = client.get_dropbox_folders(org_id)
    lower_id = identifier.lower()
    matches = [
        f for f in folders
        if lower_id in f.get("Name", "").lower()
    ]
    if len(matches) == 1:
        return int(matches[0]["Id"])
    if len(matches) > 1:
        names = [f"  {f['Id']} – {f['Name']}" for f in matches]
        raise ValueError(
            f"Ambiguous match '{identifier}'. Multiple folders found:\n"
            + "\n".join(names)
            + "\n\nUse the numeric FolderId for an exact match."
        )
    # Zero matches — show available folders in error
    available = [f"  {f['Id']} – {f.get('Name', 'Unnamed')}" for f in folders]
    if available:
        available_list = "\n".join(available)
        raise FileNotFoundError(
            f"Folder '{identifier}' not found in course {org_id}.\n"
            f"Available folders:\n{available_list}\n\n"
            "Run: lighthouse assignments"
        )
    else:
        raise FileNotFoundError(
            f"No dropbox folders found in course {org_id}. "
            "Run: lighthouse assignments"
        )


def _get_folder_name(client: LighthouseClient, org_id: int, folder_id: int) -> str:
    """Get the name of a dropbox folder by ID."""
    try:
        detail = client.get_dropbox_folder_detail(org_id, folder_id)
        return detail.get("Name", f"Folder-{folder_id}")
    except Exception:
        return f"Folder-{folder_id}"


def cmd_quiz_detail(
    course_id: str,
    quiz_id: int,
    json_output: bool = False,
) -> int:
    """Show detailed info for a specific quiz."""
    client = LighthouseClient()
    try:
        org_id = resolve_course_id(client, course_id)
        quiz = client.get_quiz_detail(org_id, quiz_id)
    except (SessionExpiredError, CourseNotFoundError) as e:
        _error(str(e))
        return 1
    except Exception as e:
        _error(str(e))
        return 1

    if json_output:
        # Wrap with course_id for consistency
        _output_json({"course_id": org_id, "quiz": quiz})
        return 0

    # Human-readable quiz detail
    attempts = quiz.get("AttemptsAllowed", {})
    if attempts.get("IsUnlimited"):
        attempts_str = "Unlimited"
    else:
        attempts_str = str(attempts.get("NumberOfAttemptsAllowed", "?"))

    time_limit = quiz.get("SubmissionTimeLimit", {})
    if time_limit.get("IsEnforced"):
        tl_str = f"{time_limit.get('TimeLimitValue', '?')} min"
    else:
        tl_str = "None"

    desc = quiz.get("Description", {})
    desc_text = desc.get("Text", {}).get("Html", "") or desc.get("Text", {}).get("Text", "") if isinstance(desc.get("Text"), dict) else ""
    desc_text = re.sub(r'<[^>]+>', '', desc_text).strip() if desc_text else ""

    instructions = quiz.get("Instructions", {})
    instr_text = instructions.get("Text", {}).get("Html", "") or instructions.get("Text", {}).get("Text", "") if isinstance(instructions.get("Text"), dict) else ""
    instr_text = re.sub(r'<[^>]+>', '', instr_text).strip() if instr_text else ""

    print(f"\n📝 {quiz.get('Name', 'Quiz')}")
    print(f"   ID: {quiz.get('QuizId')}")
    print(f"   Active: {'Yes' if quiz.get('IsActive') else 'No'}")
    print(f"   Start: {_fmt_date(quiz.get('StartDate'))}")
    print(f"   End: {_fmt_date(quiz.get('EndDate'))}")
    print(f"   Due: {_fmt_date(quiz.get('DueDate'))}")
    print(f"   Attempts: {attempts_str}")
    print(f"   Time Limit: {tl_str}")
    print(f"   Shuffle Questions: {'Yes' if quiz.get('Shuffle') else 'No'}")
    print(f"   Prevent Moving Back: {'Yes' if quiz.get('PreventMovingBackwards') else 'No'}")
    print(f"   Single Session: {'Yes' if quiz.get('IsSingleSession') else 'No'}")
    print(f"   Allow Hints: {'Yes' if quiz.get('AllowHints') else 'No'}")
    print(f"   Auto-export to Grades: {'Yes' if quiz.get('AutoExportToGrades') else 'No'}")
    if desc_text:
        print(f"\n   Description: {_short(desc_text, 200)}")
    if instr_text:
        print(f"   Instructions: {_short(instr_text, 200)}")

    print(f"\n   ⚠ Quiz questions and past attempts require instructor-level API access.")
    print(f"   View in browser: {BASE_URL}/d2l/lms/quizzing/user/quizzes_list.d2l?ou={org_id}")
    return 0


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _resolve_course_folder_name(course_name: str, org_unit_id: int) -> str:
    """Sanitize a course name for use as a folder name.

    Two courses with the same Name get disambiguated by appending -OrgUnitId.
    """
    base = _sanitize_filename(course_name)
    return f"{base}-{org_unit_id}"


def _resolve_course_folder_name_sanitized(course_name: str, org_unit_id: int) -> str:
    """Resolve the course folder name without disambiguation suffix.

    Two courses with the same Name get disambiguated by appending -OrgUnitId.
    This version returns the folder name without the suffix (for parent dir).
    """
    return _sanitize_filename(course_name)
