"""Lease-based delivery worker for external notification channel plugins."""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any, Protocol

from pydantic import ValidationError

from .channel_contract import (
    ChannelDeliverRequest,
    ChannelDeliveryOutcome,
    NotificationChannelEnvelope,
    validate_recipient_address,
)
from .channel_crypto import (
    NotificationChannelCryptoError,
    NotificationChannelKeyNotConfigured,
    decrypt_channel_token,
)
from .channel_http_client import NotificationChannelHttpClient
from .delivery_read import (
    PRIVATE_MIGRATION_JOB_KINDS,
    NotificationDeliveryReadData,
    NotificationDeliveryReadPolicy,
)

_LABELING_TARGET_TYPES = frozenset(
    {"task", "batch", "feedback", "annotation_comment", "failed_prediction"}
)
_JOB_TARGET_TYPES = frozenset({"export", "async_job"})


class NotificationChannelDeliveryData(NotificationDeliveryReadData, Protocol):
    async def channel_delivery_policy(self) -> str: ...

    async def claim_deliveries(
        self,
        worker_id: uuid.UUID,
        *,
        limit: int = 16,
        lease_seconds: int = 60,
    ) -> list[dict[str, Any]]: ...

    async def finish_delivery(
        self,
        *,
        delivery_id: uuid.UUID,
        worker_id: uuid.UUID,
        lease_id: uuid.UUID,
        outcome: str,
        reason: str | None = None,
        provider_message_id: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> bool: ...

    async def is_bug_report_visible(
        self, actor_id: uuid.UUID, report_id: uuid.UUID
    ) -> bool: ...

    async def is_labeling_target_visible(
        self,
        actor_id: uuid.UUID,
        target_type: str,
        target_id: uuid.UUID,
        project_id: uuid.UUID | None,
        task_id: uuid.UUID | None,
    ) -> bool: ...


def _optional_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class NotificationChannelDeliveryWorker:
    """Claims outbox rows, runs authoritative pre-delivery checks, and calls plugins."""

    def __init__(
        self,
        data: NotificationChannelDeliveryData,
        http_client: NotificationChannelHttpClient,
        *,
        encryption_key: str,
        worker_id: uuid.UUID | None = None,
        limit: int = 16,
        lease_seconds: int = 60,
        logger: logging.Logger | None = None,
    ) -> None:
        self.data = data
        self.http_client = http_client
        self.encryption_key = encryption_key
        self.worker_id = worker_id or uuid.uuid4()
        self.limit = limit
        self.lease_seconds = lease_seconds
        self.logger = logger or logging.getLogger(__name__)
        self._policy_verified = False

    async def _target_visible(self, item: dict[str, Any]) -> bool:
        recipient_id = uuid.UUID(str(item["recipient_id"]))
        target_type = str(item["target_type"])
        target_id = uuid.UUID(str(item["target_id"]))
        project_id = _optional_uuid(item.get("project_id"))
        task_id = _optional_uuid(item.get("task_id"))
        envelope_data = item.get("envelope_data")
        payload = envelope_data if isinstance(envelope_data, dict) else {}

        if target_type == "bug_report":
            return await self.data.is_bug_report_visible(recipient_id, target_id)

        if target_type in _LABELING_TARGET_TYPES:
            return await self.data.is_labeling_target_visible(
                recipient_id, target_type, target_id, project_id, task_id
            )

        if target_type in _JOB_TARGET_TYPES:
            if payload.get("kind") in PRIVATE_MIGRATION_JOB_KINDS:
                return False
            row = SimpleNamespace(
                user_id=recipient_id,
                target_type=target_type,
                target_id=target_id,
                payload=payload,
            )
            policy = NotificationDeliveryReadPolicy(self.data)
            scopes = await policy.resolve_scopes([row])
            allowed = await policy.allowed_indices([row], scopes)
            return 0 in allowed

        return False

    async def _deliver_one(self, item: dict[str, Any]) -> str:
        delivery_id = uuid.UUID(str(item["delivery_id"]))
        lease_id = uuid.UUID(str(item["lease_id"]))
        worker_id = uuid.UUID(str(item["worker_id"]))

        async def finish(
            outcome: str,
            *,
            reason: str | None = None,
            provider_message_id: str | None = None,
            retry_after_seconds: int | None = None,
        ) -> str:
            await self.data.finish_delivery(
                delivery_id=delivery_id,
                worker_id=worker_id,
                lease_id=lease_id,
                outcome=outcome,
                reason=reason,
                provider_message_id=provider_message_id,
                retry_after_seconds=retry_after_seconds,
            )
            return outcome

        if item.get("channel_status") != "active":
            return await finish("skipped", reason="channel_disabled")

        if item.get("account_active") is not True:
            return await finish("skipped", reason="access_revoked")

        if item.get("preference_enabled") is not True:
            return await finish("skipped", reason="preference_disabled")

        recipient_address = item.get("recipient_address")
        if not isinstance(recipient_address, dict):
            return await finish("skipped", reason="binding_removed")

        address_schema = item.get("address_schema")
        try:
            if not isinstance(address_schema, dict):
                raise ValueError("address_schema missing")
            validate_recipient_address(recipient_address, address_schema)
        except ValueError:
            return await finish("permanent", reason="invalid_recipient_address")

        try:
            visible = await self._target_visible(item)
        except Exception:
            self.logger.warning(
                "external channel target visibility check failed delivery=%s",
                delivery_id,
                exc_info=True,
            )
            return await finish(
                "retryable",
                reason="visibility_check_unavailable",
                retry_after_seconds=30,
            )
        if not visible:
            return await finish("skipped", reason="access_revoked")

        ciphertext = item.get("auth_token_ciphertext")
        try:
            token = decrypt_channel_token(ciphertext, self.encryption_key)
        except (NotificationChannelKeyNotConfigured, NotificationChannelCryptoError):
            return await finish(
                "retryable",
                reason="channel_key_unavailable",
                retry_after_seconds=60,
            )

        envelope_data = item.get("envelope_data")
        if not isinstance(envelope_data, dict):
            return await finish("permanent", reason="invalid_envelope_data")

        try:
            request = ChannelDeliverRequest(
                channel_id=str(item["channel_id"]),
                envelope=NotificationChannelEnvelope(
                    event=str(item["notification_type"]),
                    delivery_id=delivery_id,
                    occurred_at=str(item["occurred_at"]),
                    data=envelope_data,
                ),
                recipient_address=recipient_address,
                locale=str(item.get("locale") or "zh-CN"),
            )
        except (ValidationError, ValueError):
            return await finish("permanent", reason="invalid_delivery_request")

        outcome: ChannelDeliveryOutcome = await self.http_client.deliver(
            endpoint_url=str(item["endpoint_url"]),
            auth_token=token,
            request=request,
        )
        return await finish(
            outcome.outcome,
            reason=outcome.reason,
            provider_message_id=outcome.provider_message_id,
            retry_after_seconds=outcome.retry_after_seconds,
        )

    async def run_once(self) -> dict[str, int]:
        if not self._policy_verified:
            await self.data.channel_delivery_policy()
            self._policy_verified = True

        items = await self.data.claim_deliveries(
            self.worker_id,
            limit=self.limit,
            lease_seconds=self.lease_seconds,
        )
        counts = {
            "claimed": len(items),
            "delivered": 0,
            "skipped": 0,
            "permanent": 0,
            "retryable": 0,
            "auth_error": 0,
        }
        for item in items:
            try:
                outcome = await self._deliver_one(item)
                if outcome in counts:
                    counts[outcome] += 1
            except Exception:
                self.logger.exception(
                    "external channel delivery processing failed delivery=%s",
                    item.get("delivery_id"),
                )
        return counts
