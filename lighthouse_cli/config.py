"""Configuration paths and cookie persistence for lighthouse-cli."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://lighthouse.manipal.edu"
API_LE = f"{BASE_URL}/d2l/api/le/1.93"
API_LP = f"{BASE_URL}/d2l/api/lp/1.59"

# Cookie names we care about
COOKIE_NAMES = (
    "d2lSameSiteCanaryA",
    "d2lSameSiteCanaryB",
    "d2lSecureSessionVal",
    "d2lSessionVal",
)

# Paths
CONFIG_DIR = Path(os.getenv("LIGHTHOUSE_CONFIG_DIR", "~/.config/lighthouse-cli")).expanduser()
COOKIE_FILE = CONFIG_DIR / "cookies.json"
DEFAULT_DOWNLOAD_DIR = Path("~/Downloads/lighthouse").expanduser()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def ensure_config_dir() -> Path:
    """Create the config directory if it doesn't exist with 0700 permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
    return CONFIG_DIR


def load_cookies() -> dict[str, str]:
    """Load cookies from disk. Returns empty dict if file is missing."""
    if not COOKIE_FILE.exists():
        return {}
    try:
        data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if k in COOKIE_NAMES}
    except (json.JSONDecodeError, OSError):
        return {}


def save_cookies(cookies: dict[str, str]) -> None:
    """Persist cookies to disk atomically (temp file + rename)."""
    ensure_config_dir()
    tmp_file = COOKIE_FILE.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp_file.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        tmp_file.chmod(0o600)
        tmp_file.replace(COOKIE_FILE)
    except OSError:
        if tmp_file.exists():
            tmp_file.unlink()
        raise
