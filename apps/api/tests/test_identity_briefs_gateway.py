"""Profile-owner selection, closed replies, and caller transaction preservation."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from aap_identity.briefs_contract import decode_briefs_request
from aap_identity.contract import IdentityUnavailable
from app.services import identity_briefs_commands as commands_module
from app.services import user_brief
from app.services.identity_briefs_commands import IdentityBriefsCommands
from app.services.identity_briefs_data import IdentityBriefsData
from app.services.identity_contracts import (
    IdentityBriefsUnavailable,
    IdentityGatewayUnavailable,
)
from app.services.identity_gateway import IdentityGateway

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/identity_briefs_legacy.json").read_text()
)


def _command(project=False):
    payload = {"user_ids": list(_FIXTURE["responses"]["global"])}
    if project:
        payload["project_id"] = _FIXTURE["project_id"]
    return decode_briefs_request(
        {
            "schema": "aap-identity-briefs/v1",
            "operation": "get_project_user_briefs" if project else "get_user_briefs",
            "payload": payload,
        }
    )


def _reply(command, *, native=False):
    project = command.operation == "get_project_user_briefs"
    values = _FIXTURE["responses"]["project" if project else "global"]
    users = [copy.deepcopy(values[key]) for key in sorted(values)]
    if native:
        for user in users:
            del user["avatar_initial"]
    return {
        "schema": "aap-identity-briefs-native/v1"
        if native
        else "aap-identity-briefs/v1",
        "ok": True,
        "operation": command.operation,
        "data": {
            "project_id": _FIXTURE["project_id"] if project else None,
            "users": users,
        },
    }


def _native_command(command):
    result = command.model_dump(mode="json", by_alias=True)
    result["schema"] = "aap-identity-briefs-native/v1"
    return result


def _db(raw, *, autoflush=True):
    return SimpleNamespace(
        autoflush=autoflush,
        scalar=AsyncMock(return_value=raw),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )


@pytest.fixture
def embedded_selector(monkeypatch):
    monkeypatch.setattr(commands_module.settings, "identity_briefs_database_url", "")
    monkeypatch.setattr(
        commands_module.settings, "identity_briefs_database_url_file", ""
    )
    monkeypatch.setattr(user_brief, "get_identity_gateway", lambda: IdentityGateway())


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("autoflush", [False, True])
async def test_embedded_briefs_reproduce_legacy_without_owning_transaction(
    embedded_selector, project, autoflush
):
    command = _command(project)
    db = _db(_reply(command, native=True), autoflush=autoflush)
    ids = list(command.payload.user_ids) + [None, command.payload.user_ids[0]]
    result = (
        await user_brief.resolve_briefs_with_project_role(
            db, command.payload.project_id, iter(ids)
        )
        if project
        else await user_brief.resolve_briefs(db, iter(ids))
    )
    assert {
        key: value.model_dump(mode="json") for key, value in result.items()
    } == _FIXTURE["responses"]["project" if project else "global"]
    assert db.scalar.await_count == 1
    assert db.flush.await_count == int(autoflush)
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    encoded = json.loads(
        db.scalar.call_args.args[1]["command"]
        if len(db.scalar.call_args.args) > 1
        else db.scalar.call_args.kwargs["params"]["command"]
    )
    assert set(encoded) == {"schema", "operation", "payload"}
    assert set(encoded["payload"]["user_ids"]) == set(_FIXTURE["responses"]["global"])


async def test_empty_and_invalid_inputs_never_acquire_owner(monkeypatch):
    def forbid(*_args, **_kwargs):
        pytest.fail("No owner or gateway should be acquired")

    monkeypatch.setattr(user_brief, "IdentityBriefsCommands", forbid)
    monkeypatch.setattr(user_brief, "get_identity_gateway", forbid)
    assert await user_brief.resolve_briefs(object(), iter([None])) == {}
    assert (
        await user_brief.resolve_briefs_with_project_role(object(), uuid.uuid4(), [])
        == {}
    )
    with pytest.raises(ValueError):
        await user_brief.resolve_briefs(object(), ["not-a-uuid"])


@pytest.mark.parametrize("failure", ["corrupt", "sql", "cancel"])
async def test_embedded_failures_leave_transaction_ownership_to_caller(failure):
    command = _command()
    raw = _reply(command, native=True)
    if failure == "corrupt":
        raw["data"]["users"][0]["password_hash"] = "must-not-leak"
    db = _db(raw)
    expected = IdentityUnavailable
    if failure == "sql":
        db.scalar.side_effect = SQLAlchemyError("unavailable")
    elif failure == "cancel":
        db.scalar.side_effect = asyncio.CancelledError()
        expected = asyncio.CancelledError
    with pytest.raises(expected):
        async with IdentityBriefsData(db).transaction() as transaction:
            await transaction.execute_briefs_command(_native_command(command))
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


@pytest.mark.parametrize("project", [False, True])
async def test_remote_selection_needs_no_user_credential_and_ignores_local_dsn(
    monkeypatch, project
):
    command = _command(project)
    monkeypatch.setattr(
        commands_module.settings, "identity_briefs_database_url", "invalid"
    )
    monkeypatch.setattr(
        commands_module.settings, "identity_briefs_database_url_file", "invalid"
    )

    async def remote(identity, path, payload):
        assert identity.identity_token == "internal-control"
        assert path == "/v1/identity/briefs/execute"
        assert json.loads(payload) == command.model_dump(mode="json", by_alias=True)
        return json.dumps(_reply(command)).encode()

    def forbid(*_args, **_kwargs):
        pytest.fail("Remote profile reads must not acquire local data")

    monkeypatch.setattr(commands_module, "_remote_json", remote)
    monkeypatch.setattr(commands_module, "IdentityBriefsData", forbid)
    monkeypatch.setattr(commands_module, "BriefsPostgresData", forbid)
    result = await IdentityBriefsCommands(
        object(),
        IdentityGateway(
            identity_url="http://identity.invalid", identity_token="internal-control"
        ),
    ).execute(command)
    assert result.model_dump(mode="json", by_alias=True) == _reply(command)


@pytest.mark.parametrize(
    "failure", ["transport", "scope", "extra-id", "initial", "extra-field"]
)
async def test_remote_errors_and_untrusted_replies_never_fall_back(
    monkeypatch, failure
):
    command = _command(True)
    raw = _reply(command)
    if failure == "scope":
        raw["data"]["project_id"] = str(uuid.uuid4())
    elif failure == "extra-id":
        raw["data"]["users"][0]["id"] = str(uuid.uuid4())
    elif failure == "initial":
        raw["data"]["users"][0]["avatar_initial"] = "wrong"
    elif failure == "extra-field":
        raw["data"]["users"][0]["preferences"] = {}

    async def remote(*_args):
        if failure == "transport":
            raise IdentityGatewayUnavailable()
        return json.dumps(raw).encode()

    def forbid(*_args, **_kwargs):
        pytest.fail("Selected remote failures must not fall back")

    monkeypatch.setattr(commands_module, "_remote_json", remote)
    monkeypatch.setattr(commands_module, "IdentityBriefsData", forbid)
    with pytest.raises(IdentityBriefsUnavailable):
        await IdentityBriefsCommands(
            object(),
            IdentityGateway(
                identity_url="http://identity.invalid",
                identity_token="internal-control",
            ),
        ).execute(command)


async def test_explicit_dsn_requires_policy_and_never_uses_caller(monkeypatch):
    monkeypatch.setattr(
        commands_module.settings,
        "identity_briefs_database_url",
        "postgresql://localhost/briefs_test",
    )
    monkeypatch.setattr(
        commands_module.settings, "identity_briefs_database_url_file", ""
    )
    policy = AsyncMock(side_effect=IdentityUnavailable())
    selected = SimpleNamespace(briefs_policy=policy)
    monkeypatch.setattr(commands_module, "BriefsPostgresData", lambda _dsn: selected)

    def forbid(*_args, **_kwargs):
        pytest.fail("Invalid selected DSN must not use caller data")

    monkeypatch.setattr(commands_module, "IdentityBriefsData", forbid)
    with pytest.raises(IdentityBriefsUnavailable):
        await IdentityBriefsCommands(object(), IdentityGateway()).execute(_command())
    policy.assert_awaited_once()


async def test_public_unavailable_response_contains_no_owner_details():
    from app.main import _identity_briefs_error_handler

    app = FastAPI()
    app.add_exception_handler(IdentityBriefsUnavailable, _identity_briefs_error_handler)

    @app.get("/probe")
    async def probe():
        raise IdentityBriefsUnavailable("private-connection-details")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/probe")
    assert response.status_code == 503
    assert response.json() == {"detail": "身份资料服务暂不可用"}
