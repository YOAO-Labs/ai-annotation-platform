"""Authenticated synchronous client for the independent media-probe service."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from . import probe as local_probe
from .protocol import (
    PacketSample,
    ProbeFailure,
    StreamComplete,
    VideoFrame,
    VideoMetadata,
)

_MAX_RECORD_BYTES = 64 * 1024
_Operation = Literal["metadata", "frames", "packets"]


class MediaProbeError(RuntimeError):
    """The selected peer failed; callers must not replace its result locally."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _validated_base_url(value: str) -> str:
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
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise MediaProbeError(
            "invalid_configuration", "Invalid media-probe URL or service credential"
        )
    return value.rstrip("/")


def _validated_token(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) < 32
        or not value.isascii()
        or any(character.isspace() for character in value)
    ):
        raise MediaProbeError(
            "invalid_configuration", "Invalid media-probe URL or service credential"
        )
    return value


class MediaProbeClient:
    """Send local media files to one explicitly selected probe owner."""

    __slots__ = ("_base_url", "_token", "_timeout_seconds")

    def __init__(self, url: str, token: str, timeout_seconds: float):
        base_url = _validated_base_url(url)
        secret = _validated_token(token)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 3600
        ):
            raise MediaProbeError(
                "invalid_configuration", "Invalid media-probe URL or service credential"
            )
        self._base_url = base_url
        self._token = secret
        self._timeout_seconds = float(timeout_seconds)

    def __repr__(self) -> str:
        return f"MediaProbeClient(timeout_seconds={self._timeout_seconds:g})"

    def probe_video_file(self, path: str | Path) -> dict[str, Any]:
        result = self._remote_probe("metadata", path)
        assert isinstance(result, dict)
        return result

    def probe_video_frame_timetable(self, path: str | Path) -> list[dict[str, Any]]:
        result = self._remote_probe("frames", path)
        assert isinstance(result, list)
        return result

    def probe_chunk_samples(
        self, path: str | Path, start_frame: int
    ) -> list[dict[str, Any]]:
        result = self._remote_probe("packets", path, start_frame=start_frame)
        assert isinstance(result, list)
        return result

    def _small_body(self, response: httpx.Response) -> bytes:
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=16 * 1024):
            body.extend(chunk)
            if len(body) > _MAX_RECORD_BYTES:
                raise MediaProbeError(
                    "invalid_response", "Media-probe response too large"
                )
        return bytes(body)

    def _stream_rows(
        self,
        response: httpx.Response,
        operation: Literal["frames", "packets"],
        start_frame: int,
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
                        raise ValueError(
                            "Media-probe packet presentation ranks changed"
                        )
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
        self,
        operation: _Operation,
        path: str | Path,
        *,
        start_frame: int = 0,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        try:
            with Path(path).open("rb") as source:
                size = source.seek(0, 2)
                source.seek(0)
                with httpx.Client(
                    timeout=self._timeout_seconds,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    with client.stream(
                        "POST",
                        f"{self._base_url}/v1/{operation}",
                        params={"start_frame": start_frame}
                        if operation == "packets"
                        else None,
                        headers={
                            "Authorization": f"Bearer {self._token}",
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(size),
                        },
                        content=iter(lambda: source.read(64 * 1024), b""),
                    ) as response:
                        if response.status_code != 200:
                            failure = ProbeFailure.model_validate_json(
                                self._small_body(response), strict=True
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
                                self._small_body(response), strict=True
                            ).model_dump()
                        if media_type != "application/x-ndjson":
                            raise ValueError("Unexpected media-probe response type")
                        return self._stream_rows(response, operation, start_frame)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise MediaProbeError(
                "unavailable", "Media-probe service unavailable"
            ) from exc
        except ValueError as exc:
            raise MediaProbeError(
                "invalid_response", "Invalid media-probe response"
            ) from exc
