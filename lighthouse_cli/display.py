"""Output and formatting helpers for lighthouse-cli.

All presentation logic lives here: table rendering (rich + plain-text
fallback), JSON output, error printing, and text truncation utilities.
"""

from __future__ import annotations

import json
import sys
from typing import Any


# ---------------------------------------------------------------------------
# Rich table rendering (optional dependency)
# ---------------------------------------------------------------------------

# Cache rich imports at module level to avoid re-import per table render.
_RICH_CACHE: tuple | None = None
_RICH_CHECKED: bool = False


def _try_rich():
    """Import rich if available, return (Table, console) or None. Cached."""
    global _RICH_CACHE, _RICH_CHECKED
    if not _RICH_CHECKED:
        _RICH_CHECKED = True
        try:
            from rich.console import Console
            from rich.table import Table
            _RICH_CACHE = (Table, Console())
        except ImportError:
            _RICH_CACHE = None
    return _RICH_CACHE


def print_table(columns: list[str], rows: list[list[str]], title: str = "") -> None:
    """Print a table using rich if available, else plain aligned text."""
    if rich := _try_rich():
        Table, console = rich
        table = Table(title=title, show_lines=False, pad_edge=False)
        for col in columns:
            table.add_column(col, overflow="ellipsis")
        for row in rows:
            table.add_row(*row)
        console.print(table)
        return

    # Plain-text fallback: columnar alignment
    widths = [max(len(c), *(len(row[i]) for row in rows)) for i, c in enumerate(columns)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    if title:
        print(f"\n{title}")
    print(fmt.format(*columns) + "\n" + fmt.format(*["-" * w for w in widths])
          + "\n" + "\n".join(fmt.format(*row) for row in rows))


def output_json(data: Any) -> None:
    """Print raw JSON to stdout (for --json mode / agent consumption)."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def error(msg: str) -> int:
    """Print error message to stderr. Returns 1 for convenient ``return _error(...)``."""
    print(f"Error: {msg}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Text formatting utilities
# ---------------------------------------------------------------------------

def short(text: str, max_len: int = 50) -> str:
    """Truncate text with ellipsis."""
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def fmt_date(date_str: str | None) -> str:
    """Format an ISO date string to something compact."""
    if not date_str:
        return "—"
    try:
        return date_str.replace("Z", "").replace("+00:00", "")[:16]
    except Exception:
        return str(date_str)[:16]


def utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 string (e.g. '2026-05-10T14:30:00Z')."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
