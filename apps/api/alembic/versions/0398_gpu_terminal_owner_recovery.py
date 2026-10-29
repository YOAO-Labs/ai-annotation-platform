"""Recover proof-bound GPU installation leases under the terminal owner."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0398"
down_revision = "bo0397"
branch_labels = depends_on = None


def _apply(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    sql = (
        Path(__file__).resolve().parents[1]
        / "sql"
        / "0398_gpu_terminal_owner_recovery.sql"
    ).read_text(encoding="utf-8")
    _apply(sql)


def downgrade() -> None:
    sql = (
        Path(__file__).resolve().parents[1] / "sql" / "0390_inference_gpu.sql"
    ).read_text(encoding="utf-8")
    statement = next(
        part.strip()
        for part in sql.split("-- aap-statement-break")
        if part.strip().startswith(
            "CREATE OR REPLACE FUNCTION aap_inference_gpu_v1.gpu_authority_step("
        )
    )
    _apply(statement)
