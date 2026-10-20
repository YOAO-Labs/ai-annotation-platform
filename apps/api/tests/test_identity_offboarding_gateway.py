"""Public offboarding compatibility and configured-owner boundaries."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from aap_identity.contract import IdentityUnavailable
from aap_identity.offboarding_contract import (
    OffboardingFailure,
    decode_offboarding_reply,
    decode_offboarding_request,
)
from app.api.v1 import users
from app.api.v1.offboarding_commands import (
    _reject_offboarding_error,
    get_offboarding_commands,
    require_offboarding_manager,
)
from app.deps import get_current_user, get_db
from app.services.identity_contracts import IdentityGatewayUnavailable
from app.services.identity_gateway import IdentityGateway
from app.services.identity_offboarding_commands import IdentityOffboardingCommands
from app.services.identity_offboarding_data import IdentityOffboardingCommandData
from tests.test_identity_platform_role_gateway import _principal, _user


def _command(operation="commit_offboarding", target=None, **values):
    payload = {"target_user_id": str(target or uuid.uuid4())}
    if operation == "commit_offboarding":
        payload.update(
            preview_version="opaque-version", reason="", mode="handoff", projects=[]
        )
    payload.update(values)
    return decode_offboarding_request(
        {
            "schema": "aap-identity-offboarding/v1",
            "operation": operation,
            "operation_id": str(uuid.uuid4()),
            "token": "fixture-offboarding-token",
            "audit_context": {
                "method": "POST",
                "path": "/api/v1/users/offboarding",
                "ip": None,
                "request_id": None,
            },
            "payload": payload,
        }
    )


def _reply(command, principal):
    user = _user(command.payload.target_user_id, "employee")
    if command.operation == "preview_offboarding":
        data = {
            "preview": {
                "user": user,
                "preview_version": "a" * 64,
                "generated_at": "2030-01-01T00:00:00+00:00",
                "projects": [],
                "api_keys": [],
                "blockers": [],
                "can_commit": True,
            }
        }
    else:
        user.update(
            is_active=False,
            status="offline",
            disabled_kind="suspended",
            disabled_at="2030-01-01T00:00:00+00:00",
            disabled_by=str(principal.actor_id),
        )
        data = {
            "result": {
                "user": user,
                "status": "suspended",
                "mode": command.payload.mode,
                "transfers": [],
                "unresolved": [],
                "revoked_api_key_ids": [],
                "audit_id": 7,
            }
        }
    return {
        "schema": "aap-identity-offboarding/v1",
        "ok": True,
        "operation": command.operation,
        "operation_id": str(command.operation_id),
        "principal": principal.model_dump(mode="json"),
        "data": data,
    }


def _app(principal, commands):
    app = FastAPI()

    @app.middleware("http")
    async def admitted(request, call_next):
        request.state.identity_principal = principal
        return await call_next(request)

    def forbid():
        pytest.fail("Offboarding handler hydrated User or acquired a SQL session")

    app.dependency_overrides[get_offboarding_commands] = lambda: commands
    app.dependency_overrides[get_current_user] = forbid
    app.dependency_overrides[get_db] = forbid
    app.include_router(users.router, prefix="/api/v1/users")
    return app


@pytest.mark.parametrize("operation", ["preview_offboarding", "commit_offboarding"])
async def test_public_offboarding_uses_principal_and_fresh_private_nonce(operation):
    principal, target, calls = _principal(), uuid.uuid4(), []
    version = "opaque\x00" + "😀" * 20_000
    project = str(uuid.uuid4())
    body = {
        "preview_version": version,
        "ignored": "public-extra",
        "projects": [{"project_id": project}, {"project_id": project}],
    }

    class Commands:
        async def execute(self, command):
            calls.append(command)
            assert (
                command.operation == operation
                and command.payload.target_user_id == target
            )
            assert command.audit_context.path.startswith(f"/api/v1/users/{target}/")
            if operation == "commit_offboarding":
                assert command.payload.preview_version == version
                assert (
                    command.payload.reason == "" and command.payload.mode == "handoff"
                )
                assert len(command.payload.projects) == 2
                assert command.payload.projects[0].annotator_receiver_id is None
            return decode_offboarding_reply(
                command, _reply(command, principal), expected_principal=principal
            )

    suffix = (
        "offboarding-preview" if operation == "preview_offboarding" else "offboarding"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(principal, Commands())),
        base_url="http://offboarding.test",
    ) as client:
        for _ in range(2):
            response = await client.request(
                "GET" if operation == "preview_offboarding" else "POST",
                f"/api/v1/users/{target}/{suffix}",
                headers={"Authorization": "Bearer fixture-offboarding-token"},
                **({} if operation == "preview_offboarding" else {"json": body}),
            )
            assert response.status_code == 200, response.text
    assert calls[0].operation_id != calls[1].operation_id
    result = response.json()
    assert len(result) == 7 and len(result["user"]) == 21
    assert result["user"]["preferences"]["unknown"]["count"] == 2**63 - 1


@pytest.mark.parametrize(
    "path,body,status",
    [
        ("not-a-uuid", {"preview_version": "v"}, 422),
        (str(uuid.uuid4()), {}, 422),
        (str(uuid.uuid4()), {"preview_version": "v", "reason": "x" * 501}, 422),
        (
            str(uuid.uuid4()),
            {"preview_version": "v", "projects": [{"project_id": "bad"}]},
            422,
        ),
    ],
)
async def test_public_offboarding_preserves_body_and_uuid_validation(
    path, body, status
):
    class Commands:
        async def execute(self, command):
            pytest.fail("Invalid public input reached the offboarding owner")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(_principal(), Commands())),
        base_url="http://offboarding.test",
    ) as client:
        response = await client.post(
            f"/api/v1/users/{path}/offboarding",
            json=body,
            headers={"Authorization": "Bearer fixture-offboarding-token"},
        )
    assert response.status_code == status, response.text


@pytest.mark.parametrize("role", ["super_admin", "project_admin", "employee", "viewer"])
def test_offboarding_manager_admission_uses_current_principal(role):
    principal = _principal().model_copy(update={"role": role})
    if role in {"super_admin", "project_admin"}:
        assert require_offboarding_manager(principal) is principal
    else:
        with pytest.raises(HTTPException) as denied:
            require_offboarding_manager(principal)
        assert denied.value.status_code == 403


@pytest.mark.parametrize(
    "mutation", ["valid", "missing_principal", "operation_id", "target", "actor"]
)
async def test_remote_offboarding_rejects_misbinding_without_another_writer(
    monkeypatch, mutation
):
    from app.services import identity_offboarding_commands as binding

    principal, command = _principal(), _command()
    raw = _reply(command, principal)
    if mutation == "operation_id":
        raw["operation_id"] = str(uuid.uuid4())
    elif mutation == "target":
        raw["data"]["result"]["user"]["id"] = str(uuid.uuid4())
    elif mutation == "actor":
        raw["principal"]["actor_id"] = str(uuid.uuid4())

    async def remote(identity, path, payload):
        assert mutation != "missing_principal"
        assert path == "/v1/identity/offboarding/execute"
        return raw

    def forbid(*args, **kwargs):
        pytest.fail("Remote offboarding selected another SQL writer")

    monkeypatch.setattr(binding, "_remote_json", remote)
    monkeypatch.setattr(binding, "OffboardingPostgresData", forbid)
    monkeypatch.setattr(binding, "IdentityOffboardingCommandData", forbid)
    adapter = IdentityOffboardingCommands(
        object(),
        IdentityGateway(identity_url="http://identity.test", identity_token="control"),
        expected_principal=None if mutation == "missing_principal" else principal,
    )
    if mutation == "valid":
        assert (await adapter.execute(command)).operation == "commit_offboarding"
    else:
        with pytest.raises(IdentityGatewayUnavailable):
            await adapter.execute(command)


@pytest.mark.parametrize("mutation", ["target", "mode", "reason", "audit_id"])
async def test_invalid_postwrite_native_reply_rolls_back_caller_transaction(mutation):
    principal, request = _principal(), _command()
    command = {
        "schema": "aap-identity-offboarding-native/v1",
        "phase": "apply",
        "operation": "commit_offboarding",
        "operation_id": str(request.operation_id),
        "actor": principal.model_dump(mode="json"),
        "expected_key_hash": None,
        "request_fingerprint": "1" * 64,
        "derivation_id": "2" * 64,
        "audit_context": request.audit_context.model_dump(mode="json"),
        "payload": {
            "transaction_id": "1",
            "target_user_id": str(request.payload.target_user_id),
            "expected_target_generation": 2,
            "expected_target_is_active": True,
            "mode": "handoff",
            "normalized_reason": None,
            "reason_present": False,
            "owner_plan": [],
            "labeling_plan": [],
            "transfers": [],
            "unresolved": [],
            "scope_basis": {
                "target_role": "employee",
                "target_was_active": True,
                "project_ids": [],
            },
        },
    }
    raw = _reply(request, principal)
    raw.update(
        schema="aap-identity-offboarding-native/v1", phase="apply", replayed=False
    )
    result = raw["data"]["result"]
    if mutation == "target":
        result["user"]["id"] = str(uuid.uuid4())
    elif mutation == "mode":
        result["mode"] = "emergency_suspend"
    elif mutation == "reason":
        result["user"]["disabled_reason"] = "foreign reason"
    else:
        result["audit_id"] = None

    class DB:
        commits, rollbacks, pending = 0, 0, False

        async def flush(self):
            pass

        async def scalar(self, statement, parameters):
            self.pending = True
            return raw

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            self.rollbacks += 1
            self.pending = False

    db = DB()
    with pytest.raises(IdentityUnavailable):
        async with IdentityOffboardingCommandData(db).transaction() as transaction:
            await transaction.apply_offboarding_command(command)
    assert db.commits == 0 and db.rollbacks == 1 and not db.pending


@pytest.mark.parametrize(
    "failure,status,detail",
    [
        (OffboardingFailure("credential_invalid"), 401, "无效的认证凭据"),
        (OffboardingFailure("user_inactive"), 403, "当前账号已无管理权限"),
        (OffboardingFailure("credential_busy"), 409, "账号正在更新，请刷新后重试"),
        (
            OffboardingFailure("offboarding_busy", 409, {"code": "offboarding_busy"}),
            409,
            {"code": "offboarding_busy"},
        ),
        (
            OffboardingFailure("operation_conflict", 409, None),
            409,
            {"reason": "operation_conflict"},
        ),
    ],
)
def test_offboarding_error_status_detail_and_auth_challenge(failure, status, detail):
    with pytest.raises(HTTPException) as denied:
        _reject_offboarding_error(failure)
    assert denied.value.status_code == status and denied.value.detail == detail
    if status == 401:
        assert denied.value.headers == {"WWW-Authenticate": "Bearer"}
