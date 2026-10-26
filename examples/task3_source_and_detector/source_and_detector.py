"""Task 3: a source cylinder and a detector cylinder of water.

The source cylinder is the one of Task 2 (radius 1 cm, height 10 cm, a uniform source of
1e6 particles/s with 1 MeV); the detector is a second water cylinder (radius 3 cm, same
height) beside it, whose volume tally gives the flux. 3.1 uses photons; 3.2 replaces them
with 1 MeV neutrons and looks at the neutron spectrum in the detector on 100 logarithmic
bins from 1e-9 to 10 MeV; 3.3 plots that spectrum on log-log axes (Simulation tab, Plots).

The model is laid out like an MCNP deck: title, cell cards, surface cards, data cards.
Above each statement sits the MCNP card it stands for, so the comments add up to the
equivalent MCNP input.

How to use it
-------------
- Builder parameters (pyvised.json): "neutron" (off = photons for 3.1, on = neutrons
  for 3.2) and the distance of the cylinder axes. The run presets "3.1" and
  "3.2" set the particle for you.
- The task gives no detector size or distance: the example uses radius 3 cm, the height of
  the source cylinder, and 10 cm between the axes.

Differences from MCNP
---------------------
- The void around the cylinders ends at sphere 10 (MCNP: the cell outside it has
  imp=0), which becomes a vacuum boundary; the outside cell does not exist. The sphere
  follows the distance, so it always encloses both cylinders.
- The source is sampled as in MCNP's sdef with cel=10: uniformly in the box around the
  source cylinder, keeping only the points inside cell 10 (OpenMC: a domain constraint).
- f4 becomes a track-length flux tally in cell 20. MCNP divides it by the cell volume,
  OpenMC does not: PyVised's normalisation (pyvised.json) multiplies by the source
  strength, 1e6 particles/s (MCNP: fm4 1e6), and divides by the detector's volume
  pi·3²·10 cm³, so the results are in particles/(cm²·s). Tally 14 is the same flux per
  energy bin (MCNP: e14).
- Thermal scattering: water has the S(a,b) table c_H_in_H2O (MCNP: mt1 lwtr), which matters
  for the thermal end of the neutron spectrum.

Running the script writes model.xml. PyVised runs it with PYVISED_EXPORT_ONLY=1 and
passes the builder parameters in PYVISED_PARAMS.
"""

import numpy as np
import openmc

from pyvised_course import parameter, strict_ids

PARTICLE = "neutron" if parameter("neutron", 0) else "photon"
DISTANCE = parameter("distance", 10.0)  # cm, between the cylinder axes
ENERGY = 1.0  # MeV


# ---------------------------------------------------------------------------
# Source and detector cylinders
# c
# c CELLS
# c
# ---------------------------------------------------------------------------


def cell_cards(s, mat):
    c = {}
    # 10   1 -1.0  -1 -2 3      imp:p=1   $ source cylinder
    c[10] = openmc.Cell(cell_id=10, name="source cylinder", fill=mat[1])
    c[10].region = -s[1] & -s[2] & +s[3]
    # 20   1 -1.0  -5 -2 3      imp:p=1   $ detector cylinder
    c[20] = openmc.Cell(cell_id=20, name="detector cylinder", fill=mat[1])
    c[20].region = -s[5] & -s[2] & +s[3]
    # 101  0       -10 #10 #20  imp:p=1   $ void around them
    c[101] = openmc.Cell(cell_id=101, name="void around them")
    c[101].region = -s[10] & ~c[10].region & ~c[20].region
    # 100  0        10          imp:p=0   $ outside
    # -> no cell: imp:p=0 makes surface 10 a vacuum boundary
    return c


# ---------------------------------------------------------------------------
# c SURFACES
# c
# ---------------------------------------------------------------------------


def surface_cards():
    s = {}
    # 1   cz    1.0          $ source cylinder
    s[1] = openmc.ZCylinder(surface_id=1, r=1.0, name="source cylinder")
    # 2   pz    5.0          $ top of both cylinders
    s[2] = openmc.ZPlane(surface_id=2, z0=5.0, name="top")
    # 3   pz   -5.0          $ bottom of both cylinders
    s[3] = openmc.ZPlane(surface_id=3, z0=-5.0, name="bottom")
    # 5   c/z  10.0 0  3.0   $ detector cylinder
    s[5] = openmc.ZCylinder(surface_id=5, x0=DISTANCE, r=3.0, name="detector cylinder")
    # 10  s     5.0 0 0  10.0   $ enclosing sphere      (vacuum: cell 100 has imp:p=0)
    s[10] = openmc.Sphere(
        surface_id=10, x0=DISTANCE / 2, r=DISTANCE / 2 + 5.0, name="enclosing sphere",
        boundary_type="vacuum",
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
    # mt1  lwtr
    mat[1] = openmc.Material(material_id=1, name="water")
    mat[1].add_element("H", 2.0, "ao")
    mat[1].add_element("O", 1.0, "ao")
    mat[1].set_density("g/cm3", 1.0)
    mat[1].add_s_alpha_beta("c_H_in_H2O")
    return mat


def source_cards(settings, c):
    # mode p              (3.2: mode n)
    settings.photon_transport = PARTICLE == "photon"
    settings.run_mode = "fixed source"
    # sdef x=d1 y=d2 z=d3 cel=10 erg=1.0 par=p   $ uniform in cell 10 [MeV]   (3.2: par=n)
    # si1 -1.0 1.0   sp1 0 1
    # si2 -1.0 1.0   sp2 0 1
    # si3 -5.0 5.0   sp3 0 1
    settings.source = openmc.IndependentSource(
        space=openmc.stats.Box((-1.0, -1.0, -5.0), (1.0, 1.0, 5.0)),
        energy=openmc.stats.Discrete([ENERGY * 1e6], [1.0]),  # MeV -> eV
        particle=PARTICLE,
        constraints={"domains": [c[10]]},
    )
    # nps 1e6   -> 100 batches of 10000 particles
    settings.particles = 10000
    settings.batches = 100


def tally_cards(c):
    detector = openmc.CellFilter([c[20]])
    particles = openmc.ParticleFilter([PARTICLE])
    # f4:p 20   $ flux in the detector cylinder           (3.2: f4:n)
    # fm4 1e6   $ source strength 1e6/s   -> pyvised.json, which divides by the volume too
    t4 = openmc.Tally(tally_id=4, name="flux in the detector")
    t4.filters = [detector, particles]
    t4.scores = ["flux"]
    # f14:p 20  $ its spectrum                            (3.2: f14:n)
    # e14 1e-9 99ilog 10   $ 100 logarithmic bins from 1e-9 to 10 MeV
    # fm14 1e6
    bins = openmc.EnergyFilter(np.logspace(-9, 1, 101) * 1e6)  # MeV -> eV
    t14 = openmc.Tally(tally_id=14, name="spectrum in the detector")
    t14.filters = [detector, particles, bins]
    t14.scores = ["flux"]
    return [t4, t14]


def build():
    s = surface_cards()
    mat = material_cards()
    c = cell_cards(s, mat)

    model = openmc.Model()
    model.geometry = openmc.Geometry(list(c.values()))
    model.materials = openmc.Materials(mat.values())
    source_cards(model.settings, c)
    model.tallies = openmc.Tallies(tally_cards(c))
    return model


if __name__ == "__main__":
    with strict_ids():
        model = build()
    model.export_to_model_xml()
