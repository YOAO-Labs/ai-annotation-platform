"""Lock AI Mask decisions through the Labeling owner during acceptance."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0406"
down_revision = "bo0405"
branch_labels = depends_on = None


def _definition(*, lock: bool) -> str:
    source = (
        Path(__file__).resolve().parents[1] / "sql" / "0348_labeling_mask_edits.sql"
    ).read_text(encoding="utf-8")
    statement = source.split("-- aap-statement-break")[-1].strip()
    statement = statement.replace("CREATE FUNCTION ", "CREATE OR REPLACE FUNCTION ", 1)
    if lock:
        statement = statement.replace(
            "WHEN 'find_decision' THEN\n            v_expected_keys",
            "WHEN 'find_decision', 'lock_decision' THEN\n            v_expected_keys",
            1,
        ).replace(
            "IF v_operation = 'find_decision' THEN",
            "IF v_operation IN ('find_decision', 'lock_decision') THEN",
            1,
        ).replace(
            "        SELECT decision.* INTO v_decision\n"
            "        FROM public.ai_mask_accept_decisions AS decision\n"
            "        WHERE decision.task_id = v_task_id\n"
            "          AND decision.idempotency_key = p_request ->> 'idempotency_key';",
            "        IF v_operation = 'lock_decision' THEN\n"
            "            SELECT decision.* INTO v_decision\n"
            "            FROM public.ai_mask_accept_decisions AS decision\n"
            "            WHERE decision.task_id = v_task_id\n"
            "              AND decision.idempotency_key = p_request ->> 'idempotency_key'\n"
            "            FOR SHARE NOWAIT;\n"
            "        ELSE\n"
            "            SELECT decision.* INTO v_decision\n"
            "            FROM public.ai_mask_accept_decisions AS decision\n"
            "            WHERE decision.task_id = v_task_id\n"
            "              AND decision.idempotency_key = p_request ->> 'idempotency_key';\n"
            "        END IF;",
            1,
        )
    return statement


def _apply(*, lock: bool) -> None:
    op.execute(
        sa.text(_definition(lock=lock).replace(":", r"\:")).execution_options(
            no_parameters=True
        )
    )


def upgrade() -> None:
    _apply(lock=True)


def downgrade() -> None:
    _apply(lock=False)
