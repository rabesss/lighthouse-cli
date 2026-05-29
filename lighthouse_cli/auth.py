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

from .config import CONFIG_DIR, ensure_config_dir, load_mfa_pending, save_cookies
from .api import LighthouseClient
from .ms_auth import (
    MFA_METHOD_APP,
    MFA_METHOD_AUTO,
    MFA_METHOD_CHOOSE,
    MFA_METHOD_SMS,
    MfaPendingError,
    MicrosoftSSOClient,
    MicrosoftSSOError,
    VALID_MFA_METHODS,
)


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


def cmd_auth_verify(
    totp_code: str,
    *,
    json_output: bool = False,
    config_dir: str | None = None,
) -> int:
    """Complete MFA using saved state from ``auth login`` (same BeginAuth session)."""
    if config_dir:
        os.environ["LIGHTHOUSE_CONFIG_DIR"] = str(config_dir)

    ensure_config_dir()

    if not totp_code or not totp_code.strip():
        return _auth_error("2FA code cannot be empty", json_output, 2)

    if not load_mfa_pending():
        return _auth_error(
            "No pending MFA session. Run: lighthouse auth login --mfa-method sms",
            json_output,
        )

    sso_client = MicrosoftSSOClient()
    try:
        cookies = sso_client.complete_mfa_pending(totp_code.strip())
    except MicrosoftSSOError as exc:
        return _auth_error(str(exc), json_output)
    except (KeyError, TypeError, ValueError) as exc:
        return _auth_error(
            f"Pending MFA session is corrupted: {exc}. "
            "Run: lighthouse auth login --mfa-method sms",
            json_output,
        )
    finally:
        sso_client.close()

    save_cookies(cookies)

    if not LighthouseClient().check_auth():
        return _auth_error(
            "Login completed but session verification failed.",
            json_output,
        )

    if json_output:
        print(json.dumps({"success": True, "cookies": list(cookies.keys())}))
    else:
        print(f"Login successful. Session valid. Cookies: {', '.join(cookies.keys())}")

    return 0


def cmd_auth_login(
    username: str | None = None,
    password: str | None = None,
    totp_code: str | None = None,
    totp_stdin: bool = False,
    save_credentials: bool = False,
    json_output: bool = False,
    config_dir: str | None = None,
    mfa_method: str | None = None,
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
        totp_code: 2FA code from --totp flag (omit for two-phase interactive login)
        totp_stdin: If True, read TOTP from stdin
        save_credentials: If True, save credentials encrypted
        json_output: If True, output JSON
        config_dir: Override config directory
        mfa_method: MFA delivery preference (auto, sms, app)

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

        resolved_mfa_method = (
            mfa_method or os.getenv("LIGHTHOUSE_MFA_METHOD") or MFA_METHOD_AUTO
        ).lower()
        if resolved_mfa_method not in VALID_MFA_METHODS:
            return _auth_error(
                f"Invalid MFA method {resolved_mfa_method!r}. "
                f"Use: {', '.join(VALID_MFA_METHODS)}",
                json_output,
                2,
            )

        # --- TOTP resolution ---
        # For SMS/OTP, BeginAuth sends a fresh code; only read stdin after that challenge.
        read_totp_after_challenge = totp_stdin
        if totp_stdin:
            totp_code = None
        elif totp_code is not None and resolved_mfa_method in (
            MFA_METHOD_SMS,
            MFA_METHOD_APP,
            MFA_METHOD_CHOOSE,
        ):
            # Literal --totp <code> cannot match the code BeginAuth is about to send.
            totp_code = None
        if totp_code is not None and totp_code.strip() == "":
            return _auth_error("2FA code cannot be empty", json_output, 2)

        # Resume same MFA session (no second BeginAuth / new code).
        if totp_code and not totp_stdin and load_mfa_pending():
            return cmd_auth_verify(totp_code, json_output=json_output, config_dir=config_dir)

        defer_mfa_to_pending = (
            not _is_interactive()
            and totp_code is None
            and not read_totp_after_challenge
        )

        def _on_password_accepted() -> None:
            if json_output or not _is_interactive():
                return
            print("Password accepted. Completing second factor...", flush=True)

        if _is_interactive() and not json_output and totp_code is None:
            print(
                "Two-step sign-in: enter email and password first; "
                "you will be asked for a verification code next.",
                flush=True,
            )
            if resolved_mfa_method == MFA_METHOD_SMS:
                print("MFA preference: text message (--mfa-method sms).", flush=True)
            elif resolved_mfa_method == MFA_METHOD_CHOOSE:
                print("You will be asked to pick a verification method.", flush=True)

        # --- Authenticate via HTTP ---
        sso_client = MicrosoftSSOClient()
        try:
            cookies = sso_client.login(
                username,
                password,
                totp_code,
                mfa_method=resolved_mfa_method,
                on_credentials_submitted=_on_password_accepted,
                read_totp_after_challenge=read_totp_after_challenge,
                defer_mfa_to_pending=defer_mfa_to_pending,
            )
        except MfaPendingError as exc:
            if json_output:
                print(json.dumps({
                    "success": False,
                    "mfa_pending": True,
                    "message": str(exc),
                    "recovery": exc.recovery,
                }))
            else:
                print(str(exc), flush=True)
            return 0
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

    except (OSError, RuntimeError, ValueError, MicrosoftSSOError) as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)
        return _auth_error(str(exc), json_output)


def _resolve_credential(value: str | None, env_var: str) -> str | None:
    """Resolve a credential: flag value first, then env var."""
    if value is not None:
        return value
    return os.getenv(env_var, "").strip() or None
