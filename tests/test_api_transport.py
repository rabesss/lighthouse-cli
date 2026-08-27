"""Focused tests for LighthouseClient's low-level transport helpers."""

from __future__ import annotations

import json
import urllib.request
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

import lighthouse_cli.api as api
from lighthouse_cli.api import (
    BASE_URL,
    ContentResponseShapeError,
    LighthouseClient,
    NetworkError,
    SubmissionOutcomeUnknownError,
    _extract_filename,
)


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

    def json(self) -> object:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Session double that records each request and returns queued responses."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return next(self.responses)


def _client_with_session(responses: list[FakeResponse]) -> tuple[LighthouseClient, FakeSession]:
    client = LighthouseClient()
    session = FakeSession(responses)
    client._session = session
    return client, session


def test_get_enrollments_reuses_paginated_items_endpoint() -> None:
    client = LighthouseClient()
    client.get_json = MagicMock(
        side_effect=[
            {"Items": [{"id": 1}], "Next": "/enrollments?page=2"},
            {"Items": [{"id": 2}], "Next": None},
        ]
    )

    assert client.get_enrollments() == [{"id": 1}, {"id": 2}]
    assert client.get_json.call_args_list == [
        ((f"{BASE_URL}/d2l/api/lp/1.47/enrollments/myenrollments/",),),
        (("/enrollments?page=2",),),
    ]


def test_paginated_query_only_next_uses_current_resource_path() -> None:
    client = LighthouseClient()
    first_page = f"{BASE_URL}/d2l/api/lp/1.47/enrollments/myenrollments/?page=1"
    second_page = f"{BASE_URL}/d2l/api/lp/1.47/enrollments/myenrollments/?page=2"
    client.get_json = MagicMock(
        side_effect=[
            {"Items": [{"id": 1}], "Next": "?page=2"},
            {"Items": [{"id": 2}], "Next": None},
        ]
    )

    assert client._paginate_list(first_page, "Items") == [
        {"id": 1},
        {"id": 2},
    ]
    assert client.get_json.call_args_list == [
        ((first_page,),),
        ((second_page,),),
    ]


def test_paginated_next_cycle_raises_clean_network_error() -> None:
    client = LighthouseClient()
    client.get_json = MagicMock(
        side_effect=[
            {"Items": [{"id": 1}], "Next": "/enrollments?page=2"},
            {"Items": [{"id": 2}], "Next": "/enrollments?page=2"},
        ]
    )

    with pytest.raises(NetworkError, match="Pagination cycle"):
        client._paginate_list("/enrollments", "Items")

    assert client.get_json.call_count == 2


def test_paginated_request_failures_are_url_free() -> None:
    client = LighthouseClient()
    url = "https://example.test/page?token=PAGINATION_URL_SENTINEL"
    client.get_json = MagicMock(
        side_effect=requests.ConnectionError(f"request failed for {url}")
    )

    with pytest.raises(NetworkError) as exc_info:
        client._paginate_list("/enrollments", "Items")

    assert "PAGINATION_URL_SENTINEL" not in str(exc_info.value)
    assert "https://example.test" not in str(exc_info.value)


def test_get_enrolled_courses_joins_paginated_course_offerings() -> None:
    """The normalized catalog spans pages and excludes non-course enrollments."""
    client = LighthouseClient()
    client.get_json = MagicMock(
        side_effect=[
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
        ]
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


@pytest.mark.parametrize("retry_after", ["not-a-number", "nan", "inf", "-1"])
def test_invalid_retry_after_uses_exponential_fallback(retry_after: str) -> None:
    client, session = _client_with_session(
        [
            FakeResponse(429, headers={"Retry-After": retry_after}),
            FakeResponse(200),
        ]
    )

    with patch.object(api.time, "sleep") as sleep:
        client._do_request("GET", "https://example.test/resource", False, 30)

    sleep.assert_called_once_with(2)
    assert len(session.calls) == 2


def test_valid_retry_after_is_not_multiplied_by_attempt_exponent() -> None:
    client, _session = _client_with_session(
        [
            FakeResponse(429, headers={"Retry-After": "4"}),
            FakeResponse(429, headers={"Retry-After": "4"}),
            FakeResponse(200),
        ]
    )

    with patch.object(api.time, "sleep") as sleep:
        client._do_request("GET", "https://example.test/resource", False, 30)

    assert [call.args[0] for call in sleep.call_args_list] == [4.0, 4.0]


def test_retry_after_server_delay_is_capped() -> None:
    client, _session = _client_with_session(
        [
            FakeResponse(429, headers={"Retry-After": "999999"}),
            FakeResponse(200),
        ]
    )

    with patch.object(api.time, "sleep") as sleep:
        client._do_request("GET", "https://example.test/resource", False, 30)

    sleep.assert_called_once_with(client._MAX_RETRY_AFTER)


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
    client, session = _client_with_session([FakeResponse(401)])
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }

    with patch.object(api, "refresh_auth_from_browser") as refresh, \
            patch.object(api, "save_cookies") as save:
        with pytest.raises(api.SessionExpiredError):
            client._request("POST", "https://example.test/resource", data=b"payload")

    assert len(session.calls) == 1
    refresh.assert_not_called()
    save.assert_not_called()


def test_post_rate_limit_is_not_auto_refreshed_or_replayed() -> None:
    client, session = _client_with_session([FakeResponse(429)])
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }

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
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }
    session = MagicMock()
    client._session = session
    return client, session


@pytest.mark.parametrize("invalid_id", ["../../evil", True, 1.5, 0, -1])
def test_dropbox_folder_detail_rejects_invalid_course_ids_before_request(
    invalid_id: object,
) -> None:
    client, session = _authenticated_client_with_session()

    with pytest.raises(ValueError, match="org_unit_id must be a positive integer"):
        client.get_dropbox_folder_detail(invalid_id, 1)

    session.request.assert_not_called()


@pytest.mark.parametrize("invalid_id", ["../../evil", True, 1.5, 0, -1])
def test_dropbox_folder_detail_rejects_invalid_folder_ids_before_request(
    invalid_id: object,
) -> None:
    client, session = _authenticated_client_with_session()

    with pytest.raises(ValueError, match="folder_id must be a positive integer"):
        client.get_dropbox_folder_detail(1, invalid_id)

    session.request.assert_not_called()


@pytest.mark.parametrize("invalid_id", ["../../evil", True, 1.5, 0, -1])
def test_download_attachment_rejects_invalid_ids_before_request(
    invalid_id: object,
) -> None:
    client, session = _authenticated_client_with_session()

    for args, field_name in (
        ((invalid_id, 1, 1), "org_unit_id"),
        ((1, invalid_id, 1), "folder_id"),
        ((1, 1, invalid_id), "file_id"),
    ):
        with pytest.raises(ValueError, match=f"{field_name} must be a positive integer"):
            client.download_attachment(*args)

    session.request.assert_not_called()


@pytest.mark.parametrize("invalid_id", ["../../evil", True, 1.5, 0, -1])
def test_submit_file_rejects_invalid_ids_before_request(invalid_id: object) -> None:
    client, session = _authenticated_client_with_session()

    for args, field_name in (
        ((invalid_id, 1), "org_unit_id"),
        ((1, invalid_id), "folder_id"),
    ):
        with pytest.raises(ValueError, match=f"{field_name} must be a positive integer"):
            client.submit_file(*args, file_bytes=b"payload", filename="test.pdf")

    session.request.assert_not_called()


@pytest.mark.parametrize("invalid_id", ["../../evil", True, 1.5, 0, -1])
def test_read_endpoints_reject_invalid_org_ids_before_request(
    invalid_id: object,
) -> None:
    client, session = _authenticated_client_with_session()
    calls = (
        (client.get_content_toc, (invalid_id,)),
        (client.get_announcements, (invalid_id,)),
        (client.get_grade_schema, (invalid_id,)),
        (client.get_my_grades, (invalid_id,)),
        (client.get_quizzes, (invalid_id,)),
        (client.get_calendar, (invalid_id,)),
        (client.get_dropbox_folders, (invalid_id,)),
        (client.get_quiz_detail, (invalid_id, 1)),
        (client.download_topic_file, (invalid_id, 1)),
        (client.get_topic_html, (invalid_id, 1)),
    )

    for call, args in calls:
        with pytest.raises(ValueError, match="org_unit_id must be a positive integer"):
            call(*args)

    session.request.assert_not_called()


@pytest.mark.parametrize("invalid_id", ["../../evil", True, 1.5, 0, -1])
def test_quiz_and_topic_endpoints_reject_invalid_resource_ids_before_request(
    invalid_id: object,
) -> None:
    client, session = _authenticated_client_with_session()
    calls = (
        (client.get_quiz_detail, (1, invalid_id), "quiz_id"),
        (client.download_topic_file, (1, invalid_id), "topic_id"),
        (client.get_topic_html, (1, invalid_id), "topic_id"),
    )

    for call, args, field_name in calls:
        with pytest.raises(ValueError, match=f"{field_name} must be a positive integer"):
            call(*args)

    session.request.assert_not_called()


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
    client = LighthouseClient()
    client.get_json = MagicMock(return_value=payload)

    content, filename = client.get_topic_html(1, 1)

    assert type(content) is bytes
    assert content == expected
    assert filename.endswith(".html")


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"Body": []},
        {"Body": {"Text": 123}},
        {"Body": {"Text": ["<p>bad</p>"]}},
        {"Body": {}, "Html": {"unexpected": "object"}},
        {"Body": {"foo": "bad"}, "Html": {"unexpected": "object"}},
    ],
)
def test_get_topic_html_rejects_malformed_shapes_with_fixed_error(
    payload: object,
) -> None:
    client = LighthouseClient()
    client.get_json = MagicMock(return_value=payload)

    with pytest.raises(ContentResponseShapeError) as exc_info:
        client.get_topic_html(1, 1)

    assert str(exc_info.value) == ContentResponseShapeError._MESSAGE


def test_get_topic_html_rejects_deep_rich_text_without_recursion() -> None:
    value: object = "<p>deep</p>"
    for _ in range(api._MAX_RICH_TEXT_DEPTH + 1):
        value = {"Text": value}
    client = LighthouseClient()
    client.get_json = MagicMock(return_value={"Body": value})

    with pytest.raises(ContentResponseShapeError) as exc_info:
        client.get_topic_html(1, 1)

    assert str(exc_info.value) == ContentResponseShapeError._MESSAGE


def test_get_topic_html_rejects_cyclic_rich_text_without_recursion() -> None:
    value: dict[str, object] = {}
    value["Text"] = value
    client = LighthouseClient()
    client.get_json = MagicMock(return_value={"Body": value})

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
        client.submit_file(
            1,
            1,
            b"payload",
            invalid_filename,
        )

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
        client.submit_file(
            1,
            1,
            b"payload",
            "test.pdf",
            content_type=invalid_content_type,
        )

    session.request.assert_not_called()


def test_submit_file_preserves_valid_explicit_content_type() -> None:
    client, session = _client_with_session([FakeResponse(200, {})])
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }

    client.submit_file(
        1,
        1,
        b"payload",
        "test.pdf",
        content_type="application/pdf",
    )

    assert len(session.calls) == 1
    assert b"Content-Type: application/pdf\r\n" in session.calls[0][2]["data"]


@pytest.mark.parametrize("response_body", [None, [], "unexpected response"])
def test_submit_file_rejects_non_object_success_response_without_retry(
    response_body: object,
) -> None:
    client, session = _client_with_session([FakeResponse(200, response_body)])
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }

    with pytest.raises(SubmissionOutcomeUnknownError) as exc_info:
        client.submit_file(1, 1, b"payload", "result.pdf")

    assert str(exc_info.value) == SubmissionOutcomeUnknownError._MESSAGE
    assert len(session.calls) == 1


def test_submit_file_rejects_invalid_json_success_response_without_echoing_body() -> None:
    class InvalidJsonResponse(FakeResponse):
        def json(self) -> object:
            raise ValueError("response body contains BODY_SENTINEL")

    client, session = _client_with_session([InvalidJsonResponse(200)])
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }

    with pytest.raises(SubmissionOutcomeUnknownError) as exc_info:
        client.submit_file(1, 1, b"payload", "result.pdf")

    assert "BODY_SENTINEL" not in str(exc_info.value)
    assert len(session.calls) == 1


def test_exhausted_get_network_errors_are_url_free_and_bounded() -> None:
    client = LighthouseClient()
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }
    session = MagicMock()
    url = "https://example.test/resource?session=NETWORK_URL_SENTINEL"
    session.request.side_effect = requests.ConnectionError(f"failed for {url}")
    client._session = session

    with patch.object(api.time, "sleep") as sleep:
        with pytest.raises(NetworkError) as exc_info:
            client._request("GET", url)

    assert "NETWORK_URL_SENTINEL" not in str(exc_info.value)
    assert "https://example.test" not in str(exc_info.value)
    assert session.request.call_count == client._MAX_RETRIES + 1
    assert [call.args[0] for call in sleep.call_args_list] == [2, 4, 8]


def test_exhausted_post_network_error_is_url_free_and_not_replayed() -> None:
    client = LighthouseClient()
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }
    session = MagicMock()
    url = "https://example.test/resource?session=POST_URL_SENTINEL"
    session.request.side_effect = requests.ConnectionError(f"failed for {url}")
    client._session = session

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
    client = LighthouseClient()
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }
    session = MagicMock()
    client._session = session

    with pytest.raises(NetworkError, match="Invalid API URL"):
        client.get(path)

    session.request.assert_not_called()


def test_get_content_toc_rejects_traversal_org_unit_before_request() -> None:
    client, session = _authenticated_client_with_session()

    with pytest.raises(ValueError, match="org_unit_id must be a positive integer"):
        client.get_content_toc("../evil")

    session.request.assert_not_called()


def test_get_normalizes_same_origin_absolute_https_url() -> None:
    client = LighthouseClient()
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }
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


def test_submit_file_rate_limit_sends_body_once_and_raises_safe_error() -> None:
    client, session = _client_with_session([FakeResponse(429)])
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }

    with pytest.raises(NetworkError, match="no retry"):
        client.submit_file(
            org_unit_id=44347,
            folder_id=789,
            file_bytes=b"payload",
            filename="test.pdf",
        )

    assert len(session.calls) == 1
    assert session.calls[0][0] == "POST"
    assert session.calls[0][2]["data"]


def test_submit_file_unauthorized_sends_body_once_without_refresh() -> None:
    client, session = _client_with_session([FakeResponse(401)])
    client._loaded = True
    client._cookies = {
        "d2lSameSiteCanaryA": "a",
        "d2lSameSiteCanaryB": "b",
        "d2lSecureSessionVal": "secure",
        "d2lSessionVal": "session",
    }

    with patch.object(api, "refresh_auth_from_browser") as refresh:
        with pytest.raises(api.SessionExpiredError):
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
        "not-a-url",
    ],
)
def test_paginated_next_rejects_untrusted_targets_without_echoing_url(
    next_url: str,
) -> None:
    client = LighthouseClient()
    client.get_json = MagicMock(
        return_value={"Items": [{"id": 1}], "Next": next_url}
    )

    with pytest.raises(NetworkError, match="Invalid pagination link") as exc_info:
        client._paginate_list("/enrollments", "Items")

    assert next_url not in str(exc_info.value)
    assert client.get_json.call_count == 1


def test_paginated_next_accepts_https_same_origin_and_relative_d2l_paths() -> None:
    client = LighthouseClient()
    client.get_json = MagicMock(
        side_effect=[
            {
                "Items": [{"id": 1}],
                "Next": f"{BASE_URL}/d2l/api/lp/1.47/enrollments?page=2",
            },
            {"Items": [{"id": 2}], "Next": None},
        ]
    )

    assert client._paginate_list("/enrollments", "Items") == [
        {"id": 1},
        {"id": 2},
    ]
    assert client.get_json.call_count == 2


def test_paginated_next_enforces_maximum_page_count() -> None:
    client = LighthouseClient()
    client._MAX_PAGINATION_PAGES = 2
    client.get_json = MagicMock(
        side_effect=[
            {"Items": [{"id": 1}], "Next": "/enrollments?page=2"},
            {"Items": [{"id": 2}], "Next": "/enrollments?page=3"},
        ]
    )

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

    def __enter__(self) -> "FakeUrlopenResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload

    def geturl(self) -> str | None:
        return self.final_url


def _fake_cdp_opener(response: FakeUrlopenResponse) -> MagicMock:
    opener = MagicMock()
    opener.open.return_value = response
    return opener


def test_cdp_rejects_non_loopback_websocket_before_connecting() -> None:
    response = FakeUrlopenResponse(
        json.dumps({"webSocketDebuggerUrl": "ws://attacker.example/devtools/browser/1"}).encode()
    )
    websocket_call = AsyncMock()
    opener = _fake_cdp_opener(response)

    with patch.object(urllib.request, "build_opener", return_value=opener), \
            patch.object(api, "_cdp_get_cookies_ws", websocket_call):
        with pytest.raises(NetworkError, match="non-loopback"):
            api._refresh_via_cdp_websocket(9222)

    websocket_call.assert_not_awaited()


def test_cdp_rejects_loopback_websocket_on_unexpected_port() -> None:
    response = FakeUrlopenResponse(
        json.dumps(
            {"webSocketDebuggerUrl": "wss://127.0.0.1:9223/devtools/browser/1"}
        ).encode()
    )
    websocket_call = AsyncMock()
    opener = _fake_cdp_opener(response)

    with patch.object(urllib.request, "build_opener", return_value=opener), \
            patch.object(api, "_cdp_get_cookies_ws", websocket_call):
        with pytest.raises(NetworkError, match="unexpected port"):
            api._refresh_via_cdp_websocket(9222)

    websocket_call.assert_not_awaited()


def test_cdp_accepts_loopback_wss_on_configured_port() -> None:
    response = FakeUrlopenResponse(
        json.dumps(
            {"webSocketDebuggerUrl": "wss://localhost:9222/devtools/browser/1"}
        ).encode(),
        status=200,
        final_url="http://127.0.0.1:9222/json/version",
    )
    websocket_call = AsyncMock(
        return_value={"d2lSessionVal": "session", "d2lSecureSessionVal": "secure"}
    )
    opener = _fake_cdp_opener(response)

    with patch.object(urllib.request, "build_opener", return_value=opener), \
            patch.object(api, "_cdp_get_cookies_ws", websocket_call):
        assert api._refresh_via_cdp_websocket(9222) == {
            "d2lSessionVal": "session",
            "d2lSecureSessionVal": "secure",
        }

    websocket_call.assert_awaited_once_with(
        "wss://localhost:9222/devtools/browser/1"
    )


def test_cdp_discovery_rejects_redirect_without_following_external_target() -> None:
    response = FakeUrlopenResponse(
        b"",
        status=302,
        final_url="https://attacker.example/cdp?token=REDIRECT_TOKEN_SENTINEL",
    )
    opener = _fake_cdp_opener(response)
    websocket_call = AsyncMock()

    with patch.object(urllib.request, "build_opener", return_value=opener), \
            patch.object(api, "_cdp_get_cookies_ws", websocket_call):
        with pytest.raises(NetworkError, match="redirect") as exc_info:
            api._refresh_via_cdp_websocket(9222)

    opener.open.assert_called_once_with(
        "http://127.0.0.1:9222/json/version", timeout=10
    )
    websocket_call.assert_not_awaited()
    assert "REDIRECT_TOKEN_SENTINEL" not in str(exc_info.value)
    assert "attacker.example" not in str(exc_info.value)


def test_cdp_discovery_rejects_external_final_url_without_websocket_connect() -> None:
    response = FakeUrlopenResponse(
        b"{}",
        status=200,
        final_url="http://attacker.example/json/version?token=FINAL_TOKEN_SENTINEL",
    )
    opener = _fake_cdp_opener(response)
    websocket_call = AsyncMock()

    with patch.object(urllib.request, "build_opener", return_value=opener), \
            patch.object(api, "_cdp_get_cookies_ws", websocket_call):
        with pytest.raises(NetworkError, match="invalid response") as exc_info:
            api._refresh_via_cdp_websocket(9222)

    websocket_call.assert_not_awaited()
    assert "FINAL_TOKEN_SENTINEL" not in str(exc_info.value)
    assert "attacker.example" not in str(exc_info.value)


def test_cdp_endpoint_failure_is_wrapped_without_url_details() -> None:
    url = "http://127.0.0.1:9222/json/version?token=CDP_URL_SENTINEL"
    opener = MagicMock()
    opener.open.side_effect = OSError(f"connection failed for {url}")

    with patch.object(
        urllib.request,
        "build_opener",
        return_value=opener,
    ):
        with pytest.raises(NetworkError) as exc_info:
            api._refresh_via_cdp_websocket(9222)

    assert "CDP_URL_SENTINEL" not in str(exc_info.value)
    assert "127.0.0.1" not in str(exc_info.value)


def test_cdp_websocket_failure_is_wrapped_without_url_details() -> None:
    response = FakeUrlopenResponse(
        json.dumps(
            {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/1"}
        ).encode()
    )
    websocket_call = AsyncMock(
        side_effect=RuntimeError(
            "websocket failed at ws://127.0.0.1:9222/?token=WS_URL_SENTINEL"
        )
    )
    opener = _fake_cdp_opener(response)

    with patch.object(urllib.request, "build_opener", return_value=opener), \
            patch.object(api, "_cdp_get_cookies_ws", websocket_call):
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
