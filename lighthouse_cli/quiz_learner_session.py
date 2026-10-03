"""Encrypted, single-writer cursors for the signed-in learner's own quiz attempts.

Brightspace keeps an attempt's position itself: Continue Quiz reopens the
attempt on the page the server holds, even after a Next. So the cursor only
records the attempt and the last page that read back, which keeps every page
request to one the server showed, and a write whose outcome is unknown is
resolved by starting again, which continues that attempt.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from .api import LighthouseClient, NetworkError, SessionExpiredError, _require_positive_endpoint_id
from .connection import active_connection
from .credential_store import CredentialStore, _validate_credential_path
from .quiz_attempt_page import (
    REFUSE_LEARNER_LAST_PAGE,
    REFUSE_LEARNER_NOT_ON_PAGE,
    REFUSE_LEARNER_UNANSWERED,
    LearnerPage,
    PreviewRefusedError,
)
from .quiz_learner_transport import (
    LearnerAdvanceUnknownError,
    LearnerSaveUnknownError,
    LearnerStartUnknownError,
    advance_learner,
    learner_page_path,
    read_learner_page,
    read_learner_summary,
    read_quiz_image,
    save_learner_answers,
    start_learner,
)


class LearnerWorkflowError(ValueError):
    """Only fixed local messages may be passed to this exception."""


UNCERTAIN = (LearnerStartUnknownError, LearnerSaveUnknownError, LearnerAdvanceUnknownError)

_INVALID = "The saved quiz attempt checkpoint is invalid."
_NOT_OPEN = "No attempt of this quiz is open in the CLI. Run attempt start."
_REOPEN = "The last change could not be verified. Run attempt start to reopen the attempt where Brightspace has it."
_SAVE_UNVERIFIED = "The last answer save could not be verified. Run attempt page to read what Brightspace stored."
_OTHER_ACCOUNT = "The saved attempt belongs to a different signed-in account."
_OTHER_UNSETTLED = ("A change by a different signed-in account to this quiz could not be verified. "
                    "Sign in as that account and run attempt start to settle it.")
_NOT_CONTINUABLE = ("The attempt whose last change could not be verified cannot be continued, "
                    "so no new attempt was started. Check it in Brightspace.")
_SETTINGS = "The quiz settings could not be read, so nothing was started."
_TIMED = "Timed quizzes are not supported yet, so nothing was started."
_ANSWERS = ('Answers must be a JSON object mapping each question id to a choice id, '
            'a list of option ids, or a list of blank texts, e.g. {"101": "o2"}.')
_IMAGE_FAILED = "The image could not be downloaded."
_IMAGE_NOT_SAVED = "The image could not be saved."
_IMAGE_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    if len({key for key, _ in pairs}) != len(pairs):
        raise LearnerWorkflowError(_ANSWERS)
    return dict(pairs)


def parse_answers(text: str) -> dict[int, object]:
    """``{"<question id>": answer}`` JSON as the page's answers; each answer is checked on the page."""
    try:
        data = json.loads(text, object_pairs_hook=_unique_keys)
    except (TypeError, ValueError):
        data = None
    if not isinstance(data, dict) or not data:
        raise LearnerWorkflowError(_ANSWERS)
    answers: dict[int, object] = {}
    for key, value in data.items():
        valid_key = (key.isascii() and key.isdigit() and len(key) <= 18
                     and key == str(int(key)) and 0 < int(key) < 10**18)
        valid_value = isinstance(value, str) or (isinstance(value, list) and all(isinstance(item, str) for item in value))
        if not (valid_key and valid_value):
            raise LearnerWorkflowError(_ANSWERS)
        answers[int(key)] = value
    return answers


def quiz_info(quiz: object) -> dict[str, Any]:
    """The REST quiz settings an agent needs; refuses quizzes the CLI cannot take yet.

    ``attempts_allowed`` is ``None`` when attempts are unlimited.
    """
    if (not isinstance(quiz, dict) or not isinstance(quiz.get("SubmissionTimeLimit"), dict)
            or type(quiz["SubmissionTimeLimit"].get("IsEnforced")) is not bool
            or type(quiz.get("PreventMovingBackwards")) is not bool):
        raise LearnerWorkflowError(_SETTINGS)
    if quiz["SubmissionTimeLimit"]["IsEnforced"]:
        raise LearnerWorkflowError(_TIMED)
    attempts = quiz.get("AttemptsAllowed")
    if not isinstance(attempts, dict) or type(attempts.get("IsUnlimited")) is not bool:
        raise LearnerWorkflowError(_SETTINGS)
    allowed = None if attempts["IsUnlimited"] else attempts.get("NumberOfAttemptsAllowed")
    if not attempts["IsUnlimited"] and (type(allowed) is not int or allowed < 1):
        raise LearnerWorkflowError(_SETTINGS)
    name = quiz.get("Name")
    return {"name": name if isinstance(name, str) else None, "forward_only": quiz["PreventMovingBackwards"],
            "attempts_allowed": allowed}


def _write_image(path: Path, data: bytes) -> None:
    # O_NONBLOCK: a FIFO planted under the image's name fails instead of blocking.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600)
    with os.fdopen(descriptor, "wb") as handle:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Not a regular file.")
        os.fchmod(descriptor, 0o600)  # also when replacing an existing file
        handle.write(data)


def _save_image(client: LighthouseClient, directory: Path, question: Mapping[str, Any],
                image: Mapping[str, Any]) -> dict[str, Any]:
    """One image's entry: the file it was saved to, or why it was not."""
    entry: dict[str, Any] = {"question_id": question["question_id"], "image": image["number"], "alt": image["alt"]}
    try:
        data, media_type = read_quiz_image(client, image["src"])
    except SessionExpiredError:
        raise
    except PreviewRefusedError as exc:
        return {**entry, "error": str(exc)}
    except (NetworkError, OSError, ValueError):
        return {**entry, "error": _IMAGE_FAILED}
    path = directory / f"question-{question['number']}-image-{image['number']}{_IMAGE_SUFFIXES[media_type]}"
    try:
        _write_image(path, data)
    except OSError:
        return {**entry, "error": _IMAGE_NOT_SAVED}
    return {**entry, "path": str(path), "media_type": media_type}


class LearnerWorkflow:
    def __init__(self, course_id: int, quiz_id: int) -> None:
        learner_page_path(course_id, quiz_id, 1, 1)
        self.course_id, self.quiz_id = course_id, quiz_id
        self.connection = active_connection()
        self.store = CredentialStore(config_dir=self.connection.cookie_dir)
        self.path = self.store.config_dir / f"learner-{course_id}-{quiz_id}.json"
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.store.preflight()
        _validate_credential_path(self.store.config_dir)
        self.store.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _validate_credential_path(self.lock_path)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise LearnerWorkflowError("Quiz attempt lock is not a regular file.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LearnerWorkflowError("Another operation is using this quiz attempt.") from None
            yield
        finally:
            os.close(descriptor)

    @contextmanager
    def _client(self) -> Iterator[LighthouseClient]:
        client = LighthouseClient(read_only_auth=True)
        try:
            yield client
        finally:
            with suppress(Exception):
                client._session.close()

    def _load(self) -> dict[str, Any] | None:
        artifact = self.store.read_artifact(self.path)
        if artifact is None:
            return None
        _, state = artifact
        if (type(state.get("version")) is not int or state["version"] != 1
                or state.get("origin") != self.connection.origin or state.get("mode") != "learner"
                or type(state.get("course_id")) is not int or state["course_id"] != self.course_id
                or type(state.get("quiz_id")) is not int or state["quiz_id"] != self.quiz_id
                or type(state.get("actor_id")) is not int or state["actor_id"] <= 0
                or state.get("status") not in ("active", "uncertain")
                or state.get("operation") not in (None, "start", "answer", "next")
                or (state["status"] == "active") != (state["operation"] is None)):
            raise LearnerWorkflowError(_INVALID)
        # Only an unresolved start may lack its attempt and page.
        if state["operation"] != "start" or state.get("attempt_id") is not None or state.get("page") is not None:
            try:
                learner_page_path(self.course_id, self.quiz_id, state.get("attempt_id"), state.get("page"))  # type: ignore[arg-type]
            except ValueError:
                raise LearnerWorkflowError(_INVALID) from None
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.store.write_artifact(self.path, metadata={}, secret=state)

    def _actor(self, client: LighthouseClient) -> int:
        who = client.get_json(client.base_url + "/d2l/api/lp/1.47/users/whoami", _replay_safe=False)
        if not isinstance(who, dict):
            raise LearnerWorkflowError("The signed-in account could not be verified.")
        return _require_positive_endpoint_id(who.get("Identifier"), "account")

    def _open(self, client: LighthouseClient, *, unverified_save: bool = False) -> dict[str, Any]:
        """The saved cursor, if it is this account's and verified (or, for a read, only a save is unverified)."""
        state = self._load()
        if state is None:
            raise LearnerWorkflowError(_NOT_OPEN)
        if self._actor(client) != state["actor_id"]:
            raise LearnerWorkflowError(_OTHER_ACCOUNT)
        if state["status"] == "uncertain":
            if state["operation"] != "answer":
                raise LearnerWorkflowError(_REOPEN)
            if not unverified_save:
                raise LearnerWorkflowError(_SAVE_UNVERIFIED)
        return state

    def _identity(self, state: dict[str, Any]) -> dict[str, int]:
        return {"course_id": self.course_id, "quiz_id": self.quiz_id,
                "attempt_id": state["attempt_id"], "page": state["page"]}

    def _write(self, state: dict[str, Any], operation: str, action: Callable[[], LearnerPage]) -> LearnerPage:
        """Record the intent durably, run one write, then commit the page it read back.

        An unknown outcome leaves the cursor uncertain. Any other failure
        happened before the request was sent, so the cursor is restored.
        """
        previous = dict(state)
        state.update(status="uncertain", operation=operation)
        self._save(state)
        try:
            page = action()
        except UNCERTAIN:
            raise
        except Exception:
            state.clear()
            state.update(previous)
            self._save(state)
            raise
        state.update(status="active", operation=None, page=page.page)
        self._save(state)
        return page

    def status(self) -> dict[str, Any]:
        """The local cursor, without contacting Brightspace."""
        with self._locked():
            state = self._load()
        if state is None:
            return {"mode": "learner", "status": "absent", "course_id": self.course_id, "quiz_id": self.quiz_id}
        return {key: state.get(key) for key in ("mode", "status", "course_id", "quiz_id", "attempt_id", "page", "operation")}

    def start(self) -> dict[str, Any]:
        """Continue the attempt in progress, else start a new one, on the page Brightspace holds."""
        with self._locked(), self._client() as client:
            try:
                previous = self._load()
            except LearnerWorkflowError:  # replaced, as starting reads everything from Brightspace
                previous = None
            actor = self._actor(client)
            unsettled = previous if previous is not None and previous["status"] == "uncertain" else None
            if unsettled is not None and unsettled["actor_id"] != actor:
                raise LearnerWorkflowError(_OTHER_UNSETTLED)
            info = quiz_info(client.get_quiz_detail(self.course_id, self.quiz_id))
            summary = read_learner_summary(client, course_id=self.course_id, quiz_id=self.quiz_id)
            # A known attempt with an unverified change may only be continued:
            # if it cannot be, a new start would use another graded attempt.
            continue_only = unsettled is not None and unsettled["attempt_id"] is not None
            if continue_only and not summary.can_continue:
                raise LearnerWorkflowError(_NOT_CONTINUABLE)
            state: dict[str, Any] = {
                "version": 1, "origin": self.connection.origin, "mode": "learner", "actor_id": actor,
                "course_id": self.course_id, "quiz_id": self.quiz_id, "status": "uncertain",
                "operation": "start", "attempt_id": None, "page": None,
            }
            self._save(state)  # durable intent before the start request

            def seal(attempt_id: int, page: int) -> None:
                state.update(attempt_id=attempt_id, page=page)
                self._save(state)

            try:
                page = start_learner(client, course_id=self.course_id, quiz_id=self.quiz_id,
                                     continue_only=continue_only, on_identity=seal, summary=summary)
            except LearnerStartUnknownError:
                raise
            except Exception:
                # Refused or failed before the start request was sent.
                if previous is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self._save(previous)
                raise
            state.update(status="active", operation=None, attempt_id=page.attempt_id, page=page.page)
            self._save(state)
            return {**page.public_data(), "resumed": summary.can_continue, "quiz": info}

    def page(self) -> dict[str, Any]:
        """Read the cursor's page; this also settles an unverified answer save."""
        with self._locked(), self._client() as client:
            state = self._open(client, unverified_save=True)
            current = read_learner_page(client, **self._identity(state))
            if state["status"] == "uncertain":
                state.update(status="active", operation=None)
                self._save(state)
            return current.public_data()

    def answer(self, answers: Mapping[int, object], *, advance: bool = False,
               allow_unanswered: bool = False) -> dict[str, Any]:
        """Save answers on the cursor's page, then optionally move to the next page.

        With ``advance``, everything Next needs is checked before the save,
        so a refused Next sends nothing.
        """
        with self._locked(), self._client() as client:
            state = self._open(client)
            identity = self._identity(state)
            current = read_learner_page(client, **identity)
            if advance:
                if not current.has_next_control:
                    raise PreviewRefusedError(REFUSE_LEARNER_LAST_PAGE)
                if current.unanswered(current.intended(answers)) and not allow_unanswered:
                    raise PreviewRefusedError(REFUSE_LEARNER_UNANSWERED)
            saved = self._write(state, "answer", lambda: save_learner_answers(
                client, **identity, answers=answers, current=current))
            if not advance:
                return saved.public_data()
            return self._write(state, "next", lambda: advance_learner(
                client, **identity, allow_unanswered=allow_unanswered, current=saved)).public_data()

    def next(self, *, allow_unanswered: bool = False) -> dict[str, Any]:
        """Move to the next page, keeping this page's saved answers."""
        with self._locked(), self._client() as client:
            state = self._open(client)
            identity = self._identity(state)
            current = read_learner_page(client, **identity)
            return self._write(state, "next", lambda: advance_learner(
                client, **identity, allow_unanswered=allow_unanswered, current=current)).public_data()

    def images(self, *, question_id: int | None = None, directory: Path | None = None) -> dict[str, Any]:
        """Download the cursor page's images (or one question's) into ``directory`` or a new temporary one."""
        with self._locked(), self._client() as client:
            state = self._open(client)
            current = read_learner_page(client, **self._identity(state))
            questions = [q for q in current.questions if question_id in (None, q["question_id"])]
            if not questions:
                raise PreviewRefusedError(REFUSE_LEARNER_NOT_ON_PAGE)
            if directory is None:
                directory = Path(tempfile.mkdtemp(prefix="lighthouse-quiz-images-"))
            else:
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            images = [_save_image(client, directory, question, image)
                      for question in questions for image in question["images"]]
            return {"mode": "learner", "course_id": self.course_id, "quiz_id": self.quiz_id,
                    "attempt_id": current.attempt_id, "page": current.page, "directory": str(directory),
                    "images": images}
