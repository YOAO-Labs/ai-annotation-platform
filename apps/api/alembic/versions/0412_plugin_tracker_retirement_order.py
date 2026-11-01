"""Fence plugin stop against unretired tracker scopes."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0412"
down_revision = "bo0411"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in (_SQL / "0412_plugin_tracker_retirement_order.sql").read_text(
        encoding="utf-8"
    ).split("-- aap-statement-break"):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    statements = (_SQL / "0400_plugin_routing_replacement.sql").read_text(
        encoding="utf-8"
    ).split("-- aap-statement-break")
    definition = next(
        statement for statement in statements
        if statement.strip().startswith(
            "CREATE OR REPLACE FUNCTION aap_inference_topology_v1.command_plugin_installation("
        )
    )
    _execute(definition.strip())
    _execute(
        "DROP TRIGGER aap_guard_disabled_plugin_tracker_scope "
        "ON aap_inference_routing_v1.scopes"
    )
    _execute(
        "DROP FUNCTION aap_inference_routing_v1._guard_disabled_plugin_tracker_scope()"
    )
    _execute(
        "DROP FUNCTION aap_inference_routing_v1._tracker_scopes_physically_retired(uuid)"
    )
