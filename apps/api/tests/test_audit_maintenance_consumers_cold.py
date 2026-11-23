"""Installed Beat scheduler and maintenance tasks on verifier-owned resources."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import uuid

from celery import Celery
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yoao_audit.maintenance_postgres import AuditMaintenancePostgresData
from app.config import settings
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.services.audit_archive_partitions import catalog, next_month, partition_name
from tests import test_labeling_module_api_cold as cold
from tests.conftest import _create_user
from tests.notifications_publisher_acceptance_helpers import (
    connect_owned_dependency,
    disconnect_owned_dependency,
)
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _local_s3_client,
    _verified_resource_manifest,
    _verify_postgres,
)
from tests.test_assets_catalog_runtime import _write_environment
from tests.test_audit_maintenance_module_live import _credentials, _expect_s3_denied
from tests.test_identity_module_assembly_live import _restored_database
from tests.test_identity_recovery import _postgres_tool
from tests.test_workspace_worker_assembly_live import _broker_state

ROOT = Path(__file__).resolve().parents[3]
ENTRIES = {
    "ensure-future-audit-partitions": "app.workers.audit_partition.ensure_future_audit_partitions",
    "archive-old-audit-partitions": "app.workers.audit_partition.archive_old_audit_partitions",
    "refresh-audit-bi-mv": "app.workers.cleanup.refresh_audit_bi_mv",
}
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_AUDIT_MAINTENANCE_CONSUMER_ACCEPTANCE") != "1",
        reason="requires the current API artifact and verifier-owned maintenance resources",
    ),
]


@asynccontextmanager
async def _worker(test_engine, directory, sender, record_property, *, database=None):
    image, image_id, artifact = cold._image_artifact()
    cold._verify_image(image, image_id, artifact)
    for name, digest in artifact["source_manifest"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
    host_url = test_engine.url
    assert host_url.username == "owner" and host_url.password and not host_url.query
    _verify_postgres(host_url)
    resources = _verified_resource_manifest(test_engine, host_url)
    redis, owner, broker = cold._redis_dependency(resources, test_engine)
    minio = cold._minio_dependency(resources)
    cold._verify_minio_identity(_inspect_docker(minio["id"]), minio)
    database = database or test_engine.url.database
    if database != test_engine.url.database:
        async with test_engine.connect() as db:
            marker = await db.scalar(
                text(
                    "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"
                ),
                {"name": database},
            )
        assert database.startswith(
            "aap_identity_module_restore_"
        ) and database.endswith("_test")
        assert marker and marker.startswith("identity-module-restore:")
    database_url = host_url.set(host=cold._POSTGRES_ALIAS, port=5432, database=database)
    key, secret = _credentials("PLATFORM")
    assert key != cold._plain(settings.minio_access_key) and secret != cold._plain(
        settings.minio_secret_key
    )
    token = uuid.uuid4().hex[:24]
    network = "aap-audit-maint-consumer-" + token[:12]
    node_name = network + "@owned"
    directory.mkdir(mode=0o700)
    broker_env = {
        "ENVIRONMENT": "development",
        "DEBUG": "false",
        "SECRET_KEY": secrets.token_urlsafe(32),
        "MIGRATION_DATABASE_URL": "",
        "ALEMBIC_AUTO_UPGRADE": "false",
        **{
            name: broker.render_as_string(hide_password=False)
            for name in (
                "REDIS_URL",
                "CELERY_BROKER_URL",
                "CELERY_BROKER_READ_URL",
                "CELERY_BROKER_WRITE_URL",
                "CELERY_RESULT_BACKEND",
            )
        },
    }
    worker_env = {
        **broker_env,
        "DATABASE_URL": database_url.render_as_string(hide_password=False),
        "MINIO_ENDPOINT": minio["endpoint"],
        "MINIO_USE_SSL": "true" if minio["ssl"] else "false",
        "MINIO_ACCESS_KEY": key,
        "MINIO_SECRET_KEY": secret,
        **resources["buckets"],
    }
    broker_file, worker_file = directory / "beat.env", directory / "worker.env"
    _write_environment(broker_file, broker_env)
    _write_environment(worker_file, worker_env)
    redacted = [
        key,
        secret,
        host_url.password,
        broker_env["SECRET_KEY"],
        worker_env["DATABASE_URL"],
        broker_env["REDIS_URL"],
    ]
    network_id = None
    containers, connected = [], []
    try:
        network_id = _docker(
            "network",
            "create",
            "--label",
            f"{cold._ACCEPTANCE_LABEL}={cold._CONTAINER_ACCEPTANCE}",
            "--label",
            f"{cold._ACCEPTANCE_ID_LABEL}={token}",
            network,
        )
        cold._network_details(network, network_id, token)
        for peer, alias in [
            (cold._required("AAP_MODULE_DATA_PG_CONTAINER_ID"), cold._POSTGRES_ALIAS),
            (redis, cold._REDIS_ALIAS),
            (minio["id"], cold._MINIO_ALIAS),
        ]:
            connect_owned_dependency(network, network_id, peer, alias)
            connected.append(peer)
        common = [
            "--network",
            network,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--user=10001:10001",
            "--pids-limit=128",
            "--cpus=1",
            "--memory=1g",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--label",
            f"{cold._ACCEPTANCE_LABEL}={cold._CONTAINER_ACCEPTANCE}",
            "--label",
            f"{cold._ACCEPTANCE_ID_LABEL}={token}",
            "--label",
            f"{cold._WORKTREE_OWNER_LABEL}={owner}",
        ]
        program = """
import sys
from app.workers.celery_app import celery_app
celery_app.conf.include=['app.workers.audit_partition','app.workers.cleanup']
celery_app.worker_main(sys.argv[1:])
"""
        worker = _docker(
            "create",
            "--name",
            network + "-worker",
            *common,
            "--env-file",
            str(worker_file),
            "--entrypoint=python",
            image_id,
            "-c",
            program,
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            "--prefetch-multiplier=1",
            "--queues=maintenance",
            "--hostname=" + node_name,
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=WARNING",
        )
        containers.append(worker)
        _docker("start", worker)
        details = _inspect_docker(worker)
        assert (
            details["Image"] == image_id and details["Config"]["User"] == "10001:10001"
        )
        assert details["HostConfig"]["ReadonlyRootfs"] and not details[
            "HostConfig"
        ].get("PortBindings")
        assert set(details["NetworkSettings"]["Networks"]) == {network}
        assert all(m["Type"] == "tmpfs" for m in details["Mounts"])
        inspector = sender.control.inspect(destination=[node_name], timeout=2)
        async with asyncio.timeout(35):
            while not set(ENTRIES.values()) <= set(
                (await asyncio.to_thread(inspector.registered) or {}).get(node_name, [])
            ):
                await asyncio.sleep(0.2)
        probe = """
import asyncio,importlib,inspect,json,os,sysconfig
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.workers.audit_partition import ensure_future_audit_partitions,archive_old_audit_partitions
from app.workers.cleanup import refresh_audit_bi_mv
async def main():
 engine=create_async_engine(os.environ['DATABASE_URL'],hide_parameters=True)
 try:
  async with engine.connect() as db:
   role,database=(await db.execute(text("SELECT current_user,current_database()"))).one()
  installed=Path(importlib.import_module('yoao_audit.maintenance_postgres').__file__).resolve().is_relative_to(Path(sysconfig.get_paths()['purelib']))
  ensure,archive,refresh=(inspect.signature(task.run) for task in (ensure_future_audit_partitions,archive_old_audit_partitions,refresh_audit_bi_mv))
  assert list(ensure.parameters)==['months_ahead'] and ensure.parameters['months_ahead'].default==3
  assert not archive.parameters and not refresh.parameters
  assert not any(task.acks_late for task in (ensure_future_audit_partitions,archive_old_audit_partitions,refresh_audit_bi_mv))
  print(json.dumps(dict(role=role,database=database,installed=installed,early_ack=True,default_signatures=True)))
 finally:await engine.dispose()
asyncio.run(main())
"""
        proof = json.loads(_docker("exec", worker, "python", "-c", probe))
        assert proof["role"] == host_url.username and proof["database"] == database
        assert proof["installed"] and proof["default_signatures"] and proof["early_ack"]
        yield {
            "worker": worker,
            "node_name": node_name,
            "image_id": image_id,
            "common": common,
            "containers": containers,
            "broker_file": broker_file,
            "broker_env": broker_env,
            "minio": minio,
            "buckets": resources["buckets"],
            "redacted": redacted,
            "proof": proof,
            "dispatches": [],
        }
    finally:
        errors = []
        for container in reversed(containers):
            try:
                try:
                    details = _inspect_docker(container)
                    assert (
                        details["Config"]["Labels"][cold._ACCEPTANCE_ID_LABEL] == token
                    )
                    record_property(
                        f"audit_maintenance_{directory.name}_{container[:12]}",
                        cold._api_failure_logs(container, redacted),
                    )
                finally:
                    _docker("rm", "--force", container)
            except BaseException as error:
                errors.append(type(error).__name__)
        for peer in reversed(connected):
            try:
                disconnect_owned_dependency(network, network_id, peer)
            except BaseException as error:
                errors.append(type(error).__name__)
        if network_id:
            try:
                assert not cold._network_details(network, network_id, token).get(
                    "Containers"
                )
                _docker("network", "rm", network_id)
            except BaseException as error:
                errors.append(type(error).__name__)
        for path in (broker_file, worker_file):
            path.unlink(missing_ok=True)
        assert not errors, errors


async def _restored_archive_api(test_engine, stack, admin_id, month):
    """Use the same installed API image and endpoint, with the ordinary role."""
    database = stack["proof"]["database"]
    api_url = cold._role_url(
        "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL",
        "identity_api_consumer",
        test_engine,
        internal_network=True,
    ).set(database=database)
    auth_url = cold._role_url(
        "AAP_MODULE_DATA_IDENTITY_AUTH_URL",
        "yoao_identity_auth_client",
        test_engine,
        internal_network=True,
    ).set(database=database)
    access, secret = _credentials("READ")
    assert (access, secret) != _credentials("PLATFORM")
    minio = stack["minio"]
    env = {
        **stack["broker_env"],
        **stack["buckets"],
        "DATABASE_URL": api_url.render_as_string(hide_password=False),
        "IDENTITY_AUTH_DATABASE_URL": auth_url.render_as_string(hide_password=False),
        "MINIO_ENDPOINT": minio["endpoint"],
        "MINIO_USE_SSL": "true" if minio["ssl"] else "false",
        "MINIO_ACCESS_KEY": access,
        "MINIO_SECRET_KEY": secret,
    }
    stack["redacted"].extend(
        (
            access,
            secret,
            api_url.password,
            auth_url.password,
            env["DATABASE_URL"],
            env["IDENTITY_AUTH_DATABASE_URL"],
        )
    )
    env_file = stack["broker_file"].with_name("archive-api.env")
    program = """
import asyncio,json,sys
import httpx
from sqlalchemy import text
from app.core.security import create_access_token
from app.db.base import async_session
from app.main import app
async def main():
 database,admin,year,month=sys.argv[1:]
 async with async_session() as db:
  assert (await db.execute(text("SELECT current_user,current_database()"))).one()==("identity_api_consumer",database)
 headers={"Authorization":"Bearer "+create_access_token(subject=admin,role="super_admin")}
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
  url="/api/v1/audit-logs/archives"
  listing=await client.get(url,headers=headers)
  rows=await client.get(f"{url}/{year}/{month}",headers=headers)
  assert listing.status_code==200 and rows.status_code==200,(listing.text,rows.text)
  print(json.dumps({"listing":listing.json(),"rows":rows.json()}))
asyncio.run(main())
"""
    _write_environment(env_file, env)
    try:
        container = _docker(
            "create",
            *stack["common"],
            "--env-file",
            str(env_file),
            "--entrypoint=python",
            stack["image_id"],
            "-c",
            program,
            database,
            str(admin_id),
            str(month.year),
            str(month.month),
        )
        stack["containers"].append(container)
        output = await asyncio.to_thread(_docker, "start", "--attach", container)
        assert _inspect_docker(container)["State"]["ExitCode"] == 0
        return json.loads(output.splitlines()[-1])
    finally:
        env_file.unlink(missing_ok=True)


async def _dispatch(stack, *names):
    """Evaluate real Beat ticks, shortening only the selected schedules in this fixture."""
    task_ids = {name: str(uuid.uuid4()) for name in names}
    program = """
import json,time
from datetime import timedelta
from celery.beat import Scheduler
from app.workers.celery_app import celery_app
report={}
selected={}
for name,task_id in TASK_IDS.items():
 entry=celery_app.conf.beat_schedule[name]
 assert entry['task']==ENTRIES[name]
 assert not entry.get('args') and not entry.get('kwargs')
 route=celery_app.amqp.router.route({},entry['task'])
 assert route['queue'].name=='maintenance'
 report[name]={'task_id':task_id,'task':entry['task'],'queue':route['queue'].name,'original_schedule':str(entry['schedule'])}
 selected[name]={**entry,'schedule':60.0,'last_run_at':celery_app.now()-timedelta(seconds=61),'options':{**entry.get('options',{}),'task_id':task_id}}
celery_app.conf.beat_schedule=selected
scheduler=Scheduler(celery_app,max_interval=.1)
try:
 assert set(scheduler.schedule)==set(selected)
 deadline=time.monotonic()+15
 while not all(scheduler.schedule[name].total_run_count==1 for name in selected):
  assert time.monotonic()<deadline
  time.sleep(max(0,min(scheduler.tick(),.1)))
 print(json.dumps(report))
finally:scheduler.close()
""".replace("TASK_IDS", repr(task_ids)).replace("ENTRIES", repr(ENTRIES))
    container = _docker(
        "create",
        *stack["common"],
        "--env-file",
        str(stack["broker_file"]),
        "--entrypoint=python",
        stack["image_id"],
        "-c",
        program,
    )
    stack["containers"].append(container)
    details = _inspect_docker(container)
    env = cold._container_environment(details)
    assert not any(
        k in env for k in ["DATABASE_URL", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"]
    )
    assert not details["Mounts"] or all(m["Type"] == "tmpfs" for m in details["Mounts"])
    output = await asyncio.to_thread(_docker, "start", "--attach", container)
    assert _inspect_docker(container)["State"]["ExitCode"] == 0
    report = json.loads(output.splitlines()[-1])
    assert {k: v["task_id"] for k, v in report.items()} == task_ids
    stack["dispatches"].append(report)
    return task_ids


async def _result(sender, task_id, deliveries):
    delivery = sender.AsyncResult(task_id)
    deliveries.append(delivery)
    return await asyncio.to_thread(delivery.get, timeout=25)


async def _verify_daily(engine, action, yesterday):
    async with engine.connect() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT day,event_count FROM yoao_audit_v1.audit_daily_facts WHERE action=:action"
                ),
                {"action": action},
            )
        ).all()
        assert rows == [(yesterday.date(), 1)]


async def _verify_archive_job(engine, task_id, notification_count, *, result=None):
    async with engine.connect() as db:
        row = (
            await db.execute(
                text(
                    "SELECT id,status,user_id,project_id,payload,result,started_at,"
                    "completed_at,progress_pct FROM public.async_jobs "
                    "WHERE celery_task_id=:id AND kind='audit_archive'"
                ),
                {"id": task_id},
            )
        ).one()
        assert row.user_id is None and row.project_id is None
        assert row.payload == {"retain_months": 12} and row.started_at is not None
        if result is None:
            assert row.status == "running" and row.completed_at is None
            assert row.progress_pct == 0 and row.result == {}
        else:
            assert row.status == "completed" and row.completed_at is not None
            assert row.progress_pct == 100
            assert result == {**row.result, "job_id": str(row.id), "status": row.status}
        assert (
            await db.scalar(text("SELECT count(*) FROM public.notifications"))
            == notification_count
        )
        assert (
            await db.scalar(
                text("SELECT count(*) FROM public.notifications WHERE target_id=:id"),
                {"id": row.id},
            )
            == 0
        )
        return row.id


async def _verify_runtime_denials(test_engine, database):
    url = cold._role_url(
        "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL",
        "identity_api_consumer",
        test_engine,
    ).set(database=database)
    engine = create_async_engine(url, hide_parameters=True)
    try:
        async with engine.connect() as db:
            assert (
                await db.execute(text("SELECT current_user,current_database()"))
            ).one() == ("identity_api_consumer", database)
            privileges = (
                await db.execute(
                    text(
                        "SELECT has_any_column_privilege('public.audit_logs','SELECT,INSERT'),"
                        "has_table_privilege('public.async_jobs','INSERT'),"
                        "has_table_privilege('public.notifications','SELECT'),"
                        "has_any_column_privilege('legacy_runtime','public.audit_logs','SELECT')"
                    )
                )
            ).one()
            assert privileges == (False, False, False, False)
            with pytest.raises(DBAPIError) as denied:
                await db.execute(
                    text(
                        "ALTER TABLE public.audit_logs "
                        "ADD COLUMN audit_consumer_forbidden integer"
                    )
                )
            assert denied.value.orig.sqlstate == "42501"
    finally:
        await engine.dispose()


async def _archive_outputs(db, partition):
    table = AuditArchiveOutput.__table__
    result = await db.execute(select(table).where(table.c.source_table == partition))
    return result.mappings().all()


async def _archive_events(db, partition):
    return (
        await db.scalars(
            text(
                "SELECT detail_json FROM yoao_audit_v1.audit_events "
                "WHERE action='audit.archive' AND target_id=:name"
            ),
            {"name": partition},
        )
    ).all()


def _stored_archive(reader, bucket, receipt):
    ref = receipt["storage_ref"]
    assert ref["bucket"] == bucket
    expected_key = f"{receipt['year']}/{receipt['month']:02d}/{receipt['id']}.jsonl.gz"
    assert ref["object_key"] == expected_key
    body = reader.get_object(Bucket=bucket, Key=expected_key)["Body"]
    try:
        compressed = body.read()
    finally:
        body.close()
    assert len(compressed) == receipt["size_bytes"]
    assert hashlib.sha256(compressed).hexdigest() == receipt["sha256"]
    return compressed


async def test_installed_beat_maintenance_consumers_recover_archive_after_restore(
    test_engine, tmp_path, record_property
):
    _verify_postgres(test_engine.url)
    await _verify_runtime_denials(test_engine, test_engine.url.database)
    old_action, daily_action = ["audit.cold." + uuid.uuid4().hex for _ in range(2)]
    month = date(2001, 2, 1)
    partition = partition_name(month)
    detail = [None, True, 9007199254740993, "文本"]
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        await AuditMaintenancePostgresData(db).ensure_partition(month)
        await db.execute(
            text(
                "INSERT INTO public.audit_logs(id,action,created_at,detail_json) VALUES (9223372036854775807,:action,'2001-02-02T03:04:05Z',CAST(:detail AS jsonb))"
            ),
            {
                "action": old_action,
                "detail": json.dumps(detail),
            },
        )
        notification_count = await db.scalar(
            text("SELECT count(*) FROM public.notifications")
        )
    resources = _verified_resource_manifest(test_engine, test_engine.url)
    bucket = resources["buckets"]["MINIO_AUDIT_ARCHIVE_BUCKET"]
    writer = _local_s3_client(settings, *_credentials("PLATFORM"))
    reader = _local_s3_client(settings, *_credentials("READ"))
    sender = Celery(
        "audit-maintenance-consumers",
        broker=os.environ["REDIS_URL"],
        backend=os.environ["REDIS_URL"],
    )
    sender.conf.update(task_serializer="json", accept_content=["json"])
    deliveries, proofs, dispatches = [], [], []
    trigger = "audit_maintenance_pause_" + uuid.uuid4().hex[:12]
    lock = uuid.uuid4().int % (1 << 62)
    trigger_created = False
    try:
        for client in (writer, reader):
            _expect_s3_denied(
                lambda: client.list_objects_v2(
                    Bucket=resources["buckets"]["MINIO_DATASETS_BUCKET"]
                )
            )
        async with _worker(
            test_engine, tmp_path / "original", sender, record_property
        ) as stack:
            proofs.append(stack["proof"])
            now = datetime.now(timezone.utc)
            yesterday = now.replace(
                hour=0, minute=10, second=0, microsecond=0
            ) - timedelta(days=1)
            retained_month = date(now.year - 1, now.month, 1)
            retained_partition = partition_name(retained_month)
            future_months = [now.date().replace(day=1)]
            for _ in range(3):
                future_months.append(next_month(future_months[-1]))
            async with maker.begin() as db:
                data = AuditMaintenancePostgresData(db)
                await data.ensure_partition(retained_month)
                if yesterday.month != now.month:
                    await data.ensure_partition(yesterday.date().replace(day=1))
                for event_id, created in [
                    (9007199254740993, yesterday),
                    (9007199254740994, now),
                ]:
                    await db.execute(
                        text(
                            "INSERT INTO public.audit_logs(id,action,created_at) "
                            "VALUES (:id,:action,:created)"
                        ),
                        {"id": event_id, "action": daily_action, "created": created},
                    )
                existing = {source.name for source in await catalog(db)}
            expected_created = [
                name
                for month_start in future_months
                if (name := partition_name(month_start)) not in existing
            ]
            ids = await _dispatch(
                stack, "ensure-future-audit-partitions", "refresh-audit-bi-mv"
            )
            ensured = await _result(
                sender, ids["ensure-future-audit-partitions"], deliveries
            )
            assert ensured == {"created": expected_created}
            assert await _result(sender, ids["refresh-audit-bi-mv"], deliveries) == {
                "refreshed": True
            }
            await _verify_daily(test_engine, daily_action, yesterday)
            async with maker() as db:
                bounds = {
                    source.month_start: source.month_end for source in await catalog(db)
                }
                assert all(
                    bounds.get(start) == next_month(start) for start in future_months
                )
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        f"""
                        CREATE FUNCTION public.{trigger}() RETURNS event_trigger
                        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
                        AS $pause$ BEGIN
                            IF EXISTS (
                                SELECT FROM pg_event_trigger_dropped_objects()
                                WHERE object_type='table' AND schema_name='public'
                                  AND object_name='{partition}'
                            ) THEN
                                PERFORM set_config('application_name', '{trigger}', true);
                                WHILE NOT pg_try_advisory_xact_lock({lock}) LOOP
                                    PERFORM pg_sleep(0.05);
                                END LOOP;
                            END IF;
                        END $pause$
                        """
                    )
                )
                await db.execute(
                    text(f"REVOKE ALL ON FUNCTION public.{trigger}() FROM PUBLIC")
                )
                await db.execute(
                    text(
                        f"CREATE EVENT TRIGGER {trigger} ON sql_drop EXECUTE FUNCTION public.{trigger}()"
                    )
                )
            trigger_created = True
            async with test_engine.connect() as control:
                await control.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock}
                )
                lost_id = (await _dispatch(stack, "archive-old-audit-partitions"))[
                    "archive-old-audit-partitions"
                ]
                loss = sender.AsyncResult(lost_id)
                deliveries.append(loss)
                try:
                    async with asyncio.timeout(25):
                        while True:
                            await control.execute(
                                text("SELECT pg_stat_clear_snapshot()")
                            )
                            if await control.scalar(
                                text(
                                    "SELECT EXISTS (SELECT FROM pg_stat_activity "
                                    "WHERE datname=current_database() AND usename=:role "
                                    "AND application_name=:mark AND wait_event='PgSleep')"
                                ),
                                {"role": stack["proof"]["role"], "mark": trigger},
                            ):
                                break
                            await asyncio.sleep(0.1)
                except TimeoutError:
                    if await asyncio.to_thread(loss.ready):
                        await asyncio.to_thread(loss.get)
                    raise
                (pending,) = await _archive_outputs(control, partition)
                assert pending["state"] == "pending" and pending["published_at"] is None
                pending_bytes = _stored_archive(reader, bucket, pending)
                assert await asyncio.to_thread(
                    _broker_state, sender, "maintenance"
                ) == {"queue_size": 0, "unacknowledged": 0}
                active = await asyncio.to_thread(
                    sender.control.inspect(
                        destination=[stack["node_name"]], timeout=2
                    ).active
                )
                tasks = [r for r in active[stack["node_name"]] if r["id"] == lost_id]
                assert len(tasks) == 1
                program = """
import os,sys,time
from pathlib import Path
pid=int(sys.argv[1]);assert pid>1
status=Path('/proc/'+str(pid)+'/status')
assert next(l.split()[1] for l in status.read_text().splitlines() if l.startswith('PPid:'))=='1'
os.kill(pid,9)
deadline=time.monotonic()+5
while True:
 try:content=status.read_text()
 except FileNotFoundError:break
 if any(l.startswith('State:') and l.split()[1]=='Z' for l in content.splitlines()):break
 assert time.monotonic()<deadline
 time.sleep(.01)
"""
                _docker(
                    "exec",
                    stack["worker"],
                    "python",
                    "-c",
                    program,
                    str(tasks[0]["worker_pid"]),
                )
                await control.rollback()
            async with test_engine.begin() as db:
                await db.execute(text(f"DROP EVENT TRIGGER {trigger}"))
                await db.execute(text(f"DROP FUNCTION public.{trigger}()"))
            trigger_created = False
            positive = await _dispatch(stack, "ensure-future-audit-partitions")
            assert await _result(
                sender, positive["ensure-future-audit-partitions"], deliveries
            ) == {"created": []}
            async with test_engine.connect() as db:
                assert await db.scalar(
                    text("SELECT to_regclass(:name)"), {"name": "public." + partition}
                )
                assert not await _archive_events(db, partition)
                assert await _archive_outputs(db, partition) == [pending]
            lost_job = await _verify_archive_job(
                test_engine, lost_id, notification_count
            )
            assert lost_job == pending["job_id"]
            assert await asyncio.to_thread(_broker_state, sender, "maintenance") == {
                "queue_size": 0,
                "unacknowledged": 0,
            }
            dispatches.extend(stack["dispatches"])
        pending_key = pending["storage_ref"]["object_key"]
        _expect_s3_denied(lambda: writer.delete_object(Bucket=bucket, Key=pending_key))
        _expect_s3_denied(
            lambda: reader.put_object(Bucket=bucket, Key=pending_key, Body=b"forbidden")
        )
        dump = tmp_path / "audit-maintenance-consumers.dump"
        dump.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", "--dbname", test_engine.url.database],
            )
        )
        dump.chmod(0o600)
        async with _restored_database(test_engine, dump) as clone:
            engine = create_async_engine(
                test_engine.url.set(database=clone), hide_parameters=True
            )
            try:
                await _verify_runtime_denials(test_engine, clone)
                async with engine.connect() as db:
                    assert await db.scalar(text("SELECT current_database()")) == clone
                    assert await _archive_outputs(db, partition) == [pending]
                    restored_source_oid = await db.scalar(
                        text("SELECT to_regclass(:name)::oid::bigint"),
                        {"name": "public." + partition},
                    )
                    assert restored_source_oid
                    assert restored_source_oid != pending["source_oid"]
                assert (
                    await _verify_archive_job(engine, lost_id, notification_count)
                    == lost_job
                )
                async with _worker(
                    test_engine,
                    tmp_path / "restored",
                    sender,
                    record_property,
                    database=clone,
                ) as stack:
                    proofs.append(stack["proof"])
                    ids = await _dispatch(stack, *ENTRIES)
                    assert await _result(
                        sender, ids["ensure-future-audit-partitions"], deliveries
                    ) == {"created": []}
                    assert await _result(
                        sender, ids["refresh-audit-bi-mv"], deliveries
                    ) == {"refreshed": True}
                    result = await _result(
                        sender, ids["archive-old-audit-partitions"], deliveries
                    )
                    assert result["archived_partitions"] == [partition]
                    assert result["total_rows"] == 1
                    restored_job = await _verify_archive_job(
                        engine,
                        ids["archive-old-audit-partitions"],
                        notification_count,
                        result=result,
                    )
                    await _verify_daily(engine, daily_action, yesterday)
                    async with engine.connect() as db:
                        assert (
                            await db.scalar(
                                text("SELECT to_regclass(:name)"),
                                {"name": "public." + partition},
                            )
                            is None
                        )
                        assert await db.scalar(
                            text("SELECT to_regclass(:name)"),
                            {"name": "public." + retained_partition},
                        )
                        outputs = await _archive_outputs(db, partition)
                        assert len(outputs) == 2 and pending in outputs
                        ready = next(row for row in outputs if row["state"] == "ready")
                        assert ready["published_at"] is not None
                        assert ready["source_oid"] == restored_source_oid
                        assert ready["job_id"] == restored_job != lost_job
                        object_key = ready["storage_ref"]["object_key"]
                        assert object_key != pending_key
                        assert result["archive_keys"] == [object_key]
                        assert result["output_ids"] == [str(ready["id"])]
                        events = await _archive_events(db, partition)
                        assert events == [
                            {
                                "partition": partition,
                                "archive_output_id": str(ready["id"]),
                                "archive_key": object_key,
                                "row_count": 1,
                                "source_sha256": ready["source_sha256"],
                                "from": month.isoformat(),
                            }
                        ]
                    ids = await _dispatch(stack, "archive-old-audit-partitions")
                    repeat = await _result(
                        sender, ids["archive-old-audit-partitions"], deliveries
                    )
                    assert repeat["archived_partitions"] == []
                    assert repeat["archive_keys"] == []
                    assert repeat["output_ids"] == []
                    assert repeat["total_rows"] == 0
                    await _verify_archive_job(
                        engine,
                        ids["archive-old-audit-partitions"],
                        notification_count,
                        result=repeat,
                    )
                    async with engine.connect() as db:
                        assert await _archive_events(db, partition) == events
                        outputs = await _archive_outputs(db, partition)
                        assert len(outputs) == 2
                        assert pending in outputs and ready in outputs
                    assert _stored_archive(reader, bucket, pending) == pending_bytes
                    source, trailer = gzip.decompress(
                        _stored_archive(reader, bucket, ready)
                    ).splitlines(keepends=True)
                    assert hashlib.sha256(source).hexdigest() == ready["source_sha256"]
                    assert len(source) == ready["source_size_bytes"]
                    row, metadata = json.loads(source), json.loads(trailer)
                    assert row["action"] == old_action
                    assert row["id"] == 9223372036854775807
                    assert row["detail_json"] == detail
                    assert metadata["_partition_meta"]["archive_id"] == str(ready["id"])
                    async with async_sessionmaker(engine).begin() as db:
                        admin, _ = await _create_user(
                            db,
                            "super_admin",
                            "audit-archive-reader@test.local",
                            "Audit Reader",
                        )
                        admin_id = admin.id
                    api = await _restored_archive_api(
                        test_engine, stack, admin_id, month
                    )
                    listed = [
                        item
                        for item in api["listing"]
                        if (item["year"], item["month"]) == (month.year, month.month)
                    ]
                    assert listed == [
                        {
                            "year": month.year,
                            "month": month.month,
                            "object_key": object_key,
                            "size_bytes": ready["size_bytes"],
                            "archived_at": ready["published_at"].isoformat(),
                        }
                    ]
                    assert api["rows"]["items"] == [row, metadata]
                    assert api["rows"]["total"] == 2
                    dispatches.extend(stack["dispatches"])
            finally:
                await engine.dispose()
        record_property(
            "audit_maintenance_consumers",
            json.dumps(
                {
                    "image_id": stack["image_id"],
                    "role_probes": proofs,
                    "beat_dispatches": dispatches,
                    "real_short_cadence_beat_ticks": True,
                    "original_task_routes_and_signatures": True,
                    "completed_utc_day_only": True,
                    "prefork_sigkill_after_put_and_db_rollback": True,
                    "child_exit_observed_before_unblocking": True,
                    "durable_running_job_and_pending_output_survived_loss": True,
                    "whole_database_restore_then_explicit_redrive": True,
                    "jobs_completed_and_system_no_recipient_preserved": True,
                    "repeat_archive_no_duplicate_partition_event": True,
                    "distinct_ready_key_and_unmodified_pending_bytes": True,
                    "restored_api_reads_only_published_output": True,
                    "retention_cutoff_month_stayed_online": True,
                    "scoped_archive_writer_and_reader": True,
                    "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
                    "worker_uses_configured_platform_maintenance_role": True,
                    "default_maintenance_cutover": False,
                }
            ),
        )
    finally:
        try:
            if trigger_created:
                async with test_engine.begin() as db:
                    await db.execute(text(f"DROP EVENT TRIGGER {trigger}"))
                    await db.execute(text(f"DROP FUNCTION public.{trigger}()"))
        finally:
            try:
                for delivery in deliveries:
                    await asyncio.to_thread(delivery.forget)
            finally:
                sender.close()
                writer.close()
                reader.close()
