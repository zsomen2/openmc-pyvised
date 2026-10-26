"""Pydantic models exchanged over the HTTP API."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .sidecar import Basis, Sidecar

MAX_SLICE_PIXELS = 4096
"""Largest slice edge in pixels the backend renders."""

PixelCount = Annotated[int, Field(gt=0, le=MAX_SLICE_PIXELS)]
Extent = Annotated[float, Field(gt=0)]


class LibraryInfo(BaseModel):
    """A nuclear-data library the backend can use."""

    id: str
    name: str
    path: str
    """Location of `cross_sections.xml` inside the container."""
    builtin: bool
    neutron: int = 0
    photon: int = 0
    thermal: int = 0
    wmp: int = 0


class HealthInfo(BaseModel):
    """Answer of `GET /health`; the client checks `protocol_version` before anything else."""

    status: str = "ok"
    protocol_version: int
    backend_version: str
    openmc_version: str | None
    python_version: str
    cpu_count: int
    capabilities: list[str] = Field(default_factory=list)
    libraries: list[LibraryInfo] = Field(default_factory=list)


class SelfTestStep(BaseModel):
    name: str
    ok: bool
    seconds: float
    detail: str = ""


class SelfTestResult(BaseModel):
    """Answer of `POST /selftest`: build, render and run a tiny pin-cell model."""

    ok: bool
    steps: list[SelfTestStep]
    k_eff: float | None = None
    k_eff_std: float | None = None


class ErrorInfo(BaseModel):
    error: str
    detail: str = ""


# -- models --------------------------------------------------------------------

ModelKind = Literal["xml", "python", "notebook"]
FillType = Literal["material", "universe", "lattice", "void", "distributed"]


class OpenModelRequest(BaseModel):
    """Body of `POST /models/open`."""

    path: str
    """Workspace-relative path with forward slashes: a `.py` builder, `.ipynb` or `.xml`."""
    library: str | None = None
    """Library id from `/health`; None means the built-in library."""
    params: dict[str, float] = Field(default_factory=dict)
    """Builder parameters; the sidecar's defaults fill in the missing ones."""


class BoundingBox(BaseModel):
    lower_left: tuple[float, float, float]
    upper_right: tuple[float, float, float]
    clipped: tuple[bool, bool, bool] = (False, False, False)
    """Axes on which the geometry is unbounded and the box was limited to ±100 cm."""


class NuclideFraction(BaseModel):
    name: str
    fraction: float
    kind: Literal["ao", "wo"] = "ao"


class MaterialInfo(BaseModel):
    id: int
    name: str = ""
    density: float | None = None
    density_units: str = ""
    mass_density: float | None = None
    """[g/cm³], computed from the composition when the density is given otherwise."""
    atom_density: float | None = None
    """Total atom density [atom/b-cm]."""
    temperature: float | None = None
    nuclides: list[NuclideFraction] = Field(default_factory=list)
    sab: list[str] = Field(default_factory=list)
    depletable: bool = False


class CellInfo(BaseModel):
    id: int
    name: str = ""
    universe: int
    fill_type: FillType
    fill: int | None = None
    """Material, universe or lattice id."""
    fills: list[int | None] = Field(default_factory=list)
    """Materials of a distributed-material cell (None = void)."""
    density: float | None = None
    """`Cell.density` override [g/cm³] (the first one for distributed cells)."""
    temperature: float | None = None
    region: str = ""
    translation: tuple[float, float, float] | None = None
    rotation: list[float] | None = None


class UniverseInfo(BaseModel):
    id: int
    name: str = ""
    cells: list[int] = Field(default_factory=list)
    parent_cells: list[int] = Field(default_factory=list)
    parent_lattices: list[int] = Field(default_factory=list)


class LatticeInfo(BaseModel):
    id: int
    name: str = ""
    kind: Literal["rect", "hex"] = "rect"
    pitch: list[float] = Field(default_factory=list)
    lower_left: list[float] | None = None
    center: list[float] | None = None
    shape: list[int] = Field(default_factory=list)
    outer: int | None = None
    universes: list[int] = Field(default_factory=list)
    """Distinct universes in the lattice, in order of first appearance."""
    parent_cells: list[int] = Field(default_factory=list)


class SurfaceInfo(BaseModel):
    id: int
    type: str
    """OpenMC surface type, e.g. "z-cylinder"."""
    coefficients: dict[str, float] = Field(default_factory=dict)
    boundary: str = "transmission"
    name: str = ""


class SourceInfo(BaseModel):
    kind: str
    space: str = ""
    energy: str = ""
    angle: str = ""
    particle: str = "neutron"
    strength: float = 1.0


class SettingsInfo(BaseModel):
    run_mode: str = ""
    particles: int | None = None
    batches: int | None = None
    inactive: int | None = None
    generations_per_batch: int | None = None
    photon_transport: bool = False
    seed: int | None = None
    weight_windows_on: bool = False
    sources: list[SourceInfo] = Field(default_factory=list)


class FilterInfo(BaseModel):
    kind: str
    bins: int = 0
    summary: str = ""


class TallyInfo(BaseModel):
    id: int
    name: str = ""
    scores: list[str] = Field(default_factory=list)
    filters: list[FilterInfo] = Field(default_factory=list)
    nuclides: list[str] = Field(default_factory=list)
    estimator: str | None = None


class WeightWindowInfo(BaseModel):
    id: int
    mesh_id: int
    mesh_kind: str
    dimension: list[int] = Field(default_factory=list)
    particle: str = "neutron"
    energy_bounds: list[float] = Field(default_factory=list)
    lower_min: float | None = None
    lower_max: float | None = None


class ModelInfo(BaseModel):
    """Everything the client shows about an opened model (`POST /models/open`).

    Universe levels count the coordinate levels of OpenMC: 0 holds the root
    universe's cells, and every fill (a universe or a lattice element) adds one.
    """

    model_id: str
    source: str
    kind: ModelKind
    library: str | None = None
    params: dict[str, float] = Field(default_factory=dict)
    sidecar: Sidecar | None = None
    warnings: list[str] = Field(default_factory=list)
    builder_output: str = ""
    build_seconds: float = 0.0
    bounding_box: BoundingBox
    root_universe: int
    max_level: int = 0
    """Deepest universe level that holds cells."""
    materials: list[MaterialInfo] = Field(default_factory=list)
    cells: list[CellInfo] = Field(default_factory=list)
    universes: list[UniverseInfo] = Field(default_factory=list)
    lattices: list[LatticeInfo] = Field(default_factory=list)
    surfaces: list[SurfaceInfo] = Field(default_factory=list)
    settings: SettingsInfo = Field(default_factory=SettingsInfo)
    tallies: list[TallyInfo] = Field(default_factory=list)
    weight_windows: list[WeightWindowInfo] = Field(default_factory=list)


class WeightWindowLayer(BaseModel):
    """Which weight windows the importance layer of a slice samples."""

    particle: str = "neutron"
    energy: float | None = None
    """Energy [eV] that picks each window set's group; None = the first group."""


class SliceRequest(BaseModel):
    """Body of `POST /models/{id}/slice`; the answer is a slice frame (see `slices`).

    The cell, instance and material maps always hold the deepest cells; the
    optional layers add maps next to them.
    """

    basis: Basis = "xy"
    origin: tuple[float, float, float]
    """Centre of the slice [cm]."""
    width: tuple[Extent, Extent]
    """Extent along the horizontal and vertical axis [cm]."""
    pixels: tuple[PixelCount, PixelCount]
    """Number of pixels along the horizontal and vertical axis."""
    show_overlaps: bool = True
    level: Annotated[int, Field(ge=0)] | None = None
    """Also return the cells and instances at this universe level (`level_cells`,
    `level_instances`; -2 where the geometry is not nested that deep)."""
    properties: bool = False
    """Also return the temperature per pixel (plot mode has no usable densities)."""
    weight_windows: WeightWindowLayer | None = None
    """Also return the weight-window lower bound per pixel (`ww_lower`, NaN without one)."""


class PathStep(BaseModel):
    """One level of the nesting path at a point: the cell found in a universe."""

    level: int
    universe: int
    cell: int
    cell_name: str = ""
    local: tuple[float, float, float]
    """The point in this universe's coordinates [cm]."""
    lattice: int | None = None
    """Lattice filling the cell, and the element the point lies in."""
    lattice_index: list[int] | None = None
    overlapping: list[int] = Field(default_factory=list)
    """Other cells of the same universe that contain the point too."""


class InspectResult(BaseModel):
    """Answer of `GET /models/{id}/inspect`: what lies at a point, from the root down."""

    point: tuple[float, float, float]
    found: bool
    path: list[PathStep] = Field(default_factory=list)
    instance: int | None = None
    """Instance of the deepest cell (OpenMC's distribcell index)."""
    material: int | None = None
    """None for a void cell or outside the geometry."""
    material_name: str = ""
    density: float | None = None
    """[g/cm³]: the cell's `Cell.density`, else its material's."""
    temperature: float | None = None
    """[K] when the model sets one for the cell or material."""
