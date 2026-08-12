import numpy as np
from paperflow.core.intent.encoders.dense import (
    FixedDenseEncoder, DenseEncoder, _deterministic_seed,
)


class TestDeterministicSeed:
    def test_same_text_same_seed(self):
        assert _deterministic_seed("circRNA") == _deterministic_seed("circRNA")

    def test_different_text_different_seed(self):
        assert _deterministic_seed("circRNA") != _deterministic_seed("miRNA")


class TestFixedDenseEncoder:
    def test_same_text_same_vector(self):
        enc = FixedDenseEncoder(dim=32)
        v1 = enc(["circRNA 机制"])
        v2 = enc(["circRNA 机制"])
        np.testing.assert_array_equal(v1, v2)

    def test_different_text_different_vector(self):
        enc = FixedDenseEncoder(dim=32)
        v1 = enc(["circRNA 机制"])
        v2 = enc(["异构图神经网络"])
        assert not np.array_equal(v1, v2)

    def test_unit_norm(self):
        enc = FixedDenseEncoder(dim=64)
        v = enc(["测试文本"])[0]
        assert abs(np.linalg.norm(v) - 1.0) < 1e-9

    def test_shape(self):
        enc = FixedDenseEncoder(dim=384)
        out = enc(["a", "b", "c"])
        assert out.shape == (3, 384)

    def test_protocol_exists(self):
        assert hasattr(DenseEncoder, "__call__")
