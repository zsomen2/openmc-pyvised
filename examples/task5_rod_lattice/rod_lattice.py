"""Task 5: a lattice of fuel rods in water, then in a box; k-eff and axial fluxes.

The lattice element (5.1) is a UO2 rod (radius 0.5 cm, height 50 cm, 10 g/cm³, 10 %
enriched uranium) in water (1 g/cm³), in a 2 x 2 x 70 cm unit cell. The lattice fills a
20 x 20 x 70 cm box with vacuum outside (5.2); the box size is a parameter (5.3). The model
calculates k-eff (5.4, 5.5) and the neutron flux in 20 axial sections in three energy
groups (5.6); without water (5.7) the rods stand in void.

The model is laid out like an MCNP deck: title, cell cards, surface cards, data cards.
Above each statement sits the MCNP card it stands for, so the comments add up to the
equivalent MCNP input.

How to use it
-------------
- Builder parameters (pyvised.json): the box width and height (5.3; a box taller than
  70 cm holds more than one layer of rods, so the lattice becomes three-dimensional) and
  the water (on; off = void around the rods, 5.7). The run presets set them per sub-task.
- 5.6: tally 4 is the flux in 20 axial sections of the box in three groups (below 0.5 eV,
  0.5 eV-0.5 MeV, above 0.5 MeV); the Plots tab draws it as an axial profile per group.

Differences from MCNP
---------------------
- An MCNP lat=1 lattice with fill=1 is infinite. An OpenMC lattice is finite, so lattice 2
  is built as large as the box needs: element (0,0,0) is centred on the lattice's origin,
  as in MCNP, and elements are added until they cover the box.
- fill=2 (1 1 0) on the box becomes Cell.translation: the lattice's origin sits at
  (1, 1, 0), so the box walls fall on cell walls and a 20 cm box holds 10 x 10 whole rods.
  A width that is not a multiple of 4 cm cuts the outer rods (5.3).
- imp:n=0 of cell 100 becomes vacuum boundaries on the box surfaces; the outside cell does
  not exist.
- The enrichment (10 % U-235 by weight) is given as atom fractions, as on the m card.
- Thermal scattering: water has the S(a,b) table c_H_in_H2O (MCNP: mt2 lwtr).
- kcode/ksrc: the first source is spread uniformly over the box, kept only in the fuel
  (OpenMC: a fissionable constraint); OpenMC needs no initial k-eff guess.
- The fmesh flux is per source neutron and not divided by the element volume: PyVised's
  normalisation (pyvised.json) divides by box_width² · box_height / 20 cm³.

Running the script writes model.xml. PyVised runs it with PYVISED_EXPORT_ONLY=1 and
passes the builder parameters in PYVISED_PARAMS.
"""

import math

import openmc

from pyvised_course import parameter, strict_ids

BOX_WIDTH = parameter("box_width", 20.0)  # cm, square cross-section
BOX_HEIGHT = parameter("box_height", 70.0)  # cm
WATER = bool(parameter("water", 1))
PITCH = 2.0  # cm, unit cell 2 x 2
CELL_HEIGHT = 70.0  # cm, unit cell height
ENRICHMENT = 0.10  # U-235 weight fraction in uranium
SHIFT = (1.0, 1.0, 0.0)  # cm, where the box puts the lattice's origin (fill=2 (1 1 0))
# elements -N...N along x and y, -K...K along z, so that they cover the (shifted) box
N = max(0, math.ceil((BOX_WIDTH / 2 + SHIFT[0] - PITCH / 2) / PITCH - 1e-9))
K = max(0, math.ceil((BOX_HEIGHT / 2 - CELL_HEIGHT / 2) / CELL_HEIGHT - 1e-9))


# ---------------------------------------------------------------------------
# Fuel rods in a lattice
# c
# c CELLS
# c
# ---------------------------------------------------------------------------


def cell_cards(s, mat):
    c = {}
    u = {}
    # c fuel rod and the water around it (universe 1)
    # 1   1 -10.0  -1 -2 3     u=1  imp:n=1   $ fuel
    c[1] = openmc.Cell(cell_id=1, name="fuel", fill=mat[1], region=-s[1] & -s[2] & +s[3])
    # 2   2 -1.0   #1          u=1  imp:n=1   $ water around the rod     (5.7: 2 0 #1)
    c[2] = openmc.Cell(cell_id=2, name="water" if WATER else "void", region=~c[1].region)
    c[2].fill = mat[2] if WATER else None
    u[1] = openmc.Universe(universe_id=1, name="fuel rod", cells=[c[1], c[2]])
    # c the lattice (universe 2)
    # 10  0  -10 11 -12 13 -14 15  lat=1 fill=1  u=2  imp:n=1   $ unit cell 2 x 2 x 70
    # -> lattice 2: MCNP's infinite lattice, as large as the box needs
    lattice = openmc.RectLattice(lattice_id=2, name="fuel rod lattice")
    lattice.pitch = (PITCH, PITCH, CELL_HEIGHT)
    lattice.lower_left = (
        -(N + 0.5) * PITCH, -(N + 0.5) * PITCH, -(K + 0.5) * CELL_HEIGHT,
    )  # fmt: skip
    lattice.universes = [[[u[1]] * (2 * N + 1)] * (2 * N + 1)] * (2 * K + 1)
    u[2] = lattice
    # c the box (5.2)
    # 20  0  -20 21 -22 23 -24 25  fill=2 (1 1 0)  imp:n=1   $ box 20 x 20 x 70
    # (the lattice's origin at (1, 1, 0): the cell walls fall on even x and y, so the box
    # holds whole unit cells, 10 x 10 of them)
    c[20] = openmc.Cell(cell_id=20, name="box", fill=lattice)
    c[20].translation = SHIFT
    c[20].region = -s[20] & +s[21] & -s[22] & +s[23] & -s[24] & +s[25]
    # 100 0  #20                   imp:n=0   $ outside
    # -> no cell: imp:n=0 makes the box surfaces vacuum boundaries
    return c, u


# ---------------------------------------------------------------------------
# c SURFACES
# c
# ---------------------------------------------------------------------------


def surface_cards():
    s = {}
    # c fuel rod
    # 1   cz   0.5     $ rod radius
    s[1] = openmc.ZCylinder(surface_id=1, r=0.5, name="rod radius")
    # 2   pz  25.0     $ rod top
    s[2] = openmc.ZPlane(surface_id=2, z0=25.0, name="rod top")
    # 3   pz -25.0     $ rod bottom
    s[3] = openmc.ZPlane(surface_id=3, z0=-25.0, name="rod bottom")
    # c unit cell (the planes of lattice element (0,0,0); OpenMC needs only the pitch)
    # 10  px   1.0
    # 11  px  -1.0
    # 12  py   1.0
    # 13  py  -1.0
    # 14  pz  35.0
    # 15  pz -35.0
    # c box                                       (vacuum: cell 100 has imp:n=0)
    # 20  px  10.0
    # 21  px -10.0
    # 22  py  10.0
    # 23  py -10.0
    # 24  pz  35.0
    # 25  pz -35.0
    half_width, half_height = BOX_WIDTH / 2, BOX_HEIGHT / 2
    for j, axis, value in (
        (20, "x", half_width), (21, "x", -half_width),
        (22, "y", half_width), (23, "y", -half_width),
        (24, "z", half_height), (25, "z", -half_height),
    ):  # fmt: skip
        plane = {"x": openmc.XPlane, "y": openmc.YPlane, "z": openmc.ZPlane}[axis]
        s[j] = plane(value, surface_id=j, name=f"box {axis} {value:+g}", boundary_type="vacuum")
    return s


# ---------------------------------------------------------------------------
# c DATA
# c
# ---------------------------------------------------------------------------


def material_cards():
    mat = {}
    # m1   92235 0.10115  $ U-235 (10 % by weight)
    #      92238 0.89885  $ U-238
    #       8016 2        $ O -> UO2 (rho 10.0)
    u235 = ENRICHMENT / 235.0439
    u238 = (1 - ENRICHMENT) / 238.0508
    mat[1] = openmc.Material(material_id=1, name="UO2 10 %")
    mat[1].add_nuclide("U235", u235 / (u235 + u238), "ao")
    mat[1].add_nuclide("U238", u238 / (u235 + u238), "ao")
    mat[1].add_element("O", 2.0, "ao")
    mat[1].set_density("g/cm3", 10.0)
    if WATER:
        # m2   1001 2   8016 1   $ water (rho 1.0)
        # mt2  lwtr
        mat[2] = openmc.Material(material_id=2, name="water")
        mat[2].add_element("H", 2.0, "ao")
        mat[2].add_element("O", 1.0, "ao")
        mat[2].set_density("g/cm3", 1.0)
        mat[2].add_s_alpha_beta("c_H_in_H2O")
    return mat


def source_cards(settings, s):
    # mode n
    settings.run_mode = "eigenvalue"
    # kcode 10000 1.0 20 120   $ neutrons per cycle, initial k, inactive, total cycles
    settings.particles = 10000
    settings.inactive = 20
    settings.batches = 120
    # ksrc ...   -> a uniform first source over the box, kept in the fuel only
    lower = (s[21].x0, s[23].y0, s[25].z0)
    upper = (s[20].x0, s[22].y0, s[24].z0)
    settings.source = openmc.IndependentSource(
        space=openmc.stats.Box(lower, upper), constraints={"fissionable": True}
    )


def tally_cards(s):
    # fmesh4:n geom=xyz origin=-10 -10 -35            $ 20 axial sections of the box (5.6)
    #          imesh 10 iints 1  jmesh 10 jints 1  kmesh 35 kints 20
    #          emesh 0.5e-6 0.5 20                     $ three groups [MeV]
    mesh = openmc.RegularMesh(mesh_id=4, name="20 axial sections")
    mesh.lower_left = (s[21].x0, s[23].y0, s[25].z0)
    mesh.upper_right = (s[20].x0, s[22].y0, s[24].z0)
    mesh.dimension = (1, 1, 20)
    groups = openmc.EnergyFilter([0.0, 0.5, 0.5e6, 20.0e6])  # eV
    t4 = openmc.Tally(tally_id=4, name="axial flux in three groups")
    t4.filters = [openmc.MeshFilter(mesh), groups]
    t4.scores = ["flux"]
    return [t4]


def build():
    s = surface_cards()
    mat = material_cards()
    c, _u = cell_cards(s, mat)

    model = openmc.Model()
    model.geometry = openmc.Geometry([c[20]])
    model.materials = openmc.Materials(mat.values())
    source_cards(model.settings, s)
    model.tallies = openmc.Tallies(tally_cards(s))
    return model


if __name__ == "__main__":
    with strict_ids():
        model = build()
    model.export_to_model_xml()
