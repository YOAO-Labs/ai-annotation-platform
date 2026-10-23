"""Compatibility imports for the Notifications-owned delivery protocol."""

from __future__ import annotations

from aap_identity.contract import IdentityUnavailable
from aap_notifications.notification_delivery_contract import (
    NOTIFICATION_DELIVERY_POLICY,
    NOTIFICATION_DELIVERY_POLICY_PROTOCOL,
    NOTIFICATION_DELIVERY_PROTOCOL,
    NOTIFICATION_TYPES,
    NotificationClaimData,
    NotificationClaimReply,
    NotificationClaimRequest,
    NotificationCompletedLease,
    NotificationCompletedPayload,
    NotificationDeliveryPolicyReply,
    NotificationDeliveryUnavailable,
    NotificationFinishData,
    NotificationFinishReply,
    NotificationFinishRequest,
    NotificationLease,
    NotificationRequestedLease,
    NotificationRequestedPayload,
    decode_notification_claim_reply as _decode_notification_claim_reply,
    decode_notification_claim_request as _decode_notification_claim_request,
    decode_notification_delivery_policy as _decode_notification_delivery_policy,
    decode_notification_finish_reply as _decode_notification_finish_reply,
    decode_notification_finish_request as _decode_notification_finish_request,
    native_notification_command_json as _native_notification_command_json,
)

__all__ = [
    "NOTIFICATION_DELIVERY_POLICY",
    "NOTIFICATION_DELIVERY_POLICY_PROTOCOL",
    "NOTIFICATION_DELIVERY_PROTOCOL",
    "NOTIFICATION_TYPES",
    "NotificationClaimData",
    "NotificationClaimReply",
    "NotificationClaimRequest",
    "NotificationCompletedLease",
    "NotificationCompletedPayload",
    "NotificationDeliveryPolicyReply",
    "NotificationDeliveryUnavailable",
    "NotificationFinishData",
    "NotificationFinishReply",
    "NotificationFinishRequest",
    "NotificationLease",
    "NotificationRequestedLease",
    "NotificationRequestedPayload",
    "decode_notification_claim_reply",
    "decode_notification_claim_request",
    "decode_notification_delivery_policy",
    "decode_notification_finish_reply",
    "decode_notification_finish_request",
    "native_notification_command_json",
]


def _identity_compatible_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except NotificationDeliveryUnavailable:
        raise IdentityUnavailable() from None


def decode_notification_delivery_policy(payload):
    return _identity_compatible_call(_decode_notification_delivery_policy, payload)


def decode_notification_claim_request(payload):
    return _identity_compatible_call(_decode_notification_claim_request, payload)


def decode_notification_finish_request(payload):
    return _identity_compatible_call(_decode_notification_finish_request, payload)


def decode_notification_claim_reply(payload):
    return _identity_compatible_call(_decode_notification_claim_reply, payload)


def decode_notification_finish_reply(payload):
    return _identity_compatible_call(_decode_notification_finish_reply, payload)


def native_notification_command_json(command):
    return _identity_compatible_call(_native_notification_command_json, command)
