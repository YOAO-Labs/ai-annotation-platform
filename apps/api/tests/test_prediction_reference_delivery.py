"""Frozen reference preannotation scopes, same-byte delivery and write receipts."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid
from unittest.mock import Mock

from fastapi import HTTPException
from PIL import Image
import pytest
from sqlalchemy import func, select

from app.core.security import create_access_token, hash_password
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.prediction import Prediction, PredictionMeta, FailedPrediction
from app.db.models.prediction_input import PredictionInputLease
from app.services import prediction_references as refs, async_job as jobs
from app.services.content_snapshot import ContentError
from app.services.ml_client import PredictionResult
from app.services.prediction import PredictionService
from app.services.prediction_execution import create_snapshot, validate_task_scope
from app.services.prediction_inputs import PredictionInputs
from app.workers import tasks as worker
from tests.conftest import create_registry_with_pool
from tests.factory import create_task
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_prediction_inputs import input_world as input_world
from tests.test_prediction_jobs_worker import _passthrough_engine_and_factory


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


@pytest.mark.parametrize("scope", ["bulk", "workbench"])
@pytest.mark.parametrize("stages", [False, True])
@pytest.mark.parametrize("mutation", [None, "version", "source", "cancel", "physical"])
async def test_api_to_worker_reference_snapshot_and_roi_are_one_owned_input(
    db_session,
    httpx_client,
    reference_world,
    super_admin,
    monkeypatch,
    input_world,
    scope,
    stages,
    mutation,
):
    _, project, task, content = reference_world
    old = Prediction(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        result=[rectangle()],
        model_version="previous",
        source="batch",
    )
    db_session.add(old)
    task.total_predictions = 1
    await db_session.flush()
    old_id = old.id
    job, backend, _, dispatch = await enqueue(
        db_session,
        httpx_client,
        reference_world,
        super_admin,
        monkeypatch,
        scope=scope,
        stages=stages,
    )
    proof = deepcopy(job.payload["prediction_authority"])
    task_id, project_id, job_id = task.id, project.id, job.id
    assert (
        proof["tasks"][0]["reference"]["sha256"]
        == hashlib.sha256(content[1]).hexdigest()
    )
    assert job.execution_snapshot_id is not None
    original_create = PredictionInputs.create.__func__

    async def create(cls, factory, **kwargs):
        return await original_create(cls, input_world.factory, **kwargs)

    monkeypatch.setattr(PredictionInputs, "create", classmethod(create))
    seen = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def predict(self, tasks, context=None):
            for payload in tasks:
                bucket, object_key = (
                    urlsplit(payload["file_path"]).path.lstrip("/").split("/", 1)
                )
                raw = input_world.objects[bucket, object_key]
                seen.append((object_key, raw))
                if "/original/" in object_key:
                    assert raw == content[1]
                else:
                    with Image.open(io.BytesIO(raw)) as image:
                        assert image.size == (48, 32)
                        assert image.getpixel((10, 10))[0] > 240
            if "/crops/" in seen[-1][0]:
                await mutate_after_inference()
                return [
                    PredictionResult(
                        task_id=t["id"], result=[{"attributes": {"color": "red"}}]
                    )
                    for t in tasks
                ]
            shape = rectangle()
            if tasks[0].get("prompts"):
                shape["parent_box_idx"] = 0
            if not stages:
                await mutate_after_inference()
            elif len(seen) == 1 and mutation == "physical":
                # Geometry and ROI must use the already captured image even if
                # the registered path changes while a model is running.
                (content[0] / "图片 空格.png").write_bytes(b"replaced")
            return [
                PredictionResult(
                    task_id=tasks[0]["id"], result=[shape], meta={"total_cost": 0.25}
                )
            ]

    async def mutate_after_inference():
        if mutation == "version":
            task.version += 1
        elif mutation == "source":
            content[5].enabled = False
        elif mutation == "cancel":
            job.payload = {**job.payload, "cancel_requested": True}
        elif mutation == "physical":
            (content[0] / "图片 空格.png").write_bytes(b"replaced")
        await db_session.commit()

    monkeypatch.setattr("app.services.ml_client.MLBackendClient", Client)
    monkeypatch.setattr(
        worker,
        "_load_task_image",
        lambda *a: pytest.fail("reference ROI reread original"),
    )
    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", fake_engine)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.async_sessionmaker", fake_factory)
    monkeypatch.setattr(worker, "_publish_progress", lambda *a, **kw: None)
    kwargs = dict(dispatch.call_args.kwargs["kwargs"])
    await worker._run_batch(
        *dispatch.call_args.kwargs["args"],
        **kwargs,
        celery_task_id=str(job_id),
        worker_node_id="worker:content@test",
    )
    job = await db_session.get(AsyncJob, job_id, populate_existing=True)
    failures = (
        await db_session.execute(
            select(FailedPrediction.error_type, FailedPrediction.message).where(
                FailedPrediction.project_id == project_id
            )
        )
    ).all()
    if mutation in {"version", "source", "cancel"}:
        assert job.status == ("cancelled" if mutation == "cancel" else "failed"), (
            failures
        )
        assert (
            await db_session.scalars(
                select(Prediction.id).where(Prediction.task_id == task_id)
            )
        ).all() == [old_id]
        assert not input_world.objects
        assert set(input_world.deletions) == {
            (input_world.bucket, key) for key, _ in seen
        }
        return
    assert job.status == "completed", failures
    prediction = await db_session.scalar(
        select(Prediction).where(Prediction.task_id == task_id)
    )
    assert prediction is not None
    assert prediction.id != old_id
    meta = await db_session.scalar(
        select(PredictionMeta).where(PredictionMeta.prediction_id == prediction.id)
    )
    evidence = meta.extra["reference_input"]
    assert evidence["sha256"] == proof["tasks"][0]["reference"]["sha256"]
    assert evidence["item_id"] == proof["tasks"][0]["item_id"]
    assert evidence["execution_snapshot_id"] == str(job.execution_snapshot_id)
    assert evidence["node_id"] == "worker:content@test"
    assert "key_proof" not in str(meta.extra) and "signature" not in str(meta.extra)
    assert not input_world.objects
    assert set(input_world.deletions) == {(input_world.bucket, key) for key, _ in seen}
    assert all(body.closed for body in input_world.bodies)
    assert all(storage.closed for storage in input_world.storages)
    assert len({key.split("/")[1] for key, _ in seen}) == 1
    assert len(seen) == (3 if stages else 1)
    if stages:
        assert [
            row["stage"]
            for row in meta.extra["pipeline"]["selections"]
            if row["executed"]
        ] == [7, 9, 11]
    async with input_world.factory() as independent:
        records = list(
            await independent.scalars(
                select(PredictionInputLease).where(
                    PredictionInputLease.owner_id == job_id
                )
            )
        )
        assert len(records) == 1 and records[0].state == "deleted"
        assert records[0].deleted_at is not None
    public = await httpx_client.get(
        f"/api/v1/async-jobs/{job_id}", headers=headers(super_admin)
    )
    assert public.status_code == 200
    assert "prediction_authority" not in public.json()["payload"]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(FailedPrediction)
            .where(FailedPrediction.project_id == project_id)
        )
        == 0
    )


async def test_implicit_selection_and_pending_republish_preserve_original_tasks_and_proof(
    db_session, httpx_client, reference_world, super_admin, monkeypatch
):
    _, project, task, content = reference_world
    key = uuid.uuid4().hex
    job, backend, body, dispatch = await enqueue(
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
    late = await create_task(db_session, project_id=project.id)
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
    assert dispatch.call_args.kwargs["args"][2] == [str(task.id)]
    assert str(late.id) not in dispatch.call_args.kwargs["args"][2]
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
    assert dispatch.call_args.kwargs["args"][2] == expected
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
    assert dispatch.call_args.kwargs["args"][2] == expected
    assert job.payload["prediction_authority"] == proof


async def prepared(
    db, world, key_id=None, node="worker:content@test", *, key_token=None
):
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
    if key_id is None:
        token = create_access_token(subject=str(content[2].id), role=content[2].role)
        scopes = None
    else:
        assert key_token is not None, (
            "a Key proof requires its real presented credential"
        )
        token = key_token
        scopes = list((await db.get(ApiKey, key_id)).scopes)
    request = SimpleNamespace(
        state=SimpleNamespace(api_key_id=key_id, api_key_scopes=scopes),
        headers={"authorization": "Bearer " + token},
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
        payload={"prediction_authority": proof},
    )
    job.execution_snapshot_id = snapshot.id
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
    token = "ak_fixture_" + uuid.uuid4().hex
    key = ApiKey(
        user_id=actor.id,
        name="prediction",
        key_prefix="ak_reference",
        key_hash=hash_password(token),
        scopes=["tasks:read"],
    )
    db_session.add(key)
    await db_session.flush()
    job, owner, task, project, actor = await prepared(
        db_session,
        reference_world,
        key.id,
        node="worker:unknown" if mutation == "worker" else "worker:content@test",
        key_token=token,
    )
    if mutation == "key_rotate":
        key.key_hash = "replacement"
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
    with pytest.raises((ContentError, HTTPException)):
        await owner.open_inputs(task, input_world.factory)
    assert not input_world.puts


async def test_receipt_is_private_and_bound_to_task_session_and_transaction(
    db_session, reference_world, input_world
):
    job, owner, task, project, actor = await prepared(db_session, reference_world)
    await owner.open_inputs(task, input_world.factory)
    task_id, project_id = task.id, project.id
    try:
        await jobs.lock_job(db_session, job.id)
        assert await worker._hold_preannotation_authority(db_session, actor, project_id)
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
        prediction = await PredictionService(db_session).create_from_ml_result(
            task_id,
            project_id,
            None,
            [],
            reference_receipt=receipt,
            pipeline_extra={
                "reference_input": {"sha256": "forged"},
                "reference_native": {"schema": 1, "kind": "forged"},
            },
            token_meta={"reference_native": {"schema": 1, "kind": "forged"}},
        )
        meta = await db_session.scalar(
            select(PredictionMeta).where(PredictionMeta.prediction_id == prediction.id)
        )
        assert meta.extra["reference_input"]["sha256"] != "forged"
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

    checks = 0

    async def guard():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ContentError("api_key_authority_revoked", 403)

    task = SimpleNamespace(id="root")
    image = Image.new("RGB", (48, 32), "red")
    try:
        with pytest.raises(ContentError):
            await worker._run_task_pipeline(
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
                load_image=lambda t: image.copy(),
                before_dispatch=guard,
            )
        assert len(calls) == 1
    finally:
        image.close()


async def test_reference_frame_execution_cannot_reach_legacy_cleanup(
    db_session, httpx_client, reference_world, super_admin, monkeypatch
):
    _, project, task, _ = reference_world
    task.status = "pending"
    task.assignee_id = None
    backend = await backend_for(db_session, project)
    stages = [{**pipeline(backend)[0], "source": {"execution_unit": "frame"}}]
    dispatch = Mock()
    monkeypatch.setattr(worker.batch_predict, "apply_async", dispatch)
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
    fake_engine, fake_factory = _passthrough_engine_and_factory(db_session)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", fake_engine)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.async_sessionmaker", fake_factory)
    with pytest.raises(ContentError):
        await worker._run_batch(
            str(project.id),
            str(backend.id),
            [str(task.id)],
            predict_mode="overwrite",
            execution_unit="frame",
        )
    cleanup.assert_not_called()
