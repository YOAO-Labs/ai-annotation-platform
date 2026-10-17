import json
import uuid
import zipfile
from io import BytesIO
from xml.etree import ElementTree

from sqlalchemy import event, func, select

from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.exporting.service import ExportService


def _tool_bindings() -> dict:
    return {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "car", "order": 0}],
            "attribute_schema": {
                "fields": [{"key": "color", "label": "Color", "type": "text"}]
            },
        }
    }


async def _create_orphan_cleanup_fixture(db_session, owner_id: uuid.UUID):
    project = Project(
        display_id=f"P-ORPH-{uuid.uuid4().hex[:6]}",
        name="Orphan cleanup",
        type_key="image-det",
        type_label="Image Detection",
        data_type="image",
        owner_id=owner_id,
        tool_bindings=_tool_bindings(),
        total_tasks=2,
    )
    db_session.add(project)
    await db_session.flush()

    task = Task(
        project_id=project.id,
        display_id=f"T-ORPH-{uuid.uuid4().hex[:6]}",
        file_name="image.jpg",
        file_path="dataset/image.jpg",
        file_type="image",
        total_annotations=3,
        is_labeled=True,
        status="in_progress",
        annotation_contributor_ids=None,
    )
    other_task = Task(
        project_id=project.id,
        display_id=f"T-ORPH-{uuid.uuid4().hex[:6]}",
        file_name="image-2.jpg",
        file_path="dataset/image-2.jpg",
        file_type="image",
        total_annotations=1,
        is_labeled=True,
        status="in_progress",
        annotation_contributor_ids=[],
    )
    db_session.add_all([task, other_task])
    await db_session.flush()

    valid = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=owner_id,
        source="manual",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        attributes={
            "color": "red",
            "legacy": "manual-stale",
            "_imported": True,
            "_mask_root": {"object_id": "mask-root-1"},
        },
    )
    ai_valid = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=owner_id,
        source="ai",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.2, "y": 0.3, "w": 0.4, "h": 0.3},
        attributes={
            "color": "blue",
            "legacy": "ai-stale",
            "_model_run": {"id": "run-1"},
        },
    )
    orphan_class = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=owner_id,
        source="manual",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="dog",
        geometry={"type": "bbox", "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2},
        attributes={"color": "brown", "_manual_note": "keep with soft-delete"},
    )
    other_orphan_class = Annotation(
        task_id=other_task.id,
        project_id=project.id,
        user_id=owner_id,
        source="manual",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="dog",
        geometry={"type": "bbox", "x": 0.6, "y": 0.6, "w": 0.1, "h": 0.1},
        attributes={"color": "gray", "_mask_root": {"object_id": "mask-root-2"}},
    )
    db_session.add_all([valid, ai_valid, orphan_class, other_orphan_class])
    await db_session.flush()
    return project, task, valid, ai_valid, orphan_class, other_task, other_orphan_class


async def _create_orphan_fixture(db_session, owner_id: uuid.UUID):
    project = Project(
        display_id=f"P-ORPH-{uuid.uuid4().hex[:6]}",
        name="Orphan cleanup",
        type_key="image-det",
        type_label="Image Detection",
        data_type="image",
        owner_id=owner_id,
        tool_bindings=_tool_bindings(),
        total_tasks=1,
    )
    db_session.add(project)
    await db_session.flush()

    task = Task(
        project_id=project.id,
        display_id=f"T-ORPH-{uuid.uuid4().hex[:6]}",
        file_name="image.jpg",
        file_path="dataset/image.jpg",
        file_type="image",
        total_annotations=2,
        is_labeled=True,
        status="in_progress",
    )
    db_session.add(task)
    await db_session.flush()

    valid = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=owner_id,
        source="manual",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
        attributes={"color": "red", "legacy": "stale", "_imported": True},
    )
    orphan_class = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=owner_id,
        source="manual",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="dog",
        geometry={"type": "bbox", "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2},
        attributes={"color": "brown"},
    )
    db_session.add_all([valid, orphan_class])
    await db_session.flush()
    return project, task, valid, orphan_class


async def test_class_usage_and_cleanup_orphans(
    test_engine,
    db_session,
    httpx_client,
    auth_headers,
    super_admin,
):
    user, _ = super_admin
    (
        project,
        task,
        valid,
        ai_valid,
        orphan_class,
        other_task,
        other_orphan_class,
    ) = await _create_orphan_cleanup_fixture(db_session, user.id)

    usage = (
        await httpx_client.get(
            f"/api/v1/projects/{project.id}/class-usage",
            headers=auth_headers,
        )
    ).json()
    assert usage["classes"] == {"car": 2, "dog": 2}
    assert usage["attributes"]["color"] == 4
    assert usage["attributes"]["legacy"] == 2

    dml_statements = []
    commit_events = []

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            dml_statements.append(statement)

    def record_commit(_session):
        commit_events.append(True)

    event.listen(test_engine.sync_engine, "before_cursor_execute", record_sql)
    event.listen(db_session.sync_session, "before_commit", record_commit)
    try:
        preview = (
            await httpx_client.post(
                f"/api/v1/projects/{project.id}/cleanup-orphans",
                headers=auth_headers,
            )
        ).json()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", record_sql)
        event.remove(db_session.sync_session, "before_commit", record_commit)
    assert dml_statements == []
    assert commit_events == []
    assert preview == {
        "orphan_annotations": 2,
        "orphan_attribute_keys": {"legacy": 2},
    }

    for annotation in (valid, ai_valid, orphan_class, other_orphan_class):
        await db_session.refresh(annotation)
    await db_session.refresh(task)
    await db_session.refresh(other_task)
    assert valid.attributes["legacy"] == "manual-stale"
    assert ai_valid.attributes["legacy"] == "ai-stale"
    assert orphan_class.is_active is True
    assert other_orphan_class.is_active is True
    assert task.total_annotations == 3
    assert task.annotation_contributor_ids is None
    assert other_task.total_annotations == 1
    assert other_task.annotation_contributor_ids == []
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "project.cleanup_annotation_orphans",
                AuditLog.target_id == str(project.id),
            )
        )
        or 0
    ) == 0

    result = (
        await httpx_client.post(
            f"/api/v1/projects/{project.id}/cleanup-orphans",
            headers=auth_headers,
            json={"dry_run": False},
        )
    ).json()
    assert result == preview

    for annotation in (valid, ai_valid, orphan_class, other_orphan_class):
        await db_session.refresh(annotation)
    await db_session.refresh(task)
    await db_session.refresh(other_task)
    assert valid.is_active is True
    assert valid.source == "manual"
    assert valid.geometry == {
        "type": "bbox",
        "x": 0.1,
        "y": 0.2,
        "w": 0.3,
        "h": 0.4,
    }
    assert valid.attributes == {
        "color": "red",
        "_imported": True,
        "_mask_root": {"object_id": "mask-root-1"},
    }
    assert ai_valid.is_active is True
    assert ai_valid.source == "ai"
    assert ai_valid.geometry == {
        "type": "bbox",
        "x": 0.2,
        "y": 0.3,
        "w": 0.4,
        "h": 0.3,
    }
    assert ai_valid.attributes == {
        "color": "blue",
        "_model_run": {"id": "run-1"},
    }
    assert orphan_class.is_active is False
    assert orphan_class.source == "manual"
    assert orphan_class.attributes == {
        "color": "brown",
        "_manual_note": "keep with soft-delete",
    }
    assert other_orphan_class.is_active is False
    assert other_orphan_class.attributes == {
        "color": "gray",
        "_mask_root": {"object_id": "mask-root-2"},
    }
    assert task.total_annotations == 2
    assert task.is_labeled is True
    assert task.annotation_contributor_ids is None
    assert other_task.total_annotations == 0
    assert other_task.is_labeled is False
    assert other_task.status == "pending"
    assert other_task.annotation_contributor_ids == [str(user.id)]
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "project.cleanup_annotation_orphans",
                AuditLog.target_id == str(project.id),
            )
        )
    ).scalar_one()
    assert audit.detail_json == {
        "orphan_annotations": 2,
        "orphan_attribute_keys": {"legacy": 2},
    }


async def test_source_less_cleanup_preserves_linked_tasks_and_recalculates_counters(
    db_session,
    httpx_client,
    auth_headers,
    super_admin,
):
    user, _ = super_admin
    project, orphan_task, _valid, _orphan_class = await _create_orphan_fixture(
        db_session,
        user.id,
    )
    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"B-ORPH-{uuid.uuid4().hex[:6]}",
        name="orphan cleanup",
        status="draft",
        assigned_user_ids=[],
    )
    db_session.add(batch)
    await db_session.flush()
    orphan_task.batch_id = batch.id

    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"D-ORPH-{uuid.uuid4().hex[:6]}",
        name="linked source",
        data_type="image",
        created_by=user.id,
    )
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="linked.jpg",
        file_path="dataset/linked.jpg",
        file_type="image",
    )
    db_session.add_all([dataset, item])
    await db_session.flush()
    db_session.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
    source_task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        batch_id=batch.id,
        display_id=f"T-SOURCE-{uuid.uuid4().hex[:6]}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type=item.file_type,
        status="completed",
    )
    db_session.add(source_task)
    project.total_tasks = 2
    project.completed_tasks = 1
    project.in_progress_tasks = 1
    batch.total_tasks = 2
    batch.completed_tasks = 1
    await db_session.flush()

    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/orphan-tasks/cleanup",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted_tasks": 1, "deleted_annotations": 2}
    assert (
        await db_session.scalar(select(Task.id).where(Task.id == orphan_task.id))
        is None
    )
    assert (
        await db_session.scalar(select(Task.id).where(Task.id == source_task.id))
        == source_task.id
    )

    await db_session.refresh(project)
    await db_session.refresh(batch)
    assert project.total_tasks == 1
    assert project.completed_tasks == 1
    assert project.review_tasks == 0
    assert project.in_progress_tasks == 0
    assert batch.total_tasks == 1
    assert batch.completed_tasks == 1
    assert batch.review_tasks == 0


async def test_export_skips_orphan_class_and_prunes_orphan_attributes(
    db_session,
    super_admin,
):
    user, _ = super_admin
    project, _task, _valid, _orphan_class = await _create_orphan_fixture(
        db_session,
        user.id,
    )
    svc = ExportService(db_session)

    coco = json.loads(await svc.export_coco(project.id))
    assert [ann["category_id"] for ann in coco["annotations"]] == [0]
    assert coco["annotations"][0]["attributes"] == {"color": "red"}

    yolo_zip = zipfile.ZipFile(BytesIO(await svc.export_yolo(project.id)))
    assert yolo_zip.read("classes.txt").decode() == "car"
    assert yolo_zip.read("labels/image.txt").decode().startswith("0 ")
    assert json.loads(yolo_zip.read("labels/image.attrs.json")) == {
        "attributes": [{"color": "red"}]
    }

    voc_zip = zipfile.ZipFile(BytesIO(await svc.export_voc(project.id)))
    voc_root = ElementTree.fromstring(
        voc_zip.read("Annotations/image.xml").decode(),
    )
    assert [node.text for node in voc_root.findall("./object/name")] == ["car"]
    assert [node.tag for node in voc_root.findall("./object/extra/*")] == ["color"]

    aap = json.loads(await svc.export_aap_json(project.id))
    annotations = aap["tasks"][0]["annotations"]
    assert [ann["class_name"] for ann in annotations] == ["car"]
    assert annotations[0]["attributes"] == {"color": "red"}

    active = (
        (
            await db_session.execute(
                select(Annotation).where(
                    Annotation.project_id == project.id,
                    Annotation.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    assert {ann.class_name for ann in active} == {"car", "dog"}
