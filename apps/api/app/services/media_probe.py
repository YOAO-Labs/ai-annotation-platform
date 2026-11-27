"""Bind the API's legacy probe functions to local or independent execution."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from yoao_media_probe import probe as local_probe
from yoao_media_probe.client import (
    MediaProbeClient,
    MediaProbeError,
    httpx as httpx,
)
from yoao_media_probe.probe import (
    FFPROBE_TIMEOUT_SECONDS as FFPROBE_TIMEOUT_SECONDS,
    FFProbeError as FFProbeError,
    _parse_frame_time_ms as _parse_frame_time_ms,
    _parse_int as _parse_int,
    _parse_ratio as _parse_ratio,
    parse_ffprobe_frame_timetable as parse_ffprobe_frame_timetable,
    parse_ffprobe_packet_samples as parse_ffprobe_packet_samples,
    parse_ffprobe_video_metadata as parse_ffprobe_video_metadata,
)

from app.config import settings


def _peer_url() -> str | None:
    value = settings.media_probe_url
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    token = settings.media_probe_token
    if (
        not valid
        or len(token) < 32
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise MediaProbeError(
            "invalid_configuration", "Invalid media-probe URL or service credential"
        )
    return value.rstrip("/")


def _remote_client() -> MediaProbeClient | None:
    peer = _peer_url()
    if peer is None:
        return None
    return MediaProbeClient(
        peer,
        settings.media_probe_token,
        settings.media_probe_timeout_seconds,
    )


def configured_probe_client() -> MediaProbeClient | None:
    """Return the selected remote client, or ``None`` for local probing."""
    return _remote_client()


def _run_verified_ffprobe(
    args: list[str], pass_fds: tuple[int, ...], error_message: str
):
    from app.services.media_process import run_process

    proc = run_process(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT_SECONDS,
        pass_fds=pass_fds,
    )
    if proc.returncode != 0:
        raise FFProbeError(proc.stderr.strip() or error_message)
    return json.loads(proc.stdout or "{}")


def probe_video_file(path: str | Path, *, pass_fds: tuple[int, ...] = ()) -> dict:
    client = _remote_client()
    if client is None:
        if pass_fds:
            payload = _run_verified_ffprobe(
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
                pass_fds,
                "ffprobe failed",
            )
            return parse_ffprobe_video_metadata(payload)
        return local_probe.probe_video_file(path, pass_fds=pass_fds)
    return client.probe_video_file(path)


def probe_video_frame_timetable(
    path: str | Path, *, pass_fds: tuple[int, ...] = ()
) -> list[dict]:
    client = _remote_client()
    if client is None:
        if pass_fds:
            payload = _run_verified_ffprobe(
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
                pass_fds,
                "ffprobe frame timetable failed",
            )
            return parse_ffprobe_frame_timetable(payload)
        return local_probe.probe_video_frame_timetable(path, pass_fds=pass_fds)
    return client.probe_video_frame_timetable(path)


def probe_chunk_samples(
    path: str | Path, start_frame: int, *, pass_fds: tuple[int, ...] = ()
) -> list[dict]:
    client = _remote_client()
    if client is None:
        if pass_fds:
            payload = _run_verified_ffprobe(
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
                pass_fds,
                "ffprobe packet probe failed",
            )
            return parse_ffprobe_packet_samples(payload, start_frame)
        return local_probe.probe_chunk_samples(path, start_frame, pass_fds=pass_fds)
    return client.probe_chunk_samples(path, start_frame)
