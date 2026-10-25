"""Move Mask QC and repair state behind the Labeling data owner."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0353"
down_revision = "0352"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0353_labeling_mask_quality.sql"
)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    for signature in (
        "aap_labeling_v1.raster_mask_retention_query(jsonb)",
        "aap_labeling_v1.mask_repair_command(jsonb)",
        "aap_labeling_v1.mask_qc_command(jsonb)",
        "aap_labeling_v1._raster_mask_reference_keys()",
        "aap_labeling_v1._quality_exact_keys(jsonb,text[])",
    ):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
