"""HTTP client, authentication, and configuration for lighthouse-cli.

Handles cookie-based session auth against D2L Brightspace APIs,
config directory management, and all low-level HTTP interactions.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

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

# CDP port for browser-harness
DEFAULT_CDP_PORT = 34165


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SessionExpiredError(Exception):
    """Raised when the server rejects our cookies (401 / redirect to login)."""


class NetworkError(Exception):
    """Raised on connectivity / DNS / timeout issues."""


class CourseNotFoundError(Exception):
    """Raised when a requested org-unit-id is not in the user's course list."""


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
    # Atomic write: write to temp file, then rename to avoid corruption
    tmp_file = COOKIE_FILE.with_suffix(".tmp")
    try:
        tmp_file.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        # Restrict permissions since these are session secrets
        tmp_file.chmod(0o600)
        tmp_file.replace(COOKIE_FILE)
    except OSError:
        # Clean up temp file on failure
        if tmp_file.exists():
            tmp_file.unlink()
        raise


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class LighthouseClient:
    """Stateful HTTP client wrapping requests.Session with D2L auth cookies.

    Instance-level cache avoids redundant API calls within a single CLI
    invocation (e.g. semester list fetched once, reused for filtering).
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._cookies: dict[str, str] = {}
        self._loaded = False
        self._cache: dict[str, Any] = {}

    # -- cookie management --------------------------------------------------

    def _ensure_cookies(self) -> dict[str, str]:
        """Load cookies from disk on first use."""
        if not self._loaded:
            self._cookies = load_cookies()
            self._loaded = True
        return self._cookies

    @property
    def cookies(self) -> dict[str, str]:
        return self._ensure_cookies()

    # -- low-level request --------------------------------------------------

    # Retry configuration
    _MAX_RETRIES = 3
    _RETRY_BACKOFF = 2  # base seconds for exponential backoff

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Make an authenticated request with rate-limit retry.

        Retries on HTTP 429 (Too Many Requests) with exponential backoff,
        respecting the Retry-After header when present.
        """
        cookies = self.cookies
        if not cookies:
            raise SessionExpiredError(
                "No cookies found. Run: lighthouse auth refresh"
            )

        last_exc: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            resp = self._session.request(
                method,
                url,
                cookies=cookies,
                allow_redirects=False,
                timeout=30,
                **kwargs,
            )

            # D2L redirects to login page when session is dead
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if "login" in location.lower() or "auth" in location.lower():
                    raise SessionExpiredError(
                        "Session expired. Run: lighthouse auth refresh"
                    )

            if resp.status_code == 401:
                raise SessionExpiredError(
                    "Session expired. Run: lighthouse auth refresh"
                )

            # Rate-limit: retry with backoff
            if resp.status_code == 429 and attempt < self._MAX_RETRIES:
                retry_after = float(resp.headers.get("Retry-After", self._RETRY_BACKOFF))
                wait = retry_after * (2 ** attempt)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp

        # Should not reach here, but just in case
        if last_exc:
            raise last_exc
        resp.raise_for_status()
        return resp  # type: ignore[unreachable]

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        """GET request with full URL construction from path."""
        if path.startswith("http"):
            url = path
        elif path.startswith("/d2l/"):
            url = f"{BASE_URL}{path}"
        else:
            url = f"{API_LE}{path}"
        return self._request("GET", url, **kwargs)

    def get_json(self, path: str, **kwargs: Any) -> Any:
        """GET request returning parsed JSON."""
        return self.get(path, **kwargs).json()

    def _paginate_list(self, path: str, items_key: str = "Objects") -> list[dict[str, Any]]:
        """GET a potentially paginated list endpoint.

        Handles D2L pagination by following the ``Next`` field in responses.
        If the response is a plain list (no pagination wrapper), returns it directly.
        If the response has no ``Next`` field, returns the items from a single page.

        Args:
            path: API path (will be resolved via get()).
            items_key: Key in the response dict containing the items array.
        """
        url: str | None = path
        all_items: list[dict[str, Any]] = []
        first = True
        while url:
            data = self.get_json(url)
            # Handle plain array responses (no pagination wrapper)
            if isinstance(data, list):
                return data
            all_items.extend(data.get(items_key, []))
            url = data.get("Next")
            if first and url is None:
                # Single-page response — if items_key is "Objects" but it's empty,
                # the endpoint might use a different key or return plain array.
                # Return what we have.
                first = False
        return all_items

    def get_raw(self, path: str, **kwargs: Any) -> tuple[bytes, dict[str, str]]:
        """GET request returning (content_bytes, headers_dict)."""
        resp = self.get(path, **kwargs)
        return resp.content, dict(resp.headers)

    # -- convenience API methods -------------------------------------------

    def _cached(self, key: str, fn: Any) -> Any:
        """Simple instance-level memoization."""
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def get_semesters(self) -> list[dict[str, Any]]:
        """GET /d2l/le/manageCourses/api/mysemesters (cached)."""
        return self._cached("semesters", lambda: self.get_json(f"{BASE_URL}/d2l/le/manageCourses/api/mysemesters"))

    def get_departments(self) -> list[dict[str, Any]]:
        """GET /d2l/le/manageCourses/api/mydepartments (cached)."""
        return self._cached("departments", lambda: self.get_json(f"{BASE_URL}/d2l/le/manageCourses/api/mydepartments"))

    def get_roles(self) -> list[dict[str, Any]]:
        """GET /d2l/le/manageCourses/api/myroles (cached)."""
        return self._cached("roles", lambda: self.get_json(f"{BASE_URL}/d2l/le/manageCourses/api/myroles"))

    def get_courses(self) -> list[dict[str, Any]]:
        """GET /d2l/le/manageCourses/api/mycourses – returns the Courses list (cached)."""
        def _fetch():
            data = self.get_json(f"{BASE_URL}/d2l/le/manageCourses/api/mycourses")
            return data.get("Courses", [])
        return self._cached("courses", _fetch)

    def get_content_toc(self, org_unit_id: int) -> dict[str, Any]:
        """GET content table-of-contents for a course."""
        return self.get_json(f"/{org_unit_id}/content/toc")

    def get_announcements(self, org_unit_id: int) -> list[dict[str, Any]]:
        """GET news/announcements for a course (handles pagination)."""
        return self._paginate_list(f"/{org_unit_id}/news/", "Objects")

    def get_grade_schema(self, org_unit_id: int) -> list[dict[str, Any]]:
        """GET grade categories/objects for a course."""
        # Grade schema is not paginated — returns a plain array
        return self.get_json(f"/{org_unit_id}/grades/")

    def get_my_grades(self, org_unit_id: int) -> list[dict[str, Any]]:
        """GET my grade values for a course (handles pagination)."""
        return self._paginate_list(f"/{org_unit_id}/grades/values/myGradeValues/", "Objects")

    def get_quizzes(self, org_unit_id: int) -> list[dict[str, Any]]:
        """GET quizzes for a course (handles pagination)."""
        return self._paginate_list(f"/{org_unit_id}/quizzes/", "Objects")

    def get_enrollments(self) -> list[dict[str, Any]]:
        """GET all enrollments (courses, sections, departments, etc.) (cached)."""
        def _fetch():
            items: list[dict[str, Any]] = []
            url: str | None = f"{BASE_URL}/d2l/api/lp/1.47/enrollments/myenrollments/"
            while url:
                data = self.get_json(url)
                items.extend(data.get("Items", []))
                url = data.get("Next")
            return items
        return self._cached("enrollments", _fetch)

    def get_course_enrollments(self) -> list[dict[str, Any]]:
        """GET enrollments filtered to Course Offering type only (cached)."""
        def _fetch():
            all_enrollments = self.get_enrollments()
            return [
                e for e in all_enrollments
                if e.get("OrgUnit", {}).get("Type", {}).get("Code") == "Course Offering"
            ]
        return self._cached("course_enrollments", _fetch)

    def get_quiz_detail(self, org_unit_id: int, quiz_id: int) -> dict[str, Any]:
        """GET full details for a specific quiz."""
        return self.get_json(f"/{org_unit_id}/quizzes/{quiz_id}")

    def get_calendar(self, org_unit_id: int) -> list[dict[str, Any]]:
        """GET calendar events for a course (handles pagination)."""
        return self._paginate_list(f"/{org_unit_id}/calendar/events/", "Objects")

    def download_topic_file(
        self, org_unit_id: int, topic_id: int
    ) -> tuple[bytes, str]:
        """Download a content topic file. Returns (bytes, filename)."""
        path = f"/{org_unit_id}/content/topics/{topic_id}/file"
        content, headers = self.get_raw(path)
        filename = _extract_filename(headers) or f"topic_{topic_id}"
        return content, filename

    def get_topic_html(self, org_unit_id: int, topic_id: int) -> tuple[bytes, str]:
        """Download an HTML content topic. Returns (html_bytes, sanitized_filename)."""
        path = f"/{org_unit_id}/content/topics/{topic_id}"
        data = self.get_json(path)
        # HTML topics have a Body.Text field with the HTML content
        body = data.get("Body", {})
        # Handle nested structure: {"Text": "..."} or directly a string
        if isinstance(body, dict):
            html_content = body.get("Text", "")
        else:
            html_content = str(body) if body else ""
        # If empty, try "Html" field
        if not html_content:
            html_content = data.get("Html", "") or data.get("html", "") or ""
        # Filename derived from topic title, sanitized
        title = data.get("Title", "") or f"topic_{topic_id}"
        sanitized = _sanitize_filename(title)
        if not sanitized.endswith(".html"):
            sanitized = sanitized + ".html"
        return html_content.encode("utf-8") if isinstance(html_content, str) else html_content, sanitized

    def check_auth(self) -> bool:
        """Quick auth check via /d2l/api/versions/."""
        try:
            self.get_json(f"{BASE_URL}/d2l/api/versions/")
            return True
        except (SessionExpiredError, requests.HTTPError):
            return False

    # -- Dropbox / Assignments ----------------------------------------------

    def get_dropbox_folders(self, org_unit_id: int) -> list[dict[str, Any]]:
        """GET list of dropbox folders (assignment submissions) for a course.

        Returns a list of DropboxFolder objects from the D2L API.
        """
        return self._paginate_list(f"/{org_unit_id}/dropbox/folders/", "Objects")

    def get_dropbox_folder_detail(self, org_unit_id: int, folder_id: int) -> dict[str, Any]:
        """GET full details for a specific dropbox folder, including attachments."""
        return self.get_json(f"/{org_unit_id}/dropbox/folders/{folder_id}")

    def download_attachment(
        self, org_unit_id: int, folder_id: int, file_id: int
    ) -> tuple[bytes, str]:
        """Download a dropbox attachment file. Returns (bytes, filename)."""
        path = f"/{org_unit_id}/dropbox/folders/{folder_id}/attachments/{file_id}"
        content, headers = self.get_raw(path)
        filename = _extract_filename(headers) or f"attachment_{file_id}"
        return content, filename

    def submit_file(
        self,
        org_unit_id: int,
        folder_id: int,
        file_bytes: bytes,
        filename: str,
        description: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Submit a file to a dropbox folder.

        Constructs a multipart/mixed request with:
        - Part 1: JSON RichText with submission text/description
        - Part 2: File binary data

        D2L API docs: https://docs.valence.desire2learn.com/basic/fileupload.html

        Returns parsed JSON response on success (HTTP 200) with submission details.
        """
        import io
        import mimetypes

        # Determine content type
        mime_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        # Build RichText description (required even if empty)
        text = description or f"Submitted via lighthouse-cli: {filename}"
        rich_text = {"Text": text, "Html": f"<p>{text}</p>"}

        # Build multipart/mixed body per D2L spec:
        # - Part 1: JSON with Content-Type application/json
        # - Part 2: File data with Content-Disposition form-data; name=""; filename="..."
        # The boundary must be unique. Using a fixed but reasonable boundary.
        boundary = "----lighthouseFormBoundary7x9f"
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json\r\n\r\n"
            f"{json.dumps(rich_text)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n"
            f'Content-Disposition: form-data; name=""; filename="{filename}"\r\n\r\n'
        )
        body_bytes = body.encode("utf-8")
        footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body_stream = io.BytesIO(body_bytes + file_bytes + footer)

        url = f"{API_LE}/{org_unit_id}/dropbox/folders/{folder_id}/submissions/mysubmissions/"
        headers = {
            "Content-Type": f"multipart/mixed; boundary={boundary}",
            "Content-Length": str(len(body_bytes) + len(file_bytes) + len(footer)),
        }
        resp = self._session.request(
            "POST",
            url,
            cookies=self.cookies,
            data=body_stream.getvalue(),
            headers=headers,
            timeout=60,
        )

        # D2L redirects to login page when session is dead
        if resp.status_code in (301, 302, 303, 307, 308):
            raise SessionExpiredError(
                "Session expired. Run: lighthouse auth refresh"
            )
        if resp.status_code == 401:
            raise SessionExpiredError(
                "Session expired. Run: lighthouse auth refresh"
            )
        if resp.status_code == 403:
            raise PermissionError(
                f"Permission denied to submit to folder {folder_id}. "
                "Check your enrollment and submission rights."
            )
        if resp.status_code == 404:
            raise FileNotFoundError(
                f"Dropbox folder {folder_id} or course {org_unit_id} not found. "
                "Run: lighthouse assignments"
            )
        if resp.status_code == 500:
            # Parse D2L error from JSON body
            try:
                err_data = resp.json()
                detail = err_data.get("detail", err_data.get("message", str(err_data)))
            except Exception:
                detail = resp.text or "Internal server error"
            raise ValueError(
                f"D2L API error (500): {detail}. "
                "This may indicate malformed request body or submission window restrictions."
            )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

from .utils import _sanitize_filename


def _extract_filename(headers: dict[str, str]) -> str:
    """Parse Content-Disposition header to get the filename."""
    cd = headers.get("Content-Disposition", headers.get("content-disposition", ""))
    if "filename=" in cd:
        parts = cd.split("filename=")
        if len(parts) > 1:
            name = parts[1].strip().strip('"').strip("'")
            if name:
                return name
    return ""


def resolve_course_id(client: LighthouseClient, identifier: str) -> int:
    """Resolve a course identifier (int org-unit-id or partial name) to an int id.

    Tries numeric parse first, then falls back to substring match on course names.
    """
    # Try as numeric org-unit-id
    try:
        return int(identifier)
    except ValueError:
        pass

    # Search by name substring (case-insensitive)
    courses = client.get_courses()
    matches = [
        c for c in courses if identifier.lower() in c.get("Name", "").lower()
    ]
    if len(matches) == 1:
        return int(matches[0]["OrgUnitId"])
    if len(matches) > 1:
        names = [f"  {c['OrgUnitId']} – {c['Name']}" for c in matches]
        raise CourseNotFoundError(
            f"Ambiguous match '{identifier}'. Multiple courses found:\n"
            + "\n".join(names)
            + "\n\nUse the numeric OrgUnitId for an exact match."
        )
    raise CourseNotFoundError(
        f"Course '{identifier}' not found. Run: lighthouse courses"
    )


# ---------------------------------------------------------------------------
# Auth refresh via browser-harness
# ---------------------------------------------------------------------------

def refresh_auth_from_browser(cdp_port: int | None = None) -> dict[str, str]:
    """Extract fresh D2L cookies from the browser via CDP.

    Uses the ``browser-harness`` tool (or falls back to raw CDP WebSocket calls)
    to connect to the user's browser, find the lighthouse.manipal.edu tab,
    and extract all d2l* cookies.

    Returns the saved cookie dict.
    """
    port = cdp_port or int(os.getenv("LIGHTHOUSE_CDP_PORT", str(DEFAULT_CDP_PORT)))

    # Strategy 1: try browser-harness CLI if available
    try:
        return _refresh_via_browser_harness(port)
    except FileNotFoundError:
        pass

    # Strategy 2: direct CDP via websockets
    try:
        return _refresh_via_cdp_websocket(port)
    except Exception as exc:
        print(
            f"Failed to extract cookies from browser: {exc}\n"
            "Make sure Chrome/Chromium is running with --remote-debugging-port="
            f"{port}",
            file=sys.stderr,
        )
        sys.exit(1)


def _refresh_via_browser_harness(port: int) -> dict[str, str]:
    """Attempt cookie extraction using the browser-harness CLI tool."""
    import subprocess

    result = subprocess.run(
        ["browser-harness", "cookies", "--port", str(port), "--domain", "lighthouse.manipal.edu"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"browser-harness failed: {result.stderr.strip()}")

    all_cookies = json.loads(result.stdout)
    d2l_cookies = {
        c["name"]: c["value"]
        for c in all_cookies
        if c["name"].startswith("d2l") and "lighthouse.manipal.edu" in c.get("domain", "")
    }
    if not d2l_cookies:
        raise RuntimeError("No d2l cookies found in browser. Is lighthouse.manipal.edu logged in?")

    save_cookies(d2l_cookies)
    return d2l_cookies


def _refresh_via_cdp_websocket(port: int) -> dict[str, str]:
    """Direct CDP cookie extraction using websocket-client (or raw http)."""
    import subprocess
    import urllib.request

    # Get browser websocket URL
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version") as resp:
        ws_url = json.loads(resp.read())["webSocketDebuggerUrl"]

    # Try using the websockets library
    try:
        import asyncio

        return asyncio.get_event_loop().run_until_complete(
            _cdp_get_cookies_ws(ws_url)
        )
    except ImportError:
        pass

    # Fall back to a small Node.js snippet if node is available
    return _cdp_get_cookies_node(port, ws_url)


async def _cdp_get_cookies_ws(ws_url: str) -> dict[str, str]:
    """Extract cookies via CDP using the websockets Python library."""
    import json as _json

    import websockets

    async with websockets.connect(ws_url, max_size=2**20) as ws:
        await ws.send(
            _json.dumps({"id": 1, "method": "Network.getAllCookies"})
        )
        resp = _json.loads(await ws.recv())
        all_cookies = resp.get("result", {}).get("cookies", [])
        d2l = {
            c["name"]: c["value"]
            for c in all_cookies
            if c["name"].startswith("d2l") and "lighthouse" in c.get("domain", "")
        }
        if not d2l:
            raise RuntimeError("No d2l cookies found. Is lighthouse.manipal.edu logged in?")
        save_cookies(d2l)
        return d2l


def _cdp_get_cookies_node(port: int, ws_url: str) -> dict[str, str]:
    """Fallback: use a tiny node script to extract CDP cookies."""
    import subprocess

    script = f"""
const ws = new WebSocket({ws_url!r});
ws.onopen = () => ws.send(JSON.stringify({{id:1, method:"Network.getAllCookies"}}));
ws.onmessage = (ev) => {{
    const cookies = JSON.parse(ev.data).result.cookies
        .filter(c => c.name.startsWith("d2l") && c.domain.includes("lighthouse"))
        .reduce((o, c) => {{ o[c.name] = c.value; return o; }}, {{}});
    console.log(JSON.stringify(cookies));
    ws.close();
    process.exit(0);
}};
setTimeout(() => {{ console.error("timeout"); process.exit(1); }}, 10000);
"""
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Node CDP extraction failed: {result.stderr.strip()}")

    d2l_cookies = json.loads(result.stdout.strip())
    if not d2l_cookies:
        raise RuntimeError("No d2l cookies found. Is lighthouse.manipal.edu logged in?")
    save_cookies(d2l_cookies)
    return d2l_cookies
