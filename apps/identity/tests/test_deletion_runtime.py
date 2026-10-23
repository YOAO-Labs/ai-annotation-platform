from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from aap_identity.contract import Principal, principal_reply
from aap_identity.deletion_contract import (
    NATIVE_DELETION_PROTOCOL,
    DeletionPolicyReply,
)
from aap_identity_runtime.api import create_app
from aap_identity_runtime.config import Settings
from deletion_helpers import (
    DELETION_CONTROL,
    DELETION_SECRET,
    deletion_request,
)
from session_helpers import ACTOR_ID, fixed_jwt


class BaseData:
    def __init__(self, actor: Principal | None = None):
        self.actor = actor

    async def authentication_policy(self):
        return {
            "schema": "aap-identity-policy/v1",
            "issuer": "aap-identity/v1",
            "refresh_grace_seconds": 604800,
        }

    async def read_jwt_principal(
        self, actor_id, jti, generation, expires_at, for_refresh=False
    ):
        return principal_reply(self.actor)


class DeletionData:
    def __init__(self, policy=None, native_failure=None):
        self.policy = policy or DeletionPolicyReply(
            schema="aap-identity-deletions-policy/v1",
            policy="aap-identity-deletions/v1",
        )
        self.native_failure = native_failure
        self.committed = False
        self.rolled_back = False

    async def deletion_policy(self):
        return self.policy

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True

    async def execute_deletion_command(self, _payload):
        return self.native_failure


def settings(**changes):
    values = {
        "database_url": SecretStr("postgresql://identity.invalid/identity"),
        "token": SecretStr(DELETION_CONTROL),
        "signing_secret": SecretStr(DELETION_SECRET),
    }
    values.update(changes)
    return Settings(**values)


@pytest.mark.asyncio
async def test_deletion_route_is_unavailable_without_owner_binding():
    app = create_app(settings(), data=BaseData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/deletions/execute",
            headers={
                "Authorization": f"Bearer {DELETION_CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_deletion_route_requires_control_bearer_json_and_closed_request():
    app = create_app(settings(), data=BaseData(), deletion_data=DeletionData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        unauthenticated = await client.post(
            "/v1/identity/deletions/execute",
            headers={"Content-Type": "application/json"},
            content=b"{}",
        )
        wrong_media = await client.post(
            "/v1/identity/deletions/execute",
            headers={
                "Authorization": f"Bearer {DELETION_CONTROL}",
                "Content-Type": "text/plain",
            },
            content=b"{}",
        )
        invalid = await client.post(
            "/v1/identity/deletions/execute",
            headers={
                "Authorization": f"Bearer {DELETION_CONTROL}",
                "Content-Type": "application/json",
            },
            content=b"{}",
        )
    assert unauthenticated.status_code == 401
    assert wrong_media.status_code == 400
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_ready_requires_exact_deletion_owner_policy():
    app = create_app(
        settings(),
        data=BaseData(),
        deletion_data=DeletionData({"schema": "wrong", "policy": "wrong"}),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 503

    app = create_app(settings(), data=BaseData(), deletion_data=DeletionData())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_pending_task_failure_returns_closed_409_data_not_owner_unavailable():
    actor_id = ACTOR_ID
    credential_id = uuid.uuid4()
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    actor = Principal(
        actor_id=actor_id,
        name="Current admin",
        email="admin@example.test",
        role="super_admin",
        is_active=True,
        credential_type="jwt",
        credential_id=credential_id,
        generation=7,
        scopes=None,
        expires_at=expires_at,
    )
    token = fixed_jwt(
        actor_id,
        "super_admin",
        int(expires_at.timestamp()),
        credential_id,
        7,
        secret=DELETION_SECRET,
    )
    sample_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    deletion_data = DeletionData(
        native_failure={
            "schema": NATIVE_DELETION_PROTOCOL,
            "ok": False,
            "error": {
                "code": "has_pending_tasks",
                "status_code": 409,
                "detail": {
                    "reason": "has_pending_tasks",
                    "pending_task_count": 1,
                    "locked_task_count": 0,
                    "sample_task_ids": [sample_id],
                    "message": "该用户当前有 1 个未完成任务、0 个锁定任务，需先转交",
                },
            },
        }
    )
    app = create_app(settings(), data=BaseData(actor), deletion_data=deletion_data)
    request = deletion_request(token=token)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://identity.test"
    ) as client:
        response = await client.post(
            "/v1/identity/deletions/execute",
            headers={
                "Authorization": f"Bearer {DELETION_CONTROL}",
                "Content-Type": "application/json",
            },
            content=request.model_dump_json(by_alias=True).encode(),
        )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == "has_pending_tasks"
    assert response.json()["error"]["status_code"] == 409
    assert response.json()["error"]["detail"]["sample_task_ids"] == [sample_id]
    assert token not in response.text
    assert deletion_data.rolled_back and not deletion_data.committed


def test_optional_deletion_dsn_file_disables_or_selects_only_this_owner(tmp_path):
    disabled = settings(deletion_database_url_file=" ")
    assert disabled.deletion_database_url is None
    assert disabled.deletion_database_url_file is None

    path = tmp_path / "deletion-dsn"
    path.write_text("postgresql://identity.invalid/deletions\n")
    configured = settings(deletion_database_url_file=str(path))
    assert configured.deletion_database_url.get_secret_value().endswith("/deletions")
    with pytest.raises(ValidationError, match="Set at most one deletion_database_url"):
        settings(
            deletion_database_url=SecretStr("postgresql://other.invalid/deletions"),
            deletion_database_url_file=str(path),
        )
