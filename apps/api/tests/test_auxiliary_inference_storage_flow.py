"""Evaluation reports and active-learning PNGs keep their durable output owner."""

from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.hooks import HierarchicalEmitter
from botocore.response import StreamingBody
from fastapi import HTTPException
import pytest

from app.api.v1 import (
    active_learning as round_api,
    sample_evaluations as evaluation_api,
)
from app.config import settings
from app.services import (
    active_learning as rounds,
    active_learning_pool,
    active_learning_storage,
    sample_evaluations as evaluations,
    sample_evaluation_storage,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import StorageService, storage_service
from app.services.training_dataset_bridge import TrainingDatasetError
from app.workers import active_learning as round_worker
from app.workers import sample_evaluations as evaluation_worker
from tests.test_managed_original_creation import creation_db as creation_db


class Objects:
    def __init__(self, endpoint):
        self.meta = SimpleNamespace(
            endpoint_url=endpoint, config=Config(), events=HierarchicalEmitter()
        )
        self.objects, self.metadata, self.calls = {}, {}, []

    def close(self):
        pass

    def head_object(self, *, Bucket, Key):
        self.calls.append(("head", Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        raw = self.objects[Bucket, Key]
        return {
            "ContentLength": len(raw),
            "Metadata": self.metadata[Bucket, Key],
            "ETag": hashlib.sha256(raw).hexdigest(),
        }

    def get_object(self, *, Bucket, Key, IfMatch=None):
        self.calls.append(("get", Bucket, Key))
        raw = self.objects[Bucket, Key]
        if IfMatch is not None:
            assert IfMatch == hashlib.sha256(raw).hexdigest()
        return {
            "Metadata": self.metadata[Bucket, Key],
            "Body": StreamingBody(io.BytesIO(raw), len(raw)),
        }

    def put_object(self, *, Bucket, Key, Body, Metadata, **kwargs):
        self.calls.append(("put", Bucket, Key))
        self.objects[Bucket, Key] = bytes(Body)
        self.metadata[Bucket, Key] = Metadata

    def delete_object(self, *, Bucket, Key):
        self.calls.append(("delete", Bucket, Key))
        self.objects.pop((Bucket, Key), None)

    def list_objects_v2(self, *, Bucket, Prefix, **kwargs):
        return {
            "IsTruncated": False,
            "Contents": [
                {"Key": key}
                for bucket, key in self.objects
                if bucket == Bucket and key.startswith(Prefix)
            ],
        }

    def delete_objects(self, *, Bucket, Delete):
        for value in Delete["Objects"]:
            self.delete_object(Bucket=Bucket, Key=value["Key"])
        return {}


def reference(project_id, identity, *, purpose, owner, endpoint):
    return {
        "schema": 1,
        "purpose": purpose,
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": "retained-inference-bucket",
        "prefix": owner.prefix_for(project_id, identity),
        "storage_identity": managed_storage_identity(
            endpoint, "retained-inference-bucket"
        ),
        "origin": "new",
        "evidence": None,
    }


@pytest.fixture
def world(monkeypatch):
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    client = Objects(endpoint)

    def provider_client(self, **_kwargs):
        self.client = client

    # Exclusive accounting transports use the same in-memory provider, not S3.
    monkeypatch.setattr(StorageService, "__init__", provider_client)
    monkeypatch.setattr(storage_service, "client", client)
    monkeypatch.setattr(storage_service, "training_bucket", "new-current-bucket")
    project_id = uuid.uuid4()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project_id,
        spec_digest="b" * 64,
        spec={"benchmark_digest": "c" * 64, "model_ref": {"sha256": "d" * 64}},
        status="succeeded",
        deletion_requested_at=None,
        generation=1,
        error_code="cancel_requested",
        error_message="Stop requested",
    )
    run.storage_ref = reference(
        project_id,
        run.id,
        purpose="sample-evaluation",
        owner=sample_evaluation_storage,
        endpoint=endpoint,
    )
    attempt = SimpleNamespace(
        id=uuid.uuid4(), run_id=run.id, status="succeeded", report=None, claim_id=None
    )
    run.current_attempt_id = attempt.id
    round = SimpleNamespace(id=uuid.uuid4(), project_id=project_id, generation=1)
    round.storage_ref = reference(
        project_id,
        round.id,
        purpose="active-learning",
        owner=active_learning_storage,
        endpoint=endpoint,
    )
    png = b"independently frozen derived PNG bytes"
    sample = {
        "ordinal": 0,
        "sha256": "e" * 64,
        "derived_sha256": hashlib.sha256(png).hexdigest(),
        "width": 2,
        "height": 2,
        "size_bytes": 123,
        "preprocessing": {"schema": 1},
        "snapshot": {"id": "frozen-source"},
    }
    return SimpleNamespace(
        client=client,
        run=run,
        attempt=attempt,
        round=round,
        png=png,
        sample=sample,
        bucket=run.storage_ref["bucket"],
    )


def published_report(world):
    report = {"status": "succeeded", "schema": 1}
    artifact = evaluations.upload_report(world.run, world.attempt, report)
    world.attempt.report = artifact
    world.client.calls.clear()
    return report, artifact


def factory_for(db):
    @asynccontextmanager
    async def factory():
        yield db

    return factory


def test_evaluation_upload_and_verification_keep_the_saved_bucket(world):
    artifact = evaluations.upload_report(
        world.run, world.attempt, {"status": "succeeded", "schema": 1}
    )
    assert (world.bucket, artifact["key"]) in world.client.objects
    assert [operation for operation, _bucket, _key in world.client.calls] == [
        "put",
        "get",
    ]
    assert all(
        bucket == world.bucket for _operation, bucket, _key in world.client.calls
    )
    assert artifact["key"] == (
        f"{world.run.storage_ref['prefix']}{world.attempt.id}/{artifact['sha256']}.json"
    )


@pytest.mark.parametrize("invalid", ["missing-owner", "foreign-attempt"])
def test_evaluation_upload_rejects_unproven_ownership_before_io(world, invalid):
    if invalid == "missing-owner":
        world.run.storage_ref = None
    else:
        world.attempt.run_id = uuid.uuid4()
    with pytest.raises(TrainingDatasetError):
        evaluations.upload_report(world.run, world.attempt, {"status": "succeeded"})
    assert not world.client.calls


async def test_evaluation_cached_reads_still_verify_saved_bytes(
    world, monkeypatch, storage_db
):
    report, artifact = published_report(world)
    monkeypatch.setattr(evaluations, "sample_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(evaluations, "verify_report", lambda value, *_args: value)
    assert await evaluations.read_report(storage_db, world.run, world.attempt) == report
    world.client.objects["new-current-bucket", artifact["key"]] = b"current trap"
    world.client.objects[world.bucket, artifact["key"]] = b"changed retained bytes"
    with pytest.raises(HTTPException) as error:
        await evaluations.read_report(storage_db, world.run, world.attempt)
    assert error.value.status_code == 409
    assert world.client.calls == [("get", world.bucket, artifact["key"])] * 2


@pytest.mark.parametrize("invalid", ["missing-owner", "foreign-attempt", "wrong-key"])
async def test_evaluation_reads_refuse_before_io(world, monkeypatch, invalid):
    _report, artifact = published_report(world)
    if invalid == "missing-owner":
        world.run.storage_ref = None
    elif invalid == "foreign-attempt":
        world.attempt.run_id = uuid.uuid4()
    else:
        artifact["key"] = artifact["key"].replace(".json", "/extra.json")
    with pytest.raises(HTTPException) as error:
        await evaluations.read_report(None, world.run, world.attempt)
    assert error.value.status_code == 409
    assert not world.client.calls


async def test_late_candidate_cleanup_carries_owner_after_run_is_deleted(
    world, storage_db
):
    _report, artifact = published_report(world)
    db = SimpleNamespace(
        bind=storage_db.bind,
        scalar=AsyncMock(return_value=None),
        get=AsyncMock(return_value=None),
        commit=AsyncMock(),
    )
    await evaluation_worker.cleanup_candidate(
        factory_for(db),
        world.run.id,
        world.attempt.id,
        artifact,
        storage_ref=deepcopy(world.run.storage_ref),
    )
    assert (world.bucket, artifact["key"]) not in world.client.objects


async def test_finish_captures_owner_before_upload_and_row_disappearance(
    world, monkeypatch, storage_db
):
    world.run.status = "running"
    monkeypatch.setattr(
        evaluation_worker,
        "fenced",
        AsyncMock(
            side_effect=[(world.run, world.attempt), evaluation_worker.FencedOut()]
        ),
    )
    monkeypatch.setattr(evaluations, "sample_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        evaluations, "build_report", Mock(return_value={"status": "succeeded"})
    )
    upload = evaluations.upload_report

    def upload_then_remove_row(*args, **kwargs):
        artifact = upload(*args, **kwargs)
        world.run.storage_ref = None
        return artifact

    monkeypatch.setattr(evaluations, "upload_report", upload_then_remove_row)
    db = SimpleNamespace(
        bind=storage_db.bind,
        scalar=AsyncMock(return_value=None),
        get=AsyncMock(return_value=None),
        commit=AsyncMock(),
    )
    with pytest.raises(evaluation_worker.FencedOut):
        await evaluation_worker.finish(factory_for(db), world.attempt.id, uuid.uuid4())
    assert not world.client.objects
    assert all(
        bucket == world.bucket for _operation, bucket, _key in world.client.calls
    )


@pytest.mark.parametrize(
    "invalid", ["missing-owner", "foreign-run", "foreign-key", "changed-owner"]
)
async def test_candidate_cleanup_never_guesses_or_crosses_owner(world, invalid):
    _report, artifact = published_report(world)
    snapshot, run_id = deepcopy(world.run.storage_ref), world.run.id
    live = None
    if invalid == "missing-owner":
        snapshot = None
    elif invalid == "foreign-run":
        run_id = uuid.uuid4()
    elif invalid == "foreign-key":
        artifact["key"] = (
            f"{world.run.storage_ref['prefix']}{uuid.uuid4()}/{artifact['sha256']}.json"
        )
    else:
        live = deepcopy(world.run)
        live.storage_ref = None
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=live),
        get=AsyncMock(return_value=None),
        commit=AsyncMock(),
    )
    await evaluation_worker.cleanup_candidate(
        factory_for(db), run_id, world.attempt.id, artifact, storage_ref=snapshot
    )
    assert not world.client.calls


async def test_candidate_cleanup_preserves_committed_report_after_lost_ack(world):
    _report, artifact = published_report(world)
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=world.run),
        get=AsyncMock(return_value=world.attempt),
        commit=AsyncMock(),
    )
    await evaluation_worker.cleanup_candidate(
        factory_for(db),
        world.run.id,
        world.attempt.id,
        artifact,
        storage_ref=deepcopy(world.run.storage_ref),
    )
    assert not world.client.calls
    db.commit.assert_not_awaited()


async def test_evaluation_cleanup_uses_saved_prefix_outside_business_transaction(
    world, monkeypatch, storage_db
):
    _report, artifact = published_report(world)
    foreign_key = "another-owner/retained.json"
    world.client.objects[world.bucket, foreign_key] = b"unrelated"
    world.run.deletion_requested_at = evaluations.now()
    db = SimpleNamespace(
        bind=storage_db.bind,
        scalar=AsyncMock(return_value=world.run),
        commit=AsyncMock(),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(active_learning_pool, "guard_source", AsyncMock())
    delete_objects = world.client.delete_objects

    def physical_delete(*, Bucket, Delete):
        assert db.commit.await_count == 1
        assert Bucket == world.bucket
        return delete_objects(Bucket=Bucket, Delete=Delete)

    monkeypatch.setattr(world.client, "delete_objects", physical_delete)
    assert await evaluations.purge_requested(db, world.run.id)
    assert (world.bucket, artifact["key"]) not in world.client.objects
    assert world.client.objects[world.bucket, foreign_key] == b"unrelated"
    db.delete.assert_awaited_once_with(world.run)


async def test_unknown_evaluation_delete_cannot_create_misleading_intent(
    world, monkeypatch
):
    world.run.storage_ref = None
    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(evaluation_api, "lock_owner", AsyncMock())
    monkeypatch.setattr(evaluations, "load_run", AsyncMock(return_value=world.run))
    monkeypatch.setattr(active_learning_pool, "guard_source", AsyncMock())
    physical = Mock(side_effect=AssertionError("unknown owner cannot delete"))
    monkeypatch.setattr(storage_service, "delete_prefix", physical)
    with pytest.raises(HTTPException) as error:
        await evaluation_api.delete(
            world.run.id,
            project=SimpleNamespace(id=world.run.project_id),
            user=SimpleNamespace(),
            db=db,
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "training_storage_unpinned"
    assert world.run.deletion_requested_at is None
    db.commit.assert_not_awaited()
    physical.assert_not_called()


async def test_unknown_owner_keeps_existing_evaluation_cleanup_responsibility(
    world, monkeypatch
):
    world.run.storage_ref = None
    world.run.deletion_requested_at = evaluations.now()
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=world.run), commit=AsyncMock(), delete=AsyncMock()
    )
    physical = Mock(side_effect=AssertionError("unknown owner cannot delete"))
    monkeypatch.setattr(storage_service, "delete_prefix", physical)
    with pytest.raises(TrainingDatasetError):
        await evaluations.purge_requested(db, world.run.id)
    db.delete.assert_not_awaited()
    physical.assert_not_called()


@pytest.mark.parametrize("has_request", [False, True])
async def test_unknown_owner_does_not_block_confirmed_evaluation_stop(
    world, monkeypatch, has_request
):
    world.run.status = world.attempt.status = "cancelling"
    world.run.storage_ref = None
    world.attempt.claim_id = uuid.uuid4()
    db = SimpleNamespace(
        get=AsyncMock(
            side_effect=[world.run, world.attempt, world.attempt, world.attempt]
        ),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(evaluations, "authority", AsyncMock())
    monkeypatch.setattr(evaluations, "load_run", AsyncMock(return_value=world.run))
    rows = (
        [
            SimpleNamespace(
                request={"schema": 1}, request_id=uuid.uuid4(), evidence=None, ordinal=0
            )
        ]
        if has_request
        else []
    )
    monkeypatch.setattr(evaluations, "sample_rows", AsyncMock(return_value=rows))
    client = object()
    monkeypatch.setattr(
        evaluation_worker, "private_client", AsyncMock(return_value=client)
    )
    receipt = AsyncMock(return_value={"state": "sealed"})
    monkeypatch.setattr(evaluation_worker, "remote_receipt", receipt)
    monkeypatch.setattr(
        evaluations, "build_report", Mock(return_value={"status": "cancelled"})
    )
    terminal = AsyncMock()
    monkeypatch.setattr(evaluations, "mark_terminal", terminal)
    upload = Mock(side_effect=AssertionError("unknown output cannot upload"))
    monkeypatch.setattr(evaluations, "upload_report", upload)
    assert await evaluation_worker.settle_cancel(factory_for(db), world.run.id)
    assert terminal.await_args.kwargs["report"] is None
    assert world.attempt.claim_id is None
    upload.assert_not_called()
    if has_request:
        receipt.assert_awaited_once_with(client, rows[0], seal=True)
    else:
        receipt.assert_not_awaited()
    assert not world.client.calls


@pytest.fixture
async def storage_db(creation_db):
    return creation_db


def png_context(world, monkeypatch, db):
    context = SimpleNamespace(proof=None, key=None)
    world.round.spec = {"original_contract": 1}
    monkeypatch.setattr(rounds.originals, "delegated", AsyncMock(return_value=context))
    monkeypatch.setattr(rounds.originals, "check", AsyncMock())
    monkeypatch.setattr(rounds.originals.access, "check_clock", Mock())

    # Admission, target preparation, migration lookup, accounting and bounded
    # threads remain real; only delegated source authorization is out of scope.

    @asynccontextmanager
    async def source(*_a, **_k):
        yield world.png, deepcopy(world.sample), None

    read = Mock(side_effect=source)
    monkeypatch.setattr(rounds.originals, "input_image", read)
    return db, context, read


async def test_active_learning_prepares_and_reuses_png_in_saved_bucket(
    world, monkeypatch, storage_db
):
    db, context, source = png_context(world, monkeypatch, storage_db)
    descriptor = rounds.media_descriptor(world.round, world.sample)
    assert await rounds.prepare_image(db, world.round, world.sample) == world.png
    world.client.objects["new-current-bucket", descriptor["key"]] = b"current trap"
    assert await rounds.prepare_image(db, world.round, world.sample) == world.png
    source.assert_called_once_with(db, context, world.sample["snapshot"], guard=None)
    assert all(bucket == world.bucket for _op, bucket, _key in world.client.calls)
    assert len([call for call in world.client.calls if call[0] == "put"]) == 1
    assert rounds.media_descriptor(world.round, world.sample) == descriptor


@pytest.mark.parametrize("operation", [rounds.read_retained_png, rounds.prepare_image])
async def test_unknown_round_owner_never_reads_current_source_or_storage(
    world, monkeypatch, operation
):
    world.round.storage_ref = None
    source = Mock(side_effect=AssertionError("NULL output cannot repair from source"))
    monkeypatch.setattr(rounds.originals, "input_image", source)
    with pytest.raises(TrainingDatasetError):
        if operation is rounds.prepare_image:
            await operation(None, world.round, world.sample)
        else:
            operation(world.round, world.sample)
    source.assert_not_called()
    assert not world.client.calls


async def test_active_learning_reuse_verifies_full_png_digest(
    world, monkeypatch, storage_db
):
    db, _context, _source = png_context(world, monkeypatch, storage_db)
    await rounds.prepare_image(db, world.round, world.sample)
    descriptor = rounds.media_descriptor(world.round, world.sample)
    world.client.objects[world.bucket, descriptor["key"]] = b"changed bytes"
    with pytest.raises(ValueError, match="identity differs"):
        rounds.read_retained_png(world.round, world.sample)


async def test_round_image_api_preserves_owner_conflict(world, monkeypatch):
    world.round.storage_ref = None
    monkeypatch.setattr(rounds, "load_round", AsyncMock(return_value=world.round))
    monkeypatch.setattr(
        rounds, "validate_spec", Mock(return_value={"samples": [world.sample]})
    )
    with pytest.raises(HTTPException) as error:
        await round_api.image(
            world.round.id,
            0,
            project=SimpleNamespace(id=world.round.project_id),
            db=SimpleNamespace(commit=AsyncMock()),
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "training_storage_unpinned"
    assert not world.client.calls


async def test_round_database_report_remains_available_without_image_owner(
    world, monkeypatch
):
    world.round.storage_ref = None
    report = {"ranking": [], "report_digest": "f" * 64}
    attempt = SimpleNamespace(
        round_id=world.round.id, status="succeeded", report=report
    )
    monkeypatch.setattr(rounds, "build_report", AsyncMock(return_value=report))
    assert await rounds.read_report(None, world.round, attempt) == report
    assert not world.client.calls


@pytest.mark.parametrize(
    "service,record_name", [(evaluations, "run"), (rounds, "round")]
)
async def test_new_attempt_cannot_implicitly_allocate_legacy_storage(
    world, monkeypatch, service, record_name
):
    record = getattr(world, record_name)
    record.storage_ref = None
    allocation = AsyncMock(side_effect=AssertionError("retry cannot allocate"))
    monkeypatch.setattr(service.owners, "allocate", allocation)
    with pytest.raises(TrainingDatasetError):
        await service.new_attempt(None, record)
    assert record.generation == 1
    allocation.assert_not_awaited()


@pytest.mark.parametrize(
    "worker,service,record_name",
    [(evaluation_worker, evaluations, "run"), (round_worker, rounds, "round")],
)
async def test_legacy_queued_claim_does_not_start_inference(
    world, monkeypatch, worker, service, record_name
):
    record = getattr(world, record_name)
    record.storage_ref, record.status = None, "queued"
    record.current_attempt_id = world.attempt.id
    attempt = world.attempt
    attempt.error_code = attempt.error_message = None
    if record_name == "round":
        attempt.round_id = record.id
    db = SimpleNamespace(
        get=AsyncMock(side_effect=[attempt, record]), commit=AsyncMock()
    )
    monkeypatch.setattr(service, "authority", AsyncMock())
    monkeypatch.setattr(
        service,
        "load_run" if record_name == "run" else "load_round",
        AsyncMock(return_value=record),
    )
    cancel = AsyncMock()
    monkeypatch.setattr(service, "request_cancel", cancel)
    started = AsyncMock()
    monkeypatch.setattr(worker.jobs, "mark_running", started)
    assert await worker.claim(factory_for(db), attempt.id, "legacy-claim") is None
    cancel.assert_awaited_once()
    started.assert_not_awaited()
    if record_name == "run":
        assert record.error_code == "training_storage_unpinned"
    else:
        assert cancel.await_args.kwargs["code"] == "training_storage_unpinned"
    assert not world.client.calls
