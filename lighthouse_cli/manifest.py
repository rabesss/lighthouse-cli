"""Manifest system for lighthouse-cli.

Each course directory contains a `.lighthouse.json` file (hidden dotfile) that
maps topic_id -> {sha256, filename, size, downloaded_at, last_modified}.

This module provides:
- Manifest class: load(), save(), validate(), atomic_write()
- SHA-256 computation from exact file bytes
- Atomic writes via temp file + os.replace()
- last_modified sourced from TOC LastModifiedDate (not HTTP headers)
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ManifestError(Exception):
    """Base exception for manifest operations."""


class ManifestCorruptError(ManifestError):
    """Raised when manifest exists but is not valid JSON."""


class ManifestValidationError(ManifestError):
    """Raised when manifest entry is missing required keys or has wrong types."""


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

MANIFEST_FILENAME = ".lighthouse.json"
REQUIRED_ENTRY_KEYS = frozenset({"sha256", "filename", "size", "downloaded_at", "last_modified"})


def compute_sha256(content: bytes) -> str:
    """Compute SHA-256 hex digest of raw file bytes."""
    return hashlib.sha256(content).hexdigest()


def utc_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Manifest class
# ---------------------------------------------------------------------------

class Manifest:
    """Represents a .lighthouse.json manifest for a single course.

    Attributes:
        path: Path to the .lighthouse.json file (or None if not yet on disk)
        entries: dict mapping topic_id (str) -> entry dict
    """

    def __init__(self, entries: dict[str, dict[str, Any]] | None = None) -> None:
        self.path: Path | None = None
        self.entries: dict[str, dict[str, Any]] = entries if entries is not None else {}

    # -- loading -----------------------------------------------------------

    @staticmethod
    def load(path: Path) -> "Manifest":
        """Load a manifest from disk.

        Raises:
            ManifestCorruptError: if file exists but is not valid JSON
        """
        if not path.exists():
            return Manifest()

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ManifestCorruptError(f"Corrupt manifest at {path}: {exc}") from exc

        if not isinstance(data, dict):
            raise ManifestCorruptError(f"Manifest at {path} is not a JSON object")

        manifest = Manifest(data)
        manifest.path = path
        return manifest

    # -- validation --------------------------------------------------------

    def validate_entry(self, topic_id: str, entry: Any) -> list[str]:
        """Validate a single manifest entry.

        Returns:
            List of error messages (empty if valid).
        """
        errors: list[str] = []
        if not isinstance(entry, dict):
            return [f"Entry for {topic_id} is not a dict"]

        missing = REQUIRED_ENTRY_KEYS - set(entry.keys())
        if missing:
            errors.append(f"Entry for {topic_id} missing keys: {missing}")

        # Type checks
        if "sha256" in entry and not isinstance(entry["sha256"], str):
            errors.append(f"Entry for {topic_id}: sha256 must be a string")
        if "filename" in entry and not isinstance(entry["filename"], str):
            errors.append(f"Entry for {topic_id}: filename must be a string")
        if "size" in entry and not isinstance(entry["size"], (int, float)):
            errors.append(f"Entry for {topic_id}: size must be a number")
        if "downloaded_at" in entry and not isinstance(entry["downloaded_at"], str):
            errors.append(f"Entry for {topic_id}: downloaded_at must be a string")
        if "last_modified" in entry and not isinstance(entry["last_modified"], str):
            errors.append(f"Entry for {topic_id}: last_modified must be a string")

        return errors

    def validate(self) -> list[str]:
        """Validate all entries in the manifest.

        Returns:
            List of error messages (empty if all valid).
        """
        errors: list[str] = []
        if not isinstance(self.entries, dict):
            return ["Manifest entries is not a dict"]
        for topic_id, entry in self.entries.items():
            errors.extend(self.validate_entry(topic_id, entry))
        return errors

    # -- saving (atomic) ---------------------------------------------------

    def save(self, path: Path) -> None:
        """Write manifest atomically: write to temp file, then os.replace().

        This ensures that a crash mid-write leaves the old manifest intact,
        never a partially-written file.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False), encoding="utf-8")
            # Atomic on POSIX; on Windows this is still the safest approach
            os.replace(tmp, path)
        except Exception:
            # Clean up temp file on failure
            if tmp.exists():
                tmp.unlink()
            raise
        finally:
            # Ensure temp file is gone even on success (shouldn't happen but defensive)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

        self.path = path

    # -- entry management --------------------------------------------------

    def add_entry(
        self,
        topic_id: str,
        *,
        content: bytes,
        filename: str,
        last_modified: str,
    ) -> dict[str, Any]:
        """Add or update a manifest entry for a downloaded topic.

        Computes SHA-256 from the exact file bytes.
        """
        entry = {
            "sha256": compute_sha256(content),
            "filename": filename,
            "size": len(content),
            "downloaded_at": utc_now(),
            "last_modified": last_modified,
        }
        self.entries[str(topic_id)] = entry
        return entry

    def get(self, topic_id: str) -> dict[str, Any] | None:
        """Get entry for a topic_id, or None if not in manifest."""
        return self.entries.get(str(topic_id))

    def __contains__(self, topic_id: object) -> bool:
        return str(topic_id) in self.entries

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)
