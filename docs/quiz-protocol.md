# Brightspace assessment protocol notes

Findings behind the `student`/`instructor` assessment commands and the
instructor quiz-preview driver. They describe Brightspace (D2L) behavior, which
Lighthouse (MAHE Manipal) runs; they were established on a disposable
Brightspace sandbox (instructor previews, and learner attempts in its "View as
Student" role), with a throwaway learner account on a public Brightspace site,
and in passive observation of a real Lighthouse attempt.
The detailed evidence log is in the git history of
`docs/assessment-coverage.md` (removed 2026-09-24).

API roots: LE `1.93`, LP `1.47`.

## Writes and request protection

- The homepage embeds a `localStorage.setItem('XSRF.Token', ...)` bootstrap.
  Assessment creation reads it through a bounded homepage GET, caches it per
  client, and sends `X-Csrf-Token`; cookie-only POSTs were rejected (403)
  without it. File submission uses Brightspace's documented cookie-only
  endpoint. Tokens are held only in memory and never logged.
- A quiz-creation payload with default timing and late-submission fields was
  rejected (400); the values in `quiz_payload()` were accepted.

## Quiz attempts (legacy HTML frames, not the REST API)

The public quiz API exposes authoring metadata and, to callers allowed to
grade attempts, attempt summaries, but no documented learner
start/answer/submit endpoints. Taking a quiz uses nested HTML frames and form
posts:

1. Summary page `quiz_summary.d2l`, then a start POST (302) through
   `quiz_start_frame_auto.d2l` and `quiz_start_iframe_2_auto.d2l` to
   `quiz_start_process_auto.d2l`, whose script calls
   `parent.GoToAttemptQuizAuto(attemptId, page, 0)`. That GET creates server
   state and must never be replayed. That start POST (the one before the
   302) is multipart to `quiz_summary.d2l?...&inProgress=false` with
   `d2l_action=Custom` and `d2l_actionparam=1`; the frame set the chain opens
   is `quiz_attempt_iframe_auto.d2l` holding the timer
   (`quiz_attempt_top_auto.d2l`), status, save and page frames. The summary
   script's `DoAction` sends that same `Custom`/`1` POST to start or to
   continue, with `inProgress` set to its `continueQuiz` flag and `cfql=1`
   when the quiz was opened from a content link. A password-protected quiz
   also needs the summary form's `password` field. The summary script's
   state says what a start would do: `canTakeQuiz`, `startQuiz` (a new
   attempt), `continueQuiz` (one in progress, or one the server is still
   submitting; see "Timed quizzes") and `hasPass`. `DoAction`'s
   code refuses to start while its `isImpersonatingRole` flag is set; a
   direct start POST with that flag set was not tried. In the sandbox's
   "View as Student" role the flag was false, `canTakeQuiz` was true and
   the page had no Start button in its markup, yet the same start POST
   created learner attempts: there, a missing button did not mean the
   start was refused.
2. Page `quiz_attempt_page_auto.d2l?ou&qi&ai&pg&isprv`. Each question sits in
   a `d2l-quiz-question-autosave-container` with hidden metadata (object id,
   page, `tAtom` group, saved flag). The prompt is either a legacy
   `d2l_read_element_*` element or a single `d2l-html-block` outside the
   answer options. On a learner page, true/false and multiple choice are
   radios named `tAtom<tid>_<tvid>`, multi-select is one checkbox per option
   named `tAtom<tid>_<tvid>_<option>` with value `1`, and fill-in-the-blank
   is one text input per blank named `tAtom<tid>_<tvid>_<blank>`. True/false
   values were numeric answer ids, but multiple-choice values and the
   multi-select `<option>` name suffix were opaque tokens such as `o9188`.
   The preview page parser cannot read learner pages as is: it requires
   `isprv=1`, marks checkbox and text questions unsupported, and rejects
   non-integer radio values.
3. Answer save: multipart POST to `quiz_attempt_save_auto.d2l` with
   `d2l_action=Update`, `d2l_actionparam=3,<page>,<tid>,<tvid>,<question
   number>`, a fresh per-request hit code and the question's
   response-present flag. A learner page's "Save All Responses" button is in
   the markup but hidden (`d2l-hidden`), so matching on its text alone would
   wrongly count it as a visible Save control. The page saves on each change
   (text on change or blur) and posts the whole page form, every question's
   current value included, with `isFinalAutoSave=false`,
   `useNewFinalAutoSave=true` and `timeLimitFromQuiz` (the limit in
   minutes: `2` and `4` on 2- and 4-minute quizzes, `0` when untimed).
   Checked multi-select options send `1`; unchecked ones are left out of
   the form. The browser sends every save through one save frame: on a
   two-question learner page, when both answers changed within a second,
   the second save cancelled the first request (status 0), and the first
   question was not reported saved until the `5,<page>` save posted the
   whole page form again. An HTTP client has no such frame, but a
   cancelled or overlapping save leaves the stored value unknown: send one
   save at a time and check each response.
   In the captured saves the response named the question object ids the
   server marked saved, comma-separated, in
   `parent.infoFrame.UpdateSaved('<ids>','')`: the changed question for a
   `3,...` save, every saved question on the page for the `5,<page>` save
   before a manual submit. At time-up the `7,<page>` save named the page's
   question and the `5,<page>` save after it named none, so an empty list
   is not by itself a failure.
   Neither HTTP 200 nor that call proves which value was stored; read the
   page back and check the selected choices and the saved marker.
   The hidden "Save All Responses" action, `d2l_actionparam=1,<page>` with
   the whole page form, stores every answer of the page in one request, so
   a client sends one save per page instead of one per question. A
   question whose answer is cleared reads back as not saved.
4. Forward navigation: the same save endpoint with
   `d2l_actionparam=2,<new page>,<current page>` and the whole page form,
   as the preview driver sends. A learner's `2,2,1` was followed by the
   page frame loading `pg=2`. On a forward-only quiz the learner page
   first asks for confirmation in a page dialog (not `window.confirm`),
   then calls `DoGoNextPage`. That dialog is page script only: an HTTP
   client's `2,2,1` save moved a forward-only learner attempt to page 2.
   Forward-only quizzes offer no previous-page control. On the last page the "Next Page" buttons are still in the
   markup, with the `disabled` attribute. Requesting a page number past the
   quiz's last page permanently broke preview attempts (every later read
   redirects to `/d2l/error/500`). It was not tried on a learner attempt;
   treat it as fatal there too.

   Backward navigation (a learner attempt in the disposable Brightspace
   sandbox, on a two-page quiz that allows moving back): both "Previous
   Page" buttons, above and below the questions, post the same save with
   `d2l_actionparam=2,<page - 1>,<page>` and the whole page form (with
   `pg=<page>` and the page's own answers) to
   `quiz_attempt_save_auto.d2l?d2l_body_type=3&ou=<ou>`, without the
   `cfql` and `fromQB` that Next's URL has. Their handler is an inline
   `NavInfo` in the page's `d2l_controlMap`; the page script's
   `GoPreviousPage`, which would add `cfql` and `fromQB`, is not what they
   call. No dialog asks for confirmation. The save frame then loaded page
   `pg=<page - 1>`, where the answer saved there earlier was still
   selected, and the attempt submitted normally from it. Continue Quiz
   after a Previous reopened the attempt on the page moved back to, with
   both pages' answers kept. The CLI's own Previous request, sent from
   page 2 of that quiz, landed on page 1 with its answer kept, and the
   attempt then submitted normally. On page 1 both buttons are in the markup
   with the `disabled` attribute (handler `return false;`). Forward-only
   and one-page quizzes render no Previous button at all. The CLI moves
   back only from a page after the first with a visible, enabled Previous
   control, so it never requests a page below 1, and never on a quiz whose
   settings forbid moving back.
5. Manual submission (time-up differs; see "Timed quizzes"): preparatory
   save (`d2l_actionparam=5,<page>`), confirmation page
   `quiz_confirm_submit_auto.d2l`, then RPC
   `quiz_attempt_iframe_auto.d2lfile?...&pg=<current>&d2l_rh=rpc&d2l_rt=call`
   with `d2l_rf=ProcessQuizSubmission` and **compact** JSON `params`
   (Brightspace answers spaced JSON with an error redirect). Success is the
   callback `parent.QuizDone(quizId, attemptId, ...)`, then a receipt and a
   completed REST attempt record (readable with the attempt-grading
   permission; the learner account tested got 403, so a learner's submission
   is checked on the receipt and the submissions list instead).
   The script passes seven params: `quizId, attemptId, isPreview,
   canBeGraded, isRldbUse, shouldAutoSubmit, cameFromTab`. The confirmation
   page passes `shouldAutoSubmit` as `false`, the time-up path as `true`.
   For a learner `isPreview` is `false` and, with no can-be-graded checkbox
   on the confirmation page, `canBeGraded` is `true`. A learner's browser
   posted to the attempt frame's own URL, which kept `pg=1` after a move to
   page 2. In the four `QuizDone` results seen, the third argument was `'1'`
   for previews and `'0'` for learners, and the fourth was `'1'` after a
   time-up submit and `'0'` after a manual one.
6. Recovery: for a forward-only attempt on page 2, requesting page 1 returns
   page 2 (`pg=2`); the preview driver's read-only `reconcile` relies on this
   only after the full attempt identity matches.

Instructor previews carry `isprv=1`; a learner's start chain, attempt frames
and confirmation page carry an empty `isprv=`.
A real graded attempt observed on Lighthouse and a learner attempt on a public
Brightspace site used the same save, confirmation and submission routes, then
a receipt at `quiz_submissions_attempt.d2l?isprv=0`. Learner specifics from
that public site (four attempts on three untimed one-page quizzes):

- Fresh start: from a summary opened directly (`cfql=0`), Start Quiz posted
  `quiz_summary.d2l?ou&qi&cfql=0&inProgress=false` with the summary form's
  fields, as a preview start does: `d2l_action=Custom`, `d2l_actionparam=1`,
  hit code, an empty `hps`, and here `drc=0`, `LockDownBrowserUrl=0`,
  `LockDownBrowserLaunchTimeout=5000` plus the form-state fields. The chain
  ran with `inProgress=0`, and `quiz_start_process_auto.d2l` returned
  `parent.GoToAttemptQuizAuto( <ai>,1,0 )` with the new attempt id. The
  preview driver posts `inProgress=0` rather than `false`, which Brightspace
  also accepts for a start.
- Resume: the summary read "Completed - 0 (Attempt 1 in progress)" with a
  Continue Quiz button. It posts `quiz_summary.d2l?...&inProgress=true`,
  which redirects through the same chain with `inProgress=1`, including
  `quiz_start_process_auto.d2l`, and reopened the same attempt id with its
  saved answers. Each chain came from its own summary POST; a process GET
  is still never replayed on its own.
- Submission `param1` to `param7` were `"<qi>"`, `"<ai>"`, `false`, `true`,
  `true`, `false`, `""` (ids as strings). The confirmation page had no
  can-be-graded checkbox (the script looks it up as `CHK_canBeGraded`; on
  a preview confirmation page it is the `attemptCanBeGraded` input), so
  `canBeGraded` stayed `true`. `isRldbUse` was `true` with no LockDown
  Browser involved, because the script applies `Boolean()` to the non-empty
  `HDN_isRldbUse` value. The result was
  `parent.QuizDone(<qi>,<ai>,'0','0','0','gotoSv','')`, and the receipt URL
  added `isInPopup=0&isTimeUp=0`.
- REST: the quiz list and quiz reads worked, but `/quizzes/{qi}/attempts/`
  and `/quizzes/{qi}/attempts/{ai}` returned 403 (`Quizzing.GradeAttempts`)
  before and after submission. The learner's attempt state was readable
  instead from the summary page, the submissions list `quiz_submissions.d2l`
  and the receipt.
- Save all: `1,<page>` saves stored and cleared single-choice,
  multi-select and fill-in-the-blank answers together. Brightspace stored a
  blank's text without its outer spaces and kept inner double spaces,
  `<`, `&amp;` and non-ASCII text as typed.
- Unanswered questions: the confirmation page said "You have N unanswered
  questions." and linked each one as "Question <number>" with
  `Events.ClickQuestion.Raise(0,<page>,<qi>,<ai>,'q<question id>')`.
- Completion: the receipt's heading read "Your work has been saved and
  submitted". The submissions list has one row per attempt, linking
  `quiz_submissions_attempt.d2l?...&qi&ai...` as "Attempt N"; an open
  attempt's row read "Attempt 1 (In progress)", and a submitted one showed
  its grade (e.g. "6 / 25 - 24 %") when the quiz releases it. In the
  sandbox's "View as Student" role the row showed no score.

Learner attempts in the sandbox's "View as Student" role (an untimed quiz
with two true/false questions on one page, and a 4-minute auto-submit quiz
with one question on each of two pages, forward-only) added:

- Resume keeps the clock: an attempt reopened on page 1, before any Next,
  had the same `timeStartedTicks`, the next `DoUtcTimeRequest` counted on
  from the original start, and the page 1 answer was still selected.
  Resuming after a Next (later, on a quiz with one question on each of
  two pages) reopened the attempt on page 2, the page the server held, and
  that attempt was then saved and submitted. So Continue Quiz is how a
  learner client recovers when it cannot tell whether a start or Next
  went through.
- Time-up with the attempt open: `7,1` and `5,1` saves, then
  `ProcessQuizSubmission` with params `<qi>, <ai>, false, true, true,
  true, ""`. The ids were JSON numbers there and strings in a manual
  submit of the same quiz; the server accepted both. The result was
  `parent.QuizDone(<qi>,<ai>,'0','1','0','gotoSv','')`, and the receipt
  URL carried `isTimeUp=1&isprv=0`. The REST `Completed` time (read as the
  instructor) was about 30 seconds after the limit, later than that RPC.
- An abandoned attempt (no request after its last save) was completed by
  the server (REST `Completed`) about 38 seconds after its limit. Its
  receipt gave the attempt's time span as its start to start plus the
  limit, not the completion time.
- Attempt numbers (the REST `AttemptNumber`, "Attempt 1" on the summary)
  restarted at 1 for learner attempts, apart from the preview attempts;
  the attempt ids continued after the preview attempts' ids.

Preview has no resume, as each Start creates a new attempt with a fresh
timer.

## Question media (equations and images)

A learner page in the disposable Brightspace sandbox, with questions from
the question importer (MathML in the HTML, an image field) showed:

- Prompt and option text is HTML in a `d2l-html-block` element's `html`
  attribute. Multiple-choice option text is a `div.d2l-htmlblock-untrusted`
  in the option's table row, with no `<label>`; multi-select options wrap
  the same block in a `<label>`.
- Equations are presentation MathML inline in that HTML
  (`<math xmlns="http://www.w3.org/1998/Math/MathML">` with `msup`, `mfrac`,
  `msqrt` and so on). Equation editors may add a `<semantics>` element with
  a LaTeX `<annotation>`.
- Inline images are `<img src="/content/enforced/<ou>-<code>/<file>">` with
  the author's `alt`, often empty. An image attached to the question is in a
  `div.d2l-quiz-image-container` before the prompt, served as
  `/d2l/common/viewFile.d2lfile/Content/<base64 path>/<file>?ou=<ou>`.
- Both kinds of image returned `image/png` to a plain GET with the
  learner's session. `/content/...` is not an API path, so it is requested
  on the LMS origin, not under the API root.
- The same attempt, continued a day later with `start` and read with
  `page`, parsed all six questions as supported (single choice,
  multi-select, true/false). `images` saved its seven images (the attached
  one and six inline, four of them in choices) as `image/png`, each
  byte-identical to the uploaded file; a file used twice in one question
  is listed and saved once per use. A Unicode identifier stays as typed
  (`<mi>π</mi>` gives `\( π \)`, not `\pi`). The answers saved, and the
  attempt was submitted and verified.

The parser writes equations as LaTeX (`\( … \)`, display math `\[ … \]`),
taking the author's LaTeX annotation when there is one, and images as
`[image N]` or `[image N: alt]`. Each question's `images` lists `number`,
`src` and `alt` in reading order: the attached image, the prompt's, then
each choice's. A question is unsupported when an equation has no clear
LaTeX form (unknown elements, stray text, prefixed `m:math`), an image
source is not a root-relative or web address (`data:`, page-relative), an
image or equation sits outside the prompt and choices, or the question
has other media (SVG, audio, video, objects). `read_quiz_image` downloads
a `src` only from the LMS itself (root-relative, or `https` on the same
host), up to 5 MB, and only when its bytes are PNG, JPEG, GIF or WebP.

## Timed quizzes (timed previews and learner attempts)

- The timer frame renders the limit as script variables: `timeStartedTicks`
  (.NET UTC ticks), `timeLimit` (seconds), `graceLimit`, `lateLimit`,
  `enforceTimeLimit`, `hasAutoSubmit`, `timeExceeded`. The REST attempt
  record carries `AttemptEnforceTimeLimit`, `AttemptSubmissionTimeLimit`
  (minutes) and `AttemptSubmissionLateTypeId` (2 = automatically submit).
- Time left: RPC POST to `/d2l/lms/quizzing/rpc/rpc_functions.d2lfile`
  (`d2l_rh=rpc&d2l_rt=call`) with `d2l_rf=DoUtcTimeRequest` and
  `params={"param1":<timeStartedTicks>,"param2":<timeLimitSeconds>}`. The
  result is `[timeTaken, timeLeft, secondsTaken]`, e.g.
  `["0:00:05","0:01:54","5.92"]`.
- At expiry with auto-submit, the browser saves with `d2l_actionparam=7,<pg>`
  then `5,<pg>` and, with no confirmation page, calls
  `ProcessQuizSubmission` at `quiz_attempt_iframe_auto.d2lfile?ou=<ou>`
  (no `qi`, `ai` or `pg` in the URL, in the preview and the learner
  attempt alike; which query parameters the server requires is unknown)
  with that boolean `true`, then opens the receipt with `isTimeUp=1`.
- With no browser open, the server still submits the attempt: an abandoned
  2-minute auto-submit preview was marked completed about 100 seconds after
  its limit, and an abandoned learner attempt (4 minutes, `graceLimit=0`)
  about 38 seconds after, both with no client request. Two abandoned
  2-minute learner attempts a day later took 148 and 158 seconds, and two
  more 49 and 158 seconds. The varying delay suggests a periodic server
  job, and whether grace or late settings change it is unknown, so do not
  rely on that timing: submit before the limit.
- Past the limit the frame still read `timeExceeded=false` (reads 15 and
  56 seconds after it), and `DoUtcTimeRequest` returned a negative time
  left, e.g. `["0:02:57","0:00:-57","177.2"]`. The deadline is
  `timeStartedTicks` plus `timeLimit`, not that flag.
- While the server submits an expired attempt, the learner summary reads
  "Completed - 9 (Attempt 10 is being processed)" instead of "(Attempt 10
  in progress)", with `continueQuiz=true`, `startQuiz=false` and a disabled
  Start Quiz! button, from the first reads after the limit (21 and 60
  seconds after it) until the REST `Completed` time. The attempt's
  submissions row read "Attempt 10 Auto-grading in progress" 124 seconds
  after the limit, and the row held "in progress" for `verify` from 20
  seconds after the limit until then.
- The limit is fixed when the attempt starts. Raising the quiz's limit
  from 2 to 8 minutes (REST quiz PUT) during an open attempt left its frame
  at `timeLimit=120` with the same `timeStartedTicks`, the REST
  `AttemptSubmissionTimeLimit` at 2, and the summary at "Time Limit 2
  minutes" until the attempt ended; the server submitted it 148 seconds
  after the original limit. The next attempt's frame had `timeLimit=480`.
- The CLI reads a learner attempt's timer frame once, when `start` opens or
  continues it: `quiz_attempt_top_auto.d2l?ou=<ou>&isprv=&impcf=&qi=<qi>&ai=<ai>&dnb=0&cfql=0&fromQB=0&cft=&d2l_body_type=3`.
  An untimed attempt's frame declares the same variables, with
  `enforceTimeLimit=false` (and `timeLimit=7200` in the sandbox), so the
  frame decides, not the quiz's settings: special access can time one
  learner's attempt. The cursor keeps the server's deadline and the server
  clock's offset from the local one, taken from the response's `Date` header
  (whole seconds, stamped after the request was sent, so the largest offset
  it allows is used and the countdown never ends late). Later commands count
  down without a request. Once the deadline passes, `verify` reads the frame
  again while the attempt is still in progress, as extra time or a local
  clock that was reset gives time back. Grace and late limits are not counted.
- In the sandbox the CLI refused `page` past the limit with nothing sent,
  `verify` reported Brightspace had not submitted yet (the frame read again
  gave the same deadline) while the row held "in progress", then reported
  the receipt. The CLI reads the "is being processed" summary only with
  `continueQuiz=true`, `startQuiz=false`, that one marker (no "in progress"
  one) and no enabled Start or Continue button; anything else is still a
  page error. Then `start` refuses before any start POST, naming the
  attempt, whether or not a cursor is kept for it (after `forget`, or from
  another computer). With the cursor kept and its deadline passed, `start`
  refuses with time-up first, before reading the summary. In the sandbox,
  after `forget`, `start` 31 seconds past the limit refused naming the
  attempt, with no POST (it read only the user, the REST quiz and the
  summary), and opened the next attempt once the server had submitted it.
- Extra time and reopened attempts are not observed. The sandbox's learner
  attempts come from the instructor account's "View as Student" role and
  belong to the instructor user. Special access takes only learner-role
  users: REST `PUT /d2l/api/le/1.93/<ou>/quizzes/<qi>/specialaccess/<userId>`
  with `{"SubmissionTimeLimit":{"IsEnforced":true,"TimeLimitValue":8}, ...}`
  returned 404 "Resource Not Found" for the instructor user and 200 for
  the course's learner-role user (read back in the list GET, then deleted),
  and the special access dialog lists only that user. The Grade page, which
  holds Reopen, lists only learner-role users too, with "No Search Results"
  for users with attempts in progress or completed. So the frame re-read in
  `verify` is untested against extra time; D2L's documentation says special
  access added after an attempt starts applies only to new attempts, and
  that Reopen can add extra time.

## Not yet implemented

| Area | Missing workflows / validation |
| --- | --- |
| Quiz authoring | Question creation/import/edit, sections/pools, settings updates, special access, grading |
| Assignments | Learner text submission, group submission, instructor feedback/rubric grading |
| Discussions | Create/reply/edit, attachments, moderation |
| Checklists and surveys | Learner completion/response and instructor authoring |
| Groups and sections | Membership changes, self-enrollment, group creation |
| Grades and progress | Gradebook changes, rubric grading, learner progress |
| Course authoring | Content creation/upload, announcements/calendar writes, import/export |
