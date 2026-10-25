"""Preserve the public API socket URL when Notifications serves the connection."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode, urlsplit, urlunsplit

from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, ProtocolError
from websockets.frames import Close


log = logging.getLogger(__name__)
# Match the SDK transport: handshake debug logs contain the credential-bearing URI.
_TRANSPORT_LOG = logging.Logger(
    "aap.notification_socket.transport", logging.CRITICAL + 1
)
_TRANSPORT_LOG.addHandler(logging.NullHandler())
_TRANSPORT_LOG.propagate = False


class _OwnerConnect(connect):
    def process_redirect(self, error):
        # Keep the credential at the configured owner endpoint.
        return error


def notification_socket_endpoint(origin: str, token: str) -> str:
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"ws", "wss"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid Notifications socket origin")
    _ = parsed.port
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            "/ws/notifications",
            urlencode({"token": token}),
            "",
        )
    )


def _closing(code: int, reason: str = "") -> tuple[int, str]:
    try:
        Close(code, reason).check()
    except ProtocolError:
        code, reason = 1011, ""
    return code, reason


async def _close_socket(websocket, code: int, reason: str = "") -> None:
    code, reason = _closing(code, reason)
    try:
        await websocket.close(code=code, reason=reason)
    except (WebSocketDisconnect, RuntimeError):
        pass


async def proxy_notification_socket(websocket, token: str, origin: str) -> None:
    """Relay raw frames only; the owner performs admission and delivery checks."""
    accepted = False
    code, reason = 1000, ""
    try:
        endpoint = notification_socket_endpoint(origin, token)
        async with _OwnerConnect(
            endpoint,
            proxy=None,
            open_timeout=10,
            close_timeout=2,
            max_size=None,
            ping_interval=None,
            logger=_TRANSPORT_LOG,
        ) as upstream:
            await websocket.accept()
            accepted = True

            async def receive_owner():
                async for data in upstream:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)

            async def receive_client():
                while True:
                    event = await websocket.receive()
                    if event["type"] == "websocket.disconnect":
                        await upstream.close(
                            *_closing(event.get("code", 1000), event.get("reason", ""))
                        )
                        return
                    if event.get("text") is not None:
                        await upstream.send(event["text"])
                    elif event.get("bytes") is not None:
                        await upstream.send(event["bytes"])

            tasks = [
                asyncio.create_task(receive_owner()),
                asyncio.create_task(receive_client()),
            ]
            try:
                done, _pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
                code, reason = upstream.close_code or 1000, upstream.close_reason or ""
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        code, reason = 1012, ""
        raise
    except ConnectionClosed as error:
        code = error.rcvd.code if error.rcvd is not None else 1011
        reason = error.rcvd.reason if error.rcvd is not None else ""
    except WebSocketDisconnect:
        return
    except Exception:
        log.warning("Notifications socket upstream is unavailable")
        code, reason = 1011, ""
    finally:
        await _close_socket(
            websocket, code if accepted else 1008, reason if accepted else ""
        )
