"""Locations of bundled resources and per-user folders."""

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
"""The `pyvised` package folder (inside the PyInstaller bundle when frozen)."""


def resource_root() -> Path:
    """Folder that holds `assets/`: the PyInstaller bundle, or `app/` in a source checkout."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle)
    return PACKAGE_DIR.parents[1]


def assets_dir() -> Path:
    return resource_root() / "assets"


def app_icon_path() -> Path:
    return assets_dir() / "pyvised_app_icon.ico"


def logo_path() -> Path:
    return assets_dir() / "pyvised_logo_transparent.png"


def i18n_dir() -> Path:
    return PACKAGE_DIR / "i18n"


def examples_dir() -> Path:
    """Example models of the course tasks: in the bundle, or `examples/` of a source checkout."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "examples"
    return PACKAGE_DIR.parents[2] / "examples"


def local_data_dir() -> Path:
    """Per-user, per-machine data: logs, lock file (`%LOCALAPPDATA%\\PyVised`)."""
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) if base else Path.home() / ".local" / "share") / "PyVised"


def log_dir() -> Path:
    return local_data_dir() / "logs"


def default_workspace() -> Path:
    """Outside Documents on purpose: OneDrive often redirects Documents on managed PCs."""
    return Path.home() / "PyVised"
