"""交互分割 mask_input 回灌的 low-res logits 编解码 (协议 §2.2)。

SAM2 / SAM3 的 `predict(..., mask_input=...)` 接收上一轮 256×256 low-res logits
回灌, 多点精修时显著提升 mask 稳定性与边界质量。为保持 backend 无状态, 这些 logits
由前端携带往返: backend 把本轮 `low_res_masks` 编码成不透明字符串 `mask_input_next`
返回, 前端原样存储、下一次点击经 `context.mask_input` 回传, backend 再解码喂回。

编码格式 (format `m1`):
    float16(256×256) → tobytes → zlib(level=6) → 前缀 magic `m1` → base64(ascii)

float16 把每像素压到 2 字节 (256×256 ≈ 128KB), zlib 对 clamp 到 [-32,32] 的 logits
(大片饱和区) 进一步压缩; base64 仅为可放进 JSON 的传输编码。numpy 惰性 import —— 本包
其余消费方 (yolo 等) 不依赖 numpy, 只有调用编解码的 SAM backend 才需要它。
"""

from __future__ import annotations

import base64
import zlib
from typing import Any

# format 标记: 解码时校验, 未来换格式 (如 int8 量化) 可换 magic 而不误读旧串。
_MAGIC = b"m1"
_SIDE = 256  # SAM low-res mask 边长 (H=W=256)
_RAW_BYTES = _SIDE * _SIDE * 2
MAX_LOW_RES_MASK_INPUT_CHARS = 512 * 1024


def encode_low_res_mask(arr: Any) -> str:
    """256×256 float logits → 不透明 base64 字符串 (供 `mask_input_next`)。

    arr 接受 (256,256) 或 (1,256,256) / (1,1,256,256) (自动 squeeze 取单张)。
    """
    import numpy as np

    a = np.asarray(arr, dtype=np.float16)
    # 仅剥离前导的 batch/通道维 (size-1), 不用 np.squeeze: 后者会压扁任意位置的 size-1 维
    # (如 (256,1,256) 被悄悄压成 (256,256) 导致轴错位); 多实例 (N,256,256) 则 N!=1 停在此处,
    # 由下面的 shape 断言显式拒绝 (见 issue 0008.4)。
    while a.ndim > 2 and a.shape[0] == 1:
        a = a[0]
    if a.shape != (_SIDE, _SIDE):
        raise ValueError(f"expected {_SIDE}x{_SIDE} low-res mask, got shape {a.shape}")
    if not np.isfinite(a).all():
        raise ValueError("mask_input logits must be finite")
    a = np.clip(a, -32.0, 32.0)
    raw = zlib.compress(np.ascontiguousarray(a).tobytes(), 6)
    return base64.b64encode(_MAGIC + raw).decode("ascii")


def decode_low_res_mask(s: str) -> Any:
    """base64 字符串 → (1,256,256) float32, 直接喂 `predict(mask_input=...)`。"""
    import numpy as np

    if not isinstance(s, str) or len(s) > MAX_LOW_RES_MASK_INPUT_CHARS:
        raise ValueError("mask_input exceeds the encoded byte budget")
    blob = base64.b64decode(s, validate=True)
    if blob[: len(_MAGIC)] != _MAGIC:
        raise ValueError("unknown mask_input encoding (bad magic)")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(blob[len(_MAGIC) :], _RAW_BYTES + 1)
    if (
        len(raw) != _RAW_BYTES
        or decoder.unconsumed_tail
        or not decoder.eof
        or decoder.unused_data
    ):
        raise ValueError("mask_input decompressed size is invalid")
    a = np.frombuffer(raw, dtype=np.float16).astype(np.float32)
    if a.size != _SIDE * _SIDE:
        raise ValueError(f"decoded {a.size} floats, expected {_SIDE * _SIDE}")
    if not np.isfinite(a).all():
        raise ValueError("decoded mask_input logits must be finite")
    return a.reshape(1, _SIDE, _SIDE)
