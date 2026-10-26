from pathlib import Path

import numpy as np

from pyvised_backend import render
from pyvised_protocol.schemas import SliceRequest, WeightWindowLayer
from pyvised_protocol.slices import decode_slice

MODEL = """<model><settings>
  <weight_windows id="1"><mesh>1</mesh><particle_type>neutron</particle_type>
    <energy_bounds>0.0 20000000.0</energy_bounds><lower_ww_bounds>0.5 0.25</lower_ww_bounds>
  </weight_windows>
  <mesh id="1"><dimension>2 1 1</dimension>
    <lower_left>-1 -1 -1</lower_left><upper_right>1 1 1</upper_right></mesh>
</settings></model>"""


class FakeLib:
    """Stands in for openmc.lib: records the calls, answers with constant maps."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def slice_data(self, **kwargs):
        self.calls.append(kwargs)
        cols, rows = kwargs["pixels"]
        ids = np.full((rows, cols, 3), 5 if kwargs.get("level") is None else 9, np.int32)
        props = np.full((rows, cols, 2), 293.6) if kwargs["include_properties"] else None
        return ids, props


def test_cut_plane_is_nudged_along_the_normal():
    request = SliceRequest(basis="xz", origin=(1.0, 2.0, 3.0), width=(4.0, 5.0), pixels=(10, 20))
    kwargs = render._slice_kwargs(request.model_dump())
    assert kwargs["origin"] == [1.0, 2.0 + render.NUDGE, 3.0]
    assert kwargs["basis"] == "xz" and kwargs["pixels"] == (10, 20)


def test_slice_layers(tmp_path: Path):
    (tmp_path / "model.xml").write_text(MODEL, encoding="utf-8")
    lib, loaded = FakeLib(), render._Loaded(str(tmp_path))
    request = SliceRequest(
        basis="xy",
        origin=(0.0, 0.0, 0.0),
        width=(4.0, 2.0),
        pixels=(4, 2),
        level=1,
        properties=True,
        weight_windows=WeightWindowLayer(),
    )
    data = decode_slice(render.render_slice(lib, loaded, request.model_dump(), {"k": 1}))
    assert [c.get("level") for c in lib.calls] == [None, 1]
    assert lib.calls[0]["include_properties"] and not lib.calls[1]["include_properties"]
    assert int(data.cells[0, 0]) == 5 and data.level_cells is not None
    assert int(data.level_cells[0, 0]) == 9
    assert data.temperature is not None and data.temperature[0, 0] == np.float32(293.6)
    assert data.ww_lower is not None
    # pixel centres x = -1.5, -0.5, 0.5, 1.5: outside, bin 0, bin 1, outside
    assert np.isnan(data.ww_lower[0, 0]) and np.isnan(data.ww_lower[1, 3])
    assert data.ww_lower[0, 1] == np.float32(0.5) and data.ww_lower[1, 2] == np.float32(0.25)
    assert data.meta["k"] == 1 and data.meta["render_seconds"] >= 0
    plain = SliceRequest(basis="xy", origin=(0.0, 0.0, 0.0), width=(4.0, 2.0), pixels=(4, 2))
    data = decode_slice(render.render_slice(FakeLib(), loaded, plain.model_dump(), {}))
    assert data.level_cells is None and data.ww_lower is None and data.temperature is None


def test_openmc_error_prefers_error_lines(tmp_path: Path):
    log = tmp_path / "render.log"
    log.write_text(
        "Reading model XML file...\n ERROR: Could not find material 9 specified on cell 1\n",
        encoding="utf-8",
    )
    assert render.openmc_error(log).startswith("ERROR: Could not find material 9")
    log.write_text("\n".join(f"line {i}" for i in range(40)), encoding="utf-8")
    assert render.openmc_error(log).splitlines()[-1] == "line 39"
    assert render.openmc_error(tmp_path / "missing.log") == ""
