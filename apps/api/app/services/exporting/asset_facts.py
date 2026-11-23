"""Published Assets facts used by the bounded export snapshot readers."""

from sqlalchemy import Integer, column, table
from sqlalchemy.orm import aliased

from app.db.models.dataset import Dataset, DatasetItem, Scene, SensorCalibrationRevision
from app.db.models.scene_pose import SceneFramePose


def _facts(model, name, names):
    source = model.__table__.c
    projection = table(
        name,
        *(column(field, source[field].type) for field in names.split()),
        column("export_row_bytes", Integer),
        schema="yoao_assets_v1",
    )
    return aliased(model, projection, adapt_on_names=True)


dataset_facts = _facts(
    Dataset,
    "export_dataset_facts",
    "id display_id name data_type is_temporal lifecycle_revision archived_at metadata",
)
item_facts = _facts(
    DatasetItem,
    "export_item_facts",
    "id dataset_id file_name file_path file_type file_size content_hash width height "
    "nuscenes_raw_original_id metadata media_source_generation media_timetable_revision "
    "media_materialized_generation media_object_identity media_source_drifted "
    "scene_id frame_index created_at updated_at thumbnail_output_id",
)
scene_facts = _facts(
    Scene,
    "export_scene_facts",
    "id display_id dataset_id name nuscenes_map_original_id source_format source_metadata "
    "created_by created_at updated_at",
)
calibration_facts = _facts(
    SensorCalibrationRevision,
    "export_calibration_facts",
    "id dataset_item_id revision digest calibration created_by created_at",
)
pose_facts = _facts(
    SceneFramePose,
    "export_pose_facts",
    "id scene_id frame_index timestamp_us ego_translation ego_rotation source_metadata created_at",
)
