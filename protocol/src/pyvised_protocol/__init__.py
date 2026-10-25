"""Schemas and binary frames shared by the PyVised client and backend."""

from importlib.metadata import PackageNotFoundError, version

PROTOCOL_VERSION = 2
"""Raised on every incompatible API change; client and backend must report the same value."""

API_PREFIX = "/api/v1"
CONTAINER_PORT = 8765
"""Port the backend listens on inside the container."""

TOKEN_ENV = "PYVISED_TOKEN"
"""Environment variable that hands the session token to the backend container."""

try:
    __version__ = version("pyvised-protocol")
except PackageNotFoundError:  # source checkout without installation
    __version__ = "0.0.0"
