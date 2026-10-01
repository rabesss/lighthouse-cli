"""Encrypted, single-writer cursors for experimental instructor quiz previews."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from .api import LighthouseClient, _require_positive_endpoint_id
from .connection import active_connection
from .credential_store import CredentialStore, _validate_credential_path
from .quiz_attempt_page import PreviewPageError
from .quiz_preview_finish import PreviewSubmitUnknownError, submit_preview, verify_receipt
from .quiz_preview_transport import (
    PreviewAdvanceUnknownError,
    PreviewSaveUnknownError,
    PreviewStartUnknownError,
    advance_current_preview,
    page_path,
    read_current_preview,
    read_server_current_preview,
    save_current_preview_answer,
    start_preview,
)


class PreviewWorkflowError(ValueError):
    """Only fixed local messages may be passed to this exception."""


_UNCERTAIN = (PreviewStartUnknownError, PreviewSaveUnknownError, PreviewAdvanceUnknownError, PreviewSubmitUnknownError)

# Bounds for the account-bound pre-start attempt snapshot kept in the sealed
# checkpoint, and the clock-skew allowance when matching a new attempt's
# server start time against the local start intent.
_MAX_BASELINE = 10_000
_MAX_ATTEMPT_PAGES = 50
_LISTING_INVALID = "The quiz attempt listing could not be verified."
_NO_REMOTE_ATTEMPT = "operator_confirmed_no_remote_attempt"
_START_SKEW = timedelta(minutes=10)
_UNRESOLVED_START = (
    "A previous start may have created a remote preview. Run preview reconcile to resolve it "
    "before starting again."
)
_NOT_VERIFIED = "That attempt could not be verified for this start; the checkpoint was not changed."


def _supported_layout(quiz: object) -> bool:
    """Untimed all-at-once, or one question per page with no backtracking."""
    return (isinstance(quiz, dict) and quiz.get("IsSingleSession") is False
            and isinstance(quiz.get("SubmissionTimeLimit"), dict)
            and quiz["SubmissionTimeLimit"].get("IsEnforced") is False
            and type(quiz.get("PagingTypeId")) is int
            and (quiz["PagingTypeId"] == 0
                 or (quiz["PagingTypeId"] == 1 and quiz.get("PreventMovingBackwards") is True)))


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class PreviewWorkflow:
    def __init__(self, course_id: int, quiz_id: int) -> None:
        page_path(course_id, quiz_id, 1, 1)
        self.course_id, self.quiz_id = course_id, quiz_id
        self.connection = active_connection()
        self.store = CredentialStore(config_dir=self.connection.cookie_dir)
        self.path = self.store.config_dir / f"preview-{course_id}-{quiz_id}.json"
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
                raise PreviewWorkflowError("Preview lock is not a regular file.")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PreviewWorkflowError("Another operation is using this quiz preview.") from None
            yield
        finally:
            os.close(descriptor)

    def _load(self) -> dict[str, Any] | None:
        artifact = self.store.read_artifact(self.path)
        if artifact is None:
            return None
        _, state = artifact
        if (type(state.get("version")) is not int or state["version"] != 1
                or state.get("origin") != self.connection.origin or state.get("mode") != "preview"
                or type(state.get("course_id")) is not int or type(state.get("quiz_id")) is not int
                or state.get("course_id") != self.course_id or state.get("quiz_id") != self.quiz_id
                or type(state.get("actor_id")) is not int or state["actor_id"] <= 0
                or state.get("status") not in ("starting", "active", "uncertain", "submitted", "abandoned")
                or state.get("operation") not in (None, "start", "answer", "next", "submit")):
            raise PreviewWorkflowError("The saved preview checkpoint is invalid.")
        baseline = state.get("baseline_attempt_ids")
        if (("unresolved_start" in state and type(state["unresolved_start"]) is not bool)
                or state.get("disposition") not in (None, _NO_REMOTE_ATTEMPT)
                or (baseline is not None and (
                    not isinstance(baseline, list) or len(baseline) > _MAX_BASELINE
                    or any(type(item) is not int or item <= 0 for item in baseline)))
                or ("start_intent_at" in state and _utc(state["start_intent_at"]) is None)):
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

    def _remote_attempt(self, client: LighthouseClient, *, actor_id: int, attempt_id: int) -> dict[str, Any]:
        if type(attempt_id) is not int or not 0 < attempt_id < 10**18:
            raise PreviewWorkflowError("The remote attempt identity could not be verified.")
        record = client.get_json(
            f"/{self.course_id}/quizzes/{self.quiz_id}/attempts/{attempt_id}",
            _replay_safe=False,
        )
        if (not isinstance(record, dict)
                or type(record.get("AttemptId")) is not int or record["AttemptId"] != attempt_id
                or type(record.get("QuizId")) is not int or record["QuizId"] != self.quiz_id
                or type(record.get("UserId")) is not int or record["UserId"] != actor_id
                or "Completed" not in record):
            raise PreviewWorkflowError("The remote attempt identity could not be verified.")
        return record

    def _attempt_listing(self, client: LighthouseClient) -> list[Any]:
        """Page through this quiz's attempts route only (read-only, bounded).

        ``Next`` links are followed only when they stay on the exact attempts
        path with a bookmark, so a malformed link can never reach another
        (possibly state-changing) route.
        """
        url = client.canonical_url(f"/{self.course_id}/quizzes/{self.quiz_id}/attempts/")
        expected = urlparse(url)
        seen: set[str] = set()
        items: list[Any] = []
        for _ in range(_MAX_ATTEMPT_PAGES):
            if url in seen:
                raise PreviewWorkflowError(_LISTING_INVALID)
            seen.add(url)
            data = client.get_json(url)
            page_items = data if isinstance(data, list) else data.get("Objects") if isinstance(data, dict) else None
            if not isinstance(page_items, list):
                raise PreviewWorkflowError(_LISTING_INVALID)
            items.extend(page_items)
            if len(items) > _MAX_BASELINE:
                raise PreviewWorkflowError("Too many attempts to reconcile safely.")
            if isinstance(data, list):
                return items
            following = data.get("Next")
            if following is None or following == "":
                return items
            if not isinstance(following, str):
                raise PreviewWorkflowError(_LISTING_INVALID)
            url = client.canonical_url(following, base_url=url)
            parsed = urlparse(url)
            if ((parsed.scheme, parsed.netloc, parsed.path) != (expected.scheme, expected.netloc, expected.path)
                    or set(parse_qs(parsed.query, keep_blank_values=True)) != {"bookmark"}):
                raise PreviewWorkflowError(_LISTING_INVALID)
        raise PreviewWorkflowError("Too many attempt pages to reconcile safely.")

    def _own_attempts(self, client: LighthouseClient, actor_id: int) -> list[dict[str, Any]]:
        """This account's attempts on this quiz, from the restricted listing."""
        own: list[dict[str, Any]] = []
        for item in self._attempt_listing(client):
            if (not isinstance(item, dict) or type(item.get("AttemptId")) is not int or item["AttemptId"] <= 0
                    or type(item.get("UserId")) is not int or "Completed" not in item
                    or item.get("QuizId", self.quiz_id) != self.quiz_id):
                raise PreviewWorkflowError(_LISTING_INVALID)
            if item["UserId"] == actor_id:
                own.append(item)
        return own

    def _start_candidates(self, client: LighthouseClient, state: dict[str, Any]) -> list[dict[str, Any]]:
        """Incomplete attempts that could belong to this unresolved start.

        Candidates are never bound automatically: an empty or stale listing
        does not prove that no attempt exists, and one new attempt is not by
        itself proof that it came from this start.
        """
        baseline = state.get("baseline_attempt_ids")
        known = set(baseline) if isinstance(baseline, list) else set()
        intent = _utc(state.get("start_intent_at"))
        candidates = []
        for item in self._own_attempts(client, state["actor_id"]):
            if item["Completed"] is not None or item["AttemptId"] in known:
                continue
            started = _utc(item.get("Started"))
            if intent is not None and (started is None or started < intent - _START_SKEW):
                continue
            # Never echo server text: only a parsed, normalized timestamp.
            candidates.append({
                "attempt_id": item["AttemptId"],
                "started": started.astimezone(timezone.utc).isoformat() if started else None,
            })
        return sorted(candidates, key=lambda c: c["attempt_id"])

    def _resolve_start(
        self, client: LighthouseClient, state: dict[str, Any], *, attempt_id: int,
    ) -> dict[str, Any]:
        """Verify one attempt read-only, then bind it; never save on failure."""
        bound = state.get("attempt_id") == attempt_id
        try:
            record = self._remote_attempt(client, actor_id=state["actor_id"], attempt_id=attempt_id)
            resolved = {**state, "attempt_id": attempt_id, "unresolved_start": False}
            if not bound:
                # An unbound candidate must prove it is this quiz's in-progress
                # preview before it can replace the uncertain start: completed
                # records skip the preview page check, and only the supported
                # layouts make the server-reported page authoritative.
                if record.get("Completed") is not None:
                    raise PreviewWorkflowError(_NOT_VERIFIED)
                if not _supported_layout(client.get_quiz_detail(self.course_id, self.quiz_id)):
                    raise PreviewWorkflowError(_NOT_VERIFIED)
            if record.get("Completed") is not None:
                receipt = verify_receipt(
                    client, course_id=self.course_id, quiz_id=self.quiz_id,
                    attempt_id=attempt_id, actor_id=state["actor_id"],
                )
                receipt = self._receipt_with_retention(receipt, resolved)
                resolved.update(status="submitted", operation=None, page=state.get("page") or 1, receipt=receipt)
                self._save(resolved)
                return {**receipt, "reconciled": True}
            identity = {"course_id": self.course_id, "quiz_id": self.quiz_id, "attempt_id": attempt_id}
            if bound and type(state.get("page")) is int:
                # Keep the start's cursor when it still reads back. If the
                # attempt moved on (e.g. advanced in the browser), use the
                # server-reported page of this exact attempt, as for unbound
                # candidates, and only on a supported layout.
                try:
                    current = read_current_preview(client, **identity, page=state["page"])
                except PreviewPageError:
                    if not _supported_layout(client.get_quiz_detail(self.course_id, self.quiz_id)):
                        raise
                    current = read_server_current_preview(client, **identity, page=state["page"])
            else:
                # Unbound: use the server-reported current page of this exact
                # preview attempt (read-only; persisted before any write).
                current = read_server_current_preview(client, **identity, page=1)
        except PreviewPageError:
            raise PreviewWorkflowError(_NOT_VERIFIED) from None
        resolved.update(status="active", operation=None, page=current.page)
        self._save(resolved)
        return {**current.public_data(), "reconciled": True}

    def _actor(self, client: LighthouseClient) -> int:
        who = client.get_json(client.base_url + "/d2l/api/lp/1.47/users/whoami", _replay_safe=False)
        if not isinstance(who, dict):
            raise PreviewWorkflowError("The signed-in account could not be verified.")
        return _require_positive_endpoint_id(who.get("Identifier"), "account")

    def status(self) -> dict[str, Any]:
        with self._locked():
            state = self._load()
            if state is None:
                return {"mode": "preview", "status": "absent", "course_id": self.course_id, "quiz_id": self.quiz_id}
            result = {key: state.get(key) for key in ("mode", "status", "course_id", "quiz_id", "attempt_id", "page", "operation")}
            result["unresolved_start"] = self._unresolved_start(state)
            return result

    @staticmethod
    def _unresolved_start(state: dict[str, Any]) -> bool:
        if state.get("status") == "uncertain" and state.get("operation") == "start":
            return True
        if "unresolved_start" in state:
            return state["unresolved_start"] is True
        # Checkpoints written before this marker existed: an abandoned
        # preview that never bound an attempt may hide an unknown start.
        return (state.get("status") == "abandoned" and state.get("attempt_id") is None
                and "baseline_attempt_ids" not in state)

    def reconcile(self, attempt_id: int | None = None, confirm_no_remote_attempt: bool = False) -> dict[str, Any]:
        """Resolve an unresolved start with read-only checks; never write remotely.

        With a bound attempt, verify and resume it (a different --attempt-id is
        refused). Without one, list candidate attempts; binding requires an
        explicit --attempt-id that is one of those verified candidates. If the
        operator has confirmed in the browser that no preview was created,
        ``confirm_no_remote_attempt`` records that disposition, but only while
        the checkpoint is unbound and no candidate is listed.
        """
        if confirm_no_remote_attempt and attempt_id is not None:
            raise PreviewWorkflowError("Choose either --attempt-id or --confirm-no-remote-attempt.")
        with self._locked():
            state = self._load()
            if state is None or not self._unresolved_start(state):
                raise PreviewWorkflowError("Preview reconciliation applies only to an unresolved start.")
            client = LighthouseClient(read_only_auth=True)
            try:
                if self._actor(client) != state["actor_id"]:
                    raise PreviewWorkflowError("The saved preview belongs to a different signed-in account.")
                bound = state.get("attempt_id")
                if type(bound) is int:
                    if confirm_no_remote_attempt:
                        raise PreviewWorkflowError(
                            "This start is bound to a known attempt; reconcile it instead."
                        )
                    if attempt_id is not None and attempt_id != bound:
                        raise PreviewWorkflowError(
                            "This start is already bound to a different attempt; the checkpoint was not changed."
                        )
                    return self._resolve_start(client, state, attempt_id=bound)
                candidates = self._start_candidates(client, state)
                if confirm_no_remote_attempt:
                    if candidates:
                        raise PreviewWorkflowError(
                            "Candidate attempts exist; bind one with --attempt-id instead."
                        )
                    state.update(status="abandoned", operation=None, unresolved_start=False,
                                 disposition=_NO_REMOTE_ATTEMPT)
                    self._save(state)
                    return {"mode": "preview", "status": "abandoned", "course_id": self.course_id,
                            "quiz_id": self.quiz_id, "reconciled": True, "disposition": _NO_REMOTE_ATTEMPT}
                if attempt_id is None:
                    return {
                        "mode": "preview", "status": state["status"], "course_id": self.course_id,
                        "quiz_id": self.quiz_id, "reconciled": False, "candidates": candidates,
                        "baseline_available": isinstance(state.get("baseline_attempt_ids"), list),
                    }
                if attempt_id not in {c["attempt_id"] for c in candidates}:
                    raise PreviewWorkflowError(_NOT_VERIFIED)
                return self._resolve_start(client, state, attempt_id=attempt_id)
            finally:
                with suppress(Exception):
                    client._session.close()

    def abandon(self) -> dict[str, Any]:
        with self._locked():
            state = self._load()
            if state is None:
                raise PreviewWorkflowError("No saved preview exists for this quiz.")
            unresolved = self._unresolved_start(state)
            state.update(status="abandoned", operation=None, unresolved_start=unresolved)
            self._save(state)
            return {"mode": "preview", "abandoned_locally": True, "remote_attempt_deleted": False,
                    "unresolved_start": unresolved}

    def run(self, operation: str, *, question_id: int | None = None, choice_id: int | None = None,
            bypass_availability: bool = False, retain: bool = False) -> dict[str, Any]:
        if operation not in {"start", "page", "answer", "next", "submit"}:
            raise PreviewWorkflowError("Unsupported preview operation.")
        with self._locked():
            previous = self._load()
            # Refusals that depend only on the local cursor come before any
            # authentication or network request, so they stay actionable.
            if operation == "start" and previous and self._unresolved_start(previous):
                raise PreviewWorkflowError(_UNRESOLVED_START)
            if operation == "start" and previous and previous["status"] not in {"submitted", "abandoned"}:
                raise PreviewWorkflowError(
                    "A preview already exists. Inspect it with preview page or reconcile; "
                    "starting again is blocked while the outcome is unresolved."
                )
            elif operation != "start" and (not previous or previous["status"] not in {"active", "uncertain"}):
                raise PreviewWorkflowError("No active preview exists for this quiz.")
            elif operation != "start" and previous and previous["status"] == "uncertain" and operation != "page":
                if previous.get("operation") == "start":
                    raise PreviewWorkflowError(
                        "The preview start is unresolved. Run preview reconcile before another write."
                    )
                raise PreviewWorkflowError("The last operation is uncertain. Inspect the browser before another write or abandon the preview.")
            client = LighthouseClient(read_only_auth=True)
            state: dict[str, Any] | None = None
            try:
                actor = self._actor(client)
                if previous and operation != "start" and previous["actor_id"] != actor:
                    raise PreviewWorkflowError("The saved preview belongs to a different signed-in account.")
                if previous and operation != "start" and previous.get("attempt_id") is not None:
                    attempt_id = previous["attempt_id"]
                    record = self._remote_attempt(client, actor_id=actor, attempt_id=attempt_id)
                    if record.get("Completed") is not None:
                        receipt = self._receipt_with_retention(
                            verify_receipt(client, course_id=self.course_id, quiz_id=self.quiz_id, attempt_id=attempt_id, actor_id=actor),
                            previous,
                        )
                        completed_state = {**previous, "status": "submitted", "operation": None, "receipt": receipt}
                        self._save(completed_state)
                        if operation in {"page", "submit"}:
                            return receipt
                        raise PreviewWorkflowError("This preview has already been submitted; no answer or navigation request was sent.")
                if operation == "start":
                    if not _supported_layout(client.get_quiz_detail(self.course_id, self.quiz_id)):
                        raise PreviewWorkflowError("This prototype supports untimed all-at-once or one-question/no-backtracking previews without single-session locking.")
                    try:
                        baseline = sorted(item["AttemptId"] for item in self._own_attempts(client, actor))
                    except PreviewWorkflowError:
                        raise
                    except Exception:
                        raise PreviewWorkflowError(
                            "Existing quiz attempts could not be listed, so nothing was started."
                        ) from None
                    state = {"version": 1, "origin": self.connection.origin, "mode": "preview", "actor_id": actor,
                             "course_id": self.course_id, "quiz_id": self.quiz_id, "status": "starting",
                             "operation": "start", "attempt_id": None, "page": None,
                             "baseline_attempt_ids": baseline,
                             "start_intent_at": datetime.now(timezone.utc).isoformat()}
                else:
                    state = dict(cast("dict[str, Any]", previous))
                    if state["status"] == "uncertain":
                        return self._recover(client, state)
                if operation == "page":
                    return read_current_preview(client, **self._identity(state)).public_data()
                state.update(status="uncertain", operation=operation, question_id=question_id, choice_id=choice_id, retain=retain)
                self._save(state)  # durable intent before any mutation
                try:
                    if operation == "start":
                        start_state = state

                        def seal_identity(attempt_id: int, page: int) -> None:
                            # Durable before the page readback, still uncertain.
                            start_state.update(attempt_id=attempt_id, page=page)
                            self._save(start_state)

                        result = start_preview(
                            client, course_id=self.course_id, quiz_id=self.quiz_id,
                            bypass_availability=bypass_availability, on_identity=seal_identity,
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
                        receipt = submit_preview(client, **self._identity(state), retain=retain, actor_id=actor)
                except PreviewStartUnknownError as exc:
                    state["status"] = "uncertain"
                    if type(exc.attempt_id) is int and type(exc.page) is int:
                        try:
                            page_path(self.course_id, self.quiz_id, exc.attempt_id, exc.page)
                        except ValueError:
                            pass
                        else:
                            state["attempt_id"] = exc.attempt_id
                            state["page"] = exc.page
                    self._save(state)
                    raise
                except _UNCERTAIN:
                    state["status"] = "uncertain"
                    self._save(state)
                    raise
                except Exception:
                    # Known pre-mutation validation errors do not consume a step.
                    if previous is not None:
                        self._save(previous)
                    elif state is not None:
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
                state.update(status="active", operation=None, attempt_id=result.attempt_id, page=result.page)
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
                verify_receipt(client, course_id=self.course_id, quiz_id=self.quiz_id, attempt_id=state["attempt_id"], actor_id=state["actor_id"]),
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
        elif operation == "start":
            try:
                result = read_current_preview(client, **identity)
            except PreviewPageError:
                raise PreviewWorkflowError(
                    "The started preview's page could not be verified. Run preview reconcile."
                ) from None
            state.update(status="active", operation=None, page=result.page)
            self._save(state)
            return result.public_data()
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
