"""Task 7: the rod's unit cell with reflective walls; optimal pitch, a critical lattice,
water density and boron.

The rod is the one of Task 5 (UO2, radius 0.5 cm, height 50 cm, 10 g/cm³, 10 % enriched)
in water. 7.1-7.2: one rod in a 2 x 2 x 70 cm box whose vertical walls reflect, so the
lattice is infinite sideways (top and bottom stay vacuum). 7.3-7.4: k-eff against the
lattice pitch, and its optimum. 7.5: a finite lattice of N x N rods with that pitch, the box
sized so that k-eff = 1.00 ± 1 %. 7.6-7.7: k-eff against the water density (where does the
lattice become undermoderated?). 7.8: the same with boron in the water.

The model is laid out like an MCNP deck: title, cell cards, surface cards, data cards.
Above each statement sits the MCNP card it stands for, so the comments add up to the
equivalent MCNP input.

How to use it
-------------
- Builder parameters (pyvised.json): the pitch, the finite lattice (off = the reflective
  unit cell; on = N x N rods in a box of N·pitch with vacuum around), the rods per side N,
  the water density and the boron concentration (ppm by weight, natural boron).
- Run one value after the other and compare the runs in the history with k-eff against the
  parameter (e.g. "pitch" for 7.3, "water_density" for 7.6).
- Tally 4 is the flux in the fuel and in the water in three groups (below 0.5 eV, 0.5 eV to
  0.5 MeV, above 0.5 MeV): it shows how the moderation changes.

Differences from MCNP
---------------------
- *px/*py (reflecting surfaces) become boundary_type="reflective"; imp:n=0 outside becomes
  vacuum boundaries; the outside cell does not exist.
- The finite lattice is an openmc.RectLattice of N x N rods (MCNP: lat=1 with
  fill=0:N-1 0:N-1 0:0 in a box of N·pitch).
- Borated water is given by weight fractions: water scaled by 1 - ppm·1e-6 plus natural
  boron (MCNP: m2 1001 -... 8016 -... 5010 -... 5011 -...). Thermal scattering: water has
  the S(a,b) table c_H_in_H2O (MCNP: mt2 lwtr).
- kcode/ksrc: the first source is spread uniformly over the box, kept only in the fuel.

Running the script writes model.xml. PyVised runs it with PYVISED_EXPORT_ONLY=1 and
passes the builder parameters in PYVISED_PARAMS.
"""

import openmc

from pyvised_course import parameter, strict_ids

PITCH = parameter("pitch", 2.0)  # cm, unit cell width
FINITE = bool(parameter("finite", 0))
RODS = int(parameter("rods", 10))  # rods per side of the finite lattice
WATER_DENSITY = parameter("water_density", 1.0)  # g/cm³
BORON_PPM = parameter("boron_ppm", 0.0)  # ppm by weight in the water
ENRICHMENT = 0.10  # U-235 weight fraction in uranium
H_WEIGHT = 2 * 1.00794 / (2 * 1.00794 + 15.9994)  # weight fraction of H in water

if PITCH <= 1.0:
    raise ValueError(f"The pitch ({PITCH:g} cm) must be larger than the rod diameter (1 cm).")


# ---------------------------------------------------------------------------
# Unit cell with reflective walls
# c
# c CELLS
# c
# ---------------------------------------------------------------------------


def cell_cards(s, mat):
    c = {}
    # 1   1 -10.0  -1 -2 3     imp:n=1   $ fuel                              (7.5: u=1)
    c[1] = openmc.Cell(cell_id=1, name="fuel", fill=mat[1], region=-s[1] & -s[2] & +s[3])
    # 2   2 -1.0   #1 -10 11 -12 13 -14 15  imp:n=1   $ water        (7.5: 2 2 -1.0 #1 u=1)
    c[2] = openmc.Cell(cell_id=2, name="water", fill=mat[2])
    if not FINITE:
        c[2].region = ~c[1].region & -s[10] & +s[11] & -s[12] & +s[13] & -s[14] & +s[15]
        # 100 0  #1 #2              imp:n=0   $ outside
        # -> no cell: the reflective walls and the vacuum top and bottom bound the model
        return c, [c[1], c[2]]
    # c 7.5: a finite lattice of N x N rods
    c[2].region = ~c[1].region
    rod = openmc.Universe(universe_id=1, name="fuel rod", cells=[c[1], c[2]])
    # 10  0  -20 21 -22 23  lat=1 fill=0:9 0:9 0:0  1 99r   u=2  imp:n=1
    lattice = openmc.RectLattice(lattice_id=2, name=f"{RODS} x {RODS} rods")
    lattice.pitch = (PITCH, PITCH)
    lattice.lower_left = (-RODS * PITCH / 2, -RODS * PITCH / 2)
    lattice.universes = [[rod] * RODS for _ in range(RODS)]
    # 20  0  -30 31 -32 33 -14 15  fill=2  imp:n=1   $ the box, N·pitch wide
    c[20] = openmc.Cell(cell_id=20, name="finite lattice", fill=lattice)
    c[20].region = -s[30] & +s[31] & -s[32] & +s[33] & -s[14] & +s[15]
    # 100 0  #20                   imp:n=0   $ outside
    # -> no cell: imp:n=0 makes the box surfaces vacuum boundaries
    return c, [c[20]]


# ---------------------------------------------------------------------------
# c SURFACES
# c
# ---------------------------------------------------------------------------


def surface_cards():
    s = {}
    # c fuel rod
    # 1    cz   0.5     $ rod radius
    s[1] = openmc.ZCylinder(surface_id=1, r=0.5, name="rod radius")
    # 2    pz  25.0     $ rod top
    s[2] = openmc.ZPlane(25.0, surface_id=2, name="rod top")
    # 3    pz -25.0     $ rod bottom
    s[3] = openmc.ZPlane(-25.0, surface_id=3, name="rod bottom")
    # c the box: 2 x 2 x 70, reflective vertical walls (7.2)
    # *10  px   1.0     $ (7.5: the planes of the lattice element)
    # *11  px  -1.0
    # *12  py   1.0
    # *13  py  -1.0
    walls = "transmission" if FINITE else "reflective"
    half = PITCH / 2
    s[10] = openmc.XPlane(half, surface_id=10, name="cell +x", boundary_type=walls)
    s[11] = openmc.XPlane(-half, surface_id=11, name="cell -x", boundary_type=walls)
    s[12] = openmc.YPlane(half, surface_id=12, name="cell +y", boundary_type=walls)
    s[13] = openmc.YPlane(-half, surface_id=13, name="cell -y", boundary_type=walls)
    # 14   pz  35.0     $ top                      (vacuum: cell 100 has imp:n=0)
    s[14] = openmc.ZPlane(35.0, surface_id=14, name="top", boundary_type="vacuum")
    # 15   pz -35.0     $ bottom                   (vacuum: cell 100 has imp:n=0)
    s[15] = openmc.ZPlane(-35.0, surface_id=15, name="bottom", boundary_type="vacuum")
    if FINITE:
        # c 7.5: the box of the finite lattice, N·pitch wide      (vacuum: imp:n=0 outside)
        # 30   px  10.0
        # 31   px -10.0
        # 32   py  10.0
        # 33   py -10.0
        width = RODS * PITCH / 2
        for j, plane, value in (
            (30, openmc.XPlane, width), (31, openmc.XPlane, -width),
            (32, openmc.YPlane, width), (33, openmc.YPlane, -width),
        ):  # fmt: skip
            s[j] = plane(value, surface_id=j, name=f"box {value:+g}", boundary_type="vacuum")
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
    # m2   1001 -0.1119  8016 -0.8881     $ water (rho 1.0; 7.6: the density)
    #      5010 -...     5011 -...        $ 7.8: boron, ppm by weight
    # mt2  lwtr
    boron = BORON_PPM * 1e-6
    mat[2] = openmc.Material(material_id=2, name="water" if not boron else "borated water")
    mat[2].add_element("H", (1 - boron) * H_WEIGHT, "wo")
    mat[2].add_element("O", (1 - boron) * (1 - H_WEIGHT), "wo")
    if boron:
        mat[2].add_element("B", boron, "wo")
    mat[2].set_density("g/cm3", WATER_DENSITY)
    mat[2].add_s_alpha_beta("c_H_in_H2O")
    return mat


def source_cards(settings, s):
    # mode n
    settings.run_mode = "eigenvalue"
    # kcode 10000 1.0 20 100   $ neutrons per cycle, initial k, inactive, total cycles
    settings.particles = 10000
    settings.inactive = 20
    settings.batches = 100
    # ksrc 0 0 0   -> a uniform first source over the box, kept in the fuel only
    half = RODS * PITCH / 2 if FINITE else PITCH / 2
    settings.source = openmc.IndependentSource(
        space=openmc.stats.Box((-half, -half, -25.0), (half, half, 25.0)),
        constraints={"fissionable": True},
    )


def tally_cards(c):
    # f4:n 1 2             $ flux in the fuel and in the water
    # e4 0.5e-6 0.5 20     $ three groups [MeV]
    t4 = openmc.Tally(tally_id=4, name="flux in the fuel and the water")
    t4.filters = [
        openmc.CellFilter([c[1], c[2]]),
        openmc.EnergyFilter([0.0, 0.5, 0.5e6, 20.0e6]),  # eV
    ]
    t4.scores = ["flux"]
    return [t4]


def build():
    s = surface_cards()
    mat = material_cards()
    c, root = cell_cards(s, mat)

    model = openmc.Model()
    model.geometry = openmc.Geometry(root)
    model.materials = openmc.Materials(mat.values())
    source_cards(model.settings, s)
    model.tallies = openmc.Tallies(tally_cards(c))
    return model


if __name__ == "__main__":
    with strict_ids():
        model = build()
    model.export_to_model_xml()
