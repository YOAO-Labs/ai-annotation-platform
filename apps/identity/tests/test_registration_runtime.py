"""The private registration read retains control authentication and read bounds."""

from __future__ import annotations

import httpx
import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.registration_contract import REGISTRATION_STATUS_PROTOCOL
from aap_identity_runtime.api import create_app
from test_recovery_runtime import CONTROL, Data, settings


class StatusData(Data):
    def __init__(self, *, unavailable=False):
        self.calls = []
        self.unavailable = unavailable

    async def read_registration_status(self, fallbacks):
        self.calls.append(fallbacks)
        if self.unavailable:
            raise IdentityUnavailable()
        return {
            "schema": REGISTRATION_STATUS_PROTOCOL,
            "operation": "read_status",
            "ok": True,
            "data": {"allow_open_registration": False},
        }


def _request(operation="read_status"):
    return {"schema": REGISTRATION_STATUS_PROTOCOL, "operation": operation}


_HEADERS = {"authorization": "Bearer " + CONTROL}
_PATH = "/v1/identity/recovery/registration-status"


@pytest.mark.asyncio
async def test_auth_only_runtime_can_report_defaults_without_advertising_registration():
    application = create_app(
        settings(
            recovery_database_url=None,
            recovery_mail_key=None,
            allow_open_registration=True,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://owner.test"
    ) as client:
        default = await client.post(
            _PATH, headers=_HEADERS, json=_request("read_deployment_default")
        )
        effective = await client.post(_PATH, headers=_HEADERS, json=_request())
    assert default.status_code == 200
    assert default.json()["data"] == {"allow_open_registration": True}
    assert effective.status_code == 503


@pytest.mark.asyncio
async def test_default_read_remains_available_with_a_bad_override_but_effective_read_fails():
    data = StatusData(unavailable=True)
    application = create_app(settings(allow_open_registration=True), recovery_data=data)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://owner.test"
    ) as client:
        default = await client.post(
            _PATH, headers=_HEADERS, json=_request("read_deployment_default")
        )
        assert default.status_code == 200
        assert default.json()["data"] == {"allow_open_registration": True}
        assert data.calls == []
        status = await client.post(_PATH, headers=_HEADERS, json=_request())
        assert status.status_code == 503
        assert len(data.calls) == 1


@pytest.mark.asyncio
async def test_effective_read_uses_exact_recovery_owner_fallbacks_and_database_false():
    data = StatusData()
    application = create_app(
        settings(
            allow_open_registration=True, frontend_base_url="http://recovery-owner.test"
        ),
        recovery_data=data,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://owner.test"
    ) as client:
        status = await client.post(_PATH, headers=_HEADERS, json=_request())
    assert status.status_code == 200
    assert status.json()["data"] == {"allow_open_registration": False}
    assert data.calls == [
        {
            "allow_open_registration": True,
            "frontend_base_url": "http://recovery-owner.test",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["unauthorized", "content_type", "oversized", "unknown", "caller_policy", "deep"],
)
async def test_private_status_rejects_unauthorized_or_invalid_reads_without_data_access(
    fault,
):
    data = StatusData()
    application = create_app(settings(), recovery_data=data)
    headers = {**_HEADERS, "content-type": "application/json"}
    body = b'{"schema":"aap-identity-registration-status/v1","operation":"read_status"}'
    expected = 400
    if fault == "unauthorized":
        headers["authorization"] = "Bearer wrong-control"
        expected = 401
    elif fault == "content_type":
        headers["content-type"] = "text/plain"
    elif fault == "oversized":
        body = b" " * 65537
    elif fault == "unknown":
        body = b'{"schema":"aap-identity-registration-status/v1","operation":"register_open"}'
    elif fault == "caller_policy":
        body = b'{"schema":"aap-identity-registration-status/v1","operation":"read_status","allow_open_registration":true}'
    else:
        body = b"[" * 1000 + b"0" + b"]" * 1000
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://owner.test"
    ) as client:
        result = await client.post(_PATH, headers=headers, content=body)
    assert result.status_code == expected
    assert data.calls == []
