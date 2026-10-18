"""Pure ffprobe/FFmpeg media generation for accepted video work."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Protocol, Sequence

from aap_assets_media.contract import (
    Artifact,
    ChunkResult,
    ChunkSpec,
    FrameEntry,
    FrameResult,
    FrameSpec,
    MediaClaim,
    MediaFailure,
    MediaSource,
    MetadataResult,
    MetadataSpec,
    ObjectIdentity,
    TimetableSpec,
    WorkResult,
)
from aap_media_probe.probe import (
    FFProbeError,
    FFPROBE_TIMEOUT_SECONDS,
    probe_chunk_samples,
    probe_video_file,
    probe_video_frame_timetable,
)

from .storage import MediaStorage

FFMPEG_POSTER_TIMEOUT_SECONDS = 60
FFMPEG_TRANSCODE_TIMEOUT_SECONDS = 600
FFMPEG_CHUNK_TIMEOUT_SECONDS = 180
FFMPEG_FRAME_TIMEOUT_SECONDS = 60
BROWSER_PLAYABLE_VIDEO_CODECS = {"h264", "avc1"}
SMART_COPY_VIDEO_CODECS = {"h264", "avc1", "hevc", "h265"}


class MediaProcessingError(RuntimeError):
    """A stable processing error safe to persist on the accepted work row."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code[:64]
        self.detail = (detail or code)[:2000]
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ProcessedArtifact:
    artifact: Artifact
    path: Path


@dataclass(frozen=True, slots=True)
class ProcessedWork:
    result: WorkResult
    artifacts: tuple[ProcessedArtifact, ...] = ()
    timetable_rows: tuple[FrameEntry, ...] = ()


class MediaProbe(Protocol):
    def probe_video_file(self, path: str | Path) -> dict[str, Any]: ...

    def probe_video_frame_timetable(self, path: str | Path) -> list[dict[str, Any]]: ...

    def probe_chunk_samples(
        self, path: str | Path, start_frame: int
    ) -> list[dict[str, Any]]: ...


def _run_ffmpeg(args: list[str], *, timeout: int, error: str) -> None:
    try:
        process = subprocess.run(
            ["ffmpeg", "-y", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaProcessingError("timeout", f"{error} timed out") from exc
    if process.returncode != 0:
        raise MediaProcessingError(
            "media_processing_failed", process.stderr.strip() or error
        )


def extract_video_frame_image(
    input_path: str | Path,
    output_path: str | Path,
    frame_index: int,
    width: int,
) -> None:
    """Select the decoded source ordinal without rounded or interpolated seeks."""
    _run_ffmpeg(
        [
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-vf",
            f"select='eq(n,{max(0, frame_index)})',scale='min({max(1, width)},iw)':-2",
            "-fps_mode",
            "vfr",
            str(output_path),
        ],
        timeout=FFMPEG_FRAME_TIMEOUT_SECONDS,
        error="ffmpeg frame extraction",
    )


def extract_video_chunk(
    input_path: str | Path,
    output_path: str | Path,
    start_frame: int,
    frame_count: int,
) -> None:
    """Transcode the exact decoded source ordinal interval, preserving VFR gaps."""
    _run_ffmpeg(
        [
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-vf",
            f"select='between(n,{start_frame},{start_frame + max(1, frame_count) - 1})',setpts=PTS-STARTPTS",
            "-fps_mode",
            "vfr",
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
        timeout=FFMPEG_CHUNK_TIMEOUT_SECONDS,
        error="ffmpeg chunk extraction",
    )


def extract_video_chunk_smart_copy(
    input_path: str | Path,
    output_path: str | Path,
    start_ms: int,
    duration_ms: int,
) -> None:
    """Copy a keyframe-aligned source interval without changing its codec."""
    _run_ffmpeg(
        [
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
        timeout=FFMPEG_CHUNK_TIMEOUT_SECONDS,
        error="ffmpeg chunk smart-copy",
    )


def transcode_video_for_browser(
    input_path: str | Path, output_path: str | Path
) -> None:
    _run_ffmpeg(
        [
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        timeout=FFMPEG_TRANSCODE_TIMEOUT_SECONDS,
        error="ffmpeg browser transcode",
    )


def _parse_int(value: Any) -> int | None:
    if value in (None, "N/A"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def derive_anchor_frames(frame_count: int, stride: int) -> list[int]:
    """Return the stride grid plus the last source frame for a right boundary."""
    if frame_count <= 0:
        return []
    stride = max(1, int(stride))
    anchors = list(range(0, frame_count, stride))
    last = frame_count - 1
    if anchors[-1] != last:
        anchors.append(last)
    return anchors


def select_sparse_anchor_rows(
    rows: list[dict[str, Any]], stride: int
) -> list[dict[str, Any]]:
    """Keep source frame numbers, stride anchors, endpoints and every keyframe."""
    stride = max(1, int(stride))
    if stride == 1 or not rows:
        return rows
    frame_count = max(row["frame_index"] for row in rows) + 1
    anchors = set(derive_anchor_frames(frame_count, stride))
    return [
        row for row in rows if row["frame_index"] in anchors or row.get("is_keyframe")
    ]


def resolve_pts_ms_sparse(
    frame_index: int,
    anchors: Sequence[FrameEntry | tuple[int, int]],
    fps: float | None,
    stride: int,
) -> int | None:
    """Resolve a source frame's exact anchor or interpolate between anchors."""
    _ = stride
    points = [
        (row.frame_index, row.pts_ms) if isinstance(row, FrameEntry) else row
        for row in anchors
    ]
    if not points:
        if not fps or not math.isfinite(fps) or fps <= 0:
            return None
        return int(round((frame_index / fps) * 1000))

    first_frame, first_pts = points[0]
    last_frame, last_pts = points[-1]
    if frame_index <= first_frame:
        if frame_index == first_frame:
            return first_pts
        if not fps or not math.isfinite(fps) or fps <= 0:
            return None
        return int(round(first_pts + ((frame_index - first_frame) / fps) * 1000))
    if frame_index >= last_frame:
        if frame_index == last_frame:
            return last_pts
        if not fps or not math.isfinite(fps) or fps <= 0:
            return None
        return int(round(last_pts + ((frame_index - last_frame) / fps) * 1000))

    low_frame, low_pts = points[0]
    for current_frame, current_pts in points[1:]:
        if current_frame == frame_index:
            return current_pts
        if current_frame > frame_index:
            span = current_frame - low_frame
            if span <= 0:
                return low_pts
            ratio = (frame_index - low_frame) / span
            return int(round(low_pts + ratio * (current_pts - low_pts)))
        low_frame, low_pts = current_frame, current_pts
    return last_pts


def _normalize_codec(codec: str | None) -> str | None:
    if not codec:
        return None
    normalized = codec.lower().strip()
    if normalized in {"avc1", "h264"}:
        return "h264"
    if normalized in {"hevc", "h265"}:
        return "hevc"
    return normalized


def _transcode_fallback_reason(
    source_codec: str | None,
    keyframe_aligned: bool | None,
    has_start_timetable: bool,
) -> str | None:
    if not source_codec:
        return "missing_source_codec"
    if source_codec not in SMART_COPY_VIDEO_CODECS:
        return "unsupported_codec"
    if not has_start_timetable:
        return "missing_start_frame_timetable"
    if not keyframe_aligned:
        return "start_frame_not_keyframe"
    return None


def _chunk_duration_ms(
    start_pts_ms: int | None,
    end_pts_ms: int | None,
    start_frame: int,
    end_frame: int,
    fps: float | None,
) -> int:
    if start_pts_ms is not None and end_pts_ms is not None:
        frame_ms = int(round(1000 / fps)) if fps and fps > 0 else 1
        return max(1, end_pts_ms - start_pts_ms + frame_ms)
    if fps and fps > 0:
        return max(1, int(round(((end_frame - start_frame + 1) / fps) * 1000)))
    return 1


def _find_mp4_box_payload(data: bytes, fourcc: bytes) -> bytes | None:
    start = 0
    while True:
        index = data.find(fourcc, start)
        if index < 0:
            return None
        if index < 4:
            start = index + 4
            continue
        box_start = index - 4
        size = int.from_bytes(data[box_start:index], "big")
        if size > 8 and box_start + size <= len(data):
            return data[index + 4 : box_start + size]
        start = index + 4


def _avc_codec_string(avcc: bytes) -> str | None:
    return "avc1." + avcc[1:4].hex() if len(avcc) >= 4 else None


def _hevc_codec_string(hvcc: bytes) -> str | None:
    if len(hvcc) < 13:
        return None
    profile = hvcc[1]
    profile_space = (profile >> 6) & 0x03
    tier_flag = (profile >> 5) & 0x01
    profile_idc = profile & 0x1F
    compatibility = int.from_bytes(hvcc[2:6], "big")
    reversed_compatibility = 0
    for _ in range(32):
        reversed_compatibility = (reversed_compatibility << 1) | (compatibility & 1)
        compatibility >>= 1
    space = {0: "", 1: "A", 2: "B", 3: "C"}.get(profile_space, "")
    parts = [
        "hvc1",
        f"{space}{profile_idc}",
        format(reversed_compatibility, "x").upper() or "0",
        ("H" if tier_flag else "L") + str(hvcc[12]),
    ]
    for byte in hvcc[6:12].rstrip(b"\x00"):
        parts.append(format(byte, "02X"))
    return ".".join(parts)


def _extract_decoder_config(chunk_path: Path) -> tuple[str | None, str | None]:
    try:
        data = chunk_path.read_bytes()
    except OSError:
        return None, None
    avcc = _find_mp4_box_payload(data, b"avcC")
    if avcc is not None:
        codec = _avc_codec_string(avcc)
        return codec, base64.b64encode(avcc).decode("ascii")
    hvcc = _find_mp4_box_payload(data, b"hvcC")
    if hvcc is not None:
        codec = _hevc_codec_string(hvcc)
        return codec, base64.b64encode(hvcc).decode("ascii")
    return None, None


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _artifact(claim: MediaClaim, path: Path, filename: str) -> Artifact:
    if claim.output_prefix is None:
        raise MediaProcessingError(
            "invalid_response", "Claim omitted its output prefix"
        )
    if not path.is_file():
        raise MediaProcessingError(
            "media_processing_failed", "FFmpeg produced no artifact"
        )
    size, digest = _file_digest(path)
    return Artifact(key=claim.output_prefix + filename, byte_size=size, sha256=digest)


def _video_error(exc: Exception, operation: str) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        timeout = (
            FFPROBE_TIMEOUT_SECONDS if operation.startswith("ffprobe") else exc.timeout
        )
        return f"{operation} timed out after {timeout}s"
    if isinstance(exc, FFProbeError):
        return str(exc)[:2000]
    return (str(exc) or type(exc).__name__)[:2000]


def _frame_path(output_dir: Path, index: int, width: int, format_: str) -> Path:
    return output_dir / f"frame-{index}-{width}.{format_}"


def process_claim(
    claim: MediaClaim,
    source_path: Path,
    output_dir: Path,
    source_identity: ObjectIdentity,
    source_sha256: str,
    *,
    probe_client: MediaProbe | None = None,
) -> ProcessedWork:
    """Run one accepted non-backfill job and return its typed publication result."""
    if claim.source is None or claim.output_prefix is None:
        raise MediaProcessingError("invalid_request", "Claim has no video source")
    output_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(claim.spec, MetadataSpec):
        return _process_metadata(
            claim,
            source_path,
            output_dir,
            source_identity,
            source_sha256,
            probe_client,
        )
    if isinstance(claim.spec, TimetableSpec):
        return _process_timetable(
            claim, source_path, source_identity, source_sha256, probe_client
        )
    if isinstance(claim.spec, FrameSpec):
        return _process_frame(
            claim, source_path, output_dir, source_identity, source_sha256
        )
    if isinstance(claim.spec, ChunkSpec):
        return _process_chunk(
            claim,
            source_path,
            output_dir,
            source_identity,
            source_sha256,
            probe_client,
        )
    raise MediaProcessingError("invalid_request", "Unsupported media work kind")


def _process_metadata(
    claim: MediaClaim,
    source_path: Path,
    output_dir: Path,
    source_identity: ObjectIdentity,
    source_sha256: str,
    probe_client: MediaProbe | None,
) -> ProcessedWork:
    assert claim.source is not None and isinstance(claim.spec, MetadataSpec)
    spec = claim.spec
    patch: dict[str, Any] = {}
    clear_errors: list[str] = []
    artifacts: list[ProcessedArtifact] = []
    timetable_rows: tuple[FrameEntry, ...] = ()
    timetable_total: int | None = None
    metadata: dict[str, Any] = {}
    probe_error: str | None = None

    try:
        metadata = (
            probe_client.probe_video_file(source_path)
            if probe_client is not None
            else probe_video_file(source_path)
        )
        patch.update(metadata)
        clear_errors.append("probe_error")
    except Exception as exc:
        probe_error = _video_error(exc, "ffprobe")
        patch["probe_error"] = probe_error

    if spec.probe_timetable:
        if probe_error is not None:
            patch["frame_timetable_error"] = "Video metadata probe failed"
        else:
            try:
                rows = (
                    probe_client.probe_video_frame_timetable(source_path)
                    if probe_client is not None
                    else probe_video_frame_timetable(source_path)
                )
                timetable_rows = tuple(FrameEntry.model_validate(row) for row in rows)
                timetable_total = len(rows)
                patch["frame_timetable_frame_count"] = timetable_total
                clear_errors.append("frame_timetable_error")
            except Exception as exc:
                patch["frame_timetable_error"] = _video_error(
                    exc, "ffprobe frame timetable"
                )

    if spec.generate_playback and probe_error is None:
        source_codec = metadata.get("codec")
        normalized = _normalize_codec(source_codec)
        if normalized not in BROWSER_PLAYABLE_VIDEO_CODECS:
            playback_path = output_dir / "playback.mp4"
            try:
                transcode_video_for_browser(source_path, playback_path)
                artifact = _artifact(claim, playback_path, "playback.mp4")
                artifacts.append(ProcessedArtifact(artifact, playback_path))
                patch["playback_path"] = artifact.key
                patch["playback_codec"] = "h264"
                clear_errors.append("playback_error")
            except Exception as exc:
                patch["playback_error"] = _video_error(exc, "ffmpeg browser transcode")
        else:
            clear_errors.append("playback_error")

    if spec.generate_poster:
        poster_path = output_dir / "poster.webp"
        try:
            extract_video_frame_image(source_path, poster_path, 0, 512)
            artifact = _artifact(claim, poster_path, "poster.webp")
            artifacts.append(ProcessedArtifact(artifact, poster_path))
            patch["poster_frame_path"] = artifact.key
            clear_errors.append("poster_error")
        except Exception as exc:
            patch["poster_error"] = _video_error(exc, "ffmpeg poster extraction")

    result = MetadataResult(
        kind="video_metadata",
        source_identity=source_identity,
        source_sha256=source_sha256,
        metadata_patch=patch,
        clear_errors=list(dict.fromkeys(clear_errors)),
        width=metadata.get("width") if probe_error is None else None,
        height=metadata.get("height") if probe_error is None else None,
        timetable_total_frames=timetable_total,
        timetable_staged_count=(
            len(timetable_rows) if timetable_total is not None else None
        ),
        poster=(
            next((a.artifact for a in artifacts if a.path.name == "poster.webp"), None)
        ),
        playback=(
            next((a.artifact for a in artifacts if a.path.name == "playback.mp4"), None)
        ),
    )
    return ProcessedWork(result, tuple(artifacts), timetable_rows)


def _process_timetable(
    claim: MediaClaim,
    source_path: Path,
    source_identity: ObjectIdentity,
    source_sha256: str,
    probe_client: MediaProbe | None,
) -> ProcessedWork:
    assert claim.spec is not None and isinstance(claim.spec, TimetableSpec)
    try:
        full_rows = (
            probe_client.probe_video_frame_timetable(source_path)
            if probe_client is not None
            else probe_video_frame_timetable(source_path)
        )
        rows = select_sparse_anchor_rows(full_rows, claim.spec.sparse_stride)
        timetable = tuple(FrameEntry.model_validate(row) for row in rows)
        metadata_patch = {"frame_timetable_frame_count": len(full_rows)}
        clear_errors = ["frame_timetable_error"]
    except Exception as exc:
        timetable = ()
        metadata_patch = {
            "frame_timetable_error": _video_error(exc, "ffprobe frame timetable")
        }
        clear_errors = []
        full_rows = []
    result = MetadataResult(
        kind="video_timetable",
        source_identity=source_identity,
        source_sha256=source_sha256,
        metadata_patch=metadata_patch,
        clear_errors=clear_errors,
        timetable_total_frames=len(full_rows) if clear_errors else None,
        timetable_staged_count=len(timetable) if clear_errors else None,
    )
    return ProcessedWork(result, timetable_rows=timetable)


def _process_frame(
    claim: MediaClaim,
    source_path: Path,
    output_dir: Path,
    source_identity: ObjectIdentity,
    source_sha256: str,
) -> ProcessedWork:
    assert isinstance(claim.spec, FrameSpec)
    source = claim.source
    assert source is not None
    frame_index = claim.spec.frame_index
    path = _frame_path(output_dir, frame_index, claim.spec.width, claim.spec.format)
    extract_video_frame_image(source_path, path, frame_index, claim.spec.width)
    artifact = _artifact(claim, path, f"frame.{claim.spec.format}")
    return ProcessedWork(
        FrameResult(
            source_identity=source_identity,
            source_sha256=source_sha256,
            artifact=artifact,
        ),
        (ProcessedArtifact(artifact, path),),
    )


def _process_chunk(
    claim: MediaClaim,
    source_path: Path,
    output_dir: Path,
    source_identity: ObjectIdentity,
    source_sha256: str,
    probe_client: MediaProbe | None,
) -> ProcessedWork:
    assert isinstance(claim.spec, ChunkSpec)
    source = claim.source
    assert source is not None
    start_frame = claim.timing.start_frame
    end_frame = claim.timing.end_frame
    if start_frame is None or end_frame is None or end_frame < start_frame:
        raise MediaProcessingError("invalid_response", "Claim has invalid chunk bounds")

    metadata = source.video_metadata
    fps = metadata.get("fps")
    anchors = claim.timing.anchors
    start_pts = resolve_pts_ms_sparse(start_frame, anchors, fps, 1)
    end_pts = resolve_pts_ms_sparse(end_frame, anchors, fps, 1)
    source_codec = _normalize_codec(metadata.get("codec"))
    start_anchor = next(
        (row for row in anchors if row.frame_index == start_frame), None
    )
    fallback = _transcode_fallback_reason(
        source_codec,
        claim.timing.is_keyframe,
        start_anchor is not None,
    )
    diagnostics: dict[str, Any] = {
        "source_codec": source_codec,
        "output_codec": None,
        "keyframe_aligned": claim.timing.is_keyframe,
        "start_byte_offset": start_anchor.byte_offset if start_anchor else None,
        "end_byte_offset": next(
            (row.byte_offset for row in anchors if row.frame_index == end_frame), None
        ),
        "smart_copy_eligible": fallback is None,
        "fallback_reason": fallback,
    }

    path = output_dir / "chunk.mp4"
    generation_mode = "transcode"
    start_ms = start_pts or 0
    if fallback is None:
        try:
            extract_video_chunk_smart_copy(
                source_path,
                path,
                start_ms,
                _chunk_duration_ms(start_pts, end_pts, start_frame, end_frame, fps),
            )
            generation_mode = "smart_copy"
            diagnostics["output_codec"] = source_codec
        except Exception as exc:
            path.unlink(missing_ok=True)
            diagnostics["fallback_reason"] = (
                f"smart_copy_failed: {_video_error(exc, 'smart-copy')}"
            )
            diagnostics["smart_copy_eligible"] = False

    if generation_mode == "transcode":
        extract_video_chunk(
            source_path,
            path,
            start_frame,
            end_frame - start_frame + 1,
        )
        diagnostics["output_codec"] = "h264"

    sample_rows: list[dict[str, Any]] = []
    try:
        samples = (
            probe_client.probe_chunk_samples(path, start_frame)
            if probe_client is not None
            else probe_chunk_samples(path, start_frame)
        )
        if samples:
            codec_string, description = _extract_decoder_config(path)
            diagnostics["codec_string"] = codec_string or _webcodecs_codec_string(
                diagnostics["output_codec"]
            )
            diagnostics["width"] = metadata.get("width") or 0
            diagnostics["height"] = metadata.get("height") or 0
            if description:
                diagnostics["description"] = description
            sample_rows = samples
            diagnostics["samples"] = sample_rows
    except Exception:
        # Chunk playback remains usable if optional WebCodecs packet facts fail.
        pass

    artifact = _artifact(claim, path, "chunk.mp4")
    result = ChunkResult(
        source_identity=source_identity,
        source_sha256=source_sha256,
        artifact=artifact,
        generation_mode=generation_mode,
        diagnostics=diagnostics,
        start_pts_ms=start_pts,
        end_pts_ms=end_pts,
    )
    return ProcessedWork(result, (ProcessedArtifact(artifact, path),))


async def probe_source_timetable(
    source: MediaSource,
    storage: MediaStorage,
    *,
    sparse_stride: int = 1,
    probe_client: MediaProbe | None = None,
) -> list[FrameEntry]:
    """Probe an authorized source without publishing timetable rows or artifacts."""
    if (
        source.file_type != "video"
        or type(sparse_stride) is not int
        or not 1 <= sparse_stride <= 1_000_000
    ):
        raise MediaFailure("invalid_request")
    if source.source_drifted:
        raise MediaFailure("source_changed")
    identity = await storage.head_source(source.file_path)
    if source.object_identity is not None and identity != source.object_identity:
        raise MediaFailure("source_changed")
    captured = source.model_copy(update={"object_identity": identity})
    with tempfile.TemporaryDirectory(prefix="aap-video-timetable-") as temp:
        suffix = Path(source.file_name).suffix or ".mp4"
        path = Path(temp) / f"source{suffix}"
        await storage.download_source(captured, path)
        size, digest = _file_digest(path)
        if source.file_size is not None and size != source.file_size:
            raise MediaFailure("source_changed")
        if source.content_hash is not None and not _matches_source_hash(
            source.content_hash, path, digest
        ):
            raise MediaFailure("source_changed")
        probe_frames = (
            probe_client.probe_video_frame_timetable
            if probe_client is not None
            else probe_video_frame_timetable
        )
        rows = await asyncio.to_thread(probe_frames, path)
        if await storage.head_source(source.file_path) != identity:
            raise MediaFailure("source_changed")
        return [
            FrameEntry.model_validate(row)
            for row in select_sparse_anchor_rows(rows, sparse_stride)
        ]


def _matches_source_hash(expected: str, path: Path, sha256: str) -> bool:
    normalized = expected.strip().lower()
    prefix = ""
    if normalized.startswith("sha256:"):
        prefix, normalized = "sha256", normalized[7:]
    elif normalized.startswith("md5:"):
        prefix, normalized = "md5", normalized[4:]
    hexadecimal = all(character in "0123456789abcdef" for character in normalized)
    if len(normalized) == 64:
        return hexadecimal and normalized == sha256
    if len(normalized) == 32 and hexadecimal:
        digest = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return normalized == digest.hexdigest()
    return not prefix and len(normalized) not in {32, 64}


def _webcodecs_codec_string(output_codec: str | None) -> str:
    normalized = (output_codec or "").lower()
    if "hevc" in normalized or "h265" in normalized:
        return "hev1.1.6.L93.B0"
    return "avc1.4d001e"
