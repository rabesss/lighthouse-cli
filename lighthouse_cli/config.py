"""Configuration paths and cookie persistence for lighthouse-cli."""

from __future__ import annotations

import json
import os
from contextlib import suppress
import uuid
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://lighthouse.manipal.edu"
API_LE = f"{BASE_URL}/d2l/api/le/1.93"

# Cookie names we care about
COOKIE_NAMES = (
    "d2lSameSiteCanaryA", "d2lSameSiteCanaryB",
    "d2lSecureSessionVal", "d2lSessionVal",
)

# Paths
CONFIG_DIR = Path(os.getenv("LIGHTHOUSE_CONFIG_DIR", "~/.config/lighthouse-cli")).expanduser()
COOKIE_FILE = CONFIG_DIR / "cookies.json"
DEFAULT_DOWNLOAD_DIR = Path("~/Downloads/lighthouse").expanduser()

# Cookie age warning threshold (days)
_COOKIE_AGE_WARNING_DAYS = 4


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def ensure_config_dir() -> Path:
    """Create the config directory if it doesn't exist with 0700 permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        CONFIG_DIR.chmod(0o700)
    return CONFIG_DIR


def load_cookies() -> dict[str, str]:
    """Load cookies from disk. Returns empty dict if file is missing.

    Handles both the new format (``{"cookies": {...}, "extracted_at": "..."}``)
    and the legacy flat-dict format for backward compatibility.
    """
    if not COOKIE_FILE.exists():
        return {}
    try:
        data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        # New format: {"cookies": {...}, "extracted_at": "..."}
        if isinstance(data, dict) and "cookies" in data:
            return {k: v for k, v in data["cookies"].items() if k in COOKIE_NAMES}
        # Legacy format: flat dict
        return {k: v for k, v in data.items() if k in COOKIE_NAMES}
    except (json.JSONDecodeError, OSError):
        return {}


def save_cookies(cookies: dict[str, str]) -> None:
    """Persist cookies to disk atomically (temp file + rename).

    Wraps cookies with an ``extracted_at`` ISO-8601 timestamp.
    """
    ensure_config_dir()
    payload = {
        "cookies": cookies,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp_file = COOKIE_FILE.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with suppress(OSError):
            tmp_file.chmod(0o600)
        tmp_file.replace(COOKIE_FILE)
    except OSError:
        if tmp_file.exists():
            tmp_file.unlink()
        raise


def get_cookie_age_days() -> float | None:
    """Return the age of stored cookies in days, or None if unavailable."""
    if not COOKIE_FILE.exists():
        return None
    try:
        data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        ts_str = data.get("extracted_at") if isinstance(data, dict) else None
        if not ts_str:
            return None
        extracted = datetime.fromisoformat(ts_str)
        if extracted.tzinfo is None:
            extracted = extracted.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - extracted).total_seconds() / 86400
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def warn_if_cookies_stale() -> bool:
    """Print a warning to stderr if cookies are older than the threshold.

    Returns True if a warning was printed.
    """
    import sys

    age = get_cookie_age_days()
    if age is not None and age > _COOKIE_AGE_WARNING_DAYS:
        print(
            f"Warning: stored cookies are {age:.1f} days old. "
            "Consider running: lighthouse auth login",
            file=sys.stderr,
        )
        return True
    return False
