"""Weight-window sampling from model.xml (numpy only, runs without OpenMC)."""

import math
from pathlib import Path

import numpy as np
import pytest

from pyvised_backend.weightwindows import (
    lower_bounds,
    read_weight_windows,
    slice_points,
)

# a box mesh first (2 x 1 x 1, two energy groups), then a cylindrical layer mesh; the
# lower bounds are written as OpenMC writes them: first mesh index fastest, energy slowest
MODEL = """<?xml version='1.0' encoding='utf-8'?>
<model>
  <settings>
    <weight_windows id="1">
      <mesh>1</mesh>
      <particle_type>neutron</particle_type>
      <energy_bounds>0.0 1.0 20000000.0</energy_bounds>
      <lower_ww_bounds>0.5 -1.0 0.05 0.06</lower_ww_bounds>
      <upper_ww_bounds>2.5 -1.0 0.25 0.3</upper_ww_bounds>
    </weight_windows>
    <mesh id="1">
      <dimension>2 1 1</dimension>
      <lower_left>-2.0 -1.0 -1.0</lower_left>
      <upper_right>2.0 1.0 1.0</upper_right>
    </mesh>
    <weight_windows id="2">
      <mesh>2</mesh>
      <particle_type>neutron</particle_type>
      <energy_bounds>0.0 20000000.0</energy_bounds>
      <lower_ww_bounds>0.25 0.125</lower_ww_bounds>
      <upper_ww_bounds>1.25 0.625</upper_ww_bounds>
    </weight_windows>
    <mesh id="2" type="cylindrical">
      <r_grid>0.0 5.0 10.0</r_grid>
      <phi_grid>0.0 6.283185307179586</phi_grid>
      <z_grid>-10.0 10.0</z_grid>
      <origin>1.0 0.0 0.0</origin>
    </mesh>
    <weight_windows id="3">
      <mesh>3</mesh>
      <particle_type>photon</particle_type>
      <energy_bounds>0.0 20000000.0</energy_bounds>
      <lower_ww_bounds>0.1</lower_ww_bounds>
    </weight_windows>
    <mesh id="3" type="spherical">
      <r_grid>0.0 100.0</r_grid>
      <theta_grid>0.0 3.141592653589793</theta_grid>
      <phi_grid>0.0 6.283185307179586</phi_grid>
    </mesh>
  </settings>
</model>
"""


@pytest.fixture
def sets(tmp_path: Path):
    path = tmp_path / "model.xml"
    path.write_text(MODEL, encoding="utf-8")
    return read_weight_windows(path)


def test_reads_every_set_in_model_order(sets):
    assert [(s.id, s.particle, s.mesh.kind, s.mesh.dimension) for s in sets] == [
        (1, "neutron", "regular", (2, 1, 1)),
        (2, "neutron", "cylindrical", (2, 1, 1)),
        (3, "photon", "spherical", (1, 1, 1)),
    ]
    assert sets[0].lower.tolist() == [[0.5, -1.0], [0.05, 0.06]]


def test_first_valid_window_wins(sets):
    points = np.array(
        [
            [-1.0, 0.0, 0.0],  # box bin 0 (0.5)
            [1.0, 0.0, 0.0],  # box bin 1 has no window (-1): the cylinder, r = 0 -> 0.25
            [4.0, 3.0, 0.0],  # outside the box, r = 4.24 -> 0.25
            [8.0, 0.0, 0.0],  # r = 7 -> 0.125
            [12.0, 0.0, 0.0],  # r = 11: outside every neutron mesh
            [1.0, 0.0, 10.0],  # z on the cylinder's upper edge: outside (half-open bins)
        ]
    )
    lower = lower_bounds(sets, points)
    np.testing.assert_allclose(lower[:4], [0.5, 0.25, 0.25, 0.125])
    assert np.isnan(lower[4]) and np.isnan(lower[5])


def test_energy_groups_and_particles(sets):
    point = np.array([[-1.0, 0.0, 0.0]])
    assert lower_bounds(sets, point, energy=0.5)[0] == pytest.approx(0.5)
    assert lower_bounds(sets, point, energy=2.0e6)[0] == pytest.approx(0.05)
    assert lower_bounds(sets, point, energy=3.0e7)[0] != lower_bounds(sets, point)[0]
    assert lower_bounds(sets, point, particle="photon")[0] == pytest.approx(0.1)
    assert np.isnan(lower_bounds(sets, point, particle="electron")[0])


def test_slice_points_follow_slice_data_orientation():
    points = slice_points("xz", (1.0, 5.0, 0.0), (4.0, 2.0), (4, 2))
    grid = points.reshape(2, 4, 3)
    assert grid[0, 0].tolist() == pytest.approx([-0.5, 5.0, 0.5]), "row 0 is the top (+z)"
    assert grid[1, 3].tolist() == pytest.approx([2.5, 5.0, -0.5])
    side = slice_points("yz", (2.0, 0.0, 0.0), (2.0, 2.0), (2, 2)).reshape(2, 2, 3)
    assert side[0, 1].tolist() == pytest.approx([2.0, 0.5, 0.5])
    assert math.isclose(side[1, 0, 2], -0.5)
