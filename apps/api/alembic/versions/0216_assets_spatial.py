"""Add Assets-owned native spatial state capabilities and receipts.

Revision ID: bo0216
Revises: bo0215
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0216"
down_revision = "bo0215"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_UPGRADE_ARTIFACTS = (
    "0216_assets_spatial_authorities.sql",
    "0216_assets_spatial.sql",
    "0216_assets_spatial_projections.sql",
)


def _run_artifact(filename: str) -> None:
    artifact = _SQL_DIR / filename
    for statement in artifact.read_text(encoding="utf-8").split(_STATEMENT_SEPARATOR):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def upgrade() -> None:
    for filename in _UPGRADE_ARTIFACTS:
        _run_artifact(filename)


def downgrade() -> None:
    receipt_count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM public.dataset_spatial_operations"))
        .scalar_one()
    )
    if receipt_count:
        raise RuntimeError(
            "Cannot downgrade persisted Dataset spatial operation receipts; "
            f"{receipt_count} retained receipt(s) exist."
        )
    op.execute("DROP VIEW aap_assets_v1.spatial_scene_references RESTRICT")
    op.execute("DROP VIEW aap_assets_v1.spatial_item_references RESTRICT")
    for signature in _DOWNGRADE_SIGNATURES:
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
    op.drop_table("dataset_spatial_operations", schema="public")


_DOWNGRADE_SIGNATURES: tuple[str, ...] = (
    "aap_assets_v1.read_spatial_pose_pairs(jsonb)",
    "aap_assets_v1.read_spatial_layout_receipt(uuid, uuid, uuid, text, boolean)",
    "aap_assets_v1.set_spatial_dataset_axis(uuid, uuid, text)",
    "aap_assets_v1.record_spatial_calibration_provenance(uuid, uuid, jsonb)",
    "aap_assets_v1.apply_pointcloud_preparation(uuid, jsonb, jsonb, jsonb)",
    "aap_assets_v1.read_pointcloud_preparation(uuid, uuid, integer)",
    "aap_assets_v1.update_spatial_calibration(uuid, uuid, uuid, text, integer, text, jsonb, text, text, jsonb, jsonb)",
    "aap_assets_v1.read_spatial_camera_target(uuid, uuid, text)",
    "aap_assets_v1._spatial_normalize_calibration(jsonb)",
    "aap_assets_v1._spatial_calibration_valid(jsonb)",
    "aap_assets_v1.apply_spatial_layout(uuid, uuid, uuid, text, boolean, jsonb, jsonb)",
    "aap_assets_v1.upsert_spatial_poses(uuid, uuid, jsonb)",
    "aap_assets_v1.assign_spatial_items(uuid, uuid, jsonb)",
    "aap_assets_v1.set_spatial_scene_source(uuid, uuid, text, jsonb)",
    "aap_assets_v1.create_spatial_scene(uuid, uuid, uuid, text, text, jsonb, uuid)",
    "aap_assets_v1.mutate_spatial_scene(uuid, uuid, uuid, jsonb, jsonb)",
    "aap_assets_v1.lock_spatial_calibration_facts(uuid, uuid, text, uuid, jsonb)",
    "aap_assets_v1.read_spatial_calibrations(uuid[], boolean, uuid, integer, integer)",
    "aap_assets_v1.read_spatial_poses(uuid, uuid, integer, integer)",
    "aap_assets_v1.read_spatial_items(uuid, uuid[], uuid, integer)",
    "aap_assets_v1.read_spatial_layout(uuid, uuid, uuid, uuid, integer)",
    "aap_assets_v1.get_spatial_scene(uuid, uuid)",
    "aap_assets_v1.list_spatial_scenes(uuid, uuid, timestamptz, uuid, integer)",
    "aap_assets_v1._spatial_item_ids_snapshot(uuid[])",
    "aap_assets_v1._spatial_roles_for_item(jsonb, uuid)",
    "aap_assets_v1._spatial_camera_roles_valid(jsonb, uuid[])",
    "aap_assets_v1._spatial_pose_json(uuid, integer)",
    "aap_assets_v1._spatial_item_json(uuid, text, jsonb)",
    "aap_assets_v1._spatial_scene_json(uuid)",
    "aap_assets_v1._spatial_dataset_snapshot(uuid)",
    "aap_assets_v1.guard_spatial_actor(uuid, text)",
    "aap_assets_v1._spatial_item_source_snapshot(uuid)",
    "aap_assets_v1._write_spatial_receipt(uuid, uuid, text, jsonb, jsonb, jsonb)",
    "aap_assets_v1._read_spatial_receipt(uuid, uuid, text, jsonb, jsonb)",
    "aap_assets_v1._lock_spatial_operation(uuid)",
    "aap_assets_v1._spatial_failure(text)",
    "aap_assets_v1._spatial_success(jsonb)",
    "aap_audit_v1.record_sensor_calibration_update(uuid, uuid, uuid, uuid, text, integer, text, jsonb)",
    "aap_labeling_v1.lock_spatial_preparation_scope(uuid)",
    "aap_labeling_v1.read_spatial_preparation_scope(uuid)",
    "aap_labeling_v1.lock_spatial_camera_authority(uuid, uuid, text, uuid)",
    "aap_labeling_v1.read_spatial_camera_target(uuid, uuid, text)",
    "aap_labeling_v1.read_spatial_camera_roles(uuid[])",
    "aap_workspace_v1.read_spatial_project_access(uuid, uuid)",
)
