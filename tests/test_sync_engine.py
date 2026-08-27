"""Zero-mock decision tests for the sync engine (lighthouse_cli/sync_engine.py).

Style follows test_manifest.py: plain data in, plain data out. The engine is
exercised through a fake client (no mocks, no network) plus CliRunner smoke
tests for the exit-code matrix and dry-run fixes.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli
from lighthouse_cli.manifest import MANIFEST_FILENAME, Manifest
from lighthouse_cli.sync_engine import Mode, flatten_all_topics, run_course

ORG_ID = 44347
LM_OLD = "2026-01-01T00:00:00Z"
LM_NEW = "2026-05-01T00:00:00Z"


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


def _mentry(lm: str = LM_OLD, filename: str = "file.pdf", sha: str = "abc123", size: int = 1024) -> dict:
    return {"sha256": sha, "filename": filename, "size": size,
            "downloaded_at": "2026-01-01T00:00:00Z", "last_modified": lm}


def _seed_manifest(course_dir: Path, entries: dict) -> Path:
    course_dir.mkdir(parents=True, exist_ok=True)
    path = course_dir / MANIFEST_FILENAME
    path.write_text(json.dumps(entries))
    return path


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

class TestSyncDecisions:
    """Incremental decisions: skip / update / download / orphan / dedup."""

    def test_unchanged_topic_skipped_without_body_fetch(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "file.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "file.pdf")},
        )
        _seed_manifest(root / "Test-44347", {"100": _mentry()})
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert [e["topic_id"] for e in result["skipped"]] == ["100"]
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
        assert orphan_file.exists(), "orphaned file must not be deleted"

    def test_corrupt_manifest_warns_records_and_recovers(self, root):
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "f.pdf", "File", LM_NEW))},
            names={ORG_ID: "Test"},
            files={100: (b"content", "f.pdf")},
        )
        _seed_manifest(root / "Test-44347", {})
        (root / "Test-44347" / MANIFEST_FILENAME).write_text("not valid json{")
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert len(result["warnings"]) == 1 and "Corrupt manifest" in result["warnings"][0]
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
        assert json.loads(result.output)["errors"][0]["error"] == "Network error"

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
        data = json.loads(result.output)
        failed = next(c for c in data["courses"] if c["course_id"] == 222)
        assert "Network error" in failed["errors"][0]["error"]

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
            assert "99999" in result.output  # reported, but lenient

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
        _seed_manifest(root / "Test-44347", {"100": _mentry(filename="file.pdf")})
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
        _seed_manifest(root / "Test-44347", {"100": _mentry(filename="file.pdf")})
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["path"] == "file.pdf"

    def test_skipped_entries_still_feed_duplicate_detection(self, root):
        """The F9 path rewrite must not drop SHA-256 tracking of skips."""
        client = FakeClient(
            tocs={ORG_ID: _toc((100, "a.pdf", "File", LM_OLD), (200, "b.pdf", "File", LM_OLD))},
            names={ORG_ID: "Test"},
        )
        _seed_manifest(root / "Test-44347", {
            "100": _mentry(filename="a.pdf", sha="same"),
            "200": _mentry(filename="b.pdf", sha="same"),
        })
        result = run_course(client, ORG_ID, root, mode=Mode.SYNC)
        assert len(result["skipped"]) == 2
        assert len(result["duplicates"]) == 2
        assert result["skipped"][0]["path"] == "Mod/a.pdf"

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
            course_root, manifest, warnings=warnings,
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
