"""Transaction-affine public core rows and bounded identity-only export locks."""

from sqlalchemy import column, func, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import aliased

from app.db.models.project import Project as ProjectModel
from app.services.labeling_core_read_relations import (
    annotation_row as Annotation,
    batch_row as TaskBatch,
    scene_track_row as SceneTrack,
    task_dataset_item_link_row as TaskDatasetItemLink,
    task_row as Task,
)
from app.services.workspace_read_relations import project_rows

Project = aliased(ProjectModel, project_rows(), adapt_on_names=True)


def lock_ids(locked):
    """Select only identities from an owner lock before measuring full rows."""
    rows = locked.table_valued(column("id", UUID(as_uuid=True)))
    return select(rows.c.id)


def row_lock_query(model, predicate, limit, *, write):
    """Retain bounded global identity ordering for simple core-row scopes."""
    if model not in (
        Project,
        TaskBatch,
        Task,
        Annotation,
        SceneTrack,
        TaskDatasetItemLink,
    ):
        return None
    identities = func.array(
        select(model.id)
        .where(predicate)
        .order_by(model.id)
        .limit(limit)
        .scalar_subquery()
    )
    mode = "update" if write else "share"
    if model is Project:
        return select(
            func.unnest(
                func.aap_workspace_v1.lock_project_references_for_update(identities)
            )
        )
    if model is TaskBatch:
        locked = func.aap_labeling_v1.lock_batch_rows_for_core_read(
            identities, None, mode, True
        )
    elif model is Task:
        function = (
            func.aap_labeling_v1.lock_task_rows_for_core_read
            if write
            else func.aap_labeling_v1.lock_task_rows_for_core_read_share
        )
        locked = function(identities, None, None)
    elif model is Annotation:
        locked = func.aap_labeling_v1.lock_annotation_rows_for_core_read(
            identities, None, None, mode, True
        )
    elif model is TaskDatasetItemLink:
        locked = (
            func.aap_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(
                identities, limit
            )
        )
    else:
        locked = func.aap_labeling_v1.lock_scene_track_rows_for_core_read(
            identities, limit
        )
    return lock_ids(locked)
