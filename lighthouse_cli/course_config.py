"""Course tracking and semester mapping configuration.

Manages the course-config.json file that maps org-unit-ids to
{name, semester} pairs, plus the CLI command for managing it.
"""

from __future__ import annotations

import json
from contextlib import suppress

from .api import LighthouseClient
from .config import CONFIG_DIR
from .display import error as _error, output_json as _output_json, print_table as _print_table, short as _short

COURSE_CONFIG_FILE = CONFIG_DIR / "course-config.json"


def load() -> dict[str, dict[str, str]]:
    """Load course config from disk. Returns {org_unit_id: {name, semester}}."""
    if not COURSE_CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(COURSE_CONFIG_FILE.read_text(encoding="utf-8")).get("tracked_courses", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save(config: dict[str, dict[str, str]]) -> None:
    """Save course config to disk atomically."""
    tmp = COURSE_CONFIG_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps({"tracked_courses": config}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(COURSE_CONFIG_FILE)
    except OSError:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


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
    config = load()

    # --reset: clear all tracking
    if reset:
        save({})
        print("Course tracking config cleared.")
        return 0

    # --remove ID: untrack a course
    if remove is not None:
        if remove not in config:
            return _error(f"Course {remove} is not in your tracked courses.")
        name = config[remove].get("name", remove)
        del config[remove]
        save(config)
        print(f"Stopped tracking {name} ({remove})")
        return 0

    # --list / --json: show tracked courses
    if list_courses or json_output:
        if not config:
            print("No courses tracked. Run: lighthouse config courses (without flags) to set up.")
            return 0
        entries = [
            {"id": oid, "name": entry.get("name", ""), "semester": entry.get("semester", "")}
            for oid, entry in sorted(config.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0)
        ]
        if json_output:
            _output_json(entries)
            return 0
        _print_table(["ID", "Name", "Semester"], [[e["id"], _short(e["name"], 45), e["semester"]] for e in entries], title=f"Tracked Courses ({len(entries)})")
        return 0

    # Fetch enrollments (needed for both --add and interactive)
    client = LighthouseClient()
    try:
        all_enrollments = client.get_course_enrollments()
    except Exception as e:
        return _error(str(e))

    if add is not None:
        # Find the course in enrollments
        match = next(
            ((str(e.get("OrgUnit", {}).get("Id", "")), e.get("OrgUnit", {}).get("Name", ""))
             for e in all_enrollments
             if str(e.get("OrgUnit", {}).get("Id", "")) == add or e.get("OrgUnit", {}).get("Name", "").lower() == add.lower()),
            None,
        )
        if not match:
            return _error(f"Course '{add}' not found in your enrollments. Run: lighthouse courses")

        oid, name = match
        config[oid] = {"name": name, "semester": semester or ""}
        save(config)
        print(f"Tracking {name} ({oid}){f' -> {semester}' if semester else ''}")
        return 0

    # No flags: interactive setup

    courses = [
        {"OrgUnitId": str(e["OrgUnit"]["Id"]),
         "Name": e["OrgUnit"].get("Name", ""), "Code": e["OrgUnit"].get("Code", "")}
        for e in all_enrollments
    ]

    print("\nAvailable courses (from API):")
    _print_table(["ID", "Name", "Code", "Tracked"], [[c["OrgUnitId"], _short(c["Name"], 40), _short(c["Code"], 35), f"-> {t['semester']}" if (t := config.get(c["OrgUnitId"])) and t.get("semester") else ("tracked" if t else "")] for c in courses], title=f"Enrolled Courses ({len(courses)})")

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
                print(f"  Warning: '{part}' not found, skipping.")

    if not selected_ids:
        print("No valid courses selected.")
        return 1

    # Prompt for semester assignment
    course_lookup = {c["OrgUnitId"]: c["Name"] for c in courses}
    for oid in sorted(selected_ids, key=lambda x: int(x) if x.isdigit() else 0):
        name = course_lookup.get(oid, oid)
        existing = config.get(oid, {}).get("semester", "")
        prompt = f"  Semester for {name} ({oid}){' [' + existing + ']' if existing else ''}: "
        try:
            sem = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSaving partial changes...")
            break
        config[oid] = {"name": name, "semester": sem or existing}

    save(config)
    print(f"\nUpdated tracking config: {len([oid for oid in selected_ids if oid in config])} course(s) updated.")
    print("View tracked courses: lighthouse config courses --list")
    return 0
