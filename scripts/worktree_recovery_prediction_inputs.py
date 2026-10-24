"""Retain genuine prediction-input owners in the isolated offline fixture.

The verified case uploads an original and a crop. The planned case performs a
real original upload, then raises a known synthetic exception before its crop's
conditional PUT. A real same-purpose permit defers close-time deletion; every
Snapshot, client and permit closes before the backup fingerprint is captured.
This is inline component qualification, not HTTP, Celery or live-crash evidence.
Current-object backup cannot preserve historical S3 version IDs. The fixture
therefore explicitly requires unversioned receipts and never rewrites inventory,
source proof or deadlines to make a relocated owner executable again.
"""

from copy import deepcopy
from datetime import timedelta
import io
from types import SimpleNamespace
import uuid

CASES = ("verified", "planned")
QUALIFICATION = (
    "offline-inline-components; synthetic crop interruption; no queue resume"
)


def owner_identifier(case):
    assert case in CASES
    return uuid.uuid5(uuid.NAMESPACE_URL, "aap:recovery-prediction-input:" + case)


class _PlannedInterruption(OSError):
    """Known fixture boundary before the transport sends a crop PUT."""


class _BeforePutClient:
    def __init__(self, client):
        self._client = client
        self.attempts = 0
        self.closed = False

    def __getattr__(self, name):
        return getattr(self._client, name)

    def put_object(self, **params):
        self.attempts += 1
        assert params["IfNoneMatch"] == "*"
        metadata = params["Metadata"]
        lease = uuid.UUID(metadata["aap-prediction-lease"])
        nonce = uuid.UUID(metadata["aap-prediction-write"])
        assert nonce.version == 4
        assert params["Key"].startswith(f"prediction-inputs/{lease}/crops/")
        raise _PlannedInterruption("synthetic prediction-input crop interruption")

    def close(self):
        self._client.close()
        self.closed = True


class _BeforePutStorage:
    def __init__(self, storage):
        self._storage = storage
        self.client = _BeforePutClient(storage.client)

    def __getattr__(self, name):
        return getattr(self._storage, name)


def _interrupted_storage():
    from app.services import prediction_inputs

    # The caller's real output permit already covers this constructor and close.
    return _BeforePutStorage(prediction_inputs.make_storage())


def _crop_content(inputs, task):
    image = inputs.load_image(task)
    with image.crop((0, 0, 1, 1)) as crop, io.BytesIO() as stream:
        crop.save(stream, format="JPEG", quality=90)
        return stream.getvalue()


def _guard(binding):
    async def guard(db):
        from app.db.models.task import Task
        from app.services import prediction_originals as originals, reference_bindings
        from app.services.content_snapshot import ContentError

        task = await db.get(Task, uuid.UUID(binding["task_id"]), populate_existing=True)
        if (
            task is None
            or reference_bindings.task_binding(task)
            != {key: value for key, value in binding.items() if key != "original"}
            or await originals.managed_binding(db, task) != binding["original"]
        ):
            raise ContentError("content_binding_changed")

    return guard


async def _close_case(inputs, source, permit, interrupted, failure):
    from app.services import prediction_originals as originals, storage_access

    problem = failure

    async def attempt(call):
        nonlocal problem
        try:
            await originals.settled(call())
        except BaseException as exc:
            if problem is None:
                problem = exc
            if getattr(exc, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(problem)

    # A same-purpose source/output permit must still exist during inputs.close.
    await attempt(inputs.close)
    if interrupted is not None and not interrupted.client.closed:
        await attempt(lambda: originals.physical(interrupted.client.close))
    if source is not None:
        await attempt(lambda: source.close(problem))
    if permit is not None:
        await attempt(lambda: originals.close_permit(permit, problem))
    if failure is None and problem is not None:
        raise problem


async def _seed_case(db, factory, task, case):
    from app.services import prediction_inputs, prediction_originals as originals
    from app.services import reference_bindings, storage_access
    from app.services.content_snapshot import ContentError

    binding = {
        **reference_bindings.task_binding(task),
        "original": await originals.managed_binding(db, task),
    }
    guard = _guard(binding)
    inputs = await prediction_inputs.PredictionInputs.create(
        factory,
        owner_id=owner_identifier(case),
        owner_kind="predict_test",
        task_id=task.id,
        source_ref=binding,
        guard=guard,
    )
    source = permit = interrupted = failure = None
    try:

        async def acquire():
            async with factory() as owned:
                return await storage_access.acquire(
                    owned,
                    uuid.UUID(inputs.record.storage_ref["location_id"]),
                    prediction_inputs.purpose(inputs.record),
                    expected_revision=inputs.record.maintenance_revision,
                )

        permit = await originals.settled(acquire(), late=originals.close_permit)
        source = await originals.open_snapshot(
            db,
            task.id,
            binding["original"],
            guard=guard,
            purpose=prediction_inputs.purpose(inputs.record),
            node_id="recovery-prediction-input",
        )
        await inputs.upload_snapshot(source.snapshot)
        content = await originals.physical(_crop_content, inputs, task)
        if case == "planned":
            interrupted = await originals.settled(
                originals.physical(_interrupted_storage),
                late=lambda storage: originals.physical(storage.client.close),
            )
            inputs.storage = interrupted
            try:
                await inputs.upload_crop(task, 0, content)
            except ContentError as exc:
                assert exc.code == "prediction_input_upload_failed"
                assert interrupted.client.attempts == 1 and interrupted.client.closed
            else:
                raise AssertionError(
                    "Synthetic crop interruption must reject its upload"
                )
        else:
            await inputs.upload_crop(task, 0, content)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        await _close_case(inputs, source, permit, interrupted, failure)
    assert source.snapshot._closed and source.snapshot.file.closed
    assert permit._settled and source.permit._settled


async def seed_prediction_inputs(db, *, factory, task_ids):
    """Sources are already committed; all new independent intents commit inline."""
    from app.db.models.task import Task

    assert set(task_ids) == set(CASES)
    for case in CASES:
        task = await db.get(Task, task_ids[case], populate_existing=True)
        assert task is not None
        await _seed_case(db, factory, task, case)


def _frozen_owner(row):
    from app.services import prediction_inputs

    fields = (*prediction_inputs._FACTS, "state", "uncertain_write", "inventory")
    fields += ("object_count", "size_bytes", "deleted_at")
    return SimpleNamespace(**{name: deepcopy(getattr(row, name)) for name in fields})


def _verify_physical(storage, record, objects):
    from botocore.exceptions import ClientError
    from app.services import prediction_inputs
    import time

    deadline = time.monotonic() + prediction_inputs.IO_SECONDS
    verified = planned = 0
    for relative, entry in objects.items():
        key = record.storage_ref["key_prefix"] + relative
        assert entry["version_id"] is None, "Versioned input recovery is unavailable"
        if entry["state"] == "verified":
            prediction_inputs._read(
                storage.client,
                record.bucket,
                key,
                entry,
                deadline,
                lease_id=record.id,
            )
            verified += 1
        else:
            assert entry["state"] == "planned" and entry["etag"] is None
            try:
                storage.client.head_object(Bucket=record.bucket, Key=key)
            except ClientError as exc:
                assert str(exc.response["Error"]["Code"]) in {
                    "404",
                    "NoSuchKey",
                    "NotFound",
                }
                assert exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404
            else:
                raise AssertionError("The synthetic pre-PUT crop must remain absent")
            planned += 1
    return verified, planned


async def verify_prediction_inputs(db):
    """Verify relocated saved copies without cleanup, signing or proof renewal."""
    from sqlalchemy import select
    from app.db.models.prediction_input import PredictionInputLease
    from app.db.models.task import Task
    from app.services import prediction_inputs, prediction_originals as originals
    from app.services import storage_access
    from app.services.content_snapshot import ContentError

    legacy = list(
        await db.scalars(
            select(PredictionInputLease).where(
                PredictionInputLease.owner_kind == "recovery_fixture"
            )
        )
    )
    assert len(legacy) == 1
    old = legacy[0]
    assert old.contract_version == 1 and old.state == "cleanup_pending"
    assert old.storage_ref is None and old.inventory is None and old.source_ref is None
    assert old.maintenance_revision is None and old.object_count == old.size_bytes == 0
    rows = list(
        await db.scalars(
            select(PredictionInputLease).where(
                PredictionInputLease.owner_id.in_(
                    [owner_identifier(case) for case in CASES]
                )
            )
        )
    )
    assert len(rows) == len(CASES)
    by_owner = {row.owner_id: row for row in rows}
    verified = planned = rejected = 0
    for case in CASES:
        record = _frozen_owner(by_owner[owner_identifier(case)])
        assert record.contract_version == 2 and record.owner_kind == "predict_test"
        assert record.state == "cleanup_pending" and record.deleted_at is None
        assert record.uncertain_write is (case == "planned")
        assert record.expires_at == record.created_at + timedelta(hours=2)
        assert record.retain_until == record.expires_at + timedelta(days=7)
        ref = prediction_inputs._resolve(record)
        objects = prediction_inputs._inventory(record, record.inventory)
        assert len(objects) == 2
        original = [
            (key, value)
            for key, value in objects.items()
            if key.startswith("original/")
        ]
        crops = [
            (key, value) for key, value in objects.items() if key.startswith("crops/")
        ]
        assert len(original) == len(crops) == 1
        assert original[0][1]["state"] == "verified"
        assert crops[0][1]["state"] == case
        proof = record.source_ref["original"]
        assert {key: original[0][1][key] for key in ("sha256", "size_bytes")} == {
            key: proof[key] for key in ("sha256", "size_bytes")
        }
        assert record.source_ref["task_id"] == str(record.task_id)
        task = await db.get(Task, record.task_id, populate_existing=True)
        assert task is not None
        current = await originals.managed_binding(db, task)
        assert current["bucket"] != proof["bucket"]
        assert current["facts_digest"] != proof["facts_digest"]
        assert {key: current[key] for key in ("sha256", "size_bytes")} == {
            key: proof[key] for key in ("sha256", "size_bytes")
        }
        try:
            await _guard(record.source_ref)(db)
        except ContentError as exc:
            assert exc.code == "content_binding_changed"
            rejected += 1
        else:
            raise AssertionError("Relocation cannot renew historical source authority")
        permit = await originals.settled(
            storage_access.acquire(
                db,
                uuid.UUID(ref["location_id"]),
                prediction_inputs.purpose(record),
                expected_revision=record.maintenance_revision,
            ),
            late=originals.close_permit,
        )
        failure = None
        try:
            counts = await originals.physical(
                prediction_inputs._using_storage,
                record,
                lambda storage: _verify_physical(storage, record, objects),
            )
            verified += counts[0]
            planned += counts[1]
        except BaseException as exc:
            failure = exc
            raise
        finally:
            await originals.close_permit(permit, failure)
        assert permit._settled
    assert (verified, planned, rejected) == (3, 1, 2)
    return {
        "prediction_input_qualification": QUALIFICATION,
        "legacy_unpinned_prediction_inputs": len(legacy),
        "pinned_prediction_inputs": len(rows),
        "verified_prediction_input_objects": verified,
        "planned_prediction_input_objects": planned,
        "relocated_prediction_source_proofs_rejected": rejected,
        "prediction_input_receipts_rewritten": False,
        "prediction_input_queue_resumed": False,
        "prediction_input_versioned_recovery_qualified": False,
    }
