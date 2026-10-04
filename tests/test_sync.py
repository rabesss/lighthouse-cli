"""Tests for sync command: incremental download with manifest."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli
from lighthouse_cli.manifest import MANIFEST_FILENAME, compute_sha256

LM_OLD = "2026-01-01T00:00:00Z"
LM_SEEDED = "2026-03-15T12:00:00Z"
LM_NEW = "2026-05-01T00:00:00Z"


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def output_dir(tmp_path):
    path = tmp_path / "sync_test"
    path.mkdir()
    return path


def _topic(tid, title, lm=LM_OLD) -> dict:
    return {"TopicId": tid, "Title": title, "TypeIdentifier": "File", "Url": "", "LastModifiedDate": lm}


def _toc(*topics, module_id=1, title="Mod") -> dict:
    return {"Modules": [{"ModuleId": module_id, "Title": title, "Modules": [], "Topics": list(topics)}]}


def _entry(filename, content=b"", *, sha256=None, size=None, last_modified=LM_SEEDED) -> dict:
    """Manifest entry; sha256 and size default to those of ``content``."""
    return {
        "sha256": compute_sha256(content) if sha256 is None else sha256,
        "filename": filename,
        "size": len(content) if size is None else size,
        "downloaded_at": LM_OLD,
        "last_modified": last_modified,
    }


def _seed(output_dir, entries, files=None):
    """Write the Test-44347 manifest and local files; return the course directory."""
    course_dir = output_dir / "Test-44347"
    course_dir.mkdir()
    (course_dir / MANIFEST_FILENAME).write_text(json.dumps(entries))
    for relative, content in (files or {}).items():
        path = course_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return course_dir


def _download_by_topic(results) -> MagicMock:
    """download_topic_file double returning ``results[topic_id]``."""
    return MagicMock(side_effect=lambda _cid, tid: results[tid])


def _sync(cli_runner, output_dir, toc, *args, download=None, **returns):
    """Run ``sync 44347 -o OUTPUT_DIR *ARGS`` for course 44347 "Test".

    ``returns`` maps other LighthouseClient methods to their return values.
    """
    courses = [{"OrgUnitId": 44347, "Name": "Test", "Code": "X"}]
    with ExitStack() as stack:
        for method, value in {"get_courses": courses, "get_content_toc": toc, **returns}.items():
            stack.enter_context(patch.object(LighthouseClient, method, return_value=value))
        stack.enter_context(
            patch.object(LighthouseClient, "download_topic_file", download or MagicMock())
        )
        return cli_runner.invoke(cli, ["sync", "44347", "-o", str(output_dir), *args])


def _sync_json(cli_runner, output_dir, toc, *args, **kwargs) -> dict:
    result = _sync(cli_runner, output_dir, toc, "--json", *args, **kwargs)
    assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
    return json.loads(result.output)


@contextmanager
def _semester_courses(tmp_path, semesters, courses):
    """Track and enroll ``courses``, given as (id, name, code, semester name)."""
    cfg_path = tmp_path / "course-config.json"
    cfg_path.write_text(json.dumps({"tracked_courses": {
        str(cid): {"name": name, "semester": semester} for cid, name, _code, semester in courses
    }}))
    enrollments = [
        {"OrgUnit": {"Id": cid, "Name": name, "Code": code}} for cid, name, code, _ in courses
    ]
    listed = [{"OrgUnitId": cid, "Name": name, "Code": code} for cid, name, code, _ in courses]
    with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg_path), \
         patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
         patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
         patch.object(LighthouseClient, "get_courses", return_value=listed):
        yield


def _folder(fid, name, att_id, filename, size, **extra) -> dict:
    attachment = {"Id": att_id, "FileName": filename, "Size": size, "Type": "File"}
    return {"Id": fid, "Name": name, "Attachments": [attachment], **extra}


class TestSyncIncremental:
    """VAL-SYNC-006, VAL-SYNC-029: sync skips unchanged files, idempotent."""

    def test_sync_skips_unchanged_files_and_is_idempotent(self, cli_runner, output_dir):
        """Manifest last_modified equal to the TOC's → zero downloads, on every run."""
        local_content = b"x" * 1024
        _seed(output_dir, {"100": _entry("file.pdf", local_content)}, {"Mod/file.pdf": local_content})
        download = MagicMock()

        for _ in range(2):
            data = _sync_json(
                cli_runner, output_dir, _toc(_topic(100, "file.pdf", LM_SEEDED)), download=download
            )

            download.assert_not_called()
            assert len(data["skipped"]) == 1
            assert data["skipped"][0]["sha256"] == compute_sha256(local_content)
            assert data["downloaded"] == []

    def test_sync_downloads_new_topic_not_in_manifest(self, cli_runner, output_dir):
        """Topic in TOC but not manifest → downloaded (VAL-SYNC-007)."""
        existing = b"x" * 1024
        _seed(output_dir, {"100": _entry("existing.pdf", existing)}, {"Mod/existing.pdf": existing})
        toc = _toc(
            _topic(100, "existing.pdf", LM_SEEDED),
            _topic(999, "new.pdf", "2026-04-01T00:00:00Z"),
        )
        download = MagicMock(return_value=(b"new content", "new.pdf"))

        data = _sync_json(cli_runner, output_dir, toc, download=download)

        download.assert_called_once_with(44347, 999)
        assert len(data["downloaded"]) == 1
        assert data["downloaded"][0]["topic_id"] == "999"

    def test_sync_re_downloads_changed_topic(self, cli_runner, output_dir):
        """Topic with changed LastModifiedDate → re-downloaded and updated (VAL-SYNC-008)."""
        _seed(output_dir, {"100": _entry(
            "file.pdf", sha256="oldhash", size=1024, last_modified=LM_OLD
        )})
        download = MagicMock(return_value=(b"new content", "file.pdf"))

        data = _sync_json(
            cli_runner, output_dir, _toc(_topic(100, "file.pdf", LM_NEW)), download=download
        )

        download.assert_called_once()
        assert len(data["updated"]) == 1
        assert data["updated"][0]["topic_id"] == "100"

    def test_sync_re_downloads_legacy_hash_with_matching_timestamp(self, cli_runner, output_dir):
        """A legacy non-digest hash must not make a topic eligible for skip."""
        _seed(
            output_dir,
            {"100": _entry("file.pdf", sha256="legacy-hash", size=7)},
            {"Mod/file.pdf": b"content"},
        )
        download = MagicMock(return_value=(b"fresh!", "file.pdf"))

        data = _sync_json(
            cli_runner, output_dir, _toc(_topic(100, "file.pdf", LM_SEEDED)), download=download
        )

        download.assert_called_once_with(44347, 100)
        assert [entry["topic_id"] for entry in data["updated"]] == ["100"]
        assert data["skipped"] == []
        assert data["updated"][0]["sha256"] == compute_sha256(b"fresh!")


class TestSyncManifestHandling:
    """VAL-SYNC-009, VAL-SYNC-010: missing/corrupt manifest handling."""

    def test_sync_missing_manifest_full_download(self, cli_runner, output_dir):
        """No manifest → treat as first-time, download all (VAL-SYNC-009)."""
        download = MagicMock(return_value=(b"content", "f.pdf"))

        _sync_json(cli_runner, output_dir, _toc(_topic(100, "f.pdf")), download=download)

        download.assert_called_once()
        assert (output_dir / "Test-44347" / MANIFEST_FILENAME).exists()

    def test_sync_corrupt_manifest_warning_and_full_download(self, cli_runner, output_dir):
        """Corrupt manifest → warning to stderr + full download (VAL-SYNC-010)."""
        course_dir = output_dir / "Test-44347"
        course_dir.mkdir()
        manifest_path = course_dir / MANIFEST_FILENAME
        manifest_path.write_text("not valid json{")
        download = MagicMock(return_value=(b"content", "f.pdf"))

        result = _sync(
            cli_runner, output_dir, _toc(_topic(100, "f.pdf")), "--json", download=download
        )

        # Uniform exit matrix: manifest corruption is recorded as an error
        # entry → exit 1 (run still continues and re-downloads).
        assert result.exit_code == 1
        assert "Warning" in result.output or "Corrupt" in result.output
        download.assert_called_once()
        # New valid manifest replaces corrupt one
        assert "100" in json.loads(manifest_path.read_text())


class TestSyncOrphaned:
    def test_sync_reports_orphaned_not_deleted(self, cli_runner, output_dir):
        """Topic in manifest but not in TOC → reported as orphaned, not deleted locally (VAL-SYNC-030)."""
        orphan_content = b"old content"
        course_dir = _seed(
            output_dir,
            {
                "100": _entry("file100.pdf", b"content", size=1024),
                "200": _entry("file200.pdf", orphan_content),
            },
            {"file200.pdf": orphan_content},
        )
        download = MagicMock(return_value=(b"content", "file100.pdf"))

        data = _sync_json(
            cli_runner, output_dir, _toc(_topic(100, "file100.pdf", LM_SEEDED)), download=download
        )

        assert len(data["orphaned"]) == 1
        assert data["orphaned"][0]["topic_id"] == "200"
        assert data["orphaned"][0]["sha256"] == compute_sha256(orphan_content)
        assert (course_dir / "file200.pdf").exists(), "Orphaned file should not be deleted"


class TestSyncDownloadedVsUpdated:
    def test_sync_json_separates_downloaded_vs_updated(self, cli_runner, output_dir):
        """JSON has distinct 'downloaded' (new) and 'updated' (re-downloaded) arrays (VAL-SYNC-058)."""
        unchanged = b"x" * 1024
        _seed(
            output_dir,
            {
                "100": _entry("unchanged.pdf", unchanged, last_modified=LM_OLD),
                "200": _entry("updated.pdf", b"old updated content", last_modified=LM_OLD),
            },
            {"Mod/unchanged.pdf": unchanged},
        )
        toc = _toc(
            _topic(100, "unchanged.pdf"),  # unchanged → skipped
            _topic(200, "updated.pdf", LM_NEW),  # changed → updated
            _topic(300, "new.pdf", "2026-04-01T00:00:00Z"),  # new → downloaded
        )
        download = _download_by_topic({
            200: (b"updated content", "updated.pdf"),
            300: (b"new content", "new.pdf"),
        })

        data = _sync_json(cli_runner, output_dir, toc, download=download)

        assert any(e["topic_id"] == "300" for e in data["downloaded"]), f"300 not in downloaded: {data['downloaded']}"
        assert any(e["topic_id"] == "200" for e in data["updated"]), f"200 not in updated: {data['updated']}"
        assert not any(e["topic_id"] == "100" for e in data["downloaded"])
        assert not any(e["topic_id"] == "100" for e in data["updated"])


class TestSyncForceFlag:
    def test_sync_force_deletes_manifest_not_files(self, cli_runner, output_dir):
        """--force deletes manifest but keeps existing files (VAL-SYNC-050)."""
        course_dir = _seed(
            output_dir,
            {
                "100": _entry("file100.pdf", b"old content", sha256="hash100"),
                "200": _entry("file200.pdf", b"content200", sha256="hash200"),
            },
            {"file100.pdf": b"old content", "file200.pdf": b"content200"},
        )
        download = MagicMock(return_value=(b"new content", "file100.pdf"))

        # TOC only has topic 100 — topic 200 will become orphaned
        _sync_json(
            cli_runner, output_dir, _toc(_topic(100, "file100.pdf", LM_NEW)), "--force",
            download=download,
        )

        assert (course_dir / "file100.pdf").exists(), "file100.pdf should still exist"
        assert (course_dir / "file200.pdf").exists(), "file200.pdf should still exist (orphaned)"
        manifest_path = course_dir / MANIFEST_FILENAME
        assert manifest_path.exists(), "New manifest should be created after --force"
        new_manifest = json.loads(manifest_path.read_text())
        assert "100" in new_manifest
        assert "200" not in new_manifest, "Topic 200 should not be in manifest (orphaned)"


class TestSyncOutput:
    def test_sync_json_includes_summary_counts(self, cli_runner, output_dir):
        """JSON output includes topic-level arrays and counts matching summary (VAL-SYNC-015, VAL-SYNC-040)."""
        toc = _toc(_topic(100, "f1.pdf"), _topic(200, "f2.pdf", "2026-04-01T00:00:00Z"))
        download = _download_by_topic({100: (b"content1", "f1.pdf"), 200: (b"content2", "f2.pdf")})

        data = _sync_json(cli_runner, output_dir, toc, download=download)

        assert {"downloaded", "skipped", "updated", "orphaned", "errors"} <= data.keys()

    def test_sync_human_output_shows_counts(self, cli_runner, output_dir):
        download = MagicMock(return_value=(b"content1", "f1.pdf"))

        result = _sync(cli_runner, output_dir, _toc(_topic(100, "f1.pdf")), download=download)

        assert result.exit_code == 0
        assert "new" in result.output or "downloaded" in result.output.lower()
        assert "orphaned" in result.output

    def test_sync_empty_toc_zero_downloads(self, cli_runner, output_dir):
        """VAL-SYNC-045: a course with an empty TOC exits 0 with nothing downloaded."""
        data = _sync_json(cli_runner, output_dir, {"Modules": []})

        assert data["downloaded"] == []
        assert data["skipped"] == []


class TestSyncAllCourses:
    def test_sync_without_course_id_syncs_latest_semester(self, cli_runner, tmp_path, output_dir):
        """No course_id → sync all courses from latest semester (VAL-SYNC-011)."""
        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "0902_I_2024-2025"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "0902_II_2024-2025"},
        ]
        courses = [
            (111, "Course A", "009_CourseA_0902_I_2024-2025", "Sem I"),
            (222, "Course B", "009_CourseB_0902_II_2024-2025", "Sem II"),
        ]
        tocs = {
            111: _toc(_topic(10, "a.pdf")),
            222: _toc(_topic(20, "b.pdf"), module_id=2, title="Mod2"),
        }
        files = {111: (b"content a", "a.pdf"), 222: (b"content b", "b.pdf")}

        with _semester_courses(tmp_path, semesters, courses), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=tocs.get), \
             patch.object(
                 LighthouseClient, "download_topic_file", side_effect=lambda cid, _tid: files[cid]
             ):
            result = cli_runner.invoke(cli, ["sync", "-o", str(output_dir)])

        assert result.exit_code == 0
        # Only Sem II courses sync (latest semester = highest OrgUnitId 200).
        assert not (output_dir / "Course A-111").exists(), "Course A (Sem I) should not be synced"
        assert (output_dir / "Course B-222").exists(), "Course B (Sem II) should be synced"


class TestSyncMultiCourseWithAssignments:
    """Multi-course sync with --include-assignments (fixes tuple unpacking bug)."""

    def test_sync_multi_course_with_include_assignments_no_value_error(
        self, cli_runner, tmp_path, output_dir
    ):
        """Multi-course sync with --include-assignments unpacks 4 values from _sync_assignments_for_course.

        Before the fix: ValueError: too many values to unpack (expected 3)
        After the fix: exit code 0, valid JSON output
        """
        semesters = [{"OrgUnitId": 300, "Name": "Sem III", "Code": "S3"}]
        courses = [(311, "Signals", "S3", "Sem III"), (322, "Physics", "S3", "Sem III")]
        details = {
            311: _folder(101, "HW 1", 1, "hw1.pdf", 512),
            322: _folder(201, "Lab 1", 2, "lab1.pdf", 2048),
        }
        due = {311: "2026-05-20T23:59:00Z", 322: "2026-06-01T23:59:00Z"}
        attachments = {101: (b"hw1 content", "hw1.pdf"), 201: (b"lab1 content", "lab1.pdf")}

        with _semester_courses(tmp_path, semesters, courses), \
             patch.object(
                 LighthouseClient, "get_content_toc",
                 side_effect=lambda cid: _toc(_topic(cid * 10, "f.pdf"), module_id=cid),
             ), \
             patch.object(
                 LighthouseClient, "download_topic_file",
                 side_effect=lambda cid, _tid: (f"content{cid}".encode(), "f.pdf"),
             ), \
             patch.object(
                 LighthouseClient, "get_dropbox_folders",
                 side_effect=lambda cid: [{**details[cid], "DueDate": due[cid]}],
             ), \
             patch.object(
                 LighthouseClient, "get_dropbox_folder_detail",
                 side_effect=lambda cid, _fid: details[cid],
             ), \
             patch.object(
                 LighthouseClient, "download_attachment",
                 side_effect=lambda _cid, fid, _att_id: attachments[fid],
             ):
            result = cli_runner.invoke(
                cli,
                ["sync", "--semester", "300", "--include-assignments", "-o", str(output_dir), "--json"],
            )

        assert result.exit_code == 0, f"exit={result.exit_code} output={result.output} exception={result.exception}"
        data = json.loads(result.output)
        assert len(data["courses"]) == 2
        course_names = {c["course_name"] for c in data["courses"]}
        assert "Signals" in course_names
        assert "Physics" in course_names
        for course in data["courses"]:
            assert "assignments_downloaded" in course
            assert "assignments_skipped" in course
            assert "assignments_updated" in course
            assert "assignment_errors" in course
        assert (output_dir / "Signals-311" / "Assignments" / "HW 1" / "hw1.pdf").exists()
        assert (output_dir / "Physics-322" / "Assignments" / "Lab 1" / "lab1.pdf").exists()

    def test_sync_single_course_with_include_assignments_no_value_error(
        self, cli_runner, output_dir
    ):
        """Single-course sync with --include-assignments also unpacks 4 values."""
        homework = _folder(101, "HW 1", 1, "hw1.pdf", 512)

        result = _sync(
            cli_runner, output_dir, _toc(_topic(10, "f.pdf")), "--include-assignments", "--json",
            download=MagicMock(return_value=(b"content", "f.pdf")),
            get_dropbox_folders=[{**homework, "DueDate": "2026-05-20T23:59:00Z"}],
            get_dropbox_folder_detail=homework,
            download_attachment=(b"hw1 content", "hw1.pdf"),
        )

        assert result.exit_code == 0, f"exit={result.exit_code} output={result.output} exception={result.exception}"
        data = json.loads(result.output)
        assert data["course_id"] == 44347
        assert len(data["assignments_downloaded"]) > 0
