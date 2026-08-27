"""Tests for assignment attachment downloading (VAL-ASGN-009 – VAL-ASGN-021, VAL-CROSS-005, VAL-CROSS-006)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.assignments import (
    _attachment_error,
    _manifest_attachment_path,
    download_for_course,
    download_single_attachment,
    sync_for_course,
)
from lighthouse_cli.cli import cli
from lighthouse_cli.manifest import MANIFEST_FILENAME, Manifest
from lighthouse_cli.manifest import compute_sha256


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def temp_download_dir(tmp_path: Path) -> Path:
    d = tmp_path / "downloads"
    d.mkdir()
    return d


def test_manifest_attachment_path_rejects_normalized_traversal(tmp_path: Path) -> None:
    """A recorded attachment path cannot escape the Assignments subtree."""
    course_dir = tmp_path / "course"
    course_dir.mkdir()

    assert _manifest_attachment_path(
        course_dir,
        {"path": "Assignments/../Mod/file.pdf"},
    ) is None


def test_attachment_error_redacts_untrusted_message(capsys) -> None:
    sentinel = "BODY_SENTINEL"

    rc = _attachment_error(f"response_body={sentinel}", json_output=True)

    captured = capsys.readouterr()
    assert rc == 1
    assert sentinel not in captured.out
    assert sentinel not in captured.err
    assert json.loads(captured.out)["error"]


def test_bulk_attachment_error_redacts_url_and_query(tmp_path: Path) -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.side_effect = RuntimeError(
        "request failed: https://example.invalid/dropbox?token=TOKEN_SENTINEL"
    )

    downloaded, errors = download_for_course(client, 44347, tmp_path, Manifest())

    assert downloaded == []
    assert errors
    assert "TOKEN_SENTINEL" not in errors[0]["error"]
    assert "https://" not in errors[0]["error"]


@pytest.mark.parametrize("bad_folders", [None, {"Id": 1}, "not-a-list"])
def test_bulk_attachment_invalid_folder_shape_fails_closed(tmp_path: Path, bad_folders) -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = bad_folders

    downloaded, errors = download_for_course(client, 44347, tmp_path, Manifest())

    assert downloaded == []
    assert errors and errors[0]["type"] == "assignment_list"


def test_bulk_attachment_invalid_element_preserves_valid_sibling(tmp_path: Path) -> None:
    folder = {
        "Id": 101,
        "Name": "Assignment 1",
        "Attachments": [None, {"Id": 1, "FileName": "q1.pdf", "Size": 5, "Type": "File"}],
    }
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [folder]
    client.download_attachment.return_value = (b"fresh", "q1.pdf")

    downloaded, errors = download_for_course(client, 44347, tmp_path, Manifest())

    assert len(downloaded) == 1
    assert errors and errors[0]["type"] == "assignment_data"


def test_bulk_attachment_invalid_collection_preserves_valid_folder(tmp_path: Path) -> None:
    folders = [
        {"Id": 100, "Name": "Malformed", "Attachments": None},
        {"Id": 101, "Name": "Valid", "Attachments": [
            {"Id": 1, "FileName": "q1.pdf", "Size": 5, "Type": "File"},
        ]},
    ]
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = folders
    client.download_attachment.return_value = (b"fresh", "q1.pdf")

    downloaded, errors = download_for_course(client, 44347, tmp_path, Manifest())

    assert len(downloaded) == 1
    assert any(error["folder_id"] == 100 for error in errors)


@pytest.mark.parametrize("bad_id", [True, 1.5, 0, -1, "../../evil", None])
def test_bulk_download_rejects_invalid_folder_id_without_followup_calls(
    tmp_path: Path, bad_id,
) -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [{
        "Id": bad_id,
        "Name": "Malformed",
        "Attachments": [{"Id": 1, "FileName": "q1.pdf", "Size": 5, "Type": "File"}],
    }]

    downloaded, errors = download_for_course(client, 44347, tmp_path, Manifest())

    assert downloaded == []
    assert errors and errors[0]["type"] == "assignment_data"
    client.get_dropbox_folder_detail.assert_not_called()
    client.download_attachment.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad_id", [True, 1.5, 0, -1, "../../evil", None])
def test_bulk_sync_rejects_invalid_folder_id_without_followup_calls(
    tmp_path: Path, bad_id,
) -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [{
        "Id": bad_id,
        "Name": "Malformed",
        "Attachments": [{"Id": 1, "FileName": "q1.pdf", "Size": 5, "Type": "File"}],
    }]

    downloaded, skipped, updated, errors = sync_for_course(
        client, 44347, tmp_path, Manifest(),
    )

    assert downloaded == [] and skipped == [] and updated == []
    assert errors and errors[0]["type"] == "assignment_data"
    client.get_dropbox_folder_detail.assert_not_called()
    client.download_attachment.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad_id", [True, 1.5, 0, -1, "../../evil", None])
def test_bulk_download_rejects_invalid_attachment_id_preserving_valid_sibling(
    tmp_path: Path, bad_id,
) -> None:
    folder = {
        "Id": 101,
        "Name": "Assignment 1",
        "Attachments": [
            {"Id": bad_id, "FileName": "bad.pdf", "Size": 3, "Type": "File"},
            {"Id": 1, "FileName": "q1.pdf", "Size": 5, "Type": "File"},
        ],
    }
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [folder]
    client.download_attachment.return_value = (b"fresh", "q1.pdf")

    downloaded, errors = download_for_course(client, 44347, tmp_path, Manifest())

    assert len(downloaded) == 1
    assert errors and errors[0]["type"] == "assignment_data"
    client.get_dropbox_folder_detail.assert_not_called()
    client.download_attachment.assert_called_once_with(44347, 101, 1)
    assert not (tmp_path / "Assignments" / "Assignment 1" / "bad.pdf").exists()


def test_bulk_download_missing_assignment_selector_is_an_error_without_writes(
    tmp_path: Path,
) -> None:
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [{
        "Id": 101,
        "Name": "Assignment 1",
        "Attachments": [{"Id": 1, "FileName": "q1.pdf", "Size": 5, "Type": "File"}],
    }]

    downloaded, errors = download_for_course(
        client, 44347, tmp_path, Manifest(), folder_ids=[999],
    )

    assert downloaded == []
    assert errors == [{
        "error": "Requested assignment folder was not found.",
        "type": "assignment_not_found",
    }]
    client.get_dropbox_folder_detail.assert_not_called()
    client.download_attachment.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad_id", [True, 1.5, 0, -1, "../../evil", None])
def test_bulk_sync_rejects_invalid_attachment_id_preserving_valid_sibling(
    tmp_path: Path, bad_id,
) -> None:
    folder = {
        "Id": 101,
        "Name": "Assignment 1",
        "Attachments": [
            {"Id": bad_id, "FileName": "bad.pdf", "Size": 3, "Type": "File"},
            {"Id": 1, "FileName": "q1.pdf", "Size": 5, "Type": "File"},
        ],
    }
    client = Mock(spec=LighthouseClient)
    client.get_dropbox_folders.return_value = [folder]
    client.download_attachment.return_value = (b"fresh", "q1.pdf")

    downloaded, skipped, updated, errors = sync_for_course(
        client, 44347, tmp_path, Manifest(),
    )

    assert len(downloaded) == 1
    assert skipped == [] and updated == []
    assert errors and errors[0]["type"] == "assignment_data"
    client.get_dropbox_folder_detail.assert_not_called()
    client.download_attachment.assert_called_once_with(44347, 101, 1)
    assert not (tmp_path / "Assignments" / "Assignment 1" / "bad.pdf").exists()


def test_single_attachment_corrupt_manifest_returns_json_error(
    tmp_path: Path, capsys
) -> None:
    client = Mock(spec=LighthouseClient)
    client.get_courses.return_value = [{"OrgUnitId": 44347, "Name": "Course"}]
    client.get_dropbox_folder_detail.return_value = {"Id": 101, "Name": "Assignment"}
    course_dir = tmp_path / "Course-44347"
    course_dir.mkdir()
    (course_dir / MANIFEST_FILENAME).write_text("not-json{")

    rc = download_single_attachment(client, 44347, 101, 1, tmp_path, True)

    captured = capsys.readouterr()
    assert rc == 1
    assert "error" in json.loads(captured.out)
    assert "Assignment attachment download failed." in captured.err
    client.download_attachment.assert_not_called()


# ---------------------------------------------------------------------------
# VAL-ASGN-009: Download all assignment attachments for a course
# ---------------------------------------------------------------------------

class TestDownloadAllAssignmentAttachments:
    """Test lighthouse download COURSE_ID --include-assignments."""

    def test_download_json_redacts_secret_shaped_server_filename(
        self, cli_runner, temp_download_dir,
    ):
        sentinel = "ATTACHMENT_SECRET_SENTINEL"
        folder_sentinel = "FOLDER_SECRET_SENTINEL"
        folders = [{
            "Id": 101,
            "Name": f"password={folder_sentinel}",
            "Attachments": [{"Id": 1, "FileName": "listed.pdf", "Size": 4, "Type": "File"}],
        }]

        with patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders), \
             patch.object(LighthouseClient, "download_attachment", return_value=(b"body", f"password={sentinel}.pdf")), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", return_value={"Modules": []}):
            result = cli_runner.invoke(cli, [
                "download", "44347", "--include-assignments",
                "-o", str(temp_download_dir), "--json",
            ])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["assignments_downloaded"][0]["filename"] == "attachment_1.pdf"
        assert sentinel not in result.stdout + result.stderr
        assert folder_sentinel not in result.stdout + result.stderr
        assert "Folder-101" in payload["assignments_downloaded"][0]["path"]
        output_path = Path(payload["folder"]) / payload["assignments_downloaded"][0]["path"]
        assert output_path.read_bytes() == b"body"
        manifest = json.loads((Path(payload["folder"]) / MANIFEST_FILENAME).read_text())
        assert manifest["assignment_101_1"]["filename"] == "attachment_1.pdf"
        assert sentinel not in json.dumps(manifest)
        assert folder_sentinel not in json.dumps(manifest)
        assert output_path.name == "attachment_1.pdf"
        assert output_path.exists()

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
            data = json.loads(result.stdout)
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
            assert result.exit_code == 1, "Expected exit 1 for partial failure"
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
                "path": "Assignments/Assignment 1/q1.pdf",
                "size": len(content_1),
                "downloaded_at": "2026-05-01T00:00:00Z",
                "last_modified": "2026-05-01T00:00:00Z",
            }
        }))
        (course_dir / "Assignments" / "Assignment 1").mkdir(parents=True)
        (course_dir / "Assignments" / "Assignment 1" / "q1.pdf").write_bytes(content_1)

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
            data = json.loads(result.stdout)
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
            data = json.loads(result.stdout)
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
                "path": "Assignments/Assignment 1/q1.pdf",
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
            data = json.loads(result.stdout)
            assert len(data.get("assignments_downloaded", [])) == 1, f"Expected 1 new, got {data.get('assignments_downloaded')}"
            assert (course_dir / "Assignments" / "Assignment 1" / "q2.pdf").exists()
            # q1 same size as before, should be skipped (not updated)
            assert len(data.get("assignments_updated", [])) == 0, f"Expected 0 updated, got {data.get('assignments_updated')}"
            assert len(data.get("assignments_skipped", [])) == 1


class TestSyncDropboxAttachmentMetadata:
    """Test attachment reuse between the list and detail Dropbox endpoints."""

    def test_populated_list_attachments_skip_detail_request(self, tmp_path: Path):
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [
                {"Id": 1, "FileName": "q1.pdf", "Size": 7, "Type": "File"},
            ],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.return_value = (b"content", "q1.pdf")

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, tmp_path, Manifest()
        )

        assert len(downloaded) == 1
        assert skipped == []
        assert updated == []
        assert errors == []
        client.get_dropbox_folder_detail.assert_not_called()

    def test_empty_list_attachments_skip_detail_request(self, tmp_path: Path):
        folder = {"Id": 102, "Name": "Empty Assignment", "Attachments": []}
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]

        result = sync_for_course(client, 44347, tmp_path, Manifest())

        assert result == ([], [], [], [])
        client.get_dropbox_folder_detail.assert_not_called()

    def test_missing_list_attachments_fetch_detail_once(self, tmp_path: Path):
        folder = {"Id": 103, "Name": "Assignment 3"}
        detail = {
            **folder,
            "Attachments": [
                {"Id": 3, "FileName": "q3.pdf", "Size": 7, "Type": "File"},
            ],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.get_dropbox_folder_detail.return_value = detail
        client.download_attachment.return_value = (b"content", "q3.pdf")

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, tmp_path, Manifest()
        )

        assert len(downloaded) == 1
        assert skipped == []
        assert updated == []
        assert errors == []
        client.get_dropbox_folder_detail.assert_called_once_with(44347, 103)

    def test_bulk_download_missing_list_attachments_fetches_detail(self, tmp_path: Path):
        folder = {"Id": 103, "Name": "Assignment 3"}
        detail = {
            **folder,
            "Attachments": [
                {"Id": 3, "FileName": "q3.pdf", "Size": 7, "Type": "File"},
            ],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.get_dropbox_folder_detail.return_value = detail
        client.download_attachment.return_value = (b"content", "q3.pdf")

        downloaded, errors = download_for_course(
            client, 44347, tmp_path, Manifest()
        )

        assert len(downloaded) == 1
        assert errors == []
        client.get_dropbox_folder_detail.assert_called_once_with(44347, 103)

    def test_bulk_download_deduplicates_folder_ids_first_record_wins(
        self, tmp_path: Path,
    ):
        folders = [
            {"Id": 101, "Name": "First assignment"},
            {
                "Id": 101,
                "Name": "Conflicting duplicate",
                "Attachments": [{"Id": 2, "FileName": "second.pdf", "Size": 6, "Type": "File"}],
            },
        ]
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = folders
        client.get_dropbox_folder_detail.return_value = {
            "Id": 101,
            "Name": "First assignment detail",
            "Attachments": [{"Id": 1, "FileName": "first.pdf", "Size": 5, "Type": "File"}],
        }
        client.download_attachment.return_value = (b"first", "first.pdf")

        downloaded, errors = download_for_course(
            client, 44347, tmp_path, Manifest(),
        )

        assert errors == []
        assert [entry["folder_id"] for entry in downloaded] == [101]
        assert [entry["file_id"] for entry in downloaded] == [1]
        assert client.get_dropbox_folder_detail.call_args_list == [
            ((44347, 101),),
        ]
        client.download_attachment.assert_called_once_with(44347, 101, 1)
        assert (tmp_path / "Assignments" / "First assignment detail" / "first.pdf").exists()
        assert not (tmp_path / "Assignments" / "Conflicting duplicate" / "second.pdf").exists()

    def test_sync_deduplicates_folder_ids_first_record_wins(self, tmp_path: Path):
        folders = [
            {
                "Id": 101,
                "Name": "First assignment",
                "Attachments": [{"Id": 1, "FileName": "first.pdf", "Size": 5, "Type": "File"}],
            },
            {
                "Id": 101,
                "Name": "Conflicting duplicate",
                "Attachments": [{"Id": 2, "FileName": "second.pdf", "Size": 6, "Type": "File"}],
            },
        ]
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = folders
        client.download_attachment.return_value = (b"first", "first.pdf")
        manifest = Manifest()

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, tmp_path, manifest,
        )

        assert skipped == []
        assert updated == []
        assert errors == []
        assert [entry["folder_id"] for entry in downloaded] == [101]
        assert [entry["file_id"] for entry in downloaded] == [1]
        client.download_attachment.assert_called_once_with(44347, 101, 1)
        assert set(manifest.entries) == {"assignment_101_1"}
        assert not (tmp_path / "Assignments" / "Conflicting duplicate").exists()

    def test_bulk_download_allows_valid_duplicate_after_malformed_first_record(
        self, tmp_path: Path,
    ):
        folders = [
            {"Id": 101, "Name": "Malformed first"},
            {
                "Id": 101,
                "Name": "Valid second",
                "Attachments": [{"Id": 2, "FileName": "second.pdf", "Size": 6, "Type": "File"}],
            },
        ]
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = folders
        client.get_dropbox_folder_detail.return_value = {
            "Id": 101,
            "Name": "Malformed detail",
            "Attachments": None,
        }
        client.download_attachment.return_value = (b"second", "second.pdf")

        downloaded, errors = download_for_course(
            client, 44347, tmp_path, Manifest(),
        )

        assert len(downloaded) == 1
        assert downloaded[0]["file_id"] == 2
        assert errors == [{
            "folder_id": 101,
            "error": "Assignment response has an invalid shape.",
            "type": "assignment_data",
        }]
        client.get_dropbox_folder_detail.assert_called_once_with(44347, 101)
        client.download_attachment.assert_called_once_with(44347, 101, 2)

    def test_sync_allows_valid_duplicate_after_malformed_first_record(
        self, tmp_path: Path,
    ):
        folders = [
            {"Id": 101, "Name": "Malformed first"},
            {
                "Id": 101,
                "Name": "Valid second",
                "Attachments": [{"Id": 2, "FileName": "second.pdf", "Size": 6, "Type": "File"}],
            },
        ]
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = folders
        client.get_dropbox_folder_detail.return_value = {
            "Id": 101,
            "Name": "Malformed detail",
            "Attachments": None,
        }
        client.download_attachment.return_value = (b"second", "second.pdf")

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, tmp_path, Manifest(),
        )

        assert len(downloaded) == 1
        assert downloaded[0]["file_id"] == 2
        assert skipped == []
        assert updated == []
        assert errors == [{
            "folder_id": 101,
            "error": "Assignment response has an invalid shape.",
            "type": "assignment_data",
        }]
        client.get_dropbox_folder_detail.assert_called_once_with(44347, 101)
        client.download_attachment.assert_called_once_with(44347, 101, 2)

    def test_bulk_download_rejects_mismatched_detail_id_before_attachment_write(
        self, tmp_path: Path,
    ):
        folder = {"Id": 101, "Name": "Assignment 1"}
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.get_dropbox_folder_detail.return_value = {
            "Id": 202,
            "Name": "Wrong assignment",
            "Attachments": [{"Id": 1, "FileName": "wrong.pdf", "Size": 5, "Type": "File"}],
        }
        client.download_attachment.return_value = (b"must not write", "wrong.pdf")

        downloaded, errors = download_for_course(
            client, 44347, tmp_path, Manifest(), folder_ids=[101],
        )

        assert downloaded == []
        assert errors == [{
            "folder_id": 101,
            "error": "Assignment record has an invalid identifier.",
            "type": "assignment_data",
        }]
        client.get_dropbox_folder_detail.assert_called_once_with(44347, 101)
        client.download_attachment.assert_not_called()
        assert list(tmp_path.iterdir()) == []

    def test_single_attachment_rejects_mismatched_detail_id_before_write(
        self, tmp_path: Path, capsys,
    ):
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folder_detail.return_value = {
            "Id": 202,
            "Name": "Wrong assignment",
            "Attachments": [{"Id": 1, "FileName": "wrong.pdf", "Size": 5, "Type": "File"}],
        }
        client.download_attachment.return_value = (b"must not write", "wrong.pdf")

        rc = download_single_attachment(client, 44347, 101, 1, tmp_path, True)

        captured = capsys.readouterr()
        assert rc == 1
        assert json.loads(captured.out) == {
            "error": "Assignment record has an invalid identifier.",
            "type": "assignment_data",
        }
        assert "202" not in captured.out + captured.err
        client.get_dropbox_folder_detail.assert_called_once_with(44347, 101)
        client.download_attachment.assert_not_called()
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "body",
        ["not bytes", bytearray(b"not bytes"), object()],
        ids=["str", "bytearray", "object"],
    )
    def test_bulk_download_rejects_non_bytes_body_before_write(
        self, tmp_path: Path, body,
    ):
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [{"Id": 1, "FileName": "wrong.pdf", "Size": 5, "Type": "File"}],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.return_value = (body, "wrong.pdf")

        downloaded, errors = download_for_course(
            client, 44347, tmp_path, Manifest(), folder_ids=[101],
        )

        assert downloaded == []
        assert errors == [{
            "folder_id": 101,
            "file_id": 1,
            "error": "Assignment response has an invalid shape.",
            "type": "assignment_data",
        }]
        client.download_attachment.assert_called_once_with(44347, 101, 1)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "body",
        ["not bytes", bytearray(b"not bytes"), object()],
        ids=["str", "bytearray", "object"],
    )
    def test_single_attachment_rejects_non_bytes_body_before_write(
        self, tmp_path: Path, body, capsys,
    ):
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folder_detail.return_value = {
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [{"Id": 1, "FileName": "wrong.pdf", "Size": 5, "Type": "File"}],
        }
        client.download_attachment.return_value = (body, "wrong.pdf")

        rc = download_single_attachment(client, 44347, 101, 1, tmp_path, True)

        captured = capsys.readouterr()
        assert rc == 1
        assert json.loads(captured.out) == {
            "error": "Assignment response has an invalid shape.",
            "type": "assignment_data",
        }
        assert "not bytes" not in captured.out + captured.err
        assert "object at" not in captured.out + captured.err
        client.download_attachment.assert_called_once_with(44347, 101, 1)
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "body",
        ["not bytes", bytearray(b"not bytes"), object()],
        ids=["str", "bytearray", "object"],
    )
    def test_sync_rejects_non_bytes_body_before_write(
        self, tmp_path: Path, body,
    ):
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [{"Id": 1, "FileName": "wrong.pdf", "Size": 5, "Type": "File"}],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.return_value = (body, "wrong.pdf")
        manifest = Manifest()

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, tmp_path, manifest,
        )

        assert downloaded == []
        assert skipped == []
        assert updated == []
        assert errors == [{
            "folder_id": 101,
            "file_id": 1,
            "error": "Assignment response has an invalid shape.",
            "type": "assignment_data",
        }]
        client.download_attachment.assert_called_once_with(44347, 101, 1)
        assert manifest.entries == {}
        assert list(tmp_path.iterdir()) == []

    def test_bulk_download_redacts_secret_shaped_server_filename_in_path_and_manifest(
        self, tmp_path: Path,
    ):
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [{"Id": 1, "FileName": "listed.pdf", "Size": 4, "Type": "File"}],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.return_value = (b"body", "password=ATTACHMENT_SECRET.pdf")
        manifest = Manifest()

        downloaded, errors = download_for_course(client, 44347, tmp_path, manifest)

        assert errors == []
        assert len(downloaded) == 1
        assert downloaded[0]["filename"] == "attachment_1.pdf"
        assert "ATTACHMENT_SECRET" not in json.dumps(downloaded)
        manifest_entry = manifest.get("assignment_101_1")
        assert manifest_entry is not None
        assert manifest_entry["filename"] == "attachment_1.pdf"
        assert "ATTACHMENT_SECRET" not in json.dumps(manifest_entry)
        assert (tmp_path / "Assignments" / "Assignment 1" / "attachment_1.pdf").read_bytes() == b"body"
        assert not any("ATTACHMENT_SECRET" in str(path) for path in tmp_path.rglob("*"))
        client.download_attachment.assert_called_once_with(44347, 101, 1)

    def test_sync_redacts_control_shaped_server_filename_in_path_and_manifest(
        self, tmp_path: Path,
    ):
        folder = {
            "Id": 101,
            "Name": "unsafe\x1b[31mFOLDER_SENTINEL",
            "Attachments": [{"Id": 1, "FileName": "listed.pdf", "Size": 4, "Type": "File"}],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.return_value = (b"body", "unsafe\x1b[31m.pdf")
        manifest = Manifest()

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, tmp_path, manifest,
        )

        assert skipped == []
        assert updated == []
        assert errors == []
        assert downloaded[0]["filename"] == "attachment_1.pdf"
        assert manifest.get("assignment_101_1")["filename"] == "attachment_1.pdf"
        assert (tmp_path / "Assignments" / "Folder-101" / "attachment_1.pdf").read_bytes() == b"body"
        assert not any("\x1b" in str(path) or "FOLDER_SENTINEL" in str(path) for path in tmp_path.rglob("*"))
        assert "FOLDER_SENTINEL" not in json.dumps(manifest.entries)
        client.download_attachment.assert_called_once_with(44347, 101, 1)

    @pytest.mark.parametrize(
        "course_name",
        ["password=COURSE_SECRET", "unsafe\x1b[31mcourse"],
        ids=["secret-shaped", "control-shaped"],
    )
    def test_single_attachment_redacts_server_filename_and_course_name(
        self, tmp_path: Path, course_name, capsys,
    ):
        client = Mock(spec=LighthouseClient)
        client.get_enrolled_courses.return_value = [{
            "OrgUnitId": 44347,
            "Name": course_name,
        }]
        client.get_dropbox_folder_detail.return_value = {
            "Id": 101,
            "Name": "password=FOLDER_SECRET",
            "Attachments": [{"Id": 1, "FileName": "listed.pdf", "Size": 4, "Type": "File"}],
        }
        client.download_attachment.return_value = (
            b"body",
            "password=ATTACHMENT_SECRET.pdf",
        )

        rc = download_single_attachment(client, 44347, 101, 1, tmp_path, True)

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert rc == 0
        assert payload["filename"] == "attachment_1.pdf"
        assert "ATTACHMENT_SECRET" not in captured.out + captured.err
        assert "COURSE_SECRET" not in captured.out + captured.err
        assert "\x1b" not in captured.out + captured.err
        assert "Course-44347" in payload["path"]
        output_path = Path(payload["path"])
        assert output_path.read_bytes() == b"body"
        manifest = json.loads((output_path.parents[2] / MANIFEST_FILENAME).read_text())
        assert manifest["assignment_101_1"]["filename"] == "attachment_1.pdf"
        assert "ATTACHMENT_SECRET" not in json.dumps(manifest)
        client.download_attachment.assert_called_once_with(44347, 101, 1)

    def test_assignment_symlink_is_rejected_before_attachment_write(self, tmp_path: Path):
        course_dir = tmp_path / "course"
        outside = tmp_path / "outside"
        course_dir.mkdir()
        outside.mkdir()
        (course_dir / "Assignments").symlink_to(outside, target_is_directory=True)

        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [{
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [
                {"Id": 1, "FileName": "q1.pdf", "Size": 7, "Type": "File"},
            ],
        }]
        client.download_attachment.return_value = (b"content", "q1.pdf")

        downloaded, errors = download_for_course(
            client, 44347, course_dir, Manifest()
        )

        assert downloaded == []
        assert errors and errors[0]["error"]
        assert not (outside / "Assignment 1" / "q1.pdf").exists()

    @pytest.mark.parametrize(
        "forged_component",
        ["password=LOCAL_SECRET", "unsafe\x1b[31m"],
        ids=["secret-shaped", "control-shaped"],
    )
    def test_forged_manifest_label_is_not_skipped_and_is_replaced_safely(
        self, tmp_path: Path, forged_component: str,
    ):
        course_dir = tmp_path / "course"
        forged_dir = course_dir / "Assignments" / forged_component
        forged_dir.mkdir(parents=True)
        content = b"fresh"
        forged_path = forged_dir / "x.pdf"
        forged_path.write_bytes(content)
        manifest = Manifest({
            "assignment_101_1": {
                "sha256": compute_sha256(content),
                "filename": "x.pdf",
                "path": f"Assignments/{forged_component}/x.pdf",
                "size": len(content),
                "downloaded_at": "2026-05-01T00:00:00Z",
                "last_modified": "",
            },
        })
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [{"Id": 1, "FileName": "x.pdf", "Size": len(content), "Type": "File"}],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.return_value = (content, "x.pdf")

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, course_dir, manifest,
        )

        assert errors == []
        assert skipped == []
        assert len(updated) == 1
        assert updated[0]["path"] == "Assignments/Assignment 1/x.pdf"
        assert forged_component not in json.dumps(updated)
        manifest_entry = manifest.get("assignment_101_1")
        assert manifest_entry is not None
        assert manifest_entry["path"] == "Assignments/Assignment 1/x.pdf"
        assert forged_component not in json.dumps(manifest.entries)
        assert (course_dir / updated[0]["path"]).read_bytes() == content
        assert forged_path.read_bytes() == content
        client.download_attachment.assert_called_once_with(44347, 101, 1)

    def test_download_replaces_secret_shaped_manifest_path_safely(self, tmp_path: Path):
        course_dir = tmp_path / "course"
        forged_dir = course_dir / "Assignments" / "password=LOCAL_SECRET"
        forged_dir.mkdir(parents=True)
        content = b"fresh"
        forged_path = forged_dir / "x.pdf"
        forged_path.write_bytes(content)
        manifest = Manifest({
            "assignment_101_1": {
                "sha256": compute_sha256(content),
                "filename": "x.pdf",
                "path": "Assignments/password=LOCAL_SECRET/x.pdf",
                "size": len(content),
                "downloaded_at": "2026-05-01T00:00:00Z",
                "last_modified": "",
            },
        })
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [{"Id": 1, "FileName": "x.pdf", "Size": len(content), "Type": "File"}],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.return_value = (content, "x.pdf")

        downloaded, errors = download_for_course(
            client, 44347, course_dir, manifest,
        )

        assert errors == []
        assert len(downloaded) == 1
        assert downloaded[0]["path"] == "Assignments/Assignment 1/x.pdf"
        assert "LOCAL_SECRET" not in json.dumps(downloaded)
        assert "LOCAL_SECRET" not in json.dumps(manifest.entries)
        assert (course_dir / downloaded[0]["path"]).read_bytes() == content
        assert forged_path.read_bytes() == content
        client.download_attachment.assert_called_once_with(44347, 101, 1)

    def test_sync_replaces_cross_folder_manifest_path_without_overwriting_wrong_folder(
        self, tmp_path: Path,
    ):
        course_dir = tmp_path / "course"
        wrong_dir = course_dir / "Assignments" / "Other"
        wrong_dir.mkdir(parents=True)
        old_content = b"old!"
        new_content = b"new!"
        wrong_path = wrong_dir / "evil.pdf"
        wrong_path.write_bytes(old_content)
        manifest = Manifest({
            "assignment_101_1": {
                "sha256": compute_sha256(old_content),
                "filename": "evil.pdf",
                "path": "Assignments/Other/evil.pdf",
                "size": len(old_content),
                "downloaded_at": "2026-05-01T00:00:00Z",
                "last_modified": "",
            },
        })
        folder = {
            "Id": 101,
            "Name": "Folder 101",
            "Attachments": [{"Id": 1, "FileName": "safe.pdf", "Size": len(new_content), "Type": "File"}],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.return_value = (new_content, "safe.pdf")

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, course_dir, manifest,
        )

        assert downloaded == []
        assert skipped == []
        assert errors == []
        assert updated == [{
            "file_id": 1,
            "folder_id": 101,
            "filename": "safe.pdf",
            "path": "Assignments/Folder 101/safe.pdf",
            "size_kb": 0.0,
        }]
        assert wrong_path.read_bytes() == old_content
        assert (course_dir / "Assignments" / "Folder 101" / "safe.pdf").read_bytes() == new_content
        assert manifest.get("assignment_101_1")["path"] == "Assignments/Folder 101/safe.pdf"
        assert "Other/evil.pdf" not in json.dumps(updated)
        assert client.download_attachment.call_args_list == [
            ((44347, 101, 1),),
        ]

    def test_invalid_manifest_path_is_redownloaded_without_escape(self, tmp_path: Path):
        course_dir = tmp_path / "course"
        course_dir.mkdir()
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(b"keep")
        content = b"fresh"
        manifest = Manifest({
            "assignment_101_1": {
                "sha256": compute_sha256(b"stale"),
                "filename": "outside.pdf",
                "path": "Assignments/../../outside.pdf",
                "size": len(b"stale"),
                "downloaded_at": "2026-05-01T00:00:00Z",
                "last_modified": "",
            },
        })
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [{"Id": 1, "FileName": "q1.pdf", "Size": len(content), "Type": "File"}],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.return_value = (content, "q1.pdf")

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, course_dir, manifest,
        )

        assert errors == []
        assert skipped == []
        assert len(updated) == 1
        assert downloaded == []
        assert outside.read_bytes() == b"keep"
        assert updated[0]["path"].startswith("Assignments/")

    def test_same_size_changed_attachment_is_not_skipped(self, tmp_path: Path):
        course_dir = tmp_path / "course"
        folder_dir = course_dir / "Assignments" / "Assignment 1"
        folder_dir.mkdir(parents=True)
        old_content = b"old!"
        new_content = b"new!"
        local_path = folder_dir / "q1.pdf"
        local_path.write_bytes(new_content)
        manifest = Manifest({
            "assignment_101_1": {
                "sha256": compute_sha256(old_content),
                "filename": "q1.pdf",
                "path": "Assignments/Assignment 1/q1.pdf",
                "size": len(old_content),
                "downloaded_at": "2026-05-01T00:00:00Z",
                "last_modified": "",
            },
        })
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [{"Id": 1, "FileName": "q1.pdf", "Size": len(new_content), "Type": "File"}],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.return_value = (new_content, "q1.pdf")

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, course_dir, manifest,
        )

        assert errors == []
        assert skipped == []
        assert len(updated) == 1
        assert downloaded == []
        assert local_path.read_bytes() == new_content

    def test_filename_symlink_is_not_overwritten(self, tmp_path: Path):
        course_dir = tmp_path / "course"
        folder_dir = course_dir / "Assignments" / "Assignment 1"
        folder_dir.mkdir(parents=True)
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(b"keep")
        (folder_dir / "q1.pdf").symlink_to(outside)
        folder = {
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [{"Id": 1, "FileName": "q1.pdf", "Size": 5, "Type": "File"}],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.return_value = (b"fresh", "q1.pdf")

        downloaded, errors = download_for_course(
            client, 44347, course_dir, Manifest(),
        )

        assert errors == []
        assert downloaded[0]["filename"] == "q1_1.pdf"
        assert outside.read_bytes() == b"keep"
        assert (folder_dir / "q1.pdf").is_symlink()

    def test_course_destination_symlink_is_rejected_before_attachment_write(self, tmp_path: Path):
        outside = tmp_path / "outside-course"
        outside.mkdir()
        course_dir = tmp_path / "course-link"
        course_dir.symlink_to(outside, target_is_directory=True)

        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [{
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [{"Id": 1, "FileName": "q1.pdf", "Size": 7, "Type": "File"}],
        }]
        client.download_attachment.return_value = (b"content", "q1.pdf")

        downloaded, errors = download_for_course(
            client, 44347, course_dir, Manifest()
        )

        assert downloaded == []
        assert errors and errors[0]["error"]
        client.download_attachment.assert_not_called()
        assert list(outside.rglob("*")) == []

    def test_assignment_folder_symlink_is_rejected_before_attachment_write(self, tmp_path: Path):
        course_dir = tmp_path / "course"
        course_dir.mkdir()
        outside = tmp_path / "outside-folder"
        outside.mkdir()
        assignments_dir = course_dir / "Assignments"
        assignments_dir.mkdir()
        (assignments_dir / "Assignment 1").symlink_to(outside, target_is_directory=True)

        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [{
            "Id": 101,
            "Name": "Assignment 1",
            "Attachments": [{"Id": 1, "FileName": "q1.pdf", "Size": 7, "Type": "File"}],
        }]
        client.download_attachment.return_value = (b"content", "q1.pdf")

        downloaded, errors = download_for_course(
            client, 44347, course_dir, Manifest()
        )

        assert downloaded == []
        assert errors and errors[0]["error"]
        client.download_attachment.assert_not_called()
        assert list(outside.rglob("*")) == []

    def test_manifest_and_result_keep_disambiguated_path_on_update(
        self, tmp_path: Path,
    ):
        first_content = b"first"
        second_content = b"second"
        updated_second = b"updated second"
        folder = {
            "Id": 104,
            "Name": "Duplicate Assignment",
            "Attachments": [
                {"Id": 1, "FileName": "solutions.pdf", "Size": len(first_content), "Type": "File"},
                {"Id": 2, "FileName": "solutions.pdf", "Size": len(second_content), "Type": "File"},
            ],
        }
        client = Mock(spec=LighthouseClient)
        client.get_dropbox_folders.return_value = [folder]
        client.download_attachment.side_effect = [
            (first_content, "solutions.pdf"),
            (second_content, "solutions.pdf"),
        ]
        manifest = Manifest()

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, tmp_path, manifest
        )

        assert errors == []
        assert skipped == []
        assert [entry["path"] for entry in downloaded] == [
            "Assignments/Duplicate Assignment/solutions.pdf",
            "Assignments/Duplicate Assignment/solutions_1.pdf",
        ]
        assert manifest.get("assignment_104_1")["path"] == downloaded[0]["path"]
        assert manifest.get("assignment_104_2")["path"] == downloaded[1]["path"]

        folder["Attachments"][1]["Size"] = len(updated_second)
        client.download_attachment.side_effect = [(updated_second, "solutions.pdf")]
        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, tmp_path, manifest
        )

        assert downloaded == []
        assert skipped == [{
            "file_id": 1,
            "folder_id": 104,
            "filename": "solutions.pdf",
            "path": "Assignments/Duplicate Assignment/solutions.pdf",
        }]
        assert errors == []
        assert updated[0]["filename"] == "solutions_1.pdf"
        assert updated[0]["path"] == "Assignments/Duplicate Assignment/solutions_1.pdf"
        assert manifest.get("assignment_104_2")["filename"] == "solutions_1.pdf"
        assert manifest.get("assignment_104_2")["path"] == updated[0]["path"]
        assert (
            tmp_path / "Assignments" / "Duplicate Assignment" / "solutions_1.pdf"
        ).read_bytes() == updated_second
        assert not (
            tmp_path / "Assignments" / "Duplicate Assignment" / "solutions_2.pdf"
        ).exists()
        client.get_dropbox_folder_detail.assert_not_called()
