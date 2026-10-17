"""Explicit public WebSocket consumers and credential-free offline discovery."""

from __future__ import annotations

import copy
import json
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncContextManager, TypeVar
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from ai_annotation._http import HttpTransport
from ai_annotation.errors import AAPError, AuthenticationError, StreamError
from ai_annotation.stream_models import (
    BatchEvent,
    MLStatsEvent,
    NotificationStreamEvent,
    PreannotationProgressEvent,
    PredictionJobEvent,
    VideoTrackerEvent,
)

EventT = TypeVar("EventT")
IdLike = str | UUID
# Socket debug logs can include the token-bearing handshake URI.
_SOCKET_LOG = logging.Logger(
    "ai_annotation.streams.transport", level=logging.CRITICAL + 1
)
_SOCKET_LOG.addHandler(logging.NullHandler())
_SOCKET_LOG.propagate = False

_CHANNELS = {
    "notifications": {
        "path": "/ws/notifications",
        "authentication": "jwt",
        "heartbeat_seconds": 30,
        "authority": "Current active user; each notification is rechecked for current resource access.",
        "reconcile": ["notifications.list", "Read the owning notification target."],
        "model": NotificationStreamEvent,
    },
    "preannotation": {
        "path": "/ws/projects/{project_id}/preannotate",
        "authentication": "jwt-or-api-key",
        "heartbeat_seconds": None,
        "authority": "Current project access, revalidated before protected payloads.",
        "reconcile": ["jobs.get", "Read current project, task and prediction state."],
        "model": PreannotationProgressEvent,
    },
    "batch_events": {
        "path": "/ws/batches/project/{project_id}",
        "authentication": "jwt-or-api-key",
        "heartbeat_seconds": 30,
        "authority": "Current project access, revalidated before protected payloads.",
        "reconcile": ["batches.list"],
        "model": BatchEvent,
    },
    "prediction_jobs": {
        "path": "/ws/prediction-jobs",
        "authentication": "jwt",
        "heartbeat_seconds": 30,
        "authority": "Current super_admin or project_admin; global prediction progress hints.",
        "reconcile": ["jobs.get", "Read persisted prediction state."],
        "model": PredictionJobEvent,
    },
    "video_tracker": {
        "path": "/ws/video-tracker-jobs/{job_id}",
        "authentication": "jwt",
        "heartbeat_seconds": 30,
        "authority": "Current visibility of the tracker job's actual task.",
        "reconcile": ["video_tracker.get_job", "video_tracker.get_preview"],
        "model": VideoTrackerEvent,
    },
    "ml_stats": {
        "path": "/ws/ml-backend-stats",
        "authentication": "jwt-or-api-key",
        "heartbeat_seconds": 30,
        "authority": "Current super_admin or project_admin. Subscription activates existing backend stats polling.",
        "reconcile": ["ml_registry.list", "ml_registry.check_health"],
        "model": MLStatsEvent,
    },
}


def describe_stream_channel(name: str) -> dict[str, Any]:
    """Describe one fixed public channel without credentials or optional dependencies."""
    if name not in _CHANNELS:
        raise ValueError("unknown stream channel; use list_stream_channels()")
    definition = _CHANNELS[name]
    return {
        "name": name,
        "protocol": "websocket",
        "sdk_method": f"streams.{name}",
        **{
            key: copy.deepcopy(value)
            for key, value in definition.items()
            if key != "model"
        },
        "event_schema": TypeAdapter(definition["model"]).json_schema(),
        "delivery": "No acknowledgement, replay cursor or delivery guarantee. Refresh REST after disconnect.",
        "connection": "One connection; JSON ping frames are ignored. No implicit reconnect, subscription message or reauthentication.",
        "optional_extra": "streams",
    }


def list_stream_channels() -> list[dict[str, Any]]:
    """List the six public channels separately from REST coverage counts."""
    return [
        {
            key: value
            for key, value in describe_stream_channel(name).items()
            if key != "event_schema"
        }
        for name in _CHANNELS
    ]


def _build_ws_url(base_url: str, path: str, token: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AAPError(
            "WebSocket base_url must be an absolute HTTP(S) URL without credentials or query"
        )
    return urlunsplit(
        (
            "wss" if parsed.scheme == "https" else "ws",
            parsed.netloc,
            parsed.path.rstrip("/") + path,
            "token=" + quote(token, safe=""),
            "",
        )
    )


def _stream_error(exc: Exception, code: str) -> StreamError:
    closed = getattr(getattr(exc, "rcvd", None), "code", None) or getattr(
        exc, "code", None
    )
    status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(
        exc, "status_code", None
    )
    return StreamError(
        "WebSocket connection ended or failed. Refresh REST state before reconnecting.",
        code=code,
        close_code=int(closed) if isinstance(closed, int) else None,
        status_code=status if isinstance(status, int) else None,
    )


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite stream number")
    return number


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite stream number")


async def _events(connection, adapter: TypeAdapter[EventT]) -> AsyncIterator[EventT]:
    iterator = connection.__aiter__()
    while True:
        try:
            raw = await iterator.__anext__()
        except StopAsyncIteration:
            return
        except Exception as exc:
            raise _stream_error(exc, "stream_disconnected") from None
        try:
            data = json.loads(
                raw, parse_float=_finite_float, parse_constant=_reject_constant
            )
            if not isinstance(data, dict):
                raise ValueError("stream event must be an object")
            if data.get("type") == "ping":
                continue
            event = adapter.validate_python(data)
        except (ValueError, TypeError, ValidationError, RecursionError):
            raise StreamError(
                "Invalid WebSocket event. Refresh REST state.",
                code="stream_invalid_event",
            ) from None
        yield event


class Streams:
    """Six typed, single-connection async streams; their context owns cleanup."""

    def __init__(self, http: HttpTransport):
        self._http = http

    @asynccontextmanager
    async def _connect(self, channel: str, path: str):
        if self._http._client.is_closed:
            raise AAPError("Client is closed")
        token = self._http._client.headers.get("Authorization", "").removeprefix(
            "Bearer "
        )
        if not token:
            raise AuthenticationError(401, "A WebSocket credential is required")
        if _CHANNELS[channel]["authentication"] == "jwt" and token.startswith("ak_"):
            raise AuthenticationError(401, "This WebSocket channel requires a JWT")
        try:
            import websockets
        except ImportError:
            raise AAPError(
                "WebSocket streams require ai-annotation-sdk[streams]"
            ) from None
        url = _build_ws_url(self._http._origin, path, token)
        try:
            connection = await websockets.connect(
                url,
                open_timeout=self._http._timeout,
                close_timeout=5,
                logger=_SOCKET_LOG,
            )
        except Exception as exc:
            raise _stream_error(exc, "stream_connection_failed") from None
        try:
            yield _events(connection, TypeAdapter(_CHANNELS[channel]["model"]))
        finally:
            # Preserve consumer exceptions and cancellation while releasing the socket.
            with suppress(Exception):
                await connection.close()

    def notifications(
        self,
    ) -> AsyncContextManager[AsyncIterator[NotificationStreamEvent]]:
        return self._connect("notifications", "/ws/notifications")

    def preannotation(
        self, project_id: IdLike
    ) -> AsyncContextManager[AsyncIterator[PreannotationProgressEvent]]:
        project_id = UUID(str(project_id))
        return self._connect("preannotation", f"/ws/projects/{project_id}/preannotate")

    def batch_events(
        self, project_id: IdLike
    ) -> AsyncContextManager[AsyncIterator[BatchEvent]]:
        project_id = UUID(str(project_id))
        return self._connect("batch_events", f"/ws/batches/project/{project_id}")

    def prediction_jobs(self) -> AsyncContextManager[AsyncIterator[PredictionJobEvent]]:
        return self._connect("prediction_jobs", "/ws/prediction-jobs")

    def video_tracker(
        self, job_id: IdLike
    ) -> AsyncContextManager[AsyncIterator[VideoTrackerEvent]]:
        job_id = UUID(str(job_id))
        return self._connect("video_tracker", f"/ws/video-tracker-jobs/{job_id}")

    def ml_stats(self) -> AsyncContextManager[AsyncIterator[MLStatsEvent]]:
        return self._connect("ml_stats", "/ws/ml-backend-stats")
