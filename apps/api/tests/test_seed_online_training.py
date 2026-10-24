"""The training fixture must produce real, stable, reviewed training input."""

import hashlib
import io
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import zipfile

from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from PIL import Image
import pytest
from sqlalchemy import func, select

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services.storage import storage_service
from app.config import settings
from app.services import async_job as jobs
from app.services import training_dataset_storage
from app.services import managed_original_reader as reader
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.training_dataset_bridge import TrainingDatasetError, assign_splits
from app.workers import training_dataset as worker
from scripts import seed_online_training as fixture_seed
from scripts.seed_assets import SeedAssetError
from tests.factory import create_project


@pytest.fixture
def coco8(tmp_path, monkeypatch):
    samples = []
    expected = {}
    for index in range(8):
        split = "train" if index < 4 else "val"
        name = f"{index:012d}"
        image = tmp_path / "images" / split / f"{name}.jpg"
        label = tmp_path / "labels" / split / f"{name}.txt"
        image.parent.mkdir(parents=True, exist_ok=True)
        label.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 24), (index * 30, 20, 40)).save(image)
        label.write_text(f"{index} 0.5 0.5 0.4 0.2\n", encoding="utf-8")
        samples.append(
            {
                "task_id": name,
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "labels": "",
            }
        )
        expected[name] = split
    # Synthetic test bytes have their own deterministic split; production uses
    # seed 4 for the pinned COCO8 asset and verifies it before touching storage.
    seed = next(
        seed
        for seed in range(10000)
        if assign_splits(samples, seed=seed, validation_fraction=0.5) == expected
    )
    monkeypatch.setattr(fixture_seed, "SPLIT_SEED", seed)
    return tmp_path


@pytest.fixture
def memory_storage(monkeypatch):
    class Client:
        def __init__(self):
            self.objects = {}
            self.writes = 0
            self.condition_failures = 0
            self.closed_clients = 0
            self.streams = []
            self.meta = SimpleNamespace(
                endpoint_url=storage_service.client.meta.endpoint_url
            )

        def put_object(self, *, Bucket, Key, Body, **extra):
            self.writes += 1
            self.objects[Bucket, Key] = (Body, extra.get("Metadata", {}))

        def head_object(self, *, Bucket, Key):
            if (Bucket, Key) not in self.objects:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            payload, metadata = self.objects[Bucket, Key]
            return {
                "ContentLength": len(payload),
                "ETag": hashlib.sha256(payload).hexdigest(),
                "Metadata": metadata,
                "ResponseMetadata": {"HTTPStatusCode": 200},
                "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc),
            }

        def get_object(self, *, Bucket, Key, IfMatch=None):
            head = self.head_object(Bucket=Bucket, Key=Key)
            if IfMatch is not None and IfMatch != head["ETag"]:
                self.condition_failures += 1
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "GetObject",
                )
            payload, _ = self.objects[Bucket, Key]
            body = StreamingBody(io.BytesIO(payload), len(payload))
            self.streams.append(body)
            return {**head, "Body": body}

        def close(self):
            self.closed_clients += 1

        def upload_file(self, filename, bucket, key, ExtraArgs):
            self.put_object(
                Bucket=bucket, Key=key, Body=Path(filename).read_bytes(), **ExtraArgs
            )

    client = Client()
    monkeypatch.setattr(storage_service, "client", client)
    monkeypatch.setattr(reader, "create_client", lambda **kwargs: client)
    monkeypatch.setattr(storage_service, "delete_prefix", lambda *args, **kwargs: 0)
    monkeypatch.setattr(worker, "notify_job_terminal", AsyncMock())
    return client


@pytest.fixture
def coco8_seg(coco8, monkeypatch):
    root = coco8 / "coco8-seg"
    shutil.copytree(coco8 / "images", root / "images")
    for image in (root / "images").glob("*/*.jpg"):
        label = root / "labels" / image.parent.name / f"{image.stem}.txt"
        label.parent.mkdir(parents=True, exist_ok=True)
        label.write_text("0 0.2 0.2 0.8 0.2 0.8 0.8 0.2 0.8\n")
    monkeypatch.setattr(settings, "raster_mask_read_enabled", True)
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    return root


async def test_segmentation_extends_existing_fixture_without_replacing_detection(
    db_session, baseline, annotator, reviewer, coco8, coco8_seg, memory_storage
):
    project, original = baseline

    @asynccontextmanager
    async def sessions():
        yield db_session

    await worker.build_dataset(
        original.id, original.async_job_id, original.attempt_id, sessions
    )
    await db_session.refresh(original)
    assert original.status == "ready", original.error_message
    original_snapshot = original.snapshot
    original_owner = deepcopy(original.storage_ref)
    kwargs = {
        "owner_id": project.owner_id,
        "annotator_id": annotator[0].id,
        "reviewer_id": reviewer[0].id,
    }
    original_annotations = list(
        (
            await db_session.scalars(
                select(Annotation).where(Annotation.project_id == project.id)
            )
        ).all()
    )
    original_ids = {annotation.id for annotation in original_annotations}
    project.name = "保留我的检测实验项目"
    _, created = await fixture_seed.seed_online_training(
        db_session, fixture=coco8, segmentation_fixture=coco8_seg, **kwargs
    )
    assert not created and project.name == "保留我的检测实验项目"
    assert await fixture_seed.prepare_baseline(db_session, project) is original
    assert original.snapshot == original_snapshot
    assert (
        project.tool_bindings["bbox"]["classes"]
        == project.tool_bindings["region"]["classes"]
    )
    version = await fixture_seed.prepare_baseline(db_session, project, task="segment")
    await db_session.commit()
    await db_session.refresh(version)
    segment_owner = deepcopy(version.storage_ref)
    assert segment_owner["origin"] == "new" and segment_owner["evidence"] is None
    assert training_dataset_storage.resolve(version).prefix == (
        f"projects/{project.id}/datasets/{version.id}/"
    )
    assert original.storage_ref == original_owner and segment_owner != original_owner

    await worker.build_dataset(
        version.id, version.async_job_id, version.attempt_id, sessions
    )
    await db_session.refresh(version)
    assert version.status == "ready", version.error_message
    assert (
        version.manifest["schema"] == 4
        and version.manifest["bridge"] == "ultralytics-instance-rle-1"
    )
    assert version.manifest["summary"]["objects"] == 8
    assert {s["task_id"]: s["split"] for s in version.manifest["samples"]} == {
        s["task_id"]: s["split"] for s in original.manifest["samples"]
    }
    annotations = list(
        (
            await db_session.scalars(
                select(Annotation).where(Annotation.project_id == project.id)
            )
        ).all()
    )
    assert len(annotations) == 16 and original_ids <= {a.id for a in annotations}
    masks = [a for a in annotations if a.tool_unit_id == "region"]
    assert len(masks) == 8 and all(a.geometry["type"] == "raster_mask" for a in masks)
    for annotation in masks:
        ref = annotation.geometry["mask"]
        payload, _ = memory_storage.objects[storage_service.bucket, ref["object_key"]]
        assert hashlib.sha256(payload).hexdigest() == ref["sha256"]
        assert json.loads(payload)["size"] == [24, 32]
    tasks = list(
        (
            await db_session.scalars(select(Task).where(Task.project_id == project.id))
        ).all()
    )
    assert all(
        t.total_annotations == 2 and t.reviewed_at and t.status == "completed"
        for t in tasks
    )
    payload, _ = memory_storage.objects[
        storage_service.training_bucket, version.package_key
    ]
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for sample in version.manifest["samples"]:
            assert len(json.loads(archive.read(sample["label_path"]))["instances"]) == 1

    # A completed extension never repairs later human edits or deleted masks.
    frozen = version.snapshot
    masks[0].version += 1
    await db_session.delete(masks[1])
    await db_session.flush()
    writes = memory_storage.writes
    _, created = await fixture_seed.seed_online_training(
        db_session,
        fixture=Path("/not-needed"),
        segmentation_fixture=Path("/not-needed"),
        **kwargs,
    )
    assert not created and memory_storage.writes == writes
    assert (
        await fixture_seed.prepare_baseline(db_session, project, task="segment")
        is version
    )
    assert version.snapshot == frozen and masks[0].version == 2
    assert version.storage_ref == segment_owner
    assert await db_session.get(Annotation, masks[1].id) is None


@pytest.mark.parametrize("conflict", ["edited", "reopened", "custom_region"])
async def test_segmentation_extension_rejects_user_changes_before_object_writes(
    db_session,
    baseline,
    annotator,
    reviewer,
    coco8,
    coco8_seg,
    memory_storage,
    conflict,
):
    project, _ = baseline
    if conflict == "edited":
        annotation = await db_session.scalar(
            select(Annotation).where(Annotation.project_id == project.id)
        )
        annotation.version += 1
    elif conflict == "reopened":
        task = await db_session.scalar(
            select(Task).where(Task.project_id == project.id)
        )
        task.status = "in_progress"
    else:
        project.tool_bindings = {
            **project.tool_bindings,
            "region": {"enabled": True, "classes": [{"name": "custom", "order": 0}]},
        }
    await db_session.flush()
    writes = memory_storage.writes
    with pytest.raises(SeedAssetError, match="edited|customized"):
        await fixture_seed.seed_online_training(
            db_session,
            fixture=coco8,
            segmentation_fixture=coco8_seg,
            owner_id=project.owner_id,
            annotator_id=annotator[0].id,
            reviewer_id=reviewer[0].id,
        )
    assert memory_storage.writes == writes


def test_segmentation_source_rejects_different_media(coco8, coco8_seg):
    (coco8_seg / "images/train/000000000000.jpg").write_bytes(b"different image")
    with pytest.raises(SeedAssetError, match="differs"):
        fixture_seed._read_segmentation(coco8_seg, fixture_seed._read_samples(coco8))


async def test_fixture_materializes_reviewed_input_and_preserves_edits(
    db_session, super_admin, annotator, reviewer, coco8, memory_storage
):
    owner, _ = super_admin
    anno, _ = annotator
    qa, _ = reviewer
    kwargs = {"owner_id": owner.id, "annotator_id": anno.id, "reviewer_id": qa.id}
    project, created = await fixture_seed.seed_online_training(
        db_session, fixture=coco8, **kwargs
    )
    assert created
    assets = list(await db_session.scalars(select(DatasetAsset)))
    replicas = list(await db_session.scalars(select(DatasetAssetLocation)))
    assert len(assets) == len(replicas) == 8
    by_asset = {asset.id: asset for asset in assets}
    for replica in replicas:
        asset = by_asset[replica.asset_id]
        raw, _ = memory_storage.objects[replica.bucket, replica.object_key]
        assert asset.verification_state == "verified"
        assert asset.checksum_algorithm == "sha256"
        assert asset.checksum_value == hashlib.sha256(raw).hexdigest()
        assert asset.size_bytes == len(raw)
        assert replica.location_id == LEGACY_LOCATION_ID
        assert (
            replica.role == "managed_original" and replica.reference_state == "present"
        )
    version = await fixture_seed.prepare_baseline(db_session, project)
    assert len(version.snapshot["samples"]) == 8
    for sample in version.snapshot["samples"]:
        receipt = sample["original"]
        asset = by_asset[uuid.UUID(receipt["content_id"])]
        assert receipt["owner_kind"] == "dataset_asset"
        assert receipt["sha256"] == asset.checksum_value
        assert receipt["size_bytes"] == asset.size_bytes
        assert receipt["location_id"] == str(LEGACY_LOCATION_ID)
    await db_session.commit()
    await db_session.refresh(version)
    saved_owner = deepcopy(version.storage_ref)
    assert saved_owner["origin"] == "new" and saved_owner["evidence"] is None
    owner_receipt = training_dataset_storage.resolve(version)
    assert owner_receipt.prefix == f"projects/{project.id}/datasets/{version.id}/"
    assert owner_receipt.bucket == storage_service.training_bucket

    @asynccontextmanager
    async def sessions():
        yield db_session

    await worker.build_dataset(
        version.id, version.async_job_id, version.attempt_id, sessions
    )
    await db_session.refresh(version)
    assert version.status == "ready", version.error_message
    assert version.manifest["summary"]["objects"] == 8
    assert len(version.snapshot["classes"]) == 80
    tasks = list(
        (
            await db_session.scalars(select(Task).where(Task.project_id == project.id))
        ).all()
    )
    items = {
        item.id: item for item in (await db_session.scalars(select(DatasetItem))).all()
    }
    assert len(tasks) == project.total_tasks == project.completed_tasks == 8
    for task in tasks:
        assert task.status == "completed" and task.reviewed_at and task.is_labeled
        assert task.assignee_id == anno.id and task.reviewer_id == qa.id
        assert task.review_contributor_ids == [str(anno.id)]
        assert task.review_submitter_id == anno.id and task.review_round_id
    expected = {
        str(task.id): items[task.dataset_item_id].metadata_["coco8_split"]
        for task in tasks
    }
    assert {s["task_id"]: s["split"] for s in version.manifest["samples"]} == expected
    payload, _ = memory_storage.objects[
        storage_service.training_bucket, version.package_key
    ]
    assert hashlib.sha256(payload).hexdigest() == version.package_sha256
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert json.loads(archive.read("classes.json")) == list(fixture_seed.COCO_NAMES)
        for sample in version.manifest["samples"]:
            assert len(archive.read(sample["label_path"]).decode().splitlines()) == 1
            assert (
                hashlib.sha256(archive.read(sample["image_path"])).hexdigest()
                == sample["sha256"]
            )
    # Later iterations inherit the original partitions even with other defaults.
    assert (
        assign_splits(
            [{**s, "labels": ""} for s in version.manifest["samples"]],
            seed=42,
            validation_fraction=0.2,
            parent_manifest=version.manifest,
        )
        == expected
    )

    annotation = await db_session.scalar(
        select(Annotation).where(Annotation.project_id == project.id)
    )
    original_snapshot = version.snapshot
    annotation.geometry = {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1}
    project.name = "我的训练试验"
    await db_session.flush()
    writes = memory_storage.writes
    again, created = await fixture_seed.seed_online_training(
        db_session, fixture=Path("/not-needed"), **kwargs
    )
    assert not created and again.name == "我的训练试验"
    assert await fixture_seed.prepare_baseline(db_session, again) is version
    assert memory_storage.writes == writes
    assert annotation.geometry["x"] == 0.1 and version.snapshot == original_snapshot
    assert version.storage_ref == saved_owner
    assert (
        await db_session.scalar(
            select(func.count()).select_from(TrainingDatasetVersion)
        )
        == 1
    )


@pytest.fixture
async def baseline(db_session, super_admin, annotator, reviewer, coco8, memory_storage):
    owner, _ = super_admin
    anno, _ = annotator
    qa, _ = reviewer
    project, _ = await fixture_seed.seed_online_training(
        db_session,
        fixture=coco8,
        owner_id=owner.id,
        annotator_id=anno.id,
        reviewer_id=qa.id,
    )
    version = await fixture_seed.prepare_baseline(db_session, project)
    await db_session.commit()
    return project, version


@pytest.mark.parametrize("status", ["failed", "cancelled", "running"])
async def test_baseline_rerun_recovers_unfinished_build(
    db_session, baseline, memory_storage, monkeypatch, status
):
    project, version = baseline
    version_id, job_id, attempt_id = (
        version.id,
        version.async_job_id,
        version.attempt_id,
    )
    snapshot, digest = version.snapshot, version.snapshot_digest
    saved_owner = deepcopy(version.storage_ref)

    @asynccontextmanager
    async def sessions():
        yield db_session

    if status == "failed":

        def temporary_failure(*args, **kwargs):
            raise TrainingDatasetError(
                "temporary_failure", "Temporary packaging failure"
            )

        with monkeypatch.context() as patch:
            patch.setattr(worker, "build_training_package", temporary_failure)
            await worker.build_dataset(version_id, job_id, attempt_id, sessions)
        await db_session.refresh(version)
        assert version.status == "failed"
        assert version.error_code == "temporary_failure"
        # The test shares the worker session; its rollback expires this caller.
        await db_session.refresh(project)
    else:
        version.status = status
        version.progress_pct = 40
        await jobs.mark_running(db_session, job_id)
        if status == "cancelled":
            version.completed_at = datetime.now(timezone.utc)
            await jobs.mark_cancelled(db_session, job_id)
        await db_session.commit()

    trap_bucket = "seed-current-training-bucket-trap"
    monkeypatch.setattr(storage_service, "training_bucket", trap_bucket)
    retried = await fixture_seed.prepare_baseline(db_session, project)
    await db_session.commit()
    await db_session.refresh(version)
    assert retried is version and retried.id == version_id
    assert retried.status == "pending"
    assert retried.async_job_id != job_id and retried.attempt_id != attempt_id
    assert retried.snapshot == snapshot and retried.snapshot_digest == digest
    assert retried.storage_ref == saved_owner
    assert retried.progress_pct == 0
    assert retried.error_code is None and retried.error_message is None
    assert retried.completed_at is None
    old_job = await db_session.get(AsyncJob, job_id)
    assert old_job.status == ("cancelled" if status == "cancelled" else "failed")
    assert old_job.completed_at is not None
    assert await fixture_seed.prepare_baseline(db_session, project) is version
    assert await db_session.scalar(select(func.count()).select_from(AsyncJob)) == 2
    assert (
        await db_session.scalar(
            select(func.count()).select_from(TrainingDatasetVersion)
        )
        == 1
    )

    # The test's outer transaction retains the retry lock across session commits.
    # Exercise the builder directly, as after the CLI commits its transaction.
    writes = memory_storage.writes
    package_key = f"{saved_owner['prefix']}attempts/{retried.attempt_id}/dataset.zip"
    memory_storage.objects[trap_bucket, package_key] = (b"current bucket trap", {})
    await worker._build_dataset(version_id, job_id, attempt_id, sessions)
    await db_session.refresh(version)
    assert memory_storage.writes == writes and version.status == "pending"
    await worker._build_dataset(
        version_id, version.async_job_id, version.attempt_id, sessions
    )
    await db_session.refresh(version)
    assert version.status == "ready", version.error_message
    assert version.snapshot == snapshot and version.manifest["summary"]["objects"] == 8
    assert version.storage_ref == saved_owner and version.package_key == package_key
    assert (saved_owner["bucket"], package_key) in memory_storage.objects
    assert memory_storage.objects[trap_bucket, package_key][0] == b"current bucket trap"


@pytest.mark.parametrize("status", ["pending", "failed"])
async def test_existing_unpinned_baseline_is_not_silently_allocated(
    db_session, super_admin, memory_storage, status
):
    project = await create_project(db_session, owner_id=super_admin[0].id)
    historical = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=project.owner_id,
        request_id=fixture_seed.BASELINE_REQUEST_ID,
        request_digest="a" * 64,
        name="historical baseline without verified ownership",
        snapshot={},
        snapshot_digest="b" * 64,
        status=status,
    )
    db_session.add(historical)
    await db_session.commit()
    writes = memory_storage.writes
    assert await fixture_seed.prepare_baseline(db_session, project) is historical
    await db_session.commit()
    await db_session.refresh(historical)
    assert historical.storage_ref is None and memory_storage.writes == writes
    with pytest.raises(TrainingDatasetError) as raised:
        training_dataset_storage.resolve(historical)
    assert raised.value.code == "training_storage_unpinned"


@pytest.mark.parametrize("status", ["failed", "cancelled", "running"])
async def test_baseline_rerun_does_not_replace_an_active_attempt(
    db_session, test_engine, baseline, status
):
    project, version = baseline
    version.status = status
    await db_session.commit()
    job_id, attempt_id = version.async_job_id, version.attempt_id
    key = func.hashtextextended(str(version.id), 1)
    async with test_engine.connect() as execution:
        await execution.execute(select(func.pg_advisory_lock(key)))
        await execution.commit()
        try:
            with pytest.raises(SeedAssetError, match="稍后重试"):
                await fixture_seed.prepare_baseline(db_session, project)
        finally:
            await execution.execute(select(func.pg_advisory_unlock(key)))
            await execution.commit()
    await db_session.refresh(version)
    assert version.status == status
    assert version.async_job_id == job_id and version.attempt_id == attempt_id
    assert await db_session.scalar(select(func.count()).select_from(AsyncJob)) == 1


async def test_fixture_refuses_unrelated_project(
    db_session, super_admin, annotator, reviewer, coco8, memory_storage
):
    owner, _ = super_admin
    anno, _ = annotator
    qa, _ = reviewer
    db_session.add(
        Project(
            display_id=fixture_seed.PROJECT_DISPLAY_ID,
            name="已有项目",
            type_key="image-det",
            type_label="检测",
            owner_id=owner.id,
        )
    )
    await db_session.flush()
    with pytest.raises(SeedAssetError, match="unrelated data"):
        await fixture_seed.seed_online_training(
            db_session,
            fixture=coco8,
            owner_id=owner.id,
            annotator_id=anno.id,
            reviewer_id=qa.id,
        )
    assert memory_storage.writes == 0


async def test_fixture_rejects_production_before_accessing_services(monkeypatch):
    monkeypatch.setattr(fixture_seed.settings, "environment", "production")
    with pytest.raises(SeedAssetError, match="production"):
        await fixture_seed.seed_online_training(
            None,
            fixture=Path("/not-needed"),
            owner_id=None,
            annotator_id=None,
            reviewer_id=None,
        )


def test_fixture_rejects_changed_partition_bytes(coco8):
    (coco8 / "images" / "train" / "000000000000.jpg").unlink()
    with pytest.raises(SeedAssetError, match="four train images"):
        fixture_seed._read_samples(coco8)


async def test_new_fixture_registers_the_exact_successfully_uploaded_originals(
    monkeypatch, coco8, memory_storage
):
    rows = []

    async def flush():
        for row in rows:
            if isinstance(row, DatasetItem) and row.id is None:
                row.id = uuid.uuid4()

    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, None]),
        execute=AsyncMock(),
        add=Mock(side_effect=rows.append),
        add_all=Mock(side_effect=rows.extend),
        flush=AsyncMock(side_effect=flush),
    )
    binding = SimpleNamespace(bucket=storage_service.datasets_bucket)
    bindings = AsyncMock(return_value=binding)
    registration = AsyncMock()
    monkeypatch.setattr(fixture_seed, "ensure_legacy_bindings", bindings, raising=False)
    monkeypatch.setattr(
        fixture_seed, "register_item_asset", registration, raising=False
    )
    monkeypatch.setattr(settings, "environment", "development")
    project, created = await fixture_seed.seed_online_training(
        db,
        fixture=coco8,
        owner_id=uuid.uuid4(),
        annotator_id=uuid.uuid4(),
        reviewer_id=uuid.uuid4(),
    )
    assert created and project.id == fixture_seed.PROJECT_ID
    assert memory_storage.writes == registration.await_count == 8
    bindings.assert_awaited_once_with(db)
    for call in registration.call_args_list:
        item = call.args[1]
        raw, _ = memory_storage.objects[binding.bucket, item.file_path]
        assert item.id is not None and call.args[0] is db
        assert call.kwargs["binding"] is binding
        assert (
            call.kwargs["sha256"]
            == item.content_hash
            == hashlib.sha256(raw).hexdigest()
        )
        assert call.kwargs["size_bytes"] == item.file_size == len(raw)


async def test_memory_transport_exercises_real_verified_original_read_and_closes(
    memory_storage,
):
    raw = b"complete pinned training original"
    bucket, key = storage_service.datasets_bucket, "training-reader/pinned-original"
    memory_storage.put_object(Bucket=bucket, Key=key, Body=raw)
    stage = await reader.capture(
        reader.ObjectSpec(
            memory_storage.meta.endpoint_url.rstrip("/"),
            bucket,
            key,
            hashlib.sha256(raw).hexdigest(),
            len(raw),
        ),
        max_bytes=len(raw),
        timeout_seconds=5,
    )
    try:
        assert stage.file.read() == raw
        assert stage.sha256 == hashlib.sha256(raw).hexdigest()
        assert memory_storage.condition_failures == 1
        assert memory_storage.closed_clients == 1
        assert memory_storage.streams and all(
            stream._raw_stream.closed for stream in memory_storage.streams
        )
    finally:
        stage.close()
    assert stage.file.closed


async def test_baseline_preparation_commits_sources_then_finalizes_under_project_lock(
    monkeypatch,
):
    events = []
    project = Project(id=uuid.uuid4(), owner_id=uuid.uuid4())
    prepared = SimpleNamespace(
        snapshot={"schema": 1, "classes": [], "samples": []},
        report={
            "ready": True,
            "digest": "a" * 64,
            "issues": [],
            "excluded": [],
            "summary": {},
        },
    )
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, None]),
        flush=AsyncMock(side_effect=lambda: events.append("flush")),
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
        execute=AsyncMock(side_effect=lambda statement: events.append("project_lock")),
        add=Mock(),
    )

    async def prepare(*args, **kwargs):
        assert events == ["flush", "commit"]
        assert kwargs == {"actor_id": project.owner_id, "trusted_actor": True}
        events.append("prepare")
        return prepared

    async def finalize(database, candidate):
        assert candidate is prepared and events[-1] == "project_lock"
        events.append("finalize")
        return prepared.snapshot, prepared.report

    monkeypatch.setattr(
        fixture_seed.training_datasets, "prepare_selection", prepare, raising=False
    )
    monkeypatch.setattr(
        fixture_seed.training_datasets, "finalize_selection", finalize, raising=False
    )
    monkeypatch.setattr(
        fixture_seed.training_dataset_storage,
        "allocate",
        AsyncMock(side_effect=lambda *args: events.append("allocate")),
    )
    membership = AsyncMock(side_effect=lambda *args, **kw: events.append("membership"))
    monkeypatch.setattr(
        fixture_seed.project_task_dataset, "ensure_membership", membership
    )
    monkeypatch.setattr(
        fixture_seed.training_datasets,
        "enqueue_dataset",
        AsyncMock(side_effect=lambda *args: events.append("enqueue")),
    )
    monkeypatch.setattr(
        fixture_seed.training_datasets,
        "check_prepared_clock",
        lambda candidate: events.append("clock"),
        raising=False,
    )
    version = await fixture_seed.prepare_baseline(db, project)
    assert (
        version.snapshot is prepared.snapshot and version.creator_id == project.owner_id
    )
    assert events == [
        "flush",
        "commit",
        "prepare",
        "project_lock",
        "finalize",
        "flush",
        "allocate",
        "membership",
        "enqueue",
        "clock",
    ]
    assert membership.call_args.kwargs == {"project_locked": True}
    db.commit.assert_awaited_once()


@pytest.mark.parametrize(
    "status", ["pending", "ready", "failed", "cancelled", "running"]
)
async def test_concurrent_baseline_publication_reuses_the_new_existing_attempt(
    monkeypatch, status
):
    project = Project(id=uuid.uuid4(), owner_id=uuid.uuid4())
    existing = SimpleNamespace(
        id=uuid.uuid4(), async_job_id=uuid.uuid4(), status=status
    )
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, existing]),
        flush=AsyncMock(),
        commit=AsyncMock(),
        execute=AsyncMock(),
        refresh=AsyncMock(),
    )
    prepare, finalize = AsyncMock(), AsyncMock()
    execution, enqueue, mark_failed = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(
        fixture_seed.training_datasets, "prepare_selection", prepare, raising=False
    )
    monkeypatch.setattr(
        fixture_seed.training_datasets, "finalize_selection", finalize, raising=False
    )
    monkeypatch.setattr(
        fixture_seed.training_datasets, "lock_dataset_execution", execution
    )
    monkeypatch.setattr(fixture_seed.training_datasets, "enqueue_dataset", enqueue)
    monkeypatch.setattr(fixture_seed.jobs, "mark_failed", mark_failed)
    assert await fixture_seed.prepare_baseline(db, project) is existing
    prepare.assert_awaited_once()
    finalize.assert_not_awaited()
    assert (
        execution.await_count
        == enqueue.await_count
        == int(status in {"failed", "cancelled", "running"})
    )
    assert mark_failed.await_count == int(status == "running")
