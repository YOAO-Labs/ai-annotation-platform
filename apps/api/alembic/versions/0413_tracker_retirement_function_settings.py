"""Restore Routing Native execution settings on tracker retirement helpers."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0413"
down_revision = "bo0412"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    for statement in (
        (_SQL / "0413_tracker_retirement_function_settings.sql")
        .read_text(encoding="utf-8")
        .split("-- aap-statement-break")
    ):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")))


def downgrade() -> None:
    for signature in (
        "aap_inference_routing_v1._tracker_scopes_physically_retired(uuid)",
        "aap_inference_routing_v1._guard_disabled_plugin_tracker_scope()",
    ):
        for setting in ("TimeZone", "DateStyle", "extra_float_digits"):
            op.execute(sa.text(f"ALTER FUNCTION {signature} RESET {setting}"))
