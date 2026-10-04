"""Tests for the manifest system (.lighthouse.json per course directory)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from lighthouse_cli.manifest import (
    MANIFEST_FILENAME,
    MAX_MANIFEST_SIZE,
    REQUIRED_ENTRY_KEYS,
    Manifest,
    ManifestCorruptError,
    ManifestError,
    compute_file_sha256,
    compute_sha256,
    normalize_sha256,
)


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    course_dir = tmp_path / "Signals & Systems"
    course_dir.mkdir()
    return course_dir / MANIFEST_FILENAME


def _entry(**overrides) -> dict:
    entry = {
        "sha256": "a" * 64,
        "filename": "file.pdf",
        "size": 0,
        "downloaded_at": "2026-05-10T10:00:00Z",
        "last_modified": "2026-01-01T00:00:00Z",
    }
    entry.update(overrides)
    return entry


def _write_entry(manifest_path: Path, entry) -> None:
    manifest_path.write_text(json.dumps({"100": entry}), encoding="utf-8")


class TestComputeSHA256:
    def test_compute_file_sha256_streams_file_contents(self, tmp_path: Path) -> None:
        path = tmp_path / "payload.bin"
        path.write_bytes(b"chunked payload")

        assert compute_file_sha256(path, chunk_size=3) == compute_sha256(
            b"chunked payload"
        )

    @pytest.mark.parametrize(
        "content",
        [b"Hello, World!", bytes([0x25, 0x50, 0x44, 0x46, 0x2D, 0x31, 0x2E, 0x34])],
        ids=["text", "non-utf8-pdf-header"],
    )
    def test_sha256_is_64_hex_chars(self, content):
        assert len(compute_sha256(content)) == 64

    def test_sha256_depends_only_on_content(self):
        assert compute_sha256(b"file1 content") != compute_sha256(b"file2 content")
        assert compute_sha256(b"identical content") == compute_sha256(b"identical content")


class TestManifestSchema:
    def test_manifest_empty_default(self):
        m = Manifest()
        assert len(m) == 0
        assert m.path is None

    def test_manifest_from_dict(self):
        m = Manifest({"12345": _entry(sha256="abc123", filename="Lecture 1.pdf", size=1024)})
        assert len(m) == 1
        assert "12345" in m
        assert m.get("12345")["filename"] == "Lecture 1.pdf"

    def test_manifest_has_required_keys(self):
        assert REQUIRED_ENTRY_KEYS.issubset(_entry().keys())

    def test_manifest_missing_key_rejected(self):
        entry = {"sha256": "abc123", "filename": "Lecture 1.pdf"}
        errors = Manifest().validate_entry("12345", entry)
        assert len(errors) > 0
        assert "size" in errors[0] or "missing" in " ".join(errors).lower()

    def test_manifest_wrong_type_rejected(self):
        entry = _entry(sha256=12345, size="1024")
        assert len(Manifest().validate_entry("12345", entry)) > 0

    def test_manifest_roundtrip(self, manifest_path: Path):
        entries = {
            "12345": _entry(filename="Lecture 1.pdf", size=1024),
            "12346": _entry(
                sha256="b" * 64,
                filename="Lecture 2.pdf",
                size=2048,
                downloaded_at="2026-05-10T10:05:00Z",
                last_modified="2026-02-01T00:00:00Z",
            ),
        }
        Manifest(entries).save(manifest_path)

        loaded = Manifest.load(manifest_path)
        assert len(loaded) == 2
        assert loaded.get("12345")["sha256"] == "a" * 64
        assert loaded.get("12346")["filename"] == "Lecture 2.pdf"

    def test_manifest_load_missing_file_returns_empty(self, tmp_path: Path):
        assert len(Manifest.load(tmp_path / "nonexistent.json")) == 0

    def test_manifest_load_corrupt_raises_and_preserves_old_file(self, manifest_path: Path):
        manifest_path.write_text("not valid json {", encoding="utf-8")
        with pytest.raises(ManifestCorruptError):
            Manifest.load(manifest_path)
        assert manifest_path.read_text(encoding="utf-8") == "not valid json {"

    @pytest.mark.parametrize(
        ("sha256", "size"),
        [("", "seven"), ("", -1), ("", True), ("a" * 64, MAX_MANIFEST_SIZE + 1)],
        ids=["string", "negative", "bool", "huge-integer"],
    )
    def test_manifest_load_rejects_malformed_sizes(self, manifest_path: Path, sha256, size):
        """Untrusted sizes are rejected before sync arithmetic can see them."""
        _write_entry(manifest_path, _entry(sha256=sha256, size=size))

        with pytest.raises(ManifestCorruptError, match="size"):
            Manifest.load(manifest_path)

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999"])
    def test_manifest_load_rejects_non_finite_json_anywhere(
        self,
        manifest_path: Path,
        literal: str,
    ) -> None:
        payload = (
            '{"100":{"sha256":"","filename":"file.pdf","size":1,'
            '"downloaded_at":"2026-01-01T00:00:00Z","last_modified":"",'
            '"note":'
            + literal
            + "}}"
        )
        manifest_path.write_text(payload, encoding="utf-8")

        with pytest.raises(ManifestCorruptError, match="corrupt or unreadable"):
            Manifest.load(manifest_path)

    @pytest.mark.parametrize("entry", [None, [], "not-an-entry", 42])
    def test_manifest_load_rejects_non_object_entries(self, manifest_path: Path, entry):
        _write_entry(manifest_path, entry)

        with pytest.raises(ManifestCorruptError, match="Manifest entry"):
            Manifest.load(manifest_path)

    def test_manifest_validation_never_embeds_untrusted_identifier(
        self,
        manifest_path: Path,
    ) -> None:
        sentinel = "SAMLResponse=MANIFEST_SECRET_SENTINEL"
        manifest_path.write_text(json.dumps({sentinel: None}), encoding="utf-8")

        with pytest.raises(ManifestCorruptError) as exc_info:
            Manifest.load(manifest_path)

        assert "MANIFEST_SECRET_SENTINEL" not in str(exc_info.value)
        assert "SAMLResponse" not in str(exc_info.value)

    def test_manifest_load_wraps_invalid_utf8(self, manifest_path: Path) -> None:
        manifest_path.write_bytes(b"\xff")

        with pytest.raises(
            ManifestCorruptError,
            match="corrupt or unreadable",
        ):
            Manifest.load(manifest_path)

    def test_normalize_sha256_rejects_arbitrary_hash_text(self):
        assert normalize_sha256("not-a-digest") == ""
        assert normalize_sha256("a" * 64) == "a" * 64

    def test_manifest_save_rejects_non_digest_hash(self, manifest_path: Path):
        with pytest.raises(ManifestError, match="sha256"):
            Manifest({"100": _entry(sha256="not-a-digest")}).save(manifest_path)

    def test_manifest_load_normalizes_uppercase_sha256(self, manifest_path: Path):
        _write_entry(manifest_path, _entry(sha256="A" * 64, size=1))

        assert Manifest.load(manifest_path).get("100")["sha256"] == "a" * 64

    def test_manifest_save_is_owner_only(self, manifest_path: Path):
        Manifest({"100": _entry(sha256="")}).save(manifest_path)

        assert stat.S_IMODE(manifest_path.stat().st_mode) & 0o077 == 0


class TestManifestAtomicWrite:
    def test_save_writes_valid_json_file_and_leaves_no_temp_file(self, manifest_path: Path):
        Manifest({"12345": _entry(filename="test.pdf", size=100)}).save(manifest_path)

        assert manifest_path.is_file()
        assert isinstance(json.loads(manifest_path.read_text(encoding="utf-8")), dict)
        assert list(manifest_path.parent.glob("*.tmp")) == []

    def test_atomic_write_no_partial_on_failure(self, manifest_path: Path):
        """Simulated crash after writing temp file leaves old manifest intact."""
        old_entries = {"99999": _entry(
            filename="old.pdf",
            size=99,
            downloaded_at="2026-01-01T10:00:00Z",
            last_modified="2025-01-01T00:00:00Z",
        )}
        Manifest(old_entries).save(manifest_path)

        # Simulate a stale temp artifact left by an interrupted write.
        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"incomplete": True}), encoding="utf-8")

        # The committed manifest remains authoritative; a stale temp artifact
        # is ignored and must never replace it.
        loaded = Manifest.load(manifest_path)
        assert loaded.entries == old_entries
        assert json.loads(tmp.read_text(encoding="utf-8")) == {"incomplete": True}


class TestManifestAddEntry:
    def test_add_entry_records_content_metadata_under_string_key(self):
        m = Manifest()
        content = b"x" * 500
        # The TOC date is stored, not the download time.
        toc_date = "2026-03-15T12:00:00Z"
        entry = m.add_entry(
            12345,
            content=content,
            filename="Lecture 1.pdf",
            last_modified=toc_date,
        )

        assert entry["sha256"] == compute_sha256(content)
        assert entry["size"] == 500
        assert entry["filename"] == "Lecture 1.pdf"
        assert entry["last_modified"] == toc_date
        # downloaded_at is the current UTC time in ISO 8601.
        assert "T" in entry["downloaded_at"]
        assert entry["downloaded_at"].endswith("Z")
        assert "12345" in m
        assert 12345 in m


class TestBinaryIntegrity:
    @pytest.mark.parametrize(
        "original",
        [bytes(range(256)), b"PDF content with non-ASCII \xe2\x82\xac\x00\xff"],
        ids=["all-byte-values", "non-ascii-pdf"],
    )
    def test_binary_file_preserved_exactly(self, tmp_path: Path, original):
        file_path = tmp_path / "binary.bin"
        file_path.write_bytes(original)

        read_back = file_path.read_bytes()
        assert read_back == original
        assert compute_sha256(read_back) == compute_sha256(original)
