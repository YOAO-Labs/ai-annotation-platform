"""Prediction source identity and receipts; DB cases use owned random schemas."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from PIL import Image

from app.db.enums import PlatformRole
from app.db.models.prediction import PredictionMeta
from app.services import comment_attachment_authority as credentials
from app.services import content_snapshot as images
from app.services import original_sources
from app.services import prediction_originals as originals
from app.services import prediction_references as refs
from app.services import storage_access, task_originals
from app.services.content_snapshot import ContentError
from app.services.prediction import PredictionService
from app.services.reference_bindings import task_binding
from app.services.storage import storage_service
from tests.test_original_sources import add_link, dataset_rows
from tests.test_task_originals import (
    original_db as original_db,
    original_client as original_client,
    verified,
)


def source():
    task_id, project_id, item_id, content_id, location_id = (
        uuid.uuid4() for _ in range(5)
    )
    relation = original_sources.TaskRelation(
        task_id,
        project_id,
        None,
        "saved/image.png",
        "image",
        None,
        item_id,
        None,
        None,
        item_id,
        (),
    )
    return original_sources.OriginalSource(
        "dataset_asset",
        content_id,
        hashlib.sha256(b"source").hexdigest(),
        6,
        "image",
        item_id,
        relation,
        "saved-bucket",
        "saved/image.png",
        "http://saved.test:9000",
        location_id,
        3,
        7,
        original_sources.AssetFacts(
            content_id, item_id, "image", hashlib.sha256(b"source").hexdigest(), 6
        ),
        original_sources.ItemFacts(
            item_id, uuid.uuid4(), "saved/image.png", "image", 24, 16
        ),
        None,
        None,
    )


def task_for(value):
    return SimpleNamespace(
        id=value.relation.task_id,
        project_id=value.relation.project_id,
        batch_id=None,
        dataset_item_id=value.item_id,
        file_name="image.png",
        file_path=value.object_key,
        file_type="image",
        version=1,
    )


def jwt_value(actor_id):
    return originals.credentials_value(
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


def owner(binding=None, *, schema=2):
    value = source()
    task = task_for(value)
    binding = binding or {
        **task_binding(task),
        "original": originals.source_receipt(value),
    }
    actor_id, job_id, snapshot_id = (uuid.uuid4() for _ in range(3))
    proof = {
        "schema": schema,
        "job_kind": "batch_predict",
        "actor_id": str(actor_id),
        "project_id": str(task.project_id),
        "batch_id": None,
        "execution_scope": "bulk",
        "predict_mode": "overwrite",
        "execution_unit": None,
        "api_key_id": None,
        "key_proof": None,
        "snapshot_id": str(snapshot_id),
        "snapshot_digest": "b" * 64,
        "tasks": [binding],
    }
    if schema == 2:
        proof["credentials"] = jwt_value(actor_id)
    result = refs.ReferencePredictionJob(
        SimpleNamespace(),
        job_id,
        proof,
        project_id=task.project_id,
        actor_id=actor_id,
        task_ids=[uuid.UUID(binding["task_id"])],
        batch_id=None,
        scope="bulk",
        predict_mode="overwrite",
        snapshot_id=snapshot_id,
        snapshot_digest="b" * 64,
        node_id="worker:prediction@test",
    )
    return result, task


@pytest.mark.parametrize(
    "field,value",
    [
        ("bucket", "current-bucket"),
        ("endpoint_url", "http://other.test:9000"),
        ("sha256", "b" * 64),
        ("size_bytes", 7),
        ("maintenance_revision", 8),
        ("location_revision", 4),
        ("content_id", uuid.uuid4()),
    ],
)
def test_source_receipt_freezes_saved_owner_and_all_canonical_facts(field, value):
    before = source()
    initial = originals.source_receipt(before)
    changed = originals.source_receipt(replace(before, **{field: value}))
    assert initial != changed
    assert initial["facts_digest"] != changed["facts_digest"]
    assert initial["facts"]["relation"]["task_id"] == str(before.relation.task_id)
    assert "version" not in initial["facts"]["relation"]


@pytest.mark.parametrize("size", [0, images.MAX_BYTES + 1, True])
def test_source_budget_precedes_any_physical_read(size):
    with pytest.raises(ContentError, match="prediction_original_unsupported"):
        originals.source_receipt(replace(source(), size_bytes=size))


@pytest.mark.parametrize("change", ["unqualified", "both"])
def test_schema_one_never_adopts_managed_binding(change):
    value = source()
    binding = task_binding(task_for(value))
    if change == "both":
        binding.update(reference={}, original={})
    with pytest.raises(ContentError, match="prediction_scope_changed"):
        owner(binding, schema=1)


def test_reference_only_schema_one_survives_and_failure_binding_is_detached():
    value = source()
    binding = {**task_binding(task_for(value)), "reference": {"sha256": "a" * 64}}
    job, _ = owner(binding, schema=1)
    identifier = uuid.UUID(binding["task_id"])
    assert not job.is_managed(identifier)
    copied = job.failure_binding(identifier)
    copied["reference"]["sha256"] = "changed"
    assert job.failure_binding(identifier)["reference"]["sha256"] == "a" * 64


async def test_managed_native_results_use_existing_materialization_without_reference_proof():
    job, task = owner()
    assert job.is_managed(task.id)
    assert await job.qualify_native_results(task, object(), [object(), object()]) == [
        None,
        None,
    ]


def test_proof_budget_and_exact_original_credentials():
    actor, key_id = uuid.uuid4(), uuid.uuid4()
    expiration = datetime.now(timezone.utc) + timedelta(minutes=2)
    proof = credentials.Authority(
        actor,
        key_id,
        ("private-verifier", ("projects:read",), expiration),
        None,
        frozenset(PlatformRole),
    )
    value = originals.credentials_value(proof)
    assert "private-verifier" not in originals.encoded(value)
    assert value["key_expires_at"] == expiration.isoformat()
    originals.validate_credentials(value, actor, key_id)
    changed = deepcopy(value)
    changed["jwt"] = jwt_value(actor)["jwt"]
    with pytest.raises(ContentError, match="prediction_credentials_invalid"):
        originals.validate_credentials(changed, actor, key_id)
    with pytest.raises(ContentError, match="prediction_proof_budget_exceeded"):
        originals.bounded_proof({"large": "x" * originals.MAX_PROOF_BYTES})


@pytest.mark.parametrize("kind", ["jwt", "key"])
def test_original_request_expiry_is_not_extended_by_a_running_job(kind):
    actor = uuid.uuid4()
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    value = jwt_value(actor)
    if kind == "jwt":
        value["jwt"]["expires_at"] = expired.isoformat()
    else:
        value = originals.credentials_value(
            credentials.Authority(
                actor,
                uuid.uuid4(),
                ("verifier", (), expired),
                None,
            )
        )
    with pytest.raises(ContentError):
        originals.check_clock(value)


@pytest.mark.parametrize("change", ["verifier", "scopes", "expires"])
async def test_api_key_facts_are_exact_without_adding_managed_read_scope(
    monkeypatch, change
):
    actor_id, key_id = uuid.uuid4(), uuid.uuid4()
    actor = SimpleNamespace(id=actor_id)
    expiration = datetime.now(timezone.utc) + timedelta(minutes=2)
    key = ("verifier", ("projects:read",), expiration)
    proof = originals.credentials_value(
        credentials.Authority(actor_id, key_id, key, None)
    )
    monkeypatch.setattr(credentials, "_actor", AsyncMock(return_value=actor))
    monkeypatch.setattr(credentials, "_jwt_current", AsyncMock())
    current = {
        "verifier": ("rotated", key[1], key[2]),
        "scopes": (key[0], ("*",), key[2]),
        "expires": (key[0], key[1], expiration + timedelta(seconds=1)),
    }[change]
    monkeypatch.setattr(credentials, "_key", AsyncMock(return_value=key))
    assert await originals.reauthorize(object(), proof, actor_id, key_id) is actor
    credentials._key.return_value = current
    with pytest.raises(ContentError, match="api_key_authority_revoked"):
        await originals.reauthorize(object(), proof, actor_id, key_id)


class SyncSession:
    def __init__(self):
        self.transaction = object()
        self.nested = False

    def in_nested_transaction(self):
        return self.nested

    def get_transaction(self):
        return self.transaction


async def test_managed_receipt_requires_same_transaction_and_current_source(
    monkeypatch,
):
    value = source()
    task = task_for(value)
    binding = {**task_binding(task), "original": originals.source_receipt(value)}
    db = SimpleNamespace(sync_session=SyncSession(), get=AsyncMock(return_value=task))
    monkeypatch.setattr(
        originals, "managed_binding", AsyncMock(return_value=binding["original"])
    )
    receipt = originals.issue_receipt(db, task.id, binding, {**binding, "schema": 1})
    assert (await originals.validate_receipt(db, task.id, task.project_id, receipt))[
        "original"
    ] == originals.public_provenance(binding)["original"]
    originals.managed_binding.return_value = originals.source_receipt(
        replace(value, bucket="other")
    )
    with pytest.raises(ContentError, match="content_binding_changed"):
        await originals.validate_receipt(db, task.id, task.project_id, receipt)
    db.sync_session.transaction = object()
    with pytest.raises(ContentError, match="prediction_original_receipt_invalid"):
        await originals.validate_receipt(db, task.id, task.project_id, receipt)
    with pytest.raises(ContentError, match="prediction_original_receipt_invalid"):
        await originals.validate_receipt(db, task.id, task.project_id, object())


async def test_prediction_original_metadata_cannot_be_supplied_by_backend(monkeypatch):
    value = source()
    task = task_for(value)
    task.total_predictions = 0
    binding = {**task_binding(task), "original": originals.source_receipt(value)}
    added = []
    db = SimpleNamespace(
        sync_session=SyncSession(),
        scalar=AsyncMock(return_value=task),
        get=AsyncMock(return_value=task),
        flush=AsyncMock(),
        execute=AsyncMock(),
        add=added.append,
    )
    monkeypatch.setattr(
        originals, "managed_binding", AsyncMock(return_value=binding["original"])
    )
    private_evidence = {
        **binding,
        "schema": 1,
        "credentials": {"jwt": "private-request"},
        "endpoint_url": "http://private.invalid",
        "bucket": "private-bucket",
    }
    receipt = originals.issue_receipt(db, task.id, binding, private_evidence)
    trusted = originals.public_provenance(private_evidence)
    assert originals.encoded(binding) == receipt.binding
    from app.services import prediction as predictions

    monkeypatch.setattr(predictions, "prepare_mask_payload_for_write", AsyncMock())
    token_meta = {"mask_objects": {"native": "still-owned-by-existing-mask-service"}}
    await PredictionService(db).create_from_ml_result(
        task.id,
        task.project_id,
        None,
        [],
        original_receipt=receipt,
        token_meta=token_meta,
        pipeline_extra={"original_input": {"forged": True}},
    )
    meta = next(row for row in added if isinstance(row, PredictionMeta))
    assert meta.extra["original_input"] == trusted

    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    assert not keys(meta.extra["original_input"]) & {
        "endpoint_url",
        "endpoint",
        "bucket",
        "object_key",
        "key",
        "facts",
        "credentials",
        "jwt",
        "file_path",
        "file_name",
        "storage_identity",
    }
    assert trusted["original"]["facts_digest"] == binding["original"]["facts_digest"]
    assert (
        predictions.prepare_mask_payload_for_write.call_args.kwargs[
            "portable_mask_meta"
        ]
        is token_meta
    )
    with pytest.raises(ContentError, match="prediction_original_receipt_invalid"):
        await PredictionService(db).create_from_ml_result(
            uuid.uuid4(),
            uuid.uuid4(),
            None,
            [],
            original_receipt=object(),
            reference_receipt=object(),
        )


async def test_unknown_input_close_preserves_source_permit(monkeypatch):
    job, task = owner()
    error = storage_access.mark_unsettled(OSError("unknown native close"))
    inputs = SimpleNamespace(close=AsyncMock(side_effect=error))
    source_input = originals.SourceInput(
        SimpleNamespace(
            close=lambda: pytest.fail("unknown file must not be reclassified")
        ),
        SimpleNamespace(__aexit__=AsyncMock()),
    )
    job.deliveries[task.id] = (inputs, "url")
    job.sources[task.id] = source_input
    with pytest.raises(OSError) as caught:
        await job.close_inputs(task.id)
    assert caught.value.storage_operation_unsettled
    source_input.permit.__aexit__.assert_not_awaited()


async def test_failed_source_close_keeps_permit_even_if_snapshot_marked_closed():
    def failed_close():
        raise OSError("native file close unknown")

    permit = SimpleNamespace(__aexit__=AsyncMock())
    owned = originals.SourceInput(
        SimpleNamespace(close=failed_close, _closed=True), permit
    )
    with pytest.raises(OSError) as caught:
        await owned.close()
    assert caught.value.storage_operation_unsettled
    permit.__aexit__.assert_not_awaited()


@pytest.mark.parametrize("unknown", [False, True])
async def test_interrupted_close_settles_only_after_known_native_close(unknown):
    started, release = threading.Event(), threading.Event()
    closed = []

    def close():
        started.set()
        assert release.wait(3)
        if unknown:
            raise OSError("native close unconfirmed")
        closed.append(True)

    permit = SimpleNamespace(__aexit__=AsyncMock())
    owned = originals.SourceInput(SimpleNamespace(close=close), permit)
    job = asyncio.create_task(owned.close())
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        job.cancel()
        await asyncio.sleep(0)
        job.cancel()
        await asyncio.sleep(0)
        assert not job.done()
        permit.__aexit__.assert_not_awaited()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await job
        assert (
            bool(getattr(caught.value, "storage_operation_unsettled", False)) is unknown
        )
        if unknown:
            permit.__aexit__.assert_not_awaited()
            assert not closed
        else:
            permit.__aexit__.assert_awaited_once()
            assert closed == [True]
    finally:
        release.set()
        if not job.done():
            try:
                await originals.drain(job)
            except BaseException:
                pass


async def test_full_stage_adoption_keeps_source_permit_through_actual_snapshot_close(
    monkeypatch,
):
    monkeypatch.setattr(original_sources, "read_options", AsyncMock(return_value={}))
    content = io.BytesIO()
    with Image.new("RGB", (24, 16), (15, 25, 35)) as image:
        image.save(content, "PNG")
    raw = content.getvalue()
    original = source()
    original = replace(
        original,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        ownership_facts=replace(
            original.ownership_facts,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
        ),
    )
    active_sessions = []

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            active_sessions.append(self)
            return self

        async def __aexit__(self, *args):
            active_sessions.remove(self)

    permit = SimpleNamespace(__aexit__=AsyncMock())
    guard = AsyncMock()
    monkeypatch.setattr(originals, "AsyncSession", Session)
    monkeypatch.setattr(storage_access, "engine", lambda db: object())
    monkeypatch.setattr(storage_access, "acquire", AsyncMock(return_value=permit))
    monkeypatch.setattr(originals, "resolve", AsyncMock(return_value=original))

    def capture(spec, maximum, seconds, deadline, stop, release, **kwargs):
        assert not active_sessions
        assert maximum == images.MAX_BYTES
        assert (
            spec.bucket == "saved-bucket"
            and spec.sha256 == hashlib.sha256(raw).hexdigest()
        )
        return originals.reader.BinaryStage(
            io.BytesIO(raw),
            spec.sha256,
            len(raw),
            {"size": len(raw)},
            _release=release,
            _retain_on_close_error=True,
        )

    monkeypatch.setattr(originals.reader, "_capture", capture)
    owned = await originals.open_snapshot(
        object(),
        original.relation.task_id,
        originals.source_receipt(original),
        guard=guard,
        purpose="prediction_input_" + uuid.uuid4().hex,
        node_id="worker:prediction@test",
    )
    try:
        assert not owned.snapshot.file.closed
        assert owned.snapshot.sha256 == hashlib.sha256(raw).hexdigest()
        assert (owned.snapshot.width, owned.snapshot.height) == (24, 16)
        permit.__aexit__.assert_not_awaited()
        assert guard.await_count == 3
    finally:
        await owned.close()
    assert owned.snapshot.file.closed
    permit.__aexit__.assert_awaited_once()


class Interrupted(BaseException):
    pass


async def test_adopt_drains_repeated_interruption_and_closes_late_snapshot(monkeypatch):
    started, release = threading.Event(), threading.Event()
    result = SimpleNamespace(close=lambda: closed.append("closed"))
    closed = []

    def gated(*args):
        started.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(images, "_adopt_stage", gated)
    job = asyncio.create_task(images.adopt_stage(object()))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        job.cancel()
        await asyncio.sleep(0)
        job.cancel()
        await asyncio.sleep(0)
        assert not job.done() and not closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert closed == ["closed"]
    finally:
        release.set()
        if not job.done():
            await originals.drain(job)


async def test_adopt_propagates_native_baseexception_with_closed_stage(monkeypatch):
    stage = SimpleNamespace(close=lambda: closes.append("closed"))
    closes = []

    def interrupted(value, *args):
        value.close()
        raise Interrupted()

    monkeypatch.setattr(images, "_adopt_stage", interrupted)
    with pytest.raises(Interrupted):
        await images.adopt_stage(stage)
    assert closes == ["closed"]


@pytest.mark.parametrize("primary", [False, True])
async def test_managed_binding_uses_actual_saved_location_and_excludes_task_version(
    original_db, monkeypatch, primary
):
    db = original_db
    item, _, replica, task = await dataset_rows(db)
    if primary:
        task.dataset_item_id = None
        await add_link(db, task.id, item.id)
    await db.commit()
    monkeypatch.setattr(storage_service, "datasets_bucket", "wrong-current-datasets")
    monkeypatch.setattr(storage_service, "bucket", "wrong-current-task")
    before = await originals.managed_binding(db, task, lock=True)
    assert before["bucket"] == replica.bucket
    assert before["facts"]["relation"]["item_id"] == str(item.id)
    task.version += 1
    await db.flush()
    assert await originals.managed_binding(db, task, lock=True) == before
    replica.bucket = "changed-original-owner"
    await db.flush()
    assert await originals.managed_binding(db, task, lock=True) != before


async def test_standalone_receipt_uses_task_original_not_current_bucket(
    original_db, original_client, monkeypatch
):
    db = original_db
    task, allocation, created = await verified(db, original_client)
    await task_originals.publish(db, task, allocation, created)
    await db.commit()
    calls = list(original_client.calls)
    monkeypatch.setattr(storage_service, "bucket", "wrong-current-task")
    value = await originals.managed_binding(db, task, lock=True)
    assert value["owner_kind"] == "task_original"
    assert value["content_id"] == str(task.original_id)
    assert value["bucket"] == allocation["bucket"]
    assert value["sha256"] == created.sha256
    assert original_client.calls == calls
