"""Tests for assignment attachment downloading (VAL-ASGN-009 – VAL-ASGN-021, VAL-CROSS-005, VAL-CROSS-006)."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.assignments import (
    _attachment_error,
    _manifest_attachment_path,
    _safe_course_name,
    download_for_course,
    download_single_attachment,
    safe_assignment_folder_name,
    safe_attachment_filename,
    sync_for_course,
)
from lighthouse_cli.cli import cli
from lighthouse_cli.manifest import MANIFEST_FILENAME, Manifest, compute_sha256
from lighthouse_cli.sync_engine import _safe_course_name as sync_safe_course_name

COURSE_DIR = "Signals & Systems-44347"


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def temp_download_dir(tmp_path: Path) -> Path:
    d = tmp_path / "downloads"
    d.mkdir()
    return d


def _att(aid, filename: str, size: int) -> dict:
    return {"Id": aid, "FileName": filename, "Size": size, "Type": "File"}


def _folder(fid, name: str, *attachments) -> dict:
    return {"Id": fid, "Name": name, "Attachments": list(attachments)}


def _entry(content: bytes, filename: str, path: str | None = None) -> dict:
    """Manifest entry recording ``content`` as ``filename`` (at ``path`` if given)."""
    entry = {
        "sha256": compute_sha256(content),
        "filename": filename,
        "size": len(content),
        "downloaded_at": "2026-05-01T00:00:00Z",
        "last_modified": "",
    }
    if path is not None:
        entry["path"] = path
    return entry


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _mock_client(**return_values) -> Mock:
    """Mock(spec=LighthouseClient) whose named methods return the given values."""
    client = Mock(spec=LighthouseClient)
    for name, value in return_values.items():
        getattr(client, name).return_value = value
    return client


def _run_bulk(kind: str, client: Mock, dest: Path, manifest: Manifest | None = None) -> tuple:
    """Run the bulk download or sync for course 44347.

    Returns (downloaded, skipped, updated, errors); download never skips or
    updates, so it reports both as empty lists.
    """
    manifest = Manifest() if manifest is None else manifest
    if kind == "sync":
        return sync_for_course(client, 44347, dest, manifest)
    downloaded, errors = download_for_course(client, 44347, dest, manifest)
    return downloaded, [], [], errors


@contextmanager
def _cli_client(folders: list[dict], files: dict, *, listed: bool = True, toc=None, topics=None):
    """Patch LighthouseClient for course 44347, "Signals & Systems".

    ``folders`` answers folder-detail lookups by id. With ``listed`` it is also
    the folder list, and ``toc`` (default: no modules) is the content TOC.
    ``files`` maps (folder_id, attachment_id) to a download result or to an
    exception to raise; other attachments raise "Not found". ``topics`` maps
    topic ids to topic download results.
    """
    def folder_detail(_cid, fid):
        return next((folder for folder in folders if folder["Id"] == fid), None)

    def download_attachment(_cid, fid, att_id):
        result = files.get((fid, att_id), Exception("Not found"))
        if isinstance(result, Exception):
            raise result
        return result

    def download_topic(_cid, tid):
        return topics[tid]

    patches = [
        patch.object(LighthouseClient, "get_courses", return_value=[
            {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "X"},
        ]),
        patch.object(LighthouseClient, "get_dropbox_folder_detail", side_effect=folder_detail),
        patch.object(LighthouseClient, "download_attachment", side_effect=download_attachment),
    ]
    if listed:
        patches.append(patch.object(LighthouseClient, "get_dropbox_folders", return_value=folders))
        patches.append(patch.object(LighthouseClient, "get_content_toc", return_value=toc or {"Modules": []}))
    if topics:
        patches.append(patch.object(LighthouseClient, "download_topic_file", side_effect=download_topic))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _invoke(cli_runner: CliRunner, download_dir: Path, command: str, *options: str):
    """Run ``lighthouse COMMAND 44347 [OPTIONS] -o DOWNLOAD_DIR``."""
    return cli_runner.invoke(cli, [command, "44347", *options, "-o", str(download_dir)])


@pytest.mark.parametrize(
    "filename",
    [
        "pass=TOPSECRET.pdf",
        "password TOPSECRET.pdf",
        "passwordValue=TOPSECRET.pdf",
        "tokenValue=TOPSECRET.pdf",
        "d2lSameSiteCanaryA%3DTOPSECRET.pdf",
        "sFT%3DTOPSECRET.pdf",
    ],
)
def test_attachment_filename_rejects_secret_key_aliases(filename: str) -> None:
    projected = safe_attachment_filename(filename, 7)

    assert "TOPSECRET" not in projected
    assert projected == "attachment_7.pdf"


@pytest.mark.parametrize("filename", ["a" * 230 + ".pdf", "é" * 115 + ".pdf"])
def test_attachment_filename_fits_atomic_temp_name_limit(filename: str) -> None:
    projected = safe_attachment_filename(filename, 7)

    assert len(projected.encode("utf-8")) <= 218
    assert projected.endswith(".pdf")


def test_session_words_are_not_treated_as_session_cookie_values() -> None:
    assert safe_assignment_folder_name("Session 1 Intro", 7) == "Session 1 Intro"
    assert safe_attachment_filename("session-notes.pdf", 7) == "session-notes.pdf"
    assert safe_assignment_folder_name("Pass Fail Grading", 7) == "Pass Fail Grading"
    assert safe_attachment_filename("Pass Criteria.pdf", 7) == "Pass Criteria.pdf"
    assert safe_assignment_folder_name("HW  1", 7) == "HW  1"
    assert safe_attachment_filename("Week  1.pdf", 7) == "Week  1.pdf"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Signals  &  Systems", "Signals & Systems"),
        (".", "Course-44347"),
        ("..", "Course-44347"),
        ("...", "Course-44347"),
    ],
)
def test_assignment_course_name_matches_sync_path(raw: str, expected: str) -> None:
    assert _safe_course_name(raw, 44347) == expected
    assert sync_safe_course_name(raw, 44347) == expected


def test_legacy_attachment_without_path_is_matched_and_migrated(
    tmp_path: Path,
) -> None:
    content = b"OLD"
    folder = _folder(7, "HW1", _att(8, "hw.pdf", len(content)))
    client = _mock_client(get_dropbox_folders=[folder], get_dropbox_folder_detail=folder)
    manifest = Manifest({"assignment_7_8": _entry(content, "hw.pdf")})
    _write(tmp_path / "Assignments" / "HW1" / "hw.pdf", content)

    downloaded, skipped, updated, errors = sync_for_course(client, 44347, tmp_path, manifest)

    assert downloaded == [] and updated == [] and errors == []
    assert skipped[0]["path"] == "Assignments/HW1/hw.pdf"
    assert manifest.get("assignment_7_8")["path"] == "Assignments/HW1/hw.pdf"
    client.download_attachment.assert_not_called()


def test_legacy_attachment_mismatch_preserves_unowned_local_file(
    tmp_path: Path,
) -> None:
    folder = _folder(7, "HW1", _att(8, "hw.pdf", 3))
    client = _mock_client(
        get_dropbox_folders=[folder],
        get_dropbox_folder_detail=folder,
        download_attachment=(b"NEW", "hw.pdf"),
    )
    manifest = Manifest({"assignment_7_8": _entry(b"OLD", "hw.pdf")})
    original = _write(tmp_path / "Assignments" / "HW1" / "hw.pdf", b"USER")

    downloaded, skipped, updated, errors = sync_for_course(client, 44347, tmp_path, manifest)

    assert skipped == [] and downloaded == [] and errors == []
    assert updated[0]["path"] == "Assignments/HW1/hw_1.pdf"
    assert original.read_bytes() == b"USER"
    assert (original.parent / "hw_1.pdf").read_bytes() == b"NEW"


def test_single_attachment_disambiguates_contested_legacy_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _mock_client(
        get_enrolled_courses=[{"OrgUnitId": 44347, "Name": "Course"}],
        get_dropbox_folder_detail=_folder(7, "HW1", _att(8, "shared.pdf", 3)),
        download_attachment=(b"NEW", "shared.pdf"),
    )
    course_dir = tmp_path / "Course-44347"
    shared_path = _write(course_dir / "Assignments" / "HW1" / "shared.pdf", b"OLD")
    entry = _entry(b"OLD", "shared.pdf", "Assignments/HW1/shared.pdf")
    Manifest({
        "assignment_7_8": dict(entry),
        "assignment_7_9": dict(entry),
    }).save(course_dir / MANIFEST_FILENAME)

    rc = download_single_attachment(client, 44347, 7, 8, tmp_path, True)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert Path(payload["path"]).name == "shared_1.pdf"
    assert shared_path.read_bytes() == b"OLD"
    assert (shared_path.parent / "shared_1.pdf").read_bytes() == b"NEW"
    manifest = Manifest.load(course_dir / MANIFEST_FILENAME)
    assert manifest.get("assignment_7_8")["path"].endswith("shared_1.pdf")
    assert manifest.get("assignment_7_9")["path"].endswith("shared.pdf")


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
    client = _mock_client(get_dropbox_folders=bad_folders)

    downloaded, errors = download_for_course(client, 44347, tmp_path, Manifest())

    assert downloaded == []
    assert errors and errors[0]["type"] == "assignment_list"


def test_bulk_attachment_invalid_element_preserves_valid_sibling(tmp_path: Path) -> None:
    client = _mock_client(
        get_dropbox_folders=[_folder(101, "Assignment 1", None, _att(1, "q1.pdf", 5))],
        download_attachment=(b"fresh", "q1.pdf"),
    )

    downloaded, errors = download_for_course(client, 44347, tmp_path, Manifest())

    assert len(downloaded) == 1
    assert errors and errors[0]["type"] == "assignment_data"


def test_bulk_attachment_invalid_collection_preserves_valid_folder(tmp_path: Path) -> None:
    folders = [
        {"Id": 100, "Name": "Malformed", "Attachments": None},
        _folder(101, "Valid", _att(1, "q1.pdf", 5)),
    ]
    client = _mock_client(get_dropbox_folders=folders, download_attachment=(b"fresh", "q1.pdf"))

    downloaded, errors = download_for_course(client, 44347, tmp_path, Manifest())

    assert len(downloaded) == 1
    assert any(error["folder_id"] == 100 for error in errors)


@pytest.mark.parametrize("kind", ["download", "sync"])
@pytest.mark.parametrize("bad_id", [True, 1.5, 0, -1, "../../evil", None])
def test_bulk_rejects_invalid_folder_id_without_followup_calls(
    tmp_path: Path, bad_id, kind,
) -> None:
    client = _mock_client(get_dropbox_folders=[_folder(bad_id, "Malformed", _att(1, "q1.pdf", 5))])

    downloaded, skipped, updated, errors = _run_bulk(kind, client, tmp_path)

    assert downloaded == [] and skipped == [] and updated == []
    assert errors and errors[0]["type"] == "assignment_data"
    client.get_dropbox_folder_detail.assert_not_called()
    client.download_attachment.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["download", "sync"])
@pytest.mark.parametrize("bad_id", [True, 1.5, 0, -1, "../../evil", None])
def test_bulk_rejects_invalid_attachment_id_preserving_valid_sibling(
    tmp_path: Path, bad_id, kind,
) -> None:
    folder = _folder(101, "Assignment 1", _att(bad_id, "bad.pdf", 3), _att(1, "q1.pdf", 5))
    client = _mock_client(get_dropbox_folders=[folder], download_attachment=(b"fresh", "q1.pdf"))

    downloaded, skipped, updated, errors = _run_bulk(kind, client, tmp_path)

    assert len(downloaded) == 1
    assert skipped == [] and updated == []
    assert errors and errors[0]["type"] == "assignment_data"
    client.get_dropbox_folder_detail.assert_not_called()
    client.download_attachment.assert_called_once_with(44347, 101, 1)
    assert not (tmp_path / "Assignments" / "Assignment 1" / "bad.pdf").exists()


def test_bulk_download_missing_assignment_selector_is_an_error_without_writes(
    tmp_path: Path,
) -> None:
    client = _mock_client(get_dropbox_folders=[_folder(101, "Assignment 1", _att(1, "q1.pdf", 5))])

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


def test_single_attachment_corrupt_manifest_returns_json_error(
    tmp_path: Path, capsys
) -> None:
    client = _mock_client(
        get_courses=[{"OrgUnitId": 44347, "Name": "Course"}],
        get_dropbox_folder_detail={"Id": 101, "Name": "Assignment"},
    )
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
# VAL-ASGN-009 / VAL-ASGN-011: Download all assignment attachments for a course
# ---------------------------------------------------------------------------

class TestDownloadAllAssignmentAttachments:
    """Test lighthouse download COURSE_ID --include-assignments."""

    def test_download_json_redacts_secret_shaped_server_filename(
        self, cli_runner, temp_download_dir,
    ):
        sentinel = "ATTACHMENT_SECRET_SENTINEL"
        folder_sentinel = "FOLDER_SECRET_SENTINEL"
        folders = [_folder(101, f"password={folder_sentinel}", _att(1, "listed.pdf", 4))]

        with _cli_client(folders, {(101, 1): (b"body", f"password={sentinel}.pdf")}):
            result = _invoke(cli_runner, temp_download_dir, "download", "--include-assignments", "--json")

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

    def test_download_include_assignments_saves_and_tracks_attachment(
        self, cli_runner, temp_download_dir
    ):
        """VAL-ASGN-009: Attachments saved to {course_dir}/Assignments/{FolderName}/{FileName}.

        VAL-ASGN-011: Manifest entry uses key pattern assignment_{folderId}_{fileId}.
        """
        folders = [_folder(101, "Assignment 1", _att(1, "q1.pdf", 1024))]

        with _cli_client(folders, {(101, 1): (b"PDF content here", "q1.pdf")}):
            result = _invoke(cli_runner, temp_download_dir, "download", "--include-assignments")

        assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
        course_dir = temp_download_dir / COURSE_DIR
        assert (course_dir / "Assignments" / "Assignment 1" / "q1.pdf").exists()
        manifest_data = json.loads((course_dir / MANIFEST_FILENAME).read_text())
        keys = list(manifest_data.keys())
        assert any(k.startswith("assignment_101_1") for k in keys), f"No namespaced key found in {keys}"
        entry = manifest_data[keys[0]]
        assert "sha256" in entry
        assert "filename" in entry
        assert "size" in entry
        assert "downloaded_at" in entry

    def test_download_include_assignments_with_content_topics(
        self, cli_runner, temp_download_dir
    ):
        """VAL-CROSS-005: Download command fetches both content topics and assignment attachments."""
        folders = [_folder(101, "Assignment 1", _att(1, "q1.pdf", 1024))]
        toc = {"Modules": [{
            "ModuleId": 1001,
            "Title": "Unit 1",
            "Modules": [],
            "Topics": [{
                "TopicId": 12345,
                "Title": "Lecture 1.pdf",
                "TypeIdentifier": "File",
                "Url": "https://example.com/files/12345",
            }],
        }]}

        with _cli_client(
            folders,
            {(101, 1): (b"PDF content", "q1.pdf")},
            toc=toc,
            topics={12345: (b"Lecture content", "Lecture 1.pdf")},
        ):
            result = _invoke(cli_runner, temp_download_dir, "download", "--include-assignments")

        assert result.exit_code == 0, f"exit={result.exit_code}"
        course_dir = temp_download_dir / COURSE_DIR
        assert (course_dir / "Unit 1" / "Lecture 1.pdf").exists()
        assert (course_dir / "Assignments" / "Assignment 1" / "q1.pdf").exists()


# ---------------------------------------------------------------------------
# VAL-ASGN-010, 013, 020, 021: Single attachment download via --assignment + --attachment
# ---------------------------------------------------------------------------

class TestSingleAttachmentDownload:
    """Test lighthouse download COURSE_ID --assignment FOLDER_ID --attachment FILE_ID."""

    @pytest.mark.parametrize(
        ("att_id", "listed_name", "content", "served_name", "saved_name"),
        [
            (1, "q1.pdf", b"Single PDF content", "q1.pdf", "q1.pdf"),
            (999, "unknown.pdf", b"Content", "", "attachment_999"),
            (1, "Q1%20Solutions.pdf", b"Content", "Q1%20Solutions.pdf", "Q1 Solutions.pdf"),
            (1, "large_video.mp4", b"X" * (5 * 1024 * 1024), "large_video.mp4", "large_video.mp4"),
        ],
        ids=[
            "VAL-ASGN-010-selected-attachment-only",
            "VAL-ASGN-013-missing-content-disposition-fallback",
            "VAL-ASGN-020-percent-encoding-decoded",
            "VAL-ASGN-021-multi-megabyte-attachment",
        ],
    )
    def test_single_attachment_download(
        self, cli_runner, temp_download_dir, att_id, listed_name, content, served_name, saved_name,
    ):
        """Only the selected attachment is saved, complete, under its sanitized name."""
        folder = _folder(101, "Assignment 1", _att(att_id, listed_name, len(content)), _att(2, "q2.pdf", 2048))
        files = {(101, att_id): (content, served_name), (101, 2): (b"Second PDF content", "q2.pdf")}

        with _cli_client([folder], files, listed=False):
            result = _invoke(
                cli_runner, temp_download_dir, "download", "--assignment", "101", "--attachment", str(att_id),
            )

        assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
        folder_dir = temp_download_dir / COURSE_DIR / "Assignments" / "Assignment 1"
        assert (folder_dir / saved_name).read_bytes() == content
        assert not (folder_dir / "q2.pdf").exists()

    def test_single_attachment_json_output(self, cli_runner, temp_download_dir):
        """VAL-ASGN-010: JSON mode returns path, size_kb, filename."""
        folder = _folder(101, "Assignment 1", _att(1, "q1.pdf", 1024))

        with _cli_client([folder], {(101, 1): (b"PDF bytes", "q1.pdf")}, listed=False):
            result = _invoke(
                cli_runner, temp_download_dir, "download", "--assignment", "101", "--attachment", "1", "--json",
            )

        assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
        data = json.loads(result.stdout)
        assert "path" in data
        assert "size_kb" in data
        assert "filename" in data
        assert data["filename"] == "q1.pdf"


# ---------------------------------------------------------------------------
# VAL-ASGN-012: Non-fatal download failures
# ---------------------------------------------------------------------------

class TestAssignmentDownloadFailures:
    """Test that individual attachment download failures are non-fatal."""

    def test_attachment_failure_is_non_fatal(self, cli_runner, temp_download_dir):
        """VAL-ASGN-012: FAILED attachment logged, remaining attachments continue."""
        folders = [_folder(101, "Assignment 1", _att(1, "q1.pdf", 1024), _att(2, "q2.pdf", 2048))]
        files = {
            (101, 1): (b"Success content", "q1.pdf"),
            (101, 2): Exception("Network error: connection refused"),
        }

        with _cli_client(folders, files):
            result = _invoke(cli_runner, temp_download_dir, "download", "--include-assignments")

        # Should complete (not crash) despite failure
        assert result.exit_code == 1, "Expected exit 1 for partial failure"
        folder_dir = temp_download_dir / COURSE_DIR / "Assignments" / "Assignment 1"
        assert (folder_dir / "q1.pdf").exists()
        assert not (folder_dir / "q2.pdf").exists()
        assert "FAILED" in result.output or "error" in result.output.lower()


# ---------------------------------------------------------------------------
# VAL-ASGN-014: Duplicate filename handling
# ---------------------------------------------------------------------------

class TestDuplicateFilenameHandling:
    """Test that duplicate filenames within same folder are disambiguated."""

    def test_duplicate_filename_within_folder_disambiguated(self, cli_runner, temp_download_dir):
        """VAL-ASGN-014: Second file with same name gets _1 suffix."""
        folder = _folder(101, "Assignment 1", _att(1, "solutions.pdf", 1024), _att(2, "solutions.pdf", 2048))
        files = {
            (101, 1): (b"Content A", "solutions.pdf"),
            (101, 2): (b"Content B", "solutions.pdf"),
        }

        with _cli_client([folder], files):
            result = _invoke(cli_runner, temp_download_dir, "download", "--include-assignments")

        assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
        folder_dir = temp_download_dir / COURSE_DIR / "Assignments" / "Assignment 1"
        assert (folder_dir / "solutions.pdf").read_bytes() == b"Content A"
        assert (folder_dir / "solutions_1.pdf").read_bytes() == b"Content B"


# ---------------------------------------------------------------------------
# VAL-ASGN-015, VAL-ASGN-016, VAL-CROSS-006: Sync detects new/updated attachments
# ---------------------------------------------------------------------------

def _seed_course(download_dir: Path, entries: dict) -> Path:
    """Create the course folder with a manifest holding ``entries``."""
    course_dir = download_dir / COURSE_DIR
    course_dir.mkdir(parents=True)
    (course_dir / MANIFEST_FILENAME).write_text(json.dumps(entries))
    return course_dir


class TestSyncAssignmentAttachments:
    """Test sync with --include-assignments detects new and updated attachments."""

    def test_sync_detects_new_attachment(self, cli_runner, temp_download_dir):
        """VAL-ASGN-015 / VAL-CROSS-006: After an initial download, sync fetches an
        attachment added on the server and skips the unchanged one."""
        content_1 = b"Content 1"
        content_2 = b"Content 2"
        folders = [_folder(
            101, "Assignment 1",
            _att(1, "q1.pdf", len(content_1)),
            _att(2, "q2.pdf", len(content_2)),
        )]
        course_dir = _seed_course(temp_download_dir, {
            "assignment_101_1": _entry(content_1, "q1.pdf", "Assignments/Assignment 1/q1.pdf"),
        })
        _write(course_dir / "Assignments" / "Assignment 1" / "q1.pdf", content_1)
        files = {(101, 1): (content_1, "q1.pdf"), (101, 2): (content_2, "q2.pdf")}

        with _cli_client(folders, files):
            result = _invoke(cli_runner, temp_download_dir, "sync", "--include-assignments", "--json")

        assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
        data = json.loads(result.stdout)
        assert len(data["assignments_downloaded"]) == 1
        # q1 is unchanged, so it is skipped rather than updated.
        assert len(data["assignments_skipped"]) == 1
        assert len(data["assignments_updated"]) == 0
        assert (course_dir / "Assignments" / "Assignment 1" / "q2.pdf").exists()

    def test_sync_detects_updated_attachment(self, cli_runner, temp_download_dir):
        """VAL-ASGN-016: Sync re-downloads attachment whose size/metadata changed."""
        folders = [_folder(101, "Assignment 1", _att(1, "q1.pdf", 9999))]  # Size changed
        course_dir = _seed_course(temp_download_dir, {
            "assignment_101_1": _entry(b"Old content", "q1.pdf"),
        })

        with _cli_client(folders, {(101, 1): (b"New content here", "q1.pdf")}):
            result = _invoke(cli_runner, temp_download_dir, "sync", "--include-assignments", "--json")

        assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
        data = json.loads(result.stdout)
        assert len(data["assignments_updated"]) == 1
        content = (course_dir / "Assignments" / "Assignment 1" / "q1.pdf").read_bytes()
        assert content == b"New content here"


# ---------------------------------------------------------------------------
# VAL-ASGN-017: Sync without --include-assignments skips assignments
# ---------------------------------------------------------------------------

class TestSyncWithoutIncludeAssignments:
    """Test that sync without --include-assignments skips assignment processing."""

    def test_sync_without_include_assignments_skips_assignments(self, cli_runner, temp_download_dir):
        """VAL-ASGN-017: Default sync skips assignment attachments."""
        (temp_download_dir / COURSE_DIR).mkdir()
        folders = [_folder(101, "Assignment 1", _att(1, "q1.pdf", 1024))]

        with _cli_client(folders, {}):
            result = _invoke(cli_runner, temp_download_dir, "sync")
            LighthouseClient.get_dropbox_folders.assert_not_called()

        assert result.exit_code == 0, f"exit={result.exit_code}"


class TestSyncDropboxAttachmentMetadata:
    """Test attachment reuse between the list and detail Dropbox endpoints."""

    def test_populated_list_attachments_skip_detail_request(self, tmp_path: Path):
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "Assignment 1", _att(1, "q1.pdf", 7))],
            download_attachment=(b"content", "q1.pdf"),
        )

        downloaded, skipped, updated, errors = sync_for_course(
            client, 44347, tmp_path, Manifest()
        )

        assert len(downloaded) == 1
        assert skipped == []
        assert updated == []
        assert errors == []
        client.get_dropbox_folder_detail.assert_not_called()

    def test_empty_list_attachments_skip_detail_request(self, tmp_path: Path):
        client = _mock_client(get_dropbox_folders=[_folder(102, "Empty Assignment")])

        result = sync_for_course(client, 44347, tmp_path, Manifest())

        assert result == ([], [], [], [])
        client.get_dropbox_folder_detail.assert_not_called()

    @pytest.mark.parametrize("kind", ["download", "sync"])
    def test_missing_list_attachments_fetch_detail_once(self, tmp_path: Path, kind):
        folder = {"Id": 103, "Name": "Assignment 3"}
        client = _mock_client(
            get_dropbox_folders=[folder],
            get_dropbox_folder_detail={**folder, "Attachments": [_att(3, "q3.pdf", 7)]},
            download_attachment=(b"content", "q3.pdf"),
        )

        downloaded, skipped, updated, errors = _run_bulk(kind, client, tmp_path)

        assert len(downloaded) == 1
        assert skipped == []
        assert updated == []
        assert errors == []
        client.get_dropbox_folder_detail.assert_called_once_with(44347, 103)

    def test_bulk_download_deduplicates_folder_ids_first_record_wins(
        self, tmp_path: Path,
    ):
        folders = [
            {"Id": 101, "Name": "First assignment"},
            _folder(101, "Conflicting duplicate", _att(2, "second.pdf", 6)),
        ]
        client = _mock_client(
            get_dropbox_folders=folders,
            get_dropbox_folder_detail=_folder(101, "First assignment detail", _att(1, "first.pdf", 5)),
            download_attachment=(b"first", "first.pdf"),
        )

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
            _folder(101, "First assignment", _att(1, "first.pdf", 5)),
            _folder(101, "Conflicting duplicate", _att(2, "second.pdf", 6)),
        ]
        client = _mock_client(get_dropbox_folders=folders, download_attachment=(b"first", "first.pdf"))
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

    @pytest.mark.parametrize("kind", ["download", "sync"])
    def test_allows_valid_duplicate_after_malformed_first_record(
        self, tmp_path: Path, kind,
    ):
        folders = [
            {"Id": 101, "Name": "Malformed first"},
            _folder(101, "Valid second", _att(2, "second.pdf", 6)),
        ]
        client = _mock_client(
            get_dropbox_folders=folders,
            get_dropbox_folder_detail={"Id": 101, "Name": "Malformed detail", "Attachments": None},
            download_attachment=(b"second", "second.pdf"),
        )

        downloaded, skipped, updated, errors = _run_bulk(kind, client, tmp_path)

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
        client = _mock_client(
            get_dropbox_folders=[{"Id": 101, "Name": "Assignment 1"}],
            get_dropbox_folder_detail=_folder(202, "Wrong assignment", _att(1, "wrong.pdf", 5)),
            download_attachment=(b"must not write", "wrong.pdf"),
        )

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
        client = _mock_client(
            get_dropbox_folder_detail=_folder(202, "Wrong assignment", _att(1, "wrong.pdf", 5)),
            download_attachment=(b"must not write", "wrong.pdf"),
        )

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
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "Assignment 1", _att(1, "wrong.pdf", 5))],
            download_attachment=(body, "wrong.pdf"),
        )

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
        client = _mock_client(
            get_dropbox_folder_detail=_folder(101, "Assignment 1", _att(1, "wrong.pdf", 5)),
            download_attachment=(body, "wrong.pdf"),
        )

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
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "Assignment 1", _att(1, "wrong.pdf", 5))],
            download_attachment=(body, "wrong.pdf"),
        )
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
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "Assignment 1", _att(1, "listed.pdf", 4))],
            download_attachment=(b"body", "password=ATTACHMENT_SECRET.pdf"),
        )
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
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "unsafe\x1b[31mFOLDER_SENTINEL", _att(1, "listed.pdf", 4))],
            download_attachment=(b"body", "unsafe\x1b[31m.pdf"),
        )
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
        client = _mock_client(
            get_enrolled_courses=[{"OrgUnitId": 44347, "Name": course_name}],
            get_dropbox_folder_detail=_folder(101, "password=FOLDER_SECRET", _att(1, "listed.pdf", 4)),
            download_attachment=(b"body", "password=ATTACHMENT_SECRET.pdf"),
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

    @pytest.mark.parametrize(
        "link",
        ["", "Assignments", "Assignments/Assignment 1"],
        ids=["course-dir", "assignments-dir", "assignment-folder"],
    )
    def test_symlinked_destination_is_rejected_before_attachment_write(self, tmp_path: Path, link):
        course_dir = tmp_path / "course"
        outside = tmp_path / "outside"
        outside.mkdir()
        link_path = course_dir / link
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(outside, target_is_directory=True)
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "Assignment 1", _att(1, "q1.pdf", 7))],
            download_attachment=(b"content", "q1.pdf"),
        )

        downloaded, errors = download_for_course(
            client, 44347, course_dir, Manifest()
        )

        assert downloaded == []
        assert errors and errors[0]["error"]
        client.download_attachment.assert_not_called()
        assert list(outside.rglob("*")) == []

    @pytest.mark.parametrize(
        "forged_component",
        ["password=LOCAL_SECRET", "unsafe\x1b[31m"],
        ids=["secret-shaped", "control-shaped"],
    )
    def test_forged_manifest_label_is_not_skipped_and_is_replaced_safely(
        self, tmp_path: Path, forged_component: str,
    ):
        course_dir = tmp_path / "course"
        content = b"fresh"
        forged_path = _write(course_dir / "Assignments" / forged_component / "x.pdf", content)
        manifest = Manifest({
            "assignment_101_1": _entry(content, "x.pdf", f"Assignments/{forged_component}/x.pdf"),
        })
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "Assignment 1", _att(1, "x.pdf", len(content)))],
            download_attachment=(content, "x.pdf"),
        )

        _downloaded, skipped, updated, errors = sync_for_course(
            client,
            44347,
            course_dir,
            manifest,
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
        content = b"fresh"
        forged_path = _write(course_dir / "Assignments" / "password=LOCAL_SECRET" / "x.pdf", content)
        manifest = Manifest({
            "assignment_101_1": _entry(content, "x.pdf", "Assignments/password=LOCAL_SECRET/x.pdf"),
        })
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "Assignment 1", _att(1, "x.pdf", len(content)))],
            download_attachment=(content, "x.pdf"),
        )

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
        old_content = b"old!"
        new_content = b"new!"
        wrong_path = _write(course_dir / "Assignments" / "Other" / "evil.pdf", old_content)
        manifest = Manifest({
            "assignment_101_1": _entry(old_content, "evil.pdf", "Assignments/Other/evil.pdf"),
        })
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "Folder 101", _att(1, "safe.pdf", len(new_content)))],
            download_attachment=(new_content, "safe.pdf"),
        )

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
        outside = _write(tmp_path / "outside.pdf", b"keep")
        content = b"fresh"
        manifest = Manifest({
            "assignment_101_1": _entry(b"stale", "outside.pdf", "Assignments/../../outside.pdf"),
        })
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "Assignment 1", _att(1, "q1.pdf", len(content)))],
            download_attachment=(content, "q1.pdf"),
        )

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
        old_content = b"old!"
        new_content = b"new!"
        local_path = _write(course_dir / "Assignments" / "Assignment 1" / "q1.pdf", new_content)
        manifest = Manifest({
            "assignment_101_1": _entry(old_content, "q1.pdf", "Assignments/Assignment 1/q1.pdf"),
        })
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "Assignment 1", _att(1, "q1.pdf", len(new_content)))],
            download_attachment=(new_content, "q1.pdf"),
        )

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
        outside = _write(tmp_path / "outside.pdf", b"keep")
        (folder_dir / "q1.pdf").symlink_to(outside)
        client = _mock_client(
            get_dropbox_folders=[_folder(101, "Assignment 1", _att(1, "q1.pdf", 5))],
            download_attachment=(b"fresh", "q1.pdf"),
        )

        downloaded, errors = download_for_course(
            client, 44347, course_dir, Manifest(),
        )

        assert errors == []
        assert downloaded[0]["filename"] == "q1_1.pdf"
        assert outside.read_bytes() == b"keep"
        assert (folder_dir / "q1.pdf").is_symlink()

    def test_manifest_and_result_keep_disambiguated_path_on_update(
        self, tmp_path: Path,
    ):
        first_content = b"first"
        second_content = b"second"
        updated_second = b"updated second"
        folder = _folder(
            104, "Duplicate Assignment",
            _att(1, "solutions.pdf", len(first_content)),
            _att(2, "solutions.pdf", len(second_content)),
        )
        client = _mock_client(get_dropbox_folders=[folder])
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
