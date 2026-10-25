"""PyVised: a desktop GUI for OpenMC with geometry visualisation and simulation control."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pyvised")
except PackageNotFoundError:  # source checkout without installation
    __version__ = "0.0.0"

REPOSITORY_URL = "https://github.com/zsomen2/openmc-pyvised"
BACKEND_IMAGE_REPOSITORY = "ghcr.io/zsomen2/pyvised-backend"
