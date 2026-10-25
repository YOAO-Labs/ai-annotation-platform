from __future__ import annotations

import uuid

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.notification_delivery_contract import (
    NotificationClaimRequest as IdentityNotificationClaimRequest,
    NotificationDeliveryPolicyReply as IdentityNotificationDeliveryPolicyReply,
    decode_notification_delivery_policy as identity_decode_policy,
)
from aap_identity.notification_delivery_postgres import (
    NotificationDeliveryPostgresData as IdentityNotificationDeliveryPostgresData,
)
from aap_identity_runtime.notification_config import (
    NotificationPublisherSettings as IdentityNotificationPublisherSettings,
)
from aap_identity_runtime.notification_worker import (
    NotificationDeliveryPostgresData as IdentityWorkerNotificationDeliveryPostgresData,
    NotificationPublisher as IdentityNotificationPublisher,
)
from aap_notifications.delivery_config import NotificationPublisherSettings
from aap_notifications.delivery_worker import NotificationPublisher
from aap_notifications.notification_delivery_contract import (
    NOTIFICATION_DELIVERY_POLICY,
    NOTIFICATION_DELIVERY_POLICY_PROTOCOL,
    NotificationClaimRequest,
    NotificationDeliveryPolicyReply,
    NotificationDeliveryUnavailable,
    decode_notification_delivery_policy,
)
from aap_notifications.notification_delivery_postgres import (
    NotificationDeliveryPostgresData,
)


def test_identity_value_and_configuration_imports_are_compatibility_aliases():
    assert IdentityNotificationClaimRequest is NotificationClaimRequest
    assert IdentityNotificationDeliveryPolicyReply is NotificationDeliveryPolicyReply
    assert IdentityNotificationPublisherSettings is NotificationPublisherSettings
    assert (
        IdentityWorkerNotificationDeliveryPostgresData
        is IdentityNotificationDeliveryPostgresData
    )
    assert IdentityNotificationDeliveryPostgresData.__bases__ == (
        NotificationDeliveryPostgresData,
    )
    assert IdentityNotificationPublisher.__bases__ == (NotificationPublisher,)


def test_identity_contract_path_keeps_identity_unavailable_error():
    payload = {
        "schema": NOTIFICATION_DELIVERY_POLICY_PROTOCOL,
        "policy": "wrong",
    }
    with pytest.raises(NotificationDeliveryUnavailable):
        decode_notification_delivery_policy(payload)
    with pytest.raises(IdentityUnavailable):
        identity_decode_policy(payload)


@pytest.mark.asyncio
async def test_identity_worker_path_translates_legacy_port_errors():
    class Data:
        async def claim_deliveries(self, *_args, **_kwargs):
            raise IdentityUnavailable()

        async def finish_delivery(self, *_args, **_kwargs):
            raise AssertionError("claim failure must not finish a lease")

    with pytest.raises(IdentityUnavailable):
        await IdentityNotificationPublisher(Data(), object()).run_once()


@pytest.mark.asyncio
async def test_identity_postgres_client_translates_invalid_owner_requests():
    with pytest.raises(IdentityUnavailable):
        await IdentityNotificationDeliveryPostgresData(
            "postgresql://publisher/db"
        ).claim_deliveries(uuid.uuid4(), limit=0)


def test_identity_module_cli_path_remains_callable():
    from aap_identity_runtime import notification_worker

    assert callable(notification_worker.main)
    assert (
        notification_worker.NOTIFICATION_DELIVERY_POLICY == NOTIFICATION_DELIVERY_POLICY
    )
