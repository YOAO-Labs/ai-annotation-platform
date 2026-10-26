"""Project authority, frozen inputs, qualified reports and retained references."""

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.db.models.task import Task
from app.db.models.annotation import Annotation
from app.services import (
    sample_evaluations as service,
    sample_evaluation_sources as sources,
)
from app.services.labeling_evaluation_source import (
    SqlAlchemyLabelingEvaluationSourceReader,
)
from app.services.labeling_evaluation_source_contract import CurrentTaskSourceRead
from app.services.training_dataset_bridge import content_digest
from tests.factory import create_membership
from tests.sample_evaluation_fixtures import Objects, complete_rows, evaluation, seed


async def test_shared_qualification_rejects_libre_before_any_profile_request(
    db_session, super_admin, objects, monkeypatch
):
    from app.services import (
        inference_backend_control,
        managed_inference,
        trained_models,
    )

    owner, _token = super_admin
    world = await seed(db_session, owner, objects)
    ref = {**world.ref, "framework": "libreyolo"}
    reference_port = SimpleNamespace(freeze=AsyncMock(return_value=ref))
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
    with pytest.raises(HTTPException) as error:
        await managed_inference.qualification(
            db_session, world.project, world.model.id, world.deployment.id
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "framework_unqualified"
    profile.assert_not_awaited()


@pytest.fixture
def objects(monkeypatch):
    store = Objects()
    monkeypatch.setattr(service.storage, "client", store)
    monkeypatch.setattr(service.storage, "delete_prefix", store.delete_prefix)
    return store


async def test_segmentation_dataset_is_rejected_even_when_selected_label_is_empty(
    db_session, super_admin, objects
):
    from training_contract.dataset import SEGMENTATION_CONTRACT

    world = await seed(db_session, super_admin[0], objects)
    version = world.dataset
    version.snapshot = {**version.snapshot, **SEGMENTATION_CONTRACT}
    version.snapshot_digest = content_digest(version.snapshot)
    manifest = {
        key: value for key, value in version.manifest.items() if key != "content_digest"
    }
    manifest.update(SEGMENTATION_CONTRACT)
    manifest["content_digest"] = content_digest(manifest)
    version.manifest = manifest
    with pytest.raises(HTTPException) as error:
        service.checked_dataset(version)
    assert (
        error.value.status_code == 422
        and error.value.detail["code"] == "unsupported_task"
    )


async def test_create_is_idempotent_and_freezes_order_mapping_and_request_binding(
    db_session, super_admin, httpx_client, objects, monkeypatch
):
    owner, token = super_admin
    world = await seed(db_session, owner, objects)
    qualified = AsyncMock(return_value=(world.ref, world.profile))
    monkeypatch.setattr(service, "qualification", qualified)
    from app.workers import sample_evaluations as worker

    monkeypatch.setattr(worker, "dispatch", AsyncMock())
    endpoint = f"/api/v1/projects/{world.project.id}/sample-evaluations"
    headers = {"Authorization": "Bearer " + token}
    body = world.body.model_dump(mode="json")
    first = await httpx_client.post(endpoint, headers=headers, json=body)
    assert first.status_code == 202, first.text
    data = first.json()
    assert data["spec"]["native_to_benchmark"] == [1, 0]
    assert [s["task_id"] for s in data["spec"]["samples"]] == body["sample_task_ids"]
    assert data["progress"] == {
        "pending": 2,
        "inflight": 0,
        "succeeded": 0,
        "failed": 0,
    }
    from app.db.models.sample_evaluation import SampleEvaluationRun

    run = await db_session.get(SampleEvaluationRun, uuid.UUID(data["id"]))
    rows = await service.sample_rows(db_session, run)
    assert [row.request_id for row in rows] == [
        uuid.uuid5(run.id, f"sample:{ordinal}") for ordinal in range(2)
    ]
    second = await httpx_client.post(endpoint, headers=headers, json=body)
    assert second.status_code == 202 and second.json()["id"] == data["id"]
    conflict = await httpx_client.post(
        endpoint, headers=headers, json={**body, "confidence": 0.3}
    )
    assert conflict.status_code == 409
    assert content_digest(data["spec"]) == data["spec_digest"]


async def test_invalid_create_does_not_write(
    db_session, super_admin, httpx_client, objects, monkeypatch
):
    from sqlalchemy import select

    from app.db.models.sample_evaluation import SampleEvaluationRun
    from app.workers import sample_evaluations as worker

    owner, token = super_admin
    world = await seed(db_session, owner, objects)
    monkeypatch.setattr(
        service, "qualification", AsyncMock(return_value=(world.ref, world.profile))
    )
    monkeypatch.setattr(worker, "dispatch", AsyncMock())
    body = world.body.model_dump(mode="json")
    body["sample_task_ids"] = [str(uuid.uuid4())]

    response = await httpx_client.post(
        f"/api/v1/projects/{world.project.id}/sample-evaluations",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "sample_unavailable"
    assert (
        await db_session.scalar(
            select(SampleEvaluationRun).where(
                SampleEvaluationRun.project_id == world.project.id,
                SampleEvaluationRun.request_id == uuid.UUID(body["request_id"]),
            )
        )
        is None
    )


async def test_retry_finalizes_lost_attempt_and_resets_only_failed_samples(
    db_session, super_admin, httpx_client, objects, monkeypatch
):
    from app.db.models.async_job import AsyncJob

    owner, token = super_admin
    world = await seed(db_session, owner, objects)
    run, attempt = await evaluation(db_session, owner, world)
    rows = await service.sample_rows(db_session, run)
    rows[0].status = "failed"
    rows[0].error_code, rows[0].error_message = "transient", "retry this sample"
    rows[0].request = {"sample": str(rows[0].request_id)}
    rows[0].evidence = None
    rows[0].produced_attempt_id = attempt.id
    rows[1].status = "inflight"
    rows[1].request = {"sample": str(rows[1].request_id)}
    rows[1].evidence = None
    rows[1].produced_attempt_id = attempt.id
    retained = [
        {
            "request": copy.deepcopy(row.request),
            "evidence": copy.deepcopy(row.evidence),
            "produced_attempt_id": row.produced_attempt_id,
        }
        for row in rows
    ]
    request_ids = [row.request_id for row in rows]
    frozen_spec, frozen_digest = copy.deepcopy(run.spec), run.spec_digest
    run.status = attempt.status = "lost"
    attempt.claim_id = uuid.uuid4()
    old_job = await db_session.get(AsyncJob, attempt.async_job_id)
    from app.workers import sample_evaluations as worker

    monkeypatch.setattr(worker, "dispatch", AsyncMock())

    response = await httpx_client.post(
        f"/api/v1/projects/{run.project_id}/sample-evaluations/{run.id}/retry",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 202, response.text
    await db_session.refresh(run)
    await db_session.refresh(attempt)
    await db_session.refresh(old_job)
    assert run.generation == 2 and run.spec == frozen_spec
    assert run.spec_digest == frozen_digest
    assert run.current_attempt_id != attempt.id
    assert attempt.claim_id is None
    assert old_job.status == "failed"
    assert [row.request_id for row in rows] == request_ids
    assert rows[0].status == "pending"
    assert rows[0].error_code is None and rows[0].error_message is None
    assert rows[1].status == "inflight"
    assert [
        {
            "request": row.request,
            "evidence": row.evidence,
            "produced_attempt_id": row.produced_attempt_id,
        }
        for row in rows
    ] == retained
    assert response.json()["current_attempt_id"] == str(run.current_attempt_id)


@pytest.mark.parametrize("unavailable", ["deletion", "expiry", "active"])
async def test_unavailable_retry_keeps_evaluation_rows_unchanged(
    db_session, super_admin, httpx_client, objects, monkeypatch, unavailable
):
    owner, token = super_admin
    world = await seed(db_session, owner, objects)
    run, attempt = await evaluation(db_session, owner, world)
    rows = await service.sample_rows(db_session, run)
    rows[0].status = "failed"
    rows[0].error_code, rows[0].error_message = "keep", "unchanged"
    run.status = attempt.status = "lost"
    attempt.claim_id = uuid.uuid4()
    if unavailable == "deletion":
        service.request_deletion(run)
    elif unavailable == "expiry":
        run.spec = {**run.spec, "expires_at": 0}
    else:
        run.status = attempt.status = "running"
    before = (
        run.generation,
        run.current_attempt_id,
        attempt.claim_id,
        attempt.completed_at,
        rows[0].status,
        rows[0].error_code,
        rows[0].error_message,
    )
    from app.workers import sample_evaluations as worker

    monkeypatch.setattr(worker, "dispatch", AsyncMock())

    response = await httpx_client.post(
        f"/api/v1/projects/{run.project_id}/sample-evaluations/{run.id}/retry",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "retry_unavailable"
    assert (
        run.generation,
        run.current_attempt_id,
        attempt.claim_id,
        attempt.completed_at,
        rows[0].status,
        rows[0].error_code,
        rows[0].error_message,
    ) == before


@pytest.mark.parametrize(
    "mutation",
    [
        "test_contamination",
        "unknown_member",
        "duplicate_binding",
        "unknown_labels",
        "media_budget",
    ],
)
async def test_creation_rejects_unqualified_inputs(
    db_session, super_admin, objects, mutation
):
    owner, _token = super_admin
    world = await seed(db_session, owner, objects)
    body = world.body
    ref = copy.deepcopy(world.ref)
    if mutation == "test_contamination":
        body.role, body.held_out_confirmed = "held_out_test", True
    elif mutation == "unknown_member":
        body.sample_task_ids = [uuid.uuid4()]
    elif mutation == "duplicate_binding":
        ref["bindings"] = ["car", "car"]
    elif mutation == "unknown_labels":
        snapshot = copy.deepcopy(world.dataset.snapshot)
        snapshot["samples"][0]["annotations"][0]["class_name"] = "unknown"
        world.dataset.snapshot = snapshot
        world.dataset.snapshot_digest = content_digest(snapshot)
    else:
        manifest = copy.deepcopy(world.dataset.manifest)
        manifest["samples"][0]["size_bytes"] = service.engine.MAX_RAW_BYTES + 1
        manifest["content_digest"] = content_digest(
            {k: v for k, v in manifest.items() if k != "content_digest"}
        )
        world.dataset.manifest = manifest
    await db_session.flush()
    with pytest.raises((HTTPException, ValueError)):
        await service.frozen_spec(
            db_session, world.project, body, uuid.uuid4(), ref, world.profile
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "truth",
        "member",
        "matching",
        "aggregate",
        "request",
        "inference",
        "producer",
        "prediction_class",
    ],
)
async def test_report_verification_recomputes_identity_matching_and_metrics(
    db_session, super_admin, objects, mutation
):
    owner, _token = super_admin
    world = await seed(db_session, owner, objects)
    run, attempt = await evaluation(db_session, owner, world)
    await complete_rows(db_session, run, attempt, world)
    rows = await service.sample_rows(db_session, run)
    report = service.build_report(run, attempt, rows, "succeeded")
    assert report["aggregate"]["fn"] == 1 and report["aggregate"]["precision"] is None
    altered = copy.deepcopy(report)
    if mutation == "aggregate":
        altered["aggregate"]["fn"] = 0
    elif mutation == "truth":
        altered["samples"][0]["truth"][0]["xyxy"][0] = 0.01
    elif mutation == "member":
        altered["samples"][0]["task_id"] = str(uuid.uuid4())
    else:
        sample = altered["samples"][0]
        if mutation == "matching":
            sample["matching"]["false_negative_indices"] = [999]
        elif mutation == "request":
            sample["binding_digest"] = "e" * 64
        elif mutation == "inference":
            sample["inference"]["options"]["imgsz"] = 320
        elif mutation == "producer":
            sample["produced_attempt_id"] = str(uuid.uuid4())
        else:
            sample["predictions"] = [
                {
                    "index": 0,
                    "class_index": 0,
                    "native_class_index": 0,
                    "score": 0.7,
                    "xyxy": [0.1, 0.1, 0.2, 0.2],
                }
            ]
        sample["evidence_digest"] = content_digest(
            {k: v for k, v in sample.items() if k != "evidence_digest"}
        )
        # Even matching storage checksums cannot replace semantic verification.
        rows[0].evidence = copy.deepcopy(sample)
    with pytest.raises((ValueError, IndexError)):
        service.verify_report(altered, run, attempt, rows)


async def test_verified_reads_filter_before_pagination_and_preserve_failed_partial_report(
    db_session, super_admin, httpx_client, objects
):
    owner, token = super_admin
    world = await seed(db_session, owner, objects, count=4)
    run, attempt = await evaluation(db_session, owner, world)
    await complete_rows(db_session, run, attempt, world)
    report = service.build_report(
        run, attempt, await service.sample_rows(db_session, run), "succeeded"
    )
    artifact = service.upload_report(run, attempt, report)
    await service.mark_terminal(db_session, run, attempt, "succeeded", report=artifact)
    await db_session.flush()
    base = f"/api/v1/projects/{run.project_id}/sample-evaluations/{run.id}/attempts/{attempt.id}"
    headers = {"Authorization": "Bearer " + token}
    page = await httpx_client.get(
        base + "/samples?error=false_negative&confidence_min=0.99&offset=1&limit=1",
        headers=headers,
    )
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 2 and [
        s["ordinal"] for s in page.json()["items"]
    ] == [2]
    frozen = await httpx_client.get(base + "/samples/0/image", headers=headers)
    assert frozen.status_code == 200, frozen.text
    assert frozen.headers["cache-control"] == "private, no-store"
    objects.objects[artifact["key"]]["raw"] += b"!"
    corrupt = await httpx_client.get(base + "/report", headers=headers)
    assert (
        corrupt.status_code == 409
        and corrupt.json()["detail"]["code"] == "report_unavailable"
    )
    rows = await service.sample_rows(db_session, run)
    rows[0].evidence = None
    rows[0].status, rows[0].error_code = "failed", "input_unavailable"
    partial = service.build_report(run, attempt, rows, "failed")
    assert (
        partial["aggregate"] is None
        and not partial["complete"]
        and partial["failed_samples"] == 1
    )


async def test_retained_references_block_storage_deletion_and_project_isolation(
    db_session, super_admin, project_admin, httpx_client, objects, monkeypatch
):
    owner, token = super_admin
    world = await seed(db_session, owner, objects)
    run, _attempt = await evaluation(db_session, owner, world)
    from app.application.project_deletion import (
        delete_project as delete_project_transaction,
    )
    from app.services.ml_backend import MLBackendService

    with pytest.raises(HTTPException) as blocked:
        await delete_project_transaction(
            db_session, world.project.id, actor_id=owner.id
        )
    assert blocked.value.status_code == 409
    with pytest.raises(HTTPException) as blocked:
        await MLBackendService(db_session).validate_delete(world.backend.id)
    assert blocked.value.status_code == 409
    deletion = Mock()
    monkeypatch.setattr(service.storage, "delete_prefix", deletion)
    response = await httpx_client.delete(
        f"/api/v1/projects/{world.project.id}/training-datasets/{world.dataset.id}",
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 409 and not deletion.called
    outsider, outsider_token = project_admin
    await create_membership(
        db_session, project_id=world.project.id, user_id=outsider.id, role="annotator"
    )
    response = await httpx_client.get(
        f"/api/v1/projects/{world.project.id}/sample-evaluations/{run.id}",
        headers={"Authorization": "Bearer " + outsider_token},
    )
    assert response.status_code == 403


async def test_source_uses_current_bytes_and_labels_and_never_falls_back_after_deletion(
    db_session, super_admin, httpx_client, objects
):
    owner, token = super_admin
    world = await seed(db_session, owner, objects)
    run, attempt = await evaluation(db_session, owner, world)
    frozen = run.spec["samples"][0]
    task = Task(
        id=uuid.UUID(frozen["task_id"]),
        project_id=run.project_id,
        display_id="T-SOURCE-" + uuid.uuid4().hex[:6],
        file_path="current-source.png",
        file_name="source.png",
        file_type="image",
        status="completed",
        reviewed_at=service.now(),
    )
    db_session.add(task)
    await db_session.flush()
    annotation = Annotation(
        task_id=task.id,
        project_id=run.project_id,
        user_id=owner.id,
        class_name="car",
        tool_unit_id="bbox",
        geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        source="manual",
    )
    db_session.add(annotation)
    await db_session.flush()
    objects.put_object(
        Bucket=service.storage.bucket,
        Key=task.file_path,
        Body=world.raw_images[0],
        ContentType="image/png",
    )
    endpoint = f"/api/v1/projects/{run.project_id}/sample-evaluations/{run.id}/attempts/{attempt.id}/samples/0/source"
    headers = {"Authorization": "Bearer " + token}
    same = await httpx_client.get(endpoint, headers=headers)
    assert same.status_code == 200, same.text
    assert same.json()["media"] == same.json()["labels"] == "same"
    annotation.geometry = {**annotation.geometry, "w": 0.2}
    await db_session.flush()
    changed = await httpx_client.get(endpoint, headers=headers)
    assert changed.json()["media"] == "same" and changed.json()["labels"] == "changed"
    assert run.spec["samples"][0] == frozen
    objects.put_object(
        Bucket=service.storage.bucket,
        Key=task.file_path,
        Body=world.raw_images[1],
        ContentType="image/png",
    )
    changed = await httpx_client.get(endpoint, headers=headers)
    assert changed.json()["media"] == changed.json()["labels"] == "changed"
    await db_session.delete(annotation)
    await db_session.delete(task)
    await db_session.flush()
    deleted = await httpx_client.get(endpoint, headers=headers)
    assert deleted.json()["source"] == "deleted" and deleted.json()["task_id"] is None


async def test_source_limits_annotation_projection_before_aggregation(
    db_session, super_admin, objects
):
    owner, _token = super_admin
    world = await seed(db_session, owner, objects)
    from tests.factory import create_task

    task = await create_task(db_session, project_id=world.project.id)
    for _ in range(service.engine.MAX_TRUTH_PER_IMAGE + 8):
        db_session.add(
            Annotation(
                task_id=task.id,
                project_id=world.project.id,
                user_id=owner.id,
                class_name="car",
                tool_unit_id="region",
                geometry={"type": "polygon", "points": [[0.1, 0.1]] * 1000},
                source="manual",
            )
        )
    await db_session.flush()
    snapshot = await SqlAlchemyLabelingEvaluationSourceReader(
        db_session
    ).read_current_task_source(
        CurrentTaskSourceRead(
            project_id=world.project.id,
            task_id=task.id,
            annotation_limit=service.engine.MAX_TRUTH_PER_IMAGE + 1,
        )
    )
    assert snapshot is not None
    assert len(snapshot["annotations"]) == service.engine.MAX_TRUTH_PER_IMAGE + 1
    assert set(snapshot["annotations"][0]["geometry"]) == {"type", "x", "y", "w", "h"}
    assert sources.classify_labels(snapshot, ["car", "person"]) is None


async def test_source_rechecks_the_current_request_key_after_object_io(
    db_session, super_admin, httpx_client, objects, monkeypatch
):
    owner, _token = super_admin
    world = await seed(db_session, owner, objects)
    run, attempt = await evaluation(db_session, owner, world)
    from tests.factory import create_api_key

    key, token = await create_api_key(db_session, owner.id, "source-resolution", ["*"])
    frozen = run.spec["samples"][0]
    task = Task(
        id=uuid.UUID(frozen["task_id"]),
        project_id=run.project_id,
        display_id="T-KEY-" + uuid.uuid4().hex[:6],
        file_path="key-source.png",
        file_name="source.png",
        file_type="image",
    )
    db_session.add(task)
    await db_session.flush()
    objects.put_object(
        Bucket=service.storage.bucket,
        Key=task.file_path,
        Body=world.raw_images[0],
        ContentType="image/png",
    )
    original = sources.asyncio.to_thread

    async def revoke_after_read(function, *args, **kwargs):
        result = await original(function, *args, **kwargs)
        if function is sources.read_current_media:
            key.revoked_at = service.now()
            await db_session.flush()
        return result

    monkeypatch.setattr(sources.asyncio, "to_thread", revoke_after_read)
    response = await httpx_client.get(
        f"/api/v1/projects/{run.project_id}/sample-evaluations/{run.id}/attempts/{attempt.id}/samples/0/source",
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "api_key_revoked"


async def test_comparison_requires_two_explicit_compatible_successful_reports(
    db_session, super_admin, httpx_client, objects
):
    owner, token = super_admin
    world = await seed(db_session, owner, objects)
    first, first_attempt = await evaluation(db_session, owner, world)
    second, second_attempt = await evaluation(db_session, owner, world)
    for run, attempt in ((first, first_attempt), (second, second_attempt)):
        await complete_rows(db_session, run, attempt, world)
        report = service.build_report(
            run, attempt, await service.sample_rows(db_session, run), "succeeded"
        )
        await service.mark_terminal(
            db_session,
            run,
            attempt,
            "succeeded",
            report=service.upload_report(run, attempt, report),
        )
    await db_session.flush()
    endpoint = f"/api/v1/projects/{first.project_id}/sample-evaluations/compare?left_id={first.id}&right_id={second.id}"
    headers = {"Authorization": "Bearer " + token}
    compared = await httpx_client.get(endpoint, headers=headers)
    assert compared.status_code == 200, compared.text
    assert (
        compared.json()["compatible"]
        and compared.json()["delta_right_minus_left"]["fn"] == 0
    )
    assert compared.json()["sample_pairs"] == [
        {"left_ordinal": 0, "right_ordinal": 0},
        {"left_ordinal": 1, "right_ordinal": 1},
    ]
    await service.mark_terminal(
        db_session, second, second_attempt, "failed", code="sample_failed"
    )
    await db_session.flush()
    incompatible = await httpx_client.get(endpoint, headers=headers)
    assert incompatible.status_code == 200 and not incompatible.json()["compatible"]
    assert (
        incompatible.json()["delta_right_minus_left"] is None
        and not incompatible.json()["sample_pairs"]
    )
    assert (
        incompatible.json()["candidates"][0]["aggregate"]
        and incompatible.json()["candidates"][1]["aggregate"] is None
    )


@pytest.mark.parametrize("replaced", [False, True])
async def test_async_job_cancel_preserves_evaluation_intent_and_current_attempt(
    db_session, super_admin, httpx_client, objects, replaced
):
    from app.db.models.async_job import AsyncJob
    from app.db.models.sample_evaluation import SampleEvaluationAttempt

    owner, token = super_admin
    world = await seed(db_session, owner, objects)
    run, attempt = await evaluation(db_session, owner, world)
    job = await db_session.get(AsyncJob, attempt.async_job_id)
    if replaced:
        run.status = attempt.status = "lost"
        current = await service.new_attempt(db_session, run)
    else:
        current = attempt
    await db_session.flush()

    response = await httpx_client.post(
        f"/api/v1/async-jobs/{job.id}/cancel",
        headers={"Authorization": "Bearer " + token},
    )
    await db_session.refresh(run)
    await db_session.refresh(job)
    current = await db_session.get(SampleEvaluationAttempt, current.id)
    await db_session.refresh(current)
    if replaced:
        assert response.status_code == 409
        assert run.status == current.status == "queued"
        assert run.current_attempt_id != attempt.id
    else:
        assert response.status_code == 200
        assert response.json()["status"] == "cancel_requested"
        assert run.status == current.status == "cancelling"
        assert current.error_code == "cancel_requested"
    assert job.status == "pending"


async def test_delete_records_intent_before_storage_cleanup_failure(
    db_session, super_admin, httpx_client, objects, monkeypatch
):
    owner, token = super_admin
    world = await seed(db_session, owner, objects)
    run, attempt = await evaluation(db_session, owner, world)
    await service.mark_terminal(
        db_session, run, attempt, "failed", code="sample_failed"
    )
    await db_session.flush()
    monkeypatch.setattr(
        service.storage,
        "delete_prefix",
        Mock(side_effect=RuntimeError("controlled storage failure")),
    )

    response = await httpx_client.delete(
        f"/api/v1/projects/{run.project_id}/sample-evaluations/{run.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "cleanup_pending"
    await db_session.refresh(run)
    assert run.deletion_requested_at is not None
    requested_at = run.deletion_requested_at
    repeated = await httpx_client.delete(
        f"/api/v1/projects/{run.project_id}/sample-evaluations/{run.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert repeated.status_code == 503
    assert repeated.json()["detail"]["code"] == "cleanup_pending"
    await db_session.refresh(run)
    assert run.deletion_requested_at == requested_at
