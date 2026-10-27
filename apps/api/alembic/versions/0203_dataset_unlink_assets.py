"""Add Assets and Diagnostics capabilities for dataset unlink.

Revision ID: bo0203
Revises: bo0202
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0203"
down_revision = "bo0202"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0203_dataset_unlink_assets.sql"
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
        "DROP FUNCTION aap_diagnostics_v1.detach_dataset_task_references(uuid, uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1.complete_storage_deletion(uuid, uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1.pending_storage_deletions(uuid, integer) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1.prepare_dataset_unlink_cleanup(uuid, uuid, uuid, text) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1.remove_dataset_association(uuid, uuid, uuid) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_assets_v1.dataset_scene_references(uuid) RESTRICT")
    op.execute(
        "DROP FUNCTION aap_assets_v1.dataset_item_references(uuid, uuid) RESTRICT"
    )
    op.execute("DROP TABLE public.asset_storage_deletions RESTRICT")
