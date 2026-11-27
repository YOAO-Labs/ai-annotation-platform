"""Committed export facts in a migrated, fixture-owned PostgreSQL database.

The worker suites double physical transport, notifications and final authorization;
this fixture does not qualify a live worker or authorization boundary.
"""

import hashlib
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.dataset_assets import ensure_legacy_bindings, register_item_asset
from tests.factory import build_tool_bindings, create_user


@pytest.fixture
async def worker_db(managed_database):
    async with managed_database() as engine:
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            try:
                yield db
            finally:
                await db.rollback()


async def setup(db, media):
    """Commit the same business owners and verified original used by the worker."""
    suffix = uuid.uuid4().hex
    owner = await create_user(
        db, "project_admin", f"export-{suffix}@worker.test", "Export owner"
    )
    project = Project(
        id=uuid.uuid4(),
        display_id=f"export-{suffix[:13]}",
        name="Export worker",
        data_type=media,
        type_key="video-track" if media == "video" else "image-detection",
        type_label="Video" if media == "video" else "Image",
        owner_id=owner.id,
        tool_bindings=build_tool_bindings(["car"]),
        **(
            {
                "video_sampling": {"mode": "step", "frame_step": 3},
                "video_collaboration": {"enabled": False, "overlap_frames": 0},
            }
            if media == "video"
            else {}
        ),
    )
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"dataset-{suffix[:12]}",
        name="Export originals",
        data_type=media,
        created_by=owner.id,
        metadata_={},
    )
    db.add_all([project, dataset])
    await db.commit()

    filename = "clip.mp4" if media == "video" else "same.jpg"
    raw = b"saved"
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name=filename,
        file_path=f"export-worker/{suffix}/{filename}",
        file_type=media,
        file_size=len(raw),
        content_hash=hashlib.sha256(raw).hexdigest(),
        width=24,
        height=16,
        metadata_={"storage_relative_path": filename},
    )
    db.add(item)
    await db.flush()
    binding = await ensure_legacy_bindings(db)
    asset = await register_item_asset(
        db,
        item,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        binding=binding,
    )
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"task-{suffix[:15]}",
        file_name=filename,
        file_path=item.file_path,
        file_type=media,
    )
    db.add(task)
    await db.flush()
    geometry = {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}
    if media == "video":
        geometry = {
            "type": "video_track_bbox",
            "track_id": "trk_video_a",
            "keyframes": [
                {"frame_index": 0, "bbox": {"x": 0, "y": 0, "w": 0.2, "h": 0.2}},
                {"frame_index": 9, "bbox": {"x": 0.5, "y": 0, "w": 0.2, "h": 0.2}},
            ],
            "outside": [],
        }
    annotation = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        user_id=owner.id,
        source="manual",
        class_name="car",
        annotation_type="video_track_bbox" if media == "video" else "bbox",
        tool_unit_id="bbox",
        track_id="trk_video_a" if media == "video" else None,
        geometry=geometry,
        attributes={},
    )
    db.add(annotation)
    await db.commit()
    return project, dataset, item, asset, task, annotation
