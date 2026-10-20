"""AUTH-only API acceptance for the Identity platform-role owner."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from tests.factory import create_api_key, create_user
from tests.test_identity_admission_live import (
    ROOT,
    _docker,
    _dsn,
    _start_api,
    _start_container_identity,
    _token,
    _world,
)
from tests.test_identity_platform_role_live import _cleanup_platform_roles

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_PLATFORM_ROLE_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity platform-role verifier",
    ),
]


def _auth_dsn(test_engine):
    return (
        make_url(_dsn("AUTH", test_engine))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )


def _node_public_check(mode, response, target_id, role, *, actor_id=None):
    peer = ROOT / "apps/api/tests/fixtures/identity_platform_role_peer.mjs"
    request = {
        "mode": mode,
        "status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "body": response.text,
        "target_user_id": str(target_id),
        "role": role,
    }
    if actor_id is not None:
        request["actor_id"] = str(actor_id)
    result = subprocess.run(
        ["node", str(peer), "--validate-public"],
        input=json.dumps(request, ensure_ascii=False, allow_nan=False),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "Public platform-role response failed independent validation"
    )
    return json.loads(result.stdout)


async def _role_receipts(engine, actor_id, target_id):
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT operation_id, operation, request_fingerprint, result, audit_id "
                "FROM public.identity_platform_role_operation_receipts "
                "WHERE actor_id=:actor AND target_user_id=:target "
                "ORDER BY created_at, operation_id"
            ),
            {"actor": actor_id, "target": target_id},
        )
        return [dict(row._mapping) for row in rows]


async def _role_snapshot(maker, user_id):
    async with maker() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT role, credential_generation, is_active "
                        "FROM public.users WHERE id=:id"
                    ),
                    {"id": user_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def _role_audit_count(maker, actor_id, target_id):
    async with maker() as db:
        return await db.scalar(
            text(
                "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor "
                "AND target_id=:target AND action='user.role_change'"
            ),
            {"actor": actor_id, "target": str(target_id)},
        )


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_auth_only_platform_role_api_bindings_and_public_contract(
    test_engine, tmp_path, binding, record_property
):
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    user_ids = [actor_id]
    identity = api = None
    api_key_value = None
    jwt = None
    async with maker.begin() as db:
        target = await create_user(
            db,
            "employee",
            f"platform-role-api-target-{uuid.uuid4()}@example.test",
            "Role API target",
        )
        same_role_target = await create_user(
            db,
            "employee",
            f"platform-role-api-noop-{uuid.uuid4()}@example.test",
            "No-op API target",
        )
        api_key, api_key_value = await create_api_key(
            db, actor_id, "Platform role API acceptance", ["*"]
        )
    user_ids.extend([target.id, same_role_target.id])
    configuration = {}
    if binding == "remote_container":
        identity = await _start_container_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_accounts=True,
            enable_groups=False,
            enable_invitations=False,
            enable_directory=False,
            enable_admin_password=False,
            enable_platform_role=True,
            enable_presence=False,
        )
        identity_origin = identity.origin
    else:
        identity_origin = ""
        configuration = {
            "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
            "IDENTITY_ACCOUNT_DATABASE_URL": _dsn("ACCOUNTS", test_engine),
            "IDENTITY_PLATFORM_ROLE_DATABASE_URL": _dsn("PLATFORM_ROLE", test_engine),
        }
    try:
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity_origin,
            database_url=_auth_dsn(test_engine),
            configuration=configuration,
        )
        jwt = _token(actor_id, secret)
        jwt_headers = {"Authorization": f"Bearer {jwt}"}
        key_headers = {"Authorization": f"Bearer {api_key_value}"}
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=120
        ) as client:
            jwt_me = await client.get("/api/v1/auth/me", headers=jwt_headers)
            assert jwt_me.status_code == 200, (
                f"JWT /auth/me failed with {jwt_me.status_code}: {jwt_me.text}"
            )
            key_me = await client.get("/api/v1/auth/me", headers=key_headers)
            assert key_me.status_code == 200, (
                f"API-key /auth/me failed with {key_me.status_code}: {key_me.text}"
            )

            if binding == "dedicated_dsn":
                before_user = await _role_snapshot(maker, target.id)
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "GRANT SELECT ON public.users "
                            "TO identity_platform_role_client"
                        )
                    )
                try:
                    misprivileged = await client.patch(
                        f"/api/v1/users/{target.id}/role",
                        headers=jwt_headers,
                        json={"role": "viewer"},
                    )
                    assert misprivileged.status_code == 503, misprivileged.text
                    assert secret not in misprivileged.text
                    assert control not in misprivileged.text
                    assert await _role_snapshot(maker, target.id) == before_user
                    assert await _role_receipts(test_engine, actor_id, target.id) == []
                finally:
                    async with maker.begin() as db:
                        await db.execute(
                            text(
                                "REVOKE SELECT ON public.users "
                                "FROM identity_platform_role_client"
                            )
                        )

            preview = await client.get(
                f"/api/v1/users/{target.id}/role/preview",
                headers=jwt_headers,
                params={"role": "viewer"},
            )
            assert preview.status_code == 200, preview.text
            preview_data = _node_public_check(
                "public_preview", preview, target.id, "viewer", actor_id=actor_id
            )
            assert preview_data["current_role"] == "employee"
            assert preview_data["requested_role"] == "viewer"
            assert preview_data["can_change"] is True
            assert preview_data["projects"] == []
            assert preview_data["other_project_count"] == 0

            same_role_preview = await client.get(
                f"/api/v1/users/{target.id}/role/preview",
                headers=jwt_headers,
                params={"role": "employee"},
            )
            assert same_role_preview.status_code == 200, same_role_preview.text
            same_role_data = _node_public_check(
                "public_preview",
                same_role_preview,
                target.id,
                "employee",
                actor_id=actor_id,
            )
            assert same_role_data["can_change"] is False
            assert same_role_data["blockers"] == ["目标角色与当前角色相同"]

            missing_role = await client.get(
                f"/api/v1/users/{target.id}/role/preview", headers=jwt_headers
            )
            assert missing_role.status_code == 422
            invalid_role = await client.get(
                f"/api/v1/users/{uuid.uuid4()}/role/preview",
                headers=jwt_headers,
                params={"role": "future_role"},
            )
            assert invalid_role.status_code == 400
            assert invalid_role.json() == {"detail": "非法平台角色: future_role"}
            invalid_path = await client.patch(
                "/api/v1/users/not-a-uuid/role",
                headers=jwt_headers,
                json={"role": "employee"},
            )
            assert invalid_path.status_code == 404

            changed = await client.patch(
                f"/api/v1/users/{target.id}/role",
                headers=jwt_headers,
                json={"role": "viewer", "ignored_legacy_field": "accepted"},
            )
            assert changed.status_code == 200, changed.text
            changed_user = _node_public_check(
                "public_user", changed, target.id, "viewer"
            )
            assert changed_user["role"] == "viewer"

            first_noop = await client.patch(
                f"/api/v1/users/{same_role_target.id}/role",
                headers=key_headers,
                json={"role": "employee"},
            )
            second_noop = await client.patch(
                f"/api/v1/users/{same_role_target.id}/role",
                headers=key_headers,
                json={"role": "employee"},
            )
            assert first_noop.status_code == 200, first_noop.text
            assert second_noop.status_code == 200, second_noop.text
            _node_public_check(
                "public_user", first_noop, same_role_target.id, "employee"
            )
            _node_public_check(
                "public_user", second_noop, same_role_target.id, "employee"
            )
            no_op_receipts = await _role_receipts(
                test_engine, actor_id, same_role_target.id
            )
            assert len(no_op_receipts) == 2
            assert len({item["operation_id"] for item in no_op_receipts}) == 2
            assert all(item["audit_id"] is None for item in no_op_receipts)
            assert await _role_audit_count(maker, actor_id, same_role_target.id) == 0
            assert await _role_snapshot(maker, same_role_target.id) == {
                "role": "employee",
                "credential_generation": 0,
                "is_active": True,
            }

            changed_receipts = await _role_receipts(test_engine, actor_id, target.id)
            assert len(changed_receipts) == 1
            assert changed_receipts[0]["audit_id"] is not None
            assert await _role_audit_count(maker, actor_id, target.id) == 1
            async with maker() as db:
                key = await db.get(type(api_key), api_key.id)
                await db.refresh(key)
                actor_generation = await db.scalar(
                    text("SELECT credential_generation FROM public.users WHERE id=:id"),
                    {"id": actor_id},
                )
            assert actor_generation == 0
            assert key.revoked_at is None
            assert (
                await client.get("/api/v1/auth/me", headers=jwt_headers)
            ).status_code == 200
            assert (
                await client.get("/api/v1/auth/me", headers=key_headers)
            ).status_code == 200

        record_property("platform_role_api_binding", binding)
    finally:
        secrets_to_check = [secret, control, jwt, api_key_value]
        if identity and hasattr(identity, "container_id") and identity.container_id:
            identity_logs = _docker("logs", identity.container_id)
            assert all(
                value not in identity_logs for value in secrets_to_check if value
            )
        if api:
            api.stop()
        if (tmp_path / "api.log").exists():
            api_log = (tmp_path / "api.log").read_text(
                encoding="utf-8", errors="replace"
            )
            assert all(value not in api_log for value in secrets_to_check if value)
        if identity:
            identity.stop()
        await _cleanup_platform_roles(maker, user_ids=user_ids)
