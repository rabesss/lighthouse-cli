"""Course tracking and semester mapping configuration.

Manages the course-config.json file that maps org-unit-ids to
{name, semester} pairs, plus the CLI command for managing it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from .api import LighthouseClient
from .config import CONFIG_DIR
from .display import (
    error as _error,
    output_json as _output_json,
    print_table as _print_table,
    safe_display_text,
    short as _short,
)
from .utils import atomic_write

COURSE_CONFIG_FILE = CONFIG_DIR / "course-config.json"
_MAX_COURSE_ID = (1 << 63) - 1
_MAX_COURSE_ID_DIGITS = len(str(_MAX_COURSE_ID))
_MAX_CATALOG_TEXT_LENGTH = 256


def _positive_course_id(value: object) -> int | None:
    """Coerce only positive integer-like enrollment IDs."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= _MAX_COURSE_ID else None
    if isinstance(value, str):
        candidate = value.strip()
        if (
            not candidate
            or len(candidate) > _MAX_COURSE_ID_DIGITS
            or not candidate.isascii()
            or not candidate.isdecimal()
        ):
            return None
        try:
            course_id = int(candidate)
        except ValueError:
            return None
        return course_id if 0 < course_id <= _MAX_COURSE_ID else None
    return None


def _safe_catalog_text(value: object, fallback: str) -> str:
    """Project an enrollment label onto a bounded printable scalar."""
    return safe_display_text(value, fallback, max_len=_MAX_CATALOG_TEXT_LENGTH)


def _safe_tracked_labels(entry: Mapping[str, object]) -> tuple[str, str]:
    """Project stored course labels before they reach output or a prompt."""
    return (
        _safe_catalog_text(entry.get("name"), "Unknown course"),
        _safe_catalog_text(entry.get("semester"), ""),
    )


def _normalise_enrollment_catalog(enrollments: object) -> list[dict[str, str]]:
    """Build a stable first-record-wins catalog from untrusted enrollments."""
    if not isinstance(enrollments, (list, tuple)):
        return []
    courses: dict[int, dict[str, str]] = {}
    for enrollment in enrollments:
        if not isinstance(enrollment, dict):
            continue
        org_unit = enrollment.get("OrgUnit")
        if not isinstance(org_unit, dict):
            continue
        course_id = _positive_course_id(org_unit.get("Id"))
        if course_id is None or course_id in courses:
            continue
        name = _safe_catalog_text(org_unit.get("Name"), "Unknown course")
        code = _safe_catalog_text(org_unit.get("Code"), "Unknown code")
        courses[course_id] = {
            "OrgUnitId": str(course_id),
            "Name": name,
            "Code": code,
        }
    return [courses[course_id] for course_id in sorted(courses)]


def semester_state(entry: Mapping[str, object] | None) -> dict[str, str]:
    """Describe a course's explicit local semester mapping.

    ``semester`` remains an empty string for backwards compatibility.  The
    source field lets consumers distinguish an unmapped course from one whose
    semester was accidentally omitted or inferred from an API label.
    This helper only reads the supplied mapping; it never writes config.
    """
    _, semester = _safe_tracked_labels(entry or {})
    mapped = bool(semester.strip())
    return {
        "semester": semester,
        "semester_source": "config" if mapped else "unmapped",
    }


def load() -> dict[str, dict[str, str]]:
    """Load course config from disk. Returns {org_unit_id: {name, semester}}."""
    if not COURSE_CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(COURSE_CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    tracked_courses = data.get("tracked_courses", {})
    if not isinstance(tracked_courses, dict):
        return {}

    # Treat the file as untrusted input. One malformed course must not make
    # list/JSON output crash or prevent valid sibling entries from loading.
    normalized: dict[str, dict[str, str]] = {}
    for org_id, entry in tracked_courses.items():
        if not isinstance(org_id, str) or not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        semester = entry.get("semester", "")
        normalized[org_id] = {
            "name": _safe_catalog_text(name, ""),
            "semester": _safe_catalog_text(semester, ""),
        }
    return normalized


def _entries(config: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    """Return tracked courses in the stable JSON/list display shape."""
    normalized: list[tuple[int, dict[str, str]]] = []
    seen_ids: set[int] = set()
    for raw_oid, entry in config.items():
        course_id = _positive_course_id(raw_oid)
        if course_id is None or not isinstance(entry, dict) or course_id in seen_ids:
            continue
        seen_ids.add(course_id)
        normalized.append((course_id, entry))
    entries: list[dict[str, str]] = []
    for course_id, entry in sorted(normalized, key=lambda item: item[0]):
        name, semester = _safe_tracked_labels(entry)
        entries.append({
            "id": str(course_id),
            "name": name,
            "semester": semester,
        })
    return entries


def save(config: dict[str, dict[str, str]]) -> None:
    """Save course config to disk atomically."""
    COURSE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        COURSE_CONFIG_FILE,
        json.dumps({"tracked_courses": config}, indent=2, ensure_ascii=False),
    )


def _config_error(message: BaseException | str, json_output: bool) -> int:
    """Emit a config failure without violating the leaf JSON contract."""
    return _error(
        message,
        json_output=json_output,
        payload={"courses": []},
    )


def cmd_config_courses(
    add: str | None = None,
    remove: str | None = None,
    semester: str | None = None,
    list_courses: bool = False,
    reset: bool = False,
    json_output: bool = False,
) -> int:
    """Manage course tracking and semester mapping.

    Without flags, runs an interactive setup that shows all enrolled courses
    and lets you pick which to track and assign semester labels.
    """
    try:
        config = load()
    except Exception as e:
        return _config_error(e, json_output)

    # --reset: clear all tracking
    if reset:
        try:
            save({})
        except Exception as e:
            return _config_error(e, json_output)
        if json_output:
            _output_json([])
        else:
            print("Course tracking config cleared.")
        return 0

    # --remove ID: untrack a course
    if remove is not None:
        remove_id = _positive_course_id(remove)
        config_id = (
            next(
                (
                    raw_id
                    for raw_id, entry in config.items()
                    if _positive_course_id(raw_id) == remove_id
                    and isinstance(entry, dict)
                ),
                None,
            )
            if remove_id is not None
            else None
        )
        if config_id is None:
            return _config_error(
                f"Course {remove} is not in your tracked courses.",
                json_output,
            )
        entry = config[config_id]
        name, _ = _safe_tracked_labels(entry)
        del config[config_id]
        try:
            save(config)
        except Exception as e:
            return _config_error(e, json_output)
        if json_output:
            _output_json(_entries(config))
        else:
            print(f"Stopped tracking {name} ({remove_id})")
        return 0

    # --list / --json: show tracked courses
    if list_courses or (json_output and add is None):
        if not config:
            if json_output:
                _output_json([])
            else:
                print("No courses tracked. Run: lighthouse config courses (without flags) to set up.")
            return 0
        entries = _entries(config)
        if json_output:
            _output_json(entries)
            return 0
        _print_table(
            ["ID", "Name", "Semester"],
            [[e["id"], _short(e["name"], 45), e["semester"].strip() or "Unmapped"] for e in entries],
            title=f"Tracked Courses ({len(entries)})",
        )
        return 0

    # Fetch enrollments (needed for both --add and interactive)
    try:
        client = LighthouseClient()
        all_enrollments = client.get_course_enrollments()
    except Exception as e:
        return _config_error(e, json_output)

    if not isinstance(all_enrollments, (list, tuple)):
        return _config_error("Invalid course enrollment response.", json_output)
    courses = _normalise_enrollment_catalog(all_enrollments)

    if add is not None:
        # Find the course in enrollments
        match = next(
            (
                (course["OrgUnitId"], course["Name"])
                for course in courses
                if course["OrgUnitId"] == add.strip()
                or course["Name"].casefold() == add.casefold()
            ),
            None,
        )
        if not match:
            return _config_error(
                f"Course '{add}' not found in your enrollments. Run: lighthouse courses",
                json_output,
            )

        oid, name = match
        config[oid] = {"name": name, "semester": semester or ""}
        try:
            save(config)
        except Exception as e:
            return _config_error(e, json_output)
        if json_output:
            _output_json(_entries(config))
        else:
            print(f"Tracking {name} ({oid}){f' -> {semester}' if semester else ''}")
        return 0

    # No flags: interactive setup

    print("\nAvailable courses (from API):")
    table_rows = []
    for course in courses:
        tracked = config.get(course["OrgUnitId"])
        tracked_semester = (
            _safe_tracked_labels(tracked)[1]
            if isinstance(tracked, Mapping)
            else ""
        )
        tracking = f"-> {tracked_semester}" if tracked_semester else ("tracked" if tracked else "")
        table_rows.append([
            course["OrgUnitId"],
            _short(course["Name"], 40),
            _short(course["Code"], 35),
            tracking,
        ])
    _print_table(
        ["ID", "Name", "Code", "Tracked"],
        table_rows,
        title=f"Enrolled Courses ({len(courses)})",
    )

    print("\nSelect courses to track (comma-separated IDs, or 'all'):")
    try:
        selection = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 0

    if not selection:
        print("No changes made.")
        return 0

    # Resolve IDs
    if selection.lower() == "all":
        selected_ids = {c["OrgUnitId"] for c in courses}
    else:
        selected_ids = set()
        for part in selection.split(","):
            if not (part := part.strip()):
                continue
            # Allow fuzzy name matching too
            matched = False
            for c in courses:
                if part == c["OrgUnitId"] or part.lower() in c["Name"].lower():
                    selected_ids.add(c["OrgUnitId"])
                    matched = True
            if not matched:
                print("  Warning: selected course was not found, skipping.")

    if not selected_ids:
        print("No valid courses selected.")
        return 1

    # Prompt for semester assignment
    course_lookup = {c["OrgUnitId"]: c["Name"] for c in courses}
    for oid in sorted(selected_ids, key=lambda x: int(x) if x.isdigit() else 0):
        name = course_lookup.get(oid, oid)
        tracked = config.get(oid)
        existing = (
            _safe_tracked_labels(tracked)[1]
            if isinstance(tracked, Mapping)
            else ""
        )
        prompt = f"  Semester for {name} ({oid}){' [' + existing + ']' if existing else ''}: "
        try:
            sem = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSaving partial changes...")
            break
        config[oid] = {"name": name, "semester": sem or existing}

    try:
        save(config)
    except Exception as e:
        return _config_error(e, json_output)
    print(f"\nUpdated tracking config: {len([oid for oid in selected_ids if oid in config])} course(s) updated.")
    print("View tracked courses: lighthouse config courses --list")
    return 0
