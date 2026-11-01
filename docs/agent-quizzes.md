# Taking a teacher's quiz with an agent

The CLI supports this sequence: discover a quiz, wait until it opens, start or
continue the signed-in learner's attempt, read its questions, save answers,
move through its pages, submit, and verify the receipt. Learner attempts are
real, graded attempts. Choosing answers and deciding when to run commands
belong to the calling agent.

## Discover and wait

Run the installed `lighthouse` command. Put `--json` on each leaf command;
it is not a global option.

```bash
lighthouse auth status --json
lighthouse courses --json
lighthouse quizzes COURSE_ID --json
lighthouse quiz COURSE_ID QUIZ_ID --json
```

Check exit codes and parse stdout as one JSON document. Diagnostics go to
stderr. Use course and quiz IDs from these results, rather than guessed names
or question IDs. Quiz listings contain settings and dates, not attempt
questions. Instructors' `quiz-questions` is a different permission-gated
surface, not a way to read a learner's attempt.

The calling agent can repeat the quiz listing to discover newly visible
teacher quizzes and wait for their start dates. There is no built-in watcher
or scheduler. Recheck authentication when the quiz opens, especially after a
long wait. A working session or configured
[unattended sign-in](auth-unattended.md) is needed; attempt operations themselves
never sign in automatically or replay writes.

Before starting, check the quiz's dates, attempt allowance and time limit.
An open attempt may resume instead of creating a new one:

```bash
lighthouse student attempt status COURSE_ID QUIZ_ID --json
lighthouse student attempt start COURSE_ID QUIZ_ID --yes --json
```

`--yes` allows the remote change without a terminal prompt. If the quiz has
not opened, a refusal may include `opens_at` (UTC) and
`seconds_until_open`. Wait until then and recheck. Other refusals must be
resolved, not treated as a countdown. `start --dry-run` only describes the
command: it does not contact Brightspace, check availability, or inspect the
questions.

A successful start returns `attempt_id`, `page`, `questions`,
`has_next_control`, `has_previous_control`, `timer`, `quiz` and
`resumed`. Its clock keeps running between commands.

## Read, answer and move

`start` already returns the first current page; `page` reads it again when
needed:

```bash
lighthouse student attempt page COURSE_ID QUIZ_ID --json
lighthouse student attempt images COURSE_ID QUIZ_ID --json
```

Each question has `question_id`, `number`, `text`, `kind`,
`supported`, `choices`, `selected_choice_ids`, `blanks`, `images`
and `saved`. Choices have `choice_id` and text. Use these IDs exactly as
returned, even when the teacher randomizes questions or choices.

Equations are returned as LaTeX. Image markers refer to the files listed by
`images`; inspect those files before answering image-dependent questions.
Check each image entry for an `error` even if the command exits successfully.

The answer object maps question IDs to the following values:

| Question kind | Answer value |
| --- | --- |
| `single-choice` (including rendered true/false radios) | One returned choice ID as a string |
| `multi-select` | A list of returned choice IDs |
| `fill-blank` | A list of texts in the order of the returned blanks |

For example, using synthetic IDs:

```bash
lighthouse student attempt answer COURSE_ID QUIZ_ID \
  --answers '{"101":"o2","102":["o11","o13"],"103":["four","six"]}' --yes --json
```

Questions omitted from the object keep their current answers. Each changed
answer is saved separately and read back before success is reported. Repeating
an answer that is already saved does not send another save.

After reading and answering a page, use `has_next_control` to decide:

- If true, use `next --yes --json`, or use `answer --next --yes --json`
  to save and advance together.
- If false, save with `answer` without `--next`, then submit.
  `answer --next` on the last page refuses before saving any of that batch.

`previous --yes --json` works only where the quiz permits moving back.
For a forward-only quiz, finish each page before leaving it. Commands return
the verified current page after a save or page change.

Only the three question kinds above are supported. An unsupported question
blocks every write on its page, including navigation and submission, because
Brightspace posts the whole page form. Do not try to skip it with
`--allow-unanswered`. Complete that attempt through Brightspace instead.
Password-protected quizzes and quizzes requiring a browser step such as
LockDown Browser are also refused.

## Submit and verify

On the last page, after checking saved answers and the time remaining:

```bash
lighthouse student attempt submit COURSE_ID QUIZ_ID --yes --json
lighthouse student attempt verify COURSE_ID QUIZ_ID --json
```

Success includes `submitted: true`, the attempt identity and attempt number.
Scores are returned only when Brightspace releases them. A successful HTTP
request alone does not establish submission; the CLI checks both the
submissions list and receipt.

By default, submission stops if any question in the quiz is unanswered, and
returns an `unanswered` list with page/question IDs and question numbers.
`--allow-unanswered` is an explicit decision to leave answers blank, not a
normal recovery option. After verified submission, stop: another `start`
can consume another allowed attempt.

Timed replies include `timer.seconds_left`, `timer.ends_at` (UTC),
`timer.limit_seconds` and `timer.auto_submit`, or `timer: null` for an
unenforced limit. Allow time for requests, reasoning and final verification;
submit before the countdown reaches zero. With auto-submit enabled, the CLI
sends no further attempt changes after expiry. Call `verify` to check whether
Brightspace has submitted it; if processing is still underway, wait and check again.

## Recover without replaying a write

After an error or an interrupted process, read
`student attempt status COURSE_ID QUIZ_ID --json`. It reports the local
checkpoint, not the current remote state. It contains no answer history.

| Local state | Next action |
| --- | --- |
| `uncertain`, operation `answer` | Run `page` to settle the save and read stored answers; then supply only the intended changes. Some earlier answers in the batch may already be saved. |
| `uncertain`, operation `next` or `previous` | Run `start --yes` to resume where Brightspace has the same attempt. Do not repeat the page-change command. |
| `uncertain`, operation `start`, known `attempt_id` | Run `start --yes` to continue only. If the attempt ended elsewhere, use `verify`. |
| `uncertain`, operation `start`, `attempt_id: null` | Run `start --yes` to continue only. If no Continue exists, inspect Brightspace before deliberate `forget`; `verify` cannot locate a receipt without the attempt ID. |
| `uncertain`, operation `submit` | Run `verify` first. If it explicitly reports still in progress, `start --yes` can continue that attempt. Do not blindly submit again. |
| `submitted` | Stop; use `verify` if a fresh receipt check is needed. |
| `active` | Use `page` to read the saved answers before continuing. |

For session expiry, authenticate separately with `auth login` (or the
configured unattended read flow), then follow the checkpoint's recovery step.
When an auto-submit timer has expired, use `verify`; it can report that
Brightspace is still processing the attempt. Stop on account mismatches or
an unreadable checkpoint and inspect the remote attempt before recovery.

`forget --yes` removes the local checkpoint and can allow a later
`start` to create another graded attempt. Use it only after inspecting
Brightspace and deliberately deciding that a new attempt is wanted.

Errors currently include a human-readable `error`; some also include
structured countdown or unanswered-question data. There is no versioned error
code or recovery-action schema. Use the checkpoint's state/operation plus the
error details, and stop on an unrecognized failure. Serialize commands for the
same course/quiz: the CLI locks each operation, but does not coordinate the
intent of multiple independent agents across commands.
