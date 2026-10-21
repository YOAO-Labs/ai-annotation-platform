from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest

from aap_identity.briefs_client import BriefsClient
from aap_identity.briefs_contract import (
    BRIEFS_PROTOCOL,
    GetUserBriefsPayload,
    GetUserBriefsRequest,
    decode_native_briefs_command,
)
from aap_identity.contract import IdentityUnavailable
from briefs_helpers import briefs_request, legacy_fixture, native_briefs_success


class _Transaction:
    def __init__(self, owner):
        self.owner = owner

    async def execute_briefs_command(self, payload):
        command = decode_native_briefs_command(payload)
        self.owner.commands += 1
        return native_briefs_success(command)


class _Data:
    def __init__(self):
        self.policy_calls = 0
        self.transactions = 0
        self.commands = 0

    async def briefs_policy(self):
        self.policy_calls += 1

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield _Transaction(self)


class _BorrowedData:
    def __init__(self):
        self.transactions = 0
        self.commands = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        yield _Transaction(self)


class _WaitingPolicyData(_BorrowedData):
    def __init__(self):
        super().__init__()
        self.policy_calls = 0

    async def briefs_policy(self):
        self.policy_calls += 1
        await asyncio.Event().wait()


def _public_user(user_id: uuid.UUID, *, name="Ada"):
    return {
        "id": str(user_id),
        "name": name,
        "email": "ada@example.test",
        "role": "employee",
        "avatar_initial": name[:1].upper(),
        "avatar_ref": "preset:ada",
    }


def _public_reply(request, users):
    project_id = (
        request.payload.project_id
        if request.operation == "get_project_user_briefs"
        else None
    )
    return {
        "schema": BRIEFS_PROTOCOL,
        "ok": True,
        "operation": request.operation,
        "data": {
            "project_id": str(project_id) if project_id is not None else None,
            "users": users,
        },
    }


@pytest.mark.asyncio
async def test_sql_data_checks_optional_policy_then_uses_briefs_manager():
    user_id = uuid.UUID(legacy_fixture()["inputs"][0]["id"])
    request = briefs_request("get_user_briefs", [user_id])
    data = _Data()

    reply = await BriefsClient(data).execute(request)

    assert [user.id for user in reply.data.users] == [user_id]
    assert data.policy_calls == 1
    assert data.transactions == 1
    assert data.commands == 1


@pytest.mark.asyncio
async def test_borrowed_data_adapter_does_not_require_a_policy_method():
    user_id = uuid.UUID(legacy_fixture()["inputs"][0]["id"])
    request = briefs_request("get_user_briefs", [user_id])
    data = _BorrowedData()

    reply = await BriefsClient(data).execute(request)

    assert [user.id for user in reply.data.users] == [user_id]
    assert data.transactions == 1


@pytest.mark.asyncio
async def test_data_factory_is_resolved_only_for_nonempty_sql_reads():
    user_id = uuid.UUID(legacy_fixture()["inputs"][0]["id"])
    data = _Data()
    factory_calls = 0

    def data_factory():
        nonlocal factory_calls
        factory_calls += 1
        return data

    reply = await BriefsClient(data_factory=data_factory).execute(
        briefs_request("get_user_briefs", [user_id])
    )

    assert [user.id for user in reply.data.users] == [user_id]
    assert factory_calls == 1
    assert data.policy_calls == data.transactions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_stage", ["factory", "policy"])
async def test_execute_deadline_bounds_sql_factory_and_policy(wait_stage):
    user_id = uuid.UUID(legacy_fixture()["inputs"][0]["id"])
    request = briefs_request("get_user_briefs", [user_id])
    factory_calls = 0
    data = _WaitingPolicyData()

    async def data_factory():
        nonlocal factory_calls
        factory_calls += 1
        await asyncio.Event().wait()

    client = (
        BriefsClient(data_factory=data_factory, timeout_seconds=0.01)
        if wait_stage == "factory"
        else BriefsClient(data, timeout_seconds=0.01)
    )

    with pytest.raises(IdentityUnavailable):
        await asyncio.wait_for(client.execute(request), timeout=1)

    if wait_stage == "factory":
        assert factory_calls == 1
        assert data.policy_calls == 0
    else:
        assert factory_calls == 0
        assert data.policy_calls == 1
    assert data.transactions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "project_id"),
    [
        ("get_user_briefs", None),
        ("get_project_user_briefs", uuid.UUID(int=99)),
    ],
)
async def test_empty_ids_skip_provider_validation_and_all_io(operation, project_id):
    factory_calls = 0

    def data_factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("empty requests must not resolve data")

    request = briefs_request(operation, [], project_id=project_id)
    reply = await BriefsClient(
        data_factory=data_factory,
        identity_url="not an HTTP URL",
        identity_token=None,
    ).execute(request)

    assert reply.data.users == []
    assert reply.data.project_id == project_id
    assert factory_calls == 0


@pytest.mark.asyncio
async def test_typed_request_is_redecoded_before_provider_dispatch():
    request = GetUserBriefsRequest.model_construct(
        protocol="wrong-protocol",
        operation="get_user_briefs",
        payload=GetUserBriefsPayload.model_construct(user_ids=[None]),
    )
    data = _Data()

    with pytest.raises(IdentityUnavailable):
        await BriefsClient(data).execute(request)

    assert data.policy_calls == data.transactions == data.commands == 0


@pytest.mark.asyncio
async def test_http_owner_wins_over_sql_and_returns_bound_strict_reply():
    user_id = uuid.UUID(int=1)
    request = briefs_request("get_user_briefs", [user_id])
    data = _Data()
    observed = {}

    def handle(http_request):
        observed["url"] = str(http_request.url)
        observed["authorization"] = http_request.headers.get("authorization")
        observed["content_type"] = http_request.headers.get("content-type")
        observed["payload"] = json.loads(http_request.content)
        return httpx.Response(
            200,
            json=_public_reply(request, [_public_user(user_id)]),
        )

    client = BriefsClient(
        data,
        identity_url="https://identity.example/base/",
        identity_token="identity-briefs-secret",
        transport=httpx.MockTransport(handle),
    )
    reply = await client.execute(request)

    assert observed["url"] == "https://identity.example/base/v1/identity/briefs/execute"
    assert observed["authorization"] == "Bearer identity-briefs-secret"
    assert observed["content_type"] == "application/json"
    assert observed["payload"] == request.model_dump(mode="json", by_alias=True)
    assert "identity-briefs-secret" not in json.dumps(observed["payload"])
    assert [user.id for user in reply.data.users] == [user_id]
    assert data.policy_calls == data.transactions == data.commands == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 503])
async def test_selected_http_failures_never_fall_back_to_sql(status_code):
    data = _Data()
    request = briefs_request("get_user_briefs", [uuid.UUID(int=1)])
    client = BriefsClient(
        data,
        identity_url="https://identity.example",
        identity_token="identity-briefs-secret",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status_code, json={"detail": "failed"})
        ),
    )

    with pytest.raises(IdentityUnavailable):
        await client.execute(request)

    assert data.policy_calls == data.transactions == data.commands == 0


@pytest.mark.asyncio
async def test_invalid_selected_http_configuration_does_not_fall_back_to_sql():
    data = _Data()
    request = briefs_request("get_user_briefs", [uuid.UUID(int=1)])
    client = BriefsClient(
        data,
        identity_url="https://user:password@identity.example",
        identity_token="identity-briefs-secret",
    )

    with pytest.raises(IdentityUnavailable):
        await client.execute(request)

    assert data.policy_calls == data.transactions == data.commands == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_reply", ["unrequested", "unsorted", "avatar"])
async def test_http_decodes_full_reply_scope_order_and_avatar_contract(invalid_reply):
    first, second, extra = (uuid.UUID(int=value) for value in (1, 2, 3))
    request = briefs_request("get_user_briefs", [first, second])
    users = [_public_user(first), _public_user(second)]
    if invalid_reply == "unrequested":
        users = [_public_user(first), _public_user(extra)]
    elif invalid_reply == "unsorted":
        users = list(reversed(users))
    else:
        users[0] = {**users[0], "avatar_initial": "Z"}

    client = BriefsClient(
        identity_url="https://identity.example",
        identity_token="identity-briefs-secret",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=_public_reply(request, users))
        ),
    )

    with pytest.raises(IdentityUnavailable):
        await client.execute(request)
