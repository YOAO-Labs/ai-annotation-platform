"""Cold export-worker recovery acceptance after an owned hard process loss."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid
import zipfile
from io import BytesIO

from celery import Celery
from kombu.transport.redis import bytes_to_str as _kombu_bytes_to_str
from kombu.transport.redis import loads as _kombu_loads
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
import pytest

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.export_artifact import ExportArtifact
from app.db.models.notification import Notification
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from tests.factory import create_project, create_user
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    endpoint_url,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_workspace_cross_frame_worker_live import (
    _assert_workspace_consumer_closure,
)
from tests.test_workspace_worker_assembly_live import (
    _broker_state,
    _delete_owned_queue,
    _wait_broker_ack,
    _wait_worker_ready,
)
from tests.workspace_worker_assembly_helpers import WorkspaceCeleryWorker

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_EXPORT_WORKER_LIVE") != "1",
        reason="requires the explicitly enabled owned export-worker verifier",
    ),
]

_TASK_NAME = "app.workers.export.run_export"
_TASK_OPTS = {
    "include_attributes": True,
    "video_frame_mode": "keyframes",
    "axis_frame": "iso",
    "indexed_overlap_policy": "error",
    "video_overlap_policy": "error",
    "mots_frame_base": 0,
}
_SOURCE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/wy8AAAAASUVORK5CYII="
)


def _new_job_payload(project: Project, opts: dict | None = None) -> dict:
    options = dict(_TASK_OPTS if opts is None else opts)
    return {
        "targets": ["aap_json"],
        "format": "aap_json",
        **options,
        "project_display_id": project.display_id,
    }


async def _seed_export_world(test_engine) -> dict:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    async with maker.begin() as db:
        owner = await create_user(
            db,
            "project_admin",
            f"export-owner-{suffix}@worker.test",
            f"Export owner {suffix}",
        )
        reviewer = await create_user(
            db,
            "employee",
            f"export-reviewer-{suffix}@worker.test",
            f"Export reviewer {suffix}",
        )
        revoked_reviewer = await create_user(
            db,
            "employee",
            f"export-revoked-reviewer-{suffix}@worker.test",
            f"Export revoked reviewer {suffix}",
        )
        project = await create_project(
            db,
            owner_id=owner.id,
            name=f"Export hard loss {suffix}",
            type_key="image-det",
            classes=["car"],
        )
        project_member = ProjectMember(
            project_id=project.id,
            user_id=reviewer.id,
            role="reviewer",
            assigned_by=owner.id,
        )
        db.add(project_member)
        revoked_member = ProjectMember(
            project_id=project.id,
            user_id=revoked_reviewer.id,
            role="reviewer",
            assigned_by=owner.id,
        )
        db.add(revoked_member)
        dataset = Dataset(
            display_id=f"DS-EWL-{suffix[:8]}",
            name=f"export-worker-{suffix}",
            data_type="image",
            created_by=owner.id,
            metadata_={},
        )
        db.add(dataset)
        await db.flush()
        source_key = f"e2e/export-worker/{suffix}/source.png"
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name="source.png",
            file_path=source_key,
            file_type="image",
            file_size=len(_SOURCE_PNG),
            content_hash=hashlib.sha256(_SOURCE_PNG).hexdigest(),
            width=1,
            height=1,
            metadata_={},
        )
        db.add(item)
        await db.flush()
        task = Task(
            project_id=project.id,
            dataset_item_id=item.id,
            display_id=f"T-EWL-{suffix}",
            file_name="source.png",
            file_path=source_key,
            file_type="image",
            status="completed",
            is_labeled=True,
        )
        db.add(task)
        await db.flush()
        annotation = Annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=reviewer.id,
            source="manual",
            annotation_type="bbox",
            tool_unit_id="bbox",
            class_name="car",
            geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            attributes={},
            is_active=True,
            was_cancelled=False,
        )
        db.add(annotation)
        first_job = AsyncJob(
            id=uuid.uuid4(),
            kind="export",
            user_id=reviewer.id,
            project_id=project.id,
            status=AsyncJobStatus.PENDING.value,
            progress_pct=0,
            payload=_new_job_payload(project),
            result={},
        )
        db.add(first_job)
        revoked_job = AsyncJob(
            id=uuid.uuid4(),
            kind="export",
            user_id=revoked_reviewer.id,
            project_id=project.id,
            status=AsyncJobStatus.PENDING.value,
            progress_pct=0,
            payload=_new_job_payload(project),
            result={},
        )
        db.add(revoked_job)
        await db.flush()

    return {
        "owner_id": owner.id,
        "reviewer_id": reviewer.id,
        "reviewer_email": reviewer.email,
        "revoked_reviewer_id": revoked_reviewer.id,
        "project_id": project.id,
        "project_display_id": project.display_id,
        "project_name": project.name,
        "member_id": project_member.id,
        "revoked_member_id": revoked_member.id,
        "dataset_id": dataset.id,
        "dataset_display_id": dataset.display_id,
        "item_id": item.id,
        "source_key": source_key,
        "task_id": task.id,
        "task_display_id": task.display_id,
        "annotation_id": annotation.id,
        "job_id": first_job.id,
        "revoked_job_id": revoked_job.id,
    }


def _create_export_storage_scope(resources, owner_s3, settings, world):
    """Give the worker exact source-read and project-export object prefixes."""
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root / "scripts"))
    from verify_module_data_boundary import (
        _cleanup_mc,
        _cleanup_storage_scope,
        _destroy_cleanup_storage_scope,
    )

    scope = _cleanup_storage_scope(
        SimpleNamespace(settings=settings, resources=resources, s3=owner_s3)
    )
    scope["policy"] = f"aap-export-worker-{secrets.token_hex(8)}"
    source_bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    export_bucket = resources["buckets"]["MINIO_EXPORT_BUCKET"]
    source_arn = f"arn:aws:s3:::{source_bucket}/{world['source_key']}"
    export_arn = f"arn:aws:s3:::{export_bucket}/image/{world['project_id']}/*"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [source_arn],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3:PutObject",
                    "s3:GetObject",
                    "s3:DeleteObject",
                    "s3:AbortMultipartUpload",
                ],
                "Resource": [export_arn],
            },
        ],
    }
    try:
        _cleanup_mc(
            scope,
            [
                "admin",
                "user",
                "add",
                "scoped",
                scope["access_key"],
                scope["secret_key"],
            ],
        )
        scope["user_created"] = True
        _cleanup_mc(
            scope,
            ["admin", "policy", "create", "scoped", scope["policy"], "/dev/stdin"],
            payload=json.dumps(policy),
        )
        scope["policy_created"] = True
        _cleanup_mc(
            scope,
            [
                "admin",
                "policy",
                "attach",
                "scoped",
                scope["policy"],
                "--user",
                scope["access_key"],
            ],
        )
        return scope, _destroy_cleanup_storage_scope
    except BaseException:
        _destroy_cleanup_storage_scope(scope)
        raise


def _export_lock_snapshot(sender: Celery) -> list[dict[str, int | str]]:
    with sender.connection_for_read() as connection:
        client = connection.default_channel.client
        keys = client.scan_iter(match="export-build:*")
        snapshot = []
        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            ttl = int(client.pttl(raw_key))
            if ttl > 0:
                raw_value = client.get(raw_key)
                value = (
                    raw_value.encode()
                    if isinstance(raw_value, str)
                    else raw_value or b""
                )
                snapshot.append(
                    {
                        "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                        "owner_sha256": hashlib.sha256(value).hexdigest(),
                        "pttl_ms": ttl,
                    }
                )
        return sorted(snapshot, key=lambda row: row["key_sha256"])


def _new_export_locks(
    baseline: list[dict[str, int | str]], current: list[dict[str, int | str]]
) -> list[dict[str, int | str]]:
    previous = {row["key_sha256"] for row in baseline}
    return [row for row in current if row["key_sha256"] not in previous]


def _broker_snapshot(sender: Celery, queue: str) -> dict[str, int]:
    return _broker_state(sender, queue)


def _find_owned_unacked_export(channel, worker, task_id: str):
    qos = channel.qos
    client = channel.client
    matches = []
    for raw_tag, raw_payload in client.hgetall(qos.unacked_key).items():
        envelope = _kombu_loads(_kombu_bytes_to_str(raw_payload))
        if not isinstance(envelope, list) or len(envelope) != 3:
            raise AssertionError("Kombu Redis unacked entry has an invalid envelope")
        message, exchange, routing_key = envelope
        headers = message.get("headers") if isinstance(message, dict) else None
        if not isinstance(headers, dict) or headers.get("id") != task_id:
            continue
        if headers.get("task") != _TASK_NAME:
            raise AssertionError("Unacked task ID belongs to a different Celery task")
        resolved_queues = channel._lookup(exchange, routing_key)
        if (
            exchange != ""
            or routing_key != worker.queue
            or resolved_queues != [worker.queue]
        ):
            raise AssertionError("Unacked export is not routed only to the owned queue")
        if client.zscore(qos.unacked_index_key, raw_tag) is None:
            raise AssertionError("Unacked export tag is absent from Kombu's index")
        matches.append(
            (
                raw_tag,
                {
                    "task_id": task_id,
                    "task": headers["task"],
                    "exchange": exchange,
                    "routing_key": routing_key,
                    "delivery_tag_sha256": hashlib.sha256(
                        _kombu_bytes_to_str(raw_tag).encode()
                    ).hexdigest(),
                },
            )
        )
    if len(matches) != 1:
        raise AssertionError(
            f"Expected exactly one owned unacked export delivery, observed {len(matches)}"
        )
    return matches[0]


def _restore_owned_unacked_export(sender: Celery, worker, task_id: str) -> dict:
    with sender.connection_for_write() as connection:
        channel = connection.default_channel
        raw_tag, evidence = _find_owned_unacked_export(channel, worker, task_id)
        channel.qos.restore_by_tag(
            raw_tag,
            client=channel.client,
            leftmost=True,
        )
        client = channel.client
        # A live consumer may immediately reinsert this same delivery tag.
        # The eventual broker ACK below verifies both entries are gone.
        return {
            **evidence,
            "restored": True,
            "hash_present_after_restore": client.hget(channel.qos.unacked_key, raw_tag)
            is not None,
            "index_present_after_restore": client.zscore(
                channel.qos.unacked_index_key, raw_tag
            )
            is not None,
        }


async def _job_snapshot(test_engine, job_id: uuid.UUID) -> dict:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        job = await db.get(AsyncJob, job_id)
        assert job is not None
        result = dict(job.result or {})
        safe_url = result.get("download_url")
        if isinstance(safe_url, str):
            parsed = urlsplit(safe_url)
            safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return {
            "id": str(job.id),
            "status": job.status,
            "progress_pct": job.progress_pct,
            "celery_task_id": job.celery_task_id,
            "error_message": job.error_message,
            "result": {
                **{
                    key: value for key, value in result.items() if key != "download_url"
                },
                **({"download_url_path": safe_url} if safe_url else {}),
            },
        }


async def _job_download_url(test_engine, job_id: uuid.UUID) -> str:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        job = await db.get(AsyncJob, job_id)
        assert job is not None
        download_url = (job.result or {}).get("download_url")
        assert isinstance(download_url, str) and download_url
        return download_url


async def _wait_export_build_gate(test_engine, worker, world) -> dict:
    client_address = worker.assert_runtime()["database_client_address"]
    deadline = asyncio.get_running_loop().time() + 75
    last = None
    while asyncio.get_running_loop().time() < deadline:
        job = await _job_snapshot(test_engine, world["job_id"])
        async with test_engine.connect() as db:
            activity = (
                (
                    await db.execute(
                        text(
                            "SELECT pid, datname, usename, pg_catalog.host(client_addr) "
                            "AS client_addr, "
                            "state, wait_event_type, wait_event, query "
                            "FROM pg_catalog.pg_stat_activity "
                            "WHERE datname=current_database() "
                            "AND usename='identity_api_consumer' "
                            "AND client_addr=CAST(:client_address AS inet) "
                            "AND state='active' AND wait_event_type='Lock' "
                            "AND query ILIKE '%predictions%' "
                            "ORDER BY query_start DESC LIMIT 1"
                        ),
                        {"client_address": client_address},
                    )
                )
                .mappings()
                .first()
            )
        last = {"job": job, "activity": dict(activity) if activity else None}
        if (
            job["status"] == "running"
            and job["progress_pct"] == 10
            and job["celery_task_id"]
            and activity is not None
        ):
            query = str(activity["query"])
            return {
                "job": job,
                "activity": {
                    **dict(activity),
                    "query": query[:1000],
                    "observed_build_marker": "predictions",
                },
                "worker_runtime": worker.assert_runtime(),
            }
        await asyncio.sleep(0.2)
    raise AssertionError(f"export worker did not reach the build lock gate: {last}")


async def _postgres_lock_snapshot(test_engine, pid: int) -> list[dict]:
    async with test_engine.connect() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT locktype, mode, granted, "
                        "CASE WHEN relation IS NULL THEN NULL "
                        "ELSE relation::regclass::text END AS relation, "
                        "classid, objid, objsubid "
                        "FROM pg_catalog.pg_locks WHERE pid=:pid "
                        "ORDER BY locktype, mode, granted"
                    ),
                    {"pid": pid},
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]


async def _wait_backend_gone(test_engine, pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        async with test_engine.connect() as db:
            exists = await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity WHERE pid=:pid)"
                ),
                {"pid": pid},
            )
        if not exists:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"worker database backend {pid} survived the owned SIGKILL")


async def _wait_delivery_and_job(
    sender: Celery, test_engine, delivery_id: str, job_id: uuid.UUID, *, timeout: float
) -> dict:
    result = sender.AsyncResult(delivery_id)
    deadline = asyncio.get_running_loop().time() + timeout
    latest = {}
    while asyncio.get_running_loop().time() < deadline:
        state = await asyncio.to_thread(lambda: result.state)
        job = await _job_snapshot(test_engine, job_id)
        latest = {"delivery_state": state, "job": job}
        if job["status"] == "completed" or state in {
            "SUCCESS",
            "FAILURE",
            "RETRY",
            "REVOKED",
        }:
            return latest
        await asyncio.sleep(0.25)
    return latest


async def _wait_delivery_state(
    sender: Celery, delivery_id: str, *, timeout: float
) -> str:
    result = sender.AsyncResult(delivery_id)
    terminal = {"SUCCESS", "FAILURE", "RETRY", "REVOKED"}
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = await asyncio.to_thread(lambda: result.state)
        if state in terminal:
            return state
        await asyncio.sleep(0.25)
    return await asyncio.to_thread(lambda: result.state)


async def _wait_export_recovery(
    sender: Celery,
    test_engine,
    delivery_id: str,
    job_id: uuid.UUID,
    *,
    timeout: float,
) -> dict:
    result = sender.AsyncResult(delivery_id)
    deadline = asyncio.get_running_loop().time() + timeout
    latest = {}
    while asyncio.get_running_loop().time() < deadline:
        state = await asyncio.to_thread(lambda: result.state)
        job = await _job_snapshot(test_engine, job_id)
        latest = {"delivery_state": state, "job": job}
        if (job["status"] == "completed" and state == "SUCCESS") or state in {
            "FAILURE",
            "REVOKED",
        }:
            return latest
        # A short advisory-lock retry can occur while the killed backend exits.
        await asyncio.sleep(0.25)
    return latest


async def _wait_active_export_task(
    sender: Celery, worker, task_id: str, *, timeout: float = 30
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        inspect = sender.control.inspect(timeout=1, destination=[worker.node_name])
        active = await asyncio.to_thread(inspect.active)
        tasks = (active or {}).get(worker.node_name, [])
        match = next(
            (
                task
                for task in tasks
                if task.get("id") == task_id and task.get("name") == _TASK_NAME
            ),
            None,
        )
        if match is not None:
            return match
        await asyncio.sleep(0.25)
    raise AssertionError(f"export delivery {task_id} did not become active")


async def _wait_failure_delivery_ack(
    sender: Celery, worker, task_id: str, *, timeout: float = 30
) -> dict:
    result = sender.AsyncResult(task_id)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = await asyncio.to_thread(lambda: result.state)
        if state == "FAILURE":
            broker = await asyncio.to_thread(_broker_snapshot, sender, worker.queue)
            inspect = sender.control.inspect(timeout=1, destination=[worker.node_name])
            active = await asyncio.to_thread(inspect.active)
            reserved = await asyncio.to_thread(inspect.reserved)
            outstanding = [
                task
                for snapshot in (active or {}, reserved or {})
                for tasks in snapshot.values()
                for task in tasks or []
                if task.get("id") == task_id
            ]
            if (
                active is not None
                and reserved is not None
                and worker.node_name in active
                and worker.node_name in reserved
                and broker["queue_size"] == 0
                and broker["unacknowledged"] == 0
                and not outstanding
            ):
                return {"task_id": task_id, "state": state, **broker}
        await asyncio.sleep(0.25)
    raise AssertionError("Failed export was not acknowledged by the broker")


async def _create_fresh_job(
    test_engine, world, *, opts: dict | None = None
) -> uuid.UUID:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    job_id = uuid.uuid4()
    async with maker.begin() as db:
        project = await db.get(Project, world["project_id"])
        assert project is not None
        db.add(
            AsyncJob(
                id=job_id,
                kind="export",
                user_id=world["reviewer_id"],
                project_id=project.id,
                status=AsyncJobStatus.PENDING.value,
                progress_pct=0,
                payload=_new_job_payload(project, opts),
                result={},
            )
        )
    return job_id


async def _owned_objects(owner_s3, bucket: str, prefix: str) -> set[str]:
    paginator = owner_s3.get_paginator("list_objects_v2")
    return {
        item["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for item in page.get("Contents", [])
    }


def _delete_owned_object_prefix(owner_s3, bucket: str, prefix: str) -> None:
    paginator = owner_s3.get_paginator("list_object_versions")
    versions = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        versions.extend(
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in page.get("Versions", [])
        )
        versions.extend(
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in page.get("DeleteMarkers", [])
        )
    if not versions:
        for page in owner_s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix
        ):
            objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if objects:
                owner_s3.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": objects, "Quiet": True},
                )
        return
    for start in range(0, len(versions), 1000):
        owner_s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": versions[start : start + 1000], "Quiet": True},
        )


async def _artifact_snapshot(test_engine, project_id) -> dict:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        artifacts = list(
            (
                await db.execute(
                    select(ExportArtifact).where(
                        ExportArtifact.project_id == project_id
                    )
                )
            ).scalars()
        )
        return {
            "count": len(artifacts),
            "keys": sorted(item.object_key for item in artifacts),
            "cache_keys_sha256": sorted(
                hashlib.sha256(item.cache_key.encode()).hexdigest()
                for item in artifacts
            ),
        }


async def _notification_count(test_engine, job_ids: list[uuid.UUID]) -> int:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        return int(
            await db.scalar(
                select(func.count(Notification.id)).where(
                    Notification.target_type == "export",
                    Notification.target_id.in_(job_ids),
                )
            )
            or 0
        )


async def _notification_counts(test_engine, job_ids: list[uuid.UUID]) -> dict[str, int]:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        rows = (
            await db.execute(
                select(Notification.target_id, func.count(Notification.id))
                .where(
                    Notification.target_type == "export",
                    Notification.target_id.in_(job_ids),
                )
                .group_by(Notification.target_id)
            )
        ).all()
        return {str(job_id): int(count) for job_id, count in rows}


def _send_export(
    sender: Celery,
    worker,
    world,
    job_id,
    delivery_id: str,
    *,
    opts: dict | None = None,
):
    return sender.send_task(
        _TASK_NAME,
        args=[
            str(world["project_id"]),
            None,
            ["aap_json"],
            dict(_TASK_OPTS if opts is None else opts),
            str(job_id),
        ],
        queue=worker.queue,
        task_id=delivery_id,
    )


async def test_export_recovers_after_worker_loss(
    test_engine, tmp_path, record_property
):
    assert test_engine.url.database and str(test_engine.url.database).endswith("_test")
    _verify_postgres(test_engine.url)
    resources, _, owner_s3 = await assert_workspace_identity(test_engine)
    export_bucket = resources["buckets"]["MINIO_EXPORT_BUCKET"]
    minio_origin = endpoint_url()
    minio_parts = urlsplit(minio_origin)
    assert minio_parts.scheme in {"http", "https"} and minio_parts.hostname
    world = await _seed_export_world(test_engine)
    scope, destroy_scope = _create_export_storage_scope(
        resources, owner_s3, settings, world
    )
    worker = None
    sender = None
    gate = None
    gate_tx = None
    deliveries: dict[str, object] = {}
    fresh_job_id = None
    redis_before_kill = []
    redis_after_kill = []
    observation: dict = {"world": {key: str(value) for key, value in world.items()}}
    try:
        source_bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
        worker_default_bucket = resources["buckets"]["MINIO_BUCKET"]
        owner_s3.put_object(
            Bucket=source_bucket,
            Key=world["source_key"],
            Body=_SOURCE_PNG,
            ContentType="image/png",
        )
        source_readback = owner_s3.get_object(
            Bucket=source_bucket,
            Key=world["source_key"],
        )["Body"].read()
        assert source_readback == _SOURCE_PNG
        worker = WorkspaceCeleryWorker(
            test_engine,
            tmp_path,
            backend_token=secrets.token_urlsafe(32),
            object_store_endpoint=minio_origin,
            object_store_bucket=worker_default_bucket,
            object_store_use_ssl=minio_parts.scheme == "https",
            object_store_access_key=scope["access_key"],
            object_store_secret_key=scope["secret_key"],
            object_store_buckets=resources["buckets"],
        )
        if worker.object_store_access_key == settings.minio_access_key or (
            worker.object_store_secret_key == settings.minio_secret_key
        ):
            raise AssertionError(
                "Export worker received owner object storage credentials"
            )
        sender_url = os.environ.get("CELERY_BROKER_URL") or os.environ["REDIS_URL"]
        sender = Celery(
            "workspace-export-worker-sender", broker=sender_url, backend=sender_url
        )
        sender.conf.update(
            task_serializer="json",
            result_serializer="json",
            accept_content=["json"],
        )

        await _assert_workspace_consumer_closure(test_engine)
        revoked_delivery_id = str(uuid.uuid4())
        deliveries[revoked_delivery_id] = _send_export(
            sender,
            worker,
            world,
            world["revoked_job_id"],
            revoked_delivery_id,
        )
        queued_before_revocation = await asyncio.to_thread(
            _broker_snapshot, sender, worker.queue
        )
        assert queued_before_revocation["queue_size"] == 1
        from app.services.workspace_membership import remove_member

        maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with maker() as db:
            await remove_member(
                db,
                project_id=world["project_id"],
                actor_id=world["owner_id"],
                member_id=world["revoked_member_id"],
            )
        async with maker() as db:
            assert await db.get(ProjectMember, world["revoked_member_id"]) is None
        queued_after_revocation = await asyncio.to_thread(
            _broker_snapshot, sender, worker.queue
        )
        assert queued_after_revocation["queue_size"] == 1
        observation["queued_revocation"] = {
            "queue_before_remove": queued_before_revocation,
            "queue_after_remove": queued_after_revocation,
            "membership_removed_before_worker_start": True,
        }

        worker.start()
        await _wait_worker_ready(sender, worker)
        worker.assert_private_network()
        signature = worker.task_signature(_TASK_NAME)
        assert signature == {
            "task": _TASK_NAME,
            "parameters": [
                "project_id",
                "batch_id",
                "targets",
                "opts",
                "async_job_id",
                "task_ids",
            ],
        }
        delivery_policy = worker.task_delivery_policy(_TASK_NAME)
        assert delivery_policy == {
            "acks_late": True,
            "reject_on_worker_lost": True,
        }
        observation["task_delivery_policy"] = delivery_policy
        revoked_outcome = await _wait_delivery_and_job(
            sender,
            test_engine,
            revoked_delivery_id,
            world["revoked_job_id"],
            timeout=30,
        )
        if revoked_outcome["delivery_state"] != "FAILURE":
            failure_state = await _wait_delivery_state(
                sender, revoked_delivery_id, timeout=20
            )
            revoked_outcome["delivery_state_after_wait"] = failure_state
        assert revoked_outcome["job"]["status"] == "failed", revoked_outcome
        assert (
            revoked_outcome.get(
                "delivery_state_after_wait", revoked_outcome["delivery_state"]
            )
            == "FAILURE"
        ), revoked_outcome
        assert "download_url_path" not in revoked_outcome["job"]["result"]
        revoked_ack = await _wait_failure_delivery_ack(
            sender, worker, revoked_delivery_id
        )
        assert (await _artifact_snapshot(test_engine, world["project_id"]))[
            "count"
        ] == 0
        assert (
            await _owned_objects(
                owner_s3, export_bucket, f"image/{world['project_id']}/"
            )
            == set()
        )
        observation["queued_revocation"].update(
            {
                "job": revoked_outcome["job"],
                "broker_ack": revoked_ack,
                "notifications": await _notification_count(
                    test_engine, [world["revoked_job_id"]]
                ),
            }
        )
        assert observation["queued_revocation"]["notifications"] == 1
        inspect = sender.control.inspect(timeout=2, destination=[worker.node_name])
        registered = await asyncio.to_thread(inspect.registered)
        active_queues = await asyncio.to_thread(inspect.active_queues)
        assert registered and _TASK_NAME in registered[worker.node_name]
        assert active_queues and {
            row["name"] for row in active_queues[worker.node_name]
        } == {worker.queue}
        observation["runtime_before_loss"] = worker.assert_runtime()
        observation["task_signature"] = signature
        observation["queue"] = worker.queue
        redis_before_kill = _export_lock_snapshot(sender)

        gate = await test_engine.connect()
        gate_tx = await gate.begin()
        await gate.execute(
            text("LOCK TABLE public.predictions IN ACCESS EXCLUSIVE MODE")
        )
        first_delivery_id = str(uuid.uuid4())
        deliveries[first_delivery_id] = _send_export(
            sender, worker, world, world["job_id"], first_delivery_id
        )
        gate_evidence = await _wait_export_build_gate(test_engine, worker, world)
        observation["build_gate"] = gate_evidence
        assert gate_evidence["job"]["celery_task_id"] == first_delivery_id
        assert gate_evidence["activity"]["usename"] == "identity_api_consumer"
        assert gate_evidence["activity"]["datname"] == test_engine.url.database
        assert (
            gate_evidence["activity"]["client_addr"]
            == gate_evidence["worker_runtime"]["database_client_address"]
        )
        backend_pid = int(gate_evidence["activity"]["pid"])
        observation["postgres_locks_before_kill"] = await _postgres_lock_snapshot(
            test_engine, backend_pid
        )
        observation["artifact_before_loss"] = await _artifact_snapshot(
            test_engine, world["project_id"]
        )
        observation["notifications_before_loss"] = await _notification_count(
            test_engine, [world["job_id"]]
        )
        redis_at_gate = _export_lock_snapshot(sender)
        observation["redis_lock_before_kill"] = {
            "baseline": redis_before_kill,
            "current": redis_at_gate,
            "new": _new_export_locks(redis_before_kill, redis_at_gate),
        }
        observation["broker_before_kill"] = await asyncio.to_thread(
            _broker_snapshot, sender, worker.queue
        )
        observation["job_before_kill"] = await _job_snapshot(
            test_engine, world["job_id"]
        )

        kill_record = worker.kill()
        observation["hard_loss"] = kill_record
        observation[
            "postgres_locks_after_kill_while_gate_held"
        ] = await _postgres_lock_snapshot(test_engine, backend_pid)
        observation["job_after_kill_while_gate_held"] = await _job_snapshot(
            test_engine, world["job_id"]
        )
        observation["artifact_after_kill_while_gate_held"] = await _artifact_snapshot(
            test_engine, world["project_id"]
        )
        observation[
            "notifications_after_kill_while_gate_held"
        ] = await _notification_count(test_engine, [world["job_id"]])
        observation["broker_after_kill_while_gate_held"] = await asyncio.to_thread(
            _broker_snapshot, sender, worker.queue
        )
        observation["first_delivery_state_after_kill"] = await asyncio.to_thread(
            lambda: sender.AsyncResult(first_delivery_id).state
        )
        redis_after_kill = _export_lock_snapshot(sender)
        observation["redis_lock_after_kill_while_gate_held"] = {
            "current": redis_after_kill,
            "new": _new_export_locks(redis_before_kill, redis_after_kill),
        }
        await gate_tx.rollback()
        gate_tx = None
        await gate.close()
        gate = None
        # A blocked backend may not notice its dead client until the query wakes.
        await _wait_backend_gone(test_engine, backend_pid)
        observation["lost_backend_gone_after_gate_release"] = True
        observation[
            "postgres_locks_after_gate_release"
        ] = await _postgres_lock_snapshot(test_engine, backend_pid)

        worker.restart()
        await _wait_worker_ready(sender, worker)
        runtime_after_restart = worker.assert_runtime()
        observation["runtime_after_restart"] = runtime_after_restart
        assert (
            runtime_after_restart["container_id"]
            == observation["runtime_before_loss"]["container_id"]
        )
        assert (
            runtime_after_restart["image_id"]
            == observation["runtime_before_loss"]["image_id"]
        )
        assert (
            runtime_after_restart["started_at"]
            != observation["runtime_before_loss"]["started_at"]
        )
        assert worker.task_signature(_TASK_NAME) == signature
        restarted_inspect = sender.control.inspect(
            timeout=2, destination=[worker.node_name]
        )
        restarted_queues = await asyncio.to_thread(restarted_inspect.active_queues)
        assert restarted_queues and {
            row["name"] for row in restarted_queues[worker.node_name]
        } == {worker.queue}

        new_delivery_id = str(uuid.uuid4())
        deliveries[new_delivery_id] = _send_export(
            sender, worker, world, world["job_id"], new_delivery_id
        )
        observation["same_job_new_delivery"] = await _wait_delivery_and_job(
            sender, test_engine, new_delivery_id, world["job_id"], timeout=75
        )
        new_delivery_state = await _wait_delivery_state(
            sender, new_delivery_id, timeout=75
        )
        observation["same_job_new_delivery"]["delivery_state_after_wait"] = (
            new_delivery_state
        )
        assert new_delivery_state == "SUCCESS", observation["same_job_new_delivery"]
        observation["job_after_new_delivery"] = await _job_snapshot(
            test_engine, world["job_id"]
        )
        assert observation["job_after_new_delivery"]["status"] == "completed"
        assert (
            observation["job_after_new_delivery"]["celery_task_id"] == new_delivery_id
        )

        original_restore = _restore_owned_unacked_export(
            sender, worker, first_delivery_id
        )
        observation["same_job_original_delivery_restore"] = original_restore
        original_delivery_state = await _wait_delivery_state(
            sender, first_delivery_id, timeout=45
        )
        observation["same_job_original_delivery_state"] = original_delivery_state
        assert original_delivery_state == "SUCCESS", observation
        observation["same_job_original_delivery_ack"] = await _wait_broker_ack(
            sender, worker, first_delivery_id
        )
        observation["same_job_new_delivery_ack"] = await _wait_broker_ack(
            sender, worker, new_delivery_id
        )
        observation["same_job_deliveries_broker"] = await asyncio.to_thread(
            _broker_snapshot, sender, worker.queue
        )

        fresh_job_id = await _create_fresh_job(test_engine, world)
        fresh_delivery_id = str(uuid.uuid4())
        deliveries[fresh_delivery_id] = _send_export(
            sender, worker, world, fresh_job_id, fresh_delivery_id
        )
        observation["fresh_same_scope_job"] = await _wait_delivery_and_job(
            sender, test_engine, fresh_delivery_id, fresh_job_id, timeout=35
        )
        if observation["fresh_same_scope_job"]["delivery_state"] != "SUCCESS":
            fresh_state_after_wait = await _wait_delivery_state(
                sender, fresh_delivery_id, timeout=20
            )
            observation["fresh_same_scope_job"]["delivery_state_after_wait"] = (
                fresh_state_after_wait
            )
        if (
            observation["fresh_same_scope_job"].get(
                "delivery_state_after_wait",
                observation["fresh_same_scope_job"]["delivery_state"],
            )
            == "SUCCESS"
        ):
            observation["fresh_same_scope_job_ack"] = await _wait_broker_ack(
                sender, worker, fresh_delivery_id
            )
        observation["fresh_same_scope_broker"] = await asyncio.to_thread(
            _broker_snapshot, sender, worker.queue
        )
        fresh_state = observation["fresh_same_scope_job"].get(
            "delivery_state_after_wait",
            observation["fresh_same_scope_job"]["delivery_state"],
        )
        observation["fresh_job_before_revoke"] = await _job_snapshot(
            test_engine, fresh_job_id
        )
        if fresh_state == "RETRY":
            await asyncio.to_thread(
                sender.control.revoke,
                fresh_delivery_id,
                terminate=False,
                destination=[worker.node_name],
            )

        job_snapshots = {
            "recovered": await _job_snapshot(test_engine, world["job_id"]),
            "fresh": await _job_snapshot(test_engine, fresh_job_id),
        }
        observation["final_jobs"] = job_snapshots
        observation["final_artifacts"] = await _artifact_snapshot(
            test_engine, world["project_id"]
        )
        notification_counts = await _notification_counts(
            test_engine, [world["job_id"], fresh_job_id]
        )
        observation["final_notification_counts"] = notification_counts
        observation["final_notifications"] = sum(notification_counts.values())
        export_prefix = f"image/{world['project_id']}/"
        export_objects = await _owned_objects(owner_s3, export_bucket, export_prefix)
        observation["final_export_object_keys"] = sorted(export_objects)
        observation["redis_lock_final"] = _export_lock_snapshot(sender)

        assert kill_record.get("exit_code") == 137, kill_record
        assert observation["job_after_kill_while_gate_held"]["status"] == "running"
        assert observation["job_after_kill_while_gate_held"]["progress_pct"] == 10
        assert observation["artifact_after_kill_while_gate_held"]["count"] == 0
        assert observation["notifications_after_kill_while_gate_held"] == 0
        recovered = job_snapshots["recovered"]
        fresh = job_snapshots["fresh"]
        assert recovered["status"] == "completed", observation
        assert fresh["status"] == "completed", observation
        assert recovered["result"].get("cache_hit") is False, observation
        assert fresh["result"].get("cache_hit") is True, observation
        assert recovered["result"].get("object_key") == fresh["result"].get(
            "object_key"
        )
        assert observation["final_artifacts"]["count"] == 1, observation
        assert notification_counts == {
            str(world["job_id"]): 1,
            str(fresh_job_id): 1,
        }, observation
        expected_key = recovered["result"].get("object_key")
        export_prefix = f"image/{world['project_id']}/"
        assert isinstance(expected_key, str)
        assert expected_key.startswith(export_prefix)
        assert expected_key.endswith(".zip")
        assert (
            str(world["job_id"]) in expected_key or world["job_id"].hex in expected_key
        )
        assert export_objects == {expected_key}, observation

        download_url = await _job_download_url(test_engine, world["job_id"])
        parsed_url = urlsplit(download_url)
        assert f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}".startswith(
            minio_origin
        ), observation
        import httpx

        response = await asyncio.to_thread(httpx.get, download_url, timeout=20)
        response.raise_for_status()
        body = response.content
        assert body, "signed export URL returned an empty artifact"
        with zipfile.ZipFile(BytesIO(body)) as archive:
            names = set(archive.namelist())
            assert {
                "annotations.json",
                "images_manifest.json",
                "classes.txt",
                "data_contract.json",
            } <= names
            payload = json.loads(archive.read("annotations.json"))
            manifest = json.loads(archive.read("images_manifest.json"))
        assert (
            payload["exported_from"]["project_display_id"]
            == world["project_display_id"]
        )
        assert len(payload["tasks"]) == 1
        task_block = payload["tasks"][0]
        assert task_block["task_match"]["display_id"] == world["task_display_id"]
        assert len(task_block["annotations"]) == 1
        assert task_block["annotations"][0]["class_name"] == "car"
        assert task_block["annotations"][0]["user_id"] == str(world["reviewer_id"])
        assert len(manifest["images"]) == 1
        image_entry = manifest["images"][0]
        assert image_entry["task_id"] == str(world["task_id"])
        assert world["source_key"] in urlsplit(image_entry["presigned_url"]).path
        source_response = await asyncio.to_thread(
            httpx.get, image_entry["presigned_url"], timeout=20
        )
        source_response.raise_for_status()
        assert source_response.content == _SOURCE_PNG
        observation["artifact"] = {
            "download_status": response.status_code,
            "source_download_status": source_response.status_code,
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
            "zip_members": sorted(names),
            "task_count": len(payload["tasks"]),
            "annotation_count": len(task_block["annotations"]),
        }

        child_opts = {**_TASK_OPTS, "include_attributes": False}
        child_job_id = await _create_fresh_job(test_engine, world, opts=child_opts)
        child_world = {**world, "job_id": child_job_id}
        child_delivery_id = str(uuid.uuid4())
        child_runtime_before = worker.assert_runtime()
        gate = await test_engine.connect()
        gate_tx = await gate.begin()
        await gate.execute(
            text("LOCK TABLE public.predictions IN ACCESS EXCLUSIVE MODE")
        )
        deliveries[child_delivery_id] = _send_export(
            sender,
            worker,
            child_world,
            child_job_id,
            child_delivery_id,
            opts=child_opts,
        )
        child_gate = await _wait_export_build_gate(test_engine, worker, child_world)
        child_active = await _wait_active_export_task(sender, worker, child_delivery_id)
        child_worker_pid = int(child_active["worker_pid"])
        child_database_pid = int(child_gate["activity"]["pid"])
        assert child_active["name"] == _TASK_NAME
        child_kill_record = worker.kill_prefork_child(child_worker_pid)
        child_runtime_after_kill = worker.assert_runtime()
        observation["prefork_child_loss"] = {
            "job_id": str(child_job_id),
            "delivery_id": child_delivery_id,
            "worker_pid": child_worker_pid,
            "database_pid": child_database_pid,
            "kill": child_kill_record,
            "runtime_before": child_runtime_before,
            "runtime_after_kill": child_runtime_after_kill,
        }
        assert (
            child_runtime_after_kill["container_id"]
            == child_runtime_before["container_id"]
        )
        assert (
            child_runtime_after_kill["started_at"] == child_runtime_before["started_at"]
        )

        await gate_tx.rollback()
        gate_tx = None
        await gate.close()
        gate = None
        await _wait_backend_gone(test_engine, child_database_pid)
        observation["prefork_child_loss"]["lost_backend_gone_after_gate_release"] = True
        try:
            observation["prefork_child_loss"][
                "replacement_active"
            ] = await _wait_active_export_task(
                sender, worker, child_delivery_id, timeout=2
            )
        except AssertionError:
            observation["prefork_child_loss"]["replacement_active"] = None
        observation["prefork_child_loss"]["logs_after_kill"] = (
            worker.capture_diagnostics()
        )

        child_recovery = await _wait_export_recovery(
            sender,
            test_engine,
            child_delivery_id,
            child_job_id,
            timeout=120,
        )
        child_delivery_state = child_recovery.get("delivery_state")
        child_job_snapshot = child_recovery.get("job", {})
        observation["prefork_child_loss"]["delivery_state"] = child_delivery_state
        observation["prefork_child_loss"]["job"] = child_job_snapshot
        assert child_delivery_state == "SUCCESS", observation["prefork_child_loss"]
        assert child_job_snapshot["status"] == "completed", child_job_snapshot
        assert child_job_snapshot["celery_task_id"] == child_delivery_id
        assert child_job_snapshot["result"].get("cache_hit") is False
        child_runtime_after_completion = worker.assert_runtime()
        observation["prefork_child_loss"]["runtime_after_completion"] = (
            child_runtime_after_completion
        )
        assert (
            child_runtime_after_completion["container_id"]
            == child_runtime_before["container_id"]
        )
        assert (
            child_runtime_after_completion["started_at"]
            == child_runtime_before["started_at"]
        )
        observation["prefork_child_loss"]["broker_ack"] = await _wait_broker_ack(
            sender, worker, child_delivery_id
        )
        child_artifacts = await _artifact_snapshot(test_engine, world["project_id"])
        observation["prefork_child_loss"]["artifacts"] = child_artifacts
        assert child_artifacts["count"] == 2
        assert child_job_snapshot["result"].get("object_key") in child_artifacts["keys"]
        child_notifications = await _notification_count(test_engine, [child_job_id])
        observation["prefork_child_loss"]["notifications"] = child_notifications
        assert child_notifications == 1
        child_objects = await _owned_objects(
            owner_s3, export_bucket, f"image/{world['project_id']}/"
        )
        observation["prefork_child_loss"]["export_objects"] = sorted(child_objects)
        child_key = child_job_snapshot["result"].get("object_key")
        assert child_key in child_objects
        assert len(child_objects) == 2
        child_attempt_objects = [
            key
            for key in child_objects
            if str(child_job_id) in key or child_job_id.hex in key
        ]
        assert child_attempt_objects == [child_key]
        observation["final_worker_diagnostics"] = worker.capture_diagnostics()

        record_property(
            "workspace_export_hard_loss_probe", json.dumps(observation, default=str)
        )
    except BaseException:
        if worker is not None:
            with suppress(Exception):
                observation["failure_diagnostics"] = worker.capture_diagnostics()
                observation["runtime_on_failure"] = worker.assert_runtime()
        if sender is not None:
            for delivery_id in deliveries:
                with suppress(Exception):
                    result = sender.AsyncResult(delivery_id)
                    observation.setdefault("failure_delivery_states", {})[
                        delivery_id
                    ] = {
                        "state": result.state,
                        "result": worker.redact(repr(result.result))
                        if worker
                        else type(result.result).__name__,
                    }
        record_property(
            "workspace_export_hard_loss_probe", json.dumps(observation, default=str)
        )
        raise
    finally:
        cleanup_outcomes: dict[str, object] = {}
        if gate_tx is not None:
            with suppress(Exception):
                if worker is not None and worker.container_id is not None:
                    with suppress(Exception):
                        worker.kill()
                await gate_tx.rollback()
                cleanup_outcomes["gate_released"] = True
        if gate is not None:
            with suppress(Exception):
                await gate.close()
        if sender is not None and worker is not None:
            for delivery_id in deliveries:
                with suppress(Exception):
                    sender.control.revoke(
                        delivery_id,
                        terminate=True,
                        destination=[worker.node_name],
                    )
        if worker is not None:
            try:
                worker.stop()
                cleanup_outcomes["worker_stopped"] = True
                cleanup_outcomes["container_id_after_stop"] = worker.container_id
                cleanup_outcomes["network_id_after_stop"] = worker.network_id
            except Exception as exc:
                cleanup_outcomes["worker_stop_error"] = type(exc).__name__
        if sender is not None and worker is not None:
            try:
                await asyncio.to_thread(_delete_owned_queue, sender, worker.queue)
                cleanup_outcomes["queue_deleted"] = True
            except Exception as exc:
                cleanup_outcomes["queue_delete_error"] = type(exc).__name__
            for delivery_id in deliveries:
                try:
                    await asyncio.to_thread(sender.AsyncResult(delivery_id).forget)
                except Exception as exc:
                    cleanup_outcomes.setdefault("result_forget_errors", {})[
                        delivery_id
                    ] = type(exc).__name__
            try:
                sender.close()
                cleanup_outcomes["sender_closed"] = True
            except Exception as exc:
                cleanup_outcomes["sender_close_error"] = type(exc).__name__
        try:
            _delete_owned_object_prefix(owner_s3, source_bucket, world["source_key"])
            _delete_owned_object_prefix(
                owner_s3, export_bucket, f"image/{world['project_id']}/"
            )
            cleanup_outcomes["owned_objects_deleted"] = True
        except Exception as exc:
            cleanup_outcomes["object_cleanup_error"] = type(exc).__name__
        try:
            destroy_scope(scope)
            cleanup_outcomes["storage_scope_destroyed"] = True
        except Exception as exc:
            cleanup_outcomes["storage_scope_destroy_error"] = type(exc).__name__
        record_property(
            "workspace_export_hard_loss_cleanup",
            json.dumps(cleanup_outcomes, default=str),
        )
        assert cleanup_outcomes.get("worker_stop_error") is None, cleanup_outcomes
        assert cleanup_outcomes.get("container_id_after_stop") is None, cleanup_outcomes
        assert cleanup_outcomes.get("network_id_after_stop") is None, cleanup_outcomes
        assert cleanup_outcomes.get("queue_delete_error") is None, cleanup_outcomes
        assert cleanup_outcomes.get("result_forget_errors", {}) == {}, cleanup_outcomes
        assert cleanup_outcomes.get("sender_close_error") is None, cleanup_outcomes
        assert cleanup_outcomes.get("object_cleanup_error") is None, cleanup_outcomes
        assert cleanup_outcomes.get("owned_objects_deleted") is True, cleanup_outcomes
        assert cleanup_outcomes.get("storage_scope_destroy_error") is None, (
            cleanup_outcomes
        )
        assert cleanup_outcomes.get("storage_scope_destroyed") is True, cleanup_outcomes
