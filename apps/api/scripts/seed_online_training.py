"""Seed reviewed COCO8 ground truth and a real training package for development."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.training_dataset import TrainingDatasetSelection
from app.services import project_task_dataset, training_datasets
from app.services.annotation_evidence import freeze_review_contributor_evidence
from app.services.storage import storage_service
from app.services.training_dataset_bridge import (
    assign_splits,
    content_digest,
    detection_labels,
)
from scripts.seed_assets import SeedAssetError
from scripts.seed_coco8 import COCO_NAMES

PROJECT_DISPLAY_ID = "P-COCO8-TRAIN"
DATASET_DISPLAY_ID = "DS-COCO8-TRAIN"
SEED_MARKER = "coco8-online-training"
PROJECT_ID = uuid.uuid5(uuid.NAMESPACE_URL, f"aap:seed:{SEED_MARKER}:project")
DATASET_ID = uuid.uuid5(uuid.NAMESPACE_URL, f"aap:seed:{SEED_MARKER}:dataset")
BASELINE_REQUEST_ID = uuid.uuid5(PROJECT_ID, "baseline")
# For the manifest-pinned COCO8 bytes, seed 4 preserves the original 4/4 split.
SPLIT_SEED = 4


def _read_samples(root: Path) -> list[dict]:
    samples = []
    for split in ("train", "val"):
        images = sorted((root / "images" / split).glob("*.jpg"))
        if len(images) != 4:
            raise SeedAssetError(f"COCO8 requires four {split} images")
        for image in images:
            labels = root / "labels" / split / f"{image.stem}.txt"
            annotations = []
            for line in labels.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    cls, cx, cy, width, height = line.split()
                    index = int(cls)
                    if not 0 <= index < len(COCO_NAMES):
                        raise ValueError("unknown COCO class")
                    cx, cy, width, height = map(float, (cx, cy, width, height))
                except ValueError as exc:
                    raise SeedAssetError(f"Invalid COCO8 label: {labels}") from exc
                annotations.append(
                    {
                        "tool_unit_id": "bbox",
                        "class_name": COCO_NAMES[index],
                        "geometry": {
                            "type": "bbox",
                            "x": cx - width / 2,
                            "y": cy - height / 2,
                            "w": width,
                            "h": height,
                        },
                    }
                )
            if not annotations:
                raise SeedAssetError(f"COCO8 ground truth is missing: {labels}")
            native = detection_labels(annotations, list(COCO_NAMES))
            payload = image.read_bytes()
            with Image.open(image) as decoded:
                decoded.load()
                width, height = decoded.size
            samples.append(
                {
                    "task_id": image.stem,
                    "path": image,
                    "split": split,
                    "payload": payload,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "width": width,
                    "height": height,
                    "annotations": annotations,
                    "labels": native,
                }
            )
    expected = {sample["task_id"]: sample["split"] for sample in samples}
    actual = assign_splits(samples, seed=SPLIT_SEED, validation_fraction=0.5)
    if actual != expected or len(expected) != 8:
        raise SeedAssetError(
            "COCO8 bytes do not match the pinned training/validation split"
        )
    return samples


async def seed_online_training(
    db: AsyncSession,
    *,
    fixture: Path,
    owner_id: uuid.UUID,
    annotator_id: uuid.UUID,
    reviewer_id: uuid.UUID,
) -> tuple[Project, bool]:
    """Create once; refuse unrelated fixed IDs and never overwrite user edits."""
    if settings.environment == "production":
        raise SeedAssetError("Online training fixtures cannot run in production")
    if annotator_id == reviewer_id:
        raise SeedAssetError(
            "The fixture requires separate annotator and reviewer users"
        )
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(SEED_MARKER, 0)))
    )
    project = await db.scalar(
        select(Project).where(Project.display_id == PROJECT_DISPLAY_ID)
    )
    dataset = await db.scalar(
        select(Dataset).where(Dataset.display_id == DATASET_DISPLAY_ID)
    )
    if project or dataset:
        if (
            project is None
            or dataset is None
            or project.id != PROJECT_ID
            or dataset.id != DATASET_ID
            or (dataset.metadata_ or {}).get("seed") != SEED_MARKER
        ):
            raise SeedAssetError(
                "Online training fixture IDs are occupied by unrelated data"
            )
        return project, False

    samples = _read_samples(fixture)
    project = Project(
        id=PROJECT_ID,
        display_id=PROJECT_DISPLAY_ID,
        name="COCO8 在线训练（dev）",
        type_key="image-det",
        type_label="图像目标检测",
        data_type="image",
        owner_id=owner_id,
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [
                    {"name": name, "order": i} for i, name in enumerate(COCO_NAMES)
                ],
            }
        },
        total_tasks=8,
        completed_tasks=8,
        annotation_guide="COCO8 在线训练开发夹具：8 张图片已导入原始检测标注并审核。可从训练数据选择初始版本，手动新建实验或配置自动训练。",
    )
    dataset = Dataset(
        id=DATASET_ID,
        display_id=DATASET_DISPLAY_ID,
        name="COCO8 在线训练（dev）",
        description="COCO8 原始 4 张训练图和 4 张验证图，供在线训练开发测试。",
        data_type="image",
        file_count=8,
        created_by=owner_id,
        metadata_={"seed": SEED_MARKER},
    )
    db.add_all([project, dataset])
    await db.flush()
    db.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
    now = datetime.now(timezone.utc)
    for sample in samples:
        key = f"{SEED_MARKER}/{dataset.id}/{sample['split']}/{sample['path'].name}"
        await asyncio.to_thread(
            storage_service.client.put_object,
            Bucket=storage_service.datasets_bucket,
            Key=key,
            Body=sample["payload"],
            ContentType="image/jpeg",
        )
        item = DatasetItem(
            dataset_id=dataset.id,
            file_name=sample["path"].name,
            file_path=key,
            file_type="image",
            file_size=len(sample["payload"]),
            content_hash=sample["sha256"],
            width=sample["width"],
            height=sample["height"],
            metadata_={"seed": SEED_MARKER, "coco8_split": sample["split"]},
        )
        db.add(item)
        await db.flush()
        task = Task(
            id=uuid.uuid5(PROJECT_ID, sample["task_id"]),
            project_id=project.id,
            dataset_item_id=item.id,
            display_id=f"T-COCO8-TRAIN-{sample['task_id']}",
            file_name=item.file_name,
            file_path=key,
            file_type="image",
            status="completed",
            is_labeled=True,
            total_annotations=len(sample["annotations"]),
            assignee_id=annotator_id,
            assigned_at=now - timedelta(minutes=3),
            submitted_at=now - timedelta(minutes=2),
            reviewer_id=reviewer_id,
            reviewer_claimed_at=now - timedelta(minutes=1),
            reviewed_at=now,
            review_round_id=uuid.uuid5(PROJECT_ID, f"review:{sample['task_id']}"),
            annotation_contributor_ids=[str(annotator_id)],
            first_review_eligible=None,
        )
        freeze_review_contributor_evidence(task, submitter_id=annotator_id)
        db.add(task)
        await db.flush()
        for annotation in sample["annotations"]:
            db.add(
                Annotation(
                    task_id=task.id,
                    project_id=project.id,
                    user_id=annotator_id,
                    source="external_import",
                    annotation_type="bbox",
                    ground_truth=True,
                    **annotation,
                )
            )
    await db.flush()
    return project, True


async def prepare_baseline(
    db: AsyncSession, project: Project
) -> TrainingDatasetVersion:
    """Freeze through the production service without starting any training run."""
    existing = await db.scalar(
        select(TrainingDatasetVersion).where(
            TrainingDatasetVersion.project_id == project.id,
            TrainingDatasetVersion.request_id == BASELINE_REQUEST_ID,
        )
    )
    if existing is not None:
        return existing
    selection = TrainingDatasetSelection(seed=SPLIT_SEED, validation_fraction=0.5)
    snapshot, report = await training_datasets.freeze_selection(
        db, project, selection, actor_id=project.owner_id
    )
    if not report["ready"]:
        raise SeedAssetError(
            f"Online training fixture preflight failed: {report['issues']}"
        )
    version = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=project.owner_id,
        request_id=BASELINE_REQUEST_ID,
        request_digest=content_digest(selection.canonical_payload()),
        name="COCO8 初始数据（4 train / 4 val）",
        snapshot=snapshot,
        snapshot_digest=report["digest"],
        preparation_report=training_datasets.frozen_preparation_report(
            report["digest"], report
        ),
    )
    db.add(version)
    await db.flush()
    await project_task_dataset.ensure_membership(db, version)
    await training_datasets.enqueue_dataset(db, version)
    return version
