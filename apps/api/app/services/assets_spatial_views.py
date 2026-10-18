"""Read-only SQL bindings for the narrow Assets spatial reference projections."""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Uuid, column, table


spatial_item_references = table(
    "spatial_item_references",
    column("item_id", Uuid(as_uuid=True)),
    column("dataset_id", Uuid(as_uuid=True)),
    column("scene_id", Uuid(as_uuid=True)),
    column("frame_index", Integer),
    column("file_type", String),
    column("axis_convention", String),
    column("scene_name", String),
    column("calibration_metadata_present", Boolean),
    schema="aap_assets_v1",
)

spatial_scene_references = table(
    "spatial_scene_references",
    column("scene_id", Uuid(as_uuid=True)),
    column("dataset_id", Uuid(as_uuid=True)),
    column("scene_name", String),
    schema="aap_assets_v1",
)
