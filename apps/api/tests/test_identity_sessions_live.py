"""Core sessions through installed owners, scoped Redis, Node and a table-free API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
import secrets
import subprocess
import uuid

import httpx
import pytest
import redis.asyncio as redis
from redis.exceptions import ResponseError
from sqlalchemy import text
from sqlalchemy.engine import make_url

from aap_identity.security import decode_access_token
from tests.test_asset_storage_cleanup_runtime import _inspect_docker
from tests.test_identity_admission_live import (
    ROOT,
    _cleanup,
    _start_api,
    _start_container_identity,
    _start_identity,
    _verdict,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_SESSIONS_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity session verifier",
    ),
]


def _verify_redis():
    assert os.environ.get("AAP_WORKTREE_MODE") == "test"
    url = make_url(os.environ["REDIS_URL"])
    assert url.host in {"127.0.0.1", "localhost"}
    details = _inspect_docker(os.environ["AAP_MODULE_DATA_REDIS_CONTAINER_NAME"])
    assert (
        details["Config"]["Labels"]["org.aap.worktree-owner"]
        == os.environ["AAP_MODULE_DATA_REDIS_OWNER"]
    )
    ports = details["NetworkSettings"]["Ports"]["6379/tcp"]
    assert len(ports) == 1 and ports[0]["HostIp"] == "127.0.0.1"
    assert int(ports[0]["HostPort"]) == url.port
    return url


@asynccontextmanager
async def _login_redis():
    url = _verify_redis()
    name, password = (
        "identity-login-" + uuid.uuid4().hex[:12],
        secrets.token_urlsafe(32),
    )
    owner = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    scoped = None
    created = False
    try:
        await owner.execute_command(
            "ACL",
            "SETUSER",
            name,
            "reset",
            "on",
            ">" + password,
            "~login_failed:*",
            "-@all",
            "+get",
            "+incrby",
            "+expire",
            "+del",
            "+ping",
            "+select",
            "+client|setinfo",
            "+client|setname",
        )
        created = True
        dsn = url.set(username=name, password=password).render_as_string(
            hide_password=False
        )
        scoped = redis.from_url(dsn, decode_responses=True)
        assert await scoped.ping()
        probe = "login_failed:acl-proof-" + name
        assert await scoped.incr(probe) == 1
        assert await scoped.get(probe) == "1"
        assert await scoped.expire(probe, 60)
        assert await scoped.delete(probe) == 1
        with pytest.raises(ResponseError):
            await scoped.get("unrelated:sentinel")
        with pytest.raises(ResponseError):
            await scoped.publish("unrelated", "denied")
        yield dsn, owner
    finally:
        if scoped is not None:
            await scoped.aclose()
        try:
            if created:
                _verify_redis()
                assert await owner.acl_deluser(name) == 1
        finally:
            await owner.aclose()


def _command(operation, *, token=None, payload=None, operation_id=None):
    return {
        "schema": "aap-identity-sessions/v1",
        "operation": operation,
        "operation_id": str(operation_id or uuid.uuid4()),
        "token": token,
        "payload": payload or {},
        "audit_context": {
            "method": "POST",
            "path": "/acceptance/identity-session",
            "ip": "127.0.0.21",
            "request_id": str(uuid.uuid4()),
        },
        "user_agent": "Session acceptance",
    }


async def _call(client, identity, control, command):
    response = await client.post(
        identity.origin + "/v1/identity/sessions/execute",
        headers={"Authorization": f"Bearer {control}"},
        json=command,
    )
    assert response.status_code == 200
    return response.json()


def _node(identity, control, command):
    result = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_sessions_peer.mjs")],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": command}
        ),
        text=True,
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=45,
    )
    assert result.returncode == 0, "Standalone Node session peer failed"
    return json.loads(result.stdout)


async def test_session_replay_restart_and_current_authority_with_scoped_redis(
    test_engine, tmp_path, record_property
):
    password = "SessionFixture123"
    maker, actor = await _world(test_engine, password=password)
    email = f"identity-live-{actor}@example.test"
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = None
    jtis = []
    try:
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET credential_generation=:gen,role='employee' WHERE id=:id"
                ),
                {"gen": 2**63 - 2, "id": actor},
            )
        async with _login_redis() as (counter_url, redis_owner):
            identity = _start_identity(
                test_engine, tmp_path, secret, control, login_redis_url=counter_url
            )
            login = _command(
                "login",
                payload={
                    "email": email,
                    "password": password,
                    "captcha_verified": True,
                    "client_ip": "127.0.0.21",
                },
            )
            node = await asyncio.to_thread(_node, identity, control, login)
            assert node["ok"] and node["principal"]["generation_decimal"] == str(
                2**63 - 2
            )
            async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
                issued = await _call(client, identity, control, login)
                token = issued["data"]["access_token"]
                assert (
                    node["access_token_sha256"]
                    == hashlib.sha256(token.encode()).hexdigest()
                )
                assert (await _verdict(client, identity, control, token))["ok"]
                refresh = _command("refresh", token=token)
                async with client.stream(
                    "POST",
                    identity.origin + "/v1/identity/sessions/execute",
                    headers={"Authorization": f"Bearer {control}"},
                    json=refresh,
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
                    login_redis_url=counter_url,
                )
                replay = await asyncio.to_thread(_node, identity, control, refresh)
                refreshed = await _call(client, identity, control, refresh)
                assert (
                    replay["ok"]
                    and replay["access_token_sha256"]
                    == hashlib.sha256(
                        refreshed["data"]["access_token"].encode()
                    ).hexdigest()
                )
                assert (await _verdict(client, identity, control, token))["ok"]
                # An API key remains a valid actor for logout-all, but not logout.
                created = await client.post(
                    identity.origin + "/v1/identity/api-keys/execute",
                    headers={"Authorization": f"Bearer {control}"},
                    json={
                        "schema": "aap-identity-keys/v1",
                        "operation": "create_api_key",
                        "operation_id": str(uuid.uuid4()),
                        "token": token,
                        "payload": {"name": "Session recovery", "scopes": []},
                    },
                )
                assert created.status_code == 200 and created.json()["ok"]
                key = created.json()["data"]["plaintext"]
                denied = await _call(
                    client, identity, control, _command("logout", token=key)
                )
                assert (
                    not denied["ok"] and denied["error"]["code"] == "credential_invalid"
                )
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "REVOKE EXECUTE ON FUNCTION aap_identity_sessions_v1.read_login_candidate(text) "
                            "FROM identity_session_client"
                        )
                    )
                try:
                    assert (
                        await client.get(identity.origin + "/ready")
                    ).status_code == 503
                    unavailable = await client.post(
                        identity.origin + "/v1/identity/sessions/execute",
                        headers={"Authorization": f"Bearer {control}"},
                        json=login,
                    )
                    assert unavailable.status_code == 503
                    assert (await _verdict(client, identity, control, key))["ok"]
                finally:
                    async with maker.begin() as db:
                        await db.execute(
                            text(
                                "GRANT EXECUTE ON FUNCTION aap_identity_sessions_v1.read_login_candidate(text) "
                                "TO identity_session_client"
                            )
                        )
                all_out = _command("logout_all", token=key)
                all_node = await asyncio.to_thread(_node, identity, control, all_out)
                assert all_node["ok"] and all_node["claims"][
                    "generation_decimal"
                ] == str(2**63 - 1)
                replacement = (await _call(client, identity, control, all_out))["data"][
                    "access_token"
                ]
                assert not (await _verdict(client, identity, control, token))["ok"]
                assert (await _verdict(client, identity, control, key))["ok"]
                logout = _command("logout", token=replacement)
                jtis.append(
                    uuid.UUID(decode_access_token(replacement, secret=secret)["jti"])
                )
                assert (await asyncio.to_thread(_node, identity, control, logout))["ok"]
                assert not (await _verdict(client, identity, control, replacement))[
                    "ok"
                ]
                # Reauthentication may recover old claims, without restoring presence/gen/JTI.
                assert (await _call(client, identity, control, all_out))["data"][
                    "access_token"
                ] == replacement
                async with maker() as db:
                    row = (
                        await db.execute(
                            text(
                                "SELECT credential_generation,status FROM public.users WHERE id=:id"
                            ),
                            {"id": actor},
                        )
                    ).one()
                    assert row == (2**63 - 1, "offline")
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM public.audit_logs WHERE actor_id=:id AND action IN "
                                "('auth.login','auth.token_refresh','auth.logout','auth.logout_all')"
                            ),
                            {"id": actor},
                        )
                        == 4
                    )
                assert not (await _verdict(client, identity, control, replacement))[
                    "ok"
                ]
                changed_secret = secrets.token_urlsafe(32)
                identity.stop()
                identity = _start_identity(
                    test_engine,
                    tmp_path,
                    changed_secret,
                    control,
                    login_redis_url=counter_url,
                )
                conflict = await _call(client, identity, control, all_out)
                assert (
                    not conflict["ok"]
                    and conflict["error"]["code"] == "operation_conflict"
                )
                assert (await _verdict(client, identity, control, key))["ok"]
            await redis_owner.delete("login_failed:127.0.0.21")
        record_property(
            "session_boundaries",
            "Node four ops,exact int64,restart,replay,current authority,scoped Redis",
        )
    finally:
        if identity:
            identity.stop()
        await _cleanup(maker, actor, jtis)


async def test_table_free_api_forwards_all_session_commands_and_preserves_login_errors(
    test_engine, tmp_path, record_property
):
    password = "SessionEdgeFixture123"
    maker, actor = await _world(test_engine, password=password)
    email = f"identity-live-{actor}@example.test"
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = api = None
    jtis = []
    try:
        async with _login_redis() as (counter_url, redis_owner):
            await redis_owner.delete("login_failed:127.0.0.1")
            identity = await _start_container_identity(
                test_engine, tmp_path, secret, control, login_redis_url=counter_url
            )
            api = _start_api(
                tmp_path,
                secret,
                control,
                identity.origin,
                database_url=os.environ["AAP_MODULE_DATA_IDENTITY_AUTH_URL"],
                configuration={"TURNSTILE_ENABLED": "true", "TURNSTILE_SECRET_KEY": ""},
            )
            async with httpx.AsyncClient(
                base_url=api.origin, trust_env=False, timeout=20
            ) as client:
                wrong = await client.post(
                    "/api/v1/auth/login",
                    json={"email": email, "password": "wrong"},
                    headers={"User-Agent": "SessionEdge/1.0"},
                )
                assert (
                    wrong.status_code == 401
                    and wrong.headers["x-login-failed-count"] == "1"
                )
                assert wrong.headers["www-authenticate"] == "Bearer"
                async with maker() as db:
                    event = await db.scalar(
                        text(
                            "SELECT detail_json FROM public.audit_logs WHERE action='auth.login' "
                            "AND detail_json->>'email'=:email AND status_code=401"
                        ),
                        {"email": email},
                    )
                    assert (
                        event["ip_failed_count"] == 1
                        and event["user_agent"] == "SessionEdge/1.0"
                    )
                await redis_owner.set("login_failed:127.0.0.1", "5", ex=3600)
                challenge = await client.post(
                    "/api/v1/auth/login", json={"email": email, "password": password}
                )
                assert (
                    challenge.status_code == 400
                    and challenge.json()["detail"] == "captcha_required"
                )
                assert challenge.headers["x-login-failed-count"] == "5"
                await redis_owner.delete("login_failed:127.0.0.1")
                logged_in = await client.post(
                    "/api/v1/auth/login", json={"email": email, "password": password}
                )
                assert logged_in.status_code == 200
                token = logged_in.json()["access_token"]
                assert (await _verdict(client, identity, control, token))["ok"]
                key_response = await client.post(
                    "/api/v1/me/api-keys",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"name": "Table-free session edge", "scopes": []},
                )
                assert key_response.status_code == 201
                key = key_response.json()["plaintext"]
                key_logout = await client.post(
                    "/api/v1/auth/logout", headers={"Authorization": f"Bearer {key}"}
                )
                assert key_logout.status_code == 401
                fresh = await client.post(
                    "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {token}"}
                )
                assert (
                    fresh.status_code == 200 and fresh.json()["access_token"] != token
                )
                assert (await _verdict(client, identity, control, token))["ok"]
                all_out = await client.post(
                    "/api/v1/auth/logout-all",
                    headers={"Authorization": f"Bearer {key}"},
                )
                assert all_out.status_code == 200
                replacement = all_out.json()["access_token"]
                assert not (await _verdict(client, identity, control, token))["ok"]
                assert (await _verdict(client, identity, control, key))["ok"]
                jtis.append(
                    uuid.UUID(decode_access_token(replacement, secret=secret)["jti"])
                )
                logged_out = await client.post(
                    "/api/v1/auth/logout",
                    headers={"Authorization": f"Bearer {replacement}"},
                )
                assert logged_out.status_code == 204
                assert not (await _verdict(client, identity, control, replacement))[
                    "ok"
                ]
            api.stop()
            api = None
            identity.stop()
            identity = _start_identity(
                test_engine, tmp_path, secret, control, enable_sessions=False
            )
            api = _start_api(
                tmp_path,
                secret,
                control,
                identity.origin,
                database_url=os.environ["AAP_MODULE_DATA_IDENTITY_AUTH_URL"],
            )
            async with httpx.AsyncClient(
                base_url=api.origin, trust_env=False, timeout=20
            ) as client:
                unavailable = await client.post(
                    "/api/v1/auth/login", json={"email": email, "password": password}
                )
                assert unavailable.status_code == 503
            await redis_owner.delete("login_failed:127.0.0.1")
        record_property(
            "session_api_edge",
            "AUTH-only DB role,no User hydration,four commands,login audit,CAPTCHA,no fallback",
        )
    finally:
        if api:
            api.stop()
        if identity:
            identity.stop()
        async with maker.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                text(
                    "DELETE FROM public.audit_logs WHERE action='auth.login' AND detail_json->>'email'=:email"
                ),
                {"email": email},
            )
        await _cleanup(maker, actor, jtis)
