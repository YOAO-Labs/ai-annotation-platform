from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.mask_qc import MaskQCIssue, MaskQCRun
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.mask_qc.topology import rle_and_not, rle_or
from app.services.mask_repair import canonical_digest
from app.services.raster_mask_storage import build_rle_reference
from app.utils.raster_mask_rle import coco_rle_area, encode_coco_rle
from app.workers.mask_repair import _execute_shard, _rollback_shard
from tests.factory import create_user


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rle(rows: list[str]) -> dict:
    height = len(rows)
    width = len(rows[0])
    pixels = [1 if value == "#" else 0 for row in rows for value in row]
    return encode_coco_rle(pixels, width, height)


async def _seed_repair_issue(db, *, owner_id: uuid.UUID, locked: bool = False):
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        display_id=f"P-RPR-{suffix}",
        name=f"Mask repair {suffix}",
        type_label="图像分割",
        type_key="image-seg",
        data_type="image",
        owner_id=owner_id,
        raster_mask_native_editing_enabled=True,
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "object"}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    db.add(project)
    await db.flush()
    dataset = Dataset(
        display_id=f"D-RPR-{suffix}",
        name=f"Mask repair {suffix}",
        data_type="image",
        created_by=owner_id,
    )
    db.add(dataset)
    await db.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="repair.png",
        file_path="repair.png",
        file_type="image",
        width=5,
        height=3,
    )
    db.add(item)
    await db.flush()
    # The review task carries a distinct literal annotator as its frozen round
    # contributor, so the owner's QC repair is not a self-review of own work.
    author = await create_user(
        db, "employee", f"rpr-author-{suffix}@test.local", "Repair Author"
    )
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-RPR-{suffix}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
        status="review",
        reviewer_id=owner_id,
        annotation_contributor_ids=[str(author.id)],
        review_contributor_ids=[str(author.id)],
        review_submitter_id=author.id,
        review_round_id=uuid.uuid4(),
    )
    db.add(task)
    await db.flush()
    source = _rle([".....", ".###.", "....#"])
    island = _rle([".....", ".....", "....#"])
    source_ref = build_rle_reference(source)
    island_ref = build_rle_reference(island)
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=author.id,
        source="manual",
        annotation_type="raster_mask",
        tool_unit_id="region",
        class_name="object",
        geometry={"type": "raster_mask", "mask": source_ref},
        is_active=True,
        was_cancelled=False,
        is_locked=locked,
        version=1,
    )
    db.add(annotation)
    await db.flush()
    issue = MaskQCIssue(
        project_id=project.id,
        task_id=task.id,
        annotation_id=annotation.id,
        annotation_version=1,
        related_annotation_ids=[annotation.id],
        source_versions={str(annotation.id): 1},
        code="small_island",
        severity="warning",
        severity_rank=1,
        status="open",
        metric={"area_pixels": 1},
        threshold={"small_component_pixels": 32},
        region_bbox={"x0": 0.8, "y0": 2 / 3, "x1": 1.0, "y1": 1.0},
        region_mask_ref=island_ref,
        region_digest=island_ref["sha256"],
        dedupe_key=hashlib.sha256(f"repair-{suffix}".encode()).hexdigest(),
        source={"kind": "single_frame"},
    )
    db.add(issue)
    await db.flush()
    return project, task, annotation, issue, source, island


@pytest.mark.asyncio
async def test_dry_run_freezes_exact_pixels_and_hashes_receipt(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, _task, annotation, issue, source, island = await _seed_repair_issue(
        db_session, owner_id=user.id
    )
    rles = {
        build_rle_reference(source)["sha256"]: source,
        build_rle_reference(island)["sha256"]: island,
    }

    async def fake_load(reference):
        return rles[reference["sha256"]]

    monkeypatch.setattr("app.services.mask_repair.load_coco_rle", fake_load)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-qc/repairs:dry-run",
        json={"actions": [{"issue_id": str(issue.id), "kind": "delete_small_islands"}]},
        headers=_bearer(token),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"] == {
        "action_count": 1,
        "executable_count": 1,
        "skipped_count": 0,
        "mutation_count": 1,
        "candidate_count": 0,
        "changed_pixels": 1,
        "shard_count": 1,
    }
    assert body["items"][0]["annotation_ids"] == [str(annotation.id)]
    assert body["items"][0]["scope_fingerprint"]
    batch = (
        await db_session.execute(
            select(MaskRepairBatch).where(
                MaskRepairBatch.plan_digest == body["plan_digest"]
            )
        )
    ).scalar_one()
    assert batch.token_hash == hashlib.sha256(body["receipt"].encode()).hexdigest()
    assert body["receipt"] not in str(batch.request_json)
    assert body["receipt"] not in str(batch.plan_json)
    assert canonical_digest(batch.plan_json) == body["plan_digest"]
    planned_rle = batch.plan_json["items"][0]["result_rle"]
    assert planned_rle == rle_and_not(source, island)
    assert coco_rle_area(planned_rle) == 3


@pytest.mark.asyncio
async def test_dry_run_reports_stable_locked_skip(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, _task, _annotation, issue, source, island = await _seed_repair_issue(
        db_session, owner_id=user.id, locked=True
    )
    rles = {
        build_rle_reference(source)["sha256"]: source,
        build_rle_reference(island)["sha256"]: island,
    }

    async def fake_load(reference):
        return rles[reference["sha256"]]

    monkeypatch.setattr("app.services.mask_repair.load_coco_rle", fake_load)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-qc/repairs:dry-run",
        json={"actions": [{"issue_id": str(issue.id), "kind": "delete_small_islands"}]},
        headers=_bearer(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["skip_code"] == "annotation_locked"
    assert response.json()["summary"]["executable_count"] == 0


@pytest.mark.asyncio
async def test_dry_run_skips_issue_from_changed_qc_policy(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, task, _annotation, issue, source, island = await _seed_repair_issue(
        db_session, owner_id=user.id
    )
    run = MaskQCRun(
        project_id=project.id,
        requested_by_id=user.id,
        status="completed",
        progress_pct=100,
        scope_json={"scope": "task_ids", "task_ids": [str(task.id)]},
        config_revision=1,
        config_digest="0" * 64,
        config_snapshot={},
        source_snapshot=[],
        source_snapshot_digest="1" * 64,
        task_snapshot_digests={},
        singleflight_key="2" * 64,
        summary={},
    )
    db_session.add(run)
    await db_session.flush()
    issue.run_id = run.id
    rles = {
        build_rle_reference(source)["sha256"]: source,
        build_rle_reference(island)["sha256"]: island,
    }

    async def fake_load(reference):
        return rles[reference["sha256"]]

    monkeypatch.setattr("app.services.mask_repair.load_coco_rle", fake_load)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-qc/repairs:dry-run",
        json={"actions": [{"issue_id": str(issue.id), "kind": "delete_small_islands"}]},
        headers=_bearer(token),
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["skip_code"] == "blocker_policy_conflict"
    assert response.json()["summary"]["executable_count"] == 0


@pytest.mark.asyncio
async def test_execute_rejects_expired_receipt(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, _task, _annotation, issue, source, island = await _seed_repair_issue(
        db_session, owner_id=user.id
    )
    rles = {
        build_rle_reference(source)["sha256"]: source,
        build_rle_reference(island)["sha256"]: island,
    }

    async def fake_load(reference):
        return rles[reference["sha256"]]

    monkeypatch.setattr("app.services.mask_repair.load_coco_rle", fake_load)
    await db_session.commit()
    dry_run = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-qc/repairs:dry-run",
        json={"actions": [{"issue_id": str(issue.id), "kind": "delete_small_islands"}]},
        headers=_bearer(token),
    )
    body = dry_run.json()
    batch = (
        await db_session.execute(
            select(MaskRepairBatch).where(
                MaskRepairBatch.plan_digest == body["plan_digest"]
            )
        )
    ).scalar_one()
    batch.receipt_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-qc/repairs",
        json={"receipt": body["receipt"], "plan_digest": body["plan_digest"]},
        headers=_bearer(token),
    )

    assert response.status_code == 410
    assert response.json()["detail"]["reason"] == "repair_receipt_expired"


@pytest.mark.asyncio
async def test_execute_dispatches_once_and_rollback_rejects_newer_annotation(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, _task, annotation, issue, source, island = await _seed_repair_issue(
        db_session, owner_id=user.id
    )
    rles = {
        build_rle_reference(source)["sha256"]: source,
        build_rle_reference(island)["sha256"]: island,
    }

    async def fake_load(reference):
        return rles[reference["sha256"]]

    monkeypatch.setattr("app.services.mask_repair.load_coco_rle", fake_load)
    await db_session.commit()
    dry_run = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-qc/repairs:dry-run",
        json={"actions": [{"issue_id": str(issue.id), "kind": "delete_small_islands"}]},
        headers=_bearer(token),
    )
    plan = dry_run.json()
    dispatches: list[tuple[uuid.UUID, bool]] = []

    async def fake_dispatch(batch_id: uuid.UUID, *, rollback: bool = False) -> str:
        dispatches.append((batch_id, rollback))
        return "celery-mask-repair-1"

    monkeypatch.setattr("app.api.v1.mask_qc.dispatch_repair_batch", fake_dispatch)
    payload = {"receipt": plan["receipt"], "plan_digest": plan["plan_digest"]}
    first = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-qc/repairs",
        json=payload,
        headers=_bearer(token),
    )
    replay = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-qc/repairs",
        json=payload,
        headers=_bearer(token),
    )

    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert len(dispatches) == 1

    batch = await db_session.get(MaskRepairBatch, uuid.UUID(first.json()["id"]))
    assert batch is not None
    private_item = batch.plan_json["items"][0]
    # Plans written before optional-null omission must remain executable.
    private_item["payload"]["cut_path"] = None
    result_rle = private_item["result_rle"]
    rles[private_item["result_reference"]["sha256"]] = result_rle
    monkeypatch.setattr("app.services.mask_mutation.load_coco_rle", fake_load)

    async def no_store(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.workers.mask_repair._store_result_rle", no_store)
    monkeypatch.setattr(
        "app.services.mask_mutation.prepare_mask_payload_for_write", no_store
    )
    shard_result = await _execute_shard(
        db_session,
        batch=batch,
        shard=batch.plan_json["shards"][0],
        actor=user,
    )
    assert shard_result["status"] == "completed"
    assert shard_result["items"][0]["changed_pixels"] == 1
    assert issue.status == "resolved"

    await db_session.refresh(annotation)
    annotation.version = shard_result["items"][0]["after_version"] + 1
    await db_session.flush()
    with pytest.raises(RuntimeError, match="rollback_version_conflict"):
        await _rollback_shard(
            db_session,
            plan_items=batch.plan_json["items"],
            shard_result=shard_result,
            actor=user,
        )


def test_fill_holes_union_is_exact_and_gc_queries_cover_repair_ledger():
    source = _rle(["###", "#.#", "###"])
    hole = _rle(["...", ".#.", "..."])
    assert coco_rle_area(rle_or(source, hole)) == 9

    from app.workers.cleanup import (
        _MASK_REFERENCE_EXISTS_QUERIES,
        _MASK_REFERENCE_QUERIES,
    )

    assert any("mask_repair_batches" in query for query in _MASK_REFERENCE_QUERIES)
    assert any(
        "mask_repair_batches" in query for query in _MASK_REFERENCE_EXISTS_QUERIES
    )
