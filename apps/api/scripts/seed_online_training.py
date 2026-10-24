"""Seed reviewed COCO8 ground truth and a real training package for development."""

from __future__ import annotations

import asyncio
import hashlib
import math
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
from app.db.models.task_lock import AnnotationDraft, TaskLock
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.training_dataset import TrainingDatasetSelection
from app.services import async_job as jobs, project_task_dataset, training_datasets
from app.services import training_dataset_storage
from app.services.annotation_evidence import freeze_review_contributor_evidence
from app.services.dataset_assets import ensure_legacy_bindings, register_item_asset
from app.services.storage import storage_service
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
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
SEGMENTATION_REQUEST_ID = uuid.uuid5(PROJECT_ID, "baseline:segment:instance-rle")
SEGMENTATION_MARKER = "coco8-seg-instance-rle"
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
    segmentation_fixture: Path | None = None,
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
        if segmentation_fixture is not None and not (dataset.metadata_ or {}).get(
            SEGMENTATION_MARKER
        ):
            await _add_segmentation(
                db,
                project,
                dataset,
                _read_samples(fixture),
                segmentation_fixture,
                annotator_id=annotator_id,
                reviewer_id=reviewer_id,
            )
        return project, False

    samples = _read_samples(fixture)
    binding = await ensure_legacy_bindings(db)
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
            Bucket=binding.bucket,
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
        # Only this successful write's bytes establish a new managed identity;
        # existing fixtures and arbitrary legacy paths are never adopted here.
        await register_item_asset(
            db,
            item,
            sha256=sample["sha256"],
            size_bytes=len(sample["payload"]),
            binding=binding,
        )
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
    if segmentation_fixture is not None:
        await _add_segmentation(
            db,
            project,
            dataset,
            samples,
            segmentation_fixture,
            annotator_id=annotator_id,
            reviewer_id=reviewer_id,
        )
    return project, True


def _read_segmentation(root: Path, samples: list[dict]) -> dict[str, list[dict]]:
    """Match exact source media before rasterizing upstream instance polygons."""
    from app.services.mask_formats.image_codecs import rasterize_coco_polygons
    from app.utils.raster_mask_rle import coco_rle_area

    expected = {f"{s['split']}/{s['path'].name}" for s in samples}
    actual = {
        str(p.relative_to(root / "images")) for p in (root / "images").glob("*/*.jpg")
    }
    if actual != expected:
        raise SeedAssetError("COCO8-Seg images must match the COCO8 4/4 partition")
    result = {}
    for sample in samples:
        relative = Path(sample["split"]) / sample["path"].name
        if (
            hashlib.sha256((root / "images" / relative).read_bytes()).hexdigest()
            != sample["sha256"]
        ):
            raise SeedAssetError(
                f"COCO8-Seg image differs from detection source: {relative}"
            )
        label = root / "labels" / relative.with_suffix(".txt")
        rows = []
        for ordinal, line in enumerate(label.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                values = line.split()
                index = int(values[0])
                points = list(map(float, values[1:]))
                if (
                    not 0 <= index < len(COCO_NAMES)
                    or not 6 <= len(points) <= 1024
                    or len(points) % 2
                ):
                    raise ValueError("invalid class or polygon vertex count")
                if any(not math.isfinite(p) or not 0 <= p <= 1 for p in points):
                    raise ValueError(
                        "polygon coordinates must be finite and normalized"
                    )
                pixel_points = [
                    p * (sample["width"] if i % 2 == 0 else sample["height"])
                    for i, p in enumerate(points)
                ]
                rle = rasterize_coco_polygons(
                    [pixel_points], width=sample["width"], height=sample["height"]
                )
                if coco_rle_area(rle) <= 0:
                    raise ValueError("empty instance")
            except (ValueError, IndexError) as exc:
                raise SeedAssetError(
                    f"Invalid COCO8-Seg instance: {label}:{ordinal + 1}"
                ) from exc
            rows.append(
                {"ordinal": ordinal, "class_name": COCO_NAMES[index], "rle": rle}
            )
        if not rows:
            raise SeedAssetError(f"COCO8-Seg ground truth is missing: {label}")
        result[sample["task_id"]] = rows
    return result


async def _add_segmentation(
    db, project, dataset, samples, root, *, annotator_id, reviewer_id
):
    """Extend an untouched fixture once, keeping all detection history immutable."""
    from app.services.raster_mask_storage import (
        assert_raster_mask_write_enabled,
        prepare_mask_payload_for_write,
        store_coco_rle,
    )

    masks = _read_segmentation(root, samples)
    classes = [{"name": name, "order": i} for i, name in enumerate(COCO_NAMES)]
    region = (project.tool_bindings or {}).get("region")
    if region is not None and region.get("classes") != classes:
        raise SeedAssetError(
            "COCO8 region classes were customized; refusing to overwrite them"
        )
    tasks = list(
        (
            await db.scalars(
                select(Task)
                .where(Task.project_id == project.id)
                .order_by(Task.id)
                .with_for_update()
            )
        ).all()
    )
    by_id = {task.id: task for task in tasks}
    expected_ids = {uuid.uuid5(PROJECT_ID, sample["task_id"]) for sample in samples}
    if not expected_ids <= by_id.keys():
        raise SeedAssetError(
            "COCO8 fixture tasks are missing; refusing to recreate user data"
        )
    if await db.scalar(
        select(func.count())
        .select_from(Annotation)
        .where(Annotation.project_id == project.id, Annotation.tool_unit_id == "region")
    ):
        raise SeedAssetError(
            "COCO8 already has region annotations; refusing to merge fixture truth"
        )
    if await db.scalar(
        select(func.count())
        .select_from(TaskLock)
        .where(
            TaskLock.task_id.in_(expected_ids),
            TaskLock.expire_at > datetime.now(timezone.utc),
        )
    ) or await db.scalar(
        select(func.count())
        .select_from(AnnotationDraft)
        .where(AnnotationDraft.task_id.in_(expected_ids))
    ):
        raise SeedAssetError(
            "COCO8 has editing locks or drafts; finish editing before extending the fixture"
        )
    for sample in samples:
        task = by_id[uuid.uuid5(PROJECT_ID, sample["task_id"])]
        item = await db.get(DatasetItem, task.dataset_item_id)
        annotations = list(
            (
                await db.scalars(
                    select(Annotation).where(Annotation.task_id == task.id)
                )
            ).all()
        )
        actual = [
            {
                "tool_unit_id": a.tool_unit_id,
                "class_name": a.class_name,
                "geometry": a.geometry,
            }
            for a in annotations
        ]
        if (
            task.status != "completed"
            or not task.reviewed_at
            or task.assignee_id != annotator_id
            or task.reviewer_id != reviewer_id
            or item is None
            or item.dataset_id != dataset.id
            or item.content_hash != sample["sha256"]
            or (item.width, item.height) != (sample["width"], sample["height"])
            or any(
                a.version != 1 or a.source != "external_import" or not a.is_active
                for a in annotations
            )
            or sorted(content_digest(a) for a in actual)
            != sorted(content_digest(a) for a in sample["annotations"])
        ):
            raise SeedAssetError(
                f"COCO8 task {task.display_id} was edited; refusing to change its review evidence"
            )
    project.tool_bindings = {
        **project.tool_bindings,
        "region": {**(region or {}), "enabled": True, "classes": classes},
    }
    assert_raster_mask_write_enabled(project)
    await db.flush()
    for sample in samples:
        task = by_id[uuid.uuid5(PROJECT_ID, sample["task_id"])]
        geometries = []
        for instance in masks[sample["task_id"]]:
            reference = await store_coco_rle(instance["rle"])
            geometry = {"type": "raster_mask", "mask": reference}
            geometries.append(geometry)
            db.add(
                Annotation(
                    id=uuid.uuid5(
                        task.id, f"{SEGMENTATION_MARKER}:{instance['ordinal']}"
                    ),
                    task_id=task.id,
                    project_id=project.id,
                    user_id=annotator_id,
                    source="external_import",
                    annotation_type="raster_mask",
                    tool_unit_id="region",
                    class_name=instance["class_name"],
                    geometry=geometry,
                    ground_truth=True,
                )
            )
        await prepare_mask_payload_for_write(db, task, geometries)
        task.total_annotations += len(geometries)
        task.reviewed_at = datetime.now(timezone.utc)
        task.review_round_id = uuid.uuid5(task.id, SEGMENTATION_MARKER)
        freeze_review_contributor_evidence(task, submitter_id=annotator_id)
    dataset.metadata_ = {
        **dataset.metadata_,
        SEGMENTATION_MARKER: {
            "source": "COCO8-Seg",
            "rasterizer": "pycocotools-coco-polygons",
            "instances": sum(len(rows) for rows in masks.values()),
            "labels_digest": content_digest(
                {
                    key: [
                        {"class_name": row["class_name"], "rle": row["rle"]}
                        for row in rows
                    ]
                    for key, rows in masks.items()
                }
            ),
        },
    }
    await db.flush()


async def prepare_baseline(
    db: AsyncSession, project: Project, *, task: str = "detect"
) -> TrainingDatasetVersion:
    """Freeze through the production service without starting any training run."""
    if task not in {"detect", "segment"}:
        raise SeedAssetError("Unsupported COCO8 training task")
    request_id = SEGMENTATION_REQUEST_ID if task == "segment" else BASELINE_REQUEST_ID
    existing = await db.scalar(
        select(TrainingDatasetVersion).where(
            TrainingDatasetVersion.project_id == project.id,
            TrainingDatasetVersion.request_id == request_id,
        )
    )
    if existing is not None:
        return await _reuse_baseline(db, existing)
    selection = TrainingDatasetSelection(
        seed=SPLIT_SEED,
        validation_fraction=0.5,
        task=task,
        **({"bridge": "ultralytics-instance-rle-1"} if task == "segment" else {}),
    )
    # The owned preparation session must observe all newly seeded source rows,
    # and physical reads must not retain the seed transaction's locks.
    project_id, actor_id = project.id, project.owner_id
    await db.flush()
    await db.commit()
    prepared = await training_datasets.prepare_selection(
        db, project, selection, actor_id=actor_id, trusted_actor=True
    )
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project_id), 0)))
    )
    # A concurrent seed may have published the fixed request while preparing.
    existing = await db.scalar(
        select(TrainingDatasetVersion).where(
            TrainingDatasetVersion.project_id == project_id,
            TrainingDatasetVersion.request_id == request_id,
        )
    )
    if existing is not None:
        return await _reuse_baseline(db, existing)
    snapshot, report = await training_datasets.finalize_selection(db, prepared)
    if not report["ready"]:
        raise SeedAssetError(
            f"Online training fixture preflight failed: {report['issues']}"
        )
    version = TrainingDatasetVersion(
        project_id=project_id,
        creator_id=actor_id,
        request_id=request_id,
        request_digest=content_digest(selection.canonical_payload()),
        name=(
            "COCO8 实例分割初始数据（原生 RLE · 4 train / 4 val）"
            if task == "segment"
            else "COCO8 初始数据（4 train / 4 val）"
        ),
        snapshot=snapshot,
        snapshot_digest=report["digest"],
        preparation_report=training_datasets.frozen_preparation_report(
            report["digest"], report
        ),
    )
    db.add(version)
    await db.flush()
    await training_dataset_storage.allocate(db, version)
    await project_task_dataset.ensure_membership(db, version, project_locked=True)
    await training_datasets.enqueue_dataset(db, version)
    training_datasets.check_prepared_clock(prepared)
    return version


async def _reuse_baseline(db, existing):
    if existing.status in {"failed", "cancelled", "running"}:
        try:
            await training_datasets.lock_dataset_execution(db, existing.id)
        except TrainingDatasetError as exc:
            raise SeedAssetError(str(exc)) from exc
        await db.refresh(existing, with_for_update=True)
        # A live builder holds this execution lock until its writes finish.
        # A running version without the lock belongs to an interrupted build.
        if existing.status == "running":
            await jobs.mark_failed(
                db, existing.async_job_id, error="种子构建已中断，重新构建同一快照"
            )
        if existing.status in {"failed", "cancelled", "running"}:
            await training_datasets.enqueue_dataset(db, existing)
    return existing
