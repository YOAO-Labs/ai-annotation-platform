"""Move API-key management and GPU caller/collector Native capability names."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0473"
down_revision = "bo0472"
branch_labels = depends_on = None


def upgrade() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "sql" / "0473_keys_gpu_capabilities.sql"
    ).read_text(encoding="utf-8")
    op.execute(
        sa.text(source.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def downgrade() -> None:
    raise NotImplementedError("Beta capability namespace cutover is forward-only")
