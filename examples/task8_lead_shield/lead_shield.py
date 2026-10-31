"""Task 8: a Co-60 + Cs-137 volume source in water, shielded by a lead annulus.

A water cylinder (height 2 cm, diameter 2 cm) holds 1 GBq Co-60 and 3 GBq Cs-137, a
uniform source of their gamma lines. A lead annulus (inner diameter 10 cm) surrounds it;
the photon flux (8.2) and the dose rate (8.3) on its outer lateral surface are tallied
while the wall grows from 2 to 20 cm (8.4, 8.5). The lead is made of 2 cm layers, so a
20 cm wall is the 10 annuli of 8.6; importances that grow from layer to layer (8.7)
become weight windows.

The model is laid out like an MCNP deck: title, cell cards, surface cards, data cards.
Above each statement sits the MCNP card it stands for, so the comments add up to the
equivalent MCNP input.

How to use it
-------------
- Builder parameters (pyvised.json): the lead thickness (2...20 cm in steps of 2) and the
  importance ratio between neighbouring layers (1 = no variance reduction; 2, 3 or 4 for
  8.7). Run the thicknesses with the model's 1e7 photons, then compare the runs in the
  history against "thickness": the dose rate, its relative error and the figure of merit.
- The task gives no height: the lead is 10 cm tall (z = -5...5 cm) and open at both ends.
  Photons that leave through the ends or the outer surface are lost (imp=0 outside).
- The source has the gamma lines only (Co-60: 1173 and 1332 keV, Cs-137: 662 keV from
  Ba-137m); the 32 keV x-rays of Ba-137m would not get through the lead anyway.

Differences from MCNP
---------------------
- imp:p=0 of cell 100 becomes vacuum boundaries on the outer lead surface and on planes 12
  and 13; the outside cell does not exist.
- The source is sampled as in MCNP's sdef with cel=1: uniformly in the box around the water
  cylinder, keeping only the points inside cell 1 (OpenMC: a domain constraint).
- f2 becomes a surface-flux tally. OpenMC does not divide it by the area: PyVised's
  normalisation (pyvised.json) multiplies by the source strength, 4.551e9 photons/s (MCNP:
  fm2), and divides by the area 2·pi·(5 + thickness)·10 cm². Tally 12 adds the ICRP-116
  dose coefficients (de/df cards: an EnergyFunctionFilter, log-log) and the conversion
  pSv/s -> µSv/h.
- imp:p of the lead layers becomes weight windows on a cylindrical mesh along the layers:
  lower bound 0.5/imp, upper/lower ratio 5, survival ratio 3 (the defaults of MCNP's WWG
  and WWP cards). With ratio 1 the model has none, like imp:p=1 everywhere. As MCNP
  splits at cell boundaries, the windows are checked at surface crossings as well as at
  collisions: at 20 cm this lifts the figure of merit from about 35 (no importances) to
  about 4e3, 1e4 and 2e4 for the ratios 2, 3 and 4 (collisions only: about 60-100).

Running the script writes model.xml. PyVised runs it with PYVISED_EXPORT_ONLY=1 and
passes the builder parameters in PYVISED_PARAMS.
"""

import math

import openmc
import openmc.data

from pyvised_course import parameter, strict_ids, weight_windows

THICKNESS = parameter("thickness", 2.0)  # cm of lead
RATIO = parameter("importance_ratio", 1.0)  # importance of a layer / the one inside it
LAYERS = max(1, math.ceil(THICKNESS / 2 - 1e-9))  # 2 cm layers; the last one may be thinner
INNER = 5.0  # cm, inner radius of the lead
LAYER_RADII = [INNER + min(2.0 * k, THICKNESS) for k in range(1, LAYERS + 1)]

# activity [Bq] and lines [keV, photons per decay] of the nuclides (ENSDF)
NUCLIDES = {
    "Co60": (1.0e9, [(1173.228, 0.9985), (1332.492, 0.999826)]),
    "Cs137": (3.0e9, [(661.657, 0.8510)]),  # through Ba-137m
}
LINES = [(kev, activity * share) for activity, lines in NUCLIDES.values() for kev, share in lines]
SOURCE_STRENGTH = sum(rate for _, rate in LINES)  # photons/s, 4.551e9


# ---------------------------------------------------------------------------
# Co-60 + Cs-137 in water, shielded by lead
# c
# c CELLS
# c
# ---------------------------------------------------------------------------


def cell_cards(s, mat):
    c = {}
    imp = {}
    # 1   1 -1.0     -1 2 -3        imp:p=1   $ source: water with Co-60 and Cs-137
    c[1] = openmc.Cell(cell_id=1, name="source", fill=mat[1], region=-s[1] & +s[2] & -s[3])
    imp[1] = 1.0
    # 10  0          #1 -10 12 -13  imp:p=1   $ gap inside the lead
    c[10] = openmc.Cell(cell_id=10, name="gap inside the lead")
    c[10].region = ~c[1].region & -s[10] & +s[12] & -s[13]
    imp[10] = 1.0
    # 11  2 -11.35   10 -101 12 -13  imp:p=1   $ lead layer 1
    # 12  2 -11.35  101 -102 12 -13  imp:p=2   $ lead layer 2 (importance ratio 2)
    # ...                                     one cell per 2 cm of lead, up to 20 (8.6)
    inside = s[10]
    for k in range(1, LAYERS + 1):
        c[10 + k] = openmc.Cell(cell_id=10 + k, name=f"lead layer {k}", fill=mat[2])
        c[10 + k].region = +inside & -s[100 + k] & +s[12] & -s[13]
        imp[10 + k] = RATIO ** (k - 1)
        inside = s[100 + k]
    # 100 0          #(-10x 12 -13)  imp:p=0   $ outside
    # -> no cell: imp:p=0 makes the outer lead surface and planes 12, 13 vacuum boundaries
    return c, imp


# ---------------------------------------------------------------------------
# c SURFACES
# c
# ---------------------------------------------------------------------------


def surface_cards():
    s = {}
    # c source volume
    # 1    cz   1.0    $ source radius
    s[1] = openmc.ZCylinder(surface_id=1, r=1.0, name="source radius")
    # 2    pz  -1.0    $ source bottom
    s[2] = openmc.ZPlane(surface_id=2, z0=-1.0, name="source bottom")
    # 3    pz   1.0    $ source top
    s[3] = openmc.ZPlane(surface_id=3, z0=1.0, name="source top")
    # c lead
    # 10   cz   5.0    $ inner lead surface
    s[10] = openmc.ZCylinder(surface_id=10, r=INNER, name="inner lead surface")
    # 12   pz  -5.0    $ lead bottom                (vacuum: cell 100 has imp:p=0)
    s[12] = openmc.ZPlane(surface_id=12, z0=-5.0, name="lead bottom", boundary_type="vacuum")
    # 13   pz   5.0    $ lead top                   (vacuum: cell 100 has imp:p=0)
    s[13] = openmc.ZPlane(surface_id=13, z0=5.0, name="lead top", boundary_type="vacuum")
    # c layers of 2 cm; the last one is the outer lead surface (vacuum: cell 100 has imp:p=0)
    # 101  cz   7.0
    # 102  cz   9.0
    # ...
    for k, radius in enumerate(LAYER_RADII, start=1):
        outer = k == LAYERS
        s[100 + k] = openmc.ZCylinder(
            surface_id=100 + k, r=radius, name="outer lead surface" if outer else "",
            boundary_type="vacuum" if outer else "transmission",
        )  # fmt: skip
    return s


# ---------------------------------------------------------------------------
# c DATA
# c
# ---------------------------------------------------------------------------


def material_cards():
    mat = {}
    # m1   1000 2   $ H
    #      8000 1   $ O -> water (rho 1.0)
    mat[1] = openmc.Material(material_id=1, name="water")
    mat[1].add_element("H", 2.0, "ao")
    mat[1].add_element("O", 1.0, "ao")
    mat[1].set_density("g/cm3", 1.0)
    # m2   82000 1  $ lead (rho 11.35)
    mat[2] = openmc.Material(material_id=2, name="lead")
    mat[2].add_element("Pb", 1.0, "ao")
    mat[2].set_density("g/cm3", 11.35)
    return mat


def source_cards(settings, c):
    # mode p
    settings.photon_transport = True
    settings.run_mode = "fixed source"
    # sdef x=d1 y=d2 z=d3 cel=1 erg=d4   $ uniform in cell 1
    # si1 -1 1   sp1 0 1
    # si2 -1 1   sp2 0 1
    # si3 -1 1   sp3 0 1
    # si4 l 0.661657 1.173228 1.332492   $ gamma lines [MeV]
    # sp4   2.553e9  0.9985e9 0.999826e9 $ photons/s: 3 GBq Cs-137, 1 GBq Co-60
    settings.source = openmc.IndependentSource(
        space=openmc.stats.Box((-1.0, -1.0, -1.0), (1.0, 1.0, 1.0)),
        energy=openmc.stats.Discrete(
            [kev * 1e3 for kev, _ in LINES], [rate / SOURCE_STRENGTH for _, rate in LINES]
        ),  # keV -> eV
        particle="photon",
        constraints={"domains": [c[1]]},
    )
    # nps 1e7   -> 100 batches of 100000 photons (8.4)
    settings.particles = 100000
    settings.batches = 100


def tally_cards(s):
    outer = openmc.SurfaceFilter([s[100 + LAYERS]])
    photons = openmc.ParticleFilter(["photon"])
    # f2:p 10x   $ photon flux on the outer lead surface (8.2)
    # fm2 4.551e9   $ photons/s   -> pyvised.json, which divides by the area too
    t2 = openmc.Tally(tally_id=2, name="photon flux on the outer lead surface")
    t2.filters = [outer, photons]
    t2.scores = ["flux"]
    # f12:p 10x   $ dose rate on the outer lead surface (8.3)
    # de12 / df12   $ ICRP-116 effective dose per fluence, AP [pSv cm²], log-log
    # fm12 1.638e7  $ 4.551e9 photons/s × 3.6e-3 (pSv/s -> µSv/h)   -> pyvised.json
    energy, coefficient = openmc.data.dose_coefficients("photon", geometry="AP")
    dose = openmc.EnergyFunctionFilter(energy, coefficient, interpolation="log-log")
    t12 = openmc.Tally(tally_id=12, name="dose rate on the outer lead surface")
    t12.filters = [outer, photons, dose]
    t12.scores = ["flux"]
    return [t2, t12]


def importance_cards(s, imp):
    """imp:p of the cells -> weight windows on a mesh of the gap and the lead layers."""
    layers = openmc.CylindricalMesh(
        r_grid=[0.0, s[10].r, *LAYER_RADII], z_grid=[s[12].z0, s[13].z0],
        mesh_id=1, name="gap and lead layers",
    )  # fmt: skip
    # the mesh's first ring holds cells 1 and 10 (both imp:p=1)
    importances = [imp[10]] + [imp[10 + k] for k in range(1, LAYERS + 1)]
    return [weight_windows(layers, importances, particle="photon")]


def build():
    s = surface_cards()
    mat = material_cards()
    c, imp = cell_cards(s, mat)

    model = openmc.Model()
    model.geometry = openmc.Geometry(list(c.values()))
    model.materials = openmc.Materials(mat.values())
    source_cards(model.settings, c)
    model.tallies = openmc.Tallies(tally_cards(s))
    if RATIO > 1:
        model.settings.weight_windows = importance_cards(s, imp)
        model.settings.weight_windows_on = True
        # MCNP splits and plays roulette where a particle crosses into a cell of other importance;
        # OpenMC checks weight windows at collisions only unless asked to at surfaces too
        model.settings.weight_window_checkpoints = {"collision": True, "surface": True}
    return model


if __name__ == "__main__":
    with strict_ids():
        model = build()
    model.export_to_model_xml()
