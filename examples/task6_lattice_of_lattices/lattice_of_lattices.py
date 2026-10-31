"""Task 6: a 3 x 3 lattice of the Task 5 box, a lattice of lattices.

The element is the finite lattice of Task 5.2: a 20 x 20 x 70 cm box holding 10 x 10 UO2
rods (radius 0.5 cm, height 50 cm) in water. 6.1: that box is a universe, and a regular
3 x 3 lattice of it fills a box fitted to it, 60 x 60 x 70 cm. 6.2: the middle 2 x 2 rods
of every box are removed (their elements hold only water). 6.3: the 3 x 3 lattice of
boxes is the lattice of lattices; the model builds it that way from the start.

The model is laid out like an MCNP deck: title, cell cards, surface cards, data cards.
Above each statement sits the MCNP card it stands for, so the comments add up to the
equivalent MCNP input.

How to use it
-------------
- Builder parameter (pyvised.json): the middle rods (on; off = 6.2). The run presets set it.
- The task is about the geometry: colour by universe (View > Colour by > Universe) and step
  through the levels in the toolbar: 0 = the outer box, 1 = the nine Task 5 boxes, 2 = the
  rods and their water.
- The run calculates k-eff, and tally 4 maps the fission rate rod by rod over all 30 x 30
  positions (Plots: mesh heat map).

Differences from MCNP
---------------------
- lat=1 cells become openmc.RectLattice objects numbered like their universes (2 and 5).
  An OpenMC lattice is finite and gives its elements by position, so lattice 2 lists its
  10 x 10 elements from (-10, -10) on; in MCNP the same comes from fill=-5:4 -5:4 0:0 and
  the box's fill=2 (1 1 0) (see Task 5).
- Universe 4 holds the 20 x 20 box and, like any MCNP universe, the space around it (cell
  21, water; inside lattice 5 it is never reached).
- imp:n=0 of cell 100 becomes vacuum boundaries on the outer box; the outside cell does
  not exist.
- Thermal scattering: water has the S(a,b) table c_H_in_H2O (MCNP: mt2 lwtr).
- kcode/ksrc: the first source is spread uniformly over the box, kept only in the fuel.

Running the script writes model.xml. PyVised runs it with PYVISED_EXPORT_ONLY=1 and
passes the builder parameters in PYVISED_PARAMS.
"""

import openmc

from pyvised_course import parameter, strict_ids

MIDDLE_RODS = bool(parameter("middle_rods", 1))
PITCH = 2.0  # cm, rod unit cell 2 x 2
RODS = 10  # rods per row in a Task 5 box
BOX = RODS * PITCH  # cm, the Task 5 box, 20 x 20
ENRICHMENT = 0.10  # U-235 weight fraction in uranium


# ---------------------------------------------------------------------------
# A 3 x 3 lattice of the Task 5 box
# c
# c CELLS
# c
# ---------------------------------------------------------------------------


def cell_cards(s, mat):
    c = {}
    u = {}
    # c fuel rod and the water around it (universe 1), as in Task 5
    # 1   1 -10.0  -1 -2 3     u=1  imp:n=1   $ fuel
    c[1] = openmc.Cell(cell_id=1, name="fuel", fill=mat[1], region=-s[1] & -s[2] & +s[3])
    # 2   2 -1.0   #1          u=1  imp:n=1   $ water around the rod
    c[2] = openmc.Cell(cell_id=2, name="water", fill=mat[2], region=~c[1].region)
    u[1] = openmc.Universe(universe_id=1, name="fuel rod", cells=[c[1], c[2]])
    # c an element without a rod (universe 3, 6.2)
    # 5   2 -1.0  -1           u=3  imp:n=1   $ water where the rod was
    # 6   2 -1.0   1           u=3  imp:n=1   $ water around it
    # -> one OpenMC cell without a region: it fills the whole universe
    c[5] = openmc.Cell(cell_id=5, name="water only", fill=mat[2])
    u[3] = openmc.Universe(universe_id=3, name="no rod", cells=[c[5]])
    # c the 10 x 10 rods of Task 5 (universe 2)
    # 10  0  -10 11 -12 13  lat=1 fill=-5:4 -5:4 0:0
    #        1 1 1 1 1 1 1 1 1 1
    #        ...                     (6.2: the middle 2 x 2 entries are 3)
    #        1 1 1 1 1 1 1 1 1 1     u=2  imp:n=1
    middle = (RODS // 2 - 1, RODS // 2)
    u[2] = openmc.RectLattice(lattice_id=2, name="10 x 10 rods")
    u[2].pitch = (PITCH, PITCH)
    u[2].lower_left = (-BOX / 2, -BOX / 2)
    u[2].universes = [
        [u[3] if not MIDDLE_RODS and i in middle and j in middle else u[1] for i in range(RODS)]
        for j in range(RODS)
    ]
    # c the Task 5 box as a universe (4)
    # 20  0  -20 21 -22 23 -24 25  fill=2 (1 1 0)  u=4  imp:n=1   $ 20 x 20 x 70 box
    c[20] = openmc.Cell(cell_id=20, name="Task 5 box", fill=u[2])
    c[20].region = -s[20] & +s[21] & -s[22] & +s[23] & -s[24] & +s[25]
    # 21  2 -1.0  #20                               u=4  imp:n=1   $ around it (never reached)
    c[21] = openmc.Cell(cell_id=21, name="around the box", fill=mat[2], region=~c[20].region)
    u[4] = openmc.Universe(universe_id=4, name="Task 5 box", cells=[c[20], c[21]])
    # c the lattice of lattices (universe 5): 3 x 3 boxes
    # 30  0  -20 21 -22 23  lat=1 fill=-1:1 -1:1 0:0  4 8r   u=5  imp:n=1
    u[5] = openmc.RectLattice(lattice_id=5, name="3 x 3 boxes")
    u[5].pitch = (BOX, BOX)
    u[5].lower_left = (-1.5 * BOX, -1.5 * BOX)
    u[5].universes = [[u[4]] * 3 for _ in range(3)]
    # c the box fitted to it
    # 40  0  -40 41 -42 43 -24 25  fill=5  imp:n=1   $ 60 x 60 x 70 box
    c[40] = openmc.Cell(cell_id=40, name="lattice of lattices", fill=u[5])
    c[40].region = -s[40] & +s[41] & -s[42] & +s[43] & -s[24] & +s[25]
    # 100 0  #40                  imp:n=0   $ outside
    # -> no cell: imp:n=0 makes the outer box surfaces vacuum boundaries
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
    s[2] = openmc.ZPlane(25.0, surface_id=2, name="rod top")
    # 3   pz -25.0     $ rod bottom
    s[3] = openmc.ZPlane(-25.0, surface_id=3, name="rod bottom")
    # c rod unit cell (the planes of lattice element (0,0); OpenMC needs only the pitch)
    # 10  px   1.0
    # 11  px  -1.0
    # 12  py   1.0
    # 13  py  -1.0
    # c the Task 5 box (also the planes of lattice 5's element (0,0))
    # 20  px  10.0
    s[20] = openmc.XPlane(BOX / 2, surface_id=20, name="Task 5 box +x")
    # 21  px -10.0
    s[21] = openmc.XPlane(-BOX / 2, surface_id=21, name="Task 5 box -x")
    # 22  py  10.0
    s[22] = openmc.YPlane(BOX / 2, surface_id=22, name="Task 5 box +y")
    # 23  py -10.0
    s[23] = openmc.YPlane(-BOX / 2, surface_id=23, name="Task 5 box -y")
    # 24  pz  35.0                                 (vacuum: cell 100 has imp:n=0)
    s[24] = openmc.ZPlane(35.0, surface_id=24, name="top", boundary_type="vacuum")
    # 25  pz -35.0                                 (vacuum: cell 100 has imp:n=0)
    s[25] = openmc.ZPlane(-35.0, surface_id=25, name="bottom", boundary_type="vacuum")
    # c the outer box                              (vacuum: cell 100 has imp:n=0)
    # 40  px  30.0
    s[40] = openmc.XPlane(1.5 * BOX, surface_id=40, name="outer +x", boundary_type="vacuum")
    # 41  px -30.0
    s[41] = openmc.XPlane(-1.5 * BOX, surface_id=41, name="outer -x", boundary_type="vacuum")
    # 42  py  30.0
    s[42] = openmc.YPlane(1.5 * BOX, surface_id=42, name="outer +y", boundary_type="vacuum")
    # 43  py -30.0
    s[43] = openmc.YPlane(-1.5 * BOX, surface_id=43, name="outer -y", boundary_type="vacuum")
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
    # kcode 10000 1.0 20 80   $ neutrons per cycle, initial k, inactive, total cycles
    settings.particles = 10000
    settings.inactive = 20
    settings.batches = 80
    # ksrc ...   -> a uniform first source over the outer box, kept in the fuel only
    settings.source = openmc.IndependentSource(
        space=openmc.stats.Box((s[41].x0, s[43].y0, -25.0), (s[40].x0, s[42].y0, 25.0)),
        constraints={"fissionable": True},
    )


def tally_cards(s):
    # fmesh4:n geom=xyz origin=-30 -30 -35   $ one mesh element per rod position
    #          imesh 30 iints 30  jmesh 30 jints 30  kmesh 35 kints 1
    # fm4 -1 1 -6                           $ fission rate (material 1, MT -6)
    mesh = openmc.RegularMesh(mesh_id=4, name="one element per rod")
    mesh.lower_left = (s[41].x0, s[43].y0, s[25].z0)
    mesh.upper_right = (s[40].x0, s[42].y0, s[24].z0)
    mesh.dimension = (3 * RODS, 3 * RODS, 1)
    t4 = openmc.Tally(tally_id=4, name="fission rate per rod")
    t4.filters = [openmc.MeshFilter(mesh)]
    t4.scores = ["fission"]
    return [t4]


def build():
    s = surface_cards()
    mat = material_cards()
    c, _u = cell_cards(s, mat)

    model = openmc.Model()
    model.geometry = openmc.Geometry([c[40]])
    model.materials = openmc.Materials(mat.values())
    source_cards(model.settings, s)
    model.tallies = openmc.Tallies(tally_cards(s))
    return model


if __name__ == "__main__":
    with strict_ids():
        model = build()
    model.export_to_model_xml()
