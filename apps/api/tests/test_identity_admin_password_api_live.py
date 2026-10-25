"""AUTH-only public API acceptance for administrator password resets."""

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

from app.core.security import verify_password
from app.db.models.user import User
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
from tests.test_identity_admin_password_live import _cleanup_admin_password

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_ADMIN_PASSWORD_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity administrator-password verifier",
    ),
]


def _auth_dsn(test_engine):
    return (
        make_url(_dsn("AUTH", test_engine))
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )


def _node_public_check(body, status, target_id, target_email):
    peer = ROOT / "apps/api/tests/fixtures/identity_admin_password_peer.mjs"
    result = subprocess.run(
        ["node", str(peer)],
        input=json.dumps(
            {
                "mode": "public",
                "response": {
                    "status": status,
                    "content_type": "application/json",
                    "body": body,
                },
                "expected_target_user_id": str(target_id),
                "expected_target_email": target_email,
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
        "Public administrator-password response failed independent validation"
    )
    response = json.loads(result.stdout)
    assert response == {
        "ok": True,
        "target_user_id": str(target_id),
        "target_email": target_email,
        "password_length": 16,
        "has_required_classes": True,
    }
    return response


async def _password_state(maker, target_id):
    async with maker() as db:
        target = await db.get(User, target_id)
        await db.refresh(target)
        return target.password_hash, target.password_admin_reset_at, target.email


def _assert_no_secret_logs(paths, secrets_to_check):
    for path in paths:
        if path is None or not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        _assert_no_secret_text(content, secrets_to_check)


def _assert_no_secret_text(content, secrets_to_check):
    for value in secrets_to_check:
        if value and value in content:
            pytest.fail("A password or credential appeared in an owned service log")


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_auth_only_admin_password_api_binding_and_credential_preservation(
    test_engine, tmp_path, binding, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    user_ids = [actor]
    identity = api = None
    passwords = []
    async with maker.begin() as db:
        first_target = await create_user(
            db,
            "employee",
            f"admin-password-api-first-{uuid.uuid4()}@example.test",
            "First API target",
        )
        second_target = await create_user(
            db,
            "employee",
            f"admin-password-api-second-{uuid.uuid4()}@example.test",
            "Second API target",
        )
        key, api_key = await create_api_key(
            db, actor, "Admin password acceptance", ["*"]
        )
    user_ids.extend([first_target.id, second_target.id])
    configuration = {}
    if binding == "remote_container":
        identity = await _start_container_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_accounts=True,
            enable_groups=False,
            enable_presence=False,
            enable_admin_password=True,
        )
        identity_origin = identity.origin
    else:
        identity_origin = ""
        configuration = {
            "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
            "IDENTITY_ACCOUNT_DATABASE_URL": _dsn("ACCOUNTS", test_engine),
            "IDENTITY_ADMIN_PASSWORD_DATABASE_URL": _dsn("ADMIN_PASSWORD", test_engine),
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
        jwt = _token(actor, secret)
        async with httpx.AsyncClient(
            base_url=api.origin,
            trust_env=False,
            timeout=60,
        ) as client:
            jwt_headers = {"Authorization": f"Bearer {jwt}"}
            key_headers = {"Authorization": f"Bearer {api_key}"}
            jwt_me = await client.get("/api/v1/auth/me", headers=jwt_headers)
            assert jwt_me.status_code == 200, (
                f"JWT /auth/me failed with {jwt_me.status_code}: {jwt_me.text}"
            )
            key_me = await client.get("/api/v1/auth/me", headers=key_headers)
            assert key_me.status_code == 200, (
                f"API-key /auth/me failed with {key_me.status_code}: {key_me.text}"
            )

            if binding == "dedicated_dsn":
                before_hash, before_marker, _ = await _password_state(
                    maker, first_target.id
                )
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "GRANT SELECT ON public.users "
                            "TO identity_admin_password_client"
                        )
                    )
                try:
                    misprivileged = await client.post(
                        f"/api/v1/users/{first_target.id}/admin-reset-password",
                        headers=jwt_headers,
                    )
                    assert misprivileged.status_code == 503
                    assert secret not in misprivileged.text
                    assert control not in misprivileged.text
                    after_hash, after_marker, _ = await _password_state(
                        maker, first_target.id
                    )
                    assert (after_hash, after_marker) == (before_hash, before_marker)
                    async with maker() as db:
                        receipt_count = await db.scalar(
                            text(
                                "SELECT count(*) FROM "
                                "public.identity_admin_password_reset_receipts "
                                "WHERE target_user_id=:target"
                            ),
                            {"target": first_target.id},
                        )
                    assert receipt_count == 0
                finally:
                    async with maker.begin() as db:
                        await db.execute(
                            text(
                                "REVOKE SELECT ON public.users "
                                "FROM identity_admin_password_client"
                            )
                        )

            first = await client.post(
                f"/api/v1/users/{first_target.id}/admin-reset-password",
                headers=jwt_headers,
            )
            assert first.status_code == 200
            first_body = first.json()
            assert set(first_body) == {"temp_password", "message", "target_email"}
            assert first_body["target_email"] == first_target.email
            _node_public_check(
                first.text, first.status_code, first_target.id, first_target.email
            )
            passwords.append(first_body["temp_password"])
            first_hash, first_marker, _ = await _password_state(maker, first_target.id)
            assert verify_password(first_body["temp_password"], first_hash)
            assert first_marker is not None

            second = await client.post(
                f"/api/v1/users/{second_target.id}/admin-reset-password",
                headers=key_headers,
            )
            assert second.status_code == 200
            second_body = second.json()
            assert set(second_body) == {"temp_password", "message", "target_email"}
            assert second_body["target_email"] == second_target.email
            _node_public_check(
                second.text, second.status_code, second_target.id, second_target.email
            )
            passwords.append(second_body["temp_password"])
            second_hash, second_marker, _ = await _password_state(
                maker, second_target.id
            )
            assert verify_password(second_body["temp_password"], second_hash)
            assert second_marker is not None

            async with maker() as db:
                generation = await db.scalar(
                    text("SELECT credential_generation FROM public.users WHERE id=:id"),
                    {"id": actor},
                )
                stored_key = await db.get(type(key), key.id)
                await db.refresh(stored_key)
            assert generation == 0
            assert stored_key.revoked_at is None
            assert (
                await client.get("/api/v1/auth/me", headers=jwt_headers)
            ).status_code == 200
            assert (
                await client.get("/api/v1/auth/me", headers=key_headers)
            ).status_code == 200

        async with maker() as db:
            counts = await db.execute(
                text(
                    "SELECT target_user_id, count(*) AS n FROM "
                    "public.identity_admin_password_reset_receipts "
                    "WHERE actor_id=:actor AND target_user_id=ANY(CAST(:targets AS uuid[])) "
                    "GROUP BY target_user_id"
                ),
                {"actor": actor, "targets": [first_target.id, second_target.id]},
            )
            receipt_counts = {row.target_user_id: row.n for row in counts}
        assert receipt_counts == {first_target.id: 1, second_target.id: 1}
        record_property("admin_password_api_binding", binding)
        _assert_no_secret_logs(
            [
                tmp_path / "api.log",
                tmp_path / f"identity-{identity.origin.rsplit(':', 1)[1]}.log"
                if identity
                else None,
            ],
            [secret, control, jwt, api_key, *passwords],
        )
        if identity and hasattr(identity, "container_id"):
            _assert_no_secret_text(
                _docker("logs", identity.container_id),
                [secret, control, jwt, api_key, *passwords],
            )
    finally:
        if api:
            api.stop()
        if identity:
            identity.stop()
        await _cleanup_admin_password(maker, user_ids=user_ids)


@pytest.mark.parametrize("binding", ["remote_container", "dedicated_dsn"])
async def test_auth_only_admin_password_api_preserves_three_per_minute_limit(
    test_engine, tmp_path, binding
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    user_ids = [actor]
    identity = api = None
    passwords = []
    async with maker.begin() as db:
        target = await create_user(
            db,
            "employee",
            f"admin-password-rate-{uuid.uuid4()}@example.test",
            "Rate target",
        )
    user_ids.append(target.id)
    if binding == "remote_container":
        identity = await _start_container_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_admin_password=True,
        )
        identity_origin = identity.origin
        configuration = {}
    else:
        identity_origin = ""
        configuration = {
            "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
            "IDENTITY_ADMIN_PASSWORD_DATABASE_URL": _dsn("ADMIN_PASSWORD", test_engine),
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
        headers = {"Authorization": f"Bearer {_token(actor, secret)}"}
        async with httpx.AsyncClient(
            base_url=api.origin,
            trust_env=False,
            timeout=60,
            headers=headers,
        ) as client:
            for _ in range(3):
                response = await client.post(
                    f"/api/v1/users/{target.id}/admin-reset-password"
                )
                assert response.status_code == 200
                body = response.json()
                assert set(body) == {"temp_password", "message", "target_email"}
                assert body["target_email"] == target.email
                _node_public_check(
                    response.text, response.status_code, target.id, target.email
                )
                passwords.append(body["temp_password"])
            limited = await client.post(
                f"/api/v1/users/{target.id}/admin-reset-password"
            )
            assert limited.status_code == 429
        async with maker() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT count(*), count(DISTINCT operation_id) "
                        "FROM public.identity_admin_password_reset_receipts "
                        "WHERE actor_id=:actor AND target_user_id=:target"
                    ),
                    {"actor": actor, "target": target.id},
                )
            ).one()
        assert row == (3, 3)
        _assert_no_secret_logs([tmp_path / "api.log"], [secret, control, *passwords])
        if identity and hasattr(identity, "container_id"):
            _assert_no_secret_text(
                _docker("logs", identity.container_id),
                [secret, control, *passwords],
            )
    finally:
        if api:
            api.stop()
        if identity:
            identity.stop()
        await _cleanup_admin_password(maker, user_ids=user_ids)
