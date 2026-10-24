"""Cold Jobs queue/API/worker acceptance against the verifier-owned stack."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import json
import os
import secrets
import time
from urllib.parse import urlsplit
import uuid

from cryptography.fernet import Fernet
from celery import Celery
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.user import User
from tests.factory import DEFAULT_PASSWORD
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


def _assert_owned_broker_target(sender: Celery) -> None:
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
    assert _broker_state(sender, "default") == {"queue_size": 0, "unacknowledged": 0}, (
        "default queue must be empty before the owned worker subscribes"
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
    from aap_jobs.lifecycle_contract import JOBS_LIFECYCLE_PROTOCOL
    from aap_jobs.row_lock_contract import JOBS_ROW_LOCK_PROTOCOL
    from app.config import settings
    from app.services import async_job
    from app.services.jobs_lifecycle_data import job_lock_statement

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

            db.expire(row, ["updated_at"])
            read_calls_before = _count(statements, "aap_jobs_v1.read_job(")
            refreshed = await async_job.get_job(db, job_id, populate_existing=True)
            read_calls_after = _count(statements, "aap_jobs_v1.read_job(")
            partial_refresh = (
                refreshed is row
                and refreshed.result == original_result
                and "updated_at" not in inspect(refreshed).expired_attributes
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
            await db.rollback()

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
        private_table_from = sum(
            bool(re.search(r"\\bfrom\\s+(?:public\\.)?async_jobs\\b", statement))
            for statement in lowered
        )
        if function_calls != {
            "read_job": 1,
            "read_job_for_share_nowait": 1,
            "read_job_for_update": 1,
        } or private_table_from:
            raise RuntimeError("sql_surface")
        return {
            "ok": True,
            "protocols": [JOBS_LIFECYCLE_PROTOCOL, JOBS_ROW_LOCK_PROTOCOL],
            "role_match": True,
            "database_match": True,
            "function_calls": function_calls,
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
                "python",
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
    assert evidence["protocols"] == ["aap-jobs-lifecycle/v1", "aap-jobs-row-locks/v1"]
    assert evidence["role_match"] is True
    assert evidence["database_match"] is True
    assert evidence["function_calls"] == {
        "read_job": 1,
        "read_job_for_share_nowait": 1,
        "read_job_for_update": 1,
    }
    assert evidence["private_table_from"] == 0
    assert evidence["partial_refresh"] is True
    assert evidence["same_orm_identity"] is True
    return evidence


async def _assert_completed_job_http_read(client, headers, job_id, *, redact):
    response = await client.get(f"/api/v1/async-jobs/{job_id}", headers=headers)
    assert response.status_code == 200, redact(response.text)
    result = response.json()
    assert result["id"] == str(job_id)
    assert result["status"] == AsyncJobStatus.COMPLETED.value
    assert result["result"]["created_annotation_count"] == 1
    return {
        "http_status": response.status_code,
        "job_status": result["status"],
        "created_annotation_count": result["result"]["created_annotation_count"],
    }


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
                        client, headers, completed_job.id, redact=stack.redact
                    ),
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
                                redact=stack.redact,
                            ),
                        )
                        record_property("jobs_queue_after_full_restore", restored_ack)

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
