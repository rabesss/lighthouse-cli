"""Draft-only instructor assignment feedback (issue #35).

The fixtures below are synthetic and follow the documented D2L shapes
(docs.valence.desire2learn.com/res/dropbox.html): Dropbox.EntityDropbox from
``GET .../submissions/user/(userId)`` and Dropbox.DropboxFeedbackOut from
``GET .../feedback/user/(userId)``. ``IsGraded: false`` is the documented draft
state; ENTITYDROPBOXSTATUS_T is 0 Unsubmitted, 1 Submitted, 2 Draft, 3 Published.
Feedback is a RichText block (``{"Text", "Html"}``, either part may be absent).
No live request is made.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import requests
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.assessment_api import AssessmentAPI
from lighthouse_cli.assignment_feedback import (
    _FEEDBACK_KEYS,
    MAX_FEEDBACK_CHARS,
    FeedbackInputError,
    FeedbackRefusedError,
    _check_saved_comment,
    _comparable,
    _html,
    _html_text,
    _UnexpectedError,
    check_feedback_target,
    feedback_payload,
    read_feedback_file,
)
from lighthouse_cli.cli import cli
from lighthouse_cli.config import COOKIE_NAMES

COURSE, FOLDER, USER = 12, 34, 56
TEXT = "Synthetic draft note: revisit the second proof.\nLine two."
CSRF = "synthetic-csrf-NEVER_PRINT"
COOKIE = "synthetic-cookie-NEVER_PRINT"
ROOT = "https://lighthouse.manipal.edu/d2l/api/le/1.93"
SUBMISSIONS_URL = f"{ROOT}/{COURSE}/dropbox/folders/{FOLDER}/submissions/user/{USER}"
FEEDBACK_URL = f"{ROOT}/{COURSE}/dropbox/folders/{FOLDER}/feedback/user/{USER}"
FOLDER_URL = f"{ROOT}/{COURSE}/dropbox/folders/{FOLDER}"
INDIVIDUAL_FOLDER = {"Id": FOLDER, "Name": "Synthetic assignment", "GroupTypeId": None}
WHOAMI_URL = "https://lighthouse.manipal.edu/d2l/api/lp/1.47/users/whoami"

ENTITY_NO_FEEDBACK: dict[str, Any] = {
    "Entity": {"EntityId": USER, "EntityType": "User", "DisplayName": "Synthetic Learner"},
    "Status": 1,
    "Feedback": None,
    "Submissions": [{"Id": 901, "SubmittedBy": {"Id": str(USER), "DisplayName": "Synthetic Learner"},
                     "SubmissionDate": "2026-11-01T10:00:00.000Z",
                     "Comment": {"Text": "", "Html": ""}, "Files": []}],
}


def draft_feedback_out(content: str = TEXT, **overrides: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "Score": None, "Feedback": {"Text": content, "Html": None},
        "RubricAssessments": [], "IsGraded": False, "Files": [], "Links": [], "GradedSymbol": None,
    }
    out.update(overrides)
    return out


def entity_with(feedback: Any = None, **overrides: Any) -> dict[str, Any]:
    entity = copy.deepcopy(ENTITY_NO_FEEDBACK)
    entity["Feedback"] = feedback
    entity.update(overrides)
    return entity


def response(status: int, body: Any = None, *, raw: bytes | None = None, headers: dict[str, str] | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
    resp.headers.update({"Content-Type": "application/json", **(headers or {})})
    resp.url = "https://lighthouse.manipal.edu/redacted"
    return resp


Route = Callable[[], requests.Response]


class FakeLMS:
    """Answers the session's requests by (method, url) and records every call."""

    def __init__(self, routes: dict[tuple[str, str], list[Any]]) -> None:
        self.routes = {key: list(value) for key, value in routes.items()}
        self.routes.setdefault(("GET", FOLDER_URL), [lambda: response(200, INDIVIDUAL_FOLDER)])
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append((method, url, kwargs))
        queue = self.routes.get((method, url))
        if not queue:
            raise AssertionError(f"unexpected request {method} {url}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item() if callable(item) else item

    def methods(self, method: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [call for call in self.calls if call[0] == method]


def make_client(fake: FakeLMS, *, read_only_auth: bool = False) -> LighthouseClient:
    client = LighthouseClient(read_only_auth=read_only_auth)
    client._loaded = True
    client._cookies = dict.fromkeys(COOKIE_NAMES, COOKIE)
    client._csrf_token = CSRF
    client._session.request = fake  # type: ignore[method-assign]
    return client


def happy_routes(**changes: list[Any]) -> dict[tuple[str, str], list[Any]]:
    routes: dict[tuple[str, str], list[Any]] = {
        ("GET", SUBMISSIONS_URL): [
            lambda: response(200, [ENTITY_NO_FEEDBACK]),
            lambda: response(200, [entity_with(draft_feedback_out(), Status=2)]),
        ],
        ("POST", FEEDBACK_URL): [lambda: response(200, None)],
        ("GET", FEEDBACK_URL): [lambda: response(200, draft_feedback_out())],
        # A 403 read asks whoami whether the session ended; here it has not.
        ("GET", WHOAMI_URL): [lambda: response(200, {"Identifier": "1"})],
    }
    for key, value in changes.items():
        method, which = key.split("_", 1)
        url = SUBMISSIONS_URL if which == "submissions" else FEEDBACK_URL
        routes[(method.upper(), url)] = value
    return routes


@pytest.fixture
def feedback_file(tmp_path: Path) -> Path:
    path = tmp_path / "feedback.txt"
    path.write_text(TEXT, encoding="utf-8")
    return path


def invoke(fake: FakeLMS, *args: str, read_only: list[bool] | None = None) -> Any:
    built: list[LighthouseClient] = []

    def factory(*_a: Any, read_only_auth: bool = False, **_k: Any) -> LighthouseClient:
        if read_only is not None:
            read_only.append(read_only_auth)
        client = make_client(fake, read_only_auth=read_only_auth)
        built.append(client)
        return client

    with patch("lighthouse_cli.api.LighthouseClient", side_effect=factory):
        result = CliRunner().invoke(cli, ["instructor", "assignment-feedback", str(COURSE), str(FOLDER), str(USER), *args])
    return result


def assert_no_leak(result: Any) -> None:
    output = result.stdout + result.stderr
    for secret in (TEXT, "revisit the second proof", "NEVER_PRINT", "Synthetic Learner"):
        assert secret not in output


# -- payload and input ---------------------------------------------------------


def test_payload_is_comment_only_and_draft():
    assert feedback_payload("Note") == {
        "Score": None, "Feedback": {"Text": "Note", "Html": "<p>Note</p>"},
        "RubricAssessments": [], "IsGraded": False, "GradedSymbol": None,
    }


def test_payload_html_part_is_escaped():
    assert feedback_payload('<b>"x" & y</b>\nz')["Feedback"]["Html"] == (
        "<p>&lt;b&gt;&quot;x&quot; &amp; y&lt;/b&gt;<br />z</p>")


@pytest.mark.parametrize("text", ["", "   \n\t", "a\x00b", "x" * (MAX_FEEDBACK_CHARS + 1)])
def test_payload_rejects_unsafe_text(text):
    with pytest.raises(FeedbackInputError):
        feedback_payload(text)


def test_payload_accepts_the_maximum_length():
    assert feedback_payload("x" * MAX_FEEDBACK_CHARS)["Feedback"]["Text"] == "x" * MAX_FEEDBACK_CHARS


def test_file_is_strict_utf8_with_bom_stripped_and_newlines_normalized(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"\xef\xbb\xbfCaf\xc3\xa9\r\nline")
    assert read_feedback_file(str(path)) == "Café\nline"


@pytest.mark.parametrize("content", [b"\xff\xfe bad", b"", b"  \n", b"x" * (4 * MAX_FEEDBACK_CHARS + 8)],
                         ids=["invalid-utf8", "empty", "blank", "too-large"])
def test_bad_file_fails_before_any_client_or_request(tmp_path, content):
    path = tmp_path / "f.txt"
    path.write_bytes(content)
    with patch("lighthouse_cli.api.LighthouseClient") as client:
        result = CliRunner().invoke(cli, ["instructor", "assignment-feedback", "12", "34", "56",
                                          "--file", str(path), "--yes", "--json"])
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["data"] is None and document["error"]
    client.assert_not_called()


@pytest.mark.parametrize("json_flag", [["--json"], []], ids=["json", "human"])
@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_unreadable_path_fails_without_client_or_echoing_the_path(tmp_path, json_flag, kind):
    path = tmp_path / "NEVER_PRINT secret path"
    if kind == "directory":
        path.mkdir()
    with patch("lighthouse_cli.api.LighthouseClient") as client:
        result = CliRunner().invoke(cli, ["instructor", "assignment-feedback", "12", "34", "56",
                                          "--file", str(path), "--yes", *json_flag])
    assert result.exit_code == 1
    assert "NEVER_PRINT" not in result.stdout + result.stderr
    if json_flag:
        assert json.loads(result.stdout) == {"course_id": 12, "folder_id": 34, "user_id": 56,
                                             "data": None, "error": "Could not read the feedback file."}
    client.assert_not_called()


@pytest.mark.parametrize("ids", [["0", "34", "56"], ["12", "-1", "56"], ["12", "34", "0"]])
def test_ids_must_be_positive(feedback_file, ids):
    with patch("lighthouse_cli.api.LighthouseClient") as client:
        result = CliRunner().invoke(cli, ["instructor", "assignment-feedback", *ids, "--file", str(feedback_file), "--yes"])
    assert result.exit_code != 0
    client.assert_not_called()


def test_there_is_no_score_or_publish_option(feedback_file):
    for option in ("--score", "--publish"):
        result = CliRunner().invoke(cli, ["instructor", "assignment-feedback", "12", "34", "56",
                                          "--file", str(feedback_file), option, "1"])
        assert result.exit_code == 2


# -- preflight -----------------------------------------------------------------


def test_preflight_accepts_the_status_as_a_digit_string():
    assert check_feedback_target([entity_with(None, Status="1")], USER, replace_draft=False).replaces_draft is False


def test_preflight_accepts_a_single_object_or_a_one_item_list():
    assert check_feedback_target(ENTITY_NO_FEEDBACK, USER, replace_draft=False).replaces_draft is False
    assert check_feedback_target([ENTITY_NO_FEEDBACK], USER, replace_draft=False).replaces_draft is False


def test_empty_draft_feedback_object_is_not_a_replacement():
    empty = draft_feedback_out(content="")
    assert check_feedback_target([entity_with(empty)], USER, replace_draft=False).replaces_draft is False
    both_empty = draft_feedback_out(Feedback={"Text": "", "Html": ""})
    assert check_feedback_target([entity_with(both_empty)], USER, replace_draft=False).replaces_draft is False


@pytest.mark.parametrize("rich", [None, {}, {"Text": None, "Html": None}], ids=["null", "empty-object", "null-parts"])
def test_existing_feedback_with_an_unreadable_comment_needs_replace_draft(rich):
    data = [entity_with(draft_feedback_out(Feedback=rich), Status=2)]
    with pytest.raises(FeedbackRefusedError, match="--replace-draft"):
        check_feedback_target(data, USER, replace_draft=False)
    assert check_feedback_target(data, USER, replace_draft=True).replaces_draft is True


@pytest.mark.parametrize("rich", [{"Text": "Older synthetic draft", "Html": None},
                                  {"Html": "<p>Older synthetic draft</p>"}], ids=["text", "html-only"])
def test_existing_draft_text_needs_replace_draft(rich):
    data = [entity_with(draft_feedback_out(Feedback=rich), Status=2)]
    with pytest.raises(FeedbackRefusedError, match="--replace-draft"):
        check_feedback_target(data, USER, replace_draft=False)
    assert check_feedback_target(data, USER, replace_draft=True).replaces_draft is True


REFUSALS: dict[str, Any] = {
    "no-submission-entity": [],
    "two-entities": [ENTITY_NO_FEEDBACK, ENTITY_NO_FEEDBACK],
    "other-user": [entity_with(Entity={"EntityId": USER + 1, "EntityType": "User"})],
    "bool-user-id": [entity_with(Entity={"EntityId": True, "EntityType": "User"})],
    "string-user-id": [entity_with(Entity={"EntityId": str(USER), "EntityType": "User"})],
    "group-entity": [entity_with(Entity={"EntityId": USER, "EntityType": "Group"})],
    "no-entity": [entity_with(Entity=None)],
    "no-submissions": [entity_with(Submissions=[])],
    "submissions-not-list": [entity_with(Submissions="1")],
    "published-status": [entity_with(draft_feedback_out(), Status=3)],
    "published-name-status": [entity_with(None, Status="Published")],
    "bool-status": [entity_with(None, Status=True)],
    "missing-status": [{k: v for k, v in ENTITY_NO_FEEDBACK.items() if k != "Status"}],
    "unknown-status": [entity_with(None, Status=7)],
    "unsubmitted-status": [entity_with(None, Status=0)],
    "draft-status-without-feedback": [entity_with(None, Status=2)],
    "graded-feedback": [entity_with(draft_feedback_out(IsGraded=True))],
    "missing-is-graded": [entity_with({k: v for k, v in draft_feedback_out().items() if k != "IsGraded"})],
    "string-is-graded": [entity_with(draft_feedback_out(IsGraded="false"))],
    "draft-score": [entity_with(draft_feedback_out("", Score=7))],
    "zero-score": [entity_with(draft_feedback_out("", Score=0))],
    "graded-symbol": [entity_with(draft_feedback_out("", GradedSymbol="A"))],
    "rubric": [entity_with(draft_feedback_out("", RubricAssessments=[{"RubricId": 1}]))],
    "files": [entity_with(draft_feedback_out("", Files=[{"FileId": 1}]))],
    "links": [entity_with(draft_feedback_out("", Links=[{"LinkId": 1}]))],
    "files-not-list": [entity_with(draft_feedback_out("", Files="x"))],
    "feedback-not-dict": [entity_with("text")],
    "rich-text-not-dict": [entity_with(draft_feedback_out(Feedback="text"))],
    "text-not-str": [entity_with(draft_feedback_out(Feedback={"Text": 5, "Html": ""}))],
    "html-not-str": [entity_with(draft_feedback_out(Feedback={"Text": "", "Html": ["x"]}))],
    "empty-feedback-object": [entity_with({})],
    **{f"feedback-missing-{key}": [entity_with({k: v for k, v in draft_feedback_out("").items() if k != key})]
       for key in _FEEDBACK_KEYS},
    "rubric-null": [entity_with(draft_feedback_out("", RubricAssessments=None))],
    "feedback-key-absent": [{k: v for k, v in ENTITY_NO_FEEDBACK.items() if k != "Feedback"}],
    "not-json-object": ["text"],
    "scalar": 5,
}


@pytest.mark.parametrize("data", list(REFUSALS.values()), ids=list(REFUSALS))
def test_preflight_refuses_unsafe_or_unexpected_targets(data):
    with pytest.raises(FeedbackRefusedError):
        check_feedback_target(data, USER, replace_draft=True)


@pytest.mark.parametrize("case", ["other-user", "group-entity", "published-status", "graded-feedback", "draft-score", "no-submissions"])
def test_cli_preflight_refusal_sends_no_write(feedback_file, case):
    fake = FakeLMS({("GET", SUBMISSIONS_URL): [lambda: response(200, REFUSALS[case])]})
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--replace-draft", "--json")
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["data"] is None and document["error"]
    assert [(m, u) for m, u, _k in fake.calls] == [("GET", FOLDER_URL), ("GET", SUBMISSIONS_URL)]
    assert_no_leak(result)


@pytest.mark.parametrize("status,phrase", [(403, "HTTP 403"), (404, "HTTP 404")])
def test_cli_preflight_permission_or_identity_errors_send_no_write(feedback_file, status, phrase):
    fake = FakeLMS({("GET", SUBMISSIONS_URL): [lambda: response(status, {"Detail": "NEVER_PRINT"})],
                    ("GET", WHOAMI_URL): [lambda: response(200, {"Identifier": "1"})]})
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 1
    assert phrase in json.loads(result.stdout)["error"]
    assert fake.methods("POST") == []
    assert_no_leak(result)


@pytest.mark.parametrize("folder", [
    lambda: response(200, {**INDIVIDUAL_FOLDER, "GroupTypeId": 77}),
    lambda: response(200, ["not", "a", "folder"]),
    lambda: response(403, {}),
    lambda: response(404, {}),
], ids=["group-folder", "not-object", "403", "404"])
def test_folder_preflight_refuses_group_or_unreadable_assignments(feedback_file, folder):
    fake = FakeLMS({("GET", FOLDER_URL): [folder], ("GET", WHOAMI_URL): [lambda: response(200, {"Identifier": "1"})]})
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 1
    assert "Nothing was sent" in json.loads(result.stdout)["error"]
    assert ("GET", SUBMISSIONS_URL) not in [(m, u) for m, u, _k in fake.calls]
    assert fake.methods("POST") == []


def test_group_folder_with_a_user_shaped_record_is_still_refused(feedback_file):
    fake = FakeLMS({("GET", FOLDER_URL): [lambda: response(200, {**INDIVIDUAL_FOLDER, "GroupTypeId": 77})],
                    ("GET", SUBMISSIONS_URL): [lambda: response(200, [ENTITY_NO_FEEDBACK])]})
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 1
    assert "group assignment" in json.loads(result.stdout)["error"]
    assert fake.methods("POST") == []


# -- dry run and confirmation --------------------------------------------------


def test_dry_run_reads_with_read_only_auth_and_never_writes(feedback_file):
    fake = FakeLMS({("GET", SUBMISSIONS_URL): [lambda: response(200, [ENTITY_NO_FEEDBACK])]})
    read_only: list[bool] = []
    result = invoke(fake, "--file", str(feedback_file), "--dry-run", "--json", read_only=read_only)
    assert result.exit_code == 0, result.output
    assert read_only == [True]
    document = json.loads(result.stdout)
    assert document == {
        "course_id": COURSE, "folder_id": FOLDER, "user_id": USER, "host": "lighthouse.manipal.edu",
        "dry_run": True, "operation": "save-draft-feedback",
        "visibility": "draft: not published, not visible to the learner, no score",
        "replaces_draft": False, "feedback_characters": len(TEXT),
    }
    assert [(m, u) for m, u, _k in fake.calls] == [("GET", FOLDER_URL), ("GET", SUBMISSIONS_URL)]
    assert_no_leak(result)


def test_dry_run_still_refuses_an_unsafe_target(feedback_file):
    fake = FakeLMS({("GET", SUBMISSIONS_URL): [lambda: response(200, REFUSALS["graded-feedback"])]})
    result = invoke(fake, "--file", str(feedback_file), "--dry-run", "--json")
    assert result.exit_code == 1
    assert fake.methods("POST") == []


def test_non_interactive_without_yes_cancels_before_any_request(feedback_file):
    with patch("lighthouse_cli.api.LighthouseClient") as client:
        result = CliRunner().invoke(cli, ["instructor", "assignment-feedback", "12", "34", "56",
                                          "--file", str(feedback_file), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"cancelled": True}
    client.assert_not_called()


def test_declined_confirmation_names_destination_and_visibility_and_sends_no_write(feedback_file):
    fake = FakeLMS({("GET", SUBMISSIONS_URL): [lambda: response(200, [ENTITY_NO_FEEDBACK])]})
    with patch("lighthouse_cli.assessment_commands._interactive", return_value=True), patch("click.confirm", return_value=False) as confirm:
        result = invoke(fake, "--file", str(feedback_file), "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"cancelled": True}
    prompt = confirm.call_args.args[0]
    for part in ("DRAFT", "not published", "not visible to the learner", "no score",
                 f"user {USER}", f"assignment {FOLDER}", f"course {COURSE}", "lighthouse.manipal.edu"):
        assert part in prompt
    assert confirm.call_args.kwargs.get("err") is True
    assert fake.methods("POST") == []


def test_confirmed_save_checks_the_target_again_just_before_the_post(feedback_file):
    fake = FakeLMS(happy_routes(get_submissions=[
        lambda: response(200, [ENTITY_NO_FEEDBACK]), lambda: response(200, [ENTITY_NO_FEEDBACK]),
        lambda: response(200, [entity_with(draft_feedback_out(), Status=2)])]))
    with patch("lighthouse_cli.assessment_commands._interactive", return_value=True), patch("click.confirm", return_value=True):
        result = invoke(fake, "--file", str(feedback_file), "--json")
    assert result.exit_code == 0, result.output
    assert [(m, u) for m, u, _k in fake.calls] == [
        ("GET", FOLDER_URL), ("GET", SUBMISSIONS_URL), ("GET", FOLDER_URL), ("GET", SUBMISSIONS_URL),
        ("POST", FEEDBACK_URL), ("GET", FEEDBACK_URL), ("GET", SUBMISSIONS_URL),
    ]


@pytest.mark.parametrize("later", [
    [entity_with(draft_feedback_out(IsGraded=True))],
    [entity_with(draft_feedback_out(), Status=3)],
    [entity_with(draft_feedback_out("Another assessor's synthetic draft"), Status=2)],
], ids=["graded-meanwhile", "published-meanwhile", "draft-added-meanwhile"])
def test_target_changed_during_confirmation_sends_nothing(feedback_file, later):
    fake = FakeLMS({("GET", SUBMISSIONS_URL): [lambda: response(200, [ENTITY_NO_FEEDBACK]), lambda: response(200, later)]})
    with patch("lighthouse_cli.assessment_commands._interactive", return_value=True), patch("click.confirm", return_value=True):
        result = invoke(fake, "--file", str(feedback_file), "--json")
    assert result.exit_code == 1
    assert "Nothing was sent" in json.loads(result.stdout)["error"]
    assert fake.methods("POST") == []
    assert_no_leak(result)


def test_replace_draft_refuses_if_the_draft_text_changes_during_confirmation(feedback_file):
    first = [entity_with(draft_feedback_out("Older synthetic draft"), Status=2)]
    later = [entity_with(draft_feedback_out("Another assessor's newer synthetic draft"), Status=2)]
    fake = FakeLMS({("GET", SUBMISSIONS_URL): [lambda: response(200, first), lambda: response(200, later)]})
    with patch("lighthouse_cli.assessment_commands._interactive", return_value=True), patch("click.confirm", return_value=True):
        result = invoke(fake, "--file", str(feedback_file), "--replace-draft", "--json")
    assert result.exit_code == 1
    assert "changed while you were confirming" in json.loads(result.stdout)["error"]
    assert fake.methods("POST") == []
    assert "Another assessor" not in result.stdout + result.stderr


def test_ctrl_c_at_the_prompt_is_a_cancel(feedback_file):
    import click

    fake = FakeLMS({("GET", SUBMISSIONS_URL): [lambda: response(200, [ENTITY_NO_FEEDBACK])]})
    with patch("lighthouse_cli.assessment_commands._interactive", return_value=True), \
            patch("click.confirm", side_effect=click.Abort()):
        result = invoke(fake, "--file", str(feedback_file), "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"cancelled": True}
    assert fake.methods("POST") == []


def test_replace_draft_refuses_if_the_draft_changes_during_confirmation(feedback_file):
    older = [entity_with(draft_feedback_out("Older synthetic draft"), Status=2)]
    fake = FakeLMS({("GET", SUBMISSIONS_URL): [lambda: response(200, [ENTITY_NO_FEEDBACK]), lambda: response(200, older)]})
    with patch("lighthouse_cli.assessment_commands._interactive", return_value=True), patch("click.confirm", return_value=True):
        result = invoke(fake, "--file", str(feedback_file), "--replace-draft", "--json")
    assert result.exit_code == 1
    assert "changed while you were confirming" in json.loads(result.stdout)["error"]
    assert fake.methods("POST") == []


def test_confirmation_says_when_a_draft_is_replaced(feedback_file):
    data = [entity_with(draft_feedback_out("Older synthetic draft"), Status=2)]
    fake = FakeLMS({("GET", SUBMISSIONS_URL): [lambda: response(200, data)]})
    with patch("lighthouse_cli.assessment_commands._interactive", return_value=True), patch("click.confirm", return_value=False) as confirm:
        invoke(fake, "--file", str(feedback_file), "--replace-draft", "--json")
    assert "replacing the existing draft" in confirm.call_args.args[0]


# -- save and verify -----------------------------------------------------------


def test_save_posts_once_then_verifies_draft_through_readback(feedback_file):
    fake = FakeLMS(happy_routes())
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "course_id": COURSE, "folder_id": FOLDER, "user_id": USER, "host": "lighthouse.manipal.edu",
        "visibility": "draft: not published, not visible to the learner, no score",
        "replaces_draft": False, "feedback_characters": len(TEXT),
        "outcome": "saved-draft", "is_graded": False,
    }
    posts = fake.methods("POST")
    assert len(posts) == 1
    _method, url, kwargs = posts[0]
    assert url == FEEDBACK_URL
    assert kwargs["json"] == feedback_payload(TEXT)
    assert kwargs["headers"]["X-Csrf-Token"] == CSRF
    assert [(m, u) for m, u, _k in fake.calls] == [
        ("GET", FOLDER_URL), ("GET", SUBMISSIONS_URL), ("POST", FEEDBACK_URL), ("GET", FEEDBACK_URL),
        ("GET", SUBMISSIONS_URL),
    ]
    assert_no_leak(result)


def test_save_human_output_has_no_feedback_text(feedback_file):
    fake = FakeLMS(happy_routes())
    result = invoke(fake, "--file", str(feedback_file), "--yes")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["outcome"] == "saved-draft"
    assert_no_leak(result)


def test_replace_draft_saves_over_a_text_only_draft(feedback_file):
    older = [entity_with(draft_feedback_out("Older synthetic draft"), Status=2)]
    fake = FakeLMS(happy_routes(get_submissions=[
        lambda: response(200, older), lambda: response(200, [entity_with(draft_feedback_out(), Status=2)])]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--replace-draft", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["replaces_draft"] is True
    assert len(fake.methods("POST")) == 1


@pytest.mark.parametrize("error", [requests.ConnectionError("NEVER_PRINT"), requests.Timeout("NEVER_PRINT")],
                         ids=["connection", "timeout"])
def test_transport_failure_after_dispatch_is_unknown_and_not_retried(feedback_file, error):
    fake = FakeLMS(happy_routes(post_feedback=[error]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["outcome"] == "unknown" and document["data"] is None
    assert "unknown" in document["error"].lower()
    assert len(fake.methods("POST")) == 1
    assert_no_leak(result)


@pytest.mark.parametrize("post", [
    lambda: response(401, {}), lambda: response(429, {}, headers={"Retry-After": "0"}), lambda: response(502, {}),
    lambda: response(302, None, headers={"Location": "https://lighthouse.manipal.edu/d2l/login"}),
    lambda: response(302, None, headers={"Location": "https://elsewhere.example/"}),
], ids=["401-expired", "429", "502", "login-redirect", "other-redirect"])
def test_ambiguous_post_status_is_unknown_and_not_retried(feedback_file, post):
    fake = FakeLMS(happy_routes(post_feedback=[post]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["outcome"] == "unknown"
    assert len(fake.methods("POST")) == 1
    assert [(m, u) for m, u, _k in fake.calls] == [("GET", FOLDER_URL), ("GET", SUBMISSIONS_URL), ("POST", FEEDBACK_URL)]


@pytest.mark.parametrize("status", [400, 403, 404])
def test_documented_post_refusal_is_reported_as_refused_not_saved(feedback_file, status):
    fake = FakeLMS(happy_routes(post_feedback=[lambda: response(status, {"Detail": "NEVER_PRINT"})]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert f"HTTP {status}" in document["error"]
    assert document["outcome"] == "refused"
    assert len(fake.methods("POST")) == 1
    assert_no_leak(result)


@pytest.mark.parametrize("status", [405, 408, 409, 412, 422])
def test_undocumented_post_status_is_unknown_not_a_refusal(feedback_file, status):
    fake = FakeLMS(happy_routes(post_feedback=[lambda: response(status, {"Detail": "NEVER_PRINT"})]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["outcome"] == "unknown"
    assert len(fake.methods("POST")) == 1
    assert_no_leak(result)


@pytest.mark.parametrize("status", [201, 202, 204, 300, 304])
def test_post_status_other_than_documented_200_is_unknown(feedback_file, status):
    fake = FakeLMS(happy_routes(post_feedback=[lambda: response(status, None)]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["outcome"] == "unknown"
    assert "saved-draft" not in result.stdout
    assert len(fake.methods("POST")) == 1
    assert [m for m, _u, _k in fake.calls].count("GET") == 2


def test_interrupt_during_the_save_reports_unknown(feedback_file):
    fake = FakeLMS(happy_routes(post_feedback=[KeyboardInterrupt()]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["outcome"] == "unknown"
    assert len(fake.methods("POST")) == 1


@pytest.mark.parametrize("readback", [
    lambda: response(200, draft_feedback_out(IsGraded=True)),
    lambda: response(200, draft_feedback_out(Score=4)),
], ids=["graded", "scored"])
def test_readback_showing_published_or_graded_says_so(feedback_file, readback):
    fake = FakeLMS(happy_routes(get_feedback=[readback]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    document = json.loads(result.stdout)
    assert document["outcome"] == "unknown"
    assert "published, graded or scored" in document["error"] and "do not retry" in document["error"]


BAD_READBACKS: dict[str, dict[str, list[Any]]] = {
    "graded": {"get_feedback": [lambda: response(200, draft_feedback_out(IsGraded=True))]},
    "is-graded-missing": {"get_feedback": [lambda: response(200, {k: v for k, v in draft_feedback_out().items() if k != "IsGraded"})]},
    "text-mismatch": {"get_feedback": [lambda: response(200, draft_feedback_out("Different synthetic text"))]},
    "score-present": {"get_feedback": [lambda: response(200, draft_feedback_out(Score=3))]},
    "symbol-present": {"get_feedback": [lambda: response(200, draft_feedback_out(GradedSymbol="A"))]},
    "rubric-added": {"get_feedback": [lambda: response(200, draft_feedback_out(RubricAssessments=[{"RubricId": 1}]))]},
    "file-added": {"get_feedback": [lambda: response(200, draft_feedback_out(Files=[{"FileId": 1}]))]},
    "readback-incomplete": {"get_feedback": [lambda: response(200, {k: v for k, v in draft_feedback_out().items() if k != "Files"})]},
    "entity-feedback-file": {"get_submissions": [
        lambda: response(200, [ENTITY_NO_FEEDBACK]),
        lambda: response(200, [entity_with(draft_feedback_out(Files=[{"FileId": 1}]), Status=2)])]},
    "rich-text-missing": {"get_feedback": [lambda: response(200, draft_feedback_out(Feedback=None))]},
    "text-matches-html-differs": {"get_feedback": [
        lambda: response(200, draft_feedback_out(Feedback={"Text": TEXT, "Html": "<p>Different synthetic text</p>"}))]},
    "html-matches-text-differs": {"get_feedback": [
        lambda: response(200, draft_feedback_out(Feedback={"Text": "Different synthetic text", "Html": feedback_payload(TEXT)["Feedback"]["Html"]}))]},
    "entity-text-matches-html-differs": {"get_submissions": [
        lambda: response(200, [ENTITY_NO_FEEDBACK]),
        lambda: response(200, [entity_with(draft_feedback_out(Feedback={"Text": TEXT, "Html": "<p>Other</p>"}), Status=2)])]},
    "text-empty-html-matches": {"get_feedback": [
        lambda: response(200, draft_feedback_out(Feedback={"Text": "", "Html": feedback_payload(TEXT)["Feedback"]["Html"]}))]},
    "text-matches-html-empty": {"get_feedback": [
        lambda: response(200, draft_feedback_out(Feedback={"Text": TEXT, "Html": ""}))]},
    "entity-text-empty-html-matches": {"get_submissions": [
        lambda: response(200, [ENTITY_NO_FEEDBACK]),
        lambda: response(200, [entity_with(draft_feedback_out(Feedback={"Text": "", "Html": feedback_payload(TEXT)["Feedback"]["Html"]}), Status=2)])]},
    "html-extra-markup": {"get_feedback": [lambda: response(200, draft_feedback_out(Feedback={
        "Text": TEXT, "Html": "<p>Synthetic draft note: revisit the second proof.<br>Line two.</p><img src=\"x\">"}))]},
    "html-only-mismatch": {"get_feedback": [lambda: response(200, draft_feedback_out(Feedback={"Html": "<p>other</p>"}))]},
    "not-object": {"get_feedback": [lambda: response(200, ["x"])]},
    "non-json": {"get_feedback": [lambda: response(200, raw=b"<html>NEVER_PRINT</html>")]},
    "feedback-read-500": {"get_feedback": [lambda: response(500, {})]},
    "feedback-read-403": {"get_feedback": [lambda: response(403, {})]},
    "status-published": {"get_submissions": [
        lambda: response(200, [ENTITY_NO_FEEDBACK]), lambda: response(200, [entity_with(draft_feedback_out(), Status=3)])]},
    "entity-feedback-graded": {"get_submissions": [
        lambda: response(200, [ENTITY_NO_FEEDBACK]),
        lambda: response(200, [entity_with(draft_feedback_out(IsGraded=True), Status=2)])]},
    "status-still-submitted": {"get_submissions": [
        lambda: response(200, [ENTITY_NO_FEEDBACK]), lambda: response(200, [entity_with(draft_feedback_out(), Status=1)])]},
    "entity-feedback-missing": {"get_submissions": [
        lambda: response(200, [ENTITY_NO_FEEDBACK]), lambda: response(200, [entity_with(None, Status=2)])]},
    "entity-text-mismatch": {"get_submissions": [
        lambda: response(200, [ENTITY_NO_FEEDBACK]),
        lambda: response(200, [entity_with(draft_feedback_out("Different synthetic text"), Status=2)])]},
    "entity-feedback-key-absent": {"get_submissions": [
        lambda: response(200, [ENTITY_NO_FEEDBACK]),
        lambda: response(200, [{**{k: v for k, v in ENTITY_NO_FEEDBACK.items() if k != "Feedback"}, "Status": 2}])]},
    "entity-gone": {"get_submissions": [lambda: response(200, [ENTITY_NO_FEEDBACK]), lambda: response(200, [])]},
    "submissions-read-500": {"get_submissions": [lambda: response(200, [ENTITY_NO_FEEDBACK]), lambda: response(500, {})]},
}


@pytest.mark.parametrize("changes", list(BAD_READBACKS.values()), ids=list(BAD_READBACKS))
def test_unverified_readback_is_unknown_never_success(feedback_file, changes):
    fake = FakeLMS(happy_routes(**changes))
    with patch("lighthouse_cli.api.time.sleep"):
        result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["outcome"] == "unknown"
    assert "saved-draft" not in result.stdout
    assert len(fake.methods("POST")) == 1
    assert_no_leak(result)


def test_fifo_path_is_refused_without_hanging(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(FeedbackInputError, match="Could not read"):
        read_feedback_file(str(fifo))


def test_lone_carriage_returns_are_normalized(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"one\rtwo\r\nthree")
    assert read_feedback_file(str(path)) == "one\ntwo\nthree"


def test_readback_tolerates_crlf_newlines_from_the_server(feedback_file):
    crlf = TEXT.replace("\n", "\r\n")
    fake = FakeLMS(happy_routes(get_feedback=[lambda: response(200, draft_feedback_out(crlf))]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 0, result.output


def test_readback_accepts_the_html_part_alone_when_it_matches_what_was_sent(feedback_file):
    sent = feedback_payload(TEXT)["Feedback"]["Html"]
    fake = FakeLMS(happy_routes(get_feedback=[lambda: response(200, draft_feedback_out(Feedback={"Html": sent}))]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("markup", [
    "<p>Synthetic draft note: revisit the second proof.<br>Line two.</p>",
    "<P>Synthetic draft note:&nbsp;revisit the second proof.<BR/>Line two.</P>",
    "\n  <p>\nSynthetic draft note: revisit the second proof.<br />\nLine two.\n</p>\n",
], ids=["br", "case-nbsp", "pretty-printed"])
def test_readback_accepts_html_reformatted_with_the_same_text(feedback_file, markup):
    fake = FakeLMS(happy_routes(get_feedback=[lambda: response(200, draft_feedback_out(Feedback={"Text": TEXT, "Html": markup}))]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("rich,text", [
    ({"Html": "<p>hello</p>"}, "  hello  "),
    ({"Html": "<p>hello</p><img src=\"x\">"}, "hello"),
    ({"Html": "<p><b>hello</b></p>"}, "hello"),
    ({"Html": "<div>hello</div>"}, "hello"),
    ({"Html": "<p>hello there</p>"}, "hello  there"),
    ({"Text": "", "Html": "<p>hello</p>"}, "hello"),
    ({"Text": "hello", "Html": ""}, "hello"),
    ({"Text": None, "Html": None}, "hello"),
], ids=["outer-spaces", "extra-image", "bold", "div", "inner-spaces", "empty-text", "empty-html", "both-null"])
def test_saved_comment_check_rejects_any_difference(rich, text):
    with pytest.raises(_UnexpectedError):
        _check_saved_comment(draft_feedback_out(Feedback=rich), text)


@pytest.mark.parametrize("rich,text", [
    ({"Text": None, "Html": "<p>a &amp; b<br />c</p>"}, "a & b\nc"),
    ({"Text": "a\u00a0b", "Html": "<p>a&nbsp;b</p>"}, "a\u00a0b"),
], ids=["html-only-escaped", "nbsp"])
def test_saved_comment_check_accepts_equivalent_parts(rich, text):
    _check_saved_comment(draft_feedback_out(Feedback=rich), text)


_REJECTED_HTML = {
    "p-hidden": ("<p hidden>hello</p>", "hello"),
    "p-style": ('<p style="display:none">hello</p>', "hello"),
    "p-class": ('<p class="sr-only">hello</p>', "hello"),
    "p-id": ("<p id=x>hello</p>", "hello"),
    "br-attribute": ('<p>a<br class="x">b</p>', "a\nb"),
    "span": ("<p><span>hello</span></p>", "hello"),
    "nested-p-in-div": ("<div><p>hello</p></div>", "hello"),
    "extra-image": ('<p>hello</p><img src="x">', "hello"),
    "script-inside": ("<p>hello<script>x()</script></p>", "hello"),
    "script-after": ("<p>hello</p><script>x()</script>", "hello"),
    "style-inside": ("<p>hello<style>p{}</style></p>", "hello"),
    "comment-inside": ("<p>hello<!-- note --></p>", "hello"),
    "comment-before": ("<!-- note --><p>hello</p>", "hello"),
    "unfinished-comment": ("<p>hello</p><!--", "hello"),
    "bogus-comment": ("<p>hello</ x></p>", "hello"),
    "doctype": ("<!DOCTYPE html><p>hello</p>", "hello"),
    "cdata": ("<p>hello<![CDATA[x]]></p>", "hello"),
    "processing-instruction": ("<p>hello<?x?></p>", "hello"),
    "second-paragraph": ("<p>hello</p><p>there</p>", "hello\nthere"),
    "empty-second-paragraph": ("<p>hello</p><p></p>", "hello"),
    "unclosed-second-paragraph": ("<p>hello</p><p>", "hello"),
    "self-closing-p": ("<p/>hello</p>", "hello"),
    "unclosed": ("<p>hello", "hello"),
    "no-paragraph": ("hello", "hello"),
    "stray-close-first": ("</p><p>hello</p>", "hello"),
    "double-close": ("<p>hello</p></p>", "hello"),
    "end-br": ("<p>a</br>b</p>", "a\nb"),
    "unfinished-tag-after": ("<p>hello</p><b", "hello"),
    "text-after": ("<p>hello</p>junk", "hello"),
    "entity-after": ("<p>hello</p>&nbsp;", "hello"),
    "raw-newline-inside": ("<p>x\ny</p>", "x\ny"),
    "two-newlines-after-br": ("<p>x<br>\n\ny</p>", "x\ny"),
    "trailing-break-dropped": ("<p>hello</p>", "hello\n"),
    "extra-outer-breaks": ("<p><br>hello<br><br></p>", "hello"),
    "extra-leading-break": ("<p><br>hello</p>", "hello"),
    "invalid-charref": ("<p>he&#1;llo</p>", "hello"),
    "c1-charref": ("<p>he&#x80;llo</p>", "he\x80llo"),
    "zero-charref": ("<p>he&#0;llo</p>", "hello"),
    "huge-charref": ("<p>he&#x110000;llo</p>", "hello"),
    "surrogate-charref": ("<p>he&#xD800;llo</p>", "hello"),
    "newline-charref": ("<p>a&#10;b</p>", "a\nb"),
    "unterminated-charref": ("<p>&#38 x</p>", "& x"),
    "unknown-entity": ("<p>a&bogus;b</p>", "ab"),
    "unterminated-entity": ("<p>a&amp b</p>", "a& b"),
    "end-tag-attribute": ("<p>hello</p hidden=x>", "hello"),
    "end-tag-junk": ("<p>hello</p foo>", "hello"),
    "end-tag-slash": ("<p>hello</p/>", "hello"),
    "empty-end-tag": ("<p>he</>llo</p>", "hello"),
    "empty-end-tag-after": ("<p>hello</p></>", "hello"),
    "slash-attribute-p": ("<p/hidden>hello</p>", "hello"),
    "slash-junk-br": ("<p>a<br/x>b</p>", "a\nb"),
    "marked-section": ("<p>x<![ x</p>", "x"),
    "vertical-tab-in-tag": ("<p\x0b>hello</p>", "hello"),
}


@pytest.mark.parametrize("markup,text", list(_REJECTED_HTML.values()), ids=list(_REJECTED_HTML))
def test_saved_comment_check_rejects_html_with_a_different_structure(markup, text):
    with pytest.raises(_UnexpectedError):
        _check_saved_comment(draft_feedback_out(Feedback={"Text": text, "Html": markup}), text)


_ACCEPTED_HTML = {
    "exact": ("<p>a &amp; b<br />c</p>", "a & b\nc"),
    "br-spellings": ("<p>a<br>b<br/>c<br />d<BR>e<br/ >f</p>", "a\nb\nc\nd\ne\nf"),
    "tag-case": ("<P>hello</P>", "hello"),
    "space-inside-tags": ("<p >hello</p >", "hello"),
    "outer-whitespace": ("\n\t <p>hello</p> \r\n", "hello"),
    "line-breaks-next-to-tags": ("<p>\na<br />\nb\n<br>c\n</p>", "a\nb\nc"),
    "crlf-next-to-tags": ("<p>\r\na<br />\r\nb\r\n</p>", "a\nb"),
    "entity-spellings": ("<p>&#38;&#x26;&amp; &quot;\"&#34; &#39;&#x27;' &gt;&#62;> &lt;3</p>",
                         "&&& \"\"\" ''' >>> <3"),
    "raw-lt-text": ("<p>x < y <3</p>", "x < y <3"),
    "nbsp": ("<p>a&nbsp;b&#160;c</p>", "a b c"),
    "bare-ampersand": ("<p>a & b &; x&</p>", "a & b &; x&"),
    "outer-breaks-kept": ("<p><br>hello<br></p>", "\nhello\n"),
}


@pytest.mark.parametrize("markup,text", list(_ACCEPTED_HTML.values()), ids=list(_ACCEPTED_HTML))
def test_saved_comment_check_accepts_known_reformatting(markup, text):
    _check_saved_comment(draft_feedback_out(Feedback={"Text": None, "Html": markup}), text)


@pytest.mark.parametrize("text", [
    "hello", "\nleading break", "trailing break\n", "\n\nboth\n\n", "a\n\nb", "<b>\"x\" & 'y'</b>",
    "tab\there", "  outer spaces  ", "nbsp here", "</p><p hidden>x", "<!-- c -->", "&amp; literal",
    "x<y and y>z", "unicode é中\U0001f600",
])
def test_html_matcher_accepts_exactly_what_was_sent(text):
    assert _html_text(_html(text)) == _comparable(text)


def test_attribute_paragraph_with_matching_text_reports_unknown(feedback_file):
    hidden = feedback_payload(TEXT)["Feedback"]["Html"].replace("<p>", "<p hidden>", 1)
    fake = FakeLMS(happy_routes(get_feedback=[
        lambda: response(200, draft_feedback_out(Feedback={"Text": TEXT, "Html": hidden}))]))
    result = invoke(fake, "--file", str(feedback_file), "--yes", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["outcome"] == "unknown"
    assert "saved-draft" not in result.stdout


def test_csrf_bootstrap_failure_means_nothing_was_sent(feedback_file):
    fake = FakeLMS(happy_routes())

    def factory(*_a: Any, read_only_auth: bool = False, **_k: Any) -> LighthouseClient:
        client = make_client(fake, read_only_auth=read_only_auth)
        client._csrf_token = None
        client.get_raw = lambda *a, **k: (b"<html>no token</html>", {})  # type: ignore[method-assign]
        return client

    with patch("lighthouse_cli.api.LighthouseClient", side_effect=factory):
        result = CliRunner().invoke(cli, ["instructor", "assignment-feedback", str(COURSE), str(FOLDER), str(USER),
                                          "--file", str(feedback_file), "--yes", "--json"])
    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document.get("outcome") != "unknown"
    assert fake.methods("POST") == []


def test_transport_method_is_dedicated_and_write_is_unchanged():
    client = make_client(FakeLMS({}))
    api = AssessmentAPI(client, COURSE)
    with pytest.raises(ValueError):
        api.write("DELETE", "assignment", {})
    with pytest.raises(KeyError):
        api.path("feedback")  # type: ignore[arg-type]

