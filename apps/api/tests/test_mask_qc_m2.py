from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select, text

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJobStatus
from app.db.models.mask_qc import MaskQCIssue, MaskQCRun
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.schemas.mask_qc import MaskQCConfig, MaskQCRunRequest
from app.services import async_job as async_job_svc
from app.services.mask_qc.config import mask_qc_config_digest
from app.services.mask_qc.service import (
    canonical_digest,
    create_mask_qc_run,
    current_task_source_snapshot,
    list_issues,
    qc_digest_for_issues,
)
from app.services.raster_mask_storage import build_rle_reference
from app.utils.raster_mask_rle import encode_coco_rle
from app.workers.mask_qc import _execute_scan
from app.workers.cleanup import _referenced_raster_mask_keys
from tests.factory import create_membership


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rle(rows: list[str]) -> dict:
    height = len(rows)
    width = len(rows[0])
    pixels = [1 if value == "#" else 0 for row in rows for value in row]
    return encode_coco_rle(pixels, width, height)


async def _seed_mask(
    db,
    *,
    owner_id: uuid.UUID,
    config: MaskQCConfig | None = None,
    status: str = "in_progress",
    annotator_id: uuid.UUID | None = None,
) -> tuple[Project, Task, Annotation, dict]:
    suffix = uuid.uuid4().hex[:8]
    # The annotation author/effective annotator defaults to the owner; callers
    # testing a manager review pass a distinct literal annotator so the frozen
    # round evidence never names the reviewing actor.
    worker_id = annotator_id or owner_id
    project = Project(
        display_id=f"P-QC-{suffix}",
        name=f"Mask QC {suffix}",
        type_label="图像-分割",
        type_key="image-seg",
        owner_id=owner_id,
        mask_qc_config=(config or MaskQCConfig()).model_dump(mode="json"),
        tool_bindings={
            "region": {
                "enabled": True,
                "classes": [{"name": "object", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    db.add(project)
    await db.flush()
    evidence: dict = {}
    if status == "review":
        evidence = {
            "annotation_contributor_ids": [str(worker_id)],
            "review_contributor_ids": [str(worker_id)],
            "review_submitter_id": worker_id,
            "review_round_id": uuid.uuid4(),
        }
    task = Task(
        project_id=project.id,
        display_id=f"T-QC-{suffix}",
        file_name="mask.png",
        file_path="/tmp/mask.png",
        file_type="image",
        status=status,
        assignee_id=worker_id,
        **evidence,
    )
    db.add(task)
    await db.flush()
    rle = _rle(
        [
            "........",
            "........",
            "..#.....",
            "........",
            "........",
            "........",
            "........",
            "........",
        ]
    )
    reference = build_rle_reference(rle)
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=worker_id,
        source="manual",
        annotation_type="mask",
        tool_unit_id="region",
        class_name="object",
        geometry={"type": "raster_mask", "mask": reference},
        is_active=True,
        was_cancelled=False,
        version=1,
    )
    db.add(annotation)
    await db.flush()
    return project, task, annotation, rle


@pytest.mark.asyncio
async def test_mask_qc_config_patch_is_revisioned_and_rejects_null(
    httpx_client, super_admin, db_session
):
    user, token = super_admin
    project, _task, _annotation, _rle_payload = await _seed_mask(
        db_session, owner_id=user.id
    )
    await db_session.commit()

    payload = MaskQCConfig().model_dump(mode="json")
    payload["blocking"] = True
    response = await httpx_client.patch(
        f"/api/v1/projects/{project.id}",
        json={"mask_qc_config": payload},
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["mask_qc_config"]["config_revision"] == 2

    response = await httpx_client.patch(
        f"/api/v1/projects/{project.id}",
        json={"mask_qc_config": payload},
        headers=_bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "reason": "mask_qc_config_revision_conflict",
        "expected": 1,
        "actual": 2,
    }

    response = await httpx_client.patch(
        f"/api/v1/projects/{project.id}",
        json={"mask_qc_config": None},
        headers=_bearer(token),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_mask_qc_run_singleflight_uses_resolved_versions(db_session, super_admin):
    user, _token = super_admin
    project, task, annotation, _rle_payload = await _seed_mask(
        db_session, owner_id=user.id
    )
    first, first_job, created = await create_mask_qc_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=MaskQCRunRequest(
            scope="annotation_ids",
            annotation_ids=[annotation.id, annotation.id],
        ),
    )
    assert created is True
    assert first_job is not None

    second, second_job, created = await create_mask_qc_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=MaskQCRunRequest(
            scope="annotation_ids",
            annotation_ids=[annotation.id],
            expected_versions={annotation.id.hex.upper(): 1},
        ),
    )
    assert created is False
    assert second_job is None
    assert second.id == first.id
    assert first.scope_json["task_ids"] == [str(task.id)]
    assert first.scope_json["expected_versions"] == {str(annotation.id): 1}


@pytest.mark.asyncio
async def test_mask_qc_worker_dedupes_findings_and_advances_last_seen(
    db_session, super_admin, monkeypatch
):
    user, _token = super_admin
    project, _task, _annotation, rle = await _seed_mask(db_session, owner_id=user.id)
    reference = build_rle_reference(rle)

    async def fake_load(value):
        assert value["sha256"] == reference["sha256"]
        return rle

    async def fake_store(value):
        return build_rle_reference(value)

    monkeypatch.setattr("app.workers.mask_qc.load_coco_rle", fake_load)
    monkeypatch.setattr("app.workers.mask_qc.store_coco_rle", fake_store)

    first, first_job, _created = await create_mask_qc_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=MaskQCRunRequest(scope="project"),
    )
    assert first_job is not None
    first.status = "running"
    await async_job_svc.mark_running(db_session, first_job.id)
    await _execute_scan(db_session, first)
    await db_session.refresh(first)
    assert first.status == "completed"
    assert first.progress_pct == 100
    first_count = int(
        (
            await db_session.execute(select(func.count()).select_from(MaskQCIssue))
        ).scalar_one()
    )
    assert first_count >= 2

    second, second_job, created = await create_mask_qc_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=MaskQCRunRequest(scope="project"),
    )
    assert created is True
    assert second_job is not None
    second.status = "running"
    await async_job_svc.mark_running(db_session, second_job.id)
    await _execute_scan(db_session, second)

    second_count = int(
        (
            await db_session.execute(select(func.count()).select_from(MaskQCIssue))
        ).scalar_one()
    )
    assert second_count == first_count
    assert set(
        (await db_session.execute(select(MaskQCIssue.last_seen_run_id))).scalars()
    ) == {second.id}
    await db_session.refresh(second_job)
    assert second_job.status == AsyncJobStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_mask_qc_api_returns_versions_and_filters_effective_stale(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, _task, annotation, _rle_payload = await _seed_mask(
        db_session, owner_id=user.id
    )
    dispatch = MagicMock()
    monkeypatch.setattr("app.workers.mask_qc.run_mask_qc.apply_async", dispatch)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/mask-qc/runs",
        json={"scope": "annotation_ids", "annotation_ids": [str(annotation.id)]},
        headers=_bearer(token),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["source_versions"] == {str(annotation.id): 1}
    dispatch.assert_called_once()

    run = await db_session.get(MaskQCRun, uuid.UUID(body["id"]))
    issue = MaskQCIssue(
        run_id=run.id,
        last_seen_run_id=run.id,
        project_id=project.id,
        task_id=annotation.task_id,
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
        dedupe_key="a" * 64,
        source={},
    )
    db_session.add(issue)
    annotation.version = 2
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/mask-qc/issues?status=stale",
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [str(issue.id)]
    assert response.json()["items"][0]["effective_status"] == "stale"

    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/mask-qc/issues?status=open",
        headers=_bearer(token),
    )
    assert response.status_code == 200
    assert response.json()["items"] == []

    response = await httpx_client.patch(
        f"/api/v1/mask-qc/issues/{issue.id}",
        json={"status": "resolved"},
        headers=_bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "mask_qc_issue_stale"


@pytest.mark.asyncio
async def test_mask_qc_issue_cursor_is_stable_when_timestamps_match(
    db_session, super_admin
):
    user, _token = super_admin
    project, task, annotation, _rle_payload = await _seed_mask(
        db_session, owner_id=user.id
    )
    run, _job, _created = await create_mask_qc_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=MaskQCRunRequest(scope="project"),
    )
    created_at = datetime.now(timezone.utc)
    issues = [
        MaskQCIssue(
            run_id=run.id,
            last_seen_run_id=run.id,
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
            metric={},
            threshold={},
            dedupe_key=character * 64,
            source={},
            created_at=created_at,
        )
        for character in ("e", "f")
    ]
    db_session.add_all(issues)
    await db_session.flush()

    first_page, cursor = await list_issues(db_session, project_id=project.id, limit=1)
    assert cursor is not None
    second_page, next_cursor = await list_issues(
        db_session, project_id=project.id, limit=1, cursor=cursor
    )

    assert next_cursor is None
    assert {first_page[0].id, second_page[0].id} == {issue.id for issue in issues}


@pytest.mark.asyncio
async def test_mask_qc_project_issue_list_hides_invisible_tasks_from_reviewer(
    httpx_client, super_admin, reviewer, db_session
):
    owner, _owner_token = super_admin
    review_user, review_token = reviewer
    project, visible_task, annotation, _rle_payload = await _seed_mask(
        db_session, owner_id=owner.id
    )
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=review_user.id,
        role="reviewer",
        assigned_by=owner.id,
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-QC-{uuid.uuid4().hex[:8]}",
        name="visible review batch",
        status="reviewing",
        total_tasks=1,
        review_tasks=1,
    )
    db_session.add(batch)
    await db_session.flush()
    visible_task.batch_id = batch.id
    invisible_task = Task(
        project_id=project.id,
        display_id=f"T-QC-{uuid.uuid4().hex[:8]}",
        file_name="hidden.png",
        file_path="/tmp/hidden.png",
        file_type="image",
        status="review",
        assignee_id=owner.id,
    )
    db_session.add(invisible_task)
    await db_session.flush()
    run, _job, _created = await create_mask_qc_run(
        db_session,
        project=project,
        actor_id=owner.id,
        request=MaskQCRunRequest(scope="project"),
    )
    hidden_annotation_id = uuid.uuid4()
    issues = [
        MaskQCIssue(
            run_id=run.id,
            last_seen_run_id=run.id,
            project_id=project.id,
            task_id=task_id,
            annotation_id=annotation_id,
            annotation_version=1,
            related_annotation_ids=[annotation_id],
            source_versions={str(annotation_id): 1},
            code="small_island",
            severity="warning",
            severity_rank=1,
            status="open",
            metric={},
            threshold={},
            dedupe_key=character * 64,
            source={},
        )
        for task_id, annotation_id, character in (
            (visible_task.id, annotation.id, "1"),
            (invisible_task.id, hidden_annotation_id, "2"),
        )
    ]
    db_session.add_all(issues)
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/projects/{project.id}/mask-qc/issues",
        headers=_bearer(review_token),
    )

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [str(issues[0].id)]


@pytest.mark.asyncio
async def test_blocking_qc_prevents_approve_until_blocker_resolved(
    httpx_client, super_admin, annotator, db_session
):
    user, token = super_admin
    contrib, _ = annotator
    config = MaskQCConfig(blocking=True)
    project, task, annotation, _rle_payload = await _seed_mask(
        db_session,
        owner_id=user.id,
        config=config,
        status="review",
        annotator_id=contrib.id,
    )
    snapshot, source_digest = await current_task_source_snapshot(
        db_session, task_id=task.id
    )
    assert source_digest is not None
    run = MaskQCRun(
        project_id=project.id,
        requested_by_id=user.id,
        status="completed",
        progress_pct=100,
        scope_json={"scope": "task_ids", "task_ids": [str(task.id)]},
        config_revision=config.config_revision,
        config_digest=mask_qc_config_digest(config),
        config_snapshot=config.model_dump(mode="json"),
        source_snapshot=snapshot,
        source_snapshot_digest=canonical_digest(snapshot),
        task_snapshot_digests={str(task.id): source_digest},
        singleflight_key="b" * 64,
        summary={},
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    await db_session.flush()
    issue = MaskQCIssue(
        run_id=run.id,
        last_seen_run_id=run.id,
        project_id=project.id,
        task_id=task.id,
        annotation_id=annotation.id,
        annotation_version=1,
        related_annotation_ids=[annotation.id],
        source_versions={str(annotation.id): 1},
        code="empty_mask",
        severity="blocker",
        severity_rank=0,
        status="open",
        metric={"area_pixels": 0},
        threshold={},
        dedupe_key="c" * 64,
        source={},
    )
    db_session.add(issue)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/approve",
        headers=_bearer(token),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "mask_qc_blockers_present"

    issue.status = "resolved"
    issue.resolved_by_id = user.id
    issue.resolved_at = datetime.now(timezone.utc)
    await db_session.commit()
    digest = await qc_digest_for_issues(db_session, run=run, task_id=task.id)
    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/approve",
        json={"expected_qc_digest": digest, "note": "blocker checked"},
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(task)
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_non_privileged_reviewer_must_claim_before_approve(
    httpx_client, super_admin, reviewer, db_session
):
    owner, _owner_token = super_admin
    review_user, review_token = reviewer
    project, task, _annotation, _rle_payload = await _seed_mask(
        db_session, owner_id=owner.id, status="review"
    )
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=review_user.id,
        role="reviewer",
        assigned_by=owner.id,
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-QC-{uuid.uuid4().hex[:8]}",
        name="QC review",
        status="reviewing",
        total_tasks=1,
        review_tasks=1,
    )
    db_session.add(batch)
    await db_session.flush()
    task.batch_id = batch.id
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/approve",
        headers=_bearer(review_token),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "task_review_not_claimed_by_user"

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/claim",
        headers=_bearer(review_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["reviewer_id"] == str(review_user.id)

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/review/approve",
        headers=_bearer(review_token),
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_submit_keeps_review_transition_when_qc_dispatch_fails(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    _project, task, _annotation, _rle_payload = await _seed_mask(
        db_session, owner_id=user.id
    )
    monkeypatch.setattr(
        "app.workers.mask_qc.run_mask_qc.apply_async",
        MagicMock(side_effect=RuntimeError("broker unavailable")),
    )
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/submit",
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["mask_qc_status"] == "failed"
    await db_session.refresh(task)
    assert task.status == "review"
    run = (
        await db_session.execute(
            select(MaskQCRun).where(MaskQCRun.project_id == task.project_id)
        )
    ).scalar_one()
    assert run.status == "failed"
    assert "broker unavailable" in (run.error_message or "")


@pytest.mark.asyncio
async def test_mask_qc_cancel_updates_generic_and_domain_ledgers(
    httpx_client, super_admin, db_session, monkeypatch
):
    user, token = super_admin
    project, _task, _annotation, _rle_payload = await _seed_mask(
        db_session, owner_id=user.id
    )
    run, job, _created = await create_mask_qc_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=MaskQCRunRequest(scope="project"),
    )
    assert job is not None
    revoke = MagicMock()
    monkeypatch.setattr("app.workers.celery_app.celery_app.control.revoke", revoke)
    await db_session.commit()

    response = await httpx_client.post(
        f"/api/v1/async-jobs/{job.id}/cancel",
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(run)
    await db_session.refresh(job)
    assert run.status == "cancelled"
    assert job.status == AsyncJobStatus.CANCELLED.value
    revoke.assert_called_once_with(job.celery_task_id, terminate=False)


@pytest.mark.asyncio
async def test_mask_qc_source_and_issue_regions_are_gc_roots(db_session, super_admin):
    user, _token = super_admin
    project, task, annotation, rle = await _seed_mask(db_session, owner_id=user.id)
    source_reference = build_rle_reference(rle)
    region_reference = build_rle_reference(
        _rle(
            [
                "........",
                "........",
                "...#....",
                "........",
                "........",
                "........",
                "........",
                "........",
            ]
        )
    )
    run, _job, _created = await create_mask_qc_run(
        db_session,
        project=project,
        actor_id=user.id,
        request=MaskQCRunRequest(scope="project"),
    )
    issue = MaskQCIssue(
        run_id=run.id,
        last_seen_run_id=run.id,
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
        metric={},
        threshold={},
        region_mask_ref=region_reference,
        region_digest=region_reference["sha256"],
        dedupe_key="d" * 64,
        source={},
    )
    db_session.add(issue)
    await db_session.flush()

    roots = await _referenced_raster_mask_keys(db_session)
    assert source_reference["object_key"] in roots
    assert region_reference["object_key"] in roots


@pytest.mark.asyncio
async def test_migration_0144_has_qc_constraints_and_indexes(db_session):
    constraints = set(
        (
            await db_session.execute(
                text(
                    """
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid IN (
                        'mask_qc_runs'::regclass,
                        'mask_qc_issues'::regclass
                    )
                    """
                )
            )
        ).scalars()
    )
    assert {
        "ck_mask_qc_runs_config_revision",
        "ck_mask_qc_issues_annotation_version",
        "ck_mask_qc_issues_frames",
        "uq_mask_qc_issues_dedupe",
    } <= constraints
    indexes = set(
        (
            await db_session.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE tablename IN ('mask_qc_runs', 'mask_qc_issues')
                    """
                )
            )
        ).scalars()
    )
    assert {
        "uq_mask_qc_runs_active_singleflight",
        "ix_mask_qc_issues_project_order",
    } <= indexes
