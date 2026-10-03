# Brightspace assessment protocol notes

Findings behind the `student`/`instructor` assessment commands and the
instructor quiz-preview driver. They describe Brightspace (D2L) behavior, which
Lighthouse (MAHE Manipal) runs; they were established on a disposable
Brightspace sandbox, with a throwaway learner account on a public Brightspace
site, and in passive observation of a real Lighthouse attempt.
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
   also needs the summary form's `password` field. The script's `DoAction`
   also refuses to start while the user is impersonating a role ("View as
   Student"); a direct start POST in that state was not tried. Instructor
   previews are not impersonation and are not affected.
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
   wrongly count it as a visible Save control. The page saves on each change (text on change or blur) and posts the
   whole page form, every question's current value included, with
   `isFinalAutoSave=false`, `useNewFinalAutoSave=true` and
   `timeLimitFromQuiz` (`0` on the untimed quizzes observed). Checked
   multi-select options send `1`; unchecked ones are left out of the form.
   In the captured saves the response named the question object ids the
   server marked saved, comma-separated, in
   `parent.infoFrame.UpdateSaved('<ids>','')`: the changed question for a
   `3,...` save, every saved question on the page for the `5,<page>` save.
   Neither HTTP 200 nor that call proves which value was stored; read the
   page back and check the selected choices and the saved marker.
4. Forward navigation: the same save endpoint with
   `d2l_actionparam=2,<new page>,<current page>`; the learner page script
   (`DoGoNextPage`) builds the same value the preview driver sends.
   Forward-only quizzes offer no previous-page control. Requesting a page
   number past the quiz's last page permanently broke preview attempts
   (every later read redirects to `/d2l/error/500`); treat it as fatal for
   learner attempts too.
5. Submission: preparatory save (`d2l_actionparam=5,<page>`), confirmation
   page `quiz_confirm_submit_auto.d2l`, then RPC
   `quiz_attempt_iframe_auto.d2lfile?...&pg=<current>&d2l_rh=rpc&d2l_rt=call`
   with `d2l_rf=ProcessQuizSubmission` and **compact** JSON `params`
   (Brightspace answers spaced JSON with an error redirect). Success is the
   callback `parent.QuizDone(quizId, attemptId, ...)`, then a receipt and a
   completed REST attempt record (readable with the attempt-grading
   permission; the learner account tested got 403).
   The script passes seven params: `quizId, attemptId, isPreview,
   canBeGraded, isRldbUse, shouldAutoSubmit, cameFromTab`. The confirmation
   page passes `shouldAutoSubmit` as `false`, the time-up path as `true`.
   For a learner `isPreview` is `false` and, with no can-be-graded checkbox
   on the confirmation page, `canBeGraded` is `true`.
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

Preview has no resume, as each Start creates a new attempt with a fresh
timer. A learner's timed attempt has not been observed yet, and a learner's
multi-page Next is known only from the page script: none of the public
site's quizzes is timed or has more than one page.

## Timed quizzes (observed in timed previews)

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
  then `5,<pg>`, calls `ProcessQuizSubmission` with that boolean `true`,
  and opens the receipt with `isTimeUp=1`.
- In one observation with no browser open, the server still submitted the
  attempt: an abandoned 2-minute auto-submit attempt was marked completed
  about 100 seconds after its limit, with no client request. Whether that
  delay is fixed or follows the quiz's grace or late settings is unknown.

## Not yet implemented

| Area | Missing workflows / validation |
| --- | --- |
| Learner quizzes | Real attempt start, resume, answer save, navigation, submit, timers, receipt |
| Quiz authoring | Question creation/import/edit, sections/pools, settings updates, special access, grading |
| Assignments | Learner text submission, group submission, instructor feedback/rubric grading |
| Discussions | Create/reply/edit, attachments, moderation |
| Checklists and surveys | Learner completion/response and instructor authoring |
| Groups and sections | Membership changes, self-enrollment, group creation |
| Grades and progress | Gradebook changes, rubric grading, learner progress |
| Course authoring | Content creation/upload, announcements/calendar writes, import/export |
