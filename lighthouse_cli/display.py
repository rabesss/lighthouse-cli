"""Output and formatting helpers for lighthouse-cli.

All presentation logic lives here: table rendering (rich + plain-text
fallback), JSON output, error printing, and text truncation utilities.
"""

from __future__ import annotations

import json
import math
import re
import sys
from http import HTTPStatus
from typing import Any

import click


# Keep this message deliberately generic.  Click's own UsageError includes the
# invalid value and can therefore contain a URL, a pasted token, or another
# piece of input that should not be copied into a machine-readable result.
JSON_USAGE_ERROR = "Invalid command arguments. See --help."


def _has_json_option(args: list[str]) -> bool:
    """Return whether a command invocation requested machine-readable output."""
    for arg in args:
        # Click treats the first bare ``--`` as the end of option parsing.
        # Values after it are positional data, even when one happens to be
        # spelled ``--json``. Do not switch the output contract based on such
        # a value.
        if arg == "--":
            return False
        if arg == "--json" or arg.startswith("--json="):
            return True
    return False


class JsonOutputCommand(click.Command):
    """Click command that prefixes JSON parse errors with a safe JSON record.

    Click validates arguments before invoking the command callback.  That is
    normally useful, but it means a callback cannot honour ``--json`` when a
    required argument is missing or an option has an invalid value.  This
    small command class handles that one boundary: it emits a generic JSON
    object to stdout, then raises a sanitized UsageError without echoing
    invalid values. JSON usage failures use the repository's error code 1;
    human-only usage failures keep Click's conventional code 2.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        requested_json = _has_json_option(list(args))
        try:
            return super().parse_args(ctx, args)
        except click.UsageError:
            if requested_json and not ctx.resilient_parsing:
                output_json({"error": JSON_USAGE_ERROR})
            # Click's original UsageError includes the invalid value. Replace
            # it before rendering so a pasted password, token, or URL cannot
            # reach stderr.
            safe_error = click.UsageError(JSON_USAGE_ERROR, ctx=ctx)
            if requested_json:
                safe_error.exit_code = 1
            raise safe_error from None


# ---------------------------------------------------------------------------
# Rich table rendering (optional dependency)
# ---------------------------------------------------------------------------

# Cache rich imports at module level to avoid re-import per table render.
_RICH_CACHE: tuple[Any, Any, Any] | None = None
_RICH_CHECKED: bool = False


def _try_rich():
    """Import Rich types, returning ``(Table, Text, console)`` when available."""
    global _RICH_CACHE, _RICH_CHECKED
    if not _RICH_CHECKED:
        _RICH_CHECKED = True
        try:
            from rich.console import Console
            from rich.table import Table
            from rich.text import Text
            _RICH_CACHE = (Table, Text, Console())
        except ImportError:
            _RICH_CACHE = None
    return _RICH_CACHE


# ---------------------------------------------------------------------------
# Safe error formatting
# ---------------------------------------------------------------------------

# Labels returned by Brightspace (course names, titles, folder names, and
# similar display fields) are data, not diagnostics. Keep their output bounded
# and printable, and reject values that look like a serialized request or
# credential-bearing option before they reach either a terminal or JSON.
_DEFAULT_DISPLAY_TEXT_LENGTH = 512
_DISPLAY_SECRET_KEY_RE = re.compile(
    r"(?ix)(?<![a-z0-9])(?:"
    r"pass(?:word|wd|phrase)?(?:[\s_-]?value)?|secret|"
    r"token(?:[\s_-]?value)?|otp|totp|cookie(?:s|value)?|"
    r"saml[\s_-]?(?:response|request)|authorization|bearer|"
    r"d2l[\s_-]?same[\s_-]?site[\s_-]?canary[ab]?|api[\s_-]?canary|"
    r"s?ctx|sft|"
    r"flow[\s_-]?token|o?postparams|response[\s_-]?(?:body|text)|"
    r"session(?:[\s_-]?(?:val|value|token|id))?|access[\s_-]?token|"
    r"client[\s_-]?secret|x?[\s_-]?api[\s_-]?key"
    r")(?![a-z0-9])"
    r"\s*(?:[:=]|\bis\b|\bwas\b)\s*"
    r"(?:[\"'][^\"']*[\"']|[^\s,;}\]]+)"
)
_DISPLAY_SECRET_BARE_VALUE_RE = re.compile(
    # Keep the keyword case-insensitive but make the candidate value
    # lowercase-sensitive: ``Password Security`` and ``Token Ring`` are
    # ordinary labels, while ``password hunter2``/``token abc123`` are shaped
    # like a pasted credential.
    r"(?x)(?<![a-z0-9])(?i:password|passwd|passphrase|secret|token|"
    r"cookie(?:s|value)?|otp|totp|canary)\b\s+"
    r"(?:[a-z0-9._-]*\d[a-z0-9._-]*|[a-z][a-z0-9._-]{7,})"
    r"(?![a-z0-9])"
)
_DISPLAY_SECRET_UPPER_VALUE_RE = re.compile(
    # An all-caps or mixed-case-with-digits value immediately following a
    # sensitive keyword is shaped like a pasted credential. Keep ordinary
    # title case prose (``Password Security``/``Token Ring``) available.
    r"(?x)(?<![a-z0-9])(?i:password|passwd|passphrase|secret|token|"
    r"cookie(?:s|value)?|otp|totp|canary)\b\s+"
    r"(?:[A-Z0-9_-]{2,}|(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{3,})"
    r"(?![a-z0-9])"
)
_DISPLAY_BEARER_RE = re.compile(r"(?ix)(?<![a-z0-9])bearer\s+[^\s,;}\]]+")
_DISPLAY_QUOTED_SECRET_KEY_RE = re.compile(
    r"(?ix)[\"'](?:pass(?:word|wd|phrase)?(?:[\s_-]?value)?|secret|"
    r"token(?:[\s_-]?value)?|otp|totp|"
    r"cookie(?:s|value)?|authorization|bearer|flow[\s_-]?token|"
    r"d2l[\s_-]?same[\s_-]?site[\s_-]?canary[ab]?|api[\s_-]?canary|"
    r"s?ctx|sft|"
    r"response[\s_-]?(?:body|text)|session(?:[\s_-]?(?:val|value|token|id))?|"
    r"access[\s_-]?token|client[\s_-]?secret|x?[\s_-]?api[\s_-]?key)"
    r"[\"']\s*:"
)
_DISPLAY_SECRET_FLAG_RE = re.compile(
    r"(?ix)(?<![a-z0-9])--(?:pass(?:word)?|password|passphrase|secret|token|"
    r"cookie|otp|totp|saml(?:-?response)?|api-key|access-token)(?:=|\s+)"
)
_DISPLAY_SECRET_KEY_ONLY_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:flow[\s_-]?token|o?postparams|"
    r"cookie(?:s|value)|session[\s_-]?(?:val|value|token|id)|"
    r"d2l[\s_-]?same[\s_-]?site[\s_-]?canary[ab]?|api[\s_-]?canary|"
    r"s?ctx|sft|"
    r"access[\s_-]?token|client[\s_-]?secret|api[\s_-]?key|"
    r"x[\s_-]?api[\s_-]?key|d2l(?:secure)?session(?:val|value)|"
    r"response[\s_-]?(?:body|text)|saml[\s_-]?(?:response|request)|"
    r"authorization|bearer)"
    r"(?![a-z0-9])"
)
_DISPLAY_BARE_SECRET_RE = re.compile(
    r"(?i)^(?:pass(?:word|wd|phrase)?(?:[\s_-]?value)?|secret|"
    r"token(?:[\s_-]?value)?|otp|totp|cookie|"
    r"cookies|authorization|bearer|session|sessionval|sessionvalue|"
    r"sessiontoken|access_token|accesstoken|client_secret|clientsecret|"
    r"api_key|apikey|x-api-key)$"
)
_DISPLAY_OBJECT_SHAPE_RE = re.compile(r"(?s)(?:\{.*?:.*\}|\[.*?:.*\])")
_DISPLAY_SENSITIVE_QUERY_RE = re.compile(
    r"(?ix)[?&](?:password|passwd|passphrase|secret|token|otp|totp|cookie(?:s|value)?|"
    r"session(?:val(?:ue)?)?|access[_-]?token|client[_-]?secret|api[_-]?key|"
    r"d2l[_-]?same[_-]?site[_-]?canary[ab]?|api[_-]?canary|s?ctx|sft)="
)
_DISPLAY_OBJECT_PREFIXES = ("{", "[")


def safe_display_text(
    value: Any,
    fallback: str = "",
    *,
    max_len: int = _DEFAULT_DISPLAY_TEXT_LENGTH,
) -> str:
    """Return a bounded printable scalar safe for labels and human output.

    The fallback is intentionally fixed by each caller (for example,
    ``Course-123`` or ``Unknown folder``). Values that are objects, serialized
    JSON, control-bearing strings, URLs with sensitive query keys, credential
    fields, or secret-bearing CLI fragments become that fallback. Ordinary
    prose, including non-ASCII course names, is preserved after whitespace is
    compacted.
    """
    if not isinstance(value, str):
        return fallback
    candidate = value.strip()
    if (
        not candidate
        or max_len <= 0
        or len(candidate) > max_len
        or not candidate.isprintable()
        or candidate.startswith(_DISPLAY_OBJECT_PREFIXES)
        or _DISPLAY_OBJECT_SHAPE_RE.search(candidate)
        or _DISPLAY_QUOTED_SECRET_KEY_RE.search(candidate)
        or _DISPLAY_SECRET_KEY_RE.search(candidate)
        or _DISPLAY_SECRET_BARE_VALUE_RE.search(candidate)
        or _DISPLAY_SECRET_UPPER_VALUE_RE.search(candidate)
        or _DISPLAY_BEARER_RE.search(candidate)
        or _DISPLAY_SECRET_KEY_ONLY_RE.search(candidate)
        or _DISPLAY_BARE_SECRET_RE.fullmatch(candidate)
        or _DISPLAY_SECRET_FLAG_RE.search(candidate)
        or _DISPLAY_SENSITIVE_QUERY_RE.search(candidate)
    ):
        return fallback
    compact = " ".join(candidate.split())
    return compact if compact and len(compact) <= max_len else fallback

_SECRET_FIELD_RE = re.compile(
    r"(?i)[\"']?\b(?:password|passwd|passphrase|secret|token|cookie|cookies|"
    r"samlresponse|otp|totp|canary|authorization|bearer|pass|"
    r"api[\s_-]?key|x[\s_-]?api[\s_-]?key)\b[\"']?"
    r"\s*(?:(?:[:=]\s*)|(?:\s+))"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_SECRET_SHAPED_VALUE_RE = re.compile(
    r"(?i)\b(?:[a-z][a-z0-9]*(?:password|passwd|passphrase|secret|token|"
    r"cookie|cookies|samlresponse|otp|totp|canary|session)[a-z0-9_-]*|"
    r"(?:password|passwd|passphrase|secret|token|cookie|cookies|samlresponse|"
    r"otp|totp|canary|session)[_-](?:sentinel|value|secret|token))\b"
)
_UNSAFE_FIELD_RE = re.compile(
    r"(?ix)"
    r"(?:"
    r"response[\s_-]?(?:body|text)|"
    r"flow[\s_-]?token|"
    r"opostparams|"
    r"cookie(?:s|value)?|"
    r"session(?:val|value|token)|"
    r"access[\s_-]?token|"
    r"client[\s_-]?secret|"
    r"samlresponse|"
    r"authorization|bearer"
    r")"
    r"(?:\s*(?:[:=]|is|was)\s*|\s+)"
    r"(?:[\"'][^\"']*[\"']|[^\s,;}\]]+)"
)
_HTTP_STATUS_PATTERNS = (
    re.compile(r"(?i)\bHTTP(?:/\d(?:\.\d+)?)?\s*([1-5]\d{2})\b"),
    re.compile(r"(?i)\bstatus(?:[_ ]code)?\s*[:=]?\s*([1-5]\d{2})\b"),
    re.compile(r"(?i)\b([1-5]\d{2})\s+(?:client|server)\s+error\b"),
    re.compile(r"(?i)\b(?:api|server|client)\s+error\s*\(?\s*([1-5]\d{2})\b"),
)
# Recovery text is untrusted exception content.  Keep only fixed, argument-free
# commands that are safe to suggest.  In particular, never copy options such
# as ``--pass`` or values supplied after them into normal CLI output.
_SAFE_RECOVERY_COMMANDS = (
    "lighthouse config courses",
    "lighthouse auth login",
    "lighthouse auth verify",
    "lighthouse auth status",
    "lighthouse semesters",
    "lighthouse courses",
    "lighthouse assignments",
)
_RECOVERY_HINT_RE = re.compile(
    r"(?im)\b(?:run|try|use)\s*:\s*(?P<command>lighthouse(?:\s+[a-z0-9_-]+){0,5})"
)
_RECOVERY_LINE_RE = re.compile(
    r"(?im)\b(?:run|try|use)\s*:\s*lighthouse\b[^\r\n]*"
)
_TRANSPORT_ERROR_NAMES = frozenset(
    {
        "connectionerror",
        "connecttimeout",
        "networkerror",
        "proxyerror",
        "readtimeout",
        "requestexception",
        "timeout",
        "timeouterror",
    }
)
_TRANSPORT_MARKERS = (
    "request failed",
    "network error",
    "transport error",
    "connection refused",
    "connection reset",
    "connectionerror",
    "connection pool",
    "httpsconnectionpool",
    "max retries exceeded",
    "name resolution",
    "timed out",
    "timeout while",
    "name or service not known",
    "sslerror",
)


def _status_code(error: BaseException, raw: str) -> int | None:
    """Extract an HTTP status without inspecting or returning response text."""
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    for pattern in _HTTP_STATUS_PATTERNS:
        match = pattern.search(raw)
        if match:
            return int(match.group(1))
    return None


def _safe_recovery_hint(raw: str) -> str:
    """Keep only short, local CLI recovery commands from an exception."""
    for match in _RECOVERY_HINT_RE.finditer(raw):
        command_text = match.group("command").lower().strip()
        for command in _SAFE_RECOVERY_COMMANDS:
            # A known command may be followed by arbitrary options or pasted
            # values.  We deliberately return only the fixed prefix.
            if command_text == command or command_text.startswith(f"{command} "):
                return f"Run: {command}"
    return ""


def _safe_local_message(raw: str) -> str | None:
    """Recognize short local validation messages without echoing secrets."""
    normalized = re.sub(r"\s+", " ", raw.strip())
    lowered = normalized.casefold()
    if lowered in {
        "schema down",
        "fetch failed",
        "single failed",
        "grades unavailable",
        "att fail",
    }:
        return normalized
    if lowered.startswith("no cookies found"):
        return "No cookies found. Run: lighthouse auth login"
    if lowered.startswith("no trustworthy local semester configuration"):
        return (
            "No trustworthy local semester configuration found. Use an explicit "
            "COURSE_ID or run: lighthouse config courses"
        )
    if lowered.startswith("output directory contains a symlinked path"):
        return "Output directory contains a symlinked path. Choose a real directory."
    if lowered.startswith("output directory is not a directory"):
        return "Output directory is not a directory. Choose a directory path."
    if lowered.startswith("unable to validate output directory"):
        return "Unable to validate output directory. Choose a real directory."
    if lowered.startswith("--attachment requires --assignment"):
        return "--attachment requires --assignment"
    if lowered in {
        "--assignment must be a positive integer",
        "--attachment must be a positive integer",
    }:
        return normalized
    if lowered.startswith("course_id is required when using --assignment"):
        return "COURSE_ID is required when using --assignment or --attachment"
    if lowered.startswith("--dry-run cannot be used with --assignment"):
        return "--dry-run cannot be used with --assignment"
    if lowered.startswith("--semester and --also are only supported"):
        return "--semester and --also are only supported when COURSE_ID is omitted"
    if lowered in {"permission denied.", "file or resource not found."}:
        return normalized
    if lowered.startswith("no course config found"):
        return "No course config found. Run: lighthouse config courses"
    if lowered.startswith("no semester matching"):
        return "No matching semester. Run: lighthouse semesters"
    if lowered.startswith("no semesters found"):
        return "No semesters found. Run: lighthouse semesters"
    if lowered.startswith("no courses to "):
        return "No courses available for this operation."
    if re.match(
        r"(?is)^course\b.*\bnot found in your enrollments\b",
        normalized,
    ):
        return "Course not found in your enrollments. Run: lighthouse courses"
    if re.match(r"(?is)^course\b.*\bnot found\b", normalized):
        return "Course not found. Run: lighthouse courses"
    if "no tracked courses mapped to semester" in lowered:
        return (
            "No tracked courses mapped to the requested semester. "
            "Run: lighthouse config courses"
        )
    if lowered.startswith("dropbox folder") and " not found" in lowered:
        return "Dropbox folder not found. Run: lighthouse assignments"
    if lowered.startswith("folder ") and " not found" in lowered:
        return "Dropbox folder not found. Run: lighthouse assignments"
    if lowered.startswith("requested assignment folder was not found"):
        return "Assignment folder not found. Run: lighthouse assignments"
    if lowered.startswith("permission denied to submit"):
        return "Permission denied to submit. Check your enrollment and submission rights."
    if lowered.startswith("refusing to submit without --yes"):
        return (
            "Refusing to submit without --yes in non-interactive mode. "
            "Use --yes flag to confirm."
        )
    if lowered.startswith("could not read file") or lowered.startswith("unable to read file"):
        return "Could not read file. Check the path and permissions."
    if lowered.startswith("submission cancelled"):
        return "Submission cancelled."
    if lowered.startswith("submission outcome is unknown"):
        return (
            "Submission outcome is unknown because the API returned an unsupported "
            "result shape. Verify the assignment status before trying again."
        )
    if lowered.startswith("submission request was rate limited"):
        return "Rate limited. No retry was attempted."
    if "multiple folders found" in lowered:
        return "Ambiguous folder match. Use a numeric FolderId for an exact match."
    if lowered.startswith("ambiguous match") or lowered.startswith("multiple courses found"):
        return "Ambiguous course match. Use a numeric OrgUnitId for an exact match."
    if lowered.startswith("course ") and " is not in your tracked courses" in lowered:
        return "Course is not in your tracked courses."
    if lowered.startswith("file not found"):
        return "File not found. Check the path and try again."
    if lowered.startswith("failed attachment"):
        return "Assignment attachment download failed."
    if (
        lowered.startswith("assignment attachments have an invalid response shape")
        or lowered.startswith("assignment folders have an invalid response shape")
        or lowered.startswith("assignment response has an invalid shape")
    ):
        return "Assignment response has an invalid shape."
    if lowered.startswith("assignment record has an invalid identifier"):
        return "Assignment record has an invalid identifier."
    if lowered == "invalid semester response.":
        return "Semester response has an invalid shape."
    if lowered == "invalid course enrollment response.":
        return "Course enrollment response has an invalid shape."
    if lowered == "invalid content response.":
        return "Content response has an invalid shape."
    if lowered == "invalid quiz response.":
        return "Quiz response has an invalid shape."
    if lowered == "resolved semester has an invalid identifier.":
        return "Resolved semester has an invalid identifier."
    if "course destination is a symlink" in lowered:
        return "Course destination is a symlink; no files were written."
    if "course destination escapes the output root" in lowered:
        return "Course destination escapes the output root; no files were written."
    if "course destination is not a directory" in lowered:
        return "Course destination is not a directory; no files were written."
    if "course manifest is a symlinked path" in lowered:
        return "Course manifest is a symlinked path; no files were written."
    if "topic path contains a symlinked course directory" in lowered:
        return "Topic path contains a symlinked course directory."
    if "topic filename is a symlink" in lowered:
        return "Topic filename is a symlink; no files were written."
    if "topic file path escapes the course root" in lowered:
        return "Topic file path escapes the course root (symlink or traversal)."
    if "assignment attachment path is symlinked" in lowered:
        return "Assignment attachment path is symlinked or escapes the course root."
    if "assignment folder is a symlink" in lowered:
        return "Assignment folder is a symlink or escapes the course root."
    return None


def _status_message(status: int) -> str:
    """Map an HTTP status to a short category suitable for a user."""
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = "HTTP error"
    if status == 401:
        return "Session expired"
    if status == 403:
        return "Permission denied"
    if status == 404:
        return "Not found"
    if status == 429:
        return "Rate limited"
    if status >= 500:
        return "Remote server error"
    return phrase


def format_user_error(error_value: BaseException | str) -> str:
    """Return a concise, secret-safe diagnostic for human and JSON output.

    Transport exceptions often include a full URL, query parameters, retry
    internals, or a response body.  Those details are useful in a debug log
    but unsafe in normal CLI output.  This formatter keeps the status and the
    broad category, plus a small local recovery hint when one is present.
    """
    if isinstance(error_value, BaseException):
        error = error_value
        raw = str(error)
        name = error.__class__.__name__.lower()
    else:
        error = RuntimeError(str(error_value))
        raw = str(error_value)
        name = ""

    lowered = raw.lower()
    hint = _safe_recovery_hint(raw)

    # If the exception is only a recovery instruction, return the fixed
    # allowlisted command directly; never add an arbitrary argument-bearing
    # command or an unnecessary generic prefix.
    if hint and _RECOVERY_LINE_RE.fullmatch(raw.strip()):
        return hint

    # The auth exception has a detailed __str__ containing nested recovery and
    # transport text.  Its category and fixed recovery command are enough.
    if name in {"sessionexpirederror", "sessionexpired"} or "session expired" in lowered:
        return "Session expired. Run: lighthouse auth login"

    # Submission rate limiting is deliberately not retried by the API client.
    # Preserve that operationally important distinction when the typed
    # ``NetworkError`` reaches the formatter, without copying response text.
    if lowered.startswith("submission request was rate limited"):
        return "Rate limited. No retry was attempted."

    status = _status_code(error, raw)
    if status is not None:
        category = _status_message(status)
        result = f"{category} (HTTP {status})."
        return f"{result} {hint}" if hint else result

    if name in _TRANSPORT_ERROR_NAMES or "requests.exceptions" in str(error.__class__.__module__).lower():
        category = "Network error"
        if name and name not in {"requestexception", "networkerror"}:
            category = f"{category} ({error.__class__.__name__})"
        result = f"{category}. Check your connection and try again."
        return f"{result} {hint}" if hint else result

    if any(marker in lowered for marker in _TRANSPORT_MARKERS):
        result = "Network error. Check your connection and try again."
        return f"{result} {hint}" if hint else result

    # A secret-bearing field can be nested in JSON-ish headers, camelCase
    # exception text, or a bare ``password hunter2`` fragment.  Once detected,
    # do not return any portion of the original string.
    if _UNSAFE_FIELD_RE.search(raw) or _SECRET_FIELD_RE.search(raw) or _SECRET_SHAPED_VALUE_RE.search(raw):
        return f"Command failed. {hint}".strip()

    if (safe_message := _safe_local_message(raw)) is not None:
        # The local template itself is fixed and contains no caller-provided
        # identifiers. Never substitute raw text back into the message.
        return f"{safe_message} {hint}".strip() if hint and "run:" not in safe_message.casefold() else safe_message

    if name == "permissionerror":
        result = "Permission denied."
        return f"{result} {hint}" if hint else result
    if name == "filenotfounderror":
        result = "File or resource not found."
        return f"{result} {hint}" if hint else result

    # Unknown upstream exception text is intentionally not echoed.  Only the
    # categorized branches and the explicit local allowlist above retain
    # detailed diagnostics.
    return f"Command failed. {hint}".strip()


def command_error(
    error_value: BaseException | str,
    *,
    json_output: bool = False,
    payload: dict[str, Any] | None = None,
    exit_code: int = 1,
) -> int:
    """Print a safe diagnostic and, optionally, one structured error object."""
    safe_message = format_user_error(error_value)
    print(f"Error: {safe_message}", file=sys.stderr)
    if json_output:
        result = dict(payload or {})
        result["error"] = safe_message
        output_json(result)
    return exit_code


def print_table(columns: list[str], rows: list[list[str]], title: str = "") -> None:
    """Print a table using rich if available, else plain aligned text."""
    if rich := _try_rich():
        Table, Text, console = rich
        table = Table(title=Text(title), show_lines=False, pad_edge=False)
        for col in columns:
            table.add_column(Text(col), overflow="ellipsis")
        for row in rows:
            table.add_row(*(Text(cell) for cell in row))
        console.print(table)
        return

    # Plain-text fallback: columnar alignment
    widths = [max([len(c), *(len(row[i]) for row in rows)]) for i, c in enumerate(columns)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    if title:
        print(f"\n{title}")
    print(fmt.format(*columns) + "\n" + fmt.format(*["-" * w for w in widths])
          + "\n" + "\n".join(fmt.format(*row) for row in rows))


def output_json(data: Any) -> None:
    """Print raw JSON to stdout (for --json mode / agent consumption)."""
    def replace_non_finite(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {key: replace_non_finite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_non_finite(item) for item in value]
        if isinstance(value, tuple):
            return [replace_non_finite(item) for item in value]
        return value

    # ``allow_nan=False`` is intentional: NaN/Infinity are not JSON and make
    # the CLI unusable for strict parsers.  Brightspace occasionally returns a
    # non-finite numeric field; represent that field as JSON null.
    print(json.dumps(replace_non_finite(data), indent=2, ensure_ascii=False, allow_nan=False))


def error(
    msg: BaseException | str,
    *,
    json_output: bool = False,
    payload: dict[str, Any] | None = None,
    exit_code: int = 1,
) -> int:
    """Print a safe error, optionally paired with one JSON error document.

    Existing callers can keep using ``error(message)``.  Callers that know
    their command's JSON schema can pass ``json_output=True`` and a payload
    containing its empty result fields.
    """
    return command_error(
        msg,
        json_output=json_output,
        payload=payload,
        exit_code=exit_code,
    )


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
