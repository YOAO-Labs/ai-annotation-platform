"""Best-effort immediate publication over a borrowed Notifications owner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
import logging
from typing import Protocol
import uuid

from .delivery_read import DeliveryScope, unrestricted_delivery_indices
from .publication_contract import (
    ImmediatePublicationRequest,
    PublicationAuthorization,
    publication_allowed,
)
from .realtime_event_contract import (
    notification_event_message,
    notification_sync_message,
)


class NotificationPublicationData(Protocol):
    async def notification_by_id(self, notification_id: uuid.UUID): ...

    async def resolve_scopes(self, rows) -> list[DeliveryScope]: ...

    async def authorization_facts(
        self, rows, scopes: list[DeliveryScope]
    ) -> list[PublicationAuthorization]: ...


NotificationPublicationTransport = Callable[
    [ImmediatePublicationRequest], Awaitable[bool]
]


class NotificationPublicationManager:
    """Preserve API publication timing and error behavior behind one owner seam."""

    def __init__(
        self,
        data: NotificationPublicationData,
        transport: NotificationPublicationTransport,
        logger: logging.Logger | None = None,
    ) -> None:
        self.data = data
        self.transport = transport
        self.log = logger or logging.getLogger(__name__)

    @staticmethod
    def _request(
        row,
        authorization: PublicationAuthorization,
    ) -> ImmediatePublicationRequest:
        user_id = row.user_id
        message = notification_event_message(
            id=str(row.id),
            type=row.type,
            target_type=row.target_type,
            target_id=str(row.target_id),
            payload=row.payload,
            created_at=(row.created_at or datetime.now(timezone.utc)).isoformat(),
        )
        return ImmediatePublicationRequest(
            schema="aap-notifications-immediate-publication/v1",
            user_id=user_id,
            message=message,
            authorization=authorization,
        )

    @staticmethod
    def _unrestricted_authorization() -> PublicationAuthorization:
        return PublicationAuthorization(
            restricted=False,
            project_id=None,
            export_required=False,
            member=False,
            reviewer=False,
            owner=False,
            super_admin=False,
        )

    async def _authorized_facts(self, rows):
        scopes = await self.data.resolve_scopes(rows)
        facts = await self.data.authorization_facts(rows, scopes)
        if len(facts) != len(rows):
            raise ValueError("authorization facts do not match notification rows")
        return facts

    async def publish_committed(self, notifications: Iterable) -> None:
        """Publish allowed committed rows independently, logging safe failures."""

        rows = list(notifications)
        fallback = False
        try:
            facts = await self._authorized_facts(rows)
            allowed = {
                index for index, fact in enumerate(facts) if publication_allowed(fact)
            }
        except Exception:
            self.log.warning("notification delivery authorization failed")
            allowed = unrestricted_delivery_indices(rows)
            fallback = True

        fallback_authorization = self._unrestricted_authorization()
        for index, row in enumerate(rows):
            if index not in allowed:
                self.log.info(
                    "notification suppressed for revoked project access user=%s type=%s",
                    row.user_id,
                    row.type,
                )
                continue
            authorization = fallback_authorization if fallback else facts[index]
            try:
                accepted = await self.transport(self._request(row, authorization))
                if type(accepted) is not bool:
                    raise TypeError("publication transport reply must be boolean")
                if not accepted:
                    self.log.info(
                        "notification publication was not accepted user=%s type=%s",
                        row.user_id,
                        row.type,
                    )
            except Exception:
                self.log.warning(
                    "notification publish failed user=%s type=%s",
                    row.user_id,
                    row.type,
                )

    async def publish_by_id(self, notification_id: uuid.UUID) -> bool:
        """Return false for missing/denied rows and propagate publication failures."""

        row = await self.data.notification_by_id(notification_id)
        if row is None:
            return False
        facts = await self._authorized_facts([row])
        authorization = facts[0]
        if not publication_allowed(authorization):
            self.log.info(
                "notification delivery suppressed for revoked project access "
                "notification=%s user=%s type=%s",
                row.id,
                row.user_id,
                row.type,
            )
            return False
        accepted = await self.transport(self._request(row, authorization))
        if type(accepted) is not bool:
            raise TypeError("publication transport reply must be boolean")
        return accepted

    async def publish_sync(self, user_id: uuid.UUID, *, reason: str) -> None:
        """Best-effort sync hint for an already committed change."""

        try:
            request = ImmediatePublicationRequest(
                schema="aap-notifications-immediate-publication/v1",
                user_id=user_id,
                message=notification_sync_message(reason=reason),
                authorization=None,
            )
            accepted = await self.transport(request)
            if type(accepted) is not bool or not accepted:
                self.log.warning("notification sync publish was not accepted")
        except Exception:
            self.log.warning("notification sync publish failed")
