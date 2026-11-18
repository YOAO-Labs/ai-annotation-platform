"""Move Identity Briefs and Labeling class reads without changing their protocols."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0472"
down_revision = "bo0471"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0472_briefs_class_query_namespaces.sql"
    ).read_text(encoding="utf-8")
    op.execute(
        sa.text(source.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def downgrade() -> None:
    raise NotImplementedError("Beta read-owner namespace cutover is forward-only")
