"""A learner's own attempt: start, save every answer of a page, move on, submit and verify.

Fixtures follow pages observed on a public Brightspace site and a disposable
Brightspace sandbox, with synthetic tokens only.
"""

from __future__ import annotations

import json
import re
from email.utils import formatdate
from unittest.mock import Mock, patch

import pytest

from lighthouse_cli.api import LighthouseClient, NetworkError, SessionExpiredError
from lighthouse_cli.quiz_attempt_page import (
    REFUSE_ANSWER_SHAPE,
    REFUSE_BLANK_TEXT,
    REFUSE_LEARNER_LAST_PAGE,
    REFUSE_LEARNER_NOT_LAST_PAGE,
    REFUSE_LEARNER_NOT_ON_PAGE,
    REFUSE_LEARNER_UNANSWERED,
    REFUSE_LEARNER_UNSUPPORTED,
    REFUSE_NO_ANSWERS,
    REFUSE_NOT_A_CHOICE,
    PreviewPageError,
    PreviewRefusedError,
    hidden_form,
    parse_learner_page,
    rpc_script,
    unanswered_questions,
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
    REFUSE_START_PROTECTION,
    REFUSE_START_ROLE,
    REFUSE_START_UNAVAILABLE,
    LearnerAdvanceUnknownError,
    LearnerSaveUnknownError,
    LearnerStartUnknownError,
    LearnerTimer,
    advance_learner,
    parse_learner_summary,
    parse_learner_timer,
    read_learner_summary,
    read_learner_timer,
    save_learner_answers,
    start_learner,
)
from lighthouse_cli.request_protection import form_protection_from_homepage
from tests.test_quiz_attempt_page import (
    LEARNER_BUTTONS,
    blank,
    bootstrap,
    checkboxes,
    html,
    learner_question,
    radios,
    segment,
)

NEXT = '<button type="button" class="d2l-button">Next Page</button>'
IDENTITY = {"course_id": 10, "quiz_id": 20, "attempt_id": 30}


def questions(*, page: int = 1, sc: tuple[str, ...] = (), ms: tuple[str, ...] = (),
              fb: tuple[str, str] = ("", ""), saved: tuple[str, str, str] = ("False", "False", "False")) -> str:
    """Single-choice 101, multi-select 102 and a two-blank question 103."""
    options = segment("Two plus two is") + blank(3, "601", fb[0]) + segment("and three plus three is") + blank(3, "602", fb[1])
    return (learner_question(1, radios(1, ["o1", "o2"], checked=sc), saved=saved[0], page=page)
            + learner_question(2, checkboxes(2, ["o11", "o12", "o13"], checked=ms), saved=saved[1], page=page)
            + learner_question(3, options, prompt="", saved=saved[2], page=page))


def body(content: str, *, page: int = 1, extra: str = LEARNER_BUTTONS, protected: bool = True) -> bytes:
    # A live attempt page embeds the session's form protection.
    return html(content, page=page, extra=extra, isprv="") + (bootstrap() if protected else b"")


def parse(content: bytes, page: int = 1):
    return parse_learner_page(content, **IDENTITY, page=page)


ANSWERED = questions(sc=("o2",), ms=("o11", "o13"), fb=("four", "six"), saved=("True", "True", "True"))


def answer_fields(fields: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in fields.items() if key.startswith("tAtom")}


# -- one form per page ----------------------------------------------------------


def test_save_all_sends_every_answer_of_the_page_in_one_form():
    page = parse(body(questions()))
    values = page.intended({101: "o2", 102: ["o13", "o11"], 103: ["four", "six"]})
    assert values == {101: ("o2",), 102: ("o11", "o13"), 103: ("four", "six")}
    fields = page.save_fields(values, page._protection)
    assert answer_fields(fields) == {
        "tAtom201_300": "o2", "tAtom202_300_o11": "1", "tAtom202_300_o13": "1",
        "tAtom203_300_601": "four", "tAtom203_300_602": "six",
    }
    assert (fields["d2l_action"], fields["d2l_actionparam"]) == ("Update", "1,1")
    assert fields["d2l_referrer"] == "SESSION_SENTINEL" and fields["d2l_hitCode"]
    assert fields["ou"] == "10" and fields["ai"] == "30"
    assert "SESSION_SENTINEL" not in repr(page)


def test_unmentioned_questions_keep_their_current_answers():
    page = parse(body(questions(sc=("o1",), ms=("o12",), fb=("four", ""))))
    fields = page.save_fields(page.intended({103: ["four", "six"]}), page._protection)
    # Unchecked options are left out, as the browser does.
    assert answer_fields(fields) == {
        "tAtom201_300": "o1", "tAtom202_300_o12": "1", "tAtom203_300_601": "four", "tAtom203_300_602": "six",
    }


def test_answers_can_be_cleared_except_a_chosen_radio():
    page = parse(body(questions(ms=("o12",), fb=("four", "six"))))
    fields = page.save_fields(page.intended({102: [], 103: ["", ""]}), page._protection)
    assert answer_fields(fields) == {"tAtom203_300_601": "", "tAtom203_300_602": ""}
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
        page.finish_fields(page._protection)


def test_form_needs_the_page_session_protection():
    page = parse(body(questions()))
    other = form_protection_from_homepage(bootstrap().replace(b"SESSION_SENTINEL", b"OTHER_SESSION"))
    with pytest.raises(PreviewPageError):
        page.save_fields(page.intended({101: "o1"}), other)
    with pytest.raises(PreviewPageError):
        page.save_fields({101: ("o1",)}, page._protection)  # not every question of the page


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


def test_next_needs_a_rendered_next_control_and_answered_questions():
    last = parse(body(ANSWERED))
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_LAST_PAGE)):
        last.advance_fields(last._protection)
    answered = parse(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT))
    assert answered.advance_fields(answered._protection)["d2l_actionparam"] == "2,2,1"
    open_page = parse(body(questions(sc=("o1",)), extra=LEARNER_BUTTONS + NEXT))
    assert open_page.unanswered() == [102, 103]
    # On a forward-only quiz an empty blank could not be filled in later.
    partial = parse(body(questions(sc=("o1",), ms=("o11",), fb=("four", "")), extra=LEARNER_BUTTONS + NEXT))
    assert partial.unanswered() == [103]
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_UNANSWERED)):
        open_page.advance_fields(open_page._protection)
    fields = open_page.advance_fields(open_page._protection, allow_unanswered=True)
    assert answer_fields(fields) == {"tAtom201_300": "o1", "tAtom203_300_601": "", "tAtom203_300_602": ""}


def test_finish_is_only_from_the_last_page():
    page = parse(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT))
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_NOT_LAST_PAGE)):
        page.finish_fields(page._protection)
    last = parse(body(ANSWERED))
    assert last.finish_fields(last._protection)["d2l_actionparam"] == "5,1"


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
            password: str = "false", button: str | None = "Start Quiz!", progress: str = "",
            fields: dict[str, str] | None = None, protected: bool = True, extra: str = "") -> bytes:
    hidden = {"d2l_action": "", "d2l_actionparam": "", "d2l_hitCode": "", "hps": "", "drc": "0",
              "LockDownBrowserUrl": "0", "d2l_referrer": "SESSION_SENTINEL", **(fields or {})}
    inputs = "".join(f'<input type="hidden" name="{key}" value="{value}">' for key, value in hidden.items())
    script = (f"<script>var isImpersonatingRole = {impersonating};\nvar canTakeQuiz = {can_take};\n"
              f"var startQuiz = {start};\nvar continueQuiz = {resume};\nvar hasPass = {password};</script>")
    # The start button is rendered outside the form.
    action = f'<button type="button" primary="" class="d2l-button">{button}</button>' if button else ""
    return (f"<h1>Summary - Quiz</h1><p>{progress}</p>{script}{action}<form>{inputs}{extra}</form>").encode() + (
        bootstrap() if protected else b"")


IN_PROGRESS = {"start": "false", "resume": "true", "button": "Continue Quiz...", "progress": "Completed - 0 (Attempt 2 in progress)"}


def test_summary_reports_start_and_continue_state():
    fresh = parse_learner_summary(summary(), course_id=10, quiz_id=20)
    assert fresh.public_data() == {"course_id": 10, "quiz_id": 20, "can_start": True, "can_continue": False,
                                   "attempt_in_progress": None}
    assert "SESSION_SENTINEL" not in repr(fresh)
    resumed = parse_learner_summary(summary(**IN_PROGRESS), course_id=10, quiz_id=20)
    assert (resumed.can_start, resumed.can_continue, resumed.attempt_in_progress) == (False, True, 2)
    closed = parse_learner_summary(summary(can_take="false", button=None), course_id=10, quiz_id=20)
    assert not closed.can_start and not closed.can_continue


@pytest.mark.parametrize("page", [
    summary(resume="true"),  # in progress without its attempt
    summary(progress="(Attempt 2 in progress)"),
    summary(progress="(Attempt 1 in progress) (Attempt 2 in progress)", resume="true"),
    summary(extra='<script>var startQuiz = false;</script>'),
    summary(start="maybe"),
    summary(extra='<button type="button">Continue Quiz...</button>'),
])
def test_inconsistent_summary_is_rejected(page):
    with pytest.raises(PreviewPageError):
        parse_learner_summary(page, course_id=10, quiz_id=20)


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

START_PROCESS = "/d2l/lms/quizzing/user/attempt/quiz_start_process_auto.d2l?ou=10&qi=20&isprv=&fromQB=0&inProgress={}"


def start_client(summary_page: bytes, *, resume: bool = False, script: bytes = b"<script>\nparent.GoToAttemptQuizAuto( 30,1,0 );\n</script>",
                 readback: object = None, location: str | None = None):
    client = LighthouseClient(read_only_auth=True)
    process = START_PROCESS.format(int(resume))
    root = process.replace("quiz_start_process_auto", "quiz_start_frame_auto")
    inner = process.replace("quiz_start_process_auto", "quiz_start_iframe_2_auto")
    client._request = Mock(return_value=Mock(status_code=302, headers={"Location": location or root}))
    client.get_raw = Mock(side_effect=[
        (summary_page, {}),
        (f'<iframe src="{inner}"></iframe>'.encode(), {}),
        (f'<iframe name="hiddenFrame" src="{process}"></iframe>'.encode(), {}),
        (script, {}),
        readback if readback is not None else (body(questions()), {}),
    ])
    return client


def test_start_posts_the_summary_action_once_and_reads_the_first_page():
    client = start_client(summary())
    seen = []
    page = start_learner(client, **{k: IDENTITY[k] for k in ("course_id", "quiz_id")},
                         on_identity=lambda attempt_id, page: seen.append((attempt_id, page, client.get_raw.call_count)))
    assert (page.attempt_id, page.page) == (30, 1)
    assert seen == [(30, 1, 4)]  # before the page readback
    client._request.assert_called_once()
    call = client._request.call_args
    assert call.args[0] == "POST" and call.args[1].endswith("quiz_summary.d2l?ou=10&qi=20&cfql=0&inProgress=false")
    fields = {key: value for key, (_, value) in call.kwargs["files"]}
    assert (fields["d2l_action"], fields["d2l_actionparam"]) == ("Custom", "1")
    assert fields["d2l_referrer"] == "SESSION_SENTINEL" and fields["d2l_hitCode"]
    assert client.get_raw.call_args_list[3].kwargs["_replay_safe"] is False
    assert "isprv=&" in client.get_raw.call_args_list[4].args[0]


def test_continue_reopens_the_attempt_in_progress():
    client = start_client(summary(**IN_PROGRESS), resume=True)
    page = start_learner(client, course_id=10, quiz_id=20, continue_only=True)
    assert page.attempt_id == 30
    assert client._request.call_args.args[1].endswith("&inProgress=true")


def test_a_summary_just_read_is_not_requested_again():
    client = start_client(summary())
    read = read_learner_summary(client, course_id=10, quiz_id=20)
    page = start_learner(client, course_id=10, quiz_id=20, summary=read)
    assert page.attempt_id == 30
    assert client._request.call_count == 1 and client.get_raw.call_count == 5
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
])
def test_start_refusals_send_nothing(page, kwargs, message):
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(return_value=(page, {}))
    client._request = Mock()
    with pytest.raises(PreviewRefusedError, match=re.escape(message)):
        start_learner(client, course_id=10, quiz_id=20, **kwargs)
    client._request.assert_not_called()


@pytest.mark.parametrize("change", [
    {"location": START_PROCESS.format(1).replace("quiz_start_process_auto", "quiz_start_frame_auto")},
    {"location": START_PROCESS.format(0).replace("quiz_start_process_auto", "quiz_start_frame_auto").replace("isprv=", "isprv=1")},
    {"script": b'<script>const text="parent.GoToAttemptQuizAuto(30,1,0)";</script>'},
])
def test_unexpected_start_chain_is_unknown_and_not_retried(change):
    client = start_client(summary(), **change)
    with pytest.raises(LearnerStartUnknownError):
        start_learner(client, course_id=10, quiz_id=20)
    client._request.assert_called_once()


def test_a_new_attempt_must_open_on_its_first_page():
    client = start_client(summary(), script=b"<script>\nparent.GoToAttemptQuizAuto( 30,2,0 );\n</script>")
    with pytest.raises(LearnerStartUnknownError) as exc_info:
        start_learner(client, course_id=10, quiz_id=20)
    assert (exc_info.value.attempt_id, exc_info.value.page) == (30, None)
    assert client.get_raw.call_count == 4  # page 2 is never requested


def test_continue_opens_the_page_the_server_names():
    client = start_client(summary(**IN_PROGRESS), resume=True, script=b"<script>\nparent.GoToAttemptQuizAuto( 30,3,0 );\n</script>",
                          readback=(body(questions(page=3), page=3), {}))
    assert start_learner(client, course_id=10, quiz_id=20, continue_only=True).page == 3


def test_start_readback_failure_is_unknown_with_the_attempt_identity():
    client = start_client(summary(), readback=SessionExpiredError("session expired"))
    with pytest.raises(LearnerStartUnknownError) as exc_info:
        start_learner(client, course_id=10, quiz_id=20)
    assert (exc_info.value.attempt_id, exc_info.value.page) == (30, 1)
    client._request.assert_called_once()


def test_start_identity_callback_failure_is_unknown_with_identity():
    client = start_client(summary())
    with pytest.raises(LearnerStartUnknownError) as exc_info:
        start_learner(client, course_id=10, quiz_id=20, on_identity=Mock(side_effect=OSError("disk full")))
    assert (exc_info.value.attempt_id, exc_info.value.page) == (30, 1)
    assert client.get_raw.call_count == 4  # no readback after a failed seal
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


def test_start_post_auth_expiry_is_unknown_after_dispatch():
    client = start_client(summary())
    client._request = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(LearnerStartUnknownError):
        start_learner(client, course_id=10, quiz_id=20)
    client._request.assert_called_once()


def test_start_without_a_redirect_is_unknown():
    client = start_client(summary())
    client._request = Mock(return_value=Mock(status_code=200, headers={}))
    with pytest.raises(LearnerStartUnknownError):
        start_learner(client, course_id=10, quiz_id=20)
    client.get_raw.assert_called_once()


# -- save and next ----------------------------------------------------------------


def write_client(*pages: object, status: int = 200):
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[page if isinstance(page, BaseException) else (page, {}) for page in pages])
    response = Mock(status_code=status)
    client._request = Mock(return_value=response)
    return client, response


def test_save_posts_once_and_returns_the_verified_readback():
    client, response = write_client(body(questions()), body(ANSWERED))
    result = save_learner_answers(client, **IDENTITY, page=1, answers={101: "o2", 102: ["o11", "o13"], 103: ["four", "six"]})
    assert result.questions[2]["blanks"][1]["value"] == "six"
    client._request.assert_called_once()
    call = client._request.call_args
    assert call.args[1].endswith("/d2l/lms/quizzing/user/attempt/quiz_attempt_save_auto.d2l?d2l_body_type=3&ou=10&fromQB=0")
    assert "isprv=&pg=1&qi=20&ai=30" in call.kwargs["headers"]["Referer"]
    fields = {key: value for key, (_, value) in call.kwargs["files"]}
    assert fields["d2l_actionparam"] == "1,1" and fields["tAtom203_300_602"] == "six"
    response.close.assert_called_once()
    assert client.get_raw.call_count == 2


def test_save_from_the_previous_readback_skips_the_page_read():
    current = parse(body(questions()))
    client, _ = write_client(body(ANSWERED))
    save_learner_answers(client, **IDENTITY, page=1, answers={101: "o2", 102: ["o11", "o13"], 103: ["four", "six"]},
                         current=current)
    client.get_raw.assert_called_once()
    client._request.assert_called_once()


def test_later_pages_are_never_read_from_a_bare_page_number():
    # Reading past the last page breaks the attempt, so pages after the
    # first come from a verified readback.
    client, _ = write_client()
    for call in (lambda: save_learner_answers(client, **IDENTITY, page=2, answers={101: "o1"}),
                 lambda: advance_learner(client, **IDENTITY, page=2),
                 lambda: submit_learner(client, **IDENTITY, page=2)):
        with pytest.raises(ValueError):
            call()
    client.get_raw.assert_not_called()
    client._request.assert_not_called()


def test_save_from_another_attempt_page_is_rejected():
    client, _ = write_client()
    other = parse_learner_page(body(questions()).replace(b'name="ai" type="hidden" value="30"', b'name="ai" type="hidden" value="31"'),
                               course_id=10, quiz_id=20, attempt_id=31, page=1)
    with pytest.raises(ValueError):
        save_learner_answers(client, **IDENTITY, page=1, answers={101: "o1"}, current=other)
    client._request.assert_not_called()


@pytest.mark.parametrize("answers, page, message", [
    ({}, body(questions()), REFUSE_NO_ANSWERS),
    ({101: "o9"}, body(questions()), REFUSE_NOT_A_CHOICE),
    ({101: "o1"}, body(questions(), protected=False), REFUSE_PAGE_PROTECTION),
])
def test_save_refusals_send_nothing(answers, page, message):
    client, _ = write_client(page)
    with pytest.raises(PreviewRefusedError, match=re.escape(message)):
        save_learner_answers(client, **IDENTITY, page=1, answers=answers)
    client._request.assert_not_called()


@pytest.mark.parametrize("readback", [
    body(questions()),  # 200 without the answers persisted
    body(questions(sc=("o2",), ms=("o11", "o13"), fb=("four", "six"))),  # not marked saved
    SessionExpiredError("session expired"),
])
def test_unverified_save_is_unknown_and_not_retried(readback):
    client, _ = write_client(body(questions()), readback)
    with pytest.raises(LearnerSaveUnknownError):
        save_learner_answers(client, **IDENTITY, page=1, answers={101: "o2", 102: ["o11", "o13"], 103: ["four", "six"]})
    client._request.assert_called_once()


def test_save_rejected_by_the_server_is_unknown_and_not_retried():
    client, response = write_client(body(questions()), status=500)
    with pytest.raises(LearnerSaveUnknownError):
        save_learner_answers(client, **IDENTITY, page=1, answers={101: "o1"})
    client._request.assert_called_once()
    client.get_raw.assert_called_once()  # no readback of a refused write
    response.close.assert_called_once()


def test_save_post_auth_expiry_is_unknown_after_dispatch():
    client, _ = write_client(body(questions()))
    client._request = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(LearnerSaveUnknownError):
        save_learner_answers(client, **IDENTITY, page=1, answers={101: "o1"})
    client._request.assert_called_once()


def test_next_posts_the_page_and_reads_the_next_one():
    client, response = write_client(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT), body(questions(page=2), page=2))
    page = advance_learner(client, **IDENTITY, page=1)
    assert page.page == 2
    call = client._request.call_args
    assert call.args[1].endswith("quiz_attempt_save_auto.d2l?cfql=0&fromQB=0&d2l_body_type=3&ou=10")
    fields = {key: value for key, (_, value) in call.kwargs["files"]}
    assert fields["d2l_actionparam"] == "2,2,1" and fields["tAtom201_300"] == "o2"
    assert "&pg=2&" in client.get_raw.call_args.args[0]
    response.close.assert_called_once()


@pytest.mark.parametrize("page, message", [
    (body(ANSWERED), REFUSE_LEARNER_LAST_PAGE),
    (body(questions(), extra=LEARNER_BUTTONS + NEXT), REFUSE_LEARNER_UNANSWERED),
])
def test_next_refusals_send_nothing(page, message):
    # Requesting a page past the last one permanently breaks an attempt.
    client, _ = write_client(page)
    with pytest.raises(PreviewRefusedError, match=re.escape(message)):
        advance_learner(client, **IDENTITY, page=1)
    client._request.assert_not_called()


def test_next_with_unanswered_questions_needs_explicit_permission():
    client, _ = write_client(body(questions(), extra=LEARNER_BUTTONS + NEXT), body(questions(page=2), page=2))
    assert advance_learner(client, **IDENTITY, page=1, allow_unanswered=True).page == 2
    client._request.assert_called_once()


@pytest.mark.parametrize("readback", [SessionExpiredError("session expired"), body(questions())])
def test_unverified_next_is_unknown_and_not_retried(readback):
    client, _ = write_client(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT), readback)
    with pytest.raises(LearnerAdvanceUnknownError):
        advance_learner(client, **IDENTITY, page=1)
    client._request.assert_called_once()


def test_next_rejected_by_the_server_is_unknown_and_never_reads_the_next_page():
    client, _ = write_client(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT), status=500)
    with pytest.raises(LearnerAdvanceUnknownError):
        advance_learner(client, **IDENTITY, page=1)
    client._request.assert_called_once()
    client.get_raw.assert_called_once()


# -- submit and verify --------------------------------------------------------------


RECEIPT = b"<h1>Quiz</h1><h2>Your work has been saved and submitted</h2><p>Written: Oct 3, 2026</p>"


def listing(*, attempt_id: int = 30, state: str = "", grade: str = "<label>6</label><label> / </label><label>25</label><label> - </label><label>24 %</label>",
            rows: int = 1) -> bytes:
    href = (f"/d2l/lms/quizzing/user/quiz_submissions_attempt.d2l?isprv=&amp;qi=20&amp;ai={attempt_id}&amp;isInPopup=0"
            "&amp;cfql=0&amp;fromQB=0&amp;fromSubmissionsList=1&amp;ou=10")
    row = (f'<tr><td><a class="d2l-link" href="{href}">Attempt 1</a>{state}</td>'
           f'<td class="d_gn"><div class="dco d2l-grades-score">{grade}</div></td></tr>')
    return f"<table><tr><th>Attempt</th><th>Grade</th></tr>{row * rows}</table>".encode()


def submit_client(*, confirm: bytes | None = None, result: str = "parent.QuizDone(20,30,'0','0','0','gotoSv','')",
                  receipt: bytes = RECEIPT, submissions: bytes | None = None):
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(confirm or confirmation(), {}), (submissions or listing(), {}), (receipt, {})])
    prep = Mock(status_code=200)
    rpc = Mock(status_code=200)
    rpc.iter_content.return_value = [reply(result)]
    client._request = Mock(side_effect=[prep, rpc])
    return client, prep, rpc


LAST = parse(body(ANSWERED))


def test_submit_saves_confirms_and_verifies_the_receipt_and_list_row():
    client, prep, rpc = submit_client()
    result = submit_learner(client, **IDENTITY, page=1, current=LAST)
    assert result == {"mode": "learner", "course_id": 10, "quiz_id": 20, "attempt_id": 30, "submitted": True,
                      "attempt_number": 1, "score": 6.0, "out_of": 25.0}
    save, final = client._request.call_args_list
    assert save.args[1].endswith("quiz_attempt_save_auto.d2l?dnb=0&cfql=0&fromQB=0&d2l_body_type=3&ou=10")
    assert {key: value for key, (_, value) in save.kwargs["files"]}["d2l_actionparam"] == "5,1"
    confirm_path = client.get_raw.call_args_list[0].args[0]
    assert "quiz_confirm_submit_auto.d2l?qi=20&ai=30&isprv=&" in confirm_path and "&btlp=1&" in confirm_path
    # isPreview false, can be graded, Boolean("False") as the browser sends it.
    assert final.kwargs["data"]["params"] == (
        '{"param1":"20","param2":"30","param3":false,"param4":true,"param5":true,"param6":false,"param7":""}')
    assert final.kwargs["data"]["d2l_rf"] == "ProcessQuizSubmission"
    assert "isprv=&" in final.args[1] and "&pg=1&" in final.args[1]
    assert "isprv=0" in client.get_raw.call_args_list[2].args[0]
    prep.close.assert_called_once()
    rpc.close.assert_called_once()


def test_unanswered_questions_stop_the_submission_before_the_final_request():
    client, _, _ = submit_client(confirm=confirmation(unanswered=((1, 102, 2),)))
    with pytest.raises(LearnerUnansweredError) as exc_info:
        submit_learner(client, **IDENTITY, page=1, current=LAST)
    assert exc_info.value.questions == [{"page": 1, "question_id": 102, "number": 2}]
    assert not isinstance(exc_info.value, PreviewRefusedError)  # the page save was sent
    client._request.assert_called_once()  # the page save only


def test_unanswered_questions_can_be_submitted_explicitly():
    client, _, _ = submit_client(confirm=confirmation(unanswered=((1, 102, 2),)))
    assert submit_learner(client, **IDENTITY, page=1, current=LAST, allow_unanswered=True)["submitted"]
    assert client._request.call_count == 2


def test_submit_reads_the_last_page_when_no_readback_is_given():
    client, _, _ = submit_client()
    client.get_raw.side_effect = [(body(ANSWERED), {}), *client.get_raw.side_effect]
    assert submit_learner(client, **IDENTITY, page=1)["submitted"]
    assert "isprv=&pg=1&qi=20&ai=30" in client.get_raw.call_args_list[0].args[0]
    assert client._request.call_count == 2


def test_submit_refuses_before_any_write_when_not_on_the_last_page():
    client, _, _ = submit_client()
    with pytest.raises(PreviewRefusedError, match=re.escape(REFUSE_LEARNER_NOT_LAST_PAGE)):
        submit_learner(client, **IDENTITY, page=1, current=parse(body(ANSWERED, extra=LEARNER_BUTTONS + NEXT)))
    client._request.assert_not_called()


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
        submit_learner(client, **IDENTITY, page=1, current=LAST)
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
        submit_learner(client, **IDENTITY, page=1, current=LAST)
    assert client._request.call_count == 2
    rpc.close.assert_called_once()


def test_submit_page_save_rejected_by_the_server_is_unknown_before_the_final_request():
    client, prep, _ = submit_client()
    prep.status_code = 500
    with pytest.raises(LearnerSubmitUnknownError):
        submit_learner(client, **IDENTITY, page=1, current=LAST)
    client._request.assert_called_once()
    client.get_raw.assert_not_called()


def test_submit_auth_expiry_after_the_page_save_is_unknown():
    client, prep, _ = submit_client()
    client._request = Mock(side_effect=[prep, SessionExpiredError("session expired")])
    with pytest.raises(LearnerSubmitUnknownError):
        submit_learner(client, **IDENTITY, page=1, current=LAST)
    assert client._request.call_count == 2


def test_verification_reports_no_score_when_the_quiz_hides_it():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(listing(grade=""), {}), (RECEIPT, {})])
    result = verify_learner_submission(client, **IDENTITY)
    assert result["submitted"] and result["score"] is None and result["out_of"] is None


@pytest.mark.parametrize("state", ["<label> (In progress)</label>", "<label> (IN PROGRESS)</label>"])
def test_verification_reports_an_attempt_in_progress_without_reading_its_receipt(state):
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(listing(state=state), {})])
    with pytest.raises(LearnerNotSubmittedError, match="still in progress"):
        verify_learner_submission(client, **IDENTITY)
    assert "quiz_submissions.d2l?" in client.get_raw.call_args.args[0]


def test_verification_does_not_mask_an_expired_session():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(SessionExpiredError):
        verify_learner_submission(client, **IDENTITY)
