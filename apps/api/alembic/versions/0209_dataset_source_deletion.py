"""Delete dataset sources through transactional owner capabilities.

Revision ID: bo0209
Revises: bo0208
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0209"
down_revision = "bo0208"
branch_labels = depends_on = None


def upgrade() -> None:
    artifact = (
        Path(__file__).resolve().parents[1] / "sql/0209_dataset_source_deletion.sql"
    )
    for statement in artifact.read_text().split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip()))


def downgrade() -> None:
    artifact = (
        Path(__file__).resolve().parents[1] / "sql/0203_dataset_unlink_assets.sql"
    )
    for function in ("prepare_dataset_unlink_cleanup", "complete_storage_deletion"):
        original = next(
            statement.strip()
            for statement in artifact.read_text().split("-- aap-statement-break")
            if statement.strip().startswith(
                f"CREATE FUNCTION aap_assets_v1.{function}("
            )
        )
        op.execute(
            sa.text(
                original.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
            )
        )
    for signature in (
        "delete_dataset_item(uuid, uuid, uuid, uuid, text, jsonb)",
        "delete_dataset(uuid, uuid, uuid, text, jsonb)",
        "_delete_dataset_source(uuid, uuid, uuid, uuid, text, jsonb)",
        "_delete_dataset_source_records(uuid, uuid, uuid, text)",
        "_queue_pyramid_deletions(uuid, uuid[], text)",
        "_lock_dataset_source_deletion(uuid, uuid)",
        "lock_scene_source_references(uuid[])",
        "dataset_association_references(uuid)",
        "_source_delete_failure(text)",
    ):
        op.execute(f"DROP FUNCTION aap_assets_v1.{signature} RESTRICT")
    op.drop_table("dataset_source_deletion_operations")
    op.drop_column("asset_storage_deletions", "not_before")
