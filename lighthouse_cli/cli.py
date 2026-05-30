"""Click CLI definitions for lighthouse-cli.

Defines the command group and all subcommands with their options/arguments.
Delegates actual logic to lighthouse_cli.commands.

Every command accepts a global --json flag for machine-readable output.
"""

from __future__ import annotations

import click

from . import __version__
from .commands import (
    cmd_announcements,
    cmd_auth_status,
    cmd_calendar,
    cmd_content,
    cmd_courses,
    cmd_download,
    cmd_grades,
    cmd_quiz_detail,
    cmd_quizzes,
    cmd_semesters,
    cmd_sync,
    cmd_assignments,
    cmd_submit,
)
from .auth import cmd_auth_login
from .course_config import cmd_config_courses

# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version=__version__, prog_name="lighthouse-cli")
def cli() -> None:
    """lighthouse-cli – CLI for D2L Brightspace LMS at lighthouse.manipal.edu.

    Interact with courses, content, grades, and more via D2L REST APIs.
    Run 'lighthouse auth login' first to set up your session.
    """


# ---------------------------------------------------------------------------
# Auth subgroup
# ---------------------------------------------------------------------------

@cli.group()
def auth() -> None:
    """Manage authentication (session cookies)."""


@auth.command("status")
@click.option("--json", "json_output", is_flag=True, help="Output JSON.")
def auth_status(json_output: bool) -> None:
    """Check if stored cookies are still valid."""
    raise SystemExit(cmd_auth_status(json_output))


@auth.command("refresh")
@click.option("--user", "username", default=None, help="Username (email) for Microsoft SSO.")
@click.option("--pass", "password", default=None, help="Password for Microsoft SSO.")
@click.option("--totp", "totp", default=None, help="2FA code. Use - to read from stdin pipe.")
@click.option(
    "--mfa-method",
    type=click.Choice(["auto", "sms", "app", "choose"]),
    default=None,
    help="MFA: auto (tenant default), sms, app, or choose (interactive list).",
)
@click.option("--json", "json_output", is_flag=True, help="Output JSON.")
def auth_refresh(
    username: str | None,
    password: str | None,
    totp: str | None,
    mfa_method: str | None,
    json_output: bool,
) -> None:
    """Refresh session cookies via Microsoft SSO.

    Runs the full HTTP-based SSO login flow to obtain fresh session cookies.
    Equivalent to ``auth login`` without the ``--save-credentials`` option.
    """
    raise SystemExit(cmd_auth_login(
        username=username,
        password=password,
        totp_code=totp,
        totp_stdin=(totp == "-"),
        json_output=json_output,
        mfa_method=mfa_method,
    ))


@auth.command("login")
@click.option("--user", "username", default=None, help="Username (email) for Microsoft SSO.")
@click.option("--pass", "password", default=None, help="Password for Microsoft SSO.")
@click.option("--totp", "totp", default=None, help="2FA code. Omit for two-phase interactive login.")
@click.option(
    "--mfa-method",
    type=click.Choice(["auto", "sms", "app", "choose"]),
    default=None,
    help="MFA: auto (tenant default), sms, app, or choose (interactive list).",
)
@click.option(
    "--save-credentials",
    "save_credentials",
    is_flag=True,
    default=False,
    help="Save email/password encrypted for future logins (session cookies still expire ~5 days).",
)
@click.option("--json", "json_output", is_flag=True, help="Output JSON.")
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

    MFA: --mfa-method auto (default), sms, app, or choose (pick from a list).
    Text codes may arrive via SMS or WhatsApp depending on Microsoft; the CLI
    cannot select the delivery channel.

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


@auth.command("verify")
@click.argument("code")
@click.option("--json", "json_output", is_flag=True, help="Output JSON.")
def auth_verify(code: str, json_output: bool) -> None:
    """Complete login with the verification code from the current ``auth login`` session.

    Use after ``auth login`` prints "Verification code sent." Do not run a second
    ``auth login`` — that sends a new code and invalidates the previous one.
    """
    from lighthouse_cli.auth import cmd_auth_verify

    raise SystemExit(cmd_auth_verify(code, json_output=json_output))




# ---------------------------------------------------------------------------
# Config subgroup
# ---------------------------------------------------------------------------

@cli.group()
def config() -> None:
    """Manage configuration (course tracking, semester mapping)."""


@config.command("courses")
@click.option("--add", default=None, help="Track a course by ID or name.")
@click.option("--remove", default=None, help="Stop tracking a course by ID.")
@click.option("-s", "--semester", default=None, help="Semester label to assign (used with --add).")
@click.option("--list", "list_courses", is_flag=True, default=False, help="Show tracked courses.")
@click.option("--reset", is_flag=True, default=False, help="Clear all course tracking config.")
@click.option("--json", "json_output", is_flag=True, help="Output JSON.")
def config_courses(add: str | None, remove: str | None, semester: str | None, list_courses: bool, reset: bool, json_output: bool) -> None:
    """Manage course tracking and semester mapping.

    Without flags, runs interactive setup: shows all enrolled courses
    and lets you pick which to track and assign semester labels.

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

@cli.command()
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
def semesters(json_output: bool) -> None:
    """List all semesters."""
    raise SystemExit(cmd_semesters(json_output))


@cli.command()
@click.option("-s", "--semester", default=None, help="Filter by semester label (requires course tracking config).")
@click.option("--tracked", "tracked_only", is_flag=True, default=False, help="Show only tracked courses.")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
def courses(semester: str | None, tracked_only: bool, json_output: bool) -> None:
    """List all courses."""
    raise SystemExit(cmd_courses(semester=semester, tracked_only=tracked_only, json_output=json_output))


@cli.command("content")
@click.argument("course_id")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
def content(course_id: str, json_output: bool) -> None:
    """Show content tree for a course (modules > submodules > topics)."""
    raise SystemExit(cmd_content(course_id, json_output))


@cli.command("download")
@click.argument("course_id", required=False)
@click.argument("topic_id", required=False, type=int)
@click.option("-o", "--output-dir", default=None, help="Custom download directory.")
@click.option("--dry-run", is_flag=True, default=False, help="List files without downloading.")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.option("--force", is_flag=True, default=False, help="Wipe manifest and re-download everything.")
@click.option("--types", default="file", help="Comma-separated content types to download (file,html). Default: file.")
@click.option("-s", "--semester", default=None, help="Filter to a specific semester (requires tracking config).")
@click.option("--also", "also_courses", multiple=True, help="Additional course(s) to include by name or ID.")
@click.option("--include-assignments", is_flag=True, default=False, help="Also download assignment attachments.")
@click.option("--assignment", "assignment_id", default=None, type=int, help="Download a specific assignment folder's attachment(s).")
@click.option("--attachment", "attachment_id", default=None, type=int, help="Download a specific attachment from an assignment folder.")
def download(
    course_id: str | None,
    topic_id: int | None,
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
    """Download files from a course.

    If COURSE_ID is given, download that course. Without COURSE_ID,
    downloads all courses from the latest semester. If TOPIC_ID is also
    given, download that single file from the specified course.

    Scope options:
      --semester  Filter courses to a specific semester (by name or ID)
      --also      Add additional course(s) outside semester scope

    Assignment options:
      --include-assignments  Download attachments from all dropbox folders
      --assignment           Download a specific dropbox folder
      --attachment           Download a specific attachment (requires --assignment)
    """
    raise SystemExit(
        cmd_download(
            course_id,
            topic_id=topic_id,
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


@cli.command("sync")
@click.argument("course_id", required=False)
@click.option("-o", "--output-dir", default=None, help="Custom download directory.")
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
@click.option("--force", is_flag=True, default=False, help="Wipe manifest and re-download everything.")
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
    """Incremental sync: only download new or changed files.

    Uses .lighthouse.json manifest to skip unchanged topics.
    Without COURSE_ID, syncs all courses from the latest semester.

    Scope options:
      --semester  Filter courses to a specific semester (by name or ID)
      --also      Add additional course(s) outside semester scope
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


@cli.command()
@click.argument("course_id", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
def grades(course_id: str | None, json_output: bool) -> None:
    """Show grades. If COURSE_ID omitted, show all courses."""
    raise SystemExit(cmd_grades(course_id=course_id, json_output=json_output))


@cli.command()
@click.argument("course_id", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
def announcements(course_id: str | None, json_output: bool) -> None:
    """Show announcements. If COURSE_ID omitted, show all courses."""
    raise SystemExit(cmd_announcements(course_id=course_id, json_output=json_output))


@cli.command()
@click.argument("course_id", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
def calendar(course_id: str | None, json_output: bool) -> None:
    """Show calendar events. If COURSE_ID omitted, show all courses."""
    raise SystemExit(cmd_calendar(course_id=course_id, json_output=json_output))


@cli.command()
@click.argument("course_id", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
def quizzes(course_id: str | None, json_output: bool) -> None:
    """Show quizzes. If COURSE_ID omitted, show all courses."""
    raise SystemExit(cmd_quizzes(course_id=course_id, json_output=json_output))


@cli.command("quiz")
@click.argument("course_id")
@click.argument("quiz_id", type=int)
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
def quiz_detail(course_id: str, quiz_id: int, json_output: bool) -> None:
    """Show detailed info for a specific quiz.

    Shows quiz settings, time limits, attempt rules, dates, etc.
    Note: quiz questions and past attempts are not accessible via the
    learner API. Use the browser link to view those.
    """
    raise SystemExit(cmd_quiz_detail(course_id, quiz_id, json_output))


@cli.command("assignments")
@click.argument("course_id", required=False)
@click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")
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


@cli.command("submit")
@click.argument("course_id")
@click.argument("folder_id")
@click.option("-f", "--file", "file_path", required=True, help="Path to the file to submit.")
@click.option("--yes", "yes", is_flag=True, default=False, help="Skip confirmation prompt and submit immediately.")
@click.option("--json", "json_output", is_flag=True, help="Output JSON result.")
def submit(course_id: str, folder_id: str, file_path: str, yes: bool, json_output: bool) -> None:
    """Submit a file to a D2L dropbox folder.

    COURSE_ID is the course identifier (numeric OrgUnitId or name substring).
    FOLDER_ID is the dropbox folder identifier (numeric folder ID or name substring).

    Use `lighthouse assignments COURSE_ID` to discover available folders with their IDs.

    Example:
      lighthouse submit "signals" "Assignment 1" --file solution.pdf
      lighthouse submit signals "Assignment 1" --file solution.pdf --yes

    The command prompts for confirmation before submitting (course name, folder
    name, file path). Use --yes to skip the prompt (required for agent/automation
    use).

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
