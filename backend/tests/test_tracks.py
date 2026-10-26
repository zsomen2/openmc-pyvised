"""The tracked run's copy of the model, OpenMC's messages and the service with a fake OpenMC."""

import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from pyvised_backend.config import Config
from pyvised_backend.errors import ServiceError
from pyvised_backend.models import OpenedModel
from pyvised_backend.tracks import TrackService, openmc_messages, patch_tracks_xml
from pyvised_protocol.schemas import BoundingBox, LibraryInfo, ModelInfo
from pyvised_protocol.tracks import (
    FISSION_BATCHES,
    FISSION_PARTICLES,
    PointSource,
    TrackEvent,
    TrackRequest,
    decode_tracks,
)

FAKE = Path(__file__).parent / "fixtures" / "fake_openmc_tracks.py"
MODEL = """<?xml version='1.0' encoding='utf-8'?>
<model>
  <materials/>
  <geometry/>
  <settings>
    <run_mode>{mode}</run_mode>
    <particles>2000</particles>
    <batches>220</batches>
    <inactive>20</inactive>
    <generations_per_batch>2</generations_per_batch>
    <photon_transport>false</photon_transport>
    <source type="independent" strength="1.0"><space type="point"><parameters>1 2 3</parameters>
    </space></source>
    <state_point><batches>10 220</batches></state_point>
    <entropy_mesh>3</entropy_mesh>
    <trigger><active>true</active><max_batches>500</max_batches></trigger>
    <keff_trigger><type>std_dev</type><threshold>0.001</threshold></keff_trigger>
    <weight_windows_on>true</weight_windows_on>
    <mesh id="3"><dimension>2 2 2</dimension></mesh>
  </settings>
  <tallies>
    <mesh id="7"><dimension>4 4</dimension></mesh>
    <tally id="1"><scores>flux</scores></tally>
  </tallies>
  <plots><plot id="1"/></plots>
</model>
"""


def patched(tmp_path: Path, request: TrackRequest, mode: str = "eigenvalue", seed: int = 9):
    path = tmp_path / "model.xml"
    path.write_text(MODEL.format(mode=mode), encoding="utf-8")
    plan = patch_tracks_xml(path, request, seed)
    root = ET.parse(path).getroot()
    settings = root.find("settings")
    assert settings is not None
    return plan, root, settings


def text(settings: ET.Element, tag: str) -> str | None:
    value = settings.findtext(tag)
    return value.strip() if value is not None else None


def test_model_start_tracks_one_batch_without_tallies_or_statistics(tmp_path: Path):
    plan, root, s = patched(tmp_path, TrackRequest(histories=50))
    assert root.find("tallies") is None and root.find("plots") is None
    assert (text(s, "particles"), text(s, "batches"), text(s, "inactive")) == ("50", "1", None)
    # one generation without the fission bank: a fixed-source run without fission neutrons
    assert text(s, "run_mode") == "fixed source" and text(s, "create_fission_neutrons") == "false"
    assert text(s, "max_tracks") == "50" and text(s, "seed") == "9"
    for tag in ("state_point", "entropy_mesh", "trigger", "keff_trigger", "track"):
        assert s.find(tag) is None, tag
    assert s.find("generations_per_batch") is None
    assert s.findtext("output/summary") == "false" and s.findtext("output/tallies") == "false"
    assert sorted(m.get("id") or "" for m in s.findall("mesh")) == ["3", "7"]
    assert len(s.findall("source")) == 1
    assert plan.all_tracks and plan.run_mode == "eigenvalue"
    assert plan.weight_windows and not plan.photon_transport


def test_fission_start_tracks_the_batch_after_the_inactive_ones(tmp_path: Path):
    plan, _, s = patched(tmp_path, TrackRequest(histories=3, start="fission"))
    batch = FISSION_BATCHES + 1
    assert text(s, "particles") == str(FISSION_PARTICLES)
    assert (text(s, "batches"), text(s, "inactive")) == (str(batch), str(FISSION_BATCHES))
    # spread over the batch: consecutive particles often share a fission site
    assert text(s, "track") == f"{batch} 1 1 {batch} 1 334 {batch} 1 667"
    assert text(s, "run_mode") == "eigenvalue" and s.find("create_fission_neutrons") is None
    assert not plan.all_tracks


def test_fission_start_needs_an_eigenvalue_model(tmp_path: Path):
    with pytest.raises(ServiceError) as caught:
        patched(tmp_path, TrackRequest(start="fission"), mode="fixed source")
    assert caught.value.error == "bad_track_start"


def test_point_source_replaces_the_model_source(tmp_path: Path):
    point = PointSource(particle="photon", position=(1.5, -2, 0), energy=6.6e5)
    request = TrackRequest(start="point", point=point, weight_windows=False)
    plan, _, s = patched(tmp_path, request, mode="fixed source")
    sources = s.findall("source")
    assert len(sources) == 1 and sources[0].get("particle") == "photon"
    assert sources[0].findtext("space/parameters") == "1.5 -2.0 0.0"
    assert sources[0].findtext("energy/parameters") == "660000.0 1.0"
    assert sources[0].find("angle") is not None
    assert text(s, "photon_transport") == "true" and plan.photon_transport
    assert plan.warnings  # photon transport was turned on for it
    assert text(s, "weight_windows_on") == "false" and not plan.weight_windows
    assert text(s, "inactive") is None and s.find("create_fission_neutrons") is None


def test_photon_transport_can_be_chosen(tmp_path: Path):
    plan, _, s = patched(tmp_path, TrackRequest(photon_transport=True))
    assert text(s, "photon_transport") == "true" and plan.photon_transport


def test_openmc_messages():
    log = (
        " WARNING: Particle 7 underwent maximum number of events.\n"
        " WARNING: Particle 7 underwent maximum number of events.\n"
        " WARNING: Could not compute uncertainties -- only one active batch simulated!\n"
        " ERROR: No fission sites banked\n"
        "        on processor 0\n"
        " something else\n"
    )
    error, warnings = openmc_messages(log)
    assert error == "ERROR: No fission sites banked on processor 0"
    assert warnings == ["Particle 7 underwent maximum number of events."]


# -- the service, with a fake OpenMC (it writes tracks.h5, so it needs h5py) ------------------


class Models:
    def __init__(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "model.xml").write_text(MODEL.format(mode="eigenvalue"), encoding="utf-8")
        info = ModelInfo(
            model_id="m1",
            source="m.py",
            kind="python",
            library="builtin",
            bounding_box=BoundingBox(lower_left=(-1, -1, -1), upper_right=(1, 1, 1)),
            root_universe=0,
        )
        self.model = OpenedModel(info, folder)

    def get(self, model_id: str) -> OpenedModel:
        if model_id != "m1":
            raise ServiceError("unknown_model", model_id, status=404)
        return self.model

    def library(self, library_id):
        return LibraryInfo(id="builtin", name="lib", path="/xs.xml", builtin=True)


@pytest.fixture
def service(tmp_path: Path) -> TrackService:
    pytest.importorskip("h5py")
    config = Config(
        token="t",
        workspace=tmp_path / "ws",
        cross_sections=None,
        custom_data=tmp_path / "custom",
        cache_dir=tmp_path / "cache",
        cpus=16,
    )
    models = Models(tmp_path / "cache" / "models" / "m1")
    return TrackService(config, models, command=[sys.executable, str(FAKE)])  # type: ignore[arg-type]


def test_generate_answers_with_a_frame_and_cleans_up(
    service: TrackService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    args = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_TRACKS_ARGS", str(args))
    data = decode_tracks(service.generate("m1", TrackRequest(histories=4, seed=11)))
    assert data.n_histories == 4 and data.history_id.tolist() == [1, 2, 3, 4]
    assert data.summary.seed == 11 and data.summary.model_id == "m1"
    assert data.summary.warnings == ["a fake warning"]
    assert data.summary.count(TrackEvent.ABSORBED) == 4
    assert args.read_text(encoding="utf-8") == "--track -s 4"  # at most 4 threads
    assert list(service.root.iterdir()) == []


def test_fission_start_uses_the_track_list(
    service: TrackService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    args = tmp_path / "args.txt"
    monkeypatch.setenv("FAKE_TRACKS_ARGS", str(args))
    data = decode_tracks(service.generate("m1", TrackRequest(histories=2, start="fission")))
    assert data.n_histories == 2 and args.read_text(encoding="utf-8") == "-s 4"
    assert 0 < data.summary.seed < 2**31


def test_openmc_errors_become_tracks_failed(service: TrackService, monkeypatch):
    monkeypatch.setenv("FAKE_TRACKS_FAIL", "1")
    with pytest.raises(ServiceError) as caught:
        service.generate("m1", TrackRequest(histories=1))
    assert caught.value.error == "tracks_failed"
    assert caught.value.detail == "ERROR: No fissionable material in the fake model."


def test_unknown_model(service: TrackService):
    with pytest.raises(ServiceError) as caught:
        service.generate("nope", TrackRequest())
    assert caught.value.status == 404


def test_cancel_ends_the_running_request(service: TrackService, monkeypatch):
    monkeypatch.setenv("FAKE_TRACKS_DELAY", "30")
    errors: list[ServiceError] = []

    def run() -> None:
        try:
            service.generate("m1", TrackRequest(histories=1))
        except ServiceError as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 10
    while service._job is None or service._job.process is None:  # wait until OpenMC runs
        assert time.monotonic() < deadline
        time.sleep(0.02)
    started = time.monotonic()
    service.cancel()
    thread.join(10)
    assert time.monotonic() - started < 5
    assert [e.error for e in errors] == ["cancelled"] and errors[0].status == 409


def test_time_limit(service: TrackService, monkeypatch):
    monkeypatch.setenv("FAKE_TRACKS_DELAY", "30")
    service.time_limit = 0.5
    with pytest.raises(ServiceError) as caught:
        service.generate("m1", TrackRequest(histories=1))
    assert caught.value.error == "tracks_timeout"
