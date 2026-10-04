"""Integration tests for download/sync command with manifest system."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli
from lighthouse_cli.commands import cmd_download
from lighthouse_cli.manifest import MANIFEST_FILENAME


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def output_dir(tmp_path):
    path = tmp_path / "downloads"
    path.mkdir()
    return path


def _topic(tid, title, kind="File", lm="2026-01-01T00:00:00Z") -> dict:
    return {"TopicId": tid, "Title": title, "TypeIdentifier": kind, "Url": "", "LastModifiedDate": lm}


def _toc(*topics, module_id=1, title="Mod") -> dict:
    return {"Modules": [{"ModuleId": module_id, "Title": title, "Modules": [], "Topics": list(topics)}]}


def _download(cli_runner, output_dir, toc, *args, name="Test", org_id=44347, files=(), html=None):
    """Run ``download ORG_ID -o OUTPUT_DIR *ARGS`` against one patched course.

    ``files`` are the successive download_topic_file results (any extra call
    fails); ``html`` is the get_topic_html result (without it, any call fails).
    """
    html_stub = {"return_value": html} if html else {"side_effect": AssertionError("HTML fetch")}
    with patch.object(LighthouseClient, "get_courses", return_value=[
        {"OrgUnitId": org_id, "Name": name, "Code": "X"}
    ]), patch.object(LighthouseClient, "get_content_toc", return_value=toc), \
         patch.object(LighthouseClient, "download_topic_file", side_effect=list(files)), \
         patch.object(LighthouseClient, "get_topic_html", **html_stub):
        return cli_runner.invoke(cli, ["download", str(org_id), "-o", str(output_dir), *args])


class TestDownloadManifestIntegration:
    def test_download_creates_manifest_with_correct_schema(self, cli_runner, output_dir):
        """Download creates .lighthouse.json with all required keys per entry."""
        toc = _toc(
            _topic(12345, "Lecture 1.pdf", lm="2026-03-15T12:00:00Z"),
            module_id=1001,
            title="Unit 1",
        )

        result = _download(
            cli_runner, output_dir, toc, "--json",
            name="Signals & Systems", files=[(b"PDF content here", "Lecture%201.pdf")],
        )

        assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
        # The course folder is named after the course Name, not just the OrgUnitId.
        course_dir = output_dir / "Signals & Systems-44347"
        manifest_path = course_dir / MANIFEST_FILENAME
        assert manifest_path.exists(), f"Manifest not found at {manifest_path}"
        entry = json.loads(manifest_path.read_text())["12345"]
        assert {"sha256", "filename", "size", "downloaded_at", "last_modified"} <= entry.keys()
        # last_modified comes from the TOC, not HTTP headers or the current time.
        assert entry["last_modified"] == "2026-03-15T12:00:00Z"
        # The Content-Disposition name is URL-decoded in the manifest and on disk.
        assert entry["filename"] == "Lecture 1.pdf"
        assert (course_dir / "Unit 1" / "Lecture 1.pdf").exists()
        assert entry["size"] == len(b"PDF content here")

    def test_download_sanitizes_course_name_special_chars(self, cli_runner, output_dir):
        result = _download(
            cli_runner, output_dir, _toc(_topic(1, "f")), "--json",
            name="Intro: CS *2025* / Section<1>", org_id=99999, files=[(b"content", "f.pdf")],
        )

        assert result.exit_code == 0
        course_dir = output_dir / "Intro_ CS _2025_ _ Section_1_-99999"
        assert course_dir.exists(), f"Expected {course_dir}. Contents: {list(output_dir.iterdir())}"

    def test_manifest_atomic_write_no_corruption(self, cli_runner, output_dir):
        """Manifest is written atomically — no partial/corrupt JSON on success."""
        toc = _toc(_topic(10, "f.pdf"), _topic(11, "g.pdf", lm="2026-01-02T00:00:00Z"))

        result = _download(
            cli_runner, output_dir, toc,
            files=[(b"content1", "f.pdf"), (b"content2", "g.pdf")],
        )

        assert result.exit_code == 0
        data = json.loads((output_dir / "Test-44347" / MANIFEST_FILENAME).read_text())
        assert len(data) == 2
        assert "10" in data
        assert "11" in data

    def test_force_flag_wipes_manifest(self, cli_runner, output_dir):
        """--force deletes an existing (here corrupt) manifest before download."""
        course_dir = output_dir / "Test-44347"
        course_dir.mkdir()
        manifest_path = course_dir / MANIFEST_FILENAME
        manifest_path.write_text("not valid json")

        result = _download(
            cli_runner, output_dir, _toc(_topic(1, "f")), "--force", files=[(b"content", "f.pdf")]
        )

        assert result.exit_code == 0
        assert "1" in json.loads(manifest_path.read_text())


class TestHTMLTopicDownload:
    """Test HTML topic download with --types file,html (VAL-SYNC-020, VAL-SYNC-034)."""

    def test_download_with_types_file_html_includes_both(self, cli_runner, output_dir):
        toc = _toc(_topic(100, "Lecture.pdf"), _topic(101, "Notes.html", kind="HTML"))

        result = _download(
            cli_runner, output_dir, toc, "--types", "file,html", "--json",
            files=[(b"PDF content", "Lecture.pdf")], html=(b"<html>test</html>", "Notes.html"),
        )

        assert result.exit_code == 0, f"exit={result.exit_code} output={result.output}"
        data = json.loads(result.output)
        assert len(data["downloaded"]) == 2, f"Expected 2 downloads, got {len(data['downloaded'])}"

    def test_html_topic_saved_as_html_file(self, cli_runner, output_dir):
        """HTML topics are saved as .html files with body content (VAL-SYNC-034)."""
        result = _download(
            cli_runner, output_dir, _toc(_topic(200, "Overview", kind="HTML")),
            "--types", "html", "--json",
            html=(b"<html><body>Hello</body></html>", "Overview.html"),
        )

        assert result.exit_code == 0
        html_file = output_dir / "Test-44347" / "Mod" / "Overview.html"
        assert html_file.exists(), f"HTML file not found at {html_file}"
        assert html_file.read_bytes() == b"<html><body>Hello</body></html>"

    def test_unknown_type_produces_warning(self, cli_runner, output_dir):
        """--types file,video warns about unknown type video and proceeds with file (VAL-SYNC-041)."""
        result = _download(
            cli_runner, output_dir, _toc(_topic(300, "f.pdf")), "--types", "file,video",
            files=[(b"content", "f.pdf")],
        )

        assert result.exit_code == 0
        assert "Unknown content type: video" in result.output


class TestFallbackFilename:
    def test_no_content_disposition_falls_back_to_topic_id(self, cli_runner, output_dir):
        """Without Content-Disposition the file is saved as topic_{id} (VAL-SYNC-059)."""
        # download_topic_file returns topic_{id} when the response has no filename=.
        result = _download(
            cli_runner, output_dir, _toc(_topic(999, "Untitled")), "--json",
            files=[(b"binary data", "topic_999")],
        )

        assert result.exit_code == 0
        fallback_file = output_dir / "Test-44347" / "Mod" / "topic_999"
        assert fallback_file.exists(), f"Fallback file not found at {fallback_file}"


class TestNoCourseIdDownloadsLatestSemester:
    def test_download_without_course_id_downloads_latest_semester(
        self, cli_runner, tmp_path, output_dir
    ):
        """download without COURSE_ID plans every course of the latest semester (VAL-SYNC-011)."""
        semesters = [
            {"OrgUnitId": 100, "Name": "Sem I", "Code": "0902_I_2024-2025"},
            {"OrgUnitId": 200, "Name": "Sem II", "Code": "0902_II_2024-2025"},
        ]
        enrollments = [
            {"OrgUnit": {"Id": 111, "Name": "Course A", "Code": "009_CourseA_0902_I_2024-2025"}},
            {"OrgUnit": {"Id": 222, "Name": "Course B", "Code": "009_CourseB_0902_II_2024-2025"}},
        ]
        tocs = {
            111: _toc(_topic(10, "f.pdf")),
            222: _toc(_topic(20, "g.pdf"), module_id=2, title="Mod2"),
        }
        cfg_path = tmp_path / "course-config.json"
        cfg_path.write_text(json.dumps({
            "tracked_courses": {
                "111": {"name": "Course A", "semester": "Sem I"},
                "222": {"name": "Course B", "semester": "Sem II"},
            }
        }))
        # A dry run must never fetch topic content.
        no_download = MagicMock(side_effect=AssertionError("dry run must not download"))

        with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg_path), \
             patch.object(LighthouseClient, "get_semesters", return_value=semesters), \
             patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
             patch.object(LighthouseClient, "get_courses", return_value=[
                 {"OrgUnitId": 111, "Name": "Course A", "Code": "A"},
                 {"OrgUnitId": 222, "Name": "Course B", "Code": "B"},
             ]), \
             patch.object(LighthouseClient, "get_content_toc", side_effect=tocs.get), \
             patch.object(LighthouseClient, "download_topic_file", no_download), \
             patch.object(LighthouseClient, "get_topic_html", no_download):
            result = cli_runner.invoke(cli, ["download", "-o", str(output_dir), "--dry-run"])

        assert result.exit_code == 0
        assert "Would download" in result.output
        no_download.assert_not_called()


class TestDownloadAssignmentValidation:
    """Invalid assignment flag combinations fail before any side effects."""

    @pytest.mark.parametrize(
        ("option", "value"),
        [
            ("--assignment", "0"),
            ("--assignment", "-1"),
            ("--attachment", "0"),
            ("--attachment", "-1"),
        ],
    )
    def test_nonpositive_selector_is_rejected_before_any_side_effects(
        self, cli_runner, tmp_path, option, value
    ):
        output_dir = tmp_path / "downloads"
        with patch("lighthouse_cli.commands.LighthouseClient") as client_cls, \
             patch("lighthouse_cli.commands._download_single_attachment") as attachment, \
             patch("lighthouse_cli.commands._run_and_render_single") as run:
            result = cli_runner.invoke(
                cli,
                [
                    "download", "44347", option, value,
                    "--json", "-o", str(output_dir),
                ],
            )

        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout)["error"]
        client_cls.assert_not_called()
        attachment.assert_not_called()
        run.assert_not_called()
        assert not output_dir.exists()

    @pytest.mark.parametrize(
        ("assignment_id", "attachment_id"),
        [(0, None), (-1, None), (None, 0), (None, -1)],
    )
    def test_command_boundary_rejects_nonpositive_selector_before_client_or_path(
        self, tmp_path, capsys, assignment_id, attachment_id
    ):
        output_dir = tmp_path / "downloads"
        with patch("lighthouse_cli.commands.LighthouseClient") as client_cls, \
             patch("lighthouse_cli.commands._download_single_attachment") as attachment, \
             patch("lighthouse_cli.commands._run_and_render_single") as run:
            result = cmd_download(
                course_id="44347",
                output_dir=str(output_dir),
                assignment_id=assignment_id,
                attachment_id=attachment_id,
                json_output=True,
            )

        assert result == 1
        assert json.loads(capsys.readouterr().out)["error"]
        client_cls.assert_not_called()
        attachment.assert_not_called()
        run.assert_not_called()
        assert not output_dir.exists()

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (["--attachment", "1"], "--attachment requires --assignment"),
            (
                ["--assignment", "101", "--attachment", "1"],
                "COURSE_ID is required",
            ),
            (
                ["44347", "--assignment", "101", "--attachment", "1", "--dry-run", "--json"],
                "--dry-run",
            ),
            (
                ["44347", "--assignment", "101", "--dry-run", "--json"],
                "--dry-run cannot be used with --assignment",
            ),
        ],
        ids=[
            "attachment-without-assignment", "no-course-id",
            "attachment-dry-run", "assignment-dry-run",
        ],
    )
    def test_invalid_assignment_scope_is_error_without_api_calls(
        self, cli_runner, tmp_path, args, message
    ):
        output_dir = tmp_path / "downloads"
        with patch("lighthouse_cli.commands.LighthouseClient") as client_cls, \
             patch("lighthouse_cli.commands._download_single_attachment") as attachment:
            result = cli_runner.invoke(cli, ["download", *args, "-o", str(output_dir)])

        assert result.exit_code == 1, result.output
        assert message in result.output
        client_cls.assert_not_called()
        attachment.assert_not_called()
        assert not output_dir.exists()

    def test_assignment_only_enables_folder_scoped_download(self):
        """A valid --assignment without --attachment must not be silently ignored."""
        with patch("lighthouse_cli.commands.LighthouseClient") as client_cls, \
             patch("lighthouse_cli.commands.resolve_course_id", return_value=44347), \
             patch("lighthouse_cli.commands._run_and_render_single", return_value=0) as run:
            client_cls.return_value.get_dropbox_folders.return_value = [{"Id": 101}]
            result = cmd_download(course_id="44347", assignment_id=101)

        assert result == 0
        client_cls.assert_called_once_with(read_only_auth=False)
        assert run.call_args.kwargs["include_assignments"] is True
        assert run.call_args.kwargs["assignment_id"] == 101

    def test_download_dry_run_constructs_read_only_client(self, cli_runner, tmp_path):
        output_dir = tmp_path / "downloads"
        with patch("lighthouse_cli.commands.LighthouseClient") as client_cls, \
             patch("lighthouse_cli.commands.resolve_course_id", return_value=44347), \
             patch("lighthouse_cli.commands._run_and_render_single", return_value=0) as run:
            result = cli_runner.invoke(
                cli,
                ["download", "44347", "--dry-run", "-o", str(output_dir)],
            )

        assert result.exit_code == 0, result.output
        client_cls.assert_called_once_with(read_only_auth=True)
        assert run.call_args.args[3].value == "plan"
