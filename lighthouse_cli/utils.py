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

    Also URL-decodes percent-encoded characters so that
    ``L1-L2%20Introduction%20to%20computing.pdf`` becomes
    ``L1-L2 Introduction to computing.pdf``.
    """
    decoded = urllib.parse.unquote(name)
    return _SANITIZE_RE.sub("_", decoded).strip(". ")
