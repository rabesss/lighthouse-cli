"""Equations, images and other rich question text, modeled on observed learner markup."""

from __future__ import annotations

from html import escape
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup

from lighthouse_cli.api import LighthouseClient, NetworkError
from lighthouse_cli.quiz_attempt_page import PreviewRefusedError, hidden_form
from lighthouse_cli.quiz_learner_transport import (
    MAX_IMAGE_BYTES,
    REFUSE_IMAGE_ROUTE,
    REFUSE_IMAGE_SOURCE,
    read_quiz_image,
)
from lighthouse_cli.quiz_math import mathml_to_latex
from tests.test_quiz_attempt_page import (
    LEARNER_BUTTONS,
    blank,
    html,
    learner,
    learner_question,
    radios,
    segment,
)

MATHML = 'xmlns="http://www.w3.org/1998/Math/MathML"'
SQUARE = f"<math {MATHML}><msup><mi>x</mi><mn>2</mn></msup><mo>=</mo><mn>16</mn></math>"
HALF = f"<math {MATHML}><mfrac><mi>x</mi><mn>2</mn></mfrac></math>"
# An image attached to the question is shown above its prompt, through the file viewer.
ATTACHED_SRC = "/d2l/common/viewFile.d2lfile/Content/c2hhcGVz/shape.png?ou=10"
ATTACHED = (f'<div class="dco d2l-quiz-image-container"><div class="dco_c"><div style="text-align:center">'
            f'<img alt="" src="{ATTACHED_SRC}"></div></div></div>')


def latex(markup: str) -> str:
    math = BeautifulSoup(markup, "html.parser").find("math")
    assert math is not None
    return mathml_to_latex(math)


def block(content: str) -> str:
    return f'<div class="d2l-htmlblock-untrusted"><d2l-html-block html="{escape(content, quote=True)}"></d2l-html-block></div>'


def block_radios(number: int, options: dict[str, str]) -> str:
    # Multiple-choice options are table rows whose text has no label element.
    group = f"tAtom{200 + number}_300"
    return "<table>" + "".join(
        f'<tr><td><input type="radio" name="{group}" id="{group}_{value}_id" value="{value}"></td>'
        f"<td>{block(content)}</td></tr>" for value, content in options.items()) + "</table>"


def block_checkboxes(number: int, options: dict[str, str]) -> str:
    group = f"tAtom{200 + number}_300"
    return "<table>" + "".join(
        f'<tr><td><input type="checkbox" name="{group}_{option}" id="{group}_{option}_id" value="1"></td>'
        f'<td><label for="{group}_{option}_id">{block(content)}</label></td></tr>' for option, content in options.items()) + "</table>"


def attached(question: str) -> str:
    return question.replace("<div><d2l-html-block", ATTACHED + "<div><d2l-html-block", 1)


def only(question: str) -> dict:
    page = learner(question)
    assert len(page.questions) == 1
    return page.questions[0]


# -- MathML ---------------------------------------------------------------------

@pytest.mark.parametrize(("markup", "expected"), [
    (SQUARE, r"\( x^{2}=16 \)"),
    (HALF, r"\( \frac{x}{2} \)"),
    ("<math><msqrt><mn>8</mn></msqrt></math>", r"\( \sqrt{8} \)"),
    # The index is braced, so a bracket in it cannot end the optional argument.
    ("<math><mroot><mi>x</mi><mn>3</mn></mroot></math>", r"\( \sqrt[{3}]{x} \)"),
    ("<math><mroot><mi>x</mi><mrow><mo>[</mo><mi>n</mi><mo>]</mo></mrow></mroot></math>", r"\( \sqrt[{[n]}]{x} \)"),
    ("<math><msub><mi>a</mi><mi>n</mi></msub></math>", r"\( a_{n} \)"),
    ("<math><msubsup><mi>x</mi><mi>i</mi><mn>2</mn></msubsup></math>", r"\( x_{i}^{2} \)"),
    # A compound base is grouped, so the whole sum is squared.
    ("<math><msup><mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow><mn>2</mn></msup></math>", r"\( {x+1}^{2} \)"),
    ("<math><msup><mi>xy</mi><mn>2</mn></msup><msup><mn>10</mn><mn>3</mn></msup></math>", r"\( {xy}^{2}{10}^{3} \)"),
    # A zero-thickness fraction is a stacked pair, such as a binomial coefficient.
    ('<math><mfenced><mfrac linethickness="0"><mn>5</mn><mn>2</mn></mfrac></mfenced></math>',
     r"\( (\genfrac{}{}{0pt}{}{5}{2}) \)"),
    ('<math><mfrac linethickness="0px"><mi>n</mi><mi>k</mi></mfrac><mfrac linethickness="2"><mn>1</mn><mn>2</mn></mfrac></math>',
     r"\( \genfrac{}{}{0pt}{}{n}{k}\frac{1}{2} \)"),
    # Text keeps the spaces it shows, and a string literal its quotes.
    ("<math><mi>n</mi><mtext> is even</mtext></math>", r"\( n\text{ is even} \)"),
    ("<math><mi>x</mi><mtext> </mtext><mi>y</mi></math>", r"\( x y \)"),
    ("""<math><ms>a</ms><mo>+</mo><ms lquote="'" rquote="'">b</ms></math>""", r"""\( \text{"a"}+\text{'b'} \)"""),
    # Invisible times is multiplication, so neighbouring numbers stay apart.
    ("<math><mn>2</mn><mo>&#x2062;</mo><mn>3</mn></math>", r"\( 2\cdot 3 \)"),
    ("<math><munderover><mo>∑</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mi>n</mi></munderover></math>",
     r"\( ∑_{i=1}^{n} \)"),
    ("<math><mover><mi>x</mi><mo>¯</mo></mover><munder><mi>y</mi><mo>_</mo></munder></math>",
     r"\( \overset{¯}{x}\underset{\_}{y} \)"),
    ("<math><mi>sin</mi><mi>θ</mi><mo>+</mo><mi>log</mi><mi>x</mi></math>", r"\( \sin θ+\log x \)"),
    ("<math><mi>f</mi><mo>&#x2061;</mo><mfenced><mi>a</mi><mi>b</mi></mfenced></math>", r"\( f(a,b) \)"),
    ('<math><mfenced open="[" close="}" separators=";"><mi>a</mi><mi>b</mi><mi>c</mi></mfenced></math>',
     r"\( [a;b;c\} \)"),
    ("<math><mn>50</mn><mo>%</mo><mtext>of</mtext><mspace/><mi>$</mi><mphantom><mi>z</mi></mphantom></math>",
     r"\( 50\%\text{of} \$ \)"),
    ("<math><mtable><mtr><mtd><mn>1</mn></mtd><mtd><mn>0</mn></mtd></mtr>"
     "<mtr><mtd><mn>0</mn></mtd><mtd><mn>1</mn></mtd></mtr></mtable></math>",
     r"\( \begin{matrix}1 & 0 \\ 0 & 1\end{matrix} \)"),
    ('<math display="block"><mn>1</mn></math>', r"\[ 1 \]"),
])
def test_mathml_reads_as_latex(markup, expected):
    assert latex(markup) == expected


def test_the_authors_latex_annotation_is_preferred():
    annotated = ('<math><semantics><mrow><mi>x</mi></mrow>'
                 '<annotation encoding="application/x-tex">\\frac{a}{b}</annotation></semantics></math>')
    assert latex(annotated) == r"\( \frac{a}{b} \)"
    other = '<math><semantics><mi>x</mi><annotation encoding="text/plain">ex</annotation></semantics></math>'
    assert latex(other) == r"\( x \)"


@pytest.mark.parametrize("annotation", [
    "x% comment\n+y", "x \\)", "\\(x", "\\[x\\]", "$x$", "\\frac{a}{b", "a}{", "\\\\% comment",
])
def test_an_annotation_that_would_break_out_of_its_delimiters_is_not_used(annotation):
    annotated = (f'<math><semantics><mrow><mi>x</mi><mo>+</mo><mi>y</mi></mrow>'
                 f'<annotation encoding="application/x-tex">{annotation}</annotation></semantics></math>')
    assert latex(annotated) == r"\( x+y \)"


def test_an_annotation_may_escape_special_characters():
    annotated = ('<math><semantics><mi>x</mi><annotation encoding="latex">50\\% \\{a\\} \\$</annotation>'
                 '</semantics></math>')
    assert latex(annotated) == r"\( 50\% \{a\} \$ \)"


@pytest.mark.parametrize("markup", [
    "<math><mi>x</mi><mglyph></mglyph></math>",
    "<math>loose text<mi>x</mi></math>",
    "<math><mi><b>x</b></mi></math>",
    "<math><mfrac><mi>x</mi></mfrac></math>",
    "<math><mtable><mi>x</mi></mtable></math>",
    "<math><mtable><mtr><mi>x</mi></mtr></mtable></math>",
    '<math><semantics><annotation encoding="LaTeX">x</annotation></semantics></math>',
    "<math>" + "<mrow>" * 70 + "<mi>x</mi>" + "</mrow>" * 70 + "</math>",
    # Nothing to show, or content hidden in a space.
    "<math></math>",
    "<math><mi>y</mi><mspace><mi>x</mi></mspace></math>",
    # A spanning cell has no matrix form.
    '<math><mtable><mtr><mtd rowspan="2"><mi>a</mi></mtd><mtd><mi>b</mi></mtd></mtr><mtr><mtd><mi>c</mi></mtd></mtr></mtable></math>',
    '<math><mtable><mtr><mtd columnspan="2"><mi>a</mi></mtd></mtr><mtr><mtd><mi>b</mi></mtd><mtd><mi>c</mi></mtd></mtr></mtable></math>',
    "<math><mrow></mrow></math>",
    "<math><mspace/></math>",
    "<math><mphantom><mi>x</mi></mphantom></math>",
])
def test_mathml_without_a_clear_latex_form_is_rejected(markup):
    with pytest.raises(ValueError):
        latex(markup)


def test_only_math_elements_are_converted():
    with pytest.raises(ValueError):
        mathml_to_latex(BeautifulSoup("<mrow><mi>x</mi></mrow>", "html.parser").find("mrow"))


# -- question text --------------------------------------------------------------

def test_equations_read_as_latex_in_prompts_and_choices():
    options = block_radios(1, {"o1": f"<p><math {MATHML}><mn>2</mn></math></p>", "o2": "<p>4</p>",
                               "o3": f"<p>Half of <math {MATHML}><mi>π</mi></math></p>"})
    q = only(learner_question(1, options, prompt=f"<p>If {SQUARE} and x &gt; 0, what is {HALF}?</p>"))
    assert q["kind"] == "single-choice" and q["supported"]
    assert q["text"] == r"If \( x^{2}=16 \) and x > 0, what is \( \frac{x}{2} \)?"
    assert [choice["text"] for choice in q["choices"]] == [r"\( 2 \)", "4", r"Half of \( π \)"]
    assert q["images"] == []


def test_images_are_numbered_in_reading_order():
    options = block_radios(1, {"o1": "<p>Triangle</p>",
                               "o2": '<p><img src="/content/enforced/10-x/sq.png" alt="Square"></p>'})
    q = only(attached(learner_question(1, options, prompt="<p>Which shape is shown?</p>")))
    assert q["kind"] == "single-choice" and q["supported"]
    assert q["text"] == "[image 1] Which shape is shown?"
    assert [choice["text"] for choice in q["choices"]] == ["Triangle", "[image 2: Square]"]
    assert q["images"] == [{"number": 1, "src": ATTACHED_SRC, "alt": ""},
                           {"number": 2, "src": "/content/enforced/10-x/sq.png", "alt": "Square"}]


def test_image_only_questions_and_options_are_supported():
    options = block_checkboxes(1, {"o1": '<p><img src="/content/enforced/10/sq.png" alt="Square"></p>',
                                   "o2": '<p><img src="/content/enforced/10/circle.png"></p>',
                                   "o3": '<p>None of <img src="/content/enforced/10/circle.png"> these</p>'})
    q = only(learner_question(1, options, prompt='<p><img src="/content/enforced/10/tri.png"></p>'))
    assert q["kind"] == "multi-select" and q["supported"]
    assert q["text"] == "[image 1]"
    assert [choice["text"] for choice in q["choices"]] == ["[image 2: Square]", "[image 3]", "None of [image 4] these"]
    assert [image["src"] for image in q["images"]] == [
        "/content/enforced/10/tri.png", "/content/enforced/10/sq.png",
        "/content/enforced/10/circle.png", "/content/enforced/10/circle.png"]


def test_an_attached_image_alone_is_not_a_prompt():
    q = only(attached(learner_question(1, radios(1, ["o1", "o2"]), prompt="<p></p>")))
    assert q["kind"] == "unsupported" and q["text"] == ""


def test_images_in_a_fill_in_the_blank_sentence():
    options = segment('<img src="/content/enforced/10/eq.png" alt="x squared"> is') + blank(1, "601")
    q = only(learner_question(1, options, prompt=""))
    assert q["kind"] == "fill-blank" and q["supported"]
    assert q["text"] == "[image 1: x squared] is (blank 1)"


@pytest.mark.parametrize("image", [
    '<img src="data:image/png;base64,iVBORw0KGgo=">',
    '<img src="tri.png">',
    '<img src="//cdn.example/tri.png">',
    '<img src="javascript:void(0)">',
    '<img src="/a\\b.png">',
    '<img src="/two words.png">',
    "<img>",
    '<img src="/content/tri.png" alt="bad\x1bthing">',
])
def test_an_image_that_cannot_be_shown_is_unsupported(image):
    q = only(learner_question(1, radios(1, ["o1", "o2"]), prompt=f"<p>Which? {image}</p>"))
    assert q["kind"] == "unsupported" and not q["supported"]
    assert len(q["images"]) == 1


def test_an_image_on_another_site_keeps_its_address():
    # The CLI will not fetch it, but the address is public page content.
    q = only(learner_question(1, radios(1, ["o1", "o2"]), prompt='<p><img src="https://cdn.example/tri.png"></p>'))
    assert q["supported"] and q["images"][0]["src"] == "https://cdn.example/tri.png"


@pytest.mark.parametrize("prompt", [
    f"<p>Solve <math {MATHML}><mi>x</mi><mglyph></mglyph></math></p>",
    '<p>Solve <m:math xmlns:m="http://www.w3.org/1998/Math/MathML"><m:mi>x</m:mi></m:math></p>',
    '<p>Read <svg viewBox="0 0 1 1"><text>x</text></svg></p>',
    "<p>Watch <video></video></p>",
    "<p>Open <object></object></p>",
    # Browsers read <image> as <img>, and a picture can show a source other than its img.
    '<p>Read <image src="/content/enforced/10/graph.png" alt="graph"></p>',
    '<p>Read <picture><source srcset="/content/a.webp"><img src="/content/a.png" alt="a"></picture></p>',
    # The CLI does not resolve dot segments, so it could not download these.
    '<p>Read <img src="/content/../fig.png" alt="fig"></p>',
    '<p>Read <img src="https://lms.example/content/%2E%2E/fig.png" alt="fig"></p>',
    '<p>Read <img src="/content/./fig.png" alt="fig"></p>',
    # Read nowhere else, so not shown in the text.
    '<p>Pick <button type="button"><img src="/content/a.png"></button></p>',
])
def test_media_the_text_cannot_show_is_unsupported(prompt):
    q = only(learner_question(1, radios(1, ["o1", "o2"]), prompt=prompt))
    assert q["kind"] == "unsupported" and not q["supported"]


def test_an_attached_image_with_unreadable_text_is_unsupported():
    question = learner_question(1, radios(1, ["o1", "o2"]), prompt="<p>Which shape?</p>").replace(
        "<div><d2l-html-block", ATTACHED.replace("<img", "\u202e<img") + "<div><d2l-html-block", 1)
    q = only(question)
    assert not q["supported"] and len(q["images"]) == 1


def test_a_noscript_fallback_image_is_not_counted():
    prompt = '<p>Which shape?</p><img src="/content/a.png" alt="a"><noscript><img src="/content/a.png" alt="a"></noscript>'
    q = only(learner_question(1, radios(1, ["o1", "o2"]), prompt=prompt))
    assert q["supported"] and len(q["images"]) == 1


def test_a_line_break_in_a_superscript_keeps_its_lines_apart():
    q = only(learner_question(1, radios(1, ["o1", "o2"]), prompt="<p>Evaluate x<sup>2<br>3</sup></p>"))
    assert q["text"] == "Evaluate x^{2 3}"


def test_a_blank_outside_the_question_text_is_unsupported():
    # The first legend of a disabled fieldset stays enabled, but legends are not question text.
    question = learner_question(1, segment("Fill the blank."), prompt="").replace(
        "<fieldset><legend>Question 1 options:", "<fieldset disabled><legend>2+2= " + blank(1, "601"), 1)
    q = only(question)
    assert q["kind"] == "unsupported" and not q["supported"]


def test_an_image_outside_the_prompt_and_choices_is_unsupported():
    question = learner_question(1, radios(1, ["o1", "o2"])).replace(
        "<fieldset>", '<div><img src="/content/enforced/10/hint.png"></div><fieldset>', 1)
    assert only(question)["kind"] == "unsupported"


def test_superscripts_and_subscripts_read_as_scripts():
    q = only(learner_question(1, radios(1, ["o1", "o2"]), prompt="<p>Is H<sub>2</sub>O x<sup>y<sup>2</sup></sup>?</p>"))
    assert q["text"] == "Is H_{2}O x^{y^{2}}?"


def test_inline_markup_does_not_split_words():
    q = only(learner_question(1, radios(1, ["o1", "o2"]), prompt="<p>un<b>believ</b>able</p><p>next<br>line</p><ul><li>a</li><li>b</li></ul>"
                                                                 "<details><summary>Answer:</summary><span>42</span></details>"))
    assert q["text"] == "unbelievable next line a b Answer: 42"


def test_invisible_marks_are_dropped_but_direction_controls_void_the_text():
    q = only(learner_question(1, radios(1, ["o1", "o2"]), prompt="<p>Pick the­ right﻿ answer​.</p>"))
    assert q["supported"] and q["text"] == "Pick the right answer."
    q = only(learner_question(1, radios(1, ["o1", "o2"]), prompt="<p>Pick ‮one</p>"))
    assert not q["supported"] and q["text"] == ""


def test_templates_are_never_read():
    prompt = "<p>Pick one.<template><p>SECRET_ANSWER</p></template></p>"
    options = radios(1, ["o1", "o2"]) + '<template><input type="radio" name="tAtom201_300" value="o9"></template>'
    q = only(learner_question(1, options, prompt=prompt))
    assert q["supported"] and q["text"] == "Pick one."
    assert [choice["choice_id"] for choice in q["choices"]] == ["o1", "o2"]
    body = html(learner_question(1, radios(1, ["o1", "o2"])), isprv="",
                extra='<template><input type="hidden" name="z_extra" value="1"></template>')
    assert "z_extra" not in hidden_form(body)[1]


@pytest.mark.parametrize(("extra", "shown"), [
    ('<div style="visibility:hidden"><button type="button">Next Page</button></div>', False),
    ('<div style="visibility:hidden"><button type="button" style="visibility: visible">Next Page</button></div>', True),
    ('<div style="visibility:collapse"><span><button type="button">Next Page</button></span></div>', False),
    ('<button type="button" style="display:/* off */none">Next Page</button>', False),
    ('<button type="button" style="display:none; display:inline-block">Next Page</button>', True),
    ('<button type="button" style="color:red /* display:none */">Next Page</button>', True),
    ('<fieldset disabled><legend><button type="button">Next Page</button></legend></fieldset>', True),
    # An inherited visibility is the ancestor's; initial is visible.
    ('<div style="visibility:hidden"><button type="button" style="visibility:inherit">Next Page</button></div>', False),
    ('<div style="visibility:hidden"><button type="button" style="visibility:unset">Next Page</button></div>', False),
    ('<div style="visibility:hidden"><button type="button" style="visibility:revert">Next Page</button></div>', False),
    ('<div style="visibility:hidden"><button type="button" style="visibility:initial">Next Page</button></div>', True),
    # An important declaration beats a later normal one; an invalid value is ignored.
    ('<button type="button" style="display:none!important;display:block">Next Page</button>', False),
    ('<button type="button" style="display:none ! IMPORTANT ;display:block">Next Page</button>', False),
    ('<button type="button" style="display:none;display:bogus">Next Page</button>', False),
    ('<button type="button" style="display:none;display:inline flow-root">Next Page</button>', True),
    ('<div style="visibility:hidden"><button type="button" style="visibility:shown">Next Page</button></div>', False),
    ('<fieldset disabled><legend>Nav</legend><legend><button type="button">Next Page</button></legend></fieldset>', False),
    ('<fieldset disabled><div><legend><button type="button">Next Page</button></legend></div></fieldset>', False),
])
def test_next_control_follows_css_and_fieldset_rules(extra, shown):
    assert learner(learner_question(1, radios(1, ["o1", "o2"])), extra=LEARNER_BUTTONS + extra).has_next_control is shown


@pytest.mark.parametrize("kind", ['type=""', 'type="bogus"', 'type="TEXT"'])
def test_an_empty_or_unknown_input_type_is_a_text_box(kind):
    q = only(learner_question(1, segment("Two is") + blank(1, "601").replace('type="text"', kind), prompt=""))
    assert q["kind"] == "fill-blank" and q["text"] == "Two is (blank 1)"


def test_an_oversized_saved_blank_makes_only_its_question_unsupported():
    long = learner_question(1, segment("Two is") + blank(1, "601", "x" * 10001), prompt="")
    page = learner(long + learner_question(2, radios(2, ["o4", "o5"])))
    assert [q["kind"] for q in page.questions] == ["unsupported", "single-choice"]
    assert page.questions[0]["blanks"] == [{"blank_id": "601", "number": 1, "value": ""}]


# -- image download -------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


def image_client(content: bytes = PNG, headers: dict[str, str] | None = None) -> LighthouseClient:
    client = LighthouseClient(read_only_auth=True)
    client.get_raw = Mock(return_value=(content, {"Content-Type": "image/png"} if headers is None else headers))
    return client


def test_a_root_relative_image_is_read_from_the_lms():
    client = image_client()
    assert read_quiz_image(client, ATTACHED_SRC) == (PNG, "image/png")
    client.get_raw.assert_called_once_with(client.base_url + ATTACHED_SRC, max_bytes=MAX_IMAGE_BYTES, _replay_safe=False)
    client = image_client()
    read_quiz_image(client, client.base_url + "/content/enforced/10/sq.png")
    client.get_raw.assert_called_once_with(client.base_url + "/content/enforced/10/sq.png", max_bytes=MAX_IMAGE_BYTES, _replay_safe=False)


@pytest.mark.parametrize("src", [
    "", "tri.png", "//cdn.example/tri.png", "https://cdn.example/tri.png", "data:image/png;base64,iVBORw0KGgo=",
    "javascript:void(0)", "/a\\b.png", "/tri\n.png", "https://[broken/tri.png", "/" + "a" * 2048, None,
])
def test_an_image_elsewhere_is_never_requested(src):
    client = image_client()
    with pytest.raises(PreviewRefusedError, match=REFUSE_IMAGE_SOURCE):
        read_quiz_image(client, src)
    client.get_raw.assert_not_called()


@pytest.mark.parametrize("src", [
    "/d2l/lms/quizzing/user/attempt/quiz_attempt_page_auto.d2l?qi=20&ai=30&pg=999999&ou=10",
    "/D2L/LMS/Quizzing/user/x.png", "/d2l/lms/%71uizzing/x.png", "/d2l//lms/./quizzing/x.png",
    "/content/../d2l/lms/quizzing/x.png", "/d2l/logout", "/d2l/LogOut?x.png", "{base}/d2l/lms/quizzing/x.png",
    "/d2l/lms/dropbox/user/folder_submit_files.d2l?db=1&ou=10", "/d2l/home/10/x.D2L", "/d2l/x.d2l/y.png",
])
def test_an_image_address_that_is_a_brightspace_action_is_never_requested(src):
    client = image_client()
    with pytest.raises(PreviewRefusedError, match=REFUSE_IMAGE_ROUTE):
        read_quiz_image(client, src.format(base=client.base_url))
    client.get_raw.assert_not_called()


def test_an_image_fragment_is_not_sent():
    client = image_client()
    read_quiz_image(client, "/content/enforced/10/sq.png#zoom")
    client.get_raw.assert_called_once_with(client.base_url + "/content/enforced/10/sq.png", max_bytes=MAX_IMAGE_BYTES, _replay_safe=False)


@pytest.mark.parametrize("address", ["https://user:pass@{host}/tri.png", "https://user@{host}/tri.png",  # pragma: allowlist secret
                                     "https://{host}:8443/tri.png", "https://{host}:bad/tri.png"])
def test_an_lms_address_with_credentials_or_another_port_is_refused(address):
    client = image_client()
    host = client.base_url.removeprefix("https://")
    with pytest.raises(PreviewRefusedError, match=REFUSE_IMAGE_SOURCE):
        read_quiz_image(client, address.format(host=host))
    client.get_raw.assert_not_called()


def test_an_insecure_lms_address_is_refused():
    client = image_client()
    with pytest.raises(PreviewRefusedError):
        read_quiz_image(client, client.base_url.replace("https://", "http://") + "/content/tri.png")
    client.get_raw.assert_not_called()


def test_a_path_leaving_the_lms_routes_is_rejected_before_any_request():
    client = LighthouseClient(read_only_auth=True)
    client._request = Mock()
    with pytest.raises(NetworkError):
        read_quiz_image(client, "/content/../../tri.png")
    client._request.assert_not_called()


@pytest.mark.parametrize(("content", "headers", "media_type"), [
    (PNG, {"content-type": "IMAGE/PNG; charset=binary"}, "image/png"),
    (b"\xff\xd8\xff\xe0" + b"\0" * 8, {"Content-Type": "application/octet-stream"}, "image/jpeg"),
    (b"GIF89a" + b"\0" * 8, {"CONTENT-TYPE": "image/gif"}, "image/gif"),
    (b"GIF87a" + b"\0" * 8, {"Content-Type": "image/gif"}, "image/gif"),
    (b"RIFF\x10\0\0\0WEBPVP8 " + b"\0" * 8, {"Content-Type": "image/webp"}, "image/webp"),
    # The bytes decide the type, not the header.
    (b"\xff\xd8\xff\xe0" + b"\0" * 8, {"Content-Type": "image/png"}, "image/jpeg"),
])
def test_image_type_is_read_from_its_bytes(content, headers, media_type):
    assert read_quiz_image(image_client(content, headers), "/content/x") == (content, media_type)


@pytest.mark.parametrize(("content", "headers"), [
    (b"<html>Sign in</html>", {"Content-Type": "text/html; charset=utf-8"}),
    (PNG, {}),
    (PNG, {"Content-Type": "text/plain"}),
    (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', {"Content-Type": "image/svg+xml"}),
    (b"BM" + b"\0" * 16, {"Content-Type": "image/bmp"}),
    (b"RIFF\x10\0\0\0WAVEfmt " + b"\0" * 8, {"Content-Type": "application/octet-stream"}),
    (b"", {"Content-Type": "image/png"}),
])
def test_anything_but_a_known_raster_image_is_rejected(content, headers):
    with pytest.raises(NetworkError):
        read_quiz_image(image_client(content, headers), "/content/x")
