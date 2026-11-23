"""Bind registered directory byte I/O to exact, short-lived Source proofs."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0504"
down_revision = "bo0503"
branch_labels = depends_on = None


def upgrade() -> None:
    sql = Path(__file__).resolve().parents[1] / "sql" / "0504_reference_io.sql"
    for statement in sql.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE yoao_assets_v1.reference_io_requests IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $function$
        BEGIN
            IF EXISTS (SELECT FROM yoao_assets_v1.reference_io_requests) THEN
                RAISE EXCEPTION 'Drain reference I/O before downgrade';
            END IF;
        END
        $function$;
    """)
    for signature in (
        "recheck_reference_io(uuid,text)",
        "capture_reference_io(uuid,text)",
        "prepare_reference_io(uuid,text,uuid,uuid,text,jsonb)",
        "_reference_io_scope(uuid,text,uuid,uuid,text,jsonb)",
    ):
        op.execute(f"DROP FUNCTION yoao_assets_v1.{signature}")
    op.execute("DROP TABLE yoao_assets_v1.reference_io_requests")
