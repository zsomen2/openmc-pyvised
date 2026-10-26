from pathlib import Path

from pyvised_backend import libraries

XML = """<?xml version='1.0' encoding='utf-8'?>
<cross_sections>
  <library materials="H1" path="H1.h5" type="neutron" />
  <library materials="U235" path="U235.h5" type="neutron" />
  <library materials="H" path="photon/H.h5" type="photon" />
  <library materials="c_H_in_H2O" path="c_H_in_H2O.h5" type="thermal" />
</cross_sections>
"""


def _library(folder: Path) -> Path:
    folder.mkdir(parents=True)
    xs = folder / "cross_sections.xml"
    xs.write_text(XML, encoding="utf-8")
    return xs


def test_builtin_and_custom_libraries(tmp_path: Path):
    builtin = _library(tmp_path / "endfb-vii.1")
    _library(tmp_path / "custom" / "my-lib")

    found = libraries.discover(builtin, tmp_path / "custom")

    assert [lib.id for lib in found] == ["builtin", "custom:my-lib"]
    assert found[0].name == "ENDF/B-VII.1 (NNDC)"
    assert (found[0].neutron, found[0].photon, found[0].thermal) == (2, 1, 1)
    assert not found[1].builtin


def test_missing_paths_give_no_libraries(tmp_path: Path):
    assert libraries.discover(None, tmp_path / "nothing") == []


def test_unreadable_xml_is_listed_with_zero_counts(tmp_path: Path):
    xs = tmp_path / "broken" / "cross_sections.xml"
    xs.parent.mkdir()
    xs.write_text("<not xml", encoding="utf-8")
    info = libraries.describe(xs, library_id="x", builtin=False)
    assert info.neutron == 0
