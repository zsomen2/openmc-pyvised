"""`QSyntaxHighlighter` around `render.syntax`: Python colours line by line, with a light and
a dark colour set; a notebook's header and Markdown lines stay plain."""

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument

from ...core.inputs import LineKind, utf16_index
from ...render.syntax import COLORS, State, Token, python_spans


class PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, document: QTextDocument, dark: bool = False) -> None:
        super().__init__(document)
        self.kinds: list[LineKind] | None = None
        """What each line is (a notebook); None: all Python code."""
        self.enabled = True
        self.formats: dict[Token, QTextCharFormat] = {}
        self.header = QTextCharFormat()
        self.header.setFontWeight(QFont.Weight.Bold)
        self._set_colors(dark)

    def _set_colors(self, dark: bool) -> None:
        self.dark = dark
        self.formats = {}
        for token, color in COLORS[dark].items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self.formats[token] = fmt

    def set_dark(self, dark: bool) -> None:
        if dark != self.dark:
            self._set_colors(dark)
            self.rehighlight()

    def set_lines(self, kinds: list[LineKind] | None, enabled: bool = True) -> None:
        """Set before the document's text: the line kinds and whether to colour at all (very
        large files stay plain)."""
        self.kinds = kinds
        self.enabled = enabled

    def highlightBlock(self, text: str) -> None:
        kind = LineKind.CODE
        if self.kinds is not None:
            number = self.currentBlock().blockNumber()
            kind = self.kinds[number] if number < len(self.kinds) else LineKind.TEXT
        if kind is LineKind.HEADER:
            self.setFormat(0, len(text), self.header)  # header lines are plain ASCII + box lines
        if kind is not LineKind.CODE or not self.enabled:
            self.setCurrentBlockState(State.CODE)
            return
        previous = self.previousBlockState()
        state = State(previous) if previous in (State.SINGLE, State.DOUBLE) else State.CODE
        spans, state = python_spans(text, state)
        position = utf16_index(text)
        for start, length, token in spans:
            first = position(start)
            self.setFormat(first, position(start + length) - first, self.formats[token])
        self.setCurrentBlockState(state)
