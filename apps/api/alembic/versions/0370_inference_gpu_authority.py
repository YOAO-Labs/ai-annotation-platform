"""Preserve GPU rollout and cancellation invariants through the Native owner."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0370"
down_revision = "bo0369"
branch_labels = None
depends_on = None


def _execute(statement: str) -> None:
    op.execute(sa.text(statement).execution_options(no_parameters=True))


def upgrade() -> None:
    payload = (
        Path(__file__).resolve().parents[1] / "sql" / "0370_inference_gpu.sql"
    ).read_text(encoding="utf-8")
    for statement in payload.split("-- aap-statement-break"):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    payload = (
        Path(__file__).resolve().parents[1] / "sql" / "0369_inference_gpu.sql"
    ).read_text(encoding="utf-8")
    prefix = "CREATE FUNCTION aap_inference_gpu_v1.gpu_authority_step("
    statement = next(
        statement.strip()
        for statement in payload.split("-- aap-statement-break")
        if statement.strip().startswith(prefix)
    )
    _execute(statement.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
