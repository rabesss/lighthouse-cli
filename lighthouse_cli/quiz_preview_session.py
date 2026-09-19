"""Encrypted, single-writer cursors for experimental trial previews."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any, cast

from .api import LighthouseClient, _require_positive_endpoint_id
from .connection import connection_for
from .credential_store import CredentialStore, _validate_credential_path
from .quiz_preview_finish import PreviewSubmitUnknownError, submit_preview, verify_receipt
from .quiz_preview_transport import (
    PreviewAdvanceUnknownError,
    PreviewSaveUnknownError,
    PreviewStartUnknownError,
    advance_current_preview,
    page_path,
    read_current_preview,
    save_current_preview_answer,
    start_preview,
)


class PreviewWorkflowError(ValueError):
    """Only fixed local messages may be passed to this exception."""


_UNCERTAIN = (
    PreviewStartUnknownError,
    PreviewSaveUnknownError,
    PreviewAdvanceUnknownError,
    PreviewSubmitUnknownError,
)


class PreviewWorkflow:
    def __init__(self, site: str, course_id: int, quiz_id: int) -> None:
        if site != "trial":
            raise PreviewWorkflowError(
                "The experimental preview workflow currently requires --site trial."
            )
        page_path(course_id, quiz_id, 1, 1)
        self.site, self.course_id, self.quiz_id = site, course_id, quiz_id
        self.connection = connection_for(site)
        self.store = CredentialStore(config_dir=self.connection.cookie_dir)
        self.path = self.store.config_dir / f"preview-{course_id}-{quiz_id}.json"
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.store.preflight()
        _validate_credential_path(self.store.config_dir)
        self.store.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _validate_credential_path(self.lock_path)
        descriptor = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise PreviewWorkflowError("Preview lock is not a regular file.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PreviewWorkflowError(
                    "Another operation is using this quiz preview."
                ) from None
            yield
        finally:
            os.close(descriptor)

    def _load(self) -> dict[str, Any] | None:
        artifact = self.store.read_artifact(self.path)
        if artifact is None:
            return None
        _, state = artifact
        if (
            type(state.get("version")) is not int
            or state["version"] != 1
            or state.get("origin") != self.connection.origin
            or state.get("mode") != "preview"
            or type(state.get("course_id")) is not int
            or type(state.get("quiz_id")) is not int
            or state.get("course_id") != self.course_id
            or state.get("quiz_id") != self.quiz_id
            or type(state.get("actor_id")) is not int
            or state["actor_id"] <= 0
            or state.get("status")
            not in ("starting", "active", "uncertain", "submitted", "abandoned")
            or state.get("operation") not in (None, "start", "answer", "next", "submit")
        ):
            raise PreviewWorkflowError("The saved preview checkpoint is invalid.")
        if state["status"] in {"active", "submitted"} or state.get("attempt_id") is not None:
            page_path(
                self.course_id,
                self.quiz_id,
                cast(int, state.get("attempt_id")),
                cast(int, state.get("page")),
            )
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.store.write_artifact(self.path, metadata={}, secret=state)

    @staticmethod
    def _receipt_with_retention(receipt: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        result = dict(receipt)
        result["retained_for_grading"] = bool(state.get("retain", False))
        return result

    def _actor(self, client: LighthouseClient) -> int:
        who = client.get_json(client.base_url + "/d2l/api/lp/1.47/users/whoami", _replay_safe=False)
        if not isinstance(who, dict):
            raise PreviewWorkflowError("The signed-in account could not be verified.")
        return _require_positive_endpoint_id(who.get("Identifier"), "account")

    def status(self) -> dict[str, Any]:
        with self._locked():
            state = self._load()
            if state is None:
                return {
                    "mode": "preview",
                    "status": "absent",
                    "course_id": self.course_id,
                    "quiz_id": self.quiz_id,
                }
            return {
                key: state.get(key)
                for key in (
                    "mode",
                    "status",
                    "course_id",
                    "quiz_id",
                    "attempt_id",
                    "page",
                    "operation",
                )
            }

    def abandon(self) -> dict[str, Any]:
        with self._locked():
            state = self._load()
            if state is None:
                raise PreviewWorkflowError("No saved preview exists for this quiz.")
            state.update(status="abandoned", operation=None)
            self._save(state)
            return {"mode": "preview", "abandoned_locally": True, "remote_attempt_deleted": False}

    def run(
        self,
        operation: str,
        *,
        question_id: int | None = None,
        choice_id: int | None = None,
        bypass_availability: bool = False,
        retain: bool = False,
    ) -> dict[str, Any]:
        if operation not in {"start", "page", "answer", "next", "submit"}:
            raise PreviewWorkflowError("Unsupported preview operation.")
        with self._locked():
            previous = self._load()
            if operation == "start":
                if previous and previous["status"] not in {"submitted", "abandoned"}:
                    raise PreviewWorkflowError(
                        "A preview already exists. Inspect it before starting again; abandon only after resolving its outcome."
                    )
            elif not previous or previous["status"] not in {"active", "uncertain"}:
                raise PreviewWorkflowError("No active preview exists for this quiz.")
            elif previous["status"] == "uncertain" and operation != "page":
                raise PreviewWorkflowError(
                    "The last operation is uncertain. Inspect the browser before another write or abandon the preview."
                )
            client = LighthouseClient(read_only_auth=True, site=self.site)
            try:
                actor = self._actor(client)
                if previous and operation != "start" and previous["actor_id"] != actor:
                    raise PreviewWorkflowError(
                        "The saved preview belongs to a different signed-in account."
                    )
                if previous and operation != "start" and previous.get("attempt_id") is not None:
                    attempt_id = previous["attempt_id"]
                    record = client.get_json(
                        f"/{self.course_id}/quizzes/{self.quiz_id}/attempts/{attempt_id}",
                        _replay_safe=False,
                    )
                    if (
                        not isinstance(record, dict)
                        or type(record.get("AttemptId")) is not int
                        or record["AttemptId"] != attempt_id
                        or type(record.get("QuizId")) is not int
                        or record["QuizId"] != self.quiz_id
                        or type(record.get("UserId")) is not int
                        or record["UserId"] != actor
                    ):
                        raise PreviewWorkflowError(
                            "The remote attempt identity could not be verified."
                        )
                    if record.get("Completed") is not None:
                        receipt = self._receipt_with_retention(
                            verify_receipt(
                                client,
                                course_id=self.course_id,
                                quiz_id=self.quiz_id,
                                attempt_id=attempt_id,
                                actor_id=actor,
                            ),
                            previous,
                        )
                        completed_state = {
                            **previous,
                            "status": "submitted",
                            "operation": None,
                            "receipt": receipt,
                        }
                        self._save(completed_state)
                        if operation in {"page", "submit"}:
                            return receipt
                        raise PreviewWorkflowError(
                            "This preview has already been submitted; no answer or navigation request was sent."
                        )
                if operation == "start":
                    quiz = client.get_quiz_detail(self.course_id, self.quiz_id)
                    if (
                        not isinstance(quiz, dict)
                        or quiz.get("IsSingleSession") is not False
                        or not isinstance(quiz.get("SubmissionTimeLimit"), dict)
                        or quiz["SubmissionTimeLimit"].get("IsEnforced") is not False
                        or not (
                            type(quiz.get("PagingTypeId")) is int
                            and (
                                quiz["PagingTypeId"] == 0
                                or (
                                    quiz["PagingTypeId"] == 1
                                    and quiz.get("PreventMovingBackwards") is True
                                )
                            )
                        )
                    ):
                        raise PreviewWorkflowError(
                            "This prototype supports untimed all-at-once or one-question/no-backtracking previews without single-session locking."
                        )
                    state = {
                        "version": 1,
                        "origin": self.connection.origin,
                        "mode": "preview",
                        "actor_id": actor,
                        "course_id": self.course_id,
                        "quiz_id": self.quiz_id,
                        "status": "starting",
                        "operation": "start",
                        "attempt_id": None,
                        "page": None,
                    }
                else:
                    state = dict(cast("dict[str, Any]", previous))
                    if state["status"] == "uncertain":
                        return self._recover(client, state)
                if operation == "page":
                    return read_current_preview(client, **self._identity(state)).public_data()
                state.update(
                    status="uncertain",
                    operation=operation,
                    question_id=question_id,
                    choice_id=choice_id,
                    retain=retain,
                )
                self._save(state)  # durable intent before any mutation
                try:
                    if operation == "start":
                        result = start_preview(
                            client,
                            course_id=self.course_id,
                            quiz_id=self.quiz_id,
                            bypass_availability=bypass_availability,
                        )
                    elif operation == "answer":
                        result = save_current_preview_answer(
                            client,
                            **self._identity(state),
                            question_id=cast(int, question_id),
                            choice_id=cast(int, choice_id),
                        )
                    elif operation == "next":
                        result = advance_current_preview(client, **self._identity(state))
                    else:
                        receipt = submit_preview(
                            client, **self._identity(state), retain=retain, actor_id=actor
                        )
                except _UNCERTAIN:
                    state["status"] = "uncertain"
                    self._save(state)
                    raise
                except Exception:
                    # Known pre-mutation validation errors do not consume a step.
                    if previous is not None:
                        self._save(previous)
                    else:
                        state.update(status="abandoned", operation=None)
                        self._save(state)
                    raise
                # Commit only after the mutation and its readback succeeded.
                # If this write fails, the durable uncertain intent remains;
                # never restore an earlier cursor after a successful advance.
                if operation == "submit":
                    state.update(status="submitted", operation=None, receipt=receipt)
                    self._save(state)
                    return receipt
                state.update(
                    status="active", operation=None, attempt_id=result.attempt_id, page=result.page
                )
                self._save(state)
                return result.public_data()
            finally:
                with suppress(Exception):
                    client._session.close()

    def _identity(self, state: dict[str, Any]) -> dict[str, int]:
        if type(state.get("attempt_id")) is not int or type(state.get("page")) is not int:
            raise PreviewWorkflowError(
                "The start outcome is unknown. Inspect the browser before abandoning or starting again."
            )
        return {
            "course_id": self.course_id,
            "quiz_id": self.quiz_id,
            "attempt_id": state["attempt_id"],
            "page": state["page"],
        }

    def _recover(self, client: LighthouseClient, state: dict[str, Any]) -> dict[str, Any]:
        identity = self._identity(state)
        operation = state.get("operation")
        if operation == "submit":
            receipt = self._receipt_with_retention(
                verify_receipt(
                    client,
                    course_id=self.course_id,
                    quiz_id=self.quiz_id,
                    attempt_id=state["attempt_id"],
                    actor_id=state["actor_id"],
                ),
                state,
            )
            state.update(status="submitted", operation=None, receipt=receipt)
            self._save(state)
            return receipt
        if operation == "next":
            # Page readability is not an authoritative cursor signal: a
            # direct GET can succeed before or after a one-way transition.
            # Keep the durable intent uncertain and require browser
            # inspection rather than risking a skip or a replay.
            raise PreviewWorkflowError(
                "Navigation outcome is uncertain. Inspect the browser before abandoning or continuing."
            )
        elif operation != "answer":
            raise PreviewWorkflowError("The start outcome must be checked in the browser.")
        result = read_current_preview(client, **identity)
        if operation == "answer" and not result.confirms_answer(
            cast(int, state.get("question_id")), cast(int, state.get("choice_id"))
        ):
            raise PreviewWorkflowError(
                "The intended answer is not confirmed saved; the checkpoint remains uncertain."
            )
        state.update(status="active", operation=None, page=result.page)
        self._save(state)
        return result.public_data()
