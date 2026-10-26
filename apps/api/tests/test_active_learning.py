"""Manager-only replay, ordinary human batches and immutable observed feedback."""

import copy
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models.active_learning import ActiveLearningFeedback as Feedback
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.task_batch import TaskBatch
from app.db.models.task_event import TaskEvent
from app.db.models.trained_model import ModelRelease
from app.schemas.active_learning import DecisionCreate, FeedbackCreate
from app.services import (
    active_learning as service,
    active_learning_feedback as feedback,
)
from app.services.training_datasets import bind_training_authority
from tests.active_learning_fixtures import (
    complete,
    create,
    decide_with_ports,
    seed,
)


async def test_preview_rejects_libre_through_real_shared_qualification(
    db_session, super_admin, httpx_client, monkeypatch
):
    from app.services import (
        active_learning_pool,
        inference_backend_control,
        managed_inference,
        trained_models,
    )

    owner, token = super_admin
    world = await seed(db_session, owner, monkeypatch)
    monkeypatch.setattr(
        active_learning_pool, "qualification", managed_inference.qualification
    )
    reference_port = SimpleNamespace(
        freeze=AsyncMock(return_value={**world.ref, "framework": "libreyolo"})
    )
    monkeypatch.setattr(
        trained_models,
        "bind_frozen_model_reference",
        lambda _db: reference_port,
    )
    profile = AsyncMock()
    control = SimpleNamespace(managed_evaluation_profile=profile)
    monkeypatch.setattr(
        inference_backend_control, "backend_control_service", lambda _db: control
    )
    response = await httpx_client.post(
        f"/api/v1/projects/{world.project.id}/active-learning-rounds:preview",
        json=world.selection.model_dump(mode="json"),
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "framework_unqualified"
    profile.assert_not_awaited()


async def test_preview_creation_replay_and_budget_change_require_repreview(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    world = await seed(db_session, owner, monkeypatch)
    from app.workers import active_learning as worker

    monkeypatch.setattr(worker, "dispatch", AsyncMock())
    path = f"/api/v1/projects/{world.project.id}/active-learning-rounds"
    headers = {"Authorization": "Bearer " + token}
    preview = await httpx_client.post(
        path + ":preview", json=world.selection.model_dump(mode="json"), headers=headers
    )
    assert preview.status_code == 200, preview.text
    body = {
        **world.selection.model_dump(mode="json"),
        "request_id": str(uuid.uuid4()),
        "name": "Pool round",
        "preview_digest": preview.json()["preview_digest"],
    }
    stale = await httpx_client.post(path, json={**body, "seed": 43}, headers=headers)
    assert (
        stale.status_code == 409 and stale.json()["detail"]["code"] == "preview_changed"
    )
    first = await httpx_client.post(path, json=body, headers=headers)
    assert first.status_code == 202, first.text
    second = await httpx_client.post(path, json=body, headers=headers)
    assert second.status_code == 202 and second.json()["id"] == first.json()["id"]
    conflict = await httpx_client.post(
        path, json={**body, "name": "changed"}, headers=headers
    )
    assert conflict.status_code == 409
    assert first.json()["spec"]["candidate_count"] == 6
    assert (
        "pool" not in first.json()["spec"] and "exclusions" not in first.json()["spec"]
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.task_id.in_([task.id for task in world.tasks]))
        )
        == 0
    )


async def test_retry_resets_failed_candidates_only_and_keeps_frozen_receipts(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    world = await seed(db_session, owner, monkeypatch)
    round, attempt, _body = await create(db_session, owner, world)
    candidates = await service.candidate_rows(db_session, round)
    failed, inflight = candidates[:2]
    failed.status = "failed"
    failed.error_code, failed.error_message = "transient", "retry this candidate"
    failed.request = {"request": str(failed.request_id)}
    failed.media = {"media": "retained"}
    failed.evidence = None
    failed.produced_attempt_id = attempt.id
    inflight.status = "inflight"
    inflight.request = {"request": str(inflight.request_id)}
    inflight.media = {"media": "retained"}
    inflight.evidence = None
    inflight.produced_attempt_id = attempt.id
    retained = [
        {
            "request": copy.deepcopy(row.request),
            "media": copy.deepcopy(row.media),
            "evidence": copy.deepcopy(row.evidence),
            "produced_attempt_id": row.produced_attempt_id,
        }
        for row in (failed, inflight)
    ]
    request_ids = [row.request_id for row in candidates]
    frozen_spec, frozen_digest = copy.deepcopy(round.spec), round.spec_digest
    round.status = attempt.status = "lost"
    attempt.claim_id = uuid.uuid4()
    from app.workers import active_learning as worker

    monkeypatch.setattr(worker, "dispatch", AsyncMock())

    response = await httpx_client.post(
        f"/api/v1/projects/{world.project.id}/active-learning-rounds/{round.id}/retry",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202, response.text
    await db_session.refresh(round)
    await db_session.refresh(attempt)
    assert round.generation == 2 and round.spec == frozen_spec
    assert round.spec_digest == frozen_digest
    assert round.current_attempt_id != attempt.id
    assert attempt.claim_id is None
    assert [row.request_id for row in candidates] == request_ids
    assert failed.status == "pending"
    assert failed.error_code is None and failed.error_message is None
    assert inflight.status == "inflight"
    assert [
        {
            "request": row.request,
            "media": row.media,
            "evidence": row.evidence,
            "produced_attempt_id": row.produced_attempt_id,
        }
        for row in (failed, inflight)
    ] == retained
    assert round.inference_dispatches == 0
    assert response.json()["current_attempt_id"] == str(round.current_attempt_id)


@pytest.mark.parametrize("unavailable", ["failed_with_request", "expiry", "active"])
async def test_unavailable_retry_keeps_round_and_candidates_unchanged(
    db_session, super_admin, httpx_client, monkeypatch, unavailable
):
    owner, token = super_admin
    world = await seed(db_session, owner, monkeypatch)
    round, attempt, _body = await create(db_session, owner, world)
    candidates = await service.candidate_rows(db_session, round)
    candidate = candidates[0]
    candidate.status = "failed"
    candidate.error_code, candidate.error_message = "keep", "unchanged"
    round.status = attempt.status = "lost"
    attempt.claim_id = uuid.uuid4()
    if unavailable == "failed_with_request":
        round.status = attempt.status = "failed"
        candidate.request = {"request": str(candidate.request_id)}
    elif unavailable == "expiry":
        round.spec = {**round.spec, "expires_at": 0}
    else:
        round.status = attempt.status = "running"
    before = (
        round.generation,
        round.current_attempt_id,
        attempt.claim_id,
        attempt.completed_at,
        candidate.status,
        candidate.request,
        candidate.error_code,
        candidate.error_message,
    )
    from app.workers import active_learning as worker

    monkeypatch.setattr(worker, "dispatch", AsyncMock())

    response = await httpx_client.post(
        f"/api/v1/projects/{world.project.id}/active-learning-rounds/{round.id}/retry",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "retry_unavailable"
    assert (
        round.generation,
        round.current_attempt_id,
        attempt.claim_id,
        attempt.completed_at,
        candidate.status,
        candidate.request,
        candidate.error_code,
        candidate.error_message,
    ) == before


async def test_accept_subset_creates_ordinary_draft_replay_preserves_batch_and_reject_is_final(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    round, attempt, _ = await create(db_session, owner, world)
    report = await complete(db_session, round, attempt)
    selected = [
        next(
            row["ordinal"]
            for row in report["ranking"]
            if row["arm"] == arm and row["recommended"]
        )
        for arm in ("active", "random_control")
    ]
    body = DecisionCreate(
        request_id=uuid.uuid4(),
        action="accept",
        ordinals=selected,
        batch_name="Human review only",
    )
    decision = await decide_with_ports(db_session, world.project, owner, round.id, body)
    again = await decide_with_ports(db_session, world.project, owner, round.id, body)
    assert (
        again.id == decision.id
        and again.accepted_batch_id == decision.accepted_batch_id
    )
    batch = await db_session.get(TaskBatch, decision.accepted_batch_id)
    assert (
        batch.status == "draft"
        and batch.total_tasks == 2
        and batch.annotator_id is None
    )
    assert round.accepted_tasks == 2
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(ModelRelease)
            .where(ModelRelease.project_id == world.project.id)
        )
        == 0
    )
    assert all(task.status == "pending" and not task.is_labeled for task in world.tasks)
    rejected = next(
        row["ordinal"] for row in report["ranking"] if row["ordinal"] not in selected
    )
    await decide_with_ports(
        db_session,
        world.project,
        owner,
        round.id,
        DecisionCreate(request_id=uuid.uuid4(), action="reject", ordinals=[rejected]),
    )
    with pytest.raises(HTTPException) as error:
        await decide_with_ports(
            db_session,
            world.project,
            owner,
            round.id,
            DecisionCreate(
                request_id=uuid.uuid4(), action="reject", ordinals=[rejected]
            ),
        )
    assert error.value.detail["code"] == "candidate_decided"
    saved = copy.deepcopy(decision.snapshot)
    decision.accepted_batch_id = None
    await db_session.flush()
    assert service.decision_output(decision)["snapshot"] == saved


async def test_feedback_uses_actual_review_facts_union_sessions_and_keeps_corrections_unknown(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    round, attempt, _ = await create(db_session, owner, world)
    report = await complete(db_session, round, attempt)
    chosen = next(
        row
        for row in report["ranking"]
        if row["recommended"] and row["arm"] == "active"
    )
    decision = await decide_with_ports(
        db_session,
        world.project,
        owner,
        round.id,
        DecisionCreate(
            request_id=uuid.uuid4(), action="accept", ordinals=[chosen["ordinal"]]
        ),
    )
    decision.created_at = service.now() - timedelta(minutes=5)
    task = next(task for task in world.tasks if str(task.id) == chosen["task_id"])
    task.status, task.reviewed_at = "completed", service.now()
    db_session.add(
        Annotation(
            task_id=task.id,
            project_id=world.project.id,
            user_id=owner.id,
            source="manual",
            class_name="car",
            geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
            ground_truth=True,
        )
    )
    start = decision.created_at + timedelta(seconds=1)
    for offset, source in ((0, "session"), (30, "session"), (90, "legacy")):
        db_session.add(
            TaskEvent(
                task_id=task.id,
                project_id=world.project.id,
                user_id=owner.id,
                kind="annotate",
                started_at=start + timedelta(seconds=offset),
                ended_at=start + timedelta(seconds=offset + 60),
                duration_ms=60000,
                collection_source=source,
                collection_coverage="qualified"
                if source == "session"
                else "unverified_collection",
                collector_version="session-v2",
            )
        )
    db_session.add(
        AuditLog(
            actor_id=owner.id,
            action="task.reject",
            target_type="task",
            target_id=str(task.id),
            status_code=200,
            detail_json={"reason_type": "incomplete"},
            created_at=start,
        )
    )
    await db_session.flush()
    body = FeedbackCreate(request_id=uuid.uuid4())
    row = await feedback.snapshot(
        db_session,
        world.project,
        owner,
        round.id,
        body,
        training_authority=bind_training_authority(db_session),
    )
    snapshot = copy.deepcopy(row.snapshot)
    active, control = snapshot["arms"]
    assert active["reviewed_tasks"] == 1 and active["reviewer_rejection_count"] == 1
    assert active["effort"]["annotate"] == {"recorded_ms": 90000, "coverage": "partial"}
    assert active["effort"]["review"]["recorded_ms"] is None
    assert active["correction_count"] is None and control["accepted_tasks"] == 0
    assert (
        not snapshot["efficiency_claim"] and not snapshot["evaluations"]["comparable"]
    )
    task.status = "in_progress"
    again = await feedback.snapshot(
        db_session,
        world.project,
        owner,
        round.id,
        body,
        training_authority=bind_training_authority(db_session),
    )
    assert again.id == row.id and again.snapshot == snapshot
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Feedback)
            .where(Feedback.round_id == round.id)
        )
        == 1
    )


async def test_repeated_cancel_and_old_job_signal_preserve_fences_and_failure(
    db_session, super_admin, monkeypatch
):
    from app.application.job_terminal import apply_signal_terminal

    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    round, attempt, _ = await create(db_session, owner, world)
    row = (await service.candidate_rows(db_session, round))[0]
    row.request = service.managed.request_identity(
        round.spec, row, round.spec["samples"][0]["preprocessing"]
    )
    round.status = attempt.status = "running"
    await service.request_cancel(
        db_session, round, code="inference_budget_exhausted", message="No refund"
    )
    await service.request_cancel(db_session, round)
    assert (
        round.status == "cancelling"
        and round.error_code == "inference_budget_exhausted"
    )
    job = await db_session.get(AsyncJob, attempt.async_job_id)
    await apply_signal_terminal(db_session, job, cancelled=True)
    assert (
        round.status == "cancelling"
        and round.error_code == "inference_budget_exhausted"
        and job.status != "cancelled"
    )
    round.status = attempt.status = "lost"
    await service.new_attempt(db_session, round)
    await db_session.flush()
    await apply_signal_terminal(db_session, job, cancelled=True)
    assert round.status == "queued" and round.current_attempt_id != attempt.id


async def test_partial_later_evaluations_keep_unknown_delta(
    db_session, super_admin, monkeypatch
):
    from app.schemas.active_learning import EvaluationReference
    from tests.sample_evaluation_fixtures import evaluation

    owner, _ = super_admin
    world = await seed(db_session, owner, monkeypatch)
    round, _attempt, _ = await create(db_session, owner, world)
    refs = []
    for _ in range(2):
        run, attempt = await evaluation(db_session, owner, world)
        attempt.report = {"sha256": "a" * 64}
        refs.append(EvaluationReference(evaluation_id=run.id, attempt_id=attempt.id))
    monkeypatch.setattr(
        feedback.sample_evaluations,
        "read_report",
        AsyncMock(
            return_value={"status": "cancelled", "complete": False, "aggregate": None}
        ),
    )
    result = await feedback.evaluation_facts(db_session, world.project, round, refs)
    assert not result["comparable"] and result["delta_second_minus_first"] is None
    assert "取消" in result["reasons"][0]
