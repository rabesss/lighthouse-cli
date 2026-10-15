"""Import an explicitly supplied session through the encrypted credential store."""

from __future__ import annotations

import sys
from contextlib import AbstractContextManager, nullcontext
from typing import NoReturn

import click

from .config import COOKIE_NAMES, missing_cookie_names
from .connection import active_connection
from .credential_store import CredentialStore
from .display import JsonOutputCommand, output_json, utc_now_iso
from .session_renewal import RenewalError, renewal_lock
from .utils import _loads_strict_json

_IMPORT_FAILED = (
    "Session import failed. Check the input origin, required cookies and encryption key source."
)
_SIGN_IN_BUSY = "Another sign-in is in progress. Run the command again."


def _fail(message: str, json_output: bool) -> NoReturn:
    click.echo(message, err=True)
    if json_output:
        output_json({"imported": False, "error": message})
    raise SystemExit(1)


@click.command("import-session", cls=JsonOutputCommand)
@click.option("--json", "json_output", is_flag=True)
def import_session(json_output: bool) -> None:
    """Seal a session supplied as JSON on stdin; never supply cookies in argv.

    Input shape: {"origin": "https://lighthouse.manipal.edu", "cookies": {...}}.
    The origin must match exactly. This does not extract browser cookies or
    prove the imported session is still authenticated.
    """
    try:
        connection = active_connection()
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
        # The built-in session is also written by automatic sign-in.
        lock: AbstractContextManager[None] = (
            renewal_lock() if connection.cookie_dir is None else nullcontext()
        )
        with lock:
            store.write_artifact(
                store.cookie_file,
                metadata={"extracted_at": utc_now_iso()},
                secret={"origin": connection.origin, "cookies": cookies},
            )
    except RenewalError as exc:
        _fail(_SIGN_IN_BUSY if exc.category == "busy" else _IMPORT_FAILED, json_output)
    except Exception:
        _fail(_IMPORT_FAILED, json_output)
    if json_output:
        output_json({"imported": True, "verified": False})
    else:
        click.echo("Session sealed; authentication has not been verified.")
