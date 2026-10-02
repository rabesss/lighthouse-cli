# Brightspace assessment protocol notes

Findings behind the `student`/`instructor` assessment commands and the
instructor quiz-preview driver. They describe Brightspace (D2L) behavior, which
Lighthouse (MAHE Manipal) runs; they were established on a disposable
Brightspace sandbox and in passive observation of a real Lighthouse attempt.
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

The public quiz API exposes authoring metadata and attempt summaries, but no
documented learner start/answer/submit endpoints. Taking a quiz uses nested
HTML frames and form posts:

1. Summary page `quiz_summary.d2l`, then a start POST (302) through
   `quiz_start_frame_auto.d2l` and `quiz_start_iframe_2_auto.d2l` to
   `quiz_start_process_auto.d2l`, whose script calls
   `parent.GoToAttemptQuizAuto(attemptId, page, 0)`. That GET creates server
   state and must never be replayed. The start POST is multipart to
   `quiz_summary.d2l?...&inProgress=false` with `d2l_action=Custom` and
   `d2l_actionparam=1`; the frame set it opens is
   `quiz_attempt_iframe_auto.d2l` holding the timer
   (`quiz_attempt_top_auto.d2l`), status, save and page frames.
2. Page `quiz_attempt_page_auto.d2l?ou&qi&ai&pg&isprv`. Each question sits in
   a `d2l-quiz-question-autosave-container` with hidden metadata (object id,
   page, `tAtom` group, saved flag). The prompt is either a legacy
   `d2l_read_element_*` element or a single `d2l-html-block` outside the
   answer options.
3. Answer save: multipart POST to `quiz_attempt_save_auto.d2l` with a fresh
   per-request hit code and the question's response-present flag. HTTP 200
   alone does not prove the answer persisted; read the page back and check the
   selected choice and saved marker.
4. Forward navigation: the same save endpoint with `d2l_actionparam=2,...`.
   Forward-only quizzes offer no previous-page control. Requesting a page
   number past the quiz's last page permanently breaks that attempt (every
   later read redirects to `/d2l/error/500`).
5. Submission: preparatory save, confirmation page
   `quiz_confirm_submit_auto.d2l`, then RPC
   `quiz_attempt_iframe_auto.d2lfile?...&pg=<current>&d2l_rh=rpc&d2l_rt=call`
   with `d2l_rf=ProcessQuizSubmission` and **compact** JSON `params`
   (Brightspace answers spaced JSON with an error redirect). Success is the
   callback `parent.QuizDone(quizId, attemptId, ...)`, then a receipt and a
   completed REST attempt record. The save-frame script names the seven params
   `quizId, attemptId, isPreview, canBeGraded, isRldbUse, <flag>,
   cameFromTab`; the confirmation page sends the flag `false`, the time-up
   path `true`. For `isprv=0` the script sets `isPreview=false`, so
   `canBeGraded=true`.
6. Recovery: for a forward-only attempt on page 2, requesting page 1 returns
   page 2 (`pg=2`); the preview driver's read-only `reconcile` relies on this
   only after the full attempt identity matches.

Instructor previews carry `isprv=1`. A real graded attempt observed on
Lighthouse used the same save, confirmation and submission routes, then a
receipt at `quiz_submissions_attempt.d2l?isprv=0`. A learner's start and
resume (the `inProgress=true` path) have not been observed yet; preview has
no resume, as each Start creates a new attempt with a fresh timer.

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
  then `5,<pg>`, calls `ProcessQuizSubmission` with the time-up flag, and
  opens the receipt with `isTimeUp=1`.
- If no browser is open, the server still submits the attempt: an
  abandoned 2-minute attempt was marked completed about 100 seconds after its
  limit, with no client request.

## Not yet implemented

| Area | Missing workflows / validation |
| --- | --- |
| Learner quizzes | Real attempt start, resume, timers, receipt |
| Quiz authoring | Question creation/import/edit, sections/pools, settings updates, special access, grading |
| Assignments | Learner text submission, group submission, instructor feedback/rubric grading |
| Discussions | Create/reply/edit, attachments, moderation |
| Checklists and surveys | Learner completion/response and instructor authoring |
| Groups and sections | Membership changes, self-enrollment, group creation |
| Grades and progress | Gradebook changes, rubric grading, learner progress |
| Course authoring | Content creation/upload, announcements/calendar writes, import/export |
