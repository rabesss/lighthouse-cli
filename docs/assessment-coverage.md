# Lighthouse student and instructor coverage

Inspected on 2026-09-17. Product target: `lighthouse.manipal.edu`. The
`hetrynow.brightspace.com` trial is a fixture environment, not proof of Manipal
instructor permissions or complete platform parity. Starting revision:
`9dbee35`. Implementation branch: `feat/assessment-workflows`.

## Evidence boundaries

- University browser: authenticated Semester V learner. EEFM course `69472`
  supplied the student baseline. No university assessments were modified or
  submitted.
- Trial: instructor in personal **Build Your Course** (`22985`), with one
  pre-enrolled **Sample Student**. The sample business course (`22984`) supplied
  populated read-only examples for discussions, surveys, checklists and
  submissions. Its existing content was not intentionally edited.
- The trial's classlist did not expose impersonation, and Admin Tools did not
  expose user management. **View as Student** displayed the quiz summary but
  no Start Quiz button. A separately authenticated learner is still needed.
- Instructor **Preview** did start, save and submit attempts. These are
  explicitly previews (`isprv=1`), not genuine learner attempts.
- API probes ran in the connected browser with its current session. Local
  CLI attempts used the existing sealed session with read-only auth. That
  session decrypted using the workstation's local-secret injection but was
  approximately 20.9 days old and returned 403 where the browser returned 200.
  It was not replaced. Do not label these browser checks as successful
  end-to-end local CLI authentication.

## Trial fixtures retained for follow-up

All fixtures below are in course `22985` and have no gradebook link.

| Fixture | ID | Purpose |
| --- | --- | --- |
| CLI Sandbox - Live Attempt Test | Quiz `54488` | Two true/false questions, all visible together; visible in this trial course |
| CLI Sandbox - One Question No Backtracking | Quiz `54489` | Same two questions, one per page, backward navigation disabled; hidden, usable in instructor preview |
| CLI Sandbox - API Quiz Shell | Quiz `54490` | Hidden empty shell created using the CLI payload via browser-authenticated REST; two allowed attempts |
| CLI Sandbox - Assignment Submission Test | Folder `23865` | Visible individual file assignment created through UI |
| CLI Sandbox - API Text Assignment | Folder `23866` | Hidden individual text assignment created using the CLI payload via REST |

Synthetic questions: “Two plus two equals four” (true), and “Three plus three
equals seven” (false). No real coursework was used.

Preview `29191` tested the initial one-question fixture and returned 100%.
Preview `29192` tested the two-page, no-backtracking fixture and returned 100%.
The transition displayed a confirmation warning, then page two offered no
previous-page control. Both answers were visibly saved before submission.
Preview `29193` tested the expanded all-at-once fixture with both questions
visible in the same content frame and returned 100% after both answers were saved.
The option to publish preview grading in the Grade Quiz area remained off.

## Confirmed API observations

### Follow-up: supported student-perspective verification

The supplied welcome email provides only the trial account, not a separate
learner login. [D2L's preview guide](https://community.d2l.com/brightspace/kb/articles/35012-preview-your-course)
distinguishes Role Switch (visibility checks), Preview (quiz interaction and
scoring), and Impersonate (student-account workflows). Preview does not cover
assignment submissions, learner accommodations or full downstream behavior.

Live validation: on quiz `54488`, cleared **Bypass Restrictions**, answered both
questions, and enabled **Allow this preview attempt to be graded in the Grade
Quiz area** before submission. New attempt `29194` returned 100%, survived Exit
Preview, and appeared in **Grade Quiz > Users > Show Search Options > Users who
have previewed attempts** with 2/2 and 100%. Its evaluation page opened.
This retained synthetic attempt is intentional test evidence, with no gradebook
link. Earlier previews were submitted without retention and must not be relied
on as durable grading fixtures after Exit Preview.

Correction to the earlier blocker: core quiz-flow development and teacher
evaluation testing can proceed with Preview. Separately authenticated learner
or authorized impersonation access remains necessary for student-specific
permissions, submissions, accommodations and downstream validation. No public
TryNow sample-student credentials or activation route were established by the
research. No request was sent to D2L; an administrator-provided learner account
or scoped sample-student impersonation is the remaining access path.

API roots used: LE `1.93`, LP `1.47`.

| Operation | University learner | Trial instructor | CLI implementation |
| --- | --- | --- | --- |
| Own assignment submissions | 200, empty for `46748` | Genuine learner submission not available | `student assignment-history` |
| Assignment definitions | Existing CLI support | 200, populated sample course | Both role groups; existing top-level command retained |
| Classlist | 200 | 200 | Both role groups |
| My sections | 200, one section | Not used as learner evidence | `student my-sections` |
| Group categories | 200, empty | 200, empty | Both role groups; groups selectable by category |
| Survey list | 200, empty | 200, two surveys | Lists and details |
| Checklist list | 200, empty | 200, one checklist; item fields inspected | Lists, details and items |
| Discussion forums | 200, empty | 200, populated forums/topics/posts | Hierarchical reads |
| Quiz question definitions | Not treated as learner access | 200 | `instructor quiz-questions` |
| Quiz attempt summaries | Not treated as learner access | 200, includes preview summaries | `instructor quiz-attempts` |
| Create assignment via cookie auth | No university write attempted | 403 without CSRF, 200 with CSRF | Hidden file/text creation |
| Create quiz via cookie auth | No university write attempted | 200 for final payload | Hidden shell creation, both layouts |
| Submit synthetic file | No university write attempted | 403 even with CSRF | Existing upload command includes CSRF when available; learner-role live validation remains blocked |
| Content userprogress route | 404 for inspected URL | 404 for inspected URL | Not added based on this failed probe |

The homepage embeds a `localStorage.setItem('XSRF.Token', ...)` bootstrap in a
script. Its parsed value matched the active browser token without exposing
either value. Assessment creation bootstraps that value through a bounded
homepage GET, caches it per client, and clears it when cookies refresh. File
submission includes it when the initializer is present but remains compatible
with the documented cookie-only endpoint when it is absent. No token is logged
or written in plaintext.

The first quiz creation payload returned 400. Replacing its unenforced timing
and late-submission defaults with the accepted values in `quiz_payload()`
produced 200. The two changes were tested together; this is not proof that one
individual field caused the rejection.

## Remaining work toward full website parity

| Area | Missing workflows / validation |
| --- | --- |
| Learner quizzes | Genuine learner start, current-page questions, save confirmation, resume, one-way page advancement, timer expiry, final submission and receipt |
| Quiz authoring | Question creation/import/edit, sections/pools, full settings updates, special access, reports and grading |
| Assignments | Real learner file/text submission validation, attachment download from history, group submission, instructor feedback/rubric grading and publication |
| Discussions | Create/reply/edit, attachments, moderation, rating and subscription actions |
| Checklists and surveys | Learner completion/response and instructor authoring |
| Groups and sections | Membership changes, self-enrollment, teacher group creation and assignment |
| Grades and progress | Teacher gradebook mutation, rubric grading, complete learner progress APIs |
| Course authoring | Content creation/upload/reordering, announcements/calendar writes, files, links, import/export/copy, dates |
| Other teacher tools | Attendance, learning outcomes, intelligent agents, Quick Eval, tool settings |
| Account and institutional tools | Notifications/profile settings, discovery/awards, external integrations; capabilities and permissions require their own validation |

These are open requirements, not completed features. The public quiz API
exposes authoring metadata and attempt summaries but no documented learner
start/answer/submit endpoints. The preview uses nested HTML frames and form
submissions such as `quiz_attempt_save_auto.d2l` and
`quiz_confirm_submit_auto.d2l`. Do not implement a guessed replay protocol or
use instructor question definitions to bypass learner paging.

For an attempt driver, separate the current rendered page from quiz metadata.
Unknown paging/backtracking values must remain unknown. Save and confirm each
answer before advancing; one-way transitions must be explicit and cannot be
retried blindly. Final submission must return a verified receipt or an unknown
outcome, never a fabricated success. A future driver must test both layouts
against a genuine learner account as well as the instructor preview.

## Experimental trial preview driver

`instructor --site trial preview` now exposes `start`, `page`, `answer`, `next`,
`submit`, `status`, and local-only `abandon`. This is restricted to untimed
instructor previews with text/radio questions and the two requested layouts.
It is not a verified real-learner driver. The implementation separates bounded
HTML parsing, form protection, HTTP transitions, submission receipts, encrypted
cursor ownership, and Click wiring. No returned JavaScript is executed.

Fresh per-request hit codes and dynamically mapped response-present fields were
required: an HTTP 200 alone did not prove an answer was saved. Save verification
reads back the selected choice and saved marker. Forward transitions require all
current answers saved. Start and writes are never automatically replayed; an
uncertain result leaves a durable checkpoint and blocks further writes. Final
submission requires both the saved/submitted receipt and a matching completed
REST attempt record. Unsupported media, question types, timers, and session
locking fail closed.

Authenticated browser HTTP probes completed and retained these synthetic
previews on September 17, 2026:

| Quiz | Attempt | Verified result |
| --- | --- | --- |
| One question per page, no backtracking (`54489`) | `29204` | Saved first answer, advanced, saved second answer, submitted; completed REST record, 2 points |
| All questions on one page (`54488`) | `29205` | Both answers persisted on readback; submission receipt and completed REST record, 2 points |

The first probe exceeded the browser tool's observation timeout; a separate
read of the completed attempt resolved that uncertainty without repeating the
write. These are live protocol validations using the browser's authenticated
session, **not end-to-end runs of the installed Python CLI**. Stored terminal
cookies were stale (403), and a fresh trial CLI session has not been imported.
Python parsing, transport, cursor recovery, and JSON behavior are covered by
local tests. Real learner authorization and assignment submission still need
a learner login or authorized impersonation.

Helium was relaunched through its installed launcher using the active desktop's
Wayland environment; the ChatGPT extension reconnected automatically. No debug
port, alternate profile, or authentication settings were introduced.

## Startup measurement

Fresh Python processes on this workstation, medians (not network timings):

| Invocation | Before | After lazy imports |
| --- | ---: | ---: |
| Root help | 269.1 ms | 64.4 ms |
| Version | 257.1 ms | 57.2 ms |
| Courses help | 250.3 ms | 62.6 ms |

The new role groups are also lazy-loaded. Regression checks ensure importing
the CLI does not import requests, BeautifulSoup, Microsoft SSO, or assessment
implementations. File splitting alone is not counted as a latency improvement.

## Local verification

Full suite: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q
-p no:cacheprovider --basetemp=/var/tmp/lighthouse-pr-delivery-8sn7bl/full-tests-after-review-fixes`
— **1,443 passed in 37.91 seconds**. The temporary test directory is disposable.
`ruff check --no-cache` passed for changed production modules and new tests;
`git diff --check` passed. The stacked PRs are open with hosted checks and
review bots still running; no merge or deployment has been performed.

Tests cover lazy imports, both paging modes, unknown navigation rules,
same-origin pagination/cookies, sealed origin-bound imports, JSON errors,
CSRF bootstrap/caching, optional submission protection, session-expiry write
handling, and non-replayed submissions.
Existing multipart tests begin with a synthetic cached CSRF token; separate
request-protection tests exercise the new bootstrap path.

Closeout: seven task-owned pytest directories under `/var/tmp` were audited
for live references and removed (66.33 MiB to zero). Source changes, this report
and the trial fixtures remain intentionally. The pre-existing repository
`.ruff_cache` was retained. The unrelated/unattributed `d2l-logo.png` (985 bytes)
was left untouched. During the earlier pass, temporary browser inspection tabs were closed and the
university tab was preserved. After the subsequent browser restart, the capture
tab was closed and the retained preview result (`29205`, 100%) was left open.

## Sources

- [D2L quizzes](https://docs.valence.desire2learn.com/res/quiz.html)
- [D2L assignments](https://docs.valence.desire2learn.com/res/dropbox.html)
- [D2L discussions](https://docs.valence.desire2learn.com/res/discuss.html)
- [D2L groups and sections](https://docs.valence.desire2learn.com/res/groups.html)
- [D2L checklists](https://docs.valence.desire2learn.com/res/checklist.html)
- [D2L surveys](https://docs.valence.desire2learn.com/res/survey.html)
- [D2L response on learner quiz API limitations](https://community.d2l.com/brightspace/discussion/7729/request-for-api-access-to-start-and-submit-quiz-attempts-via-rest-api)

Follow-up closeout: `/var/tmp/lighthouse-quiz-parser-20260917` and
`/var/tmp/lighthouse-helium-launch-8lqhp74n.log` had no live references and were
removed (21,913,600 allocated bytes to zero). The failed launch was caused by
missing desktop display environment variables; the successful browser remains
open. Source, tests, this evidence report and the synthetic trial fixtures are
retained; `.ruff_cache` and `d2l-logo.png` remain untouched as noted above.
