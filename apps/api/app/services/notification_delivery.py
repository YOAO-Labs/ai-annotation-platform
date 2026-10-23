"""Bind the Notifications publisher to short, committed database operations."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_identity.contract import IdentityUnavailable
from aap_notifications.notification_delivery_contract import (
    NOTIFICATION_DELIVERY_PROTOCOL,
    NotificationDeliveryUnavailable,
    NotificationClaimRequest,
    NotificationFinishRequest,
    decode_notification_claim_reply,
    decode_notification_delivery_policy,
    decode_notification_finish_reply,
    native_notification_command_json,
)
from aap_notifications.notification_delivery_postgres import (
    NotificationDeliveryPostgresData,
)
from aap_notifications.delivery_worker import NotificationPublisher
from app.config import settings
from app.db.base import async_session
from app.services.identity_gateway import get_identity_gateway, identity_database_source
from app.services.identity_session_data import _json_value


class NotificationDeliveryData:
    """Commit the claim before Redis, using a separate session for each finish."""

    def __init__(self, session_factory=async_session):
        self.session_factory = session_factory

    async def _execute(self, function, request, decode):
        async with self.session_factory() as db:
            try:
                async with asyncio.timeout(10):
                    statement = f"SELECT aap_notifications_v1.{function}("
                    parameters = {}
                    if request is not None:
                        statement += "CAST(:command AS jsonb)"
                        parameters["command"] = native_notification_command_json(
                            request
                        )
                    raw = _json_value(
                        await db.scalar(text(statement + ")"), parameters)
                    )
                    result = decode(raw)
                    await db.commit()
                    return result
            except (SQLAlchemyError, TimeoutError, NotificationDeliveryUnavailable):
                await db.rollback()
                raise NotificationDeliveryUnavailable() from None
            except BaseException:
                await db.rollback()
                raise

    async def delivery_policy(self):
        return await self._execute(
            "identity_delivery_policy", None, decode_notification_delivery_policy
        )

    async def claim_deliveries(self, worker_id, *, limit=16, lease_seconds=60):
        request = NotificationClaimRequest(
            schema=NOTIFICATION_DELIVERY_PROTOCOL,
            worker_id=worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )

        def decode(raw):
            items = decode_notification_claim_reply(raw).data.items
            if (
                len(items) > request.limit
                or len({item.id for item in items}) != len(items)
                or len({item.lease_id for item in items}) != len(items)
                or any(item.worker_id != request.worker_id for item in items)
            ):
                raise NotificationDeliveryUnavailable()
            return items

        return await self._execute("claim_identity_deliveries", request, decode)

    async def finish_delivery(self, lease, *, outcome, reason):
        request = NotificationFinishRequest(
            schema=NOTIFICATION_DELIVERY_PROTOCOL,
            notification_id=lease.id,
            worker_id=lease.worker_id,
            lease_id=lease.lease_id,
            outcome=outcome,
            reason=reason,
        )
        return await self._execute(
            "finish_identity_delivery",
            request,
            lambda raw: decode_notification_finish_reply(raw).data.accepted,
        )


async def run_identity_notifications(*, session_factory=None, redis=None):
    dsn = identity_database_source(
        settings.notification_publish_database_url,
        settings.notification_publish_database_url_file,
    )
    if not dsn and get_identity_gateway().identity_url is not None:
        return {"state": "remote"}
    data = (
        NotificationDeliveryPostgresData(dsn)
        if dsn
        else NotificationDeliveryData(session_factory or async_session)
    )
    owns_redis = redis is None
    if owns_redis:
        from redis.asyncio import Redis

        redis = Redis.from_url(
            settings.redis_url, socket_connect_timeout=5, socket_timeout=5
        )
    try:
        await data.delivery_policy()
        result = await NotificationPublisher(data, redis).run_once()
        return {"state": "idle" if result["claimed"] == 0 else "processed", **result}
    except NotificationDeliveryUnavailable:
        raise IdentityUnavailable() from None
    finally:
        if owns_redis:
            await redis.aclose()
