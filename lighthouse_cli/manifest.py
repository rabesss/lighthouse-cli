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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import _parse_finite_float, _reject_non_finite_json, atomic_write


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ManifestError(Exception):
    """Base exception for manifest operations."""


class ManifestCorruptError(ManifestError):
    """Raised when manifest exists but is not valid JSON."""


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

MANIFEST_FILENAME = ".lighthouse.json"
REQUIRED_ENTRY_KEYS = frozenset({"sha256", "filename", "size", "downloaded_at", "last_modified"})
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# Keep untrusted integer arithmetic bounded before deriving display metadata.
MAX_MANIFEST_SIZE = (1 << 63) - 1


def is_valid_sha256(value: Any) -> bool:
    """Return whether *value* is a complete hexadecimal SHA-256 digest.

    Older manifests may contain a non-empty, non-digest string in the
    ``sha256`` field.  Those values remain loadable for compatibility and are
    surfaced as metadata, but callers must not use them as content identity.
    """
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def normalize_sha256(value: Any) -> str:
    """Return a canonical SHA-256 digest or an empty string.

    Digest values are case-insensitive on input, but normalizing accepted
    values keeps duplicate keys and comparisons deterministic.  Unknown or
    legacy hash text is intentionally not treated as content identity.
    """
    return value.lower() if is_valid_sha256(value) else ""


def _path_has_symlink_component(path: Path) -> bool:
    """Return whether *path* or one of its existing parents is a symlink.

    Manifest paths are write targets.  Resolving a path before checking it is
    unsafe because an existing course directory symlink would then become the
    apparent trust root.  ``lstat`` preserves the lexical path boundary and
    rejects symlink components before any read, mkdir, or atomic replace.
    """
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            if current.is_symlink():
                return True
        except OSError:
            # A path that cannot be inspected is not a safe write target.
            return True
    return False


def compute_sha256(content: bytes) -> str:
    """Compute SHA-256 hex digest of raw file bytes."""
    return hashlib.sha256(content).hexdigest()


def compute_file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Compute a file digest without buffering the whole file in memory."""
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


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
    def load(path: Path) -> Manifest:
        """Load a manifest from disk.

        Raises:
            ManifestCorruptError: if file exists but is not valid JSON
        """
        if _path_has_symlink_component(path):
            raise ManifestCorruptError("Manifest path is symlinked.")

        if not path.exists():
            return Manifest()

        try:
            data = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_non_finite_json,
                parse_float=_parse_finite_float,
            )
        except (json.JSONDecodeError, OSError, UnicodeError, ValueError):
            raise ManifestCorruptError("Manifest is corrupt or unreadable.") from None

        if not isinstance(data, dict):
            raise ManifestCorruptError("Manifest is not a JSON object.")

        normalized_entries: dict[str, Any] = {}
        for topic_id, entry in data.items():
            if isinstance(entry, dict):
                entry = dict(entry)
                digest = normalize_sha256(entry.get("sha256"))
                # Keep legacy records loadable, but never carry arbitrary
                # strings forward as content identity.  A later save will
                # persist the isolated empty value after the topic is
                # reconciled.
                if "sha256" in entry:
                    entry["sha256"] = digest
            normalized_entries[str(topic_id)] = entry
        manifest = Manifest(normalized_entries)
        if errors := manifest.validate():
            detail = "; ".join(errors[:8])
            if len(errors) > 8:
                detail += f"; and {len(errors) - 8} more"
            raise ManifestCorruptError(f"Invalid manifest data: {detail}")
        manifest.path = path
        return manifest

    # -- validation --------------------------------------------------------

    def validate_entry(self, _topic_id: str, entry: Any) -> list[str]:
        """Validate a single manifest entry.

        Returns:
            List of error messages (empty if valid).
        """
        errors: list[str] = []
        if not isinstance(entry, dict):
            return ["Manifest entry is not an object"]

        if missing := REQUIRED_ENTRY_KEYS - set(entry.keys()):
            errors.append(f"Manifest entry missing keys: {missing}")

        # Type checks
        type_map = {
            "sha256": str,
            "filename": str,
            "downloaded_at": str,
            "last_modified": str,
            "path": str,
        }
        for key, expected_type in type_map.items():
            if key in entry and not isinstance(entry[key], expected_type):
                errors.append(
                    f"Manifest entry: {key} must be a {expected_type}"
                )

        if (
            "sha256" in entry
            and isinstance(entry["sha256"], str)
            and entry["sha256"]
            and not is_valid_sha256(entry["sha256"])
        ):
            errors.append(
                "Manifest entry: sha256 must be a 64-character hexadecimal digest"
            )

        if "size" in entry:
            size = entry["size"]
            invalid_size = (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or size > MAX_MANIFEST_SIZE
            )
            if invalid_size:
                errors.append(
                    "Manifest entry: size must be a finite non-negative number"
                )

        if "filename" in entry and isinstance(entry["filename"], str) and not entry["filename"]:
            errors.append("Manifest entry: filename must not be empty")

        return errors

    def validate(self) -> list[str]:
        """Validate all entries in the manifest.

        Returns:
            List of error messages (empty if all valid).
        """
        if not isinstance(self.entries, dict):
            return ["Manifest entries is not a dict"]
        return [e for tid, entry in self.entries.items() for e in self.validate_entry(tid, entry)]

    # -- saving (atomic) ---------------------------------------------------

    def save(self, path: Path) -> None:
        """Write manifest atomically (temp file + os.replace).

        This ensures that a crash mid-write leaves the old manifest intact,
        never a partially-written file.
        """
        if _path_has_symlink_component(path):
            raise ManifestError("Refusing to write through a symlinked manifest path.")
        if errors := self.validate():
            detail = "; ".join(errors[:8])
            if len(errors) > 8:
                detail += f"; and {len(errors) - 8} more"
            raise ManifestError(f"Invalid manifest: {detail}")
        entries_to_save: dict[str, Any] = {}
        for topic_id, entry in self.entries.items():
            if isinstance(entry, dict):
                entry = dict(entry)
                if "sha256" in entry:
                    entry["sha256"] = normalize_sha256(entry.get("sha256"))
            entries_to_save[str(topic_id)] = entry
        self.entries = entries_to_save
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            path,
            json.dumps(entries_to_save, indent=2, ensure_ascii=False, allow_nan=False),
            mode=0o600,
        )
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
            "sha256": compute_sha256(content), "filename": filename,
            "size": len(content), "downloaded_at": utc_now(),
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
