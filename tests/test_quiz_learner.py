"""A learner's own attempt: start, save each answer as the browser does, move on, submit and verify.

Fixtures follow pages observed on a public Brightspace site, a disposable
Brightspace sandbox and a recorded browser session, with synthetic tokens only.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from email.utils import formatdate
from html import escape
from unittest.mock import Mock, patch

import pytest

from lighthouse_cli.api import LighthouseClient, NetworkError, SessionExpiredError
from lighthouse_cli.quiz_attempt_page import (
    MAX_PAGE_BYTES,
    REFUSE_ANSWER_SHAPE,
    REFUSE_BLANK_TEXT,
    REFUSE_LEARNER_FIRST_PAGE,
    REFUSE_LEARNER_FORM,
    REFUSE_LEARNER_LAST_PAGE,
    REFUSE_LEARNER_NOT_LAST_PAGE,
    REFUSE_LEARNER_NOT_ON_PAGE,
    REFUSE_LEARNER_UNANSWERED,
    REFUSE_LEARNER_UNSUPPORTED,
    REFUSE_NO_ANSWERS,
    REFUSE_NOT_A_CHOICE,
    PreviewPageError,
    PreviewRefusedError,
    browser_form,
    form_state,
    hidden_form,
    parse_learner_page,
    rpc_script,
    unanswered_questions,
    valid_tab_id,
)
from lighthouse_cli.quiz_learner_browser import (
    ATTEMPT_ROUTE,
    SAVE_PATH,
    AttemptView,
    PageChange,
    PageState,
    attempt_frame_path,
    autosave_marker,
    autosave_path,
    check_attempt_frame,
    check_finish_marker,
    check_reload_marker,
    d2l_encode,
    finish_path,
    identity,
    learner_page_path,
    onload_calls,
    plan_page_change,
    rpc_body,
    show_incorrect_only,
    summary_path,
    tab_id_of,
    timer_path,
    valid_scopes,
)
from lighthouse_cli.quiz_learner_finish import (
    LearnerNotSubmittedError,
    LearnerSubmitUnknownError,
    LearnerUnansweredError,
    submit_learner,
    verify_learner_submission,
)
from lighthouse_cli.quiz_learner_transport import (
    REFUSE_NOTHING_IN_PROGRESS,
    REFUSE_PAGE_PROTECTION,
    REFUSE_START_BROWSER,
    REFUSE_START_PASSWORD,
    REFUSE_START_PROCESSING,
    REFUSE_START_PROTECTION,
    REFUSE_START_ROLE,
    REFUSE_START_UNAVAILABLE,
    LearnerAdvanceUnknownError,
    LearnerNotOpenError,
    LearnerSaveUnknownError,
    LearnerStartUnknownError,
    LearnerTimer,
    advance_learner,
    parse_learner_summary,
    parse_learner_timer,
    read_learner_summary,
    read_learner_timer,
    retreat_learner,
    save_learner_answers,
    start_learner,
)
from lighthouse_cli.request_protection import form_protection_from_homepage
from tests.test_quiz_attempt_page import (
    LEARNER_BUTTONS,
    blank,
    bootstrap,
    checkboxes,
    learner_question,
    radios,
    segment,
)

# Page-level buttons carry the ids the control map gives their scripts.
NEXT = '<button type="button" class="d2l-button" id="z_x">Next Page</button>'
PREVIOUS = '<button type="button" class="d2l-button" id="z_y" style="float:left;">Previous Page</button>'
IDENTITY = {"course_id": 10, "quiz_id": 20, "attempt_id": 30}
TAB = "1234567890123"
BASE = "https://lighthouse.manipal.edu"
# d2l_state's scopes as the page writes them, and as page script posts them.
SCOPES = {"3": ["grid"], "1": ["pagenum"], "2": ["lcs"]}
POSTED_SCOPES = '{"1":["pagenum"],"2":["lcs"],"3":["grid"]}'
ENCODED_SCOPES = "%7B%221%22%3A%5B%22pagenum%22%5D,%222%22%3A%5B%22lcs%22%5D,%223%22%3A%5B%22grid%22%5D%7D"
PAGE1 = learner_page_path(10, 20, 30, 1)
FRAME1 = attempt_frame_path(10, 20, 30, 1)
VIEW = AttemptView(10, 20, 30, 1, TAB, "true")
VIEW2 = replace(VIEW, state=PageState(2, POSTED_SCOPES, 4242, 1, None))
VIEW3 = replace(VIEW, state=PageState(3, POSTED_SCOPES, 4242, 2, "0"))
FORM_HEADERS = {"Origin": BASE, "Upgrade-Insecure-Requests": "1", "Cache-Control": "max-age=0"}


def goself(page: int, change: str, show: str = "true") -> str:
    """A page as page script loads it after a page change."""
    return (ATTEMPT_ROUTE + f"quiz_attempt_page_auto.d2l?ou=10&isprv=&impcf=&pg={page}&qi=20&ai=30&dnb=0&cfql=0"
            f"&fromQB=0&showIncorrectOnly={show}&cft=&d2l_body_type=3&d2l_stateScopes={ENCODED_SCOPES}"
            f"&d2l_stateGroups=&d2l_statePageId=4242&d2l_change={change}")


def document(referer: str | None = None, *, reload: bool = False) -> dict[str, str]:
    """A page load's headers as the browser sends them, for a referer path on the site."""
    headers = {"Upgrade-Insecure-Requests": "1"}
    if reload:
        headers["Cache-Control"] = "max-age=0"
    if referer is not None:
        headers["Referer"] = referer if referer.startswith("https://") else BASE + referer
    return headers


def questions(*, page: int = 1, sc: tuple[str, ...] = (), ms: tuple[str, ...] = (),
              fb: tuple[str, str] = ("", ""), saved: tuple[str, str, str] = ("False", "False", "False")) -> str:
    """Single-choice 101, multi-select 102 and a two-blank question 103."""
    options = segment("Two plus two is") + blank(3, "601", fb[0]) + segment("and three plus three is") + blank(3, "602", fb[1])
    return (learner_question(1, radios(1, ["o1", "o2"], checked=sc), saved=saved[0], page=page)
            + learner_question(2, checkboxes(2, ["o11", "o12", "o13"], checked=ms), saved=saved[1], page=page)
            + learner_question(3, options, prompt="", saved=saved[2], page=page))


def saved_flags(content: str) -> dict[int, str]:
    """Each question's saved flag as the page's hidden fields hold it: 1 once Brightspace has its answer."""
    ids = re.findall(r'object-id"><input type="hidden" value="(\d+)"', content)
    saved = re.findall(r'autosave-is-saved"><input type="hidden" value="(\w+)"', content)
    return {int(qid): "1" if value == "True" else "0" for qid, value in zip(ids, saved, strict=True)}


def hidden_inputs(pairs: list[tuple[str, str]]) -> str:
    return "".join(f'<input type="hidden" name="{key}" value="{escape(value, quote=True)}">' for key, value in pairs)


def body(content: str, *, page: int = 1, extra: str = LEARNER_BUTTONS, protected: bool = True,
         flags: dict[int, str] | None = None, count: str | None = None, race: str | None = "False",
         fetch: str | None = "false", state: str | None = None, page_id: int | None = 4242,
         next_handler: str | None = None, previous_handler: str | None = None,
         controls: dict[str, list[object]] | None = None, control_map: str | None = None, after: str = "") -> bytes:
    """A learner's attempt page as the browser loads it: one form, its control map and the scripts page script reads.

    The attempt's saved flags cover question 104 on another page too.
    """
    marks = dict.fromkeys(range(101, 105), "0") | saved_flags(content) | (flags or {})
    cm0: dict[str, object] = {"hd_savedquestions": ["z_c", "Hidden", [], {}, 0, 1]}
    cm0.update({f"hdn_resp_{qid}": [f"z_r{qid}", "Hidden", [], {}, 0, 1] for qid in marks})
    if 'id="z_y"' in extra:
        script = previous_handler or f"GoPreviousPage({page - 1}, {page}, 0);;return false;"
        cm0["btn_previous"] = ["z_y", "Button", [script], {}, 0, 0]
    cm0.update(controls or {})
    cm1: dict[str, object] = {"lblatom": {"ctl_2": ["z_l", "RichText", [], {}, 0, 0]}}
    if 'id="z_x"' in extra:
        script = next_handler or f"GoNextPage(false, {page},{page + 1},0);;return false;"
        cm1["btn_next"] = {"ctl_1": ["z_x", "Button", [script], {}, 0, 0]}
    head = [("d2l_action", ""), ("d2l_actionparam", ""), ("d2l_hitCode", ""), ("d2l_rf", ""), ("d2l_controlMapPrev", ""),
            ("ou", "10"), ("qi", "20"), ("ai", "30"), ("pg", str(page)), ("isprv", ""), ("drc", ""), ("impcf", ""),
            ("cft", ""), ("isFinalAutoSave", "false"), ("useNewFinalAutoSave", "true"),
            ("z_c", str(sum(value == "1" for value in marks.values())) if count is None else count), ("z_d", "4"),
            ("timeLimitFromQuiz", "30")]
    tail = [*((f"z_r{qid}", value) for qid, value in marks.items()), ("QuizTabId", "")]
    end = [("d2l_controlMap", json.dumps([cm0, cm1]) if control_map is None else control_map),
           ("d2l_state", json.dumps([SCOPES, []]) if state is None else state), ("d2l_referrer", "SESSION_SENTINEL")]
    scripts = ""
    if page_id is not None:
        scripts += f'<script>D2L.Serialization.JsonDeserializerMin.Deserialize(["abababababababababababababababab",{page_id},1,[]])</script>'
    if fetch is not None:
        scripts += f"<script>var quizAttemptSaveFetchEnabled = {fetch};</script>"
    if race is not None:
        scripts += ("<script>function GoNextPage(isFO, currPg, newPg, isPreviewFromQB) {\r\n"
                    f"\tconst isForwardOnlyDialogRaceConditionFlagEnabled = '{race}' === 'True';\r\n}}</script>")
    page_html = f"<form>{hidden_inputs(head)}{content}{hidden_inputs(tail)}{extra}{hidden_inputs(end)}</form>{scripts}{after}"
    # A live attempt page embeds the session's form protection.
    return page_html.encode() + (bootstrap() if protected else b"")


def parse(content: bytes, page: int = 1):
    return parse_learner_page(content, **IDENTITY, page=page)


ANSWERED = questions(sc=("o2",), ms=("o11", "o13"), fb=("four", "six"), saved=("True", "True", "True"))
SECOND = questions(page=2, sc=("o1",))


def checked(fields: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``fields`` with the time-based hit code checked and replaced."""
    result = []
    for key, value in fields:
        if key == "d2l_hitCode":
            assert re.fullmatch(r"1234567890[0-9]+", value)
            value = "HIT"
        result.append((key, value))
    return result


def sent(call) -> list[tuple[str, str]]:
    """A form POST's fields in order, as multipart parts without file names."""
    assert all(filename is None for _, (filename, _) in call.kwargs["files"])
    return checked([(key, value) for key, (_, value) in call.kwargs["files"]])


def answer_fields(fields) -> dict[str, str]:
    return {key: value for key, value in dict(fields).items() if key.startswith("tAtom")}


# -- answers ----------------------------------------------------------------------


def test_answers_are_checked_and_read_as_values():
    page = parse(body(questions()))
    assert page.intended({101: "o2", 102: ["o13", "o11"], 103: ["four", "six"]}) == {
        101: ("o2",), 102: ("o11", "o13"), 103: ("four", "six")}
    assert "SESSION_SENTINEL" not in repr(page) and "SESSION_SENTINEL" not in repr(page.snapshot())


def test_unmentioned_questions_keep_their_current_answers():
    page = parse(body(questions(sc=("o1",), ms=("o12",), fb=("four", ""))))
    values = page.intended({103: ["four", "six"]})
    fields = page.snapshot().with_value(103, values[103]).autosave_fields(103, page._protection, TAB)
    # Unchecked options are left out, as the browser does.
    assert answer_fields(fields) == {
        "tAtom201_300": "o1", "tAtom202_300_o12": "1", "tAtom203_300_601": "four", "tAtom203_300_602": "six",
    }


def test_answers_can_be_cleared_except_a_chosen_radio():
    page = parse(body(questions(ms=("o12",), fb=("four", "six"))))
    values = page.intended({102: [], 103: ["", ""]})
    snapshot = page.snapshot().with_value(102, values[102]).with_value(103, values[103])
    assert answer_fields(snapshot.autosave_fields(102, page._protection, TAB)) == {
        "tAtom203_300_601": "", "tAtom203_300_602": ""}
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_ANSWER_SHAPE)):
        page.intended({101: []})


def test_blank_outer_spaces_are_trimmed_as_the_server_stores_them():
    page = parse(body(questions()))
    # Brightspace stores "  spaced  " as "spaced" and keeps inner spaces.
    assert page.intended({103: ["  a  b ", "x<b>&amp;"]})[103] == ("a  b", "x<b>&amp;")


@pytest.mark.parametrize("answers, message", [
    ({999: "o1"}, REFUSE_LEARNER_NOT_ON_PAGE),
    ({"101": "o1"}, REFUSE_LEARNER_NOT_ON_PAGE),
    ({True: "o1"}, REFUSE_LEARNER_NOT_ON_PAGE),
    ({101: "o9"}, REFUSE_NOT_A_CHOICE),
    ({101: ["o1"]}, REFUSE_ANSWER_SHAPE),
    ({101: 1}, REFUSE_ANSWER_SHAPE),
    ({102: "o11"}, REFUSE_ANSWER_SHAPE),
    ({102: ["o11", "o11"]}, REFUSE_ANSWER_SHAPE),
    ({102: ["o11", 12]}, REFUSE_ANSWER_SHAPE),
    ({102: ["o99"]}, REFUSE_NOT_A_CHOICE),
    ({103: "four"}, REFUSE_ANSWER_SHAPE),
    ({103: ["four"]}, REFUSE_ANSWER_SHAPE),
    ({103: ["a\tb", "x"]}, REFUSE_BLANK_TEXT),
    ({103: ["a\nb", "x"]}, REFUSE_BLANK_TEXT),
    ({103: ["a‮b", "x"]}, REFUSE_BLANK_TEXT),
    ({103: ["x" * 1001, "y"]}, REFUSE_BLANK_TEXT),
])
def test_answers_are_checked_against_the_page(answers, message):
    with pytest.raises(PreviewRefusedError, match=re.escape(message)):
        parse(body(questions())).intended(answers)


def test_a_page_with_an_unsupported_question_is_never_sent():
    # The browser posts every control of the page, so the unsupported
    # question's answer could be cleared.
    media = learner_question(4, radios(4, ["o1", "o2"]), prompt='Which? <img src="x.png">')
    page = parse(body(questions() + media))
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_UNSUPPORTED)):
        page.intended({101: "o1"})
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_UNSUPPORTED)):
        page.snapshot()


def test_readback_confirms_every_value_and_the_saved_flag_of_answered_questions():
    values = parse(body(questions())).intended({101: "o2", 102: ["o11", "o13"], 103: ["four", "six"]})
    assert parse(body(ANSWERED)).confirms(values, [101, 102, 103])
    unsaved = parse(body(questions(sc=("o2",), ms=("o11", "o13"), fb=("four", "six"), saved=("True", "False", "True"))))
    assert not unsaved.confirms(values, [101, 102, 103])
    assert unsaved.confirms(values, [101, 103])  # an untouched question keeps its own flag
    changed = parse(body(questions(sc=("o2",), ms=("o11", "o13"), fb=("four", "seven"), saved=("True",) * 3)))
    assert not changed.confirms(values, [103])
    assert not parse(body(ANSWERED)).confirms({101: ("o2",)}, [101])


def test_a_cleared_answer_is_confirmed_by_value_only():
    # Brightspace marks a question unsaved once its answer is cleared.
    page = parse(body(questions(ms=("o12",), saved=("False", "True", "False"))))
    values = page.intended({102: []})
    assert parse(body(questions(saved=("False", "False", "False")))).confirms(values, [102])


# -- the form the browser posts ----------------------------------------------------


PREV = ('{"ID":{"hd_savedquestions":"z_c","hdn_resp_101":"z_r101","hdn_resp_102":"z_r102","hdn_resp_103":"z_r103",'
        '"hdn_resp_104":"z_r104"},"SID":{"lblatom":{"ctl_2":"z_l"}}}')


def test_an_autosave_posts_every_control_of_the_page_in_document_order():
    page_body = body(questions(sc=("o2",), ms=("o13",), fb=("four", "")))
    page = parse(page_body)
    fields = page.snapshot().with_value(102, ("o11", "o13")).autosave_fields(102, page._protection, TAB)
    hidden = hidden_form(page_body)[1]
    assert hidden["d2l_state"] == json.dumps([SCOPES, []])
    assert checked(fields) == [
        ("d2l_action", "Update"), ("d2l_actionparam", "3,1,202,300,2"), ("d2l_hitCode", "HIT"), ("d2l_rf", ""),
        ("d2l_controlMapPrev", PREV), ("ou", "10"), ("qi", "20"), ("ai", "30"), ("pg", "1"), ("isprv", ""), ("drc", ""),
        ("impcf", ""), ("cft", ""), ("isFinalAutoSave", "false"), ("useNewFinalAutoSave", "true"), ("z_c", "0"),
        ("z_d", "4"), ("timeLimitFromQuiz", "30"),
        ("tAtom201_300", "o2"), ("tAtom202_300_o11", "1"), ("tAtom202_300_o13", "1"), ("tAtom203_300_601", "four"),
        ("tAtom203_300_602", ""),
        ("z_r101", "0"), ("z_r102", "0"), ("z_r103", "0"), ("z_r104", "0"), ("QuizTabId", TAB),
        ("d2l_controlMap", hidden["d2l_controlMap"]), ("d2l_state", hidden["d2l_state"]),
        ("d2l_referrer", "SESSION_SENTINEL"),
        ("d2l_multiedit", '{"Controls":[]}'), ("d2l_stateScopes", POSTED_SCOPES), ("d2l_stateGroups", ""),
        ("d2l_statePageId", "4242"),
    ]


def test_the_previous_control_map_lists_controls_in_javascript_key_order():
    # A JavaScript object lists integer-like keys first, ascending; the page
    # script keeps each key as the control map has it.
    extra = '<input type="hidden" name="z_q" value="x"><input type="hidden" name="z_m" value="">'
    page = parse(body(questions(), extra=LEARNER_BUTTONS + extra + NEXT,
                      controls={"7": ["z_q", "Hidden", [], {}, 0, 1], "MyControl": ["z_m", "Hidden", [], {}, 0, 1]}))
    fields = dict(page.snapshot().autosave_fields(101, page._protection, TAB))
    assert json.loads(fields["d2l_controlMapPrev"]) == {
        "ID": {"7": "z_q", "hd_savedquestions": "z_c", "hdn_resp_101": "z_r101", "hdn_resp_102": "z_r102",
               "hdn_resp_103": "z_r103", "hdn_resp_104": "z_r104", "MyControl": "z_m"},
        "SID": {"lblatom": {"ctl_2": "z_l"}, "btn_next": {"ctl_1": "z_x"}}}
    assert fields["d2l_controlMapPrev"].startswith('{"ID":{"7":"z_q","hd_savedquestions"')
    assert '"MyControl":"z_m"' in fields["d2l_controlMapPrev"]
    assert fields["z_q"] == "x"


@pytest.mark.parametrize("page_body", [
    body(questions(), after='<input type="hidden" name="z_e" value="1" form="f">'),  # joins the form from outside
    body(questions(), extra=LEARNER_BUTTONS + '<noscript><input type="hidden" name="z_e" value="1"></noscript>'),
    body(questions(), fetch="true"),  # the fetch transport posts no form
    body(questions(), fetch=None),
    body(questions(), after="<script>var quizAttemptSaveFetchEnabled = false;</script>"),
    body(questions(), state=json.dumps([SCOPES, ["grp"]])),
    body(questions(), state=json.dumps([{"1": ["page-num"]}, []])),
    body(questions(), page_id=None),
    body(questions(), after='<script>D2L.Serialization.JsonDeserializerMin.Deserialize(["abababababababababababababababab",7,1,[]])</script>'),
    body(questions(), extra=LEARNER_BUTTONS + '<input type="hidden" name="tAtom203_300_601" value="x">'),
    body(questions(), extra=LEARNER_BUTTONS + '<input type="hidden" name="d2l_multiedit" value="">'),
    body(questions()).replace(b'<input type="hidden" name="d2l_controlMapPrev" value="">', b""),
    body(questions()).replace(b'<input type="hidden" name="QuizTabId" value="">', b""),
    body(questions(), extra=LEARNER_BUTTONS + '<input type="text" name="search">'),
], ids=["form-attribute", "noscript", "fetch-on", "fetch-unknown", "fetch-twice", "state-groups", "scope-name",
        "no-page-id", "two-page-ids", "duplicate-name", "appended-field", "no-control-map-prev", "no-tab-field",
        "text-outside-a-question"])
def test_a_page_the_cli_cannot_post_as_the_browser_does_is_refused(page_body):
    page = parse(page_body)
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FORM)):
        page.snapshot().autosave_fields(101, page._protection, TAB)


@pytest.mark.parametrize("page_body", [
    body(questions(), flags={104: "2"}),
    body(questions(), count="01"),
    body(questions(), control_map='[{"hd_savedquestions":["z_c"],"hd_savedquestions":["z_c"]},{}]'),
    body(questions(), control_map='[{"hd_savedquestions":["z c"]},{}]'),
    body(questions(), control_map='[{"hd_savedquestions":["z_c"]},{"lblatom":{"ctl_2":[]}}]'),
    body(questions(), control_map='[{"hd_savedquestions":["z_c"]},{},{}]'),
    body(questions(), control_map='[{"hdn_resp_101":["z_r101"],"hdn_resp_102":["z_r102"],"hdn_resp_103":["z_r103"]},{}]'),
])
def test_unreadable_saved_flags_are_refused_before_any_form_is_built(page_body):
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FORM)):
        parse(page_body).snapshot()


def test_the_form_needs_the_page_session_protection_and_a_valid_tab():
    page = parse(body(questions()))
    other = form_protection_from_homepage(bootstrap().replace(b"SESSION_SENTINEL", b"OTHER_SESSION"))
    for protection, tab in ((other, TAB), (page._protection, "12a"), (page._protection, "1" * 21)):
        with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FORM)):
            page.snapshot().autosave_fields(101, protection, tab)
    with pytest.raises(PreviewPageError):  # not every question of the page
        replace(page.snapshot(), values={101: ("o1",)}).autosave_fields(101, page._protection, TAB)


def test_only_the_pages_own_controls_and_answers_are_posted():
    page_body = body(questions())
    protection = parse(page_body)._protection
    blanks = {"tAtom203_300_601": "four", "tAtom203_300_602": "six"}
    form = dict(browser_form(page_body, protection, action=("Update", "x"), tab_id=TAB, answers=blanks))
    assert (form["tAtom203_300_601"], form["d2l_actionparam"]) == ("four", "x")
    for answers in ({"tAtom203_300_601": "four"},  # a blank left out
                    {**blanks, "tAtom203_300_602": 6},
                    {**blanks, "tAtom999_300": "o1"}):  # a control the page lacks
        with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FORM)):
            browser_form(page_body, protection, action=("Update", "x"), tab_id=TAB, answers=answers)


def test_the_page_state_is_read_from_one_form():
    assert form_state(body(questions())) == (POSTED_SCOPES, 4242)
    for page_body in ("text", b"<p>no form</p>", body(questions()) + b"<form></form>", b"x" * (MAX_PAGE_BYTES + 1)):
        with pytest.raises(PreviewPageError):
            form_state(page_body)
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FORM)):
        form_state(body(questions()).replace(b'name="d2l_state"', b'name="d2l_other"'))
    assert valid_scopes(POSTED_SCOPES)
    assert not any(valid_scopes(text) for text in (None, '{"1":["page-num"]}', '{"1": ["pagenum"]}', "{", "x" * 4097))


def test_save_replies_update_the_saved_flags_as_page_script_does():
    page = parse(body(questions(), flags={104: "1"}))
    snapshot = page.snapshot()
    assert snapshot.flags == {"z_c": "1", "z_r101": "0", "z_r102": "0", "z_r103": "0", "z_r104": "1"}
    saved = snapshot.update_saved((101,), ())
    assert saved.flags == {"z_c": "2", "z_r101": "1", "z_r102": "0", "z_r103": "0", "z_r104": "1"}
    assert saved.update_saved((101,), ()).flags == saved.flags  # already saved
    # Another page's question's flag is carried in every form, and cleared when the reply says so.
    assert dict(saved.autosave_fields(102, page._protection, TAB))["z_r104"] == "1"
    cleared = saved.update_saved((), (104,))
    assert cleared.flags == {"z_c": "1", "z_r101": "1", "z_r102": "0", "z_r103": "0", "z_r104": "0"}
    assert dict(cleared.autosave_fields(102, page._protection, TAB))["z_c"] == "1"
    # Unsaved ones are cleared first.
    assert snapshot.update_saved((102,), (102,)).flags["z_r102"] == "1"
    with pytest.raises(PreviewPageError):
        snapshot.update_saved((105,), ())


# -- page rules -------------------------------------------------------------------


def test_next_needs_a_rendered_next_control_and_answered_questions():
    last = parse(body(ANSWERED))
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_LAST_PAGE)):
        last.snapshot().advance_fields(last._protection, TAB)
    answered = parse(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT))
    assert dict(answered.snapshot().advance_fields(answered._protection, TAB))["d2l_actionparam"] == "2,2,1"
    open_page = parse(body(questions(sc=("o1",)), extra=LEARNER_BUTTONS + NEXT))
    assert open_page.unanswered() == [102, 103]
    # On a forward-only quiz an empty blank could not be filled in later.
    partial = parse(body(questions(sc=("o1",), ms=("o11",), fb=("four", "")), extra=LEARNER_BUTTONS + NEXT))
    assert partial.unanswered() == [103]
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_UNANSWERED)):
        open_page.snapshot().advance_fields(open_page._protection, TAB)
    fields = open_page.snapshot().advance_fields(open_page._protection, TAB, allow_unanswered=True)
    assert answer_fields(fields) == {"tAtom201_300": "o1", "tAtom203_300_601": "", "tAtom203_300_602": ""}
    # The answers as the browser holds them count, not as the page loaded.
    filled = open_page.snapshot().with_value(102, ("o12",)).with_value(103, ("a", "b"))
    assert dict(filled.advance_fields(open_page._protection, TAB))["tAtom202_300_o12"] == "1"


def test_previous_needs_a_rendered_previous_control_after_the_first_page():
    for page in (parse(body(ANSWERED)),
                 parse(body(ANSWERED, extra=LEARNER_BUTTONS + PREVIOUS.replace('type="button"', 'type="button" disabled', 1))),
                 parse(body(ANSWERED, extra=LEARNER_BUTTONS + PREVIOUS)),  # never a page below 1
                 parse(body(questions(page=2), page=2), page=2)):  # forward-only
        with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FIRST_PAGE)):
            page.snapshot().retreat_fields(page._protection, TAB)
    # The page stays open to come back to, so its empty answers are allowed.
    second = parse(body(questions(page=2, sc=("o1",), fb=("four", "")), page=2, extra=LEARNER_BUTTONS + PREVIOUS), page=2)
    fields = dict(second.snapshot().retreat_fields(second._protection, TAB))
    assert (fields["d2l_action"], fields["d2l_actionparam"], fields["pg"]) == ("Update", "2,1,2", "2")
    assert answer_fields(fields) == {"tAtom201_300": "o1", "tAtom203_300_601": "four", "tAtom203_300_602": ""}
    # Unanswered, the choice is left out as a browser leaves out unchecked radios.
    empty = parse(body(questions(page=2), page=2, extra=LEARNER_BUTTONS + PREVIOUS), page=2)
    assert answer_fields(empty.snapshot().retreat_fields(empty._protection, TAB)) == {
        "tAtom203_300_601": "", "tAtom203_300_602": ""}


def test_finish_is_only_from_the_last_page():
    page = parse(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT))
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_NOT_LAST_PAGE)):
        page.snapshot().finish_fields(page._protection, TAB)
    last = parse(body(ANSWERED))
    fields = dict(last.snapshot().finish_fields(last._protection, TAB))
    assert (fields["d2l_actionparam"], fields["QuizTabId"]) == ("5,1", TAB)


# -- the browser's URLs and replies --------------------------------------------------


def test_values_are_encoded_as_the_page_script_encodes_them():
    assert d2l_encode('{"a":[1,2]}+=/') == "%7B%22a%22%3A%5B1,2%5D%7D%2B%3D%2F"
    assert d2l_encode("Az09,._~-") == "Az09,._~-"
    for value in (" ", "&", "?", "#", "%", "é", "\n"):
        with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FORM)):
            d2l_encode(value)


def test_attempt_urls_are_the_ones_the_browser_loads():
    assert summary_path(10, 20) == "/d2l/lms/quizzing/user/quiz_summary.d2l?qi=20&ou=10"
    assert PAGE1 == (ATTEMPT_ROUTE + "quiz_attempt_page_auto.d2l?ou=10&isprv=&impcf=&pg=1&qi=20&ai=30&dnb=0&cfql=0"
                     "&fromQB=0&showIncorrectOnly=1&cft=&d2l_body_type=3")
    assert FRAME1 == (ATTEMPT_ROUTE + "quiz_attempt_iframe_auto.d2l?qi=20&ai=30&isprv=&dnb=0&drc=&pg=1&cfql=0&rldbsv=1"
                      "&fromQB=0&cft=&d2l_body_type=1&ou=10")
    assert attempt_frame_path(10, 20, 30, 1, rpc=True) == (
        ATTEMPT_ROUTE + "quiz_attempt_iframe_auto.d2lfile?qi=20&ai=30&isprv=&dnb=0&drc=&pg=1&cfql=0&rldbsv=1"
        "&fromQB=0&cft=&d2l_body_type=1&ou=10&d2l_rh=rpc&d2l_rt=call")
    assert timer_path(10, 20, 30) == (ATTEMPT_ROUTE + "quiz_attempt_top_auto.d2l?ou=10&isprv=&impcf=&qi=20&ai=30"
                                      "&dnb=0&cfql=0&fromQB=0&cft=&d2l_body_type=3")
    assert autosave_path(10) == SAVE_PATH + "?d2l_body_type=3&ou=10&fromQB=0"
    assert finish_path(10) == SAVE_PATH + "?dnb=0&cfql=0&fromQB=0&d2l_body_type=3&ou=10"


@pytest.mark.parametrize("value", [0, -1, 10**18, True, 1.0, "1", None])
def test_attempt_identities_are_positive_integers(value):
    for build in (lambda: identity(value), lambda: learner_page_path(10, 20, 30, value),
                  lambda: attempt_frame_path(10, value, 30, 1), lambda: autosave_path(value)):
        with pytest.raises(ValueError, match="Invalid quiz attempt identity"):
            build()


def test_tab_ids_are_the_process_pages_digits():
    assert all(valid_tab_id(value) for value in ("", TAB, "1" * 20))
    assert not any(valid_tab_id(value) for value in ("1" * 21, "12a", " 1", "١٢", 1, None))


@pytest.mark.parametrize("state", [
    (3, POSTED_SCOPES, 4242, 1, None),
    (2, POSTED_SCOPES, 4242, 1, "2"),
    (2, '{"1": ["pagenum"]}', 4242, 1, None),  # not as the page script posts it
    (2, POSTED_SCOPES, True, 1, None),
    (2, POSTED_SCOPES, 10**9, 1, None),
    (0, POSTED_SCOPES, 4242, 1, None),
])
def test_an_invalid_page_state_is_rejected(state):
    with pytest.raises(ValueError):
        PageState(*state)


@pytest.mark.parametrize("change", [
    {"tab_id": "12a"}, {"show_incorrect_only": "1"}, {"state": "page 2"},
    {"state": PageState(3, POSTED_SCOPES, 4242, 2, None)},  # loaded from a frame form on another page
    {"frame_page": 0},
])
def test_an_invalid_attempt_view_is_rejected(change):
    with pytest.raises(ValueError):
        replace(VIEW, **change)


def test_each_page_loads_from_the_url_of_the_one_before():
    assert (VIEW.page, VIEW.page_path(), VIEW.referer(), VIEW.frame_path()) == (1, PAGE1, FRAME1, FRAME1)
    assert (VIEW2.page, VIEW2.page_path(), VIEW2.referer()) == (2, goself(2, "0"), PAGE1)
    assert (VIEW3.page, VIEW3.page_path(), VIEW3.referer()) == (3, goself(3, "1"), goself(2, "0"))
    assert VIEW3.frame_path() == FRAME1
    hidden_scores = replace(VIEW2, show_incorrect_only="false")
    assert hidden_scores.page_path() == goself(2, "0", show="false")


def plan(content: str = ANSWERED, *, page: int = 1, view: AttemptView = VIEW, forward: bool = True,
         forward_only: bool | None = None, origin: str = BASE, **kwargs) -> PageChange:
    return plan_page_change(parse(body(content, page=page, **kwargs), page=page), view, forward=forward,
                            forward_only=forward_only, origin=origin)


NEXT_POST = SAVE_PATH + "?cfql=0&fromQB=0&d2l_body_type=3&ou=10"


def test_next_is_planned_from_its_button_script():
    assert plan(extra=LEARNER_BUTTONS + NEXT) == PageChange(NEXT_POST, VIEW2)
    assert plan(extra=LEARNER_BUTTONS + NEXT, forward_only=False) == PageChange(NEXT_POST, VIEW2)
    assert plan(SECOND, page=2, view=VIEW2, extra=LEARNER_BUTTONS + NEXT) == PageChange(NEXT_POST, VIEW3)
    preview_flag = plan(extra=LEARNER_BUTTONS + NEXT, next_handler="GoNextPage(false, 1,2,5);;return false;")
    assert preview_flag.post_path == SAVE_PATH + "?cfql=0&fromQB=5&d2l_body_type=3&ou=10"
    # Two Next buttons running the same script are one move.
    twice = LEARNER_BUTTONS + NEXT + '<button type="button" id="z_w">Next Page</button>'
    same = {"btn_next2": ["z_w", "Button", ["GoNextPage(false, 1,2,0);;return false;"], {}, 0, 0]}
    assert plan(extra=twice, controls=same).view.page == 2


@pytest.mark.parametrize("race, query", [
    # Without the server's race-condition flag a forward-only Next sends fromQB=undefined.
    ("False", "cfql=0&fromQB=undefined"),
    ("True", "cfql=0&fromQB=0&isFONav=1"),
])
def test_a_forward_only_next_adds_what_its_script_does(race, query):
    for forward_only in (None, True):
        change = plan(extra=LEARNER_BUTTONS + NEXT, race=race, forward_only=forward_only,
                      next_handler="GoNextPage(true, 1,2,0);;return false;")
        assert change.post_path == f"{SAVE_PATH}?{query}&d2l_body_type=3&ou=10"


@pytest.mark.parametrize("kwargs", [
    {"forward_only": True},  # the quiz setting disagrees with the page
    {"forward_only": False, "next_handler": "GoNextPage(true, 1,2,0);;return false;"},
    {"next_handler": "GoNextPage(false, 1,3,0);;return false;"},
    {"next_handler": "GoNextPage(false, 2,3,0);;return false;"},
    {"next_handler": "GoNextPage(false, 1,2,0);;return false;steal();"},
    {"next_handler": "GoNextPage(false, 1,2,00);;return false;"},
    {"race": None}, {"race": "Maybe"},
    {"after": "<script>function GoNextPage(isFO, currPg, newPg, isPreviewFromQB) {\r\n"
              "\tconst isForwardOnlyDialogRaceConditionFlagEnabled = 'True' === 'True';\r\n}</script>"},
    {"extra": LEARNER_BUTTONS + NEXT + '<button type="button" id="z_w">Next Page</button>',
     "controls": {"btn_next2": ["z_w", "Button", ["GoNextPage(false, 1,2,7);;return false;"], {}, 0, 0]}},
    {"extra": LEARNER_BUTTONS + NEXT + '<button type="button" id="z_v">Next Page</button>'},  # no script
    {"extra": LEARNER_BUTTONS + NEXT + '<button type="button">Next Page</button>'},
    {"controls": {"btn_next2": ["z_x", "Hidden", [], {}, 0, 0]}},  # two entries for one button
    {"page_id": None},
], ids=["fo-setting", "fo-page", "skips-a-page", "other-page", "extra-script", "padded-flag", "no-race-flag",
        "odd-race-flag", "two-race-flags", "two-scripts", "unmapped-button", "unnamed-button", "two-entries",
        "no-page-id"])
def test_a_next_the_cli_cannot_send_as_the_browser_does_is_refused(kwargs):
    kwargs.setdefault("extra", LEARNER_BUTTONS + NEXT)
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FORM)):
        plan(**kwargs)


@pytest.mark.parametrize("handler, query", [
    (None, "cfql=0&fromQB=0&"),
    ("GoPreviousPage(1,2,0);;return false;", "cfql=0&fromQB=0&"),
    # Some quizzes inline the save instead, without cfql and fromQB.
    ("var n=new D2L.NavInfo();n.action='Update';n.actionParam='2,1,2';n.target='saveFrame';"
     r"n.navigation='\/d2l\/lms\/quizzing\/user\/attempt\/quiz_attempt_save_auto.d2l';Nav.Go(n, false, false);return false;", ""),
])
def test_previous_is_planned_from_its_button_script(handler, query):
    change = plan(SECOND, page=2, view=VIEW2, forward=False, extra=LEARNER_BUTTONS + PREVIOUS, previous_handler=handler)
    assert change == PageChange(f"{SAVE_PATH}?{query}d2l_body_type=3&ou=10",
                                replace(VIEW, state=PageState(1, POSTED_SCOPES, 4242, 2, "0")))


@pytest.mark.parametrize("handler", [
    "GoPreviousPage(2, 3, 0);;return false;",
    "GoPreviousPage(1, 2, 0);;return false;steal();",
    "var n=new D2L.NavInfo();n.action='Update';n.actionParam='2,1,2';n.target='saveFrame';"
    "n.navigation='/d2l/lms/quizzing/user/attempt/quiz_attempt_save_auto.d2l';Nav.Go(n, false, false);return false;",
])
def test_a_previous_the_cli_cannot_send_as_the_browser_does_is_refused(handler):
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FORM)):
        plan(SECOND, page=2, view=VIEW2, forward=False, extra=LEARNER_BUTTONS + PREVIOUS, previous_handler=handler)


def test_page_changes_need_their_controls_and_the_views_page():
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_LAST_PAGE)):
        plan()
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FIRST_PAGE)):
        plan(forward=False, extra=LEARNER_BUTTONS + PREVIOUS)
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FIRST_PAGE)):
        plan(SECOND, page=2, view=VIEW2, forward=False)
    with pytest.raises(ValueError, match="Invalid quiz attempt identity"):
        plan(view=VIEW2, extra=LEARNER_BUTTONS + NEXT)


def test_a_page_change_the_browser_would_post_instead_of_load_is_refused():
    room = 2000 - len(goself(2, "0"))
    assert plan(extra=LEARNER_BUTTONS + NEXT, origin="x" * (room - 1)).view.page == 2
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FORM)):
        plan(extra=LEARNER_BUTTONS + NEXT, origin="x" * room)


def onload(*calls: str) -> bytes:
    """A save reply as the hidden save frame loads it: its calls run once the page has loaded."""
    lines = "".join(f"\r\n\t\t{call};" for call in calls)
    return ("<html><head><script>function d2l_Onload() {\r\n\tD2L.WebComponentsReady.then(function() {\r\n"
            "\t\tUI.OnPageLoad().Trigger();" + lines + "\r\n\t});\r\n}</script></head>"
            '<body onload="d2l_Onload();"></body></html>').encode()


def saved_reply(saved: str, unsaved: str = "") -> bytes:
    return onload(f"parent.infoFrame.UpdateSaved('{saved}','{unsaved}')",
                  f"parent.pageFrame.UpdateSaved('{saved}','{unsaved}', '','true')")


def reload_reply(target: int) -> bytes:
    return onload(f"parent.infoFrame.DoReload({target})", f"parent.pageFrame.DoReload({target},'','true')")


def test_save_replies_are_read_as_data():
    assert onload_calls(saved_reply("101")) == [
        "parent.infoFrame.UpdateSaved('101','')", "parent.pageFrame.UpdateSaved('101','', '','true')"]
    assert autosave_marker(saved_reply("101")) == ((101,), ())
    assert autosave_marker(saved_reply("101,102", "103")) == ((101, 102), (103,))
    assert autosave_marker(saved_reply("", "102")) == ((), (102,))
    assert autosave_marker(saved_reply("")) == ((), ())
    check_reload_marker(reload_reply(2), 2)


@pytest.mark.parametrize("reply", [
    onload("parent.infoFrame.UpdateSaved('101','')"),  # one frame told
    onload("parent.infoFrame.UpdateSaved('101','')", "parent.pageFrame.UpdateSaved('102','', '','true')"),
    onload("parent.pageFrame.UpdateSaved('101','', '','true')", "parent.infoFrame.UpdateSaved('101','')"),
    onload("parent.infoFrame.UpdateSaved('101','')", "parent.pageFrame.UpdateSaved('101','', '','true')", "steal()"),
    saved_reply("101,101"), saved_reply("0101"), saved_reply("101,"),
    saved_reply("101") + saved_reply("101"),
    saved_reply("101").replace(b"'true');", b"'true')"),
    b"<script>parent.infoFrame.UpdateSaved('101','');parent.pageFrame.UpdateSaved('101','', '','true');</script>",
    reload_reply(2),
])
def test_unexpected_save_replies_are_rejected(reply):
    with pytest.raises(PreviewPageError):
        autosave_marker(reply)


@pytest.mark.parametrize("reply", [
    reload_reply(3), saved_reply("101"),
    onload("parent.infoFrame.DoReload(2)"),
    onload("parent.pageFrame.DoReload(2,'','true')", "parent.infoFrame.DoReload(2)"),
])
def test_a_page_change_reply_must_load_the_target_page(reply):
    with pytest.raises(PreviewPageError):
        check_reload_marker(reply, 2)


LAST = parse(body(ANSWERED))
FINISHED = onload("parent.infoFrame.UpdateSaved('101,102,103','')", "DoConfirmSubmitAuto(20,30,1,'',0)")


def test_the_finish_reply_must_mark_this_pages_questions_and_open_the_confirmation():
    check_finish_marker(FINISHED, LAST)
    check_finish_marker(onload("parent.infoFrame.UpdateSaved('','')", "DoConfirmSubmitAuto(20,30,1,'',0)"), LAST)
    for reply in (onload("parent.infoFrame.UpdateSaved('101,104','')", "DoConfirmSubmitAuto(20,30,1,'',0)"),
                  onload("parent.infoFrame.UpdateSaved('101','102')", "DoConfirmSubmitAuto(20,30,1,'',0)"),
                  onload("parent.infoFrame.UpdateSaved('101','')", "DoConfirmSubmitAuto(20,31,1,'',0)"),
                  onload("parent.infoFrame.UpdateSaved('101','')", "DoConfirmSubmitAuto(20,30,2,'',0)"),
                  onload("DoConfirmSubmitAuto(20,30,1,'',0)"), reload_reply(1)):
        with pytest.raises(PreviewPageError):
            check_finish_marker(reply, LAST)


def process_page(page: int = 1, *, tab: str = f"\r\n sessionStorage.setItem( 'D2L_BROWSER_TAB_ID', '{TAB}' );") -> bytes:
    """The start's hidden process page: it sets the browser tab's id, then opens the attempt."""
    return (f"<script>try {{{tab}\r\n}} catch( err ) {{}}\r\n parent.GoToAttemptQuizAuto( 30,{page},0 );\r\n</script>").encode()


def test_the_tab_id_is_read_from_the_process_page():
    assert tab_id_of(process_page()) == TAB
    assert tab_id_of(process_page(tab="")) is None
    for page in (process_page(tab=f" sessionStorage.setItem( 'D2L_BROWSER_TAB_ID', '{TAB}' ); "
                                  f"sessionStorage.setItem( 'D2L_BROWSER_TAB_ID', '{TAB}' );"),
                 process_page(tab=" sessionStorage.setItem('D2L_BROWSER_TAB_ID', '123');"),
                 process_page(tab=" sessionStorage.setItem( 'D2L_BROWSER_TAB_ID', 'abc' );"),
                 process_page() + b"<script>// D2L_BROWSER_TAB_ID</script>"):
        with pytest.raises(PreviewPageError):
            tab_id_of(page)


SHOW_INCORRECT = b"<script>var QuizAttemptViewState = {};\r\nQuizAttemptViewState.showIncorrectOnly = true;</script>"


def test_show_incorrect_only_is_read_from_the_header():
    assert show_incorrect_only(SHOW_INCORRECT) == "true"
    assert show_incorrect_only(SHOW_INCORRECT.replace(b"= true", b"= false")) == "false"
    for header in (b"<p>none</p>", SHOW_INCORRECT * 2, SHOW_INCORRECT.replace(b"= true", b"= 1")):
        with pytest.raises(PreviewPageError):
            show_incorrect_only(header)


def attempt_frame(page: int = 1) -> bytes:
    """The attempt frame: the timer header and the page at its frame form."""
    return (f'<iframe name="headerFrame" src="{escape(timer_path(10, 20, 30), quote=True)}"></iframe>'
            f'<iframe name="pageFrame" src="{escape(learner_page_path(10, 20, 30, page), quote=True)}"></iframe>').encode()


def test_the_attempt_frame_must_load_this_attempts_header_and_page():
    check_attempt_frame(attempt_frame(), **IDENTITY, page=1)
    check_attempt_frame(attempt_frame().replace(b"iframe", b"frame"), **IDENTITY, page=1)
    for frame in (attempt_frame(2), attempt_frame().replace(b'name="headerFrame"', b'name="topFrame"'),
                  attempt_frame() + attempt_frame()):
        with pytest.raises(PreviewPageError):
            check_attempt_frame(frame, **IDENTITY, page=1)


def test_the_submission_rpc_body_is_encoded_as_the_page_script_sends_it():
    assert rpc_body(20, 30, rldb=True, token="SESSION_SENTINEL", hit_code="123") == (
        "d2l_rf=ProcessQuizSubmission&params=%7B%22param1%22%3A%2220%22,%22param2%22%3A%2230%22,%22param3%22%3Afalse,"
        "%22param4%22%3Atrue,%22param5%22%3Atrue,%22param6%22%3Afalse,%22param7%22%3A%22%22%7D"
        "&d2l_referrer=SESSION_SENTINEL&d2l_hitcode=123&d2l_action=rpc")
    assert "%22param5%22%3Afalse" in rpc_body(20, 30, rldb=False, token="t", hit_code="1")
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FORM)):
        rpc_body(20, 30, rldb=False, token="a&b", hit_code="1")


# -- unanswered questions on the confirmation page -------------------------------


def confirmation(*, unanswered: tuple[tuple[int, int, int], ...] = (), count: int | None = None, attempt_id: int = 30,
                 referrer: str = "SESSION_SENTINEL", secure_browser: str = "0", extra: str = "") -> bytes:
    links = "".join(
        f'<a href="#" onclick="if( Events !== undefined ) {{ Events.ClickQuestion.Raise(0,{page},20,{attempt_id},\'q{qid}\'); }} return false;">'
        f"Question {number}</a>" for page, qid, number in unanswered)
    total = len(unanswered) if count is None else count
    warning = f"<p>You have {total} unanswered question{'' if total == 1 else 's'}.</p>" if total else ""
    return f'''<form><input type="hidden" name="d2l_referrer" value="{referrer}">
    <input type="hidden" name="HDN_isRldbUse" value="False"><input type="hidden" name="HDN_isUsingRldb" value="{secure_browser}">
    {warning}{links}<button type="button" primary="">Submit Quiz</button><button type="button">Back to Questions</button>{extra}</form>'''.encode()


def test_unanswered_questions_are_read_from_the_confirmation_links():
    form = hidden_form(confirmation(unanswered=((1, 102, 2), (3, 140855, 9))))[0]
    assert unanswered_questions(form, quiz_id=20, attempt_id=30) == [
        {"page": 1, "question_id": 102, "number": 2}, {"page": 3, "question_id": 140855, "number": 9},
    ]
    assert unanswered_questions(hidden_form(confirmation())[0], quiz_id=20, attempt_id=30) == []
    single = hidden_form(confirmation(unanswered=((1, 102, 2),)))[0]
    assert len(unanswered_questions(single, quiz_id=20, attempt_id=30)) == 1
    none_left = hidden_form(confirmation(extra="<p>You have 0 unanswered questions.</p>"))[0]
    assert unanswered_questions(none_left, quiz_id=20, attempt_id=30) == []


@pytest.mark.parametrize("page", [
    confirmation(unanswered=((1, 102, 2),), count=2),
    confirmation(count=1),
    confirmation(unanswered=((1, 102, 2),), attempt_id=31),
    confirmation(unanswered=((1, 102, 2),)).replace(b"Question 2", b"Question two"),
    confirmation(unanswered=((1, 102, 2),)).replace(b"return false;", b"steal(); return false;"),
])
def test_unanswered_links_must_agree_with_the_count_and_attempt(page):
    with pytest.raises(PreviewPageError):
        unanswered_questions(hidden_form(page)[0], quiz_id=20, attempt_id=30)


# -- the RPC reply ---------------------------------------------------------------


def reply(result: str = "parent.QuizDone(20,30,'0','0', '0', 'gotoSv' , '');", **changes: object) -> bytes:
    return ("while(true){}" + json.dumps({"ResponseType": 0, "IsResultMin": False, "Result": result,
                                           "RedirectUrl": "", "MessageArea": {}, **changes})).encode()


def test_rpc_reply_is_read_as_data_without_whitespace():
    assert rpc_script([reply()[:10], reply()[10:]]) == "parent.QuizDone(20,30,'0','0','0','gotoSv','')"


@pytest.mark.parametrize("chunks", [
    [reply(ResponseType=1)], [reply(IsResultMin=True)], [reply(RedirectUrl="/d2l/error")],
    [reply(Result=None)], [b"[]"], ["text"], [b" " * 65537], [b"[" * 30000 + b"]" * 30000],
])
def test_other_rpc_replies_are_rejected(chunks):
    with pytest.raises(ValueError):
        rpc_script(chunks)


# -- summary and start ------------------------------------------------------------


def summary(*, impersonating: str = "false", can_take: str = "true", start: str = "true", resume: str = "false",
            password: str = "false", button: str | None = "Start Quiz!", disabled: bool = False, progress: str = "",
            fields: dict[str, str] | None = None, protected: bool = True, extra: str = "",
            countdown: str = "") -> bytes:
    hidden = {"d2l_action": "", "d2l_actionparam": "", "d2l_hitCode": "", "d2l_rf": "", "d2l_controlMapPrev": "",
              "hps": "", "drc": "0", "LockDownBrowserUrl": "0", "LockDownBrowserLaunchTimeout": "0",
              "d2l_controlMap": "[{},{}]", "d2l_state": '[{"1":["pagenum"]},[]]', "d2l_referrer": "SESSION_SENTINEL",
              **(fields or {})}
    # Every summary defines the countdown; one before its quiz opens calls it.
    script = ("<script>function StartCountdown(startDateTicks) {\n\tUI.GetControl('BTN_action');\n}</script>"
              f"<script>{countdown}\n\tvar isImpersonatingRole = {impersonating};\nvar canTakeQuiz = {can_take};\n"
              f"var startQuiz = {start};\nvar continueQuiz = {resume};\nvar hasPass = {password};</script>"
              '<script>D2L.Serialization.JsonDeserializerMin.Deserialize(["abababababababababababababababab",77,1,[]])</script>')
    # The start button is rendered outside the form.
    action = (f'<button type="button" primary="" class="d2l-button"{" disabled" if disabled else ""}>{button}</button>'
              if button else "")
    return (f"<h1>Summary - Quiz</h1><p>{progress}</p>{script}{action}<form>{hidden_inputs(list(hidden.items()))}"
            f"{extra}</form>").encode() + (bootstrap() if protected else b"")


IN_PROGRESS = {"start": "false", "resume": "true", "button": "Continue Quiz...", "progress": "Completed - 0 (Attempt 2 in progress)"}
# Before a quiz opens (observed on Lighthouse): Start is flagged but rendered
# disabled, and the summary counts down to the open time in .NET ticks.
OPENS_AT = 1_800_000_000


def countdown(opens_at: float) -> str:
    return f"\tStartCountdown({621_355_968_000_000_000 + int(opens_at * 10**7)});"


NOT_OPEN = {"disabled": True, "countdown": countdown(OPENS_AT)}
# While Brightspace submits an attempt whose time ran out (observed in a
# disposable Brightspace sandbox): Continue is flagged, Start Quiz! is disabled.
PROCESSING = {"start": "false", "resume": "true", "disabled": True,
              "progress": "Allowed - Unlimited, Completed - 9 (Attempt 10 is being processed)"}


def test_summary_reports_start_and_continue_state():
    fresh = parse_learner_summary(summary(), course_id=10, quiz_id=20)
    assert fresh.public_data() == {"course_id": 10, "quiz_id": 20, "can_start": True, "can_continue": False,
                                   "attempt_in_progress": None, "attempt_processing": None, "opens_at": None}
    assert "SESSION_SENTINEL" not in repr(fresh)
    resumed = parse_learner_summary(summary(**IN_PROGRESS), course_id=10, quiz_id=20)
    assert (resumed.can_start, resumed.can_continue, resumed.attempt_in_progress) == (False, True, 2)
    assert resumed.attempt_processing is None
    closed = parse_learner_summary(summary(can_take="false", button=None), course_id=10, quiz_id=20)
    assert not closed.can_start and not closed.can_continue


@pytest.mark.parametrize("page", [summary(**PROCESSING), summary(**{**PROCESSING, "button": None})])
def test_an_attempt_brightspace_is_still_submitting_can_be_neither_continued_nor_started(page):
    processing = parse_learner_summary(page, course_id=10, quiz_id=20)
    assert processing.public_data() == {"course_id": 10, "quiz_id": 20, "can_start": False, "can_continue": False,
                                        "attempt_in_progress": None, "attempt_processing": 10, "opens_at": None}


@pytest.mark.parametrize("page", [
    summary(resume="true"),  # in progress without its attempt
    summary(progress="(Attempt 2 in progress)"),
    summary(progress="(Attempt 1 in progress) (Attempt 2 in progress)", resume="true"),
    summary(extra='<script>var startQuiz = false;</script>'),
    summary(start="maybe"),
    summary(extra='<button type="button">Continue Quiz...</button>'),
    summary(**{**PROCESSING, "resume": "false"}),  # being processed without continueQuiz
    summary(**{**PROCESSING, "start": "true"}),
    summary(**{**PROCESSING, "disabled": False}),  # an enabled start
    summary(**{**PROCESSING, "button": "Continue Quiz...", "disabled": False}),
    summary(**{**PROCESSING, "progress": "(Attempt 9 is being processed) (Attempt 10 is being processed)"}),
    summary(**{**PROCESSING, "progress": "(Attempt 10 in progress) (Attempt 10 is being processed)"}),
    summary(**{**IN_PROGRESS, "progress": "(Attempt 2 in progress) (Attempt 3 is being processed)"}),
])
def test_inconsistent_summary_is_rejected(page):
    with pytest.raises(PreviewPageError):
        parse_learner_summary(page, course_id=10, quiz_id=20)


def test_the_summary_is_reloaded_as_the_browser_reloads_it():
    client = write_client(summary())
    read_learner_summary(client, course_id=10, quiz_id=20)
    call = client.get_raw.call_args
    assert call.args == (summary_path(10, 20),)
    assert call.kwargs == {"max_bytes": MAX_PAGE_BYTES, "_replay_safe": False, "headers": document(reload=True)}


# -- timer ----------------------------------------------------------------------

STARTED = 1_791_000_000  # a Unix time
TIMER = "lighthouse_cli.quiz_learner_transport"


def timer_frame(*, quiz: int = 20, attempt: int = 30, preview: str = "false", started: float = STARTED,
                limit: int = 240, enforced: str = "true", auto_submit: str = "true", exceeded: str = "false",
                extra: str = "", logging_quiz: int | None = None) -> bytes:
    ticks = 621_355_968_000_000_000 + int(started * 10**7)
    # The frame's functions assign and test the same names; only declarations count.
    functions = ("<script>function OnTimeUp() {\n      timeExceeded = true;\n}\n"
                 "function Limit() { return typeof timeLimit == \"undefined\" ? 30000 : timeLimit; }</script>")
    declarations = (f"<script>\n\t\tvar quizId = {quiz};\n\t\tvar isPreview = {preview};\n"
                    f"\t\tvar timeStartedTicks = {ticks};\n\t\tvar attemptTimeLoggingQuizId = {logging_quiz or quiz};\n"
                    f"\t\tvar attemptTimeLoggingAttemptId = {attempt};\n\t\tvar timeLimit = {limit};\n"
                    f"\t\tvar enforceTimeLimit = {enforced};\n\t\tvar timeExceeded = {exceeded};\n"
                    f"\t\tvar hasAutoSubmit = {auto_submit};\n\t\tvar isPreviewFromQB = '0';\n{extra}</script>")
    return (functions + declarations).encode()


def parse_timer(page: bytes, *, now: float = STARTED + 60) -> LearnerTimer | None:
    return parse_learner_timer(page, quiz_id=20, attempt_id=30, now=now)


def test_timer_reads_an_enforced_limit_and_none_otherwise():
    assert parse_timer(timer_frame()) == LearnerTimer(240, STARTED + 240, True)
    assert parse_timer(timer_frame(auto_submit="false")) == LearnerTimer(240, STARTED + 240, False)
    # An untimed attempt's frame still declares a limit, unenforced.
    assert parse_timer(timer_frame(enforced="false", limit=7200)) is None
    # Over time by the server's account: it ends now, whatever the start says.
    assert parse_timer(timer_frame(exceeded="true"), now=STARTED + 100) == LearnerTimer(240, STARTED + 100, True)
    assert parse_timer(timer_frame(exceeded="true"), now=STARTED + 300).ends_at == STARTED + 240


@pytest.mark.parametrize("page", [
    timer_frame(quiz=21), timer_frame(attempt=31), timer_frame(preview="true"),
    timer_frame(logging_quiz=21), timer_frame(limit=0), timer_frame(started=STARTED + 60 + 301), timer_frame(started=0),
    timer_frame(enforced="1"),
    timer_frame(extra="var timeLimit = 60;\n"),  # declared twice
    timer_frame().replace(b"var hasAutoSubmit", b"var autoSubmit"),
    b"<p>var quizId = 20;</p>",
])
def test_an_unexpected_timer_frame_is_rejected(page):
    with pytest.raises(PreviewPageError):
        parse_timer(page)


@pytest.mark.parametrize(("headers", "offset"), [
    # The local clock runs about 10 s ahead of the server's. The offset is the
    # largest the Date allows, so the countdown never ends late.
    ({"Date": formatdate(STARTED + 60, usegmt=True)}, -9),
    ({"date": formatdate(STARTED + 60, usegmt=True)}, -9),
    ({}, 0), ({"Date": "soon"}, 0), ({"Date": formatdate(STARTED + 60)}, 0),  # "-0000": no zone
])
def test_timer_is_read_once_with_the_server_clocks_offset(headers, offset):
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(return_value=(timer_frame(), headers))
    # The clock reads before the request is sent and after its reply, 2 s later.
    with patch(f"{TIMER}.time", Mock(time=Mock(side_effect=[STARTED + 70, STARTED + 72]))):
        timer = read_learner_timer(client, course_id=10, quiz_id=20, attempt_id=30)
    assert timer == LearnerTimer(240, STARTED + 240, True, clock_offset=offset)
    client.get_raw.assert_called_once()
    assert client.get_raw.call_args.args[0] == (
        "/d2l/lms/quizzing/user/attempt/quiz_attempt_top_auto.d2l?ou=10&isprv=&impcf=&qi=20&ai=30"
        "&dnb=0&cfql=0&fromQB=0&cft=&d2l_body_type=3")
    assert client.get_raw.call_args.kwargs["_replay_safe"] is False
    assert client.get_raw.call_args.kwargs["headers"] == document(reload=True)


def test_timer_reloads_as_the_attempt_frame_loads_it():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(return_value=(timer_frame(enforced="false"), {}))
    assert read_learner_timer(client, course_id=10, quiz_id=20, attempt_id=30, frame_page=2) is None
    assert client.get_raw.call_args.kwargs["headers"] == document(attempt_frame_path(10, 20, 30, 2), reload=True)


START_PROCESS = "/d2l/lms/quizzing/user/attempt/quiz_start_process_auto.d2l?ou=10&qi=20&isprv=&fromQB=0&inProgress={}"
HEADER = timer_frame(enforced="false") + SHOW_INCORRECT


def start_client(summary_page: bytes, *, resume: bool = False, page: int = 1, process: bytes | None = None,
                 frame: bytes | None = None, header: bytes | None = None, readback: object = None,
                 location: str | None = None) -> LighthouseClient:
    """The start's redirect, its frames and process page, then the attempt frame, header and page."""
    client = LighthouseClient(read_only_auth=True)
    process_path = START_PROCESS.format(int(resume))
    root = process_path.replace("quiz_start_process_auto", "quiz_start_frame_auto")
    inner = process_path.replace("quiz_start_process_auto", "quiz_start_iframe_2_auto")
    client._request = Mock(return_value=Mock(status_code=302, headers={"Location": location or root}))
    replies = [summary_page, f'<iframe src="{escape(inner, quote=True)}"></iframe>'.encode(),
               f'<iframe name="hiddenFrame" src="{escape(process_path, quote=True)}"></iframe>'.encode(),
               process_page(page) if process is None else process, attempt_frame(page) if frame is None else frame,
               HEADER if header is None else header,
               body(questions(page=page), page=page) if readback is None else readback]
    client.get_raw = Mock(side_effect=[item if isinstance(item, BaseException) else (item, {}) for item in replies])
    return client


def test_start_posts_the_summary_form_once_and_opens_the_attempt_as_the_browser_does():
    client = start_client(summary())
    seen = []
    started = start_learner(client, course_id=10, quiz_id=20,
                            on_identity=lambda attempt_id, page, tab_id: seen.append(
                                (attempt_id, page, tab_id, client.get_raw.call_count)))
    assert (started.page.attempt_id, started.page.page, started.view, started.timer) == (30, 1, VIEW, None)
    assert seen == [(30, 1, TAB, 4)]  # before the attempt frame is read
    client._request.assert_called_once()
    call = client._request.call_args
    assert call.args == ("POST", BASE + "/d2l/lms/quizzing/user/quiz_summary.d2l?qi=20&ou=10&cfql=0&inProgress=false")
    # The summary's own load spent its first hit code.
    assert {key: value for key, (_, value) in call.kwargs["files"]}["d2l_hitCode"].endswith("2")
    assert call.kwargs["headers"] == {**FORM_HEADERS, "Referer": BASE + summary_path(10, 20)}
    assert sent(call) == [
        ("d2l_action", "Custom"), ("d2l_actionparam", "1"), ("d2l_hitCode", "HIT"), ("d2l_rf", ""),
        ("d2l_controlMapPrev", '{"ID":{},"SID":{}}'), ("hps", ""), ("drc", "0"), ("LockDownBrowserUrl", "0"),
        ("LockDownBrowserLaunchTimeout", "0"), ("d2l_controlMap", "[{},{}]"), ("d2l_state", '[{"1":["pagenum"]},[]]'),
        ("d2l_referrer", "SESSION_SENTINEL"),
        ("d2l_multiedit", '{"Controls":[]}'), ("d2l_stateScopes", '{"1":["pagenum"]}'), ("d2l_stateGroups", ""),
        ("d2l_statePageId", "77"),
    ]
    root = BASE + START_PROCESS.format(0).replace("quiz_start_process_auto", "quiz_start_frame_auto")
    inner = BASE + START_PROCESS.format(0).replace("quiz_start_process_auto", "quiz_start_iframe_2_auto")
    assert [(read.args[0], read.kwargs["headers"]) for read in client.get_raw.call_args_list] == [
        (summary_path(10, 20), document(reload=True)),
        # The browser follows the start's redirect as a reload of the summary.
        (root, document(summary_path(10, 20), reload=True)),
        (inner, document(root)),
        (BASE + START_PROCESS.format(0), document(inner)),
        (FRAME1, document(inner)),
        (timer_path(10, 20, 30), document(FRAME1)),
        (PAGE1, document(FRAME1)),
    ]
    assert all(read.kwargs["_replay_safe"] is False and read.kwargs["max_bytes"] == MAX_PAGE_BYTES
               for read in client.get_raw.call_args_list)


@pytest.mark.parametrize("page", [1, 3])
def test_continue_reopens_the_attempt_in_progress_on_the_page_the_server_names(page):
    client = start_client(summary(**IN_PROGRESS), resume=True, page=page)
    opened = start_learner(client, course_id=10, quiz_id=20, continue_only=True)
    assert (opened.page.attempt_id, opened.page.page) == (30, page)
    assert opened.view == AttemptView(10, 20, 30, page, TAB, "true")
    assert client._request.call_args.args[1].endswith("&inProgress=true")
    assert client.get_raw.call_args.args[0] == learner_page_path(10, 20, 30, page)


@pytest.mark.parametrize("resume, known, tab", [(True, "987", "987"), (False, "987", ""), (True, "", "")])
def test_a_process_page_without_a_tab_id_keeps_the_known_one_only_for_a_continued_attempt(resume, known, tab):
    kwargs = IN_PROGRESS if resume else {}
    client = start_client(summary(**kwargs), resume=resume, process=process_page(tab=""))
    seen = []
    opened = start_learner(client, course_id=10, quiz_id=20, known_tab_id=known,
                           on_identity=lambda *identity: seen.append(identity))
    assert opened.view.tab_id == tab and seen == [(30, 1, tab)]


def test_an_unclear_tab_id_seals_the_attempt_and_is_unknown():
    client = start_client(summary(), process=process_page(tab=" sessionStorage.setItem('D2L_BROWSER_TAB_ID', '1');"))
    seen = []
    with pytest.raises(LearnerStartUnknownError) as exc_info:
        start_learner(client, course_id=10, quiz_id=20, on_identity=lambda *identity: seen.append(identity))
    assert (exc_info.value.attempt_id, exc_info.value.page, exc_info.value.timer) == (30, 1, False)
    assert seen == [(30, 1, "")] and client.get_raw.call_count == 4


@pytest.mark.parametrize("built, timer, reads", [
    ({"header": timer_frame(enforced="false")}, True, 6),  # no showIncorrectOnly
    ({"header": NetworkError("connection reset")}, True, 6),
    ({"frame": attempt_frame(2)}, False, 5),  # the frame loads another page
], ids=["no-view-state", "header-fails", "other-frame"])
def test_an_attempt_frame_that_cannot_be_read_is_unknown_with_the_attempt_identity(built, timer, reads):
    client = start_client(summary(), **built)
    with pytest.raises(LearnerStartUnknownError) as exc_info:
        start_learner(client, course_id=10, quiz_id=20)
    assert (exc_info.value.attempt_id, exc_info.value.page, exc_info.value.timer) == (30, 1, timer)
    assert client.get_raw.call_count == reads
    client._request.assert_called_once()


@pytest.mark.parametrize("read, reads", [("frame", 5), ("header", 6), ("readback", 7)])
def test_a_session_expiry_opening_the_attempt_is_kept_with_the_attempt_identity(read, reads):
    expired = SessionExpiredError("Session expired.")
    client = start_client(summary(), **{read: expired})
    with pytest.raises(LearnerStartUnknownError) as exc_info:
        start_learner(client, course_id=10, quiz_id=20)
    assert (exc_info.value.attempt_id, exc_info.value.page, exc_info.value.timer) == (30, 1, False)
    assert exc_info.value.expired is expired
    assert client.get_raw.call_count == reads
    client._request.assert_called_once()


@pytest.mark.parametrize("kwargs", [{"known_tab_id": "12a"}, {"known_tab_id": None}, {"continue_only": 1}])
def test_invalid_start_settings_send_nothing(kwargs):
    client = start_client(summary())
    with pytest.raises(ValueError, match="Invalid quiz start settings"):
        start_learner(client, course_id=10, quiz_id=20, **kwargs)
    client.get_raw.assert_not_called()
    client._request.assert_not_called()


def test_start_refuses_while_brightspace_is_still_submitting_an_attempt():
    client = start_client(summary(**PROCESSING))
    read = read_learner_summary(client, course_id=10, quiz_id=20)
    with pytest.raises(PreviewRefusedError) as exc_info:
        start_learner(client, course_id=10, quiz_id=20, summary=read)
    assert str(exc_info.value) == ("Brightspace is still submitting attempt 10 after its time ran out, so nothing was "
                                   "started. Run attempt start again in a few minutes, or attempt verify if the CLI was "
                                   "taking that attempt.")
    # Neither Continue, which the summary flags, nor Start was sent.
    client._request.assert_not_called()
    client.get_raw.assert_called_once()


def test_a_summary_just_read_is_not_requested_again():
    client = start_client(summary())
    read = read_learner_summary(client, course_id=10, quiz_id=20)
    started = start_learner(client, course_id=10, quiz_id=20, summary=read)
    assert started.page.attempt_id == 30
    assert client._request.call_count == 1 and client.get_raw.call_count == 7
    other = start_client(summary())
    with pytest.raises(ValueError, match="Invalid quiz start settings"):
        start_learner(other, course_id=10, quiz_id=21, summary=read_learner_summary(other, course_id=10, quiz_id=20))
    other._request.assert_not_called()


@pytest.mark.parametrize("page, kwargs, message", [
    (summary(), {"continue_only": True}, REFUSE_NOTHING_IN_PROGRESS),
    (summary(impersonating="true"), {}, REFUSE_START_ROLE),
    (summary(password="true"), {}, REFUSE_START_PASSWORD),  # pragma: allowlist secret
    (summary(extra='<input type="password" name="password">'), {}, REFUSE_START_PASSWORD),
    (summary(can_take="false"), {}, REFUSE_START_UNAVAILABLE),
    (summary(button=None), {}, REFUSE_START_UNAVAILABLE),
    (summary(button=None, extra='<button type="button" class="d2l-hidden">Start Quiz!</button>'), {}, REFUSE_START_UNAVAILABLE),
    (summary(button=None, extra='<fieldset disabled><button type="button">Start Quiz!</button></fieldset>'), {},
     REFUSE_START_UNAVAILABLE),
    (summary(button="Continue Quiz..."), {}, REFUSE_START_UNAVAILABLE),
    (summary(fields={"LockDownBrowserUrl": "1"}), {}, REFUSE_START_BROWSER),
    (summary(fields={"hps": "1"}), {}, REFUSE_START_BROWSER),
    (summary(protected=False), {}, REFUSE_START_PROTECTION),
    (summary(fields={"d2l_referrer": "OTHER_SESSION"}), {}, REFUSE_START_PROTECTION),
    (summary(**PROCESSING), {}, REFUSE_START_PROCESSING.format(10)),
    (summary(**PROCESSING), {"continue_only": True}, REFUSE_START_PROCESSING.format(10)),
    (summary(**PROCESSING, password="true"), {}, REFUSE_START_PROCESSING.format(10)),  # pragma: allowlist secret
    (summary(fields={"d2l_controlMap": "[{},{},{}]"}), {}, REFUSE_LEARNER_FORM),
    (summary(extra='<input type="hidden" name="d2l_stateScopes" value="">'), {}, REFUSE_LEARNER_FORM),
    # The open time is told only for a start the countdown holds back.
    (summary(**NOT_OPEN), {"continue_only": True}, REFUSE_NOTHING_IN_PROGRESS),
    (summary(**NOT_OPEN, can_take="false"), {}, REFUSE_START_UNAVAILABLE),
    (summary(**NOT_OPEN, password="true"), {}, REFUSE_START_PASSWORD),  # pragma: allowlist secret
])
def test_start_refusals_send_nothing(page, kwargs, message):
    client = write_client(page)
    with pytest.raises(PreviewRefusedError, match=re.escape(message)) as exc_info:
        start_learner(client, course_id=10, quiz_id=20, **kwargs)
    assert not isinstance(exc_info.value, LearnerNotOpenError)
    client._request.assert_not_called()


@pytest.mark.parametrize("calls", ["", "function StartCountdown(startDateTicks) {}",
                                   "window.StartCountdown(639355968000000000);", "StartCountdown(1, 2);",
                                   "var x = StartCountdown(639355968000000000);"])
def test_a_summary_without_its_own_countdown_call_has_no_open_time(calls):
    assert parse_learner_summary(summary(countdown=calls), course_id=10, quiz_id=20).opens_at is None


@pytest.mark.parametrize("calls", [countdown(OPENS_AT), countdown(OPENS_AT) + "\n" + countdown(OPENS_AT)])
def test_a_summary_before_the_quiz_opens_names_the_open_time(calls):
    page = parse_learner_summary(summary(disabled=True, countdown=calls), course_id=10, quiz_id=20)
    assert (page.can_start, page.opens_at) == (True, OPENS_AT)
    assert page.public_data()["opens_at"] == OPENS_AT


@pytest.mark.parametrize("calls", [
    countdown(OPENS_AT) + "\n" + countdown(OPENS_AT + 60),  # two open times
    countdown(946_684_799),  # before 2000
    "StartCountdown(9999999999999999999);",  # after 9999
])
def test_an_implausible_countdown_is_rejected(calls):
    with pytest.raises(PreviewPageError):
        parse_learner_summary(summary(disabled=True, countdown=calls), course_id=10, quiz_id=20)


@pytest.mark.parametrize("read_at, seconds, relative", [
    (OPENS_AT - 9, 9, " (in 9 seconds)"),
    (OPENS_AT - 0.5, 1, " (in 1 second)"),
    (OPENS_AT - 60, 60, " (in 1 minute)"),
    (OPENS_AT - 61, 61, " (in 2 minutes)"),
    (OPENS_AT - 3600, 3600, ""),
    (OPENS_AT + 5, 0, ""),
    (None, None, ""),
])
def test_a_start_before_the_quiz_opens_says_when_and_sends_nothing(read_at, seconds, relative):
    client = write_client()
    page = parse_learner_summary(summary(**NOT_OPEN), course_id=10, quiz_id=20, read_at=read_at)
    with pytest.raises(LearnerNotOpenError) as exc_info:
        start_learner(client, course_id=10, quiz_id=20, summary=page)
    local = datetime.fromtimestamp(OPENS_AT).astimezone()
    assert str(exc_info.value) == (f"This quiz opens at {local:%H:%M} {local:%Z} on {local.day} {local:%B %Y}"
                                   f"{relative}. Nothing was started.")
    assert (exc_info.value.opens_at, exc_info.value.seconds_until_open) == (OPENS_AT, seconds)
    assert exc_info.value.public_data() == {"opens_at": "2027-01-15T08:00:00Z", "seconds_until_open": seconds}
    client._request.assert_not_called()


def test_the_summary_s_server_time_counts_down_to_the_open_time():
    client = write_client()
    client.get_raw = Mock(return_value=(summary(**NOT_OPEN), {"Date": formatdate(OPENS_AT - 9, usegmt=True)}))
    with pytest.raises(LearnerNotOpenError) as exc_info:
        start_learner(client, course_id=10, quiz_id=20)
    assert exc_info.value.seconds_until_open == 9
    client._request.assert_not_called()


def test_an_enabled_start_is_posted_even_with_a_countdown():
    client = start_client(summary(countdown=countdown(OPENS_AT)))
    assert start_learner(client, course_id=10, quiz_id=20).page.attempt_id == 30
    client._request.assert_called_once()


@pytest.mark.parametrize("change", [
    {"location": START_PROCESS.format(1).replace("quiz_start_process_auto", "quiz_start_frame_auto")},
    {"location": START_PROCESS.format(0).replace("quiz_start_process_auto", "quiz_start_frame_auto").replace("isprv=", "isprv=1")},
    {"process": b'<script>const text="parent.GoToAttemptQuizAuto(30,1,0)";</script>'},
])
def test_unexpected_start_chain_is_unknown_and_not_retried(change):
    client = start_client(summary(), **change)
    with pytest.raises(LearnerStartUnknownError) as exc_info:
        start_learner(client, course_id=10, quiz_id=20)
    assert exc_info.value.attempt_id is None
    client._request.assert_called_once()


@pytest.mark.parametrize("built, called, page, reads", [
    # A new attempt must open on its first page: page 2 is never requested.
    ({"process": process_page(2)}, {}, None, 4),
    ({"readback": SessionExpiredError("session expired")}, {}, 1, 7),
    ({}, {"on_identity": Mock(side_effect=OSError("disk full"))}, 1, 4),  # no frame read after a failed seal
], ids=["new-attempt-on-page-2", "readback-auth-expiry", "identity-callback-fails"])
def test_start_failure_after_the_post_is_unknown_with_the_attempt_identity(built, called, page, reads):
    client = start_client(summary(), **built)
    with pytest.raises(LearnerStartUnknownError) as exc_info:
        start_learner(client, course_id=10, quiz_id=20, **called)
    assert (exc_info.value.attempt_id, exc_info.value.page) == (30, page)
    assert client.get_raw.call_count == reads
    client._request.assert_called_once()


@pytest.mark.parametrize("failing", [1, 2, 3])  # the outer frame, inner frame and process page
def test_start_chain_network_failure_is_unknown_and_not_retried(failing):
    client = start_client(summary())
    replies = list(client.get_raw.side_effect)
    replies[failing] = NetworkError("connection reset")
    client.get_raw = Mock(side_effect=replies)
    with pytest.raises(LearnerStartUnknownError):
        start_learner(client, course_id=10, quiz_id=20)
    client._request.assert_called_once()
    assert client.get_raw.call_count == failing + 1


@pytest.mark.parametrize("post", [
    pytest.param(SessionExpiredError("session expired"), id="auth-expiry-after-dispatch"),
    pytest.param(Mock(status_code=200, headers={}), id="no-redirect"),
])
def test_start_post_without_its_redirect_is_unknown(post):
    client = start_client(summary())
    client._request = Mock(side_effect=[post])
    with pytest.raises(LearnerStartUnknownError):
        start_learner(client, course_id=10, quiz_id=20)
    client._request.assert_called_once()
    client.get_raw.assert_called_once()


# -- save ---------------------------------------------------------------------------


def posted(reply: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> Mock:
    """A page form's response: the reply its hidden save frame loads."""
    response = Mock(status_code=status, headers=headers or {})
    response.iter_content.return_value = [reply]
    return response


def write_client(*pages: object, posts: list[object] | tuple[object, ...] = ()) -> LighthouseClient:
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[page if isinstance(page, BaseException) else (page, {}) for page in pages])
    client._request = Mock(side_effect=list(posts))
    return client


ALL = {101: "o2", 102: ["o11", "o13"], 103: ["four", "six"]}


def test_save_sends_one_autosave_per_changed_answer_and_reads_each_back():
    posts = [posted(saved_reply("101")), posted(saved_reply("102")), posted(saved_reply("103"))]
    client = write_client(body(questions()),
                          body(questions(sc=("o2",), saved=("True", "False", "False"))),
                          body(questions(sc=("o2",), ms=("o11", "o13"), saved=("True", "True", "False"))),
                          body(ANSWERED), posts=posts)
    readback, snapshot = save_learner_answers(client, VIEW, answers=ALL)
    assert readback.questions[2]["blanks"][1]["value"] == "six"
    assert snapshot.values == {101: ("o2",), 102: ("o11", "o13"), 103: ("four", "six")}
    assert snapshot.flags == {"z_c": "3", "z_r101": "1", "z_r102": "1", "z_r103": "1", "z_r104": "0"}
    calls = client._request.call_args_list
    assert [dict(sent(call))["d2l_actionparam"] for call in calls] == ["3,1,201,300,1", "3,1,202,300,2", "3,1,203,300,3"]
    # Each form holds the page as the browser does: earlier answers and their saved flags.
    assert [dict(sent(call))["z_c"] for call in calls] == ["0", "1", "2"]
    assert [dict(sent(call))["z_r101"] for call in calls] == ["0", "1", "1"]
    assert answer_fields(sent(calls[0])) == {"tAtom201_300": "o2", "tAtom203_300_601": "", "tAtom203_300_602": ""}
    assert answer_fields(sent(calls[2])) == {"tAtom201_300": "o2", "tAtom202_300_o11": "1", "tAtom202_300_o13": "1",
                                             "tAtom203_300_601": "four", "tAtom203_300_602": "six"}
    for call in calls:
        assert call.args == ("POST", BASE + autosave_path(10))
        assert call.kwargs["headers"] == {**FORM_HEADERS, "Referer": BASE + PAGE1}
        assert call.kwargs["_skip_raise"] is True and call.kwargs["stream"] is True
    assert [(read.args[0], read.kwargs["headers"]) for read in client.get_raw.call_args_list] == [
        (PAGE1, document(FRAME1, reload=True))] * 4
    for response in posts:
        response.close.assert_called_once()


def test_an_answer_already_saved_as_given_is_not_sent_again():
    client = write_client(body(ANSWERED))
    readback, snapshot = save_learner_answers(client, VIEW, answers={101: "o2", 103: ["four", "six"]})
    assert readback.page == 1 and snapshot.values[101] == ("o2",)
    client._request.assert_not_called()
    # Shown but not saved: sent.
    shown = parse(body(questions(sc=("o2",))))
    client = write_client(body(questions(sc=("o2",), saved=("True", "False", "False"))), posts=[posted(saved_reply("101"))])
    save_learner_answers(client, VIEW, answers={101: "o2"}, current=shown)
    client._request.assert_called_once()
    client.get_raw.assert_called_once()  # the readback only


@pytest.mark.parametrize("reply, readback", [
    (saved_reply("", "102"), body(questions())),
    # The reply may leave a cleared answer's flag as it was.
    (saved_reply(""), body(questions(), flags={102: "1"})),
])
def test_a_cleared_answer_is_saved_as_the_server_marks_it(reply, readback):
    current = parse(body(questions(ms=("o12",), saved=("False", "True", "False"))))
    client = write_client(readback, posts=[posted(reply)])
    _, snapshot = save_learner_answers(client, VIEW, answers={102: []}, current=current)
    assert snapshot.values[102] == ()
    assert answer_fields(sent(client._request.call_args)) == {"tAtom203_300_601": "", "tAtom203_300_602": ""}


def test_a_refused_post_stops_the_save_before_it_is_sent():
    before_post = Mock(side_effect=[None, OSError("disk full")])
    client = write_client(body(questions()), body(questions(sc=("o2",), saved=("True", "False", "False"))),
                          posts=[posted(saved_reply("101"))])
    with pytest.raises(OSError):
        save_learner_answers(client, VIEW, answers=ALL, before_post=before_post)
    client._request.assert_called_once()
    assert before_post.call_count == 2


def test_a_page_the_cli_cannot_post_is_refused_before_the_first_save():
    client = write_client(body(questions(), fetch="true"))
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_FORM)):
        save_learner_answers(client, VIEW, answers={101: "o1"})
    client._request.assert_not_called()


@pytest.mark.parametrize("post, readback", [
    (posted(saved_reply("102")), None),  # the reply marks another question
    (posted(saved_reply("101", "102")), None),
    (posted(reload_reply(2)), None),
    (posted(saved_reply("101"), status=500), None),
    (posted(saved_reply("101"), headers={"Content-Length": str(MAX_PAGE_BYTES + 1)}), None),
    (posted(b"x" * (MAX_PAGE_BYTES + 1)), None),
    (SessionExpiredError("session expired"), None),
    (posted(saved_reply("101")), body(questions())),  # 200 without the answer persisted
    (posted(saved_reply("101")), body(questions(sc=("o2",)))),  # not marked saved
    (posted(saved_reply("101")), body(questions(sc=("o2",), saved=("True", "False", "False")), flags={104: "1"})),
    (posted(saved_reply("101")), SessionExpiredError("session expired")),
], ids=["other-question", "saved-and-unsaved", "page-change-reply", "server-error", "oversized-length", "oversized-body",
        "post-auth-expiry", "not-persisted", "not-marked", "other-flags", "readback-auth-expiry"])
def test_unverified_save_is_unknown_and_not_retried(post, readback):
    client = write_client(body(questions()), body(questions(sc=("o2",), saved=("True", "False", "False")))
                          if readback is None else readback, posts=[post])
    with pytest.raises(LearnerSaveUnknownError):
        save_learner_answers(client, VIEW, answers={101: "o2", 102: ["o11"]})
    client._request.assert_called_once()
    if isinstance(post, Mock):
        post.close.assert_called_once()


def test_later_pages_are_never_read_from_a_bare_view():
    # Reading past the last page breaks the attempt, so pages after the
    # first come from a verified readback.
    client = write_client()
    for call in (lambda: save_learner_answers(client, VIEW2, answers={101: "o1"}),
                 lambda: advance_learner(client, VIEW2),
                 lambda: retreat_learner(client, VIEW2),
                 lambda: submit_learner(client, VIEW2)):
        with pytest.raises(ValueError, match="verified readback"):
            call()
    client.get_raw.assert_not_called()
    client._request.assert_not_called()


def test_a_page_from_another_attempt_or_page_is_rejected():
    client = write_client()
    other = parse_learner_page(body(questions()).replace(b'name="ai" value="30"', b'name="ai" value="31"'),
                               course_id=10, quiz_id=20, attempt_id=31, page=1)
    for call in (lambda: save_learner_answers(client, VIEW, answers={101: "o1"}, current=other),
                 lambda: save_learner_answers(client, VIEW2, answers={101: "o1"}, current=parse(body(questions()))),
                 lambda: advance_learner(client, VIEW, snapshot=other.snapshot()),
                 lambda: save_learner_answers(client, "view", answers={101: "o1"})):
        with pytest.raises(ValueError, match="Invalid quiz attempt"):
            call()
    client._request.assert_not_called()


@pytest.mark.parametrize("answers, page, message", [
    ({}, body(questions()), REFUSE_NO_ANSWERS),
    ({101: "o9"}, body(questions()), REFUSE_NOT_A_CHOICE),
    ({101: "o1"}, body(questions(), protected=False), REFUSE_PAGE_PROTECTION),
])
def test_save_refusals_send_nothing(answers, page, message):
    client = write_client(page)
    with pytest.raises(PreviewRefusedError, match=re.escape(message)):
        save_learner_answers(client, VIEW, answers=answers)
    client._request.assert_not_called()


# -- next and previous ----------------------------------------------------------------


def test_next_posts_the_page_and_loads_the_next_one_as_the_browser_does():
    response = posted(reload_reply(2))
    client = write_client(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT), body(questions(page=2), page=2), posts=[response])
    page, view = advance_learner(client, VIEW)
    assert (page.page, view) == (2, VIEW2)
    call = client._request.call_args
    assert call.args == ("POST", BASE + NEXT_POST)
    assert call.kwargs["headers"] == {**FORM_HEADERS, "Referer": BASE + PAGE1}
    fields = dict(sent(call))
    assert (fields["d2l_actionparam"], fields["tAtom201_300"], fields["QuizTabId"]) == ("2,2,1", "o2", TAB)
    read = client.get_raw.call_args
    assert (read.args[0], read.kwargs["headers"]) == (goself(2, "0"), document(PAGE1))
    response.close.assert_called_once()


def test_next_from_the_page_as_just_saved_skips_the_page_read():
    saved = parse(body(questions(), extra=LEARNER_BUTTONS + NEXT)).snapshot().with_value(101, ("o1",))
    client = write_client(body(questions(page=2), page=2), posts=[posted(reload_reply(2))])
    page, view = advance_learner(client, VIEW, snapshot=saved, allow_unanswered=True)
    assert (page.page, view) == (2, VIEW2)
    assert dict(sent(client._request.call_args))["tAtom201_300"] == "o1"
    client.get_raw.assert_called_once()


def test_next_from_a_page_without_its_protection_sends_nothing():
    unprotected = parse(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT, protected=False)).snapshot()
    client = write_client()
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_PAGE_PROTECTION)):
        advance_learner(client, VIEW, snapshot=unprotected)
    client._request.assert_not_called()


def test_next_from_a_later_page_loads_from_its_own_url():
    current = parse(body(SECOND, page=2, extra=LEARNER_BUTTONS + NEXT), page=2)
    client = write_client(body(questions(page=3), page=3), posts=[posted(reload_reply(3))])
    page, view = advance_learner(client, VIEW2, current=current, allow_unanswered=True)
    assert (page.page, view) == (3, VIEW3)
    assert client._request.call_args.kwargs["headers"]["Referer"] == BASE + goself(2, "0")
    read = client.get_raw.call_args
    assert (read.args[0], read.kwargs["headers"]) == (goself(3, "1"), document(goself(2, "0")))


@pytest.mark.parametrize("page, kwargs, message", [
    (body(ANSWERED), {}, REFUSE_LEARNER_LAST_PAGE),
    (body(questions(), extra=LEARNER_BUTTONS + NEXT), {}, REFUSE_LEARNER_UNANSWERED),
    (body(ANSWERED, extra=LEARNER_BUTTONS + NEXT), {"forward_only": True}, REFUSE_LEARNER_FORM),
    (body(ANSWERED, extra=LEARNER_BUTTONS + NEXT, race=None), {}, REFUSE_LEARNER_FORM),
    (body(ANSWERED, extra=LEARNER_BUTTONS + NEXT, protected=False), {}, REFUSE_PAGE_PROTECTION),
])
def test_next_refusals_send_nothing(page, kwargs, message):
    # Requesting a page past the last one permanently breaks an attempt.
    client = write_client(page)
    with pytest.raises(PreviewRefusedError, match=re.escape(message)):
        advance_learner(client, VIEW, **kwargs)
    client._request.assert_not_called()


@pytest.mark.parametrize("kwargs", [{"forward_only": 1}, {"forward_only": "true"}, {"allow_unanswered": "yes"},
                                    {"snapshot": "page"}])
def test_invalid_page_change_settings_send_nothing(kwargs):
    client = write_client(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT))
    with pytest.raises(ValueError, match="Invalid"):
        advance_learner(client, VIEW, **kwargs)
    client._request.assert_not_called()


@pytest.mark.parametrize("post, readback, reads", [
    # Without the reply that loads it, the next page is never requested.
    (posted(reload_reply(2), status=500), None, 1),
    (posted(reload_reply(3)), None, 1),
    (posted(saved_reply("101")), None, 1),
    (SessionExpiredError("session expired"), None, 1),
    (posted(reload_reply(2)), SessionExpiredError("session expired"), 2),
    (posted(reload_reply(2)), body(questions()), 2),  # page 1 again
], ids=["server-error", "other-page-reply", "save-reply", "post-auth-expiry", "readback-auth-expiry", "same-page"])
def test_unverified_next_is_unknown_and_not_retried(post, readback, reads):
    client = write_client(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT),
                          body(questions(page=2), page=2) if readback is None else readback, posts=[post])
    with pytest.raises(LearnerAdvanceUnknownError):
        advance_learner(client, VIEW)
    client._request.assert_called_once()
    assert client.get_raw.call_count == reads


def second_page(extra: str = LEARNER_BUTTONS + PREVIOUS, *, protected: bool = True):
    # Pages after the first come from a verified readback.
    return parse(body(SECOND, page=2, extra=extra, protected=protected), page=2)


def test_previous_posts_the_page_and_loads_the_one_before():
    response = posted(reload_reply(1))
    client = write_client(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT), posts=[response])
    page, view = retreat_learner(client, VIEW2, current=second_page())
    assert page.page == 1 and page.questions[0]["selected_choice_ids"] == ["o2"]
    assert view == replace(VIEW, state=PageState(1, POSTED_SCOPES, 4242, 2, "0"))
    call = client._request.call_args
    assert call.args == ("POST", BASE + NEXT_POST)
    assert call.kwargs["headers"] == {**FORM_HEADERS, "Referer": BASE + goself(2, "0")}
    fields = dict(sent(call))
    assert (fields["d2l_actionparam"], fields["pg"], fields["tAtom201_300"]) == ("2,1,2", "2", "o1")
    read = client.get_raw.call_args
    assert (read.args[0], read.kwargs["headers"]) == (goself(1, "1"), document(goself(2, "0")))
    client.get_raw.assert_called_once()
    response.close.assert_called_once()


@pytest.mark.parametrize("view, current, message, reads", [
    # Page 1: no control (the page read fresh), a disabled one, or an enabled one below which there is no page.
    (VIEW, None, REFUSE_LEARNER_FIRST_PAGE, 1),
    (VIEW, parse(body(ANSWERED, extra=LEARNER_BUTTONS + PREVIOUS.replace('type="button"', 'type="button" disabled', 1))),
     REFUSE_LEARNER_FIRST_PAGE, 0),
    (VIEW, parse(body(ANSWERED, extra=LEARNER_BUTTONS + PREVIOUS)), REFUSE_LEARNER_FIRST_PAGE, 0),
    (VIEW2, second_page(LEARNER_BUTTONS + NEXT), REFUSE_LEARNER_FIRST_PAGE, 0),
    (VIEW2, second_page(protected=False), REFUSE_PAGE_PROTECTION, 0),
    (VIEW2, second_page(LEARNER_BUTTONS + PREVIOUS.replace('id="z_y"', 'id="z_u"')), REFUSE_LEARNER_FORM, 0),
])
def test_previous_refusals_send_nothing(view, current, message, reads):
    client = write_client(body(ANSWERED))
    with pytest.raises(PreviewRefusedError, match=re.escape(message)):
        retreat_learner(client, view, current=current)
    client._request.assert_not_called()
    assert client.get_raw.call_count == reads


@pytest.mark.parametrize("post, readback", [
    (posted(reload_reply(1), status=500), None),
    (posted(reload_reply(2)), None),
    (SessionExpiredError("session expired"), None),
    (posted(reload_reply(1)), SessionExpiredError("session expired")),
    (posted(reload_reply(1)), body(questions(page=2), page=2)),
], ids=["server-error", "other-page-reply", "post-auth-expiry", "readback-auth-expiry", "same-page"])
def test_unverified_previous_is_unknown_and_not_retried(post, readback):
    client = write_client(body(ANSWERED) if readback is None else readback, posts=[post])
    with pytest.raises(LearnerAdvanceUnknownError):
        retreat_learner(client, VIEW2, current=second_page())
    client._request.assert_called_once()


# -- submit and verify --------------------------------------------------------------


RECEIPT = b"<h1>Quiz</h1><h2>Your work has been saved and submitted</h2><p>Written: Oct 3, 2026</p>"


def listing(*, attempt_id: int = 30, state: str = "", grade: str | None = "<label>6</label><label> / </label><label>25</label><label> - </label><label>24 %</label>",
            rows: int = 1) -> bytes:
    href = (f"/d2l/lms/quizzing/user/quiz_submissions_attempt.d2l?isprv=&amp;qi=20&amp;ai={attempt_id}&amp;isInPopup=0"
            "&amp;cfql=0&amp;fromQB=0&amp;fromSubmissionsList=1&amp;ou=10")
    cell = "" if grade is None else f'<td class="d_gn"><div class="dco d2l-grades-score">{grade}</div></td>'
    row = f'<tr><td><a class="d2l-link" href="{href}">Attempt 1</a>{state}</td>{cell}</tr>'
    return f"<table><tr><th>Attempt</th><th>Grade</th></tr>{row * rows}</table>".encode()


def submit_client(*, confirm: bytes | None = None, result: str = "parent.QuizDone(20,30,'0','0','0','gotoSv','')",
                  receipt: bytes = RECEIPT, submissions: bytes | None = None, finished: bytes = FINISHED):
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(confirm or confirmation(), {}), (submissions or listing(), {}), (receipt, {})])
    prep = posted(finished)
    rpc = Mock(status_code=200)
    rpc.iter_content.return_value = [reply(result)]
    client._request = Mock(side_effect=[prep, rpc])
    return client, prep, rpc


RECEIPT_PATH = ("/d2l/lms/quizzing/user/quiz_submissions_attempt.d2l?qi=20&ai=30&isInPopup=0&isTimeUp=0&isprv=0&dnb=0"
                "&cfql=0&cft=&fromQB=0&d2l_body_type=1&ou=10")


def test_submit_saves_confirms_and_verifies_the_receipt_and_list_row():
    client, prep, rpc = submit_client()
    result = submit_learner(client, VIEW, current=LAST)
    assert result == {"mode": "learner", "course_id": 10, "quiz_id": 20, "attempt_id": 30, "submitted": True,
                      "attempt_number": 1, "score": 6.0, "out_of": 25.0, "percent": 24.0}
    save, final = client._request.call_args_list
    assert save.args == ("POST", BASE + finish_path(10))
    assert save.kwargs["headers"] == {**FORM_HEADERS, "Referer": BASE + PAGE1}
    fields = dict(sent(save))
    assert (fields["d2l_actionparam"], fields["QuizTabId"]) == ("5,1", TAB)
    # The page spends its first hit code on this save, and the attempt
    # frame its own first on the call below.
    assert {key: value for key, (_, value) in save.kwargs["files"]}["d2l_hitCode"].endswith("2")
    confirm, submissions, receipt = client.get_raw.call_args_list
    assert confirm.args[0] == (ATTEMPT_ROUTE + "quiz_confirm_submit_auto.d2l?qi=20&ai=30&isprv=&drc=&impcf=&dnb=0&cfql=0"
                               "&btlp=1&fromQB=0&cft=&d2l_body_type=3&ou=10")
    assert confirm.kwargs["headers"] == document(finish_path(10))
    # The attempt frame's own submission call: not a preview, can be graded,
    # and Boolean("False") as the browser sends it.
    assert final.args == ("POST", BASE + attempt_frame_path(10, 20, 30, 1, rpc=True))
    assert re.fullmatch(
        re.escape("d2l_rf=ProcessQuizSubmission&params=%7B%22param1%22%3A%2220%22,%22param2%22%3A%2230%22,"
                  "%22param3%22%3Afalse,%22param4%22%3Atrue,%22param5%22%3Atrue,%22param6%22%3Afalse,"
                  "%22param7%22%3A%22%22%7D&d2l_referrer=SESSION_SENTINEL&d2l_hitcode=")
        + "1234567890[0-9]+2&d2l_action=rpc", final.kwargs["data"])
    assert final.kwargs["headers"] == {"Content-Type": "application/x-www-form-urlencoded", "Origin": BASE,
                                       "Referer": BASE + FRAME1}
    assert (submissions.args[0], submissions.kwargs["headers"]) == (
        "/d2l/lms/quizzing/user/quiz_submissions.d2l?ou=10&qi=20", document(reload=True))
    assert (receipt.args[0], receipt.kwargs["headers"]) == (RECEIPT_PATH, document(FRAME1))
    prep.close.assert_called_once()
    rpc.close.assert_called_once()


def test_submit_from_a_later_page_posts_from_its_own_url():
    current = parse(body(ANSWERED.replace('autosave-page"><input type="hidden" value="1"',
                                          'autosave-page"><input type="hidden" value="2"'), page=2), page=2)
    finished = onload("parent.infoFrame.UpdateSaved('101','')", "DoConfirmSubmitAuto(20,30,2,'',0)")
    client, _, _ = submit_client(finished=finished)
    assert submit_learner(client, VIEW2, current=current)["submitted"]
    save, final = client._request.call_args_list
    assert save.kwargs["headers"]["Referer"] == BASE + goself(2, "0")
    assert dict(sent(save))["d2l_actionparam"] == "5,2"
    assert "&btlp=2&" in client.get_raw.call_args_list[0].args[0]
    # The attempt frame is still the one opened on page 1.
    assert final.args[1] == BASE + attempt_frame_path(10, 20, 30, 1, rpc=True)


def test_unanswered_questions_stop_the_submission_before_the_final_request():
    client, _, _ = submit_client(confirm=confirmation(unanswered=((1, 102, 2),)))
    with pytest.raises(LearnerUnansweredError) as exc_info:
        submit_learner(client, VIEW, current=LAST)
    assert exc_info.value.questions == [{"page": 1, "question_id": 102, "number": 2}]
    assert not isinstance(exc_info.value, PreviewRefusedError)  # the page save was sent
    client._request.assert_called_once()  # the page save only


def test_unanswered_questions_can_be_submitted_explicitly():
    client, _, _ = submit_client(confirm=confirmation(unanswered=((1, 102, 2),)))
    assert submit_learner(client, VIEW, current=LAST, allow_unanswered=True)["submitted"]
    assert client._request.call_count == 2


def test_submit_reads_the_last_page_when_no_readback_is_given():
    client, _, _ = submit_client()
    client.get_raw.side_effect = [(body(ANSWERED), {}), *client.get_raw.side_effect]
    assert submit_learner(client, VIEW)["submitted"]
    first = client.get_raw.call_args_list[0]
    assert (first.args[0], first.kwargs["headers"]) == (PAGE1, document(FRAME1, reload=True))
    assert client._request.call_count == 2


def test_submit_refuses_before_any_write_when_not_on_the_last_page():
    client, _, _ = submit_client()
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_NOT_LAST_PAGE)):
        submit_learner(client, VIEW, current=parse(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT)))
    with pytest.raises(ValueError, match="Invalid quiz submission settings"):
        submit_learner(client, VIEW, current=LAST, allow_unanswered="yes")
    client._request.assert_not_called()


@pytest.mark.parametrize("finished", [
    onload("parent.infoFrame.UpdateSaved('101,104','')", "DoConfirmSubmitAuto(20,30,1,'',0)"),
    onload("parent.infoFrame.UpdateSaved('101','')", "DoConfirmSubmitAuto(20,30,2,'',0)"),
    reload_reply(2),
])
def test_an_unexpected_finish_reply_is_unknown_before_the_confirmation(finished):
    client, prep, _ = submit_client(finished=finished)
    with pytest.raises(LearnerSubmitUnknownError):
        submit_learner(client, VIEW, current=LAST)
    client._request.assert_called_once()
    client.get_raw.assert_not_called()
    prep.close.assert_called_once()


@pytest.mark.parametrize("confirm", [
    confirmation(extra='<input type="checkbox" name="attemptCanBeGraded">'),  # a preview's page
    confirmation(secure_browser="1"),
    confirmation(referrer="OTHER_SESSION"),
    confirmation().replace(b'primary="">Submit Quiz', b'disabled>Submit Quiz'),
    confirmation().replace(b'primary="">Submit Quiz', b'primary="" aria-disabled="true">Submit Quiz'),
    confirmation().replace(b'primary="">Submit Quiz', b'primary="" class="d2l-hidden">Submit Quiz'),
    confirmation().replace(b'<button type="button" primary="">Submit Quiz</button>',
                           b'<fieldset disabled><button type="button" primary="">Submit Quiz</button></fieldset>'),
    confirmation().replace(b'primary="">Submit Quiz</button>',
                           b'disabled>Submit Quiz</button><button type="button" hidden>Submit Quiz</button>'),
    confirmation(count=1),
])
def test_unexpected_confirmation_page_is_unknown_before_the_final_request(confirm):
    client, _, _ = submit_client(confirm=confirm)
    with pytest.raises(LearnerSubmitUnknownError):
        submit_learner(client, VIEW, current=LAST)
    client._request.assert_called_once()


@pytest.mark.parametrize("change", [
    {"result": "parent.QuizDone(20,30,'1','0','0','gotoSv','')"},  # a preview
    {"result": "parent.QuizDone(20,31,'0','0','0','gotoSv','')"},
    {"result": "parent.QuizDone(20,30,'0','0','0','gotoSv','');evil()"},
    {"receipt": b"<h2>Quiz Submission Confirmation</h2>"},
    {"receipt": RECEIPT + b"<p>This attempt is still in progress.</p>"},
    {"receipt": RECEIPT + b"<p>This attempt is Still In Progress.</p>"},
    {"submissions": listing(state="<label> (In progress)</label>")},
    {"submissions": listing(state="<label> (IN PROGRESS)</label>")},
    {"submissions": listing(attempt_id=31)},
    {"submissions": listing(rows=2)},
])
def test_unverified_submission_is_unknown_and_not_retried(change):
    client, _, rpc = submit_client(**change)
    with pytest.raises(LearnerSubmitUnknownError):
        submit_learner(client, VIEW, current=LAST)
    assert client._request.call_count == 2
    rpc.close.assert_called_once()


def test_submit_page_save_rejected_by_the_server_is_unknown_before_the_final_request():
    client, prep, _ = submit_client()
    prep.status_code = 500
    with pytest.raises(LearnerSubmitUnknownError):
        submit_learner(client, VIEW, current=LAST)
    client._request.assert_called_once()
    client.get_raw.assert_not_called()


def test_submit_auth_expiry_after_the_page_save_is_unknown():
    client, prep, _ = submit_client()
    client._request = Mock(side_effect=[prep, SessionExpiredError("session expired")])
    with pytest.raises(LearnerSubmitUnknownError):
        submit_learner(client, VIEW, current=LAST)
    assert client._request.call_count == 2


@pytest.mark.parametrize(("grade", "points", "percent"), [
    ("<label>6</label><label> / </label><label>25</label><label> - </label><label>24 %</label>", (6.0, 25.0), 24.0),
    # Points hidden, percentage shown, or the percentage alone; a decimal comma.
    ("<label>/ </label><label> - </label><label>88.89 %</label>", (None, None), 88.89),
    ("<label>88.89 %</label>", (None, None), 88.89), ("<label>88,89 %</label>", (None, None), 88.89),
    # Hidden or unpublished: a blank cell, no grade cell at all, "Pending Evaluation".
    ("", (None, None), None), (None, (None, None), None), ("<label>Pending Evaluation</label>", (None, None), None),
    # Extra text after the percentage: the points still parse, the percentage is not guessed.
    ("<label>6</label><label> / </label><label>25</label><label> - </label><label>24 %</label><label>(rescored)</label>",
     (6.0, 25.0), None),
    # A signed or stray-dash value is not read as a percentage.
    ("<label> - </label><label>-5 %</label>", (None, None), None), ("<label>-5 %</label>", (None, None), None),
    ("<label>-</label><label>5 %</label>", (None, None), None),
])
def test_verification_reports_the_grade_only_as_the_list_shows_it(grade, points, percent):
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(listing(grade=grade), {}), (RECEIPT, {})])
    result = verify_learner_submission(client, **IDENTITY)
    assert result["submitted"] and (result["score"], result["out_of"]) == points and result["percent"] == percent
    # Without an attempt frame open, the receipt is reloaded on its own.
    receipt = client.get_raw.call_args
    assert (receipt.args[0], receipt.kwargs["headers"]) == (RECEIPT_PATH, document(reload=True))


def test_a_later_verification_reloads_the_receipt_from_the_attempt_frame():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(listing(), {}), (RECEIPT, {})])
    assert verify_learner_submission(client, **IDENTITY, frame_page=1)["submitted"]
    receipt = client.get_raw.call_args
    assert (receipt.args[0], receipt.kwargs["headers"]) == (RECEIPT_PATH, document(FRAME1, reload=True))


@pytest.mark.parametrize("submissions", [
    listing(state="<label> (In progress)</label>"), listing(state="<label> (IN PROGRESS)</label>"),
    # An attempt Brightspace is still submitting after its time ran out.
    listing(grade="<label>Auto-grading in progress</label>"),
])
def test_verification_reports_an_attempt_in_progress_without_reading_its_receipt(submissions):
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(submissions, {})])
    with pytest.raises(LearnerNotSubmittedError, match="still in progress"):
        verify_learner_submission(client, **IDENTITY)
    assert "quiz_submissions.d2l?" in client.get_raw.call_args.args[0]


def test_verification_does_not_mask_an_expired_session():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(SessionExpiredError):
        verify_learner_submission(client, **IDENTITY)
