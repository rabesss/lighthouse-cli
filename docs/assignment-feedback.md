# Draft assignment feedback

`lighthouse instructor assignment-feedback COURSE_ID FOLDER_ID USER_ID --file PATH [--replace-draft] [--yes] [--dry-run] [--json]`
saves a draft comment on one learner's individual assignment submission
(issue #35). This first slice is comment only: no score, graded symbol,
rubric, attachment, group feedback or publishing.

## Contract

From the D2L developer reference (`res/dropbox.html`, LE 1.82+; the CLI uses
LE 1.93) and the RichText definition in `basic/conventions.html`:

| Step | Request | Checked |
| --- | --- | --- |
| Preflight | `GET .../dropbox/folders/{folder}/submissions/user/{user}` (EntityDropbox, 1.88+) | Exactly one block for this user (`EntityType` User, integer `EntityId`); at least one submission; `Status` an integer 1 (Submitted) or 2 (Draft), never 3 (Published), 0 or unknown; existing feedback `IsGraded` exactly `false`, no `Score`, `GradedSymbol`, rubric assessments, files or links; existing draft text only with `--replace-draft` |
| Re-check | The same GET, after an interactive confirmation | The result must match the first preflight; otherwise nothing is sent |
| Save | `POST .../feedback/user/{user}`, one attempt, CSRF header | Body `{"Score": null, "Feedback": {"Text": ..., "Html": ...}, "RubricAssessments": [], "IsGraded": false, "GradedSymbol": null}`. The reference marks `Feedback` as RichText. The response body is undocumented and ignored |
| Readback 1 | `GET .../feedback/user/{user}` (DropboxFeedbackOut) | `IsGraded` `false`, no score or symbol, and `Text` equal to the comment (or `Html` equal to what was sent) |
| Readback 2 | `GET .../submissions/user/{user}` | Same user block, `Status` 2 (Draft), `Feedback.IsGraded` `false` |

`IsGraded: false` is the documented draft state ("If true, the feedback is in
a graded state; otherwise, it's still in draft state"), and the learner's own
`mysubmissions` read includes feedback only once it is published.

A network error, expired session, HTTP 429, 5xx or redirect after the POST was
sent, or any readback that does not match, gives `"outcome": "unknown"` with
exit 1. The command never retries. Check the submission in Brightspace before
running it again. A 403 or 404 on the POST is a refusal.

D2L has no conditional write. Another assessor could still publish or grade in
the short gap between the last preflight read and the POST.

## Owner live check

This was not run against the real LMS. Before relying on the command, with a
synthetic course, assignment and test learner that you own:

1. Run `--dry-run --json` on a submission with no feedback. Note what the
   submission record's `Feedback` and `Status` look like (null or an empty
   object; 1 or something else).
2. Save a draft. In the instructor UI, confirm the feedback shows as a draft
   with the comment text intact, including line breaks and characters such as
   `<`, `&` and quotes.
3. As the test learner, confirm that the submission page and
   `lighthouse student assignment-history` show no feedback and no grade.
4. Confirm that no grade is released and that an existing gradebook value for
   that learner is unchanged.
5. If the command reports `"outcome": "unknown"` after a save that worked,
   record the readback shape (`Text`, `Html`, `Status`), because the check
   needs adjusting. The likely causes are D2L rewriting `Text` or keeping
   `Status` at 1.
6. Add a draft with a rubric assessment or an attachment in the UI and confirm
   the command refuses it.
