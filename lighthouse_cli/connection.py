"""Explicit sandbox connection settings; production remains the default."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


SUPPORTED_SITES = ("lighthouse", "trial")


@dataclass(frozen=True)
class Connection:
    origin: str
    cookie_dir: Path | None

    @property
    def host(self) -> str:
        return urlsplit(self.origin).hostname or ""

    @property
    def api_le(self) -> str:
        return f"{self.origin}/d2l/api/le/1.93"


def connection_for(site: str) -> Connection:
    """Keep alternate-site cookies out of the default authentication files.

    An explicit site is deliberately limited to the two inspected tenants.
    Adding other tenants requires verifying their authentication contract.
    """
    if site == "lighthouse":
        return Connection("https://lighthouse.manipal.edu", None)
    if site != "trial":
        raise ValueError("Unknown connection. Choose lighthouse or trial.")
    root = Path(os.getenv("LIGHTHOUSE_CONFIG_DIR", "~/.config/lighthouse-cli")).expanduser()
    return Connection("https://hetrynow.brightspace.com", root / "sites" / "hetrynow.brightspace.com")
