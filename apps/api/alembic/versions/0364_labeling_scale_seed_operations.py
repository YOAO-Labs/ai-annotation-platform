"""Move local scale-seed Labeling writes behind fixed owner operations."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0364"
down_revision = "0363"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0364_labeling_scale_seed_operations.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION aap_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.create_scale_seed_tasks(text,uuid,uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[]) RESTRICT"
    )
    op.execute("DROP VIEW aap_assets_v1.scale_seed_dataset_item_facts RESTRICT")
