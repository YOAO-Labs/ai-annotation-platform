"""AUTH-only API qualification for remote and dedicated deletion ownership."""

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
    _dsn,
    _start_api,
    _start_container_identity,
    _token,
    _world,
)
from tests.test_identity_deletions_live import _cleanup_deletions

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_DELETION_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled AUTH-only deletion API verifier",
    ),
]


def _auth_dsn(test_engine):
    return (
        make_url(_dsn("AUTH", test_engine))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )


def _node_public(response, target_id):
    peer = ROOT / "apps/api/tests/fixtures/identity_deletions_peer.mjs"
    result = subprocess.run(
        ["node", str(peer), "--validate-public"],
        input=json.dumps(
            {
                "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "body": response.text,
                "target_user_id": str(target_id),
            },
            ensure_ascii=False,
            allow_nan=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "Public deletion response failed independent validation"
    )
    return json.loads(result.stdout)


async def _deletion_receipts(engine, actor_id, target_id):
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT operation_id, operation, request_fingerprint, derivation_id, "
                "result, audit_id, target_generation, target_is_active "
                "FROM public.identity_deletion_operation_receipts "
                "WHERE actor_id=:actor AND target_user_id=:target "
                "ORDER BY created_at, operation_id"
            ),
            {"actor": actor_id, "target": target_id},
        )
        return [dict(row._mapping) for row in rows]


async def _account_state(maker, user_id):
    async with maker() as db:
        row = await db.execute(
            text(
                "SELECT is_active, disabled_kind, disabled_reason, "
                "credential_generation FROM public.users WHERE id=:id"
            ),
            {"id": user_id},
        )
        return dict(row.mappings().one())


@pytest.mark.parametrize(
    ("binding", "credential_kind"),
    [("remote_container", "jwt"), ("dedicated_dsn", "api_key")],
)
async def test_auth_only_deletion_api_bindings_and_public_contract(
    test_engine, tmp_path, binding, credential_kind
):
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    user_ids = [actor_id]
    operation_ids = []
    identity = api = None
    api_key_text = None
    try:
        async with maker.begin() as db:
            target = await create_user(
                db,
                "employee",
                f"deletion-api-target-{suffix}@example.test",
                "API delete target",
            )
            blocked_target = await create_user(
                db,
                "employee",
                f"deletion-api-blocked-{suffix}@example.test",
                "Blocked configuration target",
            )
            if credential_kind == "api_key":
                api_key, api_key_text = await create_api_key(
                    db, actor_id, "Deletion API manager key", ["*"]
                )
        user_ids.extend([target.id, blocked_target.id])
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
                enable_platform_role=False,
                enable_suspension=False,
                enable_deactivation=False,
                enable_deletions=True,
                enable_presence=False,
            )
            identity_origin = identity.origin
        else:
            identity_origin = ""
            configuration = {
                "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
                "IDENTITY_ACCOUNT_DATABASE_URL": _dsn("ACCOUNTS", test_engine),
                "IDENTITY_DELETION_DATABASE_URL": _dsn("DELETION", test_engine),
            }
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity_origin,
            database_url=_auth_dsn(test_engine),
            configuration=configuration,
        )
        jwt = _token(actor_id, secret)
        access_token = api_key_text if credential_kind == "api_key" else jwt
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=120
        ) as client:
            me = await client.get("/api/v1/auth/me", headers=headers)
            assert me.status_code == 200, (
                f"AUTH-only /auth/me failed: {me.status_code} {me.text}"
            )

            malformed = await client.delete(
                "/api/v1/users/not-a-uuid",
                headers=headers,
            )
            assert malformed.status_code == 404
            assert (await _account_state(maker, target.id))["is_active"] is True
            assert await _deletion_receipts(test_engine, actor_id, target.id) == []

            if binding == "dedicated_dsn":
                before = await _account_state(maker, blocked_target.id)
                async with maker.begin() as db:
                    await db.execute(
                        text("GRANT SELECT ON public.users TO identity_deletion_client")
                    )
                try:
                    misprivileged = await client.request(
                        "DELETE",
                        f"/api/v1/users/{blocked_target.id}",
                        headers=headers,
                        json={"transfer_to_user_id": None},
                    )
                    assert misprivileged.status_code == 503, misprivileged.text
                    assert secret not in misprivileged.text
                    assert control not in misprivileged.text
                    assert await _account_state(maker, blocked_target.id) == before
                    assert (
                        await _deletion_receipts(
                            test_engine, actor_id, blocked_target.id
                        )
                        == []
                    )
                finally:
                    async with maker.begin() as db:
                        await db.execute(
                            text(
                                "REVOKE SELECT ON public.users FROM identity_deletion_client"
                            )
                        )

            if binding == "remote_container":
                response = await client.request(
                    "DELETE",
                    f"/api/v1/users/{target.id}",
                    headers=headers,
                    json={
                        "transfer_to_user_id": None,
                        "ignored_legacy_extra": "ignored by public request model",
                    },
                )
            else:
                response = await client.request(
                    "DELETE",
                    f"/api/v1/users/{target.id}",
                    content=b"null",
                    headers={
                        **headers,
                        "content-type": "application/json",
                    },
                )
            assert response.status_code == 200, response.text
            public_user = _node_public(response, target.id)
            assert public_user["disabled_kind"] == "deleted"
            assert public_user["disabled_reason"] == "管理员删除账号"
            state = await _account_state(maker, target.id)
            assert state["is_active"] is False
            assert state["disabled_kind"] == "deleted"
            assert state["credential_generation"] == 0
            receipts = await _deletion_receipts(test_engine, actor_id, target.id)
            assert len(receipts) == 1
            assert receipts[0]["operation"] == "delete_account"
            assert receipts[0]["target_is_active"] is False
            assert receipts[0]["audit_id"] is not None
            operation_ids.append(receipts[0]["operation_id"])
    finally:
        if api:
            api.stop()
        if identity:
            identity.stop()
        await _cleanup_deletions(
            maker,
            user_ids=user_ids,
            operation_ids=operation_ids,
        )
