"""Current installed API and Audit queue consumers on verifier-owned resources."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import uuid

from celery import Celery
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_identity.security import create_access_token
from app.config import settings
from app.db.models.audit_log import AuditLog
from tests import test_labeling_module_api_cold as cold
from tests.factory import create_project, create_task, create_user
from tests.notifications_publisher_acceptance_helpers import (
    connect_owned_dependency,
    disconnect_owned_dependency,
)
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _verified_resource_manifest,
    _verify_postgres,
)
from tests.test_assets_catalog_runtime import _write_environment
from tests.test_identity_module_assembly_live import _restored_database
from tests.test_identity_recovery import _postgres_tool
from tests.test_identity_sessions_live import _verify_redis
from tests.test_module_data_roles import _role_url
from tests.test_workspace_worker_assembly_live import _broker_state

ROOT = Path(__file__).resolve().parents[3]
TASK = "app.workers.audit.persist_audit_entry"
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_AUDIT_COLD_CONSUMER_ACCEPTANCE") != "1",
        reason="requires current API artifact and explicitly selected owned Audit resources",
    ),
]


@asynccontextmanager
async def _consumers(
    test_engine, directory, *, database=None, signing_secret, record_property
):
    image, image_id, artifact = cold._image_artifact()
    cold._verify_image(image, image_id, artifact)
    for name, digest in artifact["source_manifest"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    api_host = cold._role_url(
        "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL",
        "identity_api_consumer",
        test_engine,
    )
    _verify_postgres(api_host)
    resources = _verified_resource_manifest(test_engine, api_host)
    postgres = cold._required("AAP_MODULE_DATA_PG_CONTAINER_ID")
    redis, owner, redis_url = cold._redis_dependency(resources, test_engine)
    minio = cold._minio_dependency(resources)
    cold._verify_minio_identity(_inspect_docker(minio["id"]), minio)
    database = database or test_engine.url.database
    assert database.endswith("_test")
    if database != test_engine.url.database:
        async with test_engine.connect() as db:
            marker = await db.scalar(
                text(
                    "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"
                ),
                {"name": database},
            )
            assert database.startswith("aap_identity_module_restore_")
            assert marker and marker.startswith("identity-module-restore:")
    api_url = api_host.set(host=cold._POSTGRES_ALIAS, port=5432, database=database)
    auth_url = cold._role_url(
        "AAP_MODULE_DATA_IDENTITY_AUTH_URL", "identity_auth_client", test_engine
    ).set(host=cold._POSTGRES_ALIAS, port=5432, database=database)
    append_url = make_url(
        _role_url("AAP_MODULE_DATA_AUDIT_APPEND_URL", "audit_append_client")
    ).set(host=cold._POSTGRES_ALIAS, port=5432, database=database)
    broker = redis_url.render_as_string(hide_password=False)
    key = cold._required("AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY")
    secret = cold._required("AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY")
    assert key != cold._plain(settings.minio_access_key)
    assert secret != cold._plain(settings.minio_secret_key)
    directory.mkdir()
    identity = uuid.uuid4().hex[:24]
    network = "aap-audit-cold-" + identity[:12]
    api_name, worker_name = network + "-api", network + "-worker"
    node_name = network + "@owned"
    auth_file = directory / "auth-database"
    api_env_file, worker_env_file = directory / "api.env", directory / "worker.env"
    api_env = {
        "ENVIRONMENT": "development",
        "DEBUG": "false",
        "SECRET_KEY": signing_secret,
        "DATABASE_URL": api_url.render_as_string(hide_password=False),
        "MIGRATION_DATABASE_URL": "",
        "ALEMBIC_AUTO_UPGRADE": "false",
        "E2E_SEED_ENABLED": "false",
        "AUDIT_ASYNC": "true",
        "IDENTITY_URL": "",
        "IDENTITY_TOKEN": "",
        "IDENTITY_AUTH_DATABASE_URL": "",
        "IDENTITY_AUTH_DATABASE_URL_FILE": cold._AUTH_SECRET_TARGET,
        "MINIO_ENDPOINT": minio["endpoint"],
        "MINIO_ACCESS_KEY": key,
        "MINIO_SECRET_KEY": secret,
        "MINIO_USE_SSL": "true" if minio["ssl"] else "false",
        **resources["buckets"],
    }
    worker_env = {
        "ENVIRONMENT": "development",
        "DATABASE_URL": append_url.render_as_string(hide_password=False),
        "MIGRATION_DATABASE_URL": "",
        "ALEMBIC_AUTO_UPGRADE": "false",
        "SECRET_KEY": secrets.token_urlsafe(32),
    }
    for env in (api_env, worker_env):
        env.update(
            {
                name: broker
                for name in (
                    "REDIS_URL",
                    "CELERY_BROKER_URL",
                    "CELERY_BROKER_READ_URL",
                    "CELERY_BROKER_WRITE_URL",
                    "CELERY_RESULT_BACKEND",
                )
            }
        )
    worker_program = """
import sys
from app.workers.celery_app import celery_app
from app.workers.audit import persist_audit_entry
assert not persist_audit_entry.acks_late
celery_app.conf.include = ['app.workers.audit']
celery_app.worker_main(sys.argv[1:])
"""
    containers = []
    connected = []
    network_id = None
    redacted = [
        signing_secret,
        key,
        secret,
        auth_url.render_as_string(hide_password=False),
        api_url.password,
        auth_url.password,
        append_url.password,
        worker_env["SECRET_KEY"],
        *[v for env in (api_env, worker_env) for k, v in env.items() if "URL" in k],
    ]
    try:
        cold._write_private_file(
            auth_file, auth_url.render_as_string(hide_password=False), mode=0o444
        )
        _write_environment(api_env_file, api_env)
        _write_environment(worker_env_file, worker_env)
        network_id = _docker(
            "network",
            "create",
            "--label",
            f"{cold._ACCEPTANCE_LABEL}={cold._CONTAINER_ACCEPTANCE}",
            "--label",
            f"{cold._ACCEPTANCE_ID_LABEL}={identity}",
            network,
        )
        cold._network_details(network, network_id, identity)
        for peer, alias in [
            (postgres, cold._POSTGRES_ALIAS),
            (redis, cold._REDIS_ALIAS),
            (minio["id"], cold._MINIO_ALIAS),
        ]:
            connect_owned_dependency(network, network_id, peer, alias)
            connected.append(peer)
        common = [
            "--network",
            network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10001:10001",
            "--pids-limit",
            "128",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--label",
            f"{cold._ACCEPTANCE_LABEL}={cold._CONTAINER_ACCEPTANCE}",
            "--label",
            f"{cold._ACCEPTANCE_ID_LABEL}={identity}",
            "--label",
            f"{cold._WORKTREE_OWNER_LABEL}={owner}",
        ]
        api = _docker(
            "create",
            "--name",
            api_name,
            *common,
            "--publish",
            "127.0.0.1::8000",
            "--mount",
            f"type=bind,source={auth_file.resolve()},target={cold._AUTH_SECRET_TARGET},readonly",
            "--env-file",
            str(api_env_file),
            image,
        )
        containers.append(api)
        worker = _docker(
            "create",
            "--name",
            worker_name,
            *common,
            "--env-file",
            str(worker_env_file),
            "--entrypoint",
            "python",
            image,
            "-c",
            worker_program,
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            "--queues=audit",
            f"--hostname={node_name}",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=WARNING",
        )
        containers.append(worker)
        for container in containers:
            _docker("start", container)
        origin = cold._verify_api_container(
            api,
            image_id=image_id,
            network_name=network,
            network_id=network_id,
            run_id=identity,
            resource_owner=owner,
            container_name=api_name,
            auth_secret_file=auth_file,
            database_url=api_url,
            auth_url=auth_url,
            minio_endpoint=minio["endpoint"],
            minio_access_key=key,
            minio_secret_key=secret,
        )
        await cold._wait_for_api(origin, api, secrets_to_redact=redacted)
        details = _inspect_docker(worker)
        assert (
            details["Image"] == image_id and details["Config"]["User"] == "10001:10001"
        )
        assert details["HostConfig"]["ReadonlyRootfs"] and not details[
            "HostConfig"
        ].get("PortBindings")
        assert all(m["Type"] == "tmpfs" for m in details["Mounts"])
        assert set(details["NetworkSettings"]["Networks"]) == {network}
        probe = """
import asyncio,importlib,json,os,sysconfig
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
async def main():
 engine=create_async_engine(os.environ['DATABASE_URL'],hide_parameters=True)
 try:
  async with engine.connect() as db:
   row=(await db.execute(text("SELECT current_user,current_database(),has_table_privilege(current_user,'public.audit_logs','SELECT'),has_table_privilege(current_user,'public.audit_logs','INSERT')"))).one()
  package=Path(importlib.import_module('aap_audit.query_contract').__file__).resolve()
  print(json.dumps({'role':row[0],'database':row[1],'private_select':row[2],'private_insert':row[3],'installed':package.is_relative_to(Path(sysconfig.get_paths()['purelib']))}))
 finally:await engine.dispose()
asyncio.run(main())
"""
        probes = {
            name: json.loads(_docker("exec", container, "python", "-c", probe))
            for name, container in [("api", api), ("worker", worker)]
        }
        assert probes["api"]["role"] == "identity_api_consumer"
        assert probes["worker"]["role"] == "audit_append_client"
        assert all(
            p["database"] == database
            and p["installed"]
            and not p["private_select"]
            and not p["private_insert"]
            for p in probes.values()
        )
        async with httpx.AsyncClient(
            base_url=origin, trust_env=False, timeout=20
        ) as client:
            yield {
                "client": client,
                "api": api,
                "worker": worker,
                "node_name": node_name,
                "origin": origin,
                "image_id": image_id,
                "probes": probes,
                "redacted": redacted,
            }
    finally:
        errors = []
        for container in reversed(containers):
            try:
                try:
                    details = _inspect_docker(container)
                    assert (
                        details["Config"]["Labels"][cold._ACCEPTANCE_ID_LABEL]
                        == identity
                    )
                    logs = cold._api_failure_logs(container, redacted)
                    (directory / (container[:12] + ".log")).write_text(logs)
                    record_property(
                        f"audit_cold_{directory.name}_{container[:12]}", logs
                    )
                finally:
                    _docker("rm", "--force", container)
            except Exception as error:
                errors.append(type(error).__name__)
        for peer in reversed(connected):
            try:
                disconnect_owned_dependency(network, network_id, peer)
            except Exception as error:
                errors.append(type(error).__name__)
        if network_id:
            try:
                state = cold._network_details(network, network_id, identity)
                assert not state.get("Containers")
                _docker("network", "rm", network_id)
            except Exception as error:
                errors.append(type(error).__name__)
        for path in (auth_file, api_env_file, worker_env_file):
            path.unlink(missing_ok=True)
        assert not errors, errors


async def _event_count(engine, request_id):
    async with engine.connect() as db:
        return await db.scalar(
            text(
                "SELECT count(*) FROM aap_audit_v1.audit_events WHERE request_id=:rid"
            ),
            {"rid": request_id},
        )


async def _wait_event(engine, request_id):
    async with asyncio.timeout(20):
        while not await _event_count(engine, request_id):
            await asyncio.sleep(0.1)
    assert await _event_count(engine, request_id) == 1


async def test_current_cold_audit_api_worker_queries_exports_and_restore(
    test_engine, tmp_path, record_property
):
    _verify_postgres(test_engine.url)
    _verify_redis()
    signing_secret = secrets.token_urlsafe(48)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    now = datetime.now(timezone.utc).replace(microsecond=123456)
    async with maker.begin() as db:
        assert await db.scalar(
            text(
                "SELECT has_table_privilege('legacy_runtime','public.audit_logs','SELECT')"
            )
        )
        admin = await create_user(
            db, "super_admin", f"audit-cold-admin-{nonce}@example.test", "Admin"
        )
        manager = await create_user(
            db, "project_admin", f"audit-cold-owner-{nonce}@example.test", "Owner"
        )
        foreign = await create_user(
            db, "project_admin", f"audit-cold-foreign-{nonce}@example.test", "Foreign"
        )
        employee = await create_user(
            db, "employee", f"audit-cold-employee-{nonce}@example.test", "Employee"
        )
        owned = await create_project(db, owner_id=manager.id)
        other = await create_project(db, owner_id=foreign.id)
        task = await create_task(db, project_id=owned.id)
        specs = [
            (9223372036854775807, "project", str(owned.id), None, "cold.audit", now),
            (9007199254740993, "task", str(task.id), None, "cold.audit", now),
            (
                0,
                "project",
                str(other.id),
                f"/api/v1/projects/{owned.id}",
                "cold.audit",
                now,
            ),
            (
                -9223372036854775808,
                "unknown_future_target",
                "opaque",
                None,
                "cold.audit",
                now,
            ),
            (
                9007199254740995,
                None,
                None,
                f"/api/v1/projects/{owned.id}/members",
                "cold.audit",
                now - timedelta(seconds=1),
            ),
            (
                9007199254740996,
                "project",
                str(owned.id),
                None,
                "http.post",
                now + timedelta(seconds=1),
            ),
        ]
        for event_id, target_type, target_id, path, action, created in specs:
            db.add(
                AuditLog(
                    id=event_id,
                    actor_id=manager.id,
                    actor_role=manager.role,
                    actor_email=None,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    path=path,
                    created_at=created,
                    ip="2001:db8::1",
                    status_code=201,
                    detail_json={
                        "cold_run": nonce,
                        "project_id": str(owned.id),
                        "native": [None, True, 9007199254740993, '文本,\n"'],
                    },
                )
            )
        # Narrow only this owned fixture's API consumer; held Learning compatibility stays intact.
        await db.execute(
            text("REVOKE SELECT ON public.audit_logs FROM identity_api_consumer")
        )
    tokens = {
        name: {
            "Authorization": "Bearer "
            + create_access_token(
                str(user.id), user.role, secret=signing_secret, expire_minutes=30
            )
        }
        for name, user in [
            ("admin", admin),
            ("manager", manager),
            ("foreign", foreign),
            ("employee", employee),
        ]
    }
    filters = {
        "actor_id": str(manager.id),
        "detail_key": "cold_run",
        "detail_value": nonce,
    }
    sender = Celery(
        "audit-cold-consumers",
        broker=os.environ["REDIS_URL"],
        backend=os.environ["REDIS_URL"],
    )
    sender.conf.update(task_serializer="json", accept_content=["json"])
    trigger = "audit_cold_pause_" + uuid.uuid4().hex[:12]
    lock = uuid.uuid4().int % (1 << 62)
    trigger_created = False
    deliveries = []
    probes = []
    try:
        async with _consumers(
            test_engine,
            tmp_path / "original",
            signing_secret=signing_secret,
            record_property=record_property,
        ) as stack:
            client = stack["client"]
            probes.append(stack["probes"])
            inspector = sender.control.inspect(
                destination=[stack["node_name"]], timeout=2
            )
            async with asyncio.timeout(35):
                while TASK not in (
                    await asyncio.to_thread(inspector.registered) or {}
                ).get(stack["node_name"], []):
                    await asyncio.sleep(0.2)
            assert (await client.get("/api/v1/audit-logs")).status_code == 401
            for role in ["employee", "manager"]:
                assert (
                    await client.get("/api/v1/audit-logs/export", headers=tokens[role])
                ).status_code == 403
            assert (
                await client.get("/api/v1/audit-logs", headers=tokens["employee"])
            ).status_code == 403
            ids = []
            cursor = None
            for _ in range(5):
                params = {**filters, "page_size": 2}
                if cursor:
                    params["cursor"] = cursor
                response = await client.get(
                    "/api/v1/audit-logs", params=params, headers=tokens["admin"]
                )
                assert response.status_code == 200, response.text
                data = response.json()
                assert data["total"] == 6
                ids.extend(r["id"] for r in data["items"])
                assert all(r["actor_email"] == manager.email for r in data["items"])
                cursor = data["next_cursor"]
                if not cursor:
                    break
            assert len(ids) == len(set(ids)) == 6 and set(ids) == {r[0] for r in specs}
            for role, expected in [
                (
                    "manager",
                    {r[0] for r in specs if r[0] not in {0, -9223372036854775808}},
                ),
                ("foreign", {0}),
            ]:
                response = await client.get(
                    "/api/v1/audit-logs",
                    params={**filters, "page_size": 100},
                    headers=tokens[role],
                )
                assert response.status_code == 200, response.text
                data = response.json()
                assert (
                    data["total"] == len(expected)
                    and {r["id"] for r in data["items"]} == expected
                )
            response = await client.get(
                "/api/v1/audit-logs",
                params={
                    **filters,
                    "from": now.isoformat(),
                    "to": now.isoformat(),
                    "target_type": "project",
                    "target_id": str(owned.id),
                },
                headers=tokens["admin"],
            )
            assert response.status_code == 200 and [
                r["id"] for r in response.json()["items"]
            ] == [9223372036854775807]
            for format in ["json", "csv"]:
                rid = uuid.uuid4().hex
                async with client.stream(
                    "GET",
                    "/api/v1/audit-logs/export",
                    params={**filters, "business_only": "true", "format": format},
                    headers={**tokens["admin"], "X-Request-ID": rid},
                ) as response:
                    assert response.status_code == 200
                    assert await _event_count(test_engine, rid) == 1
                    body = await response.aread()
                if format == "json":
                    data = json.loads(body)
                    rows = data["items"]
                    assert (
                        data["_export_meta"]["rows"] == 5
                        and data["_export_meta"]["request_id"] == rid
                    )
                    assert rows[0]["detail_json"]["native"] == [
                        None,
                        True,
                        9007199254740993,
                        '文本,\n"',
                    ]
                else:
                    raw = body.decode()
                    assert raw.startswith("# Exported by: " + admin.email)
                    rows = list(csv.DictReader(io.StringIO(raw.split("\n", 3)[3])))
                    assert json.loads(rows[0]["detail_json"])["native"] == [
                        None,
                        True,
                        9007199254740993,
                        '文本,\n"',
                    ]
                assert len(rows) == 5 and {int(r["id"]) for r in rows} == {
                    r[0] for r in specs if r[4] != "http.post"
                }
            rid = uuid.uuid4().hex
            assert (
                await client.post(
                    "/api/v1/projects",
                    json={},
                    headers={**tokens["admin"], "X-Request-ID": rid},
                )
            ).status_code == 422
            await _wait_event(test_engine, rid)
            lost_id = uuid.uuid4().hex
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        f"""CREATE FUNCTION public.{trigger}() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $pause$ BEGIN IF NEW.request_id='{lost_id}' THEN PERFORM pg_advisory_xact_lock({lock}); END IF; RETURN NEW; END $pause$"""
                    )
                )
                await db.execute(
                    text(f"REVOKE ALL ON FUNCTION public.{trigger}() FROM PUBLIC")
                )
                await db.execute(
                    text(
                        f"CREATE TRIGGER {trigger} BEFORE INSERT ON public.audit_logs FOR EACH ROW EXECUTE FUNCTION public.{trigger}()"
                    )
                )
            trigger_created = True
            async with test_engine.connect() as control:
                await control.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock}
                )
                assert (
                    await client.post(
                        "/api/v1/projects",
                        json={},
                        headers={**tokens["admin"], "X-Request-ID": lost_id},
                    )
                ).status_code == 422
                async with asyncio.timeout(20):
                    while True:
                        # The worker connection starts after the first activity snapshot.
                        await control.execute(text("SELECT pg_stat_clear_snapshot()"))
                        if await control.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid WHERE l.locktype='advisory' AND NOT l.granted AND l.classid=:high AND l.objid=:low AND a.usename='audit_append_client'"
                            ),
                            {"high": lock >> 32, "low": lock & 0xFFFFFFFF},
                        ):
                            break
                        await asyncio.sleep(0.1)
                assert await asyncio.to_thread(_broker_state, sender, "audit") == {
                    "queue_size": 0,
                    "unacknowledged": 0,
                }
                inspector = sender.control.inspect(
                    destination=[stack["node_name"]], timeout=2
                )
                active = await asyncio.to_thread(inspector.active)
                running = [
                    r
                    for r in active[stack["node_name"]]
                    if r["args"][0].get("request_id") == lost_id
                ]
                assert len(running) == 1
                payload = running[0]["args"][0]
                pid = running[0]["worker_pid"]
                program = "import os,sys;from pathlib import Path;pid=int(sys.argv[1]);assert pid>1;parent=next(l.split()[1] for l in Path('/proc/'+str(pid)+'/status').read_text().splitlines() if l.startswith('PPid:'));assert parent=='1';os.kill(pid,9)"
                _docker("exec", stack["worker"], "python", "-c", program, str(pid))
                await control.rollback()
            async with test_engine.begin() as db:
                await db.execute(text(f"DROP TRIGGER {trigger} ON public.audit_logs"))
                await db.execute(text(f"DROP FUNCTION public.{trigger}()"))
            trigger_created = False
            marker = uuid.uuid4().hex
            assert (
                await client.post(
                    "/api/v1/projects",
                    json={},
                    headers={**tokens["admin"], "X-Request-ID": marker},
                )
            ).status_code == 422
            await _wait_event(test_engine, marker)
            assert await _event_count(test_engine, lost_id) == 0
            delivery = await asyncio.to_thread(
                sender.send_task, TASK, args=[payload], queue="audit"
            )
            deliveries.append(delivery)
            assert await asyncio.to_thread(delivery.get, timeout=15) is None
            await _wait_event(test_engine, lost_id)
            _docker("restart", stack["api"])
            await cold._wait_for_api(
                stack["origin"], stack["api"], secrets_to_redact=stack["redacted"]
            )
            assert (
                await client.get(
                    "/api/v1/audit-logs", params=filters, headers=tokens["admin"]
                )
            ).json()["total"] == 6
        dump = tmp_path / "audit-consumers.dump"
        dump.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", "--dbname", test_engine.url.database],
            )
        )
        dump.chmod(0o600)
        async with _restored_database(test_engine, dump) as clone:
            async with _consumers(
                test_engine,
                tmp_path / "restored",
                database=clone,
                signing_secret=signing_secret,
                record_property=record_property,
            ) as stack:
                client = stack["client"]
                probes.append(stack["probes"])
                response = await client.get(
                    "/api/v1/audit-logs", params=filters, headers=tokens["admin"]
                )
                assert response.status_code == 200 and response.json()["total"] == 6
                response = await client.get(
                    "/api/v1/audit-logs/export",
                    params={**filters, "format": "json"},
                    headers=tokens["admin"],
                )
                assert (
                    response.status_code == 200 and len(response.json()["items"]) == 6
                )
                payload = {
                    "actor_id": str(uuid.uuid4()),
                    "actor_role": "employee",
                    "action": "http.post",
                    "method": "POST",
                    "path": "/api/v1/audit-cold-restored",
                    "status_code": 201,
                    "ip": "127.0.0.1",
                    "request_id": uuid.uuid4().hex,
                }
                delivery = await asyncio.to_thread(
                    sender.send_task, TASK, args=[payload], queue="audit"
                )
                deliveries.append(delivery)
                assert await asyncio.to_thread(delivery.get, timeout=15) is None
                clone_engine = create_async_engine(
                    test_engine.url.set(database=clone), hide_parameters=True
                )
                try:
                    await _wait_event(clone_engine, payload["request_id"])
                    async with clone_engine.connect() as db:
                        row = (
                            await db.execute(
                                text(
                                    "SELECT actor_id,action FROM aap_audit_v1.audit_events WHERE request_id=:rid"
                                ),
                                {"rid": payload["request_id"]},
                            )
                        ).one()
                        assert row.actor_id is None and row.action == "http.post"
                        assert await db.scalar(
                            text(
                                "SELECT has_table_privilege('legacy_runtime','public.audit_logs','SELECT')"
                            )
                        )
                finally:
                    await clone_engine.dispose()
        record_property(
            "audit_cold_consumers",
            json.dumps(
                {
                    "image_id": stack["image_id"],
                    "role_probes": probes,
                    "real_http_query_filter_cursor_project_scope": True,
                    "csv_and_json_exports": True,
                    "export_event_committed_before_stream_body": True,
                    "installed_queue_worker_and_http_producer": True,
                    "early_ack_prefork_child_loss_and_explicit_replay": True,
                    "api_restart": True,
                    "full_database_restore_and_consumer_resume": True,
                    "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
                    "held_learning_parent_select_unchanged": True,
                    "production_or_daily_cutover": False,
                }
            ),
        )
    finally:
        try:
            if trigger_created:
                async with test_engine.begin() as db:
                    await db.execute(
                        text(f"DROP TRIGGER {trigger} ON public.audit_logs")
                    )
                    await db.execute(text(f"DROP FUNCTION public.{trigger}()"))
        finally:
            try:
                for delivery in deliveries:
                    await asyncio.to_thread(delivery.forget)
            finally:
                sender.close()
