"""Focused tests for LighthouseClient's low-level transport helpers."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import types
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

import lighthouse_cli.api as api
from lighthouse_cli import connection, session_renewal
from lighthouse_cli.api import (
    BASE_URL,
    MAX_HTML_TOPIC_RESPONSE_BYTES,
    ContentResponseShapeError,
    CourseNotFoundError,
    LighthouseClient,
    NetworkError,
    SessionExpiredError,
    SubmissionOutcomeUnknownError,
    _extract_filename,
    resolve_course_id,
)
from lighthouse_cli.assessment_api import AssessmentAPI, AssignmentClosedError
from lighthouse_cli.config import cookie_file_is_legacy, load_cookies, save_cookies
from lighthouse_cli.connection import LIGHTHOUSE, Connection
from lighthouse_cli.credential_store import CredentialStore
from lighthouse_cli.ms_errors import RELOGIN_CATEGORIES
from lighthouse_cli.quiz_learner_session import LearnerWorkflow, LearnerWorkflowError
from lighthouse_cli.quiz_preview_session import PreviewWorkflow, PreviewWorkflowError
from lighthouse_cli.session_renewal import RenewalError, RenewalResult, RenewalUnavailableError

SYNTHETIC_COOKIES = {
    "d2lSameSiteCanaryA": "a",
    "d2lSameSiteCanaryB": "b",
    "d2lSecureSessionVal": "secure",
    "d2lSessionVal": "session",
}


def _authenticate(client: LighthouseClient) -> None:
    client._loaded = True
    client._cookies = dict(SYNTHETIC_COOKIES)


def test_legacy_state_creating_get_is_not_retried_or_refreshed() -> None:
    client = LighthouseClient()
    client._loaded = True
    client._cookies = dict.fromkeys(api.COOKIE_NAMES, "synthetic")
    client._session.request = MagicMock(side_effect=requests.ConnectionError("token=SENTINEL"))
    with patch.object(api, "refresh_auth_from_browser") as refresh:
        with pytest.raises(NetworkError):
            client._request("GET", BASE_URL + "/d2l/legacy-start", _replay_safe=False)
    assert client._session.request.call_count == 1
    assert "_replay_safe" not in client._session.request.call_args.kwargs
    refresh.assert_not_called()


class FakeResponse:
    """Small requests.Response substitute for transport tests."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: object | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}
        self.closed = False

    def json(self) -> object:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """Session double that records each request and returns queued responses."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return next(self.responses)


def _client_with_session(
    responses: list[FakeResponse],
    authenticated: bool = False,
) -> tuple[LighthouseClient, FakeSession]:
    client = LighthouseClient()
    # Transport tests start after CSRF bootstrap, tested separately.
    client._csrf_token = "synthetic-csrf"
    session = FakeSession(responses)
    client._session = session
    if authenticated:
        _authenticate(client)
    return client, session


class StreamingResponse(FakeResponse):
    def __init__(self, chunks: list[bytes], headers: dict[str, str] | None = None) -> None:
        super().__init__(headers=headers)
        self.chunks = chunks
        self.iterated = False

    def iter_content(self, chunk_size: int) -> object:
        assert chunk_size > 0
        self.iterated = True
        return iter(self.chunks)


def _raw_client(response: FakeResponse | None = None) -> LighthouseClient:
    client = LighthouseClient()
    client.get = MagicMock(return_value=response)
    return client


def test_get_raw_streams_and_rejects_actual_bytes_above_limit() -> None:
    response = StreamingResponse([b"abcd", b"efgh"])
    client = _raw_client(response)

    with pytest.raises(NetworkError, match="configured size limit"):
        client.get_raw("/file", max_bytes=7)

    client.get.assert_called_once_with("/file", stream=True)
    assert response.closed is True


def test_get_raw_rejects_oversized_content_length_before_reading() -> None:
    response = StreamingResponse(
        [b"must not be read"],
        headers={"Content-Length": "9"},
    )
    client = _raw_client(response)

    with pytest.raises(NetworkError, match="configured size limit"):
        client.get_raw("/file", max_bytes=8)

    assert response.closed is True
    assert response.iterated is False


def test_get_raw_returns_bounded_streamed_content() -> None:
    response = StreamingResponse([b"ab", b"", b"cd"])
    client = _raw_client(response)

    content, headers = client.get_raw("/file", max_bytes=4)

    assert content == b"abcd"
    assert headers == {}
    assert response.closed is True


@pytest.mark.parametrize("raw", ["0", "-1", "not-a-number", "1073741825"])
def test_get_raw_rejects_invalid_environment_limits(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    client = _raw_client()
    monkeypatch.setenv("LIGHTHOUSE_MAX_DOWNLOAD_BYTES", raw)

    with pytest.raises(NetworkError, match="size limit is invalid"):
        client.get_raw("/file")

    client.get.assert_not_called()


def test_retry_closes_streamed_rate_limit_response() -> None:
    limited = FakeResponse(429)
    success = FakeResponse(200)
    client, _session = _client_with_session([limited, success])

    with patch.object(api.time, "sleep"):
        assert client._do_request("GET", "https://example.test", False, 30) is success

    assert limited.closed is True
    assert success.closed is False


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(401),
        FakeResponse(302, headers={"Location": "/login"}),
        FakeResponse(500),
    ],
)
def test_terminal_error_responses_are_closed(response: FakeResponse) -> None:
    client, _session = _client_with_session([response])

    with pytest.raises((SessionExpiredError, requests.HTTPError)):
        client._do_request("GET", "https://example.test", False, 30)

    assert response.closed is True


def test_non_login_redirect_raises_fixed_network_error_and_closes() -> None:
    response = FakeResponse(302, headers={"Location": "/d2l/other"})
    client, _session = _client_with_session([response])

    with pytest.raises(NetworkError, match="unexpected redirect"):
        client._do_request("GET", "https://example.test", False, 30)

    assert response.closed is True


def test_skip_raise_preserves_non_login_redirect_for_submission_handler() -> None:
    response = FakeResponse(302, headers={"Location": "/d2l/other"})
    client, _session = _client_with_session([response])

    assert client._do_request("POST", "https://example.test", True, 30) is response
    assert response.closed is False


def _paged_client(*pages: object) -> LighthouseClient:
    client = LighthouseClient()
    client.get_json = MagicMock(side_effect=list(pages))
    return client


def test_get_enrollments_reuses_paginated_items_endpoint() -> None:
    client = _paged_client(
        {"Items": [{"id": 1}], "Next": "/enrollments?page=2"},
        {"Items": [{"id": 2}], "Next": None},
    )

    assert client.get_enrollments() == [{"id": 1}, {"id": 2}]
    assert client.get_json.call_args_list == [
        ((f"{BASE_URL}/d2l/api/lp/1.47/enrollments/myenrollments/",),),
        (("/enrollments?page=2",),),
    ]


ENROLLMENTS_PAGE = f"{BASE_URL}/d2l/api/lp/1.47/enrollments/myenrollments/"


@pytest.mark.parametrize(
    ("first_url", "next_link", "second_page", "second_url"),
    [
        pytest.param(
            f"{ENROLLMENTS_PAGE}?page=1",
            "?page=2",
            {"Items": [{"id": 2}], "Next": None},
            f"{ENROLLMENTS_PAGE}?page=2",
            id="query-only-next-uses-current-resource-path",
        ),
        pytest.param(
            "/enrollments",
            f"{BASE_URL}/d2l/api/lp/1.47/enrollments?page=2",
            {"Items": [{"id": 2}], "Next": None},
            f"{BASE_URL}/d2l/api/lp/1.47/enrollments?page=2",
            id="https-same-origin",
        ),
        pytest.param(
            "/enrollments",
            "enrollments?page=2",
            {"Items": [{"id": 2}], "Next": None},
            "enrollments?page=2",
            id="bare-relative-scoped-beneath-api-root",
        ),
        pytest.param(
            "/enrollments",
            "/enrollments?page=2",
            [{"id": 2}],
            "/enrollments?page=2",
            id="plain-list-tail-keeps-prior-wrapped-items",
        ),
    ],
)
def test_paginated_next_follows_trusted_links(
    first_url: str,
    next_link: str,
    second_page: object,
    second_url: str,
) -> None:
    client = _paged_client({"Items": [{"id": 1}], "Next": next_link}, second_page)

    assert client._paginate_list(first_url, "Items") == [{"id": 1}, {"id": 2}]
    assert client.get_json.call_args_list == [((first_url,),), ((second_url,),)]


def test_paginated_next_cycle_raises_clean_network_error() -> None:
    client = _paged_client(
        {"Items": [{"id": 1}], "Next": "/enrollments?page=2"},
        {"Items": [{"id": 2}], "Next": "/enrollments?page=2"},
    )

    with pytest.raises(NetworkError, match="Pagination cycle"):
        client._paginate_list("/enrollments", "Items")

    assert client.get_json.call_count == 2


def test_paginated_request_failures_are_url_free() -> None:
    url = "https://example.test/page?token=PAGINATION_URL_SENTINEL"
    client = _paged_client(requests.ConnectionError(f"request failed for {url}"))

    with pytest.raises(NetworkError) as exc_info:
        client._paginate_list("/enrollments", "Items")

    assert "PAGINATION_URL_SENTINEL" not in str(exc_info.value)
    assert "https://example.test" not in str(exc_info.value)


def test_get_enrolled_courses_joins_paginated_course_offerings() -> None:
    """The normalized catalog spans pages and excludes non-course enrollments."""
    client = _paged_client(
        {
            "Items": [
                {
                    "OrgUnit": {
                        "Id": 22,
                        "Name": "Course B",
                        "Code": "B",
                        "Type": {"Code": "Course Offering"},
                    },
                    "Access": {"IsActive": False},
                },
                {
                    "OrgUnit": {
                        "Id": 77,
                        "Name": "Aggregate roster",
                        "Type": {"Code": "Section"},
                    }
                },
            ],
            "Next": "/enrollments?page=2",
        },
        {
            "Items": [
                {
                    "OrgUnit": {
                        "Id": "11",
                        "Name": "Course A",
                        "Code": "A",
                        "Type": {"Code": "Course Offering"},
                    },
                    "Access": {"IsActive": True},
                }
            ],
            "Next": None,
        },
    )

    assert client.get_enrolled_courses() == [
        {"OrgUnitId": 11, "Name": "Course A", "Code": "A", "IsActive": True},
        {"OrgUnitId": 22, "Name": "Course B", "Code": "B", "IsActive": False},
    ]
    assert client.get_json.call_count == 2


def test_get_enrolled_courses_deduplicates_and_skips_invalid_ids() -> None:
    """Only positive IDs survive normalization, with stable first-record wins."""
    client = LighthouseClient()
    enrollments = [
        {"OrgUnit": {"Id": 0, "Name": "Zero"}},
        {"OrgUnit": {"Id": -7, "Name": "Negative"}},
        {"OrgUnit": {"Id": "not-an-id", "Name": "Malformed"}},
        {"OrgUnit": {"Id": "20", "Name": "First", "Code": "F"}},
        {"OrgUnit": {"Id": 20, "Name": "Duplicate", "Code": "D"}},
        {"OrgUnit": {"Id": 3, "Name": "Third", "Code": "T"}},
        None,
    ]
    client.get_course_enrollments = MagicMock(return_value=enrollments)

    assert client.get_enrolled_courses() == [
        {"OrgUnitId": 3, "Name": "Third", "Code": "T", "IsActive": True},
        {"OrgUnitId": 20, "Name": "First", "Code": "F", "IsActive": True},
    ]
    assert enrollments[3]["OrgUnit"]["Name"] == "First"


@pytest.mark.parametrize(
    ("retry_after", "expected_sleeps"),
    [
        pytest.param(["not-a-number"], [2], id="invalid-uses-exponential-fallback-not-a-number"),
        pytest.param(["nan"], [2], id="invalid-uses-exponential-fallback-nan"),
        pytest.param(["inf"], [2], id="invalid-uses-exponential-fallback-inf"),
        pytest.param(["-1"], [2], id="invalid-uses-exponential-fallback-negative"),
        pytest.param(["4", "4"], [4.0, 4.0], id="valid-not-multiplied-by-attempt-exponent"),
        pytest.param(
            ["999999"], [LighthouseClient._MAX_RETRY_AFTER], id="server-delay-is-capped",
        ),
    ],
)
def test_retry_after_delays(retry_after: list[str], expected_sleeps: list[float]) -> None:
    limited = [FakeResponse(429, headers={"Retry-After": value}) for value in retry_after]
    client, session = _client_with_session([*limited, FakeResponse(200)])

    with patch.object(api.time, "sleep") as sleep:
        client._do_request("GET", "https://example.test/resource", False, 30)

    assert [call.args for call in sleep.call_args_list] == [(delay,) for delay in expected_sleeps]
    assert all(call.kwargs == {} for call in sleep.call_args_list)
    assert len(session.calls) == len(retry_after) + 1


def test_post_rate_limit_is_not_retried() -> None:
    client, session = _client_with_session([FakeResponse(429)])
    payload = b"payload"

    with patch.object(api.time, "sleep") as sleep:
        response = client._do_request(
            "POST",
            "https://example.test/resource",
            True,
            30,
            data=payload,
        )

    assert response.status_code == 429
    assert [call[0] for call in session.calls] == ["POST"]
    assert session.calls[0][2]["data"] == payload
    sleep.assert_not_called()


def test_post_unauthorized_is_not_auto_refreshed_or_replayed() -> None:
    client, session = _client_with_session([FakeResponse(401)], authenticated=True)

    with patch.object(api, "refresh_auth_from_browser") as refresh, \
            patch.object(api, "save_cookies") as save:
        with pytest.raises(api.SessionExpiredError):
            client._request("POST", "https://example.test/resource", data=b"payload")

    assert len(session.calls) == 1
    refresh.assert_not_called()
    save.assert_not_called()


def test_post_rate_limit_is_not_auto_refreshed_or_replayed() -> None:
    client, session = _client_with_session([FakeResponse(429)], authenticated=True)

    with patch.object(api, "refresh_auth_from_browser") as refresh, \
            patch.object(api, "save_cookies") as save, \
            patch.object(api.time, "sleep") as sleep:
        response = client._request(
            "POST", "https://example.test/resource", _skip_raise=True, data=b"payload"
        )

    assert response.status_code == 429
    assert len(session.calls) == 1
    refresh.assert_not_called()
    save.assert_not_called()
    sleep.assert_not_called()


def _authenticated_client_with_session() -> tuple[LighthouseClient, MagicMock]:
    """Return an authenticated client whose session records unexpected calls."""
    client = LighthouseClient()
    _authenticate(client)
    session = MagicMock()
    client._session = session
    return client, session


# Stands in for the invalid ID in each argument tuple below.
INVALID = object()

ID_VALIDATED_CALLS = [
    pytest.param("get_dropbox_folder_detail", (INVALID, 1), "org_unit_id", id="dropbox-detail-org"),
    pytest.param("get_dropbox_folder_detail", (1, INVALID), "folder_id", id="dropbox-detail-folder"),
    pytest.param("download_attachment", (INVALID, 1, 1), "org_unit_id", id="attachment-org"),
    pytest.param("download_attachment", (1, INVALID, 1), "folder_id", id="attachment-folder"),
    pytest.param("download_attachment", (1, 1, INVALID), "file_id", id="attachment-file"),
    pytest.param(
        "submit_file", (INVALID, 1, b"payload", "test.pdf"), "org_unit_id", id="submit-org",
    ),
    pytest.param(
        "submit_file", (1, INVALID, b"payload", "test.pdf"), "folder_id", id="submit-folder",
    ),
    pytest.param("get_content_toc", (INVALID,), "org_unit_id", id="toc-org"),
    pytest.param("get_announcements", (INVALID,), "org_unit_id", id="announcements-org"),
    pytest.param("get_grade_schema", (INVALID,), "org_unit_id", id="grade-schema-org"),
    pytest.param("get_my_grades", (INVALID,), "org_unit_id", id="my-grades-org"),
    pytest.param("get_quizzes", (INVALID,), "org_unit_id", id="quizzes-org"),
    pytest.param("get_calendar", (INVALID,), "org_unit_id", id="calendar-org"),
    pytest.param("get_dropbox_folders", (INVALID,), "org_unit_id", id="dropbox-folders-org"),
    pytest.param("get_quiz_detail", (INVALID, 1), "org_unit_id", id="quiz-detail-org"),
    pytest.param("get_quiz_detail", (1, INVALID), "quiz_id", id="quiz-detail-quiz"),
    pytest.param("download_topic_file", (INVALID, 1), "org_unit_id", id="topic-file-org"),
    pytest.param("download_topic_file", (1, INVALID), "topic_id", id="topic-file-topic"),
    pytest.param("get_topic_html", (INVALID, 1), "org_unit_id", id="topic-html-org"),
    pytest.param("get_topic_html", (1, INVALID), "topic_id", id="topic-html-topic"),
]


@pytest.mark.parametrize("invalid_id", ["../../evil", "../evil", True, 1.5, 0, -1])
@pytest.mark.parametrize(("method", "args", "field_name"), ID_VALIDATED_CALLS)
def test_endpoints_reject_invalid_ids_before_request(
    method: str,
    args: tuple[object, ...],
    field_name: str,
    invalid_id: object,
) -> None:
    client, session = _authenticated_client_with_session()
    call_args = [invalid_id if arg is INVALID else arg for arg in args]

    with pytest.raises(ValueError, match=f"{field_name} must be a positive integer"):
        getattr(client, method)(*call_args)

    session.request.assert_not_called()


def test_blank_course_selector_never_matches_every_enrollment() -> None:
    client = MagicMock()
    client.get_enrolled_courses.return_value = [
        {"OrgUnitId": 123, "Name": "Course"},
    ]

    with pytest.raises(CourseNotFoundError, match="cannot be empty"):
        resolve_course_id(client, "   ")


def _topic_client(payload: object) -> LighthouseClient:
    client = LighthouseClient()
    client.get_raw = MagicMock(
        return_value=(json.dumps(payload).encode("utf-8"), {})
    )
    return client


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "Title": "Nested RichText",
                "Body": {"Text": {"Html": "<p>nested</p>"}},
            },
            b"<p>nested</p>",
        ),
        (
            {"Title": "Direct Text", "Body": {"Text": "<p>direct</p>"}},
            b"<p>direct</p>",
        ),
        (
            {"Title": "Top Level", "Body": {}, "Html": "<p>top-level</p>"},
            b"<p>top-level</p>",
        ),
        (
            {"Title": "Body Fallback", "Body": {"foo": "bad"}, "Html": "<p>fallback</p>"},
            b"<p>fallback</p>",
        ),
    ],
)
def test_get_topic_html_extracts_bounded_rich_text_as_bytes(
    payload: dict[str, object], expected: bytes
) -> None:
    client = _topic_client(payload)

    content, filename = client.get_topic_html(1, 1)

    assert type(content) is bytes
    assert content == expected
    assert filename.endswith(".html")
    client.get_raw.assert_called_once_with(
        "/1/content/topics/1",
        max_bytes=MAX_HTML_TOPIC_RESPONSE_BYTES,
    )


def _deep_rich_text() -> dict[str, object]:
    value: object = "<p>deep</p>"
    for _ in range(api._MAX_RICH_TEXT_DEPTH + 1):
        value = {"Text": value}
    return {"Body": value}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"Body": []},
        {"Body": {"Text": 123}},
        {"Body": {"Text": ["<p>bad</p>"]}},
        {"Body": {}, "Html": {"unexpected": "object"}},
        {"Body": {"foo": "bad"}, "Html": {"unexpected": "object"}},
        pytest.param(_deep_rich_text(), id="deep-rich-text-without-recursion"),
    ],
)
def test_get_topic_html_rejects_malformed_shapes_with_fixed_error(
    payload: object,
) -> None:
    client = _topic_client(payload)

    with pytest.raises(ContentResponseShapeError) as exc_info:
        client.get_topic_html(1, 1)

    assert str(exc_info.value) == ContentResponseShapeError._MESSAGE


def test_get_topic_html_rejects_cyclic_rich_text_without_recursion() -> None:
    value: dict[str, object] = {}
    value["Text"] = value
    client = LighthouseClient()
    client.get_raw = MagicMock(return_value=(b"{}", {}))

    with patch("lighthouse_cli.api.json.loads", return_value={"Body": value}):
        with pytest.raises(ContentResponseShapeError) as exc_info:
            client.get_topic_html(1, 1)

    assert str(exc_info.value) == ContentResponseShapeError._MESSAGE


@pytest.mark.parametrize(
    "invalid_filename",
    [
        "",
        "   ",
        ".",
        "..",
        "../evil.txt",
        "nested/file.txt",
        "nested\\file.txt",
        "report\r\nX-Injected: yes.pdf",
        "report\x00.pdf",
        "report\x1f.pdf",
        "report\x7f.pdf",
        "a" * 256,
    ],
)
def test_submit_file_rejects_unsafe_filename_before_request(
    invalid_filename: str,
) -> None:
    client, session = _authenticated_client_with_session()

    with pytest.raises(ValueError):
        client.submit_file(1, 1, b"payload", invalid_filename)

    session.request.assert_not_called()


@pytest.mark.parametrize(
    "invalid_content_type",
    [
        "application/pdf\r\nX-Injected: yes",
        "application/pdf\n",
        "application/pdf\x00",
        "application/pdf\x1f",
        "application/pdf\x7f",
        "application",
        "application/",
        "/pdf",
        "application/pdf/extra",
        "application/pdf; charset=utf-8",
        "application/pdf charset=utf-8",
        "application/pdf\t",
        "application/пдф",
        "a" * 256 + "/pdf",
    ],
)
def test_submit_file_rejects_invalid_content_type_before_request(
    invalid_content_type: str,
) -> None:
    client, session = _authenticated_client_with_session()

    with pytest.raises(ValueError, match="valid ASCII MIME type"):
        client.submit_file(1, 1, b"payload", "test.pdf", content_type=invalid_content_type)

    session.request.assert_not_called()


def test_submit_file_preserves_valid_explicit_content_type() -> None:
    client, session = _client_with_session([FakeResponse(200, {})], authenticated=True)

    client.submit_file(1, 1, b"payload", "test.pdf", content_type="application/pdf")

    assert len(session.calls) == 1
    assert b"Content-Type: application/pdf\r\n" in session.calls[0][2]["data"]


@pytest.mark.parametrize("response_body", [None, [], "unexpected response"])
def test_submit_file_rejects_non_object_success_response_without_retry(
    response_body: object,
) -> None:
    client, session = _client_with_session([FakeResponse(200, response_body)], authenticated=True)

    with pytest.raises(SubmissionOutcomeUnknownError) as exc_info:
        client.submit_file(1, 1, b"payload", "result.pdf")

    assert str(exc_info.value) == SubmissionOutcomeUnknownError._MESSAGE
    assert len(session.calls) == 1


def test_submit_file_rejects_invalid_json_success_response_without_echoing_body() -> None:
    class InvalidJsonResponse(FakeResponse):
        def json(self) -> object:
            raise ValueError("response body contains BODY_SENTINEL")

    client, session = _client_with_session([InvalidJsonResponse(200)], authenticated=True)

    with pytest.raises(SubmissionOutcomeUnknownError) as exc_info:
        client.submit_file(1, 1, b"payload", "result.pdf")

    assert "BODY_SENTINEL" not in str(exc_info.value)
    assert len(session.calls) == 1


def test_exhausted_get_network_errors_are_url_free_and_bounded() -> None:
    client, session = _authenticated_client_with_session()
    url = "https://example.test/resource?session=NETWORK_URL_SENTINEL"
    session.request.side_effect = requests.ConnectionError(f"failed for {url}")

    with patch.object(api.time, "sleep") as sleep:
        with pytest.raises(NetworkError) as exc_info:
            client._request("GET", url)

    assert "NETWORK_URL_SENTINEL" not in str(exc_info.value)
    assert "https://example.test" not in str(exc_info.value)
    assert session.request.call_count == client._MAX_RETRIES + 1
    assert [call.args[0] for call in sleep.call_args_list] == [2, 4, 8]


def test_exhausted_post_network_error_is_url_free_and_not_replayed() -> None:
    client, session = _authenticated_client_with_session()
    url = "https://example.test/resource?session=POST_URL_SENTINEL"
    session.request.side_effect = requests.ConnectionError(f"failed for {url}")

    with pytest.raises(NetworkError) as exc_info:
        client._request("POST", url, data=b"payload")

    assert "POST_URL_SENTINEL" not in str(exc_info.value)
    assert "https://example.test" not in str(exc_info.value)
    session.request.assert_called_once()


@pytest.mark.parametrize(
    "path",
    [
        "http://lighthouse.manipal.edu/d2l/api/le/1.93/resource",
        "https://attacker.example/d2l/api/le/1.93/resource",
        "https://user:pass@lighthouse.manipal.edu/d2l/api/le/1.93/resource",
        "https://lighthouse.manipal.edu:8443/d2l/api/le/1.93/resource",
        "https://lighthouse.manipal.edu/d2l/api/le/1.93/../secret",
        "//attacker.example/d2l/api/le/1.93/resource",
        "https://lighthouse.manipal.edu/d2l/api/le/1.93/resource#fragment",
        "/../evil",
        "/../../evil",
        "/d2l/api/le/1.93/../evil",
        "/%2e%2e/evil",
        "/d2l/api/le/1.93/%2e%2e/evil",
        "/d2l/api/le/1.93/%5c%2e%2e/evil",
    ],
)
def test_get_rejects_absolute_urls_outside_lighthouse_origin(path: str) -> None:
    client, session = _authenticated_client_with_session()

    with pytest.raises(NetworkError, match="Invalid API URL"):
        client.get(path)

    session.request.assert_not_called()


def test_get_normalizes_same_origin_absolute_https_url() -> None:
    client = LighthouseClient()
    _authenticate(client)
    session = FakeSession([FakeResponse(200)])
    client._session = session

    response = client.get(
        "HTTPS://LIGHTHOUSE.MANIPAL.EDU:443/d2l/api/le/1.93/resource?page=2"
    )

    assert response.status_code == 200
    assert session.calls[0][1] == (
        f"{BASE_URL}/d2l/api/le/1.93/resource?page=2"
    )


def test_final_rate_limit_response_raises_url_free_http_error() -> None:
    client, session = _client_with_session([FakeResponse(429) for _ in range(4)])
    url = "https://example.test/resource?session=RATE_LIMIT_URL_SENTINEL"

    with patch.object(api.time, "sleep"):
        with pytest.raises(requests.HTTPError) as exc_info:
            client._do_request("GET", url, False, 30)

    assert "RATE_LIMIT_URL_SENTINEL" not in str(exc_info.value)
    assert "https://example.test" not in str(exc_info.value)
    assert len(session.calls) == 4


@pytest.mark.parametrize(
    ("status_code", "error", "match"),
    [
        pytest.param(429, NetworkError, "no retry", id="rate-limit-raises-safe-error"),
        pytest.param(401, api.SessionExpiredError, None, id="unauthorized-without-refresh"),
    ],
)
def test_submit_file_failure_sends_body_once_without_refresh(
    status_code: int,
    error: type[Exception],
    match: str | None,
) -> None:
    client, session = _client_with_session([FakeResponse(status_code)], authenticated=True)

    with patch.object(api, "refresh_auth_from_browser") as refresh:
        with pytest.raises(error, match=match):
            client.submit_file(
                org_unit_id=44347,
                folder_id=789,
                file_bytes=b"payload",
                filename="test.pdf",
            )

    assert len(session.calls) == 1
    assert session.calls[0][0] == "POST"
    assert session.calls[0][2]["data"]
    refresh.assert_not_called()


@pytest.mark.parametrize(
    "next_url",
    [
        "http://lighthouse.manipal.edu/d2l/api/le/1.93/page=2",
        "https://attacker.example/d2l/api/le/1.93/page=2",
        "https://lighthouse.manipal.edu:8443/d2l/api/le/1.93/page=2",
        "https://user:pass@lighthouse.manipal.edu/d2l/api/le/1.93/page=2",
        f"{BASE_URL}/d2l/api/le/1.93/../secret",
        "//attacker.example/d2l/api/le/1.93/page=2",
        "../page=2",
    ],
)
def test_paginated_next_rejects_untrusted_targets_without_echoing_url(
    next_url: str,
) -> None:
    client = _paged_client({"Items": [{"id": 1}], "Next": next_url})

    with pytest.raises(NetworkError, match="Invalid pagination link") as exc_info:
        client._paginate_list("/enrollments", "Items")

    assert next_url not in str(exc_info.value)
    assert client.get_json.call_count == 1


def test_paginated_next_enforces_maximum_page_count() -> None:
    client = _paged_client(
        {"Items": [{"id": 1}], "Next": "/enrollments?page=2"},
        {"Items": [{"id": 2}], "Next": "/enrollments?page=3"},
    )
    client._MAX_PAGINATION_PAGES = 2

    with pytest.raises(NetworkError, match="maximum page count"):
        client._paginate_list("/enrollments", "Items")

    assert client.get_json.call_count == 2


def test_extract_filename_preserves_quoted_semicolon() -> None:
    headers = {"Content-Disposition": 'attachment; filename="notes;week-1.pdf"'}

    assert _extract_filename(headers) == "notes;week-1.pdf"


def test_extract_filename_decodes_rfc5987_utf8_and_prefers_it() -> None:
    headers = {
        "content-disposition": (
            "attachment; filename=legacy.pdf; "
            "filename*=UTF-8''caf%C3%A9%20notes.pdf"
        )
    }

    assert _extract_filename(headers) == "café notes.pdf"


class FakeUrlopenResponse:
    """Context manager returning a browser version response."""

    def __init__(
        self,
        payload: bytes,
        *,
        status: int | None = None,
        final_url: str | None = None,
    ) -> None:
        self.payload = payload
        self.status = status
        self.final_url = final_url

    def __enter__(self) -> FakeUrlopenResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload

    def geturl(self) -> str | None:
        return self.final_url


def _cdp_version(websocket_url: str, **kwargs: object) -> FakeUrlopenResponse:
    payload = json.dumps({"webSocketDebuggerUrl": websocket_url}).encode()
    return FakeUrlopenResponse(payload, **kwargs)


@contextmanager
def _patched_cdp(response: FakeUrlopenResponse, websocket_call: AsyncMock) -> Iterator[MagicMock]:
    opener = MagicMock()
    opener.open.return_value = response
    with patch.object(urllib.request, "build_opener", return_value=opener), \
            patch.object(api, "_cdp_get_cookies_ws", websocket_call):
        yield opener


@pytest.mark.parametrize(
    ("response", "match", "leaked"),
    [
        pytest.param(
            _cdp_version("ws://attacker.example/devtools/browser/1"),
            "non-loopback",
            [],
            id="non-loopback-websocket",
        ),
        pytest.param(
            _cdp_version("wss://127.0.0.1:9223/devtools/browser/1"),
            "unexpected port",
            [],
            id="loopback-websocket-on-unexpected-port",
        ),
        pytest.param(
            FakeUrlopenResponse(
                b"",
                status=302,
                final_url="https://attacker.example/cdp?token=REDIRECT_TOKEN_SENTINEL",
            ),
            "redirect",
            ["REDIRECT_TOKEN_SENTINEL", "attacker.example"],
            id="discovery-redirect-not-followed",
        ),
        pytest.param(
            FakeUrlopenResponse(
                b"{}",
                status=200,
                final_url="http://attacker.example/json/version?token=FINAL_TOKEN_SENTINEL",
            ),
            "invalid response",
            ["FINAL_TOKEN_SENTINEL", "attacker.example"],
            id="discovery-external-final-url",
        ),
    ],
)
def test_cdp_rejects_unsafe_discovery_before_websocket_connect(
    response: FakeUrlopenResponse,
    match: str,
    leaked: list[str],
) -> None:
    websocket_call = AsyncMock()

    with _patched_cdp(response, websocket_call) as opener:
        with pytest.raises(NetworkError, match=match) as exc_info:
            api._refresh_via_cdp_websocket(9222)

    opener.open.assert_called_once_with(
        "http://127.0.0.1:9222/json/version", timeout=10
    )
    websocket_call.assert_not_awaited()
    for value in leaked:
        assert value not in str(exc_info.value)


def test_cdp_accepts_loopback_wss_on_configured_port() -> None:
    response = _cdp_version(
        "wss://localhost:9222/devtools/browser/1",
        status=200,
        final_url="http://127.0.0.1:9222/json/version",
    )
    websocket_call = AsyncMock(
        return_value={"d2lSessionVal": "session", "d2lSecureSessionVal": "secure"}
    )

    with _patched_cdp(response, websocket_call):
        assert api._refresh_via_cdp_websocket(9222) == {
            "d2lSessionVal": "session",
            "d2lSecureSessionVal": "secure",
        }

    websocket_call.assert_awaited_once_with(
        "wss://localhost:9222/devtools/browser/1"
    )


def test_cdp_endpoint_failure_is_wrapped_without_url_details() -> None:
    url = "http://127.0.0.1:9222/json/version?token=CDP_URL_SENTINEL"
    opener = MagicMock()
    opener.open.side_effect = OSError(f"connection failed for {url}")

    with patch.object(urllib.request, "build_opener", return_value=opener):
        with pytest.raises(NetworkError) as exc_info:
            api._refresh_via_cdp_websocket(9222)

    assert "CDP_URL_SENTINEL" not in str(exc_info.value)
    assert "127.0.0.1" not in str(exc_info.value)


def test_cdp_websocket_failure_is_wrapped_without_url_details() -> None:
    response = _cdp_version("ws://127.0.0.1:9222/devtools/browser/1")
    websocket_call = AsyncMock(
        side_effect=RuntimeError(
            "websocket failed at ws://127.0.0.1:9222/?token=WS_URL_SENTINEL"
        )
    )

    with _patched_cdp(response, websocket_call):
        with pytest.raises(NetworkError) as exc_info:
            api._refresh_via_cdp_websocket(9222)

    assert "WS_URL_SENTINEL" not in str(exc_info.value)
    assert "127.0.0.1" not in str(exc_info.value)


def test_browser_harness_failure_does_not_expose_stderr() -> None:
    result = MagicMock(
        returncode=1,
        stderr="helper failed with COOKIE_SECRET_SENTINEL",
        stdout="",
    )

    with patch("subprocess.run", return_value=result):
        with pytest.raises(NetworkError) as exc_info:
            api._refresh_via_browser_harness(9222)

    assert "COOKIE_SECRET_SENTINEL" not in str(exc_info.value)


def test_browser_harness_failure_falls_back_to_direct_cdp() -> None:
    expected = {"d2lSessionVal": "session"}

    with (
        patch.object(
            api,
            "_refresh_via_browser_harness",
            side_effect=api._BrowserHarnessFallbackError("helper failed"),
        ),
        patch.object(
            api,
            "_refresh_via_cdp_websocket",
            return_value=expected,
        ) as direct,
    ):
        assert api.refresh_auth_from_browser(9222) == expected

    direct.assert_called_once_with(9222)


def test_cdp_cookie_receive_has_an_end_to_end_timeout() -> None:
    observed: dict[str, float] = {}

    class FakeWebSocket:
        async def send(self, _payload: str) -> None:
            raise TimeoutError

        async def recv(self) -> str:
            return "{}"

    class FakeConnection:
        async def __aenter__(self) -> FakeWebSocket:
            return FakeWebSocket()

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def timeout(awaitable: object, *, timeout: float) -> object:
        observed["timeout"] = timeout
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    fake_websockets = types.SimpleNamespace(
        connect=lambda *_args, **_kwargs: FakeConnection()
    )
    with patch.dict(sys.modules, {"websockets": fake_websockets}), \
            patch("asyncio.wait_for", side_effect=timeout):
        with pytest.raises(NetworkError, match="cookie connection failed"):
            asyncio.run(
                api._cdp_get_cookies_ws(
                    "ws://127.0.0.1:9222/devtools/browser/1"
                )
            )

    assert observed["timeout"] == api.CDP_RESPONSE_TIMEOUT_SECONDS


def test_a_403_keeps_its_status_for_the_attempt_account_check() -> None:
    # The learner and preview account checks tell an ended session apart by this status.
    client, _session = _client_with_session([FakeResponse(403)])

    with pytest.raises(requests.HTTPError) as raised:
        client._do_request("GET", "https://example.test", False, 30)

    assert raised.value.response.status_code == 403


# ---------------------------------------------------------------------------
# Session probe (whoami) and automatic sign-in
# ---------------------------------------------------------------------------

RESOURCE_URL = BASE_URL + "/d2l/api/lp/1.47/enrollments/myenrollments/"
WHOAMI_URL = BASE_URL + "/d2l/api/lp/1.47/users/whoami"
RENEWED_COOKIES = dict.fromkeys(api.COOKIE_NAMES, "renewed-sentinel")
BROWSER_COOKIES = dict.fromkeys(api.COOKIE_NAMES, "browser-sentinel")
LEGACY_COOKIES = dict.fromkeys(api.COOKIE_NAMES, "legacy-sentinel")
PRIVATE_NAME = "Private Learner Sentinel"


def _whoami(identifier: object = "4242") -> FakeResponse:
    return FakeResponse(200, {"Identifier": identifier, "FirstName": PRIVATE_NAME})


class NonJsonResponse(FakeResponse):
    def json(self) -> object:
        raise ValueError(PRIVATE_NAME)


class JarSession:
    """Session double with the cookie jar that installed cookies populate.

    Queued exceptions are raised instead of returned.
    """

    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = iter(responses)
        self.calls: list[str] = []
        self.urls: list[str] = []
        self.cookies = requests.cookies.RequestsCookieJar()

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(method)
        self.urls.append(url)
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass


def _jar_client(
    responses: list[FakeResponse | Exception],
    *,
    read_only_auth: bool = False,
    cookies: dict[str, str] | None = SYNTHETIC_COOKIES,
) -> tuple[LighthouseClient, JarSession]:
    """A client on a fake session; ``cookies=None`` loads them from disk."""
    client = LighthouseClient(read_only_auth=read_only_auth)
    client._csrf_token = "synthetic-csrf"
    session = JarSession(responses)
    client._session = session
    if cookies is not None:
        client._loaded = True
        client._cookies = dict(cookies)
    return client, session


def _jar(session: JarSession) -> dict[str, str]:
    return {cookie.name: cookie.value or "" for cookie in session.cookies}


@pytest.fixture
def renewal(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Unattended sign-in is configured and signs in with fresh cookies."""

    def sign_in(**kwargs: Any) -> RenewalResult:
        result: RenewalResult = renew.return_value
        if result.signed_in:
            kwargs["on_sign_in"]()
        return result

    monkeypatch.setattr(api, "unattended_configured", lambda: True)
    renew = MagicMock(
        return_value=RenewalResult(dict(RENEWED_COOKIES), signed_in=True), side_effect=sign_in,
    )
    monkeypatch.setattr(api, "renew_session", renew)
    return renew


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A signed-in browser that CDP can copy cookies from."""
    refresh = MagicMock(return_value=dict(BROWSER_COOKIES))
    monkeypatch.setattr(api, "refresh_auth_from_browser", refresh)
    return refresh


@contextmanager
def _sign_in_lock_held_elsewhere() -> Iterator[None]:
    """Hold the sign-in lock from another thread for the duration."""
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with session_renewal.renewal_lock():
            held.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(5)
        yield
    finally:
        release.set()
        thread.join(5)


def _write_legacy_cookies() -> bytes:
    path = CredentialStore().cookie_file
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    original = json.dumps({"cookies": LEGACY_COOKIES}).encode("utf-8")
    path.write_bytes(original)
    path.chmod(0o600)
    return original


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param(_whoami("4242"), "valid", id="string-identifier"),
        pytest.param(_whoami(4242), "valid", id="integer-identifier"),
        pytest.param(FakeResponse(200, {"FirstName": PRIVATE_NAME}), "invalid", id="no-identifier"),
        pytest.param(_whoami("0"), "invalid", id="zero-identifier"),
        pytest.param(_whoami(True), "invalid", id="boolean-identifier"),
        pytest.param(FakeResponse(200, ["4242"]), "invalid", id="not-an-object"),
        pytest.param(NonJsonResponse(200), "invalid", id="not-json"),
    ],
)
def test_probe_reads_only_a_positive_whoami_identifier(
    response: FakeResponse,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, session = _jar_client([response], read_only_auth=True)

    assert client.probe_session() == expected

    assert session.calls == ["GET"]
    assert response.closed is True
    captured = capsys.readouterr()
    assert PRIVATE_NAME not in captured.out + captured.err


@pytest.mark.parametrize("status", [401, 403])
def test_signed_out_whoami_is_invalid_for_a_read_only_client(
    status: int,
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, _session = _jar_client([FakeResponse(status)], read_only_auth=True)
    assert client.probe_session() == "invalid"

    client, _session = _jar_client([FakeResponse(status)], read_only_auth=True)
    assert client.check_auth() is False

    renewal.assert_not_called()
    browser.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(FakeResponse(503), id="server-error"),
        pytest.param(requests.ConnectionError("NETWORK_SENTINEL"), id="connection-error"),
    ],
)
def test_probe_reports_unavailable_when_d2l_does_not_answer(
    failure: FakeResponse | Exception,
) -> None:
    client, _session = _jar_client([failure] * (LighthouseClient._MAX_RETRIES + 1), read_only_auth=True)

    with patch.object(api.time, "sleep"):
        assert client.probe_session() == "unavailable"


def test_check_auth_keeps_its_exception_contract() -> None:
    # A server error still reads as signed out; a transport failure still raises.
    client, _session = _jar_client([FakeResponse(503)], read_only_auth=True)
    assert client.check_auth() is False

    client, _session = _jar_client(
        [requests.ConnectionError("NETWORK_SENTINEL")] * (LighthouseClient._MAX_RETRIES + 1),
        read_only_auth=True,
    )
    with patch.object(api.time, "sleep"):
        with pytest.raises(NetworkError):
            client.check_auth()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param(_whoami(), "valid", id="signed-in"),
        pytest.param(FakeResponse(401), "invalid", id="signed-out"),
    ],
)
def test_probe_cookies_sends_only_the_candidate_cookies(
    response: FakeResponse,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    save_cookies(dict(BROWSER_COOKIES))
    on_disk = CredentialStore().cookie_file.read_bytes()
    sessions: list[JarSession] = []

    def new_session() -> JarSession:
        sessions.append(JarSession([response]))
        return sessions[-1]

    monkeypatch.setattr(api.requests, "Session", new_session)
    monkeypatch.setattr(api, "load_cookies", MagicMock(side_effect=AssertionError("cookies.json read")))

    assert api.probe_cookies(dict(RENEWED_COOKIES)) == expected

    [session] = sessions
    assert session.calls == ["GET"]
    assert _jar(session) == RENEWED_COOKIES
    renewal.assert_not_called()
    browser.assert_not_called()
    assert CredentialStore().cookie_file.read_bytes() == on_disk


def test_a_403_while_whoami_is_signed_in_stays_a_permission_error(
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    # Genuine denials (a closed folder, a hidden course) keep their 403.
    denied, signed_in = FakeResponse(403), _whoami()
    client, session = _jar_client([denied, signed_in, FakeResponse(403), _whoami()])

    with pytest.raises(requests.HTTPError) as raised:
        client._request("GET", RESOURCE_URL)

    assert raised.value.response is denied
    assert denied.closed
    assert signed_in.closed
    assert session.urls == [RESOURCE_URL, WHOAMI_URL]

    # The answer is not remembered: the session may expire in between.
    with pytest.raises(requests.HTTPError):
        client._request("GET", RESOURCE_URL)
    assert session.urls == [RESOURCE_URL, WHOAMI_URL, RESOURCE_URL, WHOAMI_URL]
    renewal.assert_not_called()
    browser.assert_not_called()


_SIGNED_OUT_WHOAMI = [
    pytest.param(401, None, id="whoami-401"),
    pytest.param(403, None, id="whoami-403"),
    pytest.param(302, {"Location": "/d2l/login"}, id="whoami-login-redirect"),
]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(("status", "headers"), _SIGNED_OUT_WHOAMI)
def test_a_403_from_a_signed_out_session_signs_in_once_and_replays_once(
    status: int,
    headers: dict[str, str] | None,
    method: str,
    renewal: MagicMock,
    browser: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # D2L answers some reads of an expired session with 403, not 401.
    client, session = _jar_client(
        [FakeResponse(403), FakeResponse(status, headers=headers), FakeResponse(200)],
    )

    assert client._request(method, RESOURCE_URL).status_code == 200

    assert session.calls == [method, "GET", method]
    assert session.urls == [RESOURCE_URL, WHOAMI_URL, RESOURCE_URL]
    renewal.assert_called_once_with(
        observed_cookies=SYNTHETIC_COOKIES, validate=api.probe_cookies, automatic=True,
        on_sign_in=api._announce_sign_in,
    )
    browser.assert_not_called()
    assert client.renewed_session is True
    assert _jar(session) == RENEWED_COOKIES
    assert client._csrf_token is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "renewed-sentinel" not in captured.err


@pytest.mark.parametrize(
    "whoami",
    [
        pytest.param(FakeResponse(500), id="server-error"),
        pytest.param(FakeResponse(503), id="unavailable"),
        pytest.param(FakeResponse(429, headers={"Retry-After": "60"}), id="rate-limited"),
        pytest.param(FakeResponse(302, headers={"Location": "/d2l/other"}), id="other-redirect"),
        pytest.param(requests.ConnectionError("SENTINEL"), id="network"),
    ],
)
def test_a_403_is_kept_when_whoami_gives_no_answer(
    whoami: FakeResponse | Exception,
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    # The check is one attempt: no retry, no backoff, no Retry-After wait.
    denied = FakeResponse(403)
    client, session = _jar_client([denied, whoami])

    with patch.object(api.time, "sleep") as sleep, pytest.raises(requests.HTTPError) as raised:
        client._request("GET", RESOURCE_URL)

    assert raised.value.response is denied
    assert session.urls == [RESOURCE_URL, WHOAMI_URL]
    sleep.assert_not_called()
    renewal.assert_not_called()
    browser.assert_not_called()


def test_the_whoami_check_has_its_own_short_timeout_and_no_request_options(renewal: MagicMock) -> None:
    client, session = _jar_client([FakeResponse(403), _whoami()])
    seen: list[dict[str, object]] = []
    request = session.request

    def recording(method: str, url: str, **kwargs: object) -> FakeResponse:
        seen.append(kwargs)
        return request(method, url, **kwargs)

    session.request = recording  # type: ignore[method-assign]
    with pytest.raises(requests.HTTPError):
        client._request("GET", RESOURCE_URL, stream=True, headers={"Range": "bytes=0-1"})

    assert seen[1] == {"allow_redirects": False, "timeout": LighthouseClient._SIGNED_OUT_CHECK_TIMEOUT}
    renewal.assert_not_called()


def test_a_403_returned_to_the_caller_is_not_checked(renewal: MagicMock) -> None:
    client, session = _jar_client([FakeResponse(403)])

    assert client._request("GET", RESOURCE_URL, _skip_raise=True).status_code == 403

    assert session.urls == [RESOURCE_URL]
    renewal.assert_not_called()


def test_a_signed_out_403_without_unattended_sign_in_refreshes_from_the_browser(
    browser: MagicMock,
) -> None:
    client, session = _jar_client([FakeResponse(403), FakeResponse(403), FakeResponse(200)])

    assert client._request("GET", RESOURCE_URL).status_code == 200

    assert session.urls == [RESOURCE_URL, WHOAMI_URL, RESOURCE_URL]
    browser.assert_called_once_with()
    assert client.cookies == BROWSER_COOKIES
    assert client.renewed_session is False


def test_a_signed_out_403_with_no_browser_reports_an_expired_session(
    browser: MagicMock,
) -> None:
    browser.side_effect = RuntimeError("no browser")
    client, session = _jar_client([FakeResponse(403), FakeResponse(403)])

    with pytest.raises(SessionExpiredError, match=r"auto-refresh failed"):
        client._request("GET", RESOURCE_URL)

    assert session.urls == [RESOURCE_URL, WHOAMI_URL]


def test_a_403_after_signing_in_again_does_not_sign_in_twice(
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, session = _jar_client(
        [FakeResponse(403), FakeResponse(403), FakeResponse(403), FakeResponse(403)],
    )

    with pytest.raises(SessionExpiredError, match="auto-refresh already attempted"):
        client._request("GET", RESOURCE_URL)

    assert session.urls == [RESOURCE_URL, WHOAMI_URL, RESOURCE_URL, WHOAMI_URL]
    renewal.assert_called_once()
    browser.assert_not_called()


def test_a_403_after_signing_in_again_is_a_permission_error_if_whoami_is_signed_in(
    renewal: MagicMock,
) -> None:
    denied = FakeResponse(403)
    client, session = _jar_client([FakeResponse(403), FakeResponse(403), denied, _whoami()])

    with pytest.raises(requests.HTTPError) as raised:
        client._request("GET", RESOURCE_URL)

    assert raised.value.response is denied
    assert session.urls == [RESOURCE_URL, WHOAMI_URL, RESOURCE_URL, WHOAMI_URL]
    renewal.assert_called_once()


def test_a_403_after_signing_in_for_a_401_does_not_sign_in_twice(
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, session = _jar_client([FakeResponse(401), FakeResponse(403), FakeResponse(403)])

    with pytest.raises(SessionExpiredError, match="auto-refresh already attempted"):
        client._request("GET", RESOURCE_URL)

    assert session.urls == [RESOURCE_URL, RESOURCE_URL, WHOAMI_URL]
    renewal.assert_called_once()
    browser.assert_not_called()


def test_a_failed_sign_in_after_a_signed_out_403_records_its_category(
    renewal: MagicMock,
    browser: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A refusal by the breaker: renew_session raises without signing in.
    renewal.side_effect = RenewalError("code_rejected")
    client, session = _jar_client([FakeResponse(403), FakeResponse(403)])

    with pytest.raises(SessionExpiredError, match="automatic sign-in failed: code_rejected"):
        client._request("GET", RESOURCE_URL)

    assert client.renewal_failure == "code_rejected"
    assert session.urls == [RESOURCE_URL, WHOAMI_URL]
    browser.assert_not_called()
    assert "Signing in again" not in capsys.readouterr().err


def test_a_signed_out_403_on_a_later_page_resumes_that_page_once(renewal: MagicMock) -> None:
    second_page = f"{ENROLLMENTS_PAGE}?page=2"
    client, session = _jar_client([
        FakeResponse(200, {"Items": [{"id": 1}], "Next": second_page}),
        FakeResponse(403),
        FakeResponse(403),
        FakeResponse(200, {"Items": [{"id": 2}], "Next": None}),
    ])

    assert client.get_enrollments() == [{"id": 1}, {"id": 2}]

    assert session.urls == [ENROLLMENTS_PAGE, second_page, WHOAMI_URL, second_page]
    renewal.assert_called_once()


def test_a_closed_folder_403_still_reports_the_closed_assignment(
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    folder = {"Availability": {"EndDateAvailabilityType": 0, "EndDate": "2020-01-01T00:00:00.000Z"}}
    client, session = _jar_client([FakeResponse(403), _whoami(), FakeResponse(200, folder)])

    with pytest.raises(AssignmentClosedError):
        AssessmentAPI(client, 12).submissions(34, mine=True)

    assert session.urls[1] == WHOAMI_URL
    renewal.assert_not_called()
    browser.assert_not_called()


@pytest.mark.parametrize(
    ("workflow", "error"),
    [
        pytest.param(LearnerWorkflow, LearnerWorkflowError, id="learner"),
        pytest.param(PreviewWorkflow, PreviewWorkflowError, id="preview"),
    ],
)
def test_quiz_account_checks_still_see_their_403(
    workflow: type[LearnerWorkflow | PreviewWorkflow],
    error: type[Exception],
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, session = _jar_client([FakeResponse(403)])

    with pytest.raises(error, match=r"HTTP 403"):
        object.__new__(workflow)._actor(client)

    assert session.urls == [WHOAMI_URL]
    renewal.assert_not_called()
    browser.assert_not_called()


def test_a_signed_out_403_after_signing_in_for_missing_cookies_is_final(
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, session = _jar_client([FakeResponse(403), FakeResponse(403)], cookies={})

    with pytest.raises(SessionExpiredError, match="auto-refresh already attempted"):
        client._request("GET", RESOURCE_URL)

    assert session.urls == [RESOURCE_URL, WHOAMI_URL]
    renewal.assert_called_once()
    browser.assert_not_called()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_an_expired_session_signs_in_once_and_replays_once(
    method: str,
    renewal: MagicMock,
    browser: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, session = _jar_client([FakeResponse(401), FakeResponse(200)])

    assert client._request(method, RESOURCE_URL).status_code == 200

    assert session.calls == [method, method]
    renewal.assert_called_once_with(
        observed_cookies=SYNTHETIC_COOKIES, validate=api.probe_cookies, automatic=True,
        on_sign_in=api._announce_sign_in,
    )
    browser.assert_not_called()
    assert client.renewed_session is True
    assert client.renewal_failure is None
    assert client.cookies == RENEWED_COOKIES
    assert _jar(session) == RENEWED_COOKIES
    assert client._csrf_token is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Signing in again with the stored authenticator" in captured.err
    assert "renewed-sentinel" not in captured.err


def test_reusing_another_callers_sign_in_is_not_reported_as_renewed(
    renewal: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    renewal.return_value = RenewalResult(dict(RENEWED_COOKIES), signed_in=False)
    client, _session = _jar_client([FakeResponse(401), FakeResponse(200)])

    client._request("GET", RESOURCE_URL)

    assert client.renewed_session is False
    assert client.cookies == RENEWED_COOKIES
    assert "Signing in again" not in capsys.readouterr().err


@pytest.mark.parametrize("status", [401, 403])
def test_check_auth_signs_in_again_after_a_signed_out_whoami(
    status: int,
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, session = _jar_client([FakeResponse(status), _whoami()])

    assert client.check_auth() is True

    assert session.calls == ["GET", "GET"]
    renewal.assert_called_once()
    browser.assert_not_called()
    assert client.renewed_session is True


def test_check_auth_without_unattended_sign_in_still_refreshes_from_the_browser(
    browser: MagicMock,
) -> None:
    client, session = _jar_client([FakeResponse(403), _whoami()])

    assert client.check_auth() is True

    assert session.calls == ["GET", "GET"]
    browser.assert_called_once_with()
    assert client.cookies == BROWSER_COOKIES
    assert load_cookies() == BROWSER_COOKIES
    assert client.renewed_session is False


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(None, id="no-cookies-file"),
        pytest.param({"d2lSessionVal": "partial"}, id="partial-cookies"),
    ],
)
def test_missing_cookies_sign_in_before_the_request_when_configured(
    stored: dict[str, str] | None,
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, session = _jar_client([FakeResponse(200)], cookies=stored)

    assert client._request("GET", RESOURCE_URL).status_code == 200

    renewal.assert_called_once_with(
        observed_cookies=stored or {}, validate=api.probe_cookies, automatic=True,
        on_sign_in=api._announce_sign_in,
    )
    browser.assert_not_called()
    assert session.calls == ["GET"]
    assert client.renewed_session is True
    assert client.cookies == RENEWED_COOKIES


def test_missing_cookies_without_unattended_sign_in_fail_before_any_request(
    browser: MagicMock,
) -> None:
    client, session = _jar_client([], cookies=None)

    with pytest.raises(SessionExpiredError, match="no cookies found"):
        client._request("GET", RESOURCE_URL)

    assert session.calls == []
    browser.assert_not_called()


def test_missing_cookies_and_no_matching_authenticator_never_open_the_browser(
    renewal: MagicMock,
    browser: MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    renewal.side_effect = RenewalUnavailableError()
    client, session = _jar_client([], cookies=None)

    with pytest.raises(SessionExpiredError, match="no cookies found"):
        client._request("GET", RESOURCE_URL)

    assert session.calls == []
    renewal.assert_called_once()
    browser.assert_not_called()
    assert "Signing in again" not in capsys.readouterr().err


def test_expiry_with_no_matching_authenticator_falls_back_to_the_browser(
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    renewal.side_effect = RenewalUnavailableError()
    client, session = _jar_client([FakeResponse(401), FakeResponse(200)])

    assert client._request("GET", RESOURCE_URL).status_code == 200

    assert session.calls == ["GET", "GET"]
    browser.assert_called_once_with()
    assert client.cookies == BROWSER_COOKIES
    assert client.renewed_session is False
    assert client.renewal_failure is None


@pytest.mark.parametrize("category", sorted(RELOGIN_CATEGORIES))
def test_a_failed_sign_in_records_its_category_and_skips_the_browser(
    category: str,
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    renewal.side_effect = RenewalError(category)
    client, session = _jar_client([FakeResponse(401)])

    with pytest.raises(SessionExpiredError) as raised:
        client._request("GET", RESOURCE_URL)

    assert f"automatic sign-in failed: {category}" in str(raised.value)
    assert client.renewal_failure == category
    assert client.renewed_session is False
    assert session.calls == ["GET"]
    browser.assert_not_called()
    assert client.cookies == SYNTHETIC_COOKIES


_NO_RECOVERY_CASES = [
    pytest.param("POST", {}, "default", id="post"),
    pytest.param("PUT", {}, "default", id="put"),
    pytest.param("DELETE", {}, "default", id="delete"),
    pytest.param("GET", {"_replay_safe": False}, "default", id="state-creating-get"),
    pytest.param("GET", {}, "read-only", id="read-only-client"),
    pytest.param("GET", {}, "override", id="connection-override"),
]


def _client_for(
    kind: str,
    responses: list[FakeResponse | Exception],
    monkeypatch: pytest.MonkeyPatch,
    cookies: dict[str, str] | None = SYNTHETIC_COOKIES,
) -> tuple[LighthouseClient, JarSession]:
    if kind == "override":
        # A value-identical copy of the built-in connection still may not sign in.
        copy = Connection(LIGHTHOUSE.origin, None)
        monkeypatch.setattr(connection, "active_connection", lambda: copy)
    return _jar_client(responses, read_only_auth=kind == "read-only", cookies=cookies)


@pytest.mark.parametrize(("method", "options", "kind"), _NO_RECOVERY_CASES)
def test_an_expiry_is_not_recovered_when_replay_or_sign_in_is_not_allowed(
    method: str,
    options: dict[str, object],
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, session = _client_for(kind, [FakeResponse(401)], monkeypatch)

    with pytest.raises(SessionExpiredError):
        client._request(method, RESOURCE_URL, **options)

    assert session.calls == [method]
    renewal.assert_not_called()
    browser.assert_not_called()


@pytest.mark.parametrize(("method", "options", "kind"), _NO_RECOVERY_CASES)
def test_missing_cookies_are_not_recovered_when_replay_or_sign_in_is_not_allowed(
    method: str,
    options: dict[str, object],
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, session = _client_for(kind, [], monkeypatch, cookies={})

    with pytest.raises(SessionExpiredError, match="no cookies found"):
        client._request(method, RESOURCE_URL, **options)

    assert session.calls == []
    renewal.assert_not_called()
    browser.assert_not_called()


@pytest.mark.parametrize(("method", "options", "kind"), _NO_RECOVERY_CASES)
def test_a_403_is_not_checked_when_replay_or_sign_in_is_not_allowed(
    method: str,
    options: dict[str, object],
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, session = _client_for(kind, [FakeResponse(403)], monkeypatch)

    with pytest.raises(requests.HTTPError) as raised:
        client._request(method, RESOURCE_URL, **options)

    assert raised.value.response.status_code == 403
    assert session.calls == [method]
    renewal.assert_not_called()
    browser.assert_not_called()


def test_a_second_expiry_after_signing_in_does_not_sign_in_again(
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, session = _jar_client([FakeResponse(401), FakeResponse(401)])

    with pytest.raises(SessionExpiredError, match="auto-refresh already attempted"):
        client._request("GET", RESOURCE_URL)

    assert session.calls == ["GET", "GET"]
    renewal.assert_called_once()
    browser.assert_not_called()


def test_signing_in_for_missing_cookies_uses_up_the_single_recovery(
    renewal: MagicMock,
    browser: MagicMock,
) -> None:
    client, session = _jar_client([FakeResponse(401)], cookies={})

    with pytest.raises(SessionExpiredError, match="auto-refresh already attempted"):
        client._request("GET", RESOURCE_URL)

    assert session.calls == ["GET"]
    renewal.assert_called_once()
    browser.assert_not_called()


def test_a_browser_refresh_writes_nothing_while_a_sign_in_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
    browser: MagicMock,
) -> None:
    monkeypatch.setattr(session_renewal, "LOCK_TIMEOUT_SECONDS", 0.2)
    client, session = _jar_client([FakeResponse(401)])

    with _sign_in_lock_held_elsewhere():
        with pytest.raises(SessionExpiredError, match="auto-refresh busy"):
            client._request("GET", RESOURCE_URL)

    browser.assert_called_once_with()
    assert session.calls == ["GET"]
    assert not CredentialStore().cookie_file.exists()
    assert client.cookies == SYNTHETIC_COOKIES


def test_the_legacy_cookie_upgrade_returns_cookies_a_concurrent_sign_in_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_legacy_cookies()
    held, waiting = threading.Event(), threading.Event()
    real_lock = api.renewal_lock

    @contextmanager
    def signalling_lock(**kwargs: float | None) -> Iterator[None]:
        waiting.set()
        with real_lock(**kwargs):
            yield

    monkeypatch.setattr(api, "renewal_lock", signalling_lock)

    def sign_in() -> None:
        with session_renewal.renewal_lock():
            held.set()
            # Write only once the upgrade has seen the legacy file and is
            # waiting for the lock.
            waiting.wait(5)
            save_cookies(dict(RENEWED_COOKIES))

    thread = threading.Thread(target=sign_in)
    thread.start()
    try:
        assert held.wait(5)
        cookies = LighthouseClient().cookies
    finally:
        waiting.set()
        thread.join(5)

    assert cookies == RENEWED_COOKIES
    assert not cookie_file_is_legacy()
    assert load_cookies() == RENEWED_COOKIES


def test_the_legacy_cookie_upgrade_leaves_the_file_untouched_while_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _write_legacy_cookies()
    monkeypatch.setattr(session_renewal, "LOCK_TIMEOUT_SECONDS", 0.2)
    client, session = _jar_client([], cookies=None)

    with _sign_in_lock_held_elsewhere():
        with pytest.raises(SessionExpiredError, match="cookie upgrade busy"):
            client._request("GET", RESOURCE_URL)

    assert session.calls == []
    assert CredentialStore().cookie_file.read_bytes() == original
