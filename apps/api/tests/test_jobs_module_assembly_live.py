"""Cold Jobs queue/API/worker acceptance against the verifier-owned stack."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import json
from io import BytesIO
import os
import secrets
import time
from urllib.parse import urlsplit
import uuid
import zipfile

from cryptography.fernet import Fernet
from celery import Celery
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.task import Task
from app.db.models.user import User
from tests.factory import DEFAULT_PASSWORD, create_batch
from tests.identity_module_assembly_helpers import IdentityModuleStack
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    create_guide_storage_scope,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_module_assembly_live import (
    _publisher_redis,
    _restored_database,
)
from tests.test_identity_recovery import _postgres_tool
from tests.identity_recovery_live_helpers import SMTPInbox
from tests.test_identity_sessions_live import _login_redis, _verify_redis
from tests.test_workspace_cross_frame_worker_live import (
    _TASK_NAME,
    _annotation_count,
    _job_state,
    _seed_member_job,
)
from tests.test_workspace_worker_assembly_live import (
    _broker_state,
    _wait_broker_ack,
    _wait_worker_ready,
)
from tests.test_workspace_export_worker_live import (
    _SOURCE_PNG,
    _create_export_storage_scope,
    _seed_export_world,
)
from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_LIVE_ACCEPTANCE") != "1"
        or os.getenv("AAP_JOBS_QUEUE_MODULE_LIVE") != "1"
        or not os.getenv("AAP_JOBS_QUEUE_RUNTIME_IMAGE")
        or not os.getenv("AAP_JOBS_QUEUE_RUNTIME_IDENTITY")
        or not os.getenv("AAP_JOBS_QUEUE_RUNTIME_SOURCE_REVISION")
        or not os.getenv("AAP_IDENTITY_API_RUNTIME_IMAGE")
        or not os.getenv("AAP_WORKSPACE_MODULE_API_IMAGE"),
        reason="requires the source-bound API, Jobs queue, and owned worker verifier",
    ),
]

_JOBS_QUEUE_IMAGE_INPUTS = (
    "AAP_JOBS_QUEUE_RUNTIME_IMAGE",
    "AAP_JOBS_QUEUE_RUNTIME_IDENTITY",
    "AAP_JOBS_QUEUE_RUNTIME_SOURCE_REVISION",
)
_CLONE_ROLE_ENVIRONMENT = (
    "TEST_DATABASE_URL",
    "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL",
    "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_CONTAINER_URL",
    "AAP_MODULE_DATA_PREANNOTATION_RESULT_URL",
)


@contextmanager
def _role_urls_for_database(database_name: str):
    """Point existing verifier role credentials at one restored database clone."""
    previous = {name: os.environ.get(name) for name in _CLONE_ROLE_ENVIRONMENT}
    try:
        for name, value in previous.items():
            if value:
                os.environ[name] = (
                    make_url(value)
                    .set(database=database_name)
                    .render_as_string(hide_password=False)
                )
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _assert_owned_broker_target(sender: Celery, queue: str = "default") -> None:
    broker = make_url(os.environ.get("CELERY_BROKER_URL") or os.environ["REDIS_URL"])
    owner = _verify_redis()
    assert (
        broker.drivername,
        broker.host,
        broker.port,
        broker.database,
    ) == (
        owner.drivername,
        owner.host,
        owner.port,
        owner.database,
    ), "Jobs queue must use the verified verifier-owned Redis database"
    assert queue in {"default", "export"}
    assert _broker_state(sender, queue) == {"queue_size": 0, "unacknowledged": 0}, (
        f"{queue} queue must be empty before the owned worker subscribes"
    )


async def _login(
    client: httpx.AsyncClient, maker, actor_id: uuid.UUID
) -> dict[str, str]:
    async with maker() as db:
        actor = await db.get(User, actor_id)
        assert actor is not None
        email = actor.email
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": DEFAULT_PASSWORD},
    )
    assert response.status_code == 200, response.text
    token = response.json().get("access_token")
    assert isinstance(token, str) and token
    return {"Authorization": "Bearer " + token}


def _cross_frame_body(world: dict) -> dict:
    return {
        "operation": "propagate",
        "scope": "selected",
        "annotation_ids": [str(world["source_annotation_id"])],
        "direction": "forward",
        "start_frame": 1,
        "end_frame": 1,
        "conflict_policy": "skip_existing",
    }


async def _latest_job(test_engine, *, actor_id: uuid.UUID, project_id: uuid.UUID):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        return await db.scalar(
            select(AsyncJob)
            .where(AsyncJob.user_id == actor_id)
            .where(AsyncJob.project_id == project_id)
            .where(AsyncJob.kind == "point_cloud_cross_frame")
            .order_by(AsyncJob.created_at.desc())
            .limit(1)
        )


async def _job_by_id(test_engine, job_id: uuid.UUID) -> AsyncJob | None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        return await db.get(AsyncJob, job_id)


async def _terminalize_seed_job(test_engine, job_id: uuid.UUID) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        job = await db.get(AsyncJob, job_id)
        assert job is not None
        job.status = AsyncJobStatus.COMPLETED.value
        job.completed_at = datetime.now(timezone.utc)


async def _wait_revoked(sender: Celery, worker: WorkspaceCeleryWorker, task_id: str):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        inspector = sender.control.inspect(timeout=1, destination=[worker.node_name])
        snapshot = await asyncio.to_thread(inspector.revoked)
        if snapshot and task_id in snapshot.get(worker.node_name, []):
            return {"task_id": task_id, "worker_answered": True}
        await asyncio.sleep(0.25)
    raise AssertionError("Jobs queue revoke did not reach the owned Celery worker")


async def _request_cross_frame(client, headers, world):
    return await client.post(
        f"/api/v1/tasks/{world['source_task_id']}/cross-frame-jobs",
        headers=headers,
        json=_cross_frame_body(world),
    )


_JOBS_API_ROW_ACCESS_PROBE = """\
import asyncio
import json
import re
import sys
import uuid

def _sqlstate(error):
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        value = getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
        if value:
            return value
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return None

def _count(statements, fragment):
    return sum(fragment in statement.lower() for statement in statements)

async def _run():
    sys.path.insert(0, "/app")
    from sqlalchemy import event, inspect, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from aap_jobs.domain_query_contract import JOBS_DOMAIN_QUERY_PROTOCOL
    from aap_jobs.fixture_cleanup_contract import JOBS_FIXTURE_CLEANUP_PROTOCOL
    from aap_jobs.fixture_filter_contract import JOBS_FILTER_FIXTURE_PROTOCOL
    from aap_jobs.lifecycle_contract import JOBS_LIFECYCLE_PROTOCOL
    from app.services.jobs_domain_query_data import JobsDomainQueryPostgresData
    from aap_jobs.row_lock_contract import JOBS_ROW_LOCK_PROTOCOL
    from app.config import settings
    from app.services.jobs_lifecycle_data import job_lock_statement
    from app.services import async_job as async_job_svc
    from app.api.v1._test_seed_filters import _set_filter_fixture_job_outcomes

    job_id = uuid.UUID(sys.argv[1])
    expected_database = sys.argv[2]
    engine = create_async_engine(settings.database_url, hide_parameters=True)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            await db.begin()
            role, database = (
                await db.execute(text("SELECT current_user, current_database()"))
            ).one()
            if (
                role != "identity_api_consumer"
                or database != expected_database
                or not database.endswith("_test")
            ):
                raise RuntimeError("database_identity")

            row = await db.scalar(
                job_lock_statement(job_id, mode="share_nowait", populate_existing=True)
            )
            if row is None or row.status != "completed":
                raise RuntimeError("share_row")
            original_result = row.result
            original_progress = row.progress_pct
            identity = inspect(row).identity_key

            db.expire(row, ["updated_at"])
            read_calls_before = _count(statements, "aap_jobs_v1.read_job(")
            await db.refresh(row, attribute_names=["updated_at"])
            read_calls_after = _count(statements, "aap_jobs_v1.read_job(")
            partial_refresh = (
                db.identity_map.get(identity) is row
                and row.result == original_result
                and row.progress_pct == original_progress
                and "updated_at" not in inspect(row).expired_attributes
                and read_calls_after == read_calls_before + 1
            )
            if not partial_refresh:
                raise RuntimeError("partial_refresh")

            locked = await db.scalar(
                job_lock_statement(job_id, mode="update", populate_existing=True)
            )
            same_identity = locked is row and locked.result == original_result
            if not same_identity:
                raise RuntimeError("update_row")
            queries = JobsDomainQueryPostgresData(db)
            actor_id = row.user_id
            scene_id = row.payload["scene_id"]
            singleflight_key = row.payload["singleflight_key"]
            await queries.lock_cross_frame_singleflight(actor_id, singleflight_key)
            active = await queries.active_cross_frame_job(actor_id, singleflight_key)
            page = await queries.cross_frame_page(actor_id, scene_id, 50)
            total = await queries.cross_frame_count(actor_id, scene_id)
            latest = await queries.latest_batch_prediction_times([row.project_id])
            domain_subject_matches = (
                active is None
                and any(item is row for item in page)
                and total >= len(page)
                and not latest
            )
            if not domain_subject_matches:
                raise RuntimeError("domain_subject")
            privileges = (
                await db.execute(text(
                    "SELECT pg_catalog.has_table_privilege(current_user,"
                    "'public.async_jobs','INSERT'), "
                    "pg_catalog.has_any_column_privilege(current_user,"
                    "'public.async_jobs','INSERT'), "
                    "pg_catalog.has_table_privilege(current_user,"
                    "'aap_jobs_v1.async_job_creation','SELECT'), "
                    "pg_catalog.has_table_privilege(current_user,"
                    "'aap_jobs_v1.async_job_creation','INSERT')"
                ))
            ).one()
            if tuple(privileges) != (False, False, True, True):
                raise RuntimeError("creation_privileges")
            created = await async_job_svc.create_job(
                db, kind="audit_archive", user_id=actor_id, project_id=row.project_id,
                payload={"probe": "jobs-creation"},
            )
            created_id = created.id
            pending_shape = (
                inspect(created).persistent and created.status == "pending"
                and created.progress_pct == 0 and created.result == {}
                and created.started_at is None and created.completed_at is None
                and created.error_message is None
            )
            if not pending_shape:
                raise RuntimeError("creation_shape")
            try:
                async with db.begin_nested():
                    await db.execute(text(
                        "INSERT INTO public.async_jobs (id) "
                        "SELECT CAST(:id AS uuid) WHERE false"
                    ), {"id": created_id})
            except Exception as error:
                private_insert_sqlstate = _sqlstate(error)
                if private_insert_sqlstate != "42501":
                    raise
            else:
                raise RuntimeError("private_insert_allowed")
            update_privileges = (await db.execute(text(
                "SELECT pg_catalog.has_table_privilege(current_user,"
                "'public.async_jobs','UPDATE'), "
                "pg_catalog.has_any_column_privilege(current_user,"
                "'public.async_jobs','UPDATE')"
            ))).one()
            if tuple(update_privileges) != (False, False):
                raise RuntimeError("private_update_privileges")
            await async_job_svc.update_progress(
                db, job_id, 37, result={"probe": "owner-update"},
                extra_payload={"owner_update_probe": True},
            )
            await db.flush()
            lifecycle_updated = (
                row.progress_pct == 37 and row.result == {"probe": "owner-update"}
                and row.payload.get("owner_update_probe") is True
                and "updated_at" in inspect(row).expired_attributes
            )
            if not lifecycle_updated:
                raise RuntimeError("owner_update_shape")
            try:
                async with db.begin_nested():
                    await db.execute(text(
                        "UPDATE public.async_jobs SET status=status WHERE false"
                    ))
            except Exception as error:
                private_update_sqlstate = _sqlstate(error)
                if private_update_sqlstate != "42501":
                    raise
            else:
                raise RuntimeError("private_update_allowed")
            completed_fixture = await async_job_svc.create_job(
                db, kind="batch_predict", user_id=actor_id, project_id=row.project_id,
            )
            failed_fixture = await async_job_svc.create_job(
                db, kind="export", user_id=actor_id, project_id=row.project_id,
            )
            await _set_filter_fixture_job_outcomes(db, completed_fixture, failed_fixture)
            await db.flush()
            fixed_fixture_outcomes = (
                completed_fixture.status == "completed"
                and completed_fixture.progress_pct == 100
                and completed_fixture.result == {"success_count": 2}
                and failed_fixture.status == "failed"
                and failed_fixture.progress_pct == 40
                and failed_fixture.error_message == "fixture failure"
                and completed_fixture.started_at is None
                and completed_fixture.completed_at is None
                and failed_fixture.started_at is None
                and failed_fixture.completed_at is None
            )
            if not fixed_fixture_outcomes:
                raise RuntimeError("fixed_fixture_outcomes")
            private_delete = await db.scalar(text(
                "SELECT pg_catalog.has_table_privilege(current_user,"
                "'public.async_jobs','DELETE')"
            ))
            if private_delete:
                raise RuntimeError("private_delete_allowed")
            deleted = await db.scalar(text(
                "SELECT aap_jobs_v1.delete_fixture_project_jobs(CAST(:ids AS uuid[]))"
            ), {"ids": [row.project_id]})
            pending_after_delete = await db.scalar(text(
                "SELECT count(*) FROM aap_jobs_v1.async_job_creation "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created_id})
            if type(deleted) is not int or deleted < 2 or pending_after_delete != 0:
                raise RuntimeError("fixture_delete_scope")
            await db.rollback()
            rolled_back = await db.scalar(text(
                "SELECT count(*) FROM aap_jobs_v1.async_job_creation "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created_id})
            if rolled_back != 0:
                raise RuntimeError("creation_rollback")
            restored = (await db.execute(text(
                "SELECT id,status,result,progress_pct "
                "FROM aap_jobs_v1.read_job(CAST(:id AS uuid))"
            ), {"id": job_id})).one()
            if tuple(restored) != (job_id, "completed", original_result, original_progress):
                raise RuntimeError("fixture_delete_rollback")
            fixture_delete_acl = {
                "private_table_delete": private_delete,
                "owner_delete": True,
                "pending_row_removed": True,
                "completed_row_restored": True,
                "caller_rollback": True,
            }
            fixture_update_acl = {
                "private_table_update": update_privileges[0],
                "private_column_update": update_privileges[1],
                "private_update_sqlstate": private_update_sqlstate,
                "lifecycle_projection_and_expiry": lifecycle_updated,
                "fixed_fixture_outcomes": fixed_fixture_outcomes,
                "caller_rollback": True,
            }
            creation_acl = {
                "private_table_insert": privileges[0],
                "private_column_insert": privileges[1],
                "creation_view_select": privileges[2],
                "creation_view_insert": privileges[3],
                "pending_shape": pending_shape,
                "private_insert_sqlstate": private_insert_sqlstate,
                "caller_rollback": True,
            }

        lowered = [statement.lower() for statement in statements]
        function_calls = {
            "read_job": _count(lowered, "aap_jobs_v1.read_job("),
            "read_job_for_share_nowait": sum(
                "aap_jobs_v1.read_job_for_share_nowait(" in statement for statement in lowered
            ),
            "read_job_for_update": sum(
                "aap_jobs_v1.read_job_for_update(" in statement for statement in lowered
            ),
        }
        domain_function_calls = {
            name: _count(lowered, "aap_jobs_v1." + name + "(")
            for name in (
                "lock_cross_frame_singleflight",
                "active_cross_frame_job",
                "cross_frame_job_page",
                "cross_frame_job_count",
                "latest_batch_prediction_times",
            )
        }
        if any(count != 1 for count in domain_function_calls.values()):
            raise RuntimeError("domain_sql_surface")
        private_table_from = sum(
            bool(re.search(r"\\bfrom\\s+(?:public\\.)?async_jobs\\b", statement))
            for statement in lowered
        )
        if function_calls != {
            "read_job": 2,
            "read_job_for_share_nowait": 1,
            "read_job_for_update": 1,
        } or private_table_from:
            raise RuntimeError("sql_surface")
        return {
            "ok": True,
            "protocols": [
                JOBS_LIFECYCLE_PROTOCOL, JOBS_ROW_LOCK_PROTOCOL, JOBS_DOMAIN_QUERY_PROTOCOL,
                JOBS_FIXTURE_CLEANUP_PROTOCOL,
                JOBS_FILTER_FIXTURE_PROTOCOL,
            ],
            "role_match": True,
            "database_match": True,
            "function_calls": function_calls,
            "domain_function_calls": domain_function_calls,
            "domain_subject_matches": domain_subject_matches,
            "creation_acl": creation_acl,
            "fixture_delete_acl": fixture_delete_acl,
            "fixture_update_acl": fixture_update_acl,
            "private_table_from": private_table_from,
            "partial_refresh": partial_refresh,
            "same_orm_identity": same_identity,
        }
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
        await engine.dispose()

try:
    result = asyncio.run(_run())
except Exception as error:
    result = {"ok": False, "error": type(error).__name__, "sqlstate": _sqlstate(error)}
print(json.dumps(result, separators=(",", ":")))
"""


def _assert_api_row_access_probe(
    stack: IdentityModuleStack, *, job_id: uuid.UUID, database: str
) -> dict:
    try:
        containers = stack._verify_project_ownership()
    except Exception as error:
        message = (
            f"Jobs row-access ownership check failed: {type(error).__name__}: {error}"
        )
        raise AssertionError(stack.redact(message)) from None
    api_container = containers.get("identity-api")
    container_id = stack.service_ids.get("identity-api")
    if (
        api_container is None
        or not container_id
        or api_container.get("Id") != container_id
    ):
        raise AssertionError(
            "Jobs row-access probe did not resolve the owned API container"
        )

    try:
        output = stack._docker(
            [
                "exec",
                container_id,
                "/opt/venv/bin/python",
                "-I",
                "-c",
                _JOBS_API_ROW_ACCESS_PROBE,
                str(job_id),
                database,
            ],
            operation="Jobs row-access probe",
            timeout=60,
        )
    except Exception as error:
        message = (
            f"Jobs row-access container probe failed: {type(error).__name__}: {error}"
        )
        raise AssertionError(stack.redact(message)) from None
    safe_output = stack.redact(output)
    try:
        evidence = json.loads(safe_output)
    except json.JSONDecodeError:
        raise AssertionError("Jobs row-access probe returned invalid JSON") from None
    assert evidence.get("ok") is True, safe_output
    assert evidence["protocols"] == [
        "aap-jobs-lifecycle/v1",
        "aap-jobs-row-locks/v1",
        "aap-jobs-domain-queries/v1",
        "aap-jobs-fixture-cleanup/v1",
        "aap-jobs-filter-fixtures/v1",
    ]
    assert evidence["role_match"] is True
    assert evidence["database_match"] is True
    assert evidence["function_calls"] == {
        "read_job": 2,
        "read_job_for_share_nowait": 1,
        "read_job_for_update": 1,
    }
    assert evidence["domain_function_calls"] == {
        "lock_cross_frame_singleflight": 1,
        "active_cross_frame_job": 1,
        "cross_frame_job_page": 1,
        "cross_frame_job_count": 1,
        "latest_batch_prediction_times": 1,
    }
    assert evidence["domain_subject_matches"] is True
    assert evidence["creation_acl"] == {
        "private_table_insert": False,
        "private_column_insert": False,
        "creation_view_select": True,
        "creation_view_insert": True,
        "pending_shape": True,
        "private_insert_sqlstate": "42501",
        "caller_rollback": True,
    }
    assert evidence["fixture_delete_acl"] == {
        "private_table_delete": False,
        "owner_delete": True,
        "pending_row_removed": True,
        "completed_row_restored": True,
        "caller_rollback": True,
    }
    assert evidence["fixture_update_acl"] == {
        "private_table_update": False,
        "private_column_update": False,
        "private_update_sqlstate": "42501",
        "lifecycle_projection_and_expiry": True,
        "fixed_fixture_outcomes": True,
        "caller_rollback": True,
    }
    assert evidence["private_table_from"] == 0
    assert evidence["partial_refresh"] is True
    assert evidence["same_orm_identity"] is True
    return evidence


async def _assert_completed_job_http_read(
    client, headers, job_id, anchor_task_id, *, redact
):
    response = await client.get(f"/api/v1/async-jobs/{job_id}", headers=headers)
    assert response.status_code == 200, redact(response.text)
    result = response.json()
    assert result["id"] == str(job_id)
    assert result["status"] == AsyncJobStatus.COMPLETED.value
    assert result["result"]["created_annotation_count"] == 1
    history = await client.get(
        f"/api/v1/tasks/{anchor_task_id}/cross-frame-jobs?limit=50", headers=headers
    )
    assert history.status_code == 200, redact(history.text)
    page = history.json()
    assert page["total"] >= len(page["items"])
    assert any(item["id"] == str(job_id) for item in page["items"])
    return {
        "history_http_status": history.status_code,
        "history_contains_job": True,
        "http_status": response.status_code,
        "job_status": result["status"],
        "created_annotation_count": result["result"]["created_annotation_count"],
    }


async def _exercise_export_dispatch(
    test_engine,
    stack,
    sender,
    resources,
    owner_s3,
    directory,
    known_task_ids,
    *,
    check_outage,
):
    _verify_postgres(test_engine.url)
    _assert_owned_broker_target(sender, "export")
    world = await _seed_export_world(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        batch = await create_batch(db, project_id=world["project_id"])
        task = await db.get(Task, world["task_id"])
        task.batch_id = batch.id
        batch_id = batch.id
    scope, destroy_scope = _create_export_storage_scope(
        resources, owner_s3, settings, world
    )
    worker = None
    receipts = []
    outages = []
    try:
        owner_s3.put_object(
            Bucket=resources["buckets"]["MINIO_DATASETS_BUCKET"],
            Key=world["source_key"],
            Body=_SOURCE_PNG,
            ContentType="image/png",
        )
        project_path = f"/api/v1/projects/{world['project_id']}"
        requests = [
            ("project", project_path + "/export?targets=aap_json", None),
            (
                "batch",
                project_path + f"/batches/{batch_id}/export?targets=aap_json",
                None,
            ),
            (
                "selected",
                project_path + "/data-manager/tasks/export",
                {
                    "task_ids": [str(world["task_id"])],
                    "targets": ["aap_json"],
                },
            ),
        ]
        async with httpx.AsyncClient(
            base_url=stack.api_origin,
            trust_env=False,
            timeout=40,
        ) as client:
            headers = await _login(client, maker, world["owner_id"])
            if check_outage:
                await stack.stop_service("jobs-queue")
                for label, path, body in requests:
                    response = await client.post(path, headers=headers, json=body)
                    assert response.status_code == 500, stack.redact(response.text)
                    async with maker() as db:
                        failed = await db.scalar(
                            select(AsyncJob)
                            .where(
                                AsyncJob.kind == "export",
                                AsyncJob.project_id == world["project_id"],
                                AsyncJob.user_id == world["owner_id"],
                            )
                            .order_by(AsyncJob.created_at.desc())
                            .limit(1)
                        )
                        assert failed is not None and failed.celery_task_id is None
                        expected = "failed" if label == "selected" else "pending"
                        assert failed.status == expected
                    assert _broker_state(sender, "export") == {
                        "queue_size": 0,
                        "unacknowledged": 0,
                    }
                    outages.append(
                        {"route": label, "http_status": 500, "status": expected}
                    )
                await stack.start_service("jobs-queue")

            for label, path, body in requests:
                response = await client.post(path, headers=headers, json=body)
                assert response.status_code == 202, stack.redact(response.text)
                job_id = uuid.UUID(response.json()["job_id"])
                row = await _job_by_id(test_engine, job_id)
                assert row is not None and row.status == "pending"
                if label == "selected":
                    assert (
                        row.celery_task_id
                        and str(uuid.UUID(row.celery_task_id)) == row.celery_task_id
                    )
                    assert row.payload["task_ids"] == [str(world["task_id"])]
                else:
                    assert row.celery_task_id is None
                receipts.append({"route": label, "job_id": str(job_id)})

            assert _broker_state(sender, "export") == {
                "queue_size": 3,
                "unacknowledged": 0,
            }
            messages = {}
            with sender.connection_for_read() as connection:
                channel = connection.default_channel
                for priority in channel.priority_steps:
                    for raw in channel.client.lrange(
                        channel._q_for_pri("export", priority), 0, -1
                    ):
                        envelope = json.loads(raw)
                        assert (
                            envelope["headers"]["task"]
                            == "app.workers.export.run_export"
                        )
                        execution_id = envelope["headers"]["id"]
                        assert str(uuid.UUID(execution_id)) == execution_id
                        args, kwargs, _embed = json.loads(
                            base64.b64decode(envelope["body"], validate=True)
                        )
                        assert args == [] and kwargs["targets"] == ["aap_json"]
                        assert kwargs["project_id"] == str(world["project_id"])
                        messages[kwargs["async_job_id"]] = (execution_id, kwargs)
            assert set(messages) == {item["job_id"] for item in receipts}
            for item in receipts:
                execution_id, kwargs = messages[item["job_id"]]
                item["execution_id"] = execution_id
                known_task_ids.add(execution_id)
                assert kwargs["batch_id"] == (
                    str(batch_id) if item["route"] == "batch" else None
                )
                if item["route"] == "selected":
                    assert kwargs["task_ids"] == [str(world["task_id"])]
                    row = await _job_by_id(test_engine, uuid.UUID(item["job_id"]))
                    assert row.celery_task_id == execution_id
                else:
                    assert "task_ids" not in kwargs

            minio_origin = endpoint_url()
            worker = WorkspaceCeleryWorker(
                test_engine,
                directory,
                backend_token=secrets.token_urlsafe(32),
                object_store_endpoint=minio_origin,
                object_store_bucket=resources["buckets"]["MINIO_BUCKET"],
                object_store_use_ssl=urlsplit(minio_origin).scheme == "https",
                object_store_access_key=scope["access_key"],
                object_store_secret_key=scope["secret_key"],
                object_store_buckets=resources["buckets"],
                queue_name="export",
            )
            assert worker.object_store_access_key != settings.minio_access_key
            assert worker.object_store_secret_key != settings.minio_secret_key
            worker.start()
            await _wait_worker_ready(sender, worker)
            assert worker.task_signature("app.workers.export.run_export") == {
                "task": "app.workers.export.run_export",
                "parameters": [
                    "project_id",
                    "batch_id",
                    "targets",
                    "opts",
                    "async_job_id",
                    "task_ids",
                ],
            }
            for item in receipts:
                delivery = sender.AsyncResult(item["execution_id"])
                assert (
                    await asyncio.to_thread(delivery.get, timeout=120, propagate=False)
                    is None
                )
            for item in receipts:
                await _wait_broker_ack(sender, worker, item["execution_id"])
                response = await client.get(
                    f"/api/v1/async-jobs/{item['job_id']}",
                    headers=headers,
                )
                assert response.status_code == 200, stack.redact(response.text)
                job = response.json()
                assert job["status"] == "completed"
                assert job["celery_task_id"] == item["execution_id"]
                download = await client.get(job["result"]["download_url"])
                assert download.status_code == 200
                with zipfile.ZipFile(BytesIO(download.content)) as archive:
                    payload = json.loads(archive.read("annotations.json"))
                assert (
                    payload["exported_from"]["project_display_id"]
                    == world["project_display_id"]
                )
                assert len(payload["tasks"]) == 1
                task = payload["tasks"][0]
                assert task["task_match"]["display_id"] == world["task_display_id"]
                assert len(task["annotations"]) == 1
                assert task["annotations"][0]["class_name"] == "car"
                item["download_http_status"] = download.status_code
        return {"owner_id": str(world["owner_id"]), "jobs": receipts, "outage": outages}
    finally:
        try:
            if worker is not None:
                worker.stop()
        finally:
            destroy_scope(scope)


async def test_cold_jobs_queue_dispatch_revoke_and_restore(
    test_engine,
    tmp_path,
    record_property,
):
    """Use an owned default queue only after proving its Redis DB starts empty."""
    _verify_postgres(test_engine.url)
    assert str(test_engine.url.database).endswith("_test")
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = await _seed_member_job(maker, label="jobs-queue-cold", target_count=1)
    await _terminalize_seed_job(test_engine, world["job_id"])

    resources, guide_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    minio_origin = endpoint_url()
    minio_parts = urlsplit(minio_origin)
    assert minio_parts.scheme in {"http", "https"} and minio_parts.hostname
    guide_scope, destroy_guide_scope = create_guide_storage_scope(
        resources,
        owner_s3,
        guide_bucket,
        f"outside-guide/jobs-queue-{uuid.uuid4()}.txt",
    )

    secret, api_secret, control, queue_token = [
        secrets.token_urlsafe(32) for _ in range(4)
    ]
    assert len({secret, api_secret, control, queue_token}) == 4
    queue_image, queue_identity, queue_revision = (
        os.environ[name] for name in _JOBS_QUEUE_IMAGE_INPUTS
    )
    inbox = SMTPInbox()
    snapshot = tmp_path / "jobs-queue-module.dump"
    stack = None
    sender = None
    worker = None
    clone_worker = None
    clone_engine = None
    known_task_ids: set[str] = set()

    @asynccontextmanager
    async def restored_database_with_cleanup():
        async with _restored_database(test_engine, snapshot) as restored_database:
            try:
                yield restored_database
            finally:
                try:
                    if clone_worker is not None:
                        clone_worker.stop()
                finally:
                    try:
                        if stack is not None:
                            await stack.stop()
                    finally:
                        if clone_engine is not None:
                            await clone_engine.dispose()

    @asynccontextmanager
    async def redis_users_with_stack_cleanup():
        async with _login_redis() as (login_url, _login_owner):
            async with _publisher_redis() as (publisher_url, _publisher_owner):
                try:
                    yield login_url, publisher_url
                finally:
                    if stack is not None:
                        await stack.stop()

    try:
        async with redis_users_with_stack_cleanup() as (login_url, publisher_url):
            sender_url = os.environ.get("CELERY_BROKER_URL") or os.environ["REDIS_URL"]
            sender = Celery(
                "jobs-queue-cold-acceptance", broker=sender_url, backend=sender_url
            )
            sender.conf.update(
                task_serializer="json",
                result_serializer="json",
                accept_content=["json"],
            )
            _assert_owned_broker_target(sender)

            stack = IdentityModuleStack(
                test_engine,
                tmp_path,
                secret=secret,
                api_secret=api_secret,
                control=control,
                mail_key=Fernet.generate_key().decode(),
                login_redis_url=login_url,
                publisher_redis_url=publisher_url,
                smtp_port=inbox.port,
                jobs_queue_image=queue_image,
                jobs_queue_identity=queue_identity,
                jobs_queue_source_revision=queue_revision,
                jobs_queue_token=queue_token,
            )
            await stack.start()
            queue_evidence = stack.inspection_evidence["jobs-queue"]
            assert queue_evidence["user"] == "10005:10005"
            assert queue_evidence["read_only"] is True
            assert queue_evidence["secret_mount_targets"] == [
                "/run/secrets/jobs_queue_broker_url",
                "/run/secrets/jobs_queue_token",
            ]
            record_property("jobs_queue_runtime", queue_evidence)

            async with httpx.AsyncClient(
                base_url=stack.api_origin,
                trust_env=False,
                timeout=40,
            ) as client:
                headers = await _login(client, maker, world["actor_id"])
                await stack.stop_service("jobs-queue")

                unavailable = await _request_cross_frame(client, headers, world)
                assert unavailable.status_code == 503, unavailable.text
                failed_job = await _latest_job(
                    test_engine,
                    actor_id=world["actor_id"],
                    project_id=world["project_id"],
                )
                assert failed_job is not None
                assert failed_job.status == AsyncJobStatus.FAILED.value
                assert failed_job.celery_task_id
                assert (
                    str(uuid.UUID(failed_job.celery_task_id))
                    == failed_job.celery_task_id
                )
                known_task_ids.add(failed_job.celery_task_id)
                assert _broker_state(sender, "default") == {
                    "queue_size": 0,
                    "unacknowledged": 0,
                }, (
                    "API fell back to its embedded Celery broker while Jobs owner was down"
                )
                record_property(
                    "jobs_queue_owner_outage",
                    {
                        "http_status": unavailable.status_code,
                        "job_status": failed_job.status,
                    },
                )

                await stack.start_service("jobs-queue")
                accepted = await _request_cross_frame(client, headers, world)
                assert accepted.status_code == 202, stack.redact(accepted.text)
                accepted_job = accepted.json()
                execution_id = accepted_job["celery_task_id"]
                assert execution_id and str(uuid.UUID(execution_id)) == execution_id
                known_task_ids.add(execution_id)
                persisted = await _job_by_id(test_engine, uuid.UUID(accepted_job["id"]))
                assert persisted is not None
                assert persisted.id == uuid.UUID(accepted_job["id"])
                assert persisted.celery_task_id == execution_id
                assert persisted.status == AsyncJobStatus.PENDING.value
                assert _broker_state(sender, "default") == {
                    "queue_size": 1,
                    "unacknowledged": 0,
                }, "cross-frame publish did not use the existing default route"
                reused = await _request_cross_frame(client, headers, world)
                assert reused.status_code == 202, stack.redact(reused.text)
                assert reused.json()["id"] == accepted_job["id"]
                assert reused.json()["celery_task_id"] == execution_id
                assert _broker_state(sender, "default") == {
                    "queue_size": 1,
                    "unacknowledged": 0,
                }
                record_property(
                    "jobs_cross_frame_singleflight_reuse",
                    {"same_job": True, "same_execution": True, "queue_size": 1},
                )

            worker = WorkspaceCeleryWorker(
                test_engine,
                tmp_path / "jobs-queue-worker",
                backend_token=uuid.uuid4().hex,
                object_store_endpoint=minio_origin,
                object_store_bucket=guide_bucket,
                object_store_use_ssl=minio_parts.scheme == "https",
                object_store_access_key=guide_scope["access_key"],
                object_store_secret_key=guide_scope["secret_key"],
                object_store_buckets=resources["buckets"],
                queue_name="default",
            )
            worker.start()
            await _wait_worker_ready(sender, worker)
            signature = worker.task_signature(_TASK_NAME)
            assert signature == {
                "task": _TASK_NAME,
                "parameters": ["job_id"],
            }
            active_queues = await asyncio.to_thread(
                sender.control.inspect(
                    timeout=2, destination=[worker.node_name]
                ).active_queues
            )
            assert active_queues and {
                queue["name"] for queue in active_queues[worker.node_name]
            } == {"default"}
            delivery = sender.AsyncResult(execution_id)
            result = await asyncio.to_thread(delivery.get, timeout=120, propagate=False)
            assert result is None
            first_ack = await _wait_broker_ack(sender, worker, execution_id)
            status, result_summary, _error = await _job_state(test_engine, persisted.id)
            assert status == AsyncJobStatus.COMPLETED.value
            assert result_summary["created_annotation_count"] == 1
            completed_job = await _job_by_id(test_engine, persisted.id)
            assert completed_job is not None
            assert completed_job.celery_task_id == execution_id
            assert await _annotation_count(test_engine, world["target_task_ids"]) == 1
            record_property(
                "jobs_queue_first_delivery",
                {"ack": first_ack, "task_signature": signature},
            )

            row_access_before_restore = _assert_api_row_access_probe(
                stack,
                job_id=completed_job.id,
                database=str(test_engine.url.database),
            )
            record_property(
                "jobs_api_container_row_access_before_restore",
                row_access_before_restore,
            )
            async with httpx.AsyncClient(
                base_url=stack.api_origin,
                trust_env=False,
                timeout=40,
            ) as client:
                record_property(
                    "jobs_api_http_read_before_restore",
                    await _assert_completed_job_http_read(
                        client,
                        headers,
                        completed_job.id,
                        world["source_task_id"],
                        redact=stack.redact,
                    ),
                )

            exports_before_restore = await _exercise_export_dispatch(
                test_engine,
                stack,
                sender,
                resources,
                owner_s3,
                tmp_path / "jobs-export-before-restore",
                known_task_ids,
                check_outage=True,
            )
            record_property(
                "jobs_export_dispatch_before_restore", exports_before_restore
            )
            worker.stop()
            worker = None
            _assert_owned_broker_target(sender)
            snapshot.write_bytes(
                _postgres_tool(
                    test_engine,
                    [
                        "pg_dump",
                        "-U",
                        "owner",
                        "--format=custom",
                        "--dbname",
                        str(test_engine.url.database),
                    ],
                )
            )

            async with restored_database_with_cleanup() as restored_database:
                clone_engine = create_async_engine(
                    test_engine.url.set(database=restored_database),
                    hide_parameters=True,
                )
                with _role_urls_for_database(restored_database):
                    await stack.switch_database(restored_database)
                    clone_maker = async_sessionmaker(
                        clone_engine, expire_on_commit=False
                    )
                    restored_world = await _seed_member_job(
                        clone_maker,
                        label="jobs-queue-restored",
                        target_count=1,
                    )
                    await _terminalize_seed_job(clone_engine, restored_world["job_id"])
                    clone_worker = WorkspaceCeleryWorker(
                        clone_engine,
                        tmp_path / "jobs-queue-worker-restored",
                        backend_token=uuid.uuid4().hex,
                        object_store_endpoint=minio_origin,
                        object_store_bucket=guide_bucket,
                        object_store_use_ssl=minio_parts.scheme == "https",
                        object_store_access_key=guide_scope["access_key"],
                        object_store_secret_key=guide_scope["secret_key"],
                        object_store_buckets=resources["buckets"],
                        queue_name="default",
                    )
                    async with httpx.AsyncClient(
                        base_url=stack.api_origin,
                        trust_env=False,
                        timeout=40,
                    ) as client:
                        restored_headers = await _login(
                            client, clone_maker, restored_world["actor_id"]
                        )
                        post_restore = await _request_cross_frame(
                            client, restored_headers, restored_world
                        )
                        assert post_restore.status_code == 202, stack.redact(
                            post_restore.text
                        )
                        restored_job = post_restore.json()
                        restored_execution_id = restored_job["celery_task_id"]
                        assert restored_execution_id
                        assert (
                            str(uuid.UUID(restored_execution_id))
                            == restored_execution_id
                        )
                        known_task_ids.add(restored_execution_id)
                        assert _broker_state(sender, "default") == {
                            "queue_size": 1,
                            "unacknowledged": 0,
                        }
                        clone_worker.start()
                        await _wait_worker_ready(sender, clone_worker)
                        restored_delivery = sender.AsyncResult(restored_execution_id)
                        restored_result = await asyncio.to_thread(
                            restored_delivery.get, timeout=120, propagate=False
                        )
                        assert restored_result is None
                        restored_ack = await _wait_broker_ack(
                            sender, clone_worker, restored_execution_id
                        )
                        restored_status, restored_summary, _error = await _job_state(
                            clone_engine, uuid.UUID(restored_job["id"])
                        )
                        assert restored_status == AsyncJobStatus.COMPLETED.value
                        assert restored_summary["created_annotation_count"] == 1
                        completed_restored_job = await _job_by_id(
                            clone_engine, uuid.UUID(restored_job["id"])
                        )
                        assert completed_restored_job is not None
                        assert (
                            completed_restored_job.celery_task_id
                            == restored_execution_id
                        )
                        assert (
                            await _annotation_count(
                                clone_engine, restored_world["target_task_ids"]
                            )
                            == 1
                        )
                        row_access_after_restore = _assert_api_row_access_probe(
                            stack,
                            job_id=completed_restored_job.id,
                            database=restored_database,
                        )
                        record_property(
                            "jobs_api_container_row_access_after_restore",
                            row_access_after_restore,
                        )
                        record_property(
                            "jobs_api_http_read_after_restore",
                            await _assert_completed_job_http_read(
                                client,
                                restored_headers,
                                completed_restored_job.id,
                                restored_world["source_task_id"],
                                redact=stack.redact,
                            ),
                        )
                        record_property("jobs_queue_after_full_restore", restored_ack)
                        export_headers = await _login(
                            client,
                            clone_maker,
                            uuid.UUID(exports_before_restore["owner_id"]),
                        )
                        for item in exports_before_restore["jobs"]:
                            restored_export = await client.get(
                                f"/api/v1/async-jobs/{item['job_id']}",
                                headers=export_headers,
                            )
                            assert restored_export.status_code == 200, stack.redact(
                                restored_export.text
                            )
                            assert restored_export.json()["status"] == "completed"
                            assert (
                                restored_export.json()["celery_task_id"]
                                == item["execution_id"]
                            )
                        exports_after_restore = await _exercise_export_dispatch(
                            clone_engine,
                            stack,
                            sender,
                            resources,
                            owner_s3,
                            tmp_path / "jobs-export-after-restore",
                            known_task_ids,
                            check_outage=False,
                        )
                        record_property(
                            "jobs_export_dispatch_after_restore", exports_after_restore
                        )

                        cancel_world = await _seed_member_job(
                            clone_maker,
                            label="jobs-queue-cancel",
                            target_count=1,
                        )
                        cancel_execution_id = str(uuid.uuid4())
                        known_task_ids.add(cancel_execution_id)
                        async with clone_maker.begin() as db:
                            cancel_job = await db.get(AsyncJob, cancel_world["job_id"])
                            assert cancel_job is not None
                            cancel_job.status = AsyncJobStatus.RUNNING.value
                            cancel_job.started_at = datetime.now(timezone.utc)
                            cancel_job.celery_task_id = cancel_execution_id
                        cancel_headers = await _login(
                            client, clone_maker, cancel_world["actor_id"]
                        )
                        cancellation = await client.post(
                            f"/api/v1/async-jobs/{cancel_world['job_id']}/cancel",
                            headers=cancel_headers,
                        )
                        assert cancellation.status_code == 200, cancellation.text
                        assert cancellation.json()["status"] == "cancel_requested"
                        cancel_job_after_request = await _job_by_id(
                            clone_engine, cancel_world["job_id"]
                        )
                        assert cancel_job_after_request is not None
                        assert (
                            cancel_job_after_request.celery_task_id
                            == cancel_execution_id
                        )
                        assert cancel_job_after_request.payload["cancel_requested"]
                        assert await _wait_revoked(
                            sender, clone_worker, cancel_execution_id
                        ) == {
                            "task_id": cancel_execution_id,
                            "worker_answered": True,
                        }
                        before_cancel_delivery = clone_worker.assert_runtime()

                        cooperative_id = str(uuid.uuid4())
                        known_task_ids.add(cooperative_id)
                        cooperative = sender.send_task(
                            _TASK_NAME,
                            args=[str(cancel_world["job_id"])],
                            task_id=cooperative_id,
                            queue="default",
                        )
                        assert (
                            await asyncio.to_thread(
                                cooperative.get, timeout=120, propagate=False
                            )
                            is None
                        )
                        cancel_ack = await _wait_broker_ack(
                            sender, clone_worker, cooperative_id
                        )
                        cancelled_status, cancelled_result, _error = await _job_state(
                            clone_engine, cancel_world["job_id"]
                        )
                        assert cancelled_status == AsyncJobStatus.CANCELLED.value
                        assert cancelled_result["cancelled_count"] == 1
                        assert (
                            await _annotation_count(
                                clone_engine, cancel_world["target_task_ids"]
                            )
                            == 0
                        )
                        after_cancel_delivery = clone_worker.assert_runtime()
                        assert (
                            after_cancel_delivery["started_at"]
                            == (before_cancel_delivery["started_at"])
                        )
                        assert (
                            after_cancel_delivery["container_id"]
                            == (before_cancel_delivery["container_id"])
                        )

                        late_replay_id = str(uuid.uuid4())
                        known_task_ids.add(late_replay_id)
                        late_replay = sender.send_task(
                            _TASK_NAME,
                            args=[str(cancel_world["job_id"])],
                            task_id=late_replay_id,
                            queue="default",
                        )
                        assert (
                            await asyncio.to_thread(
                                late_replay.get, timeout=120, propagate=False
                            )
                            is None
                        )
                        replay_ack = await _wait_broker_ack(
                            sender, clone_worker, late_replay_id
                        )
                        final_status, _final_result, _error = await _job_state(
                            clone_engine, cancel_world["job_id"]
                        )
                        assert final_status == AsyncJobStatus.CANCELLED.value
                        assert (
                            await _annotation_count(
                                clone_engine, cancel_world["target_task_ids"]
                            )
                            == 0
                        )
                        record_property(
                            "jobs_queue_cooperative_cancel_and_terminal_replay",
                            {
                                "revoke_execution_id": cancel_execution_id,
                                "cancel_ack": cancel_ack,
                                "late_replay_ack": replay_ack,
                            },
                        )
                    assert _broker_state(sender, "default") == {
                        "queue_size": 0,
                        "unacknowledged": 0,
                    }, "default queue must be drained before worker and stack cleanup"

            record_property(
                "jobs_queue_execution_ids",
                sorted(known_task_ids),
            )
    except BaseException:
        if stack is not None:
            try:
                record_property(
                    "jobs_queue_module_diagnostics", stack.capture_diagnostics()
                )
            except Exception as diagnostic_error:
                record_property(
                    "jobs_queue_module_diagnostics_error",
                    type(diagnostic_error).__name__,
                )
        raise
    finally:
        try:
            if clone_worker is not None:
                clone_worker.stop()
        finally:
            try:
                if worker is not None:
                    worker.stop()
            finally:
                try:
                    if stack is not None:
                        await stack.stop()
                finally:
                    if clone_engine is not None:
                        await clone_engine.dispose()
                    if sender is not None:
                        sender.close()
                    inbox.close()
                    destroy_guide_scope(guide_scope)
