"""One sync/download engine for per-course Content Topic pipelines.

Owns the entire per-course pipeline — TOC walk, manifest reconciliation,
topic download, SHA-256 dedup, assignment hooks, single save — for every
mode:

    SYNC      incremental: skip topics whose manifest ``last_modified``
              matches the TOC's ``LastModifiedDate``
    DOWNLOAD  re-download every matching topic; unrelated manifest entries
              (other topics, assignment attachments) are preserved
    FORCE     rebuild the whole manifest, then behave like DOWNLOAD
    PLAN      ``--dry-run``: walk the TOC and decide, but perform NO
              file-body fetches, NO filesystem writes and NO manifest
              mutation (``FORCE`` + ``PLAN`` therefore deletes nothing)

The engine emits no human output to stdout; assignment-phase failures log
to stderr outside the topic pipeline (assignments.py).  Every outcome —
including warnings — is returned as data for the caller to render.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from os.path import normpath
from pathlib import Path
import re
from stat import S_ISREG
from typing import Any
from urllib.parse import unquote

from .api import LighthouseClient
from .assignments import assignment_key, download_for_course, sync_for_course
from .display import format_user_error, safe_display_text
from .manifest import (
    MANIFEST_FILENAME,
    MAX_MANIFEST_SIZE,
    Manifest,
    ManifestCorruptError,
    compute_file_sha256,
    compute_sha256,
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


class Mode(Enum):
    """Explicit pipeline mode for :func:`run_course`."""

    SYNC = "sync"
    DOWNLOAD = "download"
    FORCE = "force"
    PLAN = "plan"


_INVALID_TOPIC_DATA = "Topic record has an invalid identifier."
_MAX_LAST_MODIFIED_LENGTH = 256
_MAX_TOC_DEPTH = 2048
_MAX_TOC_NODES = 10000
_TOC_TRAVERSAL_ERROR = "Content TOC exceeded safe traversal limits."
_MAX_COURSE_NAME_LENGTH = 256
_MAX_UNKNOWN_TYPE_LENGTH = 64
_MAX_TOPIC_LABEL_LENGTH = 256
_INVALID_ASSIGNMENT_FOLDERS = "Assignment folders have an invalid response shape."
_ASSIGNMENT_NOT_FOUND = "Requested assignment folder was not found."
_OUTPUT_PATH_SECRET_RE = re.compile(
    r"(?ix)(?<![a-z0-9])(?:"
    r"pass(?:word|wd|phrase)?(?:[\s_-]?value)?|secret|"
    r"token(?:[\s_-]?value)?|cookie(?:s|value)?|samlresponse|otp|totp|"
    r"canary|authorization|bearer|api[\s_-]?key|access[\s_-]?token|"
    r"client[\s_-]?secret|session(?:[\s_-]?(?:val|value|token|id))?|"
    r"d2l(?:secure)?session(?:val|value|token)?|"
    r"d2l[\s_-]?same[\s_-]?site[\s_-]?canary[ab]?|api[\s_-]?canary|"
    r"s?ctx|sft|flow[\s_-]?token|o?postparams"
    r")\s*(?:[:=]|\bis\b|\bwas\b)\s*[^/\\\s,;]+"
)
_OUTPUT_PATH_BARE_SECRET_RE = re.compile(
    r"(?x)(?<![a-z0-9])(?i:password|passwd|passphrase|secret|token|"
    r"cookie(?:s|value)?|otp|totp|canary)\b\s+"
    r"(?:[a-z0-9._-]*\d[a-z0-9._-]*|[a-z][a-z0-9._-]{7,}|"
    r"[A-Z0-9_-]{2,}|(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{3,})"
    r"(?![a-z0-9])"
)
_OUTPUT_PATH_SENSITIVE_KEY_RE = re.compile(
    r"(?ix)(?<![a-z0-9])(?:"
    r"session[\s_-]?(?:val|value|token|id)|"
    r"d2l(?:secure)?session(?:val|value|token)?|"
    r"d2l[\s_-]?same[\s_-]?site[\s_-]?canary[ab]?|api[\s_-]?canary|"
    r"sctx|sft|flow[\s_-]?token|o?postparams|"
    r"access[\s_-]?token|client[\s_-]?secret|x?[\s_-]?api[\s_-]?key|"
    r"samlresponse|authorization|bearer"
    r")(?![a-z0-9])"
)
_OUTPUT_PATH_CONTEXT_VALUE_RE = re.compile(
    r"(?x)(?<![a-z0-9])(?i:ctx|cookies?)\b\s+"
    r"(?:(?i:value|token|secret|sentinel)\b|"
    r"[a-z0-9._-]*[a-z_][a-z0-9._-]*\d[a-z0-9._-]*|"
    r"[a-z][a-z0-9._-]{7,}|(?=[A-Z0-9_-]*[A-Z_])[A-Z0-9_-]{2,}|"
    r"(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z_])"
    r"[A-Za-z0-9_-]{3,})"
    r"(?![a-z0-9])"
)
_OUTPUT_PATH_SESSION_VALUE_RE = re.compile(
    r"(?x)(?<![a-z0-9])(?i:session)\b\s+"
    r"(?:[a-z0-9._-]*[a-z_][a-z0-9._-]*\d[a-z0-9._-]*|"
    r"[a-z][a-z0-9._-]{7,}|(?=[A-Z0-9_-]*[A-Z_])[A-Z0-9_-]{2,}|"
    r"(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z_])"
    r"[A-Za-z0-9_-]{3,})"
    r"(?![a-z0-9])"
)


def _positive_int(value: object) -> int | None:
    """Return a strictly positive integer topic identifier, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _safe_last_modified(value: object) -> str | None:
    """Return a bounded printable TOC timestamp, or ``None`` when invalid."""
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > _MAX_LAST_MODIFIED_LENGTH:
        return None
    return value if all(character.isprintable() for character in value) else None


def _safe_course_name(value: object, org_id: int) -> str:
    """Return a bounded, printable, non-secret course name for all outputs."""
    fallback = f"Course-{org_id}"
    return safe_display_text(value, fallback, max_len=_MAX_COURSE_NAME_LENGTH)


def _safe_label(value: object, fallback: str = "") -> str:
    """Return a bounded printable label without secret-shaped content."""
    return safe_display_text(value, fallback, max_len=_MAX_TOPIC_LABEL_LENGTH)


def _safe_display_filename(value: object) -> str:
    """Return a safe basename for result and progress projections."""
    candidate = _safe_label(value)
    if not candidate or "/" in candidate or "\\" in candidate:
        return ""
    return candidate


def _safe_topic_filename(value: object, topic_id: int) -> str:
    """Return a safe local filename or a fixed topic-ID fallback."""
    fallback = f"topic_{topic_id}"
    candidate = _safe_label(value)
    if not candidate:
        return fallback
    sanitized = _safe_label(_sanitize_filename(candidate))
    return _fit_filename(sanitized) if sanitized else fallback


def _collision_filename(filename: str, topic_id: int, attempt: int) -> str:
    """Add a bounded topic identity suffix to a colliding filename."""
    path = Path(filename)
    suffix = path.suffix
    stem = path.name[:-len(suffix)] if suffix else path.name
    topic_token = str(topic_id)
    if len(topic_token) > 20:
        topic_token = hashlib.sha256(topic_token.encode("ascii")).hexdigest()[:16]
    marker = f"--topic-{topic_token}" + (f"-{attempt}" if attempt > 1 else "")
    max_stem_bytes = MAX_ATOMIC_TARGET_NAME_BYTES - len(
        (marker + suffix).encode("utf-8")
    )
    while stem and len(stem.encode("utf-8")) > max_stem_bytes:
        stem = stem[:-1]
    return f"{stem or 'topic'}{marker}{suffix}"


def _reserve_topic_path(
    file_dest: Path,
    filename: str,
    topic_id: int,
    path_owners: dict[Path, str | None],
    *,
    allow_unowned_overwrite: bool,
) -> Path:
    """Reserve one local path for a topic without overwriting another owner."""
    tid = str(topic_id)
    candidate = file_dest / filename
    if candidate.is_symlink():
        raise ValueError("Topic filename is a symlink; refusing to overwrite it")

    attempt = 0
    while True:
        key = candidate.absolute()
        owner = path_owners.get(key)
        if owner == tid or (
            allow_unowned_overwrite
            and key in path_owners
            and owner is None
        ) or (
            key not in path_owners
            and (allow_unowned_overwrite or not candidate.exists())
        ):
            path_owners[key] = tid
            return candidate
        attempt += 1
        candidate = file_dest / _collision_filename(filename, topic_id, attempt)
        if candidate.is_symlink():
            raise ValueError("Topic filename is a symlink; refusing to overwrite it")


def _safe_unknown_type(value: object) -> str | None:
    """Return an allowlisted unknown type label, or ``None`` for unsafe input."""
    safe_value = safe_display_text(value, max_len=_MAX_UNKNOWN_TYPE_LENGTH)
    if not safe_value or not re.fullmatch(r"[a-z][a-z0-9_-]*", safe_value):
        return None
    return safe_value


def _normalise_size(value: Any) -> int | None:
    """Return a safe manifest size, or ``None`` for malformed values."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_MANIFEST_SIZE
    ):
        return None
    return value


def _safe_size(value: Any) -> int:
    """Return a non-negative finite size suitable for result serialization."""
    size = _normalise_size(value)
    return size if size is not None else 0


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


def safe_output_path_text(value: object) -> str | None:
    """Return an output path only when it contains no secret-shaped value."""
    try:
        candidate = str(value)
    except Exception:
        return None
    if not candidate or len(candidate) > 4096:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate):
        return None
    decoded = candidate
    for _ in range(4):
        expanded = unquote(decoded)
        if expanded == decoded:
            break
        decoded = expanded
    if any(
        pattern.search(decoded)
        for pattern in (
            _OUTPUT_PATH_SECRET_RE,
            _OUTPUT_PATH_BARE_SECRET_RE,
            _OUTPUT_PATH_SENSITIVE_KEY_RE,
            _OUTPUT_PATH_CONTEXT_VALUE_RE,
            _OUTPUT_PATH_SESSION_VALUE_RE,
        )
    ):
        return None
    return candidate


def validate_output_root(root: Path) -> Path:
    """Validate and canonicalize a user-selected output directory.

    The lexical path must be checked before ``resolve()``.  Otherwise a
    symlink supplied as ``--output-dir`` would be silently promoted to the
    trusted root and all subsequent course writes would land at its target.
    Existing symlink components are rejected consistently with course/module
    path handling.  A missing root is allowed and is created lazily by the
    first successful topic or attachment write.
    """
    try:
        candidate = Path(root).expanduser().absolute()
    except (TypeError, ValueError, OSError):
        raise ValueError("Unable to validate output directory") from None
    if safe_output_path_text(candidate) is None:
        raise ValueError("Output directory contains unsafe text")
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        try:
            if current.is_symlink():
                raise ValueError("Output directory contains a symlinked path")
        except OSError:
            raise ValueError("Unable to validate output directory") from None
    if candidate.exists() and not candidate.is_dir():
        raise ValueError("Output directory is not a directory")
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ValueError("Unable to validate output directory") from None


def _topic_directory(
    dest: Path,
    raw_path: Any,
    *,
    warnings: list[str] | None = None,
) -> tuple[Path, Path]:
    """Resolve a topic's directory under a lexical course boundary.

    The returned pair is ``(course_root, directory)``.  Traversal and
    absolute paths are clamped to the course root for compatibility with the
    TOC sanitization defense.  Existing symlinked directory components are a
    distinct hard failure: resolving them first would let an attacker turn a
    trusted course path into an outside write.
    """
    course_root = _course_boundary(dest)
    try:
        topic_path = Path(raw_path)
    except (TypeError, ValueError, OSError):
        raise ValueError("Invalid topic path") from None

    file_dest = topic_path.parent
    if not file_dest.is_absolute():
        file_dest = course_root / file_dest

    try:
        normalized_dest = Path(normpath(str(file_dest.absolute())))
        relative_dest = normalized_dest.relative_to(course_root)
    except ValueError:
        relative_dest = None
    if (
        relative_dest is not None
        and relative_dest.parts
        and relative_dest.parts[0].casefold() == "assignments"
    ):
        file_dest = course_root / "_Content" / relative_dest
        if warnings is not None:
            warnings.append(
                "Topic path overlapped the reserved Assignments directory; "
                "moved under _Content."
            )

    if _has_symlink_component(file_dest, course_root):
        raise ValueError("Topic path contains a symlinked course directory")

    # Keep both lexical and resolved containment checks.  The lexical check
    # prevents an absolute symlink path outside the selected root from being
    # accepted merely because it resolves back inside it.
    try:
        lexical_inside = file_dest.absolute().is_relative_to(course_root)
        resolved = file_dest.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("Unable to validate topic path") from None
    if not lexical_inside or not resolved.is_relative_to(course_root):
        if warnings is not None:
            warnings.append(
                "Path for topic was resolved outside the course root; "
                "clamped to the course root."
            )
        file_dest = course_root
    return course_root, file_dest


def _validate_course_destination(output_root: Path, dest: Path) -> None:
    """Reject symlinked course/manifest paths before any filesystem action."""
    try:
        root_resolved = output_root.resolve(strict=False)
        dest_resolved = dest.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ValueError("Unable to validate course destination") from None
    if not dest_resolved.is_relative_to(root_resolved):
        raise ValueError("Course destination escapes the output root")
    if dest.is_symlink() or _has_symlink_component(dest, output_root):
        raise ValueError("Course destination is a symlink")
    if dest.exists() and not dest.is_dir():
        raise ValueError("Course destination is not a directory")
    manifest_path = dest / MANIFEST_FILENAME
    if _has_symlink_component(manifest_path, output_root):
        raise ValueError("Course manifest is a symlinked path")


def build_entry(tid: str, name: str, path: str, content_or_entry: bytes | dict, sha: str = "") -> dict:
    """Build a sync/download entry dict. content_or_entry is bytes (content) or dict (manifest entry)."""
    safe_tid = str(tid)
    safe_name = _safe_display_filename(name)
    safe_path = path if isinstance(path, str) else ""
    manifest_entry = content_or_entry if isinstance(content_or_entry, dict) else {}
    size = len(content_or_entry) if isinstance(content_or_entry, bytes) else _safe_size(manifest_entry.get("size", 0))
    normalized_sha = normalize_sha256(sha)
    if not normalized_sha and isinstance(content_or_entry, bytes):
        normalized_sha = compute_sha256(content_or_entry)
    if not normalized_sha and isinstance(content_or_entry, dict):
        normalized_sha = normalize_sha256(manifest_entry.get("sha256", ""))
    return {
        "topic_id": safe_tid,
        "filename": safe_name,
        "path": safe_path,
        "size": size,
        "size_kb": round(size / 1024, 1),
        "sha256": normalized_sha,
        **({"extension": Path(safe_name).suffix.lower()} if safe_name and "." in safe_name else {}),
    }


def fetch_toc_and_name(client: LighthouseClient, org_id: int) -> tuple[dict, str]:
    """Fetch content TOC and course name. Raises on failure."""
    return client.get_content_toc(org_id), get_course_name(client, org_id)


def download_and_persist_topic(
    client: LighthouseClient,
    org_id: int,
    topic: dict,
    dest: Path,
    manifest: Manifest,
    *,
    path_owners: dict[Path, str | None],
    allow_unowned_overwrite: bool = False,
    warnings: list[str] | None = None,
) -> tuple[bytes, str, Path]:
    """Download a topic, write to disk, update manifest. Returns (content, name, path).

    When *warnings* is given, a path-containment clamp records an entry there.
    """
    topic_id = _positive_int(topic.get("topic_id")) if isinstance(topic, dict) else None
    if topic_id is None:
        raise ValueError(_INVALID_TOPIC_DATA)
    last_modified = _safe_last_modified(topic.get("last_modified"))
    if last_modified is None:
        raise ValueError(_INVALID_TOPIC_DATA)
    tid = str(topic_id)
    course_root, file_dest = _topic_directory(
        dest, topic.get("path", ""), warnings=warnings,
    )
    topic_type = topic.get("type", "")
    if isinstance(topic_type, str) and topic_type.lower() == "html":
        content, raw_filename = client.get_topic_html(org_id, topic_id)
    else:
        content, filename = client.download_topic_file(org_id, topic_id)
        raw_filename = filename
    if not isinstance(content, bytes):
        raise ValueError(_INVALID_TOPIC_DATA)
    sanitized_name = _safe_topic_filename(raw_filename, topic_id)
    if _has_symlink_component(file_dest, course_root):
        raise ValueError("Topic path contains a symlinked course directory")
    file_dest.mkdir(parents=True, exist_ok=True)
    filepath = _reserve_topic_path(
        file_dest,
        sanitized_name,
        topic_id,
        path_owners,
        allow_unowned_overwrite=allow_unowned_overwrite,
    )
    sanitized_name = filepath.name
    try:
        filepath_resolved = filepath.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ValueError("Unable to validate topic file path") from None
    if not filepath.absolute().is_relative_to(course_root) or not filepath_resolved.is_relative_to(course_root):
        raise ValueError("Topic file path escapes the course root")
    atomic_write(filepath, content, mode=0o600)
    manifest.add_entry(tid, content=content, filename=sanitized_name, last_modified=last_modified)
    return content, sanitized_name, filepath


def parse_type_filter(types: str) -> tuple[set[str], list[str]]:
    """Parse a comma-separated content-type filter string into a validated set.

    Accepts "file", "html", or comma-separated combos. Returns
    ``(valid_set, unknown_values)`` so the caller can record unknown
    values as warnings. Falls back to ``{"file"}`` when nothing valid
    remains.
    """
    if not isinstance(types, str):
        return {"file"}, []
    valid, raw = {"file", "html"}, {t.strip().lower() for t in types.split(",")}
    return (raw & valid) or {"file"}, sorted(raw - valid)


def _record_topic_data_error(
    errors: list[dict[str, Any]] | None,
    message: str = _INVALID_TOPIC_DATA,
) -> None:
    """Record a fixed error for one malformed TOC record, when requested."""
    if errors is not None:
        errors.append({"error": message, "type": "topic_data"})


def flatten_all_topics(
    modules: Any,
    prefix: str = "",
    *,
    errors: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Collect safe topic records from a possibly malformed content TOC.

    Invalid module/topic records are skipped.  When *errors* is supplied, each
    skipped record contributes a fixed ``topic_data`` error without copying
    untrusted values into the result.  Traversal is iterative and bounded so a
    malformed deep or cyclic fixture cannot exhaust the Python call stack.
    """
    if modules is None:
        return []
    topics: list[dict[str, Any]] = []
    work: list[tuple[str, Any, str, int, frozenset[int]]] = [
        ("modules", modules, prefix, 0, frozenset()),
    ]
    node_count = 0
    limit_reported = False

    def report_limit() -> None:
        nonlocal limit_reported
        if not limit_reported:
            _record_topic_data_error(errors, _TOC_TRAVERSAL_ERROR)
            limit_reported = True

    while work:
        kind, value, current_prefix, depth, active = work.pop()

        if kind == "modules":
            if value is None:
                continue
            if not isinstance(value, (list, tuple)):
                _record_topic_data_error(errors)
                continue
            value_id = id(value)
            if value_id in active:
                report_limit()
                continue
            next_active = active | {value_id}
            remaining = max(_MAX_TOC_NODES - node_count, 0)
            if len(value) > remaining:
                report_limit()
                value = value[:remaining]
            for module in reversed(value):
                work.append(("module", module, current_prefix, depth, next_active))
            continue

        if kind == "topics":
            if value is None:
                continue
            if not isinstance(value, (list, tuple)):
                _record_topic_data_error(errors)
                continue
            value_id = id(value)
            if value_id in active:
                report_limit()
                continue
            next_active = active | {value_id}
            remaining = max(_MAX_TOC_NODES - node_count, 0)
            if len(value) > remaining:
                report_limit()
                value = value[:remaining]
            for topic in reversed(value):
                work.append(("topic", topic, current_prefix, depth, next_active))
            continue

        if depth > _MAX_TOC_DEPTH:
            report_limit()
            continue
        if node_count >= _MAX_TOC_NODES:
            report_limit()
            work.clear()
            continue
        node_count += 1

        if kind == "module":
            if not isinstance(value, dict):
                _record_topic_data_error(errors)
                continue
            module_id = id(value)
            if module_id in active:
                report_limit()
                continue
            module_active = active | {module_id}

            raw_module_title = value.get("Title", "")
            if raw_module_title is None:
                safe_module_title = ""
            elif isinstance(raw_module_title, str):
                safe_module_title = _safe_label(raw_module_title)
                if raw_module_title and not safe_module_title:
                    safe_module_title = "Module"
                else:
                    safe_module_title = _sanitize_filename(safe_module_title)
            else:
                _record_topic_data_error(errors)
                safe_module_title = ""
            new_prefix = f"{current_prefix}/{safe_module_title}" if current_prefix else safe_module_title

            # The recursive implementation visited child modules before this
            # module's topics. Push in reverse stack order to preserve that.
            work.append(("topics", value.get("Topics", []), new_prefix, depth, module_active))
            work.append(("modules", value.get("Modules", []), new_prefix, depth + 1, module_active))
            continue

        if not isinstance(value, dict):
            _record_topic_data_error(errors)
            continue
        raw_topic_title = value.get("Title", "")
        if raw_topic_title is None:
            safe_topic_title = ""
        elif isinstance(raw_topic_title, str):
            safe_topic_title = _safe_label(raw_topic_title)
        else:
            _record_topic_data_error(errors)
            continue

        raw_topic_type = value.get("TypeIdentifier", "")
        if raw_topic_type is None:
            safe_topic_type = ""
        elif isinstance(raw_topic_type, str):
            safe_topic_type = raw_topic_type
        else:
            _record_topic_data_error(errors)
            continue

        topic_path_name = _sanitize_filename(safe_topic_title)
        if raw_topic_title and not safe_topic_title:
            topic_path_name = "Topic"
        topics.append({
            "topic_id": value.get("TopicId"),
            "title": safe_topic_title,
            "url": value.get("Url"),
            "type": safe_topic_type,
            "path": f"{current_prefix}/{topic_path_name}",
            "last_modified": value.get("LastModifiedDate", ""),
        })
    return topics


def _empty_result(org_id: int, mode: Mode) -> dict[str, Any]:
    """Skeleton result dict with every collection present (never printed)."""
    return {
        "org_id": org_id, "mode": mode, "course_name": "", "dest": None,
        "manifest_path": None, "topic_count": 0, "planned": [],
        "downloaded": [], "skipped": [], "updated": [], "duplicates": [],
        "orphaned": [], "errors": [], "warnings": [],
        "assignments": {"downloaded": [], "skipped": [], "updated": [], "errors": []},
        "manifest_total": 0, "saved": False, "empty": False,
    }


def _assignment_selector_error(
    folders: object,
    assignment_id: int,
) -> dict[str, str] | None:
    """Validate an assignment folder snapshot for one requested ID."""
    if _positive_int(assignment_id) is None:
        return {"error": _ASSIGNMENT_NOT_FOUND, "type": "assignment_not_found"}
    if not isinstance(folders, (list, tuple)):
        return {"error": _INVALID_ASSIGNMENT_FOLDERS, "type": "assignment_list"}
    for folder in folders:
        if not isinstance(folder, dict):
            continue
        if _positive_int(folder.get("Id")) == assignment_id:
            return None
    return {"error": _ASSIGNMENT_NOT_FOUND, "type": "assignment_not_found"}


def _preflight_assignment_selector(
    client: LighthouseClient,
    org_id: int,
    assignment_id: int,
    folder_snapshot: object | None,
) -> tuple[list[dict[str, Any]] | tuple[dict[str, Any], ...] | None, dict[str, str] | None]:
    """Fetch/validate one assignment snapshot before any course processing."""
    if folder_snapshot is None:
        try:
            folder_snapshot = client.get_dropbox_folders(org_id)
        except Exception as exc:
            return None, {"error": format_user_error(exc), "type": "assignment_list"}
    error = _assignment_selector_error(folder_snapshot, assignment_id)
    if error is not None:
        return None, error
    return folder_snapshot, None


def _load_manifest(manifest_path: Path, result: dict[str, Any]) -> Manifest:
    """Load the manifest, recording a warning + error entry when corrupt."""
    manifest = Manifest()
    try:
        return Manifest.load(manifest_path)
    except ManifestCorruptError as exc:
        # Manifest error text is fixed-string sanitized. Keep the printed
        # warning constant so this direct stderr path remains safe if those
        # messages gain detail in the future.
        result["warnings"].append("Corrupt manifest; performing full sync.")
        result["errors"].append({"error": str(exc), "type": "manifest_corrupt"})
        return manifest


def _track_duplicate(sha_hashes: dict[str, list[dict]], file_hash: str, tid: str, filename: str) -> None:
    """Record an entry hash for per-course SHA-256 duplicate detection."""
    normalized_hash = normalize_sha256(file_hash)
    if not normalized_hash:
        return
    sha_hashes.setdefault(normalized_hash, []).append({"topic_id": tid, "filename": filename})


def _matching_local_topic_file(dest: Path, topic: dict[str, Any], entry: dict[str, Any]) -> Path | None:
    """Return the manifest path when its local bytes match the manifest.

    A matching TOC timestamp alone is not sufficient to skip a topic: the
    local file may have been removed, replaced by a symlink, or truncated
    since the manifest was written.  ``lstat`` deliberately rejects symlinks
    while the resolved-path check also rejects symlinked parent directories
    that would point outside the course root.  The content digest is checked
    as well, so an edited file with the same size cannot be silently skipped.
    """
    if not isinstance(entry, dict):
        return None
    filename = entry.get("filename", "")
    if not isinstance(filename, str) or not filename:
        return None
    if _safe_display_filename(filename) != filename:
        return None
    filename_path = Path(filename)
    if filename_path.is_absolute() or filename_path.name != filename:
        return None
    expected_size = _normalise_size(entry.get("size"))
    if expected_size is None:
        return None
    expected_hash = normalize_sha256(entry.get("sha256", ""))
    if not expected_hash:
        return None

    try:
        course_root, file_dest = _topic_directory(dest, topic.get("path", ""))
        candidate = file_dest / filename
        if not candidate.absolute().is_relative_to(course_root):
            return None
        if not candidate.resolve(strict=False).is_relative_to(course_root):
            return None
        file_stat = candidate.lstat()
    except (OSError, RuntimeError, ValueError):
        return None

    if not S_ISREG(file_stat.st_mode) or file_stat.st_size != expected_size:
        return None
    try:
        actual_hash = compute_file_sha256(candidate)
    except (OSError, ValueError):
        return None
    if actual_hash != expected_hash:
        return None
    return candidate


def run_course(
    client: LighthouseClient,
    org_id: int,
    root: Path,
    *,
    mode: Mode = Mode.SYNC,
    types: str = "file",
    include_assignments: bool = False,
    assignment_id: int | None = None,
    assignment_folders: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """Run the Content Topic pipeline for one course and return result data.

    Raises when the TOC or course name cannot be fetched — the caller
    decides whether that aborts the command or only skips the course.
    """
    result = _empty_result(org_id, mode)
    try:
        output_root = validate_output_root(root)
    except (OSError, RuntimeError, ValueError) as exc:
        result["errors"].append({"error": str(exc), "type": "path"})
        result["empty"] = True
        return result
    if assignment_id is not None:
        assignment_folders, assignment_error = _preflight_assignment_selector(
            client, org_id, assignment_id, assignment_folders,
        )
        if assignment_error is not None:
            result["assignments"]["errors"].append(assignment_error)
            return result
    toc, raw_course_name = fetch_toc_and_name(client, org_id)
    course_name = _safe_course_name(raw_course_name, org_id)
    result["course_name"] = course_name

    type_set, unknown = parse_type_filter(types)
    for u in unknown:
        if safe_type := _safe_unknown_type(u):
            result["warnings"].append(f"Unknown content type: {safe_type}")
        else:
            result["warnings"].append("Ignored unsupported content type.")
    # Keep the complete flattened TOC for reconciliation.  The requested
    # ``--types`` filter controls which topic bodies we fetch, not whether a
    # topic is still live in Brightspace.  Using only ``downloadable`` here
    # would report a live file topic as orphaned during ``--types html``.
    all_topics = flatten_all_topics(toc.get("Modules", []), errors=result["errors"])
    downloadable = [
        topic for topic in all_topics if topic.get("type", "").lower() in type_set
    ]
    valid_downloadable: list[dict[str, Any]] = []
    seen_topic_ids: set[int] = set()
    for topic in downloadable:
        topic_id = _positive_int(topic.get("topic_id"))
        last_modified = _safe_last_modified(topic.get("last_modified"))
        if topic_id is None or last_modified is None:
            result["errors"].append({"error": _INVALID_TOPIC_DATA, "type": "topic_data"})
            continue
        if topic_id in seen_topic_ids:
            continue
        seen_topic_ids.add(topic_id)
        validated_topic = dict(topic)
        validated_topic["topic_id"] = topic_id
        validated_topic["last_modified"] = last_modified
        valid_downloadable.append(validated_topic)
    downloadable = valid_downloadable
    result["topic_count"] = len(downloadable)

    dest = output_root / resolve_course_folder_name(course_name, org_id)
    manifest_path = dest / MANIFEST_FILENAME
    result["dest"], result["manifest_path"] = dest, manifest_path

    try:
        _validate_course_destination(output_root, dest)
    except (OSError, RuntimeError, ValueError) as exc:
        result["errors"].append({"error": str(exc), "type": "path"})
        result["empty"] = not downloadable and not include_assignments
        return result

    if mode is Mode.PLAN:
        result["planned"] = [
            {"topic_id": t["topic_id"], "title": t["title"], "path": t["path"]}
            for t in downloadable
        ]
        return result

    force_manifest_exists = mode is Mode.FORCE and manifest_path.exists()
    if mode is Mode.FORCE:
        # Keep the prior ownership map long enough to preserve stable paths
        # when same-name topics are reordered, but rebuild the persisted
        # manifest from only this run's successful downloads.
        try:
            ownership_manifest = Manifest.load(manifest_path)
        except ManifestCorruptError:
            ownership_manifest = Manifest()
        manifest = Manifest()
    else:
        manifest = _load_manifest(manifest_path, result)
        ownership_manifest = manifest

    live_topic_ids = {
        str(topic_id)
        for topic in all_topics
        if (topic_id := _positive_int(topic.get("topic_id"))) is not None
    }
    orphan_candidates = (
        {str(topic_id) for topic_id in manifest.entries} - live_topic_ids
        if mode is Mode.SYNC
        else set()
    )

    if not downloadable and not include_assignments:
        result["empty"] = True
        if mode is Mode.SYNC:
            result["orphaned"] = [
                build_entry(topic_id, entry.get("filename", ""), "", entry)
                for topic_id, entry in sorted(
                    ((topic_id, manifest.get(topic_id)) for topic_id in orphan_candidates),
                    key=lambda item: str(item[0]),
                )
                if isinstance(entry, dict)
            ]
            result["manifest_total"] = len(manifest)
        elif force_manifest_exists and not result["errors"]:
            manifest.save(manifest_path)
            result["saved"] = True
        return result

    downloaded, skipped, updated = result["downloaded"], result["skipped"], result["updated"]
    errors = result["errors"]
    sha_hashes: dict[str, list[dict]] = {}
    path_owners: dict[Path, str | None] = {}
    for topic in all_topics:
        topic_id = _positive_int(topic.get("topic_id"))
        if topic_id is None:
            continue
        entry = ownership_manifest.get(str(topic_id))
        if not isinstance(entry, dict):
            continue
        filename = entry.get("filename")
        if not isinstance(filename, str) or _safe_display_filename(filename) != filename:
            continue
        try:
            _course_root, file_dest = _topic_directory(dest, topic.get("path", ""))
        except (OSError, RuntimeError, ValueError):
            continue
        key = (file_dest / filename).absolute()
        tid = str(topic_id)
        if key in path_owners and path_owners[key] != tid:
            prior_owner = path_owners[key]
            if mode is Mode.FORCE:
                candidates = [tid]
                if prior_owner is not None:
                    candidates.append(prior_owner)
                path_owners[key] = min(candidates, key=int)
            else:
                path_owners[key] = None
        else:
            path_owners[key] = tid

    for topic in downloadable:
        topic_id = _positive_int(topic.get("topic_id"))
        if topic_id is None:
            # ``downloadable`` is validated above. Keep this guard local to
            # the body-fetch loop in case a caller mutates the TOC objects.
            errors.append({"error": _INVALID_TOPIC_DATA, "type": "topic_data"})
            continue
        tid = str(topic_id)
        existing = manifest.get(tid)
        orphan_candidates.discard(tid)

        if mode is Mode.SYNC and existing is not None:
            if existing.get("last_modified") == (topic.get("last_modified") or ""):
                filename = existing.get("filename", "")
                matching_file = _matching_local_topic_file(dest, topic, existing)
                if (
                    matching_file is not None
                    and path_owners.get(matching_file.absolute()) == tid
                ):
                    # Strip a leading separator from display-only skipped paths.
                    # Download writes have their own resolved-path containment clamp.
                    safe_filename = _safe_display_filename(filename)
                    rel_path = str(Path(topic["path"]).parent / safe_filename).lstrip("/\\")
                    skipped.append(build_entry(tid, safe_filename, rel_path, existing))
                    if file_hash := normalize_sha256(existing.get("sha256", "")):
                        _track_duplicate(sha_hashes, file_hash, tid, filename)
                    continue
            target_list = updated
        else:
            target_list = downloaded

        try:
            _, sanitized_name, filepath = download_and_persist_topic(
                client,
                org_id,
                topic,
                dest,
                manifest,
                path_owners=path_owners,
                allow_unowned_overwrite=mode is Mode.FORCE,
                warnings=result["warnings"],
            )
            entry = manifest.get(tid)
            if entry is None:
                raise RuntimeError(f"Manifest entry missing for downloaded topic {tid}")
            file_hash = entry.get("sha256", "")
            if file_hash:
                _track_duplicate(sha_hashes, file_hash, tid, sanitized_name)
            target_list.append(build_entry(tid, sanitized_name, str(filepath.relative_to(dest)), entry, file_hash))
        except ValueError as exc:
            if str(exc) == _INVALID_TOPIC_DATA:
                errors.append({
                    "topic_id": tid,
                    "error": _INVALID_TOPIC_DATA,
                    "type": "topic_data",
                })
            else:
                errors.append({
                    "topic_id": tid,
                    "filename": _safe_label(topic.get("title", "")),
                    "error": str(exc),
                })
        except Exception as e:
            errors.append({
                "topic_id": tid,
                "filename": _safe_label(topic.get("title", "")),
                "error": str(e),
            })

    assignments = result["assignments"]
    assignment_paths_before = {
        str(key): entry.get("path")
        for key, entry in manifest.entries.items()
        if str(key).startswith("assignment_") and isinstance(entry, dict)
    }
    if mode is Mode.SYNC:
        live_orphans = {
            tid: manifest.get(tid)
            for tid in orphan_candidates
            if isinstance(manifest.get(tid), dict)
        }
        if include_assignments:
            downloaded_a, skipped_a, updated_a, errors_a = sync_for_course(client, org_id, dest, manifest)
            assignments.update(downloaded=downloaded_a, skipped=skipped_a, updated=updated_a, errors=errors_a)
            for entry in skipped_a + updated_a + downloaded_a:
                live_orphans.pop(assignment_key(entry.get("folder_id", 0), entry.get("file_id", 0)), None)
        result["orphaned"] = [
            build_entry(tid, e.get("filename", ""), "", e)
            for tid, e in sorted(live_orphans.items(), key=lambda item: str(item[0]))
        ]
    elif include_assignments:
        downloaded_a, errors_a = download_for_course(
            client,
            org_id,
            dest,
            manifest,
            folder_ids=[assignment_id] if assignment_id is not None else None,
            folder_snapshot=assignment_folders,
            path_manifest=ownership_manifest if mode is Mode.FORCE else None,
        )
        assignments["downloaded"], assignments["errors"] = downloaded_a, errors_a

    assignment_paths_after = {
        str(key): entry.get("path")
        for key, entry in manifest.entries.items()
        if str(key).startswith("assignment_") and isinstance(entry, dict)
    }
    assignment_paths_changed = assignment_paths_after != assignment_paths_before
    force_completed_without_errors = (
        mode is Mode.FORCE
        and force_manifest_exists
        and not result["errors"]
        and not assignments["errors"]
    )
    if (
        force_completed_without_errors
        or downloaded
        or updated
        or assignments["downloaded"]
        or assignments["updated"]
        or assignment_paths_changed
    ):
        manifest.save(manifest_path)
        result["saved"] = True
    result["duplicates"] = [
        {"topic_id": e["topic_id"], "filename": e["filename"], "sha256": h}
        for h, es in sha_hashes.items() if len(es) > 1 for e in es
    ]
    result["manifest_total"] = len(manifest)
    return result
