"""HTTP client and authentication for lighthouse-cli.

Handles cookie-based session auth against D2L Brightspace APIs
and all low-level HTTP interactions.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import sys
import time
import urllib.request
from contextlib import suppress
from typing import Any
from urllib.parse import unquote_to_bytes, urlparse

import requests

from .config import API_LE, BASE_URL, COOKIE_NAMES, COOKIE_SETTING_HOST, d2l_cookies_from_entries, load_cookies, missing_cookie_names, save_cookies
from .utils import _sanitize_filename, get_enrolled_course_catalog

# CDP port for browser-harness
DEFAULT_CDP_PORT = 34165

# Keep the bytes-returning download API bounded. Operators can raise the
# default for a known tenant, but the environment value itself is capped so a
# typo cannot restore an unlimited allocation.
DEFAULT_MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_CONFIGURABLE_DOWNLOAD_BYTES = 1024 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024
MAX_HTML_TOPIC_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_DOWNLOAD_BYTES_ENV = "LIGHTHOUSE_MAX_DOWNLOAD_BYTES"
CDP_RESPONSE_TIMEOUT_SECONDS = 15


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SessionExpiredError(Exception):
    """Raised when the server rejects our cookies (401 / redirect to login)."""

    def __init__(self, message: str, recovery: str | None = None) -> None:
        super().__init__(message)
        self.recovery = recovery

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.recovery:
            parts.append(f"  Recovery: {self.recovery}")
        return "\n".join(parts)


class NetworkError(Exception):
    """Raised on connectivity, protocol, or timeout issues."""


class SubmissionOutcomeUnknownError(NetworkError):
    """Raised when a successful submission response cannot be interpreted."""

    _MESSAGE = (
        "Submission outcome is unknown because the API returned an unsupported "
        "result shape. Verify the assignment status before trying again."
    )

    def __init__(self) -> None:
        super().__init__(self._MESSAGE)


class ContentResponseShapeError(NetworkError):
    """Raised when an HTML topic response has an unsupported data shape."""

    _MESSAGE = "Content response has an unsupported shape."

    def __init__(self) -> None:
        super().__init__(self._MESSAGE)


class _BrowserHarnessFallback(NetworkError):
    """Signal that direct CDP extraction should try after helper failure."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Stop local CDP discovery before urllib follows an untrusted redirect."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise NetworkError("Browser debugging endpoint returned a redirect.")


class CourseNotFoundError(Exception):
    """Raised when a requested org-unit-id is not in the user's course list."""


def _download_size_limit() -> int:
    """Return the bounded binary-download limit configured for this process."""
    raw = os.getenv(_MAX_DOWNLOAD_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_DOWNLOAD_BYTES
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        raise NetworkError("Binary download size limit is invalid.")
    limit = int(raw)
    if limit <= 0 or limit > MAX_CONFIGURABLE_DOWNLOAD_BYTES:
        raise NetworkError("Binary download size limit is invalid.")
    return limit


def _safe_http_error(response: Any) -> requests.HTTPError:
    """Build an HTTP error without copying URL or response-body details.

    ``requests.Response.raise_for_status()`` includes the complete request URL
    in its exception text.  Pagination links and future API endpoints may
    carry query parameters, so that default message is not safe to surface to
    a CLI caller.  Keep the response attached for status-aware formatters,
    while making the human-readable message deliberately minimal.
    """
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        message = f"HTTP {status} response"
    else:
        message = "HTTP request failed"
    return requests.HTTPError(message, response=response)


def _close_response(response: Any) -> None:
    """Close a response without surfacing adapter-specific cleanup errors."""
    close = getattr(response, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


def _positive_org_unit_id(value: Any) -> int | None:
    """Coerce a Brightspace org-unit ID, accepting only positive integers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        with suppress(ValueError):
            number = int(value)
            return number if number > 0 else None
    return None


def _require_positive_endpoint_id(value: Any, field_name: str) -> int:
    """Validate an assignment endpoint identifier before URL construction."""
    identifier = _positive_org_unit_id(value)
    if identifier is None:
        raise ValueError(f"{field_name} must be a positive integer.")
    return identifier


def _require_safe_submission_filename(value: Any) -> str:
    """Validate a multipart filename before embedding it in an HTTP header."""
    if not isinstance(value, str) or not value.strip() or value in {".", ".."}:
        raise ValueError("filename must be a non-empty basename.")
    try:
        overlong = len(value.encode("utf-8")) > 255
    except UnicodeEncodeError:
        overlong = True
    if overlong:
        raise ValueError("filename is too long; use a name of at most 255 bytes.")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError("filename contains unsupported control characters.")
    if "/" in value or "\\" in value:
        raise ValueError("filename must not contain path separators.")
    return value


_MIME_TYPE_RE = re.compile(
    r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+/[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
)


def _require_safe_content_type(value: Any) -> str:
    """Validate a caller-provided media type before header interpolation."""
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8", errors="ignore")) > 255
        or not value.isascii()
        or _MIME_TYPE_RE.fullmatch(value) is None
    ):
        raise ValueError("content_type must be a valid ASCII MIME type without parameters.")
    return value


_MISSING = object()
_MAX_RICH_TEXT_DEPTH = 16


def _extract_rich_text(value: Any) -> str | None:
    """Extract bounded RichText content without recursive traversal.

    Brightspace commonly returns ``{"Text": {"Html": "..."}}`` for an
    HTML topic, while older responses use a direct string.  Only the known
    RichText keys are followed.  Cycles, excessive nesting, and non-scalar
    values are rejected with a fixed shape error so an untrusted response
    cannot cause a recursion error or leak arbitrary content.
    """
    current = value
    seen: set[int] = set()
    depth = 0
    blank: str | None = None
    while True:
        if isinstance(current, str):
            return current
        if current is None:
            return None
        if not isinstance(current, dict):
            raise ContentResponseShapeError()
        if depth >= _MAX_RICH_TEXT_DEPTH:
            raise ContentResponseShapeError()
        object_id = id(current)
        if object_id in seen:
            raise ContentResponseShapeError()
        seen.add(object_id)
        depth += 1

        if current and not any(
            key in current for key in ("Html", "html", "HTML", "Text")
        ):
            raise ContentResponseShapeError()

        html_value: Any = _MISSING
        for key in ("Html", "html", "HTML"):
            if key in current:
                html_value = current[key]
                break
        text_value = current.get("Text", _MISSING)
        for candidate in (html_value, text_value):
            if candidate is not _MISSING and candidate is not None and not isinstance(
                candidate, (str, dict)
            ):
                raise ContentResponseShapeError()

        # Prefer actual HTML when it is present and non-empty.  Keep an empty
        # scalar as a fallback so an explicitly empty RichText remains a
        # valid, bytes-valued response.
        for candidate in (html_value, text_value):
            if isinstance(candidate, str):
                if candidate:
                    return candidate
                if blank is None:
                    blank = candidate

        # If no scalar was available, follow one nested RichText object.  The
        # HTML key wins over Text to mirror the scalar preference above.
        nested = next(
            (
                candidate
                for candidate in (html_value, text_value)
                if isinstance(candidate, dict)
            ),
            _MISSING,
        )
        if nested is _MISSING:
            return blank
        current = nested


def _normalise_course_enrollment(
    enrollment: Any,
) -> dict[str, Any] | None:
    """Project one raw enrollment into the public course-list shape."""
    if not isinstance(enrollment, dict):
        return None
    org_unit = enrollment.get("OrgUnit")
    if not isinstance(org_unit, dict):
        return None

    org_unit_id = _positive_org_unit_id(org_unit.get("Id"))
    if org_unit_id is None:
        return None

    access = enrollment.get("Access")
    is_active = access.get("IsActive", True) if isinstance(access, dict) else True
    name = org_unit.get("Name")
    code = org_unit.get("Code")
    return {
        "OrgUnitId": org_unit_id,
        "Name": name if isinstance(name, str) else "",
        "Code": code if isinstance(code, str) else "",
        "IsActive": is_active,
    }


# ---------------------------------------------------------------------------
# Expanded session-expired message
# ---------------------------------------------------------------------------

_SESSION_EXPIRED_RECOVERY = (
    "Options:\n"
    "  1. If Chrome is open and logged in to lighthouse.manipal.edu:"
    " re-run any command (CDP auto-refresh may apply)\n"
    "  2. HTTP SSO: lighthouse auth login --mfa-method sms"
    " (then lighthouse auth verify <code> if prompted)\n"
    "  3. Set LIGHTHOUSE_USERNAME and LIGHTHOUSE_PASSWORD for non-interactive login"
)


def _session_expired_msg(detail: str = "") -> str:
    """Build a short session-expired message."""
    return f"Session expired{' (' + detail + ')' if detail else ''}. Run: lighthouse auth login"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class LighthouseClient:
    """Stateful HTTP client wrapping requests.Session with D2L auth cookies.

    Instance-level cache avoids redundant API calls within a single CLI
    invocation (e.g. semester list fetched once, reused for filtering).
    ``read_only_auth`` disables legacy-cookie migration and browser-based
    session refresh so dry-run callers cannot modify local auth state.
    """

    def __init__(self, read_only_auth: bool = False) -> None:
        self._session = requests.Session()
        self._cookies: dict[str, str] = {}
        self._loaded = False
        self._cache: dict[str, Any] = {}
        self._read_only_auth = bool(read_only_auth)

    # -- cookie management --------------------------------------------------

    def _apply_cookies_to_session(self, cookies: dict[str, str]) -> None:
        """Load D2L cookies into the session jar so Set-Cookie responses are preserved."""
        for name in COOKIE_NAMES:
            self._session.cookies.set(
                name,
                cookies.get(name, ""),
                domain=COOKIE_SETTING_HOST,
                path="/",
            )

    def _ensure_cookies(self) -> dict[str, str]:
        """Load cookies from disk on first use."""
        if not self._loaded:
            self._cookies = load_cookies(read_only=self._read_only_auth)
            self._apply_cookies_to_session(self._cookies)
            self._loaded = True
        return self._cookies

    @property
    def cookies(self) -> dict[str, str]:
        return self._ensure_cookies()

    # -- low-level request --------------------------------------------------

    # Retry configuration
    _MAX_RETRIES = 3
    _RETRY_BACKOFF = 2  # base seconds for exponential backoff
    # Never honor an untrusted server-provided delay beyond one minute.
    _MAX_RETRY_AFTER = 60.0
    # A request is replayed only when repeating it cannot create another
    # remote side effect.  In particular, a submission POST must never be
    # retried after the server may have accepted the body.
    _RETRYABLE_METHODS = frozenset({"GET", "HEAD"})
    # D2L normally returns a small number of pages, but a broken ``Next``
    # chain must not make a command loop or issue unbounded requests.
    _MAX_PAGINATION_PAGES = 100

    def _request(self, method: str, url: str, _skip_raise: bool = False, _timeout: int = 30, **kwargs: Any) -> requests.Response:
        """Make an authenticated request with safe retry and auto-refresh.

        GET and HEAD requests retry HTTP 429 (Too Many Requests) with
        exponential backoff, respecting the Retry-After header when present.
        A valid server delay is capped at ``_MAX_RETRY_AFTER`` seconds;
        malformed delays use the exponential fallback.  Other methods are
        sent exactly once because replaying a non-idempotent request could
        duplicate a remote side effect.

        On SessionExpiredError, GET and HEAD attempt one auto-refresh via CDP
        if a browser with valid cookies is running, then retry the request
        once.  Non-idempotent methods surface the failure without refreshing
        or replaying the request.

        Args:
            _skip_raise: If True, skip raise_for_status() and return the raw
                response. Caller handles error status codes.
            _timeout: Request timeout in seconds (default 30).
        """
        cookies = self.cookies
        if missing_cookie_names(cookies):
            raise SessionExpiredError(_session_expired_msg("no cookies found"), recovery=_SESSION_EXPIRED_RECOVERY)

        retryable = method.upper() in self._RETRYABLE_METHODS
        refresh_attempted = False
        while True:
            try:
                return self._do_request(method, url, _skip_raise, _timeout, **kwargs)
            except SessionExpiredError:
                if self._read_only_auth or not retryable:
                    # A POST/PUT/etc. may already have been accepted by the
                    # server even when its response reports an expired
                    # session.  Never refresh and send that body again.  The
                    # read-only auth mode also forbids browser refresh and
                    # cookie persistence for GET/HEAD requests.
                    raise
                if refresh_attempted:
                    raise SessionExpiredError(
                        _session_expired_msg("auto-refresh already attempted"),
                        recovery=_SESSION_EXPIRED_RECOVERY,
                    )
                refresh_attempted = True
                print("Session expired. Refreshing from browser...", file=sys.stderr)
                try:
                    new_cookies = refresh_auth_from_browser()
                except Exception:
                    raise SessionExpiredError(
                        _session_expired_msg("auto-refresh failed"),
                        recovery=_SESSION_EXPIRED_RECOVERY,
                    ) from None

                missing = missing_cookie_names(new_cookies)
                if missing:
                    raise SessionExpiredError(
                        _session_expired_msg(f"CDP cookies missing: {missing}"),
                        recovery=_SESSION_EXPIRED_RECOVERY,
                    )

                save_cookies(new_cookies)
                self._cookies = new_cookies
                self._apply_cookies_to_session(new_cookies)

    def _do_request(
        self, method: str, url: str,
        # skip_raise forwarded from _request._skip_raise
        skip_raise: bool, timeout: int, **kwargs: Any,
    ) -> requests.Response:
        """Execute the HTTP request with bounded idempotent retries.

        Network exceptions are deliberately converted to a URL-free
        :class:`NetworkError`.  ``requests`` includes the full request URL in
        several exception messages, and pagination/query URLs can contain
        sensitive values that must not escape through a CLI error.
        """
        retryable = method.upper() in self._RETRYABLE_METHODS
        max_attempts = self._MAX_RETRIES + 1 if retryable else 1
        for attempt in range(max_attempts):
            try:
                resp = self._session.request(
                    method,
                    url,
                    allow_redirects=False,
                    timeout=timeout,
                    **kwargs,
                )
            except requests.RequestException:
                if attempt >= max_attempts - 1:
                    raise NetworkError(
                        f"Network request failed after {max_attempts} attempt(s)."
                    ) from None
                time.sleep(self._RETRY_BACKOFF * (2 ** attempt))
                continue

            # D2L redirects to login page when session is dead
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "").lower()
                if "login" in location or "auth" in location:
                    _close_response(resp)
                    raise SessionExpiredError(
                        _session_expired_msg(f"HTTP {resp.status_code} redirect to login"),
                        recovery=_SESSION_EXPIRED_RECOVERY,
                    )
                if not skip_raise:
                    _close_response(resp)
                    raise NetworkError("The server returned an unexpected redirect.")

            if resp.status_code == 401:
                _close_response(resp)
                raise SessionExpiredError(
                    _session_expired_msg("HTTP 401 Unauthorized"),
                    recovery=_SESSION_EXPIRED_RECOVERY,
                )

            # Rate-limit: retry with backoff
            if resp.status_code == 429 and retryable and attempt < max_attempts - 1:
                fallback = self._RETRY_BACKOFF * (2 ** attempt)
                retry_after = resp.headers.get("Retry-After")
                try:
                    server_delay = float(retry_after) if retry_after is not None else None
                except (TypeError, ValueError):
                    server_delay = None

                # Retry-After is untrusted input.  Invalid, non-finite, and
                # negative values use the normal exponential fallback.  A
                # valid server delay is authoritative (do not exponentiate it)
                # but is capped to keep a malicious response from stalling the
                # CLI indefinitely.
                if server_delay is None or not math.isfinite(server_delay) or server_delay < 0:
                    delay = fallback
                else:
                    delay = min(server_delay, self._MAX_RETRY_AFTER)
                _close_response(resp)
                time.sleep(delay)
                continue

            if not skip_raise:
                try:
                    resp.raise_for_status()
                except requests.HTTPError:
                    # Do not let requests copy the URL (or any adapter
                    # diagnostics) into the exception text.
                    error = _safe_http_error(resp)
                    _close_response(resp)
                    raise error from None
                except Exception:
                    # A custom response adapter should not be able to leak a
                    # raw response/URL through an unexpected validation
                    # exception either.
                    _close_response(resp)
                    raise NetworkError("Network response validation failed.") from None
            return resp

        # All retries exhausted
        raise NetworkError(f"Network request failed after {max_attempts} attempt(s).")

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        """GET request with full URL construction from path."""
        if not isinstance(path, str):
            raise NetworkError("Invalid API URL.")

        # Absolute URLs and root-relative paths are all routed through the
        # same validator.  This keeps the convenience method from becoming
        # an arbitrary-origin HTTP client and prevents path traversal from
        # reaching the request layer.  Query-only paths remain supported for
        # pagination-style callers and resolve beneath the API root here.
        url = self._canonical_pagination_url(path, error_message="Invalid API URL.")
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
        seen_urls: set[str] = set()
        pages_fetched = 0
        base_url: str | None = None
        while url is not None and url != "":
            if pages_fetched >= self._MAX_PAGINATION_PAGES:
                raise NetworkError("Pagination exceeded the maximum page count.")
            canonical_url = self._canonical_pagination_url(url, base_url=base_url)
            if canonical_url in seen_urls:
                raise NetworkError("Pagination cycle detected while following a Next link.")
            seen_urls.add(canonical_url)
            # Preserve relative links for compatibility with the public
            # ``get_json`` seam, but normalize absolute links before passing
            # them to it.  In particular, URL schemes are case-insensitive
            # while ``get()`` uses the normalized lowercase HTTPS form.
            parsed = urlparse(url)
            request_url = (
                canonical_url
                if parsed.scheme or parsed.netloc or url.startswith("?")
                else url
            )
            try:
                data = self.get_json(request_url)
            except (NetworkError, SessionExpiredError):
                raise
            except Exception:
                # The normal request path already converts transport errors,
                # but keep this pagination boundary safe for alternate client
                # adapters and test doubles that may expose raw URLs.
                raise NetworkError("Could not fetch a paginated response.") from None
            pages_fetched += 1
            # Handle plain array responses (no pagination wrapper)
            if isinstance(data, list):
                all_items.extend(data)
                return all_items
            if not isinstance(data, dict):
                raise NetworkError("Invalid paginated response returned by the server.")
            page_items = data.get(items_key, [])
            if not isinstance(page_items, list):
                raise NetworkError("Invalid paginated items returned by the server.")
            all_items.extend(page_items)
            base_url = canonical_url
            url = data.get("Next")
        return all_items

    @staticmethod
    def _canonical_pagination_url(
        url: str,
        *,
        base_url: str | None = None,
        error_message: str = "Invalid pagination link returned by the server.",
    ) -> str:
        """Validate and canonicalize one server-provided pagination target.

        D2L may return either a same-origin absolute URL or a relative API
        path.  Query-only links (for example ``?page=2``) are resolved
        against ``base_url`` when supplied.  Other relative paths without the
        ``/d2l/`` prefix retain the historical ``get()`` behavior and are
        scoped beneath ``API_LE``;
        this keeps existing enrollment pagination links working while still
        preventing network-path references (``//host``) and traversal out of
        the API path.  The canonical value is used for cycle detection and
        for normalized absolute requests; relative links retain their
        original form when passed to ``get_json`` for compatibility.
        """
        invalid = error_message
        if not isinstance(url, str) or not url or url != url.strip():
            raise NetworkError(invalid)
        if any(ord(char) < 0x20 for char in url) or "\\" in url:
            raise NetworkError(invalid)

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError):
            raise NetworkError(invalid) from None

        decoded_path = unquote_to_bytes(parsed.path).decode("utf-8", errors="replace")
        if (
            "\\" in decoded_path
            or any(ord(char) < 0x20 for char in decoded_path)
            or any(segment == ".." for segment in decoded_path.split("/"))
        ):
            raise NetworkError(invalid)

        # A URL with a scheme or authority must be an HTTPS URL on the exact
        # LMS origin.  Explicit :443 is the only non-empty port accepted.
        if parsed.scheme or parsed.netloc:
            if (
                parsed.scheme.lower() != "https"
                or hostname is None
                or hostname.lower() != COOKIE_SETTING_HOST
                or parsed.username is not None
                or parsed.password is not None
                or port not in (None, 443)
                or parsed.params
                or parsed.fragment
            ):
                raise NetworkError(invalid)
            path = parsed.path or "/"
            return f"{BASE_URL}{path}" + (f"?{parsed.query}" if parsed.query else "")

        # Relative API paths are scoped beneath API_LE. In particular,
        # ``//evil.example`` has a parsed netloc and is rejected above, while
        # decoded ``..`` segments cannot escape the approved D2L API scope.
        if parsed.fragment:
            raise NetworkError(invalid)
        if not (url.startswith("/") or url.startswith("?")):
            return f"{API_LE}/{url}"
        if url.startswith("?") and base_url is not None:
            try:
                base = LighthouseClient._canonical_pagination_url(
                    base_url, error_message=error_message
                )
                base_parsed = urlparse(base)
            except (TypeError, ValueError):
                raise NetworkError(invalid) from None
            return f"{BASE_URL}{base_parsed.path or '/'}" + (
                f"?{parsed.query}" if parsed.query else ""
            )
        if parsed.path.startswith("/d2l/"):
            return f"{BASE_URL}{url}"
        return f"{API_LE}{url}"

    def get_raw(
        self,
        path: str,
        *,
        max_bytes: int | None = None,
        **kwargs: Any,
    ) -> tuple[bytes, dict[str, str]]:
        """GET a binary response with an enforced actual-byte ceiling."""
        limit = _download_size_limit() if max_bytes is None else max_bytes
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > MAX_CONFIGURABLE_DOWNLOAD_BYTES
        ):
            raise NetworkError("Binary download size limit is invalid.")

        kwargs.pop("stream", None)
        resp = self.get(path, stream=True, **kwargs)
        try:
            headers = dict(resp.headers)
            raw_length = resp.headers.get("Content-Length")
            if (
                isinstance(raw_length, str)
                and raw_length.isascii()
                and raw_length.isdecimal()
                and len(raw_length) <= 10
                and int(raw_length) > limit
            ):
                raise NetworkError("Binary download exceeds the configured size limit.")

            content = bytearray()
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                if not chunk:
                    continue
                if not isinstance(chunk, bytes):
                    raise NetworkError("Binary download returned an unsupported chunk.")
                if len(content) + len(chunk) > limit:
                    raise NetworkError("Binary download exceeds the configured size limit.")
                content.extend(chunk)
            return bytes(content), headers
        except NetworkError:
            raise
        except requests.RequestException:
            raise NetworkError("Binary download failed while reading the response.") from None
        except Exception:
            raise NetworkError("Binary download response could not be processed.") from None
        finally:
            _close_response(resp)

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
        return self._cached("courses", lambda: self.get_json(f"{BASE_URL}/d2l/le/manageCourses/api/mycourses").get("Courses", []))

    def get_content_toc(self, org_unit_id: int) -> dict[str, Any]:
        """GET content table-of-contents for a course."""
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        return self.get_json(f"/{course_id}/content/toc")

    def get_announcements(self, org_unit_id: int) -> list[dict[str, Any]]:
        """GET news/announcements for a course (handles pagination)."""
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        return self._paginate_list(f"/{course_id}/news/", "Objects")

    def get_grade_schema(self, org_unit_id: int) -> list[dict[str, Any]]:
        """GET grade categories/objects for a course."""
        # Grade schema is not paginated — returns a plain array
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        return self.get_json(f"/{course_id}/grades/")

    def get_my_grades(self, org_unit_id: int) -> list[dict[str, Any]]:
        """GET my grade values for a course (handles pagination)."""
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        return self._paginate_list(
            f"/{course_id}/grades/values/myGradeValues/", "Objects"
        )

    def get_quizzes(self, org_unit_id: int) -> list[dict[str, Any]]:
        """GET quizzes for a course (handles pagination)."""
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        return self._paginate_list(f"/{course_id}/quizzes/", "Objects")

    def get_enrollments(self) -> list[dict[str, Any]]:
        """GET all enrollments (courses, sections, departments, etc.) (cached)."""
        return self._cached(
            "enrollments",
            lambda: self._paginate_list(
                f"{BASE_URL}/d2l/api/lp/1.47/enrollments/myenrollments/", "Items"
            ),
        )

    def get_course_enrollments(self) -> list[dict[str, Any]]:
        """GET enrollments filtered to Course Offering type only (cached)."""
        def _fetch() -> list[dict[str, Any]]:
            return [
                enrollment
                for enrollment in self.get_enrollments()
                if isinstance(enrollment, dict)
                and isinstance(enrollment.get("OrgUnit"), dict)
                and isinstance(enrollment["OrgUnit"].get("Type"), dict)
                and enrollment["OrgUnit"]["Type"].get("Code") == "Course Offering"
            ]

        return self._cached("course_enrollments", _fetch)

    def get_enrolled_courses(self) -> list[dict[str, Any]]:
        """Return a normalized, deterministic view of enrolled courses.

        ``get_courses()`` intentionally remains the manage-courses endpoint
        exposed by Brightspace.  Learner-facing read commands need the
        paginated enrollment universe instead, though, because a course can
        be present in ``myenrollments`` before it appears in that endpoint.
        Keep the raw enrollment records available through
        ``get_course_enrollments()`` for scope/configuration code and expose a
        small, stable projection for consumers that only need course
        identity/display fields.

        Malformed enrollment records and non-positive org-unit IDs are ignored.
        Duplicate IDs retain the first valid record and the returned list is
        sorted numerically without modifying the API response objects.
        """

        def _fetch() -> list[dict[str, Any]]:
            courses: dict[int, dict[str, Any]] = {}
            for enrollment in self.get_course_enrollments():
                course = _normalise_course_enrollment(enrollment)
                if course is not None:
                    courses.setdefault(course["OrgUnitId"], course)
            return [courses[org_id] for org_id in sorted(courses)]

        return self._cached("enrolled_courses", _fetch)

    def get_quiz_detail(self, org_unit_id: int, quiz_id: int) -> dict[str, Any]:
        """GET full details for a specific quiz."""
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        quiz_identifier = _require_positive_endpoint_id(quiz_id, "quiz_id")
        return self.get_json(f"/{course_id}/quizzes/{quiz_identifier}")

    def get_calendar(self, org_unit_id: int) -> list[dict[str, Any]]:
        """GET calendar events for a course (handles pagination)."""
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        return self._paginate_list(f"/{course_id}/calendar/events/", "Objects")

    def download_topic_file(self, org_unit_id: int, topic_id: int) -> tuple[bytes, str]:
        """Download a content topic file. Returns (bytes, filename)."""
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        topic_identifier = _require_positive_endpoint_id(topic_id, "topic_id")
        content, headers = self.get_raw(
            f"/{course_id}/content/topics/{topic_identifier}/file"
        )
        return content, _extract_filename(headers) or f"topic_{topic_identifier}"

    def get_topic_html(self, org_unit_id: int, topic_id: int) -> tuple[bytes, str]:
        """Download an HTML content topic. Returns (html_bytes, sanitized_filename)."""
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        topic_identifier = _require_positive_endpoint_id(topic_id, "topic_id")
        raw_data, _headers = self.get_raw(
            f"/{course_id}/content/topics/{topic_identifier}",
            max_bytes=MAX_HTML_TOPIC_RESPONSE_BYTES,
        )
        try:
            data = json.loads(raw_data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ContentResponseShapeError() from None
        if not isinstance(data, dict):
            raise ContentResponseShapeError()

        # HTML topics have appeared as a direct string, Body.Text, or nested
        # RichText (Body.Text.Html).  Try the body first, then top-level HTML
        # fields when the body is absent/empty.
        body_shape_error = False
        try:
            html_content = _extract_rich_text(data.get("Body"))
        except ContentResponseShapeError:
            # Some Brightspace responses include an unrelated/legacy Body
            # object while exposing the usable markup at the top level.
            body_shape_error = True
            html_content = None

        top_level_shape_error = False
        if not html_content:
            for key in ("Html", "html", "HTML"):
                if key not in data:
                    continue
                try:
                    candidate = _extract_rich_text(data[key])
                except ContentResponseShapeError:
                    top_level_shape_error = True
                    continue
                if candidate:
                    html_content = candidate
                    break
                if html_content is None:
                    html_content = candidate

        if html_content is None:
            if body_shape_error or top_level_shape_error:
                raise ContentResponseShapeError()
            html_content = ""

        try:
            content_bytes = html_content.encode("utf-8")
        except UnicodeError:
            raise ContentResponseShapeError() from None

        # Filename derived from topic title, sanitized
        title = data.get("Title")
        if not isinstance(title, str) or not title:
            title = f"topic_{topic_identifier}"
        sanitized = _sanitize_filename(title)
        if not sanitized:
            sanitized = f"topic_{topic_identifier}"
        if not sanitized.endswith(".html"):
            sanitized = sanitized + ".html"
        return content_bytes, sanitized

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
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        return self._paginate_list(f"/{course_id}/dropbox/folders/", "Objects")

    def get_dropbox_folder_detail(self, org_unit_id: int, folder_id: int) -> dict[str, Any]:
        """GET full details for a specific dropbox folder, including attachments."""
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        dropbox_id = _require_positive_endpoint_id(folder_id, "folder_id")
        return self.get_json(f"/{course_id}/dropbox/folders/{dropbox_id}")

    def download_attachment(
        self, org_unit_id: int, folder_id: int, file_id: int
    ) -> tuple[bytes, str]:
        """Download a dropbox attachment file. Returns (bytes, filename)."""
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        dropbox_id = _require_positive_endpoint_id(folder_id, "folder_id")
        attachment_id = _require_positive_endpoint_id(file_id, "file_id")
        content, headers = self.get_raw(
            f"/{course_id}/dropbox/folders/{dropbox_id}/attachments/{attachment_id}"
        )
        return content, _extract_filename(headers) or f"attachment_{attachment_id}"

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
        course_id = _require_positive_endpoint_id(org_unit_id, "org_unit_id")
        dropbox_id = _require_positive_endpoint_id(folder_id, "folder_id")
        safe_filename = _require_safe_submission_filename(filename)
        safe_content_type = (
            _require_safe_content_type(content_type)
            if content_type is not None
            else None
        )

        import html
        import mimetypes
        import uuid

        # Build RichText description (required even if empty)
        text = description or f"Submitted via lighthouse-cli: {safe_filename}"
        rich_text = {"Text": text, "Html": f"<p>{html.escape(text)}</p>"}
        mime_type = safe_content_type or mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
        header_filename = safe_filename.replace('"', '\\"')

        # Build multipart/mixed body per D2L spec:
        # - Part 1: JSON with Content-Type application/json
        # - Part 2: File data with Content-Disposition form-data; name=""; filename="..."
        boundary = f"----lighthouseFormBoundary{uuid.uuid4().hex}"
        body_bytes = (
            f"--{boundary}\r\nContent-Type: application/json\r\n\r\n"
            f"{json.dumps(rich_text)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n"
            f'Content-Disposition: form-data; name=""; filename="{header_filename}"\r\n\r\n'
        ).encode()
        footer = f"\r\n--{boundary}--\r\n".encode()
        payload = body_bytes + file_bytes + footer
        resp = self._request(
            "POST",
            f"{API_LE}/{course_id}/dropbox/folders/{dropbox_id}/submissions/mysubmissions/",
            data=payload,
            headers={
                "Content-Type": f"multipart/mixed; boundary={boundary}",
                "Content-Length": str(len(payload)),
            },
            _skip_raise=True,
            _timeout=60,
        )
        # D2L redirects to login page when session is dead
        if resp.status_code in (301, 302, 303, 307, 308):
            raise SessionExpiredError(_session_expired_msg(f"HTTP {resp.status_code} redirect to login"), recovery=_SESSION_EXPIRED_RECOVERY)

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
        if resp.status_code == 429:
            raise NetworkError(
                "Submission request was rate limited; no retry was attempted."
            )
        if resp.status_code == 500:
            raise ValueError(
                "D2L API error (500): the remote server rejected the submission. "
                "This may indicate malformed request body or submission window restrictions."
            )
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            raise _safe_http_error(resp) from None
        except Exception:
            raise NetworkError("Network response validation failed.") from None

        try:
            result = resp.json()
        except (TypeError, ValueError):
            # A successful HTTP status does not guarantee that the server
            # returned a usable submission document.  Do not include the raw
            # body in the diagnostic: it may contain echoed form fields.
            raise SubmissionOutcomeUnknownError() from None
        if not isinstance(result, dict):
            raise SubmissionOutcomeUnknownError()
        return result


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _extract_filename(headers: dict[str, str]) -> str:
    """Parse a Content-Disposition filename, including RFC 5987 values.

    A quoted filename may contain semicolons, so splitting the header on
    ``;`` directly is unsafe.  ``filename*`` is preferred over the legacy
    ``filename`` parameter and is decoded from its declared charset after
    percent-decoding (the common form is ``UTF-8''...``).
    """
    cd = next(
        (value for key, value in headers.items() if key.lower() == "content-disposition"),
        "",
    )
    if not isinstance(cd, str) or not cd:
        return ""

    # Split parameters while preserving semicolons inside quoted values.  A
    # single-quoted value is not part of the HTTP grammar, but was accepted by
    # the old parser, so retain that compatibility when it is visibly paired.
    parts: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(cd):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char == '"':
            quote = char
        elif char == "'" and cd[start:index].rstrip().endswith("="):
            # Only treat an apostrophe immediately after ``=`` as a legacy
            # quote.  Apostrophes in RFC 5987's charset/lang separator remain
            # ordinary characters.
            closing = cd.find("'", index + 1)
            if closing >= 0:
                quote = char
        elif char == ";":
            parts.append(cd[start:index])
            start = index + 1
    parts.append(cd[start:])

    filename = ""
    extended_filename = ""
    for parameter in parts:
        name, separator, value = parameter.partition("=")
        if not separator or name.strip().lower() not in {"filename", "filename*"}:
            continue

        name = name.strip().lower()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
            if name == "filename":
                # Undo quoted-pair escaping from a quoted-string.
                value = value.replace('\\"', '"').replace("\\\\", "\\")

        if name == "filename*":
            charset, separator, encoded = value.partition("'")
            if not separator:
                continue
            _language, separator, encoded = encoded.partition("'")
            if not separator or not charset:
                continue
            try:
                decoded = unquote_to_bytes(encoded).decode(charset, errors="replace")
            except (LookupError, UnicodeError):
                continue
            if decoded:
                extended_filename = decoded
        elif value and not filename:
            filename = value

    return extended_filename or filename


def resolve_course_id(client: LighthouseClient, identifier: str) -> int:
    """Resolve a course identifier (int org-unit-id or partial name) to an int id.

    Tries numeric parse first, then falls back to substring match on course names.
    """
    # Try as numeric org-unit-id
    numeric_id = _positive_org_unit_id(identifier)
    if numeric_id is not None:
        return numeric_id

    # Search by name substring (case-insensitive)
    needle = identifier.strip().casefold()
    if not needle:
        raise CourseNotFoundError(
            "Course identifier cannot be empty. Run: lighthouse courses"
        )
    courses = get_enrolled_course_catalog(client)
    matches = [
        c for c in courses
        if isinstance(c, dict)
        and needle in (c.get("Name") if isinstance(c.get("Name"), str) else "").casefold()
    ]
    if len(matches) == 1:
        return int(matches[0]["OrgUnitId"])
    if len(matches) > 1:
        raise CourseNotFoundError(
            "Ambiguous match '" + identifier + "'. Multiple courses found:\n"
            + "\n".join(f"  {c['OrgUnitId']} – {c['Name']}" for c in matches)
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

    Returns the cookie dict (does NOT save to disk — caller must do that).
    """
    configured_port: object = (
        cdp_port
        if cdp_port is not None
        else os.getenv("LIGHTHOUSE_CDP_PORT", str(DEFAULT_CDP_PORT))
    )
    try:
        port = int(configured_port)
    except (TypeError, ValueError):
        raise NetworkError("CDP port must be an integer from 1 to 65535.") from None
    if not 1 <= port <= 65535:
        raise NetworkError("CDP port must be an integer from 1 to 65535.")

    # Strategy 1: try browser-harness CLI if available
    try:
        return _refresh_via_browser_harness(port)
    except (FileNotFoundError, _BrowserHarnessFallback):
        pass

    # Strategy 2: direct CDP WebSocket via Python websockets library
    return _refresh_via_cdp_websocket(port)


def _refresh_via_browser_harness(port: int) -> dict[str, str]:
    """Attempt cookie extraction using the browser-harness CLI tool."""
    import subprocess

    # Ask for every cookie and apply the SAME domain policy the session-jar
    # extractor uses — a browser may record d2l* cookies on .manipal.edu.
    # d2l_cookies_from_entries enforces dot-boundary domain matching, skips
    # malformed entries, and prefers host-only cookies (no shadowing).
    try:
        result = subprocess.run(
            ["browser-harness", "cookies", "--port", str(port)],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        # ``refresh_auth_from_browser`` uses this signal to fall back to the
        # direct CDP path when browser-harness is not installed.
        raise
    except Exception:
        raise _BrowserHarnessFallback(
            "Could not run the local browser cookie helper."
        ) from None
    if result.returncode != 0:
        raise _BrowserHarnessFallback("The local browser cookie helper failed.")

    try:
        entries = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise _BrowserHarnessFallback(
            "The local browser cookie helper returned invalid data."
        ) from None
    d2l_cookies = d2l_cookies_from_entries(entries)
    if not d2l_cookies:
        raise _BrowserHarnessFallback(
            "No usable Lighthouse cookies were found in the browser."
        )

    return d2l_cookies


def _refresh_via_cdp_websocket(port: int) -> dict[str, str]:
    """Direct CDP cookie extraction using Python websockets library."""
    # Get browser websocket URL.  The endpoint is local, but its response is
    # still untrusted input and network/parser failures must not escape with
    # adapter diagnostics or URL/query details.
    discovery_url = f"http://127.0.0.1:{port}/json/version"
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(discovery_url, timeout=10) as resp:
            status = getattr(resp, "status", getattr(resp, "code", None))
            if status in (301, 302, 303, 307, 308):
                raise NetworkError("Browser debugging endpoint returned a redirect.")
            if status is not None and status != 200:
                raise NetworkError("Browser debugging endpoint returned an invalid response.")
            final_url = (
                resp.geturl() if callable(getattr(resp, "geturl", None)) else discovery_url
            )
            if not isinstance(final_url, str) or not final_url:
                final_url = discovery_url
            _validate_cdp_discovery_url(final_url, expected_port=port)
            payload = json.loads(resp.read())
            ws_url = payload["webSocketDebuggerUrl"]
    except NetworkError:
        raise
    except Exception:
        raise NetworkError("Could not query the local browser debugging endpoint.") from None

    _validate_cdp_websocket_url(ws_url, expected_port=port)

    try:
        import asyncio
        return asyncio.run(_cdp_get_cookies_ws(ws_url))
    except ImportError:
        raise NetworkError(
            "Cannot extract cookies: neither browser-harness nor websockets library available. "
            f"Install with: pip install websockets\n"
            f"Or ensure Chrome is running with --remote-debugging-port={port}"
        )
    except NetworkError:
        raise
    except Exception:
        raise NetworkError("Could not extract cookies from the local browser.") from None


def _is_loopback_hostname(hostname: object) -> bool:
    """Return whether a hostname resolves syntactically to loopback."""
    if not isinstance(hostname, str):
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_cdp_discovery_url(url: object, *, expected_port: int) -> None:
    """Reject a redirected CDP discovery response outside local HTTP."""
    try:
        parsed = urlparse(url if isinstance(url, str) else "")
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        raise NetworkError("Browser debugging endpoint returned an invalid response.") from None
    if (
        parsed.scheme.lower() != "http"
        or not parsed.netloc
        or not _is_loopback_hostname(hostname)
        or port != expected_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/json/version"
    ):
        raise NetworkError("Browser debugging endpoint returned an invalid response.")


def _validate_cdp_websocket_url(
    ws_url: object,
    *,
    expected_port: int | None = None,
) -> None:
    """Reject CDP WebSocket URLs outside the configured local endpoint."""
    parsed = None
    try:
        parsed = urlparse(ws_url if isinstance(ws_url, str) else "")
        hostname = parsed.hostname
        websocket_port = parsed.port
    except (TypeError, ValueError):
        hostname = None
        websocket_port = None

    if (
        parsed is None
        or parsed.scheme.lower() not in {"ws", "wss"}
        or not parsed.netloc
        or not hostname
        or not parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise NetworkError("Browser returned an invalid CDP WebSocket URL.")

    if not _is_loopback_hostname(hostname):
        raise NetworkError("Refusing a non-loopback CDP WebSocket URL.")
    if websocket_port is None:
        raise NetworkError("Browser returned an invalid CDP WebSocket URL.")
    if expected_port is not None and websocket_port != expected_port:
        raise NetworkError("Browser returned a CDP WebSocket URL on an unexpected port.")


async def _cdp_get_cookies_ws(ws_url: str) -> dict[str, str]:
    """Extract cookies via CDP using the websockets Python library."""
    import asyncio
    import json as _json

    import websockets

    _validate_cdp_websocket_url(ws_url)
    async def exchange() -> Any:
        async with websockets.connect(
            ws_url,
            max_size=2**20,
            open_timeout=CDP_RESPONSE_TIMEOUT_SECONDS,
            close_timeout=CDP_RESPONSE_TIMEOUT_SECONDS,
        ) as ws:
            await ws.send(_json.dumps({"id": 1, "method": "Network.getAllCookies"}))
            response_text = await ws.recv()
            return _json.loads(response_text)

    try:
        resp = await asyncio.wait_for(
            exchange(),
            timeout=CDP_RESPONSE_TIMEOUT_SECONDS,
        )
    except NetworkError:
        raise
    except Exception:
        raise NetworkError("The local browser cookie connection failed.") from None

    if not isinstance(resp, dict):
        raise NetworkError("The local browser returned invalid cookie data.")
    result = resp.get("result")
    if not isinstance(result, dict):
        raise NetworkError("The local browser returned invalid cookie data.")
    d2l = d2l_cookies_from_entries(result.get("cookies"))
    if not d2l:
        raise NetworkError("No usable Lighthouse cookies were found in the browser.")
    return d2l
