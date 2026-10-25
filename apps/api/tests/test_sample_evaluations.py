"""Project authority, frozen inputs, qualified reports and retained references."""

import copy
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.db.models.task import Task
from app.db.models.annotation import Annotation
from app.services import (
    sample_evaluations as service,
    sample_evaluation_sources as sources,
)
from app.services.training_dataset_bridge import content_digest
from tests.factory import create_membership
from tests.sample_evaluation_fixtures import Objects, complete_rows, evaluation, seed
from tests.sample_evaluation_original_fixtures import (
    Objects as OriginalObjects,
    registered,
)


async def test_shared_qualification_rejects_libre_before_any_profile_request(
    db_session, super_admin, objects, monkeypatch
):
    from app.services import managed_inference, trained_models
    from app.services.ml_client import MLBackendClient

    owner, _token = super_admin
    world = await seed(db_session, owner, objects)
    ref = {**world.ref, "framework": "libreyolo"}
    monkeypatch.setattr(
        trained_models, "resolve_reference", AsyncMock(return_value=ref)
    )
    profile = AsyncMock()
    monkeypatch.setattr(MLBackendClient, "managed_evaluation_request", profile)
    with pytest.raises(HTTPException) as error:
        await managed_inference.qualification(
            db_session, world.project, world.model.id, world.deployment.id
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "framework_unqualified"
    profile.assert_not_awaited()


@pytest.fixture
def objects(monkeypatch):
    from tests.training_consumer_fixtures import mock_legacy_transport

    mock_legacy_transport(monkeypatch)
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
    second = await httpx_client.post(endpoint, headers=headers, json=body)
    assert second.status_code == 202 and second.json()["id"] == data["id"]
    assert qualified.await_count == 1
    conflict = await httpx_client.post(
        endpoint, headers=headers, json={**body, "confidence": 0.3}
    )
    assert conflict.status_code == 409
    assert content_digest(data["spec"]) == data["spec_digest"]


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
    assert (
        frozen.status_code == 200
        and frozen.headers["cache-control"] == "private, no-store"
    )
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
    from app.services.project_delete import delete_project_records
    from app.services.ml_backend import MLBackendService

    with pytest.raises(HTTPException) as blocked:
        await delete_project_records(db_session, world.project)
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
    db_session, super_admin, httpx_client, objects, monkeypatch
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
    current = OriginalObjects().install(monkeypatch)
    _item, _asset, replica = await registered(
        db_session, task, owner, world.raw_images[0]
    )
    current.put(replica.bucket, replica.object_key, world.raw_images[0])
    current.put(
        service.storage.datasets_bucket, replica.object_key, world.raw_images[1]
    )
    endpoint = f"/api/v1/projects/{run.project_id}/sample-evaluations/{run.id}/attempts/{attempt.id}/samples/0/source"
    headers = {"Authorization": "Bearer " + token}
    same = await httpx_client.get(endpoint, headers=headers)
    assert same.status_code == 200, same.text
    assert same.json()["media"] == same.json()["labels"] == "same"
    assert all(
        bucket == replica.bucket for op, bucket, _ in current.calls if op != "close"
    )
    annotation.geometry = {**annotation.geometry, "w": 0.2}
    await db_session.flush()
    changed = await httpx_client.get(endpoint, headers=headers)
    assert changed.json()["media"] == "same" and changed.json()["labels"] == "changed"
    assert run.spec["samples"][0] == frozen
    current.put(replica.bucket, replica.object_key, world.raw_images[1])
    tampered = await httpx_client.get(endpoint, headers=headers)
    assert tampered.status_code == 200, tampered.text
    assert tampered.json()["media"] == "unavailable"
    assert tampered.json()["current_sha256"] is None
    _item, _asset, replica = await registered(
        db_session, task, owner, world.raw_images[1], key="new-registered-source.png"
    )
    current.put(replica.bucket, replica.object_key, world.raw_images[1])
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
    snapshot = await sources.current_snapshot(db_session, world.project.id, task.id)
    assert len(snapshot["annotations"]) == service.engine.MAX_TRUTH_PER_IMAGE + 1
    assert set(snapshot["annotations"][0]["geometry"]) == {"type", "x", "y", "w", "h"}
    assert sources.classify_labels(snapshot, ["car", "person"]) is None


async def test_source_rechecks_the_current_request_key_after_object_io(
    db_session, super_admin, httpx_client, objects, monkeypatch
):
    owner, _token = super_admin
    world = await seed(db_session, owner, objects)
    run, attempt = await evaluation(db_session, owner, world)
    from app.services.api_key_service import create_key

    key, token = await create_key(db_session, owner, "source-resolution", ["*"])
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
    current = OriginalObjects().install(monkeypatch)
    _item, _asset, replica = await registered(
        db_session, task, owner, world.raw_images[0]
    )
    current.put(replica.bucket, replica.object_key, world.raw_images[0])
    from app.services import sample_evaluation_originals as originals

    original = originals.bounded_thread

    async def revoke_after_read(function, *args, **kwargs):
        result = await original(function, *args, **kwargs)
        if function is originals.inspect_image:
            key.revoked_at = service.now()
            await db_session.flush()
        return result

    monkeypatch.setattr(originals, "bounded_thread", revoke_after_read)
    response = await httpx_client.get(
        f"/api/v1/projects/{run.project_id}/sample-evaluations/{run.id}/attempts/{attempt.id}/samples/0/source",
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "api_key_authority_revoked"


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


@pytest.mark.parametrize(
    "revision,version,status",
    [
        (2, "8.4.183", None),
        (99, "8.4.183", 502),
        (1, "8.4.183", 409),
        (2, "8.4.184", 409),
    ],
)
async def test_qualification_separates_training_and_inference_and_bounds_protocol(
    db_session, super_admin, objects, monkeypatch, revision, version, status
):
    from app.services import managed_inference, trained_models
    from app.services.ml_client import MLBackendClient
    from training_contract.inference_runtime import inference_runtime

    world = await seed(db_session, super_admin[0], objects)
    ref = {
        **world.ref,
        "provenance": {**world.ref["provenance"], "base_model": {"id": "yolo11n"}},
        "inference_runtime": inference_runtime(world.ref, "8.4.183"),
    }
    monkeypatch.setattr(
        trained_models, "resolve_reference", AsyncMock(return_value=ref)
    )
    profile = {**world.profile, "revision": revision, "ultralytics_version": version}
    profile["profile_digest"] = content_digest(
        {k: v for k, v in profile.items() if k != "profile_digest"}
    )
    monkeypatch.setattr(
        MLBackendClient, "managed_evaluation_request", AsyncMock(return_value=profile)
    )
    if status:
        with pytest.raises(HTTPException) as error:
            await managed_inference.qualification(
                db_session, world.project, world.model.id, world.deployment.id
            )
        assert error.value.status_code == status
    else:
        frozen, checked = await managed_inference.qualification(
            db_session, world.project, world.model.id, world.deployment.id
        )
        assert frozen["framework_version"] == "8.4.177"
        assert checked["ultralytics_version"] == "8.4.183"
        assert (
            "inference_runtime"
            in trained_models.wire_context(frozen)["frozen_model_ref"]
        )


def test_runtime_metadata_is_pinned_and_legacy_wire_shape_is_preserved():
    from app.services import trained_models
    from training_contract.inference_runtime import inference_runtime

    ref = {
        "model_version_id": str(uuid.uuid4()),
        "sha256": "a" * 64,
        "classes_digest": "b" * 64,
        "framework": "ultralytics",
        "framework_version": "8.4.177",
    }
    assert set(trained_models.wire_reference(ref)) == {
        "model_version_id",
        "sha256",
        "classes_digest",
    }
    upgraded_runtime = inference_runtime(ref, "8.4.183")
    with pytest.raises(ValueError):
        trained_models.validate_result_runtime(
            {"inference_runtime": upgraded_runtime}, ref
        )
    ref["inference_runtime"] = upgraded_runtime
    trained_models.validate_result_runtime(
        {"inference_runtime": ref["inference_runtime"]}, ref
    )
    with pytest.raises(ValueError):
        trained_models.validate_result_runtime({}, ref)
    with pytest.raises(ValueError):
        trained_models.validate_result_runtime(
            {"inference_runtime": {**ref["inference_runtime"], "version": "8.4.184"}},
            ref,
        )
