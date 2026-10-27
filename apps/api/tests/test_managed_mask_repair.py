"""Committed canonical image repair through real API, owners and RLE storage.

Only physical original/object bytes and the ML transport are substituted. SQL
authority, publication, request credentials, attempts, readers and adoption use
their production implementations in an independently committed private schema.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
from types import SimpleNamespace
import traceback
from urllib.parse import parse_qs, urlsplit
import uuid

from aap_protocol_v2 import (
    CocoRlePayload,
    NativeMaskCandidate,
    NativeMaskCandidateValue,
    native_mask_candidate_id,
)
from PIL import Image
import pytest
from sqlalchemy import func, select

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.mask_qc import MaskQCIssue
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.prediction_input import PredictionInputLease
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.schemas.async_job import AsyncJobOut
from aap_inference import extract_capabilities
from aap_inference_contract.backend_control import BackendControlSetupResult
from aap_inference_contract.execution import ModelTaskResult, thaw_json
from app.services.mask_repair import canonical_digest
from tests.inference_execution_test_ports import FakeModelExecutionPort
from app.services.raster_mask_storage import (
    build_rle_reference,
    canonical_rle_bytes,
    lock_raster_mask_references,
    reserve_raster_mask_upload,
    store_coco_rle,
)
from app.utils.raster_mask_rle import encode_coco_rle
from app.application import mask_repair_execution as worker
from app.services.jobs_queue import build_video_tracker_job_dispatcher
from tests._owned_s3 import patch_owned_client
from tests.managed_image_inference_fixtures import (
    assert_inputs_retired,
    attach_managed,
    db_session as db_session,
    input_world as input_world,
    sync_world as sync_world,
)
from tests.test_interactive_native_mask_endpoint import _setup
from tests.test_managed_image_inference import httpx_client as httpx_client
from tests.test_mask_repair_m5 import _seed_repair_issue
from tests.test_ml_backend_project_scope import _seed_backend
from tests.test_prediction_reference_actions import api_key
from tests.test_raster_mask_objects import Objects


pytestmark = pytest.mark.asyncio
ORIGINS = ("direct", "primary", "task")
PRIVATE = {
    "original_input",
    "original_annotation",
    "original_annotations",
    "original_model",
    "repair_authority",
    "credentials",
    "endpoint_url",
    "bucket",
    "storage_identity",
    "key_proof",
}


def assert_public(value):
    if isinstance(value, dict):
        assert not PRIVATE.intersection(value), PRIVATE.intersection(value)
        for child in value.values():
            assert_public(child)
    elif isinstance(value, list):
        for child in value:
            assert_public(child)


def assert_reference(actual, rle):
    expected = build_rle_reference(rle)
    assert {key: actual[key] for key in expected} == expected
    assert set(actual) - set(expected) <= {"storage_encoding"}
    assert actual.get("storage_encoding") in {None, "identity"}


@pytest.fixture
async def repair_world(db_session, sync_world, super_admin, monkeypatch, request):
    blobs = Objects()
    # The shared StorageService object remains real; its physical client alone
    # is an in-memory provider with conditional writes and complete byte reads.
    # Owned conditional writes build a per-call transport from the saved client
    # config, so that seam must forward to the same in-memory provider.
    patch_owned_client(monkeypatch, blobs.client)
    from app.services.storage import storage_service

    monkeypatch.setattr(storage_service, "client", blobs.client)

    @asynccontextmanager
    async def rle_session():
        async with sync_world.factory() as db:
            yield db

    monkeypatch.setattr("app.workers._db.task_session", rle_session)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    actor = super_admin[0]
    pixels = [0] * (48 * 32)
    pixels[49:52], pixels[-1] = [1, 1, 1], 1
    source = encode_coco_rle(pixels, 48, 32)
    island = encode_coco_rle([0] * (48 * 32 - 1) + [1], 48, 32)
    project, task, annotation, issue, _, _ = await _seed_repair_issue(
        db_session, owner_id=actor.id, source=source, island=island
    )
    origin = getattr(request, "param", "direct")
    member = await attach_managed(
        db_session,
        project,
        actor,
        task=task,
        size=(48, 32),
        origin=origin,
    )
    task.status = "review"
    project.ai_interactive_enabled = False
    issue.region_bbox = {"x0": 0.05, "y0": 0.05, "x1": 0.9, "y1": 0.9}
    backend = await _seed_backend(db_session, project.id)
    backend.health_meta = {
        "capabilities": extract_capabilities(_setup(["mask", "polygon"]))
    }
    pool = await db_session.scalar(
        select(MLBackendServicePool).where(
            MLBackendServicePool.legacy_instance_id == backend.id
        )
    )
    pool.enabled = True
    for rle in (source, island):
        await store_coco_rle(rle, db=db_session)
    await reserve_raster_mask_upload(
        db_session,
        task_id=task.id,
        object_key=build_rle_reference(source)["object_key"],
        limit=100,
    )
    await lock_raster_mask_references(db_session, annotation.geometry, task_id=task.id)
    await db_session.commit()
    w = SimpleNamespace(
        factory=sync_world.factory,
        sources=sync_world,
        inputs=sync_world.inputs,
        actor_id=actor.id,
        task_id=task.id,
        project_id=project.id,
        annotation_id=annotation.id,
        issue_id=issue.id,
        backend_id=backend.id,
        pool_id=pool.id,
        member=member,
        source=source,
        island=island,
        blobs=blobs,
        candidate=encode_coco_rle(pixels[:-1] + [0], 48, 32),
        calls=[],
        dispatched=[],
        setup_value=_setup(["mask", "polygon"]),
        setup_during=None,
        during=None,
        failure=None,
        failures=[],
        expect_unsettled=False,
        expected_input_kind="mask_repair",
        source_closed_before_ml=True,
    )

    failure_result = worker._candidate_failure_result

    def record_failure(kind, exc):
        w.failures.append(
            {
                "type": type(exc).__name__,
                "detail": getattr(exc, "detail", str(exc)),
                "frames": [
                    (frame.name, frame.lineno)
                    for frame in traceback.extract_tb(exc.__traceback__)
                ],
            }
        )
        return failure_result(kind, exc)

    monkeypatch.setattr(worker, "_candidate_failure_result", record_failure)

    async def dispatch(batch_id, *, dispatcher, execution_id, rollback=False):
        w.dispatched.append((batch_id, execution_id, rollback))
        return execution_id

    monkeypatch.setattr("app.api.v1.mask_qc.dispatch_repair_batch", dispatch)

    async def setup(registry_id):
        if w.setup_during:
            await w.setup_during()
        return BackendControlSetupResult(
            registry_id=uuid.UUID(str(registry_id)),
            setup=deepcopy(w.setup_value),
        )

    async def interactive(operation, task_input):
        task_data = thaw_json(task_input)
        context = thaw_json(operation.context)
        parsed = urlsplit(task_data["file_path"])
        bucket, key = parsed.path.lstrip("/").split("/", 1)
        version = parse_qs(parsed.query)["versionId"][0]
        assert version not in {"", "null"}
        raw = w.inputs.versions[bucket, key][version][0]
        assert raw == member.raw
        if w.source_closed_before_ml:
            assert all(stream.closed for stream in w.sources.source_files)
        with Image.open(io.BytesIO(raw)) as image:
            assert image.size == (48, 32)
        lease_id = uuid.UUID(key.split("/")[1])
        async with w.factory() as db:
            owner = await db.get(PredictionInputLease, lease_id)
            assert owner.owner_kind == w.expected_input_kind and owner.object_count == 1
            relative = key[len(owner.storage_ref["key_prefix"]) :]
            receipt = owner.inventory["objects"][relative]
            assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
            assert receipt["version_id"] == version
            assert receipt["size_bytes"] == len(raw)
        w.calls.append(deepcopy(context))
        if w.during:
            await w.during()
        if w.failure:
            raise w.failure
        rle = CocoRlePayload.model_validate(w.candidate)
        result = NativeMaskCandidate(
            value=NativeMaskCandidateValue(rle=rle, masklabels=["object"]),
            score=0.9,
            candidate_id=native_mask_candidate_id(
                rle, prompt_revision=context["prompt_revision"], candidate_index=0
            ),
        )
        return ModelTaskResult(
            task_id=task_data["id"], result=[result.model_dump(mode="json")]
        )

    class Control:
        async def setup(self, registry_id):
            return await setup(registry_id)

    control = Control()
    monkeypatch.setattr(
        "app.application.mask_repair_execution.backend_control_service",
        lambda _db: control,
    )

    class Execution:
        async def execute(self, operation, *, before_dispatch=None):
            reply = await setup(operation.requested_registry_id)
            port = FakeModelExecutionPort(
                interactive,
                pool_id=w.pool_id,
                capabilities=extract_capabilities(reply.setup),
            )
            return await port.execute(operation, before_dispatch=before_dispatch)

    monkeypatch.setattr(
        "app.application.mask_repair_execution.model_execution_port",
        lambda _db: Execution(),
    )
    # The interactive API path owns both seams independently; reuse the same
    # canonical setup fact and qualified input reader so it reaches the same
    # saved original with the truthful input kind.
    monkeypatch.setattr(
        "app.api.v1.ml_backends.backend_control_service",
        lambda _db: control,
    )
    monkeypatch.setattr(
        "app.api.v1.ml_backends.model_execution_port",
        lambda _db: Execution(),
    )
    try:
        yield w
    finally:
        if not w.expect_unsettled:
            await assert_inputs_retired(
                sync_world, task.id, monkeypatch, require_records=False
            )
        for body in blobs.bodies:
            body.close()
        blobs.objects.clear()


def auth(actor):
    return {"Authorization": "Bearer " + actor[1]}


async def plan(client, w, headers, *, sam=False):
    if sam:
        async with w.factory() as db:
            (await db.get(Project, w.project_id)).ai_interactive_enabled = True
            (await db.get(MaskQCIssue, w.issue_id)).code = "boundary_noise"
            await db.commit()
    response = await client.post(
        f"/api/v1/projects/{w.project_id}/mask-qc/repairs:dry-run",
        headers=headers,
        json={
            "actions": [
                {
                    "issue_id": str(w.issue_id),
                    "kind": "rerun_local_sam" if sam else "delete_small_islands",
                    **({"backend_id": str(w.backend_id)} if sam else {}),
                }
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["executable_count"] == 1, body
    assert_public(body)
    w.receipt, w.plan_digest = body["receipt"], body["plan_digest"]
    return body


async def execute(client, w, headers):
    response = await client.post(
        f"/api/v1/projects/{w.project_id}/mask-qc/repairs",
        headers=headers,
        json={"receipt": w.receipt, "plan_digest": w.plan_digest},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert_public(body)
    w.batch_id, w.job_id = uuid.UUID(body["id"]), uuid.UUID(body["async_job_id"])
    return body


async def forward(w, *, job_id=None):
    identity = job_id or w.job_id
    async with w.factory() as db:
        job = await db.get(AsyncJob, identity)
        celery_task_id = job.celery_task_id
    await worker.run_mask_repair(
        str(w.batch_id),
        celery_task_id,
        session_factory=w.factory,
        tracker_dispatcher=build_video_tracker_job_dispatcher(),
        worker_node_id="worker:managed-repair@test",
    )


async def rollback_worker(w, job_id):
    async with w.factory() as db:
        job = await db.get(AsyncJob, uuid.UUID(str(job_id)))
        celery_task_id = job.celery_task_id
    await worker.run_mask_repair_rollback(
        str(w.batch_id),
        celery_task_id,
        session_factory=w.factory,
        worker_node_id="worker:managed-repair@test",
    )


async def state(w):
    async with w.factory() as db:
        batch = await db.get(MaskRepairBatch, w.batch_id)
        job = await db.get(AsyncJob, w.job_id)
        annotation = await db.get(Annotation, w.annotation_id)
        candidates = list(
            await db.scalars(select(Prediction).where(Prediction.task_id == w.task_id))
        )
        assert_public(AsyncJobOut.model_validate(job).model_dump(mode="json"))
        return batch, job, annotation, candidates


async def candidate(client, w, headers):
    await plan(client, w, headers, sam=True)
    await execute(client, w, headers)
    await forward(w)
    batch, job, parent, predictions = await state(w)
    assert (batch.status, job.status, parent.version, len(predictions)) == (
        "completed",
        "completed",
        1,
        1,
    ), batch.result_json
    return predictions[0]


@pytest.mark.parametrize("repair_world", ORIGINS, indirect=True)
async def test_deterministic_ai_off_exact_pixels_replay_and_rollback(
    repair_world, httpx_client, super_admin
):
    w, headers = repair_world, auth(super_admin)
    await plan(httpx_client, w, headers)
    await execute(httpx_client, w, headers)
    await forward(w)
    batch, job, annotation, predictions = await state(w)
    assert (batch.status, job.status, annotation.version, len(predictions)) == (
        "completed",
        "completed",
        2,
        0,
    ), batch.result_json
    assert_reference(annotation.geometry["mask"], w.candidate)
    assert not w.calls and not w.inputs.puts
    assert all(stream.closed for stream in w.sources.source_files)
    before = deepcopy(batch.result_json)
    await forward(w)
    replay = await state(w)
    assert replay[2].version == 2 and replay[0].result_json == before
    response = await httpx_client.get(
        f"/api/v1/mask-qc/repairs/{w.batch_id}", headers=headers
    )
    assert response.status_code == 200, response.text
    assert_public(response.json())
    rollback = await httpx_client.post(
        f"/api/v1/mask-qc/repairs/{w.batch_id}/rollback",
        headers=headers,
        json={"expected_result_digest": response.json()["result_digest"]},
    )
    assert rollback.status_code == 202, rollback.text
    rollback_id = rollback.json()["rollback_async_job_id"]
    await rollback_worker(w, rollback_id)
    batch, _, annotation, _ = await state(w)
    assert (batch.status, annotation.version) == ("rolled_back", 3), (
        w.failures,
        batch.result_json.get("rollback"),
    )
    assert_reference(annotation.geometry["mask"], w.source)
    assert not w.inputs.puts


@pytest.mark.parametrize("repair_world", ORIGINS, indirect=True)
async def test_sam_owned_input_public_projection_preview_adopt_and_read(
    repair_world, httpx_client, super_admin
):
    w, headers = repair_world, auth(super_admin)
    prediction = await candidate(httpx_client, w, headers)
    async with w.factory() as db:
        batch = await db.get(MaskRepairBatch, w.batch_id)
        assert canonical_digest(batch.plan_json) == batch.plan_digest
        assert batch.plan_json["items"][0]["original_input"]
        meta = await db.scalar(
            select(PredictionMeta).where(PredictionMeta.prediction_id == prediction.id)
        )
        proof = meta.extra["original_input"]
        assert (proof["width"], proof["height"]) == (48, 32)
        assert proof["sha256"] == w.member.source.sha256
    url = f"/api/v1/tasks/{w.task_id}/predictions/{prediction.id}"
    content = await httpx_client.get(url + "/mask-content/0", headers=headers)
    assert content.status_code == 200, content.text
    assert content.json() == w.candidate
    accepted = await httpx_client.post(url + "/accept", headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert len(accepted.json()) == 1
    annotation_id = accepted.json()[0]["id"]
    readable = await httpx_client.get(
        f"/api/v1/annotations/{annotation_id}/mask-content", headers=headers
    )
    assert readable.status_code == 200, readable.text
    assert readable.json() == w.candidate
    replay = await httpx_client.post(url + "/accept", headers=headers)
    assert replay.status_code == 409, replay.text
    assert len(w.calls) == 1 and len(w.inputs.puts) == 1


@pytest.mark.parametrize("sam", [False, True])
@pytest.mark.parametrize(
    "change", ["native_gate", "parent", "canonical", "role", "key"]
)
async def test_worker_revalidates_execute_authority_before_mutation(
    repair_world, httpx_client, super_admin, change, sam
):
    w = repair_world
    key_id, headers = await api_key(httpx_client, super_admin, ["annotations:write"])
    await plan(httpx_client, w, headers, sam=sam)
    await execute(httpx_client, w, headers)
    async with w.factory() as db:
        if change == "native_gate":
            (
                await db.get(Project, w.project_id)
            ).raster_mask_native_editing_enabled = False
        elif change == "parent":
            (await db.get(Annotation, w.annotation_id)).geometry = {
                "type": "raster_mask",
                "mask": build_rle_reference(w.island),
            }
        elif change == "canonical":
            (await db.get(Task, w.task_id)).dataset_item_id = None
        elif change == "role":
            (await db.get(User, w.actor_id)).role = "employee"
        else:
            (await db.get(ApiKey, key_id)).revoked_at = datetime.now(timezone.utc)
        await db.commit()
    await forward(w)
    batch, job, annotation, predictions = await state(w)
    assert (batch.status, job.status, annotation.version, len(predictions)) == (
        "failed",
        "failed",
        2 if change == "parent" else 1,
        0,
    ), batch.result_json
    assert not w.calls and not w.inputs.puts


@pytest.mark.parametrize(
    "change", ["key", "parent", "canonical", "native_gate", "model", "ai_off"]
)
async def test_sam_after_actual_get_cannot_publish_stale_result(
    repair_world, httpx_client, super_admin, change
):
    w = repair_world
    key_id, headers = await api_key(httpx_client, super_admin, ["annotations:write"])
    await plan(httpx_client, w, headers, sam=True)
    await execute(httpx_client, w, headers)

    async def change_now():
        async with w.factory() as db:
            if change == "key":
                (await db.get(ApiKey, key_id)).revoked_at = datetime.now(timezone.utc)
            elif change == "parent":
                (await db.get(Annotation, w.annotation_id)).class_name = "changed"
            elif change == "canonical":
                (await db.get(Task, w.task_id)).dataset_item_id = None
            elif change == "native_gate":
                (
                    await db.get(Project, w.project_id)
                ).raster_mask_native_editing_enabled = False
            elif change == "model":
                (await db.get(MLBackendRegistry, w.backend_id)).health_meta = {}
            else:
                (await db.get(Project, w.project_id)).ai_interactive_enabled = False
            await db.commit()

    w.during = change_now
    await forward(w)
    batch, job, annotation, predictions = await state(w)
    assert len(w.calls) == 1
    assert (batch.status, job.status, annotation.version, len(predictions)) == (
        "failed",
        "failed",
        2 if change == "parent" else 1,
        0,
    ), batch.result_json


@pytest.mark.parametrize(
    "change",
    ["result", "metadata", "ledger", "parent", "plan", "lock", "canonical", "bytes"],
)
async def test_candidate_preview_and_adoption_reject_tampered_provenance(
    repair_world, httpx_client, super_admin, change
):
    w, headers = repair_world, auth(super_admin)
    prediction = await candidate(httpx_client, w, headers)
    async with w.factory() as db:
        if change == "result":
            row = await db.scalar(
                select(Prediction).where(Prediction.id == prediction.id)
            )
            row.result = [{**row.result[0], "score": 0.123}]
        elif change == "metadata":
            meta = await db.scalar(
                select(PredictionMeta).where(
                    PredictionMeta.prediction_id == prediction.id
                )
            )
            meta.extra = {**meta.extra, "unowned": True}
        elif change == "ledger":
            row = await db.get(MaskRepairBatch, w.batch_id)
            value = deepcopy(row.result_json)
            value["candidates"]["0"]["candidate_records"] = {}
            row.result_json = value
        elif change == "plan":
            row = await db.get(MaskRepairBatch, w.batch_id)
            row.plan_json = {**row.plan_json, "changed": True}
        elif change == "parent":
            (await db.get(Annotation, w.annotation_id)).attributes = {"changed": True}
        elif change == "lock":
            (await db.get(Annotation, w.annotation_id)).is_locked = True
        elif change == "canonical":
            (await db.get(Task, w.task_id)).dataset_item_id = None
        else:
            for key in list(w.blobs.objects):
                if w.blobs.objects[key] == canonical_rle_bytes(w.candidate):
                    w.blobs.objects[key] = b"corrupt"
        await db.commit()
    url = f"/api/v1/tasks/{w.task_id}/predictions/{prediction.id}"
    preview = await httpx_client.get(url + "/mask-content/0", headers=headers)
    assert preview.status_code in {404, 409, 503}, preview.text
    accepted = await httpx_client.post(url + "/accept", headers=headers)
    assert accepted.status_code in {404, 409, 503}, accepted.text
    async with w.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(Annotation.parent_prediction_id == prediction.id)
            )
            == 0
        )


async def test_offline_candidate_rejection_has_no_source_io(
    repair_world, httpx_client, super_admin
):
    w, headers = repair_world, auth(super_admin)
    prediction = await candidate(httpx_client, w, headers)
    w.sources.source_bytes.clear()
    before = len(w.sources.captured)
    url = f"/api/v1/tasks/{w.task_id}/predictions/{prediction.id}"
    rejected = await httpx_client.post(url + "/reject", headers=headers)
    assert rejected.status_code == 204, rejected.text
    assert len(w.sources.captured) == before
    hidden = await httpx_client.get(url + "/mask-content/0", headers=headers)
    assert hidden.status_code == 404, hidden.text


async def test_concurrent_candidate_acceptance_commits_one_annotation(
    repair_world, httpx_client, super_admin
):
    w, headers = repair_world, auth(super_admin)
    prediction = await candidate(httpx_client, w, headers)
    url = f"/api/v1/tasks/{w.task_id}/predictions/{prediction.id}/accept"
    responses = await asyncio.gather(
        *(httpx_client.post(url, headers=headers) for _ in range(2))
    )
    assert sorted(r.status_code for r in responses) == [200, 409], [
        r.text for r in responses
    ]
    async with w.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(Annotation.parent_prediction_id == prediction.id)
            )
            == 1
        )


@pytest.mark.parametrize("repair_world", ORIGINS, indirect=True)
async def test_resume_renews_request_authority_but_preserves_plan_and_source(
    repair_world, httpx_client, super_admin
):
    w = repair_world
    old_key, headers = await api_key(httpx_client, super_admin, ["annotations:write"])
    await plan(httpx_client, w, headers, sam=True)
    await execute(httpx_client, w, headers)
    w.failure = TimeoutError("controlled failed inference")
    await forward(w)
    before, job, annotation, candidates = await state(w)
    assert (before.status, job.status, annotation.version, len(candidates)) == (
        "failed",
        "failed",
        1,
        0,
    ), before.result_json
    old_job = w.job_id
    frozen = deepcopy(before.plan_json)
    async with w.factory() as db:
        (await db.get(ApiKey, old_key)).revoked_at = datetime.now(timezone.utc)
        (await db.get(Task, w.task_id)).version += 1
        await db.commit()
    _, fresh = await api_key(httpx_client, super_admin, ["annotations:write"])
    resumed = await httpx_client.post(
        f"/api/v1/mask-qc/repairs/{w.batch_id}/resume", headers=fresh
    )
    assert resumed.status_code == 202, resumed.text
    w.job_id = uuid.UUID(resumed.json()["async_job_id"])
    assert w.job_id != old_job
    w.failure = None
    count = len(w.calls)
    await forward(w, job_id=old_job)
    assert len(w.calls) == count
    await forward(w)
    after, job, annotation, candidates = await state(w)
    assert (after.status, job.status, annotation.version, len(candidates)) == (
        "completed",
        "completed",
        1,
        1,
    ), after.result_json
    assert after.plan_json == frozen and after.plan_digest == w.plan_digest


@pytest.mark.parametrize("change", ["version", "geometry", "locked"])
async def test_rollback_refuses_any_changed_after_state(
    repair_world, httpx_client, super_admin, change
):
    w, headers = repair_world, auth(super_admin)
    await plan(httpx_client, w, headers)
    await execute(httpx_client, w, headers)
    await forward(w)
    async with w.factory() as db:
        row = await db.get(Annotation, w.annotation_id)
        if change == "version":
            row.version += 1
        elif change == "geometry":
            row.geometry = {
                "type": "raster_mask",
                "mask": build_rle_reference(w.island),
            }
        else:
            row.is_locked = True
        await db.commit()
    batch, _, modified, _ = await state(w)
    expected = deepcopy(modified.geometry)
    requested = await httpx_client.post(
        f"/api/v1/mask-qc/repairs/{w.batch_id}/rollback",
        headers=headers,
        json={"expected_result_digest": canonical_digest(batch.result_json)},
    )
    if requested.status_code == 202:
        rollback_id = requested.json()["rollback_async_job_id"]
        await rollback_worker(w, rollback_id)
        batch, _, modified, _ = await state(w)
        assert batch.status != "rolled_back", batch.result_json
        assert modified.geometry == expected
    else:
        assert requested.status_code == 409, requested.text
