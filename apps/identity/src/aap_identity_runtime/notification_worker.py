"""Compatibility entry for the Notifications-owned publisher."""

from __future__ import annotations

import asyncio
import argparse
import json
import logging
import uuid

from aap_identity.contract import IdentityUnavailable
from aap_identity.notification_delivery_postgres import (
    NotificationDeliveryPostgresData,
)
from aap_notifications.delivery_config import NotificationPublisherSettings
from aap_notifications.delivery_worker import (
    NotificationPublisher as _NotificationPublisher,
    _notification_message as _notifications_notification_message,
    run_forever as _notifications_run_forever,
    run_once as _notifications_run_once,
)
from aap_notifications.notification_delivery_contract import (
    NotificationDeliveryUnavailable,
    NotificationLease,
    NOTIFICATION_DELIVERY_POLICY,
)


log = logging.getLogger("aap_identity_runtime.notification_worker")

__all__ = [
    "NOTIFICATION_DELIVERY_POLICY",
    "NotificationDeliveryUnavailable",
    "NotificationDeliveryPostgresData",
    "NotificationLease",
    "NotificationPublisher",
    "NotificationPublisherSettings",
    "_notification_message",
    "_serve",
    "main",
    "run_forever",
    "run_once",
]


class _IdentityErrorAdapter:
    def __init__(self, data):
        self.data = data

    async def _call(self, method, *args, **kwargs):
        try:
            return await getattr(self.data, method)(*args, **kwargs)
        except IdentityUnavailable:
            raise NotificationDeliveryUnavailable() from None

    async def delivery_policy(self):
        return await self._call("delivery_policy")

    async def claim_deliveries(self, worker_id, *, limit=16, lease_seconds=60):
        return await self._call(
            "claim_deliveries",
            worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )

    async def finish_delivery(self, lease, *, outcome, reason):
        return await self._call(
            "finish_delivery",
            lease,
            outcome=outcome,
            reason=reason,
        )


def _identity_data(data):
    return _IdentityErrorAdapter(data) if data is not None else None


def _notification_message(lease):
    try:
        return _notifications_notification_message(lease)
    except NotificationDeliveryUnavailable:
        raise IdentityUnavailable() from None


class NotificationPublisher(_NotificationPublisher):
    def __init__(
        self,
        data,
        redis,
        *,
        worker_id: uuid.UUID | None = None,
        claim_limit: int = 16,
        lease_seconds: int = 60,
        concurrency: int = 4,
        redis_timeout_seconds: float = 5,
    ):
        super().__init__(
            _IdentityErrorAdapter(data),
            redis,
            worker_id=worker_id,
            claim_limit=claim_limit,
            lease_seconds=lease_seconds,
            concurrency=concurrency,
            redis_timeout_seconds=redis_timeout_seconds,
        )

    async def run_once(self):
        try:
            return await super().run_once()
        except NotificationDeliveryUnavailable:
            raise IdentityUnavailable() from None


async def run_forever(
    settings: NotificationPublisherSettings,
    *,
    data=None,
    redis=None,
    stop_event: asyncio.Event | None = None,
) -> None:
    try:
        return await _notifications_run_forever(
            settings,
            data=_identity_data(data),
            redis=redis,
            stop_event=stop_event,
        )
    except NotificationDeliveryUnavailable:
        raise IdentityUnavailable() from None


async def run_once(
    settings: NotificationPublisherSettings,
    *,
    data=None,
    redis=None,
) -> dict[str, int]:
    try:
        return await _notifications_run_once(
            settings,
            data=_identity_data(data),
            redis=redis,
        )
    except NotificationDeliveryUnavailable:
        raise IdentityUnavailable() from None


async def _serve(settings: NotificationPublisherSettings, *, once: bool) -> None:
    if once:
        result = await run_once(settings)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return
    await run_forever(settings)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish committed Identity notifications"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="claim and publish one bounded page, then exit",
    )
    args = parser.parse_args()
    try:
        settings = NotificationPublisherSettings()
        asyncio.run(_serve(settings, once=args.once))
    except KeyboardInterrupt:
        return
    except Exception:
        raise SystemExit(
            "Notification publisher configuration or owner unavailable"
        ) from None


if __name__ == "__main__":
    main()
