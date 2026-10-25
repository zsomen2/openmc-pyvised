"""Discovery of the nuclear-data libraries available in the container."""

import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from pyvised_protocol.schemas import LibraryInfo

# display names of known library folders
_KNOWN_NAMES = {
    "endfb-vii.1": "ENDF/B-VII.1 (NNDC)",
    "nndc_hdf5": "ENDF/B-VII.1 (NNDC)",
}


def describe(cross_sections: Path, *, library_id: str, builtin: bool) -> LibraryInfo:
    """Summarise one `cross_sections.xml` (counts of neutron/photon/thermal/wmp entries)."""
    counts: Counter[str] = Counter()
    try:
        root = ET.parse(cross_sections).getroot()
        counts.update(lib.get("type", "") for lib in root.iter("library"))
    except (OSError, ET.ParseError):
        pass  # listed with zero counts; loading a model with it will report the real error
    folder = cross_sections.parent.name
    return LibraryInfo(
        id=library_id,
        name=_KNOWN_NAMES.get(folder, folder),
        path=str(cross_sections),
        builtin=builtin,
        neutron=counts["neutron"],
        photon=counts["photon"],
        thermal=counts["thermal"],
        wmp=counts["wmp"],
    )


def discover(builtin: Path | None, custom_root: Path) -> list[LibraryInfo]:
    """The built-in library first, then every `<custom_root>/<name>/cross_sections.xml`."""
    found = []
    if builtin is not None and builtin.is_file():
        found.append(describe(builtin, library_id="builtin", builtin=True))
    if custom_root.is_dir():
        for xs in sorted(custom_root.glob("*/cross_sections.xml")):
            found.append(describe(xs, library_id=f"custom:{xs.parent.name}", builtin=False))
    return found
