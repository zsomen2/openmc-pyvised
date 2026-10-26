"""Runs real OpenMC: only inside the backend container (`just backend-test`)."""

import pytest

pytest.importorskip("openmc")

from pyvised_backend import server
from pyvised_backend.selftest import run_selftest

pytestmark = pytest.mark.openmc


def test_selftest_builds_renders_and_runs():
    result = run_selftest()
    assert result.ok, result.steps
    assert [s.name for s in result.steps] == ["build", "render", "transport"]
    # infinite lattice of 10 % enriched UO2 rods in water: k-inf ≈ 1.64
    assert result.k_eff is not None and 1.4 < result.k_eff < 1.9


def test_selftest_subprocess_reports_the_same_result():
    result = server.run_selftest_subprocess()
    assert result.ok, result.steps
