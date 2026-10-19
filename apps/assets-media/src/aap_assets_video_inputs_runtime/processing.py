"""Measure decoded ordinals and derive frames from verified retained video files."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path
from typing import Iterator

from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import values as v

# Exclude playlists and external protocols: all decoding must use retained bytes.
_INPUT_OPTIONS = (
    "-protocol_whitelist",
    "file,pipe",
    "-format_whitelist",
    "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv,asf",
)
_LINE_BYTES = 4096


@dataclass(frozen=True)
class VideoExecutionPolicy:
    max_source_bytes: int = 8 * 1024**3
    max_temp_bytes: int = 12 * 1024**3
    max_timebase_bytes: int = 64 * 1024**2
    max_dimension: int = 32768
    max_pixels: int = 300_000_000
    job_timeout_seconds: int = 1800
    lease_seconds: int = 2100

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("video execution budgets must be positive integers")
        if (
            self.max_source_bytes > self.max_temp_bytes
            or self.max_timebase_bytes > self.max_temp_bytes
            or not 60 <= self.lease_seconds <= 7200
            or self.lease_seconds <= self.job_timeout_seconds + 60
        ):
            raise ValueError("video execution budgets or lease are inconsistent")


@dataclass(frozen=True)
class ProbedVideo:
    video: v.VideoFacts
    entries_path: Path

    def entries(self) -> Iterator[v.FrameTimeEntry]:
        with self.entries_path.open("rb") as rows:
            while line := rows.readline(_LINE_BYTES + 1):
                if len(line) > _LINE_BYTES or not line.endswith(b"\n"):
                    raise c.VideoInputFailure("timebase_incomplete")
                yield v.FrameTimeEntry.model_validate_json(line)

    def pages(self) -> Iterator[tuple[v.FrameTimeEntry, ...]]:
        page = []
        for entry in self.entries():
            page.append(entry)
            if len(page) == v.PAGE_SIZE:
                yield tuple(page)
                page.clear()
        if page:
            yield tuple(page)


def _milliseconds(raw: str) -> int:
    try:
        seconds = Decimal(raw)
        if not seconds.is_finite():
            raise ValueError
        milliseconds = seconds * 1000
        if abs(milliseconds) > v.SAFE_INTEGER:
            raise ValueError
        return int(milliseconds.to_integral_value(rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, ValueError, TypeError):
        raise c.VideoInputFailure("timebase_incomplete") from None


def decoded_frame_entry(
    line: bytes, frame_index: int, sampling: v.FrameSampling
) -> v.FrameTimeEntry | None:
    """Missing selected PTS fails; an unselected missing PTS never renumbers n."""
    try:
        fields = dict(
            part.split("=", 1)
            for part in line.decode("ascii").strip().split("|")
            if part
        )
        if fields.get("key_frame") not in {"0", "1"}:
            raise ValueError
        if (
            frame_index % sampling.step
            or frame_index // sampling.step >= sampling.max_frames
        ):
            return None
        raw_pts = next(
            (
                fields[key]
                for key in (
                    "best_effort_timestamp_time",
                    "pkt_pts_time",
                    "pts_time",
                )
                if fields.get(key) not in {None, "N/A"}
            ),
            None,
        )
        if raw_pts is None:
            raise c.VideoInputFailure("timebase_incomplete")
        return v.FrameTimeEntry(
            ordinal=frame_index // sampling.step + 1,
            frame_index=frame_index,
            pts_ms=_milliseconds(raw_pts),
            is_keyframe=fields["key_frame"] == "1",
        )
    except c.VideoInputFailure:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise c.VideoInputFailure("processing_failed") from None


async def _drain_errors(stream) -> bool:
    found = False
    while chunk := await stream.read(_LINE_BYTES):
        found |= bool(chunk.strip())
    return found


async def _stop(process):
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def _read_document(stream) -> bytes:
    data = bytearray()
    while chunk := await stream.read(min(_LINE_BYTES, 65537 - len(data))):
        data.extend(chunk)
        if len(data) > 65536:
            raise c.VideoInputFailure("processing_failed")
    return bytes(data)


async def _metadata(path: Path, policy: VideoExecutionPolicy) -> tuple[int, int, int]:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        *_INPUT_OPTIONS,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration:format=duration",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    errors = asyncio.create_task(_drain_errors(process.stderr))
    try:
        async with asyncio.timeout(min(60, policy.job_timeout_seconds)):
            data = await _read_document(process.stdout)
            if await process.wait() != 0 or await errors:
                raise c.VideoInputFailure("processing_failed")
        payload = json.loads(data)
        streams = payload.get("streams")
        if not streams:
            raise c.VideoInputFailure("not_video")
        width, height = streams[0]["width"], streams[0]["height"]
        if (
            type(width) is not int
            or type(height) is not int
            or min(width, height) < 1
            or max(width, height) > policy.max_dimension
            or width * height > policy.max_pixels
        ):
            raise c.VideoInputFailure("resource_limit")
        duration = next(
            (
                value
                for value in (
                    streams[0].get("duration"),
                    payload.get("format", {}).get("duration"),
                )
                if value not in {None, "N/A"}
            ),
            None,
        )
        duration_ms = _milliseconds(duration)
        if duration_ms < 0:
            raise c.VideoInputFailure("processing_failed")
        return width, height, duration_ms
    except TimeoutError:
        raise c.VideoInputFailure("timeout") from None
    except (ValueError, TypeError, KeyError, IndexError):
        raise c.VideoInputFailure("processing_failed") from None
    finally:
        await _stop(process)
        with suppress(asyncio.CancelledError):
            await errors


async def probe_retained_video(
    path: Path,
    sampling: v.FrameSampling,
    entries_path: Path,
    *,
    policy: VideoExecutionPolicy | None = None,
) -> ProbedVideo:
    """Stream every decoded row, spooling only bounded selected time entries."""
    policy = policy or VideoExecutionPolicy()
    if not 0 < path.stat().st_size <= policy.max_source_bytes:
        raise c.VideoInputFailure("resource_limit")
    width, height, duration = await _metadata(path, policy)
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        *_INPUT_OPTIONS,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        "frame=key_frame,best_effort_timestamp_time,pkt_pts_time,pts_time:frame_side_data=",
        "-of",
        "compact=p=0:nk=0",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_LINE_BYTES,
    )
    errors = asyncio.create_task(_drain_errors(process.stderr))
    created, complete = False, False
    try:
        async with asyncio.timeout(policy.job_timeout_seconds):
            with entries_path.open("xb") as output:
                created = True
                count, written = 0, 0
                while line := await process.stdout.readline():
                    if not line.strip():
                        continue
                    entry = decoded_frame_entry(line, count, sampling)
                    count += 1
                    if count > v.SAFE_INTEGER:
                        raise c.VideoInputFailure("resource_limit")
                    if entry is not None:
                        row = c.json_bytes(entry) + b"\n"
                        written += len(row)
                        if written > policy.max_timebase_bytes:
                            raise c.VideoInputFailure("resource_limit")
                        output.write(row)
                if await process.wait() != 0 or await errors or count == 0:
                    raise c.VideoInputFailure("processing_failed")
            entries_path.chmod(0o400)
            result = ProbedVideo(
                v.VideoFacts(
                    frame_count=count, width=width, height=height, duration_ms=duration
                ),
                entries_path,
            )
            complete = True
            return result
    except TimeoutError:
        raise c.VideoInputFailure("timeout") from None
    except (ValueError, TypeError, UnicodeError):
        raise c.VideoInputFailure("processing_failed") from None
    finally:
        await _stop(process)
        with suppress(asyncio.CancelledError):
            await errors
        if created and not complete:
            entries_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ProcessedFrame:
    path: Path
    byte_size: int
    sha256: str


def _finish_frame(path: Path, intermediate: Path, *, limit: int) -> ProcessedFrame:
    if intermediate != path:
        from PIL import Image

        with Image.open(intermediate) as image:
            image.save(path, format="WEBP", quality=80)
    size, digest = 0, hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise c.VideoInputFailure("resource_limit")
            digest.update(chunk)
    if not size:
        raise c.VideoInputFailure("processing_failed")
    path.chmod(0o400)
    return ProcessedFrame(path, size, digest.hexdigest())


async def extract_retained_frame(
    source_path: Path,
    output_path: Path,
    plan: v.FrozenFramePlan,
    entry: v.FrameTimeEntry,
    *,
    policy: VideoExecutionPolicy | None = None,
) -> ProcessedFrame:
    """The caller downloads and verifies plan.input.blob before invoking this."""
    policy = policy or VideoExecutionPolicy()
    if (
        entry.ordinal > plan.selected_count
        or entry.frame_index != (entry.ordinal - 1) * plan.sampling.step
        or source_path.stat().st_size != plan.input.blob.byte_size
    ):
        raise c.VideoInputFailure("input_corrupt")
    if output_path.exists():
        raise c.VideoInputFailure("invalid_request")
    intermediate = (
        output_path.with_suffix(".png")
        if plan.sampling.format == "webp"
        else output_path
    )
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-n",
        *_INPUT_OPTIONS,
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-vf",
        f"select='eq(n,{entry.frame_index})',scale='min({plan.sampling.width},iw)':-2",
        "-fps_mode",
        "vfr",
        str(intermediate),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    errors = asyncio.create_task(_drain_errors(process.stderr))
    complete = False
    try:
        async with asyncio.timeout(min(60, policy.job_timeout_seconds)):
            if await process.wait() != 0 or await errors:
                raise c.VideoInputFailure("processing_failed")
        task = asyncio.create_task(
            asyncio.to_thread(
                _finish_frame,
                output_path,
                intermediate,
                limit=policy.max_temp_bytes,
            )
        )
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await task
            raise
        complete = True
        return result
    except TimeoutError:
        raise c.VideoInputFailure("timeout") from None
    except (ValueError, OSError):
        raise c.VideoInputFailure("processing_failed") from None
    finally:
        await _stop(process)
        with suppress(asyncio.CancelledError):
            await errors
        if intermediate != output_path:
            intermediate.unlink(missing_ok=True)
        if not complete:
            output_path.unlink(missing_ok=True)
