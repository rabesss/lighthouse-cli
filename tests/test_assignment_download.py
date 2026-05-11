"""Tests for assignment attachment downloading (VAL-ASGN-009 – VAL-ASGN-021, VAL-CROSS-005, VAL-CROSS-006)."""

from __future__ import annotations

import json
import os
import tempfile
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


@pytest.fixture
def temp_download_dir(tmp_path: Path) -> Path:
    d = tmp_path / "downloads"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# VAL-ASGN-009: Download all assignment attachments for a course
# ---------------------------------------------------------------------------

class TestDownloadAllAssignmentAttachments:
    """Test lighthouse download COURSE_ID --include-assignments."""

    def test_download_include_assignments_saves_to_assignments_subfolder(
        self, cli_runner, temp_download_dir
    ):
        """VAL-ASGN-009: Attachments saved to {course_dir}/Assignments/{FolderName}/{FileName}."""
        folders = [
            {
                "Id": 101,
                "Name": "Assignment 1",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 1, "FileName": "q1.pdf", "Size": 1024, "Type": "File"},
                ],
            },
        ]

        def get_dropbox_folder_detail(cid, fid):
            for f in folders:
                if f["Id"] == fid:
                    return f
            return None

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return b"PDF content here", "q1.pdf"
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", return_value={"Modules": []}):

            result = cli_runner.invoke(cli, [
                "download", "44347",
                "--include-assignments",
                "-o", str(temp_download_dir),
            ])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dir = temp_download_dir / "Signals & Systems-44347"
            assignments_dir = course_dir / "Assignments" / "Assignment 1"
            assert assignments_dir.exists(), f"Assignments dir not found: {assignments_dir}"
            assert (assignments_dir / "q1.pdf").exists()

    def test_download_include_assignments_with_content_topics(
        self, cli_runner, temp_download_dir
    ):
        """VAL-CROSS-005: Download command fetches both content topics and assignment attachments."""
        folders = [
            {
                "Id": 101,
                "Name": "Assignment 1",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 1, "FileName": "q1.pdf", "Size": 1024, "Type": "File"},
                ],
            },
        ]

        toc = {
            "Modules": [
                {
                    "ModuleId": 1001,
                    "Title": "Unit 1",
                    "Modules": [],
                    "Topics": [
                        {
                            "TopicId": 12345,
                            "Title": "Lecture 1.pdf",
                            "TypeIdentifier": "File",
                            "Url": "https://example.com/files/12345",
                        },
                    ],
                },
            ]
        }

        def get_dropbox_folder_detail(cid, fid):
            for f in folders:
                if f["Id"] == fid:
                    return f
            return None

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return b"PDF content", "q1.pdf"
            raise Exception("Not found")

        def download_topic(cid, tid):
            if tid == 12345:
                return b"Lecture content", "Lecture 1.pdf"
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", return_value=toc):

            result = cli_runner.invoke(cli, [
                "download", "44347",
                "--include-assignments",
                "-o", str(temp_download_dir),
            ])

            assert result.exit_code == 0, f"exit={result.exit_code}"
            course_dir = temp_download_dir / "Signals & Systems-44347"
            # Content topic
            assert (course_dir / "Unit 1" / "Lecture 1.pdf").exists()
            # Assignment attachment
            assert (course_dir / "Assignments" / "Assignment 1" / "q1.pdf").exists()


# ---------------------------------------------------------------------------
# VAL-ASGN-010: Single attachment download via --assignment + --attachment
# ---------------------------------------------------------------------------

class TestSingleAttachmentDownload:
    """Test lighthouse download COURSE_ID --assignment FOLDER_ID --attachment FILE_ID."""

    def test_single_attachment_download(self, cli_runner, temp_download_dir):
        """VAL-ASGN-010: Single attachment download via --assignment and --attachment flags."""
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "DueDate": "2026-05-20T23:59:00Z",
            "Attachments": [
                {"Id": 1, "FileName": "q1.pdf", "Size": 1024, "Type": "File"},
                {"Id": 2, "FileName": "q2.pdf", "Size": 2048, "Type": "File"},
            ],
        }

        def get_dropbox_folder_detail(cid, fid):
            return folder

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return b"Single PDF content", "q1.pdf"
            if fid == 101 and att_id == 2:
                return b"Second PDF content", "q2.pdf"
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, [
                "download", "44347",
                "--assignment", "101",
                "--attachment", "1",
                "-o", str(temp_download_dir),
            ])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dir = temp_download_dir / "Signals & Systems-44347"
            assert (course_dir / "Assignments" / "Assignment 1" / "q1.pdf").exists()
            # q2 should NOT be downloaded
            assert not (course_dir / "Assignments" / "Assignment 1" / "q2.pdf").exists()

    def test_single_attachment_json_output(self, cli_runner, temp_download_dir):
        """VAL-ASGN-010: JSON mode returns path, size_kb, filename."""
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "DueDate": "2026-05-20T23:59:00Z",
            "Attachments": [
                {"Id": 1, "FileName": "q1.pdf", "Size": 1024, "Type": "File"},
            ],
        }

        def get_dropbox_folder_detail(cid, fid):
            return folder

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return b"PDF bytes", "q1.pdf"
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, [
                "download", "44347",
                "--assignment", "101",
                "--attachment", "1",
                "-o", str(temp_download_dir),
                "--json",
            ])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)
            assert "path" in data
            assert "size_kb" in data
            assert "filename" in data
            assert data["filename"] == "q1.pdf"


# ---------------------------------------------------------------------------
# VAL-ASGN-011: Assignment attachments tracked in manifest
# ---------------------------------------------------------------------------

class TestAssignmentManifestTracking:
    """Test that assignment attachments are recorded in .lighthouse.json."""

    def test_manifest_has_namespaced_keys(self, cli_runner, temp_download_dir):
        """VAL-ASGN-011: Manifest entry uses key pattern assignment_{folderId}_{fileId}."""
        folders = [
            {
                "Id": 101,
                "Name": "Assignment 1",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 1, "FileName": "q1.pdf", "Size": 1024, "Type": "File"},
                ],
            },
        ]

        def get_dropbox_folder_detail(cid, fid):
            for f in folders:
                if f["Id"] == fid:
                    return f
            return None

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return b"Content", "q1.pdf"
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", return_value={"Modules": []}):

            result = cli_runner.invoke(cli, [
                "download", "44347",
                "--include-assignments",
                "-o", str(temp_download_dir),
            ])

            assert result.exit_code == 0, f"exit={result.exit_code}"
            course_dir = temp_download_dir / "Signals & Systems-44347"
            manifest_path = course_dir / MANIFEST_FILENAME
            assert manifest_path.exists(), "Manifest not created"

            manifest_data = json.loads(manifest_path.read_text())
            # Should have namespaced key
            keys = list(manifest_data.keys())
            assert any(k.startswith("assignment_101_1") for k in keys), f"No namespaced key found in {keys}"
            entry = manifest_data[keys[0]]
            assert "sha256" in entry
            assert "filename" in entry
            assert "size" in entry
            assert "downloaded_at" in entry


# ---------------------------------------------------------------------------
# VAL-ASGN-012: Non-fatal download failures
# ---------------------------------------------------------------------------

class TestAssignmentDownloadFailures:
    """Test that individual attachment download failures are non-fatal."""

    def test_attachment_failure_is_non_fatal(self, cli_runner, temp_download_dir):
        """VAL-ASGN-012: FAILED attachment logged, remaining attachments continue."""
        folders = [
            {
                "Id": 101,
                "Name": "Assignment 1",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 1, "FileName": "q1.pdf", "Size": 1024, "Type": "File"},
                    {"Id": 2, "FileName": "q2.pdf", "Size": 2048, "Type": "File"},
                ],
            },
        ]

        def get_dropbox_folder_detail(cid, fid):
            for f in folders:
                if f["Id"] == fid:
                    return f
            return None

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return b"Success content", "q1.pdf"
            if fid == 101 and att_id == 2:
                raise Exception("Network error: connection refused")
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", return_value={"Modules": []}):

            result = cli_runner.invoke(cli, [
                "download", "44347",
                "--include-assignments",
                "-o", str(temp_download_dir),
            ])

            # Should complete (not crash) despite failure
            assert result.exit_code == 1, f"Expected exit 1 for partial failure"
            course_dir = temp_download_dir / "Signals & Systems-44347"
            # q1 should be saved
            assert (course_dir / "Assignments" / "Assignment 1" / "q1.pdf").exists()
            # q2 should NOT exist (failed)
            assert not (course_dir / "Assignments" / "Assignment 1" / "q2.pdf").exists()
            # Error message should be present
            assert "FAILED" in result.output or "error" in result.output.lower()


# ---------------------------------------------------------------------------
# VAL-ASGN-013: Missing Content-Disposition fallback
# ---------------------------------------------------------------------------

class TestMissingContentDisposition:
    """Test attachment download with missing Content-Disposition header."""

    def test_missing_content_disposition_fallback(self, cli_runner, temp_download_dir):
        """VAL-ASGN-013: Fallback filename attachment_{fileId} when Content-Disposition missing."""
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "DueDate": "2026-05-20T23:59:00Z",
            "Attachments": [
                {"Id": 999, "FileName": "unknown.pdf", "Size": 1024, "Type": "File"},
            ],
        }

        def get_dropbox_folder_detail(cid, fid):
            return folder

        def download_attachment(cid, fid, att_id):
            # Simulate no Content-Disposition: return empty filename
            if fid == 101 and att_id == 999:
                return b"Content", ""
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, [
                "download", "44347",
                "--assignment", "101",
                "--attachment", "999",
                "-o", str(temp_download_dir),
            ])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dir = temp_download_dir / "Signals & Systems-44347"
            # Fallback name should be used
            assert (course_dir / "Assignments" / "Assignment 1" / "attachment_999").exists()


# ---------------------------------------------------------------------------
# VAL-ASGN-014: Duplicate filename handling
# ---------------------------------------------------------------------------

class TestDuplicateFilenameHandling:
    """Test that duplicate filenames within same folder are disambiguated."""

    def test_duplicate_filename_within_folder_disambiguated(self, cli_runner, temp_download_dir):
        """VAL-ASGN-014: Second file with same name gets _1 suffix."""
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "DueDate": "2026-05-20T23:59:00Z",
            "Attachments": [
                {"Id": 1, "FileName": "solutions.pdf", "Size": 1024, "Type": "File"},
                {"Id": 2, "FileName": "solutions.pdf", "Size": 2048, "Type": "File"},
            ],
        }

        def get_dropbox_folder_detail(cid, fid):
            return folder

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return b"Content A", "solutions.pdf"
            if fid == 101 and att_id == 2:
                return b"Content B", "solutions.pdf"
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=[folder]), \
             patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", return_value={"Modules": []}):

            result = cli_runner.invoke(cli, [
                "download", "44347",
                "--include-assignments",
                "-o", str(temp_download_dir),
            ])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            course_dir = temp_download_dir / "Signals & Systems-44347"
            folder_dir = course_dir / "Assignments" / "Assignment 1"
            # Both files should exist with disambiguation
            assert (folder_dir / "solutions.pdf").exists()
            assert (folder_dir / "solutions_1.pdf").exists()
            # Contents should be different
            assert (folder_dir / "solutions.pdf").read_bytes() == b"Content A"
            assert (folder_dir / "solutions_1.pdf").read_bytes() == b"Content B"


# ---------------------------------------------------------------------------
# VAL-ASGN-015 & VAL-ASGN-016: Sync detects new/updated assignment attachments
# ---------------------------------------------------------------------------

class TestSyncAssignmentAttachments:
    """Test sync with --include-assignments detects new and updated attachments."""

    def test_sync_detects_new_attachment(self, cli_runner, temp_download_dir):
        """VAL-ASGN-015: Sync downloads new attachment not in manifest."""
        content_1 = b"Content 1"
        content_2 = b"Content 2"
        folders = [
            {
                "Id": 101,
                "Name": "Assignment 1",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 1, "FileName": "q1.pdf", "Size": len(content_1), "Type": "File"},
                    {"Id": 2, "FileName": "q2.pdf", "Size": len(content_2), "Type": "File"},
                ],
            },
        ]

        # Pre-seed manifest with only file 1
        course_dir = temp_download_dir / "Signals & Systems-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        import hashlib
        manifest_path.write_text(json.dumps({
            "assignment_101_1": {
                "sha256": hashlib.sha256(content_1).hexdigest(),
                "filename": "q1.pdf",
                "size": len(content_1),
                "downloaded_at": "2026-05-01T00:00:00Z",
                "last_modified": "2026-05-01T00:00:00Z",
            }
        }))

        def get_dropbox_folder_detail(cid, fid):
            return folders[0]

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return content_1, "q1.pdf"
            if fid == 101 and att_id == 2:
                return content_2, "q2.pdf"
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", return_value={"Modules": []}):

            result = cli_runner.invoke(cli, [
                "sync", "44347",
                "--include-assignments",
                "-o", str(temp_download_dir),
                "--json",
            ])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)
            # New attachment downloaded
            assert len(data.get("assignments_downloaded", [])) == 1, f"Expected 1 new, got {data.get('assignments_downloaded')}"
            # q1 skipped (already in manifest)
            assert len(data.get("assignments_skipped", [])) == 1, f"Expected 1 skipped, got {data.get('assignments_skipped')}"
            # q2 on disk
            assert (course_dir / "Assignments" / "Assignment 1" / "q2.pdf").exists()

    def test_sync_detects_updated_attachment(self, cli_runner, temp_download_dir):
        """VAL-ASGN-016: Sync re-downloads attachment whose size/metadata changed."""
        folders = [
            {
                "Id": 101,
                "Name": "Assignment 1",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 1, "FileName": "q1.pdf", "Size": 9999, "Type": "File"},  # Size changed
                ],
            },
        ]

        course_dir = temp_download_dir / "Signals & Systems-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        import hashlib
        # Old content hash
        old_hash = hashlib.sha256(b"Old content").hexdigest()
        manifest_path.write_text(json.dumps({
            "assignment_101_1": {
                "sha256": old_hash,
                "filename": "q1.pdf",
                "size": 11,
                "downloaded_at": "2026-05-01T00:00:00Z",
                "last_modified": "2026-05-01T00:00:00Z",
            }
        }))

        def get_dropbox_folder_detail(cid, fid):
            return folders[0]

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return b"New content here", "q1.pdf"
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", return_value={"Modules": []}):

            result = cli_runner.invoke(cli, [
                "sync", "44347",
                "--include-assignments",
                "-o", str(temp_download_dir),
                "--json",
            ])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)
            # Should detect size changed → re-download
            assert len(data.get("assignments_updated", [])) == 1, f"Expected 1 updated, got {data.get('assignments_updated')}"
            # File should have new content
            content = (course_dir / "Assignments" / "Assignment 1" / "q1.pdf").read_bytes()
            assert content == b"New content here"


# ---------------------------------------------------------------------------
# VAL-ASGN-017: Sync without --include-assignments skips assignments
# ---------------------------------------------------------------------------

class TestSyncWithoutIncludeAssignments:
    """Test that sync without --include-assignments skips assignment processing."""

    def test_sync_without_include_assignments_skips_assignments(self, cli_runner, temp_download_dir):
        """VAL-ASGN-017: Default sync skips assignment attachments."""
        folders = [
            {
                "Id": 101,
                "Name": "Assignment 1",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 1, "FileName": "q1.pdf", "Size": 1024, "Type": "File"},
                ],
            },
        ]

        course_dir = temp_download_dir / "Signals & Systems-44347"
        course_dir.mkdir(parents=True)

        # No dropbox API should be called without --include-assignments
        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders) as mockFolders, \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", return_value={"Modules": []}):

            result = cli_runner.invoke(cli, [
                "sync", "44347",
                "-o", str(temp_download_dir),
            ])

            assert result.exit_code == 0, f"exit={result.exit_code}"
            # dropbox API should NOT have been called
            mockFolders.assert_not_called()


# ---------------------------------------------------------------------------
# VAL-ASGN-020: Special characters in attachment filenames
# ---------------------------------------------------------------------------

class TestSpecialCharacterFilenames:
    """Test attachment filenames with special characters are sanitized."""

    def test_special_characters_sanitized(self, cli_runner, temp_download_dir):
        """VAL-ASGN-020: Attachment filename passed through _sanitize_filename."""
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "DueDate": "2026-05-20T23:59:00Z",
            "Attachments": [
                {"Id": 1, "FileName": "Q1%20Solutions.pdf", "Size": 1024, "Type": "File"},
            ],
        }

        def get_dropbox_folder_detail(cid, fid):
            return folder

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return b"Content", "Q1%20Solutions.pdf"
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, [
                "download", "44347",
                "--assignment", "101",
                "--attachment", "1",
                "-o", str(temp_download_dir),
            ])

            assert result.exit_code == 0, f"exit={result.exit_code}"
            course_dir = temp_download_dir / "Signals & Systems-44347"
            # %20 should be decoded to space
            assert (course_dir / "Assignments" / "Assignment 1" / "Q1 Solutions.pdf").exists()


# ---------------------------------------------------------------------------
# VAL-ASGN-021: Large attachment download
# ---------------------------------------------------------------------------

class TestLargeAttachmentDownload:
    """Test large attachment files download without timeout."""

    def test_large_attachment_download(self, cli_runner, temp_download_dir):
        """VAL-ASGN-021: Large attachment (multi-MB) downloads completely."""
        # Create a large content (simulate multi-MB)
        large_content = b"X" * (5 * 1024 * 1024)  # 5 MB

        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "DueDate": "2026-05-20T23:59:00Z",
            "Attachments": [
                {"Id": 1, "FileName": "large_video.mp4", "Size": len(large_content), "Type": "File"},
            ],
        }

        def get_dropbox_folder_detail(cid, fid):
            return folder

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return large_content, "large_video.mp4"
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=get_dropbox_folder_detail), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]):

            result = cli_runner.invoke(cli, [
                "download", "44347",
                "--assignment", "101",
                "--attachment", "1",
                "-o", str(temp_download_dir),
            ])

            assert result.exit_code == 0, f"exit={result.exit_code}"
            course_dir = temp_download_dir / "Signals & Systems-44347"
            saved_file = course_dir / "Assignments" / "Assignment 1" / "large_video.mp4"
            assert saved_file.exists()
            assert len(saved_file.read_bytes()) == len(large_content)


# ---------------------------------------------------------------------------
# VAL-CROSS-006: Sync detects new assignment attachments
# ---------------------------------------------------------------------------

class TestCrossAssignmentSync:
    """Test cross-area flow: sync detects new assignment attachments after initial download."""

    def test_sync_after_initial_download_detects_new_attachments(self, cli_runner, temp_download_dir):
        """VAL-CROSS-006: After initial download, new attachments in remote detected by sync."""
        # Content bytes that match the actual download_attachment return values
        content_1 = b"Content 1"
        content_2 = b"Content 2"

        # Initial download state
        folders_initial = [
            {
                "Id": 101,
                "Name": "Assignment 1",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 1, "FileName": "q1.pdf", "Size": len(content_1), "Type": "File"},
                ],
            },
        ]

        # New state: professor added q2.pdf
        folders_updated = [
            {
                "Id": 101,
                "Name": "Assignment 1",
                "DueDate": "2026-05-20T23:59:00Z",
                "Attachments": [
                    {"Id": 1, "FileName": "q1.pdf", "Size": len(content_1), "Type": "File"},
                    {"Id": 2, "FileName": "q2.pdf", "Size": len(content_2), "Type": "File"},
                ],
            },
        ]

        course_dir = temp_download_dir / "Signals & Systems-44347"
        course_dir.mkdir(parents=True)
        manifest_path = course_dir / MANIFEST_FILENAME
        import hashlib
        manifest_path.write_text(json.dumps({
            "assignment_101_1": {
                "sha256": hashlib.sha256(content_1).hexdigest(),
                "filename": "q1.pdf",
                "size": len(content_1),
                "downloaded_at": "2026-05-01T00:00:00Z",
                "last_modified": "2026-05-01T00:00:00Z",
            }
        }))
        # Write the existing file
        (course_dir / "Assignments" / "Assignment 1").mkdir(parents=True)
        (course_dir / "Assignments" / "Assignment 1" / "q1.pdf").write_bytes(content_1)

        call_count = [0]

        def get_dropbox_folders(cid):
            call_count[0] += 1
            if call_count[0] == 1:
                return folders_initial
            return folders_updated

        def download_attachment(cid, fid, att_id):
            if fid == 101 and att_id == 1:
                return content_1, "q1.pdf"
            if fid == 101 and att_id == 2:
                return content_2, "q2.pdf"
            raise Exception("Not found")

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders_updated), \
             patch.object(LighthouseClient, "get_dropbox_folder_detail", return_value=folders_updated[0]), \
             patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", return_value={"Modules": []}):

            result = cli_runner.invoke(cli, [
                "sync", "44347",
                "--include-assignments",
                "-o", str(temp_download_dir),
                "--json",
            ])

            assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
            data = json.loads(result.output)
            assert len(data.get("assignments_downloaded", [])) == 1, f"Expected 1 new, got {data.get('assignments_downloaded')}"
            assert (course_dir / "Assignments" / "Assignment 1" / "q2.pdf").exists()
            # q1 same size as before, should be skipped (not updated)
            assert len(data.get("assignments_updated", [])) == 0, f"Expected 0 updated, got {data.get('assignments_updated')}"
            assert len(data.get("assignments_skipped", [])) == 1
