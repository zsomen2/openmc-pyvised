"""OpenMC log parsing on output recorded from OpenMC 0.16.0 (no OpenMC needed)."""

import pytest

from pyvised_backend.runlog import LogParser

EIGENVALUE = """\
 Reading model XML file 'model.xml' ...
 Reading cross sections XML file...
 Reading U235 from /opt/nuclear-data/endfb-vii.1/U235.h5
 Minimum neutron data temperature: 294 K
 Preparing distributed cell instances...
 Writing summary.h5 file...
 Initializing source particles...

 ====================>     K EIGENVALUE SIMULATION     <====================

  Bat./Gen.      k       Entropy         Average k
  =========   ========   ========   ====================
        1/1    1.66236    3.99095
        2/1    1.58913    3.99240
        3/1    1.59112    3.99450
        4/1    1.62688    3.99502
        5/1    1.69419    3.99480
        6/1    1.65248    3.99521
        7/1    1.57944    3.99224    1.61596 +/- 0.03652
        8/1    1.63044    3.99407    1.62079 +/- 0.02163
        9/1    1.64293    3.99231    1.62632 +/- 0.01627
       10/1    1.57862    3.99098    1.61678 +/- 0.01580
 Creating state point statepoint.10.h5...
       11/1    1.66807    3.99426    1.62533 +/- 0.01548
       12/1    1.74233    3.99377    1.65127 +/- 0.01748
 Triggers unsatisfied, max unc./thresh. is 2.604915815512985 for eigenvalue
 WARNING: Particle 1234 was lost: no cell found at
          (1.0, 2.0, 3.0)
       13/1    1.71853    3.99085    1.65874 +/- 0.01713
 Triggers satisfied for batch 13
 Creating state point statepoint.13.h5...

 =======================>     TIMING STATISTICS     <=======================

 Total time for initialization     = 9.7575e-02 seconds
 Total time elapsed                = 1.0398e+00 seconds
 Calculation Rate (inactive)       = 135900 particles/second
 Calculation Rate (active)         = 126917 particles/second

 ============================>     RESULTS     <============================

 k-effective (Collision)     = 1.64793 +/- 0.00370
 k-effective (Track-length)  = 1.64948 +/- 0.00533
 k-effective (Absorption)    = 1.64572 +/- 0.00225
 Combined k-effective        = 1.64630 +/- 0.00213
 Leakage Fraction            = 0.00000 +/- 0.00000
"""

NO_ENTROPY = """\
  Bat./Gen.      k            Average k
  =========   ========   ====================
        1/1    0.11018
        6/1    0.08824
        7/1    0.11628    0.10226 +/- 0.01402
"""

FIXED = """\
 ===============>     FIXED SOURCE TRANSPORT SIMULATION     <===============

 Simulating batch 1
 Simulating batch 2
 Simulating batch 3
 Creating state point statepoint.03.h5...
 Leakage Fraction            = 1.03750 +/- 0.00178
"""

ERROR = """\
 Simulating batch 1
 ERROR: The secondary particle bank appears to be growing without bound. You are
        likely running a subcritical multiplication problem with k-effective
        close to or greater than one.
"""


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def feed(parser: LogParser, text: str, clock: Clock | None = None, step: float = 0.0) -> None:
    for line in text.splitlines():
        if clock is not None:
            clock.now += step
        parser.feed(line)
    parser.finish()


def test_eigenvalue_generations_entropy_and_average():
    parser = LogParser(particles=2000, batches=30, inactive=6)
    feed(parser, EIGENVALUE)
    p = parser.progress
    assert len(p.k) == 13 and p.k[0] == pytest.approx(1.66236)
    assert len(p.entropy) == 13 and p.entropy[-1] == pytest.approx(3.99085)
    assert p.mean[:6] == [None] * 6
    assert p.mean[6] == pytest.approx(1.61596) and p.std[6] == pytest.approx(0.03652)
    assert p.batch == 13
    assert p.statepoint == 13
    assert parser.statepoints == ["statepoint.10.h5", "statepoint.13.h5"]
    assert parser.triggers_satisfied and p.trigger == "Triggers satisfied for batch 13"


def test_results_timing_and_warnings():
    parser = LogParser(particles=2000, batches=30, inactive=6)
    feed(parser, EIGENVALUE)
    assert parser.results["combined"] == pytest.approx((1.64630, 0.00213))
    assert parser.results["collision"] == pytest.approx((1.64793, 0.00370))
    assert parser.results["leakage"] == (0.0, 0.0)
    assert parser.rates == {"inactive": 135900.0, "active": 126917.0}
    assert parser.elapsed == pytest.approx(1.0398)
    assert parser.warnings == ["WARNING: Particle 1234 was lost: no cell found at (1.0, 2.0, 3.0)"]
    assert parser.error == ""


def test_batch_lines_are_not_warning_continuations():
    parser = LogParser(particles=10, batches=20)
    feed(parser, " WARNING: something\n        13/1    1.71853    3.99085\n")
    assert parser.progress.k == [1.71853]
    assert parser.warnings == ["WARNING: something"]


def test_no_entropy_column():
    parser = LogParser(particles=10, batches=7, inactive=6)
    feed(parser, NO_ENTROPY)
    p = parser.progress
    assert p.k == pytest.approx([0.11018, 0.08824, 0.11628])
    assert p.entropy == []
    assert p.mean == [None, None, pytest.approx(0.10226)]


def test_phases():
    parser = LogParser(particles=10, batches=30, inactive=6)
    lines = EIGENVALUE.splitlines()
    parser.feed(lines[2])
    assert parser.progress.phase == "Reading U235"
    parser.feed(lines[5])
    assert parser.progress.phase == "Writing summary.h5 file"
    for line in lines[6:14]:
        parser.feed(line)
    assert parser.progress.phase == "inactive batches"
    for line in lines[14:22]:
        parser.feed(line)
    assert parser.progress.phase == "active batches"


def test_fixed_source_batches():
    parser = LogParser(particles=10, batches=3)
    feed(parser, FIXED)
    assert parser.progress.batch == 2  # "Simulating batch 3" starts the third
    assert parser.progress.statepoint == 3  # the zero-padded name
    assert parser.results["leakage"] == pytest.approx((1.0375, 0.00178))
    assert parser.progress.k == []


def test_the_last_fixed_source_batch_counts_at_the_end():
    parser = LogParser(particles=10, batches=3)
    feed(parser, FIXED + "\n =======================>     TIMING STATISTICS     <==\n")
    assert parser.progress.batch == 3


def test_error_block():
    parser = LogParser(particles=10, batches=3)
    feed(parser, ERROR)
    assert parser.error.startswith("ERROR: The secondary particle bank appears")
    assert parser.error.endswith("close to or greater than one.")
    assert "\n" not in parser.error


def test_rate_and_eta_from_batch_times():
    clock = Clock()
    parser = LogParser(particles=1000, batches=20, inactive=0, clock=clock)
    parser.start()
    for batch in range(1, 6):
        clock.now += 0.5
        parser.feed(f"       {batch}/1    1.00000")
    p = parser.progress
    assert p.rate == pytest.approx(2000.0)
    assert p.eta == pytest.approx(15 * 0.5)
    assert p.elapsed == pytest.approx(2.5)


def test_trigger_runs_beyond_the_plan_extend_the_batches():
    parser = LogParser(particles=10, batches=3)
    feed(parser, "\n".join(f"       {b}/1    1.00000" for b in range(1, 6)))
    assert parser.progress.batch == 5 and parser.progress.batches == 5
