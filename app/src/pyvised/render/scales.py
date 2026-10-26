"""Value ranges of the continuous colour modes: linear or logarithmic, with tick values."""

import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

LOG_RATIO = 50.0
"""A range wider than this factor is shown on a log scale."""


@dataclass(frozen=True)
class Scale:
    low: float
    high: float
    log: bool = False

    @classmethod
    def fit(cls, values: Iterable[float], *, log: bool | None = None) -> "Scale | None":
        """The range of the finite `values` (positive ones for a log scale); None if empty."""
        finite = [float(v) for v in values if v is not None and math.isfinite(v)]
        if log is not False:
            positive = [v for v in finite if v > 0]
            if log is None:
                log = bool(positive) and max(positive) / min(positive) > LOG_RATIO
            if log:
                finite = positive
        if not finite:
            return None
        low, high = min(finite), max(finite)
        if high <= low:  # one value: a small range around it
            pad = abs(low) * 0.05 or 1.0
            low, high = (low / 1.5, high * 1.5) if log else (low - pad, high + pad)
        return cls(low, high, bool(log))

    def normalise(self, values: np.ndarray) -> np.ndarray:
        """Values -> [0, 1] (clipped); NaN stays NaN."""
        v = np.asarray(values, dtype=np.float64)
        if self.log:
            with np.errstate(divide="ignore", invalid="ignore"):
                v = np.log10(np.where(v > 0, v, np.nan))
            low, high = math.log10(self.low), math.log10(self.high)
        else:
            low, high = self.low, self.high
        return np.clip((v - low) / (high - low), 0.0, 1.0)

    def position(self, value: float) -> float:
        """Where `value` sits on the colour bar, 0 … 1."""
        return float(self.normalise(np.array([value]))[0])

    def ticks(self, count: int = 5) -> list[float]:
        """Round values within the range for the colour bar."""
        if self.log:
            first = math.ceil(math.log10(self.low) - 1e-9)
            last = math.floor(math.log10(self.high) + 1e-9)
            decades = list(range(first, last + 1))
            step = max(1, math.ceil(len(decades) / count))
            ticks = [10.0**d for d in decades[::step]]
            return ticks if len(ticks) >= 2 else [self.low, self.high]
        span = self.high - self.low
        exponent = math.floor(math.log10(span / max(1, count - 1)))

        def fits(step: float) -> int:
            return math.floor(self.high / step + 1e-9) - math.ceil(self.low / step - 1e-9) + 1

        steps = [m * 10.0**e for e in (exponent - 1, exponent) for m in (1, 2, 5)]
        step = min(steps, key=lambda s: (abs(fits(s) - count), -s))  # ~count ticks
        start = math.ceil(self.low / step - 1e-9) * step
        ticks = []
        value = start
        while value <= self.high + 1e-9 * span:
            ticks.append(round(value, 12))
            value += step
        return ticks


def format_value(value: float) -> str:
    """Short text for a tick or a read-out: 3 significant digits."""
    if value == 0:
        return "0"
    if 1e-3 <= abs(value) < 1e5:
        return f"{value:.3g}"
    return f"{value:.2e}"
