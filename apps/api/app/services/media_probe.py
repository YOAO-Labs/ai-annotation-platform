"""Bind media probing to its local implementation or independent HTTP owner."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from aap_media_probe import probe as local_probe
from aap_media_probe.protocol import (
    PacketSample,
    ProbeFailure,
    StreamComplete,
    VideoFrame,
    VideoMetadata,
)

from app.config import settings

_MAX_RECORD_BYTES = 64 * 1024
_Operation = Literal["metadata", "frames", "packets"]


class MediaProbeError(RuntimeError):
    """The selected peer failed; never replace its result with local work."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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


def _small_body(response: httpx.Response) -> bytes:
    body = bytearray()
    for chunk in response.iter_bytes(chunk_size=16 * 1024):
        body.extend(chunk)
        if len(body) > _MAX_RECORD_BYTES:
            raise MediaProbeError("invalid_response", "Media-probe response too large")
    return bytes(body)


def _stream_rows(
    response: httpx.Response, operation: Literal["frames", "packets"], start_frame: int
) -> list[dict[str, Any]]:
    buffer = bytearray()
    rows: list[dict[str, Any]] = []
    complete = False
    digest = hashlib.sha256()
    ranks: set[int] = set()
    record_type = "frame" if operation == "frames" else "packet"
    row_model = VideoFrame if operation == "frames" else PacketSample
    for chunk in response.iter_bytes(chunk_size=16 * 1024):
        buffer.extend(chunk)
        while (newline := buffer.find(b"\n")) >= 0:
            if newline + 1 > _MAX_RECORD_BYTES or complete:
                raise ValueError("Oversized or trailing media-probe stream record")
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("Media-probe record must be an object")
            if record.get("type") == "complete":
                footer = StreamComplete.model_validate(record, strict=True)
                if footer.count != len(rows):
                    raise ValueError("Media-probe stream count mismatch")
                if footer.sha256 != digest.hexdigest():
                    raise ValueError("Media-probe stream digest mismatch")
                if (
                    operation == "packets"
                    and ranks
                    and (
                        min(ranks) != start_frame
                        or max(ranks) != start_frame + len(rows) - 1
                    )
                ):
                    raise ValueError("Media-probe packet presentation ranks changed")
                complete = True
                continue
            if set(record) != {"type", "data"} or record["type"] != record_type:
                raise ValueError("Unexpected media-probe stream record")
            row = row_model.model_validate(record["data"], strict=True)
            if operation == "frames":
                if row.frame_index != len(rows):
                    raise ValueError("Media-probe frame order changed")
            else:
                if row.frame_index in ranks:
                    raise ValueError("Duplicate media-probe presentation rank")
                ranks.add(row.frame_index)
            rows.append(row.model_dump())
            digest.update(line + b"\n")
        if len(buffer) > _MAX_RECORD_BYTES:
            raise ValueError("Media-probe stream record too large")
    if buffer or not complete:
        raise ValueError("Incomplete media-probe stream")
    return rows


def _remote_probe(
    base_url: str, operation: _Operation, path: str | Path, *, start_frame: int = 0
) -> dict[str, Any] | list[dict[str, Any]]:
    try:
        with Path(path).open("rb") as source:
            size = source.seek(0, 2)
            source.seek(0)
            with httpx.Client(
                timeout=settings.media_probe_timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                with client.stream(
                    "POST",
                    f"{base_url}/v1/{operation}",
                    params={"start_frame": start_frame}
                    if operation == "packets"
                    else None,
                    headers={
                        "Authorization": f"Bearer {settings.media_probe_token}",
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(size),
                    },
                    content=iter(lambda: source.read(64 * 1024), b""),
                ) as response:
                    if response.status_code != 200:
                        failure = ProbeFailure.model_validate_json(
                            _small_body(response), strict=True
                        )
                        if failure.code == "probe_timeout":
                            raise subprocess.TimeoutExpired(
                                cmd=["ffprobe"],
                                timeout=local_probe.FFPROBE_TIMEOUT_SECONDS,
                            )
                        raise MediaProbeError(failure.code, failure.message)
                    media_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if operation == "metadata":
                        if media_type != "application/json":
                            raise ValueError("Unexpected media-probe response type")
                        return VideoMetadata.model_validate_json(
                            _small_body(response), strict=True
                        ).model_dump()
                    if media_type != "application/x-ndjson":
                        raise ValueError("Unexpected media-probe response type")
                    return _stream_rows(response, operation, start_frame)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise MediaProbeError("unavailable", "Media-probe service unavailable") from exc
    except ValueError as exc:
        raise MediaProbeError(
            "invalid_response", "Invalid media-probe response"
        ) from exc


def probe_video_file(path: str | Path) -> dict[str, Any]:
    peer = _peer_url()
    if peer is None:
        return local_probe.probe_video_file(path)
    return _remote_probe(peer, "metadata", path)


def probe_video_frame_timetable(path: str | Path) -> list[dict[str, Any]]:
    peer = _peer_url()
    if peer is None:
        return local_probe.probe_video_frame_timetable(path)
    return _remote_probe(peer, "frames", path)


def probe_chunk_samples(path: str | Path, start_frame: int) -> list[dict[str, Any]]:
    peer = _peer_url()
    if peer is None:
        return local_probe.probe_chunk_samples(path, start_frame)
    return _remote_probe(peer, "packets", path, start_frame=start_frame)
