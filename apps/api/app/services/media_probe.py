"""Bounded ffprobe calls and video metadata, frame and packet parsing."""

import json
import math
import subprocess
from pathlib import Path
from typing import Any

from app.services.media_process import run_process

FFPROBE_TIMEOUT_SECONDS = 30


def _parse_ratio(value: str | None) -> float | None:
    if not value:
        return None
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            n = float(num)
            d = float(den)
            if d == 0:
                return None
            return n / d
        except ValueError:
            return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_ffprobe_video_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    streams = payload.get("streams") or []
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video_stream:
        raise ValueError("ffprobe did not return a video stream")

    fmt = payload.get("format") or {}
    fps = _parse_ratio(video_stream.get("avg_frame_rate")) or _parse_ratio(
        video_stream.get("r_frame_rate")
    )
    duration_s: float | None = None
    for raw in (video_stream.get("duration"), fmt.get("duration")):
        if raw is None:
            continue
        try:
            duration_s = float(raw)
            break
        except (TypeError, ValueError):
            continue

    frame_count: int | None = None
    raw_frames = video_stream.get("nb_frames")
    if raw_frames not in (None, "N/A"):
        try:
            frame_count = int(raw_frames)
        except (TypeError, ValueError):
            frame_count = None
    if frame_count is None and fps and duration_s:
        frame_count = max(1, int(round(fps * duration_s)))

    width = video_stream.get("width")
    height = video_stream.get("height")
    return {
        "duration_ms": int(round(duration_s * 1000))
        if duration_s is not None
        else None,
        "fps": round(float(fps), 3) if fps and math.isfinite(fps) else None,
        "frame_count": frame_count,
        "width": int(width) if width is not None else None,
        "height": int(height) if height is not None else None,
        "codec": video_stream.get("codec_name"),
    }


def _parse_frame_time_ms(frame: dict[str, Any]) -> int | None:
    for key in (
        "best_effort_timestamp_time",
        "pkt_pts_time",
        "pts_time",
        "pkt_dts_time",
    ):
        raw = frame.get(key)
        if raw in (None, "N/A"):
            continue
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds):
            return int(round(seconds * 1000))
    return None


def _parse_int(value: Any) -> int | None:
    if value in (None, "N/A"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_ffprobe_frame_timetable(payload: dict[str, Any]) -> list[dict[str, Any]]:
    frames = payload.get("frames") or []
    out: list[dict[str, Any]] = []
    for frame in frames:
        if frame.get("media_type") not in (None, "video"):
            continue
        pts_ms = _parse_frame_time_ms(frame)
        if pts_ms is None:
            continue
        out.append(
            {
                "frame_index": len(out),
                "pts_ms": pts_ms,
                "is_keyframe": bool(_parse_int(frame.get("key_frame")) or 0),
                "pict_type": frame.get("pict_type"),
                "byte_offset": _parse_int(frame.get("pkt_pos")),
            }
        )
    return out


def parse_ffprobe_packet_samples(
    payload: dict[str, Any], start_frame: int
) -> list[dict[str, Any]]:
    packets = payload.get("packets") or []
    valid: list[dict[str, Any]] = []
    for pkt in packets:
        pos = _parse_int(pkt.get("pos"))
        pts_time = pkt.get("pts_time")
        if pos is None or pts_time in (None, "N/A"):
            continue
        pts_ms = int(round(float(pts_time) * 1000))
        dur = pkt.get("duration_time")
        duration_ms = int(round(float(dur) * 1000)) if dur not in (None, "N/A") else 0
        size_bytes = _parse_int(pkt.get("size")) or 0
        flags = pkt.get("flags") or ""
        is_keyframe = "K" in flags
        valid.append(
            {
                "pts_ms": pts_ms,
                "duration_ms": duration_ms,
                "is_keyframe": is_keyframe,
                "size_bytes": size_bytes,
                "offset_in_chunk": pos,
            }
        )
    if not valid:
        return []
    order = sorted(range(len(valid)), key=lambda i: valid[i]["pts_ms"])
    rank = {idx: r for r, idx in enumerate(order)}
    for i in range(len(valid)):
        valid[i]["frame_index"] = start_frame + rank[i]
    return valid


def probe_video_file(
    path: str | Path, *, pass_fds: tuple[int, ...] = ()
) -> dict[str, Any]:
    runner = run_process if pass_fds else subprocess.run
    proc = runner(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames,duration",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT_SECONDS,
        **({"pass_fds": pass_fds} if pass_fds else {}),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffprobe failed")
    return parse_ffprobe_video_metadata(json.loads(proc.stdout or "{}"))


def probe_video_frame_timetable(
    path: str | Path, *, pass_fds: tuple[int, ...] = ()
) -> list[dict[str, Any]]:
    runner = run_process if pass_fds else subprocess.run
    proc = runner(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=media_type,key_frame,pict_type,best_effort_timestamp_time,pkt_pts_time,pts_time,pkt_dts_time,pkt_pos",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT_SECONDS,
        **({"pass_fds": pass_fds} if pass_fds else {}),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffprobe frame timetable failed")
    return parse_ffprobe_frame_timetable(json.loads(proc.stdout or "{}"))


def probe_chunk_samples(
    path: str | Path, start_frame: int, *, pass_fds: tuple[int, ...] = ()
) -> list[dict[str, Any]]:
    runner = run_process if pass_fds else subprocess.run
    proc = runner(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_entries",
            "packet=pts_time,dts_time,duration_time,size,pos,flags",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT_SECONDS,
        **({"pass_fds": pass_fds} if pass_fds else {}),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ffprobe packet probe failed")
    return parse_ffprobe_packet_samples(json.loads(proc.stdout or "{}"), start_frame)
