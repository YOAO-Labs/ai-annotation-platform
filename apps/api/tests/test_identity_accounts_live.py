"""Installed account owners, Node clients and a table-free real API."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import io
import json
import os
import secrets
import subprocess
import time
import uuid

import httpx
from PIL import Image
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from aap_identity.security import decode_access_token, verify_password
from tests.test_identity_admission_live import (
    ROOT,
    _cleanup,
    _dsn,
    _start_api,
    _start_container_identity,
    _start_identity,
    _token,
    _verdict,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_ACCOUNTS_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned account verifier",
    ),
]


def _command(operation, token, payload=None, *, operation_id=None):
    return {
        "schema": "aap-identity-accounts/v1",
        "operation": operation,
        "operation_id": str(operation_id or uuid.uuid4()),
        "token": token,
        "payload": payload or {},
        "audit_context": {
            "method": "POST",
            "path": "/acceptance/accounts",
            "ip": "127.0.0.1",
            "request_id": str(uuid.uuid4()),
        },
    }


async def _call(client, identity, control, command):
    response = await client.post(
        identity.origin + "/v1/identity/accounts/execute",
        headers={"Authorization": f"Bearer {control}"},
        json=command,
    )
    assert response.status_code == 200, "Account service returned a transport failure"
    return response.json()


def _node(identity, control, command):
    result = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_accounts_peer.mjs")],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": command}
        ),
        text=True,
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=45,
    )
    assert result.returncode == 0, "Independent Node account peer failed"
    return json.loads(result.stdout)


async def test_installed_account_process_node_replay_and_current_fences(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine, password="OldAccount123")
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = None
    token = _token(actor, secret, gen=2**63 - 1)
    jti = uuid.UUID(decode_access_token(token, secret=secret)["jti"])
    try:
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE users SET credential_generation=:generation,password_admin_reset_at=clock_timestamp() WHERE id=:actor"
                ),
                {"generation": 2**63 - 1, "actor": actor},
            )
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=False,
            enable_sessions=False,
        )
        read = await asyncio.to_thread(
            _node, identity, control, _command("read_self_account", token)
        )
        assert read["ok"] and read["principal"]["generation_decimal"] == str(2**63 - 1)
        assert read["data"]["account"]["id"] == str(actor)
        operations = [
            ("update_name", {"name": "  Independent account\u3000"}),
            ("get_preferences", {}),
            (
                "patch_preferences",
                {
                    "patch": {
                        "ui": {"theme": "dark"},
                        "ai": {
                            "params_by_backend": {"fixture": {"future": "x" * 80000}}
                        },
                    }
                },
            ),
            ("heartbeat", {}),
            (
                "change_password",
                {"old_password": "OldAccount123", "new_password": "NewAccount123"},
            ),
            (
                "set_avatar_ref",
                {
                    "avatar_ref": "preset:pixel-07",
                    "kind": "preset_or_clear",
                    "bytes": None,
                },
            ),
        ]
        for operation, payload in operations:
            receipt = await asyncio.to_thread(
                _node, identity, control, _command(operation, token, payload)
            )
            assert receipt["ok"] and receipt["operation"] == operation
        async with maker.begin() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT name,preferences,status,last_seen_at,password_hash,password_admin_reset_at,credential_generation FROM users WHERE id=:id"
                    ),
                    {"id": actor},
                )
            ).one()
            assert (
                row.name == "Independent account"
                and row.preferences["ui"]["theme"] == "dark"
            )
            assert row.status == "online" and row.last_seen_at is not None
            assert verify_password("NewAccount123", row.password_hash)
            assert (
                row.password_admin_reset_at is None
                and row.credential_generation == 2**63 - 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM identity_account_operation_receipts WHERE actor_id=:actor AND operation='heartbeat'"
                    ),
                    {"actor": actor},
                )
                == 0
            )
        noop = await asyncio.to_thread(
            _node,
            identity,
            control,
            _command(
                "set_avatar_ref",
                token,
                {
                    "avatar_ref": "preset:pixel-07",
                    "kind": "preset_or_clear",
                    "bytes": None,
                },
            ),
        )
        assert (
            noop["ok"]
            and noop["data"]["changed"] is False
            and noop["data"]["previous_avatar_ref"] is None
        )
        presence = await asyncio.to_thread(
            _node,
            identity,
            control,
            {
                "schema": "aap-identity-presence/v1",
                "cutoff": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            },
        )
        assert int(presence["affected_decimal"]) >= 1

        lost = _command(
            "patch_preferences", token, {"patch": {"ui": {"theme": "light"}}}
        )
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            async with client.stream(
                "POST",
                identity.origin + "/v1/identity/accounts/execute",
                headers={"Authorization": f"Bearer {control}"},
                json=lost,
            ) as response:
                assert response.status_code == 200
            port = int(identity.origin.rsplit(":", 1)[1])
            identity.stop()
            identity = _start_identity(
                test_engine,
                tmp_path,
                secret,
                control,
                port=port,
                enable_keys=False,
                enable_sessions=False,
            )
            async with maker.begin() as db:
                await db.execute(
                    text(
                        "UPDATE users SET preferences=jsonb_set(preferences,'{ui,theme}','\"system\"') WHERE id=:id"
                    ),
                    {"id": actor},
                )
            replay = await _call(client, identity, control, lost)
            assert (
                replay["ok"] and replay["data"]["preferences"]["ui"]["theme"] == "light"
            )
            async with maker.begin() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT preferences#>>'{ui,theme}' FROM users WHERE id=:id"
                        ),
                        {"id": actor},
                    )
                    == "system"
                )
                await db.execute(
                    text("SELECT aap_identity_v1.revoke_jti(:jti,:retain)"),
                    {"jti": jti, "retain": datetime.now(UTC) + timedelta(days=8)},
                )
            revoked = await _call(client, identity, control, lost)
            assert (
                revoked["ok"] is False
                and revoked["error"]["code"] == "credential_revoked"
            )
        record_property(
            "account_operations",
            "read,name,preferences-read,preferences-patch,heartbeat,password,avatar,presence,lost-reply-restart,current-revocation,int64",
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup(maker, actor, [jti])


async def test_table_free_real_api_forwards_accounts_and_avatar_storage(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine, password="OldAccount123")
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = api = None
    try:
        identity = await _start_container_identity(
            test_engine, tmp_path, secret, control
        )
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity.origin,
            database_url=make_url(_dsn("AUTH", test_engine))
            .set(drivername="postgresql+asyncpg")
            .render_as_string(hide_password=False),
        )
        headers = {"Authorization": f"Bearer {_token(actor, secret)}"}
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=30
        ) as client:
            me = await client.get("/api/v1/auth/me", headers=headers)
            assert me.status_code == 200 and me.json()["id"] == str(actor)
            assert "password_hash" not in me.json()
            name = await client.patch(
                "/api/v1/auth/me", headers=headers, json={"name": "Container account"}
            )
            assert (
                name.status_code == 200 and name.json()["name"] == "Container account"
            )
            prefs = await client.patch(
                "/api/v1/auth/me/preferences",
                headers=headers,
                json={"ui": {"theme": "dark"}},
            )
            assert prefs.status_code == 200 and prefs.json()["ui"]["theme"] == "dark"
            assert (
                await client.get("/api/v1/auth/me/preferences", headers=headers)
            ).json()["ui"]["theme"] == "dark"
            assert (
                await client.post("/api/v1/auth/me/heartbeat", headers=headers)
            ).status_code == 204
            password = await client.post(
                "/api/v1/auth/me/password",
                headers=headers,
                json={"old_password": "OldAccount123", "new_password": "NewAccount123"},
            )
            assert password.status_code == 204
            preset = await client.patch(
                "/api/v1/auth/me/avatar",
                headers=headers,
                json={"avatar_ref": "preset:pixel-07"},
            )
            assert preset.status_code == 200
            png = io.BytesIO()
            Image.new("RGB", (256, 256), "blue").save(png, format="PNG")
            uploaded = await client.post(
                "/api/v1/auth/me/avatar",
                headers=headers,
                files={"file": ("avatar.png", png.getvalue(), "image/png")},
            )
            assert uploaded.status_code == 200 and uploaded.json()[
                "avatar_ref"
            ].startswith("upload:")
            cleared = await client.delete("/api/v1/auth/me/avatar", headers=headers)
            assert cleared.status_code == 200 and cleared.json()["avatar_ref"] is None
        async with maker.begin() as db:
            assert not await db.scalar(
                text(
                    "SELECT has_table_privilege('identity_auth_client','public.users','SELECT')"
                )
            )
            assert (
                await db.scalar(
                    text("SELECT name FROM users WHERE id=:id"), {"id": actor}
                )
                == "Container account"
            )
        record_property(
            "api_database_boundary",
            "AUTH-only LOGIN; profile,name,preferences,heartbeat,password,avatar-upload-preset-clear",
        )
    finally:
        if api is not None:
            api.stop()
        if identity is not None:
            identity.stop()
        await _cleanup(maker, actor, [])


async def test_healthy_owner_without_account_capability_returns_503_without_fallback(
    test_engine, tmp_path
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = api = None
    try:
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_accounts=False,
            enable_presence=False,
            enable_keys=False,
            enable_sessions=False,
        )
        api = _start_api(
            tmp_path,
            secret,
            control,
            identity.origin,
            database_url=make_url(_dsn("AUTH", test_engine))
            .set(drivername="postgresql+asyncpg")
            .render_as_string(hide_password=False),
        )
        token = _token(actor, secret)
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            assert (await client.get(identity.origin + "/ready")).status_code == 200
            assert (await _verdict(client, identity, control, token))["ok"]
            response = await client.get(
                api.origin + "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 503
        async with maker.begin() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM identity_account_operation_receipts WHERE actor_id=:id"
                    ),
                    {"id": actor},
                )
                == 0
            )
    finally:
        if api is not None:
            api.stop()
        if identity is not None:
            identity.stop()
        await _cleanup(maker, actor, [])


async def test_dedicated_account_and_presence_dsns_keep_api_table_free(
    test_engine,
    tmp_path,
    monkeypatch,
):
    from app.config import settings
    from app.services.identity_account_commands import IdentityPresenceCommands
    from app.services.identity_gateway import IdentityGateway

    maker, actor = await _world(test_engine)
    secret = secrets.token_urlsafe(32)
    api = None
    try:
        api = _start_api(
            tmp_path,
            secret,
            "unused-control",
            "",
            database_url=make_url(_dsn("AUTH", test_engine))
            .set(drivername="postgresql+asyncpg")
            .render_as_string(hide_password=False),
            configuration={
                "IDENTITY_AUTH_DATABASE_URL": _dsn("AUTH", test_engine),
                "IDENTITY_ACCOUNT_DATABASE_URL": _dsn("ACCOUNTS", test_engine),
                "IDENTITY_PRESENCE_DATABASE_URL": _dsn("PRESENCE", test_engine),
            },
        )
        headers = {"Authorization": f"Bearer {_token(actor, secret)}"}
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=30
        ) as client:
            assert (
                await client.get("/api/v1/auth/me", headers=headers)
            ).status_code == 200
            response = await client.patch(
                "/api/v1/auth/me/preferences",
                headers=headers,
                json={"ui": {"theme": "dark"}},
            )
            assert (
                response.status_code == 200 and response.json()["ui"]["theme"] == "dark"
            )
            assert (
                await client.post("/api/v1/auth/me/heartbeat", headers=headers)
            ).status_code == 204
        monkeypatch.setattr(
            settings, "identity_presence_database_url", _dsn("PRESENCE", test_engine)
        )
        monkeypatch.setattr(settings, "identity_presence_database_url_file", "")
        affected = await IdentityPresenceCommands(
            object(), IdentityGateway()
        ).mark_inactive_accounts_offline(datetime.now(UTC) + timedelta(seconds=1))
        assert type(affected) is int and affected >= 1
        async with maker.begin() as db:
            assert (
                await db.scalar(
                    text("SELECT status FROM users WHERE id=:id"), {"id": actor}
                )
                == "offline"
            )
    finally:
        if api is not None:
            api.stop()
        await _cleanup(maker, actor, [])


async def test_account_container_concurrency_and_large_preference_round_trip(
    test_engine,
    tmp_path,
    record_property,
):
    from tests.test_asset_storage_cleanup_runtime import _docker, _inspect_docker

    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = None
    try:
        identity = await _start_container_identity(
            test_engine, tmp_path, secret, control
        )
        token = _token(actor, secret)
        slots = asyncio.Semaphore(8)
        timings = []
        async with httpx.AsyncClient(trust_env=False, timeout=45) as client:

            async def read():
                async with slots:
                    started = time.monotonic()
                    result = await _call(
                        client, identity, control, _command("read_self_account", token)
                    )
                    timings.append(time.monotonic() - started)
                    assert result["ok"]

            started = time.monotonic()
            await asyncio.gather(*(read() for _ in range(32)))
            duration = time.monotonic() - started
            # This metadata is valid in the original arbitrary AI-parameter map;
            # neither the session request cap nor admission response cap applies.
            value = "x" * (16 * 1024 * 1024 + 1)
            large = _command(
                "patch_preferences",
                token,
                {
                    "patch": {
                        "ai": {"params_by_backend": {"fixture": {"future": value}}}
                    }
                },
            )
            started = time.monotonic()
            result = await _call(client, identity, control, large)
            assert (
                result["ok"]
                and result["data"]["preferences"]["ai"]["params_by_backend"]["fixture"][
                    "future"
                ]
                == value
            )
            assert (
                await _call(
                    client, identity, control, _command("get_preferences", token)
                )
            )["data"]["preferences"]["ai"]["params_by_backend"]["fixture"][
                "future"
            ] == value
            large_duration = time.monotonic() - started
        details = _inspect_docker(identity.container_id)
        assert not details["State"]["OOMKilled"]
        stats = _docker(
            "stats", "--no-stream", "--format", "{{.MemUsage}}", identity.container_id
        ).strip()
        record_property(
            "account_local_load",
            json.dumps(
                {
                    "reads": 32,
                    "concurrency": 8,
                    "read_seconds": round(duration, 3),
                    "read_p95_seconds": round(sorted(timings)[30], 3),
                    "metadata_bytes": len(value),
                    "write_read_seconds": round(large_duration, 3),
                    "container_memory": stats,
                    "memory_limit_bytes": details["HostConfig"]["Memory"],
                }
            ),
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup(maker, actor, [])
