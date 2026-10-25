"""Pydantic models exchanged over the HTTP API."""

from pydantic import BaseModel, Field


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
