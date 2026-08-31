"""Assignment submission commands for lighthouse-cli."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from .api import (
    CourseNotFoundError,
    LighthouseClient,
    SubmissionOutcomeUnknownError,
    resolve_course_id,
)
from .display import format_user_error, output_json as _output_json, safe_display_text, utc_now_iso as _utc_now_iso
from .utils import get_course_name as _get_course_name


_MAX_DISPLAY_NAME_LENGTH = 256
_MAX_SUBMISSION_ID = (1 << 63) - 1
_DEFAULT_COURSE_NAME = "Unknown course"
_DEFAULT_FOLDER_NAME = "Unknown folder"
_DEFAULT_FILE_NAME = "Unknown file"
_CLIENT_INIT_ERROR = "Could not initialize Lighthouse client."


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
    Shows course name, folder name, and file path before submitting. In JSON
    mode, the prompt is written to stderr so stdout remains JSON-only.

    On success, prints JSON with submission details (submission_id, folder_id,
    folder_name, course_id, course_name, file, submitted_at).

    Non-interactive / agent-friendly: --yes + --json = only JSON on stdout.
    """
    # Validate the local input before constructing a client or resolving any
    # remote identifiers. A declined submission should not read the file body,
    # so defer ``read_bytes`` until after confirmation below.
    try:
        file_path_obj = Path(file_path).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return _submit_error("File not found.", json_output)
    if not file_path_obj.exists() or not file_path_obj.is_file():
        # The path is user input and may include secrets or local filesystem
        # details. Keep the diagnostic fixed while retaining the useful local
        # failure category.
        return _submit_error("File not found.", json_output)

    filename = file_path_obj.name
    display_filename = _safe_display_name(filename, _DEFAULT_FILE_NAME)

    # Keep the explicit confirmation requirement for non-interactive callers.
    # This check happens after local validation, but before any API work.
    if not yes and not sys.stdin.isatty():
        return _submit_error(
            "Refusing to submit without --yes in non-interactive mode. Use --yes flag to confirm.",
            json_output,
        )

    try:
        client = LighthouseClient()
    except Exception:
        return _submit_error(_CLIENT_INIT_ERROR, json_output)

    try:
        org_id = resolve_course_id(client, course_id)
        course_name = _safe_display_name(_get_course_name(client, org_id), _DEFAULT_COURSE_NAME)
        folder_id_int = _resolve_folder_id(client, org_id, folder_id)
    except Exception as e:
        return _submit_error(e, json_output)

    folder_name = _get_folder_name(client, org_id, folder_id_int)

    # Confirmation prompt (skip with --yes). JSON-mode prompts must not pollute
    # stdout; ``input`` is called without a prompt because input() writes its
    # prompt to stdout.
    if not yes:
        prompt_stream = sys.stderr if json_output else sys.stdout
        print(
            f"Submit to '{folder_name}' in '{course_name}'?\n  File: {display_filename}",
            file=prompt_stream,
        )
        print("Confirm [y/N]: ", end="", flush=True, file=prompt_stream)
        try:
            response = input()
        except (EOFError, KeyboardInterrupt, OSError):
            response = ""
        if not isinstance(response, str) or response.strip().lower() not in ("y", "yes"):
            print("Submission cancelled.", file=prompt_stream)
            if json_output:
                _output_json({"cancelled": True})
            return 0

    # Read the body only once the user has confirmed (or --yes bypassed the
    # prompt), so declined submissions do no unnecessary file work.
    try:
        file_bytes = file_path_obj.read_bytes()
    except OSError:
        # Do not echo a path or OS error details; both can contain sensitive
        # local information. The fixed message is sufficient for recovery.
        return _submit_error("Could not read file.", json_output)

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
        return _submit_error(e, json_output)

    # A successful POST can still leave the remote outcome ambiguous if the
    # response body is malformed or unexpectedly shaped.  Do not turn that
    # into an ``AttributeError`` (or tell callers to blindly retry): the
    # request may already have been accepted by D2L.
    if not isinstance(result, dict):
        return _submit_error(
            "Submission outcome is unknown because the API returned an unsupported result shape. "
            "Verify the assignment status before trying again.",
            json_output,
        )

    # Build output
    submitted_at = _safe_display_name(result.get("submittedAt"), _utc_now_iso())
    submission_id = _safe_submission_id(result.get("submissionId"))
    output_filename = display_filename
    if json_output:
        _output_json({
            "submission_id": submission_id, "folder_id": folder_id_int,
            "folder_name": folder_name, "course_id": org_id,
            "course_name": course_name,
            "file": {"name": output_filename, "size_bytes": len(file_bytes)},
            "submitted_at": submitted_at,
        })
    else:
        print(f"Submitted successfully!\n"
              f"  Submission ID: {submission_id}\n  Folder: {folder_name}\n"
              f"  Course: {course_name}\n  File: {output_filename}\n"
              f"  Submitted at: {submitted_at}")

    return 0


def _submit_error(message: BaseException | str, json_output: bool) -> int:
    """Emit a safe submit failure without double-formatting its diagnostic.

    ``display.error`` intentionally treats a preformatted string as untrusted
    and may collapse it to ``Command failed.``.  Submit has a few known states
    where a fixed recovery hint is safe and materially useful, so classify the
    exception here and emit the already-sanitized template directly. Unknown
    exception text still goes through the centralized formatter exactly once.
    """
    safe_message = _safe_submit_error(message)
    print(f"Error: {safe_message}", file=sys.stderr)
    if json_output:
        _output_json({"error": safe_message})
    return 1


def _safe_submit_error(message: BaseException | str) -> str:
    """Return an allowlisted, actionable submit diagnostic.

    Never interpolate identifiers, paths, folder listings, response bodies, or
    other exception text into the fixed templates below. Those values can be
    useful to a debugger but are not safe for normal CLI output.
    """
    if isinstance(message, FileNotFoundError):
        return "Dropbox folder not found. Run: lighthouse assignments"
    if isinstance(message, PermissionError):
        return "Permission denied. Check your enrollment and submission rights."
    if isinstance(message, SubmissionOutcomeUnknownError):
        return (
            "Submission outcome is unknown because the API returned an unsupported result shape. "
            "Verify the assignment status before trying again."
        )
    if isinstance(message, _InvalidFolderIdentifierError):
        return "Folder identifier is invalid. Use a positive numeric FolderId or a folder name."
    if isinstance(message, CourseNotFoundError):
        if "ambiguous" in str(message).casefold() or "multiple courses" in str(message).casefold():
            return "Ambiguous course match. Use a numeric OrgUnitId for an exact match."
        return "Course not found. Run: lighthouse courses"
    if isinstance(message, ValueError) and "ambiguous" in str(message).casefold():
        return "Ambiguous folder match. Use the numeric FolderId for an exact match."
    if isinstance(message, str):
        normalized = " ".join(message.split())
        lowered = normalized.casefold()
        if lowered.startswith("file not found"):
            return "File not found."
        if lowered.startswith("could not read file"):
            return "Could not read file."
        if lowered.startswith("could not initialize lighthouse client"):
            return _CLIENT_INIT_ERROR
        if lowered.startswith("refusing to submit without --yes"):
            return (
                "Refusing to submit without --yes in non-interactive mode. "
                "Use --yes flag to confirm."
            )
        if lowered.startswith("submission outcome is unknown"):
            return (
                "Submission outcome is unknown because the API returned an unsupported result shape. "
                "Verify the assignment status before trying again."
            )
    return format_user_error(message)


def _resolve_folder_id(client: LighthouseClient, org_id: int, identifier: object) -> int:
    """Resolve a folder identifier (numeric ID or name substring) to an int folder ID.

    Raises FileNotFoundError if zero matches (with suggestions to run assignments).
    Raises ValueError if multiple matches (ambiguous).
    """
    normalized, numeric_id = _normalise_folder_identifier(identifier)
    folders = client.get_dropbox_folders(org_id)
    if not isinstance(folders, (list, tuple)):
        raise _InvalidFolderIdentifierError

    # Numeric identifiers are matched against only valid positive IDs from the
    # remote response. This prevents bools, floats, zero, negatives, and other
    # malformed metadata from reaching the write-capable submit endpoint.
    if numeric_id is not None:
        if any(
            isinstance(folder, dict) and _positive_folder_id(folder.get("Id")) == numeric_id
            for folder in folders
        ):
            return numeric_id
        raise FileNotFoundError(
            f"Folder '{normalized}' not found in course {org_id}. Run: lighthouse assignments"
        )

    # Name substring match. Ignore malformed records until a matching folder is
    # found; a matching malformed record is an explicit safe error below.
    matches = [
        folder
        for folder in folders
        if isinstance(folder, dict)
        and isinstance(folder.get("Name"), str)
        and normalized.casefold() in folder["Name"].casefold()
    ]
    if len(matches) == 1:
        folder_id = _positive_folder_id(matches[0].get("Id"))
        if folder_id is None:
            raise _InvalidFolderIdentifierError
        return folder_id
    if len(matches) > 1:
        raise ValueError(
            "Ambiguous folder match. Multiple folders matched the supplied name. "
            "Use the numeric FolderId for an exact match."
        )

    # Do not echo folder IDs, names, or the caller's identifier in a normal
    # diagnostic; those values originate in untrusted API/user input.
    raise FileNotFoundError(
        "Folder not found. Run: lighthouse assignments"
    )


class _InvalidFolderIdentifierError(ValueError):
    """Raised when local or remote folder metadata is not a positive ID."""


def _normalise_folder_identifier(identifier: object) -> tuple[str, int | None]:
    """Validate a folder selector and return ``(text, numeric_id_or_none)``."""
    if isinstance(identifier, bool):
        raise _InvalidFolderIdentifierError
    if isinstance(identifier, int):
        if identifier <= 0:
            raise _InvalidFolderIdentifierError
        return str(identifier), identifier
    if not isinstance(identifier, str):
        raise _InvalidFolderIdentifierError

    normalized = identifier.strip()
    if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise _InvalidFolderIdentifierError
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", normalized):
        # Numeric-looking selectors must be strict positive integer IDs. Do
        # not reinterpret floats, zero, negatives, or signed values as names.
        if re.fullmatch(r"\d+", normalized) and int(normalized) > 0:
            return normalized, int(normalized)
        raise _InvalidFolderIdentifierError
    return normalized, None


def _positive_folder_id(value: object) -> int | None:
    """Coerce only strict positive integer folder IDs from API data."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _get_folder_name(client: LighthouseClient, org_id: int, folder_id: int) -> str:
    """Get the name of a dropbox folder by ID."""
    try:
        detail = client.get_dropbox_folder_detail(org_id, folder_id)
    except Exception:
        return _DEFAULT_FOLDER_NAME
    if not isinstance(detail, dict):
        return _DEFAULT_FOLDER_NAME
    return _safe_display_name(detail.get("Name"), _DEFAULT_FOLDER_NAME)


def _safe_display_name(value: object, fallback: str) -> str:
    """Project an API-provided label onto a bounded printable scalar."""
    return safe_display_text(value, fallback, max_len=_MAX_DISPLAY_NAME_LENGTH)


def _safe_submission_id(value: object) -> int | None:
    """Project a submission identifier onto a bounded non-negative integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_SUBMISSION_ID else None
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        try:
            parsed = int(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed <= _MAX_SUBMISSION_ID else None
    return None
