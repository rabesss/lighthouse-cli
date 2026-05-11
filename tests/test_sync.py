"""Tests for sync command: incremental download with manifest."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli
from lighthouse_cli.manifest import MANIFEST_FILENAME


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


class TestSyncIncremental:
    """VAL-SYNC-006, VAL-SYNC-029: sync skips unchanged files, idempotent."""

    def test_sync_skips_unchanged_files_no_download(self, cli_runner, tmp_path):
        """Pre-seeded manifest with current last_modified → zero downloads (VAL-SYNC-006)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        # Pre-seed manifest
        course_dir = output_dir / "Test-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        manifest_data = {
            "100": {
                "sha256": "abc123",
                "filename": "file.pdf",
                "size": 1024,
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-03-15T12:00:00Z",
            }
        }
        manifest_path.write_text(json.dumps(manifest_data))

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "file.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-03-15T12:00:00Z"},  # Same as manifest
                ]
            }]
        }

        download_mock = MagicMock()

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", download_mock):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            # download_topic_file should NOT have been called (no changes)
            download_mock.assert_not_called()
            data = json.loads(result.output)
            assert len(data["skipped"]) == 1
            assert data["downloaded"] == []

    def test_sync_idempotent_second_run_no_downloads(self, cli_runner, tmp_path):
        """Running sync twice with no remote changes → zero downloads on second run (VAL-SYNC-029)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        # Pre-seed manifest
        course_dir = output_dir / "Test-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        manifest_data = {
            "100": {
                "sha256": "abc123",
                "filename": "file.pdf",
                "size": 1024,
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-03-15T12:00:00Z",
            }
        }
        manifest_path.write_text(json.dumps(manifest_data))

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "file.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-03-15T12:00:00Z"},
                ]
            }]
        }

        download_mock = MagicMock()

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", download_mock):

            # First sync
            result1 = cli_runner.invoke(cli, ["sync", "44347", "-o", str(output_dir), "--json"])
            assert result1.exit_code == 0
            download_mock.assert_not_called()

            # Second sync — same state
            result2 = cli_runner.invoke(cli, ["sync", "44347", "-o", str(output_dir), "--json"])
            assert result2.exit_code == 0
            download_mock.assert_not_called()

    def test_sync_downloads_new_topic_not_in_manifest(self, cli_runner, tmp_path):
        """Topic in TOC but not manifest → downloaded (VAL-SYNC-007)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        # Pre-seed manifest with 2 entries (topic 100 and 200)
        course_dir = output_dir / "Test-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        manifest_data = {
            "100": {
                "sha256": "abc123",
                "filename": "existing.pdf",
                "size": 1024,
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-03-15T12:00:00Z",
            }
        }
        manifest_path.write_text(json.dumps(manifest_data))

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "existing.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-03-15T12:00:00Z"},  # unchanged
                    {"TopicId": 999, "Title": "new.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-04-01T00:00:00Z"},  # NEW
                ]
            }]
        }

        download_mock = MagicMock(return_value=(b"new content", "new.pdf"))

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", download_mock):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            # Should have downloaded new topic 999
            download_mock.assert_called_once_with(44347, 999)
            data = json.loads(result.output)
            assert len(data["downloaded"]) == 1
            assert data["downloaded"][0]["topic_id"] == "999"

    def test_sync_re_downloads_changed_topic(self, cli_runner, tmp_path):
        """Topic with changed LastModifiedDate → re-downloaded and updated (VAL-SYNC-008)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        # Pre-seed manifest with old last_modified
        course_dir = output_dir / "Test-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        manifest_data = {
            "100": {
                "sha256": "oldhash",
                "filename": "file.pdf",
                "size": 1024,
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-01-01T00:00:00Z",  # old date
            }
        }
        manifest_path.write_text(json.dumps(manifest_data))

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "file.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-05-01T00:00:00Z"},  # NEW date
                ]
            }]
        }

        download_mock = MagicMock(return_value=(b"new content", "file.pdf"))

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", download_mock):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            download_mock.assert_called_once()
            data = json.loads(result.output)
            assert len(data["updated"]) == 1
            assert data["updated"][0]["topic_id"] == "100"


class TestSyncManifestHandling:
    """VAL-SYNC-009, VAL-SYNC-010: missing/corrupt manifest handling."""

    def test_sync_missing_manifest_full_download(self, cli_runner, tmp_path):
        """No manifest → treat as first-time, download all (VAL-SYNC-009)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "f.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        download_mock = MagicMock(return_value=(b"content", "f.pdf"))

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", download_mock):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0
            download_mock.assert_called_once()
            manifest_path = output_dir / "Test-44347" / MANIFEST_FILENAME
            assert manifest_path.exists()

    def test_sync_corrupt_manifest_warning_and_full_download(self, cli_runner, tmp_path):
        """Corrupt manifest → warning to stderr + full download (VAL-SYNC-010)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        # Write garbage to manifest
        course_dir = output_dir / "Test-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        manifest_path.write_text("not valid json{")

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "f.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        download_mock = MagicMock(return_value=(b"content", "f.pdf"))

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", download_mock):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0
            assert "Warning" in result.output or "Corrupt" in result.output
            download_mock.assert_called_once()
            # New valid manifest replaces corrupt one
            new_data = json.loads(manifest_path.read_text())
            assert "100" in new_data


class TestSyncOrphaned:
    """VAL-SYNC-030: orphaned topics (in manifest but not in TOC)."""

    def test_sync_reports_orphaned_not_deleted(self, cli_runner, tmp_path):
        """Topic in manifest but not in TOC → reported as orphaned, not deleted locally (VAL-SYNC-030)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        # Pre-seed manifest with 3 topics
        course_dir = output_dir / "Test-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        manifest_data = {
            "100": {
                "sha256": "hash100",
                "filename": "file100.pdf",
                "size": 1024,
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-03-15T12:00:00Z",
            },
            "200": {
                "sha256": "hash200",
                "filename": "file200.pdf",
                "size": 2048,
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-03-15T12:00:00Z",
            },
        }
        manifest_path.write_text(json.dumps(manifest_data))

        # File on disk for topic 200 (orphaned)
        file200 = course_dir / "file200.pdf"
        file200.write_bytes(b"old content")

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "file100.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-03-15T12:00:00Z"},  # Only 100 in TOC
                ]
            }]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"content", "file100.pdf")):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)
            assert len(data["orphaned"]) == 1
            assert data["orphaned"][0]["topic_id"] == "200"
            # File still exists on disk
            assert file200.exists(), "Orphaned file should not be deleted"


class TestSyncDownloadedVsUpdated:
    """VAL-SYNC-058: JSON output separates downloaded (new) vs updated (re-downloaded)."""

    def test_sync_json_separates_downloaded_vs_updated(self, cli_runner, tmp_path):
        """JSON has distinct 'downloaded' (new) and 'updated' (re-downloaded) arrays (VAL-SYNC-058)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        course_dir = output_dir / "Test-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        manifest_data = {
            "100": {
                "sha256": "oldhash",
                "filename": "unchanged.pdf",
                "size": 1024,
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-01-01T00:00:00Z",
            },
            "200": {
                "sha256": "oldhash200",
                "filename": "updated.pdf",
                "size": 2048,
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-01-01T00:00:00Z",  # Old date — will differ from TOC
            },
        }
        manifest_path.write_text(json.dumps(manifest_data))

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "unchanged.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},  # unchanged → skipped
                    {"TopicId": 200, "Title": "updated.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-05-01T00:00:00Z"},  # changed → updated
                    {"TopicId": 300, "Title": "new.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-04-01T00:00:00Z"},  # new → downloaded
                ]
            }]
        }

        def download_side_effect(cid, tid):
            if tid == 200:
                return (b"updated content", "updated.pdf")
            elif tid == 300:
                return (b"new content", "new.pdf")
            return (b"unchanged content", "unchanged.pdf")

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_side_effect):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)
            assert "downloaded" in data
            assert "updated" in data
            # 300 is new → downloaded
            assert any(e["topic_id"] == "300" for e in data["downloaded"]), f"300 not in downloaded: {data['downloaded']}"
            # 200 is updated (changed) → updated
            assert any(e["topic_id"] == "200" for e in data["updated"]), f"200 not in updated: {data['updated']}"
            # 100 is skipped → not in downloaded or updated
            assert not any(e["topic_id"] == "100" for e in data["downloaded"])
            assert not any(e["topic_id"] == "100" for e in data["updated"])


class TestSyncForceFlag:
    """VAL-SYNC-016, VAL-SYNC-050: --force flag behavior."""

    def test_sync_force_deletes_manifest_not_files(self, cli_runner, tmp_path):
        """--force deletes manifest but keeps existing files (VAL-SYNC-050)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        # Pre-seed files and manifest
        course_dir = output_dir / "Test-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        file100 = course_dir / "file100.pdf"
        file100.write_bytes(b"old content")
        manifest_data = {
            "100": {
                "sha256": "hash100",
                "filename": "file100.pdf",
                "size": len(b"old content"),
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-03-15T12:00:00Z",
            },
            "200": {
                "sha256": "hash200",
                "filename": "file200.pdf",
                "size": len(b"content200"),
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-03-15T12:00:00Z",
            },
        }
        manifest_path.write_text(json.dumps(manifest_data))
        file200 = course_dir / "file200.pdf"
        file200.write_bytes(b"content200")

        # TOC only has topic 100 — topic 200 will become orphaned
        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "file100.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-05-01T00:00:00Z"},  # Changed
                ]
            }]
        }

        download_mock = MagicMock(return_value=(b"new content", "file100.pdf"))

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", download_mock):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "-o", str(output_dir), "--force", "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            # Files should still exist
            assert file100.exists(), "file100.pdf should still exist"
            assert file200.exists(), "file200.pdf should still exist (orphaned)"
            # New manifest should be created (--force wipes old, then new one is written)
            assert manifest_path.exists(), "New manifest should be created after --force"
            new_manifest = json.loads(manifest_path.read_text())
            assert "100" in new_manifest
            assert "200" not in new_manifest, "Topic 200 should not be in manifest (orphaned)"


class TestSyncOutput:
    """VAL-SYNC-006, VAL-SYNC-015, VAL-SYNC-040: output formatting."""

    def test_sync_json_includes_summary_counts(self, cli_runner, tmp_path):
        """JSON output includes topic-level arrays and counts matching summary (VAL-SYNC-015, VAL-SYNC-040)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "f1.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    {"TopicId": 200, "Title": "f2.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-04-01T00:00:00Z"},
                ]
            }]
        }

        def download_side_effect(cid, tid):
            if tid == 200:
                return (b"content2", "f2.pdf")
            return (b"content1", "f1.pdf")

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_side_effect):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)
            assert "downloaded" in data
            assert "skipped" in data
            assert "updated" in data
            assert "orphaned" in data
            assert "errors" in data


class TestSyncHumanOutput:
    """Human-readable sync output."""

    def test_sync_human_output_shows_counts(self, cli_runner, tmp_path):
        """Human output shows counts for new/updated/skipped/orphaned/errors."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 100, "Title": "f1.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"content1", "f1.pdf")):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "-o", str(output_dir)],
            )

            assert result.exit_code == 0
            assert "new" in result.output or "downloaded" in result.output.lower()
            assert "orphaned" in result.output


class TestSyncEmptyCourse:
    """VAL-SYNC-045: empty TOC handling."""

    def test_sync_empty_toc_zero_downloads(self, cli_runner, tmp_path):
        """Course with empty TOC → exit 0, no manifest, no errors (VAL-SYNC-045)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()

        toc = {"Modules": []}

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Test", "Code": "X"}
        ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)
            assert data["downloaded"] == []
            assert data["skipped"] == []


class TestSyncAllCourses:
    """Sync without course_id syncs all courses from latest semester."""

    def test_sync_without_course_id_syncs_latest_semester(self, cli_runner, tmp_path):
        """No course_id → sync all courses from latest semester (VAL-SYNC-011)."""
        output_dir = tmp_path / "sync_test"
        output_dir.mkdir()
        cfg_path = tmp_path / "course-config.json"

        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "0902_I_2024-2025"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "0902_II_2024-2025"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2024-2025"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2024-2025"}},
        ]

        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "111": {"name": "Course A", "semester": "Sem I"},
                "222": {"name": "Course B", "semester": "Sem II"},
            }
        }))

        toc_course_a = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [], "Topics": [
                    {"TopicId": 10, "Title": "a.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        toc_course_b = {
            "Modules": [{
                "ModuleId": 2, "Title": "Mod2", "Modules": [], "Topics": [
                    {"TopicId": 20, "Title": "b.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        def get_content_toc(cid):
            if cid == 222:
                return toc_course_b
            return toc_course_a

        def download_side_effect(cid, tid):
            if cid == 222:
                return (b"content b", "b.pdf")
            return (b"content a", "a.pdf")

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", side_effect=lambda: [
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "A"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "B"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_side_effect):

            result = cli_runner.invoke(
                cli,
                ["sync", "-o", str(output_dir)],
            )

            assert result.exit_code == 0
            # Should have synced only Sem II courses (highest OrgUnitId = 200)
            # Course A (Sem I) should NOT be synced
            assert not (output_dir / "Course A-111").exists(), "Course A (Sem I) should not be synced"
            # Course B (Sem II) should be synced
            assert (output_dir / "Course B-222").exists(), "Course B (Sem II) should be synced"


class TestSyncMultiCourseWithAssignments:
    """Multi-course sync with --include-assignments (fixes tuple unpacking bug)."""

    def test_sync_multi_course_with_include_assignments_no_value_error(self, cli_runner, tmp_path):
        """Multi-course sync with --include-assignments unpacks 4 values from _sync_assignments_for_course.

        Before the fix: ValueError: too many values to unpack (expected 3)
        After the fix: exit code 0, valid JSON output
        """
        output_dir = tmp_path / "sync_assignments_test"
        output_dir.mkdir()
        cfg_path = tmp_path / "course-config.json"

        semesters = [{"OrgUnitId": 300, "Name": "Sem III", "Code": "S3"}]
        enrollments = [
            {"OrgUnit": {"Id": 311, "Name": "Signals", "Code": "S3"}},
            {"OrgUnit": {"Id": 322, "Name": "Physics", "Code": "S3"}},
        ]

        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "311": {"name": "Signals", "semester": "Sem III"},
                "322": {"name": "Physics", "semester": "Sem III"},
            }
        }))

        def get_content_toc(cid):
            return {
                "Modules": [{
                    "ModuleId": cid, "Title": "Mod", "Modules": [],
                    "Topics": [
                        {"TopicId": cid * 10, "Title": "f.pdf",
                         "TypeIdentifier": "File", "Url": "",
                         "LastModifiedDate": "2026-01-01T00:00:00Z"},
                    ]
                }]
            }

        def download_side_effect(cid, tid):
            return f"content{cid}".encode(), "f.pdf"

        folders_signals = [
            {"Id": 101, "Name": "HW 1", "DueDate": "2026-05-20T23:59:00Z",
             "Attachments": [{"Id": 1, "FileName": "hw1.pdf", "Size": 512, "Type": "File"}]},
        ]
        folders_physics = [
            {"Id": 201, "Name": "Lab 1", "DueDate": "2026-06-01T23:59:00Z",
             "Attachments": [{"Id": 2, "FileName": "lab1.pdf", "Size": 2048, "Type": "File"}]},
        ]

        def get_dropbox_folders(cid):
            if cid == 311:
                return folders_signals
            return folders_physics

        def get_dropbox_folder_detail(cid, fid):
            if cid == 311 and fid == 101:
                return {"Id": 101, "Name": "HW 1", "Attachments": [{"Id": 1, "FileName": "hw1.pdf", "Size": 512, "Type": "File"}]}
            return {"Id": 201, "Name": "Lab 1", "Attachments": [{"Id": 2, "FileName": "lab1.pdf", "Size": 2048, "Type": "File"}]}

        def download_attachment(cid, fid, att_id):
            if fid == 101:
                return b"hw1 content", "hw1.pdf"
            return b"lab1 content", "lab1.pdf"

        with patch("lighthouse_cli.commands.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", side_effect=lambda: [
                 {"OrgUnitId": 311, "Name": "Signals", "Code": "S3"},
                 {"OrgUnitId": 322, "Name": "Physics", "Code": "S3"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=get_content_toc), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_side_effect), \
             patch.object(LighthouseClient, "get_dropbox_folders", side_effect=get_dropbox_folders), \
             patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment):

            result = cli_runner.invoke(
                cli,
                ["sync", "--semester", "300", "--include-assignments", "-o", str(output_dir), "--json"],
            )

            # Before fix: ValueError: too many values to unpack (expected 3)
            # After fix: exit code 0 with valid JSON
            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output} exception={result.exception}"
            data = json.loads(result.output)

            # Verify JSON structure
            assert "courses" in data
            assert len(data["courses"]) == 2

            # Check assignments were processed
            course_names = {c["course_name"] for c in data["courses"]}
            assert "Signals" in course_names
            assert "Physics" in course_names

            # Each course should have assignment results
            for course in data["courses"]:
                assert "assignments_downloaded" in course
                assert "assignments_skipped" in course
                assert "assignments_updated" in course
                assert "assignment_errors" in course

            # Verify assignment files exist on disk
            signals_dir = output_dir / "Signals-311"
            assert (signals_dir / "Assignments" / "HW 1" / "hw1.pdf").exists()
            physics_dir = output_dir / "Physics-322"
            assert (physics_dir / "Assignments" / "Lab 1" / "lab1.pdf").exists()

    def test_sync_single_course_with_include_assignments_no_value_error(self, cli_runner, tmp_path):
        """Single-course sync with --include-assignments also unpacks 4 values.

        This ensures the fix works for both single and multi-course sync paths.
        """
        output_dir = tmp_path / "sync_single_assignments_test"
        output_dir.mkdir()

        toc = {
            "Modules": [{
                "ModuleId": 1, "Title": "Mod", "Modules": [],
                "Topics": [
                    {"TopicId": 10, "Title": "f.pdf", "TypeIdentifier": "File",
                     "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"},
                ]
            }]
        }

        folders = [
            {"Id": 101, "Name": "HW 1", "DueDate": "2026-05-20T23:59:00Z",
             "Attachments": [{"Id": 1, "FileName": "hw1.pdf", "Size": 512, "Type": "File"}]},
        ]

        with patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Signals", "Code": "X"},
        ]), \
             patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
             patch.object(LighthouseClient, "download_topic_file", return_value=(b"content", "f.pdf")), \
             patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_dropbox_folder_detail", return_value={
                 "Id": 101, "Name": "HW 1",
                 "Attachments": [{"Id": 1, "FileName": "hw1.pdf", "Size": 512, "Type": "File"}]
             }), \
             patch.object(LighthouseClient, "download_attachment", return_value=(b"hw1 content", "hw1.pdf")):

            result = cli_runner.invoke(
                cli,
                ["sync", "44347", "--include-assignments", "-o", str(output_dir), "--json"],
            )

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output} exception={result.exception}"
            data = json.loads(result.output)
            assert data["course_id"] == 44347
            assert "assignments_downloaded" in data
            assert len(data["assignments_downloaded"]) > 0
