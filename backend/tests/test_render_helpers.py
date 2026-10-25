from pathlib import Path

from pyvised_backend import render


def test_cut_plane_is_nudged_along_the_normal():
    request = {
        "basis": "xz",
        "origin": (1.0, 2.0, 3.0),
        "width": (4.0, 5.0),
        "pixels": (10, 20),
        "show_overlaps": True,
        "level": None,
        "properties": False,
    }
    kwargs = render._slice_kwargs(request)
    assert kwargs["origin"] == [1.0, 2.0 + render.NUDGE, 3.0]
    assert kwargs["basis"] == "xz" and kwargs["pixels"] == (10, 20)
    assert kwargs["include_properties"] is False


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
