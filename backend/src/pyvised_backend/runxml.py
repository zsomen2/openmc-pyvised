"""The run's copy of the model: the run settings patched into its ``model.xml``.

Only the copy in the run folder changes, never the student's source. The patch
sets particles, batches, inactive batches and the seed; turns the σ(k-eff)
and tally relative-error targets into OpenMC triggers (the planned batches are
then the most batches); schedules statepoints every ~5 % of the batches (~1 % with
a time limit, which usually ends the run early), so a stopped run keeps results;
and adds a Shannon entropy mesh over the bounding
box to eigenvalue runs that have none.
"""

import math
import xml.etree.ElementTree as ET
from pathlib import Path

from pyvised_protocol.runs import RunPlan, RunSettings
from pyvised_protocol.schemas import BoundingBox

from .errors import ServiceError

STATEPOINT_SHARE = 0.05
"""Statepoints are written about this often (share of the batches)."""
TIME_LIMIT_STATEPOINT_SHARE = 0.01
"""… and this often in a run with a time limit."""
FIRST_CHECK_SHARE = 0.1
MIN_ACTIVE_BEFORE_CHECK = 10
"""Active batches before a trigger is checked the first time (few batches give a poor σ)."""
ENTROPY_PARTICLES_PER_CELL = 20.0
"""OpenMC's heuristic for the entropy mesh: about particles / 20 mesh cells."""


def _int(element: ET.Element, tag: str) -> int | None:
    text = (element.findtext(tag) or "").strip()
    return int(text) if text else None


def _set(parent: ET.Element, tag: str, value: object) -> ET.Element:
    child = parent.find(tag)
    if child is None:
        child = ET.SubElement(parent, tag)
    child.text = str(value)
    return child


def _remove(parent: ET.Element, tag: str) -> None:
    for child in parent.findall(tag):
        parent.remove(child)


def statepoint_batches(
    batches: int, every: int | None = None, extra: tuple[int, ...] = ()
) -> list[int]:
    """Batches that get a statepoint: every `every` (default ~5 % of `batches`) and the last."""
    step = every or max(1, round(batches * STATEPOINT_SHARE))
    planned = set(range(step, batches + 1, step)) | {batches}
    planned |= {b for b in extra if 0 < b <= batches}
    return sorted(planned)


def first_check(batches: int, inactive: int) -> int:
    """Batch of the first trigger check: some active batches in, at most `batches`."""
    active = max(MIN_ACTIVE_BEFORE_CHECK, math.ceil(FIRST_CHECK_SHARE * (batches - inactive)))
    return min(batches, inactive + active)


def entropy_dimension(particles: int) -> int:
    return max(1, math.ceil((particles / ENTROPY_PARTICLES_PER_CELL) ** (1.0 / 3.0)))


def _mesh_ids(root: ET.Element) -> set[int]:
    ids = set()
    for mesh in root.iter("mesh"):
        value = mesh.get("id")
        if value is not None and value.strip().lstrip("-").isdigit():
            ids.add(int(value))
    return ids


def _add_entropy_mesh(root: ET.Element, settings: ET.Element, box: BoundingBox, n: int) -> None:
    mesh_id = max(_mesh_ids(root), default=0) + 1
    mesh = ET.SubElement(settings, "mesh", {"id": str(mesh_id), "name": "PyVised entropy mesh"})
    ET.SubElement(mesh, "dimension").text = f"{n} {n} {n}"
    ET.SubElement(mesh, "lower_left").text = " ".join(f"{v:.10g}" for v in box.lower_left)
    ET.SubElement(mesh, "upper_right").text = " ".join(f"{v:.10g}" for v in box.upper_right)
    _set(settings, "entropy_mesh", mesh_id)


def patch_model(
    model_xml: Path, request: RunSettings, threads: int, box: BoundingBox | None = None
) -> tuple[RunPlan, list[str]]:
    """Apply `request` to the run copy `model_xml` in place; returns what the run will do and
    warnings about settings that do not apply."""
    tree = ET.parse(model_xml)
    root = tree.getroot()
    settings = root.find("settings") if root.tag == "model" else None
    if settings is None:
        raise ServiceError("bad_model", "the exported model has no <settings>")
    run_mode = (settings.findtext("run_mode") or "eigenvalue").strip()
    eigenvalue = run_mode == "eigenvalue"
    warnings: list[str] = []

    particles = request.particles or _int(settings, "particles")
    batches = request.batches or _int(settings, "batches")
    if not particles or not batches:
        raise ServiceError(
            "incomplete_settings",
            "The model sets no particles or batches; enter them in the run settings.",
        )
    inactive = 0
    if eigenvalue:
        inactive = (
            request.inactive if request.inactive is not None else _int(settings, "inactive") or 0
        )
        if inactive >= batches:
            raise ServiceError(
                "bad_settings",
                f"{inactive} inactive batches leave no active batch of {batches} batches.",
            )
    generations = _int(settings, "generations_per_batch") or 1
    _set(settings, "particles", particles)
    if eigenvalue:
        _set(settings, "inactive", inactive)
    if request.seed is not None:
        _set(settings, "seed", request.seed)

    triggers: list[str] = []
    keff_std = request.keff_std
    if keff_std is not None and not eigenvalue:
        warnings.append("σ(k-eff) target ignored: the model runs in fixed-source mode")
        keff_std = None
    check: int | None = None
    tallies = root.find("tallies")
    tally_elements = [] if tallies is None else tallies.findall("tally")
    rel_err = request.tally_rel_err
    if rel_err is not None and not tally_elements:
        warnings.append("relative-error target ignored: the model has no tallies")
        rel_err = None
    # trigger mode only with a condition: without one OpenMC stops at the first check
    if keff_std is not None or rel_err is not None:
        _remove(settings, "keff_trigger")
        _remove(settings, "trigger")
        check = first_check(batches, inactive)
        _set(settings, "batches", check)
        trigger = ET.SubElement(settings, "trigger")
        ET.SubElement(trigger, "active").text = "true"
        ET.SubElement(trigger, "max_batches").text = str(batches)
        ET.SubElement(trigger, "batch_interval").text = "1"
        if keff_std is not None:
            element = ET.SubElement(settings, "keff_trigger")
            ET.SubElement(element, "threshold").text = repr(float(keff_std))
            ET.SubElement(element, "type").text = "std_dev"
            triggers.append(f"σ(k-eff) ≤ {keff_std:g}")
        if rel_err is not None:
            for tally in tally_elements:
                ET.SubElement(
                    tally,
                    "trigger",
                    {"type": "rel_err", "threshold": repr(float(rel_err)), "ignore_zeros": "true"},
                )
            triggers.append(f"tally relative error ≤ {rel_err:g}")
    else:
        _set(settings, "batches", batches)
        trigger = settings.find("trigger")
        if trigger is not None and (trigger.findtext("active") or "").strip() == "true":
            # the model's own triggers: its batches are the first check
            check = batches
            batches = int((trigger.findtext("max_batches") or str(batches)).strip())
            triggers.append("the model's triggers")

    every = request.statepoint_every
    if every is None and request.time_limit is not None:
        every = max(1, round(batches * TIME_LIMIT_STATEPOINT_SHARE))
    statepoints = statepoint_batches(batches, every, () if check is None else (check,))
    _remove(settings, "state_point")
    state_point = ET.SubElement(settings, "state_point")
    ET.SubElement(state_point, "batches").text = " ".join(str(b) for b in statepoints)

    entropy = settings.find("entropy_mesh") is not None
    added = False
    if eigenvalue and not entropy and request.entropy_mesh and box is not None:
        _add_entropy_mesh(root, settings, box, entropy_dimension(particles))
        entropy = added = True

    ET.indent(tree, space="  ")
    tree.write(model_xml, encoding="utf-8", xml_declaration=True)
    photon = (settings.findtext("photon_transport") or "").strip().lower() == "true"
    seed_text = (settings.findtext("seed") or "").strip()
    plan = RunPlan(
        run_mode=run_mode,
        particles=particles,
        batches=batches,
        inactive=inactive,
        generations_per_batch=generations,
        first_check=check,
        seed=int(seed_text) if seed_text else None,
        threads=threads,
        statepoints=statepoints,
        entropy=entropy,
        entropy_added=added,
        photon_transport=photon,
        triggers=triggers,
    )
    return plan, warnings
