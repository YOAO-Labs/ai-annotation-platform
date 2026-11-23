from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

import pytest

from app.observability.mask_ai import refresh_mask_ai_inventory
from app.observability.metrics import (
    MASK_AI_ACCEPT_DECISIONS,
    MASK_AI_CORRECTION_JOBS,
    MASK_AI_CORRECTION_OLDEST_AGE_SECONDS,
    MASK_AI_OLDEST_EXPIRED_DECISION_AGE_SECONDS,
    MASK_AI_OPERATIONS_TOTAL,
    MASK_AI_PHASE_DURATION_SECONDS,
    MASK_AI_STAGED_MASK_REFERENCES,
    mask_ai_candidate_count_bucket,
    mask_ai_operation,
    mask_ai_prompt_family,
    observe_mask_ai_phase,
    record_mask_ai_operation,
)


def _counter_value(**labels: str) -> float:
    return float(MASK_AI_OPERATIONS_TOTAL.labels(**labels)._value.get())


def test_mask_ai_operation_metric_uses_only_controlled_labels() -> None:
    labels = {
        "operation": "single_frame",
        "prompt_family": "unknown",
        "output_geometry": "unknown",
        "candidate_count": "11_plus",
        "decision": "none",
        "fallback_reason": "unknown",
        "outcome": "error",
    }
    before = _counter_value(**labels)
    record_mask_ai_operation(
        operation="task-8de9d047",
        prompt_family="user secret",
        output_geometry="raster-masks/sha256/secret",
        candidate_count=99,
        decision="annotation-id",
        fallback_reason="backend-id",
        outcome="unexpected",
    )
    assert _counter_value(**labels) == before + 1


def test_mask_ai_classifiers_and_phase_histogram() -> None:
    assert mask_ai_operation({"type": "point"}) == "single_frame"
    assert mask_ai_operation({"type": "scribble", "mask_input": "opaque"}) == "refine"
    assert mask_ai_prompt_family({"type": "interactive_box"}) == "bbox"
    assert mask_ai_prompt_family({"type": "text"}) == "text"
    assert [mask_ai_candidate_count_bucket(value) for value in (0, 1, 3, 10, 11)] == [
        "0",
        "1",
        "2_3",
        "4_10",
        "11_plus",
    ]
    child = MASK_AI_PHASE_DURATION_SECONDS.labels(
        operation="refine", phase="commit", outcome="conflict"
    )
    before = float(child._sum.get())
    observe_mask_ai_phase(
        operation="refine",
        phase="commit",
        outcome="conflict",
        duration_seconds=0.25,
    )
    assert float(child._sum.get()) == pytest.approx(before + 0.25)


@pytest.mark.asyncio
async def test_inventory_refresh_sets_fixed_zero_series() -> None:
    db = SimpleNamespace(
        scalar=AsyncMock(
            return_value={
                "correction_jobs": {"queued": 2, "accepted": 3},
                "correction_oldest_age_seconds": {"queued": 12.5},
                "staged_mask_references": {"correction": 4},
                "accept_decisions": {"active": 5, "expired": 1},
                "oldest_expired_decision_age_seconds": 42.0,
            }
        )
    )

    result = await refresh_mask_ai_inventory(db)

    assert result["correction_jobs"] == {"queued": 2, "accepted": 3}
    assert MASK_AI_CORRECTION_JOBS.labels(status="queued")._value.get() == 2
    assert MASK_AI_CORRECTION_JOBS.labels(status="running")._value.get() == 0
    assert (
        MASK_AI_CORRECTION_OLDEST_AGE_SECONDS.labels(status="queued")._value.get()
        == 12.5
    )
    assert (
        MASK_AI_STAGED_MASK_REFERENCES.labels(job_kind="correction")._value.get() == 4
    )
    assert MASK_AI_STAGED_MASK_REFERENCES.labels(job_kind="tracking")._value.get() == 0
    assert MASK_AI_ACCEPT_DECISIONS.labels(state="active")._value.get() == 5
    assert MASK_AI_ACCEPT_DECISIONS.labels(state="expired")._value.get() == 1
    assert MASK_AI_OLDEST_EXPIRED_DECISION_AGE_SECONDS._value.get() == 42


@pytest.mark.asyncio
async def test_inventory_refresh_queries_the_real_schema(db_session) -> None:
    result = await refresh_mask_ai_inventory(db_session)

    assert set(result) == {
        "correction_jobs",
        "correction_oldest_age_seconds",
        "staged_mask_references",
        "accept_decisions",
        "oldest_expired_decision_age_seconds",
    }
    assert set(result["accept_decisions"]) == {"active", "expired"}


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the disposable verifier and the ordinary API LOGIN",
)
async def test_metrics_reads_current_tracker_and_accept_ledger_as_api_login(
    test_engine, app_module
):
    from prometheus_client.parser import text_string_to_metric_families
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.models.ai_mask_accept_decision import AiMaskAcceptDecision
    from tests.factory import create_user
    from tests.test_project_orphan_cleanup_transactions import _asgi_client_for_maker
    from tests.test_video_tracker_jobs_list import _make_job, _make_video_task
    from tests.test_workspace_project_authority_locking import _api_url

    async def scrape(client):
        response = await client.get("/metrics")
        assert response.status_code == 200, response.text
        return {
            (sample.name, tuple(sorted(sample.labels.items()))): sample.value
            for family in text_string_to_metric_families(response.text)
            for sample in family.samples
            if sample.name.startswith("mask_ai_")
        }

    api_engine = create_async_engine(_api_url(test_engine))
    api_maker = async_sessionmaker(api_engine, expire_on_commit=False)
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    context = None
    try:
        async with api_maker() as restricted:
            assert (
                await restricted.scalar(text("SELECT session_user"))
                == "identity_api_consumer"
            )
            for statement in (
                "SELECT id FROM public.video_tracker_jobs LIMIT 1",
                "SELECT id FROM public.ai_mask_accept_decisions LIMIT 1",
            ):
                with pytest.raises(DBAPIError) as denied:
                    async with restricted.begin_nested():
                        await restricted.execute(text(statement))
                assert denied.value.orig.sqlstate == "42501"
        async with _asgi_client_for_maker(app_module, api_maker) as client:
            before = await scrape(client)
            now = datetime.now(timezone.utc)
            suffix = uuid.uuid4().hex
            async with owner.begin() as db:
                actor = await create_user(
                    db,
                    "super_admin",
                    f"mask-metrics-{suffix}@example.test",
                    "Metrics owner",
                )
                task, item = await _make_video_task(db, actor.id)
                job = await _make_job(
                    db, task, item, actor.id, status="partially_reviewed"
                )
                job.job_kind = "correction"
                job.correction_frame = 1
                job.track_id_snapshot = suffix
                job.completed_at = now - timedelta(hours=1)
                key = f"raster-masks/sha256/aa/bb/{suffix}.json"
                job.staged_result = {
                    "mask": {"object_key": key},
                    "duplicate": {"object_key": key},
                }
                db.add(
                    AiMaskAcceptDecision(
                        task_id=task.id,
                        idempotency_key=suffix,
                        request_digest="a" * 64,
                        candidate_id="metrics-scrape",
                        content_digest="b" * 64,
                        prediction_id=uuid.uuid4(),
                        prediction_created_at=now,
                        annotation_id=job.annotation_id,
                        result_version=1,
                        actor_id=actor.id,
                        response_json={},
                        expires_at=now - timedelta(hours=1),
                    )
                )
                context = {
                    "task": task.id,
                    "item": item.id,
                    "dataset": item.dataset_id,
                    "project": task.project_id,
                    "actor": actor.id,
                }
            after = await scrape(client)
            for name, label in (
                ("mask_ai_correction_jobs", ("status", "partially_reviewed")),
                ("mask_ai_staged_mask_references", ("job_kind", "correction")),
                ("mask_ai_accept_decisions", ("state", "expired")),
            ):
                metric = (name, (label,))
                assert after[metric] == before[metric] + 1
            assert after["mask_ai_oldest_expired_decision_age_seconds", ()] >= 3600
            assert (
                after[
                    "mask_ai_correction_oldest_age_seconds",
                    (("status", "partially_reviewed"),),
                ]
                >= 3600
            )
    finally:
        await api_engine.dispose()
        if context is not None:
            async with owner.begin() as db:
                for cleanup in (
                    "DELETE FROM public.ai_mask_accept_decisions WHERE task_id = :task",
                    "DELETE FROM public.video_tracker_jobs WHERE task_id = :task",
                    "DELETE FROM public.annotations WHERE task_id = :task",
                    "DELETE FROM public.tasks WHERE id = :task",
                    "DELETE FROM public.dataset_items WHERE id = :item",
                    "DELETE FROM public.datasets WHERE id = :dataset",
                    "DELETE FROM public.project_members WHERE project_id = :project",
                    "DELETE FROM public.projects WHERE id = :project",
                    "DELETE FROM public.users WHERE id = :actor",
                ):
                    await db.execute(text(cleanup), context)
