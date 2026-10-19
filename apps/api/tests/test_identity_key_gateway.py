"""The public key adapter preserves frozen request scope without local ORM I/O."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import uuid

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import httpx
import pytest
from starlette.requests import Request

from aap_identity.contract import Principal
from aap_identity.key_contract import decode_key_reply, decode_key_request
from app.api.v1.api_keys import _execute
from app.services.api_key_service import IdentityKeyManagement
from app.services.identity_gateway import IdentityGateway


def _principal():
    return Principal(
        actor_id=uuid.uuid4(),
        name="Key owner",
        email="key-owner@example.test",
        role="employee",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=0,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


@pytest.mark.asyncio
async def test_remote_patch_keeps_omitted_fields_and_makes_no_local_table_calls(
    monkeypatch,
):
    target = uuid.uuid4()
    actor = _principal()
    command = decode_key_request(
        {
            "schema": "aap-identity-keys/v1",
            "operation": "update_api_key",
            "operation_id": str(uuid.uuid4()),
            "token": "private-bearer",
            "payload": {"key_id": str(target), "patch": {"scopes": []}},
        }
    )
    calls = []

    def respond(request):
        body = json.loads(request.content)
        assert body["payload"]["patch"] == {"scopes": []}
        assert request.url.path == "/v1/identity/api-keys/execute"
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "schema": "aap-identity-keys/v1",
                "ok": True,
                "operation": body["operation"],
                "operation_id": body["operation_id"],
                "principal": actor.model_dump(mode="json"),
                "data": {
                    "key": {
                        "id": str(target),
                        "name": "Automation",
                        "key_prefix": "ak_123456789",
                        "scopes": [],
                        "expires_at": None,
                        "last_used_at": None,
                        "revoked_at": None,
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                },
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.api_key_service.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    manager = IdentityKeyManagement(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
    )
    reply = await manager.execute(command)
    assert reply.data.key.id == target and calls == ["/v1/identity/api-keys/execute"]


@pytest.mark.asyncio
async def test_public_key_result_cannot_replace_the_admitted_actor():
    admitted = _principal()
    request = Request({"type": "http"})
    request.state.identity_principal = admitted
    foreign = admitted.model_copy(update={"actor_id": uuid.uuid4()})

    class Owner:
        async def execute(self, command):
            return decode_key_reply(
                command,
                {
                    "schema": "aap-identity-keys/v1",
                    "ok": True,
                    "operation": command.operation,
                    "operation_id": str(command.operation_id),
                    "principal": foreign.model_dump(mode="json"),
                    "data": {"items": []},
                },
            )

    with pytest.raises(HTTPException) as error:
        await _execute(
            request,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="private-bearer"),
            Owner(),
            "list_api_keys",
            {},
        )
    assert error.value.status_code == 503
    assert request.state.identity_principal is admitted
