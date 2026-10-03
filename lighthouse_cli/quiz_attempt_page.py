"""Parse a current preview or learner attempt page, without executing scripts.

This is a parser, not an attempt driver. In particular, the server's HTML
form lacks some runtime-populated fields required for successful writes.
Never treat its hidden fields as a ready-to-replay submission request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from bs4 import BeautifulSoup, Tag

from .display import safe_display_text
from .request_protection import FormProtection

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_QUESTIONS = 200


class PreviewPageError(ValueError):
    """A fixed diagnostic that never includes HTML, state values or URLs."""

    def __init__(self) -> None:
        super().__init__("The response is not a supported current quiz preview page.")


class PreviewRefusedError(ValueError):
    """A fixed, actionable refusal raised before any state-changing request.

    Only the module constants below may be passed; callers show the message
    verbatim, so it must never contain page content, tokens or URLs.
    """


REFUSE_UNSUPPORTED = "This page has a question type the preview driver does not support."
REFUSE_NOT_ON_PAGE = "That question is not on the current preview page. Run preview page to see it."
REFUSE_NOT_A_CHOICE = "That choice is not an option for this question."
REFUSE_UNANSWERED = "Answer and save every question on this page first."
REFUSE_LAST_PAGE = "This is the last page. Use preview submit."
REFUSE_NOT_LAST_PAGE = "Move to the last page with preview next before submitting."


def _id(value: object) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 18:
        raise PreviewPageError()
    result = int(value)
    if result <= 0:
        raise PreviewPageError()
    return result


def _token(value: object) -> str:
    """A learner choice, option or blank id: ``o<digits>`` or a positive integer."""
    if not isinstance(value, str) or not re.fullmatch(r"o[0-9]{1,18}|[1-9][0-9]{0,17}", value):
        raise PreviewPageError()
    return value


def _metadata(container: Tag, name: str) -> str:
    values = container.select(f".{name} input")
    if len(values) != 1:
        raise PreviewPageError()
    value = values[0].get("value")
    if not isinstance(value, str):
        raise PreviewPageError()
    return value


def _inside_options(node: Tag, container: Tag) -> bool:
    """Whether ``node`` sits inside the answer options of ``container``."""
    for parent in node.parents:
        if parent is container:
            return False
        if parent.name in {"fieldset", "table", "label"}:
            return True
    return False


def _expand_blocks(copy: BeautifulSoup) -> BeautifulSoup:
    # Brightspace puts question content in a custom element's html attribute;
    # textContent alone would silently produce an empty prompt.
    for block in copy.find_all("d2l-html-block"):
        content = block.get("html")
        if isinstance(content, str):
            block.replace_with(BeautifulSoup(content, "html.parser"))
    return copy


def _expanded(node: Tag) -> BeautifulSoup:
    return _expand_blocks(BeautifulSoup(str(node), "html.parser"))


def _input_type(control: Tag) -> str:
    """HTML input types are case-insensitive, and a missing type means text."""
    return str(control.get("type", "text")).lower()


def _text(node: Tag, *, blanks: bool = False) -> str:
    copy = BeautifulSoup(str(node), "html.parser")
    removed = "script, style, button, input, fieldset, legend"
    if blanks:
        # A fill-in-the-blank sentence is the options fieldset itself: keep
        # it and mark each blank, numbered in document order. Inputs in the
        # custom blocks are content, so number before expanding them.
        removed = "script, style, button, input, legend"
        text_inputs = [control for control in copy.find_all("input") if _input_type(control) == "text"]
        for number, blank in enumerate(text_inputs, 1):
            blank.replace_with(f" (blank {number}) ")
    _expand_blocks(copy)
    for element in copy.select(removed):
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
            "mode": "preview", "course_id": self.course_id,
            "quiz_id": self.quiz_id, "attempt_id": self.attempt_id,
            "page": self.page, "questions": list(self.questions),
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

    def answer_fields(self, question_id: int, choice_id: int, protection: FormProtection) -> dict[str, str]:
        """Build one autosave form. Returned fields are secret-bearing.

        Callers must send once and verify a fresh readback. This helper does
        not advance pages, finalize attempts or grant navigation permission.
        """
        if type(question_id) is not int or type(choice_id) is not int:
            raise PreviewPageError()
        if not all(q["supported"] for q in self.questions):
            raise PreviewRefusedError(REFUSE_UNSUPPORTED)
        question = next((q for q in self.questions if q["question_id"] == question_id), None)
        if question is None:
            raise PreviewRefusedError(REFUSE_NOT_ON_PAGE)
        if choice_id not in {c["choice_id"] for c in question["choices"]}:
            raise PreviewRefusedError(REFUSE_NOT_A_CHOICE)
        if self._hidden_fields.get("d2l_referrer") != protection.csrf_token:
            raise PreviewPageError()
        try:
            control_map = json.loads(self._hidden_fields["d2l_controlMap"])
            if not isinstance(control_map, list) or not control_map or not isinstance(control_map[0], dict):
                raise PreviewPageError()
            response_control = control_map[0][f"hdn_resp_{question_id}"]
            if not isinstance(response_control, list) or not response_control:
                raise PreviewPageError()
            response_name = response_control[0]
            if (not isinstance(response_name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,80}", response_name)
                    or response_name not in self._hidden_fields):
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
        if not self.has_next_control:
            raise PreviewRefusedError(REFUSE_LAST_PAGE)
        if not self.ready_to_leave():
            raise PreviewRefusedError(REFUSE_UNANSWERED)
        first = self.questions[0]
        fields = self.answer_fields(first["question_id"], first["selected_choice_ids"][0], protection)
        fields["d2l_actionparam"] = f"2,{self.page + 1},{self.page}"
        return fields


_ChoiceId = TypeVar("_ChoiceId", int, str)


def _attempt_form(
    body: bytes, isprv: str, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> tuple[Tag, dict[str, str]]:
    """Check the attempt identity; ``isprv`` is ``1`` on previews, empty for learners."""
    if not isinstance(body, bytes) or len(body) > MAX_PAGE_BYTES:
        raise PreviewPageError()
    if any(type(value) is not int or value <= 0 for value in (course_id, quiz_id, attempt_id, page)):
        raise PreviewPageError()
    form, hidden = hidden_form(body)
    expected = {"ou": course_id, "qi": quiz_id, "ai": attempt_id, "pg": page}
    if hidden.get("isprv") != isprv or any(_id(hidden.get(key)) != value for key, value in expected.items()):
        raise PreviewPageError()
    return form, hidden


def _question_containers(form: Tag, page: int) -> list[tuple[Tag, int, int, int, int, bool | None]]:
    """Each question container with its object id, number, tid, tvid and saved flag."""
    containers = form.select(".d2l-quiz-question-autosave-container")
    if not containers or len(containers) > MAX_QUESTIONS:
        raise PreviewPageError()
    result: list[tuple[Tag, int, int, int, int, bool | None]] = []
    ids: set[int] = set()
    ordinals: set[int] = set()
    # Controls sharing a tAtom name are one group in the browser.
    groups: set[tuple[int, int]] = set()
    for container in containers:
        qid = _id(_metadata(container, "d2l-quiz-question-object-id"))
        ordinal = _id(_metadata(container, "d2l-quiz-question-autosave-question-num"))
        if qid in ids or ordinal in ordinals or _id(_metadata(container, "d2l-quiz-question-autosave-page")) != page:
            raise PreviewPageError()
        ids.add(qid)
        ordinals.add(ordinal)
        tid = _id(_metadata(container, "d2l-quiz-question-autosave-tid"))
        tvid = _id(_metadata(container, "d2l-quiz-question-autosave-tvid"))
        if (tid, tvid) in groups:
            raise PreviewPageError()
        groups.add((tid, tvid))
        saved = _metadata(container, "d2l-quiz-question-autosave-is-saved")
        result.append((container, qid, ordinal, tid, tvid, {"true": True, "false": False}.get(saved.lower())))
    return result


def _prompt(container: Tag) -> Tag | None:
    prompt = container.select_one('[id^="d2l_read_element_"]')
    if prompt is None:
        # Brightspace tenant variants sometimes put the prompt directly
        # in one custom HTML block without the legacy read-element ID.
        # Rich-text answer choices use the same element, so only blocks
        # outside the options (fieldset/table/label) can be the prompt.
        blocks = [block for block in container.find_all("d2l-html-block")
                  if not _inside_options(block, container)]
        if len(blocks) == 1 and isinstance(blocks[0].get("html"), str):
            prompt = blocks[0]
    return prompt


def _disabled(control: Tag) -> bool:
    return bool(control.has_attr("disabled") or control.get("aria-disabled") == "true"
                or control.find_parent("fieldset", attrs={"disabled": True}))


def _choices(
    container: Tag, controls: list[Tag], group: str, choice_id: Callable[[Tag, str], _ChoiceId],
) -> tuple[list[dict[str, Any]], list[_ChoiceId], bool]:
    """Labelled choices, the checked ids and whether any control is disabled."""
    choices: list[dict[str, Any]] = []
    selected: list[_ChoiceId] = []
    seen: set[_ChoiceId] = set()
    control_ids: set[str] = set()
    disabled = False
    for control in controls:
        disabled = disabled or _disabled(control)
        cid = choice_id(control, group)
        if cid in seen:
            raise PreviewPageError()
        seen.add(cid)
        control_id = control.get("id")
        if isinstance(control_id, str):
            # A shared id would give two choices the first one's label.
            if control_id in control_ids:
                raise PreviewPageError()
            control_ids.add(control_id)
        label = (
            container.find("label", attrs={"for": control_id})
            if isinstance(control_id, str)
            else None
        )
        label = label if label is not None else control.find_parent("tr")
        if label is None:
            raise PreviewPageError()
        choices.append({"choice_id": cid, "text": _text(label)})
        if control.has_attr("checked"):
            selected.append(cid)
    return choices, selected, disabled


def _radio_id(radio: Tag, group: str) -> int:
    if radio.get("name") != group:
        raise PreviewPageError()
    return _id(radio.get("value"))


def _hidden(node: Tag) -> bool:
    return any(tag.has_attr("hidden") or "d2l-hidden" in tag.get_attribute_list("class")
               for tag in (node, *node.parents))


def _button_present(form: Tag, label: str, *, visible_only: bool = False) -> bool:
    # A button inside a question is its content, not page navigation.
    return any(
        button.get_text(" ", strip=True) == label
        and not _disabled(button)
        and button.find_parent(class_="d2l-quiz-question-autosave-container") is None
        and not (visible_only and _hidden(button))
        for button in form.find_all("button")
    )


def parse_preview_page(
    body: bytes, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> PreviewPage:
    """Reject wrong attempts, student pages, duplicates and incomplete pages.

    Only radio-choice questions are characterized so far. Other controls and
    media are reported as unsupported rather than silently treated as text.
    Navigation flags describe rendered controls, not permission to construct
    arbitrary page URLs or return to a previous page.
    """
    form, hidden = _attempt_form(body, "1", course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
    questions: list[dict[str, Any]] = []
    groups: dict[int, tuple[str, int, int, int]] = {}
    for container, qid, ordinal, tid, tvid, saved_value in _question_containers(form, page):
        groups[qid] = (f"tAtom{tid}_{tvid}", tid, tvid, ordinal)
        prompt = _prompt(container)
        if prompt is None:
            raise PreviewPageError()
        radios = container.select('input[type="radio"]')
        unsupported = bool(_expanded(container).select('textarea, select, input[type="checkbox"], input[type="text"], img, math, iframe, audio, video'))
        question_text = _text(prompt)
        choices, selected, disabled = _choices(container, radios, groups[qid][0], _radio_id)
        if len(selected) > 1:
            raise PreviewPageError()
        unsupported = unsupported or disabled or not question_text or any(not choice["text"] for choice in choices)
        questions.append({
            "question_id": qid, "number": ordinal, "text": question_text,
            "kind": "single-choice" if radios and not unsupported else "unsupported",
            "supported": bool(radios) and not unsupported,
            "choices": choices, "selected_choice_ids": selected, "saved": saved_value,
        })

    return PreviewPage(course_id, quiz_id, attempt_id, page, tuple(questions),
                       _button_present(form, "Next Page"), _button_present(form, "Previous Page"), hidden, groups)


@dataclass(frozen=True)
class LearnerPage:
    """A learner's attempt page. Choice, option and blank ids are strings."""

    course_id: int
    quiz_id: int
    attempt_id: int
    page: int
    questions: tuple[dict[str, Any], ...]
    has_next_control: bool
    has_previous_control: bool
    # Private, as on PreviewPage: session-bound form tokens, never logged.
    _hidden_fields: dict[str, str] = field(repr=False, compare=False)
    _groups: dict[int, tuple[str, int, int, int]] = field(repr=False, compare=False)

    def public_data(self) -> dict[str, Any]:
        return {
            "mode": "learner", "course_id": self.course_id,
            "quiz_id": self.quiz_id, "attempt_id": self.attempt_id,
            "page": self.page, "questions": list(self.questions),
            "has_next_control": self.has_next_control,
            "has_previous_control": self.has_previous_control,
        }


_LEARNER_KINDS = {"radio": "single-choice", "checkbox": "multi-select", "text": "fill-blank"}


def _learner_radio_id(radio: Tag, group: str) -> str:
    if radio.get("name") != group:
        raise PreviewPageError()
    return _token(radio.get("value"))


def _suffix_id(control: Tag, group: str) -> str:
    """The option or blank id that ends a ``<group>_<id>`` control name."""
    name = control.get("name")
    if not isinstance(name, str) or not name.startswith(f"{group}_"):
        raise PreviewPageError()
    return _token(name[len(group) + 1:])


def _learner_option_id(checkbox: Tag, group: str) -> str:
    if checkbox.get("value") != "1":
        raise PreviewPageError()
    return _suffix_id(checkbox, group)


def _blanks(controls: list[Tag], group: str) -> tuple[list[dict[str, Any]], bool]:
    """Each blank's id, its number in the question text and its current value."""
    blanks: list[dict[str, Any]] = []
    seen: set[str] = set()
    disabled = False
    for number, blank in enumerate(controls, 1):
        disabled = disabled or _disabled(blank) or blank.has_attr("readonly")
        blank_id = _suffix_id(blank, group)
        value = blank.get("value", "")
        if blank_id in seen or not isinstance(value, str) or len(value) > 10000:
            raise PreviewPageError()
        seen.add(blank_id)
        blanks.append({"blank_id": blank_id, "number": number, "value": value})
    return blanks, disabled


def _learner_question(container: Tag, qid: int, ordinal: int, group: str, saved: bool | None) -> dict[str, Any]:
    """One question of a single known kind; mixed or other controls are unsupported."""
    controls = [control for control in container.find_all("input") if _input_type(control) != "hidden"]
    types = {_input_type(control) for control in controls}
    kind = _LEARNER_KINDS.get(types.pop()) if len(types) == 1 else None
    expanded = _expanded(container)
    # Inputs inside custom HTML blocks are content, not form controls.
    unsupported = (bool(expanded.select("textarea, select, img, math, iframe, audio, video"))
                   or len([c for c in expanded.find_all("input") if _input_type(c) != "hidden"]) != len(controls))
    choices: list[dict[str, Any]] = []
    selected: list[str] = []
    blanks: list[dict[str, Any]] = []
    disabled = False
    if kind == "single-choice":
        choices, selected, disabled = _choices(container, controls, group, _learner_radio_id)
        if len(selected) > 1:
            raise PreviewPageError()
    elif kind == "multi-select":
        choices, selected, disabled = _choices(container, controls, group, _learner_option_id)
    elif kind == "fill-blank":
        blanks, disabled = _blanks(controls, group)
    if kind == "fill-blank":
        text = _text(container, blanks=True)
    else:
        # A known kind needs its prompt; any other question is reported as
        # unsupported, not allowed to abort the whole page.
        prompt = _prompt(container)
        if prompt is None and kind is not None:
            raise PreviewPageError()
        text = _text(prompt) if prompt is not None else ""
    supported = kind is not None and not (
        unsupported or disabled or not text or any(not choice["text"] for choice in choices))
    return {
        "question_id": qid, "number": ordinal, "text": text,
        "kind": kind if supported else "unsupported", "supported": supported,
        "choices": choices, "selected_choice_ids": selected, "blanks": blanks, "saved": saved,
    }


def parse_learner_page(
    body: bytes, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> LearnerPage:
    """Read a learner's attempt page (empty ``isprv``) with the preview's checks.

    True/false and multiple choice are radios, multi-select is one checkbox
    per option and fill-in-the-blank is one text input per blank. Ids are
    strings, as multiple-choice values and option names carry opaque tokens.
    Hidden buttons, such as the always-present "Save All Responses", are not
    controls. Unrecognized controls and media are reported as unsupported.
    """
    form, hidden = _attempt_form(body, "", course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
    questions: list[dict[str, Any]] = []
    groups: dict[int, tuple[str, int, int, int]] = {}
    for container, qid, ordinal, tid, tvid, saved in _question_containers(form, page):
        groups[qid] = (f"tAtom{tid}_{tvid}", tid, tvid, ordinal)
        questions.append(_learner_question(container, qid, ordinal, groups[qid][0], saved))
    return LearnerPage(course_id, quiz_id, attempt_id, page, tuple(questions),
                       _button_present(form, "Next Page", visible_only=True),
                       _button_present(form, "Previous Page", visible_only=True), hidden, groups)
