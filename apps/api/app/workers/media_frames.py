"""视频单帧 ffmpeg 抽取(纯 subprocess 封装)。

从 workers/media.py 抽出:单帧图片 extract_video_frame_image 及其超时常量
FFMPEG_FRAME_TIMEOUT_SECONDS。无 DB/存储依赖,不 import media.py → 无循环;
media.py 经 re-export 保持旧入口与 monkeypatch target(
app.workers.media.extract_video_frame_image)不变 —— 调用方 _store_frame_cache_image
仍在 media.py 以裸名调用,走 media 模块全局查找。常量亦被 media.py 留下的
poster 超时消息复用,故 media.py import 回自用并 re-export。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.workers.media_process import run_process
from app.workers.media_timestamps import rounded_frame_seek_seconds

FFMPEG_FRAME_TIMEOUT_SECONDS = 60


def extract_video_frame_image(
    input_path: str | Path,
    output_path: str | Path,
    pts_ms: int,
    width: int,
    *,
    pass_fds: tuple[int, ...] = (),
) -> None:
    runner = run_process if pass_fds else subprocess.run
    proc = runner(
        [
            "ffmpeg",
            "-y",
            "-ss",
            rounded_frame_seek_seconds(pts_ms),
            "-i",
            str(input_path),
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({max(1, width)},iw)':-2",
            str(output_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=FFMPEG_FRAME_TIMEOUT_SECONDS,
        **({"pass_fds": pass_fds} if pass_fds else {}),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffmpeg frame extraction failed")
