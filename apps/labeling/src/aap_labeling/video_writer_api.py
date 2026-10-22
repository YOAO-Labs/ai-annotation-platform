"""Authenticated private HTTP surface for the Labeling video writer."""

from __future__ import annotations

import asyncio
import hmac
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .video_writer import VideoWriterManager
from .video_writer_config import VideoWriterSettings
from .video_writer_contract import (
    MAX_VIDEO_WRITER_REQUEST_BYTES,
    LABELING_VIDEO_WRITER_PROTOCOL,
    VideoWriterExecuteReply,
    VideoWriterFailure,
    VideoWriterInvalidRequest,
    VideoWriterRejected,
    VideoWriterStatusReply,
    VideoWriterUnavailable,
    collect_video_writer_mask_references,
    decode_video_writer_command,
    decode_video_writer_data_reply,
    decode_video_writer_status_request,
    decode_video_writer_publication_request,
    decode_video_writer_policy,
)
from .video_writer_postgres import VideoWriterPostgresProvider


class _PermanentMaskContentFailure(Exception):
    pass


def _error(code: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {
            "schema": LABELING_VIDEO_WRITER_PROTOCOL,
            "error": {"code": code},
        },
        status_code=status_code,
        headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else None,
    )


class AssetsMaskContentVerifier:
    """Verify every referenced Mask through the configured Assets byte owner."""

    def __init__(self, settings: VideoWriterSettings) -> None:
        self._settings = settings

    async def __call__(self, staged_result: dict[str, Any]) -> None:
        references = collect_video_writer_mask_references(staged_result)
        if not references:
            return
        try:
            origin, token = self._settings.read_mask_content_credentials()
        except (OSError, TypeError, ValueError):
            raise VideoWriterUnavailable() from None

        def verify() -> None:
            from aap_assets_mask_content.client import MaskContentClient
            from aap_assets_mask_content.contract import (
                GetRequest,
                MaskContentFailure,
            )

            try:
                with MaskContentClient(
                    origin,
                    token,
                    timeout_seconds=self._settings.assets_mask_content_timeout_seconds,
                ) as client:
                    for reference in references:
                        client.get(GetRequest(reference=reference))
            except MaskContentFailure as exc:
                if exc.code in {
                    "invalid_reference",
                    "object_missing",
                    "object_corrupt",
                    "content_changed",
                    "resource_limit",
                    "invalid_response",
                }:
                    raise _PermanentMaskContentFailure() from None
                raise

        try:
            await asyncio.to_thread(verify)
        except asyncio.CancelledError:
            raise
        except _PermanentMaskContentFailure:
            raise VideoWriterRejected("mask_content_invalid") from None
        except Exception:
            # Keep owner credentials and provider diagnostics out of this API.
            raise VideoWriterUnavailable() from None


async def _read_bounded(request: Request, *, limit: int | None = None) -> bytes | None:
    if limit is None:
        limit = MAX_VIDEO_WRITER_REQUEST_BYTES
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        if not declared_length.isascii() or not declared_length.isdigit():
            return None
        try:
            declared_size = int(declared_length)
        except ValueError:
            return None
        if declared_size > limit:
            return None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            return None
        body.extend(chunk)
    return bytes(body)


async def _policy(data: Any):
    policy_method = getattr(data, "policy", None)
    if callable(policy_method):
        return decode_video_writer_policy(await policy_method())
    async with data.transaction() as transaction:
        reply = decode_video_writer_data_reply(
            "policy", {}, await transaction.call("policy", {})
        )
        return decode_video_writer_policy(reply)


def create_video_writer_app(
    settings: VideoWriterSettings | None = None,
    *,
    data: Any | None = None,
    manager: VideoWriterManager | None = None,
    verify_masks=None,
) -> FastAPI:
    settings = settings or VideoWriterSettings()
    raw_control_token = settings.read_control_token()
    if (
        len(raw_control_token) < 32
        or not raw_control_token.isascii()
        or any(character.isspace() for character in raw_control_token)
    ):
        raise ValueError("Video-writer control token is invalid")
    control_token = raw_control_token.encode("ascii")
    if data is None:
        data = VideoWriterPostgresProvider(
            settings.read_database_url(),
            command_timeout=settings.timeout_seconds,
        )
    if verify_masks is None:
        verify_masks = AssetsMaskContentVerifier(settings)
    if manager is None:
        manager = VideoWriterManager(data, verify_masks=verify_masks)
    gate = asyncio.Semaphore(settings.max_concurrency)
    app = FastAPI(
        title="Labeling video-writer owner",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def authorized(request: Request) -> bool:
        authorization = request.headers.get("authorization", "")
        return authorization.startswith("Bearer ") and hmac.compare_digest(
            authorization[7:].encode("utf-8"), control_token
        )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with gate:
                    await _policy(data)
            return {"status": "ready"}
        except (VideoWriterFailure, OSError, TimeoutError):
            return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.post("/v1/labeling/video-writer/execute")
    async def execute(request: Request):
        if not authorized(request):
            return _error("unauthorized", 401)
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with gate:
                    raw = await _read_bounded(request)
                    if raw is None:
                        return _error("request_too_large", 413)
                    try:
                        command = decode_video_writer_command(raw)
                    except VideoWriterInvalidRequest:
                        return _error("invalid_request", 422)
                    receipt = await manager.execute(command)
            reply = VideoWriterExecuteReply(
                schema=LABELING_VIDEO_WRITER_PROTOCOL,
                receipt=receipt,
            )
            return JSONResponse(reply.model_dump(mode="json", by_alias=True))
        except VideoWriterInvalidRequest:
            return _error("invalid_request", 422)
        except VideoWriterRejected as exc:
            return _error(exc.code, 409)
        except (VideoWriterUnavailable, OSError, TimeoutError):
            return _error("unavailable", 503)
        except Exception:
            return _error("unavailable", 503)

    @app.post("/v1/labeling/video-writer/status")
    async def status(request: Request):
        if not authorized(request):
            return _error("unauthorized", 401)
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with gate:
                    raw = await _read_bounded(request, limit=16 * 1024)
                    if raw is None:
                        return _error("request_too_large", 413)
                    try:
                        status_request = decode_video_writer_status_request(raw)
                    except VideoWriterInvalidRequest:
                        publication = decode_video_writer_publication_request(raw)
                        receipt = await manager.publication(publication.execution)
                    else:
                        receipt = await manager.status(
                            status_request.operation_id,
                            status_request.execution,
                            status_request.request_digest,
                        )
            reply = VideoWriterStatusReply(
                schema=LABELING_VIDEO_WRITER_PROTOCOL,
                receipt=receipt,
            )
            return JSONResponse(reply.model_dump(mode="json", by_alias=True))
        except VideoWriterInvalidRequest:
            return _error("invalid_request", 422)
        except VideoWriterRejected as exc:
            return _error(exc.code, 409)
        except (VideoWriterUnavailable, OSError, TimeoutError):
            return _error("unavailable", 503)
        except Exception:
            return _error("unavailable", 503)

    return app
