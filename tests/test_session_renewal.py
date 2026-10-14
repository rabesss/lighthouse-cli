"""Unattended sign-in: lock, circuit breaker, reuse, gates and step reservation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import requests

from lighthouse_cli import session_renewal as sr
from lighthouse_cli.config import load_cookies, save_cookies
from lighthouse_cli.credential_store import CredentialStore, CredentialStoreError
from lighthouse_cli.ms_errors import MicrosoftSSOError
from lighthouse_cli.session_renewal import (
    RenewalError,
    RenewalResult,
    RenewalUnavailableError,
    renew_session,
    renewal_lock,
    unattended_configured,
    unattended_status,
)
from lighthouse_cli.totp import parse_totp_secret, totp_code

USERNAME = "student@example.edu"
PASSWORD = "pw-sentinel-Q7"  # pragma: allowlist secret
SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # pragma: allowlist secret
T0 = 1_800_000_015.0  # 15 s into a 30 s step
STEP0 = int(T0 // 30)


def _cookies(tag: str) -> dict[str, str]:
    return {
        "d2lSameSiteCanaryA": f"canA-{tag}",
        "d2lSameSiteCanaryB": f"canB-{tag}",
        "d2lSecureSessionVal": f"secure-{tag}",
        "d2lSessionVal": f"session-{tag}",
    }


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeSSO:
    """Stands in for ``MicrosoftSSOClient``; records each login."""

    def __init__(self) -> None:
        self.logins: list[dict[str, Any]] = []
        self.codes: list[str] = []
        self.closed = 0
        self.result: Callable[[], dict[str, str]] = lambda: _cookies("fresh")
        self.delay = 0.0

    def factory(self) -> FakeSSO:
        return self

    def login(self, username: str, password: str, **kwargs: Any) -> dict[str, str]:
        self.logins.append({"username": username, "password": password, **kwargs})
        self.codes.append(kwargs["totp_provider"]())
        if self.delay:
            time.sleep(self.delay)
        return self.result()

    def close(self) -> None:
        self.closed += 1


class Validator:
    def __init__(self, *answers: sr.SessionProbe) -> None:
        self.answers = list(answers) or ["valid"]
        self.calls: list[dict[str, str]] = []

    def __call__(self, cookies: dict[str, str]) -> sr.SessionProbe:
        self.calls.append(cookies)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cfg"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(d))
    return d


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(sr, "_clock", c.time)
    monkeypatch.setattr(sr, "_sleep", c.sleep)
    return c


@pytest.fixture
def sso(monkeypatch: pytest.MonkeyPatch) -> FakeSSO:
    fake = FakeSSO()
    monkeypatch.setattr(sr, "MicrosoftSSOClient", fake.factory)
    return fake


@pytest.fixture
def configured(config_dir: Path, clock: Clock, sso: FakeSSO) -> CredentialStore:
    store = CredentialStore()
    store.save(USERNAME, PASSWORD)
    store.save_totp(USERNAME, parse_totp_secret(SECRET), last_step=None)
    return store


def _renew(validate: Callable[[dict[str, str]], sr.SessionProbe] | None = None, **kwargs: Any) -> RenewalResult:
    kwargs.setdefault("observed_cookies", {})
    kwargs.setdefault("automatic", True)
    return renew_session(validate=validate or Validator("valid"), **kwargs)


def _state(config_dir: Path) -> dict[str, Any] | None:
    path = config_dir / "relogin_state.json"
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _fail_with(sso: FakeSSO, exc: BaseException) -> None:
    def raise_it() -> dict[str, str]:
        raise exc

    sso.result = raise_it


# ---------------------------------------------------------------------------
# Success, availability and reuse
# ---------------------------------------------------------------------------


def test_success_signs_in_saves_cookies_and_reserves_the_step(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO
) -> None:
    validate = Validator("valid")
    result = _renew(validate)
    assert result.signed_in is True
    assert result.cookies == _cookies("fresh")
    assert load_cookies(read_only=True) == _cookies("fresh")
    assert validate.calls == [_cookies("fresh")]
    [call] = sso.logins
    assert (call["username"], call["password"], call["mfa_method"]) == (USERNAME, PASSWORD, "app")
    key = parse_totp_secret(SECRET).key
    assert sso.codes == [totp_code(key, for_time=T0)]
    stored = configured.load_totp()
    assert stored is not None and stored.last_step == STEP0
    assert sso.closed == 1
    assert _state(config_dir) is None
    assert (config_dir / "relogin.lock").stat().st_mode & 0o777 == 0o600


def test_success_clears_a_network_cooldown(configured: CredentialStore, config_dir: Path, clock: Clock) -> None:
    (config_dir / "relogin_state.json").write_text(json.dumps({
        "v": 1, "blocked": False, "category": "network",
        "since": "2027-01-01T00:00:00+00:00", "last_attempt": "2027-01-01T00:00:00+00:00",
    }))
    assert _renew().signed_in is True
    assert _state(config_dir) is None


def test_result_repr_hides_cookies(configured: CredentialStore) -> None:
    result = _renew()
    assert "secure-fresh" not in repr(result)
    assert repr(result) == "RenewalResult(signed_in=True)"


@pytest.mark.parametrize(("has_credentials", "has_totp"), [(False, True), (True, False), (False, False)])
def test_unavailable_without_stored_pair(
    config_dir: Path, clock: Clock, sso: FakeSSO, has_credentials: bool, has_totp: bool
) -> None:
    store = CredentialStore()
    if has_credentials:
        store.save(USERNAME, PASSWORD)
    if has_totp:
        store.save_totp(USERNAME, parse_totp_secret(SECRET), last_step=None)
    with pytest.raises(RenewalUnavailableError):
        _renew()
    assert sso.logins == []
    assert _state(config_dir) is None


def test_unavailable_when_usernames_differ(configured: CredentialStore, config_dir: Path, sso: FakeSSO) -> None:
    with pytest.raises(RenewalUnavailableError):
        _renew(automatic=False, credentials=("other@example.edu", PASSWORD))
    assert sso.logins == []
    assert _state(config_dir) is None


def test_username_match_is_case_insensitive(configured: CredentialStore, sso: FakeSSO) -> None:
    _renew(automatic=False, credentials=(USERNAME.upper(), PASSWORD))
    assert sso.logins[0]["username"] == USERNAME.upper()


def test_reuses_newer_valid_cookies_without_signing_in(configured: CredentialStore, sso: FakeSSO) -> None:
    save_cookies(_cookies("other-caller"))
    validate = Validator("valid")
    result = _renew(validate, observed_cookies=_cookies("stale"))
    assert result.signed_in is False
    assert result.cookies == _cookies("other-caller")
    assert sso.logins == []


def test_same_cookies_as_observed_are_not_reused(configured: CredentialStore, sso: FakeSSO) -> None:
    save_cookies(_cookies("stale"))
    validate = Validator("valid")
    result = _renew(validate, observed_cookies=_cookies("stale"))
    assert result.signed_in is True
    assert validate.calls == [_cookies("fresh")]


def test_invalid_newer_cookies_fall_through_to_sign_in(configured: CredentialStore, sso: FakeSSO) -> None:
    save_cookies(_cookies("other-caller"))
    result = _renew(Validator("invalid", "valid"), observed_cookies=_cookies("stale"))
    assert result.signed_in is True
    assert len(sso.logins) == 1


def test_unavailable_probe_on_reuse_records_network_cooldown(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO
) -> None:
    save_cookies(_cookies("other-caller"))
    with pytest.raises(RenewalError) as caught:
        _renew(Validator("unavailable"), observed_cookies=_cookies("stale"))
    assert caught.value.category == "network"
    assert sso.logins == []
    state = _state(config_dir)
    assert state is not None and (state["blocked"], state["category"]) == (False, "network")


def test_explicit_login_skips_reuse(configured: CredentialStore, sso: FakeSSO) -> None:
    save_cookies(_cookies("other-caller"))
    result = _renew(automatic=False, observed_cookies=_cookies("stale"))
    assert result.signed_in is True
    assert len(sso.logins) == 1


def test_explicit_login_uses_supplied_credentials_and_persists_them(
    configured: CredentialStore, sso: FakeSSO
) -> None:
    _renew(automatic=False, credentials=(USERNAME, "new-pw-sentinel"), persist_credentials=True)
    assert sso.logins[0]["password"] == "new-pw-sentinel"  # pragma: allowlist secret
    assert configured.load() == (USERNAME, "new-pw-sentinel")


def test_credentials_not_persisted_when_validation_fails(configured: CredentialStore) -> None:
    with pytest.raises(RenewalError):
        _renew(Validator("invalid"), automatic=False, credentials=(USERNAME, "new-pw-sentinel"),
               persist_credentials=True)
    assert configured.load() == (USERNAME, PASSWORD)


# ---------------------------------------------------------------------------
# Failure categories and the circuit breaker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "category", "blocked"),
    [
        (MicrosoftSSOError("x", category="credentials_rejected"), "credentials_rejected", True),
        (MicrosoftSSOError("x", category="code_rejected"), "code_rejected", True),
        (MicrosoftSSOError("x", category="method_unavailable"), "method_unavailable", True),
        (MicrosoftSSOError("x", category="clock_skew"), "clock_skew", True),
        (MicrosoftSSOError("x", category="network"), "network", False),
        (MicrosoftSSOError("needs a person"), "interrupt", True),
        (requests.ConnectionError("down"), "network", False),
        (RuntimeError("boom"), "unknown", True),
    ],
)
def test_failures_are_categorized_and_recorded(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO,
    exc: BaseException, category: str, blocked: bool,
) -> None:
    _fail_with(sso, exc)
    with pytest.raises(RenewalError) as caught:
        _renew()
    assert caught.value.category == category
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert str(caught.value) == sr._MESSAGES[category]
    state = _state(config_dir)
    assert state is not None
    assert (state["blocked"], state["category"]) == (blocked, category)
    assert state["since"] == state["last_attempt"] == "2027-01-15T08:00:15+00:00"
    assert sso.closed == 1


def test_store_failures_are_not_recorded(configured: CredentialStore, config_dir: Path, sso: FakeSSO) -> None:
    _fail_with(sso, CredentialStoreError("disk"))
    with pytest.raises(RenewalError) as caught:
        _renew()
    assert caught.value.category == "store"
    assert _state(config_dir) is None


@pytest.mark.parametrize(("probe", "category"), [("invalid", "interrupt"), ("unavailable", "network")])
def test_post_login_validation(configured: CredentialStore, config_dir: Path, probe: sr.SessionProbe,
                               category: str) -> None:
    with pytest.raises(RenewalError) as caught:
        _renew(Validator(probe))
    assert caught.value.category == category
    state = _state(config_dir)
    assert state is not None
    assert (state["blocked"], state["category"]) == (category != "network", category)


def test_validation_runs_inside_the_renewal_lock(configured: CredentialStore, sso: FakeSSO) -> None:
    lock_was_held: list[bool] = []

    def validate(_cookies: dict[str, str]) -> sr.SessionProbe:
        try:
            with renewal_lock(timeout=0):
                pass
        except RuntimeError:
            lock_was_held.append(True)
        else:
            lock_was_held.append(False)
        return "valid"

    result = _renew(validate)
    assert result.signed_in is True
    assert lock_was_held == [True]


def test_blocked_breaker_refuses_automatic_without_rewriting_state(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO, clock: Clock
) -> None:
    _fail_with(sso, MicrosoftSSOError("x", category="code_rejected"))
    with pytest.raises(RenewalError):
        _renew()
    before = (config_dir / "relogin_state.json").read_bytes()
    clock.now += 3600
    with pytest.raises(RenewalError) as caught:
        _renew()
    assert caught.value.category == "code_rejected"
    assert len(sso.logins) == 1
    assert (config_dir / "relogin_state.json").read_bytes() == before


def test_explicit_login_ignores_breaker_and_clears_it_on_success(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO, clock: Clock
) -> None:
    _fail_with(sso, MicrosoftSSOError("x", category="credentials_rejected"))
    with pytest.raises(RenewalError):
        _renew()
    sso.result = lambda: _cookies("fresh")
    clock.now += 60
    assert _renew(automatic=False).signed_in is True
    assert _state(config_dir) is None


def test_explicit_failure_while_blocked_keeps_the_block(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO, clock: Clock
) -> None:
    _fail_with(sso, MicrosoftSSOError("x", category="code_rejected"))
    with pytest.raises(RenewalError):
        _renew()
    clock.now += 60
    _fail_with(sso, requests.ConnectionError("down"))
    with pytest.raises(RenewalError):
        _renew(automatic=False)
    state = _state(config_dir)
    assert state is not None and (state["blocked"], state["category"]) == (True, "code_rejected")


def test_repeated_failure_keeps_since_and_moves_last_attempt(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO, clock: Clock
) -> None:
    _fail_with(sso, MicrosoftSSOError("x", category="code_rejected"))
    with pytest.raises(RenewalError):
        _renew()
    clock.now += 120
    with pytest.raises(RenewalError):
        _renew(automatic=False)
    state = _state(config_dir)
    assert state is not None
    assert state["since"] == "2027-01-15T08:00:15+00:00"
    assert state["last_attempt"] == "2027-01-15T08:02:15+00:00"


def test_network_cooldown(configured: CredentialStore, config_dir: Path, sso: FakeSSO, clock: Clock) -> None:
    _fail_with(sso, requests.ConnectionError("down"))
    with pytest.raises(RenewalError):
        _renew()
    clock.now += sr.NETWORK_COOLDOWN_SECONDS - 1
    with pytest.raises(RenewalError) as caught:
        _renew()
    assert caught.value.category == "network"
    assert len(sso.logins) == 1
    clock.now += 2
    sso.result = lambda: _cookies("fresh")
    assert _renew().signed_in is True
    assert len(sso.logins) == 2


def test_pending_two_step_login_blocks_automatic_only(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO
) -> None:
    (config_dir / "mfa_pending.json").write_text("{}")
    with pytest.raises(RenewalError) as caught:
        _renew()
    assert caught.value.category == "pending_human_login"
    assert sso.logins == []
    assert _state(config_dir) is None
    assert _renew(automatic=False).signed_in is True


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"v": 2, "blocked": false, "category": null, "since": null, "last_attempt": null}',
        '{"v": 1, "blocked": "no", "category": null, "since": null, "last_attempt": null}',
        '{"v": 1, "blocked": false, "category": "code_rejected", "since": null, "last_attempt": null}',
        '{"v": 1, "blocked": true, "category": null, "since": null, "last_attempt": null}',
        '{"v": 1, "blocked": true, "category": "store", "since": null, "last_attempt": null}',
        '{"v": 1, "blocked": false, "category": null, "since": "yesterday", "last_attempt": null}',
        '{"v": 1, "blocked": false, "category": null, "since": "2027-01-01T00:00:00", "last_attempt": null}',
        '{"v": 1, "blocked": false, "category": null, "since": "2027-01-01T00:00:00+05:30", "last_attempt": null}',
        '{"v": 1, "blocked": false, "category": null, "since": null}',
        '{"v": 1, "blocked": false, "category": null, "since": null, "last_attempt": null, "x": 1}',
        '{"v": true, "blocked": false, "category": null, "since": null, "last_attempt": null}',
    ],
)
def test_corrupt_state_fails_closed(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO, content: str
) -> None:
    (config_dir / "relogin_state.json").write_text(content)
    with pytest.raises(RenewalError) as caught:
        _renew()
    assert caught.value.category == "unknown"
    assert sso.logins == []
    assert unattended_status() == {"configured": True, "blocked": True}


def test_state_file_is_private_and_secret_free(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO
) -> None:
    _fail_with(sso, MicrosoftSSOError("x", category="code_rejected"))
    with pytest.raises(RenewalError) as caught:
        _renew()
    path = config_dir / "relogin_state.json"
    assert path.stat().st_mode & 0o777 == 0o600
    rendered = path.read_text() + str(caught.value) + repr(caught.value)
    for secret in (PASSWORD, SECRET, USERNAME, sso.codes[0], "secure-fresh"):
        assert secret not in rendered


def test_unknown_category_is_normalized() -> None:
    assert RenewalError("nonsense").category == "unknown"


def test_unattended_configured_and_status(config_dir: Path) -> None:
    assert unattended_configured() is False
    store = CredentialStore()
    store.save(USERNAME, PASSWORD)
    assert unattended_configured() is False
    store.save_totp(USERNAME, parse_totp_secret(SECRET), last_step=None)
    assert unattended_configured() is True
    assert unattended_configured(config_dir.parent / "elsewhere") is False
    assert unattended_status() == {"configured": True, "blocked": False}


# ---------------------------------------------------------------------------
# Code provider: one fresh step per sign-in
# ---------------------------------------------------------------------------


def test_provider_waits_for_the_next_step_after_a_used_one(
    configured: CredentialStore, sso: FakeSSO, clock: Clock
) -> None:
    configured.reserve_totp_step(STEP0)
    _renew(automatic=False)
    assert len(clock.sleeps) == 1 and 15 < clock.sleeps[0] <= 31
    stored = configured.load_totp()
    assert stored is not None and stored.last_step == STEP0 + 1
    key = parse_totp_secret(SECRET).key
    assert sso.codes == [totp_code(key, for_time=(STEP0 + 1) * 30)]


def test_provider_persists_reservation_before_generating_code(
    configured: CredentialStore, sso: FakeSSO, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_totp_code = sr.totp_code
    reserved_steps: list[int | None] = []

    def inspect_reservation(
        key: bytes, *, for_time: float, digits: int = 6, period: int = 30
    ) -> str:
        stored = configured.load_totp()
        reserved_steps.append(stored.last_step if stored is not None else None)
        return original_totp_code(key, for_time=for_time, digits=digits, period=period)

    monkeypatch.setattr(sr, "totp_code", inspect_reservation)
    _renew(automatic=False)
    assert reserved_steps == [STEP0]


def test_provider_reports_clock_skew_when_no_fresh_step(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO, clock: Clock
) -> None:
    configured.reserve_totp_step(STEP0 + 10)
    with pytest.raises(RenewalError) as caught:
        _renew(automatic=False)
    assert caught.value.category == "clock_skew"
    assert clock.sleeps == []
    stored = configured.load_totp()
    assert stored is not None and stored.last_step == STEP0 + 10
    state = _state(config_dir)
    assert state is not None and state["category"] == "clock_skew"


def test_provider_reports_clock_skew_after_clock_rolls_back_during_wait(
    configured: CredentialStore, sso: FakeSSO, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured.reserve_totp_step(STEP0)

    def rollback_during_sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now = T0 - 1

    monkeypatch.setattr(sr, "_sleep", rollback_during_sleep)
    with pytest.raises(RenewalError) as caught:
        _renew(automatic=False)

    assert caught.value.category == "clock_skew"
    assert len(clock.sleeps) == 1 and 15 < clock.sleeps[0] <= 31
    stored = configured.load_totp()
    assert stored is not None and stored.last_step == STEP0
    assert sso.closed == 1


def test_back_to_back_sign_ins_never_reuse_a_step(configured: CredentialStore, sso: FakeSSO) -> None:
    _renew(automatic=False)
    _renew(automatic=False)
    assert len(set(sso.codes)) == 2
    stored = configured.load_totp()
    assert stored is not None and stored.last_step == STEP0 + 1


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


def test_lock_is_not_reentrant(config_dir: Path) -> None:
    with renewal_lock(), pytest.raises(RuntimeError):
        with renewal_lock():
            pass


def test_lock_rejects_a_symlinked_lock_file(config_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("")
    (config_dir / "relogin.lock").symlink_to(target)
    with pytest.raises(RenewalError) as caught:
        with renewal_lock(timeout=0.2):
            pass
    assert caught.value.category == "store"


def test_threads_share_one_sign_in(configured: CredentialStore, sso: FakeSSO) -> None:
    sso.delay = 0.2
    results: list[RenewalResult] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(_renew(Validator("valid")))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    assert sorted(r.signed_in for r in results) == [False, False, True]
    assert len(sso.logins) == 1


def test_thread_waiter_times_out_as_busy(config_dir: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with renewal_lock():
            entered.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert entered.wait(5)
        with pytest.raises(RenewalError) as caught:
            with renewal_lock(timeout=0.2):
                pass
        assert caught.value.category == "busy"
    finally:
        release.set()
        t.join(5)


_HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def other_process_lock(config_dir: Path) -> Iterator[subprocess.Popen[str]]:
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", _HOLDER, str(config_dir / "relogin.lock")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "locked"
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)


def test_other_process_holding_the_lock_makes_renewal_busy(
    configured: CredentialStore, config_dir: Path, sso: FakeSSO,
    other_process_lock: subprocess.Popen[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = config_dir / "relogin_state.json"
    state_path.write_text(json.dumps({
        "v": 1, "blocked": False, "category": "network",
        "since": "2027-01-15T08:00:15+00:00", "last_attempt": "2027-01-15T08:00:15+00:00",
    }))
    before = state_path.read_bytes()
    monkeypatch.setattr(sr, "_sleep", time.sleep)
    monkeypatch.setattr(sr, "LOCK_TIMEOUT_SECONDS", 0.3)
    with pytest.raises(RenewalError) as caught:
        _renew()
    assert caught.value.category == "busy"
    assert sso.logins == []
    assert state_path.read_bytes() == before


def test_renewal_proceeds_once_the_other_process_releases(
    configured: CredentialStore, sso: FakeSSO,
    other_process_lock: subprocess.Popen[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sr, "_sleep", time.sleep)
    assert other_process_lock.stdin is not None
    timer = threading.Timer(0.3, other_process_lock.stdin.close)
    timer.start()
    try:
        assert _renew().signed_in is True
    finally:
        timer.cancel()


def test_lock_file_is_left_in_place_and_reusable(config_dir: Path) -> None:
    with renewal_lock():
        pass
    with renewal_lock():
        pass
    assert (config_dir / "relogin.lock").is_file()
    assert os.stat(config_dir / "relogin.lock").st_size == 0
