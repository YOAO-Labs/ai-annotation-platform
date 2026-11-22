"""流式导出 DB 集成 + 对拍。

覆盖：
- iter_export_chunks 分块（chunk_size=2 跨多块）的 task 顺序 / annotation 集合 == _load_data 全量。
- build_export_zip 落盘产物正确（多目标 yolo+coco 镜像目录 + manifest + COCO 真值尺寸）；
  返回 (zip 路径, file_count, size_bytes) 且路径存在、size 一致。
"""

from __future__ import annotations

import json
import os
import uuid
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.exporting.service import ExportService
from app.services.exporting.packaging import build_export_zip

pytestmark = pytest.mark.asyncio


async def _seed(
    db: AsyncSession, owner_id: uuid.UUID, n: int = 5
) -> tuple[Project, list[Task]]:
    short = uuid.uuid4().hex[:6]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-ST{short}",
        name=f"stream {short}",
        type_key="image-det",
        type_label="测试",
        owner_id=owner_id,
        status="in_progress",
        tool_bindings={
            "bbox": {"enabled": True, "classes": [{"name": "car", "order": 0}]}
        },
    )
    db.add(project)
    await db.flush()

    ds = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-ST{short}",
        name=f"ds-{short}",
        data_type="image",
        created_by=owner_id,
    )
    db.add(ds)
    await db.flush()

    tasks: list[Task] = []
    for i in range(n):
        item = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=ds.id,
            file_name=f"img_{i}.jpg",
            file_path=f"mydataset/sub/img_{i}.jpg",
            file_type="image",
            width=800,
            height=600,
        )
        db.add(item)
        await db.flush()
        t = Task(
            id=uuid.uuid4(),
            project_id=project.id,
            dataset_item_id=item.id,
            display_id=f"T-ST{short}-{i}",
            file_name=f"img_{i}.jpg",
            file_path=f"mydataset/sub/img_{i}.jpg",
            file_type="image",
            status="pending",
            sequence_order=i,
        )
        db.add(t)
        tasks.append(t)
        await db.flush()
        db.add(
            Annotation(
                id=uuid.uuid4(),
                task_id=t.id,
                project_id=project.id,
                user_id=owner_id,
                source="manual",
                class_name="car",
                geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            )
        )
    await db.flush()
    return project, tasks


async def test_iter_export_chunks_matches_load_data(super_admin, db_session):
    """分块流式产出的 task 顺序与 annotation 总数应与 _load_data 全量一致。"""
    user, _ = super_admin
    project, _ = await _seed(db_session, user.id, n=5)
    svc = ExportService(db_session)
    _, full_tasks, full_anns = await svc._load_data(project.id)

    streamed_task_ids: list[uuid.UUID] = []
    streamed_ann_count = 0
    chunks = 0
    async for ts, ann_by_task, items in svc.iter_export_chunks(
        project.id, chunk_size=2
    ):
        chunks += 1
        streamed_task_ids.extend(t.id for t in ts)
        streamed_ann_count += sum(len(v) for v in ann_by_task.values())
        assert all(t.dataset_item_id in items for t in ts if t.dataset_item_id)

    assert chunks == 3
    assert streamed_task_ids == [t.id for t in full_tasks]
    assert streamed_ann_count == len(full_anns)


async def test_export_annotations_use_id_as_equal_timestamp_tiebreaker(
    super_admin, db_session
):
    """同一事务的 now() 时间戳相同时，全量与分块导出仍必须稳定同序。"""
    user, _ = super_admin
    project, tasks = await _seed(db_session, user.id, n=1)
    svc = ExportService(db_session)
    await db_session.execute(
        delete(Annotation).where(Annotation.project_id == project.id)
    )
    await db_session.flush()

    same_created_at = datetime(2026, 1, 1, tzinfo=UTC)
    expected_ids = [uuid.UUID(int=value) for value in range(1, 9)]
    for annotation_id in reversed(expected_ids):
        db_session.add(
            Annotation(
                id=annotation_id,
                task_id=tasks[0].id,
                project_id=project.id,
                user_id=user.id,
                source="manual",
                class_name="car",
                geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                created_at=same_created_at,
            )
        )
    await db_session.flush()
    await db_session.execute(text("SET LOCAL enable_indexscan = off"))
    await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))

    _, _, full_annotations = await svc._load_data(project.id)
    assert [annotation.id for annotation in full_annotations] == expected_ids

    streamed_ids: list[uuid.UUID] = []
    async for _, annotations_by_task, _ in svc.iter_export_chunks(
        project.id, chunk_size=1
    ):
        streamed_ids.extend(
            annotation.id for annotation in annotations_by_task[tasks[0].id]
        )
    assert streamed_ids == expected_ids


async def test_iter_export_chunks_skips_annotations_when_disabled(
    super_admin, db_session
):
    """with_annotations=False 跳过 annotation 查询（纯 COCO/manifest pass），仍水合 items。"""
    user, _ = super_admin
    project, _ = await _seed(db_session, user.id, n=3)
    svc = ExportService(db_session)
    async for ts, ann_by_task, items in svc.iter_export_chunks(
        project.id, chunk_size=10, with_annotations=False
    ):
        assert ann_by_task == {}
        assert all(t.dataset_item_id in items for t in ts if t.dataset_item_id)


async def test_build_export_zip_streamed_to_disk(super_admin, db_session, monkeypatch):
    """落盘多目标（yolo+coco）：返回磁盘路径 + size 一致；镜像目录 / manifest /
    COCO 真值像素坐标正确。"""
    monkeypatch.setattr(
        "app.services.exporting.packaging.storage_service.generate_download_url",
        lambda *args, **kwargs: "signed-url",
    )
    user, _ = super_admin
    project, _ = await _seed(db_session, user.id, n=5)

    zip_path, file_count, size_bytes = await build_export_zip(
        db_session,
        project.id,
        batch_id=None,
        targets=["yolo", "coco"],
        include_attributes=True,
        video_frame_mode="keyframes",
    )
    try:
        assert os.path.exists(zip_path)
        assert size_bytes == os.path.getsize(zip_path)
        assert file_count == 10

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            assert zf.read("classes.txt").decode() == "car"
            assert "images_manifest.json" in names
            assert "fetch_images.py" in names
            assert "yolo/data.yaml" in names
            assert "coco/annotations.json" in names
            yolo_labels = [
                n for n in names if n.startswith("yolo/") and n.endswith(".txt")
            ]
            assert len(yolo_labels) == 5
            assert (
                zf.read(yolo_labels[0]).decode()
                == "0 0.250000 0.400000 0.300000 0.400000"
            )

            manifest = json.loads(zf.read("images_manifest.json"))
            assert len(manifest["images"]) == 5
            assert manifest["images"][0]["rel_path"] == "sub/img_0.jpg"
            assert manifest["images"][0]["presigned_url"] == "signed-url"

            coco = json.loads(zf.read("coco/annotations.json"))
            assert coco["images"][0]["width"] == 800
            assert coco["images"][0]["height"] == 600
            assert coco["annotations"][0]["bbox"] == [80.0, 120.0, 240.0, 240.0]
    finally:
        try:
            os.unlink(zip_path)
        except OSError:
            pass
