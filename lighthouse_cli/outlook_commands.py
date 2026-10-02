"""CLI presentation for the experimental, one-shot Outlook capability probe."""

from __future__ import annotations

from typing import Any

import click

from .display import output_json
from .outlook_web import OutlookWebError, collect_outlook_rows


def _validate_options(
    interactive_login: bool, search: str | None, limit: int, login_timeout: int,
) -> tuple[str, str] | None:
    """Reject unsafe or unsupported invocations before opening a browser."""
    if not interactive_login:
        return (
            "interactive_login_required",
            "Use --interactive-login to open a temporary browser and sign in yourself.",
        )
    if type(limit) is not int or not 1 <= limit <= 100:
        return "invalid_limit", "--limit must be an integer from 1 to 100."
    if type(login_timeout) is not int or not 30 <= login_timeout <= 600:
        return "invalid_login_timeout", "--login-timeout must be an integer from 30 to 600."
    if search is not None and (
        not isinstance(search, str) or not search.strip()
        or len(search) > 512 or not search.isprintable()
    ):
        return "invalid_search", "--search must contain 1 to 512 printable characters."
    if search is not None:
        return (
            "search_not_supported",
            "Outlook search is not supported yet because result freshness cannot be verified. "
            "No browser was opened.",
        )
    return None


def _error(code: str, message: str, *, json_output: bool, exit_code: int = 1) -> int:
    """Emit only fixed diagnostics; never echo browser exceptions or input."""
    click.echo(f"Error: {message}", err=True)
    if json_output:
        output_json({"source": "outlook_web", "code": code, "error": message})
    return exit_code


def _metadata_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep the output boundary content-free, even if the collector adds fields."""
    rows = [{
        "position": position,
        "unread": row["unread"] if type(row["unread"]) is bool else None,
        "rendered_text": "",
        "text_omitted": True,
    } for position, row in enumerate(snapshot["rows"], start=1)]
    return {
        "source": "outlook_web",
        "coverage": "rendered_rows_only",
        "complete_mailbox": False,
        "stable_ids": False,
        "scope": "current_view",
        "text_included": False,
        "rows": rows,
        "limit_reached": snapshot["limit_reached"] is True,
    }


def _print_rows(snapshot: dict[str, Any]) -> None:
    click.echo(f"Outlook current view: {len(snapshot['rows'])} currently rendered rows")
    click.echo("Metadata only: all message content is withheld.")
    click.echo("Partial view only; row positions are not message IDs.")
    for row in snapshot["rows"]:
        unread = row["unread"]
        state = "unread" if unread is True else "read" if unread is False else "unknown"
        click.echo(f"{row['position']}. [{state}] [content withheld]")
    if snapshot["limit_reached"]:
        click.echo("Requested row limit reached; additional rows may exist.")


def cmd_outlook_probe(
    *,
    interactive_login: bool = False,
    search: str | None = None,
    limit: int = 25,
    login_timeout: int = 180,
    json_output: bool = False,
) -> int:
    """Collect one content-free metadata snapshot with opt-in, bounded arguments."""
    invalid = _validate_options(interactive_login, search, limit, login_timeout)
    if invalid is not None:
        return _error(*invalid, json_output=json_output)

    click.echo(
        "Opening a separate temporary Outlook browser. Complete sign-in and MFA "
        "there yourself; the browser closes after this metadata-only check. "
        "No sign-in session is saved.",
        err=True,
    )
    try:
        snapshot = _metadata_snapshot(
            collect_outlook_rows(search=None, limit=limit, login_timeout=login_timeout)
        )
    except KeyboardInterrupt:
        return _error(
            "interrupted", "Outlook row collection interrupted.",
            json_output=json_output, exit_code=130,
        )
    except OutlookWebError as exc:
        # Rebuild the diagnostic from the fixed code map, even if an exception
        # subclass or caller has attached upstream text to the original error.
        safe_error = OutlookWebError(exc.code if type(exc.code) is str else "browser_error")
        return _error(safe_error.code, str(safe_error), json_output=json_output)
    except Exception:
        return _error(
            "outlook_failed", "Outlook row collection failed.", json_output=json_output,
        )

    if json_output:
        output_json(snapshot)
    else:
        _print_rows(snapshot)
    return 0
