"""Bind the Notifications publisher to short, committed database operations."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from yoao_notifications.delivery_config import optional_notification_secret
from yoao_notifications.delivery_worker import NotificationPublisher
from yoao_notifications.notification_delivery_contract import (
    NOTIFICATION_DELIVERY_POLICY,
    NOTIFICATION_DELIVERY_PROTOCOL,
    NotificationDeliveryUnavailable,
    NotificationFinishRequest,
    decode_claimed_deliveries,
    decode_notification_delivery_policy,
    decode_notification_finish_reply,
    native_notification_command_json,
    notification_claim_request,
)
from yoao_notifications.notification_delivery_postgres import (
    NotificationDeliveryPostgresData,
)
from app.config import settings


class NotificationDeliveryData:
    """Commit the claim before Redis, using a separate session for each finish."""

    def __init__(self, session_factory, *, include_terminal=False):
        self.session_factory = session_factory
        self.include_terminal = include_terminal

    async def _execute(self, function, request, decode):
        async with self.session_factory() as db:
            try:
                async with asyncio.timeout(10):
                    statement = f"SELECT yoao_notifications_v1.{function}("
                    parameters = {}
                    if request is not None:
                        statement += "CAST(:command AS jsonb)"
                        parameters["command"] = native_notification_command_json(
                            request
                        )
                    raw = await db.scalar(text(statement + ")"), parameters)
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
        request = notification_claim_request(
            worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
            include_terminal=self.include_terminal,
        )
        return await self._execute(
            "claim_identity_deliveries",
            request,
            lambda raw: decode_claimed_deliveries(request, raw),
        )

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


async def run_notifications(*, session_factory, redis=None):
    try:
        dsn = optional_notification_secret(
            settings.notification_publish_database_url,
            settings.notification_publish_database_url_file,
        )
        redis_url = optional_notification_secret(
            settings.notification_publish_redis_url,
            settings.notification_publish_redis_url_file,
        )
        if dsn:
            dsn = (
                make_url(dsn)
                .set(drivername="postgresql")
                .render_as_string(hide_password=False)
            )
    except (ArgumentError, ValueError):
        raise NotificationDeliveryUnavailable() from None

    data = (
        NotificationDeliveryPostgresData(dsn, include_terminal=True)
        if dsn
        else NotificationDeliveryData(session_factory, include_terminal=True)
    )
    owns_redis = redis is None
    try:
        if owns_redis:
            from redis.asyncio import Redis

            redis = Redis.from_url(
                redis_url or settings.redis_url,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        if (await data.delivery_policy()).policy != NOTIFICATION_DELIVERY_POLICY:
            raise NotificationDeliveryUnavailable()
        result = await NotificationPublisher(data, redis).run_once()
        return {"state": "idle" if result["claimed"] == 0 else "processed", **result}
    except ValueError:
        raise NotificationDeliveryUnavailable() from None
    finally:
        if owns_redis and redis is not None:
            await redis.aclose()
