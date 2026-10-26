from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from aap_protocol_v2 import (
    CocoRlePayload,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
    native_mask_candidate_id,
)
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import (
    AnnotationLineageEdge,
    AnnotationOperation,
)
from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, DatasetItem, VideoSegment
from app.db.models.mask_format_import import MaskFormatImport
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.notification import Notification
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.user import User
from app.services.mask_qc.topology import rle_and_not
from app.services.raster_mask_storage import (
    build_rle_reference,
    load_coco_rle,
    reserve_raster_mask_upload,
    store_mask_reference_objects,
)
from app.services.storage import storage_service
from app.utils.raster_mask_rle import decode_coco_rle, encode_coco_rle
from tests.conftest import create_registry_with_pool
from tests.factory import create_user


def _rle(rows: list[str]) -> dict[str, Any]:
    height = len(rows)
    width = len(rows[0])
    return encode_coco_rle(
        [1 if pixel == "#" else 0 for row in rows for pixel in row], width, height
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _api_client(app, sessions, *, dispatcher=None):
    from app.deps import get_db, get_quality_job_dispatcher

    previous = dict(app.dependency_overrides)

    async def override_db():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    if dispatcher is None:
        app.dependency_overrides.pop(get_quality_job_dispatcher, None)
    else:
        app.dependency_overrides[get_quality_job_dispatcher] = lambda: dispatcher
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


async def _seed_world(
    sessions,
    *,
    count: int = 1,
    data_type: str = "image",
    statuses: list[str] | None = None,
) -> dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    statuses = statuses or ["review"] * count
    async with sessions() as db:
        owner = await create_user(
            db, "super_admin", f"mask-runtime-{suffix}@test.local", "Mask Runtime"
        )
        project = Project(
            display_id=f"P-MRT-{suffix}",
            name=f"Mask runtime {suffix}",
            type_label="视频追踪" if data_type == "video" else "图像分割",
            type_key="video-track" if data_type == "video" else "image-seg",
            data_type=data_type,
            owner_id=owner.id,
            raster_mask_native_editing_enabled=True,
            tool_bindings={
                "region": {
                    "enabled": True,
                    "classes": [{"name": "object", "order": 0}],
                    "attribute_schema": {"fields": []},
                }
            },
        )
        dataset = Dataset(
            display_id=f"D-MRT-{suffix}",
            name=f"Mask runtime dataset {suffix}",
            data_type=data_type,
            created_by=owner.id,
        )
        db.add_all([project, dataset])
        await db.flush()
        item_ids: list[uuid.UUID] = []
        task_ids: list[uuid.UUID] = []
        display_ids: list[str] = []
        for index in range(count):
            extension = "mp4" if data_type == "video" else "png"
            item = DatasetItem(
                dataset_id=dataset.id,
                file_name=f"frame-{index}.{extension}",
                file_path=f"mask-runtime/{suffix}/frame-{index}.{extension}",
                file_type=data_type,
                width=5,
                height=3,
                metadata_=(
                    {"video": {"width": 5, "height": 3, "frame_count": 3, "fps": 10}}
                    if data_type == "video"
                    else {}
                ),
            )
            db.add(item)
            await db.flush()
            display_id = f"T-MRT-{suffix}-{index}"
            task = Task(
                project_id=project.id,
                dataset_item_id=item.id,
                display_id=display_id,
                file_name=item.file_name,
                file_path=item.file_path,
                file_type=data_type,
                status=statuses[index],
                reviewer_id=owner.id,
            )
            db.add(task)
            await db.flush()
            item_ids.append(item.id)
            task_ids.append(task.id)
            display_ids.append(display_id)
        await db.commit()
    return {
        "suffix": suffix,
        "owner_id": owner.id,
        "token": create_access_token(subject=str(owner.id), role="super_admin"),
        "user_ids": [owner.id],
        "project_id": project.id,
        "dataset_id": dataset.id,
        "item_ids": item_ids,
        "task_ids": task_ids,
        "display_ids": display_ids,
        "mask_keys": set(),
        "staged_keys": set(),
        "backend_id": None,
        "pool_id": None,
    }


async def _store_rles(db, world, task_id, values: list[dict[str, Any]]) -> list[dict]:
    references = [(build_rle_reference(rle), rle) for rle in values]
    for reference, _rle_value in references:
        await reserve_raster_mask_upload(
            db, task_id=task_id, object_key=reference["object_key"], limit=1000
        )
        world["mask_keys"].add(reference["object_key"])
    await store_mask_reference_objects(
        db,
        {"references": [reference for reference, _rle_value in references]},
        references,
        task_id=task_id,
    )
    return [reference for reference, _rle_value in references]


async def _add_repair_issues(
    sessions,
    world,
    *,
    task_indexes: list[int],
    code: str = "small_island",
) -> list[uuid.UUID]:
    issue_ids: list[uuid.UUID] = []
    async with sessions() as db:
        author = await create_user(
            db,
            "employee",
            f"mask-author-{world['suffix']}@test.local",
            "Mask Author",
        )
        world["user_ids"].append(author.id)
        for index in task_indexes:
            task_id = world["task_ids"][index]
            task = await db.get(Task, task_id)
            task.reviewer_id = world["owner_id"]
            task.annotation_contributor_ids = [str(author.id)]
            task.review_contributor_ids = [str(author.id)]
            task.review_submitter_id = author.id
            task.review_round_id = uuid.uuid4()
            source = _rle([".....", ".###.", "....#"])
            region = _rle([".....", ".....", "....#"])
            source_ref, region_ref = await _store_rles(
                db, world, task_id, [source, region]
            )
            annotation = Annotation(
                task_id=task_id,
                project_id=world["project_id"],
                user_id=author.id,
                source="manual",
                annotation_type="raster_mask",
                tool_unit_id="region",
                class_name="object",
                geometry={"type": "raster_mask", "mask": source_ref},
                is_active=True,
                was_cancelled=False,
                version=1,
            )
            db.add(annotation)
            await db.flush()
            issue = MaskQCIssue(
                project_id=world["project_id"],
                task_id=task_id,
                annotation_id=annotation.id,
                annotation_version=1,
                related_annotation_ids=[annotation.id],
                source_versions={str(annotation.id): 1},
                code=code,
                severity="warning",
                severity_rank=1,
                status="open",
                metric={"area_pixels": 1},
                threshold={"small_component_pixels": 32},
                region_bbox={"x0": 0.8, "y0": 2 / 3, "x1": 1.0, "y1": 1.0},
                region_mask_ref=region_ref,
                region_digest=region_ref["sha256"],
                dedupe_key=hashlib.sha256(
                    f"mask-runtime-{world['suffix']}-{index}".encode()
                ).hexdigest(),
                source={"kind": "single_frame"},
            )
            db.add(issue)
            await db.flush()
            issue_ids.append(issue.id)
        await db.commit()
    return issue_ids


async def _dry_run(client, world, actions):
    response = await client.post(
        f"/api/v1/projects/{world['project_id']}/mask-qc/repairs:dry-run",
        headers=_headers(world["token"]),
        json={"actions": actions},
    )
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["summary"]["executable_count"] == len(actions), (
        plan["items"][0].get("skip_code"),
        plan["items"][0].get("skip_detail"),
    )
    return plan


async def _execute_repair(client, world, plan):
    response = await client.post(
        f"/api/v1/projects/{world['project_id']}/mask-qc/repairs",
        headers=_headers(world["token"]),
        json={"receipt": plan["receipt"], "plan_digest": plan["plan_digest"]},
    )
    assert response.status_code == 202, response.text
    return response.json()


async def _wait_for_row(sessions, model, identity, field, terminal, timeout=45):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        async with sessions() as db:
            row = await db.get(model, identity)
            if row is not None and getattr(row, field) in terminal:
                return row
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {model.__name__}.{field}={terminal}")


async def _wait_for_advisory_lock(engine, class_id: int, object_id: int, timeout=10):
    deadline = asyncio.get_running_loop().time() + timeout
    statement = text(
        "SELECT EXISTS (SELECT 1 FROM pg_locks "
        "WHERE locktype = 'advisory' AND classid = CAST(:class_id AS oid) "
        "AND objid = CAST(:object_id AS oid) AND objsubid = 2 AND granted)"
    )
    while asyncio.get_running_loop().time() < deadline:
        async with engine.connect() as connection:
            found = await connection.scalar(
                statement, {"class_id": class_id, "object_id": object_id}
            )
        if found:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("worker did not enter the guarded real-consumer operation")


async def _wait_for_lock_wait(engine, query_part: str, timeout=2):
    deadline = asyncio.get_running_loop().time() + timeout
    statement = text(
        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
        "AND query ILIKE :query_part)"
    )
    while asyncio.get_running_loop().time() < deadline:
        async with engine.connect() as connection:
            found = await connection.scalar(
                statement, {"query_part": f"%{query_part}%"}
            )
        if found:
            return
        await asyncio.sleep(0.03)
    raise AssertionError(f"expected a real PostgreSQL lock wait on {query_part}")


def _create_trigger_sql(
    trigger: str, function: str, table: str, body: str
) -> list[str]:
    return [
        f'CREATE FUNCTION "{function}"() RETURNS trigger LANGUAGE plpgsql AS $body$ {body} $body$',
        f'CREATE TRIGGER "{trigger}" BEFORE INSERT ON {table} '
        f'FOR EACH ROW EXECUTE FUNCTION "{function}"()',
    ]


async def _install_trigger(engine, trigger: str, function: str, table: str, body: str):
    async with engine.begin() as connection:
        for statement in _create_trigger_sql(trigger, function, table, body):
            await connection.execute(text(statement))


async def _drop_trigger(engine, trigger: str, function: str, table: str):
    async with engine.begin() as connection:
        await connection.execute(text(f'DROP TRIGGER IF EXISTS "{trigger}" ON {table}'))
        await connection.execute(text(f'DROP FUNCTION IF EXISTS "{function}"()'))


async def _stage_aap_import(client, world, task_indexes: list[int]) -> dict[str, Any]:
    init = await client.post(
        f"/api/v1/projects/{world['project_id']}/mask-formats/imports:upload-init",
        headers=_headers(world["token"]),
        json={"file_name": "runtime-aap.json", "content_type": "application/json"},
    )
    assert init.status_code == 200, init.text
    object_key = init.json()["object_key"]
    world["staged_keys"].add(object_key)
    mask_objects: dict[str, dict] = {}
    staged_tasks: list[dict] = []
    expected: dict[uuid.UUID, dict] = {}
    for index in task_indexes:
        rle = _rle(
            [
                "#...." if index % 2 == 0 else ".#...",
                "..#..",
                "...##",
            ]
        )
        reference = build_rle_reference(rle)
        world["mask_keys"].add(reference["object_key"])
        mask_objects[reference["sha256"]] = rle
        task_id = world["task_ids"][index]
        expected[task_id] = rle
        staged_tasks.append(
            {
                "task_match": {"display_id": world["display_ids"][index]},
                "file_path": f"mask-runtime/{world['suffix']}/frame-{index}.png",
                "media_type": "image",
                "annotations": [
                    {
                        "geometry": {"type": "raster_mask", "mask": reference},
                        "class_name": "object",
                        "tool_unit_id": "region",
                        "source": "manual",
                    }
                ],
            }
        )
    payload = json.dumps(
        {"schema_version": "1.3", "mask_objects": mask_objects, "tasks": staged_tasks},
        separators=(",", ":"),
    ).encode()
    storage_service.client.put_object(
        Bucket=storage_service.import_bucket,
        Key=object_key,
        Body=payload,
        ContentType="application/json",
    )
    preflight = await client.post(
        f"/api/v1/projects/{world['project_id']}/mask-formats/imports:preflight",
        headers=_headers(world["token"]),
        json={
            "format_id": "aap_json",
            "staged_object_key": object_key,
            "staged_sha256": hashlib.sha256(payload).hexdigest(),
            "mapping": {},
            "options": {},
        },
    )
    assert preflight.status_code == 200, preflight.text
    return {
        "preflight": preflight.json(),
        "expected": expected,
        "object_key": object_key,
    }


async def _execute_import(client, world, staged, *, expected_status=202):
    preflight = staged["preflight"]
    response = await client.post(
        f"/api/v1/projects/{world['project_id']}/mask-formats/imports",
        headers=_headers(world["token"]),
        json={
            "receipt": preflight["receipt"],
            "plan_digest": preflight["plan"]["plan_digest"],
            "confirm_lossy": False,
        },
    )
    assert response.status_code == expected_status, response.text
    return response


class _UnavailableDispatcher:
    def dispatch_run(self, kind, run_id, *, execution_id):
        raise RuntimeError("dispatch intentionally unavailable")

    def dispatch_mask_repair(self, batch_id, *, rollback, execution_id):
        raise RuntimeError("dispatch intentionally unavailable")

    def dispatch_mask_import(self, batch_id, *, execution_id):
        raise RuntimeError("dispatch intentionally unavailable")


async def _cleanup_world(sessions, world):
    if world is None:
        return
    task_ids = world["task_ids"]
    project_id = world["project_id"]
    user_ids = world["user_ids"]
    async with sessions() as db:
        prediction_ids = select(Prediction.id).where(
            Prediction.project_id == project_id
        )
        operation_ids = select(AnnotationOperation.id).where(
            AnnotationOperation.task_id.in_(task_ids)
        )
        await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
        await db.execute(delete(Notification).where(Notification.user_id.in_(user_ids)))
        await db.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
        await db.execute(
            delete(PredictionMeta).where(
                PredictionMeta.prediction_id.in_(prediction_ids)
            )
        )
        await db.execute(delete(Prediction).where(Prediction.project_id == project_id))
        await db.execute(
            delete(AnnotationLineageEdge).where(
                AnnotationLineageEdge.operation_id.in_(operation_ids)
            )
        )
        await db.execute(
            delete(AnnotationOperation).where(AnnotationOperation.task_id.in_(task_ids))
        )
        await db.execute(
            delete(MaskQCIssue).where(MaskQCIssue.project_id == project_id)
        )
        await db.execute(
            delete(MaskRepairBatch).where(MaskRepairBatch.project_id == project_id)
        )
        await db.execute(
            delete(MaskFormatImport).where(MaskFormatImport.project_id == project_id)
        )
        await db.execute(delete(AsyncJob).where(AsyncJob.project_id == project_id))
        await db.execute(
            delete(ProjectMLBackendPool).where(
                ProjectMLBackendPool.project_id == project_id
            )
        )
        await db.execute(
            delete(ProjectMember).where(ProjectMember.project_id == project_id)
        )
        await db.execute(delete(Annotation).where(Annotation.project_id == project_id))
        await db.execute(
            delete(VideoSegment).where(
                VideoSegment.dataset_item_id.in_(world["item_ids"])
            )
        )
        await db.execute(delete(Task).where(Task.project_id == project_id))
        await db.execute(
            delete(DatasetItem).where(DatasetItem.id.in_(world["item_ids"]))
        )
        await db.execute(delete(Dataset).where(Dataset.id == world["dataset_id"]))
        await db.execute(delete(Project).where(Project.id == project_id))
        if world["pool_id"] is not None:
            await db.execute(
                delete(MLBackendPoolMember).where(
                    MLBackendPoolMember.pool_id == world["pool_id"]
                )
            )
            await db.execute(
                delete(MLBackendServicePool).where(
                    MLBackendServicePool.id == world["pool_id"]
                )
            )
        if world["backend_id"] is not None:
            from app.db.models.ml_backend_registry import MLBackendRegistry

            await db.execute(
                delete(MLBackendRegistry).where(
                    MLBackendRegistry.id == world["backend_id"]
                )
            )
        await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.commit()
    for key in world["mask_keys"]:
        storage_service.delete_object(key)
    for key in world["staged_keys"]:
        storage_service.delete_object(key, bucket=storage_service.import_bucket)


@pytest.mark.asyncio
async def test_repair_celery_consumer_commits_real_mask_and_rolls_back(
    test_engine, app_module, record_property
):
    sessions = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    world = await _seed_world(sessions)
    try:
        issue_ids = await _add_repair_issues(sessions, world, task_indexes=[0])
        expected_source = _rle([".....", ".###.", "....#"])
        expected_region = _rle([".....", ".....", "....#"])
        expected_result = rle_and_not(expected_source, expected_region)
        world["mask_keys"].add(build_rle_reference(expected_result)["object_key"])
        async with _api_client(app_module, sessions) as client:
            plan = await _dry_run(
                client,
                world,
                [{"issue_id": str(issue_ids[0]), "kind": "delete_small_islands"}],
            )
            submitted = await _execute_repair(client, world, plan)
            batch_id = uuid.UUID(submitted["id"])
            job_id = uuid.UUID(submitted["async_job_id"])
            record_property("repair_batch_id", str(batch_id))
            record_property("repair_job_id", str(job_id))
            await _wait_for_row(
                sessions, MaskRepairBatch, batch_id, "status", {"completed"}
            )
            async with sessions() as db:
                annotation = (
                    await db.execute(
                        select(Annotation).where(
                            Annotation.task_id == world["task_ids"][0]
                        )
                    )
                ).scalar_one()
                assert (
                    await load_coco_rle(annotation.geometry["mask"]) == expected_result
                )
                completed = await client.get(
                    f"/api/v1/mask-qc/repairs/{batch_id}",
                    headers=_headers(world["token"]),
                )
                assert completed.status_code == 200, completed.text
            rollback = await client.post(
                f"/api/v1/mask-qc/repairs/{batch_id}/rollback",
                headers=_headers(world["token"]),
                json={"expected_result_digest": completed.json()["result_digest"]},
            )
            assert rollback.status_code == 202, rollback.text
            rollback_job_id = uuid.UUID(rollback.json()["rollback_async_job_id"])
            record_property("rollback_job_id", str(rollback_job_id))
            await _wait_for_row(
                sessions,
                MaskRepairBatch,
                batch_id,
                "status",
                {"rolled_back", "rollback_failed"},
            )
            async with sessions() as db:
                batch = await db.get(MaskRepairBatch, batch_id)
                annotation = await db.get(Annotation, annotation.id)
                assert batch.status == "rolled_back"
                assert batch.result_json["rollback"]["status"] == "completed"
                assert (
                    await load_coco_rle(annotation.geometry["mask"]) == expected_source
                )
                operation_kinds = set(
                    (
                        await db.execute(
                            select(AnnotationOperation.kind).where(
                                AnnotationOperation.task_id == world["task_ids"][0]
                            )
                        )
                    ).scalars()
                )
                relations = set(
                    (
                        await db.execute(
                            select(AnnotationLineageEdge.relation).where(
                                AnnotationLineageEdge.operation_id.in_(
                                    select(AnnotationOperation.id).where(
                                        AnnotationOperation.task_id
                                        == world["task_ids"][0]
                                    )
                                )
                            )
                        )
                    ).scalars()
                )
                assert {
                    "delete_small_islands",
                    "mask_repair_rollback",
                } <= operation_kinds
                assert {"mask_repaired", "mask_repair_rolled_back"} <= relations
            await _wait_for_row(
                sessions, AsyncJob, rollback_job_id, "status", {"completed", "failed"}
            )
    finally:
        await _cleanup_world(sessions, world)


@pytest.mark.asyncio
async def test_repair_rollback_failure_is_terminal_and_preserves_completed_shard(
    test_engine, app_module, record_property
):
    sessions = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    world = await _seed_world(sessions, count=2)
    try:
        issue_ids = await _add_repair_issues(sessions, world, task_indexes=[0, 1])
        async with _api_client(app_module, sessions) as client:
            plan = await _dry_run(
                client,
                world,
                [
                    {"issue_id": str(issue_id), "kind": "delete_small_islands"}
                    for issue_id in issue_ids
                ],
            )
            submitted = await _execute_repair(client, world, plan)
            batch_id = uuid.UUID(submitted["id"])
            execute_job_id = uuid.UUID(submitted["async_job_id"])
            record_property("repair_batch_id", str(batch_id))
            record_property("repair_job_id", str(execute_job_id))
            await _wait_for_row(
                sessions, MaskRepairBatch, batch_id, "status", {"completed", "failed"}
            )
            async with sessions() as db:
                batch = await db.get(MaskRepairBatch, batch_id)
                shard_values = list(
                    (batch.result_json or {}).get("shards", {}).values()
                )
                assert len(shard_values) == 2
                conflict_task_id = uuid.UUID(str(shard_values[0]["task_id"]))
                conflict_annotation = (
                    await db.execute(
                        select(Annotation).where(Annotation.task_id == conflict_task_id)
                    )
                ).scalar_one()
                conflict_annotation.version += 1
                await db.commit()
            current = await client.get(
                f"/api/v1/mask-qc/repairs/{batch_id}",
                headers=_headers(world["token"]),
            )
            assert current.status_code == 200, current.text
            request = await client.post(
                f"/api/v1/mask-qc/repairs/{batch_id}/rollback",
                headers=_headers(world["token"]),
                json={"expected_result_digest": current.json()["result_digest"]},
            )
            assert request.status_code == 202, request.text
            rollback_job_id = uuid.UUID(request.json()["rollback_async_job_id"])
            record_property("rollback_job_id", str(rollback_job_id))
            terminal = await _wait_for_row(
                sessions,
                MaskRepairBatch,
                batch_id,
                "status",
                {"rolled_back", "rollback_failed"},
            )
            assert terminal.status == "rollback_failed"
            assert terminal.result_json["rollback"]["status"] == "failed"
            assert terminal.result_json["rollback"]["rolled_back_count"] == 1
            assert terminal.result_json["rollback"]["failed_shard_id"] is not None
            job = await _wait_for_row(
                sessions, AsyncJob, rollback_job_id, "status", {"failed", "completed"}
            )
            assert job.status == "failed"
            assert terminal.status != "rolling_back"
    finally:
        await _cleanup_world(sessions, world)


@pytest.mark.asyncio
async def test_sam_candidate_persists_prediction_and_cancellation_lock_order_is_safe(
    test_engine, app_module, record_property
):
    sessions = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    world = await _seed_world(sessions)
    world["server_entered"] = threading.Event()
    world["candidate_rle"] = _rle(["#....", ".##..", "....#"])
    world["mask_keys"].add(build_rle_reference(world["candidate_rle"])["object_key"])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            body = json.dumps(
                {
                    "name": "local-protocol-test",
                    "infra": "cpu-fixture",
                    "models": [
                        {
                            "id": "interactive-mask",
                            "task": "interactive_seg",
                            "is_interactive": True,
                            "supported_prompts": ["interactive_box"],
                            "supported_inputs": ["full_image", "bbox_prompt"],
                            "supported_geometric_outputs": ["mask"],
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            world["server_entered"].set()
            rle = CocoRlePayload.model_validate(world["candidate_rle"])
            candidate = NativeMaskCandidate(
                value=NativeMaskCandidateValue(rle=rle, masklabels=["object"]),
                score=0.91,
                candidate_id=native_mask_candidate_id(
                    rle,
                    prompt_revision=request["context"]["prompt_revision"],
                    candidate_index=0,
                ),
            )
            body = json.dumps(
                {
                    "result": [candidate.model_dump(mode="json")],
                    "model_version": "protocol-fixture-only",
                    "inference_time_ms": 1,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    suffix = uuid.uuid4().hex[:12]
    trigger = f"mrpred_{suffix}"
    function = f"mrpredfn_{suffix}"
    lock_class = int(suffix[:6], 16) & 0x7FFFFFFF
    lock_object = int(suffix[6:], 16) & 0x7FFFFFFF
    try:
        async with sessions() as db:
            backend, pool = await create_registry_with_pool(
                db,
                name=f"mask-repair-{world['suffix']}",
                url=f"http://127.0.0.1:{server.server_port}",
                state="connected",
                is_interactive=True,
                enabled_pool=True,
            )
            world["backend_id"] = backend.id
            world["pool_id"] = pool.id
            project = await db.get(Project, world["project_id"])
            project.ml_backend_pool_id = pool.id
            db.add(
                ProjectMLBackendPool(
                    project_id=world["project_id"], pool_id=pool.id, enabled=True
                )
            )
            await db.commit()
        issue_ids = await _add_repair_issues(
            sessions, world, task_indexes=[0], code="near_empty_mask"
        )
        await _install_trigger(
            test_engine,
            trigger,
            function,
            "predictions",
            f"BEGIN IF NEW.task_id = '{world['task_ids'][0]}'::uuid THEN "
            f"PERFORM pg_advisory_xact_lock({lock_class}, {lock_object}); "
            "PERFORM pg_sleep(3); END IF; RETURN NEW; END;",
        )
        async with _api_client(app_module, sessions) as client:
            plan = await _dry_run(
                client,
                world,
                [
                    {
                        "issue_id": str(issue_ids[0]),
                        "kind": "rerun_local_sam",
                        "backend_id": str(world["backend_id"]),
                        "model_id": "interactive-mask",
                    }
                ],
            )
            submitted = await _execute_repair(client, world, plan)
            batch_id = uuid.UUID(submitted["id"])
            job_id = uuid.UUID(submitted["async_job_id"])
            record_property("repair_batch_id", str(batch_id))
            record_property("repair_job_id", str(job_id))
            await _wait_for_advisory_lock(test_engine, lock_class, lock_object)
            assert await asyncio.to_thread(world["server_entered"].wait, 5)
            cancellation = asyncio.create_task(
                client.post(
                    f"/api/v1/async-jobs/{job_id}/cancel",
                    headers=_headers(world["token"]),
                )
            )
            await _wait_for_lock_wait(test_engine, "async_jobs")
            cancelled = await asyncio.wait_for(cancellation, timeout=10)
            assert cancelled.status_code == 200, cancelled.text
            terminal = await _wait_for_row(
                sessions,
                MaskRepairBatch,
                batch_id,
                "status",
                {"completed", "partial", "failed", "cancelled"},
            )
            assert terminal.status == "completed"
            async with sessions() as db:
                predictions = list(
                    (
                        await db.execute(
                            select(Prediction).where(
                                Prediction.task_id == world["task_ids"][0]
                            )
                        )
                    ).scalars()
                )
                assert len(predictions) == 1
                prediction = predictions[0]
                assert prediction.source == "mask_repair"
                mask_ref = prediction.result[0]["geometry"]["mask"]
                assert await load_coco_rle(mask_ref) == world["candidate_rle"]
                metadata = (
                    await db.execute(
                        select(PredictionMeta).where(
                            PredictionMeta.prediction_id == prediction.id
                        )
                    )
                ).scalar_one()
                assert metadata.extra["mask_repair"]["batch_id"] == str(batch_id)
                batch = await db.get(MaskRepairBatch, batch_id)
                candidate = batch.result_json["candidates"]["0"]
                assert candidate["status"] == "completed"
                assert candidate["candidate_count"] == 1
                assert candidate["prediction_ids"] == [str(prediction.id)]
                world["mask_keys"].add(mask_ref["object_key"])
    finally:
        await _drop_trigger(test_engine, trigger, function, "predictions")
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        await _cleanup_world(sessions, world)


@pytest.mark.asyncio
async def test_video_dry_run_reaches_privileged_segment_authorization(
    test_engine, app_module
):
    sessions = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    world = await _seed_world(sessions, data_type="video")
    try:
        async with sessions() as db:
            task = await db.get(Task, world["task_ids"][0])
            segment = VideoSegment(
                id=uuid.uuid4(),
                dataset_item_id=world["item_ids"][0],
                segment_index=0,
                start_frame=0,
                end_frame=2,
                status="open",
            )
            db.add(segment)
            author = await create_user(
                db,
                "employee",
                f"video-mask-author-{world['suffix']}@test.local",
                "Video Mask Author",
            )
            world["user_ids"].append(author.id)
            task.reviewer_id = world["owner_id"]
            task.annotation_contributor_ids = [str(author.id)]
            task.review_contributor_ids = [str(author.id)]
            task.review_submitter_id = author.id
            task.review_round_id = uuid.uuid4()
            video_rle = _rle([".....", ".###.", "....#"])
            reference = (await _store_rles(db, world, task.id, [video_rle]))[0]
            annotation = Annotation(
                task_id=task.id,
                project_id=world["project_id"],
                user_id=author.id,
                source="manual",
                annotation_type="video_track_mask",
                tool_unit_id="region",
                class_name="object",
                geometry={
                    "type": "video_track_mask",
                    "track_id": "track-runtime",
                    "keyframes": [
                        {"frame_index": 0, "mask": reference, "source": "manual"}
                    ],
                },
                video_segment_id=segment.id,
                is_active=True,
                was_cancelled=False,
                version=1,
            )
            db.add(annotation)
            await db.flush()
            issue = MaskQCIssue(
                project_id=world["project_id"],
                task_id=task.id,
                annotation_id=annotation.id,
                annotation_version=1,
                related_annotation_ids=[annotation.id],
                source_versions={str(annotation.id): 1},
                code="flicker",
                severity="warning",
                severity_rank=1,
                status="open",
                frame_start=0,
                frame_end=0,
                metric={"frame_index": 0},
                threshold={},
                region_bbox={"x0": 0, "y0": 0, "x1": 1, "y1": 1},
                region_mask_ref=reference,
                region_digest=reference["sha256"],
                dedupe_key=hashlib.sha256(
                    f"video-mask-runtime-{world['suffix']}".encode()
                ).hexdigest(),
                source={"kind": "single_frame", "frame_index": 0},
            )
            db.add(issue)
            await db.commit()
        async with _api_client(app_module, sessions) as client:
            plan = await _dry_run(
                client,
                world,
                [
                    {
                        "issue_id": str(issue.id),
                        "kind": "rerun_tracker",
                        "backend_id": str(uuid.uuid4()),
                        "model_id": "tracker-model",
                        "model_key": "tracker",
                        "from_frame": 0,
                        "to_frame": 0,
                        "direction": "forward",
                        "segment_id": str(segment.id),
                    }
                ],
            )
            assert plan["items"][0]["kind"] == "rerun_tracker"
            assert plan["summary"]["candidate_count"] == 1
    finally:
        await _cleanup_world(sessions, world)


@pytest.mark.asyncio
async def test_format_import_celery_consumer_persists_real_rle_pixels(
    test_engine, app_module, record_property
):
    sessions = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    world = await _seed_world(sessions, statuses=["pending"])
    try:
        async with _api_client(app_module, sessions) as client:
            staged = await _stage_aap_import(client, world, [0])
            response = await _execute_import(client, world, staged)
            batch_id = uuid.UUID(response.json()["id"])
            job_id = uuid.UUID(response.json()["async_job_id"])
            record_property("format_import_id", str(batch_id))
            record_property("format_import_job_id", str(job_id))
            await _wait_for_row(
                sessions,
                MaskFormatImport,
                batch_id,
                "status",
                {"completed", "partial", "failed"},
            )
            async with sessions() as db:
                batch = await db.get(MaskFormatImport, batch_id)
                annotation = (
                    await db.execute(
                        select(Annotation).where(
                            Annotation.task_id == world["task_ids"][0]
                        )
                    )
                ).scalar_one()
                expected = staged["expected"][world["task_ids"][0]]
                assert batch.status == "completed"
                assert batch.result_json["items"]["0"]["status"] == "committed"
                actual = await load_coco_rle(annotation.geometry["mask"])
                assert actual == expected
                assert list(decode_coco_rle(actual)) == list(decode_coco_rle(expected))
                assert batch.format_id == "aap_json"
                world["mask_keys"].add(annotation.geometry["mask"]["object_key"])
    finally:
        await _cleanup_world(sessions, world)


@pytest.mark.asyncio
async def test_format_import_partial_failure_keeps_committed_items(
    test_engine, app_module, record_property
):
    sessions = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    world = await _seed_world(sessions, count=3, statuses=["pending"] * 3)
    suffix = uuid.uuid4().hex[:12]
    trigger, function = f"mfimp_{suffix}", f"mfimpfn_{suffix}"
    installed = False
    try:
        async with _api_client(app_module, sessions) as client:
            staged = await _stage_aap_import(client, world, [0, 1, 2])
            plan_items = staged["preflight"]["plan"]["items"]
            failed_task = uuid.UUID(str(plan_items[1]["task_id"]))
            await _install_trigger(
                test_engine,
                trigger,
                function,
                "annotations",
                f"BEGIN IF NEW.task_id = '{failed_task}'::uuid THEN "
                "RAISE EXCEPTION 'runtime import failure fixture'; END IF; RETURN NEW; END;",
            )
            installed = True
            response = await _execute_import(client, world, staged)
            batch_id = uuid.UUID(response.json()["id"])
            job_id = uuid.UUID(response.json()["async_job_id"])
            record_property("format_import_id", str(batch_id))
            record_property("format_import_job_id", str(job_id))
            terminal = await _wait_for_row(
                sessions,
                MaskFormatImport,
                batch_id,
                "status",
                {"completed", "partial", "failed"},
            )
            assert terminal.status == "partial"
            items = terminal.result_json["items"]
            assert [items[str(index)]["status"] for index in range(3)] == [
                "committed",
                "failed",
                "committed",
            ]
            async with sessions() as db:
                stored = set(
                    (
                        await db.execute(
                            select(Annotation.task_id).where(
                                Annotation.task_id.in_(world["task_ids"])
                            )
                        )
                    ).scalars()
                )
                assert stored == set(world["task_ids"]) - {failed_task}
                for task_id in stored:
                    annotation = (
                        await db.execute(
                            select(Annotation).where(Annotation.task_id == task_id)
                        )
                    ).scalar_one()
                    assert (
                        await load_coco_rle(annotation.geometry["mask"])
                        == staged["expected"][task_id]
                    )
                    world["mask_keys"].add(annotation.geometry["mask"]["object_key"])
            await _wait_for_row(
                sessions, AsyncJob, job_id, "status", {"completed", "failed"}
            )
    finally:
        if installed:
            await _drop_trigger(test_engine, trigger, function, "annotations")
        await _cleanup_world(sessions, world)


@pytest.mark.asyncio
async def test_format_import_cancel_finishes_partial_after_committed_item(
    test_engine, app_module, record_property
):
    sessions = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    world = await _seed_world(sessions, count=3, statuses=["pending"] * 3)
    suffix = uuid.uuid4().hex[:12]
    trigger, function = f"mfcancel_{suffix}", f"mfcancelfn_{suffix}"
    lock_class = int(suffix[:6], 16) & 0x7FFFFFFF
    lock_object = int(suffix[6:], 16) & 0x7FFFFFFF
    installed = False
    try:
        await _install_trigger(
            test_engine,
            trigger,
            function,
            "annotations",
            f"BEGIN IF NEW.task_id = '{world['task_ids'][0]}'::uuid THEN "
            f"PERFORM pg_advisory_xact_lock({lock_class}, {lock_object}); "
            "PERFORM pg_sleep(3); END IF; RETURN NEW; END;",
        )
        installed = True
        async with _api_client(app_module, sessions) as client:
            staged = await _stage_aap_import(client, world, [0, 1, 2])
            response = await _execute_import(client, world, staged)
            batch_id = uuid.UUID(response.json()["id"])
            job_id = uuid.UUID(response.json()["async_job_id"])
            record_property("format_import_id", str(batch_id))
            record_property("format_import_job_id", str(job_id))
            await _wait_for_advisory_lock(test_engine, lock_class, lock_object)
            cancellation = asyncio.create_task(
                client.post(
                    f"/api/v1/async-jobs/{job_id}/cancel",
                    headers=_headers(world["token"]),
                )
            )
            await _wait_for_lock_wait(test_engine, "mask_format_imports")
            cancelled = await asyncio.wait_for(cancellation, timeout=12)
            assert cancelled.status_code == 200, cancelled.text
            terminal = await _wait_for_row(
                sessions,
                MaskFormatImport,
                batch_id,
                "status",
                {"partial", "cancelled", "failed", "completed"},
            )
            assert terminal.status == "partial"
            assert terminal.result_json["items"]["0"]["status"] == "committed"
            assert "1" not in terminal.result_json["items"]
            assert "2" not in terminal.result_json["items"]
            job = await _wait_for_row(
                sessions,
                AsyncJob,
                job_id,
                "status",
                {"cancelled", "completed", "failed"},
            )
            assert job.status == "cancelled"
            assert job.result == {
                "committed": 1,
                "reason": "cancelled_by_user",
            }
            async with sessions() as db:
                notification_types = (
                    (
                        await db.execute(
                            select(Notification.type).where(
                                Notification.target_id == job_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert notification_types == ["job.cancelled"]
                annotation = (
                    await db.execute(
                        select(Annotation).where(
                            Annotation.task_id == world["task_ids"][0]
                        )
                    )
                ).scalar_one()
                assert (
                    await load_coco_rle(annotation.geometry["mask"])
                    == staged["expected"][world["task_ids"][0]]
                )
                world["mask_keys"].add(annotation.geometry["mask"]["object_key"])
                for task_id in world["task_ids"][1:]:
                    assert (
                        await db.scalar(
                            select(Annotation.id).where(Annotation.task_id == task_id)
                        )
                        is None
                    )
    finally:
        if installed:
            await _drop_trigger(test_engine, trigger, function, "annotations")
        await _cleanup_world(sessions, world)


@pytest.mark.asyncio
async def test_dispatch_failure_routes_return_503_and_keep_real_retry_paths(
    test_engine, app_module, record_property
):
    sessions = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )
    world = await _seed_world(sessions, count=2, statuses=["review", "pending"])
    try:
        issue_ids = await _add_repair_issues(sessions, world, task_indexes=[0])
        async with _api_client(app_module, sessions) as client:
            staged = await _stage_aap_import(client, world, [1])
            plan = await _dry_run(
                client,
                world,
                [{"issue_id": str(issue_ids[0]), "kind": "delete_small_islands"}],
            )
        async with _api_client(
            app_module, sessions, dispatcher=_UnavailableDispatcher()
        ) as client:
            repair_failure = await client.post(
                f"/api/v1/projects/{world['project_id']}/mask-qc/repairs",
                headers=_headers(world["token"]),
                json={"receipt": plan["receipt"], "plan_digest": plan["plan_digest"]},
            )
            assert repair_failure.status_code == 503, repair_failure.text
            async with sessions() as db:
                repair_batch_id = await db.scalar(
                    select(MaskRepairBatch.id).where(
                        MaskRepairBatch.project_id == world["project_id"]
                    )
                )
                repair_batch = await db.get(MaskRepairBatch, repair_batch_id)
                repair_job_id = repair_batch.async_job_id
                assert repair_batch.status == "failed"
                assert (await db.get(AsyncJob, repair_job_id)).status == "failed"
            record_property("repair_batch_id", str(repair_batch_id))
            record_property("repair_failed_job_id", str(repair_job_id))
        async with _api_client(app_module, sessions) as client:
            repair_retry = await client.post(
                f"/api/v1/mask-qc/repairs/{repair_batch_id}/resume",
                headers=_headers(world["token"]),
            )
            assert repair_retry.status_code == 202, repair_retry.text
            retry_job_id = uuid.UUID(repair_retry.json()["async_job_id"])
            record_property("repair_retry_job_id", str(retry_job_id))
            repair_terminal = await _wait_for_row(
                sessions,
                MaskRepairBatch,
                repair_batch_id,
                "status",
                {"completed", "failed"},
            )
            assert repair_terminal.status == "completed"
            async with sessions() as db:
                annotation = (
                    await db.execute(
                        select(Annotation).where(
                            Annotation.task_id == world["task_ids"][0]
                        )
                    )
                ).scalar_one()
                world["mask_keys"].add(annotation.geometry["mask"]["object_key"])
                assert await load_coco_rle(annotation.geometry["mask"]) == rle_and_not(
                    _rle([".....", ".###.", "....#"]),
                    _rle([".....", ".....", "....#"]),
                )
        async with _api_client(
            app_module, sessions, dispatcher=_UnavailableDispatcher()
        ) as client:
            await _execute_import(client, world, staged, expected_status=503)
            import_id = uuid.UUID(staged["preflight"]["import_id"])
            async with sessions() as db:
                import_batch = await db.get(MaskFormatImport, import_id)
                import_job_id = import_batch.async_job_id
                assert import_batch.status == "failed"
                assert (await db.get(AsyncJob, import_job_id)).status == "failed"
            record_property("format_import_id", str(import_id))
            record_property("format_failed_job_id", str(import_job_id))
        async with _api_client(app_module, sessions) as client:
            import_retry = await client.post(
                f"/api/v1/projects/{world['project_id']}/mask-formats/imports/{import_id}/resume",
                headers=_headers(world["token"]),
            )
            assert import_retry.status_code == 202, import_retry.text
            import_retry_job_id = uuid.UUID(import_retry.json()["async_job_id"])
            record_property("format_retry_job_id", str(import_retry_job_id))
            await _wait_for_row(
                sessions,
                MaskFormatImport,
                import_id,
                "status",
                {"completed", "partial", "failed"},
            )
            async with sessions() as db:
                batch = await db.get(MaskFormatImport, import_id)
                assert batch.status == "completed"
                annotation = (
                    await db.execute(
                        select(Annotation).where(
                            Annotation.task_id == world["task_ids"][1]
                        )
                    )
                ).scalar_one()
                assert (
                    await load_coco_rle(annotation.geometry["mask"])
                    == staged["expected"][world["task_ids"][1]]
                )
                world["mask_keys"].add(annotation.geometry["mask"]["object_key"])
    finally:
        await _cleanup_world(sessions, world)
