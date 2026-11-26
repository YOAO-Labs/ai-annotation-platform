"""Add external format adapter plugins table and owner SQL capabilities."""

from importlib.resources import files

import sqlalchemy as sa
from alembic import op

revision = "bo0511"
down_revision = "bo0510"
branch_labels = depends_on = None

_BREAK = "-- aap-statement-break"


def _execute(statement: str) -> None:
    stripped = statement.strip()
    if not stripped:
        return
    op.execute(
        sa.text(stripped.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    sql = files("yoao_labeling.sql").joinpath("0511_format_adapter_plugins.sql")
    for statement in sql.read_text(encoding="utf-8").split(_BREAK):
        _execute(statement)


def downgrade() -> None:
    for signature in (
        "yoao_labeling_v1.open_format_adapter_plugin_secret(text)",
        "yoao_labeling_v1.transition_format_adapter_plugin(jsonb)",
        "yoao_labeling_v1.register_format_adapter_plugin(jsonb)",
        "yoao_labeling_v1.get_format_adapter_plugin(text)",
        "yoao_labeling_v1.list_format_adapter_plugins(boolean)",
        "yoao_labeling_v1._format_adapter_public_projection(yoao_labeling_v1.format_adapter_plugins)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.drop_table("format_adapter_plugins", schema="yoao_labeling_v1")
