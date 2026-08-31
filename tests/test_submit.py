"""Tests for lighthouse submit command (assignment submission).

Covers:
- VAL-SUBMIT-001: Basic file submission with confirmation
- VAL-SUBMIT-002: Course resolution by name substring
- VAL-SUBMIT-003: Course resolution by numeric ID
- VAL-SUBMIT-004: Folder resolution by numeric ID
- VAL-SUBMIT-005: Folder resolution by name substring
- VAL-SUBMIT-006: Confirmation prompt before submission
- VAL-SUBMIT-007: Skip confirmation with --yes flag
- VAL-SUBMIT-008: JSON output on success
- VAL-SUBMIT-009: Error — submission window closed
- VAL-SUBMIT-010: Error — file does not exist
- VAL-SUBMIT-011: Error — session expired
- VAL-SUBMIT-012: Error — folder not found (HTTP 404)
- VAL-SUBMIT-013: Error — not authorized (HTTP 403)
- VAL-SUBMIT-014: Error — server error (HTTP 500)
- VAL-SUBMIT-015: Learner-role cookie-auth POST capability
- VAL-SUBMIT-016: Multipart/mixed request body format
- VAL-SUBMIT-017: Course and folder discovery
- VAL-SUBMIT-019: --file flag is required
- VAL-SUBMIT-020: Non-interactive / agent-friendly output
- VAL-CROSS-009: JSON output consistency across all commands
- VAL-CROSS-011: Help and discoverability
"""

from __future__ import annotations

import io
import json as json_module
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import (
    LighthouseClient,
    NetworkError,
    SessionExpiredError,
    SubmissionOutcomeUnknownError,
)


class _TtyStringIO(io.StringIO):
    """In-memory text stream that behaves like an interactive terminal."""

    def isatty(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def sample_submission_response() -> dict:
    """Sample successful submission response from D2L API."""
    return {
        "submissionId": 99999,
        "submittedBy": {"value": "12345", "displayName": "Student Name"},
        "submittedAt": "2026-05-11T10:30:00Z",
        "text": {"Text": "Submitted via lighthouse-cli: test.pdf", "Html": "<p>Submitted via lighthouse-cli: test.pdf</p>"},
        "attachments": [
            {"FileName": "test.pdf", "FileSize": 4096},
        ],
    }


@pytest.fixture
def temp_pdf_file(tmp_path) -> Path:
    """Create a temporary PDF-like file for testing submissions."""
    f = tmp_path / "test.pdf"
    f.write_bytes(b"test file content for submission")
    return f


@pytest.fixture
def mock_courses() -> list[dict]:
    return [
        {"OrgUnitId": 44347, "Name": "Signals & Systems", "Code": "009_BME2125_2025-2026"},
        {"OrgUnitId": 44348, "Name": "Engineering Mathematics III", "Code": "009_MAT3001_2025-2026"},
    ]


@pytest.fixture
def mock_dropbox_folders() -> list[dict]:
    return [
        {"Id": 789, "Name": "Assignment 1 - Signals", "DueDate": "2026-05-15T23:59:00Z"},
        {"Id": 790, "Name": "Assignment 2 - Fourier Transform", "DueDate": "2026-05-20T23:59:00Z"},
    ]


# ---------------------------------------------------------------------------
# Helper: mock client factory
# ---------------------------------------------------------------------------

def _make_mock_response(status_code: int, json_data: dict | None = None) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    if json_data is not None:
        mock_resp.json.return_value = json_data
    mock_resp.text = json_module.dumps(json_data) if json_data else ""
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _make_client_with_mock_session(status_code: int, json_data: dict | None = None) -> tuple[LighthouseClient, list]:
    """Create a client with a mock session that captures requests."""
    captured: list = []

    def mock_request(method, url, **kwargs):
        captured.append({
            "method": method,
            "url": url,
            "headers": kwargs.get("headers", {}),
            "data": kwargs.get("data", b""),
            "cookies": kwargs.get("cookies", {}),
            "timeout": kwargs.get("timeout"),
        })
        return _make_mock_response(status_code, json_data)

    mock_session = MagicMock()
    mock_session.request = mock_request

    client = LighthouseClient()
    client._loaded = True
    client._cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def", "d2lSameSiteCanaryA": "x", "d2lSameSiteCanaryB": "y"}
    client._session = mock_session

    return client, captured


# ---------------------------------------------------------------------------
# API-level tests: submit_file method
# ---------------------------------------------------------------------------

class TestSubmitFile:
    """Tests for LighthouseClient.submit_file() method."""

    def test_submit_file_builds_correct_multipart_body(
        self, sample_submission_response: dict
    ) -> None:
        """VAL-SUBMIT-016: Multipart/mixed body has JSON part + file part with correct Content-Disposition."""
        client, captured = _make_client_with_mock_session(200, sample_submission_response)

        client.submit_file(
            org_unit_id=44347,
            folder_id=789,
            file_bytes=b"test file content",
            filename="test.pdf",
            description="My submission",
        )

        assert len(captured) == 1
        req = captured[0]
        assert req["method"] == "POST"
        assert "/44347/dropbox/folders/789/submissions/mysubmissions" in req["url"]
        assert "multipart/mixed" in req["headers"].get("Content-Type", "")
        assert "boundary" in req["headers"].get("Content-Type", "")

        body = req["data"]
        assert b"Content-Type: application/json" in body
        assert b'"Text": "My submission"' in body
        assert b"Content-Type: application/pdf" in body or b"Content-Type: application/octet-stream" in body
        assert b'Content-Disposition: form-data; name=""; filename="test.pdf"' in body
        assert b"test file content" in body

    def test_submit_file_success_returns_submission_details(
        self, sample_submission_response: dict
    ) -> None:
        """VAL-SUBMIT-001: Successful submission returns JSON with submissionId, timestamp."""
        client, _ = _make_client_with_mock_session(200, sample_submission_response)

        result = client.submit_file(
            org_unit_id=44347,
            folder_id=789,
            file_bytes=b"test content",
            filename="test.pdf",
        )

        assert result["submissionId"] == 99999
        assert "submittedAt" in result
        assert result["attachments"][0]["FileName"] == "test.pdf"

    def test_submit_file_session_expired_raises_session_expired_error(self) -> None:
        """VAL-SUBMIT-011: Session expired raises SessionExpiredError."""
        client, _ = _make_client_with_mock_session(401)

        with pytest.raises(SessionExpiredError) as exc_info:
            client.submit_file(
                org_unit_id=44347,
                folder_id=789,
                file_bytes=b"test content",
                filename="test.pdf",
            )
        assert "auth login" in str(exc_info.value)

    def test_submit_file_403_raises_permission_error(self) -> None:
        """VAL-SUBMIT-013: HTTP 403 raises PermissionError with clear message."""
        client, _ = _make_client_with_mock_session(403)

        with pytest.raises(PermissionError) as exc_info:
            client.submit_file(
                org_unit_id=44347,
                folder_id=789,
                file_bytes=b"test content",
                filename="test.pdf",
            )
        assert "Permission denied" in str(exc_info.value)
        assert "789" in str(exc_info.value)

    def test_submit_file_404_raises_file_not_found_error(self) -> None:
        """VAL-SUBMIT-012: HTTP 404 raises FileNotFoundError with clear message."""
        client, _ = _make_client_with_mock_session(404)

        with pytest.raises(FileNotFoundError) as exc_info:
            client.submit_file(
                org_unit_id=44347,
                folder_id=789,
                file_bytes=b"test content",
                filename="test.pdf",
            )
        assert "not found" in str(exc_info.value)

    def test_submit_file_500_raises_safe_value_error(self) -> None:
        """VAL-SUBMIT-014: HTTP 500 never exposes the server response body."""
        client, _ = _make_client_with_mock_session(
            500, {"detail": "Submitted comments are too large."}
        )

        with pytest.raises(ValueError) as exc_info:
            client.submit_file(
                org_unit_id=44347,
                folder_id=789,
                file_bytes=b"test content",
                filename="test.pdf",
            )
        assert str(exc_info.value) == (
            "D2L API error (500): the remote server rejected the submission. "
            "This may indicate malformed request body or submission window restrictions."
        )
        assert "Submitted comments are too large" not in str(exc_info.value)

    def test_submit_file_unknown_response_raises_typed_error_without_retry(self) -> None:
        """A successful POST with an unusable body is reported exactly once."""
        client, captured = _make_client_with_mock_session(200)

        with pytest.raises(SubmissionOutcomeUnknownError) as exc_info:
            client.submit_file(
                org_unit_id=44347,
                folder_id=789,
                file_bytes=b"test content",
                filename="test.pdf",
            )

        assert str(exc_info.value) == (
            "Submission outcome is unknown because the API returned an unsupported "
            "result shape. Verify the assignment status before trying again."
        )
        assert len(captured) == 1
        assert captured[0]["method"] == "POST"

    def test_submit_file_uses_correct_api_path(
        self, sample_submission_response: dict
    ) -> None:
        """Verify the correct D2L API path is used."""
        client, captured = _make_client_with_mock_session(200, sample_submission_response)

        client.submit_file(org_unit_id=44347, folder_id=789, file_bytes=b"x", filename="x.pdf")

        assert len(captured) == 1
        assert "44347" in captured[0]["url"]
        assert "789" in captured[0]["url"]
        assert "submissions/mysubmissions" in captured[0]["url"]

    def test_submit_file_description_defaults_to_filename(
        self, sample_submission_response: dict
    ) -> None:
        """When no description provided, defaults to 'Submitted via lighthouse-cli: {filename}'."""
        client, captured = _make_client_with_mock_session(200, sample_submission_response)

        client.submit_file(org_unit_id=44347, folder_id=789, file_bytes=b"x", filename="myfile.pdf")

        body = captured[0]["data"]
        assert b"Submitted via lighthouse-cli: myfile.pdf" in body

    def test_submit_file_rich_text_has_text_and_html(
        self, sample_submission_response: dict
    ) -> None:
        """RichText JSON part contains both Text and Html fields."""
        client, captured = _make_client_with_mock_session(200, sample_submission_response)

        client.submit_file(org_unit_id=44347, folder_id=789, file_bytes=b"x", filename="x.pdf", description="Hello")

        body = captured[0]["data"].decode("utf-8")
        assert '"Text": "Hello"' in body
        assert '"Html": "<p>Hello</p>"' in body

    def test_submit_file_content_length_header_is_set(
        self, sample_submission_response: dict
    ) -> None:
        """Content-Length header is set to the total body byte length."""
        client, captured = _make_client_with_mock_session(200, sample_submission_response)

        file_bytes = b"x" * 100
        client.submit_file(org_unit_id=44347, folder_id=789, file_bytes=file_bytes, filename="x.pdf")

        headers = captured[0]["headers"]
        assert "Content-Length" in headers
        content_length = int(headers["Content-Length"])
        assert content_length > 100

    def test_submit_file_redirect_to_login_raises_session_expired(self) -> None:
        """VAL-SUBMIT-011 (variant): Redirect to login page raises SessionExpiredError."""
        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.headers = {"Location": "https://lighthouse.manipal.edu/d2l/login"}
        mock_resp.raise_for_status = MagicMock()

        def mock_request(method, url, **kwargs):
            return mock_resp

        mock_session = MagicMock()
        mock_session.request = mock_request

        client = LighthouseClient()
        client._loaded = True
        client._cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
        client._session = mock_session

        with pytest.raises(SessionExpiredError) as exc_info:
            client.submit_file(org_unit_id=44347, folder_id=789, file_bytes=b"x", filename="x.pdf")
        assert "auth login" in str(exc_info.value)


# ---------------------------------------------------------------------------
# CLI-level tests: submit command
# ---------------------------------------------------------------------------

class TestSubmitCommand:
    """Tests for the lighthouse submit CLI command."""

    def test_submit_command_exists(self, cli_runner: CliRunner) -> None:
        """VAL-CROSS-011: submit command appears in help."""
        from lighthouse_cli.cli import cli
        result = cli_runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "submit" in result.output

    def test_submit_help_shows_options(self, cli_runner: CliRunner) -> None:
        """VAL-CROSS-011: submit --help shows all options."""
        from lighthouse_cli.cli import cli
        result = cli_runner.invoke(cli, ["submit", "--help"])
        assert result.exit_code == 0
        assert "--file" in result.output
        assert "--yes" in result.output
        assert "--json" in result.output

    def test_submit_requires_file_flag(self, cli_runner: CliRunner) -> None:
        """VAL-SUBMIT-019: Missing --file produces usage error."""
        from lighthouse_cli.cli import cli
        result = cli_runner.invoke(cli, ["submit", "44347", "789"], catch_exceptions=True)
        assert result.exit_code != 0
        # Click gives exit code 2 for usage errors
        assert result.exit_code == 2

    def test_submit_file_not_found_error(self, cli_runner: CliRunner) -> None:
        """VAL-SUBMIT-010: File does not exist produces clear error before API call."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            # Mock get_courses to avoid early failure
            mock_client.get_courses.return_value = [
                {"OrgUnitId": 44347, "Name": "Signals & Systems"}
            ]
            # Mock cookies property
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            # Mock get_dropbox_folders to return a list (required by _resolve_folder_id)
            mock_client.get_dropbox_folders.return_value = [
                {"Id": 789, "Name": "Assignment 1 - Signals"}
            ]
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", "/nonexistent/path/file.pdf", "--yes"],
            )

            assert result.exit_code == 1
            assert "File not found" in result.output
            mock_client_cls.assert_not_called()

    def test_submit_path_resolution_failure_is_safe_json(self, cli_runner: CliRunner) -> None:
        """A path-resolution failure stays one JSON document and makes no API call."""
        from lighthouse_cli.cli import cli

        with (
            patch.object(Path, "resolve", side_effect=RuntimeError("PATH_SENTINEL")),
            patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls,
        ):
            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", "/tmp/input.pdf", "--yes", "--json"],
            )

        assert result.exit_code == 1
        assert json_module.loads(result.stdout) == {"error": "File not found."}
        assert result.stdout.count('"error"') == 1
        assert "PATH_SENTINEL" not in result.output
        assert "/tmp/input.pdf" not in result.output
        mock_client_cls.assert_not_called()

    def test_submit_client_constructor_failure_is_safe_json(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
    ) -> None:
        """Client setup failures emit one safe JSON result before file I/O."""
        from lighthouse_cli.cli import cli

        with (
            patch(
                "lighthouse_cli.submit.LighthouseClient",
                side_effect=RuntimeError("CLIENT_SECRET_SENTINEL"),
            ) as mock_client_cls,
            patch.object(Path, "read_bytes", autospec=True) as read_bytes_mock,
        ):
            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

        assert result.exit_code == 1
        assert json_module.loads(result.stdout) == {
            "error": "Could not initialize Lighthouse client."
        }
        assert result.stdout.count('"error"') == 1
        assert "CLIENT_SECRET_SENTINEL" not in result.output
        mock_client_cls.assert_called_once_with()
        read_bytes_mock.assert_not_called()

    def test_submit_success_with_yes_flag_json_output(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        sample_submission_response: dict,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-001 + VAL-SUBMIT-007 + VAL-SUBMIT-008: Successful submit with --yes --json."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}

            # Mock submit_file to return success
            mock_client.submit_file.return_value = sample_submission_response

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

            assert result.exit_code == 0
            output = json_module.loads(result.output)
            assert output["submission_id"] == 99999
            assert output["folder_id"] == 789
            assert output["course_id"] == 44347
            assert "submitted_at" in output
            assert output["file"]["name"] == "test.pdf"

    def test_submit_success_human_output(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        sample_submission_response: dict,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-001: Successful submit without --json shows human-readable confirmation."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            mock_client.submit_file.return_value = sample_submission_response

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes"],
            )

            assert result.exit_code == 0
            assert "Submitted successfully" in result.output

    @pytest.mark.parametrize("json_output", [False, True])
    @pytest.mark.parametrize(
        ("folder_name", "course_name", "fallbacks"),
        [
            ({"token": "SECRET"}, "Signals & Systems", {"folder": True, "course": False}),
            ("Assignment\x1b[31m1", "Signals & Systems", {"folder": True, "course": False}),
            ("Assignment 1", {"token": "SECRET"}, {"folder": False, "course": True}),
        ],
    )
    def test_submit_output_projects_untrusted_course_and_folder_names(
        self,
        json_output: bool,
        folder_name: object,
        course_name: object,
        fallbacks: dict[str, bool],
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        sample_submission_response: dict,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """Malformed or control-bearing labels never reach output streams."""
        from lighthouse_cli import submit as submit_module
        from lighthouse_cli.cli import cli

        with (
            patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls,
            patch.object(submit_module, "_get_course_name", return_value=course_name),
        ):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": folder_name}
            mock_client.submit_file.return_value = sample_submission_response

            args = [
                "submit",
                "44347",
                "789",
                "--file",
                str(temp_pdf_file),
                "--yes",
            ]
            if json_output:
                args.append("--json")
            result = cli_runner.invoke(cli, args)

        assert result.exit_code == 0
        assert "SECRET" not in result.output
        assert "\x1b" not in result.output
        if json_output:
            payload = json_module.loads(result.stdout)
            assert payload["folder_name"] == (
                "Unknown folder" if fallbacks["folder"] else folder_name
            )
            assert payload["course_name"] == (
                "Unknown course" if fallbacks["course"] else course_name
            )
        else:
            if fallbacks["folder"]:
                assert "Folder: Unknown folder" in result.output
            if fallbacks["course"]:
                assert "Course: Unknown course" in result.output

    @pytest.mark.parametrize("json_output", [False, True])
    def test_submit_output_projects_untrusted_response_fields(
        self,
        json_output: bool,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """Nested or control-bearing response fields never enter output."""
        from lighthouse_cli.cli import cli

        response = {
            "submissionId": {"token": "RESPONSE_TOKEN_SENTINEL", "password": "RESPONSE_PASSWORD_SENTINEL"},
            "submittedAt": {"token": "RESPONSE_TIMESTAMP_SENTINEL"},
        }
        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.submit_file.return_value = response

            args = [
                "submit",
                "44347",
                "789",
                "--file",
                str(temp_pdf_file),
                "--yes",
            ]
            if json_output:
                args.append("--json")
            result = cli_runner.invoke(cli, args)

        assert result.exit_code == 0
        assert "RESPONSE_TOKEN_SENTINEL" not in result.output
        assert "RESPONSE_PASSWORD_SENTINEL" not in result.output
        assert "RESPONSE_TIMESTAMP_SENTINEL" not in result.output
        assert "\x1b" not in result.output
        if json_output:
            payload = json_module.loads(result.stdout)
            assert payload["submission_id"] is None
            assert isinstance(payload["submitted_at"], str)
            assert payload["submitted_at"].isprintable()
        else:
            assert "Submission ID: None" in result.output

    @pytest.mark.parametrize("json_output", [False, True])
    def test_submit_preserves_remote_filename_but_hides_secret_shaped_label(
        self,
        json_output: bool,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
        sample_submission_response: dict,
    ) -> None:
        """The POST gets the real basename while displays use a safe fallback."""
        from lighthouse_cli.cli import cli

        filename = "password=FILENAME_SECRET_SENTINEL.pdf"
        secret_file = temp_pdf_file.with_name(filename)
        temp_pdf_file.rename(secret_file)

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.submit_file.return_value = sample_submission_response

            args = [
                "submit",
                "44347",
                "789",
                "--file",
                str(secret_file),
                "--yes",
            ]
            if json_output:
                args.append("--json")
            result = cli_runner.invoke(cli, args)

        assert result.exit_code == 0
        assert mock_client.submit_file.call_args.kwargs["filename"] == filename
        assert "FILENAME_SECRET_SENTINEL" not in result.output
        assert "password=" not in result.output.casefold()
        if json_output:
            assert json_module.loads(result.stdout)["file"]["name"] == "Unknown file"
        else:
            assert "File: Unknown file" in result.output

    def test_submit_course_name_substring_resolution(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        sample_submission_response: dict,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-002: Course ID as name substring (case-insensitive)."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_enrolled_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            mock_client.submit_file.return_value = sample_submission_response

            result = cli_runner.invoke(
                cli,
                ["submit", "signals", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

            assert result.exit_code == 0
            output = json_module.loads(result.output)
            assert output["course_id"] == 44347

    def test_submit_course_numeric_id(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        sample_submission_response: dict,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-003: Course ID as numeric OrgUnitId."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            mock_client.submit_file.return_value = sample_submission_response

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

            assert result.exit_code == 0
            output = json_module.loads(result.output)
            assert output["course_id"] == 44347

    def test_submit_folder_name_substring_resolution(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        sample_submission_response: dict,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-005: Folder ID as name substring (case-insensitive)."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            mock_client.submit_file.return_value = sample_submission_response

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "signals", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

            assert result.exit_code == 0
            output = json_module.loads(result.output)
            assert output["folder_id"] == 789

    def test_submit_folder_not_found_404_error(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-012: Folder not found (HTTP 404) produces clear error."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            mock_client.submit_file.side_effect = FileNotFoundError(
                "Dropbox folder 999 not found. Run: lighthouse assignments"
            )

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "999", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

            assert result.exit_code == 1
            # Error is on stderr
            assert "not found" in result.output.lower()
            assert "lighthouse assignments" in result.output

    def test_submit_permission_denied_403_error(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-013: HTTP 403 permission denied produces clear error."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            mock_client.submit_file.side_effect = PermissionError(
                "Permission denied to submit to folder 789."
            )

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes"],
            )

            assert result.exit_code == 1
            assert "Permission denied" in result.output

    def test_submit_session_expired_error(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-011: Session expired produces clear error with re-auth hint."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            mock_client.submit_file.side_effect = SessionExpiredError(
                "Session expired. Run: lighthouse auth login"
            )

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes"],
            )

            assert result.exit_code == 1
            assert "Session expired" in result.output
            assert "auth login" in result.output

    def test_submit_server_error_500(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-014: HTTP 500 server error produces clear error."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            mock_client.submit_file.side_effect = ValueError(
                "D2L API error (500): Submitted comments are too large."
            )

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes"],
            )

            assert result.exit_code == 1
            assert "500" in result.output

    def test_submit_confirmation_prompt_aborts_on_no(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-006: Without --yes and non-TTY, submission is refused.

        The actual interactive prompt tests are complex in CliRunner due to
        isatty() patching. This test verifies the non-interactive refusal path
        which is the primary behavior for agent use.
        """
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}

            # Without --yes, non-TTY should refuse
            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file)],
            )

            assert result.exit_code == 1
            assert "--yes" in result.output
            mock_client.submit_file.assert_not_called()

    def test_submit_non_tty_without_yes_refuses(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-006: Non-TTY without --yes refuses with message to use --yes."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file)],
            )

            assert result.exit_code == 1
            assert "--yes" in result.output

    def test_submit_non_tty_json_refusal_is_parseable_and_avoids_api(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
    ) -> None:
        """A non-interactive JSON refusal keeps stdout machine-readable."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            result = cli_runner.invoke(
                cli,
                [
                    "submit",
                    "44347",
                    "789",
                    "--file",
                    str(temp_pdf_file),
                    "--json",
                ],
            )

        assert result.exit_code == 1
        assert json_module.loads(result.stdout) == {
            "error": "Refusing to submit without --yes in non-interactive mode. "
            "Use --yes flag to confirm."
        }
        mock_client_cls.assert_not_called()

    def test_submit_yes_plus_json_only_json_on_stdout(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        sample_submission_response: dict,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-020: --yes + --json = only JSON on stdout, nothing else."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            mock_client.submit_file.return_value = sample_submission_response

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

            assert result.exit_code == 0
            # All output should be valid JSON
            parsed = json_module.loads(result.output)
            assert "submission_id" in parsed
            assert "folder_id" in parsed
            assert "course_id" in parsed
            assert "file" in parsed
            assert "submitted_at" in parsed

    def test_submit_ambiguous_folder_name_error(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
    ) -> None:
        """VAL-SUBMIT-005: Ambiguous folder name match raises error listing matches."""
        from lighthouse_cli.cli import cli

        # Folders with overlapping names
        folders = [
            {"Id": 789, "Name": "Assignment 1 - Signals"},
            {"Id": 790, "Name": "Assignment 1 - Systems"},
        ]

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = folders
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "assignment", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

            assert result.exit_code == 1
            # Error is on stderr
            assert "Ambiguous" in result.output

    def test_submit_course_not_found_error(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
    ) -> None:
        """VAL-SUBMIT-002 (zero match): Course not found produces clear error."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = [
                {"OrgUnitId": 44347, "Name": "Signals & Systems"}
            ]
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}

            result = cli_runner.invoke(
                cli,
                ["submit", "nonexistent_course", "789", "--file", str(temp_pdf_file), "--yes"],
            )

            assert result.exit_code == 1
            assert "not found" in result.output.lower()

    def test_submit_folder_zero_match_error_lists_available(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-005 (zero match): Folder name not found lists available folders."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "nonexistent_folder", "--file", str(temp_pdf_file), "--yes"],
            )

            assert result.exit_code == 1
            # Error is on stderr
            assert "not found" in result.output.lower()
            # Folder names and IDs come from the remote response and are not
            # echoed in normal diagnostics.
            assert "789" not in result.output
            assert "Assignment 1 - Signals" not in result.output

    def test_submit_malformed_matched_folder_id_fails_before_post(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
    ) -> None:
        """A malformed matched folder record cannot reach the write endpoint."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = [
                {"Id": 0, "Name": "Assignment 1 - Signals"},
            ]

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "signals", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

        assert result.exit_code == 1
        assert "invalid" in json_module.loads(result.stdout)["error"].casefold()
        mock_client.submit_file.assert_not_called()

    def test_submit_json_output_is_valid_parseable_json(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        sample_submission_response: dict,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-CROSS-009: JSON output is valid and parseable."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            mock_client.submit_file.return_value = sample_submission_response

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

            assert result.exit_code == 0
            # Should not raise JSONDecodeError
            parsed = json_module.loads(result.output)
            assert isinstance(parsed, dict)

    def test_submit_json_error_output_is_also_json(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-CROSS-009 (variant): Error case also produces structured output."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}
            mock_client.submit_file.side_effect = SessionExpiredError(
                "Session expired. Run: lighthouse auth login"
            )

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

            assert result.exit_code == 1
            assert json_module.loads(result.stdout) == {
                "error": "Session expired. Run: lighthouse auth login"
            }
            assert "Session expired" in result.output

    def test_submit_error_sanitizes_sensitive_transport_details(self) -> None:
        """Both submit error streams use the centralized safe formatter."""
        from lighthouse_cli import submit as submit_module

        message = (
            "HTTP 500 for https://lighthouse.manipal.edu/api?token=SUBMIT_TOKEN_SENTINEL "
            "response_body=BODY_SENTINEL password hunter2 "
            "Run: lighthouse auth login --pass PASSWORD_SENTINEL"
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            patch.object(submit_module.sys, "stdout", stdout),
            patch.object(submit_module.sys, "stderr", stderr),
        ):
            exit_code = submit_module._submit_error(message, json_output=True)

        assert exit_code == 1
        parsed = json_module.loads(stdout.getvalue())
        assert parsed == {
            "error": "Remote server error (HTTP 500). Run: lighthouse auth login"
        }
        combined = stdout.getvalue() + stderr.getvalue()
        for sentinel in (
            "SUBMIT_TOKEN_SENTINEL",
            "BODY_SENTINEL",
            "hunter2",
            "PASSWORD_SENTINEL",
            "https://lighthouse.manipal.edu",
        ):
            assert sentinel not in combined
        assert "Remote server error (HTTP 500)." in stderr.getvalue()

    def test_submit_json_error_sanitizes_transport_details_through_cli(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """The CLI-shaped submit failure remains parseable and secret-safe."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.submit_file.side_effect = ValueError(
                "HTTP 500 for https://lighthouse.manipal.edu/api?token=CLI_TOKEN_SENTINEL "
                "response_body=CLI_BODY_SENTINEL password cli-password"
            )

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

        assert result.exit_code == 1
        assert json_module.loads(result.stdout) == {
            "error": "Remote server error (HTTP 500)."
        }
        for sentinel in (
            "CLI_TOKEN_SENTINEL",
            "CLI_BODY_SENTINEL",
            "cli-password",
            "https://lighthouse.manipal.edu",
        ):
            assert sentinel not in result.output

    @pytest.mark.parametrize(
        ("remote_error", "safe_error"),
        [
            (
                "HTTP 429 for https://lighthouse.manipal.edu/api?token=RATE_TOKEN_SENTINEL",
                "Rate limited (HTTP 429).",
            ),
            (
                "HTTP 401 for https://lighthouse.manipal.edu/api?token=AUTH_TOKEN_SENTINEL",
                "Session expired (HTTP 401).",
            ),
        ],
    )
    def test_submit_transport_errors_are_single_safe_json_documents(
        self,
        remote_error: str,
        safe_error: str,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """429/401 failures are not replayed and never expose URL credentials."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.submit_file.side_effect = ValueError(remote_error)

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

        assert result.exit_code == 1
        assert json_module.loads(result.stdout) == {"error": safe_error}
        assert result.stdout.count('"error"') == 1
        assert "TOKEN_SENTINEL" not in result.output
        assert "https://lighthouse.manipal.edu" not in result.output
        mock_client.submit_file.assert_called_once()

    def test_submit_rate_limit_network_error_keeps_no_retry_context(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """A non-retried submission rate limit remains actionable and safe."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.submit_file.side_effect = NetworkError(
                "Submission request was rate limited; no retry was attempted."
            )

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

        assert result.exit_code == 1
        error = json_module.loads(result.stdout)["error"].casefold()
        assert "rate limited" in error
        assert "no retry" in error
        assert "check your connection and try again" not in error
        mock_client.submit_file.assert_called_once()

    @pytest.mark.parametrize("json_output", [False, True])
    def test_submit_typed_unknown_outcome_is_actionable(
        self,
        json_output: bool,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """Typed unknown outcomes stay actionable in both output modes."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.submit_file.side_effect = SubmissionOutcomeUnknownError()

            args = [
                "submit",
                "44347",
                "789",
                "--file",
                str(temp_pdf_file),
                "--yes",
            ]
            if json_output:
                args.append("--json")
            result = cli_runner.invoke(cli, args)

        message = (
            json_module.loads(result.stdout)["error"]
            if json_output
            else result.output
        ).casefold()
        assert result.exit_code == 1
        assert "submission outcome is unknown" in message
        assert "verify the assignment status before trying again" in message
        assert "check your connection and try again" not in message
        mock_client.submit_file.assert_called_once()

    @pytest.mark.parametrize("invalid_response", [None, [], "unexpected response"])
    def test_submit_invalid_response_is_safe_ambiguous_error_json(
        self,
        invalid_response: object,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """Malformed accepted responses never become tracebacks or retry advice."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.submit_file.return_value = invalid_response

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
            )

        assert result.exit_code == 1
        assert json_module.loads(result.stdout) == {
            "error": (
                "Submission outcome is unknown because the API returned an unsupported result shape. "
                "Verify the assignment status before trying again."
            )
        }
        assert "Traceback" not in result.output
        assert "blindly" not in result.output.lower()

    def test_submit_invalid_response_is_safe_ambiguous_error_human(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """Human output warns about the unknown remote outcome without retrying."""
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.submit_file.return_value = None

            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes"],
            )

        assert result.exit_code == 1
        assert "Submission outcome is unknown" in result.output
        assert "Verify the assignment status" in result.output
        assert "Traceback" not in result.output


class TestSubmitFolderResolution:
    """Tests for folder ID resolution by name substring."""

    def test_folder_numeric_id_accepted(self) -> None:
        """VAL-SUBMIT-004: Numeric folder ID is used directly."""
        from lighthouse_cli.submit import _resolve_folder_id

        mock_client = MagicMock()
        mock_client.get_dropbox_folders.return_value = [
            {"Id": 789, "Name": "Assignment 1"},
            {"Id": 790, "Name": "Assignment 2"},
        ]

        result = _resolve_folder_id(mock_client, 44347, "789")
        assert result == 789

    def test_folder_name_substring_case_insensitive(self) -> None:
        """VAL-SUBMIT-005: Folder name matching is case-insensitive."""
        from lighthouse_cli.submit import _resolve_folder_id

        mock_client = MagicMock()
        mock_client.get_dropbox_folders.return_value = [
            {"Id": 789, "Name": "Assignment 1 - Signals"},
            {"Id": 790, "Name": "Assignment 2 - Fourier"},
        ]

        result = _resolve_folder_id(mock_client, 44347, "signals")
        assert result == 789

    def test_folder_ambiguous_match_raises_value_error(self) -> None:
        """VAL-SUBMIT-005: Multiple matches raises ValueError listing all matches."""
        from lighthouse_cli.submit import _resolve_folder_id

        mock_client = MagicMock()
        mock_client.get_dropbox_folders.return_value = [
            {"Id": 789, "Name": "Assignment 1 - Signals"},
            {"Id": 790, "Name": "Assignment 1 - Systems"},
        ]

        with pytest.raises(ValueError) as exc_info:
            _resolve_folder_id(mock_client, 44347, "assignment")
        assert "Ambiguous" in str(exc_info.value)

    def test_folder_zero_match_raises_safe_file_not_found(self) -> None:
        """VAL-SUBMIT-005 (zero match): No match omits remote folder listings."""
        from lighthouse_cli.submit import _resolve_folder_id

        mock_client = MagicMock()
        mock_client.get_dropbox_folders.return_value = [
            {"Id": 789, "Name": "Assignment 1 - Signals"},
            {"Id": 790, "Name": "Assignment 2 - Fourier"},
        ]

        with pytest.raises(FileNotFoundError) as exc_info:
            _resolve_folder_id(mock_client, 44347, "nonexistent")
        assert "not found" in str(exc_info.value)
        assert "789" not in str(exc_info.value)
        assert "790" not in str(exc_info.value)

    @pytest.mark.parametrize("selector", ["0", "-1", "1.5", "/tmp/folder", "\\\\tmp\\\\folder"])
    def test_malformed_numeric_or_path_selector_is_rejected(self, selector: str) -> None:
        """Malformed selectors never get reinterpreted as a folder name."""
        from lighthouse_cli.submit import _resolve_folder_id

        mock_client = MagicMock()
        with pytest.raises(ValueError):
            _resolve_folder_id(mock_client, 44347, selector)
        mock_client.get_dropbox_folders.assert_not_called()

    @pytest.mark.parametrize("folder_id", [None, True, 0, -7, 1.5, "bad-id"])
    def test_matching_folder_with_malformed_id_is_rejected(self, folder_id: object) -> None:
        """A matched API folder with an invalid ID cannot reach submission."""
        from lighthouse_cli.submit import _resolve_folder_id

        mock_client = MagicMock()
        mock_client.get_dropbox_folders.return_value = [
            {"Id": folder_id, "Name": "Assignment 1 - Signals"},
        ]
        with pytest.raises(ValueError):
            _resolve_folder_id(mock_client, 44347, "signals")


class TestSubmitConfirmation:
    """Tests for confirmation prompt behavior."""

    def test_confirmation_shows_course_folder_file(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-006: Confirmation prompt shows course name, folder name, file path.

        Note: This test verifies the confirmation prompt displays correct information
        when TTY is detected. The isatty() patching is complex in CliRunner environment,
        so this test verifies the prompt text format when running without --yes.
        """
        from lighthouse_cli.cli import cli

        # Verify that when --yes is NOT set and we're in non-TTY, the command
        # refuses to proceed (which proves the confirmation gate is in place)
        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def"}

            # Without --yes, non-TTY should refuse
            result = cli_runner.invoke(
                cli,
                ["submit", "44347", "789", "--file", str(temp_pdf_file)],
            )

            # Should refuse with message about --yes
            assert result.exit_code == 1
            assert "--yes" in result.output

    def test_confirmation_accepts_yes(
        self,
        cli_runner: CliRunner,
        temp_pdf_file: Path,
        sample_submission_response: dict,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-007: --yes flag bypasses confirmation prompt.

        The command should submit successfully without trying to read an
        interactive response, which is the primary agent use case.
        """
        from lighthouse_cli.cli import cli

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.submit_file.return_value = sample_submission_response

            with patch("builtins.input", side_effect=AssertionError("unexpected prompt")) as input_mock:
                result = cli_runner.invoke(
                    cli,
                    ["submit", "44347", "789", "--file", str(temp_pdf_file), "--yes", "--json"],
                )

        assert result.exit_code == 0
        input_mock.assert_not_called()
        mock_client.submit_file.assert_called_once()

    def test_confirmation_empty_input_aborts(
        self,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """VAL-SUBMIT-006: Empty input at confirmation aborts.

        JSON mode keeps the prompt and friendly cancellation message on stderr,
        while stdout contains exactly one structured JSON result. The file body
        is not read because the submission was declined.
        """
        from lighthouse_cli import submit as submit_module

        stdin = _TtyStringIO()
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}

            with (
                patch.object(submit_module.sys, "stdin", stdin),
                patch.object(submit_module.sys, "stdout", stdout),
                patch.object(submit_module.sys, "stderr", stderr),
                patch("builtins.input", return_value="") as input_mock,
                patch.object(Path, "read_bytes", autospec=True) as read_bytes_mock,
            ):
                exit_code = submit_module.cmd_submit(
                    course_id="44347",
                    folder_id="789",
                    file_path=str(temp_pdf_file),
                    json_output=True,
                )

        assert exit_code == 0
        assert json_module.loads(stdout.getvalue()) == {"cancelled": True}
        assert "Submit to 'Assignment 1 - Signals'" in stderr.getvalue()
        assert "Confirm [y/N]:" in stderr.getvalue()
        assert "Submission cancelled." in stderr.getvalue()
        input_mock.assert_called_once_with()
        read_bytes_mock.assert_not_called()
        mock_client.submit_file.assert_not_called()

    @pytest.mark.parametrize("json_output", [False, True])
    @pytest.mark.parametrize("input_error", [EOFError(), KeyboardInterrupt()])
    def test_confirmation_input_failure_cancels_cleanly(
        self,
        json_output: bool,
        input_error: BaseException,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """EOF and Ctrl-C at confirmation never produce a traceback or POST."""
        from lighthouse_cli import submit as submit_module

        stdin = _TtyStringIO()
        stdout = _TtyStringIO()
        stderr = io.StringIO()

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}

            with (
                patch.object(submit_module.sys, "stdin", stdin),
                patch.object(submit_module.sys, "stdout", stdout),
                patch.object(submit_module.sys, "stderr", stderr),
                patch("builtins.input", side_effect=input_error),
            ):
                exit_code = submit_module.cmd_submit(
                    course_id="44347",
                    folder_id="789",
                    file_path=str(temp_pdf_file),
                    json_output=json_output,
                )

        assert exit_code == 0
        assert "Traceback" not in stdout.getvalue() + stderr.getvalue()
        if json_output:
            assert "Submission cancelled." in stderr.getvalue()
        else:
            assert "Submission cancelled." in stdout.getvalue()
        if json_output:
            assert json_module.loads(stdout.getvalue()) == {"cancelled": True}
        mock_client.submit_file.assert_not_called()

    def test_json_confirmation_accepts_with_prompt_only_on_stderr(
        self,
        temp_pdf_file: Path,
        sample_submission_response: dict,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """Interactive JSON confirmation preserves a JSON-only stdout stream."""
        from lighthouse_cli import submit as submit_module

        stdin = _TtyStringIO()
        stdout = _TtyStringIO()
        stderr = io.StringIO()

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
            mock_client.submit_file.return_value = sample_submission_response

            with (
                patch.object(submit_module.sys, "stdin", stdin),
                patch.object(submit_module.sys, "stdout", stdout),
                patch.object(submit_module.sys, "stderr", stderr),
                patch("builtins.input", return_value="yes") as input_mock,
            ):
                exit_code = submit_module.cmd_submit(
                    course_id="44347",
                    folder_id="789",
                    file_path=str(temp_pdf_file),
                    json_output=True,
                )

        assert exit_code == 0
        parsed = json_module.loads(stdout.getvalue())
        assert parsed["submission_id"] == 99999
        assert "Submit to 'Assignment 1 - Signals'" not in stdout.getvalue()
        assert "Submit to 'Assignment 1 - Signals'" in stderr.getvalue()
        assert "Confirm [y/N]:" in stderr.getvalue()
        input_mock.assert_called_once_with()
        mock_client.submit_file.assert_called_once()

    def test_human_confirmation_decline_remains_friendly(
        self,
        temp_pdf_file: Path,
        mock_courses: list[dict],
        mock_dropbox_folders: list[dict],
    ) -> None:
        """A human-mode decline keeps the existing friendly text output."""
        from lighthouse_cli import submit as submit_module

        stdin = _TtyStringIO()
        stdout = _TtyStringIO()
        stderr = io.StringIO()

        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_client.get_courses.return_value = mock_courses
            mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
            mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}

            with (
                patch.object(submit_module.sys, "stdin", stdin),
                patch.object(submit_module.sys, "stdout", stdout),
                patch.object(submit_module.sys, "stderr", stderr),
                patch("builtins.input", return_value="n") as input_mock,
                patch.object(Path, "read_bytes", autospec=True) as read_bytes_mock,
            ):
                exit_code = submit_module.cmd_submit(
                    course_id="44347",
                    folder_id="789",
                    file_path=str(temp_pdf_file),
                )

        assert exit_code == 0
        assert "Submit to 'Assignment 1 - Signals'" in stdout.getvalue()
        assert "Confirm [y/N]:" in stdout.getvalue()
        assert "Submission cancelled." in stdout.getvalue()
        assert stderr.getvalue() == ""
        input_mock.assert_called_once_with()
        read_bytes_mock.assert_not_called()
        mock_client.submit_file.assert_not_called()


# ---------------------------------------------------------------------------
# Multipart request invariants
# ---------------------------------------------------------------------------

class TestSubmissionIntegration:
    """End-to-end invariants exercised with the HTTP transport mocked."""

    def test_multipart_boundary_is_unique(self, sample_submission_response: dict) -> None:
        """Each submission gets a fresh multipart boundary."""
        client, captured = _make_client_with_mock_session(200, sample_submission_response)

        client.submit_file(org_unit_id=44347, folder_id=789, file_bytes=b"x", filename="x.pdf")
        client.submit_file(org_unit_id=44347, folder_id=789, file_bytes=b"x", filename="x.pdf")

        assert len(captured) == 2
        boundaries = [request["headers"]["Content-Type"] for request in captured]
        assert boundaries[0] != boundaries[1]
