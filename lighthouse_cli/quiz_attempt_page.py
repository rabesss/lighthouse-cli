"""Parse a current preview or learner attempt page, without executing scripts.

This is a parser, not an attempt driver. In particular, the server's HTML
form lacks some runtime-populated fields required for successful writes.
Never treat its hidden fields as a ready-to-replay submission request.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from .quiz_math import mathml_to_latex
from .request_protection import FormProtection, form_protection_from_homepage

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
REFUSE_LEARNER_UNSUPPORTED = ("This page has a question the CLI cannot answer yet, so its form cannot be sent. "
                              "Answer and submit this page in Brightspace in a browser.")
REFUSE_LEARNER_NOT_ON_PAGE = "That question is not on the current page."
REFUSE_ANSWER_SHAPE = ("Answer a single-choice question with one choice id, a multi-select question with "
                       "a list of option ids and a fill-in-the-blank question with one text per blank.")
REFUSE_BLANK_TEXT = "Each blank takes one line of printable text of at most 1000 characters."
REFUSE_NO_ANSWERS = "Give at least one answer to save."
REFUSE_LEARNER_LAST_PAGE = "This is the last page. Submit the quiz instead."
REFUSE_LEARNER_FIRST_PAGE = "There is no previous page: this is the first page, or the quiz does not allow moving back."
REFUSE_LEARNER_NOT_LAST_PAGE = "Move to the last page before submitting."
REFUSE_LEARNER_UNANSWERED = "This page has unanswered questions. Answer them, or explicitly allow leaving them unanswered."


def _id(value: object) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 18 or int(value) <= 0:
        raise PreviewPageError()
    return int(value)


def _token(value: object) -> str:
    """A learner choice, option or blank id: ``o<digits>`` or a positive integer."""
    if not isinstance(value, str) or not re.fullmatch(r"o[0-9]{1,18}|[1-9][0-9]{0,17}", value):
        raise PreviewPageError()
    return value


def _metadata(container: Tag, name: str) -> str:
    values = container.select(f".{name} input")
    value = values[0].get("value") if len(values) == 1 else None
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


def _drop_templates(root: Tag) -> None:
    """Remove ``<template>`` content, which browsers never render or submit."""
    while (template := root.find("template")) is not None:
        template.decompose()


def _expand_blocks(copy: BeautifulSoup) -> BeautifulSoup:
    # Brightspace puts question content in a custom element's html attribute;
    # textContent alone would silently produce an empty prompt.
    for block in copy.find_all("d2l-html-block"):
        content = block.get("html")
        if isinstance(content, str):
            block.replace_with(BeautifulSoup(content, "html.parser"))
    _drop_templates(copy)
    return copy


def _expanded(node: Tag) -> BeautifulSoup:
    return _expand_blocks(BeautifulSoup(str(node), "html.parser"))


_INPUT_TYPES = {"hidden", "text", "search", "tel", "url", "email", "password", "date", "month", "week", "time",
                "datetime-local", "number", "range", "color", "checkbox", "radio", "file", "submit", "image",
                "reset", "button"}


def _input_type(control: Tag) -> str:
    """HTML input types are case-insensitive; a missing, empty or unknown type means text."""
    value = str(control.get("type", "")).lower()
    return value if value in _INPUT_TYPES else "text"


@dataclass
class _Media:
    """The images and equations one question's text shows, in reading order."""

    images: list[dict[str, Any]] = field(default_factory=list)
    equations: int = 0
    unsupported: bool = False


# A root-relative or web address; others (data:, page-relative) are not fetched,
# nor are paths with dot segments, which a browser would resolve first.
_IMAGE_SOURCE = re.compile(r"/(?!/)[!-~]{0,2047}|https?://[!-~]{1,2040}", re.IGNORECASE)
_DOT_SEGMENT = re.compile(r"/(?:\.|%2e){1,2}(?:/|$)", re.IGNORECASE)
# Soft hyphens, zero-width spaces and joiners, word joiners and byte order
# marks are invisible and meaningless here; other format characters, such as
# bidirectional controls, still void the text.
_INVISIBLE_MARKS = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\u2060\ufeff"))
# Elements that start a new line, so their text is not run into a neighbour's.
_BLOCK_TAGS = ["address", "article", "aside", "blockquote", "br", "caption", "center", "dd", "details", "dir", "div",
               "dl", "dt", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
               "label", "li", "main", "menu", "nav", "ol", "p", "pre", "section", "summary", "table", "tbody", "td",
               "tfoot", "th", "thead", "tr", "ul"]


def _clean(text: str, limit: int = 16384) -> str | None:
    text = " ".join(text.translate(_INVISIBLE_MARKS).split())
    return text if len(text) <= limit and text.isprintable() else None


def _render_media(copy: BeautifulSoup, media: _Media) -> None:
    """Write equations as LaTeX, images as numbered markers and HTML scripts as ^{} and _{}."""
    while (math := copy.find("math")) is not None:
        try:
            latex = mathml_to_latex(math)
            media.equations += 1
        except ValueError:
            latex = ""
            media.unsupported = True
        math.replace_with(latex)
    for image in copy.find_all("img"):
        source = image.get("src")
        if not (isinstance(source, str) and _IMAGE_SOURCE.fullmatch(source) and "\\" not in source
                and not _DOT_SEGMENT.search(re.split(r"[?#]", source, maxsplit=1)[0])):
            source = None
        alt = _clean(str(image.get("alt", "")), 1000)
        number = len(media.images) + 1
        media.images.append({"number": number, "src": source, "alt": alt or ""})
        media.unsupported = media.unsupported or source is None or alt is None
        image.replace_with(f"[image {number}: {alt}]" if alt else f"[image {number}]")
    # Innermost first, so x<sup>y<sup>2</sup></sup> keeps its nesting.
    for script in reversed(copy.find_all(["sup", "sub"])):
        script.replace_with(f"{'^' if script.name == 'sup' else '_'}{{{script.get_text()}}}")


def _text(node: Tag, *, blanks: bool = False, media: _Media | None = None) -> str:
    media = media if media is not None else _Media()
    copy = BeautifulSoup(str(node), "html.parser")
    removed = "script, style, noscript, button, input, fieldset, legend"
    if blanks:
        # A fill-in-the-blank sentence is the options fieldset itself: keep
        # it and mark each blank, numbered in document order. Inputs in the
        # custom blocks are content, so number before expanding them.
        removed = "script, style, noscript, button, input, legend"
        text_inputs = [control for control in copy.find_all("input") if _input_type(control) == "text"]
        for number, blank in enumerate(text_inputs, 1):
            # A blank in text that is not read, such as a legend, would lose its marker.
            media.unsupported = media.unsupported or blank.find_parent(removed.split(", ")) is not None
            blank.replace_with(f" (blank {number}) ")
    _expand_blocks(copy)
    for element in copy.select(removed):
        element.decompose()
    # Spaced first, so a line break inside a superscript does not join its lines.
    for element in copy.find_all(_BLOCK_TAGS):
        element.insert_before(" ")
        element.insert_after(" ")
    _render_media(copy, media)
    # Quiz content is authored text the answer depends on, so it is not
    # screened like a label: words such as "password", a JSON snippet, a
    # non-breaking space or a line break must survive. Only whitespace is
    # compacted; control characters still void the text, and any image or
    # equation it showed.
    text = _clean(copy.get_text())
    media.unsupported = media.unsupported or text is None
    return text or ""


def rpc_script(chunks: Iterable[object]) -> str:
    """The script a bounded legacy RPC reply returns, without whitespace.

    It is compared as data and never executed. Any other reply shape raises.
    """
    data = bytearray()
    for chunk in chunks:
        if not isinstance(chunk, bytes) or len(data) + len(chunk) > 65536:
            raise ValueError("Unexpected RPC reply.")
        data.extend(chunk)
    try:
        reply = json.loads(bytes(data).decode("utf-8").removeprefix("while(true){}"))
    except RecursionError:
        raise ValueError("Unexpected RPC reply.") from None
    if (not isinstance(reply, dict) or type(reply.get("ResponseType")) is not int
            or reply["ResponseType"] != 0 or reply.get("IsResultMin") is not False
            or reply.get("RedirectUrl") != "" or not isinstance(reply.get("Result"), str)):
        raise ValueError("Unexpected RPC reply.")
    return re.sub(r"\s+", "", reply["Result"]).removesuffix(";")


def hidden_form(body: bytes) -> tuple[Tag, dict[str, str]]:
    """Read one bounded form. Returned values must remain private."""
    if not isinstance(body, bytes) or len(body) > MAX_PAGE_BYTES:
        raise PreviewPageError()
    forms = BeautifulSoup(body, "html.parser").find_all("form")
    if len(forms) != 1:
        raise PreviewPageError()
    form = forms[0]
    _drop_templates(form)
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
class _AttemptPage:
    """The fields and public view a preview and a learner page share."""

    _mode: ClassVar[str]
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
            "mode": self._mode, "course_id": self.course_id,
            "quiz_id": self.quiz_id, "attempt_id": self.attempt_id,
            "page": self.page, "questions": list(self.questions),
            "has_next_control": self.has_next_control,
            "has_previous_control": self.has_previous_control,
        }


@dataclass(frozen=True)
class PreviewPage(_AttemptPage):
    _mode: ClassVar[str] = "preview"

    def confirms_answer(self, question_id: int, choice_id: int) -> bool:
        """A 200 response is insufficient: require saved, selected readback."""
        if type(question_id) is not int or type(choice_id) is not int:
            return False
        return any(q["question_id"] == question_id and q["supported"] and q["saved"] is True
                   and q["selected_choice_ids"] == [choice_id] for q in self.questions)

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


def _in_disabled_fieldset(control: Tag) -> bool:
    """Inside a disabled fieldset, but not in its first legend, which HTML keeps enabled."""
    for fieldset in control.find_parents("fieldset"):
        if fieldset.has_attr("disabled"):
            legend = fieldset.find("legend", recursive=False)
            if legend is None or not any(parent is legend for parent in control.parents):
                return True
    return False


def _disabled(control: Tag) -> bool:
    return bool(control.has_attr("disabled") or control.get("aria-disabled") == "true" or control.has_attr("inert")
                or _in_disabled_fieldset(control) or control.find_parent(attrs={"inert": True}))


def _choices(
    container: Tag, controls: list[Tag], group: str, choice_id: Callable[[Tag, str], _ChoiceId],
    media: _Media | None = None,
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
        label = container.find("label", attrs={"for": control_id}) if isinstance(control_id, str) else None
        label = label if label is not None else control.find_parent("tr")
        if label is None:
            raise PreviewPageError()
        choices.append({"choice_id": cid, "text": _text(label, media=media)})
        if control.has_attr("checked"):
            selected.append(cid)
    return choices, selected, disabled


def _radio_id(radio: Tag, group: str) -> int:
    if radio.get("name") != group:
        raise PreviewPageError()
    return _id(radio.get("value"))


# An unclosed comment runs to the end of the style, as in a browser.
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)", re.DOTALL)


_IMPORTANT = re.compile(r"!\s*important$")
_CSS_WIDE = {"inherit", "initial", "unset", "revert", "revert-layer"}
_DISPLAY_SINGLE = {"none", "contents", "inline-block", "inline-table", "inline-flex", "inline-grid", "table-row-group",
                   "table-header-group", "table-footer-group", "table-row", "table-cell", "table-column-group",
                   "table-column", "table-caption", "ruby-base", "ruby-text", "ruby-base-container",
                   "ruby-text-container", "-webkit-box", "-webkit-inline-box", *_CSS_WIDE}
# Words of the multi-keyword syntax, such as "inline flow-root".
_DISPLAY_WORDS = {"block", "inline", "run-in", "flow", "flow-root", "table", "flex", "grid", "ruby", "list-item", "math"}
_VISIBILITY = {"visible", "hidden", "collapse", *_CSS_WIDE}


def _valid(prop: str, value: str) -> bool:
    if prop == "display":
        words = value.split()
        return value in _DISPLAY_SINGLE or (0 < len(set(words)) == len(words) <= 3 and set(words) <= _DISPLAY_WORDS)
    return prop != "visibility" or value in _VISIBILITY


def _style(tag: Tag) -> dict[str, str]:
    """A tag's valid inline declarations, by lower-case property, resolved as CSS does.

    A later declaration wins unless an earlier one is ``!important``, and a
    value the property does not accept is ignored.
    """
    declarations: dict[str, tuple[str, bool]] = {}
    for declaration in _CSS_COMMENT.sub("", str(tag.get("style", ""))).split(";"):
        prop, _, value = declaration.partition(":")
        prop, value = prop.strip().lower(), " ".join(value.lower().split())
        important = _IMPORTANT.search(value) is not None
        value = _IMPORTANT.sub("", value).strip()
        if _valid(prop, value) and (important or not declarations.get(prop, ("", False))[1]):
            declarations[prop] = (value, important)
    return {prop: value for prop, (value, _) in declarations.items()}


def _hidden(node: Tag) -> bool:
    """Not rendered: ``display:none`` on it or an ancestor, or ``visibility:hidden`` it inherits.

    An element can make itself visible again inside a ``visibility:hidden``
    ancestor, so the nearest declared visibility decides. Visibility is
    inherited, so ``inherit``, ``unset`` and ``revert`` leave it to the
    ancestors, and ``initial`` is ``visible``.
    """
    visibility = None
    for tag in (node, *node.parents):
        style = _style(tag)
        if tag.has_attr("hidden") or "d2l-hidden" in tag.get_attribute_list("class") or style.get("display") == "none":
            return True
        declared = style.get("visibility")
        if visibility is None and declared not in {None, "inherit", "unset", "revert", "revert-layer"}:
            visibility = declared
    return visibility in {"hidden", "collapse"}


def active_buttons(root: Tag, labels: Collection[str]) -> list[str]:
    """The labels of enabled, rendered buttons that have one of ``labels``, in page order."""
    return [text for button in root.find_all("button")
            if (text := button.get_text(" ", strip=True)) in labels
            and not _disabled(button) and not _hidden(button) and button.find_parent("template") is None]


def _button_present(form: Tag, label: str, *, visible_only: bool = False) -> bool:
    # A button inside a question is its content, not page navigation, and
    # template content is never rendered.
    return any(
        button.get_text(" ", strip=True) == label
        and not _disabled(button)
        and button.find_parent(class_="d2l-quiz-question-autosave-container") is None
        and button.find_parent("template") is None
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
class LearnerPage(_AttemptPage):
    """A learner's attempt page. Choice, option and blank ids are strings."""

    _mode: ClassVar[str] = "learner"
    # The page's own form protection, when it embeds one.
    _protection: FormProtection | None = field(default=None, repr=False, compare=False)

    def intended(self, answers: Mapping[int, object]) -> dict[int, tuple[str, ...]]:
        """Every question's value once ``answers`` are applied; others keep theirs.

        A value is the selected choice or option ids in page order, or the
        text of each blank. The browser posts every control of the page, so
        a page with an unsupported question is never sent: its answer could
        be cleared.
        """
        if not all(q["supported"] for q in self.questions):
            raise PreviewRefusedError(REFUSE_LEARNER_UNSUPPORTED)
        questions = {q["question_id"]: q for q in self.questions}
        values = {qid: _learner_value(q) for qid, q in questions.items()}
        for qid, answer in answers.items():
            question = questions.get(qid) if type(qid) is int else None
            if question is None:
                raise PreviewRefusedError(REFUSE_LEARNER_NOT_ON_PAGE)
            values[qid] = _answer_value(question, answer)
        return values

    def unanswered(self, values: Mapping[int, tuple[str, ...]] | None = None) -> list[int]:
        """Questions with no answer, or with an empty blank, as read or as in ``values``."""
        current = {q["question_id"]: _learner_value(q) for q in self.questions} if values is None else values
        return [qid for qid, value in current.items() if not value or not all(value)]

    def confirms(self, values: Mapping[int, tuple[str, ...]], answered: Iterable[int]) -> bool:
        """A 200 response is insufficient: every value must read back as sent.

        An answered question must also be marked saved. Brightspace marks a
        question unsaved once its answer is cleared, so an intended empty
        answer is checked by value only.
        """
        questions = {q["question_id"]: q for q in self.questions}
        answered = set(answered)
        if set(questions) != set(values) or not answered <= set(questions):
            return False
        if any(not q["supported"] or _learner_value(q) != values[qid] for qid, q in questions.items()):
            return False
        return all(questions[qid]["saved"] is True for qid in answered if any(values[qid]))

    def save_fields(self, values: Mapping[int, tuple[str, ...]], protection: FormProtection) -> dict[str, str]:
        """The "Save All Responses" form. Returned fields are secret-bearing."""
        return self._form(values, protection, f"1,{self.page}")

    def advance_fields(self, protection: FormProtection, *, allow_unanswered: bool = False) -> dict[str, str]:
        """Forward to the next page, which a visible, enabled Next control proves exists."""
        if not self.has_next_control:
            raise PreviewRefusedError(REFUSE_LEARNER_LAST_PAGE)
        if self.unanswered() and allow_unanswered is not True:
            raise PreviewRefusedError(REFUSE_LEARNER_UNANSWERED)
        return self._form(self.intended({}), protection, f"2,{self.page + 1},{self.page}")

    def retreat_fields(self, protection: FormProtection) -> dict[str, str]:
        """Back to the previous page, which a visible, enabled Previous control after page 1 proves exists.

        Empty answers are allowed: the page can be revisited, and Next and
        submit still check them.
        """
        if not self.has_previous_control or self.page <= 1:
            raise PreviewRefusedError(REFUSE_LEARNER_FIRST_PAGE)
        return self._form(self.intended({}), protection, f"2,{self.page - 1},{self.page}")

    def finish_fields(self, protection: FormProtection) -> dict[str, str]:
        """The save that opens the submission confirmation page."""
        if self.has_next_control:
            raise PreviewRefusedError(REFUSE_LEARNER_NOT_LAST_PAGE)
        return self._form(self.intended({}), protection, f"5,{self.page}")

    def _form(self, values: Mapping[int, tuple[str, ...]], protection: FormProtection, action: str) -> dict[str, str]:
        # Checked again, as values may come from another page object.
        self.intended({})
        if set(values) != set(self._groups) or self._hidden_fields.get("d2l_referrer") != protection.csrf_token:
            raise PreviewPageError()
        fields = dict(self._hidden_fields)
        answers: dict[str, str] = {}
        for q in self.questions:
            group = self._groups[q["question_id"]][0]
            value = values[q["question_id"]]
            if q["kind"] == "single-choice":
                if value:
                    answers[group] = value[0]
            elif q["kind"] == "multi-select":
                # Unchecked options are left out, as the browser does.
                answers.update({f"{group}_{option}": "1" for option in value})
            else:
                answers.update({f"{group}_{b['blank_id']}": text for b, text in zip(q["blanks"], value, strict=True)})
        if set(answers) & set(fields):
            raise PreviewPageError()
        fields.update(answers)
        fields.update(d2l_action="Update", d2l_actionparam=action, d2l_hitCode=protection.next_hit_code())
        return fields


def _learner_value(question: dict[str, Any]) -> tuple[str, ...]:
    if question["kind"] == "fill-blank":
        return tuple(b["value"] for b in question["blanks"])
    return tuple(question["selected_choice_ids"])


def _answer_value(question: dict[str, Any], answer: object) -> tuple[str, ...]:
    """Check one answer against its question; ids must be offered on the page."""
    kind = question["kind"]
    choices = [c["choice_id"] for c in question["choices"]]
    if kind == "single-choice" and isinstance(answer, str):
        if answer not in choices:
            raise PreviewRefusedError(REFUSE_NOT_A_CHOICE)
        return (answer,)
    if not isinstance(answer, (list, tuple)) or not all(isinstance(item, str) for item in answer):
        raise PreviewRefusedError(REFUSE_ANSWER_SHAPE)
    if kind == "multi-select":
        if len(set(answer)) != len(answer):
            raise PreviewRefusedError(REFUSE_ANSWER_SHAPE)
        if not set(answer) <= set(choices):
            raise PreviewRefusedError(REFUSE_NOT_A_CHOICE)
        return tuple(choice for choice in choices if choice in answer)
    if kind != "fill-blank" or len(answer) != len(question["blanks"]):
        raise PreviewRefusedError(REFUSE_ANSWER_SHAPE)
    if any(len(text) > 1000 or not text.isprintable() for text in answer):
        raise PreviewRefusedError(REFUSE_BLANK_TEXT)
    # Brightspace trims a blank's outer spaces (the only whitespace a
    # printable string can hold) and keeps inner ones, so the readback matches.
    return tuple(text.strip(" ") for text in answer)


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
    """Each blank's id, number in the question text and current value, and whether any is unusable.

    A saved value too long to be one of the CLI's answers is not shown and
    makes the question unsupported.
    """
    blanks: list[dict[str, Any]] = []
    seen: set[str] = set()
    unusable = False
    for number, blank in enumerate(controls, 1):
        unusable = unusable or _disabled(blank) or blank.has_attr("readonly")
        blank_id = _suffix_id(blank, group)
        value = blank.get("value", "")
        if blank_id in seen or not isinstance(value, str):
            raise PreviewPageError()
        if len(value) > 10000:
            unusable, value = True, ""
        seen.add(blank_id)
        blanks.append({"blank_id": blank_id, "number": number, "value": value})
    return blanks, unusable


def _learner_kind(controls: list[Tag], group: str) -> str | None:
    """The kind of a question whose controls are all of one known kind and named for its group.

    Other question types may name their controls differently, so a stray
    name makes the question unsupported instead of aborting the page.
    """
    types = {_input_type(control) for control in controls}
    kind = _LEARNER_KINDS.get(types.pop()) if len(types) == 1 else None
    names = [str(control.get("name", "")) for control in controls]
    if kind == "single-choice":
        return kind if all(name == group for name in names) else None
    return kind if kind is not None and all(name.startswith(f"{group}_") for name in names) else None


def _learner_text(container: Tag, kind: str | None, media: _Media) -> str:
    """The question text, read before the choices so images are numbered in reading order."""
    if kind == "fill-blank":
        return _text(container, blanks=True, media=media)
    # A known kind needs its prompt; any other question is reported as
    # unsupported, not allowed to abort the whole page.
    prompt = _prompt(container)
    if prompt is None:
        if kind is not None:
            raise PreviewPageError()
        return ""
    # An image attached to the question is shown above its prompt.
    attached = [_text(image, media=media) for image in container.select(".d2l-quiz-image-container")]
    text = _text(prompt, media=media)
    return " ".join(part for part in [*attached, text] if part) if text else ""


def _media_count(expanded: BeautifulSoup) -> tuple[int, int]:
    """The images and outermost equations a question shows; a noscript fallback is not shown."""
    images = [image for image in expanded.find_all("img") if image.find_parent("noscript") is None]
    equations = [m for m in expanded.find_all("math") if m.find_parent(["math", "noscript"]) is None]
    return len(images), len(equations)


def _learner_question(container: Tag, qid: int, ordinal: int, group: str, saved: bool | None) -> dict[str, Any]:
    """One question of a single known kind; mixed or other controls are unsupported.

    Equations read as LaTeX and images as numbered markers, which ``images``
    lists in reading order: an attached image, the prompt's, then each choice's.
    """
    controls = [control for control in container.find_all("input") if _input_type(control) != "hidden"]
    kind = _learner_kind(controls, group)
    expanded = _expanded(container)
    # Inputs inside custom HTML blocks are content, not form controls.
    # Browsers read <image> as <img>, and a picture can show a source other than its img.
    unsupported = (bool(expanded.select("textarea, select, iframe, audio, video, svg, object, embed, canvas, "
                                        "image, picture"))
                   or bool(expanded.find_all(re.compile(r":math$")))
                   or len([c for c in expanded.find_all("input") if _input_type(c) != "hidden"]) != len(controls))
    media = _Media()
    text = _learner_text(container, kind, media)
    choices: list[dict[str, Any]] = []
    selected: list[str] = []
    blanks: list[dict[str, Any]] = []
    disabled = False
    if kind == "single-choice":
        choices, selected, disabled = _choices(container, controls, group, _learner_radio_id, media)
        if len(selected) > 1:
            raise PreviewPageError()
    elif kind == "multi-select":
        choices, selected, disabled = _choices(container, controls, group, _learner_option_id, media)
    elif kind == "fill-blank":
        blanks, disabled = _blanks(controls, group)
    # Every image and equation must have been read into the text above.
    unsupported = (unsupported or media.unsupported
                   or (len(media.images), media.equations) != _media_count(expanded))
    supported = kind is not None and not (
        unsupported or disabled or not text or any(not choice["text"] for choice in choices))
    return {
        "question_id": qid, "number": ordinal, "text": text,
        "kind": kind if supported else "unsupported", "supported": supported,
        "choices": choices, "selected_choice_ids": selected, "blanks": blanks, "images": media.images,
        "saved": saved,
    }


def parse_learner_page(
    body: bytes, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> LearnerPage:
    """Read a learner's attempt page (empty ``isprv``) with the preview's checks.

    True/false and multiple choice are radios, multi-select is one checkbox
    per option and fill-in-the-blank is one text input per blank. Ids are
    strings, as multiple-choice values and option names carry opaque tokens.
    Hidden buttons, such as the always-present "Save All Responses", are not
    controls. Equations read as LaTeX and images as numbered markers; other
    media and unrecognized controls are reported as unsupported.
    """
    form, hidden = _attempt_form(body, "", course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
    questions: list[dict[str, Any]] = []
    groups: dict[int, tuple[str, int, int, int]] = {}
    for container, qid, ordinal, tid, tvid, saved in _question_containers(form, page):
        groups[qid] = (f"tAtom{tid}_{tvid}", tid, tvid, ordinal)
        questions.append(_learner_question(container, qid, ordinal, groups[qid][0], saved))
    try:
        # A live attempt page carries the session's form protection, which
        # saves a separate homepage request before each write.
        protection: FormProtection | None = form_protection_from_homepage(body)
    except ValueError:
        protection = None
    return LearnerPage(course_id, quiz_id, attempt_id, page, tuple(questions),
                       _button_present(form, "Next Page", visible_only=True),
                       _button_present(form, "Previous Page", visible_only=True), hidden, groups, protection)


_UNANSWERED_LINK = re.compile(
    r"if\(Events!==undefined\)\{Events\.ClickQuestion\.Raise\([0-9]{1,6},([0-9]{1,6}),([0-9]{1,18}),([0-9]{1,18}),"
    r"'q([0-9]{1,18})'\);\}returnfalse;"
)


def unanswered_questions(form: Tag, *, quiz_id: int, attempt_id: int) -> list[dict[str, int]]:
    """The unanswered questions a submission confirmation page links to.

    Each link carries its page and question id in a fixed script call, read
    here as data. The links must agree with the page's own count.
    """
    found: list[dict[str, int]] = []
    for link in form.find_all("a", onclick=True):
        script = re.sub(r"\s+", "", str(link.get("onclick")))
        if "ClickQuestion" not in script:
            continue
        match = _UNANSWERED_LINK.fullmatch(script)
        number = re.fullmatch(r"Question ([0-9]{1,6})", link.get_text(" ", strip=True))
        if match is None or number is None or (int(match[2]), int(match[3])) != (quiz_id, attempt_id):
            raise PreviewPageError()
        found.append({"page": _id(match[1]), "question_id": _id(match[4]), "number": int(number[1])})
    text = " ".join(form.get_text(" ", strip=True).split())
    counts = re.findall(r"You have ([0-9]{1,6}) unanswered questions?\.", text)
    if counts != [str(len(found))] and not (counts in ([], ["0"]) and not found):
        raise PreviewPageError()
    return found


# The start pages that lead to an attempt: the frame set, its hidden process
# frame, and the process page's one callback naming the attempt and its page.
_STARTED_ATTEMPT = re.compile(
    r"^\s*parent\.GoToAttemptQuizAuto\(\s*([0-9]{1,18})\s*,\s*([0-9]{1,6})\s*,\s*0\s*\)\s*;?\s*$", re.MULTILINE
)


def start_frame_src(body: bytes) -> str:
    """The ``src`` of the start page's one ``quiz_start_iframe_2_auto.d2l`` iframe."""
    sources = [src for frame in BeautifulSoup(body, "html.parser").find_all("iframe")
               if isinstance(src := frame.get("src"), str) and urlparse(src).path.endswith("/quiz_start_iframe_2_auto.d2l")]
    if len(sources) != 1:
        raise PreviewPageError()
    return sources[0]


def process_frame_src(body: bytes) -> str:
    """The ``src`` of the start frame's one hidden process frame."""
    frames = BeautifulSoup(body, "html.parser").select('iframe[name="hiddenFrame"], frame[name="hiddenFrame"]')
    src = frames[0].get("src") if len(frames) == 1 else None
    if not isinstance(src, str):
        raise PreviewPageError()
    return src


def started_attempt(body: bytes) -> tuple[int, int]:
    """The attempt id and page that the process page's scripts name, exactly once."""
    matches = {(int(match[1]), int(match[2])) for script in BeautifulSoup(body, "html.parser").find_all("script")
               for match in _STARTED_ATTEMPT.finditer(script.get_text())}
    if len(matches) != 1:
        raise PreviewPageError()
    return matches.pop()
