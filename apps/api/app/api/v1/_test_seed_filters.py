"""Dedicated disposable fixtures for the filtering acceptance matrix.

This module is imported only by the guarded ``_test_seed`` route.  It deliberately
constructs the expected records and returns their IDs; tests never derive expected
membership by calling the filtering implementation under test.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession


# Owned-namespace display ids for every fixed identifier below. Bases keep the
# legacy literal so `build_filtering_seed` stays readable; owned values are
# sized to the column limits (projects/datasets/scenes/templates/bugs ≤ 20,
# batches/tasks ≤ 30) and stay exact-matchable for owned cleanup.
_FILTER_OWNED_IDS: dict[str, str] = {
    "P-E2E-FILTER-IMAGE": "P-FI-I-{ns}",
    "P-E2E-FILTER-VIDEO": "P-FI-V-{ns}",
    "P-E2E-FILTER-PAGE": "P-FI-P-{ns}",
    "P-E2E-FILTER-LIDAR": "P-FI-L-{ns}",
    "P-E2E-FILTER-OPSA": "P-FI-OA-{ns}",
    "P-E2E-FILTER-OPSB": "P-FI-OB-{ns}",
    "DS-E2E-FILTER-I": "DS-FI-I-{ns}",
    "DS-E2E-FILTER-V": "DS-FI-V-{ns}",
    "DS-E2E-FILTER-P": "DS-FI-P-{ns}",
    "DS-E2E-FILTER-L": "DS-FI-L-{ns}",
    "DS-E2E-FILTER-OA": "DS-OA-{ns}",
    "DS-E2E-FILTER-OB": "DS-OB-{ns}",
    "B-E2E-FILTER-I-V": "B-FI-I-V-{ns}",
    "B-E2E-FILTER-I-H": "B-FI-I-H-{ns}",
    "B-E2E-FILTER-V": "B-FI-V-{ns}",
    "B-E2E-FILTER-P": "B-FI-P-{ns}",
    "B-E2E-FILTER-L-V": "B-FI-L-V-{ns}",
    "B-E2E-FILTER-L-H": "B-FI-L-H-{ns}",
    "T-E2E-FILTER-I-CROSS": "T-FI-I-CROSS-{ns}",
    "T-E2E-FILTER-I-SAME": "T-FI-I-SAME-{ns}",
    "T-E2E-FILTER-I-MISSING": "T-FI-I-MISSING-{ns}",
    "T-E2E-FILTER-I-PRESENT": "T-FI-I-PRESENT-{ns}",
    "T-E2E-FILTER-I-INELIGIBLE": "T-FI-I-INELIGIBLE-{ns}",
    "T-E2E-FILTER-I-HIDDEN": "T-FI-I-HIDDEN-{ns}",
    "T-E2E-FILTER-PAGE": "T-FI-P-{ns}",
    "SCN-E2E-FILTER-L": "SC-FI-L-{ns}",
    "TPL-E2E-FILTER-A": "TPLFI-A-{ns}",
    "TPL-E2E-FILTER-B": "TPLFI-B-{ns}",
    "BUG-E2E-FILTER-A": "BUGFI-A-{ns}",
    "BUG-E2E-FILTER-B": "BUGFI-B-{ns}",
}

# Parametric task ids: `{0}` is the numeric suffix, `{ns}` the namespace.
_FILTER_OWNED_PARAMETRIC: dict[str, str] = {
    "T-E2E-FILTER-V-{0}": "T-FI-V-{0}-{ns}",
    "T-E2E-FILTER-L-{0}": "T-FI-L-{0}-{ns}",
}


@dataclass(frozen=True)
class _FilterIds:
    """Namespace-aware display-id / email / storage resolver.

    ``namespace=None`` reproduces the legacy shared-namespace fixture exactly;
    a namespace maps every identifier onto that namespace's exact owned values
    so ``owned-cleanup`` can converge the fixture without touching neighbours.
    """

    namespace: str | None

    def display(self, base: str, *args: int) -> str:
        if self.namespace is None:
            return base.format(*args) if args else base
        template = _FILTER_OWNED_PARAMETRIC.get(base) or _FILTER_OWNED_IDS[base]
        return template.format(*args, ns=self.namespace)

    @property
    def storage_prefix(self) -> str:
        if self.namespace is None:
            return "e2e/filtering/"
        return f"e2e/owned/{self.namespace}/filtering/"

    def user_email(self, local: str) -> str:
        if self.namespace is None:
            return f"{local}@e2e.test"
        return f"{local}-{self.namespace}@e2e.test"

    def invitation_email(self, local: str) -> str:
        if self.namespace is None:
            return f"{local}@example.test"
        return f"{local}-{self.namespace}@example.test"

    @property
    def invitation_search_key(self) -> str:
        if self.namespace is None:
            return "filter-pending"
        return f"filter-pending-{self.namespace}"

    def audit_scope(self, scope: str) -> str:
        if self.namespace is None:
            return scope
        return f"{scope}-{self.namespace}"

    def job_prompt(self, base: str) -> str:
        if self.namespace is None:
            return base
        return f"{base} {self.namespace}"

    def display_name(self, base: str) -> str:
        # Token-prefixed, so a namespace-unique search key (`[ns] Prefix`)
        # stays a substring of every derived display name.
        if self.namespace is None:
            return base
        return f"[{self.namespace}] {base}"

    @property
    def namespace_token(self) -> str:
        return self.namespace or ""


def _filter_audit_detail(ids: _FilterIds, scope: str) -> dict[str, Any]:
    """Audit detail for filtering fixture rows; namespaced rows stay namespaced."""
    detail: dict[str, Any] = {
        "fixture": "filtering",
        "scope": ids.audit_scope(scope),
    }
    if ids.namespace is not None:
        detail["namespace"] = ids.namespace
    return detail


class FilteringImageManifest(BaseModel):
    project_id: str
    task_ids: dict[str, str]
    object_ids: dict[str, list[str]]
    batch_ids: dict[str, str]
    schema_: dict[str, str] = Field(alias="schema")
    expected: dict[str, list[str]]
    saved_view_ids: dict[str, str]

    model_config = ConfigDict(populate_by_name=True)


class FilteringVideoManifest(BaseModel):
    project_id: str
    task_ids: dict[str, str]
    candidate_ids: dict[str, str]
    tracker_job_ids: dict[str, str]
    expected: dict[str, list[str]]


class FilteringPagingManifest(BaseModel):
    project_id: str
    task_id: str
    object_ids: list[str]
    expected_page_one_object_ids: list[str]
    expected_page_two_object_ids: list[str]


class FilteringLidarManifest(BaseModel):
    project_id: str
    scene_id: str
    task_ids: list[str]
    track_refs: list[str]
    hidden_track_ref: str
    expected_visible_track_refs: list[str]
    saved_view_ids: dict[str, str]


class FilteringOperationsManifest(BaseModel):
    project_ids: list[str]
    dataset_ids: list[str]
    template_ids: list[str]
    user_ids: list[str]
    user_emails: list[str]
    invitation_ids: list[str]
    job_ids: list[str]
    bug_ids: list[str]
    audit_ids: list[str]
    # Namespace-unique display values, so global list endpoints can be
    # searched without cross-fixture contamination between namespaces.
    display_names: dict[str, str]
    invitation_emails: dict[str, str]
    search_keys: dict[str, str]
    audit_scopes: dict[str, str]


class FilteringSeedManifest(BaseModel):
    users: dict[str, str]
    user_emails: dict[str, str]
    image: FilteringImageManifest
    video: FilteringVideoManifest
    paging: FilteringPagingManifest
    lidar: FilteringLidarManifest
    operations: FilteringOperationsManifest


def _now(offset: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(microseconds=offset)


def _image_binding(*, include_obsolete: bool = True) -> dict[str, Any]:
    fields: list[dict[str, Any]] = [
        {
            "key": "color",
            "label": "Color",
            "type": "select",
            "required": True,
            "applies_to": ["car"],
            "options": [
                {"value": "red", "label": "Red"},
                {"value": "blue", "label": "Blue"},
            ],
        },
        {
            "key": "weather",
            "label": "Weather",
            "type": "select",
            "options": [
                {"value": "sunny", "label": "Sunny"},
                {"value": "rain", "label": "Rain"},
            ],
        },
        {
            "key": "finish",
            "label": "Finish",
            "type": "select",
            "required": True,
            "applies_to": ["car"],
            "visible_if": {"key": "weather", "equals": "rain"},
            "options": [
                {"value": "matte", "label": "Matte"},
                {"value": "gloss", "label": "Gloss"},
            ],
        },
    ]
    if include_obsolete:
        fields.append({"key": "obsolete", "label": "Obsolete", "type": "text"})
    return {
        "bbox": {
            "enabled": True,
            "classes": [
                {"name": "car", "order": 0},
                {"name": "person", "order": 1},
            ],
            "attribute_schema": {"fields": fields},
        }
    }


def _video_binding() -> dict[str, Any]:
    return {
        "bbox": {
            "enabled": True,
            "classes": [{"name": "car", "order": 0}],
            "video_modes": {"track": True},
            "attribute_schema": {"fields": []},
        }
    }


async def _get_users(db: AsyncSession, ids: _FilterIds) -> dict[str, Any]:
    """Resolve the fixture's admin/annotator/reviewer accounts.

    Legacy shared mode keeps its fail-closed contract (accounts must already
    exist from ``seed/reset``); owned mode reuses the namespace's accounts and
    creates them when absent so ``seed/filtering`` can run standalone.
    """
    from sqlalchemy import select

    from app.db.models.user import User

    emails = [ids.user_email("admin"), ids.user_email("anno"), ids.user_email("rev")]
    rows = (
        (await db.execute(select(User).where(User.email.in_(emails)))).scalars().all()
    )
    users = {
        row.email.split("@", 1)[0].removesuffix(f"-{ids.namespace}"): row
        for row in rows
    }
    if set(users) != {"admin", "anno", "rev"}:
        from tests.factory import create_user

        if ids.namespace is None:
            raise RuntimeError(
                "filtering fixture requires seed/reset users; POST /seed/reset first"
            )
        roles = {"admin": "super_admin", "anno": "employee", "rev": "employee"}
        for key in {"admin", "anno", "rev"} - set(users):
            users[key] = await create_user(
                db, roles[key], ids.user_email(key), f"E2E {key}"
            )
    return users


async def _create_project(
    db: AsyncSession,
    *,
    owner_id: UUID,
    display_id: str,
    name: str,
    type_key: str,
    type_label: str,
    data_type: str,
    tool_bindings: dict[str, Any],
    scene_mode: bool = False,
):
    from app.db.models.project import Project

    project = Project(
        display_id=display_id,
        name=name,
        type_key=type_key,
        type_label=type_label,
        data_type=data_type,
        owner_id=owner_id,
        tool_bindings=tool_bindings,
        scene_mode=scene_mode,
        ai_enabled=False,
    )
    db.add(project)
    await db.flush()
    return project


async def _create_dataset(
    db: AsyncSession,
    *,
    project_id: UUID,
    created_by: UUID,
    display_id: str,
    name: str,
    data_type: str,
    file_count: int = 0,
    is_temporal: bool = False,
):
    from app.db.models.dataset import Dataset, ProjectDataset

    dataset = Dataset(
        display_id=display_id,
        name=name,
        data_type=data_type,
        file_count=file_count,
        is_temporal=is_temporal,
        created_by=created_by,
    )
    db.add(dataset)
    await db.flush()
    db.add(ProjectDataset(project_id=project_id, dataset_id=dataset.id))
    await db.flush()
    return dataset


async def _create_batch(
    db: AsyncSession,
    *,
    project_id: UUID,
    created_by: UUID,
    display_id: str,
    name: str,
    status: str,
    annotator_id: UUID | None,
    reviewer_id: UUID | None,
):
    from app.db.models.task_batch import TaskBatch

    batch = TaskBatch(
        project_id=project_id,
        display_id=display_id,
        name=name,
        status=status,
        annotator_id=annotator_id,
        reviewer_id=reviewer_id,
        assigned_user_ids=[
            str(value) for value in (annotator_id, reviewer_id) if value
        ],
        created_by=created_by,
    )
    db.add(batch)
    await db.flush()
    return batch


async def _create_item(
    db: AsyncSession,
    *,
    dataset_id: UUID,
    file_name: str,
    file_path: str,
    file_type: str,
    width: int | None = 64,
    height: int | None = 48,
    scene_id: UUID | None = None,
    frame_index: int | None = None,
    metadata: dict[str, Any] | None = None,
):
    from app.db.models.dataset import DatasetItem

    item = DatasetItem(
        dataset_id=dataset_id,
        file_name=file_name,
        file_path=file_path,
        file_type=file_type,
        file_size=0,
        width=width,
        height=height,
        scene_id=scene_id,
        frame_index=frame_index,
        metadata_=metadata or {},
    )
    db.add(item)
    await db.flush()
    return item


async def _create_task(
    db: AsyncSession,
    *,
    project_id: UUID,
    batch_id: UUID,
    item_id: UUID,
    display_id: str,
    file_name: str,
    file_path: str,
    file_type: str,
    sequence_order: int,
):
    from app.db.models.task import Task

    task = Task(
        project_id=project_id,
        batch_id=batch_id,
        dataset_item_id=item_id,
        display_id=display_id,
        file_name=file_name,
        file_path=file_path,
        file_type=file_type,
        sequence_order=sequence_order,
        status="pending",
        created_at=_now(sequence_order),
        updated_at=_now(sequence_order),
    )
    db.add(task)
    await db.flush()
    return task


def _bbox_annotation(
    *,
    task_id: UUID,
    project_id: UUID,
    user_id: UUID,
    class_name: str,
    attributes: dict[str, Any],
    source: str = "manual",
    active: bool = True,
    cancelled: bool = False,
    track_id: str | None = None,
    annotation_type: str = "bbox",
    tool_unit_id: str = "bbox",
    frame_index: int | None = None,
    scene_track_id: UUID | None = None,
):
    from app.db.models.annotation import Annotation

    geometry: dict[str, Any] = {
        "type": annotation_type,
        "x": 0.1,
        "y": 0.1,
        "w": 0.2,
        "h": 0.2,
    }
    if track_id:
        geometry["track_id"] = track_id
    if frame_index is not None:
        geometry["frame_index"] = frame_index
    if annotation_type == "box_3d":
        geometry = {
            "type": "box_3d",
            "center": [1.0, 0.0, 1.0],
            "size": [2.0, 1.0, 1.5],
            "rotation": [0.0, 0.0, 0.0],
            "frame_index": frame_index,
        }
    return Annotation(
        task_id=task_id,
        project_id=project_id,
        user_id=user_id,
        source=source,
        annotation_type=annotation_type,
        tool_unit_id=tool_unit_id,
        class_name=class_name,
        geometry=geometry,
        attributes=attributes,
        track_id=track_id,
        scene_track_id=scene_track_id,
        temporal_role="sample",
        is_active=active,
        was_cancelled=cancelled,
    )


async def _seed_image_semantics(
    db: AsyncSession, ids: _FilterIds, users: dict[str, Any], image_key: str
) -> FilteringImageManifest:
    from app.db.models.project_member import ProjectMember
    from app.db.models.project_task_view import ProjectTaskView

    admin, annotator, reviewer = users["admin"], users["anno"], users["rev"]
    project = await _create_project(
        db,
        owner_id=admin.id,
        display_id=ids.display("P-E2E-FILTER-IMAGE"),
        name="Filter Image Semantics",
        type_key="image-det",
        type_label="Image detection",
        data_type="image",
        tool_bindings=_image_binding(),
    )
    db.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=annotator.id,
                role="annotator",
                assigned_by=admin.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=reviewer.id,
                role="reviewer",
                assigned_by=admin.id,
            ),
        ]
    )
    dataset = await _create_dataset(
        db,
        project_id=project.id,
        created_by=admin.id,
        display_id=ids.display("DS-E2E-FILTER-I"),
        name="Filter Image Dataset",
        data_type="image",
        file_count=6,
    )
    visible_batch = await _create_batch(
        db,
        project_id=project.id,
        created_by=admin.id,
        display_id=ids.display("B-E2E-FILTER-I-V"),
        name="Filter Image Visible",
        status="annotating",
        annotator_id=annotator.id,
        reviewer_id=reviewer.id,
    )
    hidden_batch = await _create_batch(
        db,
        project_id=project.id,
        created_by=admin.id,
        display_id=ids.display("B-E2E-FILTER-I-H"),
        name="Filter Image Hidden",
        status="draft",
        annotator_id=reviewer.id,
        reviewer_id=reviewer.id,
    )
    task_specs = {
        "cross_object": (
            ids.display("T-E2E-FILTER-I-CROSS"),
            visible_batch,
            [
                ("car", {"color": "red"}),
                ("person", {"color": "blue"}),
            ],
        ),
        "same_object": (
            ids.display("T-E2E-FILTER-I-SAME"),
            visible_batch,
            [
                ("car", {"color": "blue"}),
            ],
        ),
        "required_missing": (
            ids.display("T-E2E-FILTER-I-MISSING"),
            visible_batch,
            [
                ("car", {"weather": "sunny"}),
            ],
        ),
        "required_present": (
            ids.display("T-E2E-FILTER-I-PRESENT"),
            visible_batch,
            [
                ("car", {"color": "red", "weather": "rain", "finish": "matte"}),
            ],
        ),
        "ineligible_missing": (
            ids.display("T-E2E-FILTER-I-INELIGIBLE"),
            visible_batch,
            [
                ("person", {"weather": "sunny"}),
            ],
        ),
        "hidden": (
            ids.display("T-E2E-FILTER-I-HIDDEN"),
            hidden_batch,
            [
                ("car", {"color": "blue"}),
            ],
        ),
    }
    tasks: dict[str, Any] = {}
    objects: dict[str, list[str]] = {}
    for index, (key, (display_id, batch, specs)) in enumerate(task_specs.items()):
        item = await _create_item(
            db,
            dataset_id=dataset.id,
            file_name=f"{key}.svg",
            file_path=image_key,
            file_type="image",
        )
        task = await _create_task(
            db,
            project_id=project.id,
            batch_id=batch.id,
            item_id=item.id,
            display_id=display_id,
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="image",
            sequence_order=index,
        )
        tasks[key] = task
        rows = [
            _bbox_annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=annotator.id,
                class_name=class_name,
                attributes=attributes,
            )
            for class_name, attributes in specs
        ]
        if key == "cross_object":
            rows.extend(
                [
                    _bbox_annotation(
                        task_id=task.id,
                        project_id=project.id,
                        user_id=annotator.id,
                        class_name="car",
                        attributes={"color": "blue"},
                        active=False,
                    ),
                    _bbox_annotation(
                        task_id=task.id,
                        project_id=project.id,
                        user_id=annotator.id,
                        class_name="car",
                        attributes={"color": "blue"},
                        cancelled=True,
                    ),
                ]
            )
        db.add_all(rows)
        await db.flush()
        task.total_annotations = sum(
            1 for row in rows if row.is_active and not row.was_cancelled
        )
        objects[key] = [str(row.id) for row in rows]
    visible_batch.total_tasks = len(tasks) - 1
    hidden_batch.total_tasks = 1
    await db.flush()

    root_or = ProjectTaskView(
        project_id=project.id,
        owner_id=admin.id,
        name="Filter Root OR",
        visibility="project",
        entity_scope="tasks",
        filter_json={
            "op": "or",
            "rules": [
                {"field": "task.status", "op": "eq", "value": "pending"},
                {"field": "annotation.class_name", "op": "eq", "value": "car"},
            ],
        },
        sort_json=[{"field": "task.created_at", "direction": "asc"}],
        columns_json=["display_id", "status"],
    )
    required_nested = ProjectTaskView(
        project_id=project.id,
        owner_id=admin.id,
        name="Filter Required Nested",
        visibility="private",
        entity_scope="tasks",
        filter_json={
            "op": "or",
            "rules": [
                {
                    "op": "and",
                    "rules": [
                        {"field": "annotation.class_name", "op": "eq", "value": "car"},
                        {"field": "annotation.attribute.bbox.color", "op": "missing"},
                    ],
                }
            ],
        },
        sort_json=[],
        columns_json=["display_id"],
    )
    object_filter = ProjectTaskView(
        project_id=project.id,
        owner_id=admin.id,
        name="Filter Object Color",
        visibility="project",
        entity_scope="objects",
        filter_json={
            "op": "and",
            "rules": [
                {"field": "annotation.class_name", "op": "eq", "value": "car"},
                {
                    "field": "annotation.attribute.bbox.color",
                    "op": "eq",
                    "value": "blue",
                },
            ],
        },
        sort_json=[{"field": "annotation.updated_at", "direction": "desc"}],
        columns_json=["class_name", "attributes"],
    )
    invalid_view = ProjectTaskView(
        project_id=project.id,
        owner_id=admin.id,
        name="Filter Removed Attribute",
        visibility="private",
        entity_scope="objects",
        filter_json={"field": "annotation.attribute.bbox.obsolete", "op": "exists"},
        sort_json=[],
        columns_json=["class_name"],
    )
    db.add_all([root_or, required_nested, object_filter, invalid_view])
    await db.flush()
    project.tool_bindings = {
        **project.tool_bindings,
        "bbox": {
            **project.tool_bindings["bbox"],
            "attribute_schema": {
                "fields": [
                    field
                    for field in project.tool_bindings["bbox"]["attribute_schema"][
                        "fields"
                    ]
                    if field["key"] != "obsolete"
                ]
            },
        },
    }
    await db.flush()
    return FilteringImageManifest(
        project_id=str(project.id),
        task_ids={key: str(task.id) for key, task in tasks.items()},
        object_ids=objects,
        batch_ids={"visible": str(visible_batch.id), "hidden": str(hidden_batch.id)},
        schema_={
            "color": "color",
            "weather": "weather",
            "finish": "finish",
            "obsolete": "obsolete",
        },
        expected={
            "same_object_task_ids": [str(tasks["same_object"].id)],
            "cross_object_task_ids": [],
            "visible_task_ids": [
                str(tasks[key].id) for key in task_specs if key != "hidden"
            ],
            "hidden_task_ids": [str(tasks["hidden"].id)],
            "missing_required_task_ids": [str(tasks["required_missing"].id)],
        },
        saved_view_ids={
            "root_or": str(root_or.id),
            "required_nested": str(required_nested.id),
            "object_filter": str(object_filter.id),
            "removed_attribute": str(invalid_view.id),
        },
    )


async def _seed_video_review(
    db: AsyncSession, ids: _FilterIds, users: dict[str, Any], video_key: str
) -> FilteringVideoManifest:
    from app.db.models.annotation import Annotation
    from app.db.models.prediction import Prediction
    from app.db.models.project_member import ProjectMember
    from app.db.models.video_tracker_job import VideoTrackerJob, VideoTrackerJobStatus

    admin, annotator, reviewer = users["admin"], users["anno"], users["rev"]
    project = await _create_project(
        db,
        owner_id=admin.id,
        display_id=ids.display("P-E2E-FILTER-VIDEO"),
        name="Filter Video AI Review",
        type_key="video-det",
        type_label="Video detection",
        data_type="video",
        tool_bindings=_video_binding(),
    )
    db.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=annotator.id,
                role="annotator",
                assigned_by=admin.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=reviewer.id,
                role="reviewer",
                assigned_by=admin.id,
            ),
        ]
    )
    dataset = await _create_dataset(
        db,
        project_id=project.id,
        created_by=admin.id,
        display_id=ids.display("DS-E2E-FILTER-V"),
        name="Filter Video Dataset",
        data_type="video",
        file_count=4,
    )
    batch = await _create_batch(
        db,
        project_id=project.id,
        created_by=admin.id,
        display_id=ids.display("B-E2E-FILTER-V"),
        name="Filter Video Batch",
        status="annotating",
        annotator_id=annotator.id,
        reviewer_id=reviewer.id,
    )
    names = ["detection_only", "tracker_only", "both", "neither"]
    tasks: dict[str, Any] = {}
    for index, name in enumerate(names):
        item = await _create_item(
            db,
            dataset_id=dataset.id,
            file_name=f"{name}.webm",
            file_path=video_key,
            file_type="video",
            width=1280,
            height=720,
            metadata={
                "video": {
                    "duration_ms": 9600,
                    "fps": 25,
                    "frame_count": 240,
                    "width": 1280,
                    "height": 720,
                }
            },
        )
        tasks[name] = await _create_task(
            db,
            project_id=project.id,
            batch_id=batch.id,
            item_id=item.id,
            display_id=ids.display("T-E2E-FILTER-V-{0}", index + 1),
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="video",
            sequence_order=index,
        )
    batch.total_tasks = 4
    # ponytail: metric-only candidate payloads omit render geometry; Workbench coverage
    # imports valid framed shapes through the product API when geometry is required.
    predictions: dict[str, Any] = {}
    predictions["low"] = Prediction(
        task_id=tasks["detection_only"].id,
        project_id=project.id,
        model_version="filter-detector-low",
        result=[{"type": "rectanglelabels", "value": {}, "score": 0.2}],
        rejected_shape_indexes=[],
    )
    predictions["at"] = Prediction(
        task_id=tasks["both"].id,
        project_id=project.id,
        model_version="filter-detector-at",
        result=[{"type": "rectanglelabels", "value": {}, "score": 0.5}],
        rejected_shape_indexes=[],
    )
    predictions["above"] = Prediction(
        task_id=tasks["both"].id,
        project_id=project.id,
        model_version="filter-detector-above",
        result=[{"type": "rectanglelabels", "value": {}, "score": 0.9}],
        rejected_shape_indexes=[],
    )
    predictions["accepted"] = Prediction(
        task_id=tasks["neither"].id,
        project_id=project.id,
        model_version="filter-detector-accepted",
        result=[{"type": "rectanglelabels", "value": {}, "score": 0.8}],
        rejected_shape_indexes=[],
    )
    predictions["rejected"] = Prediction(
        task_id=tasks["neither"].id,
        project_id=project.id,
        model_version="filter-detector-rejected",
        result=[{"type": "rectanglelabels", "value": {}, "score": 0.9}],
        rejected_shape_indexes=[0],
    )
    db.add_all(predictions.values())
    await db.flush()
    accepted = Annotation(
        task_id=tasks["neither"].id,
        project_id=project.id,
        user_id=annotator.id,
        source="prediction_based",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2},
        parent_prediction_id=predictions["accepted"].id,
        attributes={"_shape_index": 0},
    )
    db.add(accepted)
    jobs: dict[str, Any] = {}
    for name in ("tracker_only", "both"):
        task = tasks[name]
        jobs[name] = VideoTrackerJob(
            task_id=task.id,
            dataset_item_id=task.dataset_item_id,
            created_by=annotator.id,
            status=VideoTrackerJobStatus.PENDING_REVIEW.value,
            model_key="filter-tracker",
            direction="forward",
            from_frame=0,
            to_frame=1,
            prompt={"fixture": "filtering"},
            staged_result={
                "results": [
                    {
                        "frame_index": 0,
                        "geometry": {
                            "type": "bbox",
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.2,
                            "h": 0.2,
                        },
                        "confidence": 0.75,
                        "outside": False,
                        "instance_id": "A",
                    }
                ]
            },
            event_channel="filtering",
        )
    db.add_all(jobs.values())
    await db.flush()
    for task_name, task in tasks.items():
        task.total_predictions = sum(
            prediction.task_id == task.id for prediction in predictions.values()
        )
        task.total_annotations = int(task_name == "neither")
    await db.flush()
    return FilteringVideoManifest(
        project_id=str(project.id),
        task_ids={key: str(task.id) for key, task in tasks.items()},
        candidate_ids={
            key: str(prediction.id) for key, prediction in predictions.items()
        },
        tracker_job_ids={key: str(job.id) for key, job in jobs.items()},
        expected={
            "ai_review_or_task_ids": [
                str(tasks[name].id)
                for name in ("detection_only", "tracker_only", "both")
            ],
            "ai_review_and_task_ids": [str(tasks["both"].id)],
            "low_confidence_task_ids": [str(tasks["detection_only"].id)],
            "accepted_or_rejected_only_task_ids": [str(tasks["neither"].id)],
        },
    )


async def _seed_paging(
    db: AsyncSession, ids: _FilterIds, users: dict[str, Any], image_key: str
) -> FilteringPagingManifest:
    from app.db.models.project_member import ProjectMember

    admin, annotator = users["admin"], users["anno"]
    project = await _create_project(
        db,
        owner_id=admin.id,
        display_id=ids.display("P-E2E-FILTER-PAGE"),
        name="Filter Paging Image",
        type_key="image-det",
        type_label="Image detection",
        data_type="image",
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    db.add(
        ProjectMember(
            project_id=project.id,
            user_id=annotator.id,
            role="annotator",
            assigned_by=admin.id,
        )
    )
    dataset = await _create_dataset(
        db,
        project_id=project.id,
        created_by=admin.id,
        display_id=ids.display("DS-E2E-FILTER-P"),
        name="Filter Paging Dataset",
        data_type="image",
        file_count=1,
    )
    batch = await _create_batch(
        db,
        project_id=project.id,
        created_by=admin.id,
        display_id=ids.display("B-E2E-FILTER-P"),
        name="Filter Paging Batch",
        status="annotating",
        annotator_id=annotator.id,
        reviewer_id=None,
    )
    item = await _create_item(
        db,
        dataset_id=dataset.id,
        file_name="paging.svg",
        file_path=image_key,
        file_type="image",
    )
    task = await _create_task(
        db,
        project_id=project.id,
        batch_id=batch.id,
        item_id=item.id,
        display_id=ids.display("T-E2E-FILTER-PAGE"),
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
        sequence_order=0,
    )
    base = _now()
    rows = []
    for index in range(101):
        row = _bbox_annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=annotator.id,
            class_name="car",
            attributes={"ordinal": index},
        )
        row.created_at = base + timedelta(microseconds=101 - index)
        row.updated_at = base + timedelta(microseconds=101 - index)
        rows.append(row)
    db.add_all(rows)
    await db.flush()
    task.total_annotations = 101
    task.is_labeled = True
    batch.total_tasks = 1
    await db.flush()
    ids = [str(row.id) for row in rows]
    return FilteringPagingManifest(
        project_id=str(project.id),
        task_id=str(task.id),
        object_ids=ids,
        expected_page_one_object_ids=ids[:100],
        expected_page_two_object_ids=ids[100:],
    )


async def _seed_lidar(
    db: AsyncSession, ids: _FilterIds, users: dict[str, Any], prefix: str
) -> FilteringLidarManifest:
    from app.api.v1._test_seed import _make_test_pcd_frames
    from app.db.models.dataset import Scene
    from app.db.models.scene_pose import SceneFramePose
    from app.db.models.project_member import ProjectMember
    from app.db.models.project_task_view import ProjectTaskView
    from app.db.models.task_dataset_item_link import TaskDatasetItemLink
    from app.services.scene_track_domain import ensure_scene_track
    from app.services.storage import storage_service

    admin, annotator = users["admin"], users["anno"]
    project = await _create_project(
        db,
        owner_id=admin.id,
        display_id=ids.display("P-E2E-FILTER-LIDAR"),
        name="Filter Scene LiDAR",
        type_key="lidar",
        type_label="LiDAR annotation",
        data_type="lidar",
        tool_bindings={
            "lidar_box_3d": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
        scene_mode=True,
    )
    db.add(
        ProjectMember(
            project_id=project.id,
            user_id=annotator.id,
            role="annotator",
            assigned_by=admin.id,
        )
    )
    dataset = await _create_dataset(
        db,
        project_id=project.id,
        created_by=admin.id,
        display_id=ids.display("DS-E2E-FILTER-L"),
        name="Filter Scene Dataset",
        data_type="lidar",
        file_count=6,
        is_temporal=True,
    )
    scene = Scene(
        display_id=ids.display("SCN-E2E-FILTER-L"),
        dataset_id=dataset.id,
        name="Filter Scene",
        source_format="nuscenes",
        source_metadata={"fixture_source": "filtering"},
        created_by=admin.id,
    )
    db.add(scene)
    await db.flush()
    frames, _, _ = _make_test_pcd_frames()
    camera = b'<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480"><rect width="640" height="480" fill="#223344"/></svg>'
    calibration = {
        "extrinsic": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
        "intrinsic": [100, 0, 320, 0, 100, 240, 0, 0, 1],
    }
    pcd_keys = []
    for index, payload in enumerate(frames):
        key = f"{prefix}lidar-{index}.pcd"
        storage_service.client.put_object(
            Bucket=storage_service.datasets_bucket,
            Key=key,
            Body=payload,
            ContentType="application/octet-stream",
        )
        pcd_keys.append(key)
    camera_key = f"{prefix}camera.svg"
    storage_service.client.put_object(
        Bucket=storage_service.datasets_bucket,
        Key=camera_key,
        Body=camera,
        ContentType="image/svg+xml",
    )
    visible_batch = await _create_batch(
        db,
        project_id=project.id,
        created_by=admin.id,
        display_id=ids.display("B-E2E-FILTER-L-V"),
        name="Filter LiDAR Visible",
        status="annotating",
        annotator_id=annotator.id,
        reviewer_id=None,
    )
    hidden_batch = await _create_batch(
        db,
        project_id=project.id,
        created_by=admin.id,
        display_id=ids.display("B-E2E-FILTER-L-H"),
        name="Filter LiDAR Hidden",
        status="draft",
        annotator_id=None,
        reviewer_id=None,
    )
    tasks = []
    for frame_index in range(3):
        item = await _create_item(
            db,
            dataset_id=dataset.id,
            file_name=f"frame-{frame_index}.pcd",
            file_path=pcd_keys[frame_index if frame_index < 2 else 1],
            file_type="point_cloud",
            width=None,
            height=None,
            scene_id=scene.id,
            frame_index=frame_index,
        )
        camera_item = await _create_item(
            db,
            dataset_id=dataset.id,
            file_name=f"frame-{frame_index}-camera.svg",
            file_path=camera_key,
            file_type="image",
            width=640,
            height=480,
            scene_id=scene.id,
            frame_index=frame_index,
            metadata={"calibration": calibration},
        )
        batch = visible_batch if frame_index < 2 else hidden_batch
        task = await _create_task(
            db,
            project_id=project.id,
            batch_id=batch.id,
            item_id=item.id,
            display_id=ids.display("T-E2E-FILTER-L-{0}", frame_index),
            file_name=item.file_name,
            file_path=item.file_path,
            file_type="point_cloud",
            sequence_order=frame_index,
        )
        db.add_all(
            [
                TaskDatasetItemLink(
                    task_id=task.id, dataset_item_id=item.id, role="primary_lidar"
                ),
                TaskDatasetItemLink(
                    task_id=task.id,
                    dataset_item_id=camera_item.id,
                    role="camera_front",
                    sensor_name="front",
                ),
            ]
        )
        tasks.append(task)
        db.add(
            SceneFramePose(
                scene_id=scene.id,
                frame_index=frame_index,
                timestamp_us=frame_index * 100000,
                ego_translation=[0, 0, 0],
                ego_rotation=[1, 0, 0, 0],
                source_metadata={},
            )
        )
    await db.flush()
    track_refs = [f"filter-track-{index + 1:03d}" for index in range(101)]
    for track_ref in track_refs:
        track = await ensure_scene_track(
            db,
            project_id=project.id,
            scene_id=scene.id,
            track_id=track_ref,
            class_name="car",
            frames={0, 1},
            actor_id=annotator.id,
            interval_source="imported",
        )
        for frame_index, task in enumerate(tasks[:2]):
            db.add(
                _bbox_annotation(
                    task_id=task.id,
                    project_id=project.id,
                    user_id=annotator.id,
                    class_name="car",
                    attributes={},
                    track_id=track_ref,
                    annotation_type="box_3d",
                    tool_unit_id="lidar_box_3d",
                    frame_index=frame_index,
                    scene_track_id=track.id,
                )
            )
    hidden_track_ref = "filter-track-hidden"
    hidden_track = await ensure_scene_track(
        db,
        project_id=project.id,
        scene_id=scene.id,
        track_id=hidden_track_ref,
        class_name="car",
        frames={2},
        actor_id=annotator.id,
        interval_source="imported",
    )
    db.add(
        _bbox_annotation(
            task_id=tasks[2].id,
            project_id=project.id,
            user_id=annotator.id,
            class_name="car",
            attributes={},
            track_id=hidden_track_ref,
            annotation_type="box_3d",
            tool_unit_id="lidar_box_3d",
            frame_index=2,
            scene_track_id=hidden_track.id,
        )
    )
    visible_batch.total_tasks = 2
    hidden_batch.total_tasks = 1
    await db.flush()
    track_view = ProjectTaskView(
        project_id=project.id,
        owner_id=admin.id,
        name="Filter Track IDs",
        visibility="project",
        entity_scope="tracks",
        filter_json={
            "field": "annotation.track_id",
            "op": "eq",
            "value": track_refs[0],
        },
        sort_json=[{"field": "track.track_id", "direction": "asc"}],
        columns_json=["track_id", "class_name"],
    )
    db.add(track_view)
    await db.flush()
    return FilteringLidarManifest(
        project_id=str(project.id),
        scene_id=str(scene.id),
        task_ids=[str(task.id) for task in tasks],
        track_refs=track_refs,
        hidden_track_ref=hidden_track_ref,
        expected_visible_track_refs=track_refs,
        saved_view_ids={"track_filter": str(track_view.id)},
    )


async def _seed_operations(
    db: AsyncSession, ids: _FilterIds, users: dict[str, Any]
) -> FilteringOperationsManifest:
    from tests.factory import create_user
    from app.db.models.async_job import AsyncJob
    from app.db.models.audit_log import AuditLog
    from app.db.models.bug_report import BugReport
    from app.db.models.project_template import ProjectTemplate
    from app.db.models.user_invitation import UserInvitation

    admin, annotator = users["admin"], users["anno"]
    active = await create_user(
        db,
        "employee",
        ids.user_email("filter-active"),
        ids.display_name("Filter Active"),
    )
    inactive = await create_user(
        db,
        "employee",
        ids.user_email("filter-inactive"),
        ids.display_name("Filter Inactive"),
    )
    inactive.is_active = False
    inactive.status = "offline"
    inactive.disabled_kind = "manual"
    inactive.disabled_at = _now()
    inactive.disabled_by = admin.id
    project_a = await _create_project(
        db,
        owner_id=admin.id,
        display_id=ids.display("P-E2E-FILTER-OPSA"),
        name=ids.display_name("Filter Ops Alpha"),
        type_key="image-det",
        type_label="Image detection",
        data_type="image",
        tool_bindings={
            "bbox": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            }
        },
    )
    project_b = await _create_project(
        db,
        owner_id=admin.id,
        display_id=ids.display("P-E2E-FILTER-OPSB"),
        name=ids.display_name("Filter Ops Beta"),
        type_key="video-det",
        type_label="Video detection",
        data_type="video",
        tool_bindings=_video_binding(),
    )
    datasets = [
        await _create_dataset(
            db,
            project_id=project_a.id,
            created_by=admin.id,
            display_id=ids.display("DS-E2E-FILTER-OA"),
            name=ids.display_name("Filter Ops Dataset Alpha"),
            data_type="image",
        ),
        await _create_dataset(
            db,
            project_id=project_b.id,
            created_by=admin.id,
            display_id=ids.display("DS-E2E-FILTER-OB"),
            name=ids.display_name("Filter Ops Dataset Beta"),
            data_type="video",
        ),
    ]
    templates = [
        ProjectTemplate(
            display_id=ids.display("TPL-E2E-FILTER-A"),
            name=ids.display_name("Filter Private Template"),
            description="private fixture",
            type_label="Image detection",
            type_key="image-det",
            data_type="image",
            tool_bindings=project_a.tool_bindings,
            label_config={},
            scope="private",
            created_by=admin.id,
            source_project_id=project_a.id,
        ),
        ProjectTemplate(
            display_id=ids.display("TPL-E2E-FILTER-B"),
            name=ids.display_name("Filter Public Template"),
            description="public fixture",
            type_label="Video detection",
            type_key="video-det",
            data_type="video",
            tool_bindings=project_b.tool_bindings,
            label_config={},
            scope="public",
            created_by=admin.id,
            source_project_id=project_b.id,
        ),
    ]
    db.add_all(templates)
    await db.flush()
    now = _now()
    invitations = [
        UserInvitation(
            email=ids.invitation_email("filter-pending"),
            role="employee",
            project_role="annotator",
            group_name="filter",
            project_id=project_a.id,
            token=f"filter-invitation-pending-{ids.namespace}",
            expires_at=now + timedelta(days=7),
            invited_by=admin.id,
        ),
        UserInvitation(
            email=ids.invitation_email("filter-accepted"),
            role="reviewer",
            group_name="filter",
            project_id=project_a.id,
            token=f"filter-invitation-accepted-{ids.namespace}",
            expires_at=now + timedelta(days=7),
            invited_by=admin.id,
            accepted_at=now - timedelta(days=1),
            accepted_user_id=annotator.id,
        ),
        UserInvitation(
            email=ids.invitation_email("filter-expired"),
            role="annotator",
            group_name="filter",
            project_id=project_b.id,
            token=f"filter-invitation-expired-{ids.namespace}",
            expires_at=now - timedelta(days=1),
            invited_by=admin.id,
        ),
        UserInvitation(
            email=ids.invitation_email("filter-revoked"),
            role="annotator",
            group_name="filter",
            project_id=project_b.id,
            token=f"filter-invitation-revoked-{ids.namespace}",
            expires_at=now + timedelta(days=7),
            invited_by=admin.id,
            revoked_at=now - timedelta(hours=1),
        ),
    ]
    db.add_all(invitations)
    jobs = [
        AsyncJob(
            kind="batch_predict",
            project_id=project_a.id,
            user_id=admin.id,
            status="completed",
            progress_pct=100,
            payload={
                "model_key": "filter-detector",
                "prompt": ids.job_prompt("alpha complete"),
            },
            result={"success_count": 2},
        ),
        AsyncJob(
            kind="export",
            project_id=project_b.id,
            user_id=admin.id,
            status="failed",
            progress_pct=40,
            payload={
                "model_key": "filter-export",
                "prompt": ids.job_prompt("beta failed"),
            },
            result={},
            error_message="fixture failure",
        ),
    ]
    db.add_all(jobs)
    bugs = [
        BugReport(
            display_id=ids.display("BUG-E2E-FILTER-A"),
            reporter_id=active.id,
            route="/projects",
            user_role=active.role,
            project_id=project_a.id,
            title="Filter Alpha bug",
            description="filter fixture alpha",
            severity="high",
            status="new",
            assigned_to_id=admin.id,
        ),
        BugReport(
            display_id=ids.display("BUG-E2E-FILTER-B"),
            reporter_id=inactive.id,
            route="/jobs",
            user_role=inactive.role,
            project_id=project_b.id,
            title="Filter Beta bug",
            description="filter fixture beta",
            severity="low",
            status="in_progress",
            assigned_to_id=admin.id,
        ),
    ]
    db.add_all(bugs)
    await db.flush()
    audit_rows = [
        AuditLog(
            actor_id=admin.id,
            actor_email=admin.email,
            actor_role=admin.role,
            action="filter.fixture.created",
            target_type="project",
            target_id=str(project_a.id),
            method="POST",
            path="/api/v1/__test/seed/filtering",
            status_code=201,
            detail_json=_filter_audit_detail(ids, "alpha"),
            created_at=now,
        ),
        AuditLog(
            actor_id=admin.id,
            actor_email=admin.email,
            actor_role=admin.role,
            action="filter.fixture.failed",
            target_type="job",
            target_id=str(jobs[1].id),
            method="POST",
            path="/api/v1/__test/seed/filtering",
            status_code=500,
            detail_json=_filter_audit_detail(ids, "beta"),
            created_at=now + timedelta(microseconds=1),
        ),
    ]
    db.add_all(audit_rows)
    await db.flush()
    return FilteringOperationsManifest(
        project_ids=[str(project_a.id), str(project_b.id)],
        dataset_ids=[str(dataset.id) for dataset in datasets],
        template_ids=[str(template.id) for template in templates],
        user_ids=[str(user.id) for user in (active, inactive)],
        user_emails=[active.email, inactive.email],
        display_names={
            "active": active.name,
            "inactive": inactive.name,
            "project_a": project_a.name,
            "project_b": project_b.name,
            "dataset_a": datasets[0].name,
            "dataset_b": datasets[1].name,
            "template_private": templates[0].name,
            "template_public": templates[1].name,
        },
        invitation_emails={
            "pending": invitations[0].email,
            "accepted": invitations[1].email,
            "expired": invitations[2].email,
            "revoked": invitations[3].email,
        },
        search_keys={
            "users": ids.namespace and f"[{ids.namespace}]" or "Filter",
            "projects": ids.display_name("Filter Ops"),
            "templates": ids.namespace and f"[{ids.namespace}]" or "Filter",
            "invitations": ids.invitation_search_key,
            "jobs": ids.job_prompt("alpha complete"),
        },
        audit_scopes={
            "alpha": ids.audit_scope("alpha"),
            "beta": ids.audit_scope("beta"),
        },
        invitation_ids=[str(invitation.id) for invitation in invitations],
        job_ids=[str(job.id) for job in jobs],
        bug_ids=[str(bug.id) for bug in bugs],
        audit_ids=[str(row.id) for row in audit_rows],
    )


async def build_filtering_seed(
    db: AsyncSession, namespace: str | None = None
) -> FilteringSeedManifest:
    """Build all filtering scenarios for the shared fixture or one owned namespace."""
    from app.services.storage import storage_service

    ids = _FilterIds(namespace)
    users = await _get_users(db, ids)
    image_svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="48"><rect width="64" height="48" fill="#f1f5f9"/></svg>'
    image_key = f"{ids.storage_prefix}image.svg"
    storage_service.client.put_object(
        Bucket=storage_service.datasets_bucket,
        Key=image_key,
        Body=image_svg,
        ContentType="image/svg+xml",
    )
    video_source = (
        Path(__file__).resolve().parents[5]
        / "docs-site/public/home/sam-tools/smart-point.webm"
    )
    video_key = f"{ids.storage_prefix}video.webm"
    storage_service.client.put_object(
        Bucket=storage_service.datasets_bucket,
        Key=video_key,
        Body=video_source.read_bytes(),
        ContentType="video/webm",
    )
    image = await _seed_image_semantics(db, ids, users, image_key)
    video = await _seed_video_review(db, ids, users, video_key)
    paging = await _seed_paging(db, ids, users, image_key)
    lidar = await _seed_lidar(db, ids, users, ids.storage_prefix)
    operations = await _seed_operations(db, ids, users)
    await db.commit()
    return FilteringSeedManifest(
        users={key: str(user.id) for key, user in users.items()},
        user_emails={key: user.email for key, user in users.items()},
        image=image,
        video=video,
        paging=paging,
        lidar=lidar,
        operations=operations,
    )
