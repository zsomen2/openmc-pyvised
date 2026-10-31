"""The Input tab: the open model's own code, read-only.

On the left the model's files (the model file in bold, then the other `.py` and
`.ipynb` files of its folder) and, when a run is selected in the Simulation tab,
the copies of them that the run kept. On the right the chosen file with line
numbers and Python colours; a notebook shows its cells. Find (Ctrl+F, F3) and go
to line (Ctrl+G) work on the shown file. The tab follows the session's file
watcher whatever the reload setting says: a changed file is read again and keeps
its scroll position. Editing happens elsewhere: "Open in editor" takes VS Code or
Notepad, never the default program of `.py` files (that may run them).
"""

import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QByteArray, QPoint, QProcess, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QGuiApplication,
    QIntValidator,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.runs import RunInfo

from ...core.icons import icon
from ...core.inputs import (
    InputText,
    TooLarge,
    editor_command,
    find_all,
    input_files,
    is_input,
    load_input,
    run_input_files,
    utf16_index,
    vscode,
)
from ...core.model_session import ModelSession
from ...core.settings import AppSettings
from ..empty_state import EmptyState
from .code_view import CodeView, Position

PATH_ROLE = Qt.ItemDataRole.UserRole
GAP = 8
"""[px] Space above the run's group in the file list."""


def start_detached(program: str, arguments: list[str], native: str = "") -> bool:
    """Start a program that outlives the app; `native` is a Windows command line tail passed
    as it is (tests replace this function)."""
    process = QProcess()
    process.setProgram(program)
    process.setArguments(arguments)
    if native:
        process.setNativeArguments(native)
    ok, _pid = process.startDetached()
    return ok


class InputTab(QWidget):
    statusMessage = Signal(str)

    def __init__(
        self, session: ModelSession, settings: AppSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.settings = settings
        self.model: Path | None = None
        self.run: RunInfo | None = None
        """The run selected in the Simulation tab."""
        self.run_folder: Path | None = None
        self.shown: Path | None = None
        self.loaded: InputText | None = None
        """The shown file as the code view has it."""
        self.matches: list[tuple[int, int]] = []
        self.current_match: int | None = None
        self._positions: dict[Path, Position] = {}
        """Where each file was left (`CodeView.position`), for switching back to it."""

        self.empty = EmptyState(
            "mdi6.file-code-outline",
            self.tr("No model open"),
            self.tr("Open a model to see its input files."),
        )
        self.xml_state = EmptyState(
            "mdi6.file-code-outline",
            self.tr("No Python input"),
            self.tr(
                "The Input tab shows Python model builders and Jupyter notebooks; this model "
                "is OpenMC XML."
            ),
        )
        self.files = QListWidget()
        self.files.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self._file_menu)
        self.files.currentItemChanged.connect(self._file_chosen)
        self.files.setMinimumWidth(140)

        self.file_label = QLabel()
        self.file_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.find_field = QLineEdit()
        self.find_field.setPlaceholderText(self.tr("Find…"))
        self.find_field.setClearButtonEnabled(True)
        self.find_field.setToolTip(
            self.tr("Find in the shown file (Ctrl+F); Enter or F3: next, Shift+F3: previous")
        )
        self.find_field.setFixedWidth(220)
        self.find_field.textChanged.connect(lambda: self.search(move=True))
        self.find_field.returnPressed.connect(self._find_return)
        self.prev_button = QToolButton()
        self.prev_button.setAutoRaise(True)
        self.prev_button.setToolTip(self.tr("Previous match (Shift+F3)"))
        self.prev_button.clicked.connect(self.find_previous)
        self.next_button = QToolButton()
        self.next_button.setAutoRaise(True)
        self.next_button.setToolTip(self.tr("Next match (F3)"))
        self.next_button.clicked.connect(self.find_next)
        self.count_label = QLabel()
        self.count_label.setMinimumWidth(self.fontMetrics().horizontalAdvance("0000/0000"))
        self.line = QLineEdit()
        self.line.setValidator(QIntValidator(1, 99_999_999, self.line))
        self.line.setFixedWidth(80)
        self.line.setToolTip(self.tr("Go to line (Ctrl+G)"))
        self.line.returnPressed.connect(self._go_to_line)
        line_label = QLabel(self.tr("Line:"))
        line_label.setBuddy(self.line)
        self.act_open = QAction(self.tr("Open in editor"), self)
        self.act_open.setToolTip(
            self.tr("Open the file in VS Code (or Notepad) to edit it; PyVised shows the changes")
        )
        self.act_open.triggered.connect(lambda: self.open_in_editor())
        self.act_folder = QAction(self.tr("Show in folder"), self)
        self.act_folder.triggered.connect(lambda: self.show_in_folder())
        self.act_copy = QAction(self.tr("Copy path"), self)
        self.act_copy.triggered.connect(lambda: self.copy_path())
        menu = QMenu(self)
        menu.addAction(self.act_folder)
        menu.addAction(self.act_copy)
        self.open_button = QToolButton()
        self.open_button.setDefaultAction(self.act_open)
        self.open_button.setMenu(menu)
        self.open_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.open_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        self.code = CodeView(settings.input_font_size)
        self.code.fontSizeChanged.connect(self._font_size_changed)
        self.message = QLabel(alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
        self.message.setMargin(24)
        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.code)
        self.view_stack.addWidget(self.message)

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.file_label, 1)
        bar.addWidget(self.find_field)
        bar.addWidget(self.prev_button)
        bar.addWidget(self.next_button)
        bar.addWidget(self.count_label)
        bar.addSpacing(8)
        bar.addWidget(line_label)
        bar.addWidget(self.line)
        bar.addSpacing(8)
        bar.addWidget(self.open_button)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addLayout(bar)
        right_layout.addWidget(self.view_stack, 1)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.files)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([240, 1000])
        self.content = QWidget()
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.addWidget(self.splitter)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.xml_state)
        self.stack.addWidget(self.content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

        # window-wide, but only while this tab is visible
        for keys, slot in (
            (QKeySequence.StandardKey.Find, self.focus_find),
            ("Ctrl+G", self.focus_line),
            ("F3", self.find_next),
            ("Shift+F3", self.find_previous),
        ):
            QShortcut(QKeySequence(keys), self).activated.connect(slot)
        escape = QShortcut(QKeySequence("Escape"), self.find_field)
        escape.setContext(Qt.ShortcutContext.WidgetShortcut)
        escape.activated.connect(self.code.setFocus)

        session.filesChanged.connect(self.refresh)
        self.refresh_icons()
        self.refresh()

    # -- model and run -------------------------------------------------------------------------

    def show_model(self, path: Path | None) -> None:
        """Show the code of the model at `path`, newly opened or reloaded."""
        if path != self.model:
            self.model, self.shown, self.loaded = path, None, None
            self._positions.clear()
        self.refresh()

    def set_run(self, run: RunInfo | None, folder: Path | None) -> None:
        """The run selected in the Simulation tab: its copies of the code join the list."""
        before = self.run.id if self.run is not None else None
        self.run = run
        self.run_folder = folder if run is not None else None
        if (run.id if run is not None else None) != before:
            self.refresh()

    def run_title(self, run: RunInfo) -> str:
        created = run.created.astimezone()
        today = created.date() == datetime.now().astimezone().date()
        time = created.strftime("%H:%M" if today else "%Y-%m-%d %H:%M")
        if run.label:
            return self.tr("Run {time} “{label}”").format(time=time, label=run.label)
        return self.tr("Run {time}").format(time=time)

    def refresh(self) -> None:
        """Read the file list and the shown file again (the files changed on disk); the shown
        file keeps its place."""
        if self.model is None or not is_input(self.model):
            self.stack.setCurrentWidget(self.empty if self.model is None else self.xml_state)
            self.files.clear()
            self.shown, self.loaded = None, None
            self.code.show_text("", None)
            self.search(move=False)
            self._update_bar()
            return
        self.stack.setCurrentWidget(self.content)
        self._fill_list()
        item = self._item_of(self.shown) or self._first_file()
        self.files.blockSignals(True)
        if item is not None:
            self.files.setCurrentItem(item)
        self.files.blockSignals(False)
        self._show(self._path_of(item))

    # -- the file list -----------------------------------------------------------------------------

    def _fill_list(self) -> None:
        assert self.model is not None
        self.files.blockSignals(True)
        self.files.clear()
        self._add_header(self.tr("Model files"))
        for path in input_files(self.model):
            self._add_file(path, bold=path == self.model)
        run, folder = self.run, self.run_folder
        copies = run_input_files(folder, PurePosixPath(run.model).name) if run and folder else []
        if run is not None and copies:
            gap = QListWidgetItem()
            gap.setFlags(Qt.ItemFlag.NoItemFlags)
            gap.setSizeHint(QSize(0, GAP))
            self.files.addItem(gap)
            self._add_header(self.run_title(run), str(folder))
            model = PurePosixPath(run.model).name
            for path in copies:
                self._add_file(path, bold=path.name == model)
        self.files.blockSignals(False)

    def _add_header(self, text: str, tooltip: str = "") -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setToolTip(tooltip)
        self.files.addItem(item)

    def _add_file(self, path: Path, bold: bool) -> None:
        item = QListWidgetItem(self._file_icon(path), path.name)
        item.setData(PATH_ROLE, str(path))
        item.setToolTip(str(path))
        if bold:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        self.files.addItem(item)

    @staticmethod
    def _file_icon(path: Path):
        notebook = path.suffix.lower() == ".ipynb"
        return icon("mdi6.notebook-outline" if notebook else "mdi6.language-python")

    @staticmethod
    def _path_of(item: QListWidgetItem | None) -> Path | None:
        value = item.data(PATH_ROLE) if item is not None else None
        return Path(value) if value else None

    def _file_items(self) -> list[QListWidgetItem]:
        items = (self.files.item(row) for row in range(self.files.count()))
        return [item for item in items if self._path_of(item) is not None]

    def _item_of(self, path: Path | None) -> QListWidgetItem | None:
        return next((i for i in self._file_items() if self._path_of(i) == path), None)

    def _first_file(self) -> QListWidgetItem | None:
        items = self._file_items()
        return items[0] if items else None

    def file_names(self) -> list[str]:
        """The list as shown: group titles, then file names."""
        return [self.files.item(row).text() for row in range(self.files.count())]

    def _file_chosen(self, item: QListWidgetItem | None, _previous=None) -> None:
        path = self._path_of(item)
        if path is not None and path != self.shown:
            self._show(path)

    def _file_menu(self, position: QPoint) -> None:
        path = self._path_of(self.files.itemAt(position))
        if path is not None:
            self.file_menu(path).exec(self.files.viewport().mapToGlobal(position))

    def file_menu(self, path: Path) -> QMenu:
        menu = QMenu(self)
        menu.addAction(icon("mdi6.open-in-new"), self.tr("Open in editor")).triggered.connect(
            lambda: self.open_in_editor(path)
        )
        menu.addAction(self.tr("Show in folder")).triggered.connect(
            lambda: self.show_in_folder(path)
        )
        menu.addAction(self.tr("Copy path")).triggered.connect(lambda: self.copy_path(path))
        return menu

    # -- the shown file ----------------------------------------------------------------------------

    def _show(self, path: Path | None) -> None:
        """Show `path`: the same file again keeps its place (a reload), another one returns
        to where it was left."""
        if self.shown is not None and self.loaded is not None:
            self._positions[self.shown] = self.code.position()
        same = path == self.shown
        self.shown = path
        try:
            loaded = load_input(path) if path is not None else None
        except TooLarge as exc:
            assert path is not None
            text = self.tr("{name} is too large to show here ({size} MB). Open it in an editor.")
            self._show_message(text.format(name=path.name, size=f"{exc.size / 1e6:.1f}"))
            return
        except OSError as exc:
            assert path is not None
            text = self.tr("{name} could not be read: {error}")
            self._show_message(text.format(name=path.name, error=exc.strerror or exc))
            return
        if loaded is None:
            self._show_message(self.tr("The model's files were not found."))
            return
        self.view_stack.setCurrentWidget(self.code)
        previous = self.loaded
        self.loaded = loaded
        unchanged = (
            same
            and previous is not None
            and (previous.text, previous.kinds) == (loaded.text, loaded.kinds)
        )
        if not unchanged:
            self.code.show_text(loaded.text, loaded.kinds)
            position = self._positions.get(path) if path is not None else None
            if position is not None:
                self.code.restore_position(position)
            self.search(move=False)
        self._update_bar()

    def _show_message(self, text: str) -> None:
        self.loaded = None
        self.message.setText(text)
        self.view_stack.setCurrentWidget(self.message)
        self.code.show_text("", None)
        self.search(move=False)
        self._update_bar()

    def is_run_copy(self, path: Path) -> bool:
        return self.run_folder is not None and path.is_relative_to(self.run_folder)

    def _update_bar(self) -> None:
        path, loaded = self.shown, self.loaded
        parts = [path.name] if path is not None else []
        if path is not None and self.run is not None and self.is_run_copy(path):
            parts.append(self.run_title(self.run))
        if loaded is not None and loaded.broken:
            parts.append(self.tr("not a valid notebook, shown as text"))
        self.file_label.setText("  ·  ".join(parts))
        self.file_label.setToolTip(str(path) if path is not None else "")
        for action in (self.act_open, self.act_folder, self.act_copy):
            action.setEnabled(path is not None)
        for widget in (self.find_field, self.line, self.prev_button, self.next_button):
            widget.setEnabled(loaded is not None)
        lines = self.code.blockCount() if loaded is not None else 0
        self.line.setPlaceholderText(f"1–{lines}" if lines else "")

    # -- find and go to line -----------------------------------------------------------------------

    def search(self, move: bool = True) -> None:
        """Find the find field's text in the shown file; `move` selects the first match from the
        cursor on, otherwise the matches are only tinted (a file was read) and F3 goes on from
        the cursor."""
        text = self.loaded.text if self.loaded is not None else ""
        position = utf16_index(text)
        spans = find_all(text, self.find_field.text())
        self.matches = [(position(a), position(b)) for a, b in spans]
        cursor = self.code.textCursor()
        selected = (cursor.selectionStart(), cursor.selectionEnd())
        self.current_match = self.matches.index(selected) if selected in self.matches else None
        if self.matches and move:
            ahead = (i for i, (a, _b) in enumerate(self.matches) if a >= selected[0])
            self.current_match = next(ahead, 0)
        self.code.set_matches(self.matches, self.current_match, select=move)
        self._update_count()

    def _step(self, step: int) -> None:
        if not self.find_field.text():
            self.focus_find()
            return
        if not self.matches:
            return
        if self.current_match is None:  # from the cursor on
            start = self.code.textCursor().selectionStart()
            ahead = [i for i, (a, _b) in enumerate(self.matches) if a >= start]
            first = ahead[0] if ahead else len(self.matches)
            index = first if step > 0 else first - 1
        else:
            index = self.current_match + step
        self.current_match = index % len(self.matches)
        self.code.set_matches(self.matches, self.current_match, select=True)
        self._update_count()

    def find_next(self) -> None:
        self._step(1)

    def find_previous(self) -> None:
        self._step(-1)

    def _find_return(self) -> None:
        shift = QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
        self._step(-1 if shift else 1)

    def _update_count(self) -> None:
        if not self.find_field.text():
            self.count_label.clear()
        elif not self.matches:
            self.count_label.setText(self.tr("no matches"))
        else:
            current = "–" if self.current_match is None else str(self.current_match + 1)
            self.count_label.setText(f"{current}/{len(self.matches)}")

    def _go_to_line(self) -> None:
        text = self.line.text().strip()
        if text.isdigit() and self.loaded is not None:
            self.code.go_to_line(int(text))
            self.code.setFocus()

    def focus_find(self) -> None:
        self.find_field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.find_field.selectAll()

    def focus_line(self) -> None:
        self.line.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.line.selectAll()

    def focus_code(self) -> None:
        if self.stack.currentWidget() is self.content:
            self.code.setFocus()

    def _font_size_changed(self, size: int) -> None:
        self.settings.input_font_size = size

    # -- elsewhere ---------------------------------------------------------------------------------

    def open_in_editor(self, path: Path | None = None) -> None:
        """Open the file in VS Code, else Notepad (never with the default program of `.py`
        files, which may run it); a notebook without VS Code shows its folder."""
        path = path or self.shown
        if path is None:
            return
        command = editor_command(path, vscode())
        if command is None:
            self.statusMessage.emit(
                self.tr("VS Code was not found, so the notebook's folder is shown instead.")
            )
            self.show_in_folder(path)
            return
        if not start_detached(command[0], command[1:]):
            QMessageBox.warning(
                self,
                self.tr("Open in editor"),
                self.tr("{program} could not be started.").format(program=command[0]),
            )

    def show_in_folder(self, path: Path | None = None) -> None:
        path = path or self.shown
        if path is None:
            return
        if sys.platform == "win32" and start_detached("explorer.exe", [], f'/select,"{path}"'):
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def copy_path(self, path: Path | None = None) -> None:
        path = path or self.shown
        if path is None:
            return
        QGuiApplication.clipboard().setText(str(path))
        self.statusMessage.emit(self.tr("Copied {path}").format(path=path))

    # -- look and state ----------------------------------------------------------------------------

    def refresh_icons(self) -> None:
        self.empty.refresh_icon()
        self.xml_state.refresh_icon()
        self.prev_button.setIcon(icon("mdi6.chevron-up"))
        self.next_button.setIcon(icon("mdi6.chevron-down"))
        self.act_open.setIcon(icon("mdi6.open-in-new"))
        self.act_folder.setIcon(icon("mdi6.folder-search-outline"))
        self.act_copy.setIcon(icon("mdi6.content-copy"))
        for item in self._file_items():
            path = self._path_of(item)
            if path is not None:
                item.setIcon(self._file_icon(path))

    def save_state(self) -> QByteArray:
        return self.splitter.saveState()

    def restore_state(self, state: QByteArray | None) -> None:
        if state is not None:
            self.splitter.restoreState(state)
