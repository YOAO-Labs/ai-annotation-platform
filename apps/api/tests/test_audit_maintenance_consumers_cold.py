"""Installed Beat scheduler and maintenance tasks on verifier-owned resources."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
import secrets
import uuid

from celery import Celery
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_audit.maintenance_postgres import AuditMaintenancePostgresData
from app.config import settings
from tests import test_labeling_module_api_cold as cold
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
        assert hashlib.sha256((cold.ROOT / name).read_bytes()).hexdigest() == digest
    host_url = cold._role_url(
        "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL",
        "identity_api_consumer",
        test_engine,
    )
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
    key, secret = _credentials("WRITE")
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
import asyncio,importlib,json,os,sysconfig
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.exc import DBAPIError
from app.workers.audit_partition import ensure_future_audit_partitions,archive_old_audit_partitions
from app.workers.cleanup import refresh_audit_bi_mv
async def main():
 engine=create_async_engine(os.environ['DATABASE_URL'],hide_parameters=True)
 try:
  async with engine.connect() as db:
   row=(await db.execute(text("SELECT current_user,current_database(),has_table_privilege(current_user,'public.audit_logs','SELECT'),has_table_privilege(current_user,'public.audit_logs','INSERT'),has_table_privilege(current_user,'public.async_jobs','SELECT'),has_table_privilege(current_user,'public.async_jobs','INSERT'),has_table_privilege(current_user,'public.notifications','SELECT'),has_function_privilege(current_user,'aap_audit_v1.maintenance_command(jsonb)','EXECUTE')"))).one()
   try:
    await db.execute(text('ALTER TABLE public.audit_logs ADD COLUMN audit_consumer_forbidden integer'))
    raise AssertionError('worker can alter Audit storage')
   except DBAPIError as error:assert error.orig.sqlstate=='42501'
   finally:await db.rollback()
  installed=Path(importlib.import_module('aap_audit.maintenance_postgres').__file__).resolve().is_relative_to(Path(sysconfig.get_paths()['purelib']))
  tasks=[ensure_future_audit_partitions,archive_old_audit_partitions,refresh_audit_bi_mv]
  assert not any(t.acks_late for t in tasks)
  print(json.dumps(dict(role=row[0],database=row[1],audit_select=row[2],audit_insert=row[3],jobs_select=row[4],jobs_insert=row[5],notifications_select=row[6],maintenance_command=row[7],audit_alter_denied=True,installed=installed,early_ack=True)))
 finally:await engine.dispose()
asyncio.run(main())
"""
        proof = json.loads(_docker("exec", worker, "python", "-c", probe))
        assert (
            proof["role"] == "identity_api_consumer" and proof["database"] == database
        )
        assert proof["installed"] and proof["maintenance_command"]
        assert not any(
            proof[k]
            for k in [
                "audit_select",
                "audit_insert",
                "jobs_select",
                "jobs_insert",
                "notifications_select",
            ]
        )
        yield {
            "worker": worker,
            "node_name": node_name,
            "image_id": image_id,
            "common": common,
            "containers": containers,
            "broker_file": broker_file,
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
                    "SELECT day,event_count FROM aap_audit_v1.audit_daily_facts WHERE action=:action"
                ),
                {"action": action},
            )
        ).all()
        assert rows == [(yesterday.date(), 1)]


async def _verify_archive_job(engine, task_id, result):
    async with engine.connect() as db:
        row = (
            await db.execute(
                text(
                    "SELECT status,user_id,project_id,payload,result FROM public.async_jobs WHERE celery_task_id=:id AND kind='audit_archive'"
                ),
                {"id": task_id},
            )
        ).one()
        assert (
            row.status == "completed" and row.user_id is None and row.project_id is None
        )
        assert row.payload == {"retain_months": 12} and row.result == result
        assert await db.scalar(text("SELECT count(*) FROM public.notifications")) == 0


async def test_installed_beat_maintenance_consumers_recover_archive_after_restore(
    test_engine, tmp_path, record_property
):
    _verify_postgres(test_engine.url)
    now = datetime.now(timezone.utc)
    yesterday = now.replace(hour=0, minute=10, second=0, microsecond=0) - timedelta(
        days=1
    )
    old_action, daily_action = ["audit.cold." + uuid.uuid4().hex for _ in range(2)]
    partition = "audit_logs_y2001m02"
    object_key = "2001/02.jsonl.gz"
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        await AuditMaintenancePostgresData(db).ensure_partition(date(2001, 2, 1))
        await db.execute(
            text(
                "INSERT INTO public.audit_logs(id,action,created_at,detail_json) VALUES (9223372036854775807,:action,'2001-02-02T03:04:05Z',CAST(:detail AS jsonb))"
            ),
            {
                "action": old_action,
                "detail": json.dumps([None, True, 9007199254740993, "文本"]),
            },
        )
        for event_id, created in [
            (9007199254740993, yesterday),
            (9007199254740994, now),
        ]:
            await db.execute(
                text(
                    "INSERT INTO public.audit_logs(id,action,created_at) VALUES (:id,:action,:created)"
                ),
                {"id": event_id, "action": daily_action, "created": created},
            )
        # Narrow this fixture only; held Learning/Training reads keep their normal grants.
        await db.execute(
            text(
                "REVOKE SELECT ON public.audit_logs,public.async_jobs FROM identity_api_consumer"
            )
        )
        assert await db.scalar(
            text(
                "SELECT has_table_privilege('legacy_runtime','public.audit_logs','SELECT')"
            )
        )
        assert await db.scalar(text("SELECT count(*) FROM public.notifications")) == 0
    resources = _verified_resource_manifest(test_engine, test_engine.url)
    bucket = resources["buckets"]["MINIO_AUDIT_ARCHIVE_BUCKET"]
    writer = _local_s3_client(settings, *_credentials("WRITE"))
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
            ids = await _dispatch(
                stack, "ensure-future-audit-partitions", "refresh-audit-bi-mv"
            )
            ensured = await _result(
                sender, ids["ensure-future-audit-partitions"], deliveries
            )
            assert isinstance(ensured["created"], list)
            assert await _result(sender, ids["refresh-audit-bi-mv"], deliveries) == {
                "refreshed": True
            }
            await _verify_daily(test_engine, daily_action, yesterday)
            async with test_engine.connect() as db:
                current = now.date().replace(day=1)
                for _ in range(4):
                    name = f"audit_logs_y{current.year}m{current.month:02d}"
                    assert (
                        await db.scalar(
                            text(
                                "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=to_regclass(:name)"
                            ),
                            {"name": "public." + name},
                        )
                        == "audit_code"
                    )
                    current = (current.replace(day=28) + timedelta(days=4)).replace(
                        day=1
                    )
            async with test_engine.begin() as db:
                await db.execute(
                    text(
                        f"CREATE FUNCTION public.{trigger}() RETURNS event_trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $pause$ BEGIN IF EXISTS (SELECT FROM pg_event_trigger_dropped_objects() WHERE object_type='table' AND schema_name='public' AND object_name='{partition}') THEN PERFORM pg_advisory_xact_lock({lock}); END IF; END $pause$"
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
                async with asyncio.timeout(25):
                    while True:
                        await control.execute(text("SELECT pg_stat_clear_snapshot()"))
                        if await control.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid WHERE l.locktype='advisory' AND NOT l.granted AND l.classid=:high AND l.objid=:low AND a.usename='identity_api_consumer'"
                            ),
                            {"high": lock >> 32, "low": lock & 0xFFFFFFFF},
                        ):
                            break
                        await asyncio.sleep(0.1)
                assert (
                    reader.head_object(Bucket=bucket, Key=object_key)["ContentLength"]
                    > 0
                )
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
                program = "import os,sys;from pathlib import Path;pid=int(sys.argv[1]);assert pid>1;parent=next(l.split()[1] for l in Path('/proc/'+str(pid)+'/status').read_text().splitlines() if l.startswith('PPid:'));assert parent=='1';os.kill(pid,9)"
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
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM aap_audit_v1.audit_events WHERE action='audit.archive' AND target_id=:name"
                        ),
                        {"name": partition},
                    )
                    == 0
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM public.async_jobs WHERE celery_task_id=:id"
                        ),
                        {"id": lost_id},
                    )
                    == 0
                )
                assert (
                    await db.scalar(text("SELECT count(*) FROM public.notifications"))
                    == 0
                )
            assert await asyncio.to_thread(_broker_state, sender, "maintenance") == {
                "queue_size": 0,
                "unacknowledged": 0,
            }
            deliveries.append(sender.AsyncResult(lost_id))
            dispatches.extend(stack["dispatches"])
        _expect_s3_denied(lambda: writer.get_object(Bucket=bucket, Key=object_key))
        _expect_s3_denied(lambda: writer.delete_object(Bucket=bucket, Key=object_key))
        _expect_s3_denied(
            lambda: reader.put_object(Bucket=bucket, Key=object_key, Body=b"forbidden")
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
                async with _worker(
                    test_engine,
                    tmp_path / "restored",
                    sender,
                    record_property,
                    database=clone,
                ) as stack:
                    proofs.append(stack["proof"])
                    ids = await _dispatch(stack, *ENTRIES)
                    await _result(
                        sender, ids["ensure-future-audit-partitions"], deliveries
                    )
                    assert await _result(
                        sender, ids["refresh-audit-bi-mv"], deliveries
                    ) == {"refreshed": True}
                    result = await _result(
                        sender, ids["archive-old-audit-partitions"], deliveries
                    )
                    assert (
                        partition in result["archived_partitions"]
                        and object_key in result["archive_keys"]
                        and result["total_rows"] == 1
                    )
                    await _verify_archive_job(
                        engine, ids["archive-old-audit-partitions"], result
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
                        assert (
                            await db.scalar(
                                text(
                                    "SELECT count(*) FROM aap_audit_v1.audit_events WHERE action='audit.archive' AND target_id=:name"
                                ),
                                {"name": partition},
                            )
                            == 1
                        )
                        assert await db.scalar(
                            text(
                                "SELECT has_table_privilege('legacy_runtime','public.audit_logs','SELECT')"
                            )
                        )
                    ids = await _dispatch(stack, "archive-old-audit-partitions")
                    repeat = await _result(
                        sender, ids["archive-old-audit-partitions"], deliveries
                    )
                    assert repeat == {
                        "archived_partitions": [],
                        "archive_keys": [],
                        "total_rows": 0,
                    }
                    await _verify_archive_job(
                        engine, ids["archive-old-audit-partitions"], repeat
                    )
                    dispatches.extend(stack["dispatches"])
            finally:
                await engine.dispose()
        body = reader.get_object(Bucket=bucket, Key=object_key)["Body"]
        try:
            rows = [
                json.loads(line) for line in gzip.decompress(body.read()).splitlines()
            ]
        finally:
            body.close()
        row = next(r for r in rows if r["action"] == old_action)
        assert row["id"] == 9223372036854775807 and row["detail_json"] == [
            None,
            True,
            9007199254740993,
            "文本",
        ]
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
                    "uncommitted_job_rolled_back": True,
                    "whole_database_restore_then_explicit_redrive": True,
                    "jobs_completed_and_system_no_recipient_preserved": True,
                    "repeat_archive_no_duplicate_partition_event": True,
                    "scoped_archive_writer_and_reader": True,
                    "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
                    "migration_credentials_used": False,
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
