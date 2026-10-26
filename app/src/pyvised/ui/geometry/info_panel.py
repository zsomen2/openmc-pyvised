"""Model information next to the views: summary, materials, geometry tree, settings, tallies.

One tree with collapsible sections. The geometry branch expands lazily
(universe -> cells -> fills -> lattices), so large models stay quick; rebuilt
MCNP decks keep their numbers, so it reads like the deck. Selecting a
material, cell, universe or lattice highlights it in the views; double-clicking
a material changes its colour. An inspected point gets its own section at the
top: the nesting path from the root universe down.
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pyvised_protocol.schemas import (
    CellInfo,
    InspectResult,
    LatticeInfo,
    ModelInfo,
    UniverseInfo,
)

from ...core.icons import icon
from ...render.palette import RGB
from ...render.views import AXIS_NAMES

_NODE = Qt.ItemDataRole.UserRole
"""Item data: ("universe" | "lattice", id) for geometry nodes that expand lazily."""
_SELECT = Qt.ItemDataRole.UserRole + 1
"""Item data: ("material" | "cell" | "universe", id) of what selecting the item highlights."""


def swatch(rgb: RGB, size: int = 12) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(*rgb))
    return QIcon(pixmap)


def _number(value: float) -> str:
    return f"{value:.6g}"


class InfoPanel(QWidget):
    outputRequested = Signal()
    highlightRequested = Signal(object)
    """("material" | "cell" | "universe", id) of the selected item, or None."""
    colorEditRequested = Signal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.info: ModelInfo | None = None
        self._cells: dict[int, CellInfo] = {}
        self._universes: dict[int, UniverseInfo] = {}
        self._lattices: dict[int, LatticeInfo] = {}

        self.title = QLabel()
        font = self.title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self.title.setFont(font)
        self.title.setWordWrap(True)
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(14)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemExpanded.connect(self._expand)
        self.tree.currentItemChanged.connect(self._selected)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self._point: InspectResult | None = None
        self.output_button = QPushButton(self.tr("Builder output…"))
        self.output_button.clicked.connect(self.outputRequested)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 0, 0)
        layout.addWidget(self.title)
        layout.addWidget(self.description)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.output_button)
        self.clear()

    def clear(self) -> None:
        self.info = None
        self._point = None
        self.title.setText(self.tr("No model open"))
        self.description.hide()
        self.tree.clear()
        self.output_button.hide()

    def set_model(self, info: ModelInfo, colors: dict[int, RGB], language: str = "en") -> None:
        expanded = self._expanded_sections()
        self.info = info
        self._cells = {c.id: c for c in info.cells}
        self._universes = {u.id: u for u in info.universes}
        self._lattices = {lat.id: lat for lat in info.lattices}
        sidecar = info.sidecar
        title = sidecar.title.get(language) if sidecar else ""
        self.title.setText(title or info.source.rsplit("/", 1)[-1])
        description = sidecar.description.get(language) if sidecar else ""
        self.description.setText(description)
        self.description.setVisible(bool(description))
        self.output_button.setVisible(bool(info.builder_output.strip()))

        self.tree.blockSignals(True)
        self.tree.clear()
        self.tree.blockSignals(False)
        self._point = None
        self._model_section(info)
        self._materials_section(info, colors)
        self._geometry_section(info)
        self._settings_section(info)
        self._tallies_section(info)
        for index, item in enumerate(self._sections()):
            item.setExpanded(item.data(0, _NODE) in expanded if expanded else index < 2)

    def set_material_colors(self, colors: dict[int, RGB]) -> None:
        """New swatches after a colour change."""
        section = self._section_item("materials")
        if section is None:
            return
        for i in range(section.childCount()):
            item = section.child(i)
            selection = item.data(0, _SELECT) if item is not None else None
            if item is not None and selection and selection[1] in colors:
                item.setIcon(0, swatch(colors[selection[1]]))

    def clear_selection(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clearSelection()
        self.tree.setCurrentItem(None)  # pyright: ignore[reportArgumentType]
        self.tree.blockSignals(False)

    def show_point(self, result: InspectResult | None) -> None:
        """The nesting path at an inspected point, as the first section."""
        old = self._section_item("point")
        if old is not None:
            self.tree.blockSignals(True)
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(old))
            self.tree.blockSignals(False)
        self._point = result
        if result is None:
            return
        x, y, z = result.point
        title = self.tr("Point ({x}, {y}, {z}) cm").format(x=_number(x), y=_number(y), z=_number(z))
        section = QTreeWidgetItem([title])
        font = section.font(0)
        font.setBold(True)
        section.setFont(0, font)
        section.setData(0, _NODE, "point")
        self.tree.insertTopLevelItem(0, section)
        section.setFirstColumnSpanned(True)
        if not result.found:
            self._row(section, self.tr("outside the geometry"))
        for step in result.path:
            cell = self.tr("cell {id}").format(id=step.cell)
            if step.cell_name:
                cell += f"  {step.cell_name}"
            where = self.tr("universe {id}").format(id=step.universe)
            if step.lattice is not None and step.lattice_index is not None:
                index = ", ".join(str(i) for i in step.lattice_index)
                where += " · " + self.tr("lattice {id} [{index}]").format(
                    id=step.lattice, index=index
                )
            label = self.tr("level {level}: {cell}").format(level=step.level, cell=cell)
            row = self._row(section, label, where)
            row.setData(0, _SELECT, ("cell", step.cell))
            row.setToolTip(0, "({:.6g}, {:.6g}, {:.6g}) cm".format(*step.local))
            if step.overlapping:
                cells = ", ".join(self.tr("cell {id}").format(id=c) for c in step.overlapping)
                warning = self._row(row, self.tr("overlaps with"), cells)
                warning.setIcon(0, icon("mdi6.alert-outline"))
                row.setExpanded(True)
        if result.found:
            if result.material is None:
                self._row(section, self.tr("Material"), self.tr("void"))
            else:
                name = f" {result.material_name}" if result.material_name else ""
                value = self.tr("material {id}").format(id=result.material) + name
                row = self._row(section, self.tr("Material"), value)
                row.setData(0, _SELECT, ("material", result.material))
            if result.density is not None:
                self._row(section, self.tr("Density"), f"{result.density:.5g} g/cm³")
            if result.temperature is not None:
                self._row(section, self.tr("Temperature"), f"{result.temperature:g} K")
            if result.instance is not None:
                self._row(section, self.tr("Instance"), str(result.instance))
        section.setExpanded(True)
        self.tree.scrollToItem(section)

    # -- sections ----------------------------------------------------------------------

    def _section(self, key: str, title: str, count: int | None = None) -> QTreeWidgetItem:
        item = QTreeWidgetItem([title if count is None else f"{title} ({count})"])
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        item.setData(0, _NODE, key)
        self.tree.addTopLevelItem(item)
        item.setFirstColumnSpanned(True)
        return item

    def _sections(self) -> list[QTreeWidgetItem]:
        items = (self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount()))
        return [item for item in items if item is not None]

    def _section_item(self, key: str) -> QTreeWidgetItem | None:
        return next((item for item in self._sections() if item.data(0, _NODE) == key), None)

    def _expanded_sections(self) -> set[str]:
        return {item.data(0, _NODE) for item in self._sections() if item.isExpanded()}

    def _row(self, parent: QTreeWidgetItem, name: str, value: str = "") -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [name, value])
        item.setToolTip(1, value)
        return item

    def _model_section(self, info: ModelInfo) -> None:
        section = self._section("model", self.tr("Model"))
        kinds = {
            "python": self.tr("Python builder"),
            "notebook": self.tr("Jupyter notebook"),
            "xml": self.tr("OpenMC XML"),
        }
        self._row(section, self.tr("Source"), info.source)
        kind = kinds.get(info.kind, info.kind)
        if info.kind != "xml":
            kind += self.tr(", exported in {seconds:.1f} s").format(seconds=info.build_seconds)
        self._row(section, self.tr("Type"), kind)
        if info.library:
            self._row(section, self.tr("Library"), info.library)
        if info.params:
            text = ", ".join(f"{k} = {_number(v)}" for k, v in info.params.items())
            self._row(section, self.tr("Parameters"), text)
        box = info.bounding_box
        for axis, lo, hi, clipped in zip(
            AXIS_NAMES, box.lower_left, box.upper_right, box.clipped, strict=True
        ):
            value = f"{_number(lo)} … {_number(hi)} cm"
            if clipped:
                value += " " + self.tr("(unbounded)")
            self._row(section, self.tr("Extent in {axis}").format(axis=axis), value)
        counts = self.tr(
            "{cells} cells, {surfaces} surfaces, {universes} universes, {lattices} lattices"
        ).format(
            cells=len(info.cells),
            surfaces=len(info.surfaces),
            universes=len(info.universes),
            lattices=len(info.lattices),
        )
        self._row(section, self.tr("Size"), counts)
        for warning in info.warnings:
            row = self._row(section, self.tr("Warning"), warning)
            row.setIcon(0, icon("mdi6.alert-outline"))

    def _materials_section(self, info: ModelInfo, colors: dict[int, RGB]) -> None:
        section = self._section("materials", self.tr("Materials"), len(info.materials))
        for mat in sorted(info.materials, key=lambda m: m.id):
            density = ""
            if mat.mass_density is not None:
                density = f"{mat.mass_density:.4g} g/cm³"
            elif mat.density is not None:
                density = f"{mat.density:.4g} {mat.density_units}"
            item = self._row(section, f"{mat.id}  {mat.name}".rstrip(), density)
            item.setData(0, _SELECT, ("material", mat.id))
            item.setToolTip(0, self.tr("Double click to change the colour"))
            if mat.id in colors:
                item.setIcon(0, swatch(colors[mat.id]))
            if mat.atom_density is not None:
                self._row(item, self.tr("Atom density"), f"{mat.atom_density:.5e} /b-cm")
            if mat.temperature is not None:
                self._row(item, self.tr("Temperature"), f"{mat.temperature:g} K")
            for nuclide in mat.nuclides:
                kind = self.tr("atom") if nuclide.kind == "ao" else self.tr("weight")
                self._row(item, nuclide.name, f"{nuclide.fraction:.6g} ({kind})")
            for table in mat.sab:
                self._row(item, "S(α,β)", table)

    def _geometry_section(self, info: ModelInfo) -> None:
        section = self._section("geometry", self.tr("Cells and universes"), len(info.cells))
        root = self._universes.get(info.root_universe)
        for cid in root.cells if root else []:
            self._cell_item(section, cid)

    def _settings_section(self, info: ModelInfo) -> None:
        s = info.settings
        section = self._section("settings", self.tr("Settings"))
        modes = {
            "eigenvalue": self.tr("eigenvalue (k-eff)"),
            "fixed source": self.tr("fixed source"),
        }
        self._row(section, self.tr("Run mode"), modes.get(s.run_mode, s.run_mode))
        if s.particles is not None:
            self._row(section, self.tr("Particles per batch"), str(s.particles))
        if s.batches is not None:
            batches = str(s.batches)
            if s.inactive:
                batches += " " + self.tr("({inactive} inactive)").format(inactive=s.inactive)
            self._row(section, self.tr("Batches"), batches)
        self._row(
            section,
            self.tr("Photon transport"),
            self.tr("on") if s.photon_transport else self.tr("off"),
        )
        for source in s.sources:
            parts = [p for p in (source.space, source.energy, source.angle) if p]
            text = f"{source.particle}: " + ", ".join(parts) if parts else source.particle
            self._row(section, self.tr("Source"), text)
        if info.weight_windows:
            state = self.tr("on") if s.weight_windows_on else self.tr("defined, off")
            ww = self._row(section, self.tr("Weight windows"), state)
            for w in info.weight_windows:
                dims = "×".join(str(d) for d in w.dimension)
                text = f"{w.particle}, {w.mesh_kind} {dims}"
                if w.lower_min is not None and w.lower_max is not None:
                    text += self.tr(", lower bound {low:.3g} … {high:.3g}").format(
                        low=w.lower_min, high=w.lower_max
                    )
                self._row(ww, self.tr("Mesh {id}").format(id=w.mesh_id), text)

    def _tallies_section(self, info: ModelInfo) -> None:
        section = self._section("tallies", self.tr("Tallies"), len(info.tallies))
        normalised = info.sidecar.tallies if info.sidecar else {}
        for tally in info.tallies:
            name = f"{tally.id}  {tally.name}".rstrip()
            item = self._row(section, name, ", ".join(tally.scores))
            for f in tally.filters:
                self._row(item, f.kind, f.summary or str(f.bins))
            if tally.nuclides:
                self._row(item, self.tr("Nuclides"), ", ".join(tally.nuclides))
            norm = normalised.get(tally.id)
            if norm is not None:
                text = f"× {norm.source_strength:g} × {norm.multiplier:g}"
                if norm.divide_by != "none" and norm.divisor:
                    text += f" / {norm.divisor:g}"
                if norm.unit:
                    text += f" → {norm.unit}"
                row = self._row(item, self.tr("Normalisation"), text)
                if norm.note:
                    row.setToolTip(1, norm.note)

    # -- geometry nodes (expanded lazily) ---------------------------------------------------

    def _cell_item(self, parent: QTreeWidgetItem, cid: int) -> None:
        cell = self._cells.get(cid)
        if cell is None:
            return
        name = self.tr("cell {id}").format(id=cid) + (f"  {cell.name}" if cell.name else "")
        if cell.fill_type == "material":
            mat = cell.fill
            value = self.tr("material {id}").format(id=mat)
            if cell.density is not None:
                value += f", {cell.density:.4g} g/cm³"
        elif cell.fill_type == "void":
            value = self.tr("void")
        elif cell.fill_type == "universe":
            value = self.tr("universe {id}").format(id=cell.fill)
        elif cell.fill_type == "lattice":
            value = self.tr("lattice {id}").format(id=cell.fill)
        else:
            value = self.tr("distributed materials")
        item = self._row(parent, name, value)
        item.setData(0, _SELECT, ("cell", cid))
        if cell.region:
            item.setToolTip(0, cell.region)
        if cell.fill_type == "universe" and cell.fill is not None:
            self._lazy(item, "universe", cell.fill)
        elif cell.fill_type == "lattice" and cell.fill is not None:
            self._lattice_item(item, cell.fill)

    def _lattice_item(self, parent: QTreeWidgetItem, lid: int) -> None:
        lattice = self._lattices.get(lid)
        if lattice is None:
            return
        name = self.tr("lattice {id}").format(id=lid) + (
            f"  {lattice.name}" if lattice.name else ""
        )
        shape = "×".join(str(n) for n in lattice.shape)
        pitch = " × ".join(_number(p) for p in lattice.pitch)
        value = self.tr("{shape}, pitch {pitch} cm").format(shape=shape, pitch=pitch)
        item = self._row(parent, name, value)
        item.setData(0, _SELECT, ("universe", lid))
        self._lazy(item, "lattice", lid)

    def _lazy(self, item: QTreeWidgetItem, kind: str, node_id: int) -> None:
        item.setData(0, _NODE, (kind, node_id))
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)

    def _expand(self, item: QTreeWidgetItem) -> None:
        node = item.data(0, _NODE)
        if not isinstance(node, tuple) or item.childCount() > 0:
            return
        kind, node_id = node
        if kind == "universe":
            universe = self._universes.get(node_id)
            for cid in universe.cells if universe else []:
                self._cell_item(item, cid)
        elif kind == "lattice":
            lattice = self._lattices.get(node_id)
            if lattice is None:
                return
            for uid in lattice.universes:
                universe = self._universes.get(uid)
                count = len(universe.cells) if universe else 0
                child = self._row(
                    item,
                    self.tr("universe {id}").format(id=uid),
                    self.tr("{count} cells").format(count=count),
                )
                child.setData(0, _SELECT, ("universe", uid))
                self._lazy(child, "universe", uid)
            if lattice.outer is not None:
                child = self._row(item, self.tr("outer universe {id}").format(id=lattice.outer))
                child.setData(0, _SELECT, ("universe", lattice.outer))
                self._lazy(child, "universe", lattice.outer)
        item.setChildIndicatorPolicy(
            QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless
        )

    # -- selection -------------------------------------------------------------------------

    def _selected(self, current: QTreeWidgetItem | None, previous=None) -> None:
        selection = current.data(0, _SELECT) if current is not None else None
        self.highlightRequested.emit(tuple(selection) if selection else None)

    def _double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        selection = item.data(0, _SELECT)
        if selection and selection[0] == "material":
            self.colorEditRequested.emit("material", int(selection[1]))
