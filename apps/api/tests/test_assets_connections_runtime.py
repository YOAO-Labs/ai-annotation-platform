"""Real Source SQL/HTTP/S3/SFTP qualification for connection administration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.test_asset_storage_cleanup_runtime import (
    _container_database_url,
    _docker,
    _inspect_docker,
    _remove_worker_image,
    _required_env,
    _stop_remove_container,
    _verified_resource_manifest,
    _verify_postgres,
    _wait_healthy,
)
from tests.test_assets_catalog_runtime import (
    _lost_response_proxy,
    _wait_public_ready,
    _write_environment,
)
from tests.test_dataset_import_runtime import _host_address, _sftp_server
from tests.test_dataset_upload_native import _seed_world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable module-data verifier",
    ),
]
ROOT = Path(__file__).resolve().parents[3]
POLICY = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


@pytest.fixture(scope="module")
async def connections_image():
    identity = secrets.token_hex(12)
    image = f"aap-assets-connections-acceptance:{identity}"
    build = await asyncio.to_thread(
        subprocess.run,
        [
            "docker",
            "build",
            "-f",
            "apps/assets-source/Dockerfile",
            "-t",
            image,
            "--label",
            "org.aap.acceptance=module-data-boundary",
            "--label",
            f"org.aap.acceptance-id={identity}",
            ".",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    assert build.returncode == 0, build.stderr[-2000:]
    try:
        yield image, identity
    finally:
        _remove_worker_image(image, identity)


def _start(stack, *, key=True, known=True):
    values = {
        **stack.values,
        "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY": stack.key if key else "",
    }
    if known:
        values["ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE"] = "/run/secrets/known_hosts"
    env_file = stack.directory / f"source-{uuid4()}.env"
    _write_environment(env_file, values)
    command = [
        "run",
        "-d",
        "--name",
        f"aap-connections-{stack.identity[:10]}-{uuid4().hex[:6]}",
        "--label",
        "org.aap.acceptance=module-data-boundary",
        "--label",
        f"org.aap.acceptance-id={stack.identity}",
        "--add-host",
        "host.docker.internal:host-gateway",
        "--publish",
        "127.0.0.1::8767",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "64",
        "--memory",
        "512m",
        "--cpus",
        "0.5",
        "--health-interval",
        "1s",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        "--env-file",
        str(env_file),
    ]
    if known:
        command.extend(
            [
                "--mount",
                f"type=bind,source={stack.ssh.known_hosts},target=/run/secrets/known_hosts,readonly",
            ]
        )
    container = _docker(*command, stack.image)
    stack.containers.append(container)
    observed = _inspect_docker(container)
    assert observed["Config"]["User"] == "10003:10003"
    assert observed["Config"]["Labels"]["org.aap.acceptance-id"] == stack.identity
    assert observed["HostConfig"]["Memory"] == 512 * 1024 * 1024
    assert observed["HostConfig"]["NanoCpus"] == 500_000_000
    mounts = observed["Mounts"]
    assert len(mounts) == int(known)
    if known:
        assert (
            mounts[0]["Destination"] == "/run/secrets/known_hosts"
            and mounts[0]["RW"] is False
        )
        assert Path(mounts[0]["Source"]).resolve() == stack.ssh.known_hosts.resolve()
    assert not any(
        value.startswith(("DATABASE_URL=", "MINIO_", "AWS_", "REDIS_", "CELERY_"))
        for value in observed["Config"]["Env"]
    )
    port = observed["NetworkSettings"]["Ports"]["8767/tcp"]
    assert len(port) == 1 and port[0]["HostIp"] == "127.0.0.1"
    stack.container = container
    stack.origin = f"http://127.0.0.1:{port[0]['HostPort']}"
    return container


async def _restart(stack, settings, *, key=True, known=True):
    if getattr(stack, "container", None):
        await asyncio.to_thread(_stop_remove_container, stack.container, stack.identity)
        stack.containers.remove(stack.container)
        stack.container = None
    container = await asyncio.to_thread(_start, stack, key=key, known=known)
    try:
        await _wait_healthy(container)
    except BaseException:
        logs = await asyncio.to_thread(
            subprocess.run,
            ["docker", "logs", container],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        diagnostic = logs.stdout + logs.stderr
        sensitive = [
            *stack.values.values(),
            *stack.secret.values(),
            stack.key,
            make_url(stack.values["ASSETS_SOURCE_DATABASE_URL"]).password,
        ]
        for value in sensitive:
            if value and len(value) > 3:
                diagnostic = diagnostic.replace(value, "<redacted>")
        output = ROOT / ".worktree/assets-connections-container-startup.log"
        output.write_text(diagnostic)
        output.chmod(0o600)
        raise
    await _wait_public_ready(stack.origin)
    settings.assets_source_url = stack.origin


@asynccontextmanager
async def _stack(test_engine, app_module, monkeypatch, connections_image):
    from app.config import settings
    from app.core.security import create_access_token
    from app.db.models.user import User
    from app.deps import get_db
    from app.services.storage import storage_service

    role_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_SOURCE_URL"))
    assert (
        role_url.username == "asset_source_client"
        and role_url.database == test_engine.url.database
    )
    assert role_url.database.endswith("_test")
    _verify_postgres(role_url)
    resources = _verified_resource_manifest(test_engine, role_url)
    world = await _seed_world(test_engine)
    owner_id = uuid4()
    async with world.maker.begin() as db:
        db.add(
            User(
                id=owner_id,
                email=f"connections-{owner_id}@test.local",
                name="Connection owner",
                password_hash="unused-test",
                role="project_admin",
                is_active=True,
                status="offline",
            )
        )
        await db.execute(
            text(
                "INSERT INTO public.system_settings(key,value_type,value_json) VALUES "
                "('connector_host_allowlist','json',CAST(:policy AS jsonb)) ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json"
            ),
            {"policy": json.dumps(POLICY)},
        )
    engine = create_async_engine(test_engine.url, hide_parameters=True)

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def no_private_relations(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        if re.search(
            r'\b(?:FROM|JOIN|UPDATE|INTO)\s+(?:public\.)?"?(?:storage_connections|storage_connection_operations)\b',
            statement,
            re.I,
        ):
            raise AssertionError("public gateway bypassed the Assets connection owner")

    async def request_db():
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db

    previous = dict(app_module.dependency_overrides)
    app_module.dependency_overrides[get_db] = request_db
    token = secrets.token_urlsafe(32)
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "assets_source_token", token)
    monkeypatch.setattr(settings, "connector_encryption_key", key)
    monkeypatch.setattr(settings, "connector_host_allowlist", POLICY)
    scheme = "https" if settings.minio_use_ssl else "http"
    minio = urlsplit(f"{scheme}://{settings.minio_endpoint}")
    host = _host_address()
    image, identity = connections_image
    with tempfile.TemporaryDirectory(
        prefix="assets-connections-runtime-", dir=os.environ["TMPDIR"]
    ) as directory:
        directory = Path(directory)
        async with _sftp_server(directory, host) as ssh:
            stack = SimpleNamespace(
                world=world,
                owner_id=owner_id,
                resources=resources,
                directory=directory,
                image=image,
                identity=identity,
                containers=[],
                container=None,
                token=token,
                key=key,
                ssh=ssh,
                storage=storage_service,
                prefix=f"connector-import-source/{uuid4()}",
                config={
                    "endpoint": f"{scheme}://{host}:{minio.port or 9000}",
                    "bucket": resources["buckets"]["MINIO_IMPORT_BUCKET"],
                    "use_ssl": settings.minio_use_ssl,
                },
                secret={
                    "access_key": _required_env(
                        "AAP_MODULE_DATA_IMPORT_SOURCE_S3_ACCESS_KEY"
                    ),
                    "secret_key": _required_env(
                        "AAP_MODULE_DATA_IMPORT_SOURCE_S3_SECRET_KEY"
                    ),
                },
                headers={
                    name: {
                        "Authorization": f"Bearer {create_access_token(str(actor), role)}"
                    }
                    for name, actor, role in (
                        ("super", world.actor_id, "super_admin"),
                        ("employee", world.other_actor_id, "employee"),
                        ("owner", owner_id, "project_admin"),
                    )
                },
            )
            stack.config["base_prefix"] = stack.prefix
            stack.values = {
                "ASSETS_SOURCE_DATABASE_URL": _container_database_url(role_url),
                "ASSETS_SOURCE_TOKEN": token,
                "ASSETS_SOURCE_S3_ENDPOINT": f"{scheme}://host.docker.internal:{minio.port or 9000}",
                "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": f"{scheme}://{settings.minio_endpoint}",
                "ASSETS_SOURCE_S3_ACCESS_KEY": _required_env(
                    "AAP_MODULE_DATA_ASSET_SOURCE_S3_ACCESS_KEY"
                ),
                "ASSETS_SOURCE_S3_SECRET_KEY": _required_env(
                    "AAP_MODULE_DATA_ASSET_SOURCE_S3_SECRET_KEY"
                ),
                "ASSETS_SOURCE_DATASETS_BUCKET": resources["buckets"][
                    "MINIO_DATASETS_BUCKET"
                ],
                "ASSETS_SOURCE_IMPORT_BUCKET": resources["buckets"][
                    "MINIO_IMPORT_BUCKET"
                ],
                "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": resources["buckets"][
                    "MINIO_MEDIA_CACHE_BUCKET"
                ],
                "ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST": json.dumps(POLICY),
            }
            keys = [f"{stack.prefix}/{index}.txt" for index in range(4)]
            try:
                for object_key in keys:
                    storage_service.client.put_object(
                        Bucket=stack.config["bucket"],
                        Key=object_key,
                        Body=b"owned probe",
                    )
                await _restart(stack, settings)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app_module),
                    base_url="http://gateway",
                    timeout=60,
                ) as api:
                    stack.api = api
                    yield stack
            finally:
                app_module.dependency_overrides.clear()
                app_module.dependency_overrides.update(previous)
                for container in reversed(stack.containers):
                    _stop_remove_container(container, identity)
                for object_key in keys:
                    storage_service.client.delete_object(
                        Bucket=stack.config["bucket"], Key=object_key
                    )
                await engine.dispose()
                # Audit and immutable receipts remain until the verified owned DB is destroyed.


async def _create(
    stack,
    *,
    actor="super",
    scope="global",
    config=None,
    secret=None,
    kind="s3",
    operation=None,
):
    operation = operation or uuid4()
    result = await stack.api.post(
        "/api/v1/storage-connections",
        headers={**stack.headers[actor], "Idempotency-Key": str(operation)},
        json={
            "name": f"connection-{operation}",
            "kind": kind,
            "config": config or stack.config,
            "secret": secret or stack.secret,
            "scope": scope,
        },
    )
    assert result.status_code == 201
    body = result.json()
    assert (
        body["id"] == str(operation)
        and body["project_id"] is None
        and body["secret_set"] is True
    )
    assert set(body) == {
        "id",
        "name",
        "kind",
        "config",
        "scope",
        "project_id",
        "secret_set",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert not any(
        value in result.text
        for value in (stack.secret["access_key"], stack.secret["secret_key"], stack.key)
    )
    return body


async def _count(stack, query, **values):
    async with stack.world.maker() as db:
        return await db.scalar(text(query), values)


@asynccontextmanager
async def _paused_s3(host):
    started, release = asyncio.Event(), asyncio.Event()
    handlers = set()

    async def respond(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            assert request.startswith(b"GET ")
            started.set()
            await asyncio.wait_for(release.wait(), 10)
            body = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                b"<KeyCount>0</KeyCount><MaxKeys>20</MaxKeys>"
                b"<IsTruncated>false</IsTruncated></ListBucketResult>"
            )
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(respond, host=host, port=0)
    try:
        yield SimpleNamespace(
            endpoint=f"http://{host}:{server.sockets[0].getsockname()[1]}",
            started=started,
            release=release,
        )
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.parametrize("change", ["source", "policy", "actor"])
async def test_connections_recheck_authority_source_and_policy_after_real_io(
    test_engine, app_module, monkeypatch, connections_image, change
):
    async with _stack(test_engine, app_module, monkeypatch, connections_image) as stack:
        async with _paused_s3(stack.ssh.host) as target:
            connection = await _create(
                stack,
                config={
                    "endpoint": target.endpoint,
                    "bucket": "paused",
                    "use_ssl": False,
                },
            )
            path = "/api/v1/storage-connections/" + connection["id"]
            operation = uuid4()
            pending = asyncio.create_task(
                stack.api.post(
                    path + "/test",
                    headers={
                        **stack.headers["employee"],
                        "Idempotency-Key": str(operation),
                    },
                )
            )
            try:
                await asyncio.wait_for(target.started.wait(), 10)
                if change == "source":
                    changed = await asyncio.wait_for(
                        stack.api.patch(
                            path,
                            headers=stack.headers["super"],
                            json={"name": "changed during connector I/O"},
                        ),
                        5,
                    )
                    assert changed.status_code == 200
                elif change == "policy":
                    changed = await asyncio.wait_for(
                        stack.api.put(
                            "/api/v1/storage-connections/allowlist",
                            headers=stack.headers["super"],
                            json={"entries": ["8.8.8.8"]},
                        ),
                        5,
                    )
                    assert changed.status_code == 200
                else:
                    async with stack.world.maker.begin() as db:
                        await asyncio.wait_for(
                            db.execute(
                                text(
                                    "UPDATE public.users SET is_active=false WHERE id=:id"
                                ),
                                {"id": stack.world.other_actor_id},
                            ),
                            5,
                        )
                assert not pending.done()
            finally:
                target.release.set()
                response = await asyncio.wait_for(pending, 15)
            assert response.status_code == (401 if change == "actor" else 409)
            assert (
                await _count(
                    stack,
                    "SELECT count(*) FROM public.storage_connection_operations WHERE operation_id=:id",
                    id=operation,
                )
                == 0
            )
            assert (
                await _count(
                    stack,
                    "SELECT count(*) FROM public.audit_logs WHERE action='storage_connection.test' AND target_id=:id",
                    id=connection["id"],
                )
                == 0
            )


async def test_connections_real_source_crud_probes_replay_restart_and_pages(
    test_engine, app_module, monkeypatch, connections_image
):
    from app.config import settings

    async with _stack(test_engine, app_module, monkeypatch, connections_image) as stack:
        async with httpx.AsyncClient(trust_env=False) as direct:
            assert (
                await direct.post(
                    stack.origin + "/v1/connections/list",
                    json={"actor_id": str(stack.world.actor_id)},
                )
            ).status_code == 401
            bad = await direct.post(
                stack.origin + "/v1/connections/list",
                headers={"Authorization": f"Bearer {stack.token}"},
                json={
                    "actor_id": str(stack.world.actor_id),
                    "fallback_allowlist": ["0.0.0.0/0"],
                },
            )
            assert bad.json()["error"]["code"] == "invalid_request"
        global_connection = await _create(stack)
        private_connection = await _create(stack, actor="owner", scope="owner")
        private_path = "/api/v1/storage-connections/" + private_connection["id"]
        assert (
            await stack.api.get(private_path, headers=stack.headers["employee"])
        ).status_code == 404
        assert (
            await stack.api.post(
                private_path + "/test", headers=stack.headers["employee"]
            )
        ).status_code == 404
        assert (
            await stack.api.patch(
                private_path,
                headers=stack.headers["employee"],
                json={"name": "forbidden"},
            )
        ).status_code == 403
        listed = await stack.api.get(
            "/api/v1/storage-connections", headers=stack.headers["employee"]
        )
        assert listed.status_code == 200
        assert global_connection["id"] in {item["id"] for item in listed.json()}
        assert private_connection["id"] not in {item["id"] for item in listed.json()}
        async with stack.world.maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET role='viewer' WHERE id=:id"),
                {"id": stack.owner_id},
            )
        renamed = await stack.api.patch(
            private_path,
            headers=stack.headers["owner"],
            json={"name": "downgraded owner"},
        )
        assert (
            renamed.status_code == 200 and renamed.json()["name"] == "downgraded owner"
        )
        path = "/api/v1/storage-connections/" + global_connection["id"]
        probe_key = uuid4()
        async with _lost_response_proxy(stack.origin) as proxy:
            monkeypatch.setattr(settings, "assets_source_url", proxy)
            lost = await stack.api.post(
                path + "/test",
                headers={
                    **stack.headers["employee"],
                    "Idempotency-Key": str(probe_key),
                },
            )
            assert lost.status_code == 503
        monkeypatch.setattr(settings, "assets_source_url", stack.origin)
        assert (
            await _count(
                stack,
                "SELECT count(*) FROM public.storage_connection_operations WHERE operation_id=:id",
                id=probe_key,
            )
            == 1
        )
        peak_before = int(
            await asyncio.to_thread(
                _docker,
                "exec",
                stack.container,
                "python",
                "-c",
                "from pathlib import Path;print(Path('/sys/fs/cgroup/memory.peak').read_text())",
            )
        )
        await _restart(stack, settings, key=False)
        replay = await stack.api.post(
            path + "/test",
            headers={**stack.headers["employee"], "Idempotency-Key": str(probe_key)},
        )
        assert replay.status_code == 200 and replay.json() == {
            "ok": True,
            "message": "连接成功",
            "sample_count": 4,
        }
        assert (
            await _count(
                stack,
                "SELECT count(*) FROM public.audit_logs WHERE action='storage_connection.test' AND target_id=:id",
                id=global_connection["id"],
            )
            == 1
        )
        policy = await stack.api.put(
            "/api/v1/storage-connections/allowlist",
            headers=stack.headers["super"],
            json={"entries": []},
        )
        assert policy.status_code == 200
        unchanged = await stack.api.patch(
            private_path,
            headers=stack.headers["owner"],
            json={"name": None, "config": None, "secret": None},
        )
        assert (
            unchanged.status_code == 200
            and unchanged.json()["updated_at"] == renamed.json()["updated_at"]
        )
        changed = await stack.api.patch(
            private_path,
            headers=stack.headers["owner"],
            json={"name": "without key or allowlist"},
        )
        assert changed.status_code == 200
        assert (
            await stack.api.put(
                "/api/v1/storage-connections/allowlist",
                headers=stack.headers["super"],
                json={"entries": POLICY},
            )
        ).status_code == 200
        await _restart(stack, settings, key=True)
        sftp = await _create(
            stack,
            kind="sftp",
            config={
                "host": stack.ssh.host,
                "port": stack.ssh.port,
                "username": "import",
                "base_path": "/data/good",
            },
            secret={"password": "owned-test"},
        )
        tested = await stack.api.post(
            "/api/v1/storage-connections/" + sftp["id"] + "/test",
            headers=stack.headers["employee"],
        )
        assert tested.status_code == 200 and tested.json() == {
            "ok": True,
            "message": "连接成功",
            "sample_count": 1,
        }
        await _restart(stack, settings, key=True, known=False)
        rejected = await stack.api.post(
            "/api/v1/storage-connections/" + sftp["id"] + "/test",
            headers=stack.headers["employee"],
        )
        assert (
            rejected.status_code == 200
            and rejected.json()["ok"] is False
            and rejected.json()["sample_count"] is None
        )
        assert re.fullmatch(
            r"连接失败: [A-Za-z_][A-Za-z0-9_]{0,127}", rejected.json()["message"]
        )
        assert "owned-test" not in rejected.text and stack.ssh.host not in rejected.text
        delete_key = uuid4()
        removed = await stack.api.delete(
            private_path,
            headers={**stack.headers["owner"], "Idempotency-Key": str(delete_key)},
        )
        assert removed.status_code == 204
        assert (
            await stack.api.delete(
                private_path,
                headers={**stack.headers["owner"], "Idempotency-Key": str(delete_key)},
            )
        ).status_code == 204
        assert (
            await _count(
                stack,
                "SELECT count(*) FROM public.audit_logs WHERE action='storage_connection.delete' AND target_id=:id",
                id=private_connection["id"],
            )
            == 1
        )
        async with stack.world.maker.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO public.storage_connections(id,name,kind,config,scope,created_by) "
                    "SELECT gen_random_uuid(),'large-'||n,'s3',jsonb_build_object('endpoint','http://8.8.8.8','bucket','data','base_prefix',repeat('p',17000)),'global',:actor "
                    "FROM generate_series(1,1005) AS n"
                ),
                {"actor": stack.world.actor_id},
            )
        large_config = {**stack.config, "base_prefix": "p" * 950000}
        await _create(stack, config=large_config)
        async with stack.world.maker.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO public.storage_connections(id,name,kind,config,scope,created_by) "
                    "SELECT gen_random_uuid(),'near-request-limit-'||n,'s3',CAST(:config AS jsonb),'global',:actor "
                    "FROM generate_series(1,25) AS n"
                ),
                {"actor": stack.world.actor_id, "config": json.dumps(large_config)},
            )
        from app.services import assets_connections

        private_page_bytes = []
        read_response = assets_connections._read_response

        async def observe_page(response):
            body = await read_response(response)
            private_page_bytes.append(len(body))
            return body

        list_started = time.perf_counter()
        with monkeypatch.context() as observed:
            observed.setattr(assets_connections, "_read_response", observe_page)
            complete = await stack.api.get(
                "/api/v1/storage-connections", headers=stack.headers["employee"]
            )
        list_seconds = time.perf_counter() - list_started
        assert complete.status_code == 200 and len(complete.content) > 16_777_216
        assert len(complete.json()) >= 1033
        assert len(private_page_bytes) >= 6
        assert max(private_page_bytes) <= 16_777_216
        peak_after_list = int(
            await asyncio.to_thread(
                _docker,
                "exec",
                stack.container,
                "python",
                "-c",
                "from pathlib import Path;print(Path('/sys/fs/cgroup/memory.peak').read_text())",
            )
        )
        await asyncio.to_thread(_stop_remove_container, stack.container, stack.identity)
        stack.containers.remove(stack.container)
        stack.container = None
        monkeypatch.setattr(settings, "assets_source_url", "")
        local = await stack.api.patch(
            path,
            headers=stack.headers["super"],
            json={"name": "explicit local cutover"},
        )
        assert (
            local.status_code == 200
            and local.json()["name"] == "explicit local cutover"
        )
        evidence = {
            "status": "passed",
            "source_functions": 26,
            "importer_functions": 9,
            "cpu": 0.5,
            "memory_limit_bytes": 512 * 1024 * 1024,
            "observed_peak_before_restart": peak_before,
            "observed_peak_after_list": peak_after_list,
            "public_list_bytes": len(complete.content),
            "public_list_count": len(complete.json()),
            "public_list_seconds": list_seconds,
            "private_page_bytes": private_page_bytes,
            "real_s3": True,
            "real_sftp_known_host": True,
            "unknown_host_rejected": True,
            "lost_response_single_audit": True,
            "replay_without_key": True,
            "no_key_no_policy_patch": True,
            "downgraded_owner": True,
            "delete_replay": True,
            "explicit_local_cutover": True,
        }
        output = ROOT / ".worktree/assets-connections-runtime-evidence.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2) + "\n")
