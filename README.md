# openmc-pyvised

PyVised is a Windows desktop application providing a GUI for [OpenMC](https://docs.openmc.org)
with geometry visualisation and simulation controls, made for teaching Monte Carlo particle
transport. OpenMC does not run natively on Windows, so PyVised starts and manages it in a
Docker container in the background; you only see one window.

**Status:** alpha (0.1.0a0), the first release for testing on lab PCs. Example models of the
course tasks (Tools › Examples) are being added one by one.

- **Geometry:** open an OpenMC model and inspect it in two linked slice views coloured by
  material, cell, universe (at any nesting level), density, temperature or importance
  (weight windows), with a legend, highlighting, region labels, an overlap count, a point
  inspector that shows the full nesting path, and PNG or `.npz` export. Builder parameters
  (e.g. rod positions) can be edited; the model is exported again and redrawn.
- **Simulation:** run the model with its own settings or a preset, with a thread count, a
  nuclear-data library, a time limit and σ(k-eff) or tally relative-error targets. The run
  shows k per generation, the running mean ± σ and the Shannon entropy live; Stop or the
  time limit keeps the latest statepoint. Results: k-eff and its estimators, reactivity (in
  $ with the model's β_eff), comparison with reference results, tally tables with relative
  errors coloured by MCNP's guideline and the figure of merit, normalisation (MCNP's `fm`,
  division by volume or area), plots chosen from the filters (spectra, mesh profiles and heat
  maps with cell outlines, bars), convergence from the statepoints, a run history with
  comparisons, and CSV/PNG/SVG export.
- **Particle tracks:** track up to 2000 histories from the model's source, the converged
  fission source or a point source, and play them back on both geometry views in event steps
  or in time of flight: the particles at their births, collisions, surface crossings and ends,
  optionally with their track lines, coloured by energy, particle type, event or history, over
  a faded geometry. Filters by history, particle type and energy; only the part near each cut
  plane if wanted; a followed history lists its events. In eigenvalue models a fission ends
  the history, as in the simulation.

## Models

- PyVised opens models from its workspace folder (`%USERPROFILE%\PyVised` unless changed), the
  folder the backend container can see: a Python script that builds an `openmc.Model` and
  exports it (`model.export_to_model_xml()`), a Jupyter notebook, or OpenMC XML (`model.xml`,
  or `geometry.xml` with its siblings).
- Scripts and notebooks run inside the container in export-only mode: `PYVISED_EXPORT_ONLY=1`
  is set, and `openmc.run()`, `Model.run()`, plots and depletion are skipped. Builder
  parameters arrive as JSON in `PYVISED_PARAMS`.
- An optional `pyvised.json` next to the model adds a title and description in English and
  Hungarian, the initial views, colours (the app saves the colours you choose there), tally
  normalisation (the volume or area may be a formula of the builder parameters, such as
  `4*pi*radius**2`), builder parameters, run presets, reference results (optionally for given
  parameter values), β_eff and the weight-window lower bound that stands for importance 1
  (default 0.5).
- Runs live next to the model in `runs/<date-time>[_label]/`: the patched copy of the
  exported model (the source is never changed), OpenMC's output and statepoints,
  `pyvised_run.json` and `pyvised_results.json`.
- Builders may use `pyvised_course`, a helper module in the backend image for models that
  mirror an MCNP deck: `lat=1` lattices, `TR`/`*TR` transformations and `trcl`, densities as
  written on cell cards, and cell importances as weight windows.
- Tools › Examples opens the example models of the course tasks, each with the task's text
  as its description. The first time, the example is copied into `examples\` of the
  workspace, so you work on your own copy; a copy you changed can be restored to the original
  (its runs stay).
- PyVised watches the model's folder and offers to reload it when a file changes.
- Besides the built-in ENDF/B-VII.1 library, folders with a `cross_sections.xml` (for example
  a library from [openmc.org](https://openmc.org/data)) can be added in Settings › Backend;
  they are mounted read-only into the container.

## Requirements

- Windows 10 22H2 or Windows 11 23H2 or later, 64-bit
- [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) (free for
  education)
- For development: [uv](https://docs.astral.sh/uv/) and [just](https://just.systems)

## Installation

1. Install Docker Desktop, start it once and accept its terms.
2. Download `PyVised-Setup-<version>.exe` from the
   [releases](https://github.com/zsomen2/openmc-pyvised/releases) and run it. It installs for
   the current user; an administrator can install it for all users.
3. At its first start PyVised downloads its OpenMC backend (about 1 GB, once per PC) and runs
   a self-test. Its setup window shows each step and what to do when one fails.

On a PC without internet access, download `pyvised-backend-<version>.tar.gz` from the same
release and choose *Tools › Backend setup… › Load from file…*. Silent installation for lab
PCs with a shared account: [installer/LAB_DEPLOYMENT.md](installer/LAB_DEPLOYMENT.md).

## Development

```powershell
just setup           # Python 3.13 environment + translations
just backend-build   # backend image: OpenMC 0.16.0 + ENDF/B-VII.1 + the PyVised service
just run             # start the app against the local backend image
just test            # client tests on Windows
just backend-test    # backend tests inside the container (real OpenMC)
just                 # list all commands
```

Packaging (needs [Inno Setup 6](https://jrsoftware.org/isinfo.php):
`winget install JRSoftware.InnoSetup`):

```powershell
just build-exe       # dist/PyVised: the app folder (PyInstaller), then its self-test
just installer       # dist/PyVised-Setup-<version>.exe
just check-install   # install it silently for you, self-test it, uninstall it
just backend-save    # dist/pyvised-backend-<version>.tar: the offline image file
```

Releases: the app, backend and protocol share one version (`just set-version 1.0.1`). Pushing
the tag `v<version>` runs `.github/workflows/release.yml`: it checks and builds everything,
pushes `ghcr.io/zsomen2/pyvised-backend:<version>` and drafts a GitHub release with the
installer, the offline image file and their checksums. The app downloads the image tag of
its own version. The first push creates the image's package as private: make it public once
(Package settings › Change visibility).

Repository layout:

- `app/` — the desktop client (PySide6)
- `backend/` — the OpenMC service that runs inside the Docker container (FastAPI), and the
  `pyvised_course` helpers for course models
- `protocol/` — schemas shared by both
- `packaging/` — the PyInstaller spec; `installer/` — the Inno Setup script and the lab
  deployment guide; `.github/workflows/` — CI and releases

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
