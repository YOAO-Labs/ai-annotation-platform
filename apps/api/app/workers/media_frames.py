"""Video frame extraction from a path or an inherited verified descriptor.

The media module re-exports these helpers for existing callers. Independent
output publication belongs to media_derivatives; these helpers never sign or
write storage objects.
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
