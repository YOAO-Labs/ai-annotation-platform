"""Saved training targets remain independent across mixed sources and cleanup."""

from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt
from app.schemas.training_run import TrainingArtifactSpec
from app.services import active_learning as learning
from app.services import sample_evaluations as evaluations
from app.services import training_observability as observations
from app.services import training_previews as previews
from app.services import training_runs as runs
from app.services import training_storage_cleanup as cleanup
from app.services import training_storage_io as storage_io
from app.services import training_dataset_storage as owners
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.workers import sample_evaluations as worker
from tests.test_training_run_storage_flow import artifact

from tests import test_training_preview_storage_flow as preview_flow
from tests import test_training_run_storage_flow as run_flow


@pytest.fixture(autouse=True)
def migration_metadata_boundary(monkeypatch):
    monkeypatch.setattr(
        storage_io.artifact_reads, "resolve", AsyncMock(return_value=None)
    )


@pytest.fixture
def run_owned(monkeypatch):
    return run_flow.owned.__wrapped__(monkeypatch)


@pytest.fixture
def preview_owned(monkeypatch):
    return preview_flow.owned.__wrapped__(monkeypatch)


def rooted(ref, name):
    ref = deepcopy(ref)
    root = f"中文/[a](b)/{name}/_aap_managed/training/"
    ref.update(
        schema=2,
        key_prefix=root,
        prefix=root + ref["prefix"],
        location_id=str(uuid.uuid4()),
        config_revision=3,
        binding_generation=7,
        endpoint_url=f"https://{name}.invalid",
    )
    ref["storage_identity"] = managed_storage_identity(
        ref["endpoint_url"], ref["bucket"]
    )
    return ref


class Database:
    def __init__(self, rows=()):
        self.rows = {(type_, row.id): row for type_, row in rows}
        self.commit = AsyncMock()

    async def get(self, model, identifier):
        return self.rows.get((model, identifier))


@pytest.fixture
def dispatch(monkeypatch):
    calls, transports = [], {}

    async def run(db, owner, purpose, callback, *args, **kwargs):
        calls.append((purpose, owner.ref))
        return callback(transports[owner.location_id], *args, **kwargs)

    monkeypatch.setattr(storage_io, "run", run)
    return calls, transports


def transport(client):
    return SimpleNamespace(
        client=client,
        generate_download_url=lambda key, **kwargs: client.generate_presigned_url(
            "get_object",
            Params={"Bucket": kwargs["bucket"], "Key": key},
            ExpiresIn=kwargs["expires_in"],
        ),
    )


@pytest.mark.parametrize("resume", [False, True])
async def test_assignment_uses_distinct_rooted_version_and_run_targets(
    run_owned, dispatch, resume
):
    world = run_owned
    world.run.storage_ref = rooted(world.run.storage_ref, "run-b")
    world.dataset.storage_ref = rooted(world.dataset.storage_ref, "dataset-c")
    world.dataset.package_key = world.dataset.storage_ref["prefix"] + "dataset.zip"
    calls, transports = dispatch
    transports[
        owners._resolve_ref(world.run.storage_ref, purpose="training-run").location_id
    ] = transport(world.client)
    transports[owners.resolve(world.dataset).location_id] = transport(world.client)
    parent = SimpleNamespace(id=uuid.uuid4(), run_id=world.run.id, artifacts={})
    if resume:
        world.attempt.resume_from_id = parent.id
        parent.artifacts["last.pt"] = {
            "role": "checkpoint",
            "key": world.run.storage_ref["prefix"] + f"attempts/{parent.id}/last.pt",
        }
    db = Database([(TrainingDatasetVersion, world.dataset), (TrainingAttempt, parent)])
    frozen = deepcopy(world.run.config)
    result = await runs.assignment(db, world.run, world.attempt, "lease")
    assert calls[0][1] == world.dataset.storage_ref
    assert world.dataset.package_key in result["dataset_url"]
    if resume:
        assert calls[1][1] == world.run.storage_ref
        assert parent.artifacts["last.pt"]["key"] in result["resume"]["url"]
    assert world.run.config == frozen
    assert db.commit.await_count == 1


async def test_rooted_upload_commits_inventory_before_signing(
    run_owned, dispatch, monkeypatch
):
    world = run_owned
    world.run.storage_ref = rooted(world.run.storage_ref, "run-b")
    spec = artifact(world)
    calls, transports = dispatch
    owner = runs.storage_owner(world.run)
    transports[owner.location_id] = transport(world.client)
    monkeypatch.setattr(
        runs, "fenced", AsyncMock(return_value=(None, world.run, world.attempt))
    )
    monkeypatch.setattr(runs.jobs, "update_progress", AsyncMock())
    db = Database()
    db.commit.side_effect = lambda: world.attempt.upload_inventory and None
    body = SimpleNamespace(
        artifacts=[
            TrainingArtifactSpec(
                **{key: value for key, value in spec.items() if key != "key"}
            )
        ]
    )
    result = await runs.authorize_upload(db, world.attempt.id, body)
    db.commit.assert_awaited_once()
    assert calls == [("training_upload_urls", owner.ref)]
    assert spec["key"] in result["best.pt"]["url"]
    assert world.client.calls[-1] == ("sign-put_object", owner.bucket, spec["key"])


async def test_preview_input_and_output_owners_are_independent_without_new_capability_gate(
    preview_owned, dispatch
):
    world = preview_owned
    world.preview.storage_ref = rooted(world.preview.storage_ref, "preview-b")
    world.dataset.storage_ref = rooted(world.dataset.storage_ref, "dataset-c")
    source = world.preview.spec["sources"][0]
    source["media_key"] = (
        world.dataset.storage_ref["prefix"] + "media/" + source["task_id"]
    )
    calls, transports = dispatch
    transports[owners.resolve(world.dataset).location_id] = transport(world.client)
    db = Database([(TrainingDatasetVersion, world.dataset)])
    result = await previews.assignment(db, world.preview)
    assert calls[0][1] == world.dataset.storage_ref
    assert source["media_key"] in result["source_urls"][source["task_id"]]
    assert result["spec"] is world.preview.spec


@pytest.mark.parametrize(
    "family,module,relative",
    [
        ("sample-evaluation", evaluations, "report"),
        ("active-learning", learning, "media"),
    ],
)
def test_auxiliary_descriptors_use_actual_saved_root(
    run_owned, family, module, relative
):
    world = run_owned
    ref = world.run.storage_ref
    namespace = (
        "sample-evaluations" if family == "sample-evaluation" else "active-learning"
    )
    ref["purpose"] = family
    ref["prefix"] = f"projects/{world.run.project_id}/{namespace}/{world.run.id}/"
    world.run.storage_ref = rooted(ref, "aux-b")
    if relative == "report":
        assert module.report_prefix(world.run) == world.run.storage_ref["prefix"]
    else:
        descriptor = module.media_descriptor(
            world.run, {"ordinal": 4, "derived_sha256": "a" * 64}
        )
        assert descriptor == {
            "key": world.run.storage_ref["prefix"] + "media/4.png",
            "sha256": "a" * 64,
        }


class Objects:
    def __init__(self):
        self.values, self.calls, self.bodies = {}, [], []

    def put_object(self, *, Bucket, Key, Body, Metadata, **kwargs):
        self.calls.append(("put", Bucket, Key))
        self.values[Bucket, Key] = bytes(Body), Metadata

    def head_object(self, *, Bucket, Key):
        raw, metadata = self.values[Bucket, Key]
        return {"ContentLength": len(raw), "Metadata": metadata, "ETag": "etag"}

    def get_object(self, *, Bucket, Key, **kwargs):
        self.calls.append(("get", Bucket, Key))
        head = self.head_object(Bucket=Bucket, Key=Key)
        body = io.BytesIO(self.values[Bucket, Key][0])
        self.bodies.append(body)
        return {**head, "Body": body}


def test_evaluation_put_and_full_readback_share_explicit_transport(
    run_owned, monkeypatch
):
    world = run_owned
    ref = world.run.storage_ref
    ref["purpose"] = "sample-evaluation"
    ref["prefix"] = (
        f"projects/{world.run.project_id}/sample-evaluations/{world.run.id}/"
    )
    world.run.storage_ref = rooted(ref, "evaluation-b")
    client = Objects()
    monkeypatch.setattr(
        evaluations,
        "report_metadata",
        lambda run, attempt: {"attempt-id": str(attempt.id)},
    )
    saved = deepcopy(world.run.storage_ref)
    receipt = evaluations.upload_report(
        world.run, world.attempt, {"status": "succeeded"}, transport=transport(client)
    )
    assert receipt["key"].startswith(saved["prefix"] + str(world.attempt.id) + "/")
    assert [item[0] for item in client.calls] == ["put", "get"]
    assert all(body.closed for body in client.bodies)
    assert world.run.storage_ref == saved
    assert world.client.calls == []


def test_active_learning_png_put_and_conditional_readback_share_transport(run_owned):
    world = run_owned
    ref = world.run.storage_ref
    ref["purpose"] = "active-learning"
    ref["prefix"] = f"projects/{world.run.project_id}/active-learning/{world.run.id}/"
    world.run.storage_ref = rooted(ref, "round-b")
    raw = b"frozen retained PNG"
    sample = {
        "ordinal": 1,
        "sha256": "a" * 64,
        "derived_sha256": hashlib.sha256(raw).hexdigest(),
    }
    client = Objects()
    learning._store_png(
        world.run,
        sample,
        learning.owners.resolve(world.run),
        raw,
        transport=transport(client),
    )
    assert all(
        call[2] == world.run.storage_ref["prefix"] + "media/1.png"
        for call in client.calls
    )
    assert all(body.closed for body in client.bodies)
    assert world.client.calls == []


def test_redacted_log_derived_write_uses_same_rooted_owner(run_owned):
    world = run_owned
    world.run.storage_ref = rooted(world.run.storage_ref, "run-b")
    receipt = artifact(world, "stdout.log", "log", b"safe retained output\n")
    source = deepcopy(receipt)
    derived = observations.redacted_log(
        world.run,
        world.attempt,
        "stdout.log",
        receipt,
        transport=transport(world.client),
    )
    assert derived["key"].startswith(
        world.run.storage_ref["prefix"] + f"attempts/{world.attempt.id}/redacted-logs/"
    )
    assert derived["source_sha256"] == receipt["sha256"]
    assert receipt == source
    assert {call[1] for call in world.client.calls} == {world.bucket}


def test_redacted_log_content_only_reads_the_saved_rooted_owner(run_owned):
    world = run_owned
    world.run.storage_ref = rooted(world.run.storage_ref, "run-b")
    receipt = artifact(world, "stdout.log", "log", b"password=private\n")
    saved = deepcopy((world.run.storage_ref, receipt))
    result = observations.read_redacted_log(
        world.run,
        world.attempt,
        "stdout.log",
        receipt,
        transport=transport(world.client),
    )
    assert b"private" not in result["content"]
    assert result["sha256"] == hashlib.sha256(result["content"]).hexdigest()
    assert result["size_bytes"] == len(result["content"])
    assert world.client.calls == [("get", world.bucket, receipt["key"])]
    assert (world.run.storage_ref, receipt) == saved


@pytest.mark.parametrize("fails", [False, True])
async def test_durable_cleanup_deletes_saved_prefix_and_retains_intent_on_failure(
    run_owned, dispatch, fails
):
    world = run_owned
    world.run.storage_ref = rooted(world.run.storage_ref, "run-b")
    owner = runs.storage_owner(world.run)
    intent = SimpleNamespace(
        id=uuid.uuid4(),
        storage_ref=owner.ref,
        bucket=owner.bucket,
        prefix=owner.prefix,
        last_attempted_at=None,
    )
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=intent), commit=AsyncMock(), delete=AsyncMock()
    )
    deleted = []

    def delete_prefix(prefix, *, bucket):
        db.commit.assert_awaited_once()
        deleted.append((prefix, bucket))
        if fails:
            raise OSError("provider unavailable")

    calls, transports = dispatch
    transports[owner.location_id] = SimpleNamespace(delete_prefix=delete_prefix)
    if fails:
        with pytest.raises(OSError):
            await cleanup.purge_requested(db, intent.id)
        db.delete.assert_not_awaited()
    else:
        assert await cleanup.purge_requested(db, intent.id)
        db.delete.assert_awaited_once_with(intent)
    assert deleted == [(owner.prefix, owner.bucket)]
    assert calls[0][1] == owner.ref


async def test_candidate_cleanup_parses_logical_project_but_deletes_full_saved_key(
    run_owned, dispatch
):
    world = run_owned
    ref = world.run.storage_ref
    ref["purpose"] = "sample-evaluation"
    ref["prefix"] = (
        f"projects/{world.run.project_id}/sample-evaluations/{world.run.id}/"
    )
    world.run.storage_ref = rooted(ref, "evaluation-b")
    owner = evaluations.owners.resolve(world.run)
    receipt = {
        "sha256": "a" * 64,
        "key": owner.prefix + f"{world.attempt.id}/" + "a" * 64 + ".json",
    }
    world.run.status, world.run.deletion_requested_at = "succeeded", None
    world.attempt.report = None
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=world.run),
        get=AsyncMock(return_value=world.attempt),
        commit=AsyncMock(),
    )

    class Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            return False

    removed = []
    calls, transports = dispatch
    transports[owner.location_id] = SimpleNamespace(
        client=SimpleNamespace(delete_object=lambda **kwargs: removed.append(kwargs))
    )
    await worker.cleanup_candidate(
        Factory(), world.run.id, world.attempt.id, receipt, storage_ref=owner.ref
    )
    assert removed == [{"Bucket": owner.bucket, "Key": receipt["key"]}]
    assert calls[0][1] == owner.ref


@pytest.mark.parametrize("change", ["lease", "inventory"])
async def test_success_readback_cannot_publish_after_attempt_or_inventory_changes(
    run_owned, dispatch, monkeypatch, change
):
    from fastapi import HTTPException

    world = run_owned
    world.run.storage_ref = rooted(world.run.storage_ref, "run-b")
    roles = {
        "best.pt": "weights",
        "last.pt": "checkpoint",
        "metrics.json": "metrics",
        "config.json": "config",
        "train.log": "log",
    }
    for name, role in roles.items():
        value = artifact(world, name, role, (name + " frozen bytes").encode())
        world.attempt.upload_inventory[name] = {
            key: value for key, value in value.items() if key != "key"
        }
    owner = runs.storage_owner(world.run)
    dispatch[1][owner.location_id] = transport(world.client)
    lease = SimpleNamespace(attempt_id=world.attempt.id)
    calls = []

    async def fenced(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            if change == "lease":
                lease.attempt_id = uuid.uuid4()
            else:
                world.attempt.upload_inventory = {}
        return lease, world.run, world.attempt

    monkeypatch.setattr(runs, "fenced", fenced)
    monkeypatch.setattr(runs, "authority", AsyncMock())
    terminal = AsyncMock()
    monkeypatch.setattr(runs, "mark_terminal", terminal)
    with pytest.raises(HTTPException) as error:
        await runs.finish(
            Database(), world.attempt.id, run_flow.finish_body("succeeded")
        )
    assert error.value.status_code == 409
    terminal.assert_not_awaited()
    assert world.attempt.artifacts == {}


async def test_model_detail_trial_sample_signs_saved_dataset_target(
    run_owned, dispatch, monkeypatch
):
    from app.api.v1 import trained_models as api

    world = run_owned
    world.dataset.storage_ref = rooted(world.dataset.storage_ref, "dataset-c")
    version = SimpleNamespace(
        id=uuid.uuid4(), provenance={"dataset_id": str(world.dataset.id)}
    )
    deployment = SimpleNamespace(status="ready", trial={"task_id": str(uuid.uuid4())})
    db = Database([(TrainingDatasetVersion, world.dataset)])
    db.scalars = AsyncMock(return_value=SimpleNamespace(all=lambda: [deployment]))
    monkeypatch.setattr(api, "lock_owner", AsyncMock())
    monkeypatch.setattr(api.service, "load_version", AsyncMock(return_value=version))
    monkeypatch.setattr(
        api.lineage, "model_metadata", AsyncMock(return_value={version.id: {}})
    )
    monkeypatch.setattr(
        api.ModelDeploymentOut,
        "model_validate",
        lambda value: SimpleNamespace(
            model_dump=lambda **kwargs: {"trial": value.trial}
        ),
    )
    monkeypatch.setattr(
        api.TrainedModelOut,
        "model_validate",
        lambda value: SimpleNamespace(
            model_dump=lambda **kwargs: {"id": str(value.id)}
        ),
    )

    def wrong_target(*args, **kwargs):
        raise AssertionError("global A signer must not receive a retained C sample")

    monkeypatch.setattr(storage_service, "generate_download_url", wrong_target)
    owner = owners.resolve(world.dataset)
    calls, transports = dispatch
    transports[owner.location_id] = SimpleNamespace(
        generate_download_url=lambda key, **kwargs: (
            f"https://dataset-c.invalid/{kwargs['bucket']}/{key}"
        )
    )
    result = await api.model_detail(
        version.id, SimpleNamespace(id=world.run.project_id), db, SimpleNamespace()
    )
    assert (
        result["deployments"][0]["trial"]["sample_url"]
        == f"https://dataset-c.invalid/{owner.bucket}/{owner.key('media/' + deployment.trial['task_id'])}"
    )
    assert calls == [("training_model_url", owner.ref)]


@pytest.mark.parametrize("failure", ["missing", "denied", "close"])
async def test_first_missing_b_png_is_classified_inside_current_target_client(
    run_owned, monkeypatch, failure
):
    from contextlib import asynccontextmanager, contextmanager
    from botocore.exceptions import ClientError
    from app.services import storage_access
    from app.services.managed_s3_target import Target
    from app.services.filesystem_storage import FilesystemStorageError

    world = run_owned
    ref = world.run.storage_ref
    ref["purpose"] = "active-learning"
    ref["prefix"] = f"projects/{world.run.project_id}/active-learning/{world.run.id}/"
    world.run.storage_ref = rooted(ref, "round-b")
    world.run.spec = {"original_contract": 1}
    owner = learning.owners.resolve(world.run)
    raw = b"bounded source PNG"
    sample = {
        "ordinal": 0,
        "sha256": "a" * 64,
        "derived_sha256": hashlib.sha256(raw).hexdigest(),
        "width": 2,
        "height": 2,
        "size_bytes": 8,
        "preprocessing": {},
        "snapshot": {},
    }

    class MissingObjects(Objects):
        def head_object(self, *, Bucket, Key):
            if (Bucket, Key) not in self.values:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "AccessDenied"
                            if failure == "denied"
                            else "NoSuchKey"
                        }
                    },
                    "HeadObject",
                )
            return super().head_object(Bucket=Bucket, Key=Key)

    client = MissingObjects()
    closed, fallback = [], []

    class Access:
        @contextmanager
        def client(self):
            try:
                yield client
            except ClientError as exc:
                raise FilesystemStorageError("path_unavailable") from exc
            finally:
                closed.append(True)
                if failure == "close":
                    raise storage_access.mark_unsettled(
                        OSError("client close uncertain")
                    )

    target = Target(
        location_id=owner.location_id,
        config_revision=3,
        connection_revision=2,
        endpoint_url=owner.ref["endpoint_url"],
        bucket=owner.bucket,
        key_prefix=owner.key_prefix,
        purpose="training",
        _access=Access(),
    )
    monkeypatch.setattr(storage_io, "prepare", AsyncMock(return_value=target))
    context = SimpleNamespace(proof=None, key=None)
    monkeypatch.setattr(
        learning.originals, "delegated", AsyncMock(return_value=context)
    )
    monkeypatch.setattr(learning.originals, "check", AsyncMock())
    monkeypatch.setattr(learning.originals.access, "check_clock", lambda *args: None)

    async def bounded(callback, *args, **kwargs):
        return callback(*args, **kwargs)

    monkeypatch.setattr(learning.originals, "bounded_thread", bounded)

    class Permit:
        revision = 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(storage_access, "acquire", AsyncMock(return_value=Permit()))
    monkeypatch.setattr(storage_access, "check", AsyncMock())

    @asynccontextmanager
    async def input_image(*args, **kwargs):
        fallback.append(True)
        yield raw, sample, None

    monkeypatch.setattr(learning.originals, "input_image", input_image)
    # This SQL-free fixture owns the physical transport and permits; durable
    # write-accounting transactions are exercised by the accounting suites.
    monkeypatch.setattr(learning.write_accounting, "recorder", lambda *_a, **_k: None)
    db = Database()
    if failure == "missing":
        assert await learning.prepare_image(db, world.run, sample) == raw
        assert fallback == [True] and len(closed) == 2
        assert [call[0] for call in client.calls] == ["put", "get"]
        db.commit.assert_awaited_once()
    else:
        with pytest.raises((FilesystemStorageError, OSError)):
            await learning.prepare_image(db, world.run, sample)
        assert not fallback and not client.calls
    assert all(body.closed for body in client.bodies)
