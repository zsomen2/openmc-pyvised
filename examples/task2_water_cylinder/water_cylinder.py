"""Task 2: a uniform photon source in a water cylinder.

The photon flux on the lateral surface of a water cylinder (radius 1 cm, height 10 cm)
that holds a spatially uniform source of 1e6 photons/s with the energy of Task 1 (1 MeV).

The model is laid out like an MCNP deck: title, cell cards, surface cards, data cards.
Above each statement sits the MCNP card it stands for, so the comments add up to the
equivalent MCNP input.

How to use it
-------------
- The radius, the height and the photon energy are builder parameters (pyvised.json): set
  them in the parameters panel and run. The run history compares runs against any of them.
- The task gives no size: the example uses radius 1 cm and height 10 cm.

Differences from MCNP
---------------------
- OpenMC needs no cell outside the cylinder: imp:p=0 of cell 11 becomes vacuum boundaries
  on surfaces 1, 2 and 3.
- The source is sampled as in MCNP's sdef with cel=10: uniformly in the box around the
  cylinder, keeping only the points inside cell 10 (OpenMC: a domain constraint).
- f2 becomes a surface-flux tally on surface 1, the lateral surface (top and bottom are
  surfaces 2 and 3). OpenMC does not divide it by the area: PyVised's normalisation
  (pyvised.json) multiplies by the source strength, 1e6 photons/s (MCNP: fm2 1e6), and
  divides by the area 2·pi·radius·height, so the results are in photons/(cm²·s).
- Tally 12 is the uncollided part of tally 2: the photons that reach the lateral surface
  without any interaction. The rest has scattered in the water at least once.

Running the script writes model.xml. PyVised runs it with PYVISED_EXPORT_ONLY=1 and
passes the builder parameters in PYVISED_PARAMS.
"""

import openmc

from pyvised_course import parameter, strict_ids

RADIUS = parameter("radius", 1.0)  # cm
HEIGHT = parameter("height", 10.0)  # cm
ENERGY = parameter("energy", 1.0)  # MeV


# ---------------------------------------------------------------------------
# Volume source in a water cylinder
# c
# c CELLS
# c
# ---------------------------------------------------------------------------


def cell_cards(s, mat):
    c = {}
    # 10  1 -1.0  -1 -2 3   imp:p=1   $ water cylinder
    c[10] = openmc.Cell(cell_id=10, name="water cylinder", fill=mat[1])
    c[10].region = -s[1] & -s[2] & +s[3]
    # 11  0       #10       imp:p=0   $ outside
    # -> no cell: imp:p=0 makes surfaces 1, 2 and 3 vacuum boundaries
    return c


# ---------------------------------------------------------------------------
# c SURFACES
# c
# ---------------------------------------------------------------------------


def surface_cards():
    s = {}
    # 1   cz   1.0   $ lateral surface              (vacuum: cell 11 has imp:p=0)
    s[1] = openmc.ZCylinder(surface_id=1, r=RADIUS, name="lateral surface", boundary_type="vacuum")
    # 2   pz   5.0   $ top                          (vacuum: cell 11 has imp:p=0)
    s[2] = openmc.ZPlane(surface_id=2, z0=HEIGHT / 2, name="top", boundary_type="vacuum")
    # 3   pz  -5.0   $ bottom                       (vacuum: cell 11 has imp:p=0)
    s[3] = openmc.ZPlane(surface_id=3, z0=-HEIGHT / 2, name="bottom", boundary_type="vacuum")
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
    return mat


def source_cards(settings, c):
    # mode p
    settings.photon_transport = True
    settings.run_mode = "fixed source"
    # sdef x=d1 y=d2 z=d3 cel=10 erg=1.0   $ uniform in the box, kept inside cell 10 [MeV]
    # si1 -1.0 1.0   sp1 0 1
    # si2 -1.0 1.0   sp2 0 1
    # si3 -5.0 5.0   sp3 0 1
    settings.source = openmc.IndependentSource(
        space=openmc.stats.Box((-RADIUS, -RADIUS, -HEIGHT / 2), (RADIUS, RADIUS, HEIGHT / 2)),
        energy=openmc.stats.Discrete([ENERGY * 1e6], [1.0]),  # MeV -> eV
        particle="photon",
        constraints={"domains": [c[10]]},
    )
    # nps 1e6   -> 100 batches of 10000 photons
    settings.particles = 10000
    settings.batches = 100


def tally_cards(s):
    surface = openmc.SurfaceFilter([s[1]])
    photons = openmc.ParticleFilter(["photon"])
    # f2:p 1   $ photon flux on the lateral surface
    # fm2 1e6  $ source strength 1e6 photons/s   -> pyvised.json, which divides by the area too
    t2 = openmc.Tally(tally_id=2, name="photon flux on the lateral surface")
    t2.filters = [surface, photons]
    t2.scores = ["flux"]
    # f12:p 1   $ its uncollided part
    # ft12 inc  fu12 0       $ photons without a collision ...
    # e12 0.999 1.001        $ ... and with the source energy
    # fm12 1e6
    # (secondary photons - fluorescence, bremsstrahlung, annihilation - start without a
    # collision of their own; the energy bin leaves them out)
    source_energy = openmc.EnergyFilter([0.999e6 * ENERGY, 1.001e6 * ENERGY])  # MeV -> eV
    t12 = openmc.Tally(tally_id=12, name="uncollided photon flux on the lateral surface")
    t12.filters = [surface, photons, openmc.CollisionFilter([0]), source_energy]
    t12.scores = ["flux"]
    return [t2, t12]


def build():
    s = surface_cards()
    mat = material_cards()
    c = cell_cards(s, mat)

    model = openmc.Model()
    model.geometry = openmc.Geometry(list(c.values()))
    model.materials = openmc.Materials(mat.values())
    source_cards(model.settings, c)
    model.tallies = openmc.Tallies(tally_cards(s))
    return model


if __name__ == "__main__":
    with strict_ids():
        model = build()
    model.export_to_model_xml()
