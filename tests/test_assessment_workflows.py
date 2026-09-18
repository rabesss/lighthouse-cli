"""Role workflows, connection boundaries and irreversible-request contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest
import requests
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient, NetworkError, SessionExpiredError
from lighthouse_cli.assessment_api import AssessmentAPI, AssessmentWriteUnknownError, assignment_payload, project, quiz_payload
from lighthouse_cli.cli import cli
from lighthouse_cli.config import COOKIE_NAMES
from lighthouse_cli.connection import connection_for
from lighthouse_cli.credential_store import CredentialStore
from lighthouse_cli.quiz_rules import navigation_rules


@pytest.mark.parametrize("layout,paging,prevent_back", [("all", 0, False), ("one-way", 1, True)])
def test_quiz_layout_round_trip(layout, paging, prevent_back):
    payload = quiz_payload("Practice", layout, 2)
    assert payload["IsActive"] is False
    assert payload["GradeItemId"] is None
    assert payload["AutoExportToGrades"] is False
    rules = navigation_rules(payload)
    assert rules["paging_type_id"] == paging
    assert rules["prevent_moving_backwards"] is prevent_back
    assert rules["can_revisit_previous_pages"] is not prevent_back


@pytest.mark.parametrize("paging,back", [(None, None), (True, "false"), (99, 0)])
def test_unknown_rules_do_not_grant_navigation(paging, back):
    rules = navigation_rules({"PagingTypeId": paging, "PreventMovingBackwards": back})
    assert rules["paging_type_id"] is None
    assert rules["can_revisit_previous_pages"] is None


def test_trial_urls_cookies_and_pagination_are_origin_scoped():
    client = LighthouseClient(site="trial")
    assert client.canonical_url("/22985/quizzes/") == "https://hetrynow.brightspace.com/d2l/api/le/1.93/22985/quizzes/"
    assert client.canonical_url("?page=2", base_url="/22985/quizzes/").endswith("/22985/quizzes/?page=2")
    with pytest.raises(NetworkError):
        client.get("https://lighthouse.manipal.edu/d2l/api/versions/")
    with pytest.raises(NetworkError):
        client.get("https://hetrynow.brightspace.com.evil.invalid/d2l/api/versions/")
    client._apply_cookies_to_session({key: "test" for key in COOKIE_NAMES})
    assert {cookie.domain for cookie in client._session.cookies} == {"hetrynow.brightspace.com"}
    assert client._read_only_auth


def test_trial_does_not_read_production_session():
    with patch("lighthouse_cli.api.load_cookies", return_value={}) as load:
        client = LighthouseClient(site="trial")
        assert client.cookies == {}
    kwargs = load.call_args.kwargs
    assert kwargs["config_dir"] == connection_for("trial").cookie_dir
    assert kwargs["expected_origin"] == connection_for("trial").origin
    assert kwargs["read_only"] is True


def test_dry_run_does_not_construct_client_or_write():
    with patch("lighthouse_cli.assessment_commands.LighthouseClient") as client:
        result = CliRunner().invoke(cli, ["instructor", "--site", "trial", "quiz-create", "22985", "--name", "Practice", "--layout", "one-way", "--dry-run", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["PagingTypeId"] == 1
    client.assert_not_called()


def test_write_requires_confirmation_before_session_access():
    with patch("lighthouse_cli.assessment_commands.LighthouseClient") as client:
        result = CliRunner().invoke(cli, ["instructor", "quiz-create", "12", "--name", "Practice", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"cancelled": True}
    client.assert_not_called()


def test_write_network_failure_is_not_replayed_and_is_not_reported_success():
    client = LighthouseClient()
    client._csrf_token = "synthetic-csrf"
    client._loaded = True
    client._cookies = {key: "test" for key in COOKIE_NAMES}
    client._session.request = Mock(side_effect=requests.ConnectionError("cookie=NEVER_PRINT"))
    with patch("lighthouse_cli.assessment_commands.LighthouseClient", return_value=client):
        result = CliRunner().invoke(cli, ["instructor", "assignment-create", "12", "--name", "Practice", "--yes", "--json"])
    assert result.exit_code == 1
    assert "unknown" in json.loads(result.stdout)["error"]
    assert "NEVER_PRINT" not in result.output
    assert client._session.request.call_count == 1


def test_learner_history_uses_my_submissions_and_retains_feedback():
    response = [{"Entity": {"EntityId": 7, "EntityType": "User"}, "Status": 3, "Feedback": {"Score": 4, "IsGraded": True}, "Submissions": [{"Id": 9, "Files": [{"FileId": 8, "FileName": "answer.txt"}]}], "Password": "NEVER_PRINT"}]
    with patch("lighthouse_cli.assessment_commands.LighthouseClient") as client:
        client.return_value.get_json.return_value = response
        result = CliRunner().invoke(cli, ["student", "assignment-history", "12", "34", "--json"])
    assert result.exit_code == 0
    client.return_value.get_json.assert_called_once_with("/12/dropbox/folders/34/submissions/mysubmissions/")
    data = json.loads(result.stdout)["data"][0]
    assert data["Status"] == 3
    assert data["Entity"] == {"EntityId": 7, "EntityType": "User"}
    assert data["Feedback"]["Score"] == 4
    assert "NEVER_PRINT" not in result.output
    client.return_value._session.close.assert_called_once()


def test_teacher_questions_follow_pagination():
    client = LighthouseClient()
    client.get_json = Mock(side_effect=[{"Objects": [{"QuestionId": 1}], "Next": "?page=2"}, {"Objects": [{"QuestionId": 2}], "Next": None}])
    assert AssessmentAPI(client, 12).questions(34) == [{"QuestionId": 1}, {"QuestionId": 2}]
    assert client.get_json.call_count == 2


def test_projection_never_exposes_unknown_or_secret_fields():
    result = project({"Name": "cookie=NEVER_PRINT", "Password": "NEVER_PRINT", "QuestionInfo": {"Token": "NEVER_PRINT", "Answers": [{"Text": "Four", "Weight": 100}]}, "Score": 10**1000})
    assert "NEVER_PRINT" not in json.dumps(result)
    assert result["QuestionInfo"]["Answers"][0]["Weight"] == 100
    assert result["Score"] is None


def test_projection_has_resource_limits():
    with pytest.raises(ValueError):
        project([None] * 20001)


def test_session_import_is_sealed_origin_bound_and_separate():
    document = {"origin": "https://hetrynow.brightspace.com", "cookies": {key: "SYNTHETIC_SESSION" for key in COOKIE_NAMES}}
    result = CliRunner().invoke(cli, ["auth", "import-session", "--site", "trial", "--json"], input=json.dumps(document))
    assert result.exit_code == 0
    assert "SYNTHETIC_SESSION" not in result.output
    store = CredentialStore(config_dir=connection_for("trial").cookie_dir)
    assert "SYNTHETIC_SESSION" not in store.cookie_file.read_text()
    assert not CredentialStore().cookie_file.exists()
    assert LighthouseClient(site="trial").cookies == document["cookies"]
    store.write_artifact(store.cookie_file, metadata={}, secret={"origin": "https://lighthouse.manipal.edu", "cookies": document["cookies"]})
    assert LighthouseClient(site="trial").cookies == {}


def test_session_import_rejects_wrong_origin_without_writes():
    document = {"origin": "https://wrong.invalid", "cookies": {key: "SYNTHETIC_SESSION" for key in COOKIE_NAMES}}
    result = CliRunner().invoke(cli, ["auth", "import-session", "--site", "trial", "--json"], input=json.dumps(document))
    assert result.exit_code == 1
    assert "SYNTHETIC_SESSION" not in result.output
    assert not CredentialStore(config_dir=connection_for("trial").cookie_dir).cookie_file.exists()


def test_trial_artifact_cannot_be_used_from_production_cookie_path():
    store = CredentialStore()
    store.write_artifact(store.cookie_file, metadata={}, secret={
        "origin": "https://hetrynow.brightspace.com",
        "cookies": {key: "SYNTHETIC_SESSION" for key in COOKIE_NAMES},
    })
    assert LighthouseClient(read_only_auth=True).cookies == {}


@pytest.mark.parametrize("submission_type,expected", [("file", 0), ("text", 1)])
def test_assignment_defaults_are_hidden_and_ungraded(submission_type, expected):
    data = assignment_payload("Practice", "Write an answer", submission_type)
    assert data["SubmissionType"] == expected
    assert data["IsHidden"] is True
    assert data["Assessment"] is None
    assert data["GradeItemId"] is None


def test_bad_create_input_has_json_error_and_no_side_effects():
    with patch("lighthouse_cli.assessment_commands.LighthouseClient") as client:
        result = CliRunner().invoke(cli, ["instructor", "quiz-create", "12", "--name", "", "--dry-run", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]
    client.assert_not_called()


def test_role_group_usage_errors_preserve_json_contract():
    result = CliRunner().invoke(cli, ["instructor", "--site", "bogus", "quizzes", "12", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]


def test_role_group_unknown_command_preserves_json_contract():
    result = CliRunner().invoke(cli, ["instructor", "unknown", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]


@pytest.mark.parametrize("role", ["student", "instructor"])
def test_discussion_post_routes_preserve_hierarchy_and_message(role):
    with patch("lighthouse_cli.assessment_commands.LighthouseClient") as client:
        client.return_value.get_json.return_value = {"PostId": 4, "Message": {"Text": "Sample post"}, "PostingUserDisplayName": "Sample Student"}
        result = CliRunner().invoke(cli, [role, "post", "1", "2", "3", "4", "--json"])
    assert result.exit_code == 0
    client.return_value.get_json.assert_called_once_with("/1/discussions/forums/2/topics/3/posts/4")
    assert json.loads(result.stdout)["data"]["Message"]["Text"] == "Sample post"


def test_quiz_create_requires_verifiable_success_identifier():
    client = LighthouseClient()
    client._csrf_token = "synthetic-csrf"
    response = Mock(status_code=200)
    response.json.return_value = {"unexpected": "secret=NEVER_PRINT"}
    client._request = Mock(return_value=response)
    with pytest.raises(NetworkError, match="could not be verified"):
        AssessmentAPI(client, 12).write("POST", "quiz", quiz_payload("Test", "all", 1))
    client._request.assert_called_once()
    response.close.assert_called_once()


def test_quiz_create_204_is_an_unknown_write_outcome():
    client = LighthouseClient()
    client._csrf_token = "synthetic-csrf"
    response = Mock(status_code=204)
    client._request = Mock(return_value=response)
    with pytest.raises(AssessmentWriteUnknownError, match="could not be verified"):
        AssessmentAPI(client, 12).write("POST", "quiz", quiz_payload("Test", "all", 1))
    response.close.assert_called_once()


def test_assessment_session_expiry_during_write_is_an_unknown_write_outcome():
    client = LighthouseClient()
    client._csrf_token = "synthetic-csrf"
    client._request = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(AssessmentWriteUnknownError, match="outcome unknown"):
        AssessmentAPI(client, 12).write("POST", "quiz", quiz_payload("Test", "all", 1))


def test_assessment_csrf_bootstrap_failure_is_retryable_before_write():
    client = LighthouseClient()
    client.get_csrf_token = Mock(side_effect=SessionExpiredError("session expired"))
    client._request = Mock()
    with pytest.raises(SessionExpiredError):
        AssessmentAPI(client, 12).write("POST", "quiz", quiz_payload("Test", "all", 1))
    client._request.assert_not_called()


@pytest.mark.parametrize("status", [429, 502])
def test_ambiguous_http_write_status_is_unknown(status):
    client = LighthouseClient()
    client._csrf_token = "synthetic-csrf"
    response = requests.Response()
    response.status_code = status
    client._request = Mock(side_effect=requests.HTTPError(response=response))
    with pytest.raises(AssessmentWriteUnknownError, match="outcome unknown"):
        AssessmentAPI(client, 12).write("POST", "quiz", quiz_payload("Test", "all", 1))


def test_classlist_uses_the_le_route():
    with patch("lighthouse_cli.assessment_commands.LighthouseClient") as client:
        client.return_value.get_json.return_value = []
        result = CliRunner().invoke(cli, ["instructor", "classlist", "12", "--json"])
    assert result.exit_code == 0
    client.return_value.get_json.assert_called_once_with("/12/classlist/")


def test_pagination_preserves_forbidden_status_without_raw_error():
    client = LighthouseClient()
    response = requests.Response()
    response.status_code = 403
    client.get_json = Mock(side_effect=requests.HTTPError("cookie=NEVER_PRINT", response=response))
    with pytest.raises(requests.HTTPError) as error:
        client._paginate_list("/12/surveys/")
    assert error.value.response.status_code == 403
    assert "NEVER_PRINT" not in str(error.value)


def test_section_reader_uses_lp_api_not_le():
    with patch("lighthouse_cli.assessment_commands.LighthouseClient") as client:
        client.return_value._paginate_list.return_value = [{"SectionId": 3, "Name": "Section A"}]
        result = CliRunner().invoke(cli, ["student", "my-sections", "12", "--json"])
    assert result.exit_code == 0
    client.return_value._paginate_list.assert_called_once_with("/d2l/api/lp/1.47/12/sections/mysections/")
    assert json.loads(result.stdout)["data"][0]["SectionId"] == 3


def test_cli_import_does_not_load_auth_http_or_assessment_implementations():
    result = subprocess.run([
        sys.executable, "-B", "-c",
        "import sys; import lighthouse_cli.cli; "
        "assert not {'requests', 'bs4', 'lighthouse_cli.ms_auth', "
        "'lighthouse_cli.assessment_commands'} & sys.modules.keys()",
    ], capture_output=True, timeout=10)
    assert result.returncode == 0
