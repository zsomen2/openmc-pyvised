"""The optional sidecar `pyvised.json` next to a model.

It carries what the OpenMC model itself cannot: titles and descriptions in both
languages, colours and saved views, tally normalisation (MCNP's ``fm``),
reference results, builder parameters and how weight windows map to
importances. Unknown keys are kept, so an older app rewriting the file does not
drop newer fields; the app edits only the keys it changes (`update_sidecar`).
"""

import json
from pathlib import Path
from typing import Any, Literal

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
    """A result to compare runs with: k-eff (no `tally`) or a normalised tally total."""

    name: str
    value: float
    std: float | None = None
    rel_err: float | None = None
    unit: str = ""
    tally: int | None = None
    source: str = ""
    params: dict[str, float] = Field(default_factory=dict)
    """Builder parameters the reference belongs to; a run matches when it has these values
    (parameters missing here may have any value)."""
    tolerance: float | None = Field(default=None, gt=0)
    """Agreed largest difference (k-eff: absolute, tallies: relative); the app judges by it
    instead of two standard deviations."""

    def matches(self, params: dict[str, float]) -> bool:
        return all(
            name in params and abs(params[name] - value) <= 1e-9 * max(1.0, abs(value))
            for name, value in self.params.items()
        )


class RunPreset(BaseModel):
    """Run settings a model suggests in the Simulation tab (None keeps the model's value)."""

    name: str
    label: LocalizedText = Field(default_factory=LocalizedText)
    particles: int | None = Field(default=None, gt=0)
    batches: int | None = Field(default=None, gt=0)
    inactive: int | None = Field(default=None, ge=0)
    time_limit: float | None = Field(default=None, gt=0)
    """[s] of wall time."""
    keff_std: float | None = Field(default=None, gt=0)
    """Stop once σ(k-eff) is this small."""
    params: dict[str, float] = Field(default_factory=dict)
    """Builder parameters the preset sets as well."""
    note: LocalizedText = Field(default_factory=LocalizedText)


class Sidecar(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = SIDECAR_VERSION
    title: LocalizedText = Field(default_factory=LocalizedText)
    description: LocalizedText = Field(default_factory=LocalizedText)
    material_colors: dict[int, str] = Field(default_factory=dict)
    """Colours ("#rrggbb") chosen in the app; the keys are ids."""
    cell_colors: dict[int, str] = Field(default_factory=dict)
    universe_colors: dict[int, str] = Field(default_factory=dict)
    views: list[SavedView] = Field(default_factory=list)
    tallies: dict[int, TallyNormalisation] = Field(default_factory=dict)
    references: list[ReferenceResult] = Field(default_factory=list)
    beta_eff: float | None = None
    importance_lower_bound: float = Field(default=0.5, gt=0)
    """Weight-window lower bound that stands for importance 1: the importance colour mode
    shows this / lower bound (MCNP-style rebuilds use lower bound 0.5/imp)."""
    parameters: list[ParameterSpec] = Field(default_factory=list)
    run_presets: list[RunPreset] = Field(default_factory=list)

    def parameter_defaults(self) -> dict[str, float]:
        return {p.name: p.default for p in self.parameters}


def update_sidecar(folder: Path, changes: dict[str, Any]) -> Sidecar:
    """Set top-level keys of the sidecar in `folder` (created if missing) and keep the rest.

    A value of None removes its key. The file is checked against `Sidecar` before it is
    written, so a bad change raises instead of producing a file the backend rejects.
    """
    path = folder / SIDECAR_NAME
    data: dict[str, Any] = {"schema_version": SIDECAR_VERSION}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    sidecar = Sidecar.model_validate(data)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return sidecar
