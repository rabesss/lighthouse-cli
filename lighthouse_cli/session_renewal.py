"""Unattended D2L sign-in with the stored authenticator secret.

When the D2L session expires, :func:`renew_session` signs in again through
Microsoft SSO using the stored password and a generated authenticator
(PhoneAppOTP) code, saves the new cookies and checks them.  Everything runs
under one sign-in lock shared by threads and processes, so concurrent
callers reuse a single renewal instead of each signing in.  Without ``fcntl``
(not POSIX) the lock covers only threads, so stored-authenticator sign-in is
off there.

A circuit breaker in ``relogin_state.json`` (plaintext, never secret) stops
automatic attempts after an authentication-class failure until a person signs
in, and spaces out attempts after a network failure.

Nothing here prints.  Errors carry fixed text and a category from
``RELOGIN_CATEGORIES``; they never contain a password, code or cookie.
"""

from __future__ import annotations

import hmac
import json
import os
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import requests

try:
    import fcntl
except ImportError:  # not POSIX: other processes cannot be locked out
    _HAS_FLOCK = False
else:
    _HAS_FLOCK = True

from .config import COOKIE_NAMES, load_cookies, missing_cookie_names, save_cookies
from .credential_store import (
    CredentialStore,
    CredentialStoreError,
    StoredTotp,
    _validate_credential_path,
)
from .ms_auth import MicrosoftSSOClient
from .ms_errors import MFA_METHOD_APP, RELOGIN_CATEGORIES, MicrosoftSSOError
from .totp import TotpParams, time_step, totp_code
from .utils import _loads_strict_json, atomic_write

SessionProbe = Literal["valid", "invalid", "unavailable"]

LOCK_TIMEOUT_SECONDS = 120.0
NETWORK_COOLDOWN_SECONDS = 300.0
_LOCK_POLL_SECONDS = 0.25
_STEP_MARGIN_SECONDS = 0.05
_LOCK_FILE = "relogin.lock"
_STATE_FILE = "relogin_state.json"
_STATE_VERSION = 1
_STATE_KEYS = frozenset({"v", "blocked", "category", "since", "last_attempt"})

#: Failures that switch automatic sign-in off until a person signs in.
BLOCKING_CATEGORIES = frozenset({
    "credentials_rejected", "code_rejected", "method_unavailable",
    "clock_skew", "interrupt", "unknown",
})
#: Local conditions: neither blocked nor recorded.
_UNRECORDED_CATEGORIES = frozenset({"store", "busy", "pending_human_login"})

_MESSAGES = {
    "credentials_rejected": "Microsoft rejected the stored username or password.",
    "code_rejected": "The authenticator code was rejected.",
    "method_unavailable": "The account offers no usable authenticator-app code method.",
    "clock_skew": "No fresh authenticator time step is available. Check the system clock.",
    "interrupt": "Microsoft sign-in stopped at a page that needs a person.",
    "network": "The sign-in service could not be reached.",
    "store": "Local sign-in data could not be read or written.",
    "busy": "Another sign-in is in progress.",
    "pending_human_login": "A two-step sign-in is waiting for verification.",
    "unknown": "Automatic sign-in failed unexpectedly.",
}
# Setup cannot keep the used steps of an authenticator it cannot read.
_UNREADABLE_AUTHENTICATOR = (
    "The stored authenticator could not be read. Check the system clock, "
    "then run: lighthouse auth totp-remove and lighthouse auth totp-setup"
)

# Indirection so tests can drive the clock without patching ``time``.
_clock: Callable[[], float] = time.time
_sleep: Callable[[float], None] = time.sleep


class RenewalError(Exception):
    """A sign-in attempt failed or was refused; ``category`` says why.

    The message is fixed per category and never contains secrets.
    """

    def __init__(self, category: str) -> None:
        if category not in RELOGIN_CATEGORIES:
            category = "unknown"
        super().__init__(_MESSAGES[category])
        self.category = category


class RenewalUnavailableError(Exception):
    """Unattended sign-in is not set up for the credentials in use."""

    def __init__(self) -> None:
        super().__init__(
            "No stored authenticator matches the saved credentials. "
            "Run: lighthouse auth totp-setup"
        )


@dataclass(frozen=True)
class RenewalResult:
    """Fresh D2L cookies.  ``signed_in`` is False when another caller's
    renewal was reused instead of signing in again."""

    cookies: dict[str, str] = field(repr=False)
    signed_in: bool


@dataclass(frozen=True)
class _BreakerState:
    blocked: bool = False
    category: str | None = None
    since: str | None = None
    last_attempt: str | None = None


# Corrupt state fails closed.
_CORRUPT_STATE = _BreakerState(blocked=True, category="unknown")


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

_THREAD_LOCK = threading.Lock()
_owner = threading.local()


def _open_lock_file(store: CredentialStore) -> int:
    try:
        _validate_credential_path(store.config_dir)
        store.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = store.config_dir / _LOCK_FILE
        _validate_credential_path(lock_path)
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
    except (CredentialStoreError, OSError):
        raise RenewalError("store") from None
    try:
        regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
    except OSError:
        regular = False
    if not regular:
        os.close(descriptor)
        raise RenewalError("store")
    return descriptor


def _flock_until(descriptor: int, deadline: float) -> None:
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RenewalError("busy") from None
            _sleep(min(_LOCK_POLL_SECONDS, remaining))
        except OSError:
            raise RenewalError("store") from None


@contextmanager
def _process_lock(deadline: float) -> Iterator[None]:
    if not _HAS_FLOCK:
        # Stored-authenticator sign-in is off here, so only this process's
        # cookie writes need serializing.
        yield
        return
    descriptor = _open_lock_file(CredentialStore())
    try:
        _flock_until(descriptor, deadline)
        yield
    finally:
        os.close(descriptor)


@contextmanager
def renewal_lock(*, timeout: float | None = None) -> Iterator[None]:
    """Hold the sign-in lock: a process-wide thread lock, then an exclusive
    ``flock`` on ``<config_dir>/relogin.lock`` where ``fcntl`` exists.

    One monotonic deadline (default ``LOCK_TIMEOUT_SECONDS``) covers both.
    Raises ``RenewalError("busy")`` on timeout and ``RenewalError("store")``
    when the lock file is unusable.  The lock is not re-entrant; re-entry from
    the holding thread raises at once instead of waiting out the deadline.
    """
    if getattr(_owner, "held", False):
        raise RuntimeError("renewal_lock is not re-entrant.")
    if timeout is None:
        timeout = LOCK_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    if not _THREAD_LOCK.acquire(timeout=max(0.0, timeout)):
        raise RenewalError("busy")
    try:
        with _process_lock(deadline):
            _owner.held = True
            try:
                yield
            finally:
                _owner.held = False
    finally:
        _THREAD_LOCK.release()


# ---------------------------------------------------------------------------
# Circuit breaker state
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.fromtimestamp(_clock(), tz=timezone.utc)


def _parse_utc(value: object) -> datetime:
    """Parse an ISO-8601 UTC timestamp; raise ValueError for anything else."""
    if not isinstance(value, str):
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value)
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp")
    return parsed


def _state_from_document(doc: object) -> _BreakerState:
    if type(doc) is not dict or set(doc) != _STATE_KEYS:
        return _CORRUPT_STATE
    blocked, category = doc["blocked"], doc["category"]
    if type(doc["v"]) is not int or doc["v"] != _STATE_VERSION or type(blocked) is not bool:
        return _CORRUPT_STATE
    allowed = BLOCKING_CATEGORIES if blocked else {None, "network"}
    if (category is not None and type(category) is not str) or category not in allowed:
        return _CORRUPT_STATE
    # A recorded failure has both timestamps; a clear state has neither.
    if any((doc[key] is None) != (category is None) for key in ("since", "last_attempt")):
        return _CORRUPT_STATE
    try:
        for key in ("since", "last_attempt"):
            if doc[key] is not None:
                _parse_utc(doc[key])
    except ValueError:
        return _CORRUPT_STATE
    return _BreakerState(blocked, category, doc["since"], doc["last_attempt"])


def _state_path(store: CredentialStore) -> Path:
    path = store.config_dir / _STATE_FILE
    _validate_credential_path(path)
    return path


def _load_state(store: CredentialStore) -> _BreakerState:
    """Read the breaker.  Missing means clear; unreadable content is blocked."""
    try:
        raw = _state_path(store).read_bytes()
    except FileNotFoundError:
        return _BreakerState()
    except (CredentialStoreError, OSError):
        raise RenewalError("store") from None
    try:
        doc = _loads_strict_json(raw)
    except ValueError:
        return _CORRUPT_STATE
    return _state_from_document(doc)


def _write_state(store: CredentialStore, state: _BreakerState) -> None:
    doc = {
        "v": _STATE_VERSION,
        "blocked": state.blocked,
        "category": state.category,
        "since": state.since,
        "last_attempt": state.last_attempt,
    }
    try:
        atomic_write(_state_path(store), json.dumps(doc), mode=0o600)
    except (CredentialStoreError, OSError):
        raise RenewalError("store") from None


def _record_failure(store: CredentialStore, state: _BreakerState, category: str) -> None:
    """Block on an auth-class failure; start a cooldown on ``network``.

    A non-blocking failure never clears an existing block.
    """
    if category in _UNRECORDED_CATEGORIES:
        return
    blocked = category in BLOCKING_CATEGORIES
    if state.blocked and not blocked:
        return
    now = _utc_now().isoformat()
    since = state.since if state.category == category and state.since else now
    _write_state(store, _BreakerState(blocked, category, since, now))


def clear_breaker() -> None:
    """Remove ``relogin_state.json``.  Call while holding :func:`renewal_lock`."""
    store = CredentialStore()
    try:
        _state_path(store).unlink()
    except FileNotFoundError:
        pass
    except (CredentialStoreError, OSError):
        raise RenewalError("store") from None


def store_authenticator(username: str, params: TotpParams) -> None:
    """Bind ``params`` to ``username`` under the sign-in lock; clear the breaker.

    The current time step counts as used, so the code just typed to register
    the app is never sent again.  Re-storing the same key keeps a newer
    recorded step, even one a fast clock reserved or one used under another
    username, and an authenticator that cannot be read is never replaced:
    only ``totp-remove`` forgets used steps.
    """
    with renewal_lock():
        store = CredentialStore()
        last_step = time_step(_clock(), params.period)
        try:
            previous = store.load_totp()
        except CredentialStoreError:
            raise CredentialStoreError(_UNREADABLE_AUTHENTICATOR) from None
        if (
            previous is not None
            and previous.last_step is not None
            and hmac.compare_digest(previous.params.key, params.key)
        ):
            last_step = max(last_step, previous.last_step)
        store.save_totp(username, params, last_step=last_step)
        clear_breaker()


def remove_authenticator() -> bool:
    """Delete the stored authenticator and the breaker state under the lock."""
    with renewal_lock():
        removed = CredentialStore().delete_totp()
        clear_breaker()
    return removed


def unattended_supported() -> bool:
    """Whether sign-ins can be kept one at a time across processes (POSIX)."""
    return _HAS_FLOCK


def unattended_configured(config_dir: Path | None = None) -> bool:
    """Cheap check: supported here, and a stored authenticator and saved
    credentials both exist.

    No decryption and no network.
    """
    store = CredentialStore(config_dir=config_dir)
    try:
        return (
            unattended_supported()
            and store.authenticator_file.is_file()
            and store.credentials_file.is_file()
        )
    except OSError:
        return False


def unattended_status() -> dict[str, bool]:
    """``{"configured": ..., "blocked": ...}`` for ``auth status``.

    Unreadable breaker state reports blocked.
    """
    try:
        blocked = _load_state(CredentialStore()).blocked
    except RenewalError:
        blocked = True
    return {"configured": unattended_configured(), "blocked": blocked}


# ---------------------------------------------------------------------------
# Renewal
# ---------------------------------------------------------------------------


def _as_renewal_error(exc: BaseException) -> RenewalError:
    if isinstance(exc, RenewalError):
        return exc
    if isinstance(exc, MicrosoftSSOError):
        return RenewalError(exc.category or "interrupt")
    # RequestException subclasses OSError, so it must be checked first.
    if isinstance(exc, requests.RequestException):
        return RenewalError("network")
    if isinstance(exc, (CredentialStoreError, OSError)):
        return RenewalError("store")
    return RenewalError("unknown")


def _same_session(current: Mapping[str, str], observed: Mapping[str, str]) -> bool:
    return all(current.get(name) == observed.get(name) for name in COOKIE_NAMES)


def _reuse_existing(
    observed: Mapping[str, str], validate: Callable[[dict[str, str]], SessionProbe]
) -> dict[str, str] | None:
    """Cookies another caller saved since ``observed`` was read, if valid."""
    current = load_cookies(read_only=True)
    if missing_cookie_names(current) or _same_session(current, observed):
        return None
    probe = validate(dict(current))
    if probe == "valid":
        return current
    if probe == "unavailable":
        raise RenewalError("network")
    return None


def _check_gates(store: CredentialStore, state: _BreakerState) -> None:
    """Refuse an automatic attempt; nothing here is recorded."""
    if state.blocked:
        raise RenewalError(state.category or "unknown")
    if state.category == "network" and state.last_attempt:
        # A clock that went back keeps the cooldown until it catches up.
        elapsed = (_utc_now() - _parse_utc(state.last_attempt)).total_seconds()
        if elapsed < NETWORK_COOLDOWN_SECONDS:
            raise RenewalError("network")
    try:
        _validate_credential_path(store.mfa_pending_file)
        pending = store.mfa_pending_file.exists()
    except (CredentialStoreError, OSError):
        raise RenewalError("store") from None
    if pending:
        raise RenewalError("pending_human_login")


def _code_provider(store: CredentialStore, stored: StoredTotp) -> Callable[[], str]:
    """Build the one-shot provider ``ms_auth`` calls after BeginAuth.

    It reserves a time step newer than the last one used (waiting at most one
    period for the next step), persists the reservation, then returns the
    code for that step.
    """
    params = stored.params

    def provide() -> str:
        now = _clock()
        step = time_step(now, params.period)
        last = stored.last_step
        if last is not None and step <= last:
            wait = (last + 1) * params.period - now + _STEP_MARGIN_SECONDS
            if 0 < wait <= params.period + 1:
                _sleep(wait)
                now = _clock()
                step = time_step(now, params.period)
            if step <= last:
                raise MicrosoftSSOError(
                    "No fresh authenticator time step is available.",
                    step="MFA",
                    category="clock_skew",
                )
        store.reserve_totp_step(step)
        return totp_code(params.key, for_time=now, digits=params.digits, period=params.period)

    return provide


def _sign_in(store: CredentialStore, username: str, password: str, stored: StoredTotp) -> dict[str, str]:
    client = MicrosoftSSOClient()
    try:
        return client.login(
            username,
            password,
            mfa_method=MFA_METHOD_APP,
            totp_provider=_code_provider(store, stored),
        )
    finally:
        with suppress(Exception):
            client.close()


def _usable_pair(
    credentials: tuple[str, str] | None, stored: StoredTotp | None
) -> tuple[str, str, StoredTotp]:
    if credentials is None or stored is None:
        raise RenewalUnavailableError()
    username, password = credentials
    if not username or not password or username.casefold() != stored.username.casefold():
        raise RenewalUnavailableError()
    return username, password, stored


class _UnrecordedError(Exception):
    """Wraps a gate refusal so it bypasses failure recording."""

    def __init__(self, error: RenewalError) -> None:
        super().__init__(str(error))
        self.error = error


def _attempt(
    store: CredentialStore,
    *,
    observed: Mapping[str, str],
    validate: Callable[[dict[str, str]], SessionProbe],
    automatic: bool,
    credentials: tuple[str, str] | None,
    persist_credentials: bool,
    state: _BreakerState,
) -> RenewalResult:
    if automatic:
        reused = _reuse_existing(observed, validate)
        if reused is not None:
            return RenewalResult(reused, signed_in=False)
    try:
        if automatic:
            _check_gates(store, state)
        stored = store.load_totp()
        if credentials is None:
            credentials = store.load()
    except RenewalError as exc:
        raise _UnrecordedError(exc) from None
    username, password, stored_totp = _usable_pair(credentials, stored)
    if automatic:
        # Block until the outcome is known, so neither an unwritable breaker
        # nor a process killed mid-sign-in lets every read send the password.
        now = _utc_now().isoformat()
        _write_state(store, _BreakerState(True, "unknown", now, now))
    cookies = _sign_in(store, username, password, stored_totp)
    save_cookies(cookies)
    probe = validate(dict(cookies))
    if probe == "unavailable":
        raise RenewalError("network")
    if probe != "valid":
        raise RenewalError("interrupt")
    if persist_credentials:
        store.save(username, password)
    return RenewalResult(dict(cookies), signed_in=True)


def renew_session(
    *,
    observed_cookies: Mapping[str, str],
    validate: Callable[[dict[str, str]], SessionProbe],
    automatic: bool,
    credentials: tuple[str, str] | None = None,
    persist_credentials: bool = False,
) -> RenewalResult:
    """Sign in with the stored authenticator under the sign-in lock.

    Args:
        observed_cookies: The cookies the caller found expired.  Automatic
            renewal reuses newer valid cookies another caller saved.
        validate: Probes a cookie set against D2L.
        automatic: True for renewal on expiry (breaker, cooldown and pending
            two-step gates apply); False for an explicit ``auth login``.
        credentials: The username/password ``auth login`` resolved; ``None``
            loads the saved pair.
        persist_credentials: Save ``credentials`` after a validated sign-in.

    Raises:
        RenewalUnavailableError: No stored authenticator matches the credentials,
            or this system cannot lock out other processes; nothing was
            attempted.
        RenewalError: Fixed-text failure with a ``category``.
    """
    if not unattended_supported():
        raise RenewalUnavailableError()
    with renewal_lock():
        store = CredentialStore()
        state = _load_state(store)
        try:
            result = _attempt(
                store,
                observed=observed_cookies,
                validate=validate,
                automatic=automatic,
                credentials=credentials,
                persist_credentials=persist_credentials,
                state=state,
            )
        except RenewalUnavailableError:
            raise
        except _UnrecordedError as exc:
            raise exc.error from None
        except Exception as exc:  # every failure maps to a category
            error = _as_renewal_error(exc)
            if error.category not in _UNRECORDED_CATEGORIES:
                _record_failure(store, state, error.category)
            elif automatic:
                # Undo the in-flight block; if that fails, it stays blocked.
                with suppress(RenewalError):
                    _write_state(store, state)
            raise error from None
        if result.signed_in:
            clear_breaker()
        return result
