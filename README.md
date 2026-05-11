# Lighthouse CLI

CLI tool for interacting with the D2L Brightspace LMS at
[lighthouse.manipal.edu](https://lighthouse.manipal.edu) (Manipal Academy of
Higher Education). Uses the D2L REST API directly — no browser automation,
no Selenium, no headless Chrome needed for data access.

Built so that AI agents (Hermes, Claude Code, etc.) can interact with the
university's LMS through terminal commands, but equally useful for students
who want quick access to their courses from the shell.

## Quick Start

```bash
cd ~/Desktop/clawds-code-crib/lighthouse-cli
pip install -e .

# Authenticate (headless browser SSO with 2FA support)
lighthouse auth login

# Or extract session cookies from your browser via CDP
lighthouse auth refresh --cdp-port 34165

# Verify the session is alive
lighthouse auth status

# Explore
lighthouse courses
lighthouse content 44347
lighthouse download 44347 --dry-run
lighthouse grades 36060

# Incremental sync — only download new/changed files
lighthouse sync 44347

# Assignments
lighthouse assignments 44347

# Submit a file to a dropbox folder
lighthouse submit -f my_homework.pdf 44347 5678 --yes
```

> **Prerequisite for `auth refresh`:** Chrome/Chromium must be running with
> `--remote-debugging-port=34165` and you must be logged in to
> lighthouse.manipal.edu in that browser.
>
> **Prerequisite for `auth login`:** Playwright must be installed
> (`playwright install chromium`). This method launches a headless browser
> for SSO authentication — no pre-running browser needed.

## Architecture

```
┌──────────────────┐    Playwright    ┌──────────────────────┐
│  Headless        │◄────────────────►│  lighthouse auth     │
│  Chromium        │   SSO + 2FA      │  login               │
└──────────────────┘                  └──────────┬───────────┘
                                                  │ cookies
                                                  ▼
┌──────────────┐     CDP      ┌───────────────────────┐
│  Browser     │◄────────────►│  lighthouse auth      │
│  (Chrome)    │   port 34165 │  refresh              │
└──────────────┘              └──────────┬────────────┘
                                         │ cookies
                                         ▼
                              ~/.config/lighthouse-cli/
                                 cookies.json
                                         │
┌──────────────┐    REST      ┌──────────┴───────────┐
│  lighthouse  │◄────────────►│  lighthouse.manipal  │
│  CLI         │   D2L API    │  .edu (D2L)          │
└──────┬───────┘              └──────────────────────┘
       │
       │  ┌─────────────────┐     ┌─────────────────────┐
       │  │  Manifest files  │     │  CredentialStore     │
       │  │  .manifest.json  │     │  (Fernet + keyring)  │
       │  └─────────────────┘     └─────────────────────┘
       │
       │  Manifest-based sync
       │  (incremental, dedup)
       │
       ▼
  ~/Downloads/lighthouse/
    {course-name}/
      .manifest.json        ← SHA-256 hashes per file
      Unit 1/
        file1.pdf
        file2.pdf
```

- **Site:** lighthouse.manipal.edu runs D2L Brightspace LMS.
- **Auth (CDP):** Session-based cookie auth. Four cookies are needed:
  `d2lSecureSessionVal`, `d2lSessionVal`, `d2lSameSiteCanaryA`,
  `d2lSameSiteCanaryB`. Extracted from the browser via Chrome DevTools
  Protocol (CDP) — either through `browser-harness` CLI, Python websockets,
  or a Node.js fallback.
- **Auth (headless):** Playwright-based headless browser authentication.
  Launches Chromium, navigates to SSO login page, waits for user to complete
  authentication (including 2FA), then extracts session cookies. Supports
  `CredentialStore` with Fernet encryption and keyring fallback for secure
  credential storage.
- **API:** D2L REST API — LE v1.93, LP v1.59.
- **Cookie storage:** `~/.config/lighthouse-cli/cookies.json` (permissions
  `0600`). Override with `LIGHTHOUSE_CONFIG_DIR` env var.
- **Download directory:** `~/Downloads/lighthouse/{course-name}/`. Downloads
  create course-name subdirectories. Override with `--output-dir` / `-o`.
- **Manifest files:** `.manifest.json` files stored in download directories
  track SHA-256 hashes of previously downloaded files for incremental sync
  and deduplication.
- **Session lifetime:** Cookies expire (typically when the browser session
  ends or D2L rotates them). Re-run `lighthouse auth refresh` or
  `lighthouse auth login` when commands fail with "Session expired".

## Command Reference

Every command accepts `--json` for machine-readable output. All commands
return exit code 0 on success, 1 on error.

---

### `lighthouse auth status`

Check whether the stored session cookies are still valid.

**Flags:** `--json`

**API call:** `GET /d2l/api/versions/` (lightweight ping)

**Human output:**
```
Session valid. Cookies: d2lSameSiteCanaryA, d2lSameSiteCanaryB, d2lSecureSessionVal, d2lSessionVal
```

**JSON output (`--json`):**
```json
{
  "valid": true,
  "cookies": ["d2lSameSiteCanaryA", "d2lSameSiteCanaryB", "d2lSecureSessionVal", "d2lSessionVal"]
}
```

---

### `lighthouse auth login [--cdp-port PORT] [--timeout SECONDS]`

NEW headless browser authentication using Playwright. Launches headless
Chromium, navigates to the SSO login page, and waits for the user to
complete authentication (including 2FA). Extracts session cookies and
stores them.

**Flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--cdp-port` | — | Optional CDP port for debugging the headless browser |
| `--timeout` | `120` | Seconds to wait for authentication to complete (accounts for 2FA) |
| `--json` | — | Machine-readable output |

**Authentication flow:**

1. Launches headless Chromium via Playwright
2. Navigates to the SSO login page
3. Opens a visible browser window for user interaction
4. Waits for the user to complete login (including 2FA if required)
5. Extracts D2L session cookies from the authenticated session
6. Stores cookies using `CredentialStore` (Fernet encryption with keyring fallback)

**Human output:**
```
Auth login successful. Cookies stored.
```

**JSON output (`--json`):**
```json
{
  "valid": true,
  "cookies": ["d2lSameSiteCanaryA", "d2lSameSiteCanaryB", "d2lSecureSessionVal", "d2lSessionVal"]
}
```

---

### `lighthouse auth refresh [--cdp-port PORT]`

Extract fresh D2L session cookies from the browser and persist them to disk.

**Flags:**

| Flag | Default | Env var | Description |
|------|---------|---------|-------------|
| `--cdp-port` | `34165` | `LIGHTHOUSE_CDP_PORT` | Chrome DevTools Protocol port |

Also accepts `--json`.

**Cookie extraction strategy (in order):**

1. `browser-harness` CLI tool (if installed)
2. Direct CDP via Python `websockets` library
3. Node.js one-liner fallback

**API call:** `GET /d2l/api/versions/` (verification after extraction)

**Human output:**
```
Auth refreshed and verified. Cookies: d2lSameSiteCanaryA, d2lSameSiteCanaryB, d2lSecureSessionVal, d2lSessionVal
```

**JSON output (`--json`):**
```json
{
  "valid": true,
  "cookies": ["d2lSameSiteCanaryA", "d2lSameSiteCanaryB", "d2lSecureSessionVal", "d2lSessionVal"]
}
```

---

### `lighthouse semesters`

List all semesters visible to the authenticated user.

**Flags:** `--json`

**API call:** `GET /d2l/le/manageCourses/api/mysemesters`

**Human output:**
```
Semesters
ID      Name                   Code
------  ---------------------  -----------------
58272   AY 2025-2026 | Sem IV  0902_IV_2025-2026
58271   AY 2025-2026 | Sem III 0902_III_2025-2026
...
```

**JSON output (`--json`):** Array of semester objects with `OrgUnitId`,
`Name`, `Code`, etc.

---

### `lighthouse courses [--semester FILTER] [--json]`

List courses visible to the authenticated user.

**Flags:**

| Flag | Description |
|------|-------------|
| `-s`, `--semester` | Filter by semester name (pipe-delimited segment match) or semester OrgUnitId |
| `--json` | Output raw JSON |

**API calls:**
- `GET /d2l/le/manageCourses/api/mycourses` → `{Courses: [...]}`
- `GET /d2l/le/manageCourses/api/mysemesters` (for filter resolution)

**Semester filter:** Uses pipe-delimited matching against semester names. For
example, `"Sem I"` matches `AY 2024-25 | Sem I` but NOT `AY 2024-25 | Sem II`
because it checks the full pipe-delimited segment. Supported filters:
`"Sem I"`, `"Sem II"`, `"Sem III"`, `"Sem IV"`.

Because the learner role cannot access the orgstructure API (returns 403),
course-to-semester mapping uses course-code suffix heuristics:

| Semester | Course code pattern | Example |
|----------|---------------------|---------|
| Sem I | ends with `_24` | `BME_101_24` |
| Sem II | contains `_2024-2025` | `BME_201_2024-2025` |
| Sem III | contains `_2025-2026` | `BME_301_2025-2026` |
| Sem IV | contains `_2025-2026` | `BME_401_2025-2026` |

**Human output:**
```
Courses (6)
ID      Name                                 Active
------  ------------------------------------  ------
44347   Signals & Systems                    ✔
44348   Engineering Mathematics III          ✔
44349   Anatomy & Physiology                 ✔
44350   Network Analysis                     ✔
44351   Electronic Circuits                  ✔
44352   Digital System Design                ✔
```

**JSON output (`--json`):** Array of course objects with `OrgUnitId`, `Name`,
`Code`, `IsActive`, etc.

---

### `lighthouse content COURSE_ID [--json]`

Show the content tree (modules > submodules > topics) for a course.

**Arguments:**

| Argument | Description |
|----------|-------------|
| `COURSE_ID` | Numeric OrgUnitId (e.g. `44347`) **or** name substring (e.g. `signals`) |

**Flags:** `--json`

**API call:** `GET /d2l/api/le/1.93/{orgId}/content/toc`

When using a name substring, the tool fetches the course list and matches
case-insensitively. Ambiguous matches print all candidates and exit with
code 1.

**Human output:**
```
📁 Unit 1 - Introduction to Signals
  📄 L1-L2 Introduction to computing.pdf  [id:12345]
  📄 L3 Signal Classification.pdf          [id:12346]
📁 Unit 2 - Systems
  📄 L4 LTI Systems.pdf                   [id:12347]
  🔗 Reference Material                   [id:12348]
```

Icons: `📁` module, `📄` file, `🔗` link, `📎` other.

**JSON output (`--json`):** Full nested TOC object as returned by the API
with `Modules` containing sub-`Modules` and `Topics`.

---

### `lighthouse download COURSE_ID [TOPIC_ID] [-o DIR] [--dry-run] [--include-assignments] [--types TYPES] [--json]`

Download files from a course.

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `COURSE_ID` | Yes | Numeric OrgUnitId or name substring |
| `TOPIC_ID` | No | Specific topic to download. If omitted, downloads all files |

**Flags:**

| Flag | Description |
|------|-------------|
| `-o`, `--output-dir` | Custom download directory (default: `~/Downloads/lighthouse/{course-name}/`) |
| `--dry-run` | List files that would be downloaded without actually downloading |
| `--include-assignments` | Also download assignment attachments from dropbox folders |
| `--types` | Filter by file type (comma-separated, e.g. `--types pdf,docx`) |
| `--json` | Output structured JSON (download plan or result) |

**API calls:**
- `GET /d2l/api/le/1.93/{orgId}/content/toc` (to enumerate topics)
- `GET /d2l/api/le/1.93/{orgId}/content/topics/{topicId}/file` (per file)
- `GET /d2l/api/le/1.93/{orgId}/dropbox/folders/` (when `--include-assignments`)
- `GET /d2l/api/le/1.93/{orgId}/dropbox/folders/{folderId}/attachments/{fileId}` (assignment attachments)

Downloads preserve the module path structure from the content tree. Only
topics with `TypeIdentifier == "File"` are downloaded (links are skipped).
Downloads now create course-name subdirectories (e.g.
`~/Downloads/lighthouse/Signals & Systems/` instead of
`~/Downloads/lighthouse/44347/`).

**Human output (single file):**
```
Downloaded: ~/Downloads/lighthouse/Signals & Systems/Unit 1/L1-L2 Introduction.pdf (245.3 KB)
```

**Human output (all files, `--dry-run`):**
```
Would download 12 files to ~/Downloads/lighthouse/Signals & Systems/

  [12345] L1-L2 Introduction to computing.pdf
  [12346] L3 Signal Classification.pdf
  [12347] L4 LTI Systems.pdf
  ...
```

**JSON output (`--json`, `--dry-run`):**
```json
[
  {"topic_id": 12345, "title": "L1-L2 Introduction to computing.pdf", "path": "Unit 1/L1-L2 Introduction to computing.pdf"},
  ...
]
```

**JSON output (`--json`, single file download):**
```json
{
  "path": "/home/user/Downloads/lighthouse/Signals & Systems/L1-L2 Introduction.pdf",
  "size_kb": 245.3,
  "filename": "L1-L2 Introduction.pdf"
}
```

---

### `lighthouse sync COURSE_ID [TOPIC_ID] [-o DIR] [--semester FILTER] [--also COURSE] [--force] [--json]`

Incremental sync command that downloads only new or changed files using
manifest-based tracking with SHA-256 dedup.

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `COURSE_ID` | Yes | Numeric OrgUnitId or name substring |
| `TOPIC_ID` | No | Specific topic to sync. If omitted, syncs all files |

**Flags:**

| Flag | Description |
|------|-------------|
| `-o`, `--output-dir` | Custom download directory (default: `~/Downloads/lighthouse/{course-name}/`) |
| `--semester` | Resolve to latest semester and sync all its courses |
| `--also` | Additional courses to sync (can be specified multiple times) |
| `--force` | Force re-download of all files, ignoring manifest |
| `--json` | Output structured JSON result |

**How it works:**

1. Loads the manifest file (`.manifest.json`) from the download directory
2. Fetches the current content tree from the API
3. Computes SHA-256 hashes for each file in the tree
4. Compares against manifest — only downloads files that are new or have
   changed hashes
5. Updates the manifest with current file hashes
6. Reports orphaned topics (files in manifest but no longer in content tree)

**Manifest files:** Stored as `.manifest.json` in the download directory.
Contains a mapping of file paths to their SHA-256 hashes:

```json
{
  "Unit 1/L1-L2 Introduction.pdf": {
    "sha256": "a1b2c3d4...",
    "topic_id": 12345,
    "last_synced": "2025-05-10T14:30:00Z"
  }
}
```

**Multi-course scope:**
- `--semester` resolves to the latest semester and syncs all its courses
- `--also` adds additional courses (by ID or name) to the sync scope
- Each course gets its own subdirectory and manifest file

**Human output:**
```
Syncing Signals & Systems...
  New:      3 files
  Updated:  1 file
  Unchanged: 8 files
  Orphaned:  2 topics (files no longer in content tree)
  Downloaded: 4.2 MB
```

**JSON output (`--json`):**
```json
{
  "course_id": 44347,
  "new": 3,
  "updated": 1,
  "unchanged": 8,
  "orphaned": 2,
  "downloaded_bytes": 4404019,
  "files": [
    {"path": "Unit 2/New Notes.pdf", "status": "new", "size_kb": 312.5},
    {"path": "Unit 1/Updated Slides.pdf", "status": "updated", "size_kb": 156.8}
  ]
}
```

---

### `lighthouse assignments COURSE_ID [--json]`

List assignments (dropbox folders) for a course.

**Arguments:**

| Argument | Description |
|----------|-------------|
| `COURSE_ID` | Numeric OrgUnitId or name substring |

**Flags:** `--json`

**API call:** `GET /d2l/api/le/1.93/{orgId}/dropbox/folders/` (handles
pagination automatically)

**Human output:**
```
Assignments – Signals & Systems
ID    Name                          Due Date            Status
----  ----------------------------  ------------------  --------
5678  Homework 1                    2025-05-15 23:59    Open
5679  Lab Report 2                  2025-05-20 23:59    Open
5680  Final Project                 2025-06-01 23:59    Closed
```

**JSON output (`--json`):**
```json
{
  "course_id": 44347,
  "assignments": [
    {
      "Id": 5678,
      "Name": "Homework 1",
      "DueDate": "2025-05-15T23:59:00Z",
      "Availability": {
        "StartDate": null,
        "EndDate": "2025-05-15T23:59:00Z",
        "IsAvailable": true
      }
    }
  ]
}
```

---

### `lighthouse grades [COURSE_ID] [--json]`

Show grades. If `COURSE_ID` is omitted, shows grades for all courses.

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `COURSE_ID` | No | Numeric OrgUnitId or name substring |

**Flags:** `--json`

**API calls:**
- `GET /d2l/api/le/1.93/{orgId}/grades/` — grade schema (names, weights, max points)
- `GET /d2l/api/le/1.93/{orgId}/grades/values/myGradeValues/` — actual grade values

Merges the two responses using `GradeObjectIdentifier` (string) from the
values API matched against `Id` from the schema API. Shows
`PointsNumerator/PointsDenominator` when available.

**Human output:**
```
Grades – PSUC
Item                    Grade    Weight  Type
----------------------  -------  ------  --------
CAT 1                   18/20    15%     Points
Assignment 1            9/10     10%     Points
Midterm                 –/50     25%     Points
```

**JSON output (`--json`):**
```json
{
  "course_id": 36060,
  "grades": [
    {"name": "CAT 1", "grade": "18/20", "weight": "15%", "type": "Points"},
    {"name": "Assignment 1", "grade": "9/10", "weight": "10%", "type": "Points"},
    {"name": "Midterm", "grade": "–/50", "weight": "25%", "type": "Points"}
  ]
}
```

---

### `lighthouse submit -f FILE COURSE_ID FOLDER_ID [--yes] [--json]`

Submit a file to a D2L dropbox folder.

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `FILE` | Yes | Path to the file to submit (via `-f` / `--file`) |
| `COURSE_ID` | Yes | Numeric OrgUnitId or name substring |
| `FOLDER_ID` | Yes | Numeric folder ID or name substring |

**Flags:**

| Flag | Description |
|------|-------------|
| `-f`, `--file` | Path to the file to submit (required) |
| `--yes` | Skip confirmation prompt (also auto-skipped in non-TTY) |
| `--json` | Output structured JSON result |

**API call:** `POST /d2l/api/le/1.93/{orgId}/dropbox/folders/{folderId}/submissions/mysubmissions/`
(multipart/mixed body)

**Resolution:**
- `COURSE_ID`: numeric OrgUnitId or case-insensitive name substring match
- `FOLDER_ID`: numeric folder ID or case-insensitive name substring match
  against assignment names

**Confirmation:** Prompts for confirmation before submitting. Skipped with
`--yes` or when running in a non-TTY environment (e.g. from an agent).

**Error handling:**
- Session expired → prints message to stderr, exit code 1
- Permission denied → folder not accessible, exit code 1
- Folder not found → lists available folders, exit code 1
- Server error → reports HTTP status, exit code 1

**Human output:**
```
Submit homework.pdf to "Homework 1" in Signals & Systems? [y/N]: y
Submitted successfully. Submission ID: 12345
```

**JSON output (`--json`):**
```json
{
  "submission_id": 12345,
  "folder_id": 5678,
  "course_id": 44347,
  "file": "homework.pdf",
  "submitted_at": "2025-05-10T15:30:00Z"
}
```

---

### `lighthouse announcements [COURSE_ID] [--json]`

Show announcements. If `COURSE_ID` is omitted, shows announcements for all
courses (courses with no announcements are skipped silently).

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `COURSE_ID` | No | Numeric OrgUnitId or name substring |

**Flags:** `--json`

**API call:** `GET /d2l/api/le/1.93/{orgId}/news/`

**Human output:**
```
📢 Signals & Systems
  [2025-05-08 14:30] Midsem schedule update
    The midsem examination for Signals & Systems has been rescheduled...
    📎 updated_schedule.pdf (156 KB)
```

**JSON output (`--json`):**
```json
{
  "course_id": 44347,
  "announcements": [
    {
      "Id": 9999,
      "Title": "Midsem schedule update",
      "Body": {"Text": "...", "Html": "..."},
      "CreatedDate": "2025-05-08T14:30:00Z",
      "Attachments": [...]
    }
  ]
}
```

---

### `lighthouse calendar [COURSE_ID] [--json]`

Show calendar events. If `COURSE_ID` is omitted, shows events for all courses.

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `COURSE_ID` | No | Numeric OrgUnitId or name substring |

**Flags:** `--json`

**API call:** `GET /d2l/api/le/1.93/{orgId}/calendar/events/`

**Human output:**
```
Calendar – Signals & Systems
Date              Title                        Course
----------------  ---------------------------- ----------------------
2025-05-15 10:00  Midsem Examination           Signals & Systems
2025-05-20 23:59  Assignment 3 Deadline        Signals & Systems
```

**JSON output (`--json`):**
```json
{
  "course_id": 44347,
  "events": [
    {
      "CalendarEventId": "...",
      "Title": "Midsem Examination",
      "StartDateTime": "2025-05-15T10:00:00Z",
      "EndDateTime": "2025-05-15T12:00:00Z",
      "OrgUnitName": "Signals & Systems"
    }
  ]
}
```

---

### `lighthouse quizzes [COURSE_ID] [--json]`

Show quizzes. If `COURSE_ID` is omitted, shows quizzes for all courses.

**Arguments:**

| Argument | Required | Description |
|----------|----------|-------------|
| `COURSE_ID` | No | Numeric OrgUnitId or name substring |

**Flags:** `--json`

**API call:** `GET /d2l/api/le/1.93/{orgId}/quizzes/` (handles pagination
automatically — follows `Next` links until exhausted)

**Human output:**
```
Quizzes – Signals & Systems
ID    Name                          Start               End
----  ----------------------------  ------------------  ------------------
101   Quiz 1 - Signal Basics        2025-05-10 10:00    2025-05-10 10:30
102   Quiz 2 - Fourier Transform    2025-05-17 10:00    2025-05-17 10:30
```

**JSON output (`--json`):**
```json
{
  "course_id": 44347,
  "quizzes": [
    {
      "QuizId": 101,
      "Name": "Quiz 1 - Signal Basics",
      "StartDate": "2025-05-10T10:00:00Z",
      "EndDate": "2025-05-10T10:30:00Z"
    }
  ]
}
```

## API Endpoints

All endpoints are relative to `https://lighthouse.manipal.edu`.

| Feature | Method | Endpoint | Notes |
|---------|--------|----------|-------|
| API versions | GET | `/d2l/api/versions/` | Used for auth verification |
| Semesters | GET | `/d2l/le/manageCourses/api/mysemesters` | |
| Departments | GET | `/d2l/le/manageCourses/api/mydepartments` | |
| Roles | GET | `/d2l/le/manageCourses/api/myroles` | |
| Courses | GET | `/d2l/le/manageCourses/api/mycourses` | Returns `{Courses: [...]}` |
| Content TOC | GET | `/d2l/api/le/1.93/{orgId}/content/toc` | Nested Modules/Topics |
| Topic details | GET | `/d2l/api/le/1.93/{orgId}/content/topics/{topicId}` | Returns topic details including HTML content |
| File download | GET | `/d2l/api/le/1.93/{orgId}/content/topics/{topicId}/file` | Binary response with `Content-Disposition` |
| Dropbox folders | GET | `/d2l/api/le/1.93/{orgId}/dropbox/folders/` | Paginated, returns assignment/dropbox info |
| Download attachment | GET | `/d2l/api/le/1.93/{orgId}/dropbox/folders/{folderId}/attachments/{fileId}` | Binary download |
| Submit file | POST | `/d2l/api/le/1.93/{orgId}/dropbox/folders/{folderId}/submissions/mysubmissions/` | Multipart/mixed body |
| Announcements | GET | `/d2l/api/le/1.93/{orgId}/news/` | |
| Grade schema | GET | `/d2l/api/le/1.93/{orgId}/grades/` | Grade objects with name, weight, max points |
| My grades | GET | `/d2l/api/le/1.93/{orgId}/grades/values/myGradeValues/` | Returns `GradeObjectIdentifier` (string) |
| Quizzes | GET | `/d2l/api/le/1.93/{orgId}/quizzes/` | Paginated: `{Objects: [...], Next: url\|null}` |
| Calendar | GET | `/d2l/api/le/1.93/{orgId}/calendar/events/` | |

## Course Map

Current student — BME (Biomedical Engineering), MIT Manipal:

**Sem I (AY 2024-25)**

| OrgUnitId | Course |
|-----------|--------|
| 29728 | Mechanics of Solids |
| 29731 | Basic Electronics |
| 29733 | Communication Skills |
| 29734 | Engineering Mathematics I |
| 29735 | Basic Mechanical Engineering |
| 29736 | Engineering Physics |

**Sem II (AY 2024-25)**

| OrgUnitId | Course |
|-----------|--------|
| 36040 | Engineering Mathematics II |
| 36059 | Biology |
| 36060 | PSUC |
| 36061 | Environmental Studies |
| 36062 | Engineering Chemistry |
| 363 | Basic Electrical Technology |
| 36064 | Basic Electronics |
| 36067 | Engineering Physics |

**Sem III (AY 2025-26)**

| OrgUnitId | Course |
|-----------|--------|
| 44347 | Signals & Systems |
| 44348 | Engineering Mathematics III |
| 44349 | Anatomy & Physiology |
| 44350 | Network Analysis |
| 44351 | Electronic Circuits |
| 44352 | Digital System Design |

**Sem IV (AY 2025-26):** No courses yet (OrgUnitId 58272).

## Gotchas & Notes

- **Cookie expiration:** Cookies expire. When they do, every command will
  print `Session expired. Run: lighthouse auth refresh` to stderr and exit
  with code 1. Use `auth login` for headless browser-based re-authentication.
- **GradeObjectIdentifier vs GradeObjectId:** The `myGradeValues` API returns
  `GradeObjectIdentifier` (a string), not `GradeObjectId` (an int). The merge
  logic in `cmd_grades` handles this by trying both field names.
- **Semester filtering without orgstructure access:** The learner role gets
  403 on the orgstructure API, so there's no direct way to discover which
  course belongs to which semester. The tool uses course-code suffix
  heuristics (`_24`, `_2024-2025`, `_2025-2026`) instead.
- **Pipe-delimited semester matching:** Semester names like
  `AY 2024-25 | Sem I` are split on `|` to get individual segments. This
  prevents `"Sem I"` from matching `"Sem II"`.
- **URL-encoded filenames in downloads:** The `Content-Disposition` header
  from the file-download API contains URL-encoded filenames
  (e.g. `%20` for spaces). The `_sanitize_filename` helper URL-decodes them.
- **Quiz API pagination:** The quiz endpoint returns
  `{Objects: [...], Next: "<url>" | null}`. `LighthouseClient.get_quizzes()`
  follows all `Next` links automatically.
- **Course ID resolution:** When you pass a non-numeric string as
  `COURSE_ID`, it performs case-insensitive substring matching against course
  names. If exactly one match, it proceeds. If ambiguous, it lists all
  matches and exits with code 1.
- **Manifest corruption:** If a `.manifest.json` file becomes corrupted, the
  sync command will warn and treat all files as new. Delete the manifest to
  force a full re-sync.
- **Orphaned topics:** Files that appear in the manifest but are no longer in
  the content tree are reported as "orphaned". They are not deleted
  automatically — the user must clean them up manually.
- **Credential storage:** `CredentialStore` uses Fernet symmetric encryption
  for credential storage with OS keyring as a fallback. If neither is
  available, credentials are stored in plaintext (with a warning).
- **Headless auth timeout:** The `auth login` command has a default 120-second
  timeout to account for 2FA. If authentication takes longer, use
  `--timeout` to increase it.

## For AI Agents

This CLI was built specifically so AI agents can interact with the LMS
programmatically. Here's the recommended workflow:

```
1. Check auth
   $ lighthouse auth status
   -> {valid: true, cookies: [...]}

2. If expired, refresh (requires browser running with CDP)
   $ lighthouse auth refresh --cdp-port 34165
   -> {valid: true, cookies: [...]}

   OR use headless browser auth (no pre-running browser needed)
   $ lighthouse auth login
   -> {valid: true, cookies: [...]}

3. Always use --json for structured output
   $ lighthouse courses --json
   $ lighthouse content 44347 --json
   $ lighthouse grades 36060 --json

4. Course IDs can be numeric or fuzzy name substrings
   $ lighthouse content signals --json
   # resolves "signals" -> OrgUnitId 44347

5. Preview downloads before committing
   $ lighthouse download 44347 --dry-run --json
   # returns [{topic_id, title, path}, ...]

6. Download specific files
   $ lighthouse download 44347 12345 --json
   # returns {path, size_kb, filename}

7. Download all files from a course
   $ lighthouse download 44347
   # saves to ~/Downloads/lighthouse/Signals & Systems/

8. Download including assignment attachments
   $ lighthouse download 44347 --include-assignments --json

9. Filter downloads by file type
   $ lighthouse download 44347 --types pdf,docx --json

10. Incremental sync — only download new/changed files
    $ lighthouse sync 44347 --json
    # returns {new, updated, unchanged, orphaned, downloaded_bytes}

11. Sync multiple courses at once
    $ lighthouse sync 44347 --also 44348 --also 44349 --json

12. Check assignments for a course
    $ lighthouse assignments 44347 --json
    # returns [{Id, Name, DueDate, Availability}, ...]

13. Submit a file to a dropbox folder
    $ lighthouse submit -f homework.pdf 44347 5678 --yes --json
    # returns {submission_id, folder_id, course_id, file, submitted_at}

14. Resolve folder ID by name
    $ lighthouse submit -f report.pdf 44347 "Homework 1" --yes --json
    # resolves "Homework 1" -> folder ID 5678
```

**Tips for agents:**
- All commands exit with code 0 on success, 1 on failure. Check the exit
  code.
- Error messages go to stderr; normal output goes to stdout.
- `--json` output is stable and machine-parseable.
- When in doubt about a course ID, run `lighthouse courses --json` and
  filter locally.
- The `content` command's JSON output contains the full nested module tree
  with `TopicId` values needed for targeted downloads.
- Use `sync` instead of `download` for repeated interactions — it's
  bandwidth-efficient and tracks changes via manifests.
- Manifest files (`.manifest.json`) enable deduplication: re-running sync
  skips unchanged files and only downloads new/modified ones.
- Orphaned topics in sync output indicate files that were removed from the
  LMS content tree but still exist locally.
- The `submit` command auto-skips confirmation in non-TTY mode, so agents
  don't need `--yes`. But include it anyway for safety.
- Use `assignments --json` to discover folder IDs before submitting.
- Course and folder resolution both support name substrings, so you don't
  need to memorize numeric IDs.

## Project Structure

```
lighthouse-cli/
  pyproject.toml           Package config, dependencies (click, requests, rich,
                           playwright, cryptography, keyring, requests-toolbelt)
  README.md                This file
  lighthouse_cli/
    __init__.py            Version string (__version__ = "0.1.0")
    api.py                 LighthouseClient — HTTP client, auth, cookie
                           management, all API methods, course ID resolution,
                           CDP-based cookie extraction
    auth.py                HeadlessAuthenticator — Playwright-based headless
                           browser SSO authentication with 2FA support;
                           CredentialStore — Fernet encryption with keyring
                           fallback for secure credential storage; cmd_auth_login
    commands.py            Command implementations — data fetching, formatting,
                           output (rich tables + plain text fallback + JSON);
                           includes sync, assignments, and submit commands
    cli.py                 Click command wiring — CLI entry point, argument
                           and option definitions
    manifest.py            Manifest class — load/save manifest files, add_entry,
                           atomic writes, SHA-256 file hashing for incremental
                           sync and deduplication
    utils.py               Shared utilities — _sanitize_filename() for cleaning
                           URL-encoded filenames and other helpers
```

**Key classes and functions:**

- `LighthouseClient` (api.py) — Stateful HTTP client wrapping
  `requests.Session` with D2L auth cookies. Lazy-loads cookies from disk on
  first request. All API methods live here.
- `resolve_course_id()` (api.py) — Resolves a string identifier (numeric
  OrgUnitId or name substring) to an integer course ID.
- `refresh_auth_from_browser()` (api.py) — Extracts cookies from browser via
  CDP (tries browser-harness, then websockets, then Node.js).
- `HeadlessAuthenticator` (auth.py) — Playwright-based SSO authentication.
  Launches headless Chromium, navigates to login page, waits for user
  auth (including 2FA), extracts session cookies.
- `CredentialStore` (auth.py) — Secure credential storage using Fernet
  symmetric encryption with OS keyring fallback.
- `Manifest` (manifest.py) — Manages `.manifest.json` files in download
  directories. Tracks file paths with SHA-256 hashes. Supports atomic writes
  to prevent corruption.
- `_sanitize_filename()` (utils.py) — URL-decodes and sanitizes filenames
  from Content-Disposition headers.
- `cmd_*` functions (commands.py) — One per CLI command. Return exit code
  (0 or 1). Handle `--json` output mode internally.
- `_walk_content_tree()` / `_flatten_all_topics()` (commands.py) — Recursively
  process the nested content TOC for display and download.

**Dependencies:**

| Package | Purpose |
|---------|---------|
| `click>=8.1` | CLI framework (commands, options, arguments) |
| `requests>=2.31` | HTTP client for D2L REST API |
| `rich>=13.0` | Pretty terminal tables (graceful fallback to plain text) |
| `playwright>=1.40` | Headless browser for auth login |
| `cryptography>=41.0` | Fernet encryption for credential storage |
| `keyring>=24.0` | OS keyring integration for credential storage |
| `requests-toolbelt>=1.0` | Multipart encoding for file submission |

**Optional dependencies (for CDP cookie extraction):**

| Package | Purpose |
|---------|---------|
| `browser-harness` | CLI tool for CDP cookie extraction |
| `websockets` | Python CDP client fallback |
| Node.js | Final fallback for CDP cookie extraction |

**Dev dependencies:**

| Package | Purpose |
|---------|---------|
| `pytest>=7.0` | Testing framework |
| `pytest-mock>=3.12` | Mocking utilities |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LIGHTHOUSE_CONFIG_DIR` | `~/.config/lighthouse-cli` | Directory for cookie storage |
| `LIGHTHOUSE_CDP_PORT` | `34165` | Default CDP port for `auth refresh` |

## License

MIT
