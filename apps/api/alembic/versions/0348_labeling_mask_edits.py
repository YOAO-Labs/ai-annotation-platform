"""Move slice, Mask edit, and AI Mask acceptance writes to Labeling."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0348"
down_revision = "0347"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0348_labeling_mask_edits.sql"


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_labeling_v1.ai_mask_accept_command(jsonb) RESTRICT")
    op.execute("DROP FUNCTION aap_labeling_v1.mask_revision_command(jsonb) RESTRICT")
    op.execute("DROP FUNCTION aap_labeling_v1.mask_edit_command(jsonb) RESTRICT")
