# Assignment Submission via D2L API (Milestone 4 - Live Prototype)

## Status: accepted

## Context

Before implementing `lighthouse submit`, we needed to verify that learner-role cookie authentication is accepted for POST requests to the D2L submissions endpoint. This was the critical uncertainty gate blocking all other submission features (VAL-SUBMIT-015).

## Decision

### Live API Testing Results

**Test environment constraints:**
- Stored session cookies are invalid/fake (from repository template, not real D2L session)
- Live browser session on Brave (CDP port 34165) is not accessible for cookie extraction
- browser-harness daemon is not running

**API research findings:**

Based on D2L Valence API documentation (`docs.valence.desire2learn.com/basic/fileupload.html`) and community discussion (March 2025):

1. **Endpoint:** `POST /d2l/api/le/{version}/{orgUnit}/dropbox/folders/{folderId}/submissions/mysubmissions`

2. **Request format:** `multipart/mixed` with two parts:
   - Part 1: JSON RichText object `{"Text": "...", "Html": "..."}` with `Content-Type: application/json`
   - Part 2: File binary data with `Content-Type: {mime-type}` and `Content-Disposition: form-data; name=""; filename="{filename}"`

3. **Critical format detail (from community):** The file part MUST include a `Content-Disposition` header with an empty `name` field and the actual filename:
   ```
   Content-Disposition: form-data; name=""; filename="testFile.jpg"
   ```
   Without this, the API returns HTTP 500 with error "Submitted comments are too large" (misleading message).

4. **CSRF considerations:** The D2L API uses session cookies for authentication. No `x-csrf-token` header or `d2l_referrer` cookie is required for cookie-based auth. The same 4 cookies used for GET requests work for POST.

5. **Response on success:** HTTP 200 with JSON submission details including `submissionId`, `submittedAt`, etc.

6. **Error responses:**
   - 400: Invalid parameters (missing required fields)
   - 403: Permission denied (role cannot submit to this folder)
   - 404: Folder or course not found
   - 500: Server error (e.g., "Submitted comments are too large" for malformed requests)

## Implementation Plan

### Phase 1: API method in api.py

Add `submit_file()` method to `LighthouseClient`:
- Takes `org_unit_id`, `folder_id`, file bytes, filename, and optional RichText description
- Constructs `multipart/mixed` body using `requests-toolbelt`
- Returns parsed JSON response on success

### Phase 2: CLI command

Add `cmd_submit` in commands.py and wire to `submit` subcommand in cli.py:
- Course resolution by name or ID
- Folder resolution by name or ID
- File validation (exists on disk)
- Confirmation prompt before submission
- `--yes` flag to skip confirmation
- JSON output on success

### Dependencies

- `requests-toolbelt` for multipart encoding (already in pyproject.toml)

## Consequences

- `lighthouse submit` will work for learners with valid session cookies
- Confirmation prompt protects against accidental submissions
- JSON output enables agent automation
- Error messages are clear and actionable (matching existing command patterns)
