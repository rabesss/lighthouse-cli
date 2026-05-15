"""Assignment submission commands for lighthouse-cli."""

from __future__ import annotations

import sys
from pathlib import Path

from .api import LighthouseClient, resolve_course_id
from .display import error as _error, output_json as _output_json, utc_now_iso as _utc_now_iso
from .utils import get_course_name as _get_course_name


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

    try:
        org_id = resolve_course_id(client, course_id)
        course_name = _get_course_name(client, org_id)
        folder_id_int = _resolve_folder_id(client, org_id, folder_id)
    except Exception as e:
        return _error(str(e))

    folder_name = _get_folder_name(client, org_id, folder_id_int)

    # Check file exists (fail fast before API call)
    file_path_obj = Path(file_path).expanduser().resolve()
    if not file_path_obj.exists():
        return _error(f"File not found: {file_path}")

    # Read file bytes
    try:
        file_bytes = file_path_obj.read_bytes()
    except OSError as e:
        return _error(f"Could not read file: {e}")

    filename = file_path_obj.name

    # Confirmation prompt (skip with --yes or non-TTY)
    if not yes:
        if not sys.stdout.isatty():
            return _error("Refusing to submit without --yes in non-interactive mode. Use --yes flag to confirm.")

        print(f"Submit to '{folder_name}' in '{course_name}'?\n  File: {file_path_obj}")
        if input("Confirm [y/N]: ").strip().lower() not in ("y", "yes"):
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
    except Exception as e:
        _error(str(e))
        if isinstance(e, FileNotFoundError):
            _error("Run: lighthouse assignments")
        return 1

    # Build output
    submitted_at = result.get("submittedAt", _utc_now_iso())
    submission_id = result.get("submissionId", 0)
    if json_output:
        _output_json({
            "submission_id": submission_id, "folder_id": folder_id_int,
            "folder_name": folder_name, "course_id": org_id,
            "course_name": course_name,
            "file": {"name": filename, "size_bytes": len(file_bytes)},
            "submitted_at": submitted_at,
        })
    else:
        print(f"Submitted successfully!\n"
              f"  Submission ID: {submission_id}\n  Folder: {folder_name}\n"
              f"  Course: {course_name}\n  File: {filename}\n"
              f"  Submitted at: {submitted_at}")

    return 0


def _resolve_folder_id(client: LighthouseClient, org_id: int, identifier: str) -> int:
    """Resolve a folder identifier (numeric ID or name substring) to an int folder ID.

    Raises FileNotFoundError if zero matches (with suggestions to run assignments).
    Raises ValueError if multiple matches (ambiguous).
    """
    # Try numeric first
    try:
        fid = int(identifier)
        folders = client.get_dropbox_folders(org_id)
        if not any(f.get("Id") == fid for f in folders):
            raise FileNotFoundError(
                f"Folder '{identifier}' not found in course {org_id}. "
                "Run: lighthouse assignments"
            )
        return fid
    except ValueError:
        pass

    # Name substring match
    folders = client.get_dropbox_folders(org_id)
    matches = [f for f in folders if identifier.lower() in f.get("Name", "").lower()]
    if len(matches) == 1:
        return int(matches[0]["Id"])
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous match '{identifier}'. Multiple folders found:\n"
            + "\n".join([f"  {f['Id']} – {f['Name']}" for f in matches])
            + "\n\nUse the numeric FolderId for an exact match."
        )
    # Zero matches — show available folders in error
    avail = [f"  {f['Id']} – {f.get('Name', 'Unnamed')}" for f in folders]
    raise FileNotFoundError(
        f"Folder '{identifier}' not found in course {org_id}."
        + (f"\nAvailable folders:\n{chr(10).join(avail)}\n\n" if avail else "")
        + "Run: lighthouse assignments"
    )


def _get_folder_name(client: LighthouseClient, org_id: int, folder_id: int) -> str:
    """Get the name of a dropbox folder by ID."""
    try:
        return client.get_dropbox_folder_detail(org_id, folder_id).get("Name", f"Folder-{folder_id}")
    except Exception:
        return f"Folder-{folder_id}"


