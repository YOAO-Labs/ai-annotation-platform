"""Add the versioned dataset task append transaction contract.

Revision ID: bo0191
Revises: bo0190
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0191"
down_revision = "bo0190"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0191_dataset_task_append_contract.sql"
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
    op.execute(
        "DROP FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_assets_v1.lock_append_sources(uuid, uuid[]) RESTRICT")
    op.execute(
        "DROP FUNCTION aap_assets_v1.lock_current_association(uuid, uuid) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_assets_v1.lock_dataset_reference(uuid) RESTRICT")
    op.drop_table("task_append_operations")
    op.execute("DROP SCHEMA aap_labeling_v1 RESTRICT")
    op.execute("DROP SCHEMA aap_assets_v1 RESTRICT")
