"""Tests for the manifest system (.lighthouse.json per course directory)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from lighthouse_cli.manifest import (
    MAX_MANIFEST_SIZE,
    Manifest,
    ManifestCorruptError,
    ManifestError,
    REQUIRED_ENTRY_KEYS,
    compute_file_sha256,
    compute_sha256,
    normalize_sha256,
    MANIFEST_FILENAME,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_course_dir(tmp_path: Path) -> Path:
    """Return a temporary course directory."""
    course_dir = tmp_path / "Signals & Systems"
    course_dir.mkdir()
    return course_dir


@pytest.fixture
def manifest_path(temp_course_dir: Path) -> Path:
    """Return the manifest path in a temp course directory."""
    return temp_course_dir / MANIFEST_FILENAME


# ---------------------------------------------------------------------------
# compute_sha256
# ---------------------------------------------------------------------------

class TestComputeSHA256:
    def test_compute_file_sha256_streams_file_contents(self, tmp_path: Path) -> None:
        path = tmp_path / "payload.bin"
        path.write_bytes(b"chunked payload")

        assert compute_file_sha256(path, chunk_size=3) == compute_sha256(
            b"chunked payload"
        )

    def test_sha256_from_bytes(self):
        """SHA-256 is computed from raw file bytes, not filename."""
        content = b"Hello, World!"
        digest = compute_sha256(content)
        # SHA-256 for b"Hello, World!" (Python's hashlib uses this exact string)
        assert len(digest) == 64  # Always 64 hex chars for SHA-256

    def test_sha256_different_content_different_hash(self):
        """Different byte content produces different hashes."""
        h1 = compute_sha256(b"file1 content")
        h2 = compute_sha256(b"file2 content")
        assert h1 != h2

    def test_sha256_same_content_same_hash(self):
        """Identical content always produces the same hash."""
        h1 = compute_sha256(b"identical content")
        h2 = compute_sha256(b"identical content")
        assert h1 == h2

    def test_sha256_binary_content(self):
        """SHA-256 works with non-UTF-8 binary data (e.g. PDF bytes)."""
        # PDF header bytes (this is NOT a valid PDF but tests binary handling)
        binary = bytes([0x25, 0x50, 0x44, 0x46, 0x2D, 0x31, 0x2E, 0x34])  # %PDF-1.4
        digest = compute_sha256(binary)
        assert len(digest) == 64  # SHA-256 hex is always 64 chars


# ---------------------------------------------------------------------------
# Manifest schema
# ---------------------------------------------------------------------------

class TestManifestSchema:
    def test_manifest_empty_default(self):
        """An empty Manifest has no entries."""
        m = Manifest()
        assert len(m) == 0
        assert m.path is None

    def test_manifest_from_dict(self):
        """Manifest can be initialized with a dict of entries."""
        entries = {
            "12345": {
                "sha256": "abc123",
                "filename": "Lecture 1.pdf",
                "size": 1024,
                "downloaded_at": "2026-05-10T10:00:00Z",
                "last_modified": "2026-01-01T00:00:00Z",
            }
        }
        m = Manifest(entries)
        assert len(m) == 1
        assert "12345" in m
        assert m.get("12345")["filename"] == "Lecture 1.pdf"

    def test_manifest_has_required_keys(self):
        """Every manifest entry must have sha256, filename, size, downloaded_at, last_modified."""
        entry = {
            "sha256": "abc123",
            "filename": "Lecture 1.pdf",
            "size": 1024,
            "downloaded_at": "2026-05-10T10:00:00Z",
            "last_modified": "2026-01-01T00:00:00Z",
        }
        assert REQUIRED_ENTRY_KEYS.issubset(entry.keys())

    def test_manifest_missing_key_rejected(self, manifest_path: Path):
        """validate_entry returns errors for missing required keys."""
        m = Manifest()
        entry = {
            "sha256": "abc123",
            "filename": "Lecture 1.pdf",
            # missing: size, downloaded_at, last_modified
        }
        errors = m.validate_entry("12345", entry)
        assert len(errors) > 0
        assert "size" in errors[0] or "missing" in " ".join(errors).lower()

    def test_manifest_wrong_type_rejected(self, manifest_path: Path):
        """validate_entry returns errors for wrong types."""
        m = Manifest()
        entry = {
            "sha256": 12345,  # should be string
            "filename": "Lecture 1.pdf",
            "size": "1024",   # should be number
            "downloaded_at": "2026-05-10T10:00:00Z",
            "last_modified": "2026-01-01T00:00:00Z",
        }
        errors = m.validate_entry("12345", entry)
        assert len(errors) > 0

    def test_manifest_roundtrip(self, manifest_path: Path):
        """Manifest saves and loads identical data."""
        entries = {
            "12345": {
                "sha256": "a" * 64,
                "filename": "Lecture 1.pdf",
                "size": 1024,
                "downloaded_at": "2026-05-10T10:00:00Z",
                "last_modified": "2026-01-01T00:00:00Z",
            },
            "12346": {
                "sha256": "b" * 64,
                "filename": "Lecture 2.pdf",
                "size": 2048,
                "downloaded_at": "2026-05-10T10:05:00Z",
                "last_modified": "2026-02-01T00:00:00Z",
            },
        }
        m = Manifest(entries)
        m.save(manifest_path)

        loaded = Manifest.load(manifest_path)
        assert len(loaded) == 2
        assert loaded.get("12345")["sha256"] == "a" * 64
        assert loaded.get("12346")["filename"] == "Lecture 2.pdf"

    def test_manifest_load_missing_file_returns_empty(self, tmp_path: Path):
        """load() on non-existent path returns empty Manifest (no error)."""
        m = Manifest.load(tmp_path / "nonexistent.json")
        assert len(m) == 0

    def test_manifest_load_corrupt_raises(self, manifest_path: Path):
        """load() on corrupt JSON raises ManifestCorruptError."""
        manifest_path.write_text("not valid json {", encoding="utf-8")
        with pytest.raises(ManifestCorruptError):
            Manifest.load(manifest_path)

    def test_manifest_load_corrupt_preserves_old_file(self, manifest_path: Path):
        """Corrupt manifest file is left untouched after failed load attempt."""
        manifest_path.write_text("not valid json {", encoding="utf-8")
        with pytest.raises(ManifestCorruptError):
            Manifest.load(manifest_path)
        # File still exists and is still corrupt
        assert manifest_path.exists()

    @pytest.mark.parametrize("size", ["seven", -1, float("nan"), float("inf"), True])
    def test_manifest_load_rejects_malformed_sizes(self, manifest_path: Path, size):
        """Untrusted sizes are rejected before sync arithmetic can see them."""
        manifest_path.write_text(json.dumps({"100": {
            "sha256": "",
            "filename": "file.pdf",
            "size": size,
            "downloaded_at": "2026-05-10T10:00:00Z",
            "last_modified": "2026-01-01T00:00:00Z",
        }}), encoding="utf-8")

        with pytest.raises(ManifestCorruptError, match="size"):
            Manifest.load(manifest_path)

    @pytest.mark.parametrize("entry", [None, [], "not-an-entry", 42])
    def test_manifest_load_rejects_non_object_entries(self, manifest_path: Path, entry):
        manifest_path.write_text(json.dumps({"100": entry}), encoding="utf-8")

        with pytest.raises(ManifestCorruptError, match="Entry for 100"):
            Manifest.load(manifest_path)

    def test_normalize_sha256_rejects_arbitrary_hash_text(self):
        assert normalize_sha256("not-a-digest") == ""
        assert normalize_sha256("a" * 64) == "a" * 64

    def test_manifest_save_rejects_non_digest_hash(self, manifest_path: Path):
        entry = {
            "sha256": "not-a-digest",
            "filename": "file.pdf",
            "size": 0,
            "downloaded_at": "2026-05-10T10:00:00Z",
            "last_modified": "2026-01-01T00:00:00Z",
        }

        with pytest.raises(ManifestError, match="sha256"):
            Manifest({"100": entry}).save(manifest_path)

    def test_manifest_load_normalizes_uppercase_sha256(self, manifest_path: Path):
        manifest_path.write_text(json.dumps({"100": {
            "sha256": "A" * 64,
            "filename": "file.pdf",
            "size": 1,
            "downloaded_at": "2026-05-10T10:00:00Z",
            "last_modified": "2026-01-01T00:00:00Z",
        }}), encoding="utf-8")

        loaded = Manifest.load(manifest_path)

        assert loaded.get("100")["sha256"] == "a" * 64

    def test_manifest_load_rejects_huge_integer_size(self, manifest_path: Path):
        manifest_path.write_text(json.dumps({"100": {
            "sha256": "a" * 64,
            "filename": "file.pdf",
            "size": MAX_MANIFEST_SIZE + 1,
            "downloaded_at": "2026-05-10T10:00:00Z",
            "last_modified": "2026-01-01T00:00:00Z",
        }}), encoding="utf-8")

        with pytest.raises(ManifestCorruptError, match="size"):
            Manifest.load(manifest_path)

    def test_manifest_save_is_owner_only(self, manifest_path: Path):
        entry = {
            "sha256": "",
            "filename": "file.pdf",
            "size": 0,
            "downloaded_at": "2026-05-10T10:00:00Z",
            "last_modified": "2026-01-01T00:00:00Z",
        }
        Manifest({"100": entry}).save(manifest_path)

        assert stat.S_IMODE(manifest_path.stat().st_mode) & 0o077 == 0


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

class TestManifestAtomicWrite:
    def test_atomic_write_leaves_no_temp_file(self, manifest_path: Path):
        """After save(), no .json.tmp file remains."""
        m = Manifest({"12345": {
            "sha256": "a" * 64,
            "filename": "test.pdf",
            "size": 100,
            "downloaded_at": "2026-05-10T10:00:00Z",
            "last_modified": "2026-01-01T00:00:00Z",
        }})
        m.save(manifest_path)

        # No temp files
        tmp_files = list(manifest_path.parent.glob("*.tmp"))
        assert tmp_files == []

    def test_atomic_write_creates_manifest(self, manifest_path: Path):
        """save() creates the manifest file."""
        m = Manifest({"12345": {
            "sha256": "a" * 64,
            "filename": "test.pdf",
            "size": 100,
            "downloaded_at": "2026-05-10T10:00:00Z",
            "last_modified": "2026-01-01T00:00:00Z",
        }})
        m.save(manifest_path)
        assert manifest_path.exists()
        assert manifest_path.is_file()

    def test_atomic_write_no_partial_on_failure(self, manifest_path: Path):
        """Simulated crash after writing temp file leaves old manifest intact."""
        # Pre-write an old manifest
        old_entries = {"99999": {
            "sha256": "a" * 64,
            "filename": "old.pdf",
            "size": 99,
            "downloaded_at": "2026-01-01T10:00:00Z",
            "last_modified": "2025-01-01T00:00:00Z",
        }}
        Manifest(old_entries).save(manifest_path)

        # Simulate a stale temp artifact left by an interrupted write.
        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"incomplete": True}), encoding="utf-8")  # Write tmp but don't replace

        # The committed manifest remains authoritative; a stale temp artifact
        # is ignored and must never replace it.
        loaded = Manifest.load(manifest_path)
        assert loaded.entries == old_entries
        assert json.loads(tmp.read_text(encoding="utf-8")) == {"incomplete": True}

    def test_atomic_write_is_valid_json_after_save(self, manifest_path: Path):
        """The written manifest is always valid JSON (no truncation)."""
        m = Manifest({"12345": {
            "sha256": "a" * 64,
            "filename": "test.pdf",
            "size": 100,
            "downloaded_at": "2026-05-10T10:00:00Z",
            "last_modified": "2026-01-01T00:00:00Z",
        }})
        m.save(manifest_path)

        # Should parse without error
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# add_entry
# ---------------------------------------------------------------------------

class TestManifestAddEntry:
    def test_add_entry_computes_sha256_from_bytes(self):
        """add_entry computes SHA-256 from the content bytes."""
        m = Manifest()
        content = b"file content here"
        entry = m.add_entry(
            "12345",
            content=content,
            filename="lecture.pdf",
            last_modified="2026-01-01T00:00:00Z",
        )
        expected = compute_sha256(content)
        assert entry["sha256"] == expected

    def test_add_entry_stores_size(self):
        """add_entry stores the byte length of the content."""
        m = Manifest()
        content = b"x" * 500
        entry = m.add_entry(
            "12345",
            content=content,
            filename="lecture.pdf",
            last_modified="2026-01-01T00:00:00Z",
        )
        assert entry["size"] == 500

    def test_add_entry_stores_filename(self):
        """add_entry stores the sanitized filename."""
        m = Manifest()
        entry = m.add_entry(
            "12345",
            content=b"content",
            filename="Lecture 1.pdf",
            last_modified="2026-01-01T00:00:00Z",
        )
        assert entry["filename"] == "Lecture 1.pdf"

    def test_add_entry_stores_last_modified(self):
        """add_entry stores the last_modified from TOC (not current time)."""
        m = Manifest()
        toc_date = "2026-03-15T12:00:00Z"
        entry = m.add_entry(
            "12345",
            content=b"content",
            filename="lecture.pdf",
            last_modified=toc_date,
        )
        assert entry["last_modified"] == toc_date

    def test_add_entry_has_downloaded_at(self):
        """add_entry sets downloaded_at to current UTC time (ISO 8601)."""
        m = Manifest()
        entry = m.add_entry(
            "12345",
            content=b"content",
            filename="lecture.pdf",
            last_modified="2026-01-01T00:00:00Z",
        )
        assert "downloaded_at" in entry
        # Should be an ISO 8601 timestamp
        assert "T" in entry["downloaded_at"]
        assert entry["downloaded_at"].endswith("Z")

    def test_add_entry_topic_id_stringified(self):
        """topic_id is stored as string key."""
        m = Manifest()
        m.add_entry(
            12345,
            content=b"content",
            filename="lecture.pdf",
            last_modified="2026-01-01T00:00:00Z",
        )
        assert "12345" in m
        assert 12345 in m  # int also works


# ---------------------------------------------------------------------------
# Binary integrity
# ---------------------------------------------------------------------------

class TestBinaryIntegrity:
    def test_binary_file_preserved_exactly(self, tmp_path: Path):
        """Downloaded binary file bytes are preserved byte-for-byte."""
        # Simulate a PDF with non-UTF-8 bytes
        binary_content = bytes(range(256))  # All byte values 0-255
        file_path = tmp_path / "binary.bin"
        file_path.write_bytes(binary_content)

        read_back = file_path.read_bytes()
        assert read_back == binary_content
        assert compute_sha256(read_back) == compute_sha256(binary_content)

    def test_sha256_matches_written_file(self, tmp_path: Path):
        """SHA-256 of file on disk matches computed from original bytes."""
        original = b"PDF content with non-ASCII \xe2\x82\xac\x00\xff"
        file_path = tmp_path / "test.pdf"
        file_path.write_bytes(original)

        disk_hash = compute_sha256(file_path.read_bytes())
        assert disk_hash == compute_sha256(original)
