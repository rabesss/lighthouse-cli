"""Configuration paths and cookie persistence for lighthouse-cli.

Secret-bearing artifacts (``cookies.json``, ``mfa_pending.json``) are sealed
by :class:`~lighthouse_cli.credential_store.CredentialStore`; the functions
here are thin wrappers.  Plaintext metadata is limited to an allowlist (e.g.
``extracted_at``, ``created_at``, ``mfa_method``).
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from lighthouse_cli.credential_store import (
    CredentialStore,
    CredentialStoreError,
    FORMAT_VERSION,
    is_sealed_document,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical D2L origin: the exact host session cookies are set on and filtered for.
COOKIE_SETTING_HOST = "lighthouse.manipal.edu"

# Domain variants accepted when extracting fresh D2L cookies from a login session jar.
COOKIE_EXTRACTION_DOMAINS = ("lighthouse.manipal.edu", ".manipal.edu", "manipal.edu")

BASE_URL = f"https://{COOKIE_SETTING_HOST}"
API_LE = f"{BASE_URL}/d2l/api/le/1.93"

# Cookie names we care about
COOKIE_NAMES = (
    "d2lSameSiteCanaryA", "d2lSameSiteCanaryB",
    "d2lSecureSessionVal", "d2lSessionVal",
)

# Paths (defaults; storage functions resolve LIGHTHOUSE_CONFIG_DIR per call)
CONFIG_DIR = Path(os.getenv("LIGHTHOUSE_CONFIG_DIR", "~/.config/lighthouse-cli")).expanduser()
DEFAULT_DOWNLOAD_DIR = Path("~/Downloads/lighthouse").expanduser()

# Cookie age warning threshold (days)
_COOKIE_AGE_WARNING_DAYS = 4

#: Plaintext metadata allowed beside the sealed payload in mfa_pending.json.
_PENDING_METADATA_KEYS = frozenset({"created_at", "mfa_method"})

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def ensure_config_dir() -> Path:
    """Create the config directory if it doesn't exist with 0700 permissions."""
    config_dir = Path(os.getenv("LIGHTHOUSE_CONFIG_DIR", str(CONFIG_DIR))).expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)
    return config_dir


def missing_cookie_names(cookies: dict[str, str]) -> list[str]:
    """Return required D2L cookie names that are absent or empty."""
    missing: list[str] = []
    for name in COOKIE_NAMES:
        value = cookies.get(name)
        if value is None or not str(value).strip():
            missing.append(name)
    return missing


def load_cookies() -> dict[str, str]:
    """Load cookies from disk. Returns empty dict if file is missing.

    Sealed v2 documents are decrypted with their recorded key source; an
    unseal failure on this non-auth read path warns on stderr and behaves as
    if no cookies were stored.  Legacy plaintext files are accepted once and
    re-saved sealed when a key source is available (auto-upgrade).
    """
    store = CredentialStore()
    path = store.cookie_file
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}

    if is_sealed_document(doc):
        try:
            _meta, secret = store.read_artifact(path)
        except CredentialStoreError as exc:
            print(
                f"Warning: stored cookies could not be unlocked ({exc}). "
                "Run: lighthouse auth login",
                file=sys.stderr,
            )
            return {}
        return _filter_cookie_names(secret.get("cookies", {}))

    # Legacy plaintext ({"cookies": ...} wrapper or flat dict).
    cookies = _cookies_from_legacy_doc(doc)
    _try_upgrade_plaintext_cookies(store, cookies, extracted_at=doc.get("extracted_at"))
    return cookies


def save_cookies(cookies: dict[str, str], *, extracted_at: str | None = None) -> None:
    """Persist cookies to disk atomically, sealed, with owner-only permissions.

    Wraps cookies with an ``extracted_at`` ISO-8601 timestamp (plaintext
    metadata beside the sealed payload).  ``extracted_at`` overrides the
    fresh timestamp — used by the legacy auto-upgrade so migrated cookies
    keep their original age.
    """
    store = CredentialStore()
    store.write_artifact(
        store.cookie_file,
        metadata={
            "extracted_at": extracted_at or datetime.now(timezone.utc).isoformat()
        },
        secret={"cookies": dict(cookies)},
    )


def get_cookie_age_days() -> float | None:
    """Return the age of stored cookies in days, or None if unavailable."""
    store = CredentialStore()
    path = store.cookie_file
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    ts_str = data.get("extracted_at")
    if not ts_str:
        return None
    try:
        extracted = datetime.fromisoformat(str(ts_str))
        if extracted.tzinfo is None:
            extracted = extracted.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - extracted).total_seconds() / 86400
    except ValueError:
        return None


def _cookies_from_legacy_doc(doc: dict) -> dict[str, str]:
    """Extract cookies from a legacy plaintext document."""
    source = doc.get("cookies") if "cookies" in doc else doc
    if not isinstance(source, dict):
        return {}
    return _filter_cookie_names(source)


def _filter_cookie_names(source: dict) -> dict[str, str]:
    return {k: v for k, v in source.items() if k in COOKIE_NAMES}


def _try_upgrade_plaintext_cookies(
    store: CredentialStore,
    cookies: dict[str, str],
    *,
    extracted_at: str | None = None,
) -> None:
    """Re-save legacy plaintext cookies sealed — only when a key source exists.

    The original ``extracted_at`` timestamp rides along so the upgraded
    document keeps the legacy cookies' age (staleness warnings stay honest).
    """
    try:
        store.preflight()
    except CredentialStoreError:
        return
    try:
        save_cookies(cookies, extracted_at=extracted_at)
    except (CredentialStoreError, OSError):
        return


# ---------------------------------------------------------------------------
# MFA pending checkpoint (sealed via CredentialStore)
# ---------------------------------------------------------------------------

def save_mfa_pending(payload: dict) -> None:
    """Persist in-progress MFA state between ``auth login`` and ``auth verify``.

    Everything except the metadata allowlist (``created_at``, ``mfa_method``)
    is sealed — cookies, flow tokens, contexts, SAML material, and all URLs.
    """
    store = CredentialStore()
    metadata = {k: payload[k] for k in _PENDING_METADATA_KEYS if k in payload}
    secret = {k: v for k, v in payload.items() if k not in _PENDING_METADATA_KEYS}
    store.write_artifact(store.mfa_pending_file, metadata=metadata, secret=secret)


def load_mfa_pending() -> dict | None:
    """Load pending MFA state (metadata + sealed secret merged), or None.

    Compatibility policy:
    - sealed v2 document → decrypted with its recorded key source; unseal
      failures raise (auth callers report a clean actionable error);
    - legacy plaintext v1 → discarded with a stderr warning (it stored
      secrets unencrypted);
    - unknown version → treated as absent, cleared, warned.
    """
    store = CredentialStore()
    path = store.mfa_pending_file
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict):
        return None

    if is_sealed_document(doc):
        metadata, secret = store.read_artifact(path)
        return {**secret, **metadata, "version": FORMAT_VERSION}

    version = doc.get("version")
    _discard_pending(path, version)
    return None


def _discard_pending(path: Path, version: object) -> None:
    """Remove an unreadable pending checkpoint and warn on stderr."""
    with suppress(OSError):
        path.unlink()
    if version == 1:
        print(
            "Warning: discarded a legacy unencrypted MFA pending session. "
            "Run: lighthouse auth login --mfa-method sms",
            file=sys.stderr,
        )
    else:
        print(
            "Warning: removed an incompatible MFA pending session "
            f"(version {version!r}). Run: lighthouse auth login --mfa-method sms",
            file=sys.stderr,
        )


def update_mfa_pending(updates: dict) -> None:
    """Merge fields into the existing pending MFA file (no-op if missing)."""
    data = load_mfa_pending()
    if not data:
        return
    data.pop("version", None)
    data.update(updates)
    save_mfa_pending(data)


def clear_mfa_pending() -> None:
    """Remove pending MFA state file."""
    store = CredentialStore()
    with suppress(OSError):
        store.mfa_pending_file.unlink()


def warn_if_cookies_stale() -> bool:
    """Print a warning to stderr if cookies are older than the threshold.

    Returns True if a warning was printed.
    """
    age = get_cookie_age_days()
    if age is not None and age > _COOKIE_AGE_WARNING_DAYS:
        print(
            f"Warning: stored cookies are {age:.1f} days old. "
            "Consider running: lighthouse auth login",
            file=sys.stderr,
        )
        return True
    return False
