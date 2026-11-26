"""API-side configuration and execution helpers for external notification channels."""

from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.config import settings
from yoao_notifications.channel_delivery_postgres import (
    NotificationChannelDeliveryPostgresData,
    NotificationChannelDeliverySessionData,
    NotificationChannelDeliveryUnavailable,
)
from yoao_notifications.channel_delivery_worker import NotificationChannelDeliveryWorker
from yoao_notifications.channel_http_client import NotificationChannelHttpClient
from yoao_notifications.delivery_config import optional_notification_secret


def channel_encryption_key() -> str:
    return (
        optional_notification_secret(
            settings.notification_channel_encryption_key,
            settings.notification_channel_encryption_key_file,
        )
        or ""
    )


def build_channel_http_client() -> NotificationChannelHttpClient:
    return NotificationChannelHttpClient(
        allow_insecure_http_hosts=settings.notification_channel_allow_insecure_http_hosts,
        host_allowlist=settings.notification_channel_host_allowlist,
    )


async def run_notification_channel_deliveries(*, session_factory) -> dict[str, int]:
    try:
        dsn = optional_notification_secret(
            settings.notification_channel_delivery_database_url,
            settings.notification_channel_delivery_database_url_file,
        )
        if dsn:
            dsn = (
                make_url(dsn)
                .set(drivername="postgresql")
                .render_as_string(hide_password=False)
            )
    except (ArgumentError, ValueError):
        raise NotificationChannelDeliveryUnavailable() from None

    data = (
        NotificationChannelDeliveryPostgresData(dsn)
        if dsn
        else NotificationChannelDeliverySessionData(session_factory)
    )
    worker = NotificationChannelDeliveryWorker(
        data,
        build_channel_http_client(),
        encryption_key=channel_encryption_key(),
    )
    return await worker.run_once()
