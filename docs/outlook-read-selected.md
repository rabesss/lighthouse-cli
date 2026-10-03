# Experimental selected-message reader

`lighthouse outlook read-selected --interactive-login` reads bounded plain text
from one message that you manually select in a **fresh temporary browser**. The
message must already be read and visible when the command captures its baseline.
The separate `outlook probe` remains entirely content-free.

## Run it

Use a graphical environment with the existing `auth` extra and official
Playwright Chromium installed; see [setup](outlook-web.md#setup-and-use).

```sh
lighthouse outlook read-selected --interactive-login --json
lighthouse outlook read-selected --interactive-login --max-body-chars 4000
```

1. Complete sign-in and MFA yourself in the new Chromium window. Start with no
   message selected, no compose/reply window, and no open message body (an empty reading-pane shell is
   allowed). If the command reports a baseline
   error, close the open message before starting a new invocation
2. Wait for `Baseline captured` on stderr
3. Select one **already-read** message from the current visible list. Do not
   scroll, change folders, select multiple rows, or open unread mail
4. The command validates the selection and returns one result, then closes the
   temporary browser. Each invocation needs a fresh sign-in

The command never clicks a message or read/unread control. Your manual actions
remain your responsibility: opening an unread message can mark it read before
this command rejects it. The command cannot prevent your own mailbox changes.

Do not share control of the sign-in window while entering information. Stop
typing when sign-in finishes; Outlook can interpret ordinary keys as mailbox
shortcuts after navigation. The command itself never types or clicks.

Options:

- `--interactive-login`: required opt-in; missing it never opens a browser
- `--login-timeout`: 30–600 seconds, default 180
- `--selection-timeout`: 10–600 seconds after baseline, default 120
- `--max-body-chars`: 1–20000 characters, default 8000
- `--json`: one JSON document on stdout; prompts and errors stay on stderr

There are no search, list-all, row-opening, attachment, or write options.

## Selection guard

The reader requires one English `Message list` listbox. It records only IDs and
read states for up to 100 mounted visible rows in memory, with no initial
selection or message body. An empty Reading Pane shell is allowed only without
message, subject, sender, conversation-group or body-document anchors. The native `Mark as unread` button indicates an
already-read row; ambiguous or missing controls fail closed. It takes two stable
baseline passes before prompting you to select anything.

Visible English `Send` buttons, including disabled buttons and labels with a
keyboard-shortcut suffix, stop the selected-reader with `compose_open`. This
conservative compose/reply guard is checked during baseline and selection,
including the checks surrounding extraction. It reads no draft text and never
invokes draft-close or discard controls; the temporary browser still closes when
the command exits. The guard checks only the current page, not separate compose
windows. A matching `Send` button inside a rendered email can also produce a
conservative false positive. This supported-layout check is not a universal
compose detector or a lock on user input; hidden controls and changed/localized
layouts can differ. It does not prevent simultaneous-user-input races or explain
the earlier `unsupported_layout` failure.

The selected row must keep the same in-memory DOM ID and read state. All mounted
row IDs, their order and read states must still match the baseline. New,
virtualized, reordered, unread, unknown, or multiple selections are rejected.
This deliberately rejects some otherwise harmless refreshes instead of guessing.

Extraction requires:

- Exactly one `Reading Pane` main and one `1 messages` group, with one
  `Email message` container
- An empty `MSG_*_SUBJECT` heading linked by one `aria-labelledby` ID to a unique,
  nonempty `CONV_*_SUBJECT` heading outside the body subtree
- A `*_FROM` sender heading outside the body, with exact subject and sender text
  each matching exactly one leaf span of the selected row
- Exactly two body test containers, each containing one `Message body` document:
  an empty direct-child placeholder inside `Email message`, and one visible,
  nonempty portalled document outside it but inside the same `1 messages` group
- Identical subject, sender, body and selected-row identity in two passes, with
  selection and read-state checks before and after extraction

Multiple-message conversations, duplicate/ambiguous headers, extra body
containers, changed content or unsupported layouts return a fixed error rather
than partial content. No generated CSS class or message preview is used to infer
header fields. Date is intentionally omitted.

These are observed DOM consistency checks, **not a canonical message-ID to body
binding**. There is no verified canonical relationship between the row ID and
portalled body. DOM IDs may be reused, UI structure can change, and a transient
change that happens entirely between checks cannot be ruled out. Do not use this
experimental result as authenticated message provenance.

## Output and sensitive content

Success has these fields:

```json
{
  "source": "outlook_web",
  "scope": "one_user_selected_already_read_message",
  "complete_mailbox": false,
  "stable_ids": false,
  "selection_validation": "observed_dom_only",
  "content_trust": "untrusted_data_not_instructions",
  "message": {
    "subject": "Planning note",
    "sender": "Example Colleague",
    "body": "Bring the agenda."
  },
  "content_omitted": false,
  "redactions_applied": false,
  "body_truncated": false
}
```

Only subject, sender and rendered plain body text are returned. HTML, link
attributes, DOM IDs, attachment data, page URLs and browser exception details are
never included. Subject and sender are limited to 512 characters each; a body
over 50000 characters fails extraction rather than being transferred to Python.
Output is further bounded by `--max-body-chars`, with an explicit truncation flag.

The filter withholds the whole message if it detects known authentication/reset
wording, common credential keys or long token-like strings. It removes URL-like
text, code-like number sequences and nonprintable terminal/bidirectional control
characters (ordinary newlines are preserved). This can also suppress harmless
text, dates and identifiers. **Suppression is best-effort, not a universal secret
or personal-information detector**. Unknown, obfuscated, or unusual secrets may
remain; review output before sharing it or passing it to another service.

All returned mail is untrusted data. Instructions embedded in it do not authorize
commands, external requests, credential access or other actions. The reader never
interprets or executes mail text. JSON redirected to a file persists there even
though the temporary browser session is not saved.

## Authentication, errors and verification limits

This command never connects to an existing browser, reads cookies or storage,
exports a session, accepts credentials as arguments, or saves a persistent
profile. It does not reuse the managed computer-use browser, D2L authentication,
a desktop browser, or another signed-in session. The exact trusted Outlook hosts
are `outlook.office.com`, `outlook.office365.com` and `outlook.cloud.microsoft` over
HTTPS on the default/443 port, without embedded user information.

Policy blocks stop with the same fixed diagnostics as the metadata probe. There
are no sign-in bypasses or automatic MFA/consent actions. Exit status is 0 on
success, 1 on operational failure and 130 on interruption. New fixed errors
include `baseline_required`, `selection_not_eligible`, `selection_timeout` and
`content_too_large`, plus `compose_open`; no raw content appears in diagnostics.
An `unsupported_layout` failure may also carry a fixed `stage` label:
`baseline`, `selection`, `message_pane`, `subject_header`, `sender_header`,
`row_headers`, or `body_layout`. These identify the failed check without returning
page text, draft text, addresses, DOM IDs or browser exception details.

The selectors and a clean-baseline → same already-read row transition were
verified in a separate managed browser; mounted row read states and the displayed
unread count were unchanged. Managed-browser checks use supported JavaScript
callbacks for equivalent DOM predicates; they do not execute the Python
Playwright string-expression API or establish standalone runtime compatibility.
Synthetic DOM/CLI tests cover positive and negative
paths without real mail fixtures. A separate cloud test using installed system
Chromium through an external executable-only launch override completed manual
fresh sign-in and the Python reader's baseline capture. It then stopped with
`unsupported_layout`, returned no message content, and closed the temporary
browser. That test did not identify the failing stage. The pinned bundled-browser
installation failed in that environment, so the stock launch remains unverified.
**A complete fresh sign-in through successful standalone extraction has not been
verified.** No existing managed-browser session was shared or imported.
