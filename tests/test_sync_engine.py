"""Zero-mock decision tests for the sync engine (lighthouse_cli/sync_engine.py).

Style follows test_manifest.py: plain data in, plain data out. The engine is
exercised through a fake client (no mocks, no network) plus CliRunner smoke
tests for the exit-code matrix and dry-run fixes.
"""

from __future__ import annotations

import contextlib
import json
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli
from lighthouse_cli.commands import _run_and_render_multi
from lighthouse_cli.manifest import MANIFEST_FILENAME, Manifest, compute_sha256 as manifest_compute_sha256
from lighthouse_cli.sync_engine import (
    Mode,
    _safe_topic_filename,
    _topic_directory,
    build_entry,
    flatten_all_topics,
    run_course,
    validate_output_root,
)
from lighthouse_cli.utils import atomic_write as shared_atomic_write

ORG_ID = 44347
LM_OLD = "2026-01-01T00:00:00Z"
LM_NEW = "2026-05-01T00:00:00Z"


def test_multi_json_retains_top_level_course_failures(tmp_path, capsys) -> None:
    with patch(
        "lighthouse_cli.commands.run_course",
        side_effect=[RuntimeError("course one failed"), RuntimeError("course two failed")],
    ):
        rc = _run_and_render_multi(
            LighthouseClient(),
            [111, 222],
            tmp_path,
            Mode.DOWNLOAD,
            "download",
            "file",
            100,
            "Sem I",
            [],
            True,
            False,
        )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 1
    assert payload["summary"]["courses_checked"] == 2
    assert [course["course_id"] for course in payload["courses"]] == [111, 222]
    assert all(course["errors"] for course in payload["courses"])


def test_multi_failure_json_redacts_secret_shaped_root(tmp_path, capsys) -> None:
    root = tmp_path / "password=ROOT_PATH_SECRET"
    with patch(
        "lighthouse_cli.commands.run_course",
        side_effect=RuntimeError("course failed"),
    ):
        rc = _run_and_render_multi(
            LighthouseClient(),
            [111],
            root,
            Mode.DOWNLOAD,
            "download",
            "file",
            100,
            "Sem I",
            [],
            True,
            False,
        )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["courses"][0]["root"] is None
    assert "ROOT_PATH_SECRET" not in json.dumps(payload)


def test_benign_keyword_output_root_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "ctx" / "cookies" / "courses"

    assert validate_output_root(root) == root.absolute()


@pytest.mark.parametrize(
    "key",
    [
        "d2lSessionVal",
        "d2lSecureSessionVal",
        "d2lSameSiteCanaryA",
        "d2lSameSiteCanaryB",
        "apiCanary",
        "sFT",
        "sCtx",
        "flowToken",
    ],
)
def test_output_root_rejects_exact_session_secret_keys(
    tmp_path: Path,
    key: str,
) -> None:
    root = tmp_path / f"{key}=OUTPUT_PATH_SECRET"

    with pytest.raises(ValueError, match="unsafe text"):
        validate_output_root(root)


def test_topic_directory_normalizes_before_reserved_subtree_check(
    tmp_path: Path,
) -> None:
    course_dir = tmp_path / "Course-1"
    warnings: list[str] = []

    _root, topic_dir = _topic_directory(
        course_dir,
        "other/../Assignments/HW1/topic.pdf",
        warnings=warnings,
    )

    assert topic_dir == course_dir / "_Content" / "Assignments" / "HW1"
    assert warnings == [
        "Topic path overlapped the reserved Assignments directory; moved under _Content."
    ]


# ---------------------------------------------------------------------------
# Fake client and data helpers
# ---------------------------------------------------------------------------

class FakeClient:
    """Stand-in for LighthouseClient: canned data in, recorded calls out."""

    def __init__(
        self,
        tocs: dict[int, dict] | None = None,
        names: dict[int, str] | None = None,
        files: dict[int, tuple[bytes, str]] | None = None,
        html: dict[int, tuple[bytes, str]] | None = None,
        folders: dict[int, list[dict]] | None = None,
        details: dict[tuple[int, int], dict] | None = None,
        attachments: dict[tuple[int, int], tuple[bytes, str]] | None = None,
    ) -> None:
        self.tocs = tocs or {}
        self.names = names or {}
        self.files = files or {}
        self.html = html or {}
        self.folders = folders or {}
        self.details = details or {}
        self.attachments = attachments or {}
        self.calls: list[tuple] = []

    def get_courses(self) -> list[dict]:
        self.calls.append(("courses",))
        return [{"OrgUnitId": oid, "Name": name, "Code": "X"} for oid, name in self.names.items()]

    def get_content_toc(self, org_id: int) -> dict:
        self.calls.append(("toc", org_id))
        return self.tocs[org_id]

    def download_topic_file(self, org_id: int, topic_id: int) -> tuple[bytes, str]:
        self.calls.append(("file", org_id, topic_id))
        payload = self.files[topic_id]
        if isinstance(payload, Exception):
            raise payload
        return payload

    def get_topic_html(self, org_id: int, topic_id: int) -> tuple[bytes, str]:
        self.calls.append(("html", org_id, topic_id))
        return self.html[topic_id]

    def get_dropbox_folders(self, org_id: int) -> list[dict]:
        self.calls.append(("folders", org_id))
        return self.folders.get(org_id, [])

    def get_dropbox_folder_detail(self, org_id: int, folder_id: int) -> dict:
        self.calls.append(("folder_detail", org_id, folder_id))
        return self.details[(org_id, folder_id)]

    def download_attachment(self, org_id: int, folder_id: int, att_id: int) -> tuple[bytes, str]:
        self.calls.append(("attachment", org_id, folder_id, att_id))
        return self.attachments[(org_id, att_id)]

    def body_calls(self) -> list[tuple]:
        """Calls that fetch file bodies (forbidden in PLAN mode)."""
        return [c for c in self.calls if c[0] in ("file", "html")]


def _toc(*topics: tuple[int, str, str, str], module: str = "Mod") -> dict:
    """Build a TOC from (topic_id, title, type, last_modified) tuples."""
    return {"Modules": [{
        "ModuleId": 1, "Title": module, "Modules": [],
        "Topics": [
            {"TopicId": tid, "Title": title, "TypeIdentifier": ttype, "Url": "", "LastModifiedDate": lm}
            for tid, title, ttype, lm in topics
        ],
    }]}


def _std_toc(cid: int) -> dict:
    """Per-course TOC used by the CliRunner matrix tests (topic id = cid*10)."""
    return _toc((cid * 10, "f.pdf", "File", LM_NEW))


def _mentry(lm: str = LM_OLD, filename: str = "file.pdf", sha: str = manifest_compute_sha256(b"content"), size: int = 1024) -> dict:
    return {"sha256": sha, "filename": filename, "size": size,
            "downloaded_at": "2026-01-01T00:00:00Z", "last_modified": lm}


def _seed_manifest(course_dir: Path, entries: dict) -> Path:
    course_dir.mkdir(parents=True, exist_ok=True)
    path = course_dir / MANIFEST_FILENAME
    path.write_text(json.dumps(entries))
    return path


def _materialize(course_dir: Path, relative_path: str, content: bytes) -> Path:
    """Create a local topic fixture at the path recorded by the manifest."""
    path = course_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_distinct_topic_ids_cannot_share_a_persisted_path(tmp_path: Path) -> None:
    client = FakeClient(
        tocs={ORG_ID: _toc(
            (101, "Same title", "File", LM_NEW),
            (202, "Same title", "File", LM_NEW),
            module="Module",
        )},
        names={ORG_ID: "Course"},
        files={
            101: (b"FIRST", "same.pdf"),
            202: (b"SECOND", "same.pdf"),
        },
    )

    first = run_course(client, ORG_ID, tmp_path, mode=Mode.SYNC)
    course_dir = tmp_path / f"Course-{ORG_ID}"
    first_path = course_dir / first["downloaded"][0]["path"]
    second_path = course_dir / first["downloaded"][1]["path"]

    assert first_path != second_path
    assert first_path.read_bytes() == b"FIRST"
    assert second_path.read_bytes() == b"SECOND"

    second = run_course(client, ORG_ID, tmp_path, mode=Mode.SYNC)
    assert [item["topic_id"] for item in second["skipped"]] == ["101", "202"]
    assert second["updated"] == []


@pytest.mark.parametrize("name", ["a" * 230 + ".pdf", "é" * 115 + ".pdf"])
def test_topic_filename_fits_atomic_temp_name_limit(name: str) -> None:
    projected = _safe_topic_filename(name, 7)

    assert len(projected.encode("utf-8")) <= 218
    assert projected.endswith(".pdf")


def test_legacy_manifest_collision_cannot_alias_or_overwrite_on_partial_sync(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        tocs={ORG_ID: _toc(
            (101, "Same title", "File", LM_NEW),
            (202, "Same title", "File", LM_NEW),
            module="Module",
        )},
        names={ORG_ID: "Course"},
        files={
            101: (b"A", "same.pdf"),
            202: RuntimeError("second download failed"),
        },
    )
    course_dir = tmp_path / f"Course-{ORG_ID}"
    _seed_manifest(course_dir, {
        "101": _mentry(
            lm=LM_OLD,
            filename="same.pdf",
            sha=manifest_compute_sha256(b"A"),
            size=1,
        ),
        "202": _mentry(
            lm=LM_NEW,
            filename="same.pdf",
            sha=manifest_compute_sha256(b"B"),
            size=1,
        ),
    })
    shared = _materialize(course_dir, "Module/Same title/same.pdf", b"B")

    result = run_course(client, ORG_ID, tmp_path, mode=Mode.SYNC)
    manifest = json.loads((course_dir / MANIFEST_FILENAME).read_text())

    assert shared.read_bytes() == b"B"
    assert manifest["202"]["filename"] == "same.pdf"
    assert manifest["202"]["sha256"] == manifest_compute_sha256(b"B")
    assert manifest["101"]["filename"] != "same.pdf"
    assert [item["topic_id"] for item in result["updated"]] == ["101"]
    assert result["errors"][0]["topic_id"] == "202"


def _tree(root: Path) -> dict[str, bytes]:
    """Snapshot every file under root (path -> bytes)."""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _boom(*_a: object) -> tuple[bytes, str]:
    raise RuntimeError("Network error")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    d = tmp_path / "downloads"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# SYNC decisions
# ---------------------------------------------------------------------------

class TestBuildEntry:
    """Entry metadata keeps manifest hashes usable without rehashing bytes."""

    def test_manifest_hash_is_reused_without_explicit_hash(self):
        entry = build_entry(
            "100", "file.pdf", "Mod/file.pdf",
            {"size": 7, "sha256": "a" * 64},
        )

        assert entry["sha256"] == "a" * 64

    def test_bytes_hash_is_computed_once(self):
        with patch("lighthouse_cli.sync_engine.compute_sha256", wraps=manifest_compute_sha256) as hash_fn:
            entry = build_entry("100", "file.pdf", "Mod/file.pdf", b"content")

        assert entry["sha256"] == manifest_compute_sha256(b"content")
        assert hash_fn.call_count == 1

    @pytest.mark.parametrize("manifest_hash", [None, 12345, ["not-a-hash"]])
    def test_non_string_manifest_hash_is_empty(self, manifest_hash):
        entry = build_entry(
            "100", "file.pdf", "Mod/file.pdf",
            {"size": 7, "sha256": manifest_hash},
        )

        assert entry["sha256"] == ""

    def test_arbitrary_manifest_hash_is_empty(self):
        entry = build_entry(
            "100", "file.pdf", "Mod/file.pdf",
            {"size": 7, "sha256": "not-a-sha256"},
        )

        assert entry["sha256"] == ""

    def test_uppercase_manifest_hash_is_normalized(self):
        entry = build_entry(
            "100", "file.pdf", "Mod/file.pdf",
            {"size": 7, "sha256": "A" * 64},
        )

        assert entry["sha256"] == "a" * 64

    @pytest.mark.parametrize("size", ["seven", float("nan"), 10**1000])
    def test_malformed_manifest_size_is_safe(self, size):
        entry = build_entry(
            "100", "file.pdf", "Mod/file.pdf",
            {"size": size, "sha256": "a" * 64},
        )

        assert entry["size"] == 0
        assert entry["size_kb"] == 0

class TestSyncDecisions:
    """Incremental decisions: skip / update / download / orphan / dedup."""

    def test_unchanged_topic_skipped_without_body_fetch(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "file.pdf")},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {"100": _mentry(size=7)})
        _materialize(course_dir, "Mod/file.pdf", b"content")
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert [e["topic_id"] for e in result["skipped"]] == ["100"]
        assert result["skipped"][0]["sha256"] == manifest_compute_sha256(b"content")
        assert result["downloaded"] == [] and result["updated"] == []
        assert not client.body_calls()
        assert result["saved"] is False, "skip-only run must not rewrite the manifest"

    def test_changed_last_modified_updates_topic(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"new content", "file.pdf")},
        )
        _seed_manifest(root / "Test-44347", {"100": _mentry(lm=LM_OLD)})
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert [e["topic_id"] for e in result["updated"]] == ["100"]
        assert (root / "Test-44347" / "Mod" / "file.pdf").read_bytes() == b"new content"
        assert result["saved"] is True

    def test_new_topic_downloaded(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((300, "new.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={300: (b"new", "new.pdf")},
        )
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert [e["topic_id"] for e in result["downloaded"]] == ["300"]
        assert result["saved"] is True

    @pytest.mark.parametrize("mode", [Mode.DOWNLOAD, Mode.FORCE], ids=["download", "force"])
    def test_duplicate_topic_ids_use_first_occurrence_once(self, root, mode):
        """Duplicate TOC IDs must not fetch, write, or overwrite twice."""
        toc = {"Modules": [
            {
                "ModuleId": 1,
                "Title": "M1",
                "Modules": [],
                "Topics": [{
                    "TopicId": 1,
                    "Title": "first.pdf",
                    "TypeIdentifier": "File",
                    "Url": "",
                    "LastModifiedDate": LM_NEW,
                }],
            },
            {
                "ModuleId": 2,
                "Title": "M2",
                "Modules": [],
                "Topics": [{
                    "TopicId": 1,
                    "Title": "second.pdf",
                    "TypeIdentifier": "File",
                    "Url": "",
                    "LastModifiedDate": LM_NEW,
                }],
            },
        ]}
        client = FakeClient(
            tocs={ORG_ID: toc},
            names={ORG_ID: "Test"},
            files={1: (b"first", "first.pdf")},
        )

        initial = run_course(client, ORG_ID, root, mode=mode)

        assert initial["topic_count"] == 1
        assert [entry["topic_id"] for entry in initial["downloaded"]] == ["1"]
        assert initial["downloaded"][0]["path"] == "M1/first.pdf"
        assert client.body_calls() == [("file", ORG_ID, 1)]
        manifest_path = root / "Test-44347" / MANIFEST_FILENAME
        manifest = json.loads(manifest_path.read_text())
        assert list(manifest) == ["1"]
        assert manifest["1"]["filename"] == "first.pdf"
        assert not (root / "Test-44347" / "M2").exists()

        synced = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert [entry["topic_id"] for entry in synced["skipped"]] == ["1"]
        assert synced["downloaded"] == [] and synced["updated"] == []
        assert synced["orphaned"] == []
        assert client.body_calls() == [("file", ORG_ID, 1)]

    def test_malformed_toc_records_are_isolated_from_valid_siblings(self, root):
        """Malformed modules/topics cannot abort valid sibling downloads."""
        marker = "NESTED_TOPIC_SENTINEL"
        toc = {"Modules": [
            None,
            "not-a-module",
            {"Title": {"nested": marker}, "Modules": [], "Topics": []},
            {
                "Title": "Good",
                "Modules": [],
                "Topics": [
                    None,
                    "not-a-topic",
                    {
                        "TopicId": 2,
                        "Title": {"nested": marker},
                        "TypeIdentifier": "File",
                        "Url": "",
                        "LastModifiedDate": LM_NEW,
                    },
                    {
                        "TopicId": 1,
                        "Title": "good.pdf",
                        "TypeIdentifier": "File",
                        "Url": "",
                        "LastModifiedDate": LM_NEW,
                    },
                ],
            },
        ]}
        client = FakeClient(
            tocs={ORG_ID: toc},
            names={ORG_ID: "Test"},
            files={1: (b"good", "good.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["topic_count"] == 1
        assert [entry["topic_id"] for entry in result["downloaded"]] == ["1"]
        assert client.body_calls() == [("file", ORG_ID, 1)]
        assert result["errors"] and all(
            error["type"] == "topic_data" for error in result["errors"]
        )
        assert marker not in json.dumps(result["errors"])
        manifest = json.loads((root / "Test-44347" / MANIFEST_FILENAME).read_text())
        assert list(manifest) == ["1"]

    def test_deep_toc_walk_preserves_valid_siblings(self, root):
        """A deeply nested module chain cannot exhaust the Python call stack."""
        deep_root = {"Title": "Deep0", "Modules": [], "Topics": []}
        cursor = deep_root
        for depth in range(1, 1101):
            child = {"Title": f"Deep{depth}", "Modules": [], "Topics": []}
            cursor["Modules"] = [child]
            cursor = child
        good_module = {
            "Title": "Good",
            "Modules": [],
            "Topics": [{
                "TopicId": 1,
                "Title": "good.pdf",
                "TypeIdentifier": "File",
                "Url": "",
                "LastModifiedDate": LM_NEW,
            }],
        }
        client = FakeClient(
            tocs={ORG_ID: {"Modules": [deep_root, good_module]}},
            names={ORG_ID: "Test"},
            files={1: (b"good", "good.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["errors"] == []
        assert [entry["topic_id"] for entry in result["downloaded"]] == ["1"]
        assert client.body_calls() == [("file", ORG_ID, 1)]

    def test_cyclic_toc_branch_is_bounded_and_siblings_continue(self, root):
        """A self-referential module is reported without blocking siblings."""
        cycle = {"Title": "Cycle", "Modules": [], "Topics": []}
        cycle["Modules"] = [cycle]
        good_module = {
            "Title": "Good",
            "Modules": [],
            "Topics": [{
                "TopicId": 1,
                "Title": "good.pdf",
                "TypeIdentifier": "File",
                "Url": "",
                "LastModifiedDate": LM_NEW,
            }],
        }
        client = FakeClient(
            tocs={ORG_ID: {"Modules": [cycle, good_module]}},
            names={ORG_ID: "Test"},
            files={1: (b"good", "good.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert [entry["topic_id"] for entry in result["downloaded"]] == ["1"]
        assert client.body_calls() == [("file", ORG_ID, 1)]
        assert any(
            error["type"] == "topic_data"
            and error["error"] == "Content TOC exceeded safe traversal limits."
            for error in result["errors"]
        )
        assert "Cycle" not in json.dumps(result["errors"])

    @pytest.mark.parametrize(
        "invalid_id",
        [True, 0, -1, 1.5, "123", "../topic-id\x1b"],
        ids=["bool", "zero", "negative", "float", "numeric-string", "path-control"],
    )
    def test_invalid_topic_ids_are_rejected_and_valid_siblings_continue(self, root, invalid_id):
        """Malformed TOC IDs cannot trigger a request or local write."""
        client = FakeClient(
            tocs={ORG_ID: _toc(
                (invalid_id, "bad.pdf", "File", LM_NEW),
                (100, "good.pdf", "File", LM_NEW),
            )},
            names={ORG_ID: "Test"},
            files={100: (b"good", "good.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["topic_count"] == 1
        assert [entry["topic_id"] for entry in result["downloaded"]] == ["100"]
        assert [call for call in client.calls if call[0] == "file"] == [
            ("file", ORG_ID, 100),
        ]
        assert [error["type"] for error in result["errors"]] == ["topic_data"]
        assert all(str(invalid_id) not in json.dumps(error) for error in result["errors"])
        manifest = json.loads((root / "Test-44347" / MANIFEST_FILENAME).read_text())
        assert list(manifest) == ["100"]

    @pytest.mark.parametrize(
        "invalid_last_modified",
        [{"unexpected": "object"}, ["unexpected"], "bad\nvalue", "x" * 257],
        ids=["dict", "list", "control", "overlong"],
    )
    def test_invalid_last_modified_is_rejected_without_partial_topic_write(
        self, root, invalid_last_modified,
    ):
        """Malformed TOC metadata cannot fetch or mutate a topic entry."""
        client = FakeClient(
            tocs={ORG_ID: _toc(
                (200, "bad.pdf", "File", invalid_last_modified),
                (100, "good.pdf", "File", LM_NEW),
            )},
            names={ORG_ID: "Test"},
            files={100: (b"good", "good.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["topic_count"] == 1
        assert [entry["topic_id"] for entry in result["downloaded"]] == ["100"]
        assert [call for call in client.calls if call[0] == "file"] == [
            ("file", ORG_ID, 100),
        ]
        assert [error["type"] for error in result["errors"]] == ["topic_data"]
        assert all(str(invalid_last_modified) not in json.dumps(error) for error in result["errors"])
        manifest = json.loads((root / "Test-44347" / MANIFEST_FILENAME).read_text())
        assert list(manifest) == ["100"]
        assert not (root / "Test-44347" / "Mod" / "bad.pdf").exists()

    def test_missing_last_modified_normalizes_to_empty(self, root):
        """Older or incomplete TOCs may omit LastModifiedDate."""
        toc = _toc((100, "good.pdf", "File", LM_NEW))
        del toc["Modules"][0]["Topics"][0]["LastModifiedDate"]
        client = FakeClient(
            tocs={ORG_ID: toc},
            names={ORG_ID: "Test"},
            files={100: (b"good", "good.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["errors"] == []
        manifest = json.loads((root / "Test-44347" / MANIFEST_FILENAME).read_text())
        assert manifest["100"]["last_modified"] == ""

    @pytest.mark.parametrize(
        "invalid_content",
        ["text body", bytearray(b"bytearray body"), object()],
        ids=["string", "bytearray", "object"],
    )
    def test_non_bytes_topic_body_is_rejected_without_partial_write(self, root, invalid_content):
        """Only byte bodies may reach filesystem writes or manifest hashing."""
        client = FakeClient(
            tocs={ORG_ID: _toc(
                (200, "bad.pdf", "File", LM_NEW),
                (100, "good.pdf", "File", LM_NEW),
            )},
            names={ORG_ID: "Test"},
            files={
                200: (invalid_content, "bad.pdf"),
                100: (b"good", "good.pdf"),
            },
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert [entry["topic_id"] for entry in result["downloaded"]] == ["100"]
        assert client.body_calls() == [
            ("file", ORG_ID, 200),
            ("file", ORG_ID, 100),
        ]
        assert [error["type"] for error in result["errors"]] == ["topic_data"]
        assert result["errors"][0]["error"] == "Topic record has an invalid identifier."
        manifest = json.loads((root / "Test-44347" / MANIFEST_FILENAME).read_text())
        assert list(manifest) == ["100"]
        assert not (root / "Test-44347" / "Mod" / "bad.pdf").exists()

    def test_secret_topic_labels_use_safe_filenames_and_projections(self, root):
        """TOC and server filenames containing secret-shaped labels stay hidden."""
        client = FakeClient(
            tocs={ORG_ID: _toc((200, "password=TOPIC_TITLE_SENTINEL", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={200: (b"body", "token=FILENAME_SECRET_SENTINEL\x1b")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["downloaded"][0]["filename"] == "topic_200"
        assert result["downloaded"][0]["path"] == "Mod/topic_200"
        assert client.body_calls() == [("file", ORG_ID, 200)]
        rendered = json.dumps(result, default=str)
        assert "TOPIC_TITLE_SENTINEL" not in rendered
        assert "FILENAME_SECRET_SENTINEL" not in rendered
        course_dir = root / "Test-44347"
        assert (course_dir / "Mod" / "topic_200").read_bytes() == b"body"
        assert not (course_dir / "Mod" / "token=FILENAME_SECRET_SENTINEL\x1b").exists()

    @pytest.mark.parametrize(
        "bad_name",
        [
            None,
            {"nested": "COURSE_NAME_SECRET_SENTINEL"},
            ["nested"],
            "Course\nNAME_CONTROL_SENTINEL",
            "x" * 257,
            "password=COURSE_NAME_SECRET_SENTINEL",
        ],
        ids=["none", "dict", "list", "control", "overlong", "secret-shaped"],
    )
    def test_malformed_course_name_uses_fixed_fallback(self, root, bad_name):
        """Untrusted enrollment names cannot reach paths or result output."""
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "good.pdf", "File", LM_NEW))},
            names={ORG_ID: bad_name},
            files={100: (b"good", "good.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["course_name"] == "Course-44347"
        assert result["dest"] == root / "Course-44347-44347"
        assert result["downloaded"]
        rendered = json.dumps(result, default=str)
        assert "COURSE_NAME_SECRET_SENTINEL" not in rendered
        assert "NAME_CONTROL_SENTINEL" not in rendered

    def test_download_hashes_body_once_and_reuses_manifest_entry(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "file.pdf")},
        )
        with patch("lighthouse_cli.manifest.compute_sha256", wraps=manifest_compute_sha256) as manifest_hash, \
             patch("lighthouse_cli.sync_engine.compute_sha256", wraps=manifest_compute_sha256) as engine_hash:
            result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert manifest_hash.call_count == 1
        assert engine_hash.call_count == 0
        assert result["downloaded"][0]["sha256"] == manifest_compute_sha256(b"content")

    def test_matching_timestamp_redownloads_when_local_file_is_missing(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "file.pdf")},
        )
        _seed_manifest(root / "Test-44347", {"100": _mentry(size=7)})

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert [e["topic_id"] for e in result["updated"]] == ["100"]
        assert client.body_calls() == [("file", ORG_ID, 100)]
        assert (root / "Test-44347" / "Mod" / "file.pdf").read_bytes() == b"content"

    def test_matching_timestamp_redownloads_when_local_file_size_differs(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "file.pdf")},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {"100": _mentry(size=7)})
        _materialize(course_dir, "Mod/file.pdf", b"truncated")

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert [e["topic_id"] for e in result["updated"]] == ["100"]
        assert (course_dir / "Mod" / "file.pdf").read_bytes() == b"content"

    def test_matching_timestamp_redownloads_same_size_changed_bytes(self, root):
        old_content = b"old!"
        new_content = b"new!"
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
            files={100: (new_content, "file.pdf")},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {
            "100": _mentry(
                size=len(old_content),
                sha=manifest_compute_sha256(old_content),
            ),
        })
        _materialize(course_dir, "Mod/file.pdf", new_content)

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert [e["topic_id"] for e in result["updated"]] == ["100"]
        assert (course_dir / "Mod" / "file.pdf").read_bytes() == new_content

    def test_matching_timestamp_redownloads_when_local_file_is_symlink(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "file.pdf")},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {"100": _mentry(size=7)})
        local_file = _materialize(course_dir, "Mod/file.pdf", b"content")
        symlink_target = root / "outside.pdf"
        symlink_target.write_bytes(b"content")
        local_file.unlink()
        local_file.symlink_to(symlink_target)

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["updated"] == []
        assert result["errors"] and "symlink" in result["errors"][0]["error"]
        assert local_file.is_symlink()
        assert symlink_target.read_bytes() == b"content"

    def test_topic_write_uses_atomic_0600(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "file.pdf")},
        )
        with patch("lighthouse_cli.sync_engine.atomic_write", wraps=shared_atomic_write) as atomic:
            result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["downloaded"]
        assert atomic.call_args.kwargs["mode"] == 0o600
        filepath = root / "Test-44347" / "Mod" / "file.pdf"
        permissions = stat.S_IMODE(filepath.stat().st_mode)
        assert permissions & 0o077 == 0

    def test_orphaned_reported_not_deleted(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file100.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
            files={100: (b"c", "file100.pdf")},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {"100": _mentry(filename="file100.pdf"), "200": _mentry(filename="file200.pdf")})
        orphan_file = course_dir / "Mod" / "file200.pdf"
        orphan_file.parent.mkdir(parents=True, exist_ok=True)
        orphan_file.write_bytes(b"old")
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert [e["topic_id"] for e in result["orphaned"]] == ["200"]
        assert result["orphaned"][0]["sha256"] == manifest_compute_sha256(b"content")
        assert orphan_file.exists(), "orphaned file must not be deleted"

    def test_orphaned_output_is_sorted_by_manifest_key(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "live.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {
            "20": _mentry(filename="twenty.pdf"),
            "10": _mentry(filename="ten.pdf"),
            "100": _mentry(filename="live.pdf", size=4),
        })
        _materialize(course_dir, "Mod/live.pdf", b"live")

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert [e["topic_id"] for e in result["orphaned"]] == ["10", "20"]

    def test_unselected_live_topics_are_not_reported_as_orphaned(self, root):
        """The type filter controls downloads, not TOC liveness."""
        file_content = b"file"
        html_content = b"<p>html</p>"
        client = FakeClient(
            tocs={ORG_ID: _toc(
                (100, "file.pdf", "File", LM_OLD),
                (200, "page.html", "HTML", LM_OLD),
            )},
            names={ORG_ID: "Test"},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {
            "100": _mentry(
                filename="file.pdf",
                sha=manifest_compute_sha256(file_content),
                size=len(file_content),
            ),
            "200": _mentry(
                filename="page.html",
                sha=manifest_compute_sha256(html_content),
                size=len(html_content),
            ),
        })
        _materialize(course_dir, "Mod/page.html", html_content)

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC, types="html")

        assert [entry["topic_id"] for entry in result["skipped"]] == ["200"]
        assert result["orphaned"] == []
        assert client.body_calls() == []

    def test_empty_toc_reconciles_stale_manifest_entries(self, root):
        """An empty live TOC still lets SYNC report stale manifest entries."""
        client = FakeClient(
            tocs={ORG_ID: {"Modules": []}},
            names={ORG_ID: "Test"},
        )
        course_dir = root / "Test-44347"
        manifest_path = _seed_manifest(course_dir, {
            "200": _mentry(filename="gone.pdf"),
            "assignment_7_8": _mentry(filename="hw.pdf"),
        })

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["empty"] is True
        assert [entry["topic_id"] for entry in result["orphaned"]] == [
            "200", "assignment_7_8",
        ]
        assert result["manifest_total"] == 2
        assert json.loads(manifest_path.read_text())
        assert client.body_calls() == []

    def test_corrupt_manifest_warns_records_and_recovers(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )
        _seed_manifest(root / "Test-44347", {})
        (root / "Test-44347" / MANIFEST_FILENAME).write_text("not valid json{")
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert result["warnings"] == ["Corrupt manifest; performing full sync."]
        assert any(e.get("type") == "manifest_corrupt" for e in result["errors"])
        assert [e["topic_id"] for e in result["downloaded"]] == ["100"]
        assert result["saved"] is True
        assert "100" in json.loads((root / "Test-44347" / MANIFEST_FILENAME).read_text())

    def test_identical_content_flagged_as_duplicates(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((1, "a.pdf", "File", LM_NEW), (2, "b.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={1: (b"same", "a.pdf"), 2: (b"same", "b.pdf")},
        )
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert len(result["duplicates"]) == 2
        assert result["duplicates"][0]["sha256"] == result["duplicates"][1]["sha256"]

    def test_engine_never_prints(self, root, capsys):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )
        run_course(client, ORG_ID, root, mode=Mode.SYNC)
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""


# ---------------------------------------------------------------------------
# DOWNLOAD / FORCE modes
# ---------------------------------------------------------------------------

class TestDownloadModes:
    """DOWNLOAD preserves unrelated manifest entries; FORCE wipes them."""

    def test_download_redownloads_and_preserves_unrelated_entries(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )
        _seed_manifest(root / "Test-44347", {
            "100": _mentry(),                       # same last_modified — still re-downloaded
            "assignment_9_8": _mentry(filename="hw.pdf"),  # unrelated entry survives
        })
        result = run_course(client, ORG_ID, root, mode=Mode.DOWNLOAD)
        assert [e["topic_id"] for e in result["downloaded"]] == ["100"]
        assert result["skipped"] == [] and result["updated"] == []
        on_disk = json.loads((root / "Test-44347" / MANIFEST_FILENAME).read_text())
        assert "100" in on_disk and "assignment_9_8" in on_disk

    def test_force_wipes_entire_manifest_including_unrelated(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )
        _seed_manifest(root / "Test-44347", {
            "999": _mentry(),
            "assignment_9_8": _mentry(filename="hw.pdf"),
        })
        result = run_course(client, ORG_ID, root, mode=Mode.FORCE)
        assert [e["topic_id"] for e in result["downloaded"]] == ["100"]
        on_disk = json.loads((root / "Test-44347" / MANIFEST_FILENAME).read_text())
        assert list(on_disk) == ["100"], "FORCE must leave only freshly downloaded entries"

    def test_repeated_force_reuses_each_topics_reserved_path(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )
        course_dir = root / "Test-44347"
        existing = _materialize(course_dir, "Mod/f.pdf", b"old")

        first = run_course(client, ORG_ID, root, mode=Mode.FORCE)
        second = run_course(client, ORG_ID, root, mode=Mode.FORCE)

        assert first["downloaded"][0]["path"] == "Mod/f.pdf"
        assert second["downloaded"][0]["path"] == "Mod/f.pdf"
        assert existing.read_bytes() == b"content"
        assert sorted(path.name for path in existing.parent.iterdir()) == ["f.pdf"]

    def test_force_reordered_collisions_keep_each_topics_existing_path(self, root):
        first_client = FakeClient(
            tocs={ORG_ID: _toc(
                (101, "Same", "File", LM_NEW),
                (202, "Same", "File", LM_NEW),
            )},
            names={ORG_ID: "Test"},
            files={101: (b"FIRST", "same.pdf"), 202: (b"SECOND", "same.pdf")},
        )
        first = run_course(first_client, ORG_ID, root, mode=Mode.FORCE)
        first_paths = {item["topic_id"]: item["path"] for item in first["downloaded"]}

        reordered_client = FakeClient(
            tocs={ORG_ID: _toc(
                (202, "Same", "File", LM_NEW),
                (101, "Same", "File", LM_NEW),
            )},
            names={ORG_ID: "Test"},
            files={101: (b"FIRST", "same.pdf"), 202: (b"SECOND", "same.pdf")},
        )
        second = run_course(reordered_client, ORG_ID, root, mode=Mode.FORCE)
        second_paths = {item["topic_id"]: item["path"] for item in second["downloaded"]}

        assert second_paths == first_paths
        module_dir = root / "Test-44347" / "Mod"
        assert sorted(path.name for path in module_dir.iterdir()) == [
            "same--topic-202.pdf",
            "same.pdf",
        ]
        course_dir = root / "Test-44347"
        assert (course_dir / first_paths["101"]).read_bytes() == b"FIRST"
        assert (course_dir / first_paths["202"]).read_bytes() == b"SECOND"

    @pytest.mark.parametrize("topic_order", [(101, 202), (202, 101)])
    def test_force_repairs_legacy_contested_path_without_stale_copy(
        self,
        root,
        topic_order,
    ):
        topics = {
            101: (101, "Same", "File", LM_NEW),
            202: (202, "Same", "File", LM_NEW),
        }
        client = FakeClient(
            tocs={ORG_ID: _toc(*(topics[topic_id] for topic_id in topic_order))},
            names={ORG_ID: "Test"},
            files={101: (b"FIRST", "same.pdf"), 202: (b"SECOND", "same.pdf")},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {
            "101": _mentry(filename="same.pdf"),
            "202": _mentry(filename="same.pdf"),
        })
        _materialize(course_dir, "Mod/same.pdf", b"legacy")

        result = run_course(client, ORG_ID, root, mode=Mode.FORCE)

        paths = {item["topic_id"]: item["path"] for item in result["downloaded"]}
        assert paths == {
            "101": "Mod/same.pdf",
            "202": "Mod/same--topic-202.pdf",
        }
        module_dir = course_dir / "Mod"
        assert sorted(path.name for path in module_dir.iterdir()) == [
            "same--topic-202.pdf",
            "same.pdf",
        ]
        assert (module_dir / "same.pdf").read_bytes() == b"FIRST"
        assert (module_dir / "same--topic-202.pdf").read_bytes() == b"SECOND"

    def test_force_empty_selection_replaces_existing_manifest(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((101, "file.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
        )
        manifest_path = _seed_manifest(
            root / "Test-44347",
            {"101": _mentry(filename="file.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.FORCE, types="html")

        assert result["empty"] is True
        assert result["saved"] is True
        assert json.loads(manifest_path.read_text(encoding="utf-8")) == {}

    def test_force_all_download_failures_preserve_prior_manifest(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((101, "file.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={101: RuntimeError("download failed")},
        )
        manifest_path = _seed_manifest(
            root / "Test-44347",
            {"101": _mentry(filename="file.pdf")},
        )
        before = manifest_path.read_bytes()

        result = run_course(client, ORG_ID, root, mode=Mode.FORCE)

        assert result["errors"]
        assert result["saved"] is False
        assert manifest_path.read_bytes() == before

    def test_force_malformed_empty_toc_preserves_prior_manifest(self, root):
        client = FakeClient(
            tocs={ORG_ID: {"Modules": [{
                "ModuleId": 1,
                "Title": "Mod",
                "Modules": [],
                "Topics": [{
                    "TopicId": "invalid",
                    "Title": "file.pdf",
                    "TypeIdentifier": "File",
                    "Url": "",
                    "LastModifiedDate": LM_NEW,
                }],
            }]}},
            names={ORG_ID: "Test"},
        )
        manifest_path = _seed_manifest(
            root / "Test-44347",
            {"101": _mentry(filename="file.pdf")},
        )
        before = manifest_path.read_bytes()

        result = run_course(client, ORG_ID, root, mode=Mode.FORCE)

        assert result["errors"]
        assert result["saved"] is False
        assert manifest_path.read_bytes() == before


# ---------------------------------------------------------------------------
# PLAN mode (--dry-run): zero writes, zero body fetches
# ---------------------------------------------------------------------------

class TestPlanNonMutation:
    """PLAN walks the TOC and decides — touches neither disk nor bodies."""

    def test_plan_lists_topics_without_fs_or_body_fetches(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "a.pdf", "File", LM_NEW), (200, "b.html", "HTML", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"x", "a.pdf")},
            html={200: (b"<html></html>", "b.html")},
        )
        _seed_manifest(root / "Test-44347", {"100": _mentry()})
        before = _tree(root)

        result = run_course(client, ORG_ID, root, mode=Mode.PLAN, types="file,html")

        assert result["planned"] == [
            {"topic_id": 100, "title": "a.pdf", "path": "Mod/a.pdf"},
            {"topic_id": 200, "title": "b.html", "path": "Mod/b.html"},
        ]
        assert client.body_calls() == [], "PLAN must not fetch any file body"
        assert _tree(root) == before, "PLAN must not write anything"
        assert result["saved"] is False and result["errors"] == []

    def test_force_plus_plan_does_not_delete_manifest(self, root):
        """Approved fix: --force --dry-run must NOT wipe the manifest."""
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"x", "f.pdf")},
        )
        manifest_path = _seed_manifest(root / "Test-44347", {"100": _mentry(), "999": _mentry()})
        before = manifest_path.read_bytes()

        result = run_course(client, ORG_ID, root, mode=Mode.PLAN)

        assert manifest_path.exists() and manifest_path.read_bytes() == before
        assert result["planned"] and result["saved"] is False

    def test_plan_with_assignments_makes_no_dropbox_calls(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"x", "f.pdf")},
        )
        run_course(client, ORG_ID, root, mode=Mode.PLAN, include_assignments=True)
        assert not any(c[0].startswith(("folders", "folder_detail", "attachment")) for c in client.calls)


# ---------------------------------------------------------------------------
# Assignment contract: same Manifest instance, one save, live-key reconciliation
# ---------------------------------------------------------------------------

class TestAssignmentContract:
    """Topics and attachments share one Manifest; the pipeline saves once."""

    def _client_with_assignments(self) -> FakeClient:
        return FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
            folders={ORG_ID: [{"Id": 7, "Name": "HW1", "Attachments": [{"Id": 8, "FileName": "hw.pdf", "Size": 10, "Type": "File"}]}]},
            details={(ORG_ID, 7): {"Id": 7, "Name": "HW1", "Attachments": [{"Id": 8, "FileName": "hw.pdf", "Size": 10, "Type": "File"}]}},
            attachments={(ORG_ID, 8): (b"hw bytes", "hw.pdf")},
        )

    @pytest.mark.parametrize(
        ("assignment_id", "expected_downloads"),
        [(7, 1), (0, 0), (-1, 0)],
    )
    def test_assignment_selector_stays_scoped_when_engine_called_directly(
        self, root, assignment_id, expected_downloads
    ):
        client = FakeClient(
            tocs={ORG_ID: {"Modules": []}},
            names={ORG_ID: "Test"},
            folders={
                ORG_ID: [{
                    "Id": 7,
                    "Name": "HW1",
                    "Attachments": [{
                        "Id": 8,
                        "FileName": "hw.pdf",
                        "Size": 10,
                        "Type": "File",
                    }],
                }],
            },
            attachments={(ORG_ID, 8): (b"hw bytes", "hw.pdf")},
        )

        result = run_course(
            client,
            ORG_ID,
            root,
            mode=Mode.DOWNLOAD,
            include_assignments=True,
            assignment_id=assignment_id,
        )

        assert len(result["assignments"]["downloaded"]) == expected_downloads
        attachment_calls = [call for call in client.calls if call[0] == "attachment"]
        assert len(attachment_calls) == expected_downloads

    def test_missing_assignment_selector_does_not_create_course_directory(self, root):
        client = FakeClient(
            tocs={ORG_ID: {"Modules": []}},
            names={ORG_ID: "Test"},
            folders={ORG_ID: [{
                "Id": 7,
                "Name": "HW1",
                "Attachments": [{"Id": 8, "FileName": "hw.pdf", "Size": 10, "Type": "File"}],
            }]},
        )

        result = run_course(
            client,
            ORG_ID,
            root,
            mode=Mode.DOWNLOAD,
            include_assignments=True,
            assignment_id=999,
        )

        assert result["assignments"]["downloaded"] == []
        assert result["assignments"]["errors"] == [{
            "error": "Requested assignment folder was not found.",
            "type": "assignment_not_found",
        }]
        assert not (root / "Test-44347").exists()
        assert client.body_calls() == []
        assert not any(call[0] == "attachment" for call in client.calls)

    def test_missing_assignment_selector_preflights_before_nonempty_toc(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "lecture.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"lecture", "lecture.pdf")},
            folders={ORG_ID: [{
                "Id": 101,
                "Name": "HW1",
                "Attachments": [{"Id": 1, "FileName": "hw.pdf", "Size": 4, "Type": "File"}],
            }]},
        )

        result = run_course(
            client,
            ORG_ID,
            root,
            mode=Mode.DOWNLOAD,
            include_assignments=True,
            assignment_id=999,
        )

        assert result["assignments"]["errors"] == [{
            "error": "Requested assignment folder was not found.",
            "type": "assignment_not_found",
        }]
        assert client.calls == [("folders", ORG_ID)]
        assert not (root / "Test-44347").exists()

    def test_malformed_assignment_snapshot_preflights_before_toc(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "lecture.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"lecture", "lecture.pdf")},
        )

        result = run_course(
            client,
            ORG_ID,
            root,
            mode=Mode.DOWNLOAD,
            include_assignments=True,
            assignment_id=101,
            assignment_folders={"malformed": True},  # type: ignore[arg-type]
        )

        assert result["assignments"]["errors"] == [{
            "error": "Assignment folders have an invalid response shape.",
            "type": "assignment_list",
        }]
        assert client.calls == []
        assert not (root / "Test-44347").exists()

    def test_topics_and_attachments_share_one_manifest_saved_once(self, root, monkeypatch):
        client = self._client_with_assignments()
        saves: list[Path] = []
        from lighthouse_cli.sync_engine import Manifest as EngineManifest
        original_save = EngineManifest.save
        monkeypatch.setattr(EngineManifest, "save", lambda self, path: saves.append(path) or original_save(self, path))

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC, include_assignments=True)

        assert len(saves) == 1, "topics + attachments must produce exactly one manifest save"
        on_disk = json.loads(saves[0].read_text())
        assert "100" in on_disk and "assignment_7_8" in on_disk
        assert result["assignments"]["downloaded"][0]["file_id"] == 8

    def test_skip_only_legacy_attachment_path_migration_is_persisted(self, root):
        content = b"OLD"
        folder = {
            "Id": 7,
            "Name": "HW1",
            "Attachments": [
                {"Id": 8, "FileName": "hw.pdf", "Size": len(content), "Type": "File"},
            ],
        }
        client = FakeClient(
            tocs={ORG_ID: {"Modules": []}},
            names={ORG_ID: "Test"},
            folders={ORG_ID: [folder]},
            details={(ORG_ID, 7): folder},
        )
        course_dir = root / "Test-44347"
        manifest_path = _seed_manifest(course_dir, {
            "assignment_7_8": {
                **_mentry(filename="hw.pdf", sha=manifest_compute_sha256(content), size=len(content)),
            },
        })
        _materialize(course_dir, "Assignments/HW1/hw.pdf", content)

        result = run_course(
            client,
            ORG_ID,
            root,
            mode=Mode.SYNC,
            include_assignments=True,
        )

        assert result["assignments"]["skipped"][0]["path"] == "Assignments/HW1/hw.pdf"
        assert result["saved"] is True
        persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert persisted["assignment_7_8"]["path"] == "Assignments/HW1/hw.pdf"

    def test_topics_cannot_claim_reserved_assignments_subtree(self, root):
        toc = {
            "Modules": [{
                "ModuleId": 1,
                "Title": "Assignments",
                "Topics": [],
                "Modules": [{
                    "ModuleId": 2,
                    "Title": "HW1",
                    "Modules": [],
                    "Topics": [{
                        "TopicId": 500,
                        "Title": "hw.pdf",
                        "TypeIdentifier": "File",
                        "Url": "",
                        "LastModifiedDate": LM_NEW,
                    }],
                }],
            }],
        }
        client = FakeClient(
            tocs={ORG_ID: toc},
            names={ORG_ID: "Test"},
            files={500: (b"TOPIC", "hw.pdf")},
            folders={ORG_ID: [{
                "Id": 7,
                "Name": "HW1",
                "Attachments": [
                    {"Id": 8, "FileName": "hw.pdf", "Size": 4, "Type": "File"},
                ],
            }]},
            details={(ORG_ID, 7): {
                "Id": 7,
                "Name": "HW1",
                "Attachments": [
                    {"Id": 8, "FileName": "hw.pdf", "Size": 4, "Type": "File"},
                ],
            }},
            attachments={(ORG_ID, 8): (b"ATT!", "hw.pdf")},
        )

        result = run_course(
            client,
            ORG_ID,
            root,
            mode=Mode.SYNC,
            include_assignments=True,
        )

        course_dir = root / "Test-44347"
        topic_path = course_dir / result["downloaded"][0]["path"]
        assignment_path = course_dir / result["assignments"]["downloaded"][0]["path"]
        assert topic_path != assignment_path
        assert topic_path.read_bytes() == b"TOPIC"
        assert assignment_path.read_bytes() == b"ATT!"
        assert topic_path.relative_to(course_dir).parts[0] == "_Content"

    def test_repeated_force_reuses_assignment_path(self, root):
        client = self._client_with_assignments()

        first = run_course(
            client,
            ORG_ID,
            root,
            mode=Mode.FORCE,
            include_assignments=True,
        )
        second = run_course(
            client,
            ORG_ID,
            root,
            mode=Mode.FORCE,
            include_assignments=True,
        )

        assert first["assignments"]["downloaded"][0]["path"] == (
            second["assignments"]["downloaded"][0]["path"]
        )
        attachment_calls = [call for call in client.calls if call[0] == "attachment"]
        assert len(attachment_calls) == 2
        folder = root / "Test-44347" / "Assignments" / "HW1"
        assert sorted(path.name for path in folder.iterdir()) == ["hw.pdf"]
        manifest = Manifest.load(root / "Test-44347" / MANIFEST_FILENAME)
        assert manifest.get("assignment_7_8") is not None

    @pytest.mark.parametrize("mode", [Mode.DOWNLOAD, Mode.FORCE, Mode.SYNC])
    def test_download_repairs_legacy_contested_assignment_path(self, root, mode):
        client = FakeClient(
            tocs={ORG_ID: {"Modules": []}},
            names={ORG_ID: "Test"},
            folders={ORG_ID: [{
                "Id": 7,
                "Name": "HW1",
                "Attachments": [
                    {"Id": 8, "FileName": "shared.pdf", "Size": 3, "Type": "File"},
                    {"Id": 9, "FileName": "shared.pdf", "Size": 3, "Type": "File"},
                ],
            }]},
            details={(ORG_ID, 7): {
                "Id": 7,
                "Name": "HW1",
                "Attachments": [
                    {"Id": 8, "FileName": "shared.pdf", "Size": 3, "Type": "File"},
                    {"Id": 9, "FileName": "shared.pdf", "Size": 3, "Type": "File"},
                ],
            }},
            attachments={
                (ORG_ID, 8): (b"ONE", "shared.pdf"),
                (ORG_ID, 9): (b"TWO", "shared.pdf"),
            },
        )
        course_dir = root / "Test-44347"
        contested = "Assignments/HW1/shared.pdf"
        _seed_manifest(course_dir, {
            "assignment_7_8": {
                **_mentry(filename="shared.pdf", size=3),
                "path": contested,
            },
            "assignment_7_9": {
                **_mentry(filename="shared.pdf", size=3),
                "path": contested,
            },
        })
        _materialize(course_dir, contested, b"OLD")

        result = run_course(
            client,
            ORG_ID,
            root,
            mode=mode,
            include_assignments=True,
        )

        result_key = "updated" if mode is Mode.SYNC else "downloaded"
        paths = [item["path"] for item in result["assignments"][result_key]]
        assert paths == [
            "Assignments/HW1/shared.pdf",
            "Assignments/HW1/shared_1.pdf",
        ]
        assert (course_dir / paths[0]).read_bytes() == b"ONE"
        assert (course_dir / paths[1]).read_bytes() == b"TWO"


    @pytest.mark.parametrize(
        "filename",
        [
            "d2lSameSiteCanaryA%3DTOPIC_SECRET",
            "ctx%3DTOPIC_SECRET",
            "sFT%3DTOPIC_SECRET",
        ],
    )
    def test_topic_filename_revalidates_secret_shape_after_url_decode(
        self,
        filename: str,
    ) -> None:
        assert _safe_topic_filename(filename, 7) == "topic_7"

    def test_live_assignment_keys_excluded_from_orphaned(self, root):
        client = self._client_with_assignments()
        _seed_manifest(root / "Test-44347", {
            "200": _mentry(filename="gone.pdf"),                   # genuinely orphaned topic
            "assignment_7_8": _mentry(filename="hw.pdf", size=5),  # stale size → updated this run
        })
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC, include_assignments=True)

        orphan_ids = [e["topic_id"] for e in result["orphaned"]]
        assert orphan_ids == ["200"], f"live assignment key must not be orphaned: {orphan_ids}"
        assert any(e["file_id"] == 8 for e in result["assignments"]["updated"])

    def test_stale_assignment_keys_stay_orphaned_without_assignment_phase(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )
        _seed_manifest(root / "Test-44347", {"assignment_7_8": _mentry(filename="hw.pdf")})
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert [e["topic_id"] for e in result["orphaned"]] == ["assignment_7_8"]


# ---------------------------------------------------------------------------
# Exit-code matrix (single|multi × human|json) via CliRunner
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _scoped_client(toc=_std_toc, download=None):
    """Patch LighthouseClient class methods for CliRunner runs."""
    with patch.object(LighthouseClient, "get_courses", return_value=[
        {"OrgUnitId": 111, "Name": "Course A", "Code": "S1"},
        {"OrgUnitId": 222, "Name": "Course B", "Code": "S1"},
    ]), patch.object(LighthouseClient, "get_content_toc", side_effect=toc), \
         patch.object(LighthouseClient, "download_topic_file",
                      side_effect=download or (lambda cid, tid: (b"c", "f.pdf"))), \
         patch.object(LighthouseClient, "get_semesters", return_value=[
             {"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"}]), \
         patch.object(LighthouseClient, "get_course_enrollments", return_value=[
             {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "S1"}},
             {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "S1"}}]):
        yield


class TestExitMatrix:
    """Uniform matrix: topic/assignment/corruption failures → 1; scope leniency and empty → 0."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    def _multi_config(self, tmp_path: Path) -> Path:
        cfg = tmp_path / "course-config.json"
        cfg.write_text(json.dumps({"tracked_courses": {
            "111": {"name": "Course A", "semester": "Sem I"},
            "222": {"name": "Course B", "semester": "Sem I"}}}))
        return cfg

    def test_single_human_topic_error_exit_1(self, runner, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        with _scoped_client(download=_boom):
            result = runner.invoke(cli, ["download", "111", "-o", str(output_dir)])
        assert result.exit_code == 1, result.output
        assert "FAILED topic" in result.output

    def test_single_json_topic_error_exit_1(self, runner, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        with _scoped_client(download=_boom):
            result = runner.invoke(cli, ["download", "111", "-o", str(output_dir), "--json"])
        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout)["errors"][0]["error"].startswith("Network error")

    def test_multi_human_topic_error_exit_1(self, runner, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        cfg = self._multi_config(tmp_path)
        def download(cid, tid):
            return _boom() if cid == 222 else (b"c", "f.pdf")
        with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg), _scoped_client(download=download):
            result = runner.invoke(cli, ["download", "--semester", "100", "-o", str(output_dir)])
        assert result.exit_code == 1, result.output
        assert "FAILED topic" in result.output

    def test_multi_json_topic_error_exit_1(self, runner, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        cfg = self._multi_config(tmp_path)
        def download(cid, tid):
            return _boom() if cid == 222 else (b"c", "f.pdf")
        with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg), _scoped_client(download=download):
            result = runner.invoke(cli, ["download", "--semester", "100", "-o", str(output_dir), "--json"])
        assert result.exit_code == 1, result.output
        data = json.loads(result.stdout)
        failed = next(c for c in data["courses"] if c["course_id"] == 222)
        assert "Network error" in failed["errors"][0]["error"]

    def test_multi_json_preserves_malformed_toc_error_type_without_extra_fields(
        self, runner, tmp_path,
    ):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        cfg = self._multi_config(tmp_path)
        sentinel = "MALFORMED_TOC_PRIVATE_SENTINEL"

        def toc(cid):
            if cid == 111:
                return {"Modules": [{
                    "ModuleId": 1,
                    "Title": sentinel,
                    "Modules": [],
                    "Topics": [{
                        "TopicId": True,
                        "Title": "bad.pdf",
                        "TypeIdentifier": "File",
                        "Url": "https://example.invalid/private",
                        "LastModifiedDate": LM_NEW,
                        "extra": sentinel,
                    }],
                }]}
            return _std_toc(cid)

        with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg), \
             _scoped_client(toc=toc):
            result = runner.invoke(cli, [
                "download", "--semester", "100", "-o", str(output_dir), "--json",
            ])

        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        malformed = next(course for course in payload["courses"] if course["course_id"] == 111)
        assert malformed["errors"] == [{
            "type": "topic_data",
            "error": "Command failed.",
        }]
        assert sentinel not in result.stdout + result.stderr
        assert "extra" not in result.stdout

    def test_assignment_error_exits_1(self, runner, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        folders = [{"Id": 7, "Name": "HW1", "Attachments": [{"Id": 8, "FileName": "hw.pdf", "Size": 10, "Type": "File"}]}]
        with _scoped_client(), \
             patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "download_attachment", side_effect=RuntimeError("att fail")):
            result = runner.invoke(cli, ["download", "111", "--include-assignments", "-o", str(output_dir), "--json"])
        assert result.exit_code == 1, result.output
        data = json.loads(result.stdout)  # stderr carries the FAILED attachment line
        assert data["assignment_errors"][0]["error"] == "att fail"

    @pytest.mark.parametrize("json_output", [True, False])
    def test_missing_assignment_selector_is_typed_actionable_and_does_not_write(
        self, runner, tmp_path, json_output,
    ):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        folders = [{
            "Id": 101,
            "Name": "HW1",
            "Attachments": [{"Id": 1, "FileName": "hw.pdf", "Size": 10, "Type": "File"}],
        }]
        args = [
            "download", "123", "--assignment", "999", "--include-assignments",
            "-o", str(output_dir),
        ]
        if json_output:
            args.append("--json")

        with _scoped_client(), patch.object(
            LighthouseClient, "get_dropbox_folders", return_value=folders,
        ), patch.object(LighthouseClient, "get_content_toc", return_value=_std_toc(123)) as toc_mock, \
             patch.object(LighthouseClient, "download_topic_file") as topic_download_mock:
            result = runner.invoke(cli, args)

        assert result.exit_code == 1, result.output
        assert "999" not in result.stdout + result.stderr
        assert not (output_dir / "Course-123-123").exists()
        assert list(output_dir.iterdir()) == []
        toc_mock.assert_not_called()
        topic_download_mock.assert_not_called()
        if json_output:
            payload = json.loads(result.stdout)
            assert payload["assignment_errors"] == [{
                "error": "Assignment folder not found. Run: lighthouse assignments",
                "type": "assignment_not_found",
            }]
        else:
            assert "Assignment folder not found. Run: lighthouse assignments" in result.stderr

    def test_assignment_preflight_snapshot_is_reused_after_topic_download(
        self, runner, tmp_path,
    ):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        folders = [{
            "Id": 101,
            "Name": "HW1",
            "Attachments": [{"Id": 1, "FileName": "hw.pdf", "Size": 4, "Type": "File"}],
        }]

        with _scoped_client(), \
             patch.object(
                 LighthouseClient,
                 "get_dropbox_folders",
                 side_effect=[folders, {"malformed": True}],
             ) as folders_mock, \
             patch.object(
                 LighthouseClient,
                 "get_content_toc",
                 return_value=_std_toc(123),
             ) as toc_mock, \
             patch.object(
                 LighthouseClient,
                 "download_topic_file",
                 return_value=(b"topic", "topic.pdf"),
             ) as topic_download_mock, \
             patch.object(
                 LighthouseClient,
                 "download_attachment",
                 return_value=(b"hw!!", "hw.pdf"),
             ) as attachment_download_mock:
            result = runner.invoke(cli, [
                "download", "123", "--assignment", "101",
                "--include-assignments", "-o", str(output_dir), "--json",
            ])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["assignment_errors"] == []
        assert len(payload["downloaded"]) == 1
        assert len(payload["assignments_downloaded"]) == 1
        folders_mock.assert_called_once_with(123)
        toc_mock.assert_called_once_with(123)
        topic_download_mock.assert_called_once_with(123, 1230)
        attachment_download_mock.assert_called_once_with(123, 101, 1)

    def test_assignment_preflight_malformed_shape_stops_before_content(
        self, runner, tmp_path,
    ):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        with _scoped_client(), \
             patch.object(
                 LighthouseClient,
                 "get_dropbox_folders",
                 return_value={"malformed": True},
             ) as folders_mock, \
             patch.object(LighthouseClient, "get_content_toc") as toc_mock, \
             patch.object(LighthouseClient, "download_topic_file") as topic_download_mock:
            result = runner.invoke(cli, [
                "download", "123", "--assignment", "101",
                "--include-assignments", "-o", str(output_dir), "--json",
            ])

        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        assert payload["assignment_errors"] == [{
            "error": "Assignment response has an invalid shape.",
            "type": "assignment_list",
        }]
        folders_mock.assert_called_once_with(123)
        toc_mock.assert_not_called()
        topic_download_mock.assert_not_called()
        assert list(output_dir.iterdir()) == []

    def test_corrupt_manifest_exits_1_single_human(self, runner, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        course_dir = output_dir / "Course A-111"
        course_dir.mkdir(parents=True)
        (course_dir / MANIFEST_FILENAME).write_text("garbage{")
        with _scoped_client():
            result = runner.invoke(cli, ["sync", "111", "-o", str(output_dir)])
        assert result.exit_code == 1, result.output
        assert "Warning" in result.output and "Corrupt manifest" in result.output

    def test_corrupt_manifest_warning_does_not_echo_path_or_controls(self, runner, tmp_path):
        """Direct corrupt-manifest warnings remain fixed and control-free."""
        output_dir = tmp_path / "sync-output"
        output_dir.mkdir()
        course_dir = output_dir / "Course A-111"
        course_dir.mkdir(parents=True)
        (course_dir / MANIFEST_FILENAME).write_text("garbage{")

        with _scoped_client():
            result = runner.invoke(cli, ["sync", "111", "-o", str(output_dir)])

        diagnostics = result.stdout + result.stderr
        assert result.exit_code == 1, result.output
        assert "Corrupt manifest; performing full sync." in result.stderr
        assert "\x1b" not in diagnostics

    def test_unsafe_output_root_never_reaches_cli_output(self, runner, tmp_path):
        sentinel = "password=OUTPUT_ROOT_SECRET"
        output_dir = tmp_path / sentinel

        with _scoped_client():
            result = runner.invoke(
                cli,
                ["sync", "111", "-o", str(output_dir), "--json"],
        )

        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]
        assert sentinel not in result.stdout + result.stderr

    def test_corrupt_manifest_exits_1_multi_json(self, runner, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        cfg = self._multi_config(tmp_path)
        course_dir = output_dir / "Course A-111"
        course_dir.mkdir(parents=True)
        (course_dir / MANIFEST_FILENAME).write_text("garbage{")
        with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg), _scoped_client():
            result = runner.invoke(cli, ["sync", "--semester", "100", "-o", str(output_dir), "--json"])
        assert result.exit_code == 1, result.output
        data = json.loads(result.stdout)  # stdout stays pure JSON
        assert any(e.get("type") == "manifest_corrupt"
                   for c in data["courses"] for e in c["errors"])

    def test_also_errors_warn_only_exit_0_both_modes(self, runner, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        cfg = self._multi_config(tmp_path)
        for extra in ([], ["--json"]):
            with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg), _scoped_client():
                result = runner.invoke(cli, ["download", "--semester", "100", "--also", "99999", "-o", str(output_dir), *extra])
            assert result.exit_code == 0, f"extra={extra}: {result.output}"
            if extra:
                assert json.loads(result.stdout)["also_errors"]
            else:
                assert "Course not found" in result.output

    def test_empty_results_exit_0_both_modes(self, runner, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        for extra in ([], ["--json"]):
            with _scoped_client(toc=lambda cid: {"Modules": []}):
                result = runner.invoke(cli, ["download", "111", "-o", str(output_dir), *extra])
            assert result.exit_code == 0, f"extra={extra}: {result.output}"


# ---------------------------------------------------------------------------
# Dry-run CLI fixes
# ---------------------------------------------------------------------------

class TestDryRunCliFixes:
    def test_force_dry_run_keeps_manifest(self, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        course_dir = output_dir / "Course A-111"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        manifest_path.write_text(json.dumps({"100": _mentry(), "999": _mentry()}))

        with _scoped_client():
            result = CliRunner().invoke(
                cli, ["download", "111", "-o", str(output_dir), "--force", "--dry-run", "--json"])

        assert result.exit_code == 0, result.output
        assert manifest_path.exists(), "--force --dry-run must not delete the manifest"
        assert "999" in json.loads(manifest_path.read_text())

    def test_dry_run_json_single_is_pure_json_array(self, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        runner = CliRunner()
        with _scoped_client():
            result = runner.invoke(cli, ["download", "111", "-o", str(output_dir), "--dry-run", "--json"])
        assert result.exit_code == 0, result.output
        planned = json.loads(result.stdout)
        assert planned == [{"topic_id": 1110, "title": "f.pdf", "path": "Mod/f.pdf"}]

    def test_dry_run_json_multi_is_pure_json_envelope(self, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        cfg = tmp_path / "course-config.json"
        cfg.write_text(json.dumps({"tracked_courses": {"111": {"name": "Course A", "semester": "Sem I"}}}))
        runner = CliRunner()
        with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg), _scoped_client():
            result = runner.invoke(cli, ["download", "--semester", "100", "-o", str(output_dir), "--dry-run", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["courses_checked"] == 1
        stub = data["courses"][0]
        assert stub["manifest_total"] == 0
        assert stub["downloaded"] == [] and stub["errors"] == []
        assert stub["planned"] == [
            {"topic_id": 1110, "title": "f.pdf", "path": "Mod/f.pdf"}
        ]

    def test_dry_run_human_still_lists_plan(self, tmp_path):
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        with _scoped_client():
            result = CliRunner().invoke(cli, ["download", "111", "-o", str(output_dir), "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "Would download 1 files" in result.output
        assert "[1110] f.pdf" in result.output


# ---------------------------------------------------------------------------
# Review-fix regressions (PR #12 triage F6/F7/F9)
# ---------------------------------------------------------------------------

class TestReviewFixRegressions:
    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_skipped_entry_reports_module_relative_path(self, root):
        """SYNC skips carry the module-relative path, not the bare filename."""
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {"100": _mentry(filename="file.pdf", size=7)})
        _materialize(course_dir, "Mod/file.pdf", b"content")
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["path"] == "Mod/file.pdf"
        assert not client.body_calls()

    def test_skipped_path_has_no_leading_separator_for_empty_module_title(self, root):
        """A module title that sanitizes to "" must not produce an
        absolute-looking skip path; skip and download paths agree (bare
        filename, matching the containment clamp)."""
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file.pdf", "File", LM_OLD), module="..")},
            names={ORG_ID: "Test"},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {"100": _mentry(filename="file.pdf", size=7)})
        _materialize(course_dir, "file.pdf", b"content")
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["path"] == "file.pdf"

    def test_skipped_entries_still_feed_duplicate_detection(self, root):
        """The F9 path rewrite must not drop SHA-256 tracking of skips."""
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "a.pdf", "File", LM_OLD), (200, "b.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {
            "100": _mentry(filename="a.pdf", sha=manifest_compute_sha256(b"same"), size=4),
            "200": _mentry(filename="b.pdf", sha=manifest_compute_sha256(b"same"), size=4),
        })
        _materialize(course_dir, "Mod/a.pdf", b"same")
        _materialize(course_dir, "Mod/b.pdf", b"same")
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert len(result["skipped"]) == 2
        assert [e["sha256"] for e in result["skipped"]] == [manifest_compute_sha256(b"same")] * 2
        assert len(result["duplicates"]) == 2
        assert result["skipped"][0]["path"] == "Mod/a.pdf"

    def test_malformed_manifest_hashes_are_empty_and_not_duplicates(self, root):
        """Non-string manifest hashes do not leak into metadata or dedup output."""
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "live.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
        )
        course_dir = root / "Test-44347"
        _seed_manifest(course_dir, {
            "100": _mentry(filename="live.pdf", sha=manifest_compute_sha256(b"live"), size=4),
            "200": _mentry(filename="gone.pdf", sha=["not-a-hash"]),
        })
        _materialize(course_dir, "Mod/live.pdf", b"live")

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["skipped"][0]["sha256"] == manifest_compute_sha256(b"live")
        assert result["orphaned"][0]["sha256"] == ""
        assert result["duplicates"] == []

    def test_multi_json_unknown_types_warning_goes_to_stderr(self, tmp_path):
        """Multi-course --json renders engine warnings on stderr; stdout stays pure JSON."""
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        cfg = tmp_path / "course-config.json"
        cfg.write_text(json.dumps({"tracked_courses": {
            "111": {"name": "Course A", "semester": "Sem I"}}}))
        runner = CliRunner()
        with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg), _scoped_client():
            result = runner.invoke(
                cli, ["download", "--semester", "100", "-o", str(output_dir),
                      "--types", "htm", "--json"])
        data = json.loads(result.stdout)  # stdout parses as JSON only
        assert data["summary"]["courses_checked"] == 1
        assert any("Unknown content type" in w for w in result.stderr.splitlines())

    def test_unknown_type_warning_does_not_echo_control_sentinel(self, tmp_path):
        """Unsupported type diagnostics must not echo arbitrary option text."""
        sentinel = "TYPE_SENTINEL\x1b[31m"
        for extra in ([], ["--json"]):
            output_dir = tmp_path / ("json" if extra else "human")
            output_dir.mkdir()
            with _scoped_client():
                result = CliRunner().invoke(
                    cli,
                    [
                        "download", "111", "-o", str(output_dir),
                        "--types", sentinel, *extra,
                    ],
                )
            diagnostics = result.stdout + result.stderr
            assert result.exit_code == 0, result.output
            if extra:
                json.loads(result.stdout)
            assert "Ignored unsupported content type." in result.stderr
            assert sentinel not in diagnostics
            assert "\x1b" not in diagnostics

    def test_empty_course_human_exits_1_on_recorded_errors(self, runner, tmp_path):
        """Empty branch follows the uniform policy: recorded errors → exit 1."""
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        course_dir = output_dir / "Course A-111"
        course_dir.mkdir(parents=True)
        (course_dir / MANIFEST_FILENAME).write_text("not valid json{")
        with _scoped_client(toc=lambda cid: {"Modules": []}):
            result = runner.invoke(cli, ["sync", "111", "-o", str(output_dir)])
        assert result.exit_code == 1, result.output

    def test_empty_course_single_sync_json_carries_errors(self, runner, tmp_path):
        """The empty sync schema populates its existing errors key instead of []"""
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        course_dir = output_dir / "Course A-111"
        course_dir.mkdir(parents=True)
        (course_dir / MANIFEST_FILENAME).write_text("not valid json{")
        with _scoped_client(toc=lambda cid: {"Modules": []}):
            result = runner.invoke(cli, ["sync", "111", "-o", str(output_dir), "--json"])
        data = json.loads(result.stdout)
        assert result.exit_code == 1
        assert data["errors"], "empty-course JSON must surface the manifest error"
        assert set(data["errors"][0]) <= {"topic_id", "error"}
        assert data["course_name"] == "Course A"
        assert data["folder"] == str(course_dir)
        assert data["downloaded"] == []
        assert data["skipped"] == []
        assert data["updated"] == []
        assert data["orphaned"] == []

    def test_empty_course_single_download_json_lists_errors(self, runner, tmp_path):
        """The empty download schema's errors match the non-empty branch's
        list-of-dicts shape (never a bare count)."""
        output_dir = tmp_path / "dl"
        output_dir.mkdir()
        course_dir = output_dir / "Course A-111"
        course_dir.mkdir(parents=True)
        (course_dir / MANIFEST_FILENAME).write_text("not valid json{")
        with _scoped_client(toc=lambda cid: {"Modules": []}):
            result = runner.invoke(cli, ["download", "111", "-o", str(output_dir), "--json"])
        data = json.loads(result.stdout)
        assert result.exit_code == 1
        assert isinstance(data["errors"], list) and data["errors"]
        assert set(data["errors"][0]) <= {"topic_id", "error"}
        assert data["course_name"] == "Course A"
        assert data["folder"] == str(course_dir)
        assert data["manifest"] == str(course_dir / MANIFEST_FILENAME)
        assert data["downloaded"] == []
        assert "files" not in data


# ---------------------------------------------------------------------------
# Path containment: hostile module/topic titles must never escape the root
# ---------------------------------------------------------------------------

class TestPathContainment:
    """TOC titles are professor-controlled input; assembled paths must stay
    inside the course directory (devin-review P0)."""

    def test_traversal_module_title_sanitized_in_flatten(self):
        topics = flatten_all_topics([{"Title": "../../evil", "Modules": [], "Topics": [
            {"TopicId": 100, "Title": "f.pdf", "TypeIdentifier": "File", "Url": "", "LastModifiedDate": LM_NEW},
        ]}])
        assert topics[0]["path"] == "_.._evil/f.pdf"
        assert ".." not in Path(topics[0]["path"]).parent.as_posix().split("/")

    def test_traversal_download_lands_under_course_root(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW), module="../../evil")},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert [e["topic_id"] for e in result["downloaded"]] == ["100"]
        assert not (root.parent / "evil").exists()
        for p in (root / "Test-44347").rglob("*"):
            assert p.resolve().is_relative_to((root / "Test-44347").resolve())

    def test_absolute_module_title_clamped_to_root(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW), module="/tmp/pwn")},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert [e["topic_id"] for e in result["downloaded"]] == ["100"]
        assert not (root / "pwn").exists()

    def test_dotdot_topic_title_cannot_escape(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "..", "File", LM_NEW), module="Mod")},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert [e["topic_id"] for e in result["downloaded"]] == ["100"]
        for p in (root / "Test-44347").rglob("*"):
            assert p.resolve().is_relative_to((root / "Test-44347").resolve())

    def test_course_directory_symlink_outside_root_is_rejected(self, root, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "Test-44347").symlink_to(outside, target_is_directory=True)
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["downloaded"] == []
        assert result["errors"] and result["errors"][0]["type"] == "path"
        assert not list(outside.iterdir())
        assert client.body_calls() == []

    def test_module_directory_symlink_outside_course_is_rejected(self, root, tmp_path):
        course_dir = root / "Test-44347"
        course_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (course_dir / "Mod").symlink_to(outside, target_is_directory=True)
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["downloaded"] == []
        assert result["errors"] and "symlink" in result["errors"][0]["error"]
        assert not list(outside.iterdir())
        assert client.body_calls() == []

    def test_filename_symlink_is_not_overwritten(self, root, tmp_path):
        course_dir = root / "Test-44347"
        (course_dir / "Mod").mkdir(parents=True)
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(b"keep")
        (course_dir / "Mod" / "f.pdf").symlink_to(outside)
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"new!", "f.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["downloaded"] == []
        assert result["errors"]
        assert outside.read_bytes() == b"keep"
        assert (course_dir / "Mod" / "f.pdf").is_symlink()

    def test_existing_course_destination_symlink_is_rejected(self, root, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        course_dir = root / "Test-44347"
        course_dir.symlink_to(outside, target_is_directory=True)
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.DOWNLOAD)

        assert result["errors"] and any(
            marker in result["errors"][0]["error"] for marker in ("symlink", "escapes")
        )
        assert client.body_calls() == []
        assert list(outside.rglob("*")) == []

    def test_course_destination_error_never_embeds_selected_path(self, tmp_path):
        sentinel = "password=OUTPUT_PATH_SECRET"
        root = tmp_path / sentinel
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "Test-44347").symlink_to(outside, target_is_directory=True)
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)

        assert result["errors"]
        assert "OUTPUT_PATH_SECRET" not in json.dumps(result, default=str)

    def test_symlinked_topic_directory_is_rejected(self, root, tmp_path):
        course_dir = root / "Test-44347"
        course_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (course_dir / "Mod").symlink_to(outside, target_is_directory=True)
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.DOWNLOAD)

        assert result["errors"] and "symlink" in result["errors"][0]["error"]
        assert client.body_calls() == []
        assert list(outside.rglob("*")) == []

    def test_symlinked_manifest_is_rejected_before_force_unlink(self, root, tmp_path):
        course_dir = root / "Test-44347"
        course_dir.mkdir()
        outside_manifest = tmp_path / "outside-manifest.json"
        outside_manifest.write_text('{"sentinel": true}', encoding="utf-8")
        (course_dir / MANIFEST_FILENAME).symlink_to(outside_manifest)
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )

        result = run_course(client, ORG_ID, root, mode=Mode.FORCE)

        assert result["errors"] and "symlink" in result["errors"][0]["error"]
        assert outside_manifest.read_text(encoding="utf-8") == '{"sentinel": true}'

    def test_clamp_branch_fires_on_preassembled_escape_path(self, root, tmp_path):
        """Direct clamp coverage: a hostile *pre-assembled* topic path that
        bypasses flatten-time sanitization is clamped and reported."""
        from lighthouse_cli.sync_engine import download_and_persist_topic

        course_root = root / "Test-44347"
        outside = tmp_path / "outside-target"
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )
        manifest = Manifest()
        manifest.path = course_root / MANIFEST_FILENAME
        warnings: list[str] = []
        _, _, filepath = download_and_persist_topic(
            client, ORG_ID,
            {"topic_id": 100, "title": "innocent.pdf",
             "path": f"../../{outside.name}/evil/f.pdf", "last_modified": LM_NEW},
            course_root, manifest, path_owners={}, warnings=warnings,
        )
        assert filepath.resolve().is_relative_to(course_root.resolve())
        assert not outside.exists()
        assert any("clamped to the course root" in w for w in warnings)

    def test_windows_reserved_names_prefixed(self):
        from lighthouse_cli.utils import _sanitize_filename as sanitize

        assert sanitize("CON") == "_CON"
        assert sanitize("con.pdf") == "_con.pdf"  # stem check, case-insensitive
        assert sanitize("NUL") == "_NUL"
        assert sanitize("COM1") == "_COM1"
        assert sanitize("lpt4.txt") == "_lpt4.txt"
        assert sanitize("constant.pdf") == "constant.pdf"  # prefix, not stem
