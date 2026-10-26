"""Live progress from OpenMC's standard output.

The parser takes one line at a time and keeps a `RunProgress`: k and the
Shannon entropy per generation, OpenMC's running average of k, statepoints,
trigger messages and what OpenMC is doing. It also collects the final results
block, the timing lines, warnings and the ``ERROR:`` block of a failed run
(continuation lines are indented). Formats checked with OpenMC 0.16.0::

      Bat./Gen.      k       Entropy         Average k
      =========   ========   ========   ====================
            1/1    1.66236    3.99095
            7/1    1.57944    3.99224    1.61596 +/- 0.03652
     Creating state point statepoint.10.h5...
     Triggers unsatisfied, max unc./thresh. is 1.25 for eigenvalue
     Simulating batch 3                                  (fixed source)
     k-effective (Collision)     = 1.04193 +/- 0.00089
     Calculation Rate (active)   = 126917 particles/second
"""

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from pyvised_protocol.runs import RunProgress

_BATCH = re.compile(r"^\s*(\d+)/(\d+)\s+(\S+)(.*)$")
_AVERAGE = re.compile(r"(\S+)\s+\+/-\s+(\S+)\s*$")
_FIXED_BATCH = re.compile(r"^\s*Simulating batch\s+(\d+)")
_STATEPOINT = re.compile(r"Creating state point\s+(\S+?\.h5)")
_STATEPOINT_BATCH = re.compile(r"statepoint\.0*(\d+)\.h5$")
_RESULT = re.compile(r"^\s*(.+?)\s*=\s*(\S+)\s+\+/-\s+(\S+)")
_RATE = re.compile(r"Calculation Rate \((\w+)\)\s*=\s*(\S+)\s+particles/second")
_ELAPSED = re.compile(r"Total time elapsed\s*=\s*(\S+)\s+seconds")
_READING = re.compile(r"^\s*Reading (\S+) from ")
RECENT_BATCHES = 10
"""Batches the rate and the ETA are averaged over."""

RESULT_NAMES = {
    "k-effective (Collision)": "collision",
    "k-effective (Track-length)": "track_length",
    "k-effective (Absorption)": "absorption",
    "Combined k-effective": "combined",
    "Leakage Fraction": "leakage",
}


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


@dataclass
class LogParser:
    """Feed OpenMC's output line by line; read `progress` and the other fields."""

    particles: int = 0
    batches: int = 0
    inactive: int = 0
    generations_per_batch: int = 1
    clock: Callable[[], float] | None = None
    """Time source (tests); `time.monotonic` by default."""
    progress: RunProgress = field(default_factory=RunProgress)
    results: dict[str, tuple[float, float]] = field(default_factory=dict)
    """Final k-eff estimates and leakage: name -> (value, σ)."""
    rates: dict[str, float] = field(default_factory=dict)
    """"active" / "inactive" -> particles per second, from the timing block."""
    elapsed: float | None = None
    statepoints: list[str] = field(default_factory=list)
    """Statepoint files in the order OpenMC announced them."""
    triggers_satisfied: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    _entropy_column: bool = False
    _block: list[str] | None = None
    _block_kind: str = ""
    _started: float | None = None
    _last_fixed: int = 0
    """The fixed-source batch last announced ("Simulating batch n")."""
    _batch_times: deque = field(default_factory=lambda: deque(maxlen=RECENT_BATCHES + 1))

    def __post_init__(self) -> None:
        self.progress.batches = self.batches
        self.progress.inactive = self.inactive
        self.progress.generations_per_batch = self.generations_per_batch

    def _now(self) -> float:
        return (self.clock or time.monotonic)()

    def start(self) -> None:
        """OpenMC was started now (elapsed time and ETA count from here)."""
        self._started = self._now()

    # -- lines -------------------------------------------------------------------------

    def feed(self, line: str) -> None:
        line = line.rstrip("\r\n")
        if self._continue_block(line):
            return
        stripped = line.strip()
        if not stripped:
            return
        if stripped.startswith("ERROR:"):
            self._open_block("error", stripped)
        elif stripped.startswith("WARNING:"):
            self._open_block("warning", stripped)
        elif self._generation(line) or self._fixed_batch(stripped):
            pass
        elif stripped.startswith("Bat./Gen."):
            self._entropy_column = "Entropy" in stripped
            self.progress.phase = "inactive batches" if self.inactive else "active batches"
        elif match := _STATEPOINT.search(stripped):
            name = match.group(1)
            self.statepoints.append(name)
            number = _STATEPOINT_BATCH.search(name)
            if number:
                self.progress.statepoint = int(number.group(1))
        elif stripped.startswith(("Triggers unsatisfied", "Triggers satisfied")):
            self.progress.trigger = stripped
            if stripped.startswith("Triggers satisfied"):
                self.triggers_satisfied = True
        elif stripped.startswith("The estimated number of batches is"):
            self.progress.trigger = f"{self.progress.trigger} · {stripped}".strip(" ·")
        elif match := _RESULT.match(line):
            key = RESULT_NAMES.get(match.group(1).strip())
            value, std = _float(match.group(2)), _float(match.group(3))
            if key and value is not None and std is not None:
                self.results[key] = (value, std)
        elif match := _RATE.search(stripped):
            rate = _float(match.group(2))
            if rate is not None:
                self.rates[match.group(1)] = rate
        elif match := _ELAPSED.search(stripped):
            self.elapsed = _float(match.group(1))
        else:
            self._phase(stripped)

    def _open_block(self, kind: str, first: str) -> None:
        self._close_block()
        self._block, self._block_kind = [first], kind

    def _continue_block(self, line: str) -> bool:
        """Indented lines right after ERROR:/WARNING: belong to its message."""
        if self._block is None:
            return False
        if line.startswith("        ") and line.strip() and not _BATCH.match(line):
            self._block.append(line.strip())
            return True
        self._close_block()
        return False

    def _close_block(self) -> None:
        if self._block is None:
            return
        text = " ".join(self._block)
        if self._block_kind == "error":
            self.error = f"{self.error}\n{text}".strip()
        else:
            self.warnings.append(text)
        self._block = None

    def finish(self) -> None:
        """The output ended: close an open message block."""
        self._close_block()

    def _phase(self, text: str) -> None:
        if match := _READING.match(text):
            self.progress.phase = f"Reading {match.group(1)}"
        elif "EIGENVALUE SIMULATION" in text or "FIXED SOURCE" in text:
            self.progress.phase = "Starting the simulation"
        elif "TIMING STATISTICS" in text:
            self.progress.phase = "Finishing"
            if self._last_fixed:  # the last fixed-source batch is done too
                self._batch_done(self._last_fixed)
        elif text.startswith(
            (
                "Reading",
                "Preparing",
                "Writing summary",
                "Initializing source",
                "Building",
                "Loading",
            )
        ):
            self.progress.phase = text.rstrip(". ")

    # -- batches -------------------------------------------------------------------------

    def _generation(self, line: str) -> bool:
        match = _BATCH.match(line)
        if match is None:
            return False
        k = _float(match.group(3))
        if k is None:
            return False
        batch, generation = int(match.group(1)), int(match.group(2))
        rest = match.group(4).strip()
        entropy = None
        if self._entropy_column and rest:
            head, _, rest = rest.partition(" ")
            entropy = _float(head)
            rest = rest.strip()
        mean = std = None
        if average := _AVERAGE.search(rest):
            mean, std = _float(average.group(1)), _float(average.group(2))
        p = self.progress
        p.k.append(k)
        if entropy is not None:
            p.entropy.append(entropy)
        p.mean.append(mean)
        p.std.append(std)
        if generation >= self.generations_per_batch:
            self._batch_done(batch)
        p.phase = "inactive batches" if batch <= self.inactive else "active batches"
        return True

    def _fixed_batch(self, text: str) -> bool:
        match = _FIXED_BATCH.match(text)
        if match is None:
            return False
        # "Simulating batch n" opens batch n, so n - 1 batches are done
        self._last_fixed = int(match.group(1))
        self._batch_done(self._last_fixed - 1)
        self.progress.phase = "batches"
        return True

    def _batch_done(self, batch: int) -> None:
        p = self.progress
        p.batch = max(p.batch, batch)
        if p.batches and p.batch > p.batches:
            p.batches = p.batch  # a trigger run beyond the planned batches
        now = self._now()
        self._batch_times.append((batch, now))
        if self._started is not None:
            p.elapsed = now - self._started
        if len(self._batch_times) >= 2:
            (b0, t0), (b1, t1) = self._batch_times[0], self._batch_times[-1]
            if b1 > b0 and t1 > t0:
                per_batch = (t1 - t0) / (b1 - b0)
                p.rate = self.particles * self.generations_per_batch / per_batch
                left = max(0, p.batches - p.batch)
                p.eta = left * per_batch
        if p.batches and p.batch >= p.batches:
            p.eta = 0.0

    def tick(self) -> None:
        """Advance the elapsed time between lines."""
        if self._started is not None:
            self.progress.elapsed = self._now() - self._started
