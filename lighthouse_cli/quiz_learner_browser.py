"""What a browser's learner attempt pages send and expect, built and checked as data.

Brightspace's attempt pages run in a frame set: the attempt frame holds the
timer header and the page, and page script posts each form to a hidden
save frame, whose reply then tells the page what to do. These are the
URLs, the page-change plan and the save replies' checks for those
requests, matched to a recorded browser session. Nothing here sends a
request or runs a script.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from bs4 import BeautifulSoup

from .quiz_attempt_page import (
    REFUSE_LEARNER_FIRST_PAGE,
    REFUSE_LEARNER_FORM,
    REFUSE_LEARNER_LAST_PAGE,
    LearnerPage,
    PreviewPageError,
    PreviewRefusedError,
    form_state,
    valid_scopes,
    valid_tab_id,
)

ATTEMPT_ROUTE = "/d2l/lms/quizzing/user/attempt/"
SAVE_PATH = ATTEMPT_ROUTE + "quiz_attempt_save_auto.d2l"
# Browsers load a page change as a GET only below this length.
_MAX_GET_URL = 2000
# The characters the page script's URL encoder changes, as encodeURIComponent
# does, except that it leaves every comma raw.
_ENCODED = {"{": "%7B", "}": "%7D", '"': "%22", ":": "%3A", "[": "%5B", "]": "%5D", "+": "%2B", "=": "%3D", "/": "%2F"}


def identity(*values: int) -> None:
    if any(type(value) is not int or not 0 < value < 10**18 for value in values):
        raise ValueError("Invalid quiz attempt identity.")


def d2l_encode(value: str) -> str:
    """A query or form value as the page script encodes it.

    Only the CLI's own JSON, ids and the form protection token are encoded,
    so any character outside them is refused before anything is sent.
    """
    encoded = []
    for char in value:
        if char.isascii() and (char.isalnum() or char in ",._~-"):
            encoded.append(char)
        elif char in _ENCODED:
            encoded.append(_ENCODED[char])
        else:
            raise PreviewRefusedError(REFUSE_LEARNER_FORM)
    return "".join(encoded)


def _query(params: list[tuple[str, object]]) -> str:
    return "&".join(f"{key}={d2l_encode(str(value))}" for key, value in params)


def summary_path(course_id: int, quiz_id: int) -> str:
    identity(course_id, quiz_id)
    return "/d2l/lms/quizzing/user/quiz_summary.d2l?" + _query([("qi", quiz_id), ("ou", course_id)])


def _page_params(course_id: int, quiz_id: int, attempt_id: int, page: int, show: str) -> list[tuple[str, object]]:
    identity(course_id, quiz_id, attempt_id, page)
    return [("ou", course_id), ("isprv", ""), ("impcf", ""), ("pg", page), ("qi", quiz_id), ("ai", attempt_id),
            ("dnb", 0), ("cfql", 0), ("fromQB", 0), ("showIncorrectOnly", show), ("cft", ""), ("d2l_body_type", 3)]


def learner_page_path(course_id: int, quiz_id: int, attempt_id: int, page: int) -> str:
    """The page as the attempt frame first loads it (the frame form)."""
    return ATTEMPT_ROUTE + "quiz_attempt_page_auto.d2l?" + _query(_page_params(course_id, quiz_id, attempt_id, page, "1"))


def attempt_frame_path(course_id: int, quiz_id: int, attempt_id: int, frame_page: int, *, rpc: bool = False) -> str:
    """The attempt frame opened on ``frame_page``, or its submission RPC address."""
    identity(course_id, quiz_id, attempt_id, frame_page)
    params: list[tuple[str, object]] = [
        ("qi", quiz_id), ("ai", attempt_id), ("isprv", ""), ("dnb", 0), ("drc", ""), ("pg", frame_page), ("cfql", 0),
        ("rldbsv", 1), ("fromQB", 0), ("cft", ""), ("d2l_body_type", 1), ("ou", course_id)]
    if rpc:
        return ATTEMPT_ROUTE + "quiz_attempt_iframe_auto.d2lfile?" + _query([*params, ("d2l_rh", "rpc"), ("d2l_rt", "call")])
    return ATTEMPT_ROUTE + "quiz_attempt_iframe_auto.d2l?" + _query(params)


def timer_path(course_id: int, quiz_id: int, attempt_id: int) -> str:
    """The attempt frame's header, which holds the timer."""
    identity(course_id, quiz_id, attempt_id)
    return ATTEMPT_ROUTE + "quiz_attempt_top_auto.d2l?" + _query([
        ("ou", course_id), ("isprv", ""), ("impcf", ""), ("qi", quiz_id), ("ai", attempt_id), ("dnb", 0), ("cfql", 0),
        ("fromQB", 0), ("cft", ""), ("d2l_body_type", 3)])


def _change(previous: str | None) -> str:
    """The ``d2l_change`` the page script sets on a page change: the current URL's, flipped (none counts as 1)."""
    return "1" if previous == "0" else "0"


@dataclass(frozen=True)
class PageState:
    """The URL page script loaded the cursor's page at, and the URL it was loaded from.

    ``from_change`` is ``None`` when that was the frame form.
    """

    page: int
    scopes: str
    page_id: int
    from_page: int
    from_change: str | None

    def __post_init__(self) -> None:
        identity(self.page, self.from_page)
        if (not valid_scopes(self.scopes) or type(self.page_id) is not int or not 0 <= self.page_id < 10**9
                or abs(self.page - self.from_page) != 1 or self.from_change not in (None, "0", "1")):
            raise ValueError("Invalid quiz page state.")

    @property
    def change(self) -> str:
        return _change(self.from_change)


@dataclass(frozen=True)
class AttemptView:
    """The attempt as the browser's attempt frame holds it: its tab, the frame's page and the page shown."""

    course_id: int
    quiz_id: int
    attempt_id: int
    frame_page: int
    tab_id: str
    show_incorrect_only: str
    state: PageState | None = None

    def __post_init__(self) -> None:
        identity(self.course_id, self.quiz_id, self.attempt_id, self.frame_page)
        if (not valid_tab_id(self.tab_id) or self.show_incorrect_only not in ("true", "false")
                or not isinstance(self.state, (PageState, type(None)))
                or (self.state is not None and self.state.from_change is None and self.state.from_page != self.frame_page)):
            raise ValueError("Invalid quiz attempt view.")

    @property
    def page(self) -> int:
        return self.frame_page if self.state is None else self.state.page

    def frame_path(self) -> str:
        return attempt_frame_path(self.course_id, self.quiz_id, self.attempt_id, self.frame_page)

    def _goself(self, page: int, change: str) -> str:
        assert self.state is not None
        params = _page_params(self.course_id, self.quiz_id, self.attempt_id, page, self.show_incorrect_only)
        return ATTEMPT_ROUTE + "quiz_attempt_page_auto.d2l?" + _query([
            *params, ("d2l_stateScopes", self.state.scopes), ("d2l_stateGroups", ""),
            ("d2l_statePageId", self.state.page_id), ("d2l_change", change)])

    def page_path(self) -> str:
        """The URL the shown page was loaded at."""
        if self.state is None:
            return learner_page_path(self.course_id, self.quiz_id, self.attempt_id, self.frame_page)
        return self._goself(self.state.page, self.state.change)

    def referer(self) -> str:
        """The URL the shown page was loaded from, which a reload of it sends too."""
        if self.state is None:
            return self.frame_path()
        if self.state.from_change is None:
            return learner_page_path(self.course_id, self.quiz_id, self.attempt_id, self.state.from_page)
        # Every attempt page has the same scopes and page id.
        return self._goself(self.state.from_page, self.state.from_change)


@dataclass(frozen=True)
class PageChange:
    """A planned Next or Previous: where its form posts, and the view of the page it loads."""

    post_path: str
    view: AttemptView


_NEXT_HANDLER = re.compile(r"GoNextPage\((true|false), ([0-9]{1,6}),([0-9]{1,6}),(0|[1-9][0-9]{0,8})\);;return false;")
_PREVIOUS_HANDLER = re.compile(r"GoPreviousPage\(([0-9]{1,6}), ?([0-9]{1,6}), ?(0|[1-9][0-9]{0,8})\);;return false;")
_PREVIOUS_INLINE = ("var n=new D2L.NavInfo();n.action='Update';n.actionParam='2,{},{}';n.target='saveFrame';"
                    r"n.navigation='\/d2l\/lms\/quizzing\/user\/attempt\/quiz_attempt_save_auto.d2l';"
                    "Nav.Go(n, false, false);return false;")


def _handler(page: LearnerPage, label: str) -> str:
    """The one script every page-level button with this label runs."""
    handlers = set(page.handlers(label))
    if len(handlers) != 1:
        raise PreviewRefusedError(REFUSE_LEARNER_FORM)
    return handlers.pop()


def _next_query(page: LearnerPage, forward_only: bool | None) -> str:
    """What the Next button's ``GoNextPage`` call adds to the save address."""
    match = _NEXT_HANDLER.fullmatch(_handler(page, "Next Page"))
    if match is None or (int(match[2]), int(match[3])) != (page.page, page.page + 1):
        raise PreviewRefusedError(REFUSE_LEARNER_FORM)
    is_forward_only = match[1] == "true"
    if forward_only is not None and is_forward_only != forward_only:
        raise PreviewRefusedError(REFUSE_LEARNER_FORM)
    race_flag = page.race_flag()
    if is_forward_only and not race_flag:
        return "cfql=0&fromQB=undefined"
    return f"cfql=0&fromQB={match[4]}" + ("&isFONav=1" if is_forward_only else "")


def _previous_query(page: LearnerPage) -> str:
    """What the Previous button's handler adds to the save address: ``GoPreviousPage``'s, or its inline form's."""
    handler = _handler(page, "Previous Page")
    match = _PREVIOUS_HANDLER.fullmatch(handler)
    if match is not None and (int(match[1]), int(match[2])) == (page.page - 1, page.page):
        return f"cfql=0&fromQB={match[3]}&"
    if handler == _PREVIOUS_INLINE.format(page.page - 1, page.page):
        return ""
    raise PreviewRefusedError(REFUSE_LEARNER_FORM)


def plan_page_change(page: LearnerPage, view: AttemptView, *, forward: bool, forward_only: bool | None,
                     origin: str) -> PageChange:
    """Check one Next or Previous from ``page`` as the browser would make it, before anything is sent.

    ``page`` is the page as loaded, which is what the page script reads. Its
    button handler gives the save address; after the save, page script
    loads the next page at a URL built from the shown one. Anything else is
    refused, as is a URL the browser would post instead.
    """
    if (page.course_id, page.quiz_id, page.attempt_id, page.page) != (
            view.course_id, view.quiz_id, view.attempt_id, view.page):
        raise ValueError("Invalid quiz attempt identity.")
    if forward:
        if not page.has_next_control:
            raise PreviewRefusedError(REFUSE_LEARNER_LAST_PAGE)
        query = _next_query(page, forward_only) + "&"
        target = page.page + 1
    else:
        if not page.has_previous_control or page.page <= 1:
            raise PreviewRefusedError(REFUSE_LEARNER_FIRST_PAGE)
        query = _previous_query(page)
        target = page.page - 1
    scopes, page_id = form_state(page._body)
    current = None if view.state is None else view.state.change
    moved = replace(view, state=PageState(target, scopes, page_id, page.page, current))
    if len(origin + moved.page_path()) >= _MAX_GET_URL:
        raise PreviewRefusedError(REFUSE_LEARNER_FORM)
    return PageChange(f"{SAVE_PATH}?{query}d2l_body_type=3&ou={view.course_id}", moved)


def autosave_path(course_id: int) -> str:
    identity(course_id)
    return f"{SAVE_PATH}?d2l_body_type=3&ou={course_id}&fromQB=0"


def finish_path(course_id: int) -> str:
    identity(course_id)
    return f"{SAVE_PATH}?dnb=0&cfql=0&fromQB=0&d2l_body_type=3&ou={course_id}"


def rpc_body(quiz_id: int, attempt_id: int, *, rldb: bool, token: str, hit_code: str) -> str:
    """The submission RPC's form body, encoded and ordered as the page script sends it. Secret-bearing.

    The parameters are isPreview, canBeGraded, isRldbUse, shouldAutoSubmit
    and cameFromTab, as observed.
    """
    identity(quiz_id, attempt_id)
    params = (f'{{"param1":"{quiz_id}","param2":"{attempt_id}","param3":false,"param4":true,'
              f'"param5":{"true" if rldb else "false"},"param6":false,"param7":""}}')
    return _query([("d2l_rf", "ProcessQuizSubmission"), ("params", params), ("d2l_referrer", token),
                   ("d2l_hitcode", hit_code), ("d2l_action", "rpc")])


def _scripts(body: bytes) -> list[str]:
    return [script.get_text() for script in BeautifulSoup(body, "html.parser").find_all("script")]


_ONLOAD = re.compile(r"function d2l_Onload\(\) \{\s*D2L\.WebComponentsReady\.then\(function\(\) \{\s*"
                     r"UI\.OnPageLoad\(\)\.Trigger\(\);([^{}]*)\}\);\s*\}")
_IDS = r"((?:[1-9][0-9]{0,17}(?:,[1-9][0-9]{0,17})*)?)"
_INFO_SAVED = re.compile(rf"parent\.infoFrame\.UpdateSaved\('{_IDS}','{_IDS}'\)")
_PAGE_SAVED = re.compile(rf"parent\.pageFrame\.UpdateSaved\('{_IDS}','{_IDS}', '','true'\)")


def onload_calls(body: bytes) -> list[str]:
    """The calls a save reply's ``d2l_Onload`` makes once its page has loaded, in order, as text."""
    found = [match[1] for script in _scripts(body) for match in _ONLOAD.finditer(script)]
    if len(found) != 1:
        raise PreviewPageError()
    *calls, rest = [call.strip() for call in found[0].split(";")]
    if rest:
        raise PreviewPageError()
    return calls


def _ids(text: str) -> tuple[int, ...]:
    ids = tuple(int(value) for value in text.split(",")) if text else ()
    if len(set(ids)) != len(ids):
        raise PreviewPageError()
    return ids


def autosave_marker(body: bytes) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """The question ids an autosave reply marks saved and unsaved, which both frames must be told alike."""
    calls = onload_calls(body)
    info = _INFO_SAVED.fullmatch(calls[0]) if len(calls) == 2 else None
    page = _PAGE_SAVED.fullmatch(calls[1]) if len(calls) == 2 else None
    if info is None or page is None or info.groups() != page.groups():
        raise PreviewPageError()
    return _ids(info[1]), _ids(info[2])


def check_reload_marker(body: bytes, target: int) -> None:
    """A page change's reply must have both frames load ``target``."""
    if onload_calls(body) != [f"parent.infoFrame.DoReload({target})", f"parent.pageFrame.DoReload({target},'','true')"]:
        raise PreviewPageError()


def check_finish_marker(body: bytes, page: LearnerPage) -> None:
    """The finish save's reply must mark only this page's questions saved, then open the confirmation."""
    calls = onload_calls(body)
    saved = re.fullmatch(rf"parent\.infoFrame\.UpdateSaved\('{_IDS}',''\)", calls[0]) if len(calls) == 2 else None
    if (saved is None or not set(_ids(saved[1])) <= {q["question_id"] for q in page.questions}
            or calls[1] != f"DoConfirmSubmitAuto({page.quiz_id},{page.attempt_id},{page.page},'',0)"):
        raise PreviewPageError()


_SET_TAB_ID = re.compile(r"sessionStorage\.setItem\( 'D2L_BROWSER_TAB_ID', '([0-9]{1,20})' \)")


def tab_id_of(body: bytes) -> str | None:
    """The browser tab id the start's process page sets, or ``None`` when it sets none."""
    scripts = _scripts(body)
    count = sum(script.count("D2L_BROWSER_TAB_ID") for script in scripts)
    found = [match[1] for script in scripts for match in _SET_TAB_ID.finditer(script)]
    if count == 0:
        return None
    if count != 1 or len(found) != 1:
        raise PreviewPageError()
    return found[0]


def show_incorrect_only(body: bytes) -> str:
    """The header's ``showIncorrectOnly``, which page script puts in each page change's URL."""
    found = [match[1] for script in _scripts(body)
             for match in re.finditer(r"QuizAttemptViewState\.showIncorrectOnly = (true|false);", script)]
    if len(found) != 1:
        raise PreviewPageError()
    return found[0]


def check_attempt_frame(body: bytes, *, course_id: int, quiz_id: int, attempt_id: int, page: int) -> None:
    """The attempt frame must load this attempt's header and its page at the frame form."""
    soup = BeautifulSoup(body, "html.parser")
    expected = {"pageFrame": learner_page_path(course_id, quiz_id, attempt_id, page),
                "headerFrame": timer_path(course_id, quiz_id, attempt_id)}
    for name, src in expected.items():
        frames = soup.find_all(["iframe", "frame"], attrs={"name": name})
        if len(frames) != 1 or frames[0].get("src") != src:
            raise PreviewPageError()
