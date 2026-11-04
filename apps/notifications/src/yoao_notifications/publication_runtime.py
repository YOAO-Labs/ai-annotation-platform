"""Optional private HTTP route hosted by the existing publisher process."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import logging

from .delivery_config import NotificationPublisherSettings
from .notification_delivery_contract import NOTIFICATION_DELIVERY_POLICY
from .notification_delivery_postgres import NotificationDeliveryPostgresData
from .publication_contract import (
    IMMEDIATE_PUBLICATION_PROTOCOL,
    ImmediatePublicationReply,
    ImmediatePublicationUnavailable,
    decode_publication_request,
    publication_allowed,
)
from .redis_pubsub import channel_for

log = logging.getLogger("aap_notifications.publication.runtime")


def create_app(
    settings: NotificationPublisherSettings,
    *,
    redis=None,
    data=None,
    publisher_ready: asyncio.Event | None = None,
):
    """Create the optional service-token route without opening resources yet."""

    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    if settings.api_token is None:
        raise ValueError("Notification publication API token is not configured")

    owns_redis = redis is None

    @asynccontextmanager
    async def lifespan(app):
        client = redis
        owner_data = data
        try:
            if client is None:
                from redis.asyncio import Redis

                client = Redis.from_url(
                    settings.redis_url.get_secret_value(),
                    socket_connect_timeout=settings.redis_timeout_seconds,
                    socket_timeout=settings.redis_timeout_seconds,
                    decode_responses=False,
                )
            if owner_data is None:
                owner_data = NotificationDeliveryPostgresData(
                    settings.database_url.get_secret_value(),
                    connect_timeout=settings.connect_timeout_seconds,
                    command_timeout=settings.command_timeout_seconds,
                )
        except Exception:
            if owns_redis and client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass
            raise RuntimeError(
                "Notification publication dependencies unavailable"
            ) from None

        app.state.redis = client
        app.state.delivery_data = owner_data
        try:
            yield
        finally:
            if owns_redis and client is not None:
                try:
                    await client.aclose()
                except Exception:
                    if not asyncio.current_task().cancelling():
                        log.warning("notification publication Redis close failed")

    def reply(published: bool, *, status_code: int = 200):
        value = ImmediatePublicationReply(
            schema=IMMEDIATE_PUBLICATION_PROTOCOL,
            published=published,
        )
        return JSONResponse(
            value.model_dump(mode="json", by_alias=True), status_code=status_code
        )

    async def live(_request: Request):
        return JSONResponse({"status": "ok"})

    async def ready(request: Request):
        if publisher_ready is not None and not publisher_ready.is_set():
            return JSONResponse({"status": "unavailable"}, status_code=503)
        try:
            await request.app.state.redis.ping()
            policy = await request.app.state.delivery_data.delivery_policy()
            if policy.policy != NOTIFICATION_DELIVERY_POLICY:
                raise ImmediatePublicationUnavailable()
        except asyncio.CancelledError:
            raise
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    async def publish(request: Request):
        expected = b"Bearer " + settings.api_token.get_secret_value().encode("ascii")
        supplied = request.headers.get("authorization", "")
        try:
            authorized = hmac.compare_digest(supplied.encode("ascii"), expected)
        except (UnicodeEncodeError, TypeError):
            authorized = False
        if not authorized:
            return reply(False, status_code=401)

        try:
            publication = decode_publication_request(await request.body())
        except ImmediatePublicationUnavailable:
            return reply(False, status_code=400)
        if publication.authorization is not None and not publication_allowed(
            publication.authorization
        ):
            return reply(False)

        try:
            # Keep the legacy immediate API encoder and treat PUBLISH(0) as success.
            await request.app.state.redis.publish(
                channel_for(publication.user_id),
                json.dumps(publication.message),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return reply(False, status_code=503)
        return reply(True)

    return Starlette(
        routes=[
            Route("/health/live", live, methods=["GET"]),
            Route("/health/ready", ready, methods=["GET"]),
            Route("/v1/notifications/publish", publish, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
