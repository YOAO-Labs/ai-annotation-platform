"""Bounded export-only metadata from the already verified original descriptor."""

import json
import math
import os
import selectors
import stat
import subprocess
import time
from types import SimpleNamespace

from app.services.content_snapshot import ContentError
from app.services import storage_access
from app.workers import media_process, media_probe

PROBE_CONTRACT = 1
MAX_SOURCE_BYTES = 5 * 1024**3
MAX_SOURCE_FRAMES = 1_000_000
MAX_OUTPUT_BYTES = 64 * 1024
PROBE_SECONDS = 300


def reject(code="invalid", status=422):
    raise ContentError("video_export_probe_" + code, status)


def _capture(command, descriptors):
    """Bound both pipes while retaining the inherited file until real process death.

    The existing decoder policy and settlement own the same native boundary as F.
    A streaming reader is necessary here: communicate(capture_output=True) does
    not impose an output budget, even when a later JSON parser rejects the data.
    """
    command = media_process._decoder_args(command, descriptors)
    process, failure, result = None, None, None
    try:
        try:
            process = subprocess.Popen(
                command,
                pass_fds=descriptors,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except BaseException as exc:
            raise media_process._unsettled(exc)
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + PROBE_SECONDS
        try:
            selector = selectors.DefaultSelector()
        except BaseException as exc:
            raise media_process._unsettled(exc)
        with storage_access.closing_registered(selector):
            for name in captured:
                selector.register(getattr(process, name), selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reject("timeout")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 16 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = captured[key.data]
                    if len(target) + len(chunk) > MAX_OUTPUT_BYTES:
                        reject("output_budget")
                    target.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reject("timeout")
            process.wait(timeout=remaining)
        if process.returncode != 0 or captured["stderr"]:
            # Native stderr can contain sensitive container metadata or paths.
            reject("failed")
        result = bytes(captured["stdout"])
    except BaseException as exc:
        failure = exc
    finally:
        if process is not None:
            closing = media_process._settle(process, failed=failure is not None)
            if closing is not None:
                if failure is None:
                    failure = closing
                elif getattr(closing, "storage_operation_unsettled", False):
                    media_process._unsettled(failure)
    if failure is not None:
        raise failure
    return result


def parse(payload):
    """Require ffprobe's decoded count; never accept nb_frames or fps*duration."""
    try:
        streams = payload["streams"]
        if not isinstance(streams, list) or len(streams) != 1:
            reject()
        stream = dict(streams[0])
        if any(type(stream.get(name)) is not int for name in ("width", "height")):
            reject()
        raw_count = stream.get("nb_read_frames")
        if (
            not isinstance(raw_count, str)
            or not raw_count.isascii()
            or not raw_count.isdecimal()
            or len(raw_count) > 10
        ):
            reject("frame_count")
        frame_count = int(raw_count)
        if not 0 < frame_count <= MAX_SOURCE_FRAMES:
            reject("frame_budget")
        stream["nb_frames"] = str(frame_count)
        info = media_probe.parse_ffprobe_video_metadata(
            {"streams": [stream], "format": payload.get("format") or {}}
        )
        if (
            type(info["fps"]) not in (float, int)
            or not math.isfinite(info["fps"])
            or info["fps"] <= 0
            or any(
                type(info[name]) is not int or info[name] <= 0
                for name in ("width", "height")
            )
            or (
                info["duration_ms"] is not None
                and (type(info["duration_ms"]) is not int or info["duration_ms"] < 0)
            )
            or not isinstance(info["codec"], str)
            or not 0 < len(info["codec"]) <= 128
        ):
            reject()
    except ContentError:
        raise
    except (TypeError, KeyError, ValueError, OverflowError, AttributeError):
        reject()
    return {"probe_contract": PROBE_CONTRACT, "video": info}


def inspect(file):
    """Borrow, never close, the verified stage; its caller owns permits and slots."""
    try:
        before = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= MAX_SOURCE_BYTES
        ):
            reject("source_budget")
        position = file.tell()
        path, descriptors = media_process.decoder_input(
            SimpleNamespace(file=file, size=before.st_size)
        )
        try:
            file.seek(0)
            raw = _capture(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-count_frames",
                    "-show_entries",
                    "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_read_frames,duration",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(path),
                ],
                descriptors,
            )
        finally:
            file.seek(position)
        after = os.fstat(file.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            reject("source_changed")
        return parse(json.loads(raw))
    except ContentError:
        raise
    except (ValueError, TypeError, OSError, OverflowError) as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise
        reject()
