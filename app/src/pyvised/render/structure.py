"""The universe structure of a model, for highlighting: what lies inside a cell or universe.

Slices hold the deepest cells, so highlighting a universe, a lattice or a filled
cell means highlighting every cell nested in it. A universe used in several
places lights up everywhere it is used.
"""

from pyvised_protocol.schemas import ModelInfo


class ModelStructure:
    def __init__(self, info: ModelInfo) -> None:
        self._cells = {u.id: list(u.cells) for u in info.universes}
        self._fills = {
            c.id: c.fill
            for c in info.cells
            if c.fill_type in ("universe", "lattice") and c.fill is not None
        }
        self._lattices = {
            lat.id: [*lat.universes, *([lat.outer] if lat.outer is not None else [])]
            for lat in info.lattices
        }

    def cells_in_universe(self, universe: int) -> set[int]:
        """Every cell of `universe` and, recursively, of what its cells are filled with."""
        found: set[int] = set()
        seen: set[int] = set()
        todo = [universe]
        while todo:
            node = todo.pop()
            if node in seen:
                continue
            seen.add(node)
            if node in self._lattices:
                todo += self._lattices[node]
                continue
            for cell in self._cells.get(node, []):
                found.add(cell)
                if cell in self._fills:
                    todo.append(self._fills[cell])
        return found

    def cells_in_cell(self, cell: int) -> set[int]:
        """The cell itself and everything nested in its fill."""
        fill = self._fills.get(cell)
        return {cell} | (self.cells_in_universe(fill) if fill is not None else set())
