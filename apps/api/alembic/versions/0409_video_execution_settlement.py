"""Prove terminal video execution settlement before issuing another claim."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0409"
down_revision = "bo0408"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in (_SQL / "0409_video_execution_settlement.sql").read_text(
        encoding="utf-8"
    ).split("-- aap-statement-break"):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    # Restore the immutable predecessor definition without dropping the public
    # function, preserving its existing function-only role grants.
    statements = (_SQL / "0281_video_execution.sql").read_text(
        encoding="utf-8"
    ).split("-- aap-statement-break")
    definition = next(
        statement for statement in statements
        if statement.strip().startswith(
            "CREATE OR REPLACE FUNCTION aap_jobs_v1.video_execution_step("
        )
    )
    _execute(definition.strip())
    _execute(
        "DROP FUNCTION aap_jobs_v1._video_execution_settled(uuid,uuid,text,text,uuid) RESTRICT"
    )
