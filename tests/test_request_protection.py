"""Cookie-authenticated API writes require a same-session CSRF header."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.request_protection import csrf_from_homepage, form_protection_from_homepage


def test_extracts_only_script_bootstrap_not_user_content():
    assert csrf_from_homepage(b'''<div>localStorage.setItem('XSRF.Token','fake')</div>
    <script>localStorage.setItem('XSRF.Token', 'synthetic-csrf');</script>''') == "synthetic-csrf"


@pytest.mark.parametrize("body", [b"<script>no bootstrap</script>", b"<script>localStorage.setItem('XSRF.Token', 'line\nbreak')</script>", b"<script>localStorage.setItem('XSRF.Token','a');localStorage.setItem('XSRF.Token','b')</script>"])
def test_bad_bootstrap_fails_without_echoing_content(body):
    with pytest.raises(ValueError, match="Could not initialize request protection"):
        csrf_from_homepage(body)


def test_bootstrap_cached_for_same_client():
    client = LighthouseClient(site="trial")
    client.get_raw = Mock(return_value=(b"<script>localStorage.setItem('XSRF.Token','synthetic-csrf')</script>", {}))
    assert client.get_csrf_token() == "synthetic-csrf"
    assert client.get_csrf_token() == "synthetic-csrf"
    client.get_raw.assert_called_once_with("/d2l/home", max_bytes=2 * 1024 * 1024)


def test_missing_bootstrap_does_not_block_submission_body():
    client = LighthouseClient()
    response = Mock(status_code=200)
    response.json.return_value = {}
    client._request = Mock(return_value=response)
    client.submit_file(12, 34, b"file body", "test.txt")
    assert "X-Csrf-Token" not in client._request.call_args.kwargs["headers"]


def test_submission_carries_csrf_and_does_not_print_it():
    client = LighthouseClient()
    client._csrf_token = "synthetic-csrf"
    response = Mock(status_code=200)
    response.json.return_value = {}
    client._request = Mock(return_value=response)
    client.submit_file(12, 34, b"file body", "test.txt")
    assert client._request.call_args.kwargs["headers"]["X-Csrf-Token"] == "synthetic-csrf"
    assert client._request.call_count == 1


def test_declarative_form_bootstrap_is_parsed_without_executing_code():
    record = json.dumps({"_type": "func", "N": "D2L.LP.Web.Authentication.Xsrf.Init", "P": ["d2l_referrer", "SYNTHETIC_TOKEN", 1234567890]})
    body = ('<script>const graph={"1":'+json.dumps(record)+'};</script>').encode()
    protection = form_protection_from_homepage(body)
    assert protection.csrf_token == "SYNTHETIC_TOKEN"
    assert protection.hit_code_seed == "1234567890"
    assert "SYNTHETIC_TOKEN" not in repr(protection)
    assert len({protection.next_hit_code() for _ in range(100)}) == 100


def test_form_bootstrap_rejects_missing_initializer_without_echoing_body():
    with pytest.raises(ValueError, match="Could not initialize form protection"):
        form_protection_from_homepage(b"<script>throw 'SECRET_SENTINEL';</script>")
