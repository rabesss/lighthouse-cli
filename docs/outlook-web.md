# Experimental Outlook capability probe

`lighthouse outlook probe` is an experimental metadata-only proof of the browser
flow, not a message-reading implementation. It reports positions and available read/unread
states for rows currently rendered by Outlook on the web. All message labels,
text, and previews are withheld, including subjects, senders, login codes, and
reset links. There is no content opt-in flag, and search is unsupported.

It uses a separate, temporary headed Chromium browser. You complete sign-in and
MFA yourself on every invocation.

This is an optional adapter, independent of Lighthouse's D2L API and authentication.
It does not reuse the CLI's D2L credentials, your everyday browser profile, a
connected computer-use browser, or an exported session. No persistent browser
profile, cookies, tokens, or sign-in session are saved by this command.

## Setup and use

Install the existing `auth` extra and the official Playwright Chromium browser
in the terminal/environment where you intend to use the command:

```bash
pip install -e '.[auth]'
playwright install chromium
```

A graphical desktop is required because sign-in is interactive. See the
[official Playwright browser installation documentation](https://playwright.dev/python/docs/browsers).
The package's normal help and version commands still work without Playwright.

```bash
# Sign in manually, then inspect metadata for currently rendered Outlook rows
lighthouse outlook probe --interactive-login

# Sign in again and return a machine-readable, content-free snapshot
lighthouse outlook probe --interactive-login --limit 25 --json

# Allow more time to complete sign-in and MFA
lighthouse outlook probe --interactive-login --login-timeout 300 --json
```

Without `--interactive-login`, the command fails before opening a browser. The
flag explicitly opts in to the temporary browser and manual sign-in. Prompts and
progress go to stderr. The browser closes after collection or an error.

Options:

- `--search`: explicitly unsupported; a supplied valid query returns
  `search_not_supported` before a browser opens
- `--limit`: maximum rows returned, from 1 to 100; default 25
- `--login-timeout`: manual sign-in timeout in seconds, from 30 to 600; default 180
- `--json`: exactly one JSON document on stdout for success or failure

The CLI retains the search option only to reject it explicitly rather than
silently ignoring it. Invalid values also fail before browser launch. The command
does not echo queries in diagnostics. Shell command history may retain them, so
avoid putting secrets in command arguments.

Search is disabled because there is no verified signal that a particular query's
results have finished rendering. Outlook can display a generic "Results" heading
before the corresponding rows arrive, which could produce a stale snapshot.

## What the result means

Success returns this schema (example values are illustrative):

```json
{
  "source": "outlook_web",
  "coverage": "rendered_rows_only",
  "complete_mailbox": false,
  "stable_ids": false,
  "scope": "current_view",
  "text_included": false,
  "rows": [
    {
      "position": 1,
      "rendered_text": "",
      "unread": false,
      "text_omitted": true
    }
  ],
  "limit_reached": false
}
```

- `scope` is `current_view`. The initial/current folder is not independently
  verified as Inbox
- `position` is a position in this snapshot, not a message ID; never use it as a
  canonical identifier or assume it is stable across runs
- `text_included` is always `false`, and `rendered_text` is always empty. No row
  labels, message text, previews, subjects, senders, or links are returned
- `unread` is `true` or `false`. Missing or conflicting read-state controls
  fail closed with `unsupported_layout`
- `text_omitted` is always `true`. The probe does not read row labels, text, or
  previews at all, and does not rely on recognizing secret or login-code patterns
- `limit_reached` says the requested count was reached; it does not establish
  whether more messages exist

The result is a partial view. Outlook virtualizes its list, so rows not rendered
at collection time are absent. An empty list is not proof of an empty mailbox.
Currently, if no rows become visible, the command returns `empty_or_loading`
rather than an empty success result. It cannot yet distinguish a genuine empty
view from a loading or changed-layout view.
The adapter does not scroll through the mailbox, paginate, open rows, or collect
canonical message identifiers.

The automation only navigates to mail and inspects rendered-row metadata.
It does not submit searches, click message rows, or click mail-changing controls. Sending,
replying, forwarding, deleting, marking read/unread, moving mail, attachment
downloads, and opened message-body access are not implemented. Your own actions
inside the sign-in browser remain under your control.

Even content-free mailbox metadata may be private. Redirected JSON persists
wherever you choose to save it, even though the adapter does not save the browser session.

## Errors and current limitations

Success exits with `0`, a command failure with `1`, and an interrupted read with
`130`. With `--json`, operational failures return a fixed error code and safe
message, for example:

```json
{
  "source": "outlook_web",
  "code": "interactive_login_required",
  "error": "Use --interactive-login to open a temporary browser and sign in yourself."
}
```

Click argument-parsing failures use the existing CLI JSON usage-error shape,
`{"error": "Invalid command arguments. See --help."}`. Human diagnostics remain
on stderr and do not include raw browser errors, URLs, or rejected input.
Human-only syntax errors retain Click's exit code `2`.

This experimental probe targets English Outlook web labels. Stable header-only
selectors have not been established, so sender/subject extraction is not implemented.
UI updates, different layouts or languages, tenant restrictions, unavailable
Chromium, or lack of a graphical display may prevent collection. The wrapper and probe core have isolated
mock-based tests; live end-to-end sign-in and collection still require validation
in the target user's graphical environment. A session already signed in elsewhere
does not close that validation gap: this adapter deliberately signs in separately.
