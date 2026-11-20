"""mask_codec 编解码往返单测 (协议 §2.2 交互精修 mask_input 回灌)。"""

from __future__ import annotations

import numpy as np
import pytest

from yoao_protocol_v2 import decode_low_res_mask, encode_low_res_mask


def test_roundtrip_equivalence():
    """encode → decode 在 float16 精度下等价 (logits 直接喂回 predict)。"""
    rng = np.random.default_rng(0)
    arr = (rng.standard_normal((256, 256)).astype(np.float32) * 8.0).clip(-32, 32)
    decoded = decode_low_res_mask(encode_low_res_mask(arr))
    assert decoded.shape == (1, 256, 256)
    assert decoded.dtype == np.float32
    # float16 往返误差: 量化到半精度后逐元素差应 ~0 (相对量级小).
    np.testing.assert_allclose(
        decoded[0], arr.astype(np.float16).astype(np.float32), rtol=0, atol=0
    )


def test_encode_accepts_batched_shapes():
    """(256,256) / (1,256,256) / (1,1,256,256) 都接受 (squeeze 取单张)。"""
    base = np.zeros((256, 256), dtype=np.float32)
    s1 = encode_low_res_mask(base)
    s2 = encode_low_res_mask(base[None, :, :])
    s3 = encode_low_res_mask(base[None, None, :, :])
    assert isinstance(s1, str) and s1.isascii()
    assert s1 == s2 == s3


def test_encode_rejects_wrong_shape():
    with pytest.raises(ValueError):
        encode_low_res_mask(np.zeros((128, 128), dtype=np.float32))


def test_decode_rejects_bad_magic():
    import base64

    with pytest.raises(ValueError):
        decode_low_res_mask(base64.b64encode(b"xx garbage").decode("ascii"))


def test_decode_rejects_trailing_or_oversized_payload():
    import base64

    encoded = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
    blob = base64.b64decode(encoded) + b"trailing"
    with pytest.raises(ValueError, match="size is invalid"):
        decode_low_res_mask(base64.b64encode(blob).decode("ascii"))

    with pytest.raises(ValueError, match="encoded byte budget"):
        decode_low_res_mask("A" * (512 * 1024 + 1))


def test_codec_rejects_non_finite_logits():
    arr = np.zeros((256, 256), dtype=np.float32)
    arr[0, 0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        encode_low_res_mask(arr)
