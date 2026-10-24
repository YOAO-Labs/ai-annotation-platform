"""Standalone ASGI runtime for the Notifications WebSocket owner."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from typing import Any

from .websocket import run_pubsub_ws, serve_notification_socket
from .websocket_postgres import (
    configure_notification_jsonb_codec,
    open_notification_socket_data,
)

log = logging.getLogger("aap_notifications.websocket.runtime")

_SOCKET_CAPABILITIES = {
    ("read_notification", "uuid"),
    ("find_terminal_notification", "uuid, text, uuid"),
    ("delivery_job_scopes", "uuid[]"),
    ("delivery_member_pairs", "jsonb"),
    ("delivery_reviewer_pairs", "jsonb"),
    ("delivery_owner_pairs", "jsonb"),
    ("delivery_super_pairs", "jsonb"),
}


async def _new_admission_client(settings):
    try:
        from aap_identity_admission.client import RemoteAdmissionClient
    except ImportError:
        raise RuntimeError("Identity admission client is unavailable") from None
    return RemoteAdmissionClient(
        origin=settings.identity_url,
        service_token=settings.identity_token.get_secret_value(),
        timeout_seconds=2,
    )


async def _close_redis_pool(pool) -> None:
    if pool is None:
        return
    try:
        await asyncio.wait_for(pool.disconnect(inuse_connections=True), timeout=2)
    except Exception:
        log.debug("notification socket Redis pool close timed out")


async def _close_postgres_pool(pool) -> None:
    if pool is None:
        return
    try:
        await asyncio.wait_for(pool.close(), timeout=2)
    except asyncio.TimeoutError:
        pool.terminate()
    except Exception:
        log.debug("notification socket PostgreSQL pool close failed")


def create_app(settings=None):
    """Build a Starlette app without importing optional runtime clients yet."""

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute

    if settings is None:
        from .websocket_config import NotificationSocketSettings

        settings = NotificationSocketSettings()

    runtime: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app):
        postgres_pool = None
        redis_pool = None
        try:
            import asyncpg
            from aap_owner_data.postgres import verify_function_only_role
            from redis.asyncio import ConnectionPool, Redis

            admission = await _new_admission_client(settings)
            postgres_pool = await asyncpg.create_pool(
                dsn=settings.database_url.get_secret_value(),
                min_size=1,
                max_size=10,
                timeout=5,
                command_timeout=10,
                init=configure_notification_jsonb_codec,
                server_settings={"application_name": "aap-notification-socket"},
            )
            async with postgres_pool.acquire() as connection:
                login = await connection.fetchval("SELECT session_user")
                await verify_function_only_role(
                    connection,
                    schema="aap_notifications_v1",
                    functions=_SOCKET_CAPABILITIES,
                    role=login,
                )

            redis_pool = ConnectionPool.from_url(
                settings.redis_url.get_secret_value(),
                max_connections=200,
                decode_responses=False,
            )
            redis_client = Redis(connection_pool=redis_pool)

            async def authenticate(token):
                from aap_identity_admission.contract import (
                    IdentityCredentialRejected,
                    IdentityGatewayUnavailable,
                )

                if token.startswith("ak_"):
                    return None
                try:
                    return await admission.authenticate(token)
                except (IdentityCredentialRejected, IdentityGatewayUnavailable):
                    return None

            async def active_actor(actor_id):
                return await admission.actor_status(actor_id)

            runtime.update(
                authenticate=authenticate,
                active_actor=active_actor,
                postgres_pool=postgres_pool,
                redis_client=redis_client,
            )
        except Exception:
            log.error("notification socket dependencies unavailable")
            await _close_redis_pool(redis_pool)
            await _close_postgres_pool(postgres_pool)
            raise RuntimeError("Notification socket dependencies unavailable") from None

        try:
            yield
        finally:
            runtime.clear()
            await _close_redis_pool(redis_pool)
            await _close_postgres_pool(postgres_pool)

    async def live(_request: Request):
        return JSONResponse({"status": "ok"})

    async def ready(_request: Request):
        try:
            async with runtime["postgres_pool"].acquire() as connection:
                await connection.fetchval("SELECT 1")
            await runtime["redis_client"].ping()
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    async def notification_socket(websocket):
        token = websocket.query_params.get("token")
        if token is None:
            await websocket.close(code=1008)
            return
        await serve_notification_socket(
            websocket,
            token,
            authenticate=runtime["authenticate"],
            active_actor=runtime["active_actor"],
            open_delivery=lambda: open_notification_socket_data(
                runtime["postgres_pool"]
            ),
            redis_client=lambda: runtime["redis_client"],
            relay=run_pubsub_ws,
        )

    return Starlette(
        routes=[
            Route("/health/live", live, methods=["GET"]),
            Route("/health/ready", ready, methods=["GET"]),
            WebSocketRoute("/ws/notifications", notification_socket),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    from pydantic import ValidationError
    import uvicorn

    from .websocket_config import NotificationSocketSettings

    try:
        settings = NotificationSocketSettings()
    except (ValidationError, ValueError):
        log.error("invalid notification socket configuration")
        raise SystemExit(2) from None

    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
