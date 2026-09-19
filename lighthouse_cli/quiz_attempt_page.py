"""Parse a current preview page, without executing Brightspace scripts.

This is a parser, not an attempt driver. In particular, the server's HTML
form lacks some runtime-populated fields required for successful writes.
Never treat its hidden fields as a ready-to-replay submission request.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Tag

from .display import safe_display_text
from .request_protection import FormProtection

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_QUESTIONS = 200


class PreviewPageError(ValueError):
    """A fixed diagnostic that never includes HTML, state values or URLs."""

    def __init__(self) -> None:
        super().__init__("The response is not a supported current quiz preview page.")


def _id(value: object) -> int:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 18
    ):
        raise PreviewPageError()
    result = int(value)
    if result <= 0:
        raise PreviewPageError()
    return result


def _metadata(container: Tag, name: str) -> str:
    values = container.select(f".{name} input")
    if len(values) != 1:
        raise PreviewPageError()
    value = values[0].get("value")
    if not isinstance(value, str):
        raise PreviewPageError()
    return value


def _expanded(node: Tag) -> BeautifulSoup:
    copy = BeautifulSoup(str(node), "html.parser")
    # Brightspace puts question content in a custom element's html attribute;
    # textContent alone would silently produce an empty prompt.
    for block in copy.find_all("d2l-html-block"):
        content = block.get("html")
        if isinstance(content, str):
            block.replace_with(BeautifulSoup(content, "html.parser"))
    return copy


def _text(node: Tag) -> str:
    copy = _expanded(node)
    for element in copy.select("script, style, button, input, fieldset, legend"):
        element.decompose()
    text = copy.get_text(" ", strip=True)
    if len(text) > 16384:
        return ""
    return safe_display_text(text, "", max_len=16384)


def hidden_form(body: bytes) -> tuple[Tag, dict[str, str]]:
    """Read one bounded form. Returned values must remain private."""
    if not isinstance(body, bytes) or len(body) > MAX_PAGE_BYTES:
        raise PreviewPageError()
    forms = BeautifulSoup(body, "html.parser").find_all("form")
    if len(forms) != 1:
        raise PreviewPageError()
    form = forms[0]
    hidden: dict[str, str] = {}
    for element in form.select('input[type="hidden"][name]'):
        name = element.get("name")
        value = element.get("value", "")
        if not isinstance(name, str) or not name:
            continue
        if name in hidden or not isinstance(value, str):
            raise PreviewPageError()
        hidden[name] = value
    return form, hidden


@dataclass(frozen=True)
class PreviewPage:
    course_id: int
    quiz_id: int
    attempt_id: int
    page: int
    questions: tuple[dict[str, Any], ...]
    has_next_control: bool
    has_previous_control: bool
    # Private debugging metadata, kept out of repr and public_data(). These
    # fields may contain session-bound form tokens and must not be logged.
    _hidden_fields: dict[str, str] = field(repr=False, compare=False)
    _groups: dict[int, tuple[str, int, int, int]] = field(repr=False, compare=False)

    def public_data(self) -> dict[str, Any]:
        return {
            "mode": "preview",
            "course_id": self.course_id,
            "quiz_id": self.quiz_id,
            "attempt_id": self.attempt_id,
            "page": self.page,
            "questions": list(self.questions),
            "has_next_control": self.has_next_control,
            "has_previous_control": self.has_previous_control,
        }

    def confirms_answer(self, question_id: int, choice_id: int) -> bool:
        """A 200 response is insufficient: require saved, selected readback."""
        if type(question_id) is not int or type(choice_id) is not int:
            return False
        return any(
            q["question_id"] == question_id
            and q["supported"]
            and q["saved"] is True
            and q["selected_choice_ids"] == [choice_id]
            for q in self.questions
        )

    def answer_fields(
        self, question_id: int, choice_id: int, protection: FormProtection
    ) -> dict[str, str]:
        """Build one autosave form. Returned fields are secret-bearing.

        Callers must send once and verify a fresh readback. This helper does
        not advance pages, finalize attempts or grant navigation permission.
        """
        if (
            type(question_id) is not int
            or type(choice_id) is not int
            or not all(q["supported"] for q in self.questions)
        ):
            raise PreviewPageError()
        question = next((q for q in self.questions if q["question_id"] == question_id), None)
        if question is None or choice_id not in {c["choice_id"] for c in question["choices"]}:
            raise PreviewPageError()
        if self._hidden_fields.get("d2l_referrer") != protection.csrf_token:
            raise PreviewPageError()
        try:
            control_map = json.loads(self._hidden_fields["d2l_controlMap"])
            if (
                not isinstance(control_map, list)
                or not control_map
                or not isinstance(control_map[0], dict)
            ):
                raise PreviewPageError()
            response_control = control_map[0][f"hdn_resp_{question_id}"]
            if not isinstance(response_control, list) or not response_control:
                raise PreviewPageError()
            response_name = response_control[0]
            if (
                not isinstance(response_name, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,80}", response_name)
                or response_name not in self._hidden_fields
            ):
                raise PreviewPageError()
        except (KeyError, TypeError, ValueError, RecursionError):
            raise PreviewPageError() from None
        fields = dict(self._hidden_fields)
        for q in self.questions:
            group = self._groups[q["question_id"]][0]
            if q["selected_choice_ids"]:
                fields[group] = str(q["selected_choice_ids"][0])
        group, tid, tvid, ordinal = self._groups[question_id]
        fields[group] = str(choice_id)
        fields[response_name] = "1"
        fields["d2l_action"] = "Update"
        fields["d2l_actionparam"] = f"3,{self.page},{tid},{tvid},{ordinal}"
        fields["d2l_hitCode"] = protection.next_hit_code()
        return fields

    def ready_to_leave(self) -> bool:
        return bool(self.questions) and all(
            q["supported"] and q["saved"] is True and len(q["selected_choice_ids"]) == 1
            for q in self.questions
        )

    def advance_fields(self, protection: FormProtection) -> dict[str, str]:
        """Forward only, after every answer on this page is confirmed saved."""
        if not self.ready_to_leave() or not self.has_next_control:
            raise PreviewPageError()
        first = self.questions[0]
        fields = self.answer_fields(
            first["question_id"], first["selected_choice_ids"][0], protection
        )
        fields["d2l_actionparam"] = f"2,{self.page + 1},{self.page}"
        return fields


def parse_preview_page(
    body: bytes,
    *,
    course_id: int,
    quiz_id: int,
    attempt_id: int,
    page: int,
) -> PreviewPage:
    """Reject wrong attempts, student pages, duplicates and incomplete pages.

    Only radio-choice questions are characterized so far. Other controls and
    media are reported as unsupported rather than silently treated as text.
    Navigation flags describe rendered controls, not permission to construct
    arbitrary page URLs or return to a previous page.
    """
    if not isinstance(body, bytes) or len(body) > MAX_PAGE_BYTES:
        raise PreviewPageError()
    if any(
        type(value) is not int or value <= 0 for value in (course_id, quiz_id, attempt_id, page)
    ):
        raise PreviewPageError()
    form, hidden = hidden_form(body)
    expected = {"ou": course_id, "qi": quiz_id, "ai": attempt_id, "pg": page}
    if hidden.get("isprv") != "1" or any(
        _id(hidden.get(key)) != value for key, value in expected.items()
    ):
        raise PreviewPageError()

    containers = form.select(".d2l-quiz-question-autosave-container")
    if not containers or len(containers) > MAX_QUESTIONS:
        raise PreviewPageError()
    questions: list[dict[str, Any]] = []
    ids: set[int] = set()
    ordinals: set[int] = set()
    groups: dict[int, tuple[str, int, int, int]] = {}
    for container in containers:
        qid = _id(_metadata(container, "d2l-quiz-question-object-id"))
        ordinal = _id(_metadata(container, "d2l-quiz-question-autosave-question-num"))
        if (
            qid in ids
            or ordinal in ordinals
            or _id(_metadata(container, "d2l-quiz-question-autosave-page")) != page
        ):
            raise PreviewPageError()
        ids.add(qid)
        ordinals.add(ordinal)
        tid = _id(_metadata(container, "d2l-quiz-question-autosave-tid"))
        tvid = _id(_metadata(container, "d2l-quiz-question-autosave-tvid"))
        groups[qid] = (f"tAtom{tid}_{tvid}", tid, tvid, ordinal)
        saved = _metadata(container, "d2l-quiz-question-autosave-is-saved")
        saved_value = {"true": True, "false": False}.get(saved.lower())
        prompt = container.select_one('[id^="d2l_read_element_"]')
        if prompt is None:
            raise PreviewPageError()
        radios = container.select('input[type="radio"]')
        unsupported = bool(
            _expanded(container).select(
                'textarea, select, input[type="checkbox"], input[type="text"], img, math, iframe, audio, video'
            )
        )
        question_text = _text(prompt)
        unsupported = unsupported or not question_text
        choices: list[dict[str, Any]] = []
        selected: list[int] = []
        choice_ids: set[int] = set()
        for radio in radios:
            if (
                radio.has_attr("disabled")
                or radio.get("aria-disabled") == "true"
                or radio.find_parent("fieldset", attrs={"disabled": True})
            ):
                unsupported = True
            if radio.get("name") != f"tAtom{tid}_{tvid}":
                raise PreviewPageError()
            cid = _id(radio.get("value"))
            if cid in choice_ids:
                raise PreviewPageError()
            choice_ids.add(cid)
            radio_id = radio.get("id")
            label = (
                container.find("label", attrs={"for": radio_id})
                if isinstance(radio_id, str)
                else None
            )
            label = label if label is not None else radio.find_parent("tr")
            if label is None:
                raise PreviewPageError()
            choices.append({"choice_id": cid, "text": _text(label)})
            if radio.has_attr("checked"):
                selected.append(cid)
        if len(selected) > 1:
            raise PreviewPageError()
        unsupported = unsupported or any(not choice["text"] for choice in choices)
        questions.append(
            {
                "question_id": qid,
                "number": ordinal,
                "text": question_text,
                "kind": "single-choice" if radios and not unsupported else "unsupported",
                "supported": bool(radios) and not unsupported,
                "choices": choices,
                "selected_choice_ids": selected,
                "saved": saved_value,
            }
        )

    def button_present(label: str) -> bool:
        return any(
            button.get_text(" ", strip=True) == label
            and not button.has_attr("disabled")
            and button.get("aria-disabled") != "true"
            for button in form.find_all("button")
        )

    return PreviewPage(
        course_id,
        quiz_id,
        attempt_id,
        page,
        tuple(questions),
        button_present("Next Page"),
        button_present("Previous Page"),
        hidden,
        groups,
    )
