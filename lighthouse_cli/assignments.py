"""Assignment attachment download and sync helpers.

Handles downloading and syncing assignment attachments from D2L dropbox
folders, including disambiguation of duplicate filenames.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path
from stat import S_ISREG
from typing import Any

from .api import LighthouseClient
from .display import format_user_error, safe_display_text
from .display import output_json as _output_json
from .manifest import (
    MANIFEST_FILENAME,
    Manifest,
    compute_file_sha256,
    normalize_sha256,
)
from .utils import (
    MAX_ATOMIC_TARGET_NAME_BYTES,
    _fit_filename,
    _sanitize_filename,
    atomic_write,
    get_course_name,
    resolve_course_folder_name,
)


def assignment_key(folder_id: int, file_id: int) -> str:
    """Generate a namespaced manifest key for an assignment attachment."""
    return f"assignment_{folder_id}_{file_id}"


_INVALID_FOLDERS = "Assignment folders have an invalid response shape."
_INVALID_ATTACHMENTS = "Assignment attachments have an invalid response shape."
_INVALID_IDENTIFIER = "Assignment record has an invalid identifier."
_ASSIGNMENT_NOT_FOUND = "Requested assignment folder was not found."
_MAX_COURSE_NAME_LENGTH = 256
_MAX_FOLDER_NAME_LENGTH = 256
_MAX_FILENAME_INPUT_LENGTH = 4096
_SECRET_KEY_PATTERN = (
    r"pass(?:word|wd|phrase)?(?:[\s_-]?value)?|secret|"
    r"token(?:[\s_-]?value)?|cookie(?:s|value)?|samlresponse|otp|totp|"
    r"canary|authorization|bearer|"
    r"d2l[\s_-]?same[\s_-]?site[\s_-]?canary[ab]?|api[\s_-]?canary|"
    r"s?ctx|sft|d2l(?:secure)?session(?:val|value|token)?|"
    r"session(?:val|value|token)|api[\s_-]?key|access[\s_-]?token"
)
_SECRET_SHAPED_COURSE_NAME_RE = re.compile(
    rf"(?ix)(?:^|[^a-z0-9])(?:{_SECRET_KEY_PATTERN})"
    r"\s*(?::|=|\bis\b|\bwas\b)\s*[^\s,;]+"
)
_SECRET_SHAPED_FOLDER_NAME_RE = _SECRET_SHAPED_COURSE_NAME_RE
_SECRET_SHAPED_FILENAME_RE = _SECRET_SHAPED_COURSE_NAME_RE


class _AssignmentDataError(ValueError):
    """Raised when a folder detail cannot be trusted as the listed folder."""


def _positive_int(value: object) -> int | None:
    """Return a strictly positive integer identifier, or ``None``.

    Brightspace identifiers are numeric values from the API, not arbitrary
    strings supplied by a response.  Rejecting booleans, floats, zero,
    negatives, and path-like strings before any follow-up request prevents a
    malformed record from becoming a request target or filesystem component.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _safe_course_name(value: object, org_id: int) -> str:
    """Return a bounded, printable, non-secret course name for a local path."""
    candidate = safe_display_text(value, "", max_len=_MAX_COURSE_NAME_LENGTH)
    if (
        not candidate
        or _SECRET_SHAPED_COURSE_NAME_RE.search(candidate)
        or not safe_display_text(_sanitize_filename(candidate), "", max_len=_MAX_COURSE_NAME_LENGTH)
    ):
        return f"Course-{org_id}"
    return candidate


def safe_assignment_folder_name(
    value: object,
    folder_id: int,
    *,
    fallback: bool = True,
) -> str:
    """Project a server folder label without secrets or control characters."""
    sanitized = _sanitize_filename(value) if isinstance(value, str) else ""
    if (
        not isinstance(value, str)
        or not safe_display_text(value, "", max_len=_MAX_FOLDER_NAME_LENGTH)
        or _SECRET_SHAPED_FOLDER_NAME_RE.search(value)
        or not sanitized
        or len(sanitized) > _MAX_FOLDER_NAME_LENGTH
        or _SECRET_SHAPED_FOLDER_NAME_RE.search(sanitized)
        or not safe_display_text(sanitized, "", max_len=_MAX_FOLDER_NAME_LENGTH)
    ):
        return f"Folder-{folder_id}" if fallback else ""
    return _fit_filename(sanitized, max_bytes=255)


def _safe_filename_suffix(value: str) -> str:
    """Keep only a simple printable extension from an untrusted filename."""
    suffix = Path(value).suffix
    return suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix) else ""


def safe_attachment_filename(
    value: object,
    attachment_id: int,
    *,
    fallback: bool = True,
) -> str:
    """Project one server filename without retaining secrets or controls.

    Normal filenames retain the existing filesystem sanitization behavior.
    Secret-shaped or control-bearing names become ``attachment_<id>`` with a
    simple safe extension when one can be retained.  Read-only projections
    such as ``show assignments`` may request an empty value instead of a local
    fallback via ``fallback=False``.
    """
    sanitized = _sanitize_filename(value) if isinstance(value, str) else ""
    if (
        not isinstance(value, str)
        or not safe_display_text(value, "", max_len=_MAX_FILENAME_INPUT_LENGTH)
        or not sanitized
        or _SECRET_SHAPED_FILENAME_RE.search(value)
        or _SECRET_SHAPED_FILENAME_RE.search(sanitized)
        or not safe_display_text(sanitized, "", max_len=_MAX_FILENAME_INPUT_LENGTH)
    ):
        return f"attachment_{attachment_id}{_safe_filename_suffix(sanitized)}" if fallback else ""
    return _fit_filename(sanitized, max_bytes=MAX_ATOMIC_TARGET_NAME_BYTES - 18)


def disambiguate_filename(
    dest_dir: Path,
    filename: str,
    *,
    reserved_paths: set[Path] | None = None,
) -> Path:
    """Return a Path with disambiguation suffix if filename already exists."""
    filepath = dest_dir / filename
    name, ext = filepath.stem, filepath.suffix
    counter = 0
    while (
        filepath.exists()
        or filepath.is_symlink()
        or (reserved_paths is not None and filepath.absolute() in reserved_paths)
    ):
        counter += 1
        filepath = dest_dir / f"{name}_{counter}{ext}"
    return filepath


def _assignment_dir(dest: Path, folder: dict[str, Any]) -> Path:
    """Return a non-symlinked directory for one assignment folder."""
    course_root = _course_boundary(dest)
    folder_id = _positive_int(folder.get("Id"))
    if folder_id is None:
        raise ValueError(_INVALID_IDENTIFIER)
    folder_name = safe_assignment_folder_name(folder.get("Name"), folder_id)
    assignments_root = course_root / "Assignments"
    folder_dir = assignments_root / folder_name
    if _has_symlink_component(assignments_root, course_root):
        raise ValueError("Assignments directory is a symlink or resolves outside the course root")
    if assignments_root.exists() and not assignments_root.is_dir():
        raise ValueError("Assignments path is not a directory")
    if _has_symlink_component(folder_dir, course_root):
        raise ValueError("Assignment folder is a symlink or resolves outside the Assignments directory")
    if folder_dir.exists() and not folder_dir.is_dir():
        raise ValueError("Assignment folder path is not a directory")
    return folder_dir


def _course_boundary(dest: Path) -> Path:
    """Return a lexical course root, rejecting an existing course symlink."""
    dest = Path(dest).expanduser()
    absolute = dest.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ValueError("Course path contains a symlinked component")
    return absolute


def _has_symlink_component(path: Path, root: Path) -> bool:
    """Return whether an existing descendant of *root* is a symlink."""
    path = Path(path).expanduser().absolute()
    root = Path(root).expanduser().absolute()
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False

    current = root
    for component in relative.parts:
        current /= component
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def folder_with_attachments(
    client: LighthouseClient,
    org_id: int,
    folder: dict[str, Any],
) -> tuple[dict[str, Any], object]:
    """Reuse list attachments, fetching folder detail only when omitted."""
    if "Attachments" in folder:
        return folder, folder.get("Attachments")
    folder_id = _positive_int(folder.get("Id"))
    if folder_id is None:
        raise _AssignmentDataError(_INVALID_IDENTIFIER)
    detail = client.get_dropbox_folder_detail(org_id, folder_id)
    if not isinstance(detail, dict):
        raise _AssignmentDataError(_INVALID_FOLDERS)
    if _positive_int(detail.get("Id")) != folder_id:
        raise _AssignmentDataError(_INVALID_IDENTIFIER)
    # The list/request identity is authoritative even when the detail payload
    # contains an equivalent ID.  Never let a detail response substitute a
    # different folder as the subsequent attachment target.
    merged = {**folder, **detail, "Id": folder_id}
    return merged, merged.get("Attachments")


def _attachment_error(
    message: BaseException | str,
    json_output: bool,
    *,
    error_type: str | None = None,
) -> int:
    """Emit a targeted attachment failure without breaking JSON stdout."""
    safe_message = format_user_error(message)
    print(f"Error: {safe_message}", file=sys.stderr)
    if json_output:
        payload = {"error": safe_message}
        if error_type is not None:
            payload["type"] = error_type
        _output_json(payload)
    return 1


def _manifest_attachment_path(
    dest: Path,
    entry: dict[str, Any] | None,
    *,
    expected_parent: Path | None = None,
) -> Path | None:
    """Resolve a recorded assignment path without allowing path traversal."""
    if not isinstance(entry, dict):
        return None
    raw_path = entry.get("path")
    if (not isinstance(raw_path, str) or not raw_path) and expected_parent is not None:
        legacy_filename = entry.get("filename")
        if not isinstance(legacy_filename, str) or not _safe_manifest_component(legacy_filename):
            return None
        try:
            course_root = _course_boundary(dest)
            candidate = expected_parent / legacy_filename
            if not candidate.absolute().is_relative_to(course_root):
                return None
            if _has_symlink_component(candidate, course_root):
                return None
            if not candidate.resolve(strict=False).is_relative_to(course_root):
                return None
        except (OSError, RuntimeError, ValueError):
            return None
        return candidate
    if not isinstance(raw_path, str) or not raw_path:
        return None

    relative_path = Path(raw_path)
    if relative_path.is_absolute() or relative_path.parts[:1] != ("Assignments",):
        return None
    if any(component in {"", ".", ".."} for component in raw_path.split("/")):
        return None
    # A contained path can still carry a forged server/local label.  Reject
    # control-bearing, secret-shaped, traversal-like, or otherwise
    # unsanitized components before trusting the manifest entry for a skip or
    # reusing it as the next write target.  Returning ``None`` deliberately
    # sends callers through the deterministic safe redownload path.
    if any(not _safe_manifest_component(component) for component in relative_path.parts[1:]):
        return None

    try:
        course_root = _course_boundary(dest)
        candidate = course_root / relative_path
        if not candidate.absolute().is_relative_to(course_root):
            return None
        if _has_symlink_component(candidate, course_root):
            return None
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved == course_root or not resolved.is_relative_to(course_root):
        return None
    canonical_relative = resolved.relative_to(course_root)
    if canonical_relative.parts[:1] != ("Assignments",):
        return None
    candidate = course_root / canonical_relative
    if candidate.is_symlink():
        return None
    if expected_parent is not None and candidate.parent != Path(expected_parent).absolute():
        return None
    return candidate


def _safe_manifest_component(component: object) -> bool:
    """Return whether one manifest path/filename component is safe to trust."""
    if not isinstance(component, str) or component in {"", ".", ".."}:
        return False
    return (
        all(character.isprintable() for character in component)
        and not _SECRET_SHAPED_FILENAME_RE.search(component)
        and _sanitize_filename(component) == component
    )


def _matching_local_attachment(
    dest: Path,
    entry: dict[str, Any] | None,
    expected_size: object,
    *,
    expected_folder: dict[str, Any] | None = None,
) -> Path | None:
    """Return a verified local attachment path, or ``None``.

    A manifest record is only a hint.  Before treating an attachment as
    unchanged, verify that its recorded path stays inside ``Assignments``, is
    a regular non-symlink file, has the recorded (and remote) size, and has
    the recorded SHA-256 digest.  This prevents forged paths and same-size
    local edits from being reported as skipped.
    """
    if not isinstance(entry, dict):
        return None
    filename = entry.get("filename")
    if not _safe_manifest_component(filename):
        return None
    size = entry.get("size")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size != size
    ):
        return None
    expected_hash = normalize_sha256(entry.get("sha256"))
    if not expected_hash:
        return None
    expected_parent = None
    if expected_folder is not None:
        try:
            expected_parent = _assignment_dir(dest, expected_folder)
        except (OSError, RuntimeError, ValueError):
            return None
    candidate = _manifest_attachment_path(dest, entry, expected_parent=expected_parent)
    if candidate is None or candidate.name != filename:
        return None
    try:
        stat_result = candidate.lstat()
        if not S_ISREG(stat_result.st_mode) or stat_result.st_size != size:
            return None
        actual_hash = compute_file_sha256(candidate)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate if actual_hash == expected_hash else None


def _assignment_path_owners(dest: Path, manifest: Manifest) -> dict[Path, str | None]:
    """Return validated manifest path owners, marking legacy aliases contested."""
    owners: dict[Path, str | None] = {}
    for manifest_key, manifest_entry in manifest.entries.items():
        key = str(manifest_key)
        if not key.startswith("assignment_") or not isinstance(manifest_entry, dict):
            continue
        prior_path = _manifest_attachment_path(dest, manifest_entry)
        if prior_path is None:
            continue
        absolute_path = prior_path.absolute()
        contested = absolute_path in owners and owners[absolute_path] != key
        owners[absolute_path] = None if contested else key
    return owners


def _claim_assignment_entry(
    dest: Path,
    manifest: Manifest,
    att_key: str,
    folder: dict[str, Any],
    owners: dict[Path, str | None],
    claimed_paths: set[Path],
    *,
    allow_contested_claim: bool,
) -> dict[str, Any] | None:
    """Claim one safe prior path without letting manifest aliases overwrite."""
    entry = manifest.get(att_key)
    if not isinstance(entry, dict):
        return None
    try:
        prior_path = _manifest_attachment_path(
            dest,
            entry,
            expected_parent=_assignment_dir(dest, folder),
        )
    except (OSError, RuntimeError, ValueError):
        return None
    if prior_path is None:
        return None
    path_key = prior_path.absolute()
    # ``owner`` is None both for an unowned path and for a contested one.
    owner = owners.get(path_key)
    can_claim = owner == att_key or (
        allow_contested_claim and owner is None and path_key not in claimed_paths
    )
    if not can_claim:
        return None
    claimed_paths.add(path_key)
    return entry


def _write_entry_for_claim(
    dest: Path,
    entry: dict[str, Any] | None,
    folder: dict[str, Any],
) -> dict[str, Any] | None:
    """Reuse a legacy inferred path only when no local file would be replaced."""
    if not isinstance(entry, dict) or entry.get("path"):
        return entry
    try:
        candidate = _manifest_attachment_path(
            dest,
            entry,
            expected_parent=_assignment_dir(dest, folder),
        )
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate is None or candidate.exists() or candidate.is_symlink():
        return None
    return entry


def _download_and_record(
    client: LighthouseClient,
    org_id: int,
    folder: dict[str, Any],
    att_id: int,
    dest: Path,
    manifest: Manifest,
    *,
    existing_entry: dict[str, Any] | None,
    claimed_paths: set[Path],
) -> dict[str, Any]:
    """Download an attachment, save to disk, update manifest. Returns entry dict."""
    folder_id = _positive_int(folder.get("Id"))
    att_key_id = _positive_int(att_id)
    if folder_id is None or att_key_id is None:
        raise ValueError(_INVALID_IDENTIFIER)
    att_key = assignment_key(folder_id, att_key_id)
    course_root = _course_boundary(dest)
    assignments_dir = _assignment_dir(course_root, folder)
    content, filename = client.download_attachment(org_id, folder_id, att_id)
    if not isinstance(content, bytes):
        raise _AssignmentDataError(_INVALID_ATTACHMENTS)
    sanitized_name = safe_attachment_filename(filename, att_id)
    assignments_dir.mkdir(parents=True, exist_ok=True)
    filepath = _manifest_attachment_path(course_root, existing_entry, expected_parent=assignments_dir)
    if filepath is None:
        filepath = disambiguate_filename(assignments_dir, sanitized_name, reserved_paths=claimed_paths)
        claimed_paths.add(filepath.absolute())
    if (
        not filepath.absolute().is_relative_to(course_root)
        or _has_symlink_component(filepath.parent, course_root)
        or filepath.is_symlink()
    ):
        raise ValueError("Assignment attachment path is symlinked or escapes the course root")
    filepath.parent.mkdir(parents=True, exist_ok=True)
    try:
        resolved_filepath = filepath.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ValueError("Unable to validate assignment attachment path") from None
    if (
        filepath.is_symlink()
        or _has_symlink_component(filepath.parent, course_root)
        or not resolved_filepath.is_relative_to(course_root)
    ):
        raise ValueError("Assignment attachment path is symlinked or escapes the course root")
    atomic_write(filepath, content, mode=0o600)
    relative_path = str(filepath.relative_to(course_root))
    manifest_entry = manifest.add_entry(
        att_key,
        content=content,
        filename=filepath.name,
        last_modified="",
    )
    manifest_entry["path"] = relative_path
    return {
        "file_id": att_id, "folder_id": folder_id, "filename": filepath.name,
        "path": relative_path, "size_kb": round(len(content) / 1024, 1),
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
        course_name = _safe_course_name(get_course_name(client, org_id), org_id)
        normalized_folder_id = _positive_int(folder_id)
        if normalized_folder_id is None:
            raise _AssignmentDataError(_INVALID_IDENTIFIER)
        folder_detail = client.get_dropbox_folder_detail(org_id, normalized_folder_id)
        if not isinstance(folder_detail, dict):
            raise _AssignmentDataError(_INVALID_FOLDERS)
        if _positive_int(folder_detail.get("Id")) != normalized_folder_id:
            raise _AssignmentDataError(_INVALID_IDENTIFIER)
        folder_id = normalized_folder_id
    except _AssignmentDataError as e:
        return _attachment_error(e, json_output, error_type="assignment_data")
    except Exception as e:
        return _attachment_error(e, json_output)

    output_root = Path(root).expanduser().resolve(strict=False)
    dest = output_root / resolve_course_folder_name(course_name, org_id)
    try:
        dest = _course_boundary(dest)
        manifest_path = dest / MANIFEST_FILENAME
        manifest = Manifest.load(manifest_path)
        att_key = assignment_key(folder_id, attachment_id)
        claimed_paths: set[Path] = set()
        existing = _claim_assignment_entry(
            dest, manifest, att_key, folder_detail, _assignment_path_owners(dest, manifest),
            claimed_paths, allow_contested_claim=False,
        )
        entry = _download_and_record(
            client, org_id, folder_detail, attachment_id, dest, manifest,
            existing_entry=existing, claimed_paths=claimed_paths,
        )
        manifest.save(manifest_path)
    except _AssignmentDataError as e:
        return _attachment_error(e, json_output, error_type="assignment_data")
    except Exception as e:
        return _attachment_error(
            f"FAILED attachment {attachment_id}: {format_user_error(e)}",
            json_output,
        )

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


def _data_error(message: BaseException | str, **ids: int) -> dict[str, Any]:
    """Build an ``assignment_data`` error record for untrusted folder data."""
    return {**ids, "error": format_user_error(message), "type": "assignment_data"}


def _failure(exc: Exception, **ids: int) -> dict[str, Any]:
    """Build the error record for a failed folder or attachment request."""
    if isinstance(exc, _AssignmentDataError):
        return _data_error(exc, **ids)
    return {**ids, "error": format_user_error(exc)}


def _file_attachments(
    client: LighthouseClient,
    org_id: int,
    folders: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    errors: list[dict[str, Any]],
    selected_ids: set[int] | None = None,
    visited_ids: set[int] | None = None,
) -> Iterator[tuple[int, dict[str, Any], int, dict[str, Any]]]:
    """Yield ``(folder_id, folder, att_id, att)`` for each valid file attachment.

    Malformed folders and attachments are recorded in ``errors`` and skipped.
    With ``selected_ids`` only those folders are visited; each visited folder
    ID is added to ``visited_ids``.
    """
    seen_folder_ids: set[int] = set()
    for folder in folders:
        if not isinstance(folder, dict):
            errors.append({"error": format_user_error(_INVALID_FOLDERS), "type": "assignment_list"})
            continue
        folder_id = _positive_int(folder.get("Id"))
        if folder_id is None:
            errors.append(_data_error(_INVALID_IDENTIFIER))
            continue
        if folder_id in seen_folder_ids or (selected_ids is not None and folder_id not in selected_ids):
            continue
        if visited_ids is not None:
            visited_ids.add(folder_id)
        try:
            folder, attachments = folder_with_attachments(client, org_id, folder)
        except Exception as e:
            errors.append(_failure(e, folder_id=folder_id))
            continue
        if not isinstance(attachments, (list, tuple)):
            errors.append(_data_error(_INVALID_ATTACHMENTS, folder_id=folder_id))
            continue
        seen_folder_ids.add(folder_id)
        for att in attachments:
            if not isinstance(att, dict):
                errors.append(_data_error(_INVALID_ATTACHMENTS, folder_id=folder_id))
                continue
            att_id = _positive_int(att.get("Id"))
            if att_id is None:
                errors.append(_data_error(_INVALID_IDENTIFIER, folder_id=folder_id))
                continue
            if att.get("Type", "File") == "File":
                yield folder_id, folder, att_id, att


def download_for_course(
    client: LighthouseClient,
    org_id: int,
    dest: Path,
    manifest: Manifest,
    folder_ids: list[int] | None = None,
    folder_snapshot: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    path_manifest: Manifest | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Download all assignment attachments for a course.

    ``folder_snapshot`` is an optional folder list already fetched and
    validated by the command layer.  Reusing it prevents a second API snapshot
    from changing after content topics have been written.

    Returns (downloaded_entries, errors).
    """
    all_folders: list[dict[str, Any]] | tuple[dict[str, Any], ...]
    if folder_snapshot is None:
        try:
            all_folders = client.get_dropbox_folders(org_id)
        except Exception as e:
            return [], [{"error": format_user_error(e), "type": "assignment_list"}]
    else:
        all_folders = folder_snapshot

    downloaded_entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if not isinstance(all_folders, (list, tuple)):
        return [], [{"error": format_user_error(_INVALID_FOLDERS), "type": "assignment_list"}]
    selected_ids: set[int] | None = None
    if folder_ids is not None:
        requested_ids = [_positive_int(requested_id) for requested_id in folder_ids]
        if not requested_ids or None in requested_ids:
            return [], [{"error": _ASSIGNMENT_NOT_FOUND, "type": "assignment_not_found"}]
        selected_ids = {folder_id for folder_id in requested_ids if folder_id is not None}
    matched_ids: set[int] = set()
    ownership_manifest = path_manifest if path_manifest is not None else manifest
    prior_path_owners = _assignment_path_owners(dest, ownership_manifest)
    claimed_prior_paths: set[Path] = set()

    attachments = _file_attachments(client, org_id, all_folders, errors, selected_ids, matched_ids)
    for folder_id, folder, att_id, att in attachments:
        existing = _claim_assignment_entry(
            dest, ownership_manifest, assignment_key(folder_id, att_id), folder,
            prior_path_owners, claimed_prior_paths, allow_contested_claim=True,
        )
        skip_entry = existing if path_manifest is None else None
        matched_path = _matching_local_attachment(
            dest, skip_entry, att.get("Size", 0), expected_folder=folder
        )
        if matched_path is not None:
            if isinstance(skip_entry, dict):
                skip_entry["path"] = str(matched_path.relative_to(_course_boundary(dest)))
            continue
        write_entry = _write_entry_for_claim(dest, existing, folder)
        try:
            downloaded_entries.append(_download_and_record(
                client, org_id, folder, att_id, dest, manifest,
                existing_entry=write_entry, claimed_paths=claimed_prior_paths,
            ))
        except _AssignmentDataError as e:
            errors.append(_failure(e, folder_id=folder_id, file_id=att_id))
        except Exception as e:
            failure = _failure(e, folder_id=folder_id, file_id=att_id)
            errors.append(failure)
            print(f"  FAILED attachment {att_id}: {failure['error']}", file=sys.stderr)

    if selected_ids is not None and selected_ids - matched_ids:
        errors.append({"error": _ASSIGNMENT_NOT_FOUND, "type": "assignment_not_found"})
    return downloaded_entries, errors


def sync_for_course(
    client: LighthouseClient,
    org_id: int,
    dest: Path,
    manifest: Manifest,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Sync assignment attachments for a course (detect new/updated).

    Returns (downloaded_entries, skipped_entries, updated_entries, errors).
    """
    try:
        all_folders = client.get_dropbox_folders(org_id)
    except Exception as e:
        return [], [], [], [{"error": format_user_error(e), "type": "assignment_list"}]

    downloaded_entries: list[dict[str, Any]] = []
    skipped_entries: list[dict[str, Any]] = []
    updated_entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    if not isinstance(all_folders, (list, tuple)):
        return [], [], [], [{"error": format_user_error(_INVALID_FOLDERS), "type": "assignment_list"}]
    prior_path_owners = _assignment_path_owners(dest, manifest)
    claimed_prior_paths: set[Path] = set()
    for folder_id, folder, att_id, att in _file_attachments(client, org_id, all_folders, errors):
        att_key = assignment_key(folder_id, att_id)
        manifest_entry = manifest.get(att_key)
        existing = _claim_assignment_entry(
            dest, manifest, att_key, folder, prior_path_owners, claimed_prior_paths,
            allow_contested_claim=True,
        )
        matched_path = _matching_local_attachment(
            dest, existing, att.get("Size", 0), expected_folder=folder
        )
        if matched_path is not None and isinstance(existing, dict):
            relative_path = str(matched_path.relative_to(_course_boundary(dest)))
            existing["path"] = relative_path
            skipped_entries.append({
                "file_id": att_id, "folder_id": folder_id,
                "filename": matched_path.name, "path": relative_path,
            })
            continue
        write_entry = _write_entry_for_claim(dest, existing, folder)
        target_list = updated_entries if isinstance(manifest_entry, dict) else downloaded_entries
        try:
            target_list.append(_download_and_record(
                client, org_id, folder, att_id, dest, manifest,
                existing_entry=write_entry, claimed_paths=claimed_prior_paths,
            ))
        except Exception as e:
            errors.append(_failure(e, folder_id=folder_id, file_id=att_id))

    return downloaded_entries, skipped_entries, updated_entries, errors
