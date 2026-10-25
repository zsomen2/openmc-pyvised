"""The Geometry tab: two slice views side by side and the model information on the right."""

from PySide6.QtCore import QByteArray, QEvent, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.schemas import ModelInfo

from ...core.model_session import ModelSession
from ...core.settings import AppSettings, PlotBackground
from ...render.colorize import ColorMode, LineMode, Style
from ...render.palette import RGB, material_colors
from ..dialogs.text_dialog import TextDialog
from ..empty_state import EmptyState
from .info_panel import InfoPanel
from .view_panel import ViewPanel


class ChangeBanner(QFrame):
    """'The model files changed' bar above the views (reload-on-change = ask)."""

    reloadRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        label = QLabel(self.tr("The model files changed on disk."))
        reload = QPushButton(self.tr("Reload"))
        dismiss = QPushButton(self.tr("Dismiss"))
        reload.clicked.connect(self.hide)
        reload.clicked.connect(self.reloadRequested)
        dismiss.clicked.connect(self.hide)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(label, 1)
        layout.addWidget(reload)
        layout.addWidget(dismiss)


class GeometryTab(QWidget):
    cursorText = Signal(str)
    renderTime = Signal(float)
    reloadRequested = Signal()
    allSharp = Signal()
    """Both views show a full-resolution slice of the newly opened model."""

    def __init__(
        self,
        session: ModelSession,
        settings: AppSettings,
        parent: QWidget | None = None,
        language: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.settings = settings
        self.language = language or settings.language
        """UI language of this run, for the model's title and description."""
        self.info: ModelInfo | None = None
        self.color_mode = ColorMode(settings.color_mode)
        self.lines = settings.show_lines
        self.material_colors: dict[int, RGB] = {}
        self.render_style = Style()
        self._announced = -1

        self.empty = EmptyState(
            "mdi6.cube-outline",
            self.tr("No model open"),
            self.tr(
                "Open a Python model builder, a Jupyter notebook or OpenMC XML from the "
                "workspace (File › Open model…)."
            ),
        )
        self.banner = ChangeBanner()
        self.banner.hide()
        self.panels = (
            ViewPanel(session, settings.render_quality),
            ViewPanel(session, settings.render_quality),
        )
        self.info_panel = InfoPanel()
        self.views = QSplitter(Qt.Orientation.Horizontal)
        for panel in self.panels:
            self.views.addWidget(panel)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.views)
        self.splitter.addWidget(self.info_panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([1000, 360])

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        content_layout.addWidget(self.banner)
        content_layout.addWidget(self.splitter, 1)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

        for panel in self.panels:
            panel.hoverText.connect(self.cursorText)
            panel.rendered.connect(self.renderTime)
            panel.sharpShown.connect(self._check_sharp)
        self.banner.reloadRequested.connect(self.reloadRequested)
        self.info_panel.outputRequested.connect(self._show_builder_output)
        self.apply_settings()

    # -- model -----------------------------------------------------------------------

    def show_model(self, info: ModelInfo, reloaded: bool) -> None:
        self.info = info
        self.banner.hide()
        overrides = info.sidecar.material_colors if info.sidecar else None
        self.material_colors = material_colors([m.id for m in info.materials], overrides)
        self.stack.setCurrentIndex(1)
        views = info.sidecar.views if info.sidecar else []
        defaults = self.settings.default_views
        for index, panel in enumerate(self.panels):
            panel.set_style(self.render_style, self.material_colors)
            saved = views[index] if index < len(views) else None
            panel.set_model(info, saved, keep=reloaded, default_basis=defaults[index])
        self.info_panel.set_model(info, self.material_colors, self.language)

    def show_files_changed(self) -> None:
        if self.info is not None:
            self.banner.show()

    def reset_views(self) -> None:
        for panel in self.panels:
            panel.reset_view()

    # -- style ------------------------------------------------------------------------

    def set_color_mode(self, mode: ColorMode) -> None:
        self.color_mode = mode
        self.settings.color_mode = mode.value
        self.apply_settings()

    def set_lines(self, on: bool) -> None:
        self.lines = on
        self.settings.show_lines = on
        self.apply_settings()

    def apply_settings(self) -> None:
        """Colours, lines and quality from the settings and the current theme."""
        palette = self.palette()
        if self.settings.plot_background is PlotBackground.WHITE:
            background, foreground = QColor("white"), QColor("black")
        else:
            background = palette.color(QPalette.ColorRole.Base)
            foreground = palette.color(QPalette.ColorRole.Text)
        self.render_style = Style.for_background(
            (background.red(), background.green(), background.blue()),
            mode=self.color_mode,
            lines=self.lines,
            line_mode=LineMode(self.settings.line_mode),
            line_width=self.settings.line_width,
        )
        for panel in self.panels:
            panel.view.set_colors(background, foreground)
            panel.set_style(self.render_style, self.material_colors)
            panel.set_quality(self.settings.render_quality)

    def refresh_icon(self) -> None:
        self.empty.refresh_icon()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self.apply_settings()  # light/dark switch: plot background, lines, axes

    # -- state ---------------------------------------------------------------------------

    def save_state(self) -> QByteArray:
        return self.splitter.saveState()

    def restore_state(self, state: QByteArray | None) -> None:
        if state is not None:
            self.splitter.restoreState(state)

    def _check_sharp(self) -> None:
        generation = self.session.generation
        if generation != self._announced and all(p.sharp for p in self.panels):
            self._announced = generation
            self.allSharp.emit()

    def _show_builder_output(self) -> None:
        if self.info is not None:
            TextDialog(self.tr("Builder output"), self.info.builder_output, self).exec()
