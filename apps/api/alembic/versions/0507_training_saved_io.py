"""Bind registered training I/O to owner facts and exact object receipts."""

from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "bo0507"
down_revision = "bo0506"
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).resolve().parents[1] / "sql" / "0507_training_saved_io.sql"
    for statement in sql.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                text(statement.replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade():
    op.execute(
        "DROP FUNCTION yoao_assets_v1.record_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb,jsonb)"
    )
    op.execute(
        "DROP FUNCTION yoao_assets_v1.capture_training_saved_io(uuid,text,uuid,uuid,text,text,jsonb)"
    )
    op.execute(
        "DROP FUNCTION yoao_learning_v1.training_saved_owner_scope(text,uuid,uuid,text,text,text,jsonb)"
    )
    op.execute("DROP TABLE public.training_saved_objects")
