"""Render a quiz's MathML equations as LaTeX text an agent can read.

Brightspace's equation editor stores MathML in question and option HTML.
Only presentation elements with a clear LaTeX form are rendered. Anything
else raises ``ValueError``, so the caller can report the question as
unsupported instead of showing a misleading equation.
"""

from __future__ import annotations

from bs4 import Tag

_MAX_DEPTH = 64
_TEX_ENCODINGS = {"latex", "tex", "application/x-tex", "application/x-latex"}
# Function application and the invisible times, separator and plus.
_INVISIBLE = dict.fromkeys(range(0x2061, 0x2065))
_ESCAPES = str.maketrans({
    "\\": r"\backslash ", "{": r"\{", "}": r"\}", "#": r"\#", "$": r"\$",
    "%": r"\%", "&": r"\&", "_": r"\_", "^": r"\^{}", "~": r"\sim ",
})
_FUNCTIONS = {"sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
              "log", "ln", "lg", "exp", "lim", "max", "min", "sup", "inf", "det", "gcd", "deg", "dim", "arg"}
_GROUPS = {"math", "mrow", "mstyle", "mpadded"}
_TOKENS = {"mi", "mn", "mo"}
# Each layout element's LaTeX, by its children in MathML order.
_LAYOUTS = {
    "msup": (2, "{0}^{{{1}}}"),
    "msub": (2, "{0}_{{{1}}}"),
    "msubsup": (3, "{0}_{{{1}}}^{{{2}}}"),
    "mfrac": (2, r"\frac{{{0}}}{{{1}}}"),
    "mroot": (2, r"\sqrt[{1}]{{{0}}}"),
    "mover": (2, r"\overset{{{1}}}{{{0}}}"),
    "munder": (2, r"\underset{{{1}}}{{{0}}}"),
    "munderover": (3, "{0}_{{{1}}}^{{{2}}}"),
}


def mathml_to_latex(math: Tag) -> str:
    """One ``<math>`` element as delimited LaTeX: ``\\( … \\)``, or ``\\[ … \\]`` for display math."""
    if math.name != "math":
        raise ValueError("Not a MathML element.")
    try:
        body = " ".join(_latex(math, 0).split())
    except RecursionError:
        raise ValueError("Unsupported MathML.") from None
    return rf"\[ {body} \]" if math.get("display") == "block" else rf"\( {body} \)"


def _children(node: Tag) -> list[Tag]:
    """Element children; text directly inside a layout element is not MathML."""
    children: list[Tag] = []
    for child in node.children:
        if isinstance(child, Tag):
            children.append(child)
        elif str(child).strip():
            raise ValueError("Unsupported MathML.")
    return children


def _token(node: Tag) -> str:
    if node.find(True) is not None:
        raise ValueError("Unsupported MathML.")
    text = node.get_text().translate(_INVISIBLE).strip()
    if node.name == "mi" and text in _FUNCTIONS:
        return rf"\{text} "
    return text.translate(_ESCAPES)


def _latex(node: Tag, depth: int) -> str:
    if depth > _MAX_DEPTH:
        raise ValueError("Unsupported MathML.")
    name = node.name
    if name in _TOKENS:
        return _token(node)
    if name in {"mtext", "ms"}:
        text = _token(node)
        return rf"\text{{{text}}}" if text else ""
    if name == "mspace":
        return " "
    if name == "mphantom":
        return ""
    if name == "semantics":
        return _semantics(node, depth)
    children = _children(node)
    if name in _GROUPS:
        return "".join(_latex(child, depth + 1) for child in children)
    if name == "msqrt":
        return r"\sqrt{" + "".join(_latex(child, depth + 1) for child in children) + "}"
    if name in _LAYOUTS:
        arity, template = _LAYOUTS[name]
        if len(children) != arity:
            raise ValueError("Unsupported MathML.")
        parts = [_latex(child, depth + 1) for child in children]
        # A compound base is grouped, so x+1 squared is not read as x+1².
        if children[0].name not in _TOKENS:
            parts[0] = f"{{{parts[0]}}}"
        return template.format(*parts)
    if name == "mfenced":
        return _fenced(node, children, depth)
    if name == "mtable":
        return _table(children, depth)
    raise ValueError("Unsupported MathML.")


def _semantics(node: Tag, depth: int) -> str:
    """The author's LaTeX annotation when there is one, else the presentation markup."""
    children = _children(node)
    for child in children[1:]:
        if child.name == "annotation" and str(child.get("encoding", "")).lower() in _TEX_ENCODINGS:
            text = child.get_text().strip()
            if text and child.find(True) is None:
                return text
    if not children or children[0].name in {"annotation", "annotation-xml"}:
        raise ValueError("Unsupported MathML.")
    return _latex(children[0], depth + 1)


def _fenced(node: Tag, children: list[Tag], depth: int) -> str:
    opening = str(node.get("open", "(")).strip().translate(_ESCAPES)
    closing = str(node.get("close", ")")).strip().translate(_ESCAPES)
    separators = "".join(str(node.get("separators", ",")).split())
    parts: list[str] = []
    for index, child in enumerate(children):
        if index and separators:
            parts.append(separators[min(index - 1, len(separators) - 1)].translate(_ESCAPES))
        parts.append(_latex(child, depth + 1))
    return opening + "".join(parts) + closing


def _table(rows: list[Tag], depth: int) -> str:
    lines: list[str] = []
    for row in rows:
        if row.name != "mtr":
            raise ValueError("Unsupported MathML.")
        cells = _children(row)
        if any(cell.name != "mtd" for cell in cells):
            raise ValueError("Unsupported MathML.")
        lines.append(" & ".join("".join(_latex(part, depth + 2) for part in _children(cell)) for cell in cells))
    return r"\begin{matrix}" + r" \\ ".join(lines) + r"\end{matrix}"
