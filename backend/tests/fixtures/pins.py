"""Test builder: a 2 x 2 pin lattice in a box, a void box beside it, vacuum outside.

The layout, seen from +z (x to the right, y up), with pitch p:

    y = +p  +---------+-----+
            | 2x2 pins| void|
    y = -p  +---------+-----+
          x=-p       x=p   x=p+1

The pitch comes from the builder parameter "pitch". The script ends with
model.run(), which PyVised must skip.
"""

import json
import os

import openmc

pitch = json.loads(os.environ.get("PYVISED_PARAMS") or "{}").get("pitch", 1.0)

fuel = openmc.Material(material_id=1, name="fuel")
fuel.add_nuclide("U235", 0.1)
fuel.add_nuclide("U238", 0.9)
fuel.add_nuclide("O16", 2.0)
fuel.set_density("g/cm3", 10.0)
water = openmc.Material(material_id=2, name="water")
water.add_nuclide("H1", 2.0)
water.add_nuclide("O16", 1.0)
water.set_density("g/cm3", 1.0)

rod = openmc.ZCylinder(surface_id=1, r=0.3 * pitch)
pin = openmc.Universe(universe_id=1, name="pin")
pin.add_cell(openmc.Cell(cell_id=1, name="fuel", fill=fuel, region=-rod))
pin.add_cell(openmc.Cell(cell_id=2, name="water", fill=water, region=+rod))

lattice = openmc.RectLattice(lattice_id=5, name="2x2")
lattice.pitch = (pitch, pitch)
lattice.lower_left = (-pitch, -pitch)
lattice.universes = [[pin, pin], [pin, pin]]

x0 = openmc.XPlane(surface_id=10, x0=-pitch, boundary_type="vacuum")
x1 = openmc.XPlane(surface_id=11, x0=pitch)
x2 = openmc.XPlane(surface_id=12, x0=pitch + 1.0, boundary_type="vacuum")
y0 = openmc.YPlane(surface_id=13, y0=-pitch, boundary_type="vacuum")
y1 = openmc.YPlane(surface_id=14, y0=pitch, boundary_type="vacuum")
z0 = openmc.ZPlane(surface_id=15, z0=-10.0, boundary_type="vacuum")
z1 = openmc.ZPlane(surface_id=16, z0=10.0, boundary_type="vacuum")
inside = +y0 & -y1 & +z0 & -z1
lattice_cell = openmc.Cell(cell_id=10, name="lattice", fill=lattice, region=+x0 & -x1 & inside)
void_cell = openmc.Cell(cell_id=11, name="void box", region=+x1 & -x2 & inside)

model = openmc.Model()
model.geometry = openmc.Geometry([lattice_cell, void_cell])
model.materials = openmc.Materials([fuel, water])
model.settings.run_mode = "eigenvalue"
model.settings.particles = 100
model.settings.batches = 3
model.settings.inactive = 1
model.settings.source = openmc.IndependentSource(space=openmc.stats.Point())
tally = openmc.Tally(tally_id=4, name="fuel flux")
tally.filters = [openmc.CellFilter([1])]
tally.scores = ["flux"]
model.tallies = [tally]
model.export_to_model_xml()
model.run()
