"""Assignment attachment download and sync helpers.

Handles downloading and syncing assignment attachments from D2L dropbox
folders, including disambiguation of duplicate filenames.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from stat import S_ISREG

from .api import LighthouseClient
from .manifest import (
    MANIFEST_FILENAME,
    Manifest,
    compute_file_sha256,
    normalize_sha256,
)
from .display import (
    format_user_error,
    output_json as _output_json,
    safe_display_text,
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


_USE_MANIFEST_ENTRY = object()


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
    fallback = f"Course-{org_id}"
    candidate = safe_display_text(value, "", max_len=_MAX_COURSE_NAME_LENGTH)
    if not candidate or _SECRET_SHAPED_COURSE_NAME_RE.search(candidate):
        return fallback
    sanitized = _sanitize_filename(candidate)
    if not safe_display_text(sanitized, "", max_len=_MAX_COURSE_NAME_LENGTH):
        return fallback
    return candidate


def safe_assignment_folder_name(
    value: object,
    folder_id: int,
    *,
    fallback: bool = True,
) -> str:
    """Project a server folder label without secrets or control characters."""
    safe_fallback = f"Folder-{folder_id}"
    candidate = safe_display_text(value, "", max_len=_MAX_FOLDER_NAME_LENGTH)
    if not candidate or _SECRET_SHAPED_FOLDER_NAME_RE.search(candidate):
        return safe_fallback if fallback else ""

    sanitized = _sanitize_filename(candidate)
    if (
        not sanitized
        or len(sanitized) > _MAX_FOLDER_NAME_LENGTH
        or _SECRET_SHAPED_FOLDER_NAME_RE.search(sanitized)
        or not safe_display_text(sanitized, "", max_len=_MAX_FOLDER_NAME_LENGTH)
    ):
        return safe_fallback if fallback else ""
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
    safe_fallback = f"attachment_{attachment_id}"
    sanitized_input = _sanitize_filename(value) if isinstance(value, str) else ""
    candidate = safe_display_text(value, "", max_len=_MAX_FILENAME_INPUT_LENGTH)
    if not candidate:
        return (
            safe_fallback + _safe_filename_suffix(sanitized_input)
            if fallback
            else ""
        )

    sanitized = _sanitize_filename(candidate)
    if (
        not sanitized
        or _SECRET_SHAPED_FILENAME_RE.search(candidate)
        or _SECRET_SHAPED_FILENAME_RE.search(sanitized)
        or not safe_display_text(sanitized, "", max_len=_MAX_FILENAME_INPUT_LENGTH)
    ):
        if not fallback:
            return ""
        return safe_fallback + _safe_filename_suffix(sanitized)
    return _fit_filename(
        sanitized,
        max_bytes=MAX_ATOMIC_TARGET_NAME_BYTES - 18,
    )


def disambiguate_filename(dest_dir: Path, filename: str) -> Path:
    """Return a Path with disambiguation suffix if filename already exists."""
    filepath = dest_dir / filename
    if not filepath.exists() and not filepath.is_symlink():
        return filepath
    name, ext = filepath.stem, filepath.suffix
    counter = 1
    while True:
        new_path = dest_dir / f"{name}_{counter}{ext}"
        if not new_path.exists() and not new_path.is_symlink():
            return new_path
        counter += 1





def _assignment_dir(dest: Path, folder: dict) -> Path:
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
    folder: dict,
) -> tuple[dict, object]:
    """Reuse list attachments, fetching folder detail only when omitted."""
    if "Attachments" in folder:
        return folder, folder.get("Attachments")
    folder_id = _positive_int(folder.get("Id"))
    if folder_id is None:
        raise _AssignmentDataError(_INVALID_IDENTIFIER)
    detail = client.get_dropbox_folder_detail(org_id, folder_id)
    if not isinstance(detail, dict):
        raise _AssignmentDataError(_INVALID_FOLDERS)
    detail_id = _positive_int(detail.get("Id"))
    if detail_id is None or detail_id != folder_id:
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
    entry: dict | None,
    *,
    expected_parent: Path | None = None,
) -> Path | None:
    """Resolve a recorded assignment path without allowing path traversal."""
    if not isinstance(entry, dict):
        return None
    raw_path = entry.get("path")
    if (not isinstance(raw_path, str) or not raw_path) and expected_parent is not None:
        legacy_filename = entry.get("filename")
        if not _safe_manifest_component(legacy_filename):
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
        dest_resolved = course_root
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved == dest_resolved or not resolved.is_relative_to(dest_resolved):
        return None
    canonical_relative = resolved.relative_to(dest_resolved)
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
    entry: dict | None,
    expected_size: object,
    *,
    expected_folder: dict | None = None,
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
    candidate = _manifest_attachment_path(
        dest,
        entry,
        expected_parent=expected_parent,
    )
    if candidate is None:
        return None
    if candidate.name != filename:
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
        owner = owners.get(absolute_path)
        if absolute_path in owners and owner != key:
            owners[absolute_path] = None
        else:
            owners[absolute_path] = key
    return owners


def _claim_assignment_entry(
    dest: Path,
    manifest: Manifest,
    att_key: str,
    folder: dict,
    owners: dict[Path, str | None],
    claimed_paths: set[Path],
    *,
    allow_contested_claim: bool,
) -> dict | None:
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
    owner = owners.get(path_key)
    can_claim = owner == att_key or (
        allow_contested_claim
        and (
            (path_key in owners and owner is None)
            or path_key not in owners
        )
        and path_key not in claimed_paths
    )
    if not can_claim:
        return None
    claimed_paths.add(path_key)
    return entry


def _download_and_record(
    client: LighthouseClient,
    org_id: int,
    folder: dict,
    att_id: int,
    dest: Path,
    manifest: Manifest,
    *,
    existing_entry: dict | None | object = _USE_MANIFEST_ENTRY,
) -> dict:
    """Download an attachment, save to disk, update manifest. Returns entry dict."""
    folder_id = _positive_int(folder.get("Id"))
    att_id = _positive_int(att_id)
    if folder_id is None or att_id is None:
        raise ValueError(_INVALID_IDENTIFIER)
    att_key = assignment_key(folder_id, att_id)
    if existing_entry is _USE_MANIFEST_ENTRY:
        existing = manifest.get(att_key)
    else:
        existing = existing_entry if isinstance(existing_entry, dict) else None
    course_root = _course_boundary(dest)
    assignments_dir = _assignment_dir(course_root, folder)
    content, filename = client.download_attachment(org_id, folder_id, att_id)
    if not isinstance(content, bytes):
        raise _AssignmentDataError(_INVALID_ATTACHMENTS)
    sanitized_name = safe_attachment_filename(filename, att_id)
    assignments_dir.mkdir(parents=True, exist_ok=True)
    filepath = _manifest_attachment_path(
        course_root,
        existing,
        expected_parent=assignments_dir,
    )
    if filepath is None:
        filepath = disambiguate_filename(assignments_dir, sanitized_name)
    if (
        not filepath.absolute().is_relative_to(course_root)
        or _has_symlink_component(filepath.parent, course_root)
        or filepath.is_symlink()
    ):
        raise ValueError("Assignment attachment path is symlinked or escapes the course root")
    filepath.parent.mkdir(parents=True, exist_ok=True)
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
        course_name = _safe_course_name(get_course_name(client, org_id), org_id)
        normalized_folder_id = _positive_int(folder_id)
        if normalized_folder_id is None:
            raise _AssignmentDataError(_INVALID_IDENTIFIER)
        folder_detail = client.get_dropbox_folder_detail(org_id, normalized_folder_id)
        if not isinstance(folder_detail, dict):
            raise _AssignmentDataError(_INVALID_FOLDERS)
        detail_id = _positive_int(folder_detail.get("Id"))
        if detail_id is None or detail_id != normalized_folder_id:
            raise _AssignmentDataError(_INVALID_IDENTIFIER)
        folder_id = normalized_folder_id
    except _AssignmentDataError as e:
        return _attachment_error(e, json_output, error_type="assignment_data")
    except Exception as e:
        return _attachment_error(e, json_output)

    output_root = Path(root).expanduser().resolve(strict=False)
    dest = output_root / resolve_course_folder_name(course_name, org_id)
    manifest_path = dest / MANIFEST_FILENAME
    try:
        dest = _course_boundary(dest)
        manifest_path = dest / MANIFEST_FILENAME
        manifest = Manifest.load(manifest_path)
        att_key = assignment_key(folder_id, attachment_id)
        existing = _claim_assignment_entry(
            dest,
            manifest,
            att_key,
            folder_detail,
            _assignment_path_owners(dest, manifest),
            set(),
            allow_contested_claim=False,
        )
        entry = _download_and_record(
            client,
            org_id,
            folder_detail,
            attachment_id,
            dest,
            manifest,
            existing_entry=existing,
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


def download_for_course(
    client: LighthouseClient,
    org_id: int,
    dest: Path,
    manifest: Manifest,
    folder_ids: list[int] | None = None,
    folder_snapshot: list[dict] | tuple[dict, ...] | None = None,
    path_manifest: Manifest | None = None,
) -> tuple[list[dict], list[dict]]:
    """Download all assignment attachments for a course.

    ``folder_snapshot`` is an optional folder list already fetched and
    validated by the command layer.  Reusing it prevents a second API snapshot
    from changing after content topics have been written.

    Returns (downloaded_entries, errors).
    """
    if folder_snapshot is None:
        try:
            all_folders = client.get_dropbox_folders(org_id)
        except Exception as e:
            return [], [{"error": format_user_error(e), "type": "assignment_list"}]
    else:
        all_folders = folder_snapshot

    downloaded_entries, errors = [], []

    if not isinstance(all_folders, (list, tuple)):
        return [], [{"error": format_user_error(_INVALID_FOLDERS), "type": "assignment_list"}]
    folders = all_folders
    selected_ids: set[int] | None = None
    if folder_ids is not None:
        selected_ids = set()
        for requested_id in folder_ids:
            normalized_id = _positive_int(requested_id)
            if normalized_id is None:
                return [], [{
                    "error": _ASSIGNMENT_NOT_FOUND,
                    "type": "assignment_not_found",
                }]
            selected_ids.add(normalized_id)
        if not selected_ids:
            return [], [{
                "error": _ASSIGNMENT_NOT_FOUND,
                "type": "assignment_not_found",
            }]
    matched_ids: set[int] = set()
    seen_folder_ids: set[int] = set()
    ownership_manifest = path_manifest if path_manifest is not None else manifest
    prior_path_owners = _assignment_path_owners(dest, ownership_manifest)
    claimed_prior_paths: set[Path] = set()

    for folder in folders:
        if not isinstance(folder, dict):
            errors.append({"error": format_user_error(_INVALID_FOLDERS), "type": "assignment_list"})
            continue
        folder_id = _positive_int(folder.get("Id"))
        if folder_id is None:
            errors.append({"error": format_user_error(_INVALID_IDENTIFIER), "type": "assignment_data"})
            continue
        if folder_id in seen_folder_ids:
            continue
        if selected_ids is not None and folder_id not in selected_ids:
            continue
        if selected_ids is not None:
            matched_ids.add(folder_id)
        try:
            folder, attachments = folder_with_attachments(client, org_id, folder)
        except _AssignmentDataError as e:
            errors.append({
                "folder_id": folder_id,
                "error": format_user_error(e),
                "type": "assignment_data",
            })
            continue
        except Exception as e:
            errors.append({"folder_id": folder_id, "error": format_user_error(e)})
            continue

        if _positive_int(folder.get("Id")) is None:
            errors.append({"folder_id": folder_id, "error": format_user_error(_INVALID_IDENTIFIER), "type": "assignment_data"})
            continue

        if not isinstance(attachments, (list, tuple)):
            errors.append({
                "folder_id": folder_id,
                "error": format_user_error(_INVALID_ATTACHMENTS),
                "type": "assignment_data",
            })
            continue
        seen_folder_ids.add(folder_id)
        for att in attachments:
            if not isinstance(att, dict):
                errors.append({
                    "folder_id": folder_id,
                    "error": format_user_error(_INVALID_ATTACHMENTS),
                    "type": "assignment_data",
                })
                continue
            att_id = _positive_int(att.get("Id"))
            if att_id is None:
                errors.append({
                    "folder_id": folder_id,
                    "error": format_user_error(_INVALID_IDENTIFIER),
                    "type": "assignment_data",
                })
                continue
            if att.get("Type", "File") != "File" or not att_id:
                continue

            att_key = assignment_key(folder_id, att_id)
            existing = _claim_assignment_entry(
                dest,
                ownership_manifest,
                att_key,
                folder,
                prior_path_owners,
                claimed_prior_paths,
                allow_contested_claim=True,
            )
            skip_entry = existing if path_manifest is None else None
            matched_path = _matching_local_attachment(
                dest,
                skip_entry,
                att.get("Size", 0),
                expected_folder=folder,
            )
            if matched_path is not None:
                if isinstance(skip_entry, dict):
                    skip_entry["path"] = str(
                        matched_path.relative_to(_course_boundary(dest))
                    )
                continue

            try:
                downloaded_entries.append(
                    _download_and_record(
                        client,
                        org_id,
                        folder,
                        att_id,
                        dest,
                        manifest,
                        existing_entry=existing,
                    )
                )
            except _AssignmentDataError as e:
                safe_error = format_user_error(e)
                errors.append({
                    "folder_id": folder_id,
                    "file_id": att_id,
                    "error": safe_error,
                    "type": "assignment_data",
                })
            except Exception as e:
                safe_error = format_user_error(e)
                errors.append({"folder_id": folder_id, "file_id": att_id, "error": safe_error})
                print(f"  FAILED attachment {att_id}: {safe_error}", file=sys.stderr)

    if selected_ids is not None and selected_ids - matched_ids:
        errors.append({
            "error": _ASSIGNMENT_NOT_FOUND,
            "type": "assignment_not_found",
        })
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
        return [], [], [], [{"error": format_user_error(e), "type": "assignment_list"}]

    downloaded_entries, skipped_entries, updated_entries, errors = [], [], [], []

    if not isinstance(all_folders, (list, tuple)):
        return [], [], [], [{"error": format_user_error(_INVALID_FOLDERS), "type": "assignment_list"}]
    folders = all_folders
    seen_folder_ids: set[int] = set()
    prior_path_owners = _assignment_path_owners(dest, manifest)
    claimed_prior_paths: set[Path] = set()
    for folder in folders:
        if not isinstance(folder, dict):
            errors.append({"error": format_user_error(_INVALID_FOLDERS), "type": "assignment_list"})
            continue
        folder_id = _positive_int(folder.get("Id"))
        if folder_id is None:
            errors.append({"error": format_user_error(_INVALID_IDENTIFIER), "type": "assignment_data"})
            continue
        if folder_id in seen_folder_ids:
            continue

        try:
            folder, attachments = folder_with_attachments(client, org_id, folder)
        except _AssignmentDataError as e:
            errors.append({
                "folder_id": folder_id,
                "error": format_user_error(e),
                "type": "assignment_data",
            })
            continue
        except Exception as e:
            errors.append({"folder_id": folder_id, "error": format_user_error(e)})
            continue

        if _positive_int(folder.get("Id")) is None:
            errors.append({"folder_id": folder_id, "error": format_user_error(_INVALID_IDENTIFIER), "type": "assignment_data"})
            continue

        if not isinstance(attachments, (list, tuple)):
            errors.append({
                "folder_id": folder_id,
                "error": format_user_error(_INVALID_ATTACHMENTS),
                "type": "assignment_data",
            })
            continue
        seen_folder_ids.add(folder_id)
        for att in attachments:
            if not isinstance(att, dict):
                errors.append({
                    "folder_id": folder_id,
                    "error": format_user_error(_INVALID_ATTACHMENTS),
                    "type": "assignment_data",
                })
                continue
            att_id = _positive_int(att.get("Id"))
            if att_id is None:
                errors.append({
                    "folder_id": folder_id,
                    "error": format_user_error(_INVALID_IDENTIFIER),
                    "type": "assignment_data",
                })
                continue
            if att.get("Type", "File") != "File" or not att_id:
                continue

            att_key = assignment_key(folder_id, att_id)
            manifest_entry = manifest.get(att_key)
            existing = _claim_assignment_entry(
                dest,
                manifest,
                att_key,
                folder,
                prior_path_owners,
                claimed_prior_paths,
                allow_contested_claim=True,
            )

            matched_path = _matching_local_attachment(
                dest,
                existing,
                att.get("Size", 0),
                expected_folder=folder,
            )
            if matched_path is not None:
                if isinstance(existing, dict):
                    existing["path"] = str(
                        matched_path.relative_to(_course_boundary(dest))
                    )
                    skipped_entry = {
                        "file_id": att_id, "folder_id": folder_id,
                        "filename": matched_path.name,
                    }
                    skipped_entry["path"] = str(
                        matched_path.relative_to(_course_boundary(dest))
                    )
                    skipped_entries.append(skipped_entry)
                    continue
            target_list = (
                updated_entries
                if isinstance(manifest_entry, dict)
                else downloaded_entries
            )

            try:
                target_list.append(
                    _download_and_record(
                        client,
                        org_id,
                        folder,
                        att_id,
                        dest,
                        manifest,
                        existing_entry=existing,
                    )
                )
            except _AssignmentDataError as e:
                errors.append({
                    "folder_id": folder_id,
                    "file_id": att_id,
                    "error": format_user_error(e),
                    "type": "assignment_data",
                })
            except Exception as e:
                errors.append({"folder_id": folder_id, "file_id": att_id, "error": format_user_error(e)})

    return downloaded_entries, skipped_entries, updated_entries, errors
