from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aap_notifications.notification_delivery_contract import (
    NOTIFICATION_DELIVERY_POLICY,
    NOTIFICATION_DELIVERY_POLICY_PROTOCOL,
    NOTIFICATION_DELIVERY_PROTOCOL,
    NotificationClaimReply,
    NotificationCompletedLease,
    NotificationDeliveryPolicyReply,
    NotificationFinishReply,
    NotificationFinishRequest,
    NotificationRequestedLease,
    NotificationDeliveryUnavailable,
    decode_notification_claim_reply,
    decode_notification_claim_request,
    decode_notification_delivery_policy,
    decode_notification_finish_reply,
    decode_notification_finish_request,
    native_notification_command_json,
)


def _lease(*, completed: bool = False) -> dict:
    now = datetime.now(UTC).isoformat()
    subject_id = uuid.uuid4()
    payload = (
        {"email": "person@example.test", "name": "Person", "auto": True}
        if completed
        else {
            "email": "person@example.test",
            "name": "Person",
            "role": "project_admin",
            "scheduled_at": now,
        }
    )
    return {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "type": (
            "user.deactivation_completed"
            if completed
            else "user.deactivation_requested"
        ),
        "target_type": "user",
        "target_id": str(subject_id),
        "payload": payload,
        "created_at": now,
        "worker_id": str(uuid.uuid4()),
        "lease_id": str(uuid.uuid4()),
        "lease_expires_at": now,
    }


def _claim(items: list[dict]) -> dict:
    return {
        "schema": NOTIFICATION_DELIVERY_PROTOCOL,
        "ok": True,
        "data": {"items": items},
    }


def test_notification_delivery_policy_is_exact_and_closed():
    policy = decode_notification_delivery_policy(
        {
            "schema": NOTIFICATION_DELIVERY_POLICY_PROTOCOL,
            "policy": NOTIFICATION_DELIVERY_POLICY,
        }
    )
    assert isinstance(policy, NotificationDeliveryPolicyReply)
    assert policy.policy == NOTIFICATION_DELIVERY_POLICY

    for payload in (
        {"schema": NOTIFICATION_DELIVERY_POLICY_PROTOCOL, "policy": "wrong"},
        {
            "schema": NOTIFICATION_DELIVERY_POLICY_PROTOCOL,
            "policy": NOTIFICATION_DELIVERY_POLICY,
            "extra": "field",
        },
    ):
        with pytest.raises(NotificationDeliveryUnavailable):
            decode_notification_delivery_policy(payload)


def test_claim_request_rejects_coercion_unknown_fields_and_duplicate_keys():
    worker_id = uuid.uuid4()
    request = decode_notification_claim_request(
        {
            "schema": NOTIFICATION_DELIVERY_PROTOCOL,
            "worker_id": str(worker_id),
            "limit": 16,
            "lease_seconds": 60,
        }
    )
    assert request.worker_id == worker_id

    for change in (
        {"limit": True},
        {"limit": 101},
        {"lease_seconds": 0},
        {"lease_seconds": 301},
        {"unexpected": None},
    ):
        payload = {
            "schema": NOTIFICATION_DELIVERY_PROTOCOL,
            "worker_id": str(worker_id),
            "limit": 16,
            "lease_seconds": 60,
            **change,
        }
        with pytest.raises(NotificationDeliveryUnavailable):
            decode_notification_claim_request(payload)

    with pytest.raises(NotificationDeliveryUnavailable):
        decode_notification_claim_request(
            (
                '{"schema":"aap-notifications-delivery/v1",'
                f'"worker_id":"{worker_id}","limit":16,"limit":16,'
                '"lease_seconds":60}'
            ).encode()
        )


@pytest.mark.parametrize("completed", [False, True])
def test_claim_reply_uses_only_lifecycle_payloads(completed):
    lease = _lease(completed=completed)
    reply = decode_notification_claim_reply(_claim([lease]))
    item = reply.data.items[0]
    if completed:
        assert isinstance(item, NotificationCompletedLease)
    else:
        assert isinstance(item, NotificationRequestedLease)
    assert item.target_type == "user"
    assert item.target_id == uuid.UUID(lease["target_id"])
    assert "person@example.test" not in repr(item)

    bad = _lease(completed=completed)
    bad["payload"]["reason"] = "must never be delivered"
    with pytest.raises(NotificationDeliveryUnavailable):
        decode_notification_claim_reply(_claim([bad]))


def test_claim_reply_rejects_non_boolean_success_target_mismatch_and_oversized_page():
    lease = _lease()
    with pytest.raises(NotificationDeliveryUnavailable):
        decode_notification_claim_reply({**_claim([lease]), "ok": 1})
    with pytest.raises(ValidationError):
        NotificationClaimReply.model_validate({**_claim([lease]), "ok": 1})

    lease["target_type"] = "project"
    with pytest.raises(NotificationDeliveryUnavailable):
        decode_notification_claim_reply(_claim([lease]))

    items = [_lease() for _ in range(101)]
    with pytest.raises(NotificationDeliveryUnavailable):
        decode_notification_claim_reply(_claim(items))


def test_finish_request_has_closed_safe_outcomes_and_reasons():
    request = {
        "schema": NOTIFICATION_DELIVERY_PROTOCOL,
        "notification_id": str(uuid.uuid4()),
        "worker_id": str(uuid.uuid4()),
        "lease_id": str(uuid.uuid4()),
        "outcome": "delivered",
        "reason": "published",
    }
    assert decode_notification_finish_request(request).outcome == "delivered"
    assert (
        decode_notification_finish_request(
            {**request, "outcome": "retry", "reason": "redis_error"}
        ).reason
        == "redis_error"
    )

    for change in (
        {"reason": "Redis password: secret"},
        {"outcome": "delivered", "reason": "redis_error"},
        {"outcome": "retry", "reason": "published"},
        {"notification_id": str(uuid.uuid4()), "extra": "field"},
    ):
        with pytest.raises(NotificationDeliveryUnavailable):
            decode_notification_finish_request({**request, **change})


def test_finish_reply_requires_strict_booleans_and_closed_data():
    payload = {
        "schema": NOTIFICATION_DELIVERY_PROTOCOL,
        "ok": True,
        "data": {"accepted": False},
    }
    reply = decode_notification_finish_reply(payload)
    assert isinstance(reply, NotificationFinishReply)
    assert reply.data.accepted is False

    with pytest.raises(NotificationDeliveryUnavailable):
        decode_notification_finish_reply({**payload, "data": {"accepted": 1}})
    with pytest.raises(NotificationDeliveryUnavailable):
        decode_notification_finish_reply(
            {**payload, "data": {"accepted": False, "extra": True}}
        )


def test_native_notification_command_json_is_compact_closed_json():
    request = NotificationFinishRequest(
        schema=NOTIFICATION_DELIVERY_PROTOCOL,
        notification_id=uuid.uuid4(),
        worker_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        outcome="delivered",
        reason="published",
    )
    encoded = native_notification_command_json(request)
    assert " " not in encoded
    assert json.loads(encoded)["schema"] == NOTIFICATION_DELIVERY_PROTOCOL
