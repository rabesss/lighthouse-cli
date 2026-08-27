"""Assignment attachment download and sync helpers.

Handles downloading and syncing assignment attachments from D2L dropbox
folders, including disambiguation of duplicate filenames.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .api import LighthouseClient
from .manifest import MANIFEST_FILENAME, Manifest
from .utils import _sanitize_filename, atomic_write, get_course_name, resolve_course_folder_name
from .display import error as _error, output_json as _output_json


def assignment_key(folder_id: int, file_id: int) -> str:
    """Generate a namespaced manifest key for an assignment attachment."""
    return f"assignment_{folder_id}_{file_id}"


def disambiguate_filename(dest_dir: Path, filename: str) -> Path:
    """Return a Path with disambiguation suffix if filename already exists."""
    filepath = dest_dir / filename
    if not filepath.exists():
        return filepath
    name, ext = filepath.stem, filepath.suffix
    counter = 1
    while True:
        new_path = dest_dir / f"{name}_{counter}{ext}"
        if not new_path.exists():
            return new_path
        counter += 1





def _assignment_dir(dest: Path, folder: dict) -> Path:
    """Return a canonical, non-symlinked directory for one assignment folder."""
    folder_id = folder.get("Id")
    folder_name = _sanitize_filename(folder.get("Name", f"Folder-{folder_id}"))
    dest_resolved = dest.resolve()
    assignments_root = dest / "Assignments"
    expected_root = dest_resolved / "Assignments"
    folder_dir = assignments_root / folder_name
    if assignments_root.resolve(strict=False) != expected_root:
        raise ValueError("Assignments directory is a symlink or resolves outside the course root")
    if folder_dir.resolve(strict=False) != expected_root / folder_name:
        raise ValueError("Assignment folder is a symlink or resolves outside the Assignments directory")
    return folder_dir


def folder_with_attachments(
    client: LighthouseClient,
    org_id: int,
    folder: dict,
) -> tuple[dict, list[dict]]:
    """Reuse list attachments, fetching folder detail only when omitted."""
    if "Attachments" in folder:
        return folder, folder.get("Attachments") or []
    detail = client.get_dropbox_folder_detail(org_id, int(folder["Id"]))
    merged = {**folder, **detail}
    return merged, merged.get("Attachments") or []


def _attachment_error(message: str, json_output: bool) -> int:
    """Emit a targeted attachment failure without breaking JSON stdout."""
    _error(message)
    if json_output:
        _output_json({"error": message})
    return 1


def _manifest_attachment_path(
    dest: Path, entry: dict | None,
) -> Path | None:
    """Resolve a recorded assignment path without allowing path traversal."""
    raw_path = entry.get("path") if entry else None
    if not isinstance(raw_path, str) or not raw_path:
        return None

    relative_path = Path(raw_path)
    if relative_path.is_absolute() or relative_path.parts[:1] != ("Assignments",):
        return None

    try:
        resolved = (dest / relative_path).resolve()
        dest_resolved = dest.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved == dest_resolved or not resolved.is_relative_to(dest_resolved):
        return None
    canonical_relative = resolved.relative_to(dest_resolved)
    if canonical_relative.parts[:1] != ("Assignments",):
        return None
    return dest / canonical_relative


def _download_and_record(client: LighthouseClient, org_id: int, folder: dict, att_id: int, dest: Path, manifest: Manifest) -> dict:
    """Download an attachment, save to disk, update manifest. Returns entry dict."""
    folder_id = folder.get("Id")
    att_key = assignment_key(folder_id, att_id)
    existing = manifest.get(att_key)
    content, filename = client.download_attachment(org_id, folder_id, att_id)
    sanitized_name = _sanitize_filename(filename or f"attachment_{att_id}")
    assignments_dir = _assignment_dir(dest, folder)
    assignments_dir.mkdir(parents=True, exist_ok=True)
    filepath = _manifest_attachment_path(dest, existing)
    if filepath is None:
        filepath = disambiguate_filename(assignments_dir, sanitized_name)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(filepath, content, mode=0o600)
    relative_path = str(filepath.relative_to(dest))
    manifest_entry = manifest.add_entry(
        att_key,
        content=content,
        filename=filepath.name,
        last_modified="",
    )
    manifest_entry["path"] = relative_path
    return {
        "file_id": att_id,
        "folder_id": folder_id,
        "filename": filepath.name,
        "path": relative_path,
        "size_kb": round(len(content) / 1024, 1),
    }

def download_single_attachment(
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
        course_name = get_course_name(client, org_id)
        folder_detail = client.get_dropbox_folder_detail(org_id, folder_id)
    except Exception as e:
        return _attachment_error(str(e), json_output)

    dest = root / resolve_course_folder_name(course_name, org_id)
    manifest_path = dest / MANIFEST_FILENAME
    try:
        manifest = Manifest.load(manifest_path)
        entry = _download_and_record(client, org_id, folder_detail, attachment_id, dest, manifest)
        manifest.save(manifest_path)
    except Exception as e:
        return _attachment_error(f"FAILED attachment {attachment_id}: {e}", json_output)

    filepath = dest / entry["path"]
    if json_output:
        _output_json({
            "course_id": org_id, "folder_id": folder_id,
            "file_id": attachment_id, "path": str(filepath),
            "size_kb": entry["size_kb"], "filename": entry["filename"],
        })
    else:
        print(f"Downloaded: {filepath} ({entry['size_kb']} KB)")
    return 0


def download_for_course(
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



    downloaded_entries, errors = [], []

    for folder in [f for f in all_folders if f.get("Id") in folder_ids] if folder_ids is not None else all_folders:
        if not (folder_id := folder.get("Id")):
            continue
        try:
            folder, attachments = folder_with_attachments(client, org_id, folder)
        except Exception as e:
            errors.append({"folder_id": folder_id, "error": str(e)})
            continue

        for att in attachments:
            att_id = att.get("Id")
            if att.get("Type", "File") != "File" or not att_id:
                continue

            att_key = assignment_key(folder_id, att_id)
            existing = manifest.get(att_key)
            if existing is not None:
                continue

            try:
                downloaded_entries.append(_download_and_record(client, org_id, folder, att_id, dest, manifest))
            except Exception as e:
                errors.append({"folder_id": folder_id, "file_id": att_id, "error": str(e)})
                print(f"  FAILED attachment {att_id}: {e}", file=sys.stderr)

    return downloaded_entries, errors


def sync_for_course(
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

    downloaded_entries, skipped_entries, updated_entries, errors = [], [], [], []

    for folder in all_folders:
        if not (folder_id := folder.get("Id")):
            continue

        try:
            folder, attachments = folder_with_attachments(client, org_id, folder)
        except Exception as e:
            errors.append({"folder_id": folder_id, "error": str(e)})
            continue

        for att in attachments:
            att_id = att.get("Id")
            if att.get("Type", "File") != "File" or not att_id:
                continue

            att_key = assignment_key(folder_id, att_id)
            existing = manifest.get(att_key)

            if existing is not None:
                if existing.get("size") == att.get("Size", 0):
                    skipped_entry = {
                        "file_id": att_id, "folder_id": folder_id,
                        "filename": existing.get("filename", ""),
                    }
                    if path := existing.get("path"):
                        skipped_entry["path"] = path
                    skipped_entries.append(skipped_entry)
                    continue
                target_list = updated_entries
            else:
                target_list = downloaded_entries

            try:
                target_list.append(_download_and_record(client, org_id, folder, att_id, dest, manifest))
            except Exception as e:
                errors.append({"folder_id": folder_id, "file_id": att_id, "error": str(e)})

    return downloaded_entries, skipped_entries, updated_entries, errors
