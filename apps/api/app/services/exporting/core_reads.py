"""Transaction-affine public core rows and bounded identity-only export locks."""

from sqlalchemy import Text, cast, column, func, inspect, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import aliased

from app.db.models.project import Project as ProjectModel
from app.services.exporting import asset_facts
from app.services.labeling_core_read_relations import (
    annotation_row as Annotation,
    batch_row as TaskBatch,
    scene_track_row as SceneTrack,
    task_dataset_item_link_row as TaskDatasetItemLink,
    task_row as Task,
    video_segment_row as VideoSegment,
    video_track_quality_run_row as VideoTrackQualityRun,
)
from app.services.workspace_read_relations import project_rows

Project = aliased(ProjectModel, project_rows(), adapt_on_names=True)
_ASSET_LOCKS = {
    asset_facts.dataset_facts: func.yoao_assets_v1.lock_export_datasets_share,
    asset_facts.item_facts: func.yoao_assets_v1.lock_export_items_update,
    asset_facts.scene_facts: func.yoao_assets_v1.lock_export_scenes_update,
    asset_facts.calibration_facts: func.yoao_assets_v1.lock_export_calibrations_share,
    asset_facts.pose_facts: func.yoao_assets_v1.lock_export_poses_share,
}


def row_bytes(model):
    relation = inspect(model).selectable
    measured = relation.c.get("export_row_bytes")
    if measured is not None:
        return measured
    return func.octet_length(cast(func.to_jsonb(relation.table_valued()), Text))


def lock_ids(locked):
    """Select only identities from an owner lock before measuring full rows."""
    rows = locked.table_valued(column("id", UUID(as_uuid=True)))
    return select(rows.c.id)


def row_lock_query(model, predicate, limit, *, write):
    """Retain bounded global identity ordering for simple core-row scopes."""
    if model not in _ASSET_LOCKS and model not in (
        Project,
        TaskBatch,
        Task,
        Annotation,
        SceneTrack,
        TaskDatasetItemLink,
        VideoSegment,
        VideoTrackQualityRun,
    ):
        return None
    identities = func.array(
        select(model.id)
        .where(predicate)
        .order_by(model.id)
        .limit(limit)
        .scalar_subquery()
    )
    lock_assets = _ASSET_LOCKS.get(model)
    if lock_assets is not None:
        return lock_ids(lock_assets(identities))
    mode = "update" if write else "share"
    if model is Project:
        return select(
            func.unnest(
                func.yoao_workspace_v1.lock_project_references_for_update(identities)
            )
        )
    if model is TaskBatch:
        locked = func.yoao_labeling_v1.lock_batch_rows_for_core_read(
            identities, None, mode, True
        )
    elif model is Task:
        function = (
            func.yoao_labeling_v1.lock_task_rows_for_core_read
            if write
            else func.yoao_labeling_v1.lock_task_rows_for_core_read_share
        )
        locked = function(identities, None, None)
    elif model is Annotation:
        locked = func.yoao_labeling_v1.lock_annotation_rows_for_core_read(
            identities, None, None, mode, True
        )
    elif model is TaskDatasetItemLink:
        locked = (
            func.yoao_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(
                identities, limit
            )
        )
    elif model is VideoSegment:
        locked = func.yoao_labeling_v1.lock_video_segment_ids_for_export_share(
            identities, limit
        )
    elif model is VideoTrackQualityRun:
        locked = func.yoao_labeling_v1.lock_video_quality_run_ids_for_export_share(
            identities, limit
        )
    else:
        locked = func.yoao_labeling_v1.lock_scene_track_rows_for_core_read(
            identities, limit
        )
    return lock_ids(locked)
