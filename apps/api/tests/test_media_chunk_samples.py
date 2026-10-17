import base64
import struct

from app.schemas.task import VideoMetadata
from app.services.media_probe import parse_ffprobe_packet_samples
from app.workers import media
from app.workers.media import (
    _avc_codec_string,
    _extract_chunk_samples,
    _extract_decoder_config,
    _find_mp4_box_payload,
    _hevc_codec_string,
)


def _box(fourcc: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + fourcc + payload


def test_parse_ffprobe_packet_samples_assigns_presentation_rank():
    # Decode order with a B-frame: pts is non-monotonic (0.0 / 0.066 / 0.033).
    samples = parse_ffprobe_packet_samples(
        {
            "packets": [
                {
                    "pts_time": "0.000000",
                    "duration_time": "0.033367",
                    "size": "5000",
                    "pos": "48",
                    "flags": "K__",
                },
                {
                    "pts_time": "0.066733",
                    "duration_time": "0.033367",
                    "size": "1200",
                    "pos": "5048",
                    "flags": "___",
                },
                {
                    "pts_time": "0.033367",
                    "duration_time": "0.033367",
                    "size": "800",
                    "pos": "6248",
                    "flags": "___",
                },
            ]
        },
        start_frame=10,
    )

    # Decode order preserved (3 items).
    assert len(samples) == 3
    assert [s["offset_in_chunk"] for s in samples] == [48, 5048, 6248]

    # frame_index assigned by presentation (pts) rank, not decode order.
    # pts 0.0 -> 10, pts 0.033 -> 11, pts 0.066 -> 12
    assert [s["frame_index"] for s in samples] == [10, 12, 11]
    assert [s["pts_ms"] for s in samples] == [0, 67, 33]

    assert samples[0]["is_keyframe"] is True
    assert samples[1]["is_keyframe"] is False
    assert samples[2]["is_keyframe"] is False

    assert [s["size_bytes"] for s in samples] == [5000, 1200, 800]
    assert [s["duration_ms"] for s in samples] == [33, 33, 33]


def test_parse_ffprobe_packet_samples_skips_invalid_packets():
    samples = parse_ffprobe_packet_samples(
        {
            "packets": [
                {
                    "pts_time": "0.000000",
                    "duration_time": "0.033367",
                    "size": "5000",
                    "pos": "48",
                    "flags": "K__",
                },
                # pos is missing -> skipped
                {
                    "pts_time": "0.033367",
                    "duration_time": "0.033367",
                    "size": "800",
                    "flags": "___",
                },
                # pts_time N/A -> skipped
                {
                    "pts_time": "N/A",
                    "duration_time": "0.033367",
                    "size": "800",
                    "pos": "6248",
                    "flags": "___",
                },
            ]
        },
        start_frame=0,
    )

    assert len(samples) == 1
    assert samples[0]["offset_in_chunk"] == 48
    assert samples[0]["frame_index"] == 0


def test_parse_ffprobe_packet_samples_empty():
    assert parse_ffprobe_packet_samples({}, start_frame=0) == []
    assert parse_ffprobe_packet_samples({"packets": []}, start_frame=5) == []


def test_find_mp4_box_payload_validates_size_field():
    avcc_payload = bytes.fromhex("0164000cffe1")
    blob = b"junkavcC" + b"\x00\x00\x00\x10ftyp" + _box(b"avcC", avcc_payload)
    # 前面的裸 "avcC" 字符串前缀不是合法 size, 应被跳过, 命中真正的 box。
    assert _find_mp4_box_payload(blob, b"avcC") == avcc_payload
    assert _find_mp4_box_payload(blob, b"hvcC") is None


def test_avc_codec_string_from_avcc_record():
    # 真实 ffmpeg High@L1.2 chunk 的 avcC 头: 01 64 00 0c ...
    avcc = bytes.fromhex("0164000cffe10019")
    assert _avc_codec_string(avcc) == "avc1.64000c"
    assert _avc_codec_string(b"\x01") is None


def test_hevc_codec_string_main_profile():
    # HEVC Main / main tier / level 3.1 (93): 期望 hvc1.1.6.L93.B0。
    hvcc = bytearray(13)
    hvcc[1] = 0x01  # profile_space=0, tier=0, profile_idc=1
    hvcc[2:6] = (0x60000000).to_bytes(4, "big")  # compat 逆序后 = 0x6
    hvcc[6] = 0xB0  # 首个 constraint 字节
    hvcc[12] = 93  # level_idc
    assert _hevc_codec_string(bytes(hvcc)) == "hvc1.1.6.L93.B0"
    assert _hevc_codec_string(b"\x01\x02") is None


def test_extract_decoder_config_avc(tmp_path):
    payload = bytes.fromhex("0164000cffe1000a")
    blob = b"\x00\x00\x00\x10ftypisom" + _box(b"avcC", payload)
    p = tmp_path / "chunk.mp4"
    p.write_bytes(blob)
    codec, desc = _extract_decoder_config(p)
    assert codec == "avc1.64000c"
    assert base64.b64decode(desc) == payload


def test_extract_decoder_config_hevc(tmp_path):
    rec = bytearray(13)
    rec[1] = 0x01
    rec[2:6] = (0x60000000).to_bytes(4, "big")
    rec[6] = 0xB0
    rec[12] = 93
    p = tmp_path / "chunk.mp4"
    p.write_bytes(_box(b"hvcC", bytes(rec)))
    codec, desc = _extract_decoder_config(p)
    assert codec == "hvc1.1.6.L93.B0"
    assert base64.b64decode(desc) == bytes(rec)


def test_extract_decoder_config_missing(tmp_path):
    p = tmp_path / "chunk.mp4"
    p.write_bytes(b"no codec config box here")
    assert _extract_decoder_config(p) == (None, None)
    # 文件不存在 → 静默 (None, None)。
    assert _extract_decoder_config(tmp_path / "nope.mp4") == (None, None)


def test_extract_chunk_samples_includes_description(monkeypatch, tmp_path):
    sample = {
        "frame_index": 0,
        "pts_ms": 0,
        "duration_ms": 33,
        "is_keyframe": True,
        "size_bytes": 100,
        "offset_in_chunk": 0,
    }
    monkeypatch.setattr(media, "probe_chunk_samples", lambda *a, **k: [sample])
    monkeypatch.setattr(
        media, "_extract_decoder_config", lambda _p: ("avc1.64000c", "Zm9v")
    )
    out = _extract_chunk_samples(
        tmp_path / "c.mp4", 0, VideoMetadata(width=1920, height=1080), "h264"
    )
    assert out["codec_string"] == "avc1.64000c"
    assert out["description"] == "Zm9v"
    assert out["width"] == 1920 and out["height"] == 1080
    assert out["samples"] == [sample]


def test_extract_chunk_samples_falls_back_codec_without_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        media, "probe_chunk_samples", lambda *a, **k: [{"frame_index": 0}]
    )
    monkeypatch.setattr(media, "_extract_decoder_config", lambda _p: (None, None))
    # 无 avcC/hvcC → 退回旧启发式 codec_string, 且不带 description。
    out = _extract_chunk_samples(tmp_path / "c.mp4", 0, VideoMetadata(), "hevc")
    assert out["codec_string"] == "hev1.1.6.L93.B0"
    assert "description" not in out


def test_extract_chunk_samples_empty_when_no_packets(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "probe_chunk_samples", lambda *a, **k: [])
    assert _extract_chunk_samples(tmp_path / "c.mp4", 0, VideoMetadata(), "h264") == {}
