"""Python syntax colours for the Input tab, without Pygments (it is left out of the build).

`python_spans` splits one line into coloured spans. A triple-quoted string that
runs on carries over to the next line as a `State`, the way `QSyntaxHighlighter`
works line by line. Indices count characters (code points), not UTF-16 units.
The colours follow VS Code's default light and dark themes, which students know.
"""

import builtins
import keyword
import re
from enum import IntEnum, StrEnum


class Token(StrEnum):
    KEYWORD = "keyword"
    BUILTIN = "builtin"
    DEFINITION = "definition"
    """The name after `def` or `class`."""
    DECORATOR = "decorator"
    NUMBER = "number"
    STRING = "string"
    COMMENT = "comment"


class State(IntEnum):
    """Where a line ends: in code, or inside a triple-quoted string."""

    CODE = 0
    SINGLE = 1
    """Inside '''…'''."""
    DOUBLE = 2
    '''Inside """…""".'''


KEYWORDS = frozenset(keyword.kwlist)
BUILTINS = frozenset(name for name in dir(builtins) if not name.startswith("_"))
COLORS = {
    False: {
        Token.KEYWORD: "#0000ff",
        Token.BUILTIN: "#267f99",
        Token.DEFINITION: "#795e26",
        Token.DECORATOR: "#795e26",
        Token.NUMBER: "#098658",
        Token.STRING: "#a31515",
        Token.COMMENT: "#008000",
    },
    True: {
        Token.KEYWORD: "#569cd6",
        Token.BUILTIN: "#4ec9b0",
        Token.DEFINITION: "#dcdcaa",
        Token.DECORATOR: "#dcdcaa",
        Token.NUMBER: "#b5cea8",
        Token.STRING: "#ce9178",
        Token.COMMENT: "#6a9955",
    },
}
"""Colour of each token on a light (False) and a dark (True) background."""

_TOKEN = re.compile(
    r"""
      (?P<comment>\#.*)
    | (?P<string>[rRbBuUfF]{0,2}(?:'''|\"\"\"|'(?:[^'\\]|\\.)*'?|"(?:[^"\\]|\\.)*"?))
    | (?P<number>(?<![\w.])(?:0[xXoObB][0-9a-fA-F_]+
                             |(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d[\d_]*)?[jJ]?))
    | (?P<decorator>^[ \t]*@[^\W\d][\w.]*)
    | (?P<name>[^\W\d]\w*)
    """,
    re.VERBOSE,
)
_CLOSE = {
    State.SINGLE: re.compile(r"(?:[^\\]|\\.)*?'''"),
    State.DOUBLE: re.compile(r'(?:[^\\]|\\.)*?"""'),
}
_OPENING = {"'''": State.SINGLE, '"""': State.DOUBLE}

Span = tuple[int, int, Token]
"""Start, length, token."""


def _after_dot(line: str, start: int) -> bool:
    index = start - 1
    while index >= 0 and line[index] in " \t":
        index -= 1
    return index >= 0 and line[index] == "."


def python_spans(line: str, state: State = State.CODE) -> tuple[list[Span], State]:
    """The coloured spans of one line of Python and the state at its end; `state` is the
    state at the end of the line before."""
    spans: list[Span] = []
    position = 0
    if state is not State.CODE:
        close = _CLOSE[state].match(line)
        if close is None:
            return ([(0, len(line), Token.STRING)] if line else []), state
        spans.append((0, close.end(), Token.STRING))
        position = close.end()
    definition = False
    while (match := _TOKEN.search(line, position)) is not None:
        kind = match.lastgroup
        start, end = match.span()
        position = max(end, start + 1)
        if kind == "name":
            word = match.group()
            if definition:
                spans.append((start, end - start, Token.DEFINITION))
            elif word in KEYWORDS:
                spans.append((start, end - start, Token.KEYWORD))
            elif word in BUILTINS and not _after_dot(line, start):
                spans.append((start, end - start, Token.BUILTIN))
            definition = word in ("def", "class")
            continue
        definition = False
        if kind == "string":
            opening = _OPENING.get(match.group().lstrip("rRbBuUfF"))
            if opening is not None:
                close = _CLOSE[opening].match(line, end)
                if close is None:
                    spans.append((start, len(line) - start, Token.STRING))
                    return spans, opening
                end = position = close.end()
            spans.append((start, end - start, Token.STRING))
        elif kind is not None and end > start:
            spans.append((start, end - start, Token(kind)))
    return spans, State.CODE
