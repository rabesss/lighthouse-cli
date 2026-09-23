"""Import an explicitly supplied session through the encrypted credential store."""

from __future__ import annotations

import sys

import click

from .config import COOKIE_NAMES, missing_cookie_names
from .connection import connection_for
from .credential_store import CredentialStore
from .display import JsonOutputCommand, output_json, utc_now_iso
from .utils import _loads_strict_json


@click.command("import-session", cls=JsonOutputCommand)
@click.option("--site", type=click.Choice(["lighthouse", "trial"]), required=True)
@click.option("--json", "json_output", is_flag=True)
def import_session(site: str, json_output: bool) -> None:
    """Seal a session supplied as JSON on stdin; never supply cookies in argv.

    Input shape: {"origin": "https://the-selected-site", "cookies": {...}}.
    The origin must exactly match the selected site. This does not extract
    browser cookies or prove the imported session is still authenticated.
    """
    try:
        connection = connection_for(site)
        if sys.stdin.isatty():
            raise ValueError()
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        document = _loads_strict_json(raw)
        if not isinstance(document, dict) or document.get("origin") != connection.origin:
            raise ValueError()
        cookies = document.get("cookies")
        if not isinstance(cookies, dict) or set(cookies) != set(COOKIE_NAMES):
            raise ValueError()
        if any(not isinstance(v, str) or not v or any(ord(c) < 32 or ord(c) == 127 for c in v) for v in cookies.values()):
            raise ValueError()
        if missing_cookie_names(cookies):
            raise ValueError()
        store = CredentialStore(config_dir=connection.cookie_dir)
        store.write_artifact(
            store.cookie_file,
            metadata={"extracted_at": utc_now_iso()},
            secret={"origin": connection.origin, "cookies": cookies},
        )
    except Exception:
        message = "Session import failed. Check the input origin, required cookies and encryption key source."
        click.echo(message, err=True)
        if json_output:
            output_json({"imported": False, "error": message})
        raise SystemExit(1) from None
    if json_output:
        output_json({"imported": True, "site": site, "verified": False})
    else:
        click.echo(f"Session sealed for {site}; authentication has not been verified.")
