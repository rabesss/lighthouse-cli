"""Shared utility functions for lighthouse-cli."""

from __future__ import annotations

import os
import re
import uuid
import urllib.parse
from contextlib import suppress
from inspect import isfunction, ismethod
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Atomic file writing
# ---------------------------------------------------------------------------

# ``atomic_write`` appends ``.<32 hex>.tmp`` to the destination name. Keeping
# the base name at 218 UTF-8 bytes fits that suffix under the usual 255-byte
# per-component filesystem limit.
MAX_ATOMIC_TARGET_NAME_BYTES = 218

def atomic_write(path: Path, data: bytes | str, *, mode: int | None = None) -> None:
    """Write ``data`` to ``path`` atomically: unique temp file in the target
    directory, fsync, then ``os.replace()``.

    A crash mid-write leaves the previous target intact and never leaves a
    partially-written file behind; on failure the temp file is removed (only
    temps this call created — cleanup runs strictly after a successful
    exclusive temp creation).  ``mode=None`` keeps the umask default (like
    ``open()``); an explicit mode is applied at temp creation.

    Permission note: the kernel applies the process umask to the requested
    mode, which can only CLEAR bits (owner bits included) — it can never add
    group/other access.  ``mode=0o600`` therefore always lands ≤ 0600.
    """
    while True:
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(
                tmp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode if mode is not None else 0o666,
            )
            break
        except FileExistsError:
            continue  # uuid collision with a concurrent writer — pick a new name
    try:
        text_mode = not isinstance(data, bytes)
        with os.fdopen(
            fd,
            "wb" if not text_mode else "w",
            **({} if not text_mode else {"encoding": "utf-8"}),
        ) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise

# ---------------------------------------------------------------------------
# Filesystem sanitization
# ---------------------------------------------------------------------------

_SANITIZE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows reserves these device names regardless of extension (CON.txt is
# invalid too), so the stem is what must be checked.
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
     *(f"LPT{i}" for i in range(1, 10))}
)


def _fit_filename(name: str, *, max_bytes: int = MAX_ATOMIC_TARGET_NAME_BYTES) -> str:
    """Trim a basename to a UTF-8 byte budget while preserving a short suffix."""
    if len(name.encode("utf-8")) <= max_bytes:
        return name
    path = Path(name)
    suffix = path.suffix if len(path.suffix.encode("utf-8")) <= 17 else ""
    stem = path.name[:-len(suffix)] if suffix else path.name
    budget = max_bytes - len(suffix.encode("utf-8"))
    while stem and len(stem.encode("utf-8")) > budget:
        stem = stem[:-1]
    return f"{stem or 'file'}{suffix}"


def _sanitize_filename(name: str) -> str:
    """Remove filesystem-unsafe characters from a filename.

    Also URL-decodes percent-encoded sequences, strips leading/trailing
    dots and spaces (to avoid hidden files and accidental relative paths),
    and prefixes Windows reserved device names (CON, NUL, COM1, ...).
    """
    sanitized = _SANITIZE_RE.sub("_", urllib.parse.unquote(name)).strip(". ")
    if sanitized.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        return f"_{sanitized}"
    return sanitized


def _positive_course_id(value: Any) -> int | None:
    """Coerce only positive integer-like course IDs.

    IDs arrive from both Brightspace JSON and lightweight client doubles. Do
    not let Python's broad ``int()`` coercion turn booleans or fractional
    values into valid-looking org-unit IDs.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            course_id = int(value.strip())
        except ValueError:
            return None
        return course_id if course_id > 0 else None
    return None


def _is_unmodified_bound_method(client: Any, name: str, candidate: Any) -> bool:
    """Return whether ``candidate`` is inherited from the production client.

    This lets compatibility doubles override ``get_courses`` without making a
    failed native enrollment request look like a successful legacy response.
    ``inspect`` checks also keep monkeypatched methods out of the production
    path: a patched enrollment helper still propagates its failure.
    """
    declaring_class = next(
        (cls for cls in type(client).__mro__ if name in cls.__dict__),
        None,
    )
    return (
        declaring_class is not None
        and declaring_class.__module__ == "lighthouse_cli.api"
        and declaring_class.__name__ == "LighthouseClient"
        and isfunction(declaring_class.__dict__.get(name))
        and ismethod(candidate)
        and candidate.__func__ is declaring_class.__dict__[name]
    )


def _is_explicit_legacy_override(client: Any, candidate: Any) -> bool:
    """Return whether ``candidate`` is an explicitly supplied legacy getter.

    A real ``LighthouseClient.get_courses`` bound method is not a compatibility
    signal.  A subclass/duck-typed client that supplies its own getter is, as
    is a configured ``unittest.mock`` return value used by a legacy double.
    An unconfigured mock is deliberately excluded so a missing test setup does
    not hide a native enrollment failure.
    """
    if not callable(candidate) or _is_unmodified_bound_method(client, "get_courses", candidate):
        return False
    if type(candidate).__module__ == "unittest.mock":
        return isinstance(getattr(candidate, "return_value", None), (list, tuple)) or (
            getattr(candidate, "side_effect", None) is not None
        )
    return True




def get_enrolled_course_catalog(client: Any) -> list[dict[str, Any]]:
    """Return a normalized course catalog from the enrollment source.

    Real clients expose ``get_enrolled_courses()``. The ``get_courses()``
    fallback keeps older lightweight client doubles and third-party callers
    working without making the live path depend on Brightspace's narrower
    manage-courses endpoint. The enrolled projection is always attempted
    first when it exists. If the native helper raises, propagate that failure
    instead of silently downgrading to the narrower manage-courses endpoint.
    A client that genuinely predates the helper can still expose only
    ``get_courses``.
    """
    getter = getattr(client, "get_enrolled_courses", None)
    legacy_getter = getattr(client, "get_courses", None)
    legacy_used = False
    if callable(getter):
        try:
            raw_courses = getter()
        except Exception:
            # A legacy test/client double may deliberately override only
            # ``get_courses`` while inheriting the newer helper.  Permit that
            # explicit compatibility route, but never downgrade when both
            # methods are the native class implementations or when the
            # enrollment helper itself was replaced/monkeypatched.
            if not (
                _is_unmodified_bound_method(client, "get_enrolled_courses", getter)
                and _is_explicit_legacy_override(client, legacy_getter)
            ):
                raise
            raw_courses = legacy_getter()
            legacy_used = True
    elif callable(legacy_getter):
        raw_courses = legacy_getter()
        legacy_used = True
    else:
        raw_courses = []

    if not legacy_used and not isinstance(raw_courses, (list, tuple)) and _is_explicit_legacy_override(
        client, legacy_getter
    ):
        raw_courses = legacy_getter()
    if not isinstance(raw_courses, (list, tuple)):
        return []

    courses: dict[int, dict[str, Any]] = {}
    for raw_course in raw_courses:
        if not isinstance(raw_course, dict):
            continue
        course_id = _positive_course_id(raw_course.get("OrgUnitId"))
        if course_id is None or course_id in courses:
            continue
        course = dict(raw_course)
        course["OrgUnitId"] = course_id
        if not isinstance(course.get("Name"), str):
            course["Name"] = ""
        if not isinstance(course.get("Code"), str):
            course["Code"] = ""
        courses[course_id] = course
    return [courses[course_id] for course_id in sorted(courses)]


def get_course_name(client: Any, org_id: int) -> str:
    """Get the D2L course Name for an org unit.

    Uses the paginated enrolled-course projection so courses that are absent
    from the manage-courses endpoint can still be named.  A lightweight client
    double that predates the projection may expose only ``get_courses``; that
    compatibility path is used only when the projection is unavailable or
    fails to load.
    """
    fallback = f"Course-{org_id}"
    courses = get_enrolled_course_catalog(client)

    try:
        target_id = int(org_id)
    except (TypeError, ValueError):
        return fallback

    for course in courses:
        if not isinstance(course, dict):
            continue
        try:
            course_id = int(course.get("OrgUnitId", 0))
        except (TypeError, ValueError):
            continue
        if course_id == target_id:
            name = course.get("Name")
            return name if isinstance(name, str) and name else fallback
    return fallback


def resolve_course_folder_name(course_name: str, org_unit_id: int) -> str:
    """Sanitize a course name for use as a folder name.

    Two courses with the same Name get disambiguated by appending -OrgUnitId.
    """
    suffix = f"-{org_unit_id}"
    safe_name = _fit_filename(
        _sanitize_filename(course_name),
        max_bytes=255 - len(suffix.encode("utf-8")),
    )
    return f"{safe_name}{suffix}"
