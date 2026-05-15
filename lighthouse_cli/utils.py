"""Shared utility functions for lighthouse-cli."""

from __future__ import annotations

import re
import urllib.parse

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
