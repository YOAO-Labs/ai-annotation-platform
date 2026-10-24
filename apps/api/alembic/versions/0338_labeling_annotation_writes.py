"""Move manual Annotation writes and create receipts behind Labeling."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0338"
down_revision = "0337"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _execute_file(path: Path) -> None:
    for statement in path.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def upgrade() -> None:
    _execute_file(_SQL_DIR / "0338_labeling_annotation_revision_support.sql")
    _execute_file(_SQL_DIR / "0338_labeling_annotation_writes.sql")


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_labeling_v1.annotation_write_command(jsonb) RESTRICT")
    _execute_file(_SQL_DIR / "0338_labeling_annotation_revision_support.previous.sql")
