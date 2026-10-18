"""Standalone HTTP adapter around the pure ffprobe helpers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from . import probe
from .protocol import (
    MAX_SAFE_INTEGER,
    PacketSample,
    PacketSampleRecord,
    ProbeFailure,
    StreamComplete,
    VideoFrame,
    VideoFrameRecord,
    VideoMetadata,
)

logger = logging.getLogger(__name__)
DEFAULT_MAX_FILE_BYTES = 1 << 30
MAX_NDJSON_LINE_BYTES = 64 * 1024
_START_FRAME_RE = re.compile(r"[0-9]+\Z")


class ProbeFault(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.failure = ProbeFailure(code=code, message=message)
        super().__init__(message)


def _error_responses() -> dict[int, dict[str, Any]]:
    return {
        400: {"model": ProbeFailure, "description": "Invalid request"},
        401: {"model": ProbeFailure, "description": "Invalid service token"},
        413: {"model": ProbeFailure, "description": "Uploaded file exceeds the limit"},
        422: {"model": ProbeFailure, "description": "Media could not be probed"},
        500: {"model": ProbeFailure, "description": "Unexpected probe failure"},
        504: {"model": ProbeFailure, "description": "ffprobe timed out"},
    }


def _parse_token(value: str | None) -> str:
    if (
        value is None
        or len(value) < 32
        or not value.isascii()
        or any(c.isspace() for c in value)
    ):
        raise ValueError(
            "MEDIA_PROBE_TOKEN must contain at least 32 non-whitespace ASCII characters"
        )
    return value


def _parse_limit(value: int | None) -> int:
    if value is None:
        raw = os.environ.get("MEDIA_PROBE_MAX_FILE_BYTES")
        if raw is None:
            return DEFAULT_MAX_FILE_BYTES
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(
                "MEDIA_PROBE_MAX_FILE_BYTES must be a positive integer"
            ) from exc
    if isinstance(value, bool) or value <= 0:
        raise ValueError("MEDIA_PROBE_MAX_FILE_BYTES must be a positive integer")
    return value


def _auth_token(request: Request, expected: str) -> None:
    raw = request.headers.get("authorization", "")
    scheme, separator, presented = raw.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not presented
        or any(c.isspace() for c in presented)
        or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise ProbeFault(401, "invalid_request", "A valid bearer token is required")


async def _save_upload(request: Request, max_file_bytes: int) -> str:
    content_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type != "application/octet-stream":
        raise ProbeFault(
            400, "invalid_request", "Content-Type must be application/octet-stream"
        )

    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        if not raw_length.isdecimal():
            raise ProbeFault(
                400, "invalid_request", "Content-Length must be a non-negative integer"
            )
        if int(raw_length) > max_file_bytes:
            raise ProbeFault(
                413, "file_too_large", "Uploaded file exceeds the configured limit"
            )

    fd, path = tempfile.mkstemp(prefix="aap-media-probe-", suffix=".media")
    size = 0
    try:
        with os.fdopen(fd, "wb") as upload:
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_file_bytes:
                    raise ProbeFault(
                        413,
                        "file_too_large",
                        "Uploaded file exceeds the configured limit",
                    )
                upload.write(chunk)
        if size == 0:
            raise ProbeFault(400, "invalid_request", "The uploaded file is empty")
    except ClientDisconnect as exc:
        Path(path).unlink(missing_ok=True)
        raise ProbeFault(
            400, "invalid_request", "The upload ended before the request completed"
        ) from exc
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    return path


async def _run_probe(
    request: Request,
    max_file_bytes: int,
    operation: Callable[..., Any],
    *args: Any,
) -> Any:
    path = await _save_upload(request, max_file_bytes)
    try:
        try:
            return await asyncio.to_thread(operation, path, *args)
        except subprocess.TimeoutExpired as exc:
            raise ProbeFault(
                504, "probe_timeout", "ffprobe exceeded its 30 second time limit"
            ) from exc
        except (probe.FFProbeError, ValueError, json.JSONDecodeError) as exc:
            raise ProbeFault(
                422, "probe_failed", "The uploaded media could not be probed"
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected media-probe failure")
            raise ProbeFault(
                500, "probe_failed", "Media probing failed unexpectedly"
            ) from exc
    finally:
        Path(path).unlink(missing_ok=True)


def _parse_start_frame(request: Request) -> int:
    values = request.query_params.getlist("start_frame")
    if len(values) != 1 or not _START_FRAME_RE.fullmatch(values[0]):
        raise ProbeFault(
            400, "invalid_request", "start_frame must be a non-negative safe integer"
        )
    value = int(values[0])
    if value > MAX_SAFE_INTEGER:
        raise ProbeFault(
            400, "invalid_request", "start_frame must be a non-negative safe integer"
        )
    return value


def _validate_stream(
    rows: Iterable[dict[str, Any]],
    record_type: type[VideoFrameRecord] | type[PacketSampleRecord],
    data_type: type[VideoFrame] | type[PacketSample],
    kind: str,
    start_frame: int = 0,
) -> int:
    count = 0
    packet_indices: set[int] = set()
    try:
        for count, row in enumerate(rows, start=1):
            record = record_type(type=kind, data=row)
            if (
                len(record.model_dump_json().encode("utf-8")) + 1
                > MAX_NDJSON_LINE_BYTES
            ):
                raise ProbeFault(
                    422, "probe_failed", "A probe result exceeded the record size limit"
                )
            if data_type is VideoFrame and record.data.frame_index != count - 1:
                raise ProbeFault(
                    422, "probe_failed", "The frame timetable is not contiguous"
                )
            if data_type is PacketSample:
                index = record.data.frame_index
                if index in packet_indices:
                    raise ProbeFault(
                        422,
                        "probe_failed",
                        "The packet sample contains duplicate frame ranks",
                    )
                packet_indices.add(index)
    except ValidationError as exc:
        raise ProbeFault(
            422, "probe_failed", "A probe result did not match the protocol"
        ) from exc
    if data_type is PacketSample:
        end_frame = start_frame + count
        if len(packet_indices) != count or any(
            index < start_frame or index >= end_frame for index in packet_indices
        ):
            raise ProbeFault(
                422, "probe_failed", "The packet sample frame ranks are incomplete"
            )
    completion = StreamComplete(type="complete", count=count, sha256="0" * 64)
    if len(completion.model_dump_json().encode("utf-8")) + 1 > MAX_NDJSON_LINE_BYTES:
        raise ProbeFault(
            422, "probe_failed", "A probe result exceeded the record size limit"
        )
    return count


async def _ndjson_response(
    rows: list[dict[str, Any]],
    record_type: type[VideoFrameRecord] | type[PacketSampleRecord],
    data_type: type[VideoFrame] | type[PacketSample],
    kind: str,
    start_frame: int = 0,
) -> StreamingResponse:
    count = await asyncio.to_thread(
        _validate_stream, rows, record_type, data_type, kind, start_frame
    )

    async def lines():
        digest = hashlib.sha256()
        for row in rows:
            record = record_type(type=kind, data=row)
            line = record.model_dump_json().encode("utf-8") + b"\n"
            digest.update(line)
            yield line
        yield (
            StreamComplete(type="complete", count=count, sha256=digest.hexdigest())
            .model_dump_json()
            .encode("utf-8")
            + b"\n"
        )

    return StreamingResponse(lines(), media_type="application/x-ndjson")


def create_app(
    *, token: str | None = None, max_file_bytes: int | None = None
) -> FastAPI:
    """Create the standalone media-probe app with only its own credentials/config."""
    expected_token = _parse_token(
        token if token is not None else os.environ.get("MEDIA_PROBE_TOKEN")
    )
    file_limit = _parse_limit(max_file_bytes)
    app = FastAPI(
        title="AAP Media Probe",
        version="0.1.0",
        description="Standalone metadata, frame timetable, and packet probing over raw media uploads.",
    )

    @app.exception_handler(ProbeFault)
    async def handle_probe_fault(_: Request, exc: ProbeFault) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.failure.model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled media-probe request failure", exc_info=exc)
        failure = ProbeFailure(
            code="probe_failed", message="Media probing failed unexpectedly"
        )
        return JSONResponse(status_code=500, content=failure.model_dump(mode="json"))

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/v1/metadata",
        operation_id="probeVideoMetadata",
        response_model=VideoMetadata,
        responses=_error_responses(),
        tags=["probe"],
    )
    async def metadata(request: Request) -> VideoMetadata:
        _auth_token(request, expected_token)
        result = await _run_probe(request, file_limit, probe.probe_video_file)
        try:
            return VideoMetadata.model_validate(result)
        except ValidationError as exc:
            raise ProbeFault(
                422, "probe_failed", "The metadata result did not match the protocol"
            ) from exc

    @app.post(
        "/v1/frames",
        operation_id="probeVideoFrameTimetable",
        response_class=StreamingResponse,
        responses={
            **_error_responses(),
            200: {
                "description": "NDJSON frame records followed by a completion record.",
                "content": {
                    "application/x-ndjson": {
                        "schema": {
                            "type": "string",
                            "format": "binary",
                            "x-ndjson-record-schemas": [
                                {"$ref": "#/components/schemas/VideoFrameRecord"},
                                {"$ref": "#/components/schemas/StreamComplete"},
                            ],
                        }
                    }
                },
            },
        },
        tags=["probe"],
    )
    async def frames(request: Request) -> StreamingResponse:
        _auth_token(request, expected_token)
        result = await _run_probe(
            request, file_limit, probe.probe_video_frame_timetable
        )
        return await _ndjson_response(result, VideoFrameRecord, VideoFrame, "frame")

    @app.post(
        "/v1/packets",
        operation_id="probeChunkPacketSamples",
        response_class=StreamingResponse,
        responses={
            **_error_responses(),
            200: {
                "description": "NDJSON packet records followed by a completion record.",
                "content": {
                    "application/x-ndjson": {
                        "schema": {
                            "type": "string",
                            "format": "binary",
                            "x-ndjson-record-schemas": [
                                {"$ref": "#/components/schemas/PacketSampleRecord"},
                                {"$ref": "#/components/schemas/StreamComplete"},
                            ],
                        }
                    }
                },
            },
        },
        tags=["probe"],
    )
    async def packets(request: Request) -> StreamingResponse:
        _auth_token(request, expected_token)
        start_frame = _parse_start_frame(request)
        result = await _run_probe(
            request, file_limit, probe.probe_chunk_samples, start_frame
        )
        return await _ndjson_response(
            result, PacketSampleRecord, PacketSample, "packet", start_frame
        )

    original_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original_openapi()
        components = schema.setdefault("components", {})
        schemas = components.setdefault("schemas", {})
        for model in (
            VideoMetadata,
            VideoFrame,
            PacketSample,
            StreamComplete,
            VideoFrameRecord,
            PacketSampleRecord,
            ProbeFailure,
        ):
            model_schema = model.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
            model_schema.pop("$defs", None)
            schemas[model.__name__] = model_schema
        components.setdefault("securitySchemes", {})["MediaProbeBearer"] = {
            "type": "http",
            "scheme": "bearer",
        }
        for path in ("/v1/metadata", "/v1/frames", "/v1/packets"):
            schema["paths"][path]["post"]["security"] = [{"MediaProbeBearer": []}]
            schema["paths"][path]["post"]["requestBody"] = {
                "required": True,
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
                "description": (
                    "Raw media bytes. The request is streamed to a private temporary file; "
                    "the configured maximum size is MEDIA_PROBE_MAX_FILE_BYTES."
                ),
            }
        schema["paths"]["/v1/packets"]["post"]["parameters"] = [
            {
                "name": "start_frame",
                "in": "query",
                "required": True,
                "description": "Non-negative safe integer used as the first presentation-rank offset.",
                "schema": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_SAFE_INTEGER,
                },
            }
        ]
        ndjson_description = (
            "Each record is compact UTF-8 JSON terminated by LF (\\n). Data records "
            "come first; the final record is {type: complete, count: N, sha256: H}. "
            "H is lowercase SHA-256 over the exact bytes of every preceding data line, "
            "including LF and excluding the completion line. An empty stream uses "
            "the SHA-256 digest of empty bytes. Clients must verify the final count "
            "and digest before accepting the response."
        )
        for path in ("/v1/frames", "/v1/packets"):
            response_schema = schema["paths"][path]["post"]["responses"]["200"][
                "content"
            ]["application/x-ndjson"]["schema"]
            response_schema["description"] = ndjson_description
            response_schema["x-requires-completion-record"] = True
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app
