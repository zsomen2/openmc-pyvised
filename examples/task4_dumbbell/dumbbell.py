"""Task 4: a double dumbbell for practising geometry.

Two dumbbells cross at the centre. The vertical one is a water cylinder with an iron
sphere at each end, the horizontal one an iron rod through the water cylinder with a lead
sphere at each end. Where a sphere meets its rod, one of them cuts into the other: the
upper iron sphere is cut by the water cylinder, the lower one cuts into the cylinder; the
left lead sphere is cut by the iron rod, the right one cuts into the rod. A 1 MeV point
photon source sits at the centre.

The model is laid out like an MCNP deck: title, cell cards, surface cards, data cards.
Above each statement sits the MCNP card it stands for, so the comments add up to the
equivalent MCNP input. The shape follows the double dumbbell built in class.

How to use it
-------------
- Every size is a builder parameter (pyvised.json): the radius of each rod, the distance of
  its spheres from the centre (= its half length), the sphere radius, and the radius of
  the enclosing sphere. Change them and look at the views: the overlap count above each
  view shows when two cells claim the same space (e.g. lead spheres so close to the centre
  that they reach into the water cylinder).
- The geometry is the point of the task; the run adds a photon flux map in the xz plane
  (tally 4) to show the shadows of the iron and the lead.

Differences from MCNP
---------------------
- MCNP's #n (everything outside cell n) is ~c[n].region in OpenMC; (a b) & ~(c) follows
  the card's order.
- imp:p=0 of cell 100 becomes a vacuum boundary on sphere 30; the outside cell does not
  exist.
- fmesh4 becomes a mesh tally. OpenMC does not divide it by the mesh element volume:
  PyVised's normalisation (pyvised.json) multiplies by 1e6 photons/s and divides by the
  element volume (world_radius/30)² · 2 cm³.

Running the script writes model.xml. PyVised runs it with PYVISED_EXPORT_ONLY=1 and
passes the builder parameters in PYVISED_PARAMS.
"""

import openmc

from pyvised_course import parameter, strict_ids

V_RADIUS = parameter("vertical_radius", 5.0)  # cm, water cylinder
V_DISTANCE = parameter("vertical_distance", 20.0)  # cm, centre -> iron spheres
V_SPHERE = parameter("vertical_sphere", 7.0)  # cm, iron sphere radius
H_RADIUS = parameter("horizontal_radius", 2.0)  # cm, iron rod
H_DISTANCE = parameter("horizontal_distance", 10.0)  # cm, centre -> lead spheres
H_SPHERE = parameter("horizontal_sphere", 3.0)  # cm, lead sphere radius
WORLD = parameter("world_radius", 30.0)  # cm, enclosing sphere
MESH = 60  # mesh elements along x and z

if V_DISTANCE + V_SPHERE > WORLD or H_DISTANCE + H_SPHERE > WORLD:
    raise ValueError(
        f"The enclosing sphere (radius {WORLD:g} cm) must hold both dumbbells: make it at "
        f"least {max(V_DISTANCE + V_SPHERE, H_DISTANCE + H_SPHERE):g} cm."
    )


# ---------------------------------------------------------------------------
# Double dumbbell
# c
# c CELLS
# c
# ---------------------------------------------------------------------------


def cell_cards(s, mat):
    c = {}
    # c vertical dumbbell
    # 10  1 -1.0   -10 -11 12 16 20  imp:p=1   $ water cylinder
    c[10] = openmc.Cell(cell_id=10, name="water cylinder", fill=mat[1])
    c[10].region = -s[10] & -s[11] & +s[12] & +s[16] & +s[20]
    # 11  2 -7.87  -16               imp:p=1   $ lower iron sphere (cuts into the cylinder)
    c[11] = openmc.Cell(cell_id=11, name="lower iron sphere", fill=mat[2], region=-s[16])
    # 12  2 -7.87  -15 #10           imp:p=1   $ upper iron sphere (cut by the cylinder)
    c[12] = openmc.Cell(cell_id=12, name="upper iron sphere", fill=mat[2])
    c[12].region = -s[15] & ~c[10].region
    # c horizontal dumbbell
    # 20  2 -7.87  -20 -21 22 25     imp:p=1   $ iron rod along x
    c[20] = openmc.Cell(cell_id=20, name="iron rod", fill=mat[2])
    c[20].region = -s[20] & -s[21] & +s[22] & +s[25]
    # 21  3 -11.35 -26 #20           imp:p=1   $ left lead sphere (cut by the rod)
    c[21] = openmc.Cell(cell_id=21, name="left lead sphere", fill=mat[3])
    c[21].region = -s[26] & ~c[20].region
    # 22  3 -11.35 -25               imp:p=1   $ right lead sphere (cuts into the rod)
    c[22] = openmc.Cell(cell_id=22, name="right lead sphere", fill=mat[3], region=-s[25])
    # c enclosing sphere
    # 30  0  -30 #10 #11 #12 #20 #21 #22  imp:p=1   $ void around the dumbbells
    c[30] = openmc.Cell(cell_id=30, name="void around the dumbbells")
    c[30].region = -s[30]
    for n in (10, 11, 12, 20, 21, 22):
        c[30].region &= ~c[n].region
    # 100 0   30                      imp:p=0   $ outside
    # -> no cell: imp:p=0 makes surface 30 a vacuum boundary
    return c


# ---------------------------------------------------------------------------
# c SURFACES
# c
# ---------------------------------------------------------------------------


def surface_cards():
    s = {}
    # c vertical dumbbell
    # 10  cz   5.0        $ water cylinder
    s[10] = openmc.ZCylinder(surface_id=10, r=V_RADIUS, name="water cylinder")
    # 11  pz  20.0        $ cylinder top (inside the upper sphere)
    s[11] = openmc.ZPlane(surface_id=11, z0=V_DISTANCE, name="cylinder top")
    # 12  pz -20.0        $ cylinder bottom (inside the lower sphere)
    s[12] = openmc.ZPlane(surface_id=12, z0=-V_DISTANCE, name="cylinder bottom")
    # 15  sz  20.0 7.0    $ upper iron sphere
    s[15] = openmc.Sphere(surface_id=15, z0=V_DISTANCE, r=V_SPHERE, name="upper iron sphere")
    # 16  sz -20.0 7.0    $ lower iron sphere
    s[16] = openmc.Sphere(surface_id=16, z0=-V_DISTANCE, r=V_SPHERE, name="lower iron sphere")
    # c horizontal dumbbell
    # 20  cx   2.0        $ iron rod
    s[20] = openmc.XCylinder(surface_id=20, r=H_RADIUS, name="iron rod")
    # 21  px  10.0        $ rod end (inside the right sphere)
    s[21] = openmc.XPlane(surface_id=21, x0=H_DISTANCE, name="rod end right")
    # 22  px -10.0        $ rod end (inside the left sphere)
    s[22] = openmc.XPlane(surface_id=22, x0=-H_DISTANCE, name="rod end left")
    # 25  sx  10.0 3.0    $ right lead sphere
    s[25] = openmc.Sphere(surface_id=25, x0=H_DISTANCE, r=H_SPHERE, name="right lead sphere")
    # 26  sx -10.0 3.0    $ left lead sphere
    s[26] = openmc.Sphere(surface_id=26, x0=-H_DISTANCE, r=H_SPHERE, name="left lead sphere")
    # c enclosing sphere
    # 30  so  30.0        $ enclosing sphere           (vacuum: cell 100 has imp:p=0)
    s[30] = openmc.Sphere(surface_id=30, r=WORLD, name="enclosing sphere", boundary_type="vacuum")
    return s


# ---------------------------------------------------------------------------
# c DATA
# c
# ---------------------------------------------------------------------------


def material_cards():
    mat = {}
    # m1   8000 1  1000 2   $ water (rho 1.0)
    mat[1] = openmc.Material(material_id=1, name="water")
    mat[1].add_element("O", 1.0, "ao")
    mat[1].add_element("H", 2.0, "ao")
    mat[1].set_density("g/cm3", 1.0)
    # m2   26000 1          $ iron (rho 7.87)
    mat[2] = openmc.Material(material_id=2, name="iron")
    mat[2].add_element("Fe", 1.0, "ao")
    mat[2].set_density("g/cm3", 7.87)
    # m3   82000 1          $ lead (rho 11.35)
    mat[3] = openmc.Material(material_id=3, name="lead")
    mat[3].add_element("Pb", 1.0, "ao")
    mat[3].set_density("g/cm3", 11.35)
    return mat


def source_cards(settings):
    # mode p
    settings.photon_transport = True
    settings.run_mode = "fixed source"
    # sdef pos=0 0 0  erg=1.0   $ isotropic point source at the centre [MeV]
    settings.source = openmc.IndependentSource(
        space=openmc.stats.Point((0.0, 0.0, 0.0)),
        energy=openmc.stats.Discrete([1.0e6], [1.0]),  # MeV -> eV
        particle="photon",
    )
    # nps 1e6   -> 100 batches of 10000 photons
    settings.particles = 10000
    settings.batches = 100


def tally_cards():
    # fmesh4:p geom=xyz origin=-30 -1 -30   $ photon flux in a 2 cm slab around y = 0
    #          imesh 30 iints 60  jmesh 1 jints 1  kmesh 30 kints 60
    # fm4 1e6   $ source strength 1e6 photons/s   -> pyvised.json, which divides by the volume
    mesh = openmc.RegularMesh(mesh_id=4, name="xz plane")
    mesh.lower_left = (-WORLD, -1.0, -WORLD)
    mesh.upper_right = (WORLD, 1.0, WORLD)
    mesh.dimension = (MESH, 1, MESH)
    t4 = openmc.Tally(tally_id=4, name="photon flux in the xz plane")
    t4.filters = [openmc.MeshFilter(mesh), openmc.ParticleFilter(["photon"])]
    t4.scores = ["flux"]
    return [t4]


def build():
    s = surface_cards()
    mat = material_cards()
    c = cell_cards(s, mat)

    model = openmc.Model()
    model.geometry = openmc.Geometry(list(c.values()))
    model.materials = openmc.Materials(mat.values())
    source_cards(model.settings)
    model.tallies = openmc.Tallies(tally_cards())
    return model


if __name__ == "__main__":
    with strict_ids():
        model = build()
    model.export_to_model_xml()
