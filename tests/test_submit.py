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

from lighthouse_cli import submit as submit_module
from lighthouse_cli.api import (
    LighthouseClient,
    NetworkError,
    SessionExpiredError,
    SubmissionOutcomeUnknownError,
)
from lighthouse_cli.cli import cli
from lighthouse_cli.config import COOKIE_NAMES
from lighthouse_cli.submit import _resolve_folder_id


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


@pytest.fixture
def client(mock_courses: list[dict], mock_dropbox_folders: list[dict]):
    """The submit command's client, patched to resolve course 44347 and folder 789."""
    with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.get_courses.return_value = mock_courses
        mock_client.get_dropbox_folders.return_value = mock_dropbox_folders
        mock_client.get_dropbox_folder_detail.return_value = {"Name": "Assignment 1 - Signals"}
        yield mock_client


@pytest.fixture
def run_submit(cli_runner: CliRunner, temp_pdf_file: Path):
    """Invoke ``lighthouse submit COURSE FOLDER --file FILE [flags]``."""

    def run(*flags: str, course: str = "44347", folder: str = "789", file: object = None):
        path = temp_pdf_file if file is None else file
        return cli_runner.invoke(cli, ["submit", course, folder, "--file", str(path), *flags])

    return run


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
    # These tests isolate the multipart POST after session bootstrap.
    client._csrf_token = "synthetic-csrf"
    client._loaded = True
    client._cookies = {"d2lSecureSessionVal": "abc", "d2lSessionVal": "def", "d2lSameSiteCanaryA": "x", "d2lSameSiteCanaryB": "y"}
    client._session = mock_session

    return client, captured


def _redirect_client(location: str) -> tuple[LighthouseClient, MagicMock]:
    """A loaded client whose submission POST is answered with a 302 to ``location``."""
    mock_resp = MagicMock(status_code=302, headers={"Location": location})
    mock_session = MagicMock()
    mock_session.request.return_value = mock_resp
    client = LighthouseClient()
    client._loaded = True
    client._cookies = dict.fromkeys(COOKIE_NAMES, "value")
    client._session = mock_session
    return client, mock_resp


def _submit(client: LighthouseClient, **overrides: object) -> dict:
    """Submit a small file to folder 789 of course 44347."""
    kwargs = {"org_unit_id": 44347, "folder_id": 789, "file_bytes": b"x", "filename": "x.pdf"}
    return client.submit_file(**{**kwargs, **overrides})


def _folder_client(*folders: dict) -> MagicMock:
    client = MagicMock()
    client.get_dropbox_folders.return_value = list(folders)
    return client


def _prompt(path: Path, *, json_output: bool, **input_behaviour: object):
    """Run ``cmd_submit`` at an interactive terminal; ``input()`` gets ``input_behaviour``.

    Returns ``(exit_code, stdout, stderr, input_mock)``.
    """
    stdout = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(submit_module.sys, "stdin", _TtyStringIO()),
        patch.object(submit_module.sys, "stdout", stdout),
        patch.object(submit_module.sys, "stderr", stderr),
        patch("builtins.input", **input_behaviour) as input_mock,
    ):
        exit_code = submit_module.cmd_submit(
            course_id="44347", folder_id="789", file_path=str(path), json_output=json_output,
        )
    return exit_code, stdout.getvalue(), stderr.getvalue(), input_mock


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

        _submit(client, file_bytes=b"test file content", filename="test.pdf", description="My submission")

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

        result = _submit(client, file_bytes=b"test content", filename="test.pdf")

        assert result["submissionId"] == 99999
        assert "submittedAt" in result
        assert result["attachments"][0]["FileName"] == "test.pdf"

    @pytest.mark.parametrize(
        ("status", "error", "fragments"),
        [
            pytest.param(401, SessionExpiredError, ["auth login"], id="VAL-SUBMIT-011-401"),
            pytest.param(403, PermissionError, ["Permission denied", "789"], id="VAL-SUBMIT-013-403"),
            pytest.param(404, FileNotFoundError, ["not found"], id="VAL-SUBMIT-012-404"),
        ],
    )
    def test_submit_file_http_errors_raise_typed_errors(
        self, status: int, error: type[Exception], fragments: list[str]
    ) -> None:
        client, _ = _make_client_with_mock_session(status)

        with pytest.raises(error) as exc_info:
            _submit(client)
        for fragment in fragments:
            assert fragment in str(exc_info.value)

    def test_submit_file_500_raises_safe_value_error(self) -> None:
        """VAL-SUBMIT-014: HTTP 500 never exposes the server response body."""
        client, _ = _make_client_with_mock_session(
            500, {"detail": "Submitted comments are too large."}
        )

        with pytest.raises(ValueError) as exc_info:
            _submit(client)
        assert str(exc_info.value) == (
            "D2L API error (500): the remote server rejected the submission. "
            "This may indicate malformed request body or submission window restrictions."
        )
        assert "Submitted comments are too large" not in str(exc_info.value)

    def test_submit_file_unknown_response_raises_typed_error_without_retry(self) -> None:
        """A successful POST with an unusable body is reported exactly once."""
        client, captured = _make_client_with_mock_session(200)

        with pytest.raises(SubmissionOutcomeUnknownError) as exc_info:
            _submit(client)

        assert str(exc_info.value) == (
            "Submission outcome is unknown because the API returned an unsupported "
            "result shape. Verify the assignment status before trying again."
        )
        assert len(captured) == 1
        assert captured[0]["method"] == "POST"

    def test_submit_file_description_defaults_to_filename(
        self, sample_submission_response: dict
    ) -> None:
        """When no description provided, defaults to 'Submitted via lighthouse-cli: {filename}'."""
        client, captured = _make_client_with_mock_session(200, sample_submission_response)

        _submit(client, filename="myfile.pdf")

        assert b"Submitted via lighthouse-cli: myfile.pdf" in captured[0]["data"]

    def test_submit_file_rich_text_has_text_and_html(
        self, sample_submission_response: dict
    ) -> None:
        client, captured = _make_client_with_mock_session(200, sample_submission_response)

        _submit(client, description="Hello")

        body = captured[0]["data"].decode("utf-8")
        assert '"Text": "Hello"' in body
        assert '"Html": "<p>Hello</p>"' in body

    def test_submit_file_content_length_header_is_set(
        self, sample_submission_response: dict
    ) -> None:
        """Content-Length header is set to the total body byte length."""
        client, captured = _make_client_with_mock_session(200, sample_submission_response)

        _submit(client, file_bytes=b"x" * 100)

        headers = captured[0]["headers"]
        assert "Content-Length" in headers
        assert int(headers["Content-Length"]) > 100

    @pytest.mark.parametrize(
        "location",
        [
            "https://lighthouse.manipal.edu/d2l/login",
            "/login",
            "/d2l/login?target=/d2l/home",
            "/d2l/lp/auth/saml/login",
            "/d2l/lp/auth/login/login.d2l",
        ],
    )
    def test_submit_file_supported_login_redirects_expire_session(self, location: str) -> None:
        """VAL-SUBMIT-011 (variant): Redirect to login page raises SessionExpiredError."""
        client, mock_resp = _redirect_client(location)

        with pytest.raises(SessionExpiredError, match="auth login"):
            _submit(client)

        mock_resp.close.assert_called_once()

    @pytest.mark.parametrize(
        "location",
        [
            "/d2l/other",
            # Substrings of a login path elsewhere in the URL do not imply login.
            "/d2l/lp/authoring/123",
            "/d2l/other?authorization=required",
        ],
    )
    def test_submit_file_non_login_redirect_raises_network_error_and_closes(self, location: str) -> None:
        client, mock_resp = _redirect_client(location)

        with pytest.raises(NetworkError, match="unexpected redirect"):
            _submit(client)

        mock_resp.close.assert_called_once()


# ---------------------------------------------------------------------------
# CLI-level tests: submit command
# ---------------------------------------------------------------------------

class TestSubmitCommand:
    """Tests for the lighthouse submit CLI command."""

    def test_submit_command_exists(self, cli_runner: CliRunner) -> None:
        """VAL-CROSS-011: submit command appears in help."""
        result = cli_runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "submit" in result.output

    def test_submit_help_shows_options(self, cli_runner: CliRunner) -> None:
        """VAL-CROSS-011: submit --help shows all options."""
        result = cli_runner.invoke(cli, ["submit", "--help"])
        assert result.exit_code == 0
        assert "--file" in result.output
        assert "--yes" in result.output
        assert "--json" in result.output

    def test_submit_requires_file_flag(self, cli_runner: CliRunner) -> None:
        """VAL-SUBMIT-019: Missing --file produces usage error."""
        result = cli_runner.invoke(cli, ["submit", "44347", "789"], catch_exceptions=True)
        # Click gives exit code 2 for usage errors
        assert result.exit_code == 2

    def test_submit_file_not_found_error(self, run_submit) -> None:
        """VAL-SUBMIT-010: File does not exist produces clear error before API call."""
        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            result = run_submit("--yes", file="/nonexistent/path/file.pdf")

        assert result.exit_code == 1
        assert "File not found" in result.output
        mock_client_cls.assert_not_called()

    def test_submit_path_resolution_failure_is_safe_json(self, run_submit) -> None:
        """A path-resolution failure stays one JSON document and makes no API call."""
        with (
            patch.object(Path, "resolve", side_effect=RuntimeError("PATH_SENTINEL")),
            patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls,
        ):
            result = run_submit("--yes", "--json", file="/tmp/input.pdf")

        assert result.exit_code == 1
        assert json_module.loads(result.stdout) == {"error": "File not found."}
        assert result.stdout.count('"error"') == 1
        assert "PATH_SENTINEL" not in result.output
        assert "/tmp/input.pdf" not in result.output
        mock_client_cls.assert_not_called()

    def test_submit_client_constructor_failure_is_safe_json(self, run_submit) -> None:
        """Client setup failures emit one safe JSON result before file I/O."""
        with (
            patch(
                "lighthouse_cli.submit.LighthouseClient",
                side_effect=RuntimeError("CLIENT_SECRET_SENTINEL"),
            ) as mock_client_cls,
            patch.object(Path, "read_bytes", autospec=True) as read_bytes_mock,
        ):
            result = run_submit("--yes", "--json")

        assert result.exit_code == 1
        assert json_module.loads(result.stdout) == {
            "error": "Could not initialize Lighthouse client."
        }
        assert result.stdout.count('"error"') == 1
        assert "CLIENT_SECRET_SENTINEL" not in result.output
        mock_client_cls.assert_called_once_with(read_only_auth=False)
        read_bytes_mock.assert_not_called()

    def test_submit_success_with_yes_flag_json_output(
        self, client: MagicMock, run_submit, sample_submission_response: dict
    ) -> None:
        """VAL-SUBMIT-001/003/007/008/020, VAL-CROSS-009: --yes --json with a numeric
        course ID submits and prints only the JSON result on stdout."""
        client.submit_file.return_value = sample_submission_response

        result = run_submit("--yes", "--json")

        assert result.exit_code == 0
        output = json_module.loads(result.output)
        assert output["submission_id"] == 99999
        assert output["folder_id"] == 789
        assert output["course_id"] == 44347
        assert "submitted_at" in output
        assert output["file"]["name"] == "test.pdf"

    def test_submit_success_human_output(
        self, client: MagicMock, run_submit, sample_submission_response: dict
    ) -> None:
        """VAL-SUBMIT-001: Successful submit without --json shows human-readable confirmation."""
        client.submit_file.return_value = sample_submission_response

        result = run_submit("--yes")

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
        client: MagicMock,
        run_submit,
        sample_submission_response: dict,
    ) -> None:
        """Malformed or control-bearing labels never reach output streams."""
        client.get_dropbox_folder_detail.return_value = {"Name": folder_name}
        client.submit_file.return_value = sample_submission_response

        with patch.object(submit_module, "_get_course_name", return_value=course_name):
            result = run_submit("--yes", "--json") if json_output else run_submit("--yes")

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
        self, json_output: bool, client: MagicMock, run_submit
    ) -> None:
        """Nested or control-bearing response fields never enter output."""
        client.submit_file.return_value = {
            "submissionId": {"token": "RESPONSE_TOKEN_SENTINEL", "password": "RESPONSE_PASSWORD_SENTINEL"},
            "submittedAt": {"token": "RESPONSE_TIMESTAMP_SENTINEL"},
        }

        result = run_submit("--yes", "--json") if json_output else run_submit("--yes")

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
        client: MagicMock,
        run_submit,
        temp_pdf_file: Path,
        sample_submission_response: dict,
    ) -> None:
        """The POST gets the real basename while displays use a safe fallback."""
        filename = "password=FILENAME_SECRET_SENTINEL.pdf"
        secret_file = temp_pdf_file.with_name(filename)
        temp_pdf_file.rename(secret_file)
        client.submit_file.return_value = sample_submission_response

        flags = ["--yes", "--json"] if json_output else ["--yes"]
        result = run_submit(*flags, file=secret_file)

        assert result.exit_code == 0
        assert client.submit_file.call_args.kwargs["filename"] == filename
        assert "FILENAME_SECRET_SENTINEL" not in result.output
        assert "password=" not in result.output.casefold()
        if json_output:
            assert json_module.loads(result.stdout)["file"]["name"] == "Unknown file"
        else:
            assert "File: Unknown file" in result.output

    @pytest.mark.parametrize(
        ("course", "folder"),
        [
            pytest.param("signals", "789", id="VAL-SUBMIT-002-course-name"),
            pytest.param("44347", "signals", id="VAL-SUBMIT-005-folder-name"),
        ],
    )
    def test_submit_resolves_case_insensitive_name_substrings(
        self,
        course: str,
        folder: str,
        client: MagicMock,
        run_submit,
        sample_submission_response: dict,
        mock_courses: list[dict],
    ) -> None:
        client.get_enrolled_courses.return_value = mock_courses
        client.submit_file.return_value = sample_submission_response

        result = run_submit("--yes", "--json", course=course, folder=folder)

        assert result.exit_code == 0
        output = json_module.loads(result.output)
        assert output["course_id"] == 44347
        assert output["folder_id"] == 789

    @pytest.mark.parametrize(
        ("folder", "flags", "error", "fragments"),
        [
            pytest.param(
                "999", ["--json"],
                FileNotFoundError("Dropbox folder 999 not found. Run: lighthouse assignments"),
                ["not found", "lighthouse assignments"],
                id="VAL-SUBMIT-012-folder-not-found",
            ),
            pytest.param(
                "789", [], PermissionError("Permission denied to submit to folder 789."),
                ["Permission denied"],
                id="VAL-SUBMIT-013-permission-denied",
            ),
            pytest.param(
                "789", [], SessionExpiredError("Session expired. Run: lighthouse auth login"),
                ["Session expired", "auth login"],
                id="VAL-SUBMIT-011-session-expired",
            ),
            pytest.param(
                "789", [], ValueError("D2L API error (500): Submitted comments are too large."),
                ["500"],
                id="VAL-SUBMIT-014-server-error",
            ),
        ],
    )
    def test_submit_remote_failures_produce_clear_errors(
        self,
        folder: str,
        flags: list[str],
        error: Exception,
        fragments: list[str],
        client: MagicMock,
        run_submit,
    ) -> None:
        client.submit_file.side_effect = error

        result = run_submit("--yes", *flags, folder=folder)

        assert result.exit_code == 1
        for fragment in fragments:
            assert fragment in result.output

    def test_submit_non_tty_json_refusal_is_parseable_and_avoids_api(self, run_submit) -> None:
        """A non-interactive JSON refusal keeps stdout machine-readable."""
        with patch("lighthouse_cli.submit.LighthouseClient") as mock_client_cls:
            result = run_submit("--json")

        assert result.exit_code == 1
        assert json_module.loads(result.stdout) == {
            "error": "Refusing to submit without --yes in non-interactive mode. "
            "Use --yes flag to confirm."
        }
        mock_client_cls.assert_not_called()

    def test_submit_ambiguous_folder_name_error(self, client: MagicMock, run_submit) -> None:
        """VAL-SUBMIT-005: Ambiguous folder name match raises error listing matches."""
        client.get_dropbox_folders.return_value = [
            {"Id": 789, "Name": "Assignment 1 - Signals"},
            {"Id": 790, "Name": "Assignment 1 - Systems"},
        ]

        result = run_submit("--yes", "--json", folder="assignment")

        assert result.exit_code == 1
        assert "Ambiguous" in result.output

    def test_submit_course_not_found_error(self, client: MagicMock, run_submit) -> None:
        """VAL-SUBMIT-002 (zero match): Course not found produces clear error."""
        client.get_courses.return_value = [
            {"OrgUnitId": 44347, "Name": "Signals & Systems"}
        ]

        result = run_submit("--yes", course="nonexistent_course")

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_submit_folder_zero_match_error_lists_available(self, client: MagicMock, run_submit) -> None:
        """VAL-SUBMIT-005 (zero match): Folder name not found lists available folders."""
        result = run_submit("--yes", folder="nonexistent_folder")

        assert result.exit_code == 1
        assert "not found" in result.output.lower()
        # Folder names and IDs come from the remote response and are not
        # echoed in normal diagnostics.
        assert "789" not in result.output
        assert "Assignment 1 - Signals" not in result.output

    def test_submit_malformed_matched_folder_id_fails_before_post(
        self, client: MagicMock, run_submit
    ) -> None:
        """A malformed matched folder record cannot reach the write endpoint."""
        client.get_dropbox_folders.return_value = [
            {"Id": 0, "Name": "Assignment 1 - Signals"},
        ]

        result = run_submit("--yes", "--json", folder="signals")

        assert result.exit_code == 1
        assert "invalid" in json_module.loads(result.stdout)["error"].casefold()
        client.submit_file.assert_not_called()

    def test_submit_json_error_output_is_also_json(self, client: MagicMock, run_submit) -> None:
        """VAL-CROSS-009 (variant): Error case also produces structured output."""
        client.submit_file.side_effect = SessionExpiredError(
            "Session expired. Run: lighthouse auth login"
        )

        result = run_submit("--yes", "--json")

        assert result.exit_code == 1
        assert json_module.loads(result.stdout) == {
            "error": "Session expired. Run: lighthouse auth login"
        }
        assert "Session expired" in result.output

    def test_submit_error_sanitizes_sensitive_transport_details(self) -> None:
        """Both submit error streams use the centralized safe formatter."""
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
        self, client: MagicMock, run_submit
    ) -> None:
        """The CLI-shaped submit failure remains parseable and secret-safe."""
        client.submit_file.side_effect = ValueError(
            "HTTP 500 for https://lighthouse.manipal.edu/api?token=CLI_TOKEN_SENTINEL "
            "response_body=CLI_BODY_SENTINEL password cli-password"
        )

        result = run_submit("--yes", "--json")

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
        self, remote_error: str, safe_error: str, client: MagicMock, run_submit
    ) -> None:
        """429/401 failures are not replayed and never expose URL credentials."""
        client.submit_file.side_effect = ValueError(remote_error)

        result = run_submit("--yes", "--json")

        assert result.exit_code == 1
        assert json_module.loads(result.stdout) == {"error": safe_error}
        assert result.stdout.count('"error"') == 1
        assert "TOKEN_SENTINEL" not in result.output
        assert "https://lighthouse.manipal.edu" not in result.output
        client.submit_file.assert_called_once()

    def test_submit_rate_limit_network_error_keeps_no_retry_context(
        self, client: MagicMock, run_submit
    ) -> None:
        """A non-retried submission rate limit remains actionable and safe."""
        client.submit_file.side_effect = NetworkError(
            "Submission request was rate limited; no retry was attempted."
        )

        result = run_submit("--yes", "--json")

        assert result.exit_code == 1
        error = json_module.loads(result.stdout)["error"].casefold()
        assert "rate limited" in error
        assert "no retry" in error
        assert "check your connection and try again" not in error
        client.submit_file.assert_called_once()

    @pytest.mark.parametrize("json_output", [False, True])
    def test_submit_typed_unknown_outcome_is_actionable(
        self, json_output: bool, client: MagicMock, run_submit
    ) -> None:
        """Typed unknown outcomes stay actionable in both output modes."""
        client.submit_file.side_effect = SubmissionOutcomeUnknownError()

        result = run_submit("--yes", "--json") if json_output else run_submit("--yes")

        message = (
            json_module.loads(result.stdout)["error"]
            if json_output
            else result.output
        ).casefold()
        assert result.exit_code == 1
        assert "submission outcome is unknown" in message
        assert "verify the assignment status before trying again" in message
        assert "check your connection and try again" not in message
        client.submit_file.assert_called_once()

    @pytest.mark.parametrize("invalid_response", [None, [], "unexpected response"])
    def test_submit_invalid_response_is_safe_ambiguous_error_json(
        self, invalid_response: object, client: MagicMock, run_submit
    ) -> None:
        """Malformed accepted responses never become tracebacks or retry advice."""
        client.submit_file.return_value = invalid_response

        result = run_submit("--yes", "--json")

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
        self, client: MagicMock, run_submit
    ) -> None:
        """Human output warns about the unknown remote outcome without retrying."""
        client.submit_file.return_value = None

        result = run_submit("--yes")

        assert result.exit_code == 1
        assert "Submission outcome is unknown" in result.output
        assert "Verify the assignment status" in result.output
        assert "Traceback" not in result.output


class TestSubmitFolderResolution:
    """Tests for folder ID resolution by name substring."""

    @pytest.mark.parametrize(
        "selector",
        [
            pytest.param("789", id="VAL-SUBMIT-004-numeric-id"),
            pytest.param("signals", id="VAL-SUBMIT-005-case-insensitive-name"),
        ],
    )
    def test_folder_selector_resolves_to_its_id(self, selector: str) -> None:
        client = _folder_client(
            {"Id": 789, "Name": "Assignment 1 - Signals"},
            {"Id": 790, "Name": "Assignment 2 - Fourier"},
        )

        assert _resolve_folder_id(client, 44347, selector) == 789

    def test_folder_ambiguous_match_raises_value_error(self) -> None:
        """VAL-SUBMIT-005: Multiple matches raises ValueError listing all matches."""
        client = _folder_client(
            {"Id": 789, "Name": "Assignment 1 - Signals"},
            {"Id": 790, "Name": "Assignment 1 - Systems"},
        )

        with pytest.raises(ValueError) as exc_info:
            _resolve_folder_id(client, 44347, "assignment")
        assert "Ambiguous" in str(exc_info.value)

    def test_folder_zero_match_raises_safe_file_not_found(self) -> None:
        """VAL-SUBMIT-005 (zero match): No match omits remote folder listings."""
        client = _folder_client(
            {"Id": 789, "Name": "Assignment 1 - Signals"},
            {"Id": 790, "Name": "Assignment 2 - Fourier"},
        )

        with pytest.raises(FileNotFoundError) as exc_info:
            _resolve_folder_id(client, 44347, "nonexistent")
        assert "not found" in str(exc_info.value)
        assert "789" not in str(exc_info.value)
        assert "790" not in str(exc_info.value)

    @pytest.mark.parametrize("selector", ["0", "-1", "1.5", "/tmp/folder", "\\\\tmp\\\\folder"])
    def test_malformed_numeric_or_path_selector_is_rejected(self, selector: str) -> None:
        """Malformed selectors never get reinterpreted as a folder name."""
        client = MagicMock()
        with pytest.raises(ValueError):
            _resolve_folder_id(client, 44347, selector)
        client.get_dropbox_folders.assert_not_called()

    @pytest.mark.parametrize("folder_id", [None, True, 0, -7, 1.5, "bad-id"])
    def test_matching_folder_with_malformed_id_is_rejected(self, folder_id: object) -> None:
        """A matched API folder with an invalid ID cannot reach submission."""
        client = _folder_client({"Id": folder_id, "Name": "Assignment 1 - Signals"})
        with pytest.raises(ValueError):
            _resolve_folder_id(client, 44347, "signals")


class TestSubmitConfirmation:
    """Tests for confirmation prompt behavior."""

    def test_confirmation_accepts_yes(
        self, client: MagicMock, run_submit, sample_submission_response: dict
    ) -> None:
        """VAL-SUBMIT-007: --yes flag bypasses confirmation prompt.

        The command should submit successfully without trying to read an
        interactive response, which is the primary agent use case.
        """
        client.submit_file.return_value = sample_submission_response

        with patch("builtins.input", side_effect=AssertionError("unexpected prompt")) as input_mock:
            result = run_submit("--yes", "--json")

        assert result.exit_code == 0
        input_mock.assert_not_called()
        client.submit_file.assert_called_once()

    def test_confirmation_empty_input_aborts(self, client: MagicMock, temp_pdf_file: Path) -> None:
        """VAL-SUBMIT-006: Empty input at confirmation aborts.

        JSON mode keeps the prompt and friendly cancellation message on stderr,
        while stdout contains exactly one structured JSON result. The file body
        is not read because the submission was declined.
        """
        with patch.object(Path, "read_bytes", autospec=True) as read_bytes_mock:
            exit_code, stdout, stderr, input_mock = _prompt(
                temp_pdf_file, json_output=True, return_value=""
            )

        assert exit_code == 0
        assert json_module.loads(stdout) == {"cancelled": True}
        assert "Submit to 'Assignment 1 - Signals'" in stderr
        assert "Confirm [y/N]:" in stderr
        assert "Submission cancelled." in stderr
        input_mock.assert_called_once_with()
        read_bytes_mock.assert_not_called()
        client.submit_file.assert_not_called()

    @pytest.mark.parametrize("json_output", [False, True])
    @pytest.mark.parametrize("input_error", [EOFError(), KeyboardInterrupt()])
    def test_confirmation_input_failure_cancels_cleanly(
        self,
        json_output: bool,
        input_error: BaseException,
        client: MagicMock,
        temp_pdf_file: Path,
    ) -> None:
        """EOF and Ctrl-C at confirmation never produce a traceback or POST."""
        exit_code, stdout, stderr, _ = _prompt(
            temp_pdf_file, json_output=json_output, side_effect=input_error
        )

        assert exit_code == 0
        assert "Traceback" not in stdout + stderr
        if json_output:
            assert "Submission cancelled." in stderr
            assert json_module.loads(stdout) == {"cancelled": True}
        else:
            assert "Submission cancelled." in stdout
        client.submit_file.assert_not_called()

    def test_json_confirmation_accepts_with_prompt_only_on_stderr(
        self, client: MagicMock, temp_pdf_file: Path, sample_submission_response: dict
    ) -> None:
        """Interactive JSON confirmation preserves a JSON-only stdout stream."""
        client.submit_file.return_value = sample_submission_response

        exit_code, stdout, stderr, input_mock = _prompt(
            temp_pdf_file, json_output=True, return_value="yes"
        )

        assert exit_code == 0
        assert json_module.loads(stdout)["submission_id"] == 99999
        assert "Submit to 'Assignment 1 - Signals'" not in stdout
        assert "Submit to 'Assignment 1 - Signals'" in stderr
        assert "Confirm [y/N]:" in stderr
        input_mock.assert_called_once_with()
        client.submit_file.assert_called_once()

    def test_human_confirmation_decline_remains_friendly(
        self, client: MagicMock, temp_pdf_file: Path
    ) -> None:
        """A human-mode decline keeps the existing friendly text output."""
        with patch.object(Path, "read_bytes", autospec=True) as read_bytes_mock:
            exit_code, stdout, stderr, input_mock = _prompt(
                temp_pdf_file, json_output=False, return_value="n"
            )

        assert exit_code == 0
        assert "Submit to 'Assignment 1 - Signals'" in stdout
        assert "Confirm [y/N]:" in stdout
        assert "Submission cancelled." in stdout
        assert stderr == ""
        input_mock.assert_called_once_with()
        read_bytes_mock.assert_not_called()
        client.submit_file.assert_not_called()


# ---------------------------------------------------------------------------
# Multipart request invariants
# ---------------------------------------------------------------------------

class TestSubmissionIntegration:
    """End-to-end invariants exercised with the HTTP transport mocked."""

    def test_multipart_boundary_is_unique(self, sample_submission_response: dict) -> None:
        """Each submission gets a fresh multipart boundary."""
        client, captured = _make_client_with_mock_session(200, sample_submission_response)

        _submit(client)
        _submit(client)

        assert len(captured) == 2
        boundaries = [request["headers"]["Content-Type"] for request in captured]
        assert boundaries[0] != boundaries[1]


class TestSubmitDryRun:
    """`submit --dry-run` resolves the destination read-only and never uploads."""

    @staticmethod
    def _client(detail: object = None) -> MagicMock:
        client = MagicMock()
        client.get_courses.return_value = [{"OrgUnitId": 44347, "Name": "Signals & Systems"}]
        client.get_dropbox_folders.return_value = [{"Id": 789, "Name": "Assignment 1 - Signals"}]
        client.get_dropbox_folder_detail.return_value = (
            {"Name": "Assignment 1 - Signals"} if detail is None else detail
        )
        return client

    def test_dry_run_reports_destination_without_reading_or_uploading(
        self, run_submit, temp_pdf_file: Path,
    ) -> None:
        client = self._client()
        with patch("lighthouse_cli.submit.LighthouseClient", return_value=client) as client_cls, \
                patch.object(Path, "read_bytes", side_effect=AssertionError("must not read the file")):
            result = run_submit("--dry-run", "--json")
        assert result.exit_code == 0, result.output
        data = json_module.loads(result.stdout)
        assert data == {
            "dry_run": True, "course_id": 44347, "course_name": "Signals & Systems",
            "folder_id": 789, "folder_name": "Assignment 1 - Signals", "folder_verified": True,
            "file": {"name": "test.pdf", "size_bytes": temp_pdf_file.stat().st_size},
        }
        client_cls.assert_called_once_with(read_only_auth=True)
        client.submit_file.assert_not_called()

    def test_dry_run_needs_no_yes_in_non_interactive_mode(self, run_submit) -> None:
        client = self._client()
        with patch("lighthouse_cli.submit.LighthouseClient", return_value=client):
            result = run_submit("--dry-run")
        assert result.exit_code == 0, result.output
        assert "Would submit to 'Assignment 1 - Signals' in 'Signals & Systems'" in result.output
        client.submit_file.assert_not_called()

    @pytest.mark.parametrize("detail", [RuntimeError("lookup failed"), {"Name": ""}, {}, {"Name": "x" * 300}])
    def test_dry_run_flags_a_folder_whose_name_could_not_be_read(
        self, run_submit, detail: object,
    ) -> None:
        client = self._client()
        if isinstance(detail, Exception):
            client.get_dropbox_folder_detail.side_effect = detail
        else:
            client.get_dropbox_folder_detail.return_value = detail
        with patch("lighthouse_cli.submit.LighthouseClient", return_value=client):
            result = run_submit("--dry-run", "--json")
        assert result.exit_code == 0
        data = json_module.loads(result.stdout)
        assert data["folder_verified"] is False
        assert data["folder_name"] == "Unknown folder"
        assert "No submission was sent" in data["warning"]
        client.submit_file.assert_not_called()

    def test_dry_run_verifies_a_folder_literally_named_like_the_fallback(self, run_submit) -> None:
        client = self._client(detail={"Name": "Unknown folder"})
        with patch("lighthouse_cli.submit.LighthouseClient", return_value=client):
            result = run_submit("--dry-run", "--json")
        assert result.exit_code == 0, result.output
        data = json_module.loads(result.stdout)
        assert data["folder_verified"] is True
        assert "warning" not in data

    def test_dry_run_still_reports_resolution_errors(self, run_submit) -> None:
        client = self._client()
        client.get_courses.return_value = []
        with patch("lighthouse_cli.submit.LighthouseClient", return_value=client):
            result = run_submit("--dry-run", "--json", course="nope")
        assert result.exit_code == 1
        assert json_module.loads(result.stdout)["error"]
        client.submit_file.assert_not_called()

    def test_real_submit_still_requires_yes_when_non_interactive(self, run_submit) -> None:
        """VAL-SUBMIT-006: without --yes a non-TTY caller is refused before any client exists."""
        with patch("lighthouse_cli.submit.LighthouseClient") as client_cls:
            result = run_submit()
        assert result.exit_code == 1
        assert "--yes" in result.output
        client_cls.assert_not_called()
