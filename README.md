# openmc-pyvised

PyVised is a Windows desktop application providing a GUI for [OpenMC](https://docs.openmc.org)
with geometry visualisation and simulation controls, made for teaching Monte Carlo particle
transport. OpenMC does not run natively on Windows, so PyVised starts and manages it in a
Docker container in the background; you only see one window.

**Status:** early development. The application shell, the OpenMC 0.16.0 backend image and
the backend lifecycle work; the geometry viewer comes next.

## Requirements

- Windows 10 22H2 or Windows 11
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (free for education)
- For development: [uv](https://docs.astral.sh/uv/) and [just](https://just.systems)

## Development

```powershell
just setup           # Python 3.13 environment + translations
just backend-build   # backend image: OpenMC 0.16.0 + ENDF/B-VII.1 + the PyVised service
just run             # start the app against the local backend image
just test            # client tests on Windows
just backend-test    # backend tests inside the container (real OpenMC)
just                 # list all commands
```

Repository layout:

- `app/` — the desktop client (PySide6)
- `backend/` — the OpenMC service that runs inside the Docker container (FastAPI)
- `protocol/` — schemas shared by both

## License

GPL-3.0-or-later, see [LICENSE](LICENSE).
