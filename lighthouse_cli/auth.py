"""Headless browser authentication for lighthouse-cli.

Implements the full Microsoft SSO login flow with 2FA using Playwright:
- Headless Chromium launch via Playwright
- D2L login → Microsoft SSO → Azure AD → 2FA → redirect back to D2L
- Cookie extraction (4 d2l cookies)
- Encrypted credential storage with system keyring
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

# Alias for tests that patch it
SyncPlaywright = sync_playwright

# Config directory and cookie file paths
from .api import (
    CONFIG_DIR,
    COOKIE_FILE,
    LighthouseClient,
    ensure_config_dir,
    save_cookies,
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AuthenticationError(Exception):
    """Raised when authentication fails (wrong credentials, 2FA, etc.)."""


class CredentialStoreError(Exception):
    """Raised when credential storage/retrieval fails."""


# ---------------------------------------------------------------------------
# Headless Authenticator
# ---------------------------------------------------------------------------

class HeadlessAuthenticator:
    """Manages Playwright headless browser lifecycle for SSO login.

    The full login chain is:
    1. Launch headless Chromium
    2. Navigate to D2L login page
    3. Follow redirect to Microsoft SSO (Azure AD)
    4. Fill credentials on Microsoft form
    5. Submit 2FA code on verification page
    6. Wait for redirect back to D2L with session cookies
    7. Extract all 4 d2l cookies
    8. Save cookies to disk
    9. Verify session with check_auth()
    """

    BASE_URL = "https://lighthouse.manipal.edu"
    LOGIN_URL = f"{BASE_URL}/d2l/login"

    # Microsoft Azure AD login selectors (robust with multiple fallbacks)
    MS_LOGIN_URL = "https://login.microsoftonline.com"
    MS_SELECTORS = {
        "username": [
            'input[name="loginfmt"]',
            'input[type="email"]',
            'input[id="i0116"]',
            'input[aria-label="Email, phone, or Skype"]',
            'input[autocomplete="username"]',
        ],
        "password": [
            'input[name="Password"]',
            'input[type="password"]',
            'input[id="i0118"]',
            'input[aria-label="Password"]',
        ],
        "submit_btn": [
            'input[type="submit"]',
            'button[type="submit"]',
            'input[value="Sign in"]',
            'button[id="idSIButton9"]',
        ],
        "2fa_input": [
            'input[name="otpc"]',
            'input[id="idTxtBx_SAOTCC_OTC"]',
            'input[id="idTxtBx_SAOTCC_ORESend"]',
            'input[aria-label="Enter your code"]',
            'input[autocomplete="one-time-code"]',
        ],
        "2fa_submit_btn": [
            'input[type="submit"]',
            'button[type="submit"]',
            'input[value="Verify"]',
            'button[id="idSubmit_SAOTCC_Continue"]',
        ],
        "stay_signed_in": [
            'input[id="idChkBx_RememberMe"]',
            'input[name="RememberMe"]',
            'input[type="checkbox"]',
        ],
    }

    def __init__(self) -> None:
        self.browser = None
        self.context = None
        self.page = None
        self._totp_timeout = 120  # seconds

    def launch_browser(self) -> None:
        """Launch headless Chromium via Playwright."""
        pw = sync_playwright().start()
        self._playwright = pw  # keep reference to prevent GC

        try:
            self.browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            self.context = self.browser.new_context(
                accept_downloads=False,
                ignore_https_errors=True,
            )
            self.page = self.context.new_page()

            # Set default timeout for waiting operations
            self.page.set_default_timeout(30000)  # 30s default

        except Exception as exc:
            pw.stop()
            raise AuthenticationError(
                f"Failed to launch browser: {exc}\n"
                "Install Chrome/Chromium or set CHROME_PATH to the browser binary."
            ) from exc

    def _find_element(self, selectors: list[str], timeout: int = 10000) -> Any:
        """Find an element using multiple fallback selectors.

        Returns the first matched element.
        Raises AuthenticationError if none found.
        """
        for selector in selectors:
            try:
                element = self.page.wait_for_selector(
                    selector, state="attached", timeout=timeout
                )
                if element:
                    return element
            except Exception:
                continue

        # Extract selector name for error message
        selector_name = selectors[0] if selectors else "element"
        raise AuthenticationError(
            f"Could not find expected element on SSO page: {selector_name}"
        )

    def navigate_sso(
        self,
        username: str,
        password: str,
        totp_code: str | None = None,
    ) -> None:
        """Navigate the full SSO chain: D2L → Microsoft SSO → 2FA → D2L redirect.

        Args:
            username: User email/username for Microsoft SSO
            password: User password
            totp_code: 2FA code (if not provided, will be prompted interactively)
        """
        if self.page is None:
            raise AuthenticationError("Browser not launched. Call launch_browser() first.")

        # Step 1: Navigate to D2L login page
        self.page.goto(self.LOGIN_URL, wait_until="networkidle")

        # Step 2: Wait for redirect to Microsoft SSO
        try:
            self.page.wait_for_url(
                lambda url: "login.microsoftonline.com" in url,
                timeout=30000,
            )
        except Exception:
            # Already on Microsoft from D2L redirect
            pass

        # Step 3: Fill username on Microsoft form
        try:
            user_input = self._find_element(self.MS_SELECTORS["username"])
            user_input.fill(username)
            # Click next/continue
            next_btn = self._find_element(self.MS_SELECTORS["submit_btn"] + [
                'input[value="Next"]',
                'button[value="Next"]',
                'input[id="idBtn_Back"]',
            ])
            next_btn.click()
            # Wait for password field to appear
            self.page.wait_for_timeout(1000)
        except AuthenticationError:
            # Might be already on password page (if redirect preserves state)
            pass

        # Step 4: Fill password
        try:
            pass_input = self._find_element(self.MS_SELECTORS["password"], timeout=15000)
            pass_input.fill(password)
            submit_btn = self._find_element(self.MS_SELECTORS["submit_btn"] + [
                'input[value="Sign in"]',
                'button[value="Sign in"]',
            ])
            submit_btn.click()
        except AuthenticationError:
            raise AuthenticationError("Login failed: invalid credentials")

        # Step 5: Handle 2FA
        self._handle_2fa(totp_code)

        # Step 6: Handle "Stay Signed In?" prompt if it appears
        try:
            stay_signed_in = self._find_element(
                self.MS_SELECTORS["stay_signed_in"] + [
                    'input[id="idChkBx_RememberMe"]',
                ],
                timeout=3000,
            )
            if stay_signed_in:
                # Don't check - just click Yes to continue
                self.page.click('input[value="Yes"]')
                self.page.wait_for_timeout(1000)
        except AuthenticationError:
            pass  # No stay signed in prompt

        # Step 7: Wait for redirect back to D2L
        try:
            self.page.wait_for_url(
                lambda url: "lighthouse.manipal.edu" in url and "/d2l/" in url,
                timeout=30000,
            )
        except Exception:
            current_url = self.page.url
            if "lighthouse.manipal.edu" in current_url:
                pass  # Already on D2L
            else:
                raise AuthenticationError(
                    f"SSO redirect did not return to D2L. Final URL: {current_url}"
                )

    def _handle_2fa(self, totp_code: str | None = None) -> None:
        """Handle 2FA step - either use provided code or prompt interactively.

        Uses _totp_timeout (120s default) to allow users time to read 2FA
        from authenticator app. Raises AuthenticationError on timeout.
        """
        try:
            # Wait for 2FA input field with the configured TOTP timeout
            otp_input = self._find_element(
                self.MS_SELECTORS["2fa_input"],
                timeout=self._totp_timeout * 1000,  # Playwright uses milliseconds
            )

            if totp_code is None:
                # Interactive prompt
                import getpass
                totp_code = getpass.getpass("Enter 2FA code: ")

            if not totp_code or totp_code.strip() == "":
                raise AuthenticationError("2FA code cannot be empty")

            otp_input.fill(totp_code.strip())

            # Submit 2FA
            submit_btn = self._find_element(self.MS_SELECTORS["2fa_submit_btn"] + [
                'input[value="Verify"]',
                'button[value="Verify"]',
            ])
            submit_btn.click()
            self.page.wait_for_timeout(2000)

        except AuthenticationError as exc:
            if "Could not find" in str(exc):
                # 2FA input not found - might have succeeded without 2FA
                return
            raise

    def extract_cookies(self) -> dict[str, str]:
        """Extract all 4 D2L session cookies from browser context.

        Returns dict mapping cookie names to values.
        """
        if self.context is None:
            raise AuthenticationError("Browser context not available")

        all_cookies = self.context.cookies()
        d2l_cookies = {}
        for c in all_cookies:
            name = c.get("name", "")
            value = c.get("value", "")
            domain = c.get("domain", "")

            if name.startswith("d2l") and ("lighthouse" in domain or domain.startswith(".")):
                d2l_cookies[name] = value

        required = (
            "d2lSecureSessionVal",
            "d2lSessionVal",
            "d2lSameSiteCanaryA",
            "d2lSameSiteCanaryB",
        )
        missing = [c for c in required if c not in d2l_cookies]
        if missing:
            raise AuthenticationError(
                f"Missing required cookies after SSO: {missing}. "
                "The login may have failed or the session expired."
            )

        return d2l_cookies

    def authenticate(
        self,
        username: str,
        password: str,
        totp_code: str | None = None,
    ) -> dict[str, str]:
        """Full authentication flow: launch browser, navigate SSO, extract cookies.

        Returns dict of all 4 d2l cookies.
        Raises AuthenticationError on any failure.
        """
        try:
            self.launch_browser()
            self.navigate_sso(username, password, totp_code)
            cookies = self.extract_cookies()
            return cookies
        finally:
            self.close()

    def close(self) -> None:
        """Terminate the headless browser process (always called in finally)."""
        if self.browser is not None:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
            self.context = None
            self.page = None

        if hasattr(self, "_playwright"):
            try:
                self._playwright.stop()
            except Exception:
                pass
            del self._playwright


# ---------------------------------------------------------------------------
# Credential Store (encrypted storage)
# ---------------------------------------------------------------------------

class CredentialStore:
    """Encrypted credential storage using Fernet + system keyring.

    Stores credentials in ~/.config/lighthouse-cli/credentials.json with
    encryption key stored in the system keyring.
    """

    SERVICE_NAME = "lighthouse-cli"
    KEY_NAME = "credential-key"
    CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"

    def __init__(self) -> None:
        self.config_dir = Path(os.getenv("LIGHTHOUSE_CONFIG_DIR", str(CONFIG_DIR))).expanduser()
        self.credentials_file = self.config_dir / "credentials.json"

    def _get_encryption_key(self) -> bytes:
        """Get or create the encryption key from system keyring."""
        import keyring

        # Try to get existing key
        key_str = keyring.get_password(self.SERVICE_NAME, self.KEY_NAME)
        if key_str:
            return key_str.encode("utf-8")

        # Generate new key
        from cryptography.fernet import Fernet
        key = Fernet.generate_key()
        keyring.set_password(self.SERVICE_NAME, self.KEY_NAME, key.decode("utf-8"))
        return key

    def _get_fernet(self) -> Any:
        """Get a Fernet instance for encryption/decryption."""
        from cryptography.fernet import Fernet

        key = self._get_encryption_key()
        return Fernet(key)

    def save(self, username: str, password: str) -> None:
        """Encrypt and save credentials to disk.

        Args:
            username: The username (email)
            password: The password

        Raises:
            CredentialStoreError: If credentials are empty or storage fails
        """
        if not username or username.strip() == "":
            raise CredentialStoreError("Username cannot be empty")
        if not password or password.strip() == "":
            raise CredentialStoreError("Password cannot be empty")

        fernet = self._get_fernet()
        credentials = json.dumps({"username": username, "password": password})
        encrypted = fernet.encrypt(credentials.encode("utf-8"))

        # Ensure config directory exists
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.chmod(0o700)

        # Atomic write: write to temp file, then rename
        tmp_file = self.credentials_file.with_suffix(".tmp")
        tmp_file.write_bytes(encrypted)
        tmp_file.chmod(0o600)
        tmp_file.replace(self.credentials_file)
        self.credentials_file.chmod(0o600)

    def load(self) -> tuple[str, str] | None:
        """Load and decrypt stored credentials.

        Returns:
            Tuple of (username, password) if credentials exist and decrypt successfully.
            None if credentials file doesn't exist, is corrupted, or keyring is unavailable.
        """
        if not self.credentials_file.exists():
            return None

        try:
            encrypted = self.credentials_file.read_bytes()
            fernet = self._get_fernet()
            decrypted = fernet.decrypt(encrypted)
            data = json.loads(decrypted.decode("utf-8"))
            return data.get("username", ""), data.get("password", "")
        except Exception:
            # Corrupted file or keyring unavailable
            return None

    def exists(self) -> bool:
        """Check if stored credentials exist."""
        return self.credentials_file.exists()


# ---------------------------------------------------------------------------
# Interactive credential helpers
# ---------------------------------------------------------------------------

def _prompt_for_credentials(
    require_user: bool = True,
    require_pass: bool = True,
) -> tuple[str, str]:
    """Prompt interactively for username and password.

    Uses getpass for masked password input.
    """
    import getpass

    if require_user:
        username = input("Username (email): ").strip()
    else:
        username = ""

    if require_pass:
        password = getpass.getpass("Password: ").strip()
    else:
        password = ""

    return username, password


def _prompt_for_totp() -> str:
    """Prompt interactively for 2FA code."""
    import getpass
    code = getpass.getpass("Enter 2FA code: ").strip()
    if not code:
        raise AuthenticationError("2FA code cannot be empty")
    return code


def _is_interactive() -> bool:
    """Check if stdin is a TTY."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main command function
# ---------------------------------------------------------------------------

def cmd_auth_login(
    username: str | None = None,
    password: str | None = None,
    totp_code: str | None = None,
    totp_stdin: bool = False,
    save_credentials: bool = False,
    json_output: bool = False,
    config_dir: str | None = None,
) -> int:
    """Main auth login command.

    Args:
        username: Username from --user flag (or None)
        password: Password from --pass flag (or None)
        totp_code: 2FA code from --totp flag (or None)
        totp_stdin: If True, read TOTP from stdin
        save_credentials: If True, save credentials encrypted
        json_output: If True, output JSON
        config_dir: Override config directory

    Returns:
        Exit code (0=success, 1=auth failure, 2=CLI usage error)
    """
    # Apply config directory override
    if config_dir:
        os.environ["LIGHTHOUSE_CONFIG_DIR"] = str(config_dir)

    # Ensure config directory exists
    ensure_config_dir()

    try:
        # --- Credential resolution ---
        # Priority: flags > env vars > stored credentials > interactive prompt

        if username is None:
            username = os.getenv("LIGHTHOUSE_USERNAME", "").strip()
        if password is None:
            password = os.getenv("LIGHTHOUSE_PASSWORD", "").strip()

        # Try stored credentials if no credentials found
        if not username or not password:
            store = CredentialStore()
            stored = store.load()
            if stored:
                if not username:
                    username = stored[0]
                if not password:
                    password = stored[1]

        # Interactive prompt if still needed
        if not username or not password:
            if not _is_interactive():
                if json_output:
                    print(json.dumps({"success": False, "error": "Credentials required. Provide --user/--pass, LIGHTHOUSE_USERNAME/PASSWORD env vars, or run interactively."}))
                    return 1
                print(
                    "Error: Credentials required. Provide --user/--pass flags, "
                    "LIGHTHOUSE_USERNAME/PASSWORD env vars, or run interactively.",
                    file=sys.stderr,
                )
                return 1

            if not username:
                print("Username (email): ", end="", flush=True)
                username = sys.stdin.readline().strip()
            if not password:
                password = getpass.getpass("Password: ").strip()  # type: ignore

        # Validate credentials
        if not username or username.strip() == "":
            if json_output:
                print(json.dumps({"success": False, "error": "Username cannot be empty"}))
            else:
                print("Error: Username cannot be empty", file=sys.stderr)
            return 1

        if not password or password.strip() == "":
            if json_output:
                print(json.dumps({"success": False, "error": "Password cannot be empty"}))
            else:
                print("Error: Password cannot be empty", file=sys.stderr)
            return 1

        # --- TOTP resolution ---
        if totp_stdin:
            totp_code = sys.stdin.readline().strip()
        elif totp_code is None:
            # Will be prompted interactively by HeadlessAuthenticator
            pass

        # --- TOTP validation ---
        if totp_code is not None and totp_code.strip() == "":
            if json_output:
                print(json.dumps({"success": False, "error": "2FA code cannot be empty"}))
            else:
                print("Error: 2FA code cannot be empty", file=sys.stderr)
            return 2

        # --- Launch headless browser and authenticate ---
        authenticator = HeadlessAuthenticator()
        try:
            cookies = authenticator.authenticate(username, password, totp_code)
        except AuthenticationError as exc:
            error_msg = str(exc)
            if json_output:
                print(json.dumps({"success": False, "error": error_msg}))
            else:
                print(f"Error: {error_msg}", file=sys.stderr)
            return 1
        finally:
            # Always clean up browser
            authenticator.close()

        # --- Save cookies ---
        save_cookies(cookies)

        # --- Verify session ---
        from .api import LighthouseClient
        client = LighthouseClient()
        if not client.check_auth():
            if json_output:
                print(json.dumps({
                    "success": False,
                    "error": "Cookies extracted but session verification failed. Run: lighthouse auth refresh"
                }))
            else:
                print(
                    "Error: Cookies extracted but session verification failed. "
                    "Run: lighthouse auth refresh",
                    file=sys.stderr,
                )
            return 1

        # --- Save credentials if requested ---
        if save_credentials:
            store = CredentialStore()
            store.save(username, password)

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
        # Ctrl+C: clean browser, exit with 130
        if "authenticator" in dir():
            authenticator.close()
        if json_output:
            print(json.dumps({"success": False, "error": "Interrupted by user"}))
        else:
            print("\nInterrupted.", file=sys.stderr)
        return 130

    except Exception as exc:
        if "authenticator" in dir():
            authenticator.close()
        if json_output:
            print(json.dumps({"success": False, "error": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
