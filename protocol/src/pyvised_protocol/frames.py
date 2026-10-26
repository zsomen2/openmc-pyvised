"""Compact binary frames for numpy arrays (geometry slices, tracks, tally data).

Layout::

    b"PVA1" | uint32 little-endian header length | UTF-8 JSON header | blobs

The header lists every array with its dtype, shape, codec and byte range in the
blob section; ``meta`` carries any JSON-serialisable extras. Only numpy and the
standard library are needed on either side.
"""

import json
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np

MAGIC = b"PVA1"
CONTENT_TYPE = "application/x-pyvised-frame"
_HEADER_LEN = struct.Struct("<I")


@dataclass
class Frame:
    arrays: dict[str, np.ndarray] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


def encode(
    arrays: dict[str, np.ndarray], meta: dict[str, Any] | None = None, *, level: int = 1
) -> bytes:
    """Pack `arrays` into one frame; `level` is the zlib level (0 stores raw bytes)."""
    entries = []
    blobs = []
    offset = 0
    for name, array in arrays.items():
        a = np.ascontiguousarray(array)
        if a.dtype.byteorder == ">":
            a = a.astype(a.dtype.newbyteorder("<"))
        raw = a.tobytes()
        blob = zlib.compress(raw, level) if level else raw
        entries.append(
            {
                "name": name,
                "dtype": a.dtype.str,
                "shape": list(a.shape),
                "codec": "zlib" if level else "raw",
                "offset": offset,
                "nbytes": len(blob),
            }
        )
        blobs.append(blob)
        offset += len(blob)
    header = json.dumps({"meta": meta or {}, "arrays": entries}, separators=(",", ":")).encode()
    return b"".join([MAGIC, _HEADER_LEN.pack(len(header)), header, *blobs])


def decode(data: bytes) -> Frame:
    """Unpack a frame produced by `encode`; the arrays are read-only views."""
    if data[:4] != MAGIC:
        raise ValueError("not a PyVised frame")
    (header_len,) = _HEADER_LEN.unpack_from(data, 4)
    header = json.loads(data[8 : 8 + header_len])
    base = 8 + header_len
    arrays = {}
    for entry in header["arrays"]:
        start = base + entry["offset"]
        blob = data[start : start + entry["nbytes"]]
        if entry["codec"] == "zlib":
            raw = zlib.decompress(blob)
        elif entry["codec"] == "raw":
            raw = blob
        else:
            raise ValueError(f"unknown codec {entry['codec']!r}")
        dtype = np.dtype(entry["dtype"])
        arrays[entry["name"]] = np.frombuffer(raw, dtype=dtype).reshape(entry["shape"])
    return Frame(arrays, header.get("meta", {}))
