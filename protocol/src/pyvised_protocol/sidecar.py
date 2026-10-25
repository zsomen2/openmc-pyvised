"""The optional sidecar `pyvised.json` next to a model.

It carries what the OpenMC model itself cannot: titles and descriptions in both
languages, colours and saved views, tally normalisation (MCNP's ``fm``),
reference results and builder parameters. Unknown keys are kept, so an older
app rewriting the file does not drop newer fields.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SIDECAR_NAME = "pyvised.json"
SIDECAR_VERSION = 1

Basis = Literal["xy", "xz", "yz"]


class LocalizedText(BaseModel):
    en: str = ""
    hu: str = ""

    def get(self, language: str) -> str:
        """Text in `language`, falling back to English."""
        return getattr(self, language, "") or self.en


class ParameterSpec(BaseModel):
    """A builder parameter; the builder reads it from ``PYVISED_PARAMS``."""

    name: str
    label: LocalizedText = Field(default_factory=LocalizedText)
    unit: str = ""
    default: float
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None


class TallyNormalisation(BaseModel):
    """result = mean × source_strength × multiplier / divisor (when dividing)."""

    source_strength: float = 1.0
    divide_by: Literal["none", "volume", "area"] = "none"
    divisor: float | None = None
    """Volume [cm³] or area [cm²]; OpenMC divides neither volume nor surface tallies."""
    multiplier: float = 1.0
    unit: str = ""
    note: str = ""


class SavedView(BaseModel):
    """Initial state of one geometry view."""

    basis: Basis
    cut: float
    """Position of the cut plane along the view's normal [cm]."""
    center: tuple[float, float] | None = None
    width: float | None = None
    """Visible extent along the horizontal axis [cm]; None shows the whole model."""


class ReferenceResult(BaseModel):
    name: str
    value: float
    std: float | None = None
    rel_err: float | None = None
    unit: str = ""
    tally: int | None = None
    source: str = ""


class Sidecar(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = SIDECAR_VERSION
    title: LocalizedText = Field(default_factory=LocalizedText)
    description: LocalizedText = Field(default_factory=LocalizedText)
    material_colors: dict[int, str] = Field(default_factory=dict)
    cell_colors: dict[int, str] = Field(default_factory=dict)
    views: list[SavedView] = Field(default_factory=list)
    tallies: dict[int, TallyNormalisation] = Field(default_factory=dict)
    references: list[ReferenceResult] = Field(default_factory=list)
    beta_eff: float | None = None
    parameters: list[ParameterSpec] = Field(default_factory=list)

    def parameter_defaults(self) -> dict[str, float]:
        return {p.name: p.default for p in self.parameters}
