"""Add trainer plugins registry table and owner SQL capabilities."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0514"
down_revision = "bo0513"
branch_labels = depends_on = None

_BREAK = "-- aap-statement-break"
_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def _execute(statement: str) -> None:
    stripped = statement.strip()
    if not stripped:
        return
    op.execute(
        sa.text(stripped.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    sql = _SQL_DIR / "0514_trainer_plugins.sql"
    for statement in sql.read_text(encoding="utf-8").split(_BREAK):
        _execute(statement)


def downgrade() -> None:
    for signature in (
        "yoao_learning_v1.training_list_gpu_leases()",
        "yoao_learning_v1.transition_trainer_plugin(jsonb)",
        "yoao_learning_v1.qualify_trainer_plugin(jsonb)",
        "yoao_learning_v1.register_trainer_plugin(jsonb)",
        "yoao_learning_v1.get_enabled_trainer_plugin_by_framework(text)",
        "yoao_learning_v1.get_trainer_plugin(text)",
        "yoao_learning_v1.list_trainer_plugins(boolean, text)",
        "yoao_learning_v1._trainer_plugin_projection(yoao_learning_v1.trainer_plugins)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.drop_table("trainer_plugins", schema="yoao_learning_v1")
