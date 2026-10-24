"""Notifications WebSocket authorization and relay lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
import json
import logging
import uuid
from typing import Any, Protocol

from .redis_pubsub import channel_for

log = logging.getLogger("aap_notifications.websocket")
HEARTBEAT_INTERVAL = 30
_NOTIFICATION_GATE_ATTEMPTS = 4
_NOTIFICATION_GATE_RETRY_SECONDS = 0.15


class NotificationSocketPrincipal(Protocol):
    actor_id: uuid.UUID


class NotificationSocketActorStatus(Protocol):
    is_active: bool


class NotificationSocketDelivery(Protocol):
    async def notification_by_id(self, notification_id: uuid.UUID) -> Any | None: ...

    async def notification_deliverable(
        self, notification_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool: ...


class NotificationWebSocket(Protocol):
    async def accept(self) -> None: ...

    async def close(self, code: int) -> None: ...

    async def receive(self) -> Mapping[str, Any]: ...

    async def send_text(self, data: str) -> None: ...


class NotificationPubSub(Protocol):
    def listen(self) -> AsyncIterator[Mapping[str, Any]]: ...

    async def subscribe(self, channel: str) -> None: ...

    async def unsubscribe(self, channel: str) -> None: ...

    async def close(self) -> None: ...


class NotificationRedis(Protocol):
    def pubsub(self) -> NotificationPubSub: ...


Authenticate = Callable[[str], Awaitable[NotificationSocketPrincipal | None]]
ActiveActor = Callable[[uuid.UUID], Awaitable[NotificationSocketActorStatus | None]]
OpenDelivery = Callable[[], AbstractAsyncContextManager[NotificationSocketDelivery]]
Relay = Callable[..., Awaitable[None]]
RedisClientFactory = Callable[[], NotificationRedis]


def _actor_uuid(principal: NotificationSocketPrincipal) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(principal.actor_id))
    except (AttributeError, TypeError, ValueError):
        return None


async def notification_message_allowed(
    token: str,
    data: Any,
    *,
    authenticate: Authenticate,
    open_delivery: OpenDelivery,
    retry_seconds: float = _NOTIFICATION_GATE_RETRY_SECONDS,
) -> bool:
    """Reauthenticate a frame and fail closed until its row is visible."""

    if token.startswith("ak_"):
        return False
    principal = await authenticate(token)
    if principal is None:
        return False
    actor_id = _actor_uuid(principal)
    if actor_id is None:
        return False

    try:
        message = json.loads(data.decode() if isinstance(data, bytes) else data)
    except Exception:
        return False
    if not isinstance(message, dict):
        return False
    if message.get("type") == "notifications.sync":
        return True
    raw_id = message.get("id")
    if not raw_id:
        return False
    try:
        notification_id = uuid.UUID(str(raw_id))
    except (TypeError, ValueError):
        return False

    async with open_delivery() as delivery:
        for attempt in range(_NOTIFICATION_GATE_ATTEMPTS):
            if await delivery.notification_by_id(notification_id) is not None:
                return await delivery.notification_deliverable(
                    notification_id, actor_id
                )
            if attempt + 1 < _NOTIFICATION_GATE_ATTEMPTS:
                await asyncio.sleep(retry_seconds)
    return False


def _is_websocket_disconnect(error: BaseException) -> bool:
    try:
        from starlette.websockets import WebSocketDisconnect
    except ImportError:
        return error.__class__.__name__ == "WebSocketDisconnect"
    return isinstance(error, WebSocketDisconnect)


async def _heartbeat_loop(websocket: NotificationWebSocket) -> None:
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await websocket.send_text(json.dumps({"type": "ping"}))
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if not _is_websocket_disconnect(error):
            log.debug("notification socket heartbeat stopped")


async def run_pubsub_ws(
    websocket: NotificationWebSocket,
    pubsub: NotificationPubSub,
    *,
    heartbeat: bool = True,
    allow_message: Callable[[Any], Awaitable[bool]] | None = None,
) -> None:
    """Relay Redis messages until disconnect, cancellation, or a relay error."""

    async def relay_messages() -> None:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            data = message["data"]
            if allow_message is not None and not await allow_message(data):
                continue
            await websocket.send_text(
                data.decode() if isinstance(data, bytes) else data
            )

    async def watch_disconnect() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    tasks = [
        asyncio.create_task(relay_messages()),
        asyncio.create_task(watch_disconnect()),
    ]
    if heartbeat:
        tasks.append(asyncio.create_task(_heartbeat_loop(websocket)))

    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    for task in done:
        if task.cancelled():
            task.result()
        error = task.exception()
        if error is not None and not _is_websocket_disconnect(error):
            raise error


async def serve_notification_socket(
    websocket: NotificationWebSocket,
    token: str,
    *,
    authenticate: Authenticate,
    active_actor: ActiveActor,
    open_delivery: OpenDelivery,
    redis_client: RedisClientFactory,
    relay: Relay = run_pubsub_ws,
) -> None:
    """Authenticate the handshake, recheck current activity, and serve one user."""

    try:
        if token.startswith("ak_"):
            raise PermissionError
        principal = await authenticate(token)
        actor_id = _actor_uuid(principal) if principal is not None else None
        if actor_id is None:
            raise PermissionError
        status = await active_actor(actor_id)
        if status is None or not status.is_active:
            raise PermissionError
    except Exception:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    channel = channel_for(actor_id)
    pubsub = None

    async def allow_message(data: Any) -> bool:
        return await notification_message_allowed(
            token,
            data,
            authenticate=authenticate,
            open_delivery=open_delivery,
        )

    try:
        pubsub = redis_client().pubsub()
        await pubsub.subscribe(channel)
        await relay(websocket, pubsub, allow_message=allow_message)
    except Exception as error:
        if not _is_websocket_disconnect(error):
            log.warning("notification socket relay stopped user_id=%s", actor_id)
    finally:
        if pubsub is not None:
            try:
                try:
                    await pubsub.unsubscribe(channel)
                finally:
                    await pubsub.close()
            except Exception:
                pass
