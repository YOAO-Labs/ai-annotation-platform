from __future__ import annotations


import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from aap_identity.deactivation_contract import (
    DeactivationDuePolicyReply,
    DeactivationDueReply,
    DeactivationPolicyReply,
)
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from deactivation_helpers import CONTROL, SECRET, due_request


class BaseData:
    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }


class DeactivationData:
    async def deactivation_policy(self):
        return DeactivationPolicyReply(
            schema="aap-identity-deactivation-policy/v1",
            policy="aap-identity-deactivation/v1",
        )


class DueData:
    def __init__(self):
        self.request = None

    async def due_policy(self):
        return DeactivationDuePolicyReply(
            schema="aap-identity-deactivation-due-policy/v1",
            policy="aap-identity-deactivation-due/v1",
        )

    async def process_due_deactivations(self, request):
        self.request = request
        return DeactivationDueReply(
            schema="aap-identity-deactivation-due/v1",
            ok=True,
            operation_id=request.operation_id,
            processed=1,
            examined=1,
            blocked_last_super_admin=0,
            has_more=False,
        )


def settings(**changes):
    values = {
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(CONTROL),
        "signing_secret": SecretStr(SECRET),
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_user_and_operator_routes_fail_closed_without_explicit_bindings():
    app = create_app(settings(), data=BaseData())
    headers = {"Authorization": f"Bearer {CONTROL}", "Content-Type": "application/json"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        user = await client.post(
            "/v1/identity/deactivation/execute", headers=headers, content=b"{}"
        )
        due = await client.post(
            "/v1/identity/deactivation/process-due", headers=headers, content=b"{}"
        )
    assert user.status_code == 503
    assert due.status_code == 503


@pytest.mark.asyncio
async def test_deactivation_routes_require_control_bearer_and_json():
    app = create_app(
        settings(),
        data=BaseData(),
        deactivation_data=DeactivationData(),
        deactivation_due_data=DueData(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthenticated = await client.post(
            "/v1/identity/deactivation/execute",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        invalid_content = await client.post(
            "/v1/identity/deactivation/process-due",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "text/plain",
            },
            content=b"{}",
        )
    assert unauthenticated.status_code == 401
    assert invalid_content.status_code == 400


@pytest.mark.asyncio
async def test_user_command_invalid_wire_is_400_before_user_authentication():
    app = create_app(settings(), data=BaseData(), deactivation_data=DeactivationData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/deactivation/execute",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_due_route_passes_the_bounded_operation_to_operator_owner():
    owner = DueData()
    app = create_app(settings(), data=BaseData(), deactivation_due_data=owner)
    request = due_request(limit=16)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/deactivation/process-due",
            headers={
                "Authorization": f"Bearer {CONTROL}",
                "Content-Type": "application/json",
            },
            content=request.model_dump_json(by_alias=True),
        )
    assert response.status_code == 200
    assert owner.request == request
    assert response.json()["processed"] == 1


@pytest.mark.asyncio
async def test_ready_requires_both_exact_deactivation_policies():
    app = create_app(
        settings(),
        data=BaseData(),
        deactivation_data=DeactivationData(),
        deactivation_due_data=DueData(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_optional_user_and_operator_dsn_files(tmp_path):
    user_file = tmp_path / "user-dsn"
    operator_file = tmp_path / "operator-dsn"
    user_file.write_text("postgresql://identity.invalid/deactivation\n")
    operator_file.write_text("postgresql://identity.invalid/operator\n")
    configured = settings(
        deactivation_database_url_file=str(user_file),
        deactivation_operator_database_url_file=str(operator_file),
    )
    assert configured.deactivation_database_url.get_secret_value().endswith(
        "/deactivation"
    )
    assert configured.deactivation_operator_database_url.get_secret_value().endswith(
        "/operator"
    )

    with pytest.raises(ValueError, match="at most one deactivation_database_url"):
        settings(
            deactivation_database_url=SecretStr("postgresql://identity.invalid/db"),
            deactivation_database_url_file=str(user_file),
        )
