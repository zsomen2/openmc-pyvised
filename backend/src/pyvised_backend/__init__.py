"""PyVised backend: the OpenMC service that runs inside the Docker container."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pyvised-backend")
except PackageNotFoundError:  # source checkout without installation
    __version__ = "0.0.0"
