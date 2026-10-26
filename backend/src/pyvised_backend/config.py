"""Runtime configuration, read from environment variables set by the desktop app."""

import logging
import math
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from pyvised_protocol import TOKEN_ENV

log = logging.getLogger(__name__)

CGROUP_CPU_MAX = Path("/sys/fs/cgroup/cpu.max")


def available_cpus() -> int:
    """CPUs the container may use: its affinity, limited by a cgroup quota (``--cpus``)."""
    try:
        count = len(os.sched_getaffinity(0))  # pyright: ignore[reportAttributeAccessIssue]
    except (AttributeError, OSError):
        count = os.cpu_count() or 1
    try:
        quota, period = CGROUP_CPU_MAX.read_text().split()[:2]
        if quota != "max":
            count = min(count, max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return max(1, count)


@dataclass(frozen=True)
class Config:
    token: str
    workspace: Path
    cross_sections: Path | None
    """`cross_sections.xml` of the built-in library (`OPENMC_CROSS_SECTIONS`)."""
    custom_data: Path
    """Folder whose sub-folders are the libraries the user added (mounted read-only)."""
    cache_dir: Path = Path("/tmp/pyvised")
    """Container-local folder for exported models and worker logs."""
    render_workers: int = 2
    build_timeout: float = 180.0
    """Seconds a builder script (.py, .ipynb) may run."""
    cpus: int = field(default_factory=available_cpus)
    """Threads a run gets unless it asks for fewer."""

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get(TOKEN_ENV, "")
        if not token:
            token = secrets.token_urlsafe(24)
            log.warning("%s is not set; generated token for this session: %s", TOKEN_ENV, token)
        xs = os.environ.get("OPENMC_CROSS_SECTIONS")
        return cls(
            token=token,
            workspace=Path(os.environ.get("PYVISED_WORKSPACE", "/workspace")),
            cross_sections=Path(xs) if xs else None,
            custom_data=Path(os.environ.get("PYVISED_CUSTOM_DATA", "/data/custom")),
            cache_dir=Path(os.environ.get("PYVISED_CACHE", "/tmp/pyvised")),
            render_workers=max(1, int(os.environ.get("PYVISED_RENDER_WORKERS", "2"))),
            build_timeout=float(os.environ.get("PYVISED_BUILD_TIMEOUT", "180")),
        )
