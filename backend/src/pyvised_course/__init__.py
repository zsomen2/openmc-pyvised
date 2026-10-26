"""Helpers for OpenMC course models; part of the PyVised backend image.

`pyvised_course.mcnp` holds the building blocks of models that mirror an MCNP
deck (same numbers, same order, the cards as comments). The package is
optional: models written with plain OpenMC work just as well.
"""

from .mcnp import (
    Cells,
    Transform,
    Universes,
    density,
    lat1,
    parameter,
    strict_ids,
    trcl,
    weight_windows,
)

__all__ = [
    "Cells",
    "Transform",
    "Universes",
    "density",
    "lat1",
    "parameter",
    "strict_ids",
    "trcl",
    "weight_windows",
]
