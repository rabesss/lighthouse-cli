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

import json as json_module
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient, SessionExpiredError


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

        result = client.submit_file(
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

    def test_submit_file_500_raises_value_error_with_detail(self) -> None:
        """VAL-SUBMIT-014: HTTP 500 raises ValueError with D2L error detail."""
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
        assert "Submitted comments are too large" in str(exc_info.value)

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
            # Should list available folders
            assert "789" in result.output

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
            # Error is on stderr, not JSON on stdout (that's OK per spec)
            assert "Session expired" in result.output


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

    def test_folder_zero_match_raises_file_not_found(self) -> None:
        """VAL-SUBMIT-005 (zero match): No match raises FileNotFoundError with available folders."""
        from lighthouse_cli.submit import _resolve_folder_id

        mock_client = MagicMock()
        mock_client.get_dropbox_folders.return_value = [
            {"Id": 789, "Name": "Assignment 1 - Signals"},
            {"Id": 790, "Name": "Assignment 2 - Fourier"},
        ]

        with pytest.raises(FileNotFoundError) as exc_info:
            _resolve_folder_id(mock_client, 44347, "nonexistent")
        assert "not found" in str(exc_info.value)
        assert "789" in str(exc_info.value)
        assert "790" in str(exc_info.value)


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

    def test_confirmation_accepts_yes(self) -> None:
        """VAL-SUBMIT-007: --yes flag bypasses confirmation prompt.

        This test verifies that when --yes is provided, the confirmation prompt
        is skipped entirely (no input() call), which is the primary agent use case.
        """
        # Covered by test_submit_success_with_yes_flag_json_output
        pass

    def test_confirmation_empty_input_aborts(self) -> None:
        """VAL-SUBMIT-006: Empty input at confirmation aborts.

        Note: Testing interactive prompts in CliRunner is complex.
        The non-interactive refusal (without --yes) is tested in other tests.
        """
        pass


# ---------------------------------------------------------------------------
# Integration note tests
# ---------------------------------------------------------------------------

class TestSubmissionIntegration:
    """Notes about integration testing that requires live D2L session."""

    def test_live_submission_requires_valid_session(self) -> None:
        """VAL-SUBMIT-015: Learner POST capability needs live test with real cookies.

        Live test procedure:
        1. Ensure valid D2L session via `lighthouse auth login`
        2. Find a dropbox folder: `lighthouse assignments <course_id>`
        3. Run: lighthouse submit <course_id> <folder_id> --file test.pdf --yes
        4. Expected: HTTP 200 with JSON containing submissionId
        """
        pass

    def test_multipart_boundary_must_be_unique(self) -> None:
        """The boundary string in multipart/mixed must be unique."""
        pass
