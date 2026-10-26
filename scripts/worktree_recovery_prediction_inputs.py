"""Retain genuine prediction-input owners in the isolated offline fixture.

The verified case uploads an original and a crop. The planned case performs a
real original upload, then raises a known synthetic exception before its crop's
conditional PUT. A real same-purpose permit defers close-time deletion; every
Snapshot, client and permit closes before the backup fingerprint is captured.
Video cases borrow the genuine canonical ffmpeg originals already seeded by
the video-export fixture. Their synthetic interruption occurs after conditional
PUT/full readback but before the receipt is published.
This is inline component qualification, not HTTP, Celery or live-crash evidence.
Current-object backup preserves content and ownership metadata, not historical
S3 version IDs. Recovery verifies those bytes separately and rejects the old
execution and cleanup receipts without rewriting inventory, proof or deadlines.
"""

from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

CASES = ("verified", "planned")
QUALIFICATION = (
    "offline-inline-components; synthetic crop interruption; no queue resume"
)
VIDEO_QUALIFICATION = (
    "offline-inline-video-components; synthetic pre-publication interruption; "
    "no HTTP, Celery, production or versioned-recovery qualification"
)


def enable_owned_import_versioning(storage, *, root):
    """Enable only the exact verified test fixture bucket, before its first PUT."""
    from app.config import settings
    from worktree_env import WorktreeError, load_identity, topology
    from worktree_resources import OWNER_TAG

    if os.environ.get("AAP_WORKTREE_MODE") != "test":
        raise WorktreeError("Test mode is required for fixture bucket versioning")
    root = Path(root)
    resources = topology(root, load_identity(root), "test")
    bucket = resources["buckets"]["MINIO_IMPORT_BUCKET"]
    if bucket != settings.minio_import_bucket or bucket != storage.import_bucket:
        raise WorktreeError("Only this fixture's owned import bucket may change")
    client = storage.client
    tags = client.get_bucket_tagging(Bucket=bucket).get("TagSet")
    if not isinstance(tags, list) or [
        tag.get("Value")
        for tag in tags
        if isinstance(tag, dict) and tag.get("Key") == OWNER_TAG
    ] != [resources["owner"]]:
        raise WorktreeError("The actual S3 bucket owner must match")
    configuration = client.get_bucket_versioning(Bucket=bucket)
    if configuration.get("Status") not in (None, "Enabled") or configuration.get(
        "MFADelete"
    ) not in (None, "Disabled"):
        raise WorktreeError(
            "Suspended or MFA-protected fixture buckets are unsupported"
        )
    if configuration.get("Status") != "Enabled":
        client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )
    after = client.get_bucket_versioning(Bucket=bucket)
    if after.get("Status") != "Enabled" or after.get("MFADelete") not in (
        None,
        "Disabled",
    ):
        raise WorktreeError("Fixture bucket versioning could not be verified")


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


def _guard(binding, *, video=False):
    async def guard(db):
        from app.db.models.task import Task
        from app.services import prediction_originals as originals, reference_bindings
        from app.services.content_snapshot import ContentError

        resolver = originals
        if video:
            from app.services import video_prediction_originals

            resolver = video_prediction_originals
        task = await db.get(Task, uuid.UUID(binding["task_id"]), populate_existing=True)
        if (
            task is None
            or reference_bindings.task_binding(task)
            != {key: value for key, value in binding.items() if key != "original"}
            or await resolver.managed_binding(db, task) != binding["original"]
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
    fields += (
        "object_count",
        "size_bytes",
        "deleted_at",
        "next_cleanup_at",
        "last_cleanup_at",
        "error_code",
    )
    return SimpleNamespace(**{name: deepcopy(getattr(row, name)) for name in fields})


def _read_restored_content(client, record, key, entry, deadline):
    """Qualify current restored bytes without adopting their new locator version."""
    from app.services import prediction_inputs, storage_access
    from storage_backup_bundle import _metadata

    prediction_inputs._deadline(deadline)
    head = client.head_object(Bucket=record.bucket, Key=key)
    prediction_inputs._http(head)
    prediction_inputs._owned(head, record.id, entry)
    etag, version = head.get("ETag"), prediction_inputs._version(head)
    if (
        not prediction_inputs._header(etag)
        or type(head.get("ContentLength")) is not int
        or head["ContentLength"] != entry["size_bytes"]
        or head.get("ContentType") != entry["media_type"]
        or head.get("DeleteMarker")
    ):
        prediction_inputs.reject("integrity_mismatch")
    metadata = _metadata(head)
    params = {"Bucket": record.bucket, "Key": key, "IfMatch": etag}
    if head.get("VersionId") is not None:
        params["VersionId"] = version or "null"
    response = client.get_object(**params)
    body = response.get("Body") if isinstance(response, dict) else None
    if body is None:
        prediction_inputs.reject("integrity_mismatch")
    with storage_access.closing_registered(body):
        prediction_inputs._http(response)
        prediction_inputs._owned(response, record.id, entry)
        if (
            response.get("ETag") != etag
            or prediction_inputs._version(response) != version
            or type(response.get("ContentLength")) is not int
            or response["ContentLength"] != entry["size_bytes"]
            or _metadata(response) != metadata
            or response.get("DeleteMarker")
        ):
            prediction_inputs.reject("integrity_mismatch")
        size, digest = 0, hashlib.sha256()
        while True:
            prediction_inputs._deadline(deadline)
            block = body.read(
                min(prediction_inputs.CHUNK_BYTES, entry["size_bytes"] - size + 1)
            )
            if type(block) is not bytes:
                prediction_inputs.reject("integrity_mismatch")
            if not block:
                break
            size += len(block)
            if size > entry["size_bytes"]:
                prediction_inputs.reject("integrity_mismatch")
            digest.update(block)
        if size != entry["size_bytes"] or digest.hexdigest() != entry["sha256"]:
            prediction_inputs.reject("integrity_mismatch")
    prediction_inputs._deadline(deadline)
    # Current HEAD also detects a replacement that appeared after the pinned GET.
    after = client.head_object(Bucket=record.bucket, Key=key)
    prediction_inputs._http(after)
    prediction_inputs._owned(after, record.id, entry)
    if (
        after.get("ETag") != etag
        or prediction_inputs._version(after) != version
        or type(after.get("ContentLength")) is not int
        or after["ContentLength"] != entry["size_bytes"]
        or _metadata(after) != metadata
        or after.get("DeleteMarker")
    ):
        prediction_inputs.reject("integrity_mismatch")


def _verify_physical(storage, record, objects):
    from botocore.exceptions import ClientError
    from app.services import prediction_inputs
    import time

    deadline = time.monotonic() + prediction_inputs.IO_SECONDS
    verified = planned = 0
    for relative, entry in objects.items():
        key = record.storage_ref["key_prefix"] + relative
        if entry["state"] == "verified":
            assert entry["version_id"] not in (None, "null")
            _read_restored_content(storage.client, record, key, entry, deadline)
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


class _NoDeleteClient:
    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    def delete_object(self, **_params):
        raise AssertionError("Recovery must reject cleanup before any DELETE")


def _verify_historical_rejections(storage, record, objects):
    from botocore.exceptions import ClientError
    from app.services import prediction_inputs
    from app.services.content_snapshot import ContentError
    import time

    before = deepcopy(objects)
    rejected = 0

    def expect_rejection(call):
        try:
            call()
        except ClientError as exc:
            assert exc.response["Error"]["Code"] in {"NoSuchVersion", "404", "NotFound"}
            assert exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404
        except ContentError as exc:
            assert exc.code in {
                "prediction_input_immutable_version_required",
                "prediction_input_integrity_mismatch",
                "prediction_input_unknown_object",
            }
        else:
            raise AssertionError("Historical versions cannot resume after relocation")

    for relative, entry in objects.items():
        if entry["state"] == "verified":
            expect_rejection(
                lambda: prediction_inputs._read(
                    storage.client,
                    record.bucket,
                    record.storage_ref["key_prefix"] + relative,
                    entry,
                    time.monotonic() + prediction_inputs.IO_SECONDS,
                    lease_id=record.id,
                )
            )
            rejected += 1
    # Cleanup must reject during physical preparation, before accounting intents
    # or deletion evidence exist. Never fabricate prepared observations from the
    # restored current object to make an obsolete receipt eligible for DELETE.
    expect_rejection(
        lambda: prediction_inputs._prepare_delete_known(
            SimpleNamespace(client=_NoDeleteClient(storage.client)),
            record,
            objects,
        )
    )
    assert objects == before, "Recovery cannot rewrite historical receipts"
    return rejected, 1


def _verify_case(storage, record, objects):
    return (
        *_verify_physical(storage, record, objects),
        *_verify_historical_rejections(storage, record, objects),
    )


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
    verified = planned = rejected = receipt_rejected = cleanup_rejected = 0
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
                lambda storage: _verify_case(storage, record, objects),
            )
            verified += counts[0]
            planned += counts[1]
            receipt_rejected += counts[2]
            cleanup_rejected += counts[3]
        except BaseException as exc:
            failure = exc
            raise
        finally:
            await originals.close_permit(permit, failure)
        assert permit._settled
    assert (verified, planned, rejected) == (3, 1, 2)
    assert (receipt_rejected, cleanup_rejected) == (3, 2)
    return {
        "prediction_input_qualification": QUALIFICATION,
        "legacy_unpinned_prediction_inputs": len(legacy),
        "pinned_prediction_inputs": len(rows),
        "verified_prediction_input_objects": verified,
        "planned_prediction_input_objects": planned,
        "relocated_prediction_source_proofs_rejected": rejected,
        "relocated_prediction_input_receipts_rejected": receipt_rejected,
        "relocated_prediction_input_cleanup_rejected": cleanup_rejected,
        "prediction_input_current_content_verified": True,
        "prediction_input_receipts_rewritten": False,
        "prediction_input_queue_resumed": False,
        "prediction_input_versioned_recovery_qualified": False,
    }


def verify_video_input_copy(storage, record):
    """Verify a restored v3 owner without renewing proof, versions or deadlines.

    The caller supplies a frozen detached row under its existing restore permit.
    Content recovery does not recreate historical S3 versions or execution rights.
    A planned member may be absent or contain the exact nonce-owned completed
    write; neither case promotes its durable state to verified.
    """
    from botocore.exceptions import ClientError
    from app.services import prediction_inputs
    import time

    if type(record.contract_version) is not int or record.contract_version != 3:
        prediction_inputs.reject("video_contract_required")
    prediction_inputs._resolve(record)
    objects = prediction_inputs._inventory(record, record.inventory)
    if len(objects) != 1:
        prediction_inputs.reject("object_not_verified")
    if record.expires_at != record.created_at + timedelta(
        hours=2
    ) or record.retain_until != record.expires_at + timedelta(days=7):
        prediction_inputs.reject()
    before = deepcopy(vars(record))
    deadline = time.monotonic() + prediction_inputs.IO_SECONDS
    verified = planned = present = 0
    for relative, entry in objects.items():
        key = record.storage_ref["key_prefix"] + relative
        if entry["state"] == "verified":
            _read_restored_content(storage.client, record, key, entry, deadline)
            verified += 1
            present += 1
        else:
            try:
                _read_restored_content(storage.client, record, key, entry, deadline)
                present += 1
            except ClientError as exc:
                if (
                    str(exc.response.get("Error", {}).get("Code"))
                    not in {
                        "404",
                        "NoSuchKey",
                        "NotFound",
                        "NoSuchVersion",
                    }
                    or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                    != 404
                ):
                    raise
            planned += 1
    rejected, cleanup = (
        _verify_historical_rejections(storage, record, objects) if present else (0, 0)
    )
    assert vars(record) == before, (
        "Recovery must preserve v3 source/probe/nonce/receipts/deadlines"
    )
    return {
        "verified_video_input_objects": verified,
        "planned_video_input_objects": planned,
        "restored_video_input_objects": present,
        "historical_video_input_receipts_rejected": rejected,
        "historical_video_input_cleanup_rejected": cleanup,
        "video_input_receipts_rewritten": False,
        "video_input_queue_resumed": False,
    }


def video_owner_identifier(case):
    assert case in CASES
    return uuid.uuid5(uuid.NAMESPACE_URL, "aap:recovery-video-prediction-input:" + case)


class _VideoReceiptInterruption(OSError):
    """Known fixture boundary after a real, fully read-back versioned PUT."""


class _BeforeVideoPublish:
    def __init__(self, probe):
        self.probe = deepcopy(probe)
        self.observed = None

    async def __call__(self, relative, expected, verified):
        assert relative.startswith("video/")
        assert expected["state"] == "planned"
        assert expected["etag"] is expected["version_id"] is None
        assert verified["state"] == "verified"
        assert verified["version_id"] not in (None, "null")
        assert expected["write_nonce"] == verified["write_nonce"]
        assert expected["probe"] == verified["probe"] == self.probe
        assert {
            key: expected[key] for key in ("sha256", "size_bytes", "container")
        } == {key: verified[key] for key in ("sha256", "size_bytes", "container")}
        self.observed = deepcopy(verified)
        raise _VideoReceiptInterruption(
            "synthetic video receipt publication interruption"
        )


async def _seed_video_case(db, factory, task, case):
    from app.services import prediction_inputs, prediction_originals as common
    from app.services import reference_bindings, storage_access
    from app.services import video_prediction_originals as originals
    from app.services.content_snapshot import ContentError

    task_id = task.id
    binding = {
        **reference_bindings.task_binding(task),
        "original": await originals.managed_binding(db, task),
    }
    # Source rows and proof are committed before independent owner/read sessions.
    await db.commit()
    guard = _guard(binding, video=True)
    inputs = await prediction_inputs.PredictionInputs.create(
        factory,
        owner_id=video_owner_identifier(case),
        owner_kind="video_predict_test",
        task_id=task_id,
        source_ref=binding,
        guard=guard,
    )
    source = permit = failure = None
    try:

        async def acquire():
            async with factory() as owned:
                return await storage_access.acquire(
                    owned,
                    uuid.UUID(inputs.record.storage_ref["location_id"]),
                    prediction_inputs.purpose(inputs.record),
                    expected_revision=inputs.record.maintenance_revision,
                )

        permit = await common.settled(acquire(), late=common.close_permit)
        source = await originals.open_snapshot(
            db,
            task_id,
            binding["original"],
            guard=guard,
            purpose=prediction_inputs.purpose(inputs.record),
            node_id="recovery-video-prediction-input",
        )
        assert source.stage.sha256 == binding["original"]["sha256"]
        assert source.stage.size == binding["original"]["size_bytes"]
        interruption = None
        if case == "planned":
            interruption = _BeforeVideoPublish(source.probe)
            inputs._publish = interruption
        try:
            # Real probe belongs to this same full-hash-verified source FD.
            await inputs.upload_video(source.stage, probe=source.probe)
        except ContentError as exc:
            assert case == "planned" and exc.code == "prediction_input_upload_failed"
            assert interruption is not None and interruption.observed is not None
        else:
            assert case == "verified"
            assert inputs.video_receipt["probe"] == source.probe
            assert inputs.video_receipt["version_id"] not in (None, "null")
    except BaseException as exc:
        failure = exc
        raise
    finally:
        await _close_case(inputs, source, permit, None, failure)
    assert source.closed and source.settled and not source.unsettled
    assert source.stage._closed and source.stage.file.closed
    assert permit._settled and source.permit._settled


async def seed_video_prediction_inputs(db, *, factory, task_ids):
    """Borrow only explicitly selected, committed canonical videos; never fake probe."""
    from app.db.models.task import Task

    assert set(task_ids) == set(CASES)
    assert len(set(task_ids.values())) == len(CASES)
    for case in CASES:
        task = await db.get(Task, task_ids[case], populate_existing=True)
        assert task is not None and task.file_type == "video"
        await _seed_video_case(db, factory, task, case)


async def verify_video_prediction_inputs(db):
    """Traverse genuine v3 fixture owners after destructive offline relocation."""
    from sqlalchemy import select
    from app.db.models.prediction_input import PredictionInputLease
    from app.db.models.task import Task
    from app.services import prediction_inputs, prediction_originals as common
    from app.services import storage_access, video_prediction_originals as originals
    from app.services.content_snapshot import ContentError

    rows = list(
        await db.scalars(
            select(PredictionInputLease).where(
                PredictionInputLease.owner_id.in_(
                    [video_owner_identifier(case) for case in CASES]
                )
            )
        )
    )
    assert len(rows) == len(CASES)
    by_owner = {row.owner_id: row for row in rows}
    counts = {
        "verified_video_input_objects": 0,
        "planned_video_input_objects": 0,
        "restored_video_input_objects": 0,
        "historical_video_input_receipts_rejected": 0,
        "historical_video_input_cleanup_rejected": 0,
    }
    source_rejected = 0
    for case in CASES:
        record = _frozen_owner(by_owner[video_owner_identifier(case)])
        assert (
            record.contract_version == 3 and record.owner_kind == "video_predict_test"
        )
        assert record.state == "cleanup_pending" and record.deleted_at is None
        assert record.uncertain_write is (case == "planned")
        ref = prediction_inputs._resolve(record)
        objects = prediction_inputs._inventory(record, record.inventory)
        assert len(objects) == 1
        entry = next(iter(objects.values()))
        assert (
            entry["state"] == case and entry["media_type"] == "application/octet-stream"
        )
        proof = originals.normalize_binding(record.source_ref["original"])
        assert {key: entry[key] for key in ("sha256", "size_bytes")} == {
            key: proof[key] for key in ("sha256", "size_bytes")
        }
        assert record.source_ref["task_id"] == str(record.task_id)
        task = await db.get(Task, record.task_id, populate_existing=True)
        assert task is not None and task.file_type == "video"
        current = await originals.managed_binding(db, task)
        assert current["bucket"] != proof["bucket"]
        assert current["facts_digest"] != proof["facts_digest"]
        assert {key: current[key] for key in ("sha256", "size_bytes")} == {
            key: proof[key] for key in ("sha256", "size_bytes")
        }
        try:
            await _guard(record.source_ref, video=True)(db)
        except ContentError as exc:
            assert exc.code == "content_binding_changed"
            source_rejected += 1
        else:
            raise AssertionError("Relocation cannot renew historical video authority")
        permit = await common.settled(
            storage_access.acquire(
                db,
                uuid.UUID(ref["location_id"]),
                prediction_inputs.purpose(record),
                expected_revision=record.maintenance_revision,
            ),
            late=common.close_permit,
        )
        failure = None
        try:
            result = await common.physical(
                prediction_inputs._using_storage,
                record,
                lambda storage: verify_video_input_copy(storage, record),
            )
            for name in counts:
                counts[name] += result[name]
        except BaseException as exc:
            failure = exc
            raise
        finally:
            await common.close_permit(permit, failure)
        assert permit._settled
    assert source_rejected == len(CASES)
    assert counts == {
        "verified_video_input_objects": 1,
        "planned_video_input_objects": 1,
        "restored_video_input_objects": 2,
        "historical_video_input_receipts_rejected": 1,
        "historical_video_input_cleanup_rejected": 2,
    }
    return {
        "video_prediction_input_qualification": VIDEO_QUALIFICATION,
        "pinned_video_prediction_inputs": len(rows),
        **counts,
        "relocated_video_source_proofs_rejected": source_rejected,
        "video_input_current_content_verified": True,
        "video_input_receipts_rewritten": False,
        "video_input_queue_resumed": False,
        "video_input_versioned_recovery_qualified": False,
    }
