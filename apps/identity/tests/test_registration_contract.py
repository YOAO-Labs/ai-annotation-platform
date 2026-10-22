"""Registration reads bind the owner defaults and the exact requested operation."""

from __future__ import annotations

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.registration_contract import (
    REGISTRATION_STATUS_PROTOCOL,
    RegistrationStatusRequest,
    decode_registration_status_reply,
    decode_registration_status_request,
)
from aap_identity.registration_reader import RegistrationStatusReader


def request(*, default=False):
    return RegistrationStatusRequest(
        schema=REGISTRATION_STATUS_PROTOCOL,
        operation="read_deployment_default" if default else "read_status",
    )


def reply(*, default=False, enabled=True):
    return {
        "schema": REGISTRATION_STATUS_PROTOCOL,
        "operation": request(default=default).operation,
        "ok": True,
        "data": {"allow_open_registration": enabled},
    }


@pytest.mark.parametrize("enabled", [False, True])
async def test_owner_default_does_not_read_or_validate_database_overrides(enabled):
    class UnavailableData:
        async def read_registration_status(self, _fallbacks):
            raise AssertionError("Default lookup must not read a malformed override")

    result = await RegistrationStatusReader(
        UnavailableData(),
        allow_open_registration=enabled,
        frontend_base_url="http://owner.test",
    ).read(request(default=True))
    assert result.data.allow_open_registration is enabled


async def test_effective_read_uses_owner_fallbacks_and_preserves_database_false():
    calls = []

    class Data:
        async def read_registration_status(self, fallbacks):
            calls.append(fallbacks)
            return reply(enabled=False)

    result = await RegistrationStatusReader(
        Data(), allow_open_registration=True, frontend_base_url="http://owner.test"
    ).read(request())
    assert result.data.allow_open_registration is False
    assert calls == [
        {"allow_open_registration": True, "frontend_base_url": "http://owner.test"}
    ]


@pytest.mark.parametrize(
    "change",
    [
        "numeric_ok",
        "numeric_value",
        "string_value",
        "extra",
        "wrong_operation",
        "wrong_protocol",
    ],
)
def test_reply_rejects_coercions_extra_fields_and_wrong_read_binding(change):
    value = reply()
    if change == "numeric_ok":
        value["ok"] = 1
    elif change == "numeric_value":
        value["data"]["allow_open_registration"] = 1
    elif change == "string_value":
        value["data"]["allow_open_registration"] = "true"
    elif change == "extra":
        value["data"]["email_verification_required"] = False
    elif change == "wrong_operation":
        value["operation"] = "read_deployment_default"
    else:
        value["schema"] = "other"
    with pytest.raises(IdentityUnavailable):
        decode_registration_status_reply(value, request())


@pytest.mark.parametrize(
    "value",
    [
        b'{"schema":"aap-identity-registration-status/v1","operation":"read_status","operation":"read_deployment_default"}',
        {
            "schema": REGISTRATION_STATUS_PROTOCOL,
            "operation": "read_status",
            "fallback": True,
        },
        {"protocol": REGISTRATION_STATUS_PROTOCOL, "operation": "read_status"},
        {"schema": REGISTRATION_STATUS_PROTOCOL, "operation": "register_open"},
    ],
)
def test_request_cannot_supply_policy_or_switch_to_a_command(value):
    with pytest.raises(ValueError):
        decode_registration_status_request(value)
