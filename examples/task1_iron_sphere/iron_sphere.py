"""Task 1: a point photon source in a solid iron sphere.

The photon flux on the surface of an iron sphere (7.85 g/cm³) with a point source of
1e6 photons/s at its centre, as a function of the sphere's radius (3 cm to start with).

The model is laid out like an MCNP deck: title, cell cards, surface cards, data cards.
Above each statement sits the MCNP card it stands for, so the comments add up to the
equivalent MCNP input.

How to use it
-------------
- The radius and the photon energy are builder parameters (pyvised.json): set them in
  the parameters panel, run, and repeat for other radii. The run history then compares
  the runs with "radius" on the x axis.
- The task gives no photon energy: the example uses 1 MeV.

Differences from MCNP
---------------------
- OpenMC needs no cell outside the sphere: imp:p=0 of cell 11 becomes a vacuum boundary
  on surface 1.
- f2 becomes a surface-flux tally. Like MCNP, OpenMC gives tallies per source particle,
  but it does not divide a surface flux by the area. PyVised's normalisation
  (pyvised.json) multiplies by the source strength, 1e6 photons/s (MCNP: fm2 1e6), and
  divides by the area 4·pi·radius², so the results are in photons/(cm²·s).
- Tally 12 is the uncollided part of tally 2, the photons that reach the surface
  without any interaction. It equals S·exp(-mu·r)/(4·pi·r²), so tally 2 / tally 12 is
  the buildup factor at radius r.

Running the script writes model.xml. PyVised runs it with PYVISED_EXPORT_ONLY=1 and
passes the builder parameters in PYVISED_PARAMS.
"""

import openmc

from pyvised_course import parameter, strict_ids

RADIUS = parameter("radius", 3.0)  # cm
ENERGY = parameter("energy", 1.0)  # MeV


# ---------------------------------------------------------------------------
# Point source in an iron sphere
# c
# c CELLS
# c
# ---------------------------------------------------------------------------


def cell_cards(s, mat):
    c = {}
    # 10  1 -7.85  -1   imp:p=1   $ iron sphere
    c[10] = openmc.Cell(cell_id=10, name="iron sphere", fill=mat[1], region=-s[1])
    # 11  0         1   imp:p=0   $ outside
    # -> no cell: imp:p=0 makes surface 1 a vacuum boundary
    return c


# ---------------------------------------------------------------------------
# c SURFACES
# c
# ---------------------------------------------------------------------------


def surface_cards():
    s = {}
    # 1   so  3.0   $ sphere surface                 (vacuum: cell 11 has imp:p=0)
    s[1] = openmc.Sphere(surface_id=1, r=RADIUS, name="sphere surface", boundary_type="vacuum")
    return s


# ---------------------------------------------------------------------------
# c DATA
# c
# ---------------------------------------------------------------------------


def material_cards():
    mat = {}
    # m1   26000 1   $ iron (rho 7.85)
    mat[1] = openmc.Material(material_id=1, name="iron")
    mat[1].add_element("Fe", 1.0, "ao")
    mat[1].set_density("g/cm3", 7.85)
    return mat


def source_cards(settings):
    # mode p
    settings.photon_transport = True
    settings.run_mode = "fixed source"
    # sdef pos=0 0 0  erg=1.0   $ isotropic point source at the centre [MeV]
    settings.source = openmc.IndependentSource(
        space=openmc.stats.Point((0.0, 0.0, 0.0)),
        energy=openmc.stats.Discrete([ENERGY * 1e6], [1.0]),  # MeV -> eV
        particle="photon",
    )
    # nps 1e6   -> 100 batches of 10000 photons
    settings.particles = 10000
    settings.batches = 100


def tally_cards(s):
    surface = openmc.SurfaceFilter([s[1]])
    photons = openmc.ParticleFilter(["photon"])
    # f2:p 1   $ photon flux on the sphere surface
    # fm2 1e6  $ source strength 1e6 photons/s   -> pyvised.json, which divides by the area too
    t2 = openmc.Tally(tally_id=2, name="photon flux on the sphere surface")
    t2.filters = [surface, photons]
    t2.scores = ["flux"]
    # f12:p 1   $ its uncollided part
    # ft12 inc  fu12 0       $ photons without a collision ...
    # e12 0.999 1.001        $ ... and with the source energy
    # fm12 1e6
    # (secondary photons - fluorescence, bremsstrahlung, annihilation - start without a
    # collision of their own; the energy bin leaves them out)
    source_energy = openmc.EnergyFilter([0.999e6 * ENERGY, 1.001e6 * ENERGY])  # MeV -> eV
    t12 = openmc.Tally(tally_id=12, name="uncollided photon flux on the sphere surface")
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
    source_cards(model.settings)
    model.tallies = openmc.Tallies(tally_cards(s))
    return model


if __name__ == "__main__":
    with strict_ids():
        model = build()
    model.export_to_model_xml()
