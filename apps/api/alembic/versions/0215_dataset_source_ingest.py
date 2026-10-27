"""Add transaction-bound native Dataset source ingest capabilities.

Revision ID: bo0215
Revises: bo0214
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0215"
down_revision = "bo0214"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_UPGRADE_ARTIFACTS = (
    "0215_dataset_source_ingest.sql",
    "0215_dataset_source_ingest_delivery.sql",
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
    preparing_operations, pending_deliveries = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT
                (SELECT count(*) FROM public.dataset_source_ingest_operations
                 WHERE state = 'preparing'),
                (SELECT count(*) FROM public.dataset_source_ingest_deliveries
                 WHERE delivered_at IS NULL)
            """
            )
        )
        .one()
    )
    if preparing_operations or pending_deliveries:
        raise RuntimeError(
            "Cannot downgrade active Dataset source ingest state: "
            f"{preparing_operations} committed preparing operation(s) and "
            f"{pending_deliveries} pending delivery record(s); drain them first."
        )

    for signature in (
        "aap_assets_v1.finish_dataset_ingest(uuid, uuid, uuid, jsonb)",
        "aap_assets_v1.read_dataset_ingest_layout(uuid, uuid, uuid, uuid, integer)",
        "aap_assets_v1.complete_dataset_ingest_batch(uuid, uuid, uuid, uuid, jsonb)",
        "aap_assets_v1.prepare_dataset_ingest_batch(uuid, uuid, uuid, uuid, jsonb)",
        "aap_assets_v1.begin_dataset_ingest(uuid, uuid, uuid, jsonb)",
        "aap_assets_v1._dataset_source_ingest_scene_plan_valid(jsonb)",
        "aap_assets_v1._dataset_source_ingest_dataset_snapshot(uuid)",
        "aap_assets_v1._dataset_source_ingest_error_message(text)",
        "aap_assets_v1._dataset_source_ingest_completion_valid(jsonb)",
        "aap_assets_v1._dataset_source_ingest_source_valid(jsonb, text, text)",
        "aap_assets_v1._dataset_source_ingest_scan_file_type(text)",
        "aap_assets_v1._dataset_source_ingest_relative_path_valid(text)",
        "aap_assets_v1._dataset_source_ingest_descriptor_valid(jsonb)",
        "aap_assets_v1._dataset_source_ingest_integer_valid(jsonb, numeric, numeric)",
        "aap_assets_v1._dataset_source_ingest_batch_plan(uuid, uuid, uuid, uuid)",
        "aap_assets_v1._dataset_source_ingest_progress(uuid, uuid, uuid)",
        "aap_assets_v1._dataset_source_ingest_context(uuid)",
        "aap_assets_v1._dataset_source_ingest_reply(jsonb, text)",
        "aap_assets_v1.complete_dataset_source_ingest_delivery(uuid, uuid)",
        "aap_assets_v1.claim_dataset_source_ingest_delivery(uuid, integer)",
        "aap_assets_v1._dataset_source_ingest_delivery_target_is_current(uuid, text, uuid)",
        "aap_assets_v1._dataset_source_ingest_delivery_reply(jsonb, text)",
    ):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")

    op.drop_table("dataset_source_ingest_deliveries", schema="public")
    op.drop_table("dataset_source_ingest_entries", schema="public")
    op.drop_table("dataset_source_ingest_operations", schema="public")
