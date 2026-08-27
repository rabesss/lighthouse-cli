"""Focused tests for LighthouseClient's low-level transport helpers."""

from __future__ import annotations

import json
import urllib.request
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

import lighthouse_cli.api as api
from lighthouse_cli.api import BASE_URL, LighthouseClient, NetworkError, _extract_filename


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


def test_post_retry_semantics_remain_unchanged() -> None:
    client, session = _client_with_session([FakeResponse(429), FakeResponse(201)])
    payload = b"payload"

    with patch.object(api.time, "sleep") as sleep:
        response = client._do_request(
            "POST",
            "https://example.test/resource",
            False,
            30,
            data=payload,
        )

    assert response.status_code == 201
    assert [call[0] for call in session.calls] == ["POST", "POST"]
    assert all(call[2]["data"] == payload for call in session.calls)
    sleep.assert_called_once_with(2)


def test_final_rate_limit_response_still_raises_http_error() -> None:
    client, session = _client_with_session([FakeResponse(429) for _ in range(4)])

    with patch.object(api.time, "sleep"):
        with pytest.raises(requests.HTTPError):
            client._do_request("GET", "https://example.test/resource", False, 30)

    assert len(session.calls) == 4


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

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeUrlopenResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_cdp_rejects_non_loopback_websocket_before_connecting() -> None:
    response = FakeUrlopenResponse(
        json.dumps({"webSocketDebuggerUrl": "ws://attacker.example/devtools/browser/1"}).encode()
    )
    websocket_call = AsyncMock()

    with patch.object(urllib.request, "urlopen", return_value=response), \
            patch.object(api, "_cdp_get_cookies_ws", websocket_call):
        with pytest.raises(NetworkError, match="non-loopback"):
            api._refresh_via_cdp_websocket(9222)

    websocket_call.assert_not_awaited()
