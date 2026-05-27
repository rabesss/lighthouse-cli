"""Pure HTTP authentication for lighthouse-cli.

Implements the Microsoft SSO login flow using ``MicrosoftSSOClient`` from
``lighthouse_cli.ms_auth`` — no browser, no Playwright, no CDP required.

Also provides encrypted credential storage via ``CredentialStore``.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, ensure_config_dir, save_cookies
from .api import LighthouseClient
from .ms_auth import MicrosoftSSOClient, MicrosoftSSOError


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AuthenticationError(Exception):
    """Raised when authentication fails (wrong credentials, 2FA, etc.)."""


class CredentialStoreError(Exception):
    """Raised when credential storage/retrieval fails."""


# ---------------------------------------------------------------------------
# Credential Store (encrypted storage)
# ---------------------------------------------------------------------------

class CredentialStore:
    """Encrypted credential storage using Fernet + system keyring.

    Stores credentials in ``~/.config/lighthouse-cli/credentials.json`` with
    encryption key stored in the system keyring.

    ``keyring`` and ``cryptography`` are optional dependencies.  Without them,
    credentials cannot be stored or loaded.
    """

    SERVICE_NAME = "lighthouse-cli"
    KEY_NAME = "credential-key"

    def __init__(self) -> None:
        self.config_dir = Path(os.getenv("LIGHTHOUSE_CONFIG_DIR", str(CONFIG_DIR))).expanduser()
        self.credentials_file = self.config_dir / "credentials.json"

    def _get_encryption_key(self) -> bytes:
        """Get or create the encryption key from system keyring."""
        import keyring

        if key_str := keyring.get_password(self.SERVICE_NAME, self.KEY_NAME):
            return key_str.encode("utf-8")

        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        keyring.set_password(self.SERVICE_NAME, self.KEY_NAME, key.decode("utf-8"))
        return key

    def _get_fernet(self) -> Any:
        """Get a Fernet instance for encryption/decryption."""
        from cryptography.fernet import Fernet
        return Fernet(self._get_encryption_key())

    def _check_deps(self) -> None:
        """Check that keyring+cryptography are installed."""
        try:
            import keyring  # noqa: F401
            import cryptography  # noqa: F401
        except ImportError as e:
            raise CredentialStoreError(
                "Credential storage requires optional dependencies. "
                "Install with: pip install lighthouse-cli[credentials]"
            ) from e

    def save(self, username: str, password: str) -> None:
        """Encrypt and save credentials to disk.

        Args:
            username: The username (email)
            password: The password

        Raises:
            CredentialStoreError: If credentials are empty, dependencies
                missing, or storage fails.
        """
        if not username or not username.strip():
            raise CredentialStoreError("Username cannot be empty")
        if not password or not password.strip():
            raise CredentialStoreError("Password cannot be empty")

        self._check_deps()

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.chmod(0o700)

        tmp_file = self.credentials_file.with_suffix(".tmp")
        encrypted = self._get_fernet().encrypt(
            json.dumps({"username": username, "password": password}).encode("utf-8")
        )
        tmp_file.write_bytes(encrypted)
        tmp_file.chmod(0o600)
        tmp_file.replace(self.credentials_file)
        self.credentials_file.chmod(0o600)

    def load(self) -> tuple[str, str] | None:
        """Load and decrypt stored credentials.

        Returns:
            Tuple of (username, password) if credentials exist and decrypt
            successfully.  None if credentials file doesn't exist.

        Raises:
            CredentialStoreError: If the file exists but is corrupted or
                keyring is unavailable.
        """
        if not self.credentials_file.exists():
            return None

        self._check_deps()

        try:
            data = json.loads(
                self._get_fernet().decrypt(
                    self.credentials_file.read_bytes()
                ).decode("utf-8")
            )
            return data.get("username", ""), data.get("password", "")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CredentialStoreError(f"Credentials file is corrupted: {exc}") from exc
        except Exception as exc:
            raise CredentialStoreError(f"Failed to load credentials: {exc}") from exc

    def exists(self) -> bool:
        """Check if stored credentials exist."""
        return self.credentials_file.exists()


# ---------------------------------------------------------------------------
# Interactive credential helpers
# ---------------------------------------------------------------------------

def _is_interactive() -> bool:
    """Check if stdin is a TTY."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main command function
# ---------------------------------------------------------------------------

def _auth_error(msg: str, json_output: bool, code: int = 1) -> int:
    """Print an auth error and return an exit code."""
    if json_output:
        print(json.dumps({"success": False, "error": msg}))
    else:
        print(f"Error: {msg}", file=sys.stderr)
    return code


def cmd_auth_login(
    username: str | None = None,
    password: str | None = None,
    totp_code: str | None = None,
    totp_stdin: bool = False,
    save_credentials: bool = False,
    json_output: bool = False,
    config_dir: str | None = None,
) -> int:
    """Authenticate via Microsoft SSO using pure HTTP (no browser).

    Flow:
    1. Resolve credentials (flags > env > stored > prompt)
    2. Resolve TOTP code (flag > stdin pipe > prompt)
    3. Authenticate via MicrosoftSSOClient (pure HTTP)
    4. Save cookies to disk
    5. Optionally save encrypted credentials

    Args:
        username: Username from --user flag
        password: Password from --pass flag
        totp_code: 2FA code from --totp flag
        totp_stdin: If True, read TOTP from stdin
        save_credentials: If True, save credentials encrypted
        json_output: If True, output JSON
        config_dir: Override config directory

    Returns:
        Exit code (0=success, 1=auth failure, 2=CLI usage error, 130=interrupted)
    """
    if config_dir:
        os.environ["LIGHTHOUSE_CONFIG_DIR"] = str(config_dir)

    ensure_config_dir()

    try:
        # --- Credential resolution ---
        # Priority: flags > env vars > stored credentials > interactive prompt
        username = _resolve_credential(username, "LIGHTHOUSE_USERNAME")
        password = _resolve_credential(password, "LIGHTHOUSE_PASSWORD")

        # Try stored credentials if still missing
        if not username or not password:
            store = CredentialStore()
            stored = None
            with suppress(CredentialStoreError):
                stored = store.load()
            if stored:
                username = username or stored[0]
                password = password or stored[1]

        # Interactive prompt if still needed
        if not username or not password:
            if not _is_interactive():
                return _auth_error(
                    "Credentials required. Provide --user/--pass, "
                    "LIGHTHOUSE_USERNAME/LIGHTHOUSE_PASSWORD env vars, "
                    "or run interactively.",
                    json_output,
                )

            if not username:
                print("Username (email): ", end="", flush=True)
                username = sys.stdin.readline().strip()
            if not password:
                password = getpass.getpass("Password: ").strip()

        if not username:
            return _auth_error("Username cannot be empty", json_output)
        if not password:
            return _auth_error("Password cannot be empty", json_output)

        # --- TOTP resolution ---
        if totp_stdin:
            totp_code = sys.stdin.readline().strip()
        if totp_code is not None and totp_code.strip() == "":
            return _auth_error("2FA code cannot be empty", json_output, 2)

        # --- Authenticate via HTTP ---
        sso_client = MicrosoftSSOClient()
        try:
            cookies = sso_client.login(username, password, totp_code)
        except MicrosoftSSOError as exc:
            return _auth_error(str(exc), json_output)
        finally:
            sso_client.close()

        # --- Save cookies ---
        save_cookies(cookies)

        # --- Verify session ---
        if not LighthouseClient().check_auth():
            return _auth_error(
                "Login completed but session verification failed. "
                "Try: lighthouse auth login",
                json_output,
            )

        # --- Save credentials if requested ---
        if save_credentials:
            try:
                store = CredentialStore()
                store.save(username, password)
            except CredentialStoreError as exc:
                print(f"Warning: Could not save credentials: {exc}", file=sys.stderr)

        # --- Success output ---
        if json_output:
            print(json.dumps({
                "success": True,
                "cookies": list(cookies.keys()),
            }))
        else:
            print(f"Login successful. Session valid. Cookies: {', '.join(cookies.keys())}")

        return 0

    except KeyboardInterrupt:
        if json_output:
            print(json.dumps({"success": False, "error": "Interrupted by user"}))
        else:
            print("\nInterrupted.", file=sys.stderr)
        return 130

    except Exception as exc:
        return _auth_error(str(exc), json_output)


def _resolve_credential(value: str | None, env_var: str) -> str | None:
    """Resolve a credential: flag value first, then env var."""
    if value is not None:
        return value
    return os.getenv(env_var, "").strip() or None
