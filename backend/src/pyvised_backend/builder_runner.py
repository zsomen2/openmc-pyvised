"""Run a model builder in export-only mode: ``python -m pyvised_backend.builder_runner FILE``.

FILE is a ``.py`` script or an ``.ipynb`` notebook. The working directory is
the cache folder that receives the exported XML. OpenMC calls that would run a
simulation, a volume calculation, a depletion or a plot become no-ops, so a
student's script may end with ``model.run()`` and still only export the model.
"""

import linecache
import os
import runpy
import sys
from pathlib import Path

# (owner, attribute) pairs replaced by no-ops; owners are looked up in the openmc package
DISABLED = [
    ("", "run"),
    ("", "calculate_volumes"),
    ("", "search_for_keff"),
    ("", "plot_geometry"),
    ("", "plot_inline"),
    ("Model", "run"),
    ("Model", "calculate_volumes"),
    ("Model", "deplete"),
    ("Model", "plot"),
    ("Model", "plot_geometry"),
    ("Geometry", "plot"),
    ("Universe", "plot"),
    ("Cell", "plot"),
    ("Material", "deplete"),
    ("Materials", "deplete"),
]


def _skipped(name: str):
    def stub(*args, **kwargs):
        print(f"PyVised: {name}() skipped, the model is only exported here", file=sys.stderr)

    return stub


def disable_simulations() -> None:
    import openmc  # pyright: ignore[reportMissingImports]  (container only)

    for owner_name, attribute in DISABLED:
        owner = getattr(openmc, owner_name) if owner_name else openmc
        if hasattr(owner, attribute):
            label = f"{owner_name}.{attribute}" if owner_name else f"openmc.{attribute}"
            setattr(owner, attribute, _skipped(label))
    try:
        import openmc.deplete  # pyright: ignore[reportMissingImports]  (container only)
    except ImportError:
        return
    for name in dir(openmc.deplete):
        cls = getattr(openmc.deplete, name)
        if isinstance(cls, type) and name.endswith("Integrator") and hasattr(cls, "integrate"):
            cls.integrate = _skipped(f"{name}.integrate")


def run_notebook(path: Path) -> None:
    from .notebook import notebook_source

    source = notebook_source(path)
    filename = f"{path} (code cells)"
    # tracebacks then quote the converted source instead of the notebook's JSON
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    namespace = {
        "__name__": "__main__",
        "__file__": str(path),
        "display": lambda *args, **kwargs: None,
    }
    exec(compile(source, filename, "exec"), namespace)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m pyvised_backend.builder_runner FILE", file=sys.stderr)
        return 2
    path = Path(argv[0]).resolve()
    os.environ["PYVISED_EXPORT_ONLY"] = "1"
    # the script's folder comes first on sys.path, as when it is run directly
    sys.path.insert(0, str(path.parent))
    sys.argv = [str(path)]
    disable_simulations()
    if path.suffix.lower() == ".ipynb":
        run_notebook(path)
    else:
        runpy.run_path(str(path), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
