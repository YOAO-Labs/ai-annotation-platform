"""Add caller-transactional Assets catalog capabilities and frozen receipts.

Revision ID: bo0211
Revises: bo0210
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0211"
down_revision = "bo0210"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0211_dataset_catalog.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def _artifact_statements() -> list[str]:
    artifact = Path(__file__).resolve().parents[1] / "sql" / _ARTIFACT_NAME
    return [
        statement.strip()
        for statement in artifact.read_text(encoding="utf-8").split(
            _STATEMENT_SEPARATOR
        )
        if statement.strip()
    ]


def upgrade() -> None:
    for statement in _artifact_statements():
        op.execute(sa.text(statement))


def downgrade() -> None:
    for signature in (
        "aap_assets_v1.update_catalog_dataset(uuid, uuid, uuid, jsonb)",
        "aap_assets_v1.create_catalog_dataset(uuid, uuid, jsonb)",
        "aap_assets_v1.list_catalog_items(uuid, uuid, integer, bigint)",
        "aap_assets_v1.get_catalog_dataset(uuid, uuid)",
        "aap_assets_v1.list_catalog_datasets(uuid, text, text, boolean, integer, bigint)",
        "aap_assets_v1._catalog_dataset_summary(uuid)",
        "aap_assets_v1._catalog_actor(uuid, boolean)",
        "aap_assets_v1._catalog_failure(text)",
    ):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
    op.drop_table("dataset_catalog_operations", schema="public")
