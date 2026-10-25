"""Log to `%LOCALAPPDATA%\\PyVised\\logs\\pyvised.log` (and stderr when there is one)."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import log_dir

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> Path:
    folder = log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "pyvised.log"
    root = logging.getLogger()
    root.setLevel(level)
    file_handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(file_handler)
    if sys.stderr is not None:  # None under pythonw (the installed GUI launcher)
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(stream)
    return path
