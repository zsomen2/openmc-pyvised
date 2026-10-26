"""Stands in for the openmc executable in run-manager tests: ``python fake_openmc.py -s N``.

Reads particles, batches and inactive batches from ./model.xml and prints
OpenMC-style output, creating an empty file for every statepoint it
announces. FAKE_OPENMC_DELAY sets the seconds per batch; FAKE_OPENMC_FAIL=1
prints an ERROR block after the first batch and exits with code 1.
"""

import os
import re
import sys
import time
from pathlib import Path


def number(text: str, tag: str, default: int = 0) -> int:
    match = re.search(rf"<{tag}>\s*(\d+)\s*</{tag}>", text)
    return int(match.group(1)) if match else default


def main() -> int:
    model = Path("model.xml").read_text(encoding="utf-8")
    batches = number(model, "batches")
    inactive = number(model, "inactive")
    match = re.search(r"<state_point>\s*<batches>([\d\s]+)</batches>", model)
    statepoints = {int(b) for b in match.group(1).split()} if match else {batches}
    delay = float(os.environ.get("FAKE_OPENMC_DELAY", "0.01"))
    width = len(str(batches))
    print(" Reading model XML file 'model.xml' ...", flush=True)
    print(f" OpenMP Threads | {sys.argv[sys.argv.index('-s') + 1]}", flush=True)
    print("\n ====================>     K EIGENVALUE SIMULATION     <====================\n")
    print("  Bat./Gen.      k       Entropy         Average k ")
    print("  =========   ========   ========   ====================", flush=True)
    for batch in range(1, batches + 1):
        time.sleep(delay)
        average = f"    1.00000 +/- 0.00{batch:03d}" if batch > inactive + 1 else ""
        print(f"{batch:>11}/1    1.00{batch:03d}    3.00000{average}", flush=True)
        if os.environ.get("FAKE_OPENMC_FAIL") == "1":
            print(" ERROR: Something went wrong in the fake", flush=True)
            print("        transport code.", flush=True)
            return 1
        if batch in statepoints:
            name = f"statepoint.{batch:0{width}d}.h5"
            print(f" Creating state point {name}...", flush=True)
            Path(name).write_bytes(b"fake")
    print("\n ============================>     RESULTS     <============================\n")
    print(" Combined k-effective        = 1.00000 +/- 0.00100", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
