"""视频分块 ffmpeg 抽取(纯 subprocess 封装)。

从 workers/media.py 抽出:transcode 分块 extract_video_chunk 与 smart-copy
extract_video_chunk_smart_copy 及其超时常量。无 DB/存储依赖,不 import media.py
→ 无循环;media.py 经 re-export 保持旧入口与 monkeypatch target(
app.workers.media.extract_video_chunk*)不变 —— 调用方 _store_video_chunk 仍在
media.py 以裸名调用,走 media 模块全局查找。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.workers.media_process import run_process
from app.workers.media_timestamps import rounded_frame_seek_seconds

FFMPEG_CHUNK_TIMEOUT_SECONDS = 180


def extract_video_chunk(
    input_path: str | Path,
    output_path: str | Path,
    start_ms: int,
    frame_count: int,
    *,
    pass_fds: tuple[int, ...] = (),
) -> None:
    runner = run_process if pass_fds else subprocess.run
    proc = runner(
        [
            "ffmpeg",
            "-y",
            "-ss",
            rounded_frame_seek_seconds(start_ms),
            "-i",
            str(input_path),
            "-frames:v",
            str(max(1, frame_count)),
            "-an",
            "-c:v",
            "libx264",
            "-profile:v",
            "baseline",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-g",
            "30",
            "-keyint_min",
            "30",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=FFMPEG_CHUNK_TIMEOUT_SECONDS,
        **({"pass_fds": pass_fds} if pass_fds else {}),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg chunk extraction failed")


def extract_video_chunk_smart_copy(
    input_path: str | Path,
    output_path: str | Path,
    start_ms: int,
    duration_ms: int,
    *,
    pass_fds: tuple[int, ...] = (),
) -> None:
    runner = run_process if pass_fds else subprocess.run
    proc = runner(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{max(0, start_ms) / 1000:.3f}",
            "-i",
            str(input_path),
            "-t",
            f"{max(1, duration_ms) / 1000:.3f}",
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=FFMPEG_CHUNK_TIMEOUT_SECONDS,
        **({"pass_fds": pass_fds} if pass_fds else {}),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg chunk smart-copy failed")
