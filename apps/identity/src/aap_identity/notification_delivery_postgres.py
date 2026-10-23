"""Compatibility import for the Notifications-owned delivery client."""

from __future__ import annotations

from aap_identity.contract import IdentityUnavailable
from aap_notifications.notification_delivery_contract import (
    NotificationDeliveryUnavailable,
)
from aap_notifications.notification_delivery_postgres import (
    NotificationDeliveryPostgresData as _NotificationDeliveryPostgresData,
)


class NotificationDeliveryPostgresData(_NotificationDeliveryPostgresData):
    async def _identity_compatible_call(self, method, *args, **kwargs):
        try:
            return await getattr(super(), method)(*args, **kwargs)
        except NotificationDeliveryUnavailable:
            raise IdentityUnavailable() from None

    async def delivery_policy(self):
        return await self._identity_compatible_call("delivery_policy")

    async def claim_deliveries(self, worker_id, *, limit=16, lease_seconds=60):
        return await self._identity_compatible_call(
            "claim_deliveries",
            worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )

    async def finish_delivery(self, lease, *, outcome, reason):
        return await self._identity_compatible_call(
            "finish_delivery",
            lease,
            outcome=outcome,
            reason=reason,
        )
