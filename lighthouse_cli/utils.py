"""Shared utility functions for lighthouse-cli."""

from __future__ import annotations

import os
import re
import uuid
import urllib.parse
from contextlib import suppress
from pathlib import Path


# ---------------------------------------------------------------------------
# Atomic file writing
# ---------------------------------------------------------------------------

def atomic_write(path: Path, data: bytes | str, *, mode: int | None = None) -> None:
    """Write ``data`` to ``path`` atomically: unique temp file in the target
    directory, then ``os.replace()``.

    A crash mid-write leaves the previous target intact and never leaves a
    partially-written file behind; on failure the temp file is removed.
    ``mode=None`` keeps the umask default (like ``open()``); an explicit mode
    is applied to the temp file before replacement.
    """
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        if mode is not None:
            # Create the temp with its final permissions from the start:
            # chmod-after-write would leave a window where a 0600-intended
            # file is readable by others. (0o600 is umask-immune since a
            # umask can only clear group/other bits.)
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w", **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as fh:
                fh.write(data)
        elif isinstance(data, str):
            with open(tmp_path, "x", encoding="utf-8") as fh:
                fh.write(data)
        else:
            with open(tmp_path, "xb") as fh:
                fh.write(data)
        os.replace(tmp_path, path)
    except Exception:
        with suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise

# ---------------------------------------------------------------------------
# Filesystem sanitization
# ---------------------------------------------------------------------------

_SANITIZE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str) -> str:
    """Remove filesystem-unsafe characters from a filename.

    Also URL-decodes percent-encoded sequences and strips leading/trailing
    dots and spaces (to avoid hidden files and accidental relative paths).
    """
    return _SANITIZE_RE.sub("_", urllib.parse.unquote(name)).strip(". ")




def get_course_name(client, org_id: int) -> str:
    """Get the D2L course Name for an org unit.

    Uses the client's get_courses() to look up the name.
    """
    return next((c.get("Name", f"Course-{org_id}") for c in client.get_courses() if int(c.get("OrgUnitId", 0)) == org_id), f"Course-{org_id}")


def resolve_course_folder_name(course_name: str, org_unit_id: int) -> str:
    """Sanitize a course name for use as a folder name.

    Two courses with the same Name get disambiguated by appending -OrgUnitId.
    """
    return f"{_sanitize_filename(course_name)}-{org_unit_id}"
