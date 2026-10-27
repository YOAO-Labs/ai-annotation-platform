"""Real repair owner progression, receipt isolation and physical drain tests."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import hashlib
import threading
import uuid

import pytest
from sqlalchemy import func, select

from app.db.models.annotation import Annotation
from app.db.models.annotation_operation import (
    AnnotationOperation,
    AnnotationLineageEdge,
)
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.prediction import PredictionMeta
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.storage_maintenance import StorageOperation
from app.services.content_snapshot import ContentError
from app.services.managed_mask_repair import ManagedRepairUnit, validate_receipt
from app.services.mask_repair import canonical_digest
from app.services.mask_repair_execution import start_attempt, lock_attempt
from app.services.raster_mask_storage import (
    build_rle_reference,
    reserve_raster_mask_upload,
    lock_raster_mask_references,
    store_coco_rle,
)
from app.utils.raster_mask_rle import encode_coco_rle
from app.workers import mask_repair as worker
from tests.test_managed_mask_repair import (
    ORIGINS,
    assert_reference,
    auth,
    candidate,
    db_session as db_session,
    execute,
    forward,
    httpx_client as httpx_client,
    input_world as input_world,
    plan,
    repair_world as repair_world,
    state,
    sync_world as sync_world,
)
from tests.test_prediction_reference_actions import api_key
from tests.test_reference_image_inference import accept_body, call


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("repair_world", ORIGINS, indirect=True)
@pytest.mark.parametrize("kind", ["fill_small_holes", "resolve_same_class_overlap"])
async def test_other_deterministic_kinds_keep_exact_algebra_and_rollback(
    repair_world, httpx_client, super_admin, kind
):
    w, headers = repair_world, auth(super_admin)
    async with w.factory() as db:
        parent = await db.get(Annotation, w.annotation_id)
        issue = await db.get(MaskQCIssue, w.issue_id)
        if kind == "fill_small_holes":
            pixels = [0] * (48 * 32)
            for y in range(1, 4):
                for x in range(1, 4):
                    pixels[y * 48 + x] = int((x, y) != (2, 2))
            source = encode_coco_rle(pixels, 48, 32)
            region = encode_coco_rle(
                [int(index == 2 * 48 + 2) for index in range(48 * 32)], 48, 32
            )
            pixels[2 * 48 + 2] = 1
            expected = encode_coco_rle(pixels, 48, 32)
            for rle in (source, region):
                await store_coco_rle(rle, db=db)
            parent.geometry = {
                "type": "raster_mask",
                "mask": build_rle_reference(source),
            }
            await reserve_raster_mask_upload(
                db,
                task_id=w.task_id,
                object_key=build_rle_reference(source)["object_key"],
                limit=100,
            )
            await lock_raster_mask_references(db, parent.geometry, task_id=w.task_id)
            issue.code = "small_hole"
            issue.region_mask_ref = build_rle_reference(region)
            issue.region_digest = issue.region_mask_ref["sha256"]
        else:
            source, expected = w.source, w.candidate
            pixels = [0] * (48 * 32)
            pixels[-1] = 1
            for index in (5 * 48 + 5, 5 * 48 + 6, 6 * 48 + 5, 6 * 48 + 6):
                pixels[index] = 1
            other_rle = encode_coco_rle(pixels, 48, 32)
            await store_coco_rle(other_rle, db=db)
            other = Annotation(
                task_id=w.task_id,
                project_id=w.project_id,
                user_id=parent.user_id,
                source="manual",
                annotation_type="raster_mask",
                tool_unit_id="region",
                class_name="object",
                geometry={
                    "type": "raster_mask",
                    "mask": build_rle_reference(other_rle),
                },
                is_active=True,
                was_cancelled=False,
                is_locked=False,
                version=1,
            )
            db.add(other)
            await db.flush()
            await reserve_raster_mask_upload(
                db,
                task_id=w.task_id,
                object_key=build_rle_reference(other_rle)["object_key"],
                limit=100,
            )
            await lock_raster_mask_references(db, other.geometry, task_id=w.task_id)
            issue.code = "same_class_overlap"
            issue.related_annotation_ids = [parent.id, other.id]
        await db.flush()
        # Geometry updates increment the version in PostgreSQL; freeze the
        # actual server value in the newly produced QC evidence.
        await db.refresh(parent)
        before_version = parent.version
        issue.annotation_version = before_version
        issue.source_versions = {str(parent.id): before_version}
        if kind == "resolve_same_class_overlap":
            issue.source_versions = {
                **issue.source_versions,
                str(other.id): other.version,
            }
        await db.commit()
    response = await httpx_client.post(
        f"/api/v1/projects/{w.project_id}/mask-qc/repairs:dry-run",
        headers=headers,
        json={"actions": [{"issue_id": str(w.issue_id), "kind": kind}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["executable_count"] == 1, body["items"]
    w.receipt, w.plan_digest = body["receipt"], body["plan_digest"]
    await execute(httpx_client, w, headers)
    await forward(w)
    batch, job, annotation, _ = await state(w)
    assert (batch.status, job.status, annotation.version) == (
        "completed",
        "completed",
        before_version + 1,
    ), (batch.result_json, w.failures)
    assert_reference(annotation.geometry["mask"], expected)
    rolled = await httpx_client.post(
        f"/api/v1/mask-qc/repairs/{w.batch_id}/rollback",
        headers=headers,
        json={"expected_result_digest": canonical_digest(batch.result_json)},
    )
    assert rolled.status_code == 202, rolled.text
    rollback_id = rolled.json()["rollback_async_job_id"]
    await worker._run_rollback(
        w.factory,
        str(w.batch_id),
        rollback_id,
        rollback_id,
        worker_node_id="worker:managed@test",
    )
    batch, _, annotation, _ = await state(w)
    assert (batch.status, annotation.version) == ("rolled_back", before_version + 2), (
        batch.result_json,
        w.failures,
    )
    assert_reference(annotation.geometry["mask"], source)
    assert not w.inputs.puts and not w.calls


@pytest.mark.parametrize("repair_world", ["primary", "task"], indirect=True)
async def test_native_interactive_accepted_mask_uses_qualified_content_reader(
    repair_world, httpx_client, super_admin
):
    w, headers = repair_world, auth(super_admin)
    async with w.factory() as db:
        (await db.get(Task, w.task_id)).status = "pending"
        (await db.get(Project, w.project_id)).ai_interactive_enabled = True
        await db.commit()
    w.mask = True
    w.source_closed_before_ml = False
    w.expected_input_kind = "interactive_predict"
    inferred = await call(httpx_client, w, headers)
    assert inferred.status_code == 200, inferred.text
    body = accept_body(inferred.json())
    body["class_name"] = "object"
    accepted = await httpx_client.post(
        f"/api/v1/tasks/{w.task_id}/ai-mask-candidates/accept",
        headers=headers,
        json=body,
    )
    assert accepted.status_code == 200, accepted.text
    annotation = accepted.json()["annotation"]
    readable = await httpx_client.get(
        f"/api/v1/annotations/{annotation['id']}/mask-content", headers=headers
    )
    assert readable.status_code == 200, readable.text
    assert readable.json() == w.candidate


@pytest.mark.parametrize("repair_world", ["direct", "primary"], indirect=True)
async def test_true_source_dimensions_override_inaccurate_item_metadata(
    repair_world, httpx_client, super_admin
):
    w = repair_world
    async with w.factory() as db:
        item = await db.get(DatasetItem, w.member.item.id)
        item.width, item.height = 777, 555
        await db.commit()
    prediction = await candidate(httpx_client, w, auth(super_admin))
    accepted = await httpx_client.post(
        f"/api/v1/tasks/{w.task_id}/predictions/{prediction.id}/accept",
        headers=auth(super_admin),
    )
    assert accepted.status_code == 200, accepted.text
    assert_reference(accepted.json()[0]["geometry"]["mask"], w.candidate)


async def test_read_only_current_key_and_fresh_adopter_outlive_generation_key(
    repair_world, httpx_client, super_admin
):
    w = repair_world
    generated_id, generated = await api_key(
        httpx_client, super_admin, ["annotations:write"]
    )
    prediction = await candidate(httpx_client, w, generated)
    async with w.factory() as db:
        (await db.get(ApiKey, generated_id)).revoked_at = datetime.now(timezone.utc)
        await db.commit()
    _, read = await api_key(httpx_client, super_admin, ["annotations:read"])
    url = f"/api/v1/tasks/{w.task_id}/predictions/{prediction.id}"
    preview = await httpx_client.get(url + "/mask-content/0", headers=read)
    assert preview.status_code == 200, preview.text
    blocked = await httpx_client.post(url + "/accept", headers=read)
    assert blocked.status_code == 403, blocked.text
    _, write = await api_key(httpx_client, super_admin, ["annotations:write"])
    accepted = await httpx_client.post(url + "/accept", headers=write)
    assert accepted.status_code == 200, accepted.text
    readable = await httpx_client.get(
        f"/api/v1/annotations/{accepted.json()[0]['id']}/mask-content", headers=read
    )
    assert readable.status_code == 200, readable.text


@pytest.mark.parametrize("entry", ["execute", "resume", "resume-completed", "rollback"])
async def test_legacy_managed_plans_require_new_preview_without_backfill(
    repair_world, httpx_client, super_admin, entry
):
    w, headers = repair_world, auth(super_admin)
    await plan(httpx_client, w, headers, sam=entry == "resume")
    if entry != "execute":
        await execute(httpx_client, w, headers)
        if entry == "resume":
            w.failure = TimeoutError("Controlled prior failed attempt")
        await forward(w)
    async with w.factory() as db:
        batch = await db.scalar(
            select(MaskRepairBatch).where(MaskRepairBatch.plan_digest == w.plan_digest)
        )
        value = deepcopy(batch.plan_json)
        for item in value["items"]:
            for key in (
                "original_input",
                "original_annotation",
                "original_annotations",
                "original_model",
            ):
                item.pop(key, None)
        batch.plan_json, batch.plan_digest = value, canonical_digest(value)
        if entry.startswith("resume"):
            batch.status = "failed"
        w.plan_digest = batch.plan_digest
        result_digest = canonical_digest(batch.result_json or {})
        job_count = await db.scalar(select(func.count()).select_from(AsyncJob))
        await db.commit()
        batch_id = batch.id
    before = len(w.sources.captured)
    if entry == "execute":
        response = await httpx_client.post(
            f"/api/v1/projects/{w.project_id}/mask-qc/repairs",
            headers=headers,
            json={"receipt": w.receipt, "plan_digest": w.plan_digest},
        )
    else:
        response = await httpx_client.post(
            f"/api/v1/mask-qc/repairs/{batch_id}/{'resume' if entry.startswith('resume') else entry}",
            headers=headers,
            **(
                {"json": {"expected_result_digest": result_digest}}
                if entry == "rollback"
                else {}
            ),
        )
    assert response.status_code == 409, response.text
    assert "repair_original_requires_new_plan" in response.text, response.text
    assert len(w.sources.captured) == before
    async with w.factory() as db:
        assert await db.scalar(select(func.count()).select_from(AsyncJob)) == job_count


async def second_issue(w):
    async with w.factory() as db:
        parent = await db.get(Annotation, w.annotation_id)
        source = await db.get(MaskQCIssue, w.issue_id)
        annotation = Annotation(
            task_id=parent.task_id,
            project_id=parent.project_id,
            user_id=parent.user_id,
            source=parent.source,
            annotation_type=parent.annotation_type,
            tool_unit_id=parent.tool_unit_id,
            class_name=parent.class_name,
            geometry=deepcopy(parent.geometry),
            is_active=True,
            was_cancelled=False,
            is_locked=False,
            version=1,
        )
        db.add(annotation)
        await db.flush()
        issue = MaskQCIssue(
            project_id=parent.project_id,
            task_id=parent.task_id,
            annotation_id=annotation.id,
            annotation_version=1,
            related_annotation_ids=[annotation.id],
            source_versions={str(annotation.id): 1},
            code=source.code,
            severity=source.severity,
            severity_rank=source.severity_rank,
            status="open",
            metric=deepcopy(source.metric),
            threshold=deepcopy(source.threshold),
            region_bbox=deepcopy(source.region_bbox),
            region_mask_ref=deepcopy(source.region_mask_ref),
            region_digest=source.region_digest,
            dedupe_key=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            source=deepcopy(source.source),
        )
        db.add(issue)
        await db.flush()
        await reserve_raster_mask_upload(
            db,
            task_id=w.task_id,
            object_key=parent.geometry["mask"]["object_key"],
            limit=100,
        )
        await lock_raster_mask_references(db, annotation.geometry, task_id=w.task_id)
        await db.commit()
        return annotation.id, issue.id


@pytest.mark.parametrize("repair_world", ORIGINS, indirect=True)
@pytest.mark.parametrize("separate", [False, True])
async def test_own_multi_mutation_progress_and_cross_shard_resume(
    repair_world, httpx_client, super_admin, monkeypatch, separate
):
    w, headers = repair_world, auth(super_admin)
    second_id, issue_id = await second_issue(w)
    if separate:
        monkeypatch.setattr("app.services.mask_repair.MAX_SHARD_MUTATIONS", 1)
    planned = await httpx_client.post(
        f"/api/v1/projects/{w.project_id}/mask-qc/repairs:dry-run",
        headers=headers,
        json={
            "actions": [
                {"issue_id": str(identity), "kind": "delete_small_islands"}
                for identity in (w.issue_id, issue_id)
            ]
        },
    )
    assert planned.status_code == 200, planned.text
    body = planned.json()
    assert body["summary"]["executable_count"] == 2, body
    assert body["summary"]["shard_count"] == (2 if separate else 1), body
    w.receipt, w.plan_digest = body["receipt"], body["plan_digest"]
    await execute(httpx_client, w, headers)
    original = worker._execute_shard
    calls = []

    async def gate(*args, **kwargs):
        calls.append(kwargs["shard"]["id"])
        if separate and len(calls) == 2:
            raise RuntimeError("Controlled interruption after first committed shard")
        return await original(*args, **kwargs)

    monkeypatch.setattr(worker, "_execute_shard", gate)
    await forward(w)
    batch, _, _, _ = await state(w)
    if separate:
        assert batch.status == "partial", (batch.result_json, w.failures)
        assert (
            len(
                [
                    r
                    for r in batch.result_json["shards"].values()
                    if r["status"] == "completed"
                ]
            )
            == 1
        )
        resumed = await httpx_client.post(
            f"/api/v1/mask-qc/repairs/{w.batch_id}/resume", headers=headers
        )
        assert resumed.status_code == 202, resumed.text
        w.job_id = uuid.UUID(resumed.json()["async_job_id"])
        await forward(w)
    batch, job, _, _ = await state(w)
    assert (batch.status, job.status) == ("completed", "completed"), (
        batch.result_json,
        w.failures,
    )
    async with w.factory() as db:
        for identity in (w.annotation_id, second_id):
            annotation = await db.get(Annotation, identity)
            assert annotation.version == 2
            assert_reference(annotation.geometry["mask"], w.candidate)
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AnnotationOperation)
                .where(AnnotationOperation.task_id == w.task_id)
            )
            == 2
        )
    assert len(calls) == (3 if separate else 1)


@pytest.mark.parametrize("change", ["prediction", "deterministic"])
async def test_jwt_expiry_after_business_writes_rolls_back_whole_unit(
    repair_world, httpx_client, super_admin, monkeypatch, change
):
    w = repair_world
    headers = auth(super_admin)
    await plan(httpx_client, w, headers, sam=change == "prediction")
    await execute(httpx_client, w, headers)
    original = ManagedRepairUnit.recheck_credentials

    async def revoke(owner):
        # SQL revocation waits for the current Key lock. JWT natural expiry
        # changes without a database write while final business locks are held.
        from app.services import prediction_originals as originals

        expires = datetime.fromisoformat(
            owner.proof["credentials"]["jwt"]["expires_at"]
        )

        with monkeypatch.context() as patch:
            patch.setattr(
                originals.credentials, "now", lambda: expires + timedelta(seconds=1)
            )
            await original(owner)

    monkeypatch.setattr(ManagedRepairUnit, "recheck_credentials", revoke)
    await forward(w)
    batch, job, annotation, predictions = await state(w)
    assert (batch.status, job.status, annotation.version, len(predictions)) == (
        "failed",
        "failed",
        1,
        0,
    ), batch.result_json
    async with w.factory() as db:
        for model in (PredictionMeta, AnnotationOperation, AnnotationLineageEdge):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
    assert not any(
        row.get("status") == "completed"
        for section in ("shards", "candidates")
        for row in (batch.result_json.get(section) or {}).values()
    )


@pytest.mark.parametrize(
    "change", ["issuer", "payload", "session", "nested", "commit", "kind"]
)
async def test_private_receipt_cannot_escape_source_unit_or_top_transaction(
    repair_world, httpx_client, super_admin, change
):
    w, headers = repair_world, auth(super_admin)
    await plan(httpx_client, w, headers)
    await execute(httpx_client, w, headers)
    async with w.factory() as db:
        attempt = await start_attempt(db, str(w.batch_id), str(w.job_id), str(w.job_id))
        await db.commit()
        batch = await db.get(MaskRepairBatch, w.batch_id)
        items = list(batch.plan_json["items"])
        shard = batch.plan_json["shards"][0]
        owner = ManagedRepairUnit(
            db, w.factory, attempt, batch, items, "worker:managed@test"
        )
        try:
            await db.commit()
            await owner.open()
            assert all(stream.closed for stream in w.sources.source_files)
            await db.rollback()
            await lock_attempt(db, attempt)
            await owner.final()
            receipt = owner.receipt("unit", shard)
            identity = await validate_receipt(
                db, w.task_id, w.actor_id, receipt, "unit", shard
            )
            assert (identity["width"], identity["height"]) == (48, 32)
            if change == "issuer":
                receipt = replace(receipt, issuer=object())
            if change == "commit":
                await db.commit()
            if change == "session":
                async with w.factory() as other:
                    with pytest.raises(ContentError):
                        await validate_receipt(
                            other, w.task_id, w.actor_id, receipt, "unit", shard
                        )
            elif change == "nested":
                async with db.begin_nested():
                    with pytest.raises(ContentError):
                        await validate_receipt(
                            db, w.task_id, w.actor_id, receipt, "unit", shard
                        )
            else:
                with pytest.raises(ContentError):
                    await validate_receipt(
                        db,
                        w.task_id,
                        w.actor_id,
                        receipt,
                        "mutation" if change == "kind" else "unit",
                        {**shard, "changed": True} if change == "payload" else shard,
                    )
        finally:
            await db.rollback()
            await owner.close()


async def test_repeated_cancellation_waits_for_actual_original_thread_close(
    repair_world, httpx_client, super_admin
):
    w, headers = repair_world, auth(super_admin)
    await plan(httpx_client, w, headers)
    await execute(httpx_client, w, headers)
    entered, release = threading.Event(), threading.Event()

    def block(spec):
        entered.set()
        assert release.wait(5), "Original test thread was abandoned"

    w.sources.capture_hook = block
    running = asyncio.create_task(forward(w))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        for _ in range(3):
            running.cancel()
            await asyncio.sleep(0)
        assert not running.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    _, _, annotation, predictions = await state(w)
    assert annotation.version == 1 and not predictions
    assert all(stream.closed for stream in w.sources.source_files)


async def test_unknown_original_close_remains_protected_after_second_close(
    repair_world, httpx_client, super_admin
):
    w, headers = repair_world, auth(super_admin)
    await plan(httpx_client, w, headers)
    await execute(httpx_client, w, headers)
    w.expect_unsettled = True

    def fail_close(stream):
        raise OSError("Controlled unknown original close")

    w.sources.source_close_hook = fail_close
    async with w.factory() as db:
        attempt = await start_attempt(db, str(w.batch_id), str(w.job_id), str(w.job_id))
        await db.commit()
        batch = await db.get(MaskRepairBatch, w.batch_id)
        owner = ManagedRepairUnit(
            db,
            w.factory,
            attempt,
            batch,
            list(batch.plan_json["items"]),
            "worker:managed@test",
        )
        await db.commit()
        with pytest.raises(BaseException) as first:
            await owner.open()
        assert getattr(first.value, "storage_operation_unsettled", False)
        await db.rollback()
        w.sources.source_close_hook = None
        for stream in w.sources.source_files:
            io.BytesIO.close(stream)
        for _ in range(2):
            with pytest.raises(ContentError) as closing:
                await owner.close()
            assert getattr(closing.value, "storage_operation_unsettled", False)
        async with w.factory() as check:
            assert (
                await check.scalar(select(func.count()).select_from(StorageOperation))
                > 0
            )
            assert (await check.get(Annotation, w.annotation_id)).version == 1
