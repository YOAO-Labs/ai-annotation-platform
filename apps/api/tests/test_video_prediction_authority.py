"""Whole-video authority helpers, physical settlement and private receipts.

These focused fakes do not replace the committed-schema HTTP/Worker acceptance
suite. They isolate closed proof shapes and asynchronous ownership boundaries.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.db.enums import PlatformRole
from app.services import comment_attachment_authority as credentials
from app.services import original_sources, prediction_originals as authority
from app.services import storage_access
from app.services import video_prediction_originals as originals
from app.services import video_prediction_references as video
from app.services.content_snapshot import ContentError
from app.services.ml_client import MLBackendClient
from app.services.reference_bindings import task_binding


def caps():
    return {
        "task_contract_revision": 1,
        "models": [
            {
                "id": "track",
                "task": "tracker",
                "supported_inputs": ["video"],
                "supported_geometric_outputs": ["bbox"],
                "params": {
                    "properties": {"persist": {"type": "boolean", "default": True}}
                },
                "resource_profile": {"device": "cpu", "batchable": True},
            }
        ],
    }


def probe():
    return {
        "probe_contract": 1,
        "video": {
            "width": 128,
            "height": 96,
            "fps": 25.0,
            "frame_count": 6,
            "duration_ms": 240,
            "codec": "h264",
        },
    }


def source():
    task_id, project_id, item_id, content_id, location_id = (
        uuid.uuid4() for _ in range(5)
    )
    sha, key = "d" * 64, "saved/clip.mp4"
    relation = original_sources.TaskRelation(
        task_id, project_id, None, key, "video", None, item_id, None, None, item_id, ()
    )
    return original_sources.OriginalSource(
        "dataset_asset",
        content_id,
        sha,
        6,
        "video",
        item_id,
        relation,
        "saved-bucket",
        key,
        "http://saved.test:9000",
        location_id,
        3,
        7,
        original_sources.AssetFacts(content_id, item_id, "video", sha, 6),
        original_sources.ItemFacts(item_id, uuid.uuid4(), key, "video", 1, 2),
        original_sources.ReplicaFacts(
            content_id,
            location_id,
            "saved-bucket",
            key,
            "original",
            "present",
            None,
            None,
            None,
        ),
        original_sources.LocationFacts(
            location_id, "s3", True, 3, "saved.test:9000", False, None
        ),
    )


def jwt_value(actor_id):
    return authority.credentials_value(
        credentials.Authority(
            actor_id,
            None,
            None,
            credentials.JwtProof(
                datetime.now(timezone.utc) + timedelta(minutes=5), 4, "jti"
            ),
            frozenset(PlatformRole),
        )
    )


def task_for(value):
    return SimpleNamespace(
        id=value.relation.task_id,
        project_id=value.relation.project_id,
        batch_id=None,
        dataset_item_id=value.relation.direct_item_id,
        file_name="clip.mp4",
        file_path=value.object_key,
        file_type="video",
        version=2,
    )


def setup_job():
    value = source()
    task = task_for(value)
    actor_id, job_id, snapshot_id, backend_id, pool_id = (
        uuid.uuid4() for _ in range(5)
    )
    stage = {
        "stage": 7,
        "parent_stage": None,
        "ml_backend_id": str(backend_id),
        "model_id": "track",
        "task": "tracker",
    }
    declaration = video.model_declaration(caps(), stage)
    models = [
        {
            "stage": 7,
            "parent_stage": None,
            "requested_registry_id": str(backend_id),
            "pool_id": str(pool_id),
            "model_id": "track",
            "declaration": declaration,
            "declaration_digest": authority.digest(declaration),
            "config_digest": authority.digest(stage),
            "identity_guarantee": "declared_configuration",
        }
    ]
    proof = {
        "schema": 1,
        "media_type": "video",
        "job_kind": "batch_predict",
        "actor_id": str(actor_id),
        "project_id": str(task.project_id),
        "batch_id": None,
        "execution_scope": "bulk",
        "predict_mode": "append",
        "execution_unit": None,
        "api_key_id": None,
        "credentials": jwt_value(actor_id),
        "snapshot_id": str(snapshot_id),
        "snapshot_digest": "a" * 64,
        "tasks": [{**task_binding(task), "original": originals.source_receipt(value)}],
        "models": models,
    }
    transaction = object()
    sync = SimpleNamespace(
        get_transaction=lambda: transaction, in_nested_transaction=lambda: False
    )
    db = SimpleNamespace(sync_session=sync)
    kwargs = dict(
        project_id=task.project_id,
        actor_id=actor_id,
        task_ids=[task.id],
        batch_id=None,
        scope="bulk",
        predict_mode="append",
        snapshot_id=snapshot_id,
        snapshot_digest="a" * 64,
        node_id="worker:video",
        celery_task_id=str(job_id),
    )
    owner = video.VideoPredictionJob(db, job_id, proof, **kwargs)
    return SimpleNamespace(
        owner=owner,
        task=task,
        proof=proof,
        db=db,
        kwargs=kwargs,
        transaction=transaction,
        stage=stage,
        actor_id=actor_id,
        job_id=job_id,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 2),
        ("media_type", "image"),
        ("job_kind", "video_tracker"),
        ("execution_unit", "frame"),
        ("snapshot_digest", "b" * 64),
        ("credentials", None),
    ],
)
def test_job_rejects_other_contracts_and_mutated_scope(field, value):
    world = setup_job()
    proof = deepcopy(world.proof)
    proof[field] = value
    with pytest.raises(ContentError):
        video.VideoPredictionJob(world.db, world.job_id, proof, **world.kwargs)


@pytest.mark.parametrize(
    "mutation",
    ["extra", "duplicate", "missing", "bool_version", "model_digest", "bool_stage"],
)
def test_closed_bindings_models_and_exact_selection(mutation):
    world = setup_job()
    proof = deepcopy(world.proof)
    if mutation == "extra":
        proof["credential_token"] = "forbidden"
    elif mutation == "duplicate":
        proof["tasks"].append(deepcopy(proof["tasks"][0]))
    elif mutation == "missing":
        proof["tasks"].clear()
    elif mutation == "bool_version":
        proof["tasks"][0]["version"] = True
    elif mutation == "model_digest":
        proof["models"][0]["declaration_digest"] = "f" * 64
    else:
        proof["models"][0]["stage"] = True
    with pytest.raises(ContentError):
        video.VideoPredictionJob(world.db, world.job_id, proof, **world.kwargs)


def test_proof_is_bounded_detached_and_requires_real_delivery():
    world = setup_job()
    world.proof["tasks"][0]["file_name"] = "mutated.mp4"
    assert world.owner.failure_binding(world.task.id)["file_name"] == "clip.mp4"
    kwargs = {**world.kwargs, "celery_task_id": ""}
    with pytest.raises(ContentError):
        video.VideoPredictionJob(world.db, world.job_id, world.owner.proof, **kwargs)
    with pytest.raises(ContentError, match="budget"):
        video.bounded({"oversized": "x" * video.MAX_PROOF_BYTES})
    with pytest.raises(ContentError):
        video.bounded({"nonfinite": float("nan")})


def test_declared_model_projection_normalizes_defaults_and_preserves_json_types():
    stage = {"model_id": "track", "task": "tracker"}
    first = caps()
    second = caps()
    second["models"][0].update(
        display_name="Renamed", classes=[{"index": 0, "name": "car"}]
    )
    assert video.model_declaration(first, stage) == video.model_declaration(
        second, stage
    )
    normalized = video.extract_capabilities(first)
    assert video.model_declaration(first, stage) == video.model_declaration(
        normalized, stage
    )
    second["models"][0]["params"]["properties"]["persist"]["default"] = 1
    assert not video._same(
        video.model_declaration(first, stage), video.model_declaration(second, stage)
    )
    assert video.model_declaration(first, {"task": "tracker"})["model"]["id"] == "track"
    second["models"].append({**deepcopy(second["models"][0]), "id": "another"})
    with pytest.raises(ContentError, match="unrecorded"):
        video.model_declaration(second, {"task": "tracker"})


@pytest.mark.parametrize(
    "key,value",
    [
        ("width", True),
        ("fps", float("inf")),
        ("frame_count", 1_000_001),
        ("duration_ms", -1),
        ("codec", ""),
    ],
)
def test_probe_has_strict_h2_shape_and_numeric_types(key, value):
    data = probe()
    data["video"][key] = value
    with pytest.raises(ContentError, match="probe_invalid"):
        video._probe(data)


def evidence(world):
    return {
        **world.owner.failure_binding(world.task.id),
        "schema": 1,
        "media_type": "video",
        "job_id": str(world.job_id),
        "actor_id": str(world.actor_id),
        "node_id": "worker:video",
        "execution_snapshot_id": world.proof["snapshot_id"],
        "execution_digest": world.proof["snapshot_digest"],
        "probe": probe(),
        "models": [
            {**world.proof["models"][0], "executed_registry_id": str(uuid.uuid4())}
        ],
        "input_id": str(uuid.uuid4()),
        "input_version_id": "frozen-version",
    }


def test_public_evidence_never_contains_private_locators_credentials_or_declarations():
    world = setup_job()
    private = evidence(world)
    private.update(
        credentials=world.proof["credentials"], nested={"endpoint_url": "hidden"}
    )
    public = video.public_provenance(private)
    serialized = authority.encoded(public)
    for forbidden in (
        "endpoint_url",
        "bucket",
        "object_key",
        "file_path",
        "file_name",
        "credentials",
        '"facts"',
        '"declaration"',
        "saved/clip.mp4",
    ):
        assert forbidden not in serialized
    assert public["original"]["sha256"] == private["original"]["sha256"]
    assert public["probe"]["video"]["width"] == 128
    assert public["original"]["facts_digest"] == private["original"]["facts_digest"]


def test_failed_execution_keeps_first_models_and_source_without_credentials():
    world = setup_job()
    failure = world.owner.failure_evidence(world.task.id)
    assert failure["original_video_input"] == world.proof["tasks"][0]
    assert failure["video_execution"]["models"] == world.proof["models"]
    assert "credentials" not in authority.encoded(failure)
    failure["video_execution"]["models"].clear()
    assert world.owner.proof["models"]


async def test_finish_source_waits_for_real_close_under_repeated_cancellation():
    world = setup_job()
    entered, release = asyncio.Event(), asyncio.Event()
    source = SimpleNamespace(closed=False, settled=False, unsettled=False)

    async def close():
        entered.set()
        await release.wait()
        source.closed = source.settled = True

    source.aclose = close
    world.owner.sources[world.task.id] = source
    pending = asyncio.create_task(world.owner.finish_source(world.task.id))
    await entered.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    assert world.task.id not in world.owner.finished
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert source.closed and source.settled
    assert world.task.id not in world.owner.finished


async def test_unknown_source_close_stays_disqualified_on_second_call():
    world = setup_job()
    source = SimpleNamespace(
        closed=False, settled=False, unsettled=True, aclose=AsyncMock()
    )
    world.owner.sources[world.task.id] = source
    with pytest.raises(ContentError, match="source_unsettled"):
        await world.owner.finish_source(world.task.id)
    assert world.task.id in world.owner.unsettled
    with pytest.raises(ContentError, match="source_unsettled"):
        await world.owner.finish_source(world.task.id)
    assert source.aclose.await_count == 1
    with pytest.raises(ContentError, match="source_unsettled"):
        await world.owner.final_receipt(world.task)


async def test_finished_source_does_not_invalidate_the_owned_model_url():
    world = setup_job()
    source = SimpleNamespace(
        closed=True, settled=True, unsettled=False, aclose=AsyncMock()
    )
    inputs = SimpleNamespace(
        _closed=False,
        _unsettled=False,
        record=SimpleNamespace(
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)
        ),
    )
    world.owner.sources[world.task.id] = source
    world.owner.deliveries[world.task.id] = (
        inputs,
        "https://target.test/fixed-version",
    )
    world.owner.probes[world.task.id] = probe()
    await world.owner.finish_source(world.task.id)
    assert world.owner.resolve_url(world.task) == "https://target.test/fixed-version"
    copy = world.owner.video_probe(world.task.id)
    copy["video"]["width"] = 1
    assert world.owner.video_probe(world.task.id)["video"]["width"] == 128


async def test_unknown_target_close_reaches_source_without_releasing_its_permit():
    world = setup_job()
    failure = storage_access.mark_unsettled(RuntimeError("unknown target close"))
    events = []

    async def target_close():
        events.append("target")
        raise failure

    async def source_close(failure=None):
        events.append("source")
        assert failure is not None and failure.storage_operation_unsettled

    with pytest.raises(RuntimeError, match="unknown target") as caught:
        await world.owner._close_pair(
            SimpleNamespace(close=target_close), SimpleNamespace(aclose=source_close)
        )
    assert caught.value is failure
    assert events == ["target", "source"]


async def test_private_receipt_rechecks_exact_current_binding_in_same_top_transaction(
    monkeypatch,
):
    world = setup_job()
    private = evidence(world)
    check = AsyncMock(return_value=private)
    monkeypatch.setattr(world.owner, "_final_check", check)
    receipt = await world.owner.final_receipt(world.task)
    validated = await video.validate_receipt(
        world.db, world.task.id, world.task.project_id, receipt
    )
    assert validated == private
    assert validated["original"]["bucket"] == "saved-bucket"
    assert validated["original"]["facts"] == private["original"]["facts"]
    assert validated["models"][0]["declaration"] == private["models"][0]["declaration"]
    assert "credentials" not in authority.encoded(validated)
    validated["original"]["facts"]["relation"]["file_path"] = "changed"
    assert private["original"]["facts"]["relation"]["file_path"] == "saved/clip.mp4"
    assert "bucket" not in authority.encoded(video.public_provenance(private))
    assert check.await_count == 2
    for forged in (
        object(),
        replace(receipt, issuer=object()),
        replace(receipt, task_id=uuid.uuid4()),
        replace(receipt, project_id=uuid.uuid4()),
        replace(receipt, transaction=object()),
        replace(receipt, session=object()),
    ):
        with pytest.raises(ContentError, match="receipt_invalid"):
            await video.validate_receipt(
                world.db, world.task.id, world.task.project_id, forged
            )
    world.db.sync_session.in_nested_transaction = lambda: True
    with pytest.raises(ContentError, match="receipt_invalid"):
        await video.validate_receipt(
            world.db, world.task.id, world.task.project_id, receipt
        )


@pytest.mark.parametrize("drift", ["probe", "locator", "facts", "declaration"])
async def test_receipt_rejects_payload_or_source_drift_even_before_second_write(
    monkeypatch,
    drift,
):
    world = setup_job()
    private = evidence(world)
    check = AsyncMock(return_value=private)
    monkeypatch.setattr(world.owner, "_final_check", check)
    receipt = await world.owner.final_receipt(world.task)
    if drift == "probe":
        private["probe"]["video"]["width"] = 256
    elif drift == "locator":
        private["original"]["bucket"] = "current-bucket"
    elif drift == "facts":
        private["original"]["facts"]["relation"]["item_id"] = str(uuid.uuid4())
    else:
        private["models"][0]["declaration"]["model"]["params"] = {"changed": True}
    with pytest.raises(ContentError, match="receipt_invalid"):
        await video.validate_receipt(
            world.db, world.task.id, world.task.project_id, receipt
        )


async def test_final_rejects_unfinished_source_before_sql_or_new_sessions(monkeypatch):
    world = setup_job()
    world.owner.sources[world.task.id] = SimpleNamespace(
        closed=False, settled=False, unsettled=False
    )
    guard = AsyncMock(side_effect=AssertionError("metadata guard must not run"))
    monkeypatch.setattr(world.owner, "_task", guard)
    with pytest.raises(ContentError, match="source_unsettled"):
        await world.owner.final_receipt(world.task)
    guard.assert_not_awaited()


async def test_write_recheck_uses_callers_session_and_original_credentials(monkeypatch):
    world = setup_job()
    reauthorize = AsyncMock()
    monkeypatch.setattr(authority, "reauthorize", reauthorize)
    await world.owner.recheck_credentials()
    reauthorize.assert_awaited_once_with(
        world.db, world.proof["credentials"], world.actor_id, None
    )


async def test_retry_requires_first_model_evidence_before_recapturing_credentials(
    monkeypatch,
):
    world = setup_job()
    capture = AsyncMock(side_effect=AssertionError("must not recapture"))
    monkeypatch.setattr(authority, "capture_credentials", capture)
    with pytest.raises(ContentError, match="requires_new_request"):
        await video.capture_authority(
            world.db,
            SimpleNamespace(data_type="video"),
            None,
            None,
            [],
            None,
            scope="bulk",
            batch_id=None,
            predict_mode="append",
            job_kind="prediction_retry",
        )
    capture.assert_not_awaited()


async def test_video_job_does_not_grant_native_mask_qualification():
    world = setup_job()
    writes = [{"result": [{"type": "video_track_bbox", "keyframes": []}]}]
    assert await world.owner.qualify_native_results(world.task, None, writes) == [None]
    with pytest.raises(ContentError, match="native_output_unsupported"):
        await world.owner.qualify_native_results(
            world.task, None, [{"result": [], "token_meta": {"mask_objects": {}}}]
        )


def live_inputs(world):
    receipt = {
        "relative": "video/input.mp4",
        "sha256": world.proof["tasks"][0]["original"]["sha256"],
        "size_bytes": 6,
        "version_id": "immutable-version",
        "probe": probe(),
    }
    inputs = SimpleNamespace(
        _closed=False,
        _unsettled=False,
        video_receipt=receipt,
        check_live=AsyncMock(),
        record=SimpleNamespace(
            id=uuid.uuid4(),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
    )
    world.owner.deliveries[world.task.id] = (
        inputs,
        "https://target.test/fixed-version",
    )
    world.owner.probes[world.task.id] = probe()
    world.owner.input_receipts[world.task.id] = deepcopy(receipt)
    return inputs


def actual_transport(identifier=None):
    backend = SimpleNamespace(
        id=identifier or uuid.uuid4(),
        url="http://controlled.invalid/",
        auth_method="token",
        auth_token="private-transport-token",
        auth_revision=3,
        extra_params={"max_concurrency": 4, "timeout": 5},
        gpu_resource_id=None,
        vram_budget_mb=None,
        state="connected",
        health_meta={"capabilities": caps()},
    )
    return MLBackendClient(backend)


def wire_config(transport, pool_id):
    return {
        "backend_id": transport.backend_id,
        "pool_id": pool_id,
        "auth_revision": video.transport_authority._transport_revision(transport),
        "wire_digest": video.transport_authority._wire(transport),
        "extra_digest": authority.digest(transport._extra_params),
    }


@pytest.mark.parametrize("transport_kind", ["stub", "actual"])
async def test_actual_transport_setup_has_no_open_owned_sql_session_and_rechecks_after(
    monkeypatch,
    transport_kind,
):
    world = setup_job()
    live_inputs(world)
    active, events = set(), []

    class Session:
        def __init__(self):
            active.add(self)
            events.append("sql-open")

        async def close(self):
            active.remove(self)
            events.append("sql-close")

    actual_id = uuid.uuid4()

    async def setup():
        assert not active
        events.append("setup")
        return caps()

    async def fresh(task_id):
        assert not active and task_id == world.task.id
        events.append("initial-guard")

    async def task_guard(db, task_id):
        assert db in active and task_id == world.task.id
        events.append("task-guard")

    monkeypatch.setattr(world.owner, "_fresh", fresh)
    monkeypatch.setattr(world.owner, "_task", task_guard)
    monkeypatch.setattr(originals, "AsyncSession", lambda **_: Session())
    monkeypatch.setattr(storage_access, "engine", lambda _: object())
    monkeypatch.setattr(
        video,
        "_backend",
        AsyncMock(
            return_value=SimpleNamespace(
                id=actual_id, state="connected", health_meta={"capabilities": caps()}
            )
        ),
    )
    pool = AsyncMock()
    monkeypatch.setattr(video, "_pool", pool)
    if transport_kind == "actual":
        transport = actual_transport(actual_id)
        transport.setup = setup
        assert transport.backend_id == str(actual_id)
    else:
        actual = actual_transport(actual_id)
        transport = SimpleNamespace(**vars(actual), setup=setup)
    configuration = wire_config(transport, world.proof["models"][0]["pool_id"])
    monkeypatch.setattr(
        video.transport_authority, "_backend", AsyncMock(return_value=configuration)
    )
    await world.owner.before_dispatch(world.task, transport, stage_id=7)
    assert events == [
        "initial-guard",
        "sql-open",
        "task-guard",
        "sql-close",
        "setup",
        "sql-open",
        "task-guard",
        "sql-close",
    ]
    assert world.owner.executions == {(world.task.id, 7): str(actual_id)}
    assert pool.await_count == 1
    assert pool.call_args.args[1:] == (
        world.task.project_id,
        actual_id,
        world.proof["models"][0]["pool_id"],
    )
    assert not active


async def test_actual_transport_changed_declaration_never_records_dispatch(monkeypatch):
    world = setup_job()
    live_inputs(world)
    monkeypatch.setattr(world.owner, "_fresh", AsyncMock())
    monkeypatch.setattr(world.owner, "_task", AsyncMock())

    @asynccontextmanager
    async def session(db):
        yield object()

    monkeypatch.setattr(originals, "_session", session)
    changed = caps()
    changed["models"][0]["params"]["properties"]["persist"]["default"] = False
    transport = actual_transport()
    transport.setup = AsyncMock(return_value=changed)
    configuration = wire_config(transport, world.proof["models"][0]["pool_id"])
    monkeypatch.setattr(
        video.transport_authority, "_backend", AsyncMock(return_value=configuration)
    )
    with pytest.raises(ContentError, match="model_changed"):
        await world.owner.before_dispatch(world.task, transport, stage_id=7)
    assert not world.owner.executions
    with pytest.raises(ContentError, match="model_changed"):
        await world.owner.before_dispatch(world.task, transport, stage_id=True)
    assert transport.setup.await_count == 1


@pytest.mark.parametrize(
    "drift", ["delivery", "cancel", "proof", "status", "snapshot", "actor", "project"]
)
async def test_job_guard_requires_exact_live_delivery_and_private_proof(
    monkeypatch, drift
):
    world = setup_job()
    project = SimpleNamespace(id=world.task.project_id, data_type="video")
    actor = SimpleNamespace(id=world.actor_id)
    job = SimpleNamespace(
        id=world.job_id,
        kind="batch_predict",
        user_id=world.actor_id,
        project_id=project.id,
        status="running",
        payload={video.PRIVATE_KEY: deepcopy(world.proof)},
        execution_snapshot_id=uuid.UUID(world.proof["snapshot_id"]),
        celery_task_id=str(world.job_id),
    )
    world.db.get, world.db.scalar = (
        AsyncMock(return_value=project),
        AsyncMock(return_value=job),
    )
    monkeypatch.setattr(video, "lock_actor_project_authority", AsyncMock())
    monkeypatch.setattr(video, "lock_actor_scope", AsyncMock())
    monkeypatch.setattr(authority, "reauthorize", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        video,
        "resolve_project_access",
        AsyncMock(return_value=SimpleNamespace(is_manager=True)),
    )
    assert await world.owner.authorize() == (project, actor)
    if drift == "delivery":
        job.celery_task_id = str(uuid.uuid4())
    elif drift == "cancel":
        job.payload["cancel_requested"] = True
    elif drift == "proof":
        job.payload[video.PRIVATE_KEY]["tasks"][0]["version"] += 1
    elif drift == "status":
        job.status = "cancelled"
    elif drift == "snapshot":
        job.execution_snapshot_id = uuid.uuid4()
    elif drift == "actor":
        job.user_id = uuid.uuid4()
    else:
        project.data_type = "image"
    with pytest.raises(ContentError, match="authority_revoked"):
        await world.owner.authorize()


def capture_world(monkeypatch, world):
    project = SimpleNamespace(id=world.task.project_id, data_type="video")
    actor = SimpleNamespace(id=world.actor_id)
    snapshot = SimpleNamespace(
        id=uuid.UUID(world.proof["snapshot_id"]), digest=world.proof["snapshot_digest"]
    )
    monkeypatch.setattr(
        authority,
        "capture_credentials",
        AsyncMock(return_value=deepcopy(world.proof["credentials"])),
    )
    monkeypatch.setattr(video, "lock_actor_project_authority", AsyncMock())
    monkeypatch.setattr(video, "lock_actor_scope", AsyncMock())
    monkeypatch.setattr(
        video,
        "resolve_project_access",
        AsyncMock(
            return_value=SimpleNamespace(
                is_manager=True, has=lambda permission: permission == "annotation.write"
            )
        ),
    )
    monkeypatch.setattr(
        video,
        "load_snapshot",
        AsyncMock(
            return_value=(
                {
                    "execution_scope": "bulk",
                    "execution_unit": None,
                    "stages": [world.stage],
                },
                snapshot.digest,
            )
        ),
    )
    monkeypatch.setattr(
        video,
        "_capture_models",
        AsyncMock(return_value=deepcopy(world.proof["models"])),
    )
    monkeypatch.setattr(authority, "locked_task", AsyncMock(return_value=world.task))
    monkeypatch.setattr(video, "validate_task_scope", AsyncMock())
    monkeypatch.setattr(
        originals,
        "managed_binding",
        AsyncMock(return_value=deepcopy(world.proof["tasks"][0]["original"])),
    )
    return project, actor, snapshot


async def test_retry_recaptures_current_task_version_without_replacing_original_or_models(
    monkeypatch,
):
    world = setup_job()
    project, actor, snapshot = capture_world(monkeypatch, world)
    world.task.version += 1
    proof = await video.capture_authority(
        world.db,
        project,
        actor,
        object(),
        [world.task],
        snapshot,
        scope="bulk",
        batch_id=None,
        predict_mode="append",
        job_kind="prediction_retry",
        expected_models=world.proof["models"],
    )
    assert proof["tasks"][0]["version"] == world.task.version
    assert proof["tasks"][0]["original"] == world.proof["tasks"][0]["original"]
    assert proof["models"] == world.proof["models"]
    assert proof["job_kind"] == "prediction_retry"
    changed = deepcopy(world.proof["models"])
    changed[0]["config_digest"] = "f" * 64
    with pytest.raises(ContentError, match="model_changed"):
        await video.capture_authority(
            world.db,
            project,
            actor,
            object(),
            [world.task],
            snapshot,
            scope="bulk",
            batch_id=None,
            predict_mode="append",
            job_kind="prediction_retry",
            expected_models=changed,
        )


async def test_capture_stops_before_hydrating_all_task_source_facts_after_budget(
    monkeypatch,
):
    world = setup_job()
    project, actor, snapshot = capture_world(monkeypatch, world)
    tasks = [
        world.task,
        *[
            SimpleNamespace(**{**vars(world.task), "id": uuid.uuid4()})
            for _ in range(2)
        ],
    ]
    by_id = {task.id: task for task in tasks}
    locked = AsyncMock(side_effect=lambda db, identifier: by_id[identifier])
    monkeypatch.setattr(authority, "locked_task", locked)
    original = source()

    async def binding(db, task, *, lock):
        return originals.source_receipt(
            replace(
                original,
                relation=replace(
                    original.relation,
                    task_id=task.id,
                    project_id=task.project_id,
                    direct_item_id=task.dataset_item_id,
                    item_id=task.dataset_item_id,
                ),
                item_id=task.dataset_item_id,
                ownership_facts=replace(
                    original.ownership_facts, item_id=task.dataset_item_id
                ),
                item_facts=replace(original.item_facts, id=task.dataset_item_id),
            )
        )

    captured = AsyncMock(side_effect=binding)
    monkeypatch.setattr(originals, "managed_binding", captured)
    size = (
        len(authority.encoded(world.proof).encode())
        + len(authority.encoded(world.proof["tasks"][0]).encode()) // 2
    )
    monkeypatch.setattr(video, "MAX_PROOF_BYTES", size)
    with pytest.raises(ContentError, match="proof_budget_exceeded"):
        await video.capture_authority(
            world.db,
            project,
            actor,
            object(),
            tasks,
            snapshot,
            scope="bulk",
            batch_id=None,
            predict_mode="append",
        )
    assert locked.await_count == captured.await_count == 2


@pytest.mark.parametrize(
    "drift", ["source", "probe", "receipt", "missing_dispatch", "other_task_dispatch"]
)
async def test_final_sql_metadata_fences_closed_source_owned_copy_and_exact_task_dispatch(
    monkeypatch, drift
):
    world = setup_job()
    inputs = live_inputs(world)
    source = SimpleNamespace(closed=True, settled=True, unsettled=False)
    world.owner.sources[world.task.id] = source
    world.owner.finished.add(world.task.id)
    actual_id = uuid.uuid4()
    world.owner.executions[(world.task.id, 7)] = str(actual_id)
    transport = actual_transport(actual_id)
    configuration = wire_config(transport, world.proof["models"][0]["pool_id"])
    copied = video._copied_transport(transport, configuration["auth_revision"])
    world.owner.selected_transports[(world.task.id, 7)] = (
        transport,
        deepcopy(configuration),
        copied,
    )
    monkeypatch.setattr(
        video.transport_authority, "_backend", AsyncMock(return_value=configuration)
    )
    monkeypatch.setattr(world.owner, "_task", AsyncMock(return_value=world.task))
    metadata = AsyncMock()
    monkeypatch.setattr(video.prediction_inputs, "check_metadata", metadata)
    monkeypatch.setattr(
        video,
        "_backend",
        AsyncMock(
            return_value=SimpleNamespace(
                id=actual_id, state="connected", health_meta={"capabilities": caps()}
            )
        ),
    )
    monkeypatch.setattr(video, "_pool", AsyncMock())
    monkeypatch.setattr(
        originals, "_session", lambda _: pytest.fail("final must not open a session")
    )
    evidence = await world.owner._final_check(world.task.id)
    assert evidence["input_version_id"] == "immutable-version"
    assert evidence["original"] == world.proof["tasks"][0]["original"]
    assert evidence["file_path"] == world.task.file_path
    assert (
        evidence["models"][0]["declaration"] == world.proof["models"][0]["declaration"]
    )
    assert "credentials" not in authority.encoded(evidence)
    assert "bucket" not in authority.encoded(video.public_provenance(evidence))
    metadata.assert_awaited_once_with(world.db, inputs)
    if drift == "source":
        source.unsettled = True
    elif drift == "probe":
        world.owner.probes[world.task.id]["video"]["width"] += 1
    elif drift == "receipt":
        inputs.video_receipt["version_id"] = "replacement-version"
    else:
        world.owner.executions.clear()
        if drift == "other_task_dispatch":
            world.owner.executions[(uuid.uuid4(), 7)] = str(actual_id)
    with pytest.raises(ContentError):
        await world.owner._final_check(world.task.id)


def routed_world(monkeypatch):
    world = setup_job()
    live_inputs(world)
    transport = actual_transport()
    live_backend = SimpleNamespace(**deepcopy(vars(transport._backend)))
    events = []

    @asynccontextmanager
    async def session(db):
        events.append("sql-open")
        try:
            yield object()
        finally:
            events.append("sql-close")

    async def configuration(db, identifier):
        assert identifier == live_backend.id
        return wire_config(
            MLBackendClient(live_backend), world.proof["models"][0]["pool_id"]
        )

    async def setup():
        assert not events or events[-1] == "sql-close"
        events.append("setup")
        return caps()

    monkeypatch.setattr(world.owner, "_fresh", AsyncMock())
    monkeypatch.setattr(world.owner, "_task", AsyncMock(return_value=world.task))
    monkeypatch.setattr(originals, "_session", session)
    monkeypatch.setattr(video.transport_authority, "_backend", configuration)
    monkeypatch.setattr(video, "_backend", AsyncMock(return_value=live_backend))
    monkeypatch.setattr(video, "_pool", AsyncMock())
    monkeypatch.setattr(video.prediction_inputs, "check_metadata", AsyncMock())
    transport.setup = AsyncMock(side_effect=setup)
    world.owner.sources[world.task.id] = SimpleNamespace(
        closed=True, settled=True, unsettled=False
    )
    world.owner.finished.add(world.task.id)
    return SimpleNamespace(
        world=world, transport=transport, backend=live_backend, events=events
    )


def change_backend(backend, field):
    if field == "url":
        backend.url = "http://other-controlled.invalid"
    elif field == "auth_token":
        backend.auth_token = "rotated-secret"
    elif field == "auth_method":
        backend.auth_method = "none"
    elif field == "auth_revision":
        backend.auth_revision += 1
    elif field == "extra":
        backend.extra_params = {
            **backend.extra_params,
            "extra_context": {"changed": True},
        }
    elif field == "concurrency":
        backend.extra_params = {**backend.extra_params, "max_concurrency": 8}
    else:
        backend.gpu_resource_id, backend.vram_budget_mb = "owned/gpu0", 1024


@pytest.mark.parametrize(
    "field",
    [
        "url",
        "auth_token",
        "auth_method",
        "auth_revision",
        "extra",
        "concurrency",
        "gpu",
    ],
)
@pytest.mark.parametrize("when", ["before_setup", "during_setup", "before_final"])
async def test_actual_transport_config_is_fixed_before_setup_after_setup_and_final(
    monkeypatch, field, when
):
    current = routed_world(monkeypatch)
    owner, task = current.world.owner, current.world.task
    if when == "before_setup":
        change_backend(current.backend, field)
    elif when == "during_setup":

        async def setup():
            change_backend(current.backend, field)
            return caps()

        current.transport.setup.side_effect = setup
    if when != "before_final":
        with pytest.raises(ContentError, match="backend_changed"):
            await owner.before_dispatch(task, current.transport, stage_id=7)
        assert not owner.executions and not owner.selected_transports
        assert current.transport.setup.await_count == (when == "during_setup")
    else:
        await owner.before_dispatch(task, current.transport, stage_id=7)
        change_backend(current.backend, field)
        monkeypatch.setattr(
            originals,
            "_session",
            lambda _: pytest.fail("final SQL must not open another session"),
        )
        with pytest.raises(ContentError, match="backend_changed"):
            await owner._final_check(task.id)


@pytest.mark.parametrize(
    "field", ["base_url", "auth_token", "gpu_resource_id", "extra"]
)
async def test_copied_transport_mutation_cannot_publish_against_unchanged_database(
    monkeypatch, field
):
    current = routed_world(monkeypatch)
    owner, task = current.world.owner, current.world.task
    await owner.before_dispatch(task, current.transport, stage_id=7)
    if field == "extra":
        current.transport._extra_params["timeout"] = 99
    else:
        setattr(current.transport, field, "mutated-transport-copy")
    with pytest.raises(ContentError, match="backend_changed"):
        await owner._final_check(task.id)


async def test_final_uses_hydrated_dispatch_revision_after_retained_backend_orm_expires(
    monkeypatch,
):
    current = routed_world(monkeypatch)
    owner, task = current.world.owner, current.world.task
    await owner.before_dispatch(task, current.transport, stage_id=7)
    assert (
        current.transport.backend_id
        != current.world.proof["models"][0]["requested_registry_id"]
    )
    # SQL rollback expires the retained registry. No final helper may ask it for
    # auth_revision, extra_params or endpoint again; copied transport fields live.
    vars(current.transport._backend).clear()
    evidence = await owner._final_check(task.id)
    assert evidence["models"][0]["executed_registry_id"] == str(current.backend.id)
    assert "rotated-secret" not in authority.encoded(evidence)


async def test_same_task_stage_cannot_silently_switch_physical_instance_after_dispatch(
    monkeypatch,
):
    current = routed_world(monkeypatch)
    owner, task = current.world.owner, current.world.task
    await owner.before_dispatch(task, current.transport, stage_id=7)
    new_id = uuid.uuid4()
    current.backend.id = new_id
    replacement = actual_transport(new_id)
    replacement.setup = AsyncMock(return_value=caps())
    with pytest.raises(ContentError, match="backend_changed"):
        await owner.before_dispatch(task, replacement, stage_id=7)
    replacement.setup.assert_not_awaited()


@pytest.mark.parametrize("phase", ["fresh", "pre_setup"])
async def test_repeated_cancel_waits_for_real_independent_sql_close(monkeypatch, phase):
    world = setup_job()
    live_inputs(world)
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []

    class Session:
        async def close(self):
            entered.set()
            await release.wait()
            closed.append(True)

    monkeypatch.setattr(originals, "AsyncSession", lambda **_: Session())
    monkeypatch.setattr(storage_access, "engine", lambda _: object())
    monkeypatch.setattr(world.owner, "_task", AsyncMock())
    transport = actual_transport()
    transport.setup = AsyncMock(return_value=caps())
    configuration = wire_config(transport, world.proof["models"][0]["pool_id"])
    monkeypatch.setattr(
        video.transport_authority, "_backend", AsyncMock(return_value=configuration)
    )
    if phase == "pre_setup":
        monkeypatch.setattr(world.owner, "_fresh", AsyncMock())
        work = world.owner.before_dispatch(world.task, transport, stage_id=7)
    else:
        work = world.owner._fresh(world.task.id)
    pending = asyncio.create_task(work)
    await entered.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done() and not closed
    transport.setup.assert_not_awaited()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert closed == [True]
    assert not world.owner.executions and not world.owner.selected_transports
