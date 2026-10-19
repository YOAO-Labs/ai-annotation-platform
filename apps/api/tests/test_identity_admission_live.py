"""Owned-process Identity qualification with committed database worlds."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest
import redis.asyncio as redis
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker
import websockets

from aap_identity.contract import decode_principal_reply
from aap_identity.security import create_access_token, decode_access_token
from app.db.models.api_key import ApiKey
from app.db.models.user import User
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
    _verify_postgres,
)

ROOT = Path(__file__).resolve().parents[3]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity verifier",
    ),
]

INSTALLATION_PROBE = """
import hashlib, importlib.util, json, os, sys
from pathlib import Path
import aap_identity, aap_identity_runtime
assert all(importlib.util.find_spec(name) is None for name in
           ('app', 'sqlalchemy', 'redis', 'celery', 'boto3', 'torch'))
files = {}
for module in (aap_identity, aap_identity_runtime):
    directory = Path(module.__file__).resolve().parent
    assert directory.is_relative_to(Path(sys.prefix).resolve())
    assert 'site-packages' in directory.parts
    for path in directory.rglob('*'):
        if path.suffix in ('.py', '.json'):
            files[str(path.relative_to(directory.parent))] = hashlib.sha256(path.read_bytes()).hexdigest()
assert 'aap_identity/protocol.schema.json' in files
mountinfo = Path('/proc/self/mountinfo')
tmpfs = mountinfo.is_file() and any(
    row.split()[4] == '/tmp' and ' - tmpfs ' in row
    for row in mountinfo.read_text().splitlines())
print(json.dumps({'files': files, 'uid': os.getuid(), 'tmpfs': tmpfs}))
"""


def _verify_installation(output):
    report = json.loads(output)
    source = ROOT / "apps/identity/src"
    expected = {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source.rglob("*")
        if path.suffix in (".py", ".json")
    }
    assert report["files"] == expected
    return report


def _port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _dsn(kind, test_engine):
    raw = os.environ[f"AAP_MODULE_DATA_IDENTITY_{kind}_URL"]
    value = make_url(raw)
    assert (value.host, value.port, value.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert value.database.endswith("_test") and os.environ.get(
        "AAP_MODULE_DATA_PG_CONTAINER_ID"
    )
    _verify_postgres(value)
    return value.set(drivername="postgresql").render_as_string(hide_password=False)


@dataclass
class Process:
    process: subprocess.Popen
    log: object
    origin: str

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log.close()


@dataclass
class Container:
    container_id: str
    identity: str
    origin: str

    def stop(self):
        if self.container_id:
            _stop_remove_container(self.container_id, self.identity)
            self.container_id = ""


async def _start_container_identity(test_engine, directory, secret, control):
    image = _inspect_docker(os.environ["AAP_IDENTITY_RUNTIME_IMAGE"])
    identity = os.environ["AAP_IDENTITY_RUNTIME_IDENTITY"]
    assert image["Config"]["Labels"] == {
        "org.aap.acceptance": "identity-admission",
        "org.aap.acceptance-id": identity,
    }
    values = {
        "IDENTITY_DATABASE_URL": make_url(_dsn("AUTH", test_engine))
        .set(host="host.docker.internal")
        .render_as_string(hide_password=False),
        "IDENTITY_TOKEN": control,
        "IDENTITY_SIGNING_SECRET": secret,
    }
    envfile = directory / "identity-container.env"
    envfile.touch(mode=0o600)
    envfile.write_text("".join(f"{name}={value}\n" for name, value in values.items()))
    container = None
    try:
        container_id = _docker(
            "run",
            "--detach",
            "--name",
            f"aap-identity-{uuid.uuid4().hex[:12]}",
            "--label=org.aap.acceptance=module-data-boundary",
            f"--label=org.aap.acceptance-id={identity}",
            "--add-host=host.docker.internal:host-gateway",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--memory=256m",
            "--cpus=1",
            "--pids-limit=64",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
            "--publish=127.0.0.1::8170",
            "--env-file",
            str(envfile),
            image["Id"],
        )
        container = Container(container_id, identity, "")
        details = _inspect_docker(container_id)
        assert details["Image"] == image["Id"]
        assert details["HostConfig"]["ReadonlyRootfs"] is True
        assert details["HostConfig"]["Memory"] == 256 * 1024 * 1024
        assert details["HostConfig"]["NanoCpus"] == 1_000_000_000
        assert details["HostConfig"]["PidsLimit"] == 64
        assert details["Config"]["User"] == "10003:10003"
        assert details["Mounts"] == []
        assert details["HostConfig"]["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,size=16m"}
        assert not any(
            name.startswith(("REDIS_", "CELERY_", "MINIO_"))
            for name in details["Config"]["Env"]
        )
        ports = details["NetworkSettings"]["Ports"]["8170/tcp"]
        assert len(ports) == 1 and ports[0]["HostIp"] == "127.0.0.1"
        container.origin = f"http://127.0.0.1:{ports[0]['HostPort']}"
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                assert _inspect_docker(container_id)["State"]["Running"] is True
                try:
                    if (
                        await client.get(container.origin + "/ready")
                    ).status_code == 200:
                        diagnostic = _verify_installation(
                            _docker(
                                "exec",
                                container_id,
                                "python",
                                "-I",
                                "-c",
                                INSTALLATION_PROBE,
                            )
                        )
                        assert diagnostic["uid"] == 10003
                        assert diagnostic["tmpfs"] is True
                        return container
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
        pytest.fail("Installed Identity container did not become ready")
    except BaseException:
        if container:
            container.stop()
        raise
    finally:
        envfile.unlink(missing_ok=True)


def _start(command, environment, cwd, log_path, port, ready):
    log = log_path.open("wb")
    process = subprocess.Popen(
        command,
        env=environment,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    result = Process(process, log, f"http://127.0.0.1:{port}")
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            assert process.poll() is None, "Owned service exited before readiness"
            try:
                if (
                    httpx.get(
                        result.origin + ready, trust_env=False, timeout=2
                    ).status_code
                    == 200
                ):
                    return result
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        pytest.fail("Owned service did not become ready")
    except BaseException:
        result.stop()
        raise


def _start_identity(test_engine, directory, secret, control, *, port=None):
    interpreter = Path(os.environ["AAP_IDENTITY_RUNTIME_PYTHON"])
    assert interpreter.is_file()
    values = {
        "database_url": _dsn("AUTH", test_engine),
        "token": control,
        "signing_secret": secret,
    }
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "IDENTITY_HOST": "127.0.0.1",
        "IDENTITY_PORT": str(port or _port()),
    }
    for name, value in values.items():
        path = directory / f"identity-{name}"
        path.write_text(value)
        path.chmod(0o600)
        environment[f"IDENTITY_{name.upper()}_FILE"] = str(path)
    return _start(
        [str(interpreter), "-I", "-m", "aap_identity_runtime"],
        environment,
        directory,
        directory / f"identity-{environment['IDENTITY_PORT']}.log",
        int(environment["IDENTITY_PORT"]),
        "/ready",
    )


def _start_api(directory, secret, control, identity_origin):
    environment = dict(os.environ)
    environment.update(
        {
            "SECRET_KEY": secret,
            "IDENTITY_URL": identity_origin,
            "IDENTITY_TOKEN": control,
            "IDENTITY_AUTH_DATABASE_URL": "",
            "IDENTITY_AUTH_DATABASE_URL_FILE": "",
            "MIGRATION_DATABASE_URL": "",
            "ALEMBIC_AUTO_UPGRADE": "false",
            "E2E_SEED_ENABLED": "false",
            "AUDIT_ASYNC": "false",
            "SENTRY_DSN": "",
            "DEBUG": "false",
        }
    )
    for prefix in (
        "ASSETS_CATALOG",
        "ASSETS_SOURCE",
        "ASSETS_MEDIA",
        "ASSETS_MASK_CONTENT",
        "ASSETS_MASK_CLEANUP",
        "LABELING_MASK_GC",
    ):
        environment[prefix + "_URL"] = ""
        environment[prefix + "_TOKEN"] = ""
    port = _port()
    return _start(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            str(ROOT / "apps/api"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            "--timeout-graceful-shutdown",
            "3",
        ],
        environment,
        directory,
        directory / "api.log",
        port,
        "/health/db",
    )


async def _world(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor = uuid.uuid4()
    async with maker.begin() as db:
        db.add(
            User(
                id=actor,
                email=f"identity-live-{actor}@example.test",
                name="Live Identity user",
                password_hash="fixture-only",
                role="super_admin",
                is_active=True,
            )
        )
    return maker, actor


async def _cleanup(maker, actor, jtis):
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await db.execute(
            text(
                "DELETE FROM public.audit_logs WHERE actor_id=:id OR target_id=:target"
            ),
            {"id": actor, "target": str(actor)},
        )
        await db.execute(delete(ApiKey).where(ApiKey.user_id == actor))
        if jtis:
            await db.execute(
                text(
                    "DELETE FROM public.identity_revoked_jtis WHERE jti=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": list(jtis)},
            )
        await db.execute(delete(User).where(User.id == actor))


def _token(actor, secret, gen=0, expires=None):
    return create_access_token(
        str(actor),
        "super_admin",
        secret=secret,
        expire_minutes=15,
        gen=gen,
        expires_delta=expires,
    )


async def _verdict(client, identity, control, value):
    result = await client.post(
        identity.origin + "/v1/identity/authenticate",
        headers={"Authorization": f"Bearer {control}"},
        json={"token": value},
    )
    assert result.status_code == 200
    return result.json()


def _node_peer(origin, control, credential):
    peer = ROOT / "apps/api/tests/fixtures/identity_admission_peer.mjs"
    result = subprocess.run(
        ["node", str(peer)],
        input=json.dumps(
            {"url": origin, "control_token": control, "credential": credential}
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, "Standalone Node Identity peer failed"
    return json.loads(result.stdout)


def _expect_node(
    node, *, actor, credential_id, role, credential_type, generation, scopes
):
    principal = node["principal"]
    assert (
        node["status"] == 200
        and node["schema"] == "aap-identity/v1"
        and node["ok"] is True
    )
    assert principal["actor_id"] == str(actor)
    assert principal["credential_id"] == str(credential_id)
    assert principal["role"] == role
    assert principal["credential_type"] == credential_type
    assert principal["generation_decimal"] == (
        None if generation is None else str(generation)
    )
    assert principal["scopes"] == scopes


async def test_installed_identity_and_node_observe_current_state_after_restart(
    test_engine, tmp_path, record_property
):
    diagnostic = subprocess.run(
        [os.environ["AAP_IDENTITY_RUNTIME_PYTHON"], "-I", "-c", INSTALLATION_PROBE],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert diagnostic.returncode == 0, "Installed Identity dependency probe failed"
    installed = _verify_installation(diagnostic.stdout)
    record_property("installed_source_hashes", json.dumps(installed["files"]))
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = None
    jtis = []
    try:
        identity = _start_identity(test_engine, tmp_path, secret, control)
        value = _token(actor, secret)
        jti = uuid.UUID(decode_access_token(value, secret=secret)["jti"])
        jtis.append(jti)
        async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
            reply = await _verdict(client, identity, control, value)
            assert decode_principal_reply(reply).actor_id == actor
            node = await asyncio.to_thread(_node_peer, identity.origin, control, value)
            _expect_node(
                node,
                actor=actor,
                credential_id=jti,
                role="super_admin",
                credential_type="jwt",
                generation=0,
                scopes=None,
            )
            assert node["principal"]["expires_at"] == reply["data"]["expires_at"]

            async def sample():
                started = time.monotonic()
                reply = await _verdict(client, identity, control, value)
                assert reply["ok"] is True
                return time.monotonic() - started

            # A bounded local smoke; these timings do not qualify production capacity.
            durations = []
            for _ in range(4):
                durations.extend(await asyncio.gather(*(sample() for _ in range(8))))
            record_property("local_jwt_requests", len(durations))
            record_property("local_jwt_parallelism", 8)
            record_property("local_jwt_p95_ms", round(sorted(durations)[30] * 1000, 3))
            async with maker.begin() as db:
                await db.execute(
                    text("UPDATE public.users SET role='viewer' WHERE id=:id"),
                    {"id": actor},
                )
            assert (await _verdict(client, identity, control, value))["data"][
                "role"
            ] == "viewer"
            expired = _token(actor, secret, expires=timedelta(seconds=-1))
            assert (await _verdict(client, identity, control, expired))["error"][
                "code"
            ] == "token_expired"
            async with maker.begin() as db:
                await db.execute(
                    text("SELECT aap_identity_v1.revoke_jti(:jti,:expiry)"),
                    {"jti": jti, "expiry": datetime.now(UTC) + timedelta(days=8)},
                )
            assert (await _verdict(client, identity, control, value))["error"][
                "code"
            ] == "credential_revoked"
            origin = identity.origin
            identity.stop()
            identity = _start_identity(
                test_engine,
                tmp_path,
                secret,
                control,
                port=int(origin.rsplit(":", 1)[1]),
            )
            assert (await _verdict(client, identity, control, value))["error"][
                "code"
            ] == "credential_revoked"
            large = 2**63 - 2
            async with maker.begin() as db:
                await db.execute(
                    text(
                        "UPDATE public.users SET credential_generation=:gen WHERE id=:id"
                    ),
                    {"gen": large, "id": actor},
                )
            large_token = _token(actor, secret, gen=large)
            node = await asyncio.to_thread(
                _node_peer, identity.origin, control, large_token
            )
            _expect_node(
                node,
                actor=actor,
                credential_id=decode_access_token(large_token, secret=secret)["jti"],
                role="viewer",
                credential_type="jwt",
                generation=large,
                scopes=None,
            )
            async with maker.begin() as db:
                await db.execute(
                    text("UPDATE public.users SET is_active=false WHERE id=:id"),
                    {"id": actor},
                )
            assert (await _verdict(client, identity, control, large_token))["error"][
                "code"
            ] == "user_inactive"
            async with maker.begin() as db:
                await db.execute(
                    text(
                        "REVOKE EXECUTE ON FUNCTION aap_identity_v1.confirm_api_key_principal(uuid,text) FROM identity_auth_client"
                    )
                )
            assert (await client.get(identity.origin + "/ready")).status_code == 503
            async with maker.begin() as db:
                await db.execute(
                    text(
                        "GRANT EXECUTE ON FUNCTION aap_identity_v1.confirm_api_key_principal(uuid,text) TO identity_auth_client"
                    )
                )
            assert (await client.get(identity.origin + "/ready")).status_code == 200
    finally:
        if identity:
            identity.stop()
        # Always restore the exact AUTH surface if an assertion interrupted the probe.
        async with maker.begin() as db:
            await db.execute(
                text(
                    "GRANT EXECUTE ON FUNCTION aap_identity_v1.confirm_api_key_principal(uuid,text) TO identity_auth_client"
                )
            )
        await _cleanup(maker, actor, jtis)


async def test_real_api_rest_and_socket_use_remote_identity_without_fallback(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    identity = api = None
    jtis = []
    pub = None
    try:
        identity = await _start_container_identity(
            test_engine, tmp_path, secret, control
        )
        record_property(
            "installed_identity_image", os.environ["AAP_IDENTITY_RUNTIME_IMAGE"]
        )
        api = _start_api(tmp_path, secret, control, identity.origin)
        value = _token(actor, secret)
        jtis.append(uuid.UUID(decode_access_token(value, secret=secret)["jti"]))
        headers = {"Authorization": f"Bearer {value}"}
        async with httpx.AsyncClient(
            base_url=api.origin, trust_env=False, timeout=20
        ) as client:
            me = await client.get("/api/v1/auth/me", headers=headers)
            assert me.status_code == 200 and me.json()["id"] == str(actor)
            created = await client.post(
                "/api/v1/me/api-keys",
                headers=headers,
                json={"name": "Live key", "scopes": ["datasets:read"]},
            )
            assert created.status_code == 201
            key_id, key = created.json()["id"], created.json()["plaintext"]
            key_headers = {"Authorization": f"Bearer {key}"}
            assert (
                await client.get("/api/v1/auth/me", headers=key_headers)
            ).status_code == 200
            node = await asyncio.to_thread(_node_peer, identity.origin, control, key)
            _expect_node(
                node,
                actor=actor,
                credential_id=key_id,
                role="super_admin",
                credential_type="api_key",
                generation=None,
                scopes=["datasets:read"],
            )
            assert node["principal"]["expires_at"] is None
            assert (
                await client.patch(
                    f"/api/v1/me/api-keys/{key_id}",
                    headers=headers,
                    json={"scopes": []},
                )
            ).status_code == 200
            denied = await client.get("/api/v1/datasets", headers=key_headers)
            assert denied.status_code == 403 and "API key" in denied.text
            rotated = await client.post(
                f"/api/v1/me/api-keys/{key_id}/rotate", headers=headers
            )
            assert rotated.status_code == 200
            assert (
                await client.get("/api/v1/auth/me", headers=key_headers)
            ).status_code == 401
            key = rotated.json()["plaintext"]
            key_headers = {"Authorization": f"Bearer {key}"}
            assert (
                await client.get("/api/v1/auth/me", headers=key_headers)
            ).status_code == 200
            async with maker() as db:
                assert (
                    await db.get(ApiKey, uuid.UUID(key_id))
                ).last_used_at is not None

            socket_url = (
                api.origin.replace("http://", "ws://")
                + "/ws/notifications?token="
                + value
            )
            pub = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
            async with websockets.connect(socket_url) as ws:
                channel = f"notify:{actor}"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if (await pub.pubsub_numsub(channel))[0][1]:
                        break
                    await asyncio.sleep(0.05)
                message = json.dumps({"type": "notifications.sync", "marker": "before"})
                await pub.publish(channel, message)
                assert (
                    json.loads(await asyncio.wait_for(ws.recv(), 5))["marker"]
                    == "before"
                )
                assert (
                    await client.post("/api/v1/auth/logout", headers=headers)
                ).status_code == 204
                assert (
                    await client.get("/api/v1/auth/me", headers=headers)
                ).status_code == 401
                await pub.publish(
                    channel,
                    json.dumps({"type": "notifications.sync", "marker": "after"}),
                )
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(ws.recv(), 0.4)
            with pytest.raises(websockets.exceptions.InvalidStatus):
                async with websockets.connect(socket_url):
                    pytest.fail("revoked JWT completed a socket handshake")

            fresh = _token(actor, secret)
            all_out = await client.post(
                "/api/v1/auth/logout-all", headers={"Authorization": f"Bearer {fresh}"}
            )
            assert all_out.status_code == 200
            replacement = all_out.json()["access_token"]
            assert (
                await client.get(
                    "/api/v1/auth/me", headers={"Authorization": f"Bearer {fresh}"}
                )
            ).status_code == 401
            assert (
                await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {replacement}"},
                )
            ).status_code == 200
            assert (
                await client.get("/api/v1/auth/me", headers=key_headers)
            ).status_code == 200
            assert (
                await client.delete(
                    f"/api/v1/me/api-keys/{key_id}",
                    headers={"Authorization": f"Bearer {replacement}"},
                )
            ).status_code == 204
            assert (
                await client.get("/api/v1/auth/me", headers=key_headers)
            ).status_code == 401
            identity.stop()
            assert (
                await client.get(
                    "/api/v1/auth/me",
                    headers={"Authorization": f"Bearer {replacement}"},
                )
            ).status_code == 503
    finally:
        if api:
            api.stop()
        if identity:
            identity.stop()
        if pub:
            await pub.aclose()
        await _cleanup(maker, actor, jtis)
