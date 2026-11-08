"""Draft-only instructor feedback on one learner's assignment submission.

Contract (D2L developer reference, res/dropbox.html, LE 1.82+; the client
uses 1.93):

- ``POST .../dropbox/folders/(folderId)/feedback/user/(userId)`` takes a
  Dropbox.DropboxFeedback block whose ``Feedback`` is a RichText object
  (``{"Text", "Html"}``, per the reference's note; not RichTextInput).
  ``IsGraded: false`` is the documented draft state; the learner's own
  ``mysubmissions`` read includes feedback only once it is published, and
  ENTITYDROPBOXSTATUS_T separates Draft (2) from Published (3).
- ``GET .../feedback/user/(userId)`` returns Dropbox.DropboxFeedbackOut, and
  ``GET .../submissions/user/(userId)`` (1.88+) returns the user's
  EntityDropbox, which the preflight and the readback use. A folder whose
  ``GroupTypeId`` is present and not null is a group folder.

This first slice is comment only: no score, graded symbol, rubric, file,
group or publish operation. The POST response body is undocumented, so only
the readback can show that a draft was saved. D2L has no conditional write,
so the caller re-runs :func:`preflight` immediately before the POST and
compares a fingerprint of the existing feedback.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import stat
from dataclasses import dataclass
from typing import Any

import requests

from .api import _close_response
from .assessment_api import AssessmentAPI, AssessmentWriteUnknownError, positive_id

MAX_FEEDBACK_CHARS = 65536
# Four UTF-8 bytes per character at most, plus a byte-order mark.
_MAX_FILE_BYTES = 4 * MAX_FEEDBACK_CHARS + 3
_UNSUBMITTED, _SUBMITTED, _DRAFT, _PUBLISHED = 0, 1, 2, 3
# Every DropboxFeedbackOut field; a partial block could hide a score or file.
_FEEDBACK_KEYS = ("Score", "Feedback", "RubricAssessments", "IsGraded", "Files", "Links", "GradedSymbol")
# Documented or definite rejections of the POST; anything else after dispatch is unknown.
_REFUSED_STATUSES = (400, 403, 404)
VISIBILITY = "draft: not published, not visible to the learner, no score"
UNVERIFIED = ("Feedback outcome unknown: it may have been saved, but it could not be verified as a draft. "
               "Inspect it in Brightspace before retrying.")
_APPEARS_PUBLISHED = ("Feedback outcome unknown: the readback shows this feedback as published, graded or scored, "
                      "so the learner may see it. Check it in Brightspace now; do not retry.")
# The only documented success status of the feedback POST.
_SAVED_STATUS = 200


class FeedbackInputError(ValueError):
    """The feedback file is unusable; fixed message, never the file's text."""


class FeedbackRefusedError(ValueError):
    """The target is unsafe or unexpected for a draft-only write; nothing was sent."""


class FeedbackSaveRefusedError(FeedbackRefusedError):
    """Brightspace definitely rejected the POST; nothing was saved."""


class _UnexpectedError(ValueError):
    """A response does not have the documented shape."""


class _PublishedReadbackError(_UnexpectedError):
    """The record shows graded or published feedback."""


@dataclass(frozen=True)
class FeedbackTarget:
    replaces_draft: bool
    # Digest of the record's status and existing feedback, never the text itself.
    fingerprint: str


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _html(text: str) -> str:
    return "<p>" + html.escape(text, quote=True).replace("\n", "<br />") + "</p>"


def feedback_payload(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise FeedbackInputError("The feedback file is empty.")
    if len(text) > MAX_FEEDBACK_CHARS:
        raise FeedbackInputError(f"The feedback is longer than {MAX_FEEDBACK_CHARS} characters.")
    if "\x00" in text:
        raise FeedbackInputError("The feedback file contains a NUL character.")
    return {
        "Score": None, "Feedback": {"Text": text, "Html": _html(text)},
        "RubricAssessments": [], "IsGraded": False, "GradedSymbol": None,
    }


def _read_regular_file(path: str) -> bytes:
    unreadable = FeedbackInputError("Could not read the feedback file.")
    try:
        # Non-blocking open so a FIFO cannot hang the command before the check.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise unreadable from None
    try:
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise unreadable
            return handle.read(_MAX_FILE_BYTES + 1)
    except OSError:
        raise unreadable from None


def read_feedback_file(path: str) -> str:
    """Strict UTF-8 text from *path*, validated as a payload before any request."""
    raw = _read_regular_file(path)
    if len(raw) > _MAX_FILE_BYTES:
        raise FeedbackInputError(f"The feedback is longer than {MAX_FEEDBACK_CHARS} characters.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FeedbackInputError("The feedback file is not valid UTF-8.") from None
    text = _normalize(text.removeprefix("﻿"))
    feedback_payload(text)
    return text


def _routes(api: AssessmentAPI, folder_id: int, user_id: int) -> tuple[str, str]:
    folder = api.path("assignment", folder_id)
    user = positive_id(user_id)
    return f"{folder}/submissions/user/{user}", f"{folder}/feedback/user/{user}"


def _user_entity(data: Any, user_id: int) -> dict[str, Any]:
    """The one EntityDropbox for this individual user, or :class:`_UnexpectedError`."""
    blocks = [data] if isinstance(data, dict) else data
    if not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict):
        raise _UnexpectedError("Brightspace did not return exactly one submission record for this user.")
    block = blocks[0]
    entity = block.get("Entity")
    if not isinstance(entity, dict):
        raise _UnexpectedError("The submission record has no entity.")
    entity_type, entity_id = entity.get("EntityType"), entity.get("EntityId")
    if not isinstance(entity_type, str) or entity_type.lower() != "user":
        raise _UnexpectedError("This is not an individual submission; group feedback is not supported.")
    if type(entity_id) is not int or entity_id != user_id:
        raise _UnexpectedError("The submission record belongs to a different user.")
    return block


def _status(block: dict[str, Any]) -> int:
    """The ENTITYDROPBOXSTATUS_T value (documented as an integer, typed as a string)."""
    status = block.get("Status")
    if isinstance(status, str) and status in {"0", "1", "2", "3"}:
        status = int(status)
    if type(status) is not int or status not in (_UNSUBMITTED, _SUBMITTED, _DRAFT, _PUBLISHED):
        raise _UnexpectedError("The submission record has no readable status.")
    if status == _PUBLISHED:
        raise _PublishedReadbackError(
            "Feedback for this submission is already published; it is never turned back into a draft.")
    return status


def _rich_text(feedback: dict[str, Any]) -> tuple[str, str]:
    """``(Text, Html)`` of a RichText ``Feedback``; absent parts are empty."""
    rich = feedback.get("Feedback")
    if rich is None:
        return "", ""
    if not isinstance(rich, dict):
        raise _UnexpectedError("The existing feedback has an unexpected shape.")
    parts = []
    for key in ("Text", "Html"):
        value = rich.get(key)
        if value is not None and not isinstance(value, str):
            raise _UnexpectedError("The existing feedback has an unexpected shape.")
        parts.append(value or "")
    return parts[0], parts[1]


def _check_draft_only(feedback: Any) -> dict[str, Any]:
    """A complete DropboxFeedbackOut that is an unscored, text-only draft."""
    if not isinstance(feedback, dict) or any(key not in feedback for key in _FEEDBACK_KEYS):
        raise _UnexpectedError("The existing feedback is incomplete or has an unexpected shape.")
    graded = feedback["IsGraded"]
    if graded is True:
        raise _PublishedReadbackError(
            "Feedback for this submission is already published; it is never turned back into a draft.")
    if graded is not False:
        raise _UnexpectedError("The existing feedback's draft state could not be read.")
    if feedback["Score"] is not None or feedback["GradedSymbol"] is not None:
        raise _PublishedReadbackError("The existing draft has a score, which a comment-only save could clear.")
    for key in ("RubricAssessments", "Files", "Links"):
        value = feedback[key]
        if not isinstance(value, list):
            raise _UnexpectedError("The existing feedback has an unexpected shape.")
        if value:
            raise _UnexpectedError(
                "The existing draft has rubric assessments or attachments, which a comment-only save could clear.")
    return feedback


def _fingerprint(block: dict[str, Any]) -> str:
    state = {"Status": block.get("Status"), "Feedback": block.get("Feedback")}
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def _existing_comment(block: dict[str, Any]) -> str:
    if "Feedback" not in block:
        raise _UnexpectedError("The submission record is incomplete.")
    feedback = block["Feedback"]
    if feedback is None:
        if _status(block) == _DRAFT:
            raise _UnexpectedError("The submission is marked as having draft feedback, but none was returned.")
        return ""
    text, markup = _rich_text(_check_draft_only(feedback))
    return text if text.strip() else markup


def check_feedback_target(data: Any, user_id: int, *, replace_draft: bool) -> FeedbackTarget:
    """Validate the preflight EntityDropbox read; raise :class:`FeedbackRefusedError`."""
    try:
        block = _user_entity(data, user_id)
        submissions = block.get("Submissions")
        if not isinstance(submissions, list) or not submissions or _status(block) == _UNSUBMITTED:
            raise _UnexpectedError("This user has no submission in this assignment.")
        existing = _existing_comment(block)
    except _UnexpectedError as exc:
        raise FeedbackRefusedError(f"{exc} Nothing was sent.") from None
    if existing.strip() and not replace_draft:
        raise FeedbackRefusedError(
            "This submission already has a draft comment. Pass --replace-draft to replace it. Nothing was sent.")
    return FeedbackTarget(replaces_draft=bool(existing.strip()), fingerprint=_fingerprint(block))


def check_individual_folder(folder: Any) -> None:
    """Refuse a group-submission folder (``GroupTypeId`` present and not null)."""
    if not isinstance(folder, dict):
        raise FeedbackRefusedError("Brightspace returned an unreadable assignment. Nothing was sent.")
    if folder.get("GroupTypeId") is not None:
        raise FeedbackRefusedError(
            "This is a group assignment; group feedback is not supported. Nothing was sent.")


def _preflight_read(api: AssessmentAPI, path: str) -> Any:
    try:
        return api.client.get_json(path)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 403:
            raise FeedbackRefusedError(
                "Brightspace refused reading this assignment or user's submissions (HTTP 403); "
                "your account cannot assess this assignment. Nothing was sent.") from None
        if status == 404:
            raise FeedbackRefusedError(
                "No such course, assignment or user submission (HTTP 404). Nothing was sent.") from None
        raise
    except ValueError:
        raise FeedbackRefusedError("Brightspace returned an unreadable record. Nothing was sent.") from None


def preflight(api: AssessmentAPI, folder_id: int, user_id: int, *, replace_draft: bool) -> FeedbackTarget:
    """Read-only checks of the folder, identity and existing feedback before any write."""
    submissions_route, _ = _routes(api, folder_id, user_id)
    check_individual_folder(_preflight_read(api, api.path("assignment", folder_id)))
    return check_feedback_target(_preflight_read(api, submissions_route), user_id, replace_draft=replace_draft)


def _check_saved_comment(feedback: Any, text: str) -> None:
    """A draft-only DropboxFeedbackOut whose comment is ``text``."""
    saved_text, saved_html = _rich_text(_check_draft_only(feedback))
    if _normalize(saved_text) != text and saved_html != _html(text):
        raise _UnexpectedError()


def _verify_saved(api: AssessmentAPI, folder_id: int, user_id: int, text: str) -> None:
    submissions_route, feedback_route = _routes(api, folder_id, user_id)
    _check_saved_comment(api.client.get_json(feedback_route), text)
    block = _user_entity(api.client.get_json(submissions_route), user_id)
    if _status(block) != _DRAFT:
        raise _UnexpectedError()
    _check_saved_comment(block.get("Feedback"), text)


def _send(api: AssessmentAPI, route: str, payload: dict[str, Any]) -> None:
    try:
        response = api.send_once("POST", route, payload)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in _REFUSED_STATUSES:
            raise FeedbackSaveRefusedError(
                f"Brightspace refused the save (HTTP {status}); the feedback was not saved.") from None
        raise AssessmentWriteUnknownError(UNVERIFIED) from None
    try:
        # A 3xx or other 2xx the transport passed through is not the documented success.
        if response.status_code != _SAVED_STATUS:
            raise AssessmentWriteUnknownError(UNVERIFIED)
    finally:
        _close_response(response)


def save_draft(api: AssessmentAPI, folder_id: int, user_id: int, text: str) -> None:
    """Send one draft-feedback POST, then prove the draft through readback.

    Raises :class:`AssessmentWriteUnknownError` whenever the request was sent
    but the saved draft cannot be confirmed; it is never retried.
    """
    payload = feedback_payload(text)
    _, feedback_route = _routes(api, folder_id, user_id)
    _send(api, feedback_route, payload)
    try:
        _verify_saved(api, folder_id, user_id, text)
    except _PublishedReadbackError:
        raise AssessmentWriteUnknownError(_APPEARS_PUBLISHED) from None
    except Exception:
        raise AssessmentWriteUnknownError(UNVERIFIED) from None
