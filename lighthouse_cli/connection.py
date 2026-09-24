"""The Lighthouse (MAHE Manipal) connection the CLI talks to."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


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


LIGHTHOUSE = Connection("https://lighthouse.manipal.edu", None)

# Private seam for out-of-repository test harnesses only. When set, it must
# name its own cookie directory; clients built for it never refresh or
# migrate authentication (see LighthouseClient).
_override: Connection | None = None


def active_connection() -> Connection:
    """Return the connection for this process: Lighthouse unless overridden."""
    override = _override
    if override is None:
        return LIGHTHOUSE
    if not override.origin.startswith("https://") or override.cookie_dir is None:
        raise ValueError("An overriding connection needs an HTTPS origin and its own cookie directory.")
    return override
