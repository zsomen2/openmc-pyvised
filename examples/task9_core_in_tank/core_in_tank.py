"""Task 9: a 5 x 5 core of 3 x 3 fuel assemblies in a water tank; k-eff and the fluxes on the
tank wall.

The rod is the one of Task 5 (UO2, radius 0.5 cm, height 50 cm, 10 g/cm³) at the optimal
pitch found in Task 7 (1.8 cm). Nine rods form a fuel assembly (a 5.4 cm box), 5 x 5
assemblies the core (a 27 x 27 x 70 cm box), and the core sits in the middle of a water
tank 100 cm across and 100 cm tall. 9.1: k-eff, adjusting the enrichment if k-eff is more
than 2 % off 1.00. 9.2: the neutron and the gamma flux on the tank's lateral surface and
their uncertainty after at most 10 minutes. 9.3: the water outside the core box in 5 cm
annuli whose importance doubles outward.

The model is laid out like an MCNP deck: title, cell cards, surface cards, data cards.
Above each statement sits the MCNP card it stands for, so the comments add up to the
equivalent MCNP input.

How to use it
-------------
- Builder parameters (pyvised.json): the uranium enrichment (9.1) and the importance ratio
  of neighbouring water annuli (1 = none; 2 doubles them, 9.3).
- The run presets "9.2" and "9.3" run for 10 minutes (the time limit stops the run and
  keeps its last statepoint); compare the relative errors and the figures of merit of
  tallies 2 and 12 with and without importances.

Differences from MCNP
---------------------
- lat=1 cells become openmc.RectLattice objects numbered like their universes (2 and 4);
  universe 3 holds the assembly box and the water around it (never reached in lattice 4).
- imp:n,p=0 of cell 100 becomes vacuum boundaries on the tank surfaces; the outside cell
  does not exist.
- mode n p: photon transport is on (OpenMC: settings.photon_transport).
- f2 becomes surface-flux tallies (neutrons: tally 2, photons: tally 12). OpenMC gives them
  per source neutron and does not divide by the area: PyVised's normalisation
  (pyvised.json) multiplies by 7.6e15 neutrons/s, a 100 kW core (P · nu / (200 MeV · k) with
  nu = 2.43, k = 1), and divides by the area 2·pi·50·100 cm².
- imp:n,p of the annuli becomes weight windows for neutrons and photons on a cylindrical
  mesh along the annuli: lower bound 0.5/imp, upper/lower ratio 5, survival ratio 3 (MCNP's
  WWG/WWP defaults), checked at surface crossings as well as at collisions (MCNP splits at
  cell boundaries). With ratio 1 the model has none.
- Thermal scattering: water has the S(a,b) table c_H_in_H2O (MCNP: mt2 lwtr).
- kcode/ksrc: the first source is spread uniformly over the core box, kept in the fuel.

Running the script writes model.xml. PyVised runs it with PYVISED_EXPORT_ONLY=1 and
passes the builder parameters in PYVISED_PARAMS.
"""

import openmc

from pyvised_course import parameter, strict_ids, weight_windows

ENRICHMENT = parameter("enrichment", 10.0) / 100  # U-235 weight fraction in uranium
RATIO = parameter("importance_ratio", 1.0)  # importance of an annulus / the one inside it
PITCH = 1.8  # cm, the optimal pitch of Task 7
ASSEMBLY = 3 * PITCH  # cm, a 3 x 3 fuel assembly
CORE = 5 * ASSEMBLY  # cm, the 5 x 5 core, 27 cm
TANK_RADIUS = 50.0  # cm
TANK_HALF_HEIGHT = 50.0  # cm
ANNULI = [20.0, 25.0, 30.0, 35.0, 40.0, 45.0, TANK_RADIUS]  # cm, outer radii (9.3)


# ---------------------------------------------------------------------------
# Core of fuel assemblies in a water tank
# c
# c CELLS
# c
# ---------------------------------------------------------------------------


def cell_cards(s, mat):
    c = {}
    u = {}
    imp = {}
    # c fuel rod and the water around it (universe 1), as in Task 5
    # 1   1 -10.0  -1 -2 3     u=1  imp:n,p=1   $ fuel
    c[1] = openmc.Cell(cell_id=1, name="fuel", fill=mat[1], region=-s[1] & -s[2] & +s[3])
    # 2   2 -1.0   #1          u=1  imp:n,p=1   $ water around the rod
    c[2] = openmc.Cell(cell_id=2, name="water", fill=mat[2], region=~c[1].region)
    u[1] = openmc.Universe(universe_id=1, name="fuel rod", cells=[c[1], c[2]])
    # c fuel assembly: 3 x 3 rods (universe 2)
    # 5   0  -5 6 -7 8  lat=1 fill=-1:1 -1:1 0:0  1 8r   u=2  imp:n,p=1
    u[2] = openmc.RectLattice(lattice_id=2, name="3 x 3 rods")
    u[2].pitch = (PITCH, PITCH)
    u[2].lower_left = (-ASSEMBLY / 2, -ASSEMBLY / 2)
    u[2].universes = [[u[1]] * 3 for _ in range(3)]
    # 10  0  -10 11 -12 13  fill=2            u=3  imp:n,p=1   $ assembly box
    c[10] = openmc.Cell(cell_id=10, name="assembly box", fill=u[2])
    c[10].region = -s[10] & +s[11] & -s[12] & +s[13]
    # 11  2 -1.0  #10                          u=3  imp:n,p=1   $ around it (never reached)
    c[11] = openmc.Cell(cell_id=11, name="around the assembly", fill=mat[2])
    c[11].region = ~c[10].region
    u[3] = openmc.Universe(universe_id=3, name="fuel assembly", cells=[c[10], c[11]])
    # c the core: 5 x 5 assemblies (universe 4)
    # 15  0  -10 11 -12 13  lat=1 fill=-2:2 -2:2 0:0  3 24r   u=4  imp:n,p=1
    u[4] = openmc.RectLattice(lattice_id=4, name="5 x 5 assemblies")
    u[4].pitch = (ASSEMBLY, ASSEMBLY)
    u[4].lower_left = (-CORE / 2, -CORE / 2)
    u[4].universes = [[u[3]] * 5 for _ in range(5)]
    # 20  0  -20 21 -22 23 -24 25  fill=4  imp:n,p=1   $ core box 27 x 27 x 70
    c[20] = openmc.Cell(cell_id=20, name="core box", fill=u[4])
    c[20].region = -s[20] & +s[21] & -s[22] & +s[23] & -s[24] & +s[25]
    imp[20] = 1.0
    # c the water tank
    # 30  2 -1.0  -30 -51 52 #20   imp:n,p=1     $ water up to r = 20 cm
    c[30] = openmc.Cell(cell_id=30, name="water r < 20", fill=mat[2])
    c[30].region = -s[30] & -s[51] & +s[52] & ~c[20].region
    imp[30] = 1.0
    # 31  2 -1.0  30 -31 -51 52    imp:n,p=2     $ annulus 20-25 cm (importance ratio 2)
    # ...                                          9.3: one annulus per 5 cm, out to the wall
    # 36  2 -1.0  35 -50 -51 52    imp:n,p=64    $ annulus 45-50 cm
    for k, j in enumerate(range(31, 31 + len(ANNULI) - 1), start=1):
        inner, outer = s[29 + k], s[30 + k] if k < len(ANNULI) - 1 else s[50]
        name = f"water {ANNULI[k - 1]:g}-{ANNULI[k]:g} cm"
        c[j] = openmc.Cell(cell_id=j, name=name, fill=mat[2])
        c[j].region = +inner & -outer & -s[51] & +s[52]
        imp[j] = RATIO**k
    # 100 0  50:51:-52             imp:n,p=0     $ outside
    # -> no cell: imp:n,p=0 makes the tank surfaces vacuum boundaries
    root = [c[20], c[30]] + [c[j] for j in range(31, 31 + len(ANNULI) - 1)]
    return c, u, imp, root


# ---------------------------------------------------------------------------
# c SURFACES
# c
# ---------------------------------------------------------------------------


def surface_cards():
    s = {}
    # c fuel rod
    # 1   cz   0.5      $ rod radius
    s[1] = openmc.ZCylinder(surface_id=1, r=0.5, name="rod radius")
    # 2   pz  25.0      $ rod top
    s[2] = openmc.ZPlane(25.0, surface_id=2, name="rod top")
    # 3   pz -25.0      $ rod bottom
    s[3] = openmc.ZPlane(-25.0, surface_id=3, name="rod bottom")
    # c rod unit cell, the planes of lattice 2's element (0,0); OpenMC needs only the pitch
    # 5   px   0.9
    # 6   px  -0.9
    # 7   py   0.9
    # 8   py  -0.9
    # c assembly box (also the planes of lattice 4's element (0,0))
    # 10  px   2.7
    s[10] = openmc.XPlane(ASSEMBLY / 2, surface_id=10, name="assembly +x")
    # 11  px  -2.7
    s[11] = openmc.XPlane(-ASSEMBLY / 2, surface_id=11, name="assembly -x")
    # 12  py   2.7
    s[12] = openmc.YPlane(ASSEMBLY / 2, surface_id=12, name="assembly +y")
    # 13  py  -2.7
    s[13] = openmc.YPlane(-ASSEMBLY / 2, surface_id=13, name="assembly -y")
    # c core box
    # 20  px  13.5
    s[20] = openmc.XPlane(CORE / 2, surface_id=20, name="core +x")
    # 21  px -13.5
    s[21] = openmc.XPlane(-CORE / 2, surface_id=21, name="core -x")
    # 22  py  13.5
    s[22] = openmc.YPlane(CORE / 2, surface_id=22, name="core +y")
    # 23  py -13.5
    s[23] = openmc.YPlane(-CORE / 2, surface_id=23, name="core -y")
    # 24  pz  35.0
    s[24] = openmc.ZPlane(35.0, surface_id=24, name="core top")
    # 25  pz -35.0
    s[25] = openmc.ZPlane(-35.0, surface_id=25, name="core bottom")
    # c water annuli (9.3)
    # 30  cz  20.0
    # 31  cz  25.0
    # ...
    # 35  cz  45.0
    for k, radius in enumerate(ANNULI[:-1]):
        s[30 + k] = openmc.ZCylinder(surface_id=30 + k, r=radius, name=f"annulus r = {radius:g}")
    # c the tank                                   (vacuum: cell 100 has imp:n,p=0)
    # 50  cz  50.0      $ tank wall
    s[50] = openmc.ZCylinder(surface_id=50, r=TANK_RADIUS, name="tank wall", boundary_type="vacuum")
    # 51  pz  50.0      $ tank top
    s[51] = openmc.ZPlane(TANK_HALF_HEIGHT, surface_id=51, name="tank top", boundary_type="vacuum")
    # 52  pz -50.0      $ tank bottom
    s[52] = openmc.ZPlane(
        -TANK_HALF_HEIGHT, surface_id=52, name="tank bottom", boundary_type="vacuum"
    )
    return s


# ---------------------------------------------------------------------------
# c DATA
# c
# ---------------------------------------------------------------------------


def material_cards():
    mat = {}
    # m1   92235 0.10115  $ U-235 (10 % by weight; 9.1: the enrichment parameter)
    #      92238 0.89885  $ U-238
    #       8016 2        $ O -> UO2 (rho 10.0)
    u235 = ENRICHMENT / 235.0439
    u238 = (1 - ENRICHMENT) / 238.0508
    mat[1] = openmc.Material(material_id=1, name=f"UO2 {100 * ENRICHMENT:g} %")
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
    # mode n p
    settings.photon_transport = True
    settings.run_mode = "eigenvalue"
    # kcode 10000 1.0 20 100   $ neutrons per cycle, initial k, inactive, total cycles
    settings.particles = 10000
    settings.inactive = 20
    settings.batches = 100
    # ksrc ...   -> a uniform first source over the core box, kept in the fuel only
    settings.source = openmc.IndependentSource(
        space=openmc.stats.Box((s[21].x0, s[23].y0, -25.0), (s[20].x0, s[22].y0, 25.0)),
        constraints={"fissionable": True},
    )


def tally_cards(s):
    wall = openmc.SurfaceFilter([s[50]])
    # f2:n 50   $ neutron flux on the tank wall
    # fm2 7.6e15   $ neutrons/s of 100 kW   -> pyvised.json, which divides by the area too
    t2 = openmc.Tally(tally_id=2, name="neutron flux on the tank wall")
    t2.filters = [wall, openmc.ParticleFilter(["neutron"])]
    t2.scores = ["flux"]
    # f12:p 50  $ gamma flux on the tank wall
    # fm12 7.6e15
    t12 = openmc.Tally(tally_id=12, name="gamma flux on the tank wall")
    t12.filters = [wall, openmc.ParticleFilter(["photon"])]
    t12.scores = ["flux"]
    return [t2, t12]


def importance_cards(s, imp):
    """imp:n,p of the water cells -> weight windows on a mesh of the annuli."""
    annuli = openmc.CylindricalMesh(
        r_grid=[0.0, *ANNULI], z_grid=[s[52].z0, s[51].z0], mesh_id=1, name="water annuli"
    )
    # the first ring holds the core box and the water around it (both imp=1)
    importances = [imp[30]] + [imp[j] for j in range(31, 31 + len(ANNULI) - 1)]
    return [
        weight_windows(annuli, importances, particle="neutron"),
        weight_windows(annuli, importances, particle="photon"),
    ]


def build():
    s = surface_cards()
    mat = material_cards()
    _c, _u, imp, root = cell_cards(s, mat)

    model = openmc.Model()
    model.geometry = openmc.Geometry(root)
    model.materials = openmc.Materials(mat.values())
    source_cards(model.settings, s)
    model.tallies = openmc.Tallies(tally_cards(s))
    if RATIO > 1:
        model.settings.weight_windows = importance_cards(s, imp)
        model.settings.weight_windows_on = True
        # MCNP splits and plays roulette where a particle crosses into a cell of other
        # importance; OpenMC checks weight windows at collisions only unless asked to at
        # surfaces too
        model.settings.weight_window_checkpoints = {"collision": True, "surface": True}
    return model


if __name__ == "__main__":
    with strict_ids():
        model = build()
    model.export_to_model_xml()
