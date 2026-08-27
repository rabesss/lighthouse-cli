"""Click CLI definitions for lighthouse-cli.

Defines the command group and all subcommands with their options/arguments.
Delegates actual logic to lighthouse_cli.commands.

Leaf commands that advertise ``--json`` emit one command-specific JSON
document on stdout; diagnostics remain on stderr.
"""

from __future__ import annotations

import click

from . import __version__
from .auth import cmd_auth_login, cmd_auth_mfa_methods, cmd_auth_refresh, cmd_auth_verify
from .commands import (
    cmd_announcements,
    cmd_assignments,
    cmd_auth_status,
    cmd_calendar,
    cmd_content,
    cmd_courses,
    cmd_download,
    cmd_grades,
    cmd_quiz_detail,
    cmd_quizzes,
    cmd_semesters,
    cmd_submit,
    cmd_sync,
)
from .course_config import cmd_config_courses
from .display import JsonOutputCommand

# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=__version__, prog_name="lighthouse-cli")
def cli() -> None:
    """lighthouse-cli – CLI for D2L Brightspace LMS at lighthouse.manipal.edu.

    Read course data and manage local downloads through the D2L REST API.
    Run 'lighthouse auth login' first to set up your session. Commands with
    ``--json`` emit one command-specific JSON value on stdout;
    the option is per-command and diagnostics go to stderr. ``submit`` is the
    only command that writes remotely. ``download`` and ``sync`` write local
    files and manifests across a course or semester scope.
    """


# ---------------------------------------------------------------------------
# Auth subgroup
# ---------------------------------------------------------------------------

@cli.group()
def auth() -> None:
    """Manage authentication (session cookies)."""


@auth.command("status", cls=JsonOutputCommand)
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def auth_status(json_output: bool) -> None:
    """Check if stored cookies are still valid."""
    raise SystemExit(cmd_auth_status(json_output))


@auth.command("refresh", cls=JsonOutputCommand)
@click.option(
    "--cdp-port",
    default=None,
    help="Loopback Chrome DevTools Protocol port (default: LIGHTHOUSE_CDP_PORT or 34165).",
)
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def auth_refresh(
    cdp_port: str | None,
    json_output: bool,
) -> None:
    """Refresh cookies from a signed-in browser through loopback CDP.

    The browser must already be running with a CDP port and signed in to
    lighthouse.manipal.edu. Use ``auth login`` for the pure-HTTP SSO flow.
    """
    raise SystemExit(cmd_auth_refresh(
        cdp_port=cdp_port,
        json_output=json_output,
    ))


@auth.command("login", cls=JsonOutputCommand)
@click.option("--user", "username", default=None, help="Username (email) for Microsoft SSO.")
@click.option("--pass", "password", default=None, help="Password for Microsoft SSO.")
@click.option("--totp", "totp", default=None, help="2FA code. Omit for two-phase interactive login.")
@click.option(
    "--mfa-method",
    type=click.Choice(["auto", "sms", "app", "call", "push", "choose"]),
    default=None,
    help="MFA: auto (tenant default), sms, call (voice), app (TOTP), push (approve), or choose.",
)
@click.option(
    "--save-credentials",
    "save_credentials",
    is_flag=True,
    default=False,
    help="Save email/password encrypted for future logins (session cookies still expire ~5 days).",
)
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def auth_login(
    username: str | None,
    password: str | None,
    totp: str | None,
    mfa_method: str | None,
    save_credentials: bool,
    json_output: bool,
) -> None:
    """Log in to D2L via Microsoft SSO (pure HTTP, no browser required).

    Credentials can be provided via:
      --user/--pass flags
      LIGHTHOUSE_USERNAME/PASSWORD env vars
      Interactive prompts (if TTY)

    Two-phase interactive login (TTY): username/password first, then verification
    code after Microsoft accepts your password.

    MFA: --mfa-method auto (default), sms, call, app, push, or choose (pick from
    a list). Text codes may arrive via SMS or WhatsApp depending on Microsoft;
    the CLI cannot select the delivery channel. Voice calls are approved by
    answering and pressing #; push is approved in Microsoft Authenticator.

    Discover what the account supports first: lighthouse auth mfa-methods

    Session cookies typically expire after ~5 days (MAHE tenant policy); re-run
    login when auth status fails. --save-credentials stores email/password only.

    2FA (SMS/WhatsApp): two-step (recommended for agents and scripts):

      lighthouse auth login --mfa-method sms
      lighthouse auth verify <code>

    Do not run login twice — each login sends a new code. In a TTY, login alone
    prompts for the code after it is sent.

    On success, D2L session cookies are saved to
    ~/.config/lighthouse-cli/cookies.json.

    Use --save-credentials to store email/password encrypted (requires:
    pip install lighthouse-cli[credentials]). You still re-authenticate when
    cookies expire.
    """
    raise SystemExit(cmd_auth_login(
        username=username,
        password=password,
        totp_code=totp,
        totp_stdin=(totp == "-"),
        save_credentials=save_credentials,
        json_output=json_output,
        mfa_method=mfa_method,
    ))


@auth.command("verify", cls=JsonOutputCommand)
@click.argument("code")
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def auth_verify(code: str, json_output: bool) -> None:
    """Complete login with the verification code from the current ``auth login`` session.

    Use after ``auth login`` prints "Verification code sent." Do not run a second
    ``auth login`` — that sends a new code and invalidates the previous one.
    """
    raise SystemExit(cmd_auth_verify(code, json_output=json_output))


@auth.command("mfa-methods", cls=JsonOutputCommand)
@click.option("--user", "username", default=None, help="Username (email) for Microsoft SSO.")
@click.option("--pass", "password", default=None, help="Password for Microsoft SSO.")
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def auth_mfa_methods(
    username: str | None,
    password: str | None,
    json_output: bool,
) -> None:
    """List the account's MFA methods without triggering a challenge.

    Performs a real sign-in through the post-password stage and may advance
    KMSI/session state, but stops before BeginAuth. Reports OneWaySMS (sms),
    TwoWayVoice* (call), PhoneAppOTP (app), and PhoneAppNotification (push).
    """
    raise SystemExit(cmd_auth_mfa_methods(
        username=username,
        password=password,
        json_output=json_output,
    ))


# ---------------------------------------------------------------------------
# Config subgroup
# ---------------------------------------------------------------------------

@cli.group()
def config() -> None:
    """Manage configuration (course tracking, semester mapping)."""


@config.command("courses", cls=JsonOutputCommand)
@click.option("--add", default=None, help="Track a course by ID or name.")
@click.option("--remove", default=None, help="Stop tracking a course by ID.")
@click.option("-s", "--semester", default=None, help="Semester label to assign (used with --add).")
@click.option("--list", "list_courses", is_flag=True, default=False, help="Show tracked courses.")
@click.option("--reset", is_flag=True, default=False, help="Clear local course tracking only; keep downloads and LMS data.")
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def config_courses(add: str | None, remove: str | None, semester: str | None, list_courses: bool, reset: bool, json_output: bool) -> None:
    """Manage course tracking and semester mapping.

    Without flags, runs interactive setup: shows all enrolled courses
    and lets you pick which to track and assign semester labels.
    ``--reset`` clears this local mapping only. It does not delete downloads or
    alter courses in the LMS. Use ``--json`` for this command's list/result.

    \b
    Examples:
      lighthouse config courses                    # Interactive setup
      lighthouse config courses --list             # Show tracked courses
      lighthouse config courses --add 44347 -s "Sem IV"  # Track one course
      lighthouse config courses --remove 44347     # Stop tracking a course
      lighthouse config courses --reset            # Clear all tracking
    """
    raise SystemExit(cmd_config_courses(
        add=add,
        remove=remove,
        semester=semester,
        list_courses=list_courses,
        reset=reset,
        json_output=json_output,
    ))


# ---------------------------------------------------------------------------
# Data commands
# ---------------------------------------------------------------------------

@cli.command(cls=JsonOutputCommand)
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def semesters(json_output: bool) -> None:
    """List all semesters."""
    raise SystemExit(cmd_semesters(json_output))


@cli.command(cls=JsonOutputCommand)
@click.option("-s", "--semester", default=None, help="Filter by semester label (requires course tracking config).")
@click.option("--tracked", "tracked_only", is_flag=True, default=False, help="Show only tracked courses.")
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def courses(semester: str | None, tracked_only: bool, json_output: bool) -> None:
    """List all courses."""
    raise SystemExit(cmd_courses(semester=semester, tracked_only=tracked_only, json_output=json_output))


@cli.command("content", cls=JsonOutputCommand)
@click.argument("course_id")
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def content(course_id: str, json_output: bool) -> None:
    """Show content tree for a course (modules > submodules > topics)."""
    raise SystemExit(cmd_content(course_id, json_output))


@cli.command("download", cls=JsonOutputCommand)
@click.argument("course_id", required=False)
@click.option("-o", "--output-dir", default=None, help="Custom download directory.")
@click.option("--dry-run", is_flag=True, default=False, help="Preview downloads without changing disk.")
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
@click.option("--force", is_flag=True, default=False, help="Replace local manifest metadata and re-download every file.")
@click.option("--types", default="file", help="Comma-separated content types to download (file,html). Default: file.")
@click.option("-s", "--semester", default=None, help="Filter to a specific semester (requires tracking config).")
@click.option("--also", "also_courses", multiple=True, help="Additional course(s) to include by name or ID.")
@click.option("--include-assignments", is_flag=True, default=False, help="Also download assignment attachments.")
@click.option(
    "--assignment",
    "assignment_id",
    default=None,
    type=click.IntRange(min=1),
    help="Download a specific assignment folder's attachment(s).",
)
@click.option(
    "--attachment",
    "attachment_id",
    default=None,
    type=click.IntRange(min=1),
    help="Download a specific attachment from an assignment folder.",
)
def download(
    course_id: str | None,
    output_dir: str | None,
    dry_run: bool,
    json_output: bool,
    force: bool,
    types: str,
    semester: str | None,
    also_courses: tuple[str, ...],
    include_assignments: bool = False,
    assignment_id: int | None = None,
    attachment_id: int | None = None,
) -> None:
    """LOCAL WRITE: download files from a course or semester scope.

    If COURSE_ID is given, download that course. Without COURSE_ID,
    downloads the latest configured semester. Without trustworthy course
    configuration, the command fails closed before writing local files.

    This command writes local files and a manifest under --output-dir. It does
    not change anything in the LMS. --dry-run prints the plan without writing.
    --force replaces local manifest metadata and fetches every file.

    Scope options:
      --semester  Filter the omitted-COURSE_ID scope to a semester (by name or ID)
      --also      Add additional course(s) to that omitted-COURSE_ID scope

    Assignment options:
      --include-assignments  Download attachments from all dropbox folders
      --assignment           Download a specific dropbox folder
      --attachment           Download a specific attachment (requires --assignment)
    """
    raise SystemExit(
        cmd_download(
            course_id,
            output_dir=output_dir,
            dry_run=dry_run,
            json_output=json_output,
            force=force,
            types=types,
            semester=semester,
            also_courses=list(also_courses),
            include_assignments=include_assignments,
            assignment_id=assignment_id,
            attachment_id=attachment_id,
        )
    )


@cli.command("sync", cls=JsonOutputCommand)
@click.argument("course_id", required=False)
@click.option("-o", "--output-dir", default=None, help="Custom download directory.")
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
@click.option("--force", is_flag=True, default=False, help="Replace local manifest metadata and re-download every file.")
@click.option("--types", default="file", help="Comma-separated content types to sync (file,html). Default: file.")
@click.option("-s", "--semester", default=None, help="Filter to a specific semester (requires tracking config).")
@click.option("--also", "also_courses", multiple=True, help="Additional course(s) to include by name or ID.")
@click.option("--include-assignments", is_flag=True, default=False, help="Also sync assignment attachments.")
def sync(
    course_id: str | None,
    output_dir: str | None,
    json_output: bool,
    force: bool,
    types: str,
    semester: str | None,
    also_courses: tuple[str, ...],
    include_assignments: bool = False,
) -> None:
    """LOCAL WRITE: incrementally sync new or changed files.

    Uses .lighthouse.json manifest to skip unchanged topics.
    Without COURSE_ID, syncs the latest configured semester. Without
    trustworthy course configuration, the command fails closed before writing
    local files.

    This command writes local files and manifests. It does not change anything
    in the LMS. --force replaces local manifest metadata and fetches every
    file. Use --json for this command's structured result.

    Scope options:
      --semester  Filter the omitted-COURSE_ID scope to a semester (by name or ID)
      --also      Add additional course(s) to that omitted-COURSE_ID scope
    """
    raise SystemExit(
        cmd_sync(
            course_id=course_id,
            output_dir=output_dir,
            json_output=json_output,
            force=force,
            types=types,
            semester=semester,
            also_courses=list(also_courses),
            include_assignments=include_assignments,
        )
    )


@cli.command(cls=JsonOutputCommand)
@click.argument("course_id", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def grades(course_id: str | None, json_output: bool) -> None:
    """Show grades. If COURSE_ID omitted, show all courses."""
    raise SystemExit(cmd_grades(course_id=course_id, json_output=json_output))


@cli.command(cls=JsonOutputCommand)
@click.argument("course_id", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def announcements(course_id: str | None, json_output: bool) -> None:
    """Show announcements. If COURSE_ID omitted, show all courses."""
    raise SystemExit(cmd_announcements(course_id=course_id, json_output=json_output))


@cli.command(cls=JsonOutputCommand)
@click.argument("course_id", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def calendar(course_id: str | None, json_output: bool) -> None:
    """Show calendar events. If COURSE_ID omitted, show all courses."""
    raise SystemExit(cmd_calendar(course_id=course_id, json_output=json_output))


@cli.command(cls=JsonOutputCommand)
@click.argument("course_id", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def quizzes(course_id: str | None, json_output: bool) -> None:
    """Show quizzes. If COURSE_ID omitted, show all courses."""
    raise SystemExit(cmd_quizzes(course_id=course_id, json_output=json_output))


@cli.command("quiz", cls=JsonOutputCommand)
@click.argument("course_id")
@click.argument("quiz_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def quiz_detail(course_id: str, quiz_id: int, json_output: bool) -> None:
    """Show detailed info for a specific quiz.

    Shows quiz settings, time limits, attempt rules, dates, etc.
    Note: quiz questions and past attempts are not accessible via the
    learner API. Use the browser link to view those.
    """
    raise SystemExit(cmd_quiz_detail(course_id, quiz_id, json_output))


@cli.command("assignments", cls=JsonOutputCommand)
@click.argument("course_id", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def assignments(course_id: str | None, json_output: bool) -> None:
    """Show dropbox folders (assignments) for a course.

    Lists all assignment dropbox folders with name, due date, and attachment
    count. Use COURSE_ID to show assignments for a specific course, or omit
    to show assignments for all enrolled courses (parallel fetch).

    Output:
      - Human: table with ID, Name, Due Date, Attachments columns
      - JSON:  structured with folder details, attachments list,
               custom instructions, and availability info
    """
    raise SystemExit(cmd_assignments(course_id=course_id, json_output=json_output))


@cli.command("submit", cls=JsonOutputCommand)
@click.argument("course_id")
@click.argument("folder_id")
@click.option("-f", "--file", "file_path", required=True, help="Path to the file to submit.")
@click.option("--yes", "yes", is_flag=True, default=False, help="Skip confirmation prompt and submit immediately.")
@click.option("--json", "json_output", is_flag=True, help="Output this command's JSON result.")
def submit(course_id: str, folder_id: str, file_path: str, yes: bool, json_output: bool) -> None:
    """REMOTE WRITE: submit a file to a D2L dropbox folder.

    COURSE_ID is the course identifier (numeric OrgUnitId or name substring).
    FOLDER_ID is the dropbox folder identifier (numeric folder ID or name substring).

    Use `lighthouse assignments COURSE_ID` to discover available folders with their IDs.

    Example:
      lighthouse submit "signals" "Assignment 1" --file solution.pdf
      lighthouse submit signals "Assignment 1" --file solution.pdf --yes

    This is the only command that changes remote LMS state. The command prompts
    for confirmation before submitting (course name, folder name, file path).
    Use --yes to skip the prompt (required for agent/automation use).

    On success, prints a JSON object with submission_id, folder_id, folder_name,
    course_id, course_name, file info, and submitted_at timestamp.
    """
    raise SystemExit(cmd_submit(
        course_id=course_id,
        folder_id=folder_id,
        file_path=file_path,
        yes=yes,
        json_output=json_output,
    ))
