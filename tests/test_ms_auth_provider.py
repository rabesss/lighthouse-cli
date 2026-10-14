"""Stored-authenticator (``totp_provider``) mode, server errors and failure
categories in ``MicrosoftSSOClient``.

Reuses the scripted transport from the characterization suite.  Codes and
tokens are sentinels; none is printed.
"""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from lighthouse_cli import session_renewal as sr
from lighthouse_cli.config import COOKIE_NAMES, save_mfa_pending
from lighthouse_cli.ms_auth import MicrosoftSSOClient, MicrosoftSSOError, build_sso_error
from tests.test_ms_auth_characterization import (
    BEGIN_URL,
    CREDS_POST_URL,
    END_URL,
    LEGACY_MFA_HTML,
    LOGIN_INIT_URL,
    MFA_PAGE_URL,
    MS_BASE,
    PASSWORD,
    PROCESS_URL,
    USERNAME,
    FakeResponse,
    ScriptedSession,
    begin_success,
    config_html,
    end_auth_payloads,
    end_success,
    home,
    install_fake_playwright,
    mfa_start,
    saml_at,
    set_d2l_cookies,
    sso_start,
)

PROVIDER_CODE = "135790"
_CHECKPOINT_FUNCS = ("load_mfa_pending", "save_mfa_pending", "update_mfa_pending", "clear_mfa_pending")


@pytest.fixture
def session() -> ScriptedSession:
    return ScriptedSession()


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "config"
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(d))
    return d


@pytest.fixture
def no_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if provider mode touches the pending MFA checkpoint."""
    def forbidden(*_a: Any, **_k: Any) -> None:
        raise AssertionError("provider mode touched mfa_pending")

    for name in _CHECKPOINT_FUNCS:
        monkeypatch.setattr(f"lighthouse_cli.config.{name}", forbidden)


class Provider:
    """Records each call and the transport calls made before it."""

    def __init__(self, session: ScriptedSession, code: str = PROVIDER_CODE) -> None:
        self._session = session
        self._code = code
        self.calls_seen: list[list[tuple[str, str]]] = []

    def __call__(self) -> str:
        self.calls_seen.append(list(self._session.calls))
        return self._code


def run(session: ScriptedSession, **kwargs: Any) -> dict[str, str]:
    client = MicrosoftSSOClient()
    kwargs.setdefault("mfa_method", "app")
    with patch("requests.Session", return_value=session):
        try:
            return client.login(USERNAME, PASSWORD, kwargs.pop("totp_code", None), **kwargs)
        finally:
            client.close()


def category_of(exc: pytest.ExceptionInfo[MicrosoftSSOError]) -> str | None:
    return exc.value.category


# ---------------------------------------------------------------------------
# Provider mode
# ---------------------------------------------------------------------------


class TestProviderMode:
    def test_success_calls_provider_once_after_beginauth(
        self, session: ScriptedSession, config_dir: Path, no_checkpoint: None
    ) -> None:
        provider = Provider(session)
        session.enqueue(*mfa_start("PhoneAppOTP"), begin_success(), end_success(), saml_at(PROCESS_URL), home())
        set_d2l_cookies(session)
        assert set(run(session, totp_provider=provider)) == set(COOKIE_NAMES)
        assert len(provider.calls_seen) == 1
        assert provider.calls_seen[0][-1] == ("POST", BEGIN_URL)
        payloads = end_auth_payloads(session)
        assert len(payloads) == 1
        assert payloads[0]["AuthMethodId"] == "PhoneAppOTP"
        assert payloads[0]["AdditionalAuthData"] == PROVIDER_CODE

    def test_existing_human_checkpoint_survives(
        self, session: ScriptedSession, config_dir: Path
    ) -> None:
        save_mfa_pending({"mfa_method": "sms", "mfa_page_url": MFA_PAGE_URL, "marker": "human"})
        checkpoint = config_dir / "mfa_pending.json"
        before = checkpoint.read_bytes()
        session.enqueue(*mfa_start("PhoneAppOTP"), begin_success(), end_success(), saml_at(PROCESS_URL), home())
        set_d2l_cookies(session)
        run(session, totp_provider=Provider(session))
        assert checkpoint.read_bytes() == before

    @pytest.mark.parametrize(
        "end_response",
        [
            pytest.param({"Retry": True}, id="retry"),
            pytest.param({"Success": False, "ResultValue": "OathCodeIncorrect"}, id="incorrect"),
            pytest.param(
                {"Success": False, "ResultValue": "AuthenticationPreviouslyCompleted"},
                id="previously-completed",
            ),
            pytest.param({"Success": False, "ErrCode": 500121}, id="errcode"),
        ],
    )
    def test_any_endauth_failure_is_single_fixed_code_rejected(
        self,
        session: ScriptedSession,
        config_dir: Path,
        no_checkpoint: None,
        end_response: dict[str, Any],
    ) -> None:
        provider = Provider(session)
        session.enqueue(*mfa_start("PhoneAppOTP"), begin_success(), FakeResponse(json_data=end_response))
        with pytest.raises(MicrosoftSSOError) as caught:
            run(session, totp_provider=provider)
        assert caught.value.args[0] == "The authenticator code was rejected."
        assert category_of(caught) == "code_rejected"
        assert len(end_auth_payloads(session)) == 1
        assert len(provider.calls_seen) == 1
        assert ("POST", PROCESS_URL) not in session.calls

    @pytest.mark.parametrize(
        ("field", "upstream_value"),
        [
            pytest.param("ErrCode", f"OTP-{PROVIDER_CODE}", id="errcode"),
            pytest.param("Message", f"Code {PROVIDER_CODE} was rejected", id="message"),
            pytest.param("ResultValue", f"Invalid code {PROVIDER_CODE}", id="result-value"),
        ],
    )
    def test_provider_rejection_never_echoes_endauth_details(
        self,
        session: ScriptedSession,
        config_dir: Path,
        no_checkpoint: None,
        field: str,
        upstream_value: str,
    ) -> None:
        session.enqueue(
            *mfa_start("PhoneAppOTP"), begin_success(),
            FakeResponse(json_data={"Success": False, field: upstream_value}),
        )
        with pytest.raises(MicrosoftSSOError) as caught:
            run(session, totp_provider=Provider(session))

        rendered = str(caught.value)
        formatted = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
        assert caught.value.category == "code_rejected"
        assert PROVIDER_CODE not in rendered
        assert PROVIDER_CODE not in formatted

    @pytest.mark.parametrize(
        ("kwargs", "mfa_method"),
        [
            pytest.param({}, "auto", id="auto"),
            pytest.param({}, "sms", id="sms"),
            pytest.param({"totp_code": "123456"}, "app", id="literal-code"),
            pytest.param({"read_totp_after_challenge": True}, "app", id="stdin-code"),
            pytest.param({"defer_mfa_to_pending": True}, "app", id="deferred"),
        ],
    )
    def test_guards_reject_before_any_io(
        self,
        session: ScriptedSession,
        config_dir: Path,
        no_checkpoint: None,
        kwargs: dict[str, Any],
        mfa_method: str,
    ) -> None:
        provider = Provider(session)
        with pytest.raises(MicrosoftSSOError, match="stored authenticator"):
            run(session, mfa_method=mfa_method, totp_provider=provider, **kwargs)
        assert session.calls == []
        assert provider.calls_seen == []

    @pytest.mark.parametrize("auth_method_id", ["OneWaySMS", "PhoneAppNotification", "TwoWayVoiceMobile"])
    def test_non_otp_account_is_method_unavailable_before_beginauth(
        self, session: ScriptedSession, config_dir: Path, no_checkpoint: None, auth_method_id: str
    ) -> None:
        provider = Provider(session)
        session.enqueue(*mfa_start(auth_method_id))
        with pytest.raises(MicrosoftSSOError) as caught:
            run(session, totp_provider=provider)
        assert category_of(caught) == "method_unavailable"
        assert ("POST", BEGIN_URL) not in session.calls
        assert provider.calls_seen == []

    def test_legacy_form_fails_closed(
        self, session: ScriptedSession, config_dir: Path, no_checkpoint: None
    ) -> None:
        provider = Provider(session)
        session.enqueue(*sso_start(), FakeResponse(200, html=LEGACY_MFA_HTML, url=MFA_PAGE_URL))
        with patch("sys.stdin") as stdin:
            with pytest.raises(MicrosoftSSOError) as caught:
                run(session, totp_provider=provider)
        stdin.readline.assert_not_called()
        assert category_of(caught) == "method_unavailable"
        assert ("POST", PROCESS_URL) not in session.calls
        assert provider.calls_seen == []

    def test_provider_error_propagates_without_endauth(
        self, session: ScriptedSession, config_dir: Path, no_checkpoint: None
    ) -> None:
        def skewed() -> str:
            raise MicrosoftSSOError("No fresh authenticator time step.", category="clock_skew")

        session.enqueue(*mfa_start("PhoneAppOTP"), begin_success())
        with pytest.raises(MicrosoftSSOError) as caught:
            run(session, totp_provider=skewed)
        assert category_of(caught) == "clock_skew"
        assert end_auth_payloads(session) == []

    def test_checkpointing_is_restored_after_provider_login(
        self, session: ScriptedSession, config_dir: Path
    ) -> None:
        session.enqueue(*mfa_start("PhoneAppOTP"), begin_success(), FakeResponse(json_data={"Retry": True}))
        client = MicrosoftSSOClient()
        with patch("requests.Session", return_value=session), pytest.raises(MicrosoftSSOError):
            client.login(USERNAME, PASSWORD, mfa_method="app", totp_provider=Provider(session))
        assert client._checkpoint_enabled is True

    def test_provider_mode_is_silent_on_stdout(
        self, session: ScriptedSession, config_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        session.enqueue(*mfa_start("PhoneAppOTP"), begin_success(), end_success(), saml_at(PROCESS_URL), home())
        set_d2l_cookies(session)
        with patch("sys.stdin") as stdin:
            stdin.isatty.return_value = True
            run(session, totp_provider=Provider(session))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Second factor required" not in captured.err
        assert PROVIDER_CODE not in captured.err


# ---------------------------------------------------------------------------
# Human-mode EndAuth detail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("err_code", "expected"),
    [(987654, "unknown error"), (50074, "50074")],
)
def test_endauth_renders_only_known_error_codes(
    session: ScriptedSession, config_dir: Path, err_code: int, expected: str
) -> None:
    session.enqueue(*mfa_start("PhoneAppOTP"), begin_success(), FakeResponse(json_data={"ErrCode": err_code}))
    with pytest.raises(MicrosoftSSOError) as caught:
        run(session, totp_code="123456")
    assert str(caught.value).startswith(f"2FA verification failed: {expected}")
    if expected == "unknown error":
        assert str(err_code) not in str(caught.value)


# ---------------------------------------------------------------------------
# Server errors and the browser bootstrap
# ---------------------------------------------------------------------------


_SERVER_ERROR = "The sign-in service returned a server error (HTTP 503). Try again later."


def test_5xx_on_main_flow_is_network(session: ScriptedSession, config_dir: Path) -> None:
    session.enqueue(FakeResponse(503, url=LOGIN_INIT_URL))
    with pytest.raises(MicrosoftSSOError) as caught:
        run(session, mfa_method="auto")
    assert caught.value.args[0] == _SERVER_ERROR
    assert category_of(caught) == "network"


def test_5xx_on_microsoft_config_fetch_is_network(session: ScriptedSession, config_dir: Path) -> None:
    session.enqueue(
        FakeResponse(302, url=LOGIN_INIT_URL, headers={"Location": MS_BASE + "/tenant-id/saml2?SAMLRequest=z"}),
        FakeResponse(503, url=MS_BASE + "/tenant-id/saml2?SAMLRequest=z"),
    )
    with pytest.raises(MicrosoftSSOError) as caught:
        run(session, mfa_method="auto")
    assert caught.value.args[0] == _SERVER_ERROR
    assert category_of(caught) == "network"


def test_5xx_on_endauth_is_network_in_provider_mode(
    session: ScriptedSession, config_dir: Path, no_checkpoint: None
) -> None:
    session.enqueue(*mfa_start("PhoneAppOTP"), begin_success(), FakeResponse(503, url=END_URL))
    with pytest.raises(MicrosoftSSOError) as caught:
        run(session, totp_provider=Provider(session))
    assert category_of(caught) == "network"


def _http_bootstrap(*, me: int = 200, probe: int = 200, dsso: int = 200, gct: int = 200) -> list[FakeResponse]:
    return [
        FakeResponse(me, html="{}", url="https://login.live.com/Me.htm?v=3"),
        FakeResponse(probe, html="", url=f"{MS_BASE}/ssoprobe"),
        FakeResponse(dsso, json_data={}, url=f"{MS_BASE}/dssostatus"),
        FakeResponse(gct, json_data={"FlowToken": "FLOW-TOKEN-2"}, url=f"{MS_BASE}/common/GetCredentialType"),
        FakeResponse(probe, html="", url=f"{MS_BASE}/ssoprobe"),
        FakeResponse(dsso, json_data={}, url=f"{MS_BASE}/dssostatus"),
    ]


@pytest.fixture
def no_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)


def test_optional_probe_5xx_does_not_abort(
    session: ScriptedSession, config_dir: Path, no_playwright: None
) -> None:
    session.enqueue(
        *sso_start(config=config_html(url_get_credential_type=True)),
        *_http_bootstrap(me=503, probe=502, dsso=500),
        saml_at(CREDS_POST_URL),
        home(),
    )
    set_d2l_cookies(session)
    assert set(run(session, mfa_method="auto")) == set(COOKIE_NAMES)


def test_get_credential_type_5xx_is_network(
    session: ScriptedSession, config_dir: Path, no_playwright: None
) -> None:
    session.enqueue(
        *sso_start(config=config_html(url_get_credential_type=True)),
        *_http_bootstrap(gct=503)[:4],
    )
    with pytest.raises(MicrosoftSSOError) as caught:
        run(session, mfa_method="auto")
    assert caught.value.args[0] == _SERVER_ERROR
    assert category_of(caught) == "network"


def test_200_error_page_with_50059_is_network(session: ScriptedSession, config_dir: Path) -> None:
    error_page = (
        "<html><body><script>$Config = {\n"
        '"pgid": "ConvergedError", "serverError": "50059", '
        '"sErrTxt": "Service unavailable."\n'
        "};</script></body></html>"
    )
    session.enqueue(*sso_start(error_page))
    with pytest.raises(MicrosoftSSOError) as caught:
        run(session, mfa_method="auto")
    assert category_of(caught) == "network"


class _PwError(Exception):
    pass


class _PwTimeoutError(_PwError):
    pass


def _fake_sync_playwright(
    goto: Callable[..., Any], *, evaluate: Callable[..., Any] | None = None
) -> Callable[[], Any]:
    page = SimpleNamespace(goto=goto)
    if evaluate is not None:
        page.query_selector = lambda *_a, **_k: None
        page.evaluate = evaluate
    context = SimpleNamespace(add_cookies=lambda cookies: None, new_page=lambda: page)
    browser = SimpleNamespace(new_context=lambda **kw: context, close=lambda: None)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw: browser))

    class Manager:
        def __enter__(self) -> Any:
            return playwright

        def __exit__(self, *exc: Any) -> None:
            return None

    return Manager


@pytest.fixture
def fake_pw_errors(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Callable[..., Any]], None]]:
    def install(goto: Callable[..., Any]) -> None:
        install_fake_playwright(monkeypatch, _fake_sync_playwright(goto))
        api = sys.modules["playwright.sync_api"]
        api.Error = _PwError  # type: ignore[attr-defined]
        api.TimeoutError = _PwTimeoutError  # type: ignore[attr-defined]

    yield install


def _raiser(exc: BaseException) -> Callable[..., Any]:
    def goto(*_a: Any, **_k: Any) -> Any:
        raise exc

    return goto


@pytest.mark.parametrize(
    "goto",
    [
        pytest.param(_raiser(_PwTimeoutError("Timeout 60000ms exceeded.")), id="timeout"),
        pytest.param(_raiser(_PwError("net::ERR_NAME_NOT_RESOLVED at https://x")), id="net-err"),
    ],
)
def test_browser_navigation_outage_is_network(
    session: ScriptedSession,
    config_dir: Path,
    fake_pw_errors: Callable[[Callable[..., Any]], None],
    goto: Callable[..., Any],
) -> None:
    fake_pw_errors(goto)
    session.enqueue(*sso_start(config=config_html(url_get_credential_type=True)))
    with pytest.raises(MicrosoftSSOError) as caught:
        run(session, mfa_method="auto")
    assert category_of(caught) == "network"
    assert "net::ERR_" not in str(caught.value)
    assert "Timeout" not in str(caught.value)


def test_browser_navigation_5xx_is_network(
    session: ScriptedSession, config_dir: Path, fake_pw_errors: Callable[[Callable[..., Any]], None]
) -> None:
    fake_pw_errors(lambda *_a, **_k: SimpleNamespace(status=503))
    session.enqueue(*sso_start(config=config_html(url_get_credential_type=True)))
    with pytest.raises(MicrosoftSSOError) as caught:
        run(session, mfa_method="auto")
    assert caught.value.args[0] == _SERVER_ERROR
    assert category_of(caught) == "network"


def test_browser_missing_config_maps_to_interrupt(
    session: ScriptedSession, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_config(*_args: Any, **_kwargs: Any) -> Any:
        raise _PwError("$Config is not defined")

    install_fake_playwright(
        monkeypatch,
        _fake_sync_playwright(
            lambda *_args, **_kwargs: SimpleNamespace(status=200),
            evaluate=missing_config,
        ),
    )
    api = sys.modules["playwright.sync_api"]
    api.Error = _PwError  # type: ignore[attr-defined]
    api.TimeoutError = _PwTimeoutError  # type: ignore[attr-defined]
    session.enqueue(*sso_start(config=config_html(url_get_credential_type=True)))

    with pytest.raises(MicrosoftSSOError) as caught:
        run(session, mfa_method="auto")

    assert "Playwright username step failed" in str(caught.value)
    assert caught.value.category is None
    assert sr._as_renewal_error(caught.value).category == "interrupt"
    assert session.calls == [("GET", LOGIN_INIT_URL), ("GET", MS_BASE + "/tenant-id/saml2?SAMLRequest=z")]


def test_other_browser_failure_stays_uncategorized(
    session: ScriptedSession, config_dir: Path, fake_pw_errors: Callable[[Callable[..., Any]], None]
) -> None:
    fake_pw_errors(_raiser(_PwError("Target page crashed")))
    session.enqueue(*sso_start(config=config_html(url_get_credential_type=True)))
    with pytest.raises(MicrosoftSSOError, match="Playwright username step failed") as caught:
        run(session, mfa_method="auto")
    assert category_of(caught) is None


# ---------------------------------------------------------------------------
# Categories from Microsoft error codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "category"),
    [
        (50126, "credentials_rejected"),
        (50053, "credentials_rejected"),
        (50055, "credentials_rejected"),
        (50057, "credentials_rejected"),
        (50059, "network"),
        (50076, "interrupt"),
        (53003, "interrupt"),
        (99999, None),
        (None, None),
    ],
)
def test_build_sso_error_category(code: int | None, category: str | None) -> None:
    assert build_sso_error(code, "msg", "POST credentials").category == category


def test_unknown_category_is_dropped() -> None:
    assert MicrosoftSSOError("x", category="not-a-category").category is None
    assert "category" not in str(MicrosoftSSOError("x", category="network"))
