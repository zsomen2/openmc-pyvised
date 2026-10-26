"""Python syntax colours: the tokenizer line by line, and the highlighter on a document."""

from PySide6.QtGui import QColor, QTextDocument
from PySide6.QtWidgets import QPlainTextEdit

from pyvised.core.inputs import LineKind
from pyvised.render.syntax import COLORS, State, Token, python_spans
from pyvised.ui.input.highlighter import PythonHighlighter


def tokens(line: str, state: State = State.CODE) -> list[tuple[str, Token]]:
    spans, _ = python_spans(line, state)
    return [(line[start : start + length], token) for start, length, token in spans]


def test_names_numbers_and_comments():
    assert tokens("def build(r=3.0, n=0x1F):  # cm") == [
        ("def", Token.KEYWORD),
        ("build", Token.DEFINITION),
        ("3.0", Token.NUMBER),
        ("0x1F", Token.NUMBER),
        ("# cm", Token.COMMENT),
    ]
    assert tokens("class Sphere(object): pass") == [
        ("class", Token.KEYWORD),
        ("Sphere", Token.DEFINITION),
        ("object", Token.BUILTIN),
        ("pass", Token.KEYWORD),
    ]
    assert tokens("x = len(cells) + 1e6 + .5 + v2") == [
        ("len", Token.BUILTIN),
        ("1e6", Token.NUMBER),
        (".5", Token.NUMBER),
    ]
    assert tokens("    @openmc.lru_cache") == [("    @openmc.lru_cache", Token.DECORATOR)]
    assert tokens("cell.id = None") == [("None", Token.KEYWORD)], "an attribute is no builtin"


def test_a_hash_inside_a_string_is_no_comment():
    assert tokens("name = 'cell #10'  # MCNP cell") == [
        ("'cell #10'", Token.STRING),
        ("# MCNP cell", Token.COMMENT),
    ]
    assert tokens('s = f"{x}" + rb"\\"#" # end') == [
        ('f"{x}"', Token.STRING),
        ('rb"\\"#"', Token.STRING),
        ("# end", Token.COMMENT),
    ]
    assert tokens("# 'not a string'") == [("# 'not a string'", Token.COMMENT)]


def test_a_triple_quoted_string_runs_over_lines():
    spans, state = python_spans('doc = """Task 1: an # iron', State.CODE)
    assert state is State.DOUBLE and spans == [(6, 20, Token.STRING)]
    assert python_spans("sphere 'with' a source", state) == ([(0, 22, Token.STRING)], state)
    assert python_spans("", state) == ([], state)
    closing = '"""  # done'
    spans, state = python_spans(closing, state)
    assert state is State.CODE
    assert spans == [(0, 3, Token.STRING), (5, 6, Token.COMMENT)]
    assert python_spans("x = '''one line''' if True else 1")[1] is State.CODE
    assert python_spans("x = r'''raw \\'''")[1] is State.SINGLE, "an escaped quote does not close"


def highlighted(document: QTextDocument, line: int) -> list[tuple[int, int, str]]:
    block = document.findBlockByNumber(line)
    layout = block.layout()
    assert layout is not None
    return [(r.start, r.length, r.format.foreground().color().name()) for r in layout.formats()]


def test_the_highlighter_colours_blocks_and_keeps_notebook_text_plain(qapp):
    editor = QPlainTextEdit()  # a bare QTextDocument has no layout to highlight
    document = editor.document()
    highlighter = PythonHighlighter(document, dark=False)
    light = COLORS[False]
    document.setPlainText('"""doc\n# not a comment"""\nimport openmc\ns = "🙂"  # c\n')
    assert highlighted(document, 1) == [(0, 18, light[Token.STRING])]
    assert highlighted(document, 2) == [(0, 6, light[Token.KEYWORD])]
    assert highlighted(document, 3) == [  # the emoji takes two UTF-16 units
        (4, 4, light[Token.STRING]),
        (10, 3, light[Token.COMMENT]),
    ]
    highlighter.set_dark(True)
    assert highlighted(document, 2)[0][2] == QColor(COLORS[True][Token.KEYWORD]).name()

    kinds = [LineKind.HEADER, LineKind.CODE, LineKind.TEXT, LineKind.HEADER, LineKind.CODE]
    highlighter.set_lines(kinds)
    document.setPlainText("── In [1] ──\ns = '''open\n# Markdown 'text'\n── In [2] ──\nx = 1")
    assert [r[0:2] for r in highlighted(document, 0)] == [(0, 12)], "the header is bold"
    assert highlighted(document, 2) == [], "Markdown stays plain"
    assert highlighted(document, 4) == [(4, 1, COLORS[True][Token.NUMBER])], "the string ended"
    highlighter.set_lines(None, enabled=False)
    document.setPlainText("import openmc")
    assert highlighted(document, 0) == [], "very large files stay plain"
