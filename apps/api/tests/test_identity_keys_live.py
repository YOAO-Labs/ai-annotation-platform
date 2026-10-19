"""Independent key owner: retained replies, Node client, and a table-free edge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import uuid

import asyncpg
import httpx
import pytest
from sqlalchemy import text

from aap_identity.key_manager import derive_key_material
from aap_identity.security import decode_access_token
from tests.test_identity_admission_live import (
    ROOT,
    _cleanup,
    _dsn,
    _start_api,
    _start_identity,
    _token,
    _verdict,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_KEYS_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity key verifier",
    ),
]


def _command(token, operation, payload, operation_id=None):
    return {
        "schema": "aap-identity-keys/v1",
        "operation": operation,
        "operation_id": str(operation_id or uuid.uuid4()),
        "token": token,
        "payload": payload,
    }


async def _call(client, identity, control, command):
    response = await client.post(
        identity.origin + "/v1/identity/api-keys/execute",
        headers={"Authorization": f"Bearer {control}"},
        json=command,
    )
    assert response.status_code == 200
    return response.json()


def _node(identity, control, command):
    result = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_keys_peer.mjs")],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": command}
        ),
        text=True,
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=45,
    )
    assert result.returncode == 0, "Standalone Node key peer failed"
    return json.loads(result.stdout)


async def test_key_replay_survives_restart_without_reviving_revoked_material(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = None
    jtis = []
    try:
        generation = 2**63 - 2
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET role='viewer',credential_generation=:gen WHERE id=:actor"
                ),
                {"gen": generation, "actor": actor},
            )
        token = _token(actor, secret, gen=generation)
        jti = uuid.UUID(decode_access_token(token, secret=secret)["jti"])
        jtis.append(jti)
        identity = _start_identity(test_engine, tmp_path, secret, control)
        created = _command(
            token,
            "create_api_key",
            {"name": "Node-owned key", "scopes": ["datasets:read"]},
        )
        first = await asyncio.to_thread(_node, identity, control, created)
        key_id = uuid.UUID(created["operation_id"])
        original, _ = derive_key_material(secret, actor, key_id)
        assert first["principal"]["actor_id"] == str(actor)
        assert first["principal"]["role"] == "viewer"
        assert first["principal"]["generation_decimal"] == str(generation)
        assert first["issued"]["ok"] is True
        assert (
            first["issued"]["plaintext_sha256"]
            == hashlib.sha256(original.encode()).hexdigest()
        )

        rotated = _command(token, "rotate_api_key", {"key_id": str(key_id)})
        async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
            # Discard the committed response body, then recover by the saved operation.
            async with client.stream(
                "POST",
                identity.origin + "/v1/identity/api-keys/execute",
                headers={"Authorization": f"Bearer {control}"},
                json=rotated,
            ) as response:
                assert response.status_code == 200
            port = int(identity.origin.rsplit(":", 1)[1])
            identity.stop()
            identity = _start_identity(
                test_engine, tmp_path, secret, control, port=port
            )
            replay = await asyncio.to_thread(_node, identity, control, rotated)
            current, _ = derive_key_material(
                secret, actor, uuid.UUID(rotated["operation_id"])
            )
            assert replay["issued"]["ok"] is True
            assert (
                replay["issued"]["plaintext_sha256"]
                == hashlib.sha256(current.encode()).hexdigest()
            )
            assert (await _verdict(client, identity, control, original))["ok"] is False
            conflict = {**rotated, "payload": {"key_id": str(uuid.uuid4())}}
            assert (await _call(client, identity, control, conflict))["error"][
                "code"
            ] == "operation_conflict"

            label = "临时密钥" + "😀" * 56
            patch = _command(
                token,
                "update_api_key",
                {
                    "key_id": str(key_id),
                    "patch": {
                        "name": label,
                        "scopes": ["annotations:read"],
                        "expires_in_days": 30,
                    },
                },
            )
            changed = await asyncio.to_thread(_node, identity, control, patch)
            assert changed["data"]["key"]["name"] == label
            finite = await asyncio.to_thread(
                _node, identity, control, _command(current, "list_api_keys", {})
            )
            assert (
                finite["ok"] is True and finite["principal"]["expires_at"] is not None
            )
            assert finite["principal"]["actor_id"] == str(actor)
            assert finite["principal"]["credential_id"] == str(key_id)
            assert finite["principal"]["generation_decimal"] is None
            assert finite["principal"]["scopes"] == ["annotations:read"]
            expired_at = finite["data"]["items"][0]["expires_at"]
            unchanged = await _call(
                client,
                identity,
                control,
                _command(token, "update_api_key", {"key_id": str(key_id), "patch": {}}),
            )
            assert unchanged["data"]["key"]["expires_at"] == expired_at
            cleared = await _call(
                client,
                identity,
                control,
                _command(
                    token,
                    "update_api_key",
                    {"key_id": str(key_id), "patch": {"expires_in_days": None}},
                ),
            )
            assert cleared["data"]["key"]["expires_at"] is None
            revoked = await asyncio.to_thread(
                _node,
                identity,
                control,
                _command(token, "revoke_api_key", {"key_id": str(key_id)}),
            )
            assert revoked["data"]["revoked"] is True
            historical = await asyncio.to_thread(_node, identity, control, rotated)
            assert historical["issued"]["ok"] is False
            assert (
                historical["issued"]["plaintext_sha256"]
                == replay["issued"]["plaintext_sha256"]
            )
            async with maker() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM public.api_keys WHERE user_id=:actor"
                        ),
                        {"actor": actor},
                    )
                    == 1
                )
                rows = await db.execute(
                    text(
                        "SELECT payload,result FROM public.identity_key_operation_receipts WHERE actor_id=:actor"
                    ),
                    {"actor": actor},
                )
                receipts = json.dumps([dict(row._mapping) for row in rows], default=str)
                assert all(
                    value not in receipts for value in (token, original, current)
                ), "Receipt retained bearer material"
                assert (
                    await db.scalar(
                        text(
                            "SELECT revoked_at IS NOT NULL FROM public.api_keys WHERE id=:key"
                        ),
                        {"key": key_id},
                    )
                    is True
                )
            async with maker.begin() as db:
                await db.execute(
                    text(
                        "SELECT aap_identity_v1.revoke_jti(:jti,now()+INTERVAL '8 days')"
                    ),
                    {"jti": jti},
                )
            denied = await asyncio.to_thread(_node, identity, control, rotated)
            assert (
                denied["ok"] is False
                and denied["error"]["code"] == "credential_revoked"
            )
            async with maker.begin() as db:
                await db.execute(
                    text(
                        "REVOKE EXECUTE ON FUNCTION aap_identity_keys_v1.execute_key_command(jsonb) FROM identity_key_client"
                    )
                )
            assert (await client.get(identity.origin + "/ready")).status_code == 503
        record_property("node_key_operations", "list,create,patch,rotate,revoke,replay")
        record_property("restart_and_lost_reply", True)
    finally:
        if identity:
            identity.stop()
        async with maker.begin() as db:
            await db.execute(
                text(
                    "GRANT EXECUTE ON FUNCTION aap_identity_keys_v1.execute_key_command(jsonb) TO identity_key_client"
                )
            )
        await _cleanup(maker, actor, jtis)


async def test_public_key_routes_use_remote_owner_without_identity_table_privileges(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = api = None
    try:
        connection = await asyncpg.connect(_dsn("AUTH", test_engine))
        try:
            for sql in (
                "SELECT id FROM public.users",
                "SELECT id FROM public.api_keys",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.fetch(sql)
        finally:
            await connection.close()
        identity = _start_identity(test_engine, tmp_path, secret, control)
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity.origin,
            database_url=os.environ["AAP_MODULE_DATA_IDENTITY_AUTH_URL"],
        )
        headers = {"Authorization": "Bearer " + _token(actor, secret)}
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=20
        ) as client:
            created = await client.post(
                "/api/v1/me/api-keys", headers=headers, json={"name": "Remote owner"}
            )
            assert created.status_code == 201
            value = created.json()
            key_id, old = value["id"], value["plaintext"]
            patched = await client.patch(
                f"/api/v1/me/api-keys/{key_id}",
                headers=headers,
                json={"scopes": [], "expires_in_days": 1},
            )
            assert patched.status_code == 200
            rotated = await client.post(
                f"/api/v1/me/api-keys/{key_id}/rotate", headers=headers
            )
            assert rotated.status_code == 200
            current = rotated.json()["plaintext"]
            assert (
                await client.get(
                    "/api/v1/me/api-keys", headers={"Authorization": f"Bearer {old}"}
                )
            ).status_code == 401
            listed = await client.get(
                "/api/v1/me/api-keys", headers={"Authorization": f"Bearer {current}"}
            )
            assert listed.status_code == 200 and listed.json()[0]["id"] == key_id
            assert (
                await client.patch(
                    f"/api/v1/me/api-keys/{uuid.uuid4()}", headers=headers, json={}
                )
            ).status_code == 404
            assert (
                await client.delete(f"/api/v1/me/api-keys/{key_id}", headers=headers)
            ).status_code == 204
            assert (
                await client.get(
                    "/api/v1/me/api-keys",
                    headers={"Authorization": f"Bearer {current}"},
                )
            ).status_code == 401
            port = int(identity.origin.rsplit(":", 1)[1])
            identity.stop()
            assert (
                await client.get("/api/v1/me/api-keys", headers=headers)
            ).status_code == 503
            identity = _start_identity(
                test_engine, tmp_path, secret, control, port=port, enable_keys=False
            )
            # Admission stays healthy while absent key command capability fails closed.
            assert (
                await client.get("/api/v1/me/api-keys", headers=headers)
            ).status_code == 503
        record_property("api_database_role", "identity_auth_client")
        record_property("command_database_role", "identity_key_client")
        record_property(
            "audit_scope", "existing asynchronous enqueue; delivery not asserted"
        )
    finally:
        if api:
            api.stop()
        if identity:
            identity.stop()
        await _cleanup(maker, actor, [])
