"""mp4 box 解析与 RFC6381 codec string 派生的纯函数(从 media.py 拆出,行为零变化)。

只做字节级解析,不触碰 DB / ffmpeg / Celery —— 被 media.py 整体 re-export 以保持
``from app.workers.media import ...`` 的旧入口不变。
"""

import base64
from pathlib import Path


def _webcodecs_codec_string(output_codec: str | None) -> str:
    normalized = (output_codec or "").lower()
    if "hevc" in normalized or "h265" in normalized:
        return "hev1.1.6.L93.B0"
    return "avc1.4d001e"


def _find_mp4_box_payload(data: bytes, fourcc: bytes) -> bytes | None:
    """在 mp4 字节里按 fourcc 定位 box, 返回 payload (8 字节 box 头之后的内容)。

    box 结构: ``[uint32 size][4 字节 type][payload...]``, size 含 8 字节头。子串扫描 +
    size 合法性校验, 避免把 payload 中偶然出现的 fourcc 误判成 box 头。找不到返回 None。
    """
    start = 0
    while True:
        i = data.find(fourcc, start)
        if i == -1:
            return None
        if i < 4:
            start = i + 4
            continue
        box_start = i - 4
        size = int.from_bytes(data[box_start:i], "big")
        end = box_start + size
        if size > 8 and end <= len(data):
            return data[i + 4 : end]
        start = i + 4


def _avc_codec_string(avcc: bytes) -> str | None:
    """从 AVCDecoderConfigurationRecord 派生 RFC6381 codec string (avc1.PPCCLL)。

    byte1/2/3 = profile_idc / profile_compatibility / level_idc, 直接 hex 即 codec 后缀。
    """
    if len(avcc) < 4:
        return None
    return "avc1." + avcc[1:4].hex()


def _hevc_codec_string(hvcc: bytes) -> str | None:
    """从 HEVCDecoderConfigurationRecord 派生 RFC6381 codec string (hvc1.…)。"""
    if len(hvcc) < 13:
        return None
    b1 = hvcc[1]
    profile_space = (b1 >> 6) & 0x03
    tier_flag = (b1 >> 5) & 0x01
    profile_idc = b1 & 0x1F
    compat = int.from_bytes(hvcc[2:6], "big")
    rev = 0
    for _ in range(32):
        rev = (rev << 1) | (compat & 1)
        compat >>= 1
    space = {0: "", 1: "A", 2: "B", 3: "C"}.get(profile_space, "")
    parts = [
        "hvc1",
        f"{space}{profile_idc}",
        format(rev, "x").upper() or "0",
        ("H" if tier_flag else "L") + str(hvcc[12]),
    ]
    for byte in hvcc[6:12].rstrip(b"\x00"):
        parts.append(format(byte, "02X"))
    return ".".join(parts)


def _extract_decoder_config(chunk_path: Path) -> tuple[str | None, str | None]:
    """从 chunk mp4 读取 avcC/hvcC, 返回 (codec_string, description_base64)。

    description = WebCodecs ``VideoDecoderConfig.description`` 所需的 AVC/HEVCDecoderConfigurationRecord
    (含 SPS/PPS)。样本是 AVCC 长度前缀格式, 缺 description 时浏览器按 Annex-B 解析必失败,
    所以这里必须把 extradata 透出。读取失败返回 (None, None), 调用方退回旧启发式 codec_string。
    """
    try:
        data = chunk_path.read_bytes()
    except OSError:
        return None, None
    avcc = _find_mp4_box_payload(data, b"avcC")
    if avcc is not None:
        return _avc_codec_string(avcc), base64.b64encode(avcc).decode("ascii")
    hvcc = _find_mp4_box_payload(data, b"hvcC")
    if hvcc is not None:
        return _hevc_codec_string(hvcc), base64.b64encode(hvcc).decode("ascii")
    return None, None
