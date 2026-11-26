"""Frozen reference preannotation scopes, same-byte delivery and write receipts."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import uuid
from unittest.mock import Mock

import httpx
from PIL import Image
import pytest
from sqlalchemy import select
from starlette.requests import Request

from yoao_assets_input_manifests.contract import ManifestPageRequest

from app.application.preannotation_contract import BatchPredictionRequest
from app.application.preannotation_execution import run_preannotation
from app.core.security import create_access_token, hash_password
from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.prediction import Prediction, PredictionMeta, FailedPrediction
from app.db.models.prediction_input import PredictionInputLease
from app.services import prediction_references as refs, async_job as jobs
from app.deps import bearer_scheme, get_current_principal, get_db
from app.services.accepted_input_manifests import SqlAlchemyManifestDatabase
from app.services.content_snapshot import ContentError
from app.services.identity_gateway import IdentityGateway
from app.services.labeling_prediction_contract import ApplyPredictionResultRequest
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.jobs_lifecycle_data import job_lock_statement
from yoao_inference.backend_transport import PredictionResult
from app.services.prediction import PredictionService
from app.services.prediction_execution import create_snapshot, validate_task_scope
from app.workers import tasks as worker
from tests.inference_execution_test_ports import (
    PredictionClientExecutionPort,
    patch_batch_execution,
    run_client_task_pipeline,
)
from tests.conftest import create_registry_with_pool
from tests.factory import create_api_key, create_task, create_user
from tests.managed_prediction_worker_fixtures import (
    managed_worker_world as managed_worker_world,
    worker_engine,
)
from tests.test_dataset_content import (
    content_world as content_world,
    create_content_world,
    headers,
)
from tests.test_dataset_references import (
    create_reference_world,
    reference_world as reference_world,
)
from tests.test_prediction_commit_boundary import (
    frame_publication_login as frame_publication_login,
    image_executor_logins as image_executor_logins,
    retained_image_storage as retained_image_storage,
)
from tests.test_prediction_inputs import input_world as input_world


async def backend_for(db, project):
    backend, pool = await create_registry_with_pool(
        db,
        name="reference-predict",
        url="http://owned.invalid",
        state="connected",
        enabled_pool=True,
        is_interactive=False,
    )
    db.add(ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True))
    project.ml_backend_pool_id = pool.id
    await db.flush()
    return backend


def pipeline(backend):
    return [
        {
            "stage": 7,
            "parent_stage": None,
            "ml_backend_id": str(backend.id),
            "model_id": "detect",
        },
        {
            "stage": 9,
            "parent_stage": 7,
            "ml_backend_id": str(backend.id),
            "model_id": "segment",
            "input": {"mode": "geometry"},
            "write": {"target": "intermediate"},
        },
        {
            "stage": 11,
            "parent_stage": 9,
            "ml_backend_id": str(backend.id),
            "model_id": "classify",
            "task": "classify",
            "input": {"mode": "crop"},
            "write": {"target": "attributes", "keys": ["color"]},
            "roi": {"pad": 0},
        },
    ]


def rectangle():
    return {
        "type": "rectanglelabels",
        "value": {
            "x": 0,
            "y": 0,
            "width": 100,
            "height": 100,
            "rectanglelabels": ["car"],
        },
    }


async def enqueue(
    db,
    client,
    world,
    admin,
    monkeypatch,
    *,
    scope="bulk",
    stages=False,
    extra=None,
    key=None,
    auth=None,
):
    _, project, task, _ = world
    if scope == "bulk":
        task.status = "pending"
        task.assignee_id = None
    backend = await backend_for(db, project)
    dispatch = Mock(side_effect=lambda **kw: SimpleNamespace(id=kw["task_id"]))
    monkeypatch.setattr(worker.batch_predict, "apply_async", dispatch)
    body = {
        "ml_backend_id": str(backend.id),
        "model_id": "detect",
        "task_ids": [str(task.id)],
        "execution_scope": scope,
        "predict_mode": "overwrite",
    }
    if stages:
        body["pipeline_stages"] = pipeline(backend)
    body.update(extra or {})
    request_headers = dict(auth) if auth is not None else headers(admin)
    if key:
        request_headers["Idempotency-Key"] = key
    response = await client.post(
        f"/api/v1/projects/{project.id}/preannotate", json=body, headers=request_headers
    )
    assert response.status_code == 200, response.text
    job = await db.get(AsyncJob, uuid.UUID(response.json()["job_id"]))
    return job, backend, body, dispatch


@pytest.fixture
async def committed_reference_delivery(
    app_module,
    tmp_path,
    monkeypatch,
    managed_worker_world,
    image_executor_logins,
    frame_publication_login,
):
    async with managed_worker_world.factory() as db:
        actor = await create_user(db, "super_admin", "delivery@test.local", "Delivery")
        assignee = await create_user(db, "employee", "assignee@test.local", "Assignee")
        content = await create_content_world(db, actor, tmp_path, monkeypatch)
        world = await create_reference_world(db, content, assignee)
        await db.commit()
        token = create_access_token(subject=str(actor.id), role=actor.role)

        async def request_db():
            yield db

        monkeypatch.setitem(app_module.dependency_overrides, get_db, request_db)
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module), base_url="http://test"
            ) as client,
            worker_engine(managed_worker_world),
        ):
            yield db, client, world, (actor, token)


async def run_dispatched(dispatch):
    envelope = dispatch.call_args.kwargs
    await run_preannotation(
        BatchPredictionRequest(*envelope["args"], **envelope["kwargs"]),
        execution_id=envelope["task_id"],
        worker_node_id="worker:content@test",
    )


def delivered_image(storage, payload):
    url = urlsplit(payload["file_path"])
    assert url.hostname == "ml-objects.test"
    bucket, key = url.path.lstrip("/").split("/", 1)
    (version,) = parse_qs(url.query)["versionId"]
    raw, _metadata = storage.versions[bucket, key][version]
    return key, raw


@pytest.mark.parametrize(
    ("mutation", "stages", "scope"),
    [
        (mutation, stages, scope)
        for mutation in (None, "version", "source", "cancel", "physical")
        for stages in (False, True)
        for scope in ("bulk", "workbench")
    ]
    + [("revoke_key", False, "workbench"), ("video_result", False, "bulk")],
)
async def test_api_to_worker_reference_snapshot_and_roi_are_one_owned_input(
    committed_reference_delivery,
    embedded_revocations,
    monkeypatch,
    input_world,
    scope,
    stages,
    mutation,
):
    db, client, world, admin = committed_reference_delivery
    registered, project, task, content = world
    task_id, project_id = task.id, project.id
    previous_job, backend, body, dispatch = await enqueue(
        db, client, world, admin, monkeypatch, scope=scope
    )

    def previous_response(_operation, payload):
        _, raw = delivered_image(input_world, payload)
        assert raw == content[1]
        return [{**rectangle(), "score": 0.4}]

    patch_batch_execution(monkeypatch, previous_response)
    await run_dispatched(dispatch)
    await db.refresh(previous_job)
    assert previous_job.status == "completed", (
        await db.scalars(
            select(FailedPrediction.message).where(FailedPrediction.task_id == task_id)
        )
    ).all()
    old_id, old_result = (
        await db.execute(
            select(Prediction.id, Prediction.result).where(
                Prediction.task_id == task_id
            )
        )
    ).one()
    previous_deletions = len(input_world.deletions)
    assert not input_world.objects

    auth = headers(admin)
    if mutation == "revoke_key":
        issued = await client.post(
            "/api/v1/me/api-keys",
            headers=auth,
            json={"name": "Reference delivery", "scopes": ["*"]},
        )
        assert issued.status_code == 201, issued.text
        key_id = issued.json()["id"]
        auth = {"Authorization": "Bearer " + issued.json()["plaintext"]}
    if stages:
        body["pipeline_stages"] = pipeline(backend)
    response = await client.post(
        f"/api/v1/projects/{project_id}/preannotate", json=body, headers=auth
    )
    assert response.status_code == 200, response.text
    job_id = uuid.UUID(response.json()["job_id"])
    assert job_id != previous_job.id
    job = await jobs.get_job(db, job_id)
    proof = job.payload["prediction_authority"]
    assert proof["actor_id"] == str(admin[0].id)
    if mutation == "revoke_key":
        assert proof["api_key_id"] == key_id
    (binding,) = proof["tasks"]
    assert binding["task_id"] == str(task_id)
    assert binding["reference"]["sha256"] == hashlib.sha256(content[1]).hexdigest()
    assert job.execution_snapshot_id is not None
    await db.commit()
    seen = []
    inference_finished = False
    original = content[0] / content[7].relative_path

    class Client:
        async def predict(self, tasks, context=None):
            for payload in tasks:
                object_key, raw = delivered_image(input_world, payload)
                seen.append((object_key, raw))
                if "/crops/" in object_key:
                    with Image.open(io.BytesIO(raw)) as image:
                        assert image.size == (48, 32)
                        red, green, blue = image.getpixel((10, 10))
                        assert red > 240 and max(green, blue) < 10
                else:
                    assert "/original/" in object_key and raw == content[1]
            if "/crops/" in seen[-1][0]:
                await mutate_after_inference()
                return [
                    PredictionResult(
                        task_id=t["id"], result=[{"attributes": {"color": "red"}}]
                    )
                    for t in tasks
                ]
            shape = rectangle()
            if mutation == "video_result":
                shape = {
                    "type": "video_track_bbox",
                    "geometry": {"type": "video_track_bbox", "keyframes": []},
                    "class_name": "car",
                }
            if tasks[0].get("prompts"):
                shape["parent_box_idx"] = 0
            if not stages:
                await mutate_after_inference()
            elif len(seen) == 1 and mutation == "physical":
                original.write_bytes(b"replaced")
            return [
                PredictionResult(
                    task_id=tasks[0]["id"],
                    result=[shape],
                    meta={"total_cost": 0.25, "mask_objects": None},
                )
            ]

    async def mutate_after_inference():
        nonlocal inference_finished
        if mutation == "version":
            await db.refresh(task)
            task.version += 1
            await db.commit()
        elif mutation == "source":
            source_url = f"/api/v1/dataset-sources/{content[5].id}"
            impact = await client.get(source_url + "/lifecycle-impact", headers=auth)
            assert impact.status_code == 200, impact.text
            change = await client.post(
                source_url + "/lifecycle",
                headers=auth,
                json={
                    "action": "pause",
                    "expected_revision": impact.json()["expected_revision"],
                    "impact_proof": impact.json()["proof"],
                },
            )
            assert change.status_code == 200, change.text
            assert change.json()["enabled"] is False
        elif mutation == "cancel":
            cancel = await client.post(
                f"/api/v1/async-jobs/{job_id}/cancel", headers=auth
            )
            assert cancel.status_code == 200, cancel.text
            assert cancel.json()["status"] == "cancel_requested"
        elif mutation == "revoke_key":
            revoked = await client.delete(
                f"/api/v1/me/api-keys/{key_id}", headers=headers(admin)
            )
            assert revoked.status_code == 204, revoked.text
        elif mutation == "physical" and not stages:
            original.write_bytes(b"replaced")
        inference_finished = True

    monkeypatch.setattr(
        "app.application.preannotation_execution._model_execution_port",
        lambda *_args, **_kwargs: PredictionClientExecutionPort(Client()),
    )
    await run_dispatched(dispatch)
    job = await jobs.get_job(db, job_id, populate_existing=True)
    failures = (
        await db.execute(
            select(FailedPrediction.error_type, FailedPrediction.message).where(
                FailedPrediction.project_id == project_id
            )
        )
    ).all()
    assert len(seen) == (3 if stages else 1), failures
    assert inference_finished, failures
    if stages:
        assert seen[0] == seen[1]
        assert "/crops/" in seen[2][0]
    (lease_id,) = {uuid.UUID(key.split("/")[1]) for key, _ in seen}
    assert not input_world.objects
    assert set(input_world.deletions[previous_deletions:]) == {
        (input_world.bucket, key) for key, _ in seen
    }
    assert all(body.closed for body in input_world.bodies)
    assert all(storage.closed for storage in input_world.storages)
    async with input_world.factory() as independent:
        record = (
            await independent.scalars(
                select(PredictionInputLease).where(
                    PredictionInputLease.owner_id == job_id
                )
            )
        ).one()
        assert (record.id, record.task_id, record.state) == (
            lease_id,
            task_id,
            "deleted",
        )
        assert record.deleted_at is not None
    public = await client.get(f"/api/v1/async-jobs/{job_id}", headers=headers(admin))
    assert public.status_code == 200, public.text
    detail = public.json()
    assert (detail["id"], detail["status"]) == (str(job_id), job.status)
    assert "prediction_authority" not in detail["payload"]
    prediction = (
        await db.scalars(
            select(Prediction)
            .where(Prediction.task_id == task_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    if mutation in {"version", "source", "cancel", "revoke_key", "video_result"}:
        expected = "cancelled" if mutation == "cancel" else "failed"
        assert job.status == expected, failures
        assert job.result["success_count"] == 0
        assert (prediction.id, prediction.result) == (old_id, old_result)
        return
    assert job.status == "completed" and job.result["success_count"] == 1, failures
    assert not failures
    assert prediction.id != old_id
    (shape,) = prediction.result
    assert shape["type"] == "rectanglelabels" and shape["value"] == rectangle()["value"]
    meta = await db.scalar(
        select(PredictionMeta).where(PredictionMeta.prediction_id == prediction.id)
    )
    evidence = meta.extra["reference_input"]
    assert evidence["sha256"] == binding["reference"]["sha256"]
    assert evidence["item_id"] == str(registered["item_id"])
    assert evidence["actor_id"] == proof["actor_id"]
    assert evidence["job_id"] == str(job_id)
    assert evidence["execution_snapshot_id"] == str(job.execution_snapshot_id)
    assert evidence["node_id"] == "worker:content@test"
    assert "key_proof" not in str(meta.extra) and "signature" not in str(meta.extra)
    if stages:
        assert [
            row["stage"]
            for row in meta.extra["pipeline"]["selections"]
            if row["executed"]
        ] == [7, 9, 11]


async def test_implicit_mixed_bulk_redelivery_writes_both_original_inputs_once(
    committed_reference_delivery,
    managed_worker_world,
    retained_image_storage,
    input_world,
    monkeypatch,
):
    from app.application.preannotation_results import (
        BatchResultExecution,
        PreannotationResultDeferred,
    )
    from app.services import prediction_originals
    from tests.test_managed_prediction_requests import managed_task

    db, client, world, admin = committed_reference_delivery
    _, project, reference, content = world
    managed = await managed_task(db, project, admin[0])
    source = await prediction_originals.resolve(db, managed.task.id)
    managed_worker_world.source_bytes[source.bucket, source.object_key] = managed.raw
    job, _, _, dispatch = await enqueue(
        db, client, world, admin, monkeypatch, extra={"task_ids": None}
    )
    reference_id, managed_id = reference.id, managed.task.id
    calls = []

    def predict(_operation, payload):
        task_id = uuid.UUID(payload["id"])
        if task_id == reference_id:
            _, raw = delivered_image(input_world, payload)
            assert raw == content[1]
        else:
            assert task_id == managed_id
            assert urlsplit(payload["file_path"]).hostname == "retained.test"
        calls.append((task_id, payload["file_path"]))
        return [rectangle()]

    patch_batch_execution(monkeypatch, predict)
    capture = BatchResultExecution.capture

    async def lose_reference_ack(execution, ordinal, requests, *, stage, context):
        receipt = await capture(
            execution, ordinal, requests, stage=stage, context=context
        )
        if stage == "prepared" and context["task_id"] == str(reference_id):
            raise PreannotationResultDeferred("storage_unavailable")
        return receipt

    with monkeypatch.context() as interrupted:
        interrupted.setattr(BatchResultExecution, "capture", lose_reference_ack)
        with pytest.raises(PreannotationResultDeferred, match="^storage_unavailable$"):
            await run_dispatched(dispatch)
    assert [task_id for task_id, _ in calls].count(reference_id) == 1
    assert (
        await db.scalar(select(Prediction.id).where(Prediction.task_id == reference_id))
        is None
    )
    await run_dispatched(dispatch)

    job = await jobs.get_job(db, job.id, populate_existing=True)
    assert job.status == "completed", job.error_message
    assert (job.result["success_count"], job.result["failed_count"]) == (2, 0)
    assert sorted(task_id for task_id, _ in calls) == sorted((reference_id, managed_id))
    page = await SqlAlchemyManifestDatabase(db, commit=False).call(
        "read_accepted_image_input_manifest_page",
        ManifestPageRequest(
            job_id=job.id,
            actor_id=job.user_id,
            project_id=project.id,
            selection_digest=dispatch.call_args.kwargs["kwargs"][
                "accepted_input_manifest_digest"
            ],
            sealed_digest=job.result["manifest"]["sealed_digest"],
        ),
    )
    assert page.manifest.status == "sealed" and page.manifest.selection.task_ids is None
    members = {member.task_id: member for member in page.members}
    assert members[reference_id].state == "reference"
    assert members[reference_id].input is None and members[reference_id].error is None
    assert members[managed_id].state == "ready"
    accepted = members[managed_id].input
    assert accepted.sha256 == hashlib.sha256(managed.raw).hexdigest()
    managed_url = urlsplit(dict(calls)[managed_id])
    assert managed_url.path.startswith(f"/accepted-inputs/{accepted.input_id}/")
    assert parse_qs(managed_url.query)["versionId"]
    rows = (
        await db.execute(
            select(Prediction.task_id, PredictionMeta.extra)
            .join(PredictionMeta, PredictionMeta.prediction_id == Prediction.id)
            .where(Prediction.project_id == project.id)
        )
    ).all()
    assert len(rows) == 2
    saved = dict(rows)
    assert (
        saved[reference_id]["reference_input"]["sha256"]
        == hashlib.sha256(content[1]).hexdigest()
    )
    assert "accepted_input" not in saved[reference_id]
    assert saved[managed_id]["accepted_input"]["input_id"] == str(accepted.input_id)
    assert "reference_input" not in saved[managed_id]


async def implicit_manifest_tasks(db, job, dispatch):
    envelope = dispatch.call_args.kwargs
    assert envelope["args"][2] is None
    assert envelope["kwargs"]["accepted_input_manifest_id"] == str(job.id)
    page = await SqlAlchemyManifestDatabase(db, commit=False).call(
        "read_accepted_image_input_manifest_page",
        ManifestPageRequest(
            job_id=job.id,
            actor_id=job.user_id,
            project_id=job.project_id,
            selection_digest=envelope["kwargs"]["accepted_input_manifest_digest"],
        ),
    )
    assert page.manifest.selection.task_ids is None
    assert page.next_cursor is None
    return [str(member.task_id) for member in page.members]


async def test_implicit_selection_and_pending_republish_preserve_original_tasks_and_proof(
    db_session, httpx_client, reference_world, super_admin, monkeypatch
):
    _, project, task, content = reference_world
    key = uuid.uuid4().hex
    job, _, body, dispatch = await enqueue(
        db_session,
        httpx_client,
        reference_world,
        super_admin,
        monkeypatch,
        extra={"task_ids": None, "predict_mode": "skip_predicted"},
        key=key,
    )
    original = deepcopy(job.payload["prediction_authority"])
    snapshot_id = job.execution_snapshot_id
    await create_task(db_session, project_id=project.id)
    task.total_predictions = 5
    content[5].config_revision += 1
    await db_session.commit()
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/preannotate",
        json=body,
        headers={**headers(super_admin), "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    assert response.json()["job_id"] == str(job.id)
    assert await implicit_manifest_tasks(db_session, job, dispatch) == [str(task.id)]
    assert job.payload["prediction_authority"] == original
    assert job.execution_snapshot_id == snapshot_id


@pytest.mark.parametrize("managed", [False, True])
async def test_skipped_reference_freezes_only_remaining_scope_without_read_scope(
    db_session, httpx_client, reference_world, super_admin, monkeypatch, managed
):
    from tests.test_managed_prediction_requests import managed_task

    _, project, task, content = reference_world
    task.total_predictions = 1
    other = (
        (await managed_task(db_session, project, content[2])).task if managed else None
    )
    key_response = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(super_admin),
        json={"name": "Skipped reference", "scopes": []},
    )
    assert key_response.status_code == 201, key_response.text
    issued = key_response.json()
    auth = {"Authorization": "Bearer " + issued["plaintext"]}
    key = uuid.uuid4().hex
    job, _, body, dispatch = await enqueue(
        db_session,
        httpx_client,
        reference_world,
        super_admin,
        monkeypatch,
        extra={"task_ids": None, "predict_mode": "skip_predicted"},
        key=key,
        auth=auth,
    )
    expected = [str(other.id)] if managed else []
    assert await implicit_manifest_tasks(db_session, job, dispatch) == expected
    proof = deepcopy(job.payload["prediction_authority"])
    assert proof["schema"] == 2 and proof["api_key_id"] == issued["id"]
    assert [row["task_id"] for row in proof["tasks"]] == expected
    assert all("original" in row and "reference" not in row for row in proof["tasks"])
    assert proof["key_proof"] is None
    assert job.execution_snapshot_id is not None
    await create_task(db_session, project_id=project.id)
    task.total_predictions = 0
    await db_session.commit()
    repeated = await httpx_client.post(
        f"/api/v1/projects/{project.id}/preannotate",
        json=body,
        headers={**auth, "Idempotency-Key": key},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["job_id"] == str(job.id)
    assert await implicit_manifest_tasks(db_session, job, dispatch) == expected
    assert job.payload["prediction_authority"] == proof


async def prepared(db, world, *, node="worker:content@test", key_token=None):
    _, project, task, content = world
    task.status, task.assignee_id = "pending", None
    backend = await backend_for(db, project)
    snapshot = await create_snapshot(
        db,
        project.id,
        {
            "stages": [
                {"stage": 7, "parent_stage": None, "ml_backend_id": str(backend.id)}
            ],
            "prompt": None,
            "output_mode": "mask",
            "execution_scope": "bulk",
            "execution_unit": None,
            "box_threshold": 0.3,
            "text_threshold": 0.25,
        },
    )
    token = key_token or create_access_token(
        subject=str(content[2].id), role=content[2].role
    )
    request = Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}
    )
    await get_current_principal(
        request, await bearer_scheme(request), db, IdentityGateway()
    )
    proof = await refs.capture_authority(
        db,
        project,
        content[2],
        request,
        [task],
        snapshot,
        scope="bulk",
        batch_id=None,
        predict_mode="overwrite",
    )
    job = await jobs.create_job(
        db,
        kind="batch_predict",
        user_id=content[2].id,
        project_id=project.id,
        payload={
            "prediction_authority": proof,
            "execution_scope": proof["execution_scope"],
            "batch_id": proof["batch_id"],
            "task_ids": [str(task.id)],
        },
    )
    await jobs.bind_execution_snapshot(
        db, job, actor_id=content[2].id, snapshot_id=snapshot.id
    )
    await jobs.mark_running(db, job.id)
    await db.commit()
    owner = refs.ReferencePredictionJob(
        db,
        job.id,
        proof,
        project_id=project.id,
        actor_id=content[2].id,
        task_ids=[task.id],
        batch_id=None,
        scope="bulk",
        predict_mode="overwrite",
        snapshot_id=snapshot.id,
        snapshot_digest=snapshot.digest,
        node_id=node,
    )
    return job, owner, task, project, content[2]


@pytest.mark.parametrize(
    "mutation",
    [
        "key_rotate",
        "key_revoke",
        "key_expire",
        "key_scope",
        "source",
        "version",
        "worker",
    ],
)
async def test_authority_and_binding_changes_block_delivery(
    db_session, reference_world, input_world, mutation
):
    actor = reference_world[3][2]
    key, token = await create_api_key(
        db_session, actor.id, "prediction", ["tasks:read"]
    )
    _, owner, task, _, _ = await prepared(
        db_session,
        reference_world,
        node="worker:unknown" if mutation == "worker" else "worker:content@test",
        key_token=token,
    )
    if mutation == "key_rotate":
        key.key_hash = hash_password("rotated_" + uuid.uuid4().hex)
    elif mutation == "key_revoke":
        key.revoked_at = datetime.now(timezone.utc)
    elif mutation == "key_expire":
        key.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif mutation == "key_scope":
        key.scopes = []
    elif mutation == "source":
        reference_world[3][5].enabled = False
    elif mutation == "version":
        task.version += 1
    await db_session.flush()
    expected_error = {
        "source": "source_unavailable",
        "version": "prediction_scope_changed",
        "worker": "worker_read_not_verified",
    }.get(mutation, "api_key_authority_revoked")
    with pytest.raises(ContentError, match=f"^{expected_error}$"):
        await owner.open_inputs(task, input_world.factory)
    assert not input_world.puts


async def test_receipt_is_private_and_bound_to_task_session_and_transaction(
    db_session, reference_world, input_world
):
    job, owner, task, project, actor = await prepared(db_session, reference_world)
    write_scope = capture_labeling_write_scope(
        task,
        actor_id=actor.id,
        actor_known=True,
        operation="preannotation",
        execution_scope="bulk",
        execution_scope_known=True,
        job_id=job.id,
        job_id_known=True,
    )
    await owner.open_inputs(task, input_world.factory)
    task_id, project_id = task.id, project.id
    try:
        await db_session.scalar(
            job_lock_statement(job.id, mode="update", populate_existing=True)
        )
        from app.application.preannotation_execution import (
            _preannotation_actor_revoked,
        )
        from app.services.project_write_guard import lock_actor_project_authority

        await lock_actor_project_authority(
            db_session, actor.id, project_id, nowait=False
        )
        assert not await _preannotation_actor_revoked(
            db_session, actor.id, project_id, execution_scope="bulk"
        )
        await owner.lock_key()
        await validate_task_scope(db_session, project, task, actor, "bulk", lock=True)
        async with db_session.begin_nested():
            with pytest.raises(ContentError):
                await owner.final_receipt(task)
        receipt = await owner.final_receipt(task)
        for fake in (True, {}, object()):
            with pytest.raises(ContentError):
                await refs.validate_receipt(db_session, task_id, project_id, fake)
        with pytest.raises(ContentError):
            await refs.validate_receipt(db_session, uuid.uuid4(), project_id, receipt)
        async with input_world.factory() as other:
            with pytest.raises(ContentError):
                await refs.validate_receipt(other, task_id, project_id, receipt)
        async with db_session.begin_nested():
            with pytest.raises(ContentError):
                await refs.validate_receipt(db_session, task_id, project_id, receipt)
        prediction = await PredictionService(db_session).apply_prediction_result(
            ApplyPredictionResultRequest(
                task_id=task_id,
                project_id=project_id,
                ml_backend_id=None,
                result=[],
                write_scope=write_scope,
                reference_receipt=receipt,
                pipeline_extra={
                    "reference_input": {"sha256": "forged"},
                    "reference_native": {"schema": 1, "kind": "forged"},
                },
                token_meta={"reference_native": {"schema": 1, "kind": "forged"}},
            )
        )
        meta = await db_session.scalar(
            select(PredictionMeta).where(PredictionMeta.prediction_id == prediction.id)
        )
        assert (
            meta.extra["reference_input"]["sha256"]
            == hashlib.sha256(reference_world[3][1]).hexdigest()
        )
        assert "reference_native" not in meta.extra
        await db_session.commit()
        with pytest.raises(ContentError):
            await refs.validate_receipt(db_session, task_id, project_id, receipt)
    finally:
        await db_session.rollback()
        await owner.close_inputs(task_id)


@pytest.mark.parametrize("mode", ["geometry", "crop"])
async def test_dispatch_authority_failure_cannot_degrade_into_partial(mode):
    calls = []

    class Client:
        async def predict(self, *a, **kw):
            calls.append(True)
            return [PredictionResult(task_id="root", result=[rectangle()])]

    async def guard(stage, _dispatch):
        if stage == 9:
            raise ContentError("api_key_authority_revoked", 403)

    task = SimpleNamespace(id="root")
    image = Image.new("RGB", (48, 32), "red")
    try:
        with pytest.raises(ContentError, match="^api_key_authority_revoked$"):
            await run_client_task_pipeline(
                task,
                [
                    {"stage": 7, "parent_stage": None},
                    {
                        "stage": 9,
                        "parent_stage": 7,
                        "on_failure": "keep_parent",
                        "write": {
                            "target": "attributes" if mode == "crop" else "geometry"
                        },
                    },
                ],
                [Client(), Client()],
                [None, None],
                resolve_url=lambda t: "http://owned.test",
                stage_modes=["crop", mode],
                instance_id=uuid.uuid4(),
                load_image=lambda _task: image,
                before_dispatch=guard,
            )
        assert len(calls) == 1
    finally:
        image.close()


async def test_reference_job_rejects_nonimage_delivery_before_cleanup(
    committed_reference_delivery, monkeypatch
):
    db_session, httpx_client, reference_world, super_admin = (
        committed_reference_delivery
    )
    _, project, task, _ = reference_world
    job, backend, _, dispatch = await enqueue(
        db_session,
        httpx_client,
        reference_world,
        super_admin,
        monkeypatch,
        scope="workbench",
    )
    stages = [{**pipeline(backend)[0], "source": {"execution_unit": "frame"}}]
    dispatch.reset_mock()
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/preannotate",
        headers=headers(super_admin),
        json={
            "ml_backend_id": str(backend.id),
            "task_ids": [str(task.id)],
            "pipeline_stages": stages,
            "predict_mode": "overwrite",
        },
    )
    assert response.status_code == 409, response.text
    dispatch.assert_not_called()
    from app.services.batch import BatchService

    cleanup = Mock(side_effect=AssertionError("reference reached legacy cleanup"))
    monkeypatch.setattr(BatchService, "clean_task_predictions", cleanup)
    for unit in ("frame", "video", "scene"):
        with pytest.raises(ValueError, match="Queued execution unit differs"):
            await run_preannotation(
                BatchPredictionRequest(
                    project_id=str(project.id),
                    ml_backend_id=str(backend.id),
                    task_ids=[str(task.id)],
                    user_id=str(super_admin[0].id),
                    async_job_id=str(job.id),
                    predict_mode="overwrite",
                    execution_unit=unit,
                ),
                execution_id=str(job.id),
                worker_node_id="worker:content@test",
            )
        await db_session.refresh(job)
        assert job.status == "pending"
    cleanup.assert_not_called()
