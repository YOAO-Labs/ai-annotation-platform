"""Publish Labeling discussion and task-event owner capabilities."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0347"
down_revision = "bo0346"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0347_labeling_discussion.sql"


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    op.execute("DROP FUNCTION aap_labeling_v1.task_event_command(jsonb) RESTRICT")
    op.execute("DROP FUNCTION aap_labeling_v1.discussion_command(jsonb) RESTRICT")
    op.execute("DROP VIEW aap_labeling_v1.task_event_facts RESTRICT")
    op.execute("DROP VIEW aap_labeling_v1.annotation_feedback_facts RESTRICT")
    op.execute("DROP VIEW aap_labeling_v1.annotation_comment_facts RESTRICT")
