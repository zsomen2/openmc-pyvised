"""Stands in for ``openmc --track`` in track-service tests: ``python fake_openmc_tracks.py …``.

Writes ./tracks.h5 in OpenMC's layout: one history per tracked particle (all
`max_tracks` particles with --track, else those of the <track> list), each a
neutron going from the origin along +x, crossing x = 1 and absorbed at x = 2.
FAKE_TRACKS_ARGS names a file that gets the arguments, FAKE_TRACKS_DELAY the
seconds to sleep first, FAKE_TRACKS_FAIL=1 prints an ERROR block and exits 1.
"""

import os
import re
import sys
import time
from pathlib import Path

import h5py
import numpy as np

VECTOR = np.dtype([("x", "<f8"), ("y", "<f8"), ("z", "<f8")])
STATE = np.dtype(
    [
        ("r", VECTOR),
        ("u", VECTOR),
        ("E", "<f8"),
        ("time", "<f8"),
        ("wgt", "<f8"),
        ("cell_id", "<i4"),
        ("cell_instance", "<i4"),
        ("material_id", "<i4"),
    ]
)


def main() -> int:
    if os.environ.get("FAKE_TRACKS_ARGS"):
        Path(os.environ["FAKE_TRACKS_ARGS"]).write_text(" ".join(sys.argv[1:]), encoding="utf-8")
    time.sleep(float(os.environ.get("FAKE_TRACKS_DELAY", "0")))
    print(" Reading model XML file 'model.xml' ...", flush=True)
    print(" WARNING: a fake warning", flush=True)
    if os.environ.get("FAKE_TRACKS_FAIL") == "1":
        print(" ERROR: No fissionable material", flush=True)
        print("        in the fake model.", flush=True)
        return 1
    model = Path("model.xml").read_text(encoding="utf-8")

    def find(tag: str) -> str:
        match = re.search(rf"<{tag}>([\d\s]+)</{tag}>", model)
        assert match is not None, tag
        return match.group(1)

    if "--track" in sys.argv:
        batch, ids = 1, range(1, int(find("max_tracks")) + 1)
    else:
        numbers = [int(v) for v in find("track").split()]
        batch, ids = numbers[0], numbers[2::3]
    with h5py.File("tracks.h5", "w") as f:
        f.attrs["filetype"] = np.bytes_(b"track")
        for i in ids:
            states = np.zeros(3, dtype=STATE)
            for k, x in enumerate((0.0, 1.0, 2.0)):
                states[k]["r"] = (x, 0.0, 0.0)
                states[k]["u"] = (1.0, 0.0, 0.0)
                states[k]["E"] = 1e6
                states[k]["time"] = k * 1e-9
                states[k]["wgt"] = 0.0 if k == 2 else 1.0
                states[k]["cell_id"] = 1
            dataset = f.create_dataset(f"track_{batch}_1_{i}", data=states)
            dataset.attrs["n_particles"] = 1
            dataset.attrs["offsets"] = np.array([0, 3])
            dataset.attrs["particles"] = np.array([2112])
    return 0


if __name__ == "__main__":
    sys.exit(main())
