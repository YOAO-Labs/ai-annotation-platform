"""Cold Jobs queue/API/worker acceptance against the verifier-owned stack."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import json
from io import BytesIO
import os
from pathlib import Path
import secrets
import shutil
import sys
import time
from urllib.parse import urlsplit
import uuid
import zipfile

from cryptography.fernet import Fernet
from celery import Celery
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.task import Task
from app.db.models.user import User
from tests.factory import DEFAULT_PASSWORD, create_batch
from tests.identity_module_assembly_helpers import (
    IdentityModuleStack,
    create_retained_realm,
)
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    create_guide_storage_scope,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_module_assembly_live import _restored_database
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
    _retire_owned_export_executions,
    _seed_export_world,
    _verify_export_source,
)
from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker
from tests.jobs_quality_module_helpers import exercise_quality_dispatch
from tests.jobs_recovery_helpers import denied_first_publication, jobs_recovery_worker


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_LIVE_ACCEPTANCE") != "1"
        or os.getenv("AAP_JOBS_HOST_ASSEMBLY_LIVE") != "1"
        or not os.getenv("AAP_IDENTITY_API_RUNTIME_IMAGE")
        or not os.getenv("AAP_WORKSPACE_MODULE_API_IMAGE"),
        reason="requires the source-bound API and owned worker verifier",
    ),
]

_CLONE_ROLE_ENVIRONMENT = (
    "TEST_DATABASE_URL",
    "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL",
    "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_CONTAINER_URL",
    "AAP_MODULE_DATA_PREANNOTATION_RESULT_URL",
    "AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL",
    "AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL",
    "AAP_MODULE_DATA_INFERENCE_ROUTING_URL",
    "AAP_MODULE_DATA_INFERENCE_ENDPOINT_SETUP_URL",
    "AAP_MODULE_DATA_INFERENCE_ENDPOINT_HEALTH_OBSERVATION_URL",
    "AAP_MODULE_DATA_INFERENCE_ENDPOINT_CATALOG_FALLBACK_URL",
    "AAP_MODULE_DATA_INFERENCE_ENDPOINT_STATS_URL",
    "AAP_MODULE_DATA_INFERENCE_ENDPOINT_BACKEND_READ_URL",
    "AAP_MODULE_DATA_INFERENCE_ENDPOINT_LIFECYCLE_URL",
    "AAP_MODULE_DATA_INFERENCE_ENDPOINT_EXECUTION_URL",
    "AAP_MODULE_DATA_INFERENCE_BACKEND_STATS_URL",
    "AAP_MODULE_DATA_INFERENCE_GPU_URL",
    "AAP_MODULE_DATA_INFERENCE_GPU_HEALTH_ENDPOINT_URL",
    "AAP_MODULE_DATA_INFERENCE_GPU_CONTROL_ENDPOINT_URL",
    "AAP_MODULE_DATA_INFERENCE_HEALTH_DATA_URL",
    "AAP_MODULE_DATA_INFERENCE_HEALTH_OBSERVATION_URL",
    "AAP_MODULE_DATA_JOBS_DISPATCH_RELAY_URL",
    "AAP_MODULE_DATA_JOBS_PUBLICATION_URL",
    "AAP_MODULE_DATA_LABELING_VIDEO_WRITER_URL",
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


def _owned_jobs_role_url(name: str, expected_user: str, test_engine) -> str:
    url = make_url(os.environ[name])
    reference = test_engine.url
    if (
        url.drivername not in {"postgresql", "postgresql+asyncpg"}
        or url.username != expected_user
        or not url.password
        or (url.host, url.port, url.database)
        != (reference.host, reference.port, reference.database)
    ):
        raise RuntimeError("Jobs relay role does not match the owned test database")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


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
    assert queue in {"default", "export", "media", "gpu"}
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
import sys
import uuid
from types import SimpleNamespace

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

async def _run():
    sys.path.insert(0, "/app")
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.services.jobs_domain_query_data import JobsDomainQueryPostgresData
    from app.config import settings
    from app.services.jobs_lifecycle_data import job_lock_statement
    from app.services import jobs_lifecycle_data as async_job_svc
    from app.services.data_management.actions import (
        lock_idempotency_key, find_idempotent_job, assert_idempotent_request_matches,
    )
    from app.services.point_cloud_quality.execution import _cancel_requested
    from fastapi import HTTPException

    job_id = uuid.UUID(sys.argv[1])
    expected_database = sys.argv[2]
    engine = create_async_engine(settings.database_url, hide_parameters=True)
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
            await db.refresh(row, attribute_names=["updated_at"])
            partial_refresh = (
                db.identity_map.get(identity) is row
                and row.result == original_result
                and row.progress_pct == original_progress
                and "updated_at" not in inspect(row).expired_attributes
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
                    "'yoao_jobs_v1.async_job_creation','SELECT'), "
                    "pg_catalog.has_table_privilege(current_user,"
                    "'yoao_jobs_v1.async_job_creation','INSERT')"
                ))
            ).one()
            if tuple(privileges) != (False, False, True, True):
                raise RuntimeError("creation_privileges")
            created = await async_job_svc.create_job(
                db, kind="audit_archive", user_id=actor_id, project_id=row.project_id,
                payload={"probe": "jobs-creation", "data_manager_idempotency_key": "cold-action-probe",
                         "data_manager_request_digest": "cold-action-digest"},
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
            await lock_idempotency_key(
                db, action="cold-probe", project_id=row.project_id,
                actor_id=actor_id, key="cold-action-probe",
            )
            found = await find_idempotent_job(
                db, kind="audit_archive", project_id=row.project_id,
                actor_id=actor_id, key="cold-action-probe",
            )
            if found is not created:
                raise RuntimeError("action_identity")
            assert_idempotent_request_matches(found, digest="cold-action-digest")
            try:
                assert_idempotent_request_matches(found, digest="changed-digest")
            except HTTPException as conflict:
                if conflict.status_code != 409 or conflict.detail["job_id"] != str(created_id):
                    raise
            else:
                raise RuntimeError("action_digest_conflict")
            point_cloud_projection = await _cancel_requested(
                db, SimpleNamespace(async_job_id=job_id)
            ) is False
            if not point_cloud_projection:
                raise RuntimeError("point_cloud_projection")
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
            # Presentation writes and cleanup belong to the fixture transport,
            # not the restricted API consumer.
            try:
                async with db.begin_nested():
                    await db.scalar(text(
                        "SELECT yoao_jobs_v1.create_filter_fixture_jobs("
                        "CAST(:run AS uuid), CAST(:completed AS uuid), "
                        "CAST(:failed AS uuid), CAST(:first_project AS uuid), "
                        "CAST(:second_project AS uuid), CAST(:actor AS uuid), "
                        "CAST(:first_prompt AS text), CAST(:second_prompt AS text))"
                    ), {
                        "run": uuid.uuid4(), "completed": uuid.uuid4(),
                        "failed": uuid.uuid4(), "first_project": row.project_id,
                        "second_project": uuid.uuid4(), "actor": actor_id,
                        "first_prompt": "completed", "second_prompt": "failed",
                    })
            except Exception as error:
                fixture_create_sqlstate = _sqlstate(error)
                if fixture_create_sqlstate != "42501":
                    raise
            else:
                raise RuntimeError("fixture_create_allowed")
            try:
                async with db.begin_nested():
                    await db.execute(text("DELETE FROM public.async_jobs WHERE false"))
            except Exception as error:
                private_delete_sqlstate = _sqlstate(error)
                if private_delete_sqlstate != "42501":
                    raise
            else:
                raise RuntimeError("private_delete_allowed")
            private_delete = await db.scalar(text(
                "SELECT pg_catalog.has_table_privilege(current_user,"
                "'public.async_jobs','DELETE')"
            ))
            if private_delete:
                raise RuntimeError("private_delete_allowed")
            try:
                async with db.begin_nested():
                    await db.scalar(text(
                        "SELECT yoao_jobs_v1.delete_fixture_project_jobs(CAST(:ids AS uuid[]))"
                    ), {"ids": [row.project_id]})
            except Exception as error:
                fixture_delete_sqlstate = _sqlstate(error)
                if fixture_delete_sqlstate != "42501":
                    raise
            else:
                raise RuntimeError("fixture_delete_allowed")
            pending_after_delete = await db.scalar(text(
                "SELECT count(*) FROM yoao_jobs_v1.async_job_creation "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created_id})
            if pending_after_delete != 1:
                raise RuntimeError("fixture_delete_lost_pending")
            await db.rollback()
            rolled_back = await db.scalar(text(
                "SELECT count(*) FROM yoao_jobs_v1.async_job_creation "
                "WHERE id=CAST(:id AS uuid)"
            ), {"id": created_id})
            if rolled_back != 0:
                raise RuntimeError("creation_rollback")
            restored = (await db.execute(text(
                "SELECT id,status,result,progress_pct "
                "FROM yoao_jobs_v1.read_job(CAST(:id AS uuid))"
            ), {"id": job_id})).one()
            if tuple(restored) != (job_id, "completed", original_result, original_progress):
                raise RuntimeError("fixture_delete_rollback")
            fixture_delete_acl = {
                "private_table_delete": private_delete,
                "private_delete_sqlstate": private_delete_sqlstate,
                "fixture_delete_sqlstate": fixture_delete_sqlstate,
                "pending_row_preserved": True,
                "completed_row_restored": True,
                "caller_rollback": True,
            }
            fixture_update_acl = {
                "private_table_update": update_privileges[0],
                "private_column_update": update_privileges[1],
                "private_update_sqlstate": private_update_sqlstate,
                "lifecycle_projection_and_expiry": lifecycle_updated,
                "fixture_create_sqlstate": fixture_create_sqlstate,
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

        return {
            "ok": True,
            "role_match": True,
            "database_match": True,
            "action_identity_and_digest": True,
            "point_cloud_projection": point_cloud_projection,
            "domain_subject_matches": domain_subject_matches,
            "creation_acl": creation_acl,
            "fixture_delete_acl": fixture_delete_acl,
            "fixture_update_acl": fixture_update_acl,
            "partial_refresh": partial_refresh,
            "same_orm_identity": same_identity,
        }
    finally:
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
    assert evidence["role_match"] is True
    assert evidence["database_match"] is True
    assert evidence["domain_subject_matches"] is True
    assert evidence["action_identity_and_digest"] is True
    assert evidence["point_cloud_projection"] is True
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
        "private_delete_sqlstate": "42501",
        "fixture_delete_sqlstate": "42501",
        "pending_row_preserved": True,
        "completed_row_restored": True,
        "caller_rollback": True,
    }
    assert evidence["fixture_update_acl"] == {
        "private_table_update": False,
        "private_column_update": False,
        "private_update_sqlstate": "42501",
        "lifecycle_projection_and_expiry": True,
        "fixture_create_sqlstate": "42501",
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


class _ColdExportRealm:
    """Retain original mounted paths until exact native retirement succeeds."""

    def __init__(self, root: Path):
        self.root = root
        self.unsettled = False
        self.worker = None
        self.scope_path = None
        self.guard_error = None

    def begin(self, directory, scope, world):
        self.unsettled = True
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.scope_path = directory / "retained-export-scope.json"
        payload = {
            "project_id": str(world["project_id"]),
            "source_key": world["source_key"],
            "container": scope["container"],
            "bucket": scope["bucket"],
            "inputs_bucket": scope["inputs_bucket"],
            "config": scope["config"],
            "policy": scope["policy"],
            "user_created": scope["user_created"],
            "policy_created": scope["policy_created"],
            "access_key": scope["access_key"],
            "secret_key": scope["secret_key"],
        }
        fd = os.open(self.scope_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(payload, target)


async def _exercise_export_dispatch(
    test_engine,
    stack,
    sender,
    resources,
    owner_s3,
    directory,
    known_task_ids,
    realm,
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
        realm.begin(directory, scope, world)
        owner_s3.put_object(
            Bucket=resources["buckets"]["MINIO_DATASETS_BUCKET"],
            Key=world["source_key"],
            Body=_SOURCE_PNG,
            ContentType="image/png",
        )
        await _verify_export_source(test_engine, world)
        project_path = f"/api/v1/projects/{world['project_id']}"
        requests = [
            ("project", project_path + "/export?targets=native_json", None),
            (
                "batch",
                project_path + f"/batches/{batch_id}/export?targets=native_json",
                None,
            ),
            (
                "selected",
                project_path + "/data-manager/tasks/export",
                {
                    "task_ids": [str(world["task_id"])],
                    "targets": ["native_json"],
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
                for label, path, body in requests:
                    async with denied_first_publication(test_engine):
                        response = await client.post(path, headers=headers, json=body)
                    assert response.status_code == 500, stack.redact(response.text)
                    async with maker() as db:
                        pending = await db.scalar(
                            select(AsyncJob)
                            .where(
                                AsyncJob.kind == "export",
                                AsyncJob.project_id == world["project_id"],
                                AsyncJob.user_id == world["owner_id"],
                            )
                            .order_by(AsyncJob.created_at.desc())
                            .limit(1)
                        )
                        assert pending is not None and pending.celery_task_id is None
                        assert pending.status == "pending"
                        intent = (
                            (
                                await db.execute(
                                    text(
                                        "SELECT i.intent_id, i.execution_id, i.envelope, i.state, "
                                        "a.state AS physical_state "
                                        "FROM yoao_jobs_v1.dispatch_intents AS i "
                                        "LEFT JOIN yoao_jobs_v1.dispatch_publication_attempts AS a "
                                        "ON a.intent_id=i.intent_id AND a.generation=i.generation "
                                        "WHERE i.job_id=:job AND i.purpose='execution'"
                                    ),
                                    {"job": pending.id},
                                )
                            )
                            .mappings()
                            .one()
                        )
                        assert intent["state"] == "pending"
                        assert intent["physical_state"] is None
                        assert (
                            intent["envelope"]["execution_id"] == intent["execution_id"]
                        )
                        assert intent["envelope"]["kwargs"]["async_job_id"] == str(
                            pending.id
                        )
                        outages.append(
                            {
                                "route": label,
                                "http_status": 500,
                                "job_id": str(pending.id),
                                "intent_id": str(intent["intent_id"]),
                                "execution_id": intent["execution_id"],
                                "envelope": intent["envelope"],
                            }
                        )
                        known_task_ids.add(intent["execution_id"])
                    assert _broker_state(sender, "export") == {
                        "queue_size": 0,
                        "unacknowledged": 0,
                    }
                relay_url = _owned_jobs_role_url(
                    "AAP_MODULE_DATA_JOBS_DISPATCH_RELAY_URL",
                    "yoao_jobs_dispatch_relay_client",
                    test_engine,
                )
                publication_url = _owned_jobs_role_url(
                    "AAP_MODULE_DATA_JOBS_PUBLICATION_URL",
                    "yoao_jobs_publication_client",
                    test_engine,
                )
                expected_jobs = {uuid.UUID(item["job_id"]) for item in outages}
                async with maker() as db:
                    candidates = set(
                        (
                            await db.execute(
                                text(
                                    "SELECT i.job_id FROM yoao_jobs_v1.dispatch_intents i "
                                    "JOIN public.async_jobs j ON j.id=i.job_id "
                                    "WHERE i.state IN ('pending','leased') "
                                    "AND j.status IN ('pending','running')"
                                )
                            )
                        ).scalars()
                    )
                assert candidates == expected_jobs, (
                    "recovery scope contains foreign work"
                )
                async with jobs_recovery_worker(
                    directory=directory / "jobs-recovery",
                    dispatch_url=relay_url,
                    publication_url=publication_url,
                ) as (worker_process, beat):
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        assert worker_process.poll() is None and beat.poll() is None
                        async with maker() as db:
                            submitted = set(
                                (
                                    await db.execute(
                                        text(
                                            "SELECT job_id FROM yoao_jobs_v1.dispatch_intents "
                                            "WHERE job_id=ANY(:jobs) AND state='submitted'"
                                        ),
                                        {"jobs": list(expected_jobs)},
                                    )
                                ).scalars()
                            )
                        if submitted == expected_jobs:
                            break
                        await asyncio.sleep(0.2)
                    else:
                        raise AssertionError(
                            "Jobs worker did not recover all outage exports"
                        )
                for item in outages:
                    async with maker() as db:
                        restored = (
                            (
                                await db.execute(
                                    text(
                                        "SELECT execution_id, envelope FROM yoao_jobs_v1.dispatch_intents "
                                        "WHERE intent_id=CAST(:intent AS uuid) AND state='submitted'"
                                    ),
                                    {"intent": item["intent_id"]},
                                )
                            )
                            .mappings()
                            .one()
                        )
                    assert restored["execution_id"] == item["execution_id"]
                    assert restored["envelope"] == item["envelope"]
                    receipts.append(
                        {
                            "route": item["route"],
                            "job_id": item["job_id"],
                            "execution_id": item["execution_id"],
                            "recovered": True,
                        }
                    )

            for label, path, body in requests:
                request_headers = dict(headers)
                if label == "selected":
                    request_headers["Idempotency-Key"] = "jobs-owner-selected-export"
                response = await client.post(path, headers=request_headers, json=body)
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
                    conflict = await client.post(
                        path,
                        headers=request_headers,
                        json={**body, "targets": ["coco"]},
                    )
                    assert conflict.status_code == 409, stack.redact(conflict.text)
                    assert conflict.json()["detail"]["code"] == "idempotency_conflict"
                    assert conflict.json()["detail"]["job_id"] == str(job_id)
                else:
                    assert row.celery_task_id is None
                receipts.append({"route": label, "job_id": str(job_id)})

            assert _broker_state(sender, "export") == {
                "queue_size": len(receipts),
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
                        assert args == [] and kwargs["targets"] == ["native_json"]
                        assert kwargs["project_id"] == str(world["project_id"])
                        messages[kwargs["async_job_id"]] = (execution_id, kwargs)
            assert set(messages) == {item["job_id"] for item in receipts}
            for item in receipts:
                execution_id, kwargs = messages[item["job_id"]]
                if item.get("recovered"):
                    assert execution_id == item["execution_id"]
                item["execution_id"] = execution_id
                known_task_ids.add(execution_id)
                assert kwargs["batch_id"] == (
                    str(batch_id) if item["route"] == "batch" else None
                )
                if item["route"] == "selected":
                    assert kwargs["task_ids"] == [str(world["task_id"])]
                    if not item.get("recovered"):
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
                export_execution=True,
            )
            realm.worker = worker
            assert worker.object_store_access_key != settings.minio_access_key
            assert worker.object_store_secret_key != settings.minio_secret_key
            worker.start()
            await _wait_worker_ready(sender, worker)
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
                if item["route"] == "selected" and not item.get("recovered"):
                    replay = await client.post(
                        project_path + "/data-manager/tasks/export",
                        headers={
                            **headers,
                            "Idempotency-Key": "jobs-owner-selected-export",
                        },
                        json={
                            "task_ids": [str(world["task_id"])],
                            "targets": ["native_json"],
                        },
                    )
                    assert replay.status_code == 202, stack.redact(replay.text)
                    assert replay.json() == {
                        "job_id": item["job_id"],
                        "status": "completed",
                    }
                    assert _broker_state(sender, "export") == {
                        "queue_size": 0,
                        "unacknowledged": 0,
                    }
                    item["keyed_conflict_http_status"] = 409
                    item["keyed_completed_replay_http_status"] = replay.status_code
        return {
            "owner_id": str(world["owner_id"]),
            "jobs": receipts,
            "outage": [
                {key: value for key, value in item.items() if key != "envelope"}
                for item in outages
            ],
        }
    finally:
        primary = sys.exception()
        cleanup_errors = []
        retired = False
        if primary is None:
            try:
                async with maker() as db:
                    project_jobs = set(
                        (
                            await db.scalars(
                                select(AsyncJob.id).where(
                                    AsyncJob.kind == "export",
                                    AsyncJob.project_id == world["project_id"],
                                )
                            )
                        ).all()
                    )
                expected_jobs = {uuid.UUID(item["job_id"]) for item in receipts}
                if project_jobs != expected_jobs:
                    raise RuntimeError("Export cleanup cannot identify every owned Job")
                retired = await _retire_owned_export_executions(
                    test_engine,
                    world["project_id"],
                    [{"job_id": job_id} for job_id in expected_jobs],
                )
                if not retired:
                    raise RuntimeError(
                        "Export native execution or publication remains unsettled"
                    )
            except BaseException as exc:
                cleanup_errors.append(exc)
        if retired and worker is not None:
            try:
                worker.stop()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if retired and not cleanup_errors:
            try:
                destroy_scope(scope)
            except BaseException as exc:
                cleanup_errors.append(exc)
        if retired and not cleanup_errors:
            realm.unsettled = False
            realm.worker = None
        if not retired:
            message = (
                "Export storage credentials retained: native retirement unconfirmed"
            )
            if primary is not None:
                primary.add_note(message)
            elif not cleanup_errors:
                cleanup_errors.append(RuntimeError(message))
        if cleanup_errors:
            if primary is not None:
                for error in cleanup_errors:
                    primary.add_note(f"Export cleanup failed: {error!r}")
            else:
                raise BaseExceptionGroup("Export cleanup failed", cleanup_errors)


async def _exercise_queue_cancellation(
    test_engine, client, maker, sender, worker, known_task_ids, record_property
):
    cancel_world = await _seed_member_job(
        maker,
        label="jobs-queue-cancel",
        target_count=1,
    )
    cancel_execution_id = str(uuid.uuid4())
    known_task_ids.add(cancel_execution_id)
    async with maker.begin() as db:
        cancel_job = await db.get(AsyncJob, cancel_world["job_id"])
        assert cancel_job is not None
        cancel_job.status = AsyncJobStatus.RUNNING.value
        cancel_job.started_at = datetime.now(timezone.utc)
        cancel_job.celery_task_id = cancel_execution_id
    cancel_headers = await _login(client, maker, cancel_world["actor_id"])
    cancellation = await client.post(
        f"/api/v1/async-jobs/{cancel_world['job_id']}/cancel",
        headers=cancel_headers,
    )
    assert cancellation.status_code == 200, cancellation.text
    assert cancellation.json()["status"] == "cancel_requested"
    cancel_job_after_request = await _job_by_id(test_engine, cancel_world["job_id"])
    assert cancel_job_after_request is not None
    assert cancel_job_after_request.celery_task_id == cancel_execution_id
    assert cancel_job_after_request.payload["cancel_requested"]
    assert await _wait_revoked(sender, worker, cancel_execution_id) == {
        "task_id": cancel_execution_id,
        "worker_answered": True,
    }
    before_cancel_delivery = worker.assert_runtime()

    cooperative_id = str(uuid.uuid4())
    known_task_ids.add(cooperative_id)
    cooperative = sender.send_task(
        _TASK_NAME,
        args=[str(cancel_world["job_id"])],
        task_id=cooperative_id,
        queue="default",
    )
    assert (
        await asyncio.to_thread(cooperative.get, timeout=120, propagate=False) is None
    )
    cancel_ack = await _wait_broker_ack(sender, worker, cooperative_id)
    cancelled_status, cancelled_result, _error = await _job_state(
        test_engine, cancel_world["job_id"]
    )
    assert cancelled_status == AsyncJobStatus.CANCELLED.value
    assert cancelled_result["cancelled_count"] == 1
    assert await _annotation_count(test_engine, cancel_world["target_task_ids"]) == 0
    after_cancel_delivery = worker.assert_runtime()
    assert after_cancel_delivery["started_at"] == (before_cancel_delivery["started_at"])
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
        await asyncio.to_thread(late_replay.get, timeout=120, propagate=False) is None
    )
    replay_ack = await _wait_broker_ack(sender, worker, late_replay_id)
    final_status, _final_result, _error = await _job_state(
        test_engine, cancel_world["job_id"]
    )
    assert final_status == AsyncJobStatus.CANCELLED.value
    assert await _annotation_count(test_engine, cancel_world["target_task_ids"]) == 0
    record_property(
        "jobs_queue_cooperative_cancel_and_terminal_replay",
        {
            "revoke_execution_id": cancel_execution_id,
            "cancel_ack": cancel_ack,
            "late_replay_ack": replay_ack,
        },
    )


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

    secret, api_secret, control = [secrets.token_urlsafe(32) for _ in range(3)]
    assert len({secret, api_secret, control}) == 3
    inbox = SMTPInbox()
    tmp_path = create_retained_realm()
    realm = _ColdExportRealm(tmp_path)
    snapshot = tmp_path / "jobs-queue-module.dump"
    stack = None
    sender = None
    worker = None
    clone_worker = None
    clone_engine = None
    known_task_ids: set[str] = set()

    def retain_realm():
        if realm.unsettled:
            return True
        if stack is None:
            return False
        try:
            return stack.retention_required
        except BaseException as exc:
            realm.guard_error = exc
            return True

    @asynccontextmanager
    async def restored_database_with_cleanup():
        async with _restored_database(
            test_engine, snapshot, retain=retain_realm
        ) as restored_database:
            try:
                yield restored_database
            finally:
                try:
                    if clone_worker is not None and not retain_realm():
                        clone_worker.stop()
                finally:
                    try:
                        if stack is not None and not retain_realm():
                            await stack.stop()
                    finally:
                        if clone_engine is not None and not retain_realm():
                            await clone_engine.dispose()

    @asynccontextmanager
    async def redis_users_with_stack_cleanup():
        async with _login_redis(retain=retain_realm) as (login_url, _login_owner):
            try:
                yield login_url
            finally:
                if stack is not None and not retain_realm():
                    await stack.stop()

    try:
        async with redis_users_with_stack_cleanup() as login_url:
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
                smtp_port=inbox.port,
                jobs_publication=True,
            )
            stack.retain_while(lambda: realm.unsettled)
            await stack.start()
            api_evidence = stack.inspection_evidence["identity-api"]
            assert api_evidence["read_only"] is True
            assert (
                "/run/secrets/jobs_publication_database_url"
                in api_evidence["secret_mount_targets"]
            )
            assert (
                "/run/secrets/jobs_dispatch_database_url"
                not in api_evidence["secret_mount_targets"]
            )
            record_property("jobs_api_broker_runtime", api_evidence)

            async with httpx.AsyncClient(
                base_url=stack.api_origin,
                trust_env=False,
                timeout=40,
            ) as client:
                headers = await _login(client, maker, world["actor_id"])
                async with denied_first_publication(test_engine):
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
                }, "failed owner admission must not send a broker message"
                record_property(
                    "jobs_publication_authority_outage",
                    {
                        "http_status": unavailable.status_code,
                        "job_status": failed_job.status,
                    },
                )

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
                {"ack": first_ack},
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
                realm,
                check_outage=True,
            )
            record_property(
                "jobs_export_dispatch_before_restore", exports_before_restore
            )
            (quality_before_restore, quality_headers) = await exercise_quality_dispatch(
                test_engine,
                stack,
                sender,
                resources,
                owner_s3,
                tmp_path / "jobs-quality-before-restore",
                known_task_ids,
                check_outage=True,
            )
            record_property(
                "jobs_quality_dispatch_before_restore", quality_before_restore
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
                            realm,
                            check_outage=False,
                        )
                        record_property(
                            "jobs_export_dispatch_after_restore", exports_after_restore
                        )

                        old_quality_job = await _job_by_id(
                            clone_engine, uuid.UUID(quality_before_restore["job_id"])
                        )
                        assert old_quality_job is not None
                        restored_quality = await client.get(
                            f"/api/v1/projects/{old_quality_job.project_id}"
                            "/point-cloud-quality/runs/"
                            + quality_before_restore["run_id"],
                            headers=quality_headers,
                        )
                        assert restored_quality.status_code == 200, stack.redact(
                            restored_quality.text
                        )
                        assert restored_quality.json()["status"] == "completed"
                        assert (
                            restored_quality.json()["async_job_id"]
                            == (quality_before_restore["job_id"])
                        )
                        assert old_quality_job.status == "completed"
                        assert (
                            old_quality_job.celery_task_id
                            == (quality_before_restore["execution_id"])
                        )
                        record_property(
                            "jobs_quality_completed_run_survives_restore",
                            {
                                "run_id": quality_before_restore["run_id"],
                                "job_id": quality_before_restore["job_id"],
                                "execution_id": old_quality_job.celery_task_id,
                                "http_status": restored_quality.status_code,
                            },
                        )
                        (
                            quality_after_restore,
                            _quality_headers,
                        ) = await exercise_quality_dispatch(
                            clone_engine,
                            stack,
                            sender,
                            resources,
                            owner_s3,
                            tmp_path / "jobs-quality-after-restore",
                            known_task_ids,
                            check_outage=False,
                        )
                        record_property(
                            "jobs_quality_dispatch_after_restore", quality_after_restore
                        )

                        await _exercise_queue_cancellation(
                            clone_engine,
                            client,
                            clone_maker,
                            sender,
                            clone_worker,
                            known_task_ids,
                            record_property,
                        )
                    assert _broker_state(sender, "default") == {
                        "queue_size": 0,
                        "unacknowledged": 0,
                    }, "default queue must be drained before worker and stack cleanup"

            record_property(
                "jobs_queue_execution_ids",
                sorted(known_task_ids),
            )
    except BaseException as primary:
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
        if retain_realm():
            primary.add_note(f"Cold Jobs realm retained at {tmp_path}")
            try:
                manifest = {
                    "realm": str(tmp_path),
                    "database": stack._database_name if stack is not None else None,
                    "stack_project": stack.project_name if stack is not None else None,
                    "service_ids": stack.service_ids if stack is not None else {},
                    "scope_file": str(realm.scope_path) if realm.scope_path else None,
                    "worker": (
                        {
                            "container_id": realm.worker.container_id,
                            "network_id": realm.worker.network_id,
                            "directory": str(realm.worker.directory),
                            "queue": realm.worker.queue,
                        }
                        if realm.worker is not None
                        else None
                    ),
                }
                path = tmp_path / "retained-realm.json"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as target:
                    json.dump(manifest, target, indent=2)
                record_property("jobs_cold_retained_realm", str(path))
            except BaseException as diagnostic_error:
                primary.add_note(
                    f"Retained realm manifest failed: {diagnostic_error!r}"
                )
            if realm.guard_error is not None:
                primary.add_note(f"Retention guard failed: {realm.guard_error!r}")
            if realm.worker is not None:
                try:
                    record_property(
                        "jobs_export_worker_diagnostics",
                        realm.worker.capture_diagnostics(),
                    )
                except BaseException as diagnostic_error:
                    primary.add_note(
                        f"Export worker diagnostics failed: {diagnostic_error!r}"
                    )
        raise
    finally:
        if not retain_realm():
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
        if sys.exception() is None and not retain_realm():
            shutil.rmtree(tmp_path)


async def test_cold_jobs_queue_cancel_and_terminal_replay(
    test_engine, tmp_path, record_property
):
    """Finish cancellation independently when earlier cold operations already passed."""
    _verify_postgres(test_engine.url)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    resources, guide_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    minio_origin = endpoint_url()
    guide_scope, destroy_guide_scope = create_guide_storage_scope(
        resources,
        owner_s3,
        guide_bucket,
        f"outside-guide/jobs-cancel-{uuid.uuid4()}.txt",
    )
    secret, api_secret, control = [secrets.token_urlsafe(32) for _ in range(3)]
    inbox = SMTPInbox()
    stack = None
    worker = None
    sender = None
    try:
        async with _login_redis() as (login_url, _login_owner):
            try:
                sender_url = (
                    os.environ.get("CELERY_BROKER_URL") or os.environ["REDIS_URL"]
                )
                sender = Celery(
                    "jobs-cancel-cold-acceptance",
                    broker=sender_url,
                    backend=sender_url,
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
                    smtp_port=inbox.port,
                    jobs_publication=True,
                )
                await stack.start()
                worker = WorkspaceCeleryWorker(
                    test_engine,
                    tmp_path / "jobs-cancel-worker",
                    backend_token=uuid.uuid4().hex,
                    object_store_endpoint=minio_origin,
                    object_store_bucket=guide_bucket,
                    object_store_use_ssl=urlsplit(minio_origin).scheme == "https",
                    object_store_access_key=guide_scope["access_key"],
                    object_store_secret_key=guide_scope["secret_key"],
                    queue_name="default",
                )
                worker.start()
                await _wait_worker_ready(sender, worker)
                known_task_ids: set[str] = set()
                async with httpx.AsyncClient(
                    base_url=stack.api_origin, trust_env=False, timeout=40
                ) as client:
                    await _exercise_queue_cancellation(
                        test_engine,
                        client,
                        maker,
                        sender,
                        worker,
                        known_task_ids,
                        record_property,
                    )
                _assert_owned_broker_target(sender)
                record_property("jobs_queue_execution_ids", sorted(known_task_ids))
            except BaseException:
                if stack is not None:
                    record_property(
                        "jobs_queue_module_diagnostics", stack.capture_diagnostics()
                    )
                raise
            finally:
                try:
                    if worker is not None:
                        worker.stop()
                finally:
                    if stack is not None:
                        await stack.stop()
    finally:
        try:
            if sender is not None:
                sender.close()
        finally:
            inbox.close()
            destroy_guide_scope(guide_scope)


async def test_cold_jobs_queue_video_tracker_and_restore(
    test_engine, tmp_path, record_property
):
    from tests.jobs_video_tracker_module_helpers import (
        exercise_video_tracker_queue_and_restore,
    )

    await exercise_video_tracker_queue_and_restore(
        test_engine, tmp_path, record_property
    )
