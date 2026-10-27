"""WebCodecs E2E 确定性 fixture 生成(test-only,无 DB / 存储依赖)。

用 numpy 生成每帧可机器识别的像素图案(背景亮度按帧分组 + 四角 bit 编码帧号低位 +
中心场景色),用 ffmpeg 编码成不同 profile / GOP / B 帧的 H.264,再复用生产同构的
ffprobe packet probe 与 avcC 提取产出 chunk samples + codec description。这样前端
demux / decode 路径在 E2E 里拿到的是与真实 worker 完全相同的字节结构,而不是手搓的
假数据。unsupported / malformed 的故障描述供浏览器测试响应注入，不修改合法输出。

帧身份编码(供 Playwright 像素采样):
  - 背景顶部窄条:灰度 = clip(32 + (i % 8) * 28),按帧分组,辅助识别相邻帧误取;
  - 四角 22×22 方块:编码 frame_index 低 4 位(bit0=TL, bit1=TR, bit2=BL, bit3=BR),
    白(235) / 黑(18),高对比,容差判定;
  - 中心 40×32 方块:场景色,区分不同 fixture。
H.264 有色度抽样与有损压缩,验证用亮度阈值 / 色距容差,不比较完全相同的 RGBA hash。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Literal

import numpy as np
from PIL import Image

from app.workers.media_codec import _extract_decoder_config
from app.workers.media_chunks import extract_video_chunk, extract_video_chunk_smart_copy
from app.services.media_probe import probe_chunk_samples, probe_video_frame_timetable

FRAME_W = 160
FRAME_H = 120
FRAME_FPS = 30
BIT_EDGE = 22

SceneName = Literal["red", "blue", "green", "magenta"]

SCENE_RGB: dict[str, tuple[int, int, int]] = {
    "red": (210, 50, 50),
    "blue": (50, 80, 210),
    "green": (50, 200, 80),
    "magenta": (200, 50, 200),
}

WebCodecsFixture = Literal[
    "h264-baseline-gop12",
    "h264-main-bframes-gop30",
    "h264-issue-context",
    "h264-boundary-gop8",
    "h264-vfr",
    "unsupported-config",
    "malformed-samples",
    "1080p-30",
    "1080p-60",
    "4k-30",
]

# 每个真实编码 fixture 的 ffmpeg 参数与帧数。frame_count 控制在几秒内保证 E2E 速度。
FIXTURE_SPECS: dict[str, dict[str, Any]] = {
    "h264-baseline-gop12": {
        "profile": "baseline",
        "gop": 12,
        "bframes": 0,
        "frames": 12,
        "scene": "red",
        "vfr": False,
    },
    "h264-main-bframes-gop30": {
        "profile": "main",
        "gop": 30,
        "bframes": 2,
        "frames": 48,
        "scene": "blue",
        "vfr": False,
    },
    "h264-issue-context": {
        "profile": "main",
        "gop": 30,
        "bframes": 2,
        "frames": 180,
        "scene": "blue",
        "vfr": False,
        "center_code": True,
    },
    "h264-boundary-gop8": {
        "profile": "baseline",
        "gop": 8,
        "bframes": 0,
        "frames": 24,
        "scene": "green",
        "vfr": False,
    },
    "h264-vfr": {
        "profile": "main",
        "gop": 24,
        "bframes": 0,
        "frames": 24,
        "scene": "magenta",
        "vfr": True,
    },
}

QUALIFICATION_CHUNK_SIZE_FRAMES = 60
QUALIFICATION_FIXTURE_SPECS: dict[str, dict[str, Any]] = {
    "1080p-30": {"width": 1920, "height": 1080, "fps": 30, "bitrate": "4M"},
    "1080p-60": {"width": 1920, "height": 1080, "fps": 60, "bitrate": "6M"},
    "4k-30": {"width": 3840, "height": 2160, "fps": 30, "bitrate": "8M"},
}

# 帧身份采样区(归一化坐标,相对视频帧;E2E 据此映射到 Konva canvas)。
_BG_NORM = {"x": 0.40, "y": 0.04, "w": 0.20, "h": 0.05}
_BIT_NORM = [
    {"bit": 0, "x": 0.0375, "y": 0.0500, "w": 0.1375, "h": 0.1833},  # TL
    {"bit": 1, "x": 0.8250, "y": 0.0500, "w": 0.1375, "h": 0.1833},  # TR
    {"bit": 2, "x": 0.0375, "y": 0.7667, "w": 0.1375, "h": 0.1833},  # BL
    {"bit": 3, "x": 0.8250, "y": 0.7667, "w": 0.1375, "h": 0.1833},  # BR
]
_CENTER_BIT_PIXELS = [
    {"bit": bit, "x": 52 + (bit % 4) * 14, "y": 46 + (bit // 4) * 14}
    for bit in range(8)
]
_CENTER_BIT_EDGE = 12


def _background_luma(frame_index: int) -> int:
    """用高位分组亮度补足四角低 4 bit，保证 0..47 帧签名唯一。"""
    return int(np.clip(32 + (frame_index >> 4) * 80, 0, 255))


def _make_frame(
    frame_index: int,
    scene_rgb: tuple[int, int, int],
    *,
    center_code: bool = False,
) -> np.ndarray:
    """生成单帧:背景分组灰度 + 四角 bit 编码 + 中心场景色。"""
    img = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.uint8)
    bg = _background_luma(frame_index)
    img[:, :] = bg
    # 中心场景色块(避开四角与背景采样条)。
    img[44:76, 60:100] = scene_rgb
    corners = [
        (6, 6),
        (FRAME_W - 6 - BIT_EDGE, 6),
        (6, FRAME_H - 6 - BIT_EDGE),
        (FRAME_W - 6 - BIT_EDGE, FRAME_H - 6 - BIT_EDGE),
    ]
    for j, (x, y) in enumerate(corners):
        on = (frame_index >> j) & 1
        img[y : y + BIT_EDGE, x : x + BIT_EDGE] = 235 if on else 18
    # This separate long fixture keeps its full 8-bit identity visible after zoom/pan.
    # Existing precise-frame fixtures retain their original pixels and signatures.
    if center_code:
        for cell in _CENTER_BIT_PIXELS:
            x, y = cell["x"], cell["y"]
            on = (frame_index >> cell["bit"]) & 1
            img[y : y + _CENTER_BIT_EDGE, x : x + _CENTER_BIT_EDGE] = 235 if on else 18
    return img


def _ffmpeg_encode(spec: dict[str, Any], frames_dir: Path, out: Path) -> None:
    """用 libx264 编码 PNG 序列为 AVCC mp4(chunk bytes 直供前端切片)。"""
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-framerate",
        str(FRAME_FPS),
        "-i",
        str(frames_dir / "f%04d.png"),
        "-frames:v",
        str(spec["frames"]),
        "-an",
        "-c:v",
        "libx264",
        "-profile:v",
        spec["profile"],
        "-g",
        str(spec["gop"]),
        "-keyint_min",
        str(spec["gop"]),
        "-sc_threshold",
        "0",
        "-bf",
        str(spec["bframes"]),
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-movflags",
        "+faststart",
    ]
    if spec.get("vfr"):
        # VFR:累计增加每两个输入帧之间的时间,得到 1/30、2/30 秒交替且严格单调的 PTS。
        cmd += ["-vf", "setpts=PTS+floor(N/2)", "-fps_mode", "vfr"]
    cmd.append(str(out))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or "ffmpeg webcodecs fixture encode failed"
        )


def generate_fixture(
    fixture: str,
    tmpdir: str | Path,
    *,
    chunk_size_frames: int = QUALIFICATION_CHUNK_SIZE_FRAMES,
    include_chunks: bool = True,
) -> dict[str, Any]:
    """生成真实编码 fixture 的 mp4 bytes 与生产同结构 chunk samples / codec metadata。

    返回:{mp4_bytes, samples, codec_string, description, width, height, fps, frame_count}。
    unsupported / malformed 由 apply_metadata_mutation 在此结果上篡改。
    """
    spec = FIXTURE_SPECS[_real_fixture_name(fixture)]
    scene_rgb = SCENE_RGB[spec["scene"]]
    frames_dir = Path(tmpdir) / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for i in range(spec["frames"]):
        Image.fromarray(
            _make_frame(i, scene_rgb, center_code=spec.get("center_code", False))
        ).save(frames_dir / f"f{i:04d}.png")
    out = Path(tmpdir) / "chunk.mp4"
    _ffmpeg_encode(spec, frames_dir, out)

    samples = probe_chunk_samples(out, 0)
    codec, description = _extract_decoder_config(out)
    if not samples or not codec or not description:
        raise RuntimeError(
            f"webcodecs fixture {fixture} produced empty samples / codec / description"
        )
    frame_indexes = sorted(int(sample["frame_index"]) for sample in samples)
    if len(samples) != spec["frames"] or frame_indexes != list(range(spec["frames"])):
        raise RuntimeError(
            f"webcodecs fixture {fixture} produced incomplete sample timetable"
        )
    duration_ms = max(
        int(sample["pts_ms"]) + int(sample["duration_ms"]) for sample in samples
    )
    meta = {
        "mp4_bytes": out.read_bytes(),
        "frame_timetable": probe_video_frame_timetable(out),
        "samples": samples,
        "codec_string": codec,
        "description": description,
        "width": FRAME_W,
        "height": FRAME_H,
        "fps": FRAME_FPS,
        "frame_count": spec["frames"],
        "duration_ms": duration_ms,
    }
    if fixture == "h264-issue-context" and include_chunks:
        # Frame-count transcode keeps B-frame packet overlap out of chunk boundaries.
        meta["chunk_size_frames"] = chunk_size_frames
        meta["chunks"] = _generate_chunks(
            out,
            Path(tmpdir),
            spec["frames"],
            FRAME_FPS,
            chunk_size_frames,
            generation_mode="transcode",
        )
    return meta


def _generate_chunks(
    source: Path,
    root: Path,
    frame_count: int,
    fps: int,
    chunk_size_frames: int,
    *,
    generation_mode: Literal["smart_copy", "transcode"],
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for chunk_id, start_frame in enumerate(range(0, frame_count, chunk_size_frames)):
        chunk_frames = min(chunk_size_frames, frame_count - start_frame)
        start_ms = round(start_frame / fps * 1000)
        duration_ms = round(chunk_frames / fps * 1000)
        chunk_path = root / f"chunk-{chunk_id:04d}.mp4"
        if generation_mode == "smart_copy":
            extract_video_chunk_smart_copy(source, chunk_path, start_ms, duration_ms)
        else:
            # Seek just before fractional frame timestamps so rounding cannot drop the first frame.
            extract_video_chunk(
                source, chunk_path, int(start_frame / fps * 1000), chunk_frames
            )
        samples = probe_chunk_samples(chunk_path, start_frame)
        codec, description = _extract_decoder_config(chunk_path)
        expected_indexes = list(range(start_frame, start_frame + chunk_frames))
        if (
            not codec
            or not description
            or sorted(int(sample["frame_index"]) for sample in samples)
            != expected_indexes
        ):
            raise RuntimeError(
                f"webcodecs fixture chunk {chunk_id} did not preserve "
                f"the {chunk_frames}-frame contract"
            )
        chunks.append(
            {
                "chunk_id": chunk_id,
                "generation_mode": generation_mode,
                "start_frame": start_frame,
                "end_frame": start_frame + chunk_frames - 1,
                "start_pts_ms": start_ms,
                "end_pts_ms": start_ms + duration_ms,
                "bytes": chunk_path.read_bytes(),
                "samples": samples,
                "codec_string": codec,
                "description": description,
            }
        )
    return chunks


def generate_qualification_fixture(
    fixture: str, tmpdir: str | Path, *, include_chunks: bool = True
) -> dict[str, Any]:
    """生成 61 秒 benchmark 源视频和 production-sized ready chunks。"""
    spec = QUALIFICATION_FIXTURE_SPECS[fixture]
    root = Path(tmpdir)
    source = root / "source.mp4"
    frame_count = int(spec["fps"]) * 61
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={spec['width']}x{spec['height']}:rate={spec['fps']}",
        "-frames:v",
        str(frame_count),
        "-an",
        "-c:v",
        "libx264",
        "-profile:v",
        "high",
        "-g",
        "30",
        "-keyint_min",
        "30",
        "-sc_threshold",
        "0",
        "-bf",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "ultrafast",
        "-b:v",
        str(spec["bitrate"]),
        "-maxrate",
        str(spec["bitrate"]),
        "-bufsize",
        str(spec["bitrate"]),
        "-movflags",
        "+faststart",
        str(source),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"webcodecs qualification fixture {fixture} encode timed out"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip()
            or f"webcodecs qualification fixture {fixture} encode failed"
        )

    chunks = (
        _generate_chunks(
            source,
            root,
            frame_count,
            spec["fps"],
            QUALIFICATION_CHUNK_SIZE_FRAMES,
            generation_mode="smart_copy",
        )
        if include_chunks
        else []
    )
    return {
        "mp4_bytes": source.read_bytes(),
        "frame_timetable": probe_video_frame_timetable(source),
        "chunks": chunks,
        "width": spec["width"],
        "height": spec["height"],
        "fps": spec["fps"],
        "frame_count": frame_count,
        "duration_ms": 61_000,
    }


def _real_fixture_name(fixture: str) -> str:
    """unsupported / malformed 复用 baseline 真实编码,再篡改 metadata。"""
    if fixture in ("unsupported-config", "malformed-samples"):
        return "h264-baseline-gop12"
    return fixture


def apply_metadata_mutation(fixture: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Pure response-fault helper; never persist this as an immutable output."""
    if fixture == "unsupported-config":
        # 真实 avcC description 保留,codec string 改成浏览器必拒的值 → isConfigSupported false。
        meta = {**meta, "codec_string": "avc1.ffff00"}
    elif fixture == "malformed-samples":
        # 把第二帧的字节偏移顶到 chunk 之外 → 前端切片越界 → demux fallback。
        mutated = [dict(s) for s in meta["samples"]]
        if len(mutated) > 1:
            mutated[1] = {**mutated[1], "offset_in_chunk": 10**9, "size_bytes": 10**9}
        meta = {**meta, "samples": mutated}
    return meta


def frame_expectations(
    fixture: str, samples: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """返回每帧的目标像素签名(视频像素空间),供 Playwright 像素采样判定。"""
    spec = FIXTURE_SPECS[_real_fixture_name(fixture)]
    sample_by_frame = {
        int(sample["frame_index"]): {
            "pts_ms": int(sample["pts_ms"]),
            "duration_ms": int(sample["duration_ms"]),
            "is_keyframe": bool(sample["is_keyframe"]),
            "decode_index": decode_index,
        }
        for decode_index, sample in enumerate(samples or [])
    }
    frames: list[dict[str, Any]] = []
    for i in range(spec["frames"]):
        bg = _background_luma(i)
        bits = [(i >> j) & 1 for j in range(4)]
        frames.append(
            {
                "frame_index": i,
                "background_luma": bg,
                "corner_bits": bits,
                **(
                    {"center_bits": [(i >> bit) & 1 for bit in range(8)]}
                    if spec.get("center_code")
                    else {}
                ),
                **sample_by_frame.get(i, {}),
            }
        )
    return {
        "scene_id": fixture,
        "response_fault": (
            {"target": "chunk-samples", "mutation": fixture}
            if fixture in {"unsupported-config", "malformed-samples"}
            else None
        ),
        "scene_color": spec["scene"],
        "width": FRAME_W,
        "height": FRAME_H,
        "fps": FRAME_FPS,
        "frame_count": spec["frames"],
        "sample_regions": {
            "background": {**_BG_NORM, "kind": "luma"},
            "corners": [{**b, "kind": "bit"} for b in _BIT_NORM],
            **(
                {
                    "center_bits": [
                        {
                            "bit": cell["bit"],
                            "x": cell["x"] / FRAME_W,
                            "y": cell["y"] / FRAME_H,
                            "w": _CENTER_BIT_EDGE / FRAME_W,
                            "h": _CENTER_BIT_EDGE / FRAME_H,
                            "kind": "bit",
                        }
                        for cell in _CENTER_BIT_PIXELS
                    ]
                }
                if spec.get("center_code")
                else {}
            ),
        },
        "frames": frames,
    }
