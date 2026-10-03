"""Characterization of the observed preview and learner DOM, using synthetic tokens only."""

from __future__ import annotations

import json
from html import escape
from unittest.mock import Mock

import pytest

from lighthouse_cli.api import LighthouseClient, SessionExpiredError
from lighthouse_cli.quiz_attempt_page import (
    MAX_PAGE_BYTES,
    PreviewPageError,
    PreviewRefusedError,
    _button_present,
    hidden_form,
    parse_learner_page,
    parse_preview_page,
)
from lighthouse_cli.quiz_preview_transport import (
    PreviewAdvanceUnknownError,
    PreviewSaveUnknownError,
    PreviewStartUnknownError,
    advance_current_preview,
    read_server_current_preview,
    save_current_preview_answer,
    start_preview,
)
from lighthouse_cli.request_protection import FormProtection


def metadata(number: int, page: int, saved: str) -> str:
    values = {
        "object-id": str(100 + number), "autosave-question-num": str(number),
        "autosave-page": str(page), "autosave-tid": str(200 + number),
        "autosave-tvid": "300", "autosave-is-saved": saved,
    }
    return "".join(f'<div class="d2l-quiz-question-{key}"><input type="hidden" value="{value}"></div>' for key, value in values.items())


def question(number: int, page: int = 1, *, saved: str = "True", selected: bool = True) -> str:
    checked = "checked" if selected else ""
    return f'''<div class="d2l-quiz-question-autosave-container">{metadata(number, page, saved)}
    <div id="d2l_read_element_{number}">Question {number}: choose true.
    <fieldset><legend>Options</legend><table>
    <tr><td><input type="radio" name="tAtom{200 + number}_300" id="q{number}a" value="401" {checked}></td><td><label for="q{number}a">True</label></td></tr>
    <tr><td><input type="radio" name="tAtom{200 + number}_300" id="q{number}b" value="402"></td><td><label for="q{number}b">False</label></td></tr>
    </table></fieldset></div></div>'''


def html(questions: str, *, page: int = 1, extra: str = "", isprv: str = "1") -> bytes:
    identity = {"ou": "10", "qi": "20", "ai": "30", "pg": str(page), "isprv": isprv, "d2l_referrer": "SESSION_SENTINEL"}
    identity.update({"d2l_controlMap": json.dumps([{"hdn_resp_101": ["z_r1"], "hdn_resp_102": ["z_r2"]}, {}]), "z_r1": "0", "z_r2": "0"})
    fields = "".join(f'<input name="{key}" type="hidden" value="{escape(value, quote=True)}">' for key, value in identity.items())
    return f"<form>{fields}{questions}{extra}</form>".encode()


def parse(body: bytes, page: int = 1):
    return parse_preview_page(body, course_id=10, quiz_id=20, attempt_id=30, page=page)


def test_all_at_once_exposes_only_current_visible_questions():
    page = parse(html(question(1) + question(2)))
    data = page.public_data()
    assert len(data["questions"]) == 2
    assert data["questions"][0]["text"] == "Question 1: choose true."
    assert data["questions"][0]["choices"] == [{"choice_id": 401, "text": "True"}, {"choice_id": 402, "text": "False"}]
    assert page.confirms_answer(101, 401)
    assert not page.confirms_answer(101, 402)
    assert not page.has_next_control
    assert "SESSION_SENTINEL" not in repr(page)
    assert "SESSION_SENTINEL" not in json.dumps(data)


def test_custom_html_block_prompt_without_legacy_id_is_supported():
    body = html(question(1))
    body = body.replace(
        b'<div id="d2l_read_element_1">Question 1: choose true.',
        b'<d2l-html-block html="&lt;p&gt;Synthetic prompt&lt;/p&gt;"></d2l-html-block>',
    )
    body = body.replace(b'<label for="q1a">True</label>', b'<span>True</span>')
    body = body.replace(b'<label for="q1b">False</label>', b'<span>False</span>')
    parsed = parse(body)
    assert parsed.questions[0]["text"] == "Synthetic prompt"
    assert parsed.questions[0]["supported"] is True
    assert parsed.questions[0]["choices"] == [
        {"choice_id": 401, "text": "True"},
        {"choice_id": 402, "text": "False"},
    ]


def test_rich_text_choice_is_never_taken_as_the_prompt():
    # No legacy read-element and no prompt block: the only html block is an
    # answer choice, so the page must fail closed instead of mislabeling it.
    q = question(1).replace('<div id="d2l_read_element_1">Question 1: choose true.', '<div>')
    q = q.replace('<label for="q1a">True</label>', '<label for="q1a"><d2l-html-block html="True"></d2l-html-block></label>')
    with pytest.raises(PreviewPageError):
        parse(html(q))
    prompt = '<d2l-html-block html="&lt;p&gt;Two plus two equals four.&lt;/p&gt;"></d2l-html-block><fieldset>'
    page = parse(html(q.replace("<fieldset>", prompt, 1)))
    assert page.questions[0]["text"] == "Two plus two equals four."


def test_one_way_page_has_next_without_previous():
    page = parse(html(question(1), extra='<button>Next Page</button>'))
    assert len(page.questions) == 1
    assert page.has_next_control
    assert not page.has_previous_control
    final_page = parse(html(question(2, 2), page=2, extra='<button disabled>Next Page</button>'), page=2)
    assert not final_page.has_next_control
    assert not final_page.has_previous_control


def test_id_less_radio_uses_its_own_row_not_a_stray_label():
    # Without an id there is no label[for] to follow. Label lookup must not
    # fall back to "first label lacking a for attribute" (BeautifulSoup's
    # meaning of attrs={"for": None}), which would attach the wrong text.
    body = question(1).replace(' id="q1a"', "").replace(' id="q1b"', "")
    body = body.replace("<fieldset>", "<label>Stray instructions</label><fieldset>")
    page = parse(html(body))
    assert page.public_data()["questions"][0]["choices"] == [
        {"choice_id": 401, "text": "True"},
        {"choice_id": 402, "text": "False"},
    ]


def test_id_less_radio_outside_a_row_fails_closed():
    # No id and no enclosing row: there is no trustworthy label, so the page
    # is rejected rather than guessing from nearby text.
    stray = '<label>Stray instructions</label><input type="radio" name="tAtom201_300" value="403">'
    body = question(1).replace("<fieldset>", stray + "<fieldset>", 1)
    with pytest.raises(PreviewPageError):
        parse(html(body))


@pytest.mark.parametrize("saved", ["False", "unknown", ""])
def test_selected_choice_without_saved_confirmation_is_not_success(saved):
    page = parse(html(question(1, saved=saved)))
    assert not page.confirms_answer(101, 401)


def test_saved_flag_without_selected_choice_is_not_success():
    assert not parse(html(question(1, selected=False))).confirms_answer(101, 401)


@pytest.mark.parametrize("field", ["ou", "qi", "ai", "pg", "isprv"])
def test_wrong_identity_or_real_learner_page_is_rejected(field):
    body = html(question(1))
    before = {"ou": "10", "qi": "20", "ai": "30", "pg": "1", "isprv": "1"}[field]
    body = body.replace(f'name="{field}" type="hidden" value="{before}"'.encode(), f'name="{field}" type="hidden" value="999"'.encode())
    with pytest.raises(PreviewPageError):
        parse(body)


@pytest.mark.parametrize("body", [b"<html>Login</html>", html(question(1)+question(1)), html(question(1), extra='<input type="hidden" name="ai" value="30">'), b"x" * (MAX_PAGE_BYTES + 1)])
def test_incomplete_duplicate_or_oversized_response_is_rejected(body):
    with pytest.raises(PreviewPageError) as exc:
        parse(body)
    assert "SESSION_SENTINEL" not in str(exc.value)


def test_media_is_explicitly_unsupported_instead_of_silently_lost():
    q = question(1).replace("Question 1: choose true.", 'Question 1: <img alt="diagram">')
    page = parse(html(q))
    assert page.questions[0]["kind"] == "unsupported"
    assert not page.confirms_answer(101, 401)


def test_server_html_block_attribute_supplies_question_text():
    q = question(1).replace("Question 1: choose true.", '<d2l-html-block html="&lt;p&gt;Two plus two equals four.&lt;/p&gt;"></d2l-html-block>')
    page = parse(html(q))
    assert page.questions[0]["text"] == "Two plus two equals four."
    assert page.questions[0]["supported"]


def test_media_inside_html_attribute_is_not_silently_ignored():
    q = question(1).replace("Question 1: choose true.", '<d2l-html-block html="&lt;p&gt;Identify:&lt;img src=&quot;diagram.png&quot;&gt;&lt;/p&gt;"></d2l-html-block>')
    assert not parse(html(q)).questions[0]["supported"]


def test_unhydrated_empty_prompt_is_not_actionable():
    q = question(1).replace("Question 1: choose true.", '<d2l-html-block></d2l-html-block>')
    assert not parse(html(q)).questions[0]["supported"]


def test_answer_form_resolves_response_flag_and_preserves_sibling_answer():
    page = parse(html(question(1) + question(2, selected=False)))
    protection = FormProtection("SESSION_SENTINEL", "1234567890")
    first = page.answer_fields(102, 402, protection)
    second = page.answer_fields(102, 402, protection)
    assert first["tAtom201_300"] == "401"
    assert first["tAtom202_300"] == "402"
    assert first["z_r2"] == "1"
    assert first["d2l_action"] == "Update"
    assert first["d2l_actionparam"] == "3,1,202,300,2"
    assert first["d2l_hitCode"] != second["d2l_hitCode"]
    assert "SESSION_SENTINEL" not in repr(protection)


def test_answer_form_rejects_stale_session_or_unknown_choice():
    page = parse(html(question(1)))
    with pytest.raises(PreviewRefusedError, match="not an option"):
        page.answer_fields(101, 999, FormProtection("SESSION_SENTINEL", "123"))
    with pytest.raises(PreviewRefusedError, match="not on the current preview page"):
        page.answer_fields(999, 401, FormProtection("SESSION_SENTINEL", "123"))
    with pytest.raises(PreviewPageError):
        page.answer_fields(101, 401, FormProtection("DIFFERENT_SESSION", "123"))


def test_answer_refusal_for_an_unsupported_question_type():
    q = question(1).replace("Question 1: choose true.", 'Question 1: <img alt="diagram">')
    with pytest.raises(PreviewRefusedError, match="does not support"):
        parse(html(q)).answer_fields(101, 401, FormProtection("SESSION_SENTINEL", "123"))


def test_advance_refusals_are_specific():
    protection = FormProtection("SESSION_SENTINEL", "123")
    unsaved = parse(html(question(1, saved="False"), extra="<button>Next Page</button>"))
    with pytest.raises(PreviewRefusedError, match="Answer and save every question"):
        unsaved.advance_fields(protection)
    last = parse(html(question(1)))
    with pytest.raises(PreviewRefusedError, match="last page"):
        last.advance_fields(protection)


LEARNER_BUTTONS = ('<button type="button" class="d2l-button d2l-hidden">Save All Responses</button>'
                   '<button type="button" primary="" class="d2l-button">Submit Quiz</button>')


def learner_question(number: int, options: str, *, prompt: str = "Pick the right answer.", saved: str = "False") -> str:
    # Learner pages put the prompt in one custom HTML block outside the options.
    lead = f'<div><d2l-html-block html="{escape(prompt, quote=True)}"></d2l-html-block></div>' if prompt else ""
    return (f'<div class="d2l-quiz-question-autosave-container">{metadata(number, 1, saved)}{lead}'
            f'<fieldset><legend>Question {number} options:</legend>{options}</fieldset></div>')


def radios(number: int, values: list[str], *, checked: tuple[str, ...] = ()) -> str:
    group = f"tAtom{200 + number}_300"
    return "<table>" + "".join(
        f'<tr><td><input type="radio" name="{group}" id="{group}_{value}_id" value="{value}"'
        f'{" checked" if value in checked else ""}></td>'
        f'<td><d2l-html-block html="&lt;p&gt;Answer {value}&lt;/p&gt;"></d2l-html-block></td></tr>'
        for value in values) + "</table>"


def checkboxes(number: int, options: list[str], *, checked: tuple[str, ...] = ()) -> str:
    group = f"tAtom{200 + number}_300"
    return "<table>" + "".join(
        f'<tr><td><input type="checkbox" name="{group}_{option}" id="{group}_{option}_id" value="1"'
        f'{" checked" if option in checked else ""}></td>'
        f'<td><label for="{group}_{option}_id">Option {option}</label></td></tr>'
        for option in options) + "</table>"


def segment(text: str) -> str:
    return f'<d2l-html-block html="{escape(text, quote=True)}" inline=""></d2l-html-block>'


def blank(number: int, blank_id: str, value: str = "") -> str:
    return f'<input type="text" name="tAtom{200 + number}_300_{blank_id}" title="Answer" value="{escape(value, quote=True)}">'


def learner(questions: str, *, extra: str = LEARNER_BUTTONS, isprv: str = ""):
    return parse_learner_page(html(questions, extra=extra, isprv=isprv), course_id=10, quiz_id=20, attempt_id=30, page=1)


def test_learner_multiple_choice_ids_are_opaque_strings():
    page = learner(learner_question(1, radios(1, ["o4330", "o89", "o7"], checked=("o89",)), saved="True"))
    data = page.public_data()
    assert data["mode"] == "learner"
    assert data["questions"] == [{
        "question_id": 101, "number": 1, "text": "Pick the right answer.",
        "kind": "single-choice", "supported": True,
        "choices": [{"choice_id": "o4330", "text": "Answer o4330"}, {"choice_id": "o89", "text": "Answer o89"},
                    {"choice_id": "o7", "text": "Answer o7"}],
        "selected_choice_ids": ["o89"], "blanks": [], "saved": True,
    }]
    assert not page.has_next_control and not page.has_previous_control
    assert "SESSION_SENTINEL" not in repr(page)
    assert "SESSION_SENTINEL" not in json.dumps(data)


def test_learner_true_false_numeric_ids_stay_strings():
    q = learner(learner_question(2, radios(2, ["501", "502"]))).questions[0]
    assert q["kind"] == "single-choice"
    assert [choice["choice_id"] for choice in q["choices"]] == ["501", "502"]
    assert q["selected_choice_ids"] == []
    assert q["saved"] is False


@pytest.mark.parametrize("checked", [(), ("o12",), ("o11", "o13")])
def test_learner_multi_select_option_id_is_the_name_suffix(checked):
    q = learner(learner_question(4, checkboxes(4, ["o11", "o12", "o13"], checked=checked))).questions[0]
    assert q["kind"] == "multi-select" and q["supported"]
    assert q["choices"] == [{"choice_id": option, "text": f"Option {option}"} for option in ("o11", "o12", "o13")]
    assert q["selected_choice_ids"] == list(checked)
    assert q["blanks"] == []


def test_learner_fill_in_the_blank_numbers_each_blank_in_the_sentence():
    options = segment("Two plus two is") + blank(3, "601", "four") + segment("and three plus three is") + blank(3, "602") + segment("in total.")
    q = learner(learner_question(3, options, prompt="")).questions[0]
    assert q["kind"] == "fill-blank" and q["supported"]
    assert q["text"] == "Two plus two is (blank 1) and three plus three is (blank 2) in total."
    assert q["blanks"] == [{"blank_id": "601", "number": 1, "value": "four"}, {"blank_id": "602", "number": 2, "value": ""}]
    assert q["choices"] == [] and q["selected_choice_ids"] == []
    # Only blanks carry their text in the options; a choice question still needs a prompt.
    with pytest.raises(PreviewPageError):
        learner(learner_question(1, radios(1, ["o1", "o2"]), prompt=""))


def test_learner_hidden_buttons_are_not_controls():
    # "Save All Responses" is always in the markup but hidden with d2l-hidden,
    # so matching on its text alone would wrongly report a visible control.
    body = html(learner_question(1, radios(1, ["o1", "o2"])), extra=LEARNER_BUTTONS, isprv="")
    form = hidden_form(body)[0]
    assert _button_present(form, "Save All Responses")
    assert not _button_present(form, "Save All Responses", visible_only=True)
    assert _button_present(form, "Submit Quiz", visible_only=True)
    hidden = '<div class="d2l-hidden"><button type="button">Next Page</button></div><button type="button" hidden>Previous Page</button>'
    page = learner(learner_question(1, radios(1, ["o1", "o2"])), extra=LEARNER_BUTTONS + hidden)
    assert not page.has_next_control and not page.has_previous_control
    styled = '<div style="color: red; DISPLAY: none"><button type="button">Next Page</button></div><button type="button" style="visibility:hidden">Previous Page</button>'
    page = learner(learner_question(1, radios(1, ["o1", "o2"])), extra=LEARNER_BUTTONS + styled)
    assert not page.has_next_control and not page.has_previous_control
    shown = learner(learner_question(1, radios(1, ["o1", "o2"])), extra=LEARNER_BUTTONS + '<button type="button">Next Page</button>')
    assert shown.has_next_control


def test_learner_next_control_follows_the_rendered_buttons():
    # The last page keeps both "Next Page" buttons in the markup, disabled.
    last = '<button type="button" class="d2l-button" id="z_e" disabled>Next Page</button>'
    options = radios(1, ["o1", "o2"])
    assert not learner(learner_question(1, options), extra=LEARNER_BUTTONS + last * 2).has_next_control
    assert learner(learner_question(1, options), extra=LEARNER_BUTTONS + last.replace(" disabled", "")).has_next_control
    fieldset = '<fieldset disabled><button type="button">Next Page</button></fieldset>'
    assert not learner(learner_question(1, options), extra=LEARNER_BUTTONS + fieldset).has_next_control
    assert not parse(html(question(1), extra=fieldset)).has_next_control
    # A button inside a question is its content, not page navigation.
    content = learner_question(1, options).replace("<fieldset>", '<div><button type="button">Next Page</button></div><fieldset>', 1)
    assert not learner(content, extra=LEARNER_BUTTONS + last).has_next_control
    for unusable in ('<template><button type="button">Next Page</button></template>',
                     '<button type="button" inert>Next Page</button>',
                     '<div inert><button type="button">Next Page</button></div>'):
        assert not learner(learner_question(1, options), extra=LEARNER_BUTTONS + unusable).has_next_control


@pytest.mark.parametrize(("authored", "shown"), [
    ("Practice good password management", "Practice good password management"),
    ("Use a secret token generator", "Use a secret token generator"),
    ('What does {"a": 1}["a"] return?', 'What does {"a": 1}["a"] return?'),
    ("Two&nbsp;plus two", "Two plus two"),
    ("What is\nthe answer?", "What is the answer?"),
])
def test_quiz_text_is_kept_as_authored(authored, shown):
    options = checkboxes(1, ["o1"]).replace("Option o1", authored)
    q = learner(learner_question(1, options, prompt=f"<p>{authored}</p>")).questions[0]
    assert q["supported"] and q["text"] == shown and q["choices"][0]["text"] == shown


def test_preview_quiz_text_is_kept_as_authored():
    q = parse(html(question(1).replace("choose true.", "choose a password manager.").replace(">True<", ">Two&nbsp;plus two<"))).questions[0]
    assert q["text"] == "Question 1: choose a password manager."
    assert [choice["text"] for choice in q["choices"]] == ["Two plus two", "False"]


def test_quiz_text_leaves_out_scripts_and_hidden_inputs():
    private = '<script>var t="SESSION_SENTINEL";</script><input type="hidden" name="z_x" value="SESSION_SENTINEL">'
    q = parse(html(question(1).replace("choose true.", "choose true." + private))).questions[0]
    assert q["text"] == "Question 1: choose true."
    q = learner(learner_question(1, radios(1, ["o1", "o2"]), prompt=f"<p>Pick one.{private}</p>")).questions[0]
    assert q["text"] == "Pick one."


def test_quiz_text_with_control_characters_is_unsupported():
    q = learner(learner_question(1, radios(1, ["o1", "o2"]), prompt="<p>bad\x1bthing</p>")).questions[0]
    assert not q["supported"] and q["text"] == ""


def test_learner_option_tokens_allow_long_ids():
    q = learner(learner_question(1, radios(1, ["o" + "9" * 18, "9" * 18]))).questions[0]
    assert [choice["choice_id"] for choice in q["choices"]] == ["o" + "9" * 18, "9" * 18]


def test_learner_control_types_follow_html_rules():
    # HTML types are case-insensitive, and an input without one is a text box.
    q = learner(learner_question(1, radios(1, ["o1", "o2"]).replace('type="radio"', 'type="RADIO"'))).questions[0]
    assert q["kind"] == "single-choice" and q["supported"]
    q = learner(learner_question(1, segment("Two is") + blank(1, "601").replace('type="text" ', ""), prompt="")).questions[0]
    assert q["kind"] == "fill-blank" and q["text"] == "Two is (blank 1)"


def test_learner_blank_numbers_skip_inputs_in_question_content():
    options = segment("Two is") + blank(1, "601") + segment('<input type="text"> or') + blank(1, "602")
    q = learner(learner_question(1, options, prompt="")).questions[0]
    assert q["kind"] == "unsupported"
    assert q["text"] == "Two is (blank 1) or (blank 2)"
    assert [b["number"] for b in q["blanks"]] == [1, 2]


def test_questions_sharing_one_answer_group_are_rejected():
    # Radios with one name are one group in the browser, which keeps one answer.
    def shared(text: str) -> str:
        return text.replace("tAtom202_300", "tAtom201_300").replace('value="202"', 'value="201"')
    with pytest.raises(PreviewPageError):
        parse(html(question(1) + shared(question(2))))
    first = learner_question(1, radios(1, ["o1", "o2"], checked=("o1",)), saved="True")
    with pytest.raises(PreviewPageError):
        learner(first + shared(learner_question(2, radios(2, ["o3", "o4"], checked=("o3",)), saved="True")))


@pytest.mark.parametrize("isprv", ["1", "0", None])
def test_learner_page_requires_an_empty_isprv(isprv):
    body = html(learner_question(1, radios(1, ["o1", "o2"])), isprv=isprv or "")
    if isprv is None:
        body = body.replace(b'<input name="isprv" type="hidden" value="">', b"")
    with pytest.raises(PreviewPageError):
        parse_learner_page(body, course_id=10, quiz_id=20, attempt_id=30, page=1)
    with pytest.raises(PreviewPageError):
        parse_learner_page(html(learner_question(1, radios(1, ["o1", "o2"])), isprv=""), course_id=10, quiz_id=20, attempt_id=31, page=1)


@pytest.mark.parametrize("options", [
    radios(1, ["o1", "x2"]),
    radios(1, ["o1", "o" + "1" * 19]),
    radios(1, ["o1", "o\u0663"]),
    radios(1, ["o1", "-2"]),
    radios(1, ["o1", "0"]),
    radios(1, ["o1", "1.5"]),
    radios(1, ["o1", "o1"]),
    radios(1, ["o1", "o2"], checked=("o1", "o2")),
    radios(1, ["o1", "o2"]).replace('name="tAtom201_300"', 'name="tAtom999_300"', 1),
    checkboxes(1, ["o1", "o2"]).replace('value="1"', 'value="on"', 1),
    checkboxes(1, ["o1", "o2"]).replace('name="tAtom201_300_o1"', 'name="tAtom201_300"'),
    checkboxes(1, ["o1", "o2"]).replace('name="tAtom201_300_o1"', 'name="tAtom201_300_o1_x"'),
    checkboxes(1, ["o1", "o2"]).replace('name="tAtom201_300_o1"', 'name="tAtom202_300_o1"'),
    segment("Two is") + blank(1, "60 1"),
    segment("Two is") + blank(1, "601") + blank(1, "601"),
    # Two labels for one control id would show one option's text twice.
    checkboxes(1, ["o1", "o2"]).replace("tAtom201_300_o2_id", "tAtom201_300_o1_id"),
])
def test_learner_malformed_ids_or_names_are_rejected(options):
    with pytest.raises(PreviewPageError) as exc:
        learner(learner_question(1, options))
    assert "SESSION_SENTINEL" not in str(exc.value)


@pytest.mark.parametrize("options", [
    radios(1, ["o1", "o2"]) + checkboxes(1, ["o3"]),
    '<textarea name="tAtom201_300_7"></textarea>',
    '<select name="tAtom201_300_7"><option>1</option></select>',
    '<input type="number" name="tAtom201_300_7">',
    radios(1, ["o1", "o2"]).replace("<input", "<input disabled", 1),
    segment("Two is") + blank(1, "601").replace("<input", "<input readonly"),
    checkboxes(1, ["o1", "o2"]).replace("Option o1", '<img alt="diagram">'),
    radios(1, ["o1", "o2"]) + '<d2l-html-block html="&lt;input type=&quot;text&quot;&gt;"></d2l-html-block>',
])
def test_learner_unknown_mixed_or_disabled_controls_are_unsupported(options):
    q = learner(learner_question(1, options)).questions[0]
    assert q["kind"] == "unsupported" and q["supported"] is False


def test_learner_unsupported_question_without_a_prompt_keeps_the_page():
    mixed = learner_question(1, radios(1, ["o1", "o2"]) + checkboxes(1, ["o3"]), prompt="")
    page = learner(mixed + learner_question(2, radios(2, ["o4", "o5"])))
    assert [(q["kind"], q["text"]) for q in page.questions] == [("unsupported", ""), ("single-choice", "Pick the right answer.")]


def test_learner_text_box_not_named_as_a_blank_keeps_the_page():
    box = learner_question(1, '<input type="text" name="tAtom201_300" value="12">')
    page = learner(box + learner_question(2, radios(2, ["o4", "o5"])))
    assert [(q["kind"], q["blanks"]) for q in page.questions] == [("unsupported", []), ("single-choice", [])]


def test_preview_parser_still_rejects_learner_markup():
    with pytest.raises(PreviewPageError):
        parse(html(learner_question(1, radios(1, ["401", "402"])), isprv=""))
    with pytest.raises(PreviewPageError):
        parse(html(learner_question(1, radios(1, ["o1", "o2"]))))
    page = parse(html(learner_question(1, checkboxes(1, ["o1", "o2"])) + learner_question(2, segment("Two is") + blank(2, "601"))))
    assert [q["kind"] for q in page.questions] == ["unsupported", "unsupported"]
    assert "blanks" not in page.questions[0]


def bootstrap() -> bytes:
    record = json.dumps({"_type": "func", "N": "D2L.LP.Web.Authentication.Xsrf.Init", "P": ["d2l_referrer", "SESSION_SENTINEL", 1234567890]})
    return ('<script>const graph={"1":'+json.dumps(record)+'};</script>').encode()


def test_save_transport_requires_persisted_readback_and_posts_once():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(bootstrap(), {}), (html(question(1, selected=False, saved="False")), {}), (html(question(1)), {})])
    response = Mock(status_code=200)
    client._request = Mock(return_value=response)
    result = save_current_preview_answer(client, course_id=10, quiz_id=20, attempt_id=30, page=1, question_id=101, choice_id=401)
    assert result.confirms_answer(101, 401)
    client._request.assert_called_once()
    assert client._request.call_args.args[0] == "POST"
    assert "isprv=1" in client._request.call_args.kwargs["headers"]["Referer"]
    fields = dict(client._request.call_args.kwargs["files"])
    assert fields["z_r1"] == (None, "1")
    response.close.assert_called_once()


def test_http_200_without_persisted_answer_is_unknown_not_success():
    client = LighthouseClient(read_only_auth=True)
    unchanged = html(question(1, selected=False, saved="False"))
    client.get_raw = Mock(side_effect=[(bootstrap(), {}), (unchanged, {}), (unchanged, {})])
    client._request = Mock(return_value=Mock(status_code=200))
    with pytest.raises(PreviewSaveUnknownError):
        save_current_preview_answer(client, course_id=10, quiz_id=20, attempt_id=30, page=1, question_id=101, choice_id=401)
    client._request.assert_called_once()


def test_save_readback_auth_expiry_is_unknown_after_post_dispatch():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(bootstrap(), {}), (html(question(1)), {}), SessionExpiredError("session expired")])
    client._request = Mock(return_value=Mock(status_code=200))
    with pytest.raises(PreviewSaveUnknownError):
        save_current_preview_answer(client, course_id=10, quiz_id=20, attempt_id=30, page=1, question_id=101, choice_id=401)
    client._request.assert_called_once()


def test_save_post_auth_expiry_is_unknown_after_dispatch():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(bootstrap(), {}), (html(question(1)), {})])
    client._request = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(PreviewSaveUnknownError):
        save_current_preview_answer(client, course_id=10, quiz_id=20, attempt_id=30, page=1, question_id=101, choice_id=401)
    client._request.assert_called_once()


def test_advance_readback_auth_expiry_is_unknown_after_post_dispatch():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(bootstrap(), {}), (html(question(1), extra="<button>Next Page</button>"), {}), SessionExpiredError("session expired")])
    client._request = Mock(return_value=Mock(status_code=200))
    with pytest.raises(PreviewAdvanceUnknownError):
        advance_current_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)
    client._request.assert_called_once()


def test_advance_post_auth_expiry_is_unknown_after_dispatch():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(bootstrap(), {}), (html(question(1), extra="<button>Next Page</button>"), {})])
    client._request = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(PreviewAdvanceUnknownError):
        advance_current_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)
    client._request.assert_called_once()


def test_uncertain_post_is_not_replayed_or_echoed():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(side_effect=[(bootstrap(), {}), (html(question(1)), {})])
    client._request = Mock(side_effect=RuntimeError("cookie=SESSION_SENTINEL"))
    with pytest.raises(PreviewSaveUnknownError) as exc:
        save_current_preview_answer(client, course_id=10, quiz_id=20, attempt_id=30, page=1, question_id=101, choice_id=401)
    assert "SESSION_SENTINEL" not in str(exc.value)
    client._request.assert_called_once()


def test_start_follows_typed_callback_without_executing_scripts():
    client = LighthouseClient(read_only_auth=True)
    process = '/d2l/lms/quizzing/user/attempt/quiz_start_process_auto.d2l?ou=10&qi=20&isprv=1&fromQB=0&inProgress=0'
    root = process.replace('quiz_start_process_auto', 'quiz_start_frame_auto')
    inner = process.replace('quiz_start_process_auto', 'quiz_start_iframe_2_auto')
    client._request = Mock(return_value=Mock(status_code=302, headers={"Location": root}))
    client.get_raw = Mock(side_effect=[
        (html('', extra='<button>Start Quiz!</button>') + bootstrap(), {}),
        (f'<iframe src="{inner}"></iframe>'.encode(), {}),
        (f'<iframe name="hiddenFrame" src="{process}"></iframe>'.encode(), {}),
        (b'<script>\nparent.GoToAttemptQuizAuto( 30,1,0 );\n</script>', {}),
        (html(question(1)), {}),
    ])
    page = start_preview(client, course_id=10, quiz_id=20)
    assert page.attempt_id == 30
    assert page.page == 1
    assert client.get_raw.call_count == 5
    assert client.get_raw.call_args_list[3].kwargs['_replay_safe'] is False
    client._request.assert_called_once()


def test_start_rejects_missing_button_without_creating_attempt():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(return_value=(b'<h1>Quiz Summary</h1>', {}))
    client._request = Mock()
    with pytest.raises(PreviewRefusedError, match="--bypass-availability"):
        start_preview(client, course_id=10, quiz_id=20)
    client.get_raw.assert_called_once()
    client._request.assert_not_called()


def start_client(readback):
    client = LighthouseClient(read_only_auth=True)
    process = '/d2l/lms/quizzing/user/attempt/quiz_start_process_auto.d2l?ou=10&qi=20&isprv=1&fromQB=0&inProgress=0'
    root = process.replace('quiz_start_process_auto', 'quiz_start_frame_auto')
    inner = process.replace('quiz_start_process_auto', 'quiz_start_iframe_2_auto')
    client._request = Mock(return_value=Mock(status_code=302, headers={"Location": root}))
    client.get_raw = Mock(side_effect=[
        (html('', extra='<button>Start Quiz!</button>') + bootstrap(), {}),
        (f'<iframe src="{inner}"></iframe>'.encode(), {}),
        (f'<iframe name="hiddenFrame" src="{process}"></iframe>'.encode(), {}),
        (b'<script>parent.GoToAttemptQuizAuto( 30,1,0 );</script>', {}),
        readback,
    ])
    return client


def test_start_reports_identity_before_the_page_readback():
    calls = []
    client = start_client(PreviewPageError())
    def on_identity(attempt_id, page):
        calls.append((attempt_id, page, client.get_raw.call_count))
    with pytest.raises(PreviewStartUnknownError) as exc_info:
        start_preview(client, course_id=10, quiz_id=20, on_identity=on_identity)
    assert calls == [(30, 1, 4)]  # before the fifth request (readback)
    assert (exc_info.value.attempt_id, exc_info.value.page) == (30, 1)
    client._request.assert_called_once()


def test_start_identity_callback_failure_is_unknown_with_identity():
    client = start_client((html(question(1)), {}))
    with pytest.raises(PreviewStartUnknownError) as exc_info:
        start_preview(client, course_id=10, quiz_id=20, on_identity=Mock(side_effect=OSError("disk full")))
    assert (exc_info.value.attempt_id, exc_info.value.page) == (30, 1)
    assert client.get_raw.call_count == 4  # no readback after a failed seal
    client._request.assert_called_once()


def test_server_current_page_is_used_only_for_the_same_preview_attempt():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(return_value=(html(question(2, page=2), page=2), {}))
    result = read_server_current_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)
    assert result.page == 2 and result.questions[0]["question_id"] == 102
    client.get_raw.assert_called_once()


@pytest.mark.parametrize("field, value", [("ai", "31"), ("isprv", "0"), ("qi", "21")])
def test_server_current_page_rejects_another_attempt_or_a_learner_page(field, value):
    body = html(question(2, page=2), page=2)
    original = {"ai": "30", "isprv": "1", "qi": "20"}[field]
    body = body.replace(f'name="{field}" type="hidden" value="{original}"'.encode(),
                        f'name="{field}" type="hidden" value="{value}"'.encode())
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(return_value=(body, {}))
    with pytest.raises(PreviewPageError):
        read_server_current_preview(client, course_id=10, quiz_id=20, attempt_id=30, page=1)


def test_start_readback_auth_expiry_is_unknown_after_state_creation():
    client = LighthouseClient(read_only_auth=True)
    process = '/d2l/lms/quizzing/user/attempt/quiz_start_process_auto.d2l?ou=10&qi=20&isprv=1&fromQB=0&inProgress=0'
    root = process.replace('quiz_start_process_auto', 'quiz_start_frame_auto')
    inner = process.replace('quiz_start_process_auto', 'quiz_start_iframe_2_auto')
    client._request = Mock(return_value=Mock(status_code=302, headers={"Location": root}))
    client.get_raw = Mock(side_effect=[
        (html('', extra='<button>Start Quiz!</button>') + bootstrap(), {}),
        (f'<iframe src="{inner}"></iframe>'.encode(), {}),
        (f'<iframe name="hiddenFrame" src="{process}"></iframe>'.encode(), {}),
        (b'<script>parent.GoToAttemptQuizAuto( 30,1,0 );</script>', {}),
        SessionExpiredError("session expired"),
    ])
    with pytest.raises(PreviewStartUnknownError) as exc_info:
        start_preview(client, course_id=10, quiz_id=20)
    assert exc_info.value.attempt_id == 30
    assert exc_info.value.page == 1
    assert client._request.call_count == 1


def test_start_process_auth_expiry_is_unknown_after_dispatch():
    client = LighthouseClient(read_only_auth=True)
    process = '/d2l/lms/quizzing/user/attempt/quiz_start_process_auto.d2l?ou=10&qi=20&isprv=1&fromQB=0&inProgress=0'
    root = process.replace('quiz_start_process_auto', 'quiz_start_frame_auto')
    inner = process.replace('quiz_start_process_auto', 'quiz_start_iframe_2_auto')
    client._request = Mock(return_value=Mock(status_code=302, headers={"Location": root}))
    client.get_raw = Mock(side_effect=[
        (html('', extra='<button>Start Quiz!</button>') + bootstrap(), {}),
        (f'<iframe src="{inner}"></iframe>'.encode(), {}),
        (f'<iframe name="hiddenFrame" src="{process}"></iframe>'.encode(), {}),
        SessionExpiredError("session expired"),
    ])
    with pytest.raises(PreviewStartUnknownError):
        start_preview(client, course_id=10, quiz_id=20)
    assert client._request.call_count == 1


def test_start_summary_post_auth_expiry_is_unknown_after_dispatch():
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(return_value=(html("", extra="<button>Start Quiz!</button>") + bootstrap(), {}))
    client._request = Mock(side_effect=SessionExpiredError("session expired"))
    with pytest.raises(PreviewStartUnknownError):
        start_preview(client, course_id=10, quiz_id=20)
    client.get_raw.assert_called_once()
    client._request.assert_called_once()
def test_ambiguous_start_does_not_retry_or_trust_script_strings():
    client = LighthouseClient(read_only_auth=True)
    process = '/d2l/lms/quizzing/user/attempt/quiz_start_process_auto.d2l?ou=10&qi=20&isprv=1&fromQB=0&inProgress=0'
    root = process.replace('quiz_start_process_auto', 'quiz_start_frame_auto')
    inner = process.replace('quiz_start_process_auto', 'quiz_start_iframe_2_auto')
    client._request = Mock(return_value=Mock(status_code=302, headers={"Location": root}))
    client.get_raw = Mock(side_effect=[
        (html('', extra='<button>Start Quiz!</button>') + bootstrap(), {}),
        (f'<iframe src="{inner}"></iframe>'.encode(), {}),
        (f'<iframe name="hiddenFrame" src="{process}"></iframe>'.encode(), {}),
        (b'<script>const text="parent.GoToAttemptQuizAuto(30,1,0)";</script>', {}),
    ])
    with pytest.raises(PreviewStartUnknownError):
        start_preview(client, course_id=10, quiz_id=20)
    assert client.get_raw.call_count == 4
    client._request.assert_called_once()
