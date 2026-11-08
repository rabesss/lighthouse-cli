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
| Folder | `GET .../dropbox/folders/{folder}` | `GroupTypeId` absent or null (an individual-submission folder) |
| Preflight | `GET .../dropbox/folders/{folder}/submissions/user/{user}` (EntityDropbox, 1.88+) | Exactly one block for this user (`EntityType` User, integer `EntityId`); at least one submission; a `Feedback` key present (null or an object); `Status` 1 (Submitted) or 2 (Draft) as an integer or digit string, never 3 (Published), 0 or unknown; existing feedback must be a complete DropboxFeedbackOut with `IsGraded` exactly `false`, no `Score` or `GradedSymbol`, and empty rubric assessments, files and links; existing draft text, or an existing feedback object whose comment is null or empty (`{}`), only with `--replace-draft` |
| Re-check | Both reads again, after an interactive confirmation | A digest of the record's status and existing feedback must match the first preflight; otherwise nothing is sent |
| Save | `POST .../feedback/user/{user}`, one attempt, CSRF header fetched before the preflight | Body `{"Score": null, "Feedback": {"Text": ..., "Html": ...}, "RubricAssessments": [], "IsGraded": false, "GradedSymbol": null}`. The reference marks `Feedback` as RichText. Only HTTP 200, the documented success, counts; the response body is undocumented and ignored |
| Readback 1 | `GET .../feedback/user/{user}` (DropboxFeedbackOut) | The same draft-only checks as the preflight. Every comment part that is present, even an empty string, must match: `Text` equal to the comment (line endings normalized), `Html` equal to what was sent, or the same structure: exactly one `<p>` with no attributes holding only text and attribute-free `<br>` line breaks, whose text is the comment. Only harmless reformatting is allowed: whitespace outside the paragraph, one line break next to a `<p>`, `<br>` or `</p>` tag, entity spelling (complete references to printable characters only), tag case and `<br>`/`<br/>`/`<br />` (non-breaking spaces count as spaces; line breaks, including leading and trailing ones, must match exactly). Any attribute or junk inside a tag, other element, comment, second paragraph, extra `<br>`, unknown or incomplete entity, or other raw line break is a mismatch and gives `unknown`. At least one part must be present |
| Readback 2 | `GET .../submissions/user/{user}` | Same user block, `Status` 2 (Draft), and its `Feedback` passes the same draft-only and comment checks as readback 1 |

`IsGraded: false` is the documented draft state ("If true, the feedback is in
a graded state; otherwise, it's still in draft state"), and the learner's own
`mysubmissions` read includes feedback only once it is published.

A network error, expired session, redirect, interrupt, a success status other than
200, or any HTTP status other than 400, 403 and 404 after the POST was sent, or any readback that does not
match, gives `"outcome": "unknown"` with exit 1. If the readback shows the
feedback as published, graded or scored, the message says so. The command never
retries. Check the submission in Brightspace before running it again. A 400,
403 or 404 on the POST gives `"outcome": "refused"`.

D2L has no conditional write. Another assessor could still publish or grade in
the short gap between the last preflight read and the POST.

## Owner live check

This was not run against the real LMS. Before relying on the command, with a
synthetic course, assignment and test learner that you own:

1. Run `--dry-run --json` on a submission with no feedback. Note what the
   submission record's `Feedback` and `Status` look like (null, `{}` or a
   partial object; 1, `"1"` or something else). The command refuses `{}`, a
   partial feedback object and unknown statuses, which would make it unusable
   if Brightspace returns those for a fresh submission.
2. Save a draft. In the instructor UI, confirm the feedback shows as a draft
   with the comment text intact, including line breaks and characters such as
   `<`, `&` and quotes.
3. As the test learner, confirm that the submission page and
   `lighthouse student assignment-history` show no feedback and no grade.
4. Confirm that no grade is released and that an existing gradebook value for
   that learner is unchanged.
5. If the command reports `"outcome": "unknown"` after a save that worked,
   record the readback shape (`Text`, `Html`, `Status`), because the check
   needs adjusting. The likely causes are D2L rewriting `Text`, storing `Html`
   with attributes, extra elements or more than one paragraph, keeping
   `Status` at 1, or answering the POST with a status other than 200.
6. Add a draft with a rubric assessment or an attachment in the UI and confirm
   the command refuses it.
