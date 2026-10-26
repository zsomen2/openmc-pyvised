import numpy as np
import pytest

from pyvised_protocol import frames


def test_round_trip_keeps_dtype_shape_and_meta():
    ids = np.arange(12, dtype=np.int32).reshape(3, 4)
    props = np.linspace(0.0, 1.0, 6).reshape(2, 3)
    flags = np.array([True, False, True])
    data = frames.encode({"ids": ids, "props": props, "flags": flags}, {"elapsed": 0.25})

    frame = frames.decode(data)

    assert frame.meta == {"elapsed": 0.25}
    for name, original in {"ids": ids, "props": props, "flags": flags}.items():
        decoded = frame.arrays[name]
        assert decoded.dtype == original.dtype
        assert decoded.shape == original.shape
        np.testing.assert_array_equal(decoded, original)


def test_raw_codec_and_empty_array():
    empty = np.zeros((0, 3), dtype=np.float32)
    frame = frames.decode(frames.encode({"empty": empty}, level=0))
    assert frame.arrays["empty"].shape == (0, 3)


def test_big_endian_input_is_stored_little_endian():
    big = np.arange(5, dtype=">i4")
    frame = frames.decode(frames.encode({"big": big}))
    assert frame.arrays["big"].dtype == np.dtype("<i4")
    np.testing.assert_array_equal(frame.arrays["big"], np.arange(5))


def test_uniform_id_map_compresses_well():
    ids = np.full((1000, 1000), 7, dtype=np.int32)
    assert len(frames.encode({"ids": ids})) < 50_000


def test_rejects_foreign_data():
    with pytest.raises(ValueError, match="not a PyVised frame"):
        frames.decode(b"PNG....")
