"""The training fixture must produce real, stable, reviewed training input."""

import hashlib
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock
import zipfile

from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from PIL import Image
import pytest
from sqlalchemy import func, select

from app.db.models.annotation import Annotation
from app.db.models.dataset import DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.training_dataset import TrainingDatasetVersion
from app.services.storage import storage_service
from app.services.training_dataset_bridge import assign_splits
from app.workers import training_dataset as worker
from scripts import seed_online_training as fixture_seed
from scripts.seed_assets import SeedAssetError


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
            }

        def get_object(self, *, Bucket, Key, IfMatch=None):
            head = self.head_object(Bucket=Bucket, Key=Key)
            assert IfMatch is None or IfMatch == head["ETag"]
            payload, _ = self.objects[Bucket, Key]
            return {"Body": StreamingBody(io.BytesIO(payload), len(payload))}

        def upload_file(self, filename, bucket, key, ExtraArgs):
            self.put_object(
                Bucket=bucket, Key=key, Body=Path(filename).read_bytes(), **ExtraArgs
            )

    client = Client()
    monkeypatch.setattr(storage_service, "client", client)
    monkeypatch.setattr(worker, "notify_job_terminal", AsyncMock())
    return client


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
    version = await fixture_seed.prepare_baseline(db_session, project)
    await db_session.commit()

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
    assert (
        await db_session.scalar(
            select(func.count()).select_from(TrainingDatasetVersion)
        )
        == 1
    )


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
