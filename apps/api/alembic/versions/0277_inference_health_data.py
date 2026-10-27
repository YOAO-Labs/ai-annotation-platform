"""Add fixed caller-transaction Inference health data functions."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0277"
down_revision = "bo0276"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0277_inference_health_data.sql"
)
_FUNCTIONS = (
    "aap_inference_health_data_v1.apply_reconciliation(uuid, uuid, text, text, text, bigint, text, text)",
    "aap_inference_health_data_v1.member_row(uuid)",
    "aap_inference_health_data_v1.pool_row(uuid, uuid)",
    "aap_inference_health_data_v1.member_pool(uuid)",
    "aap_inference_health_data_v1.write_backend(uuid, text, timestamptz, text, boolean, boolean)",
    "aap_inference_health_data_v1.try_promotion()",
    "aap_inference_health_data_v1.lock_resource(text)",
    "aap_inference_health_data_v1.observation_clock()",
    "aap_inference_health_data_v1.membership_row(uuid, text, boolean)",
    "aap_inference_health_data_v1.backend_row(uuid, boolean)",
    "aap_inference_health_data_v1.health_data_policy()",
)


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION {signature} RESTRICT")
    _execute("DROP SCHEMA aap_inference_health_data_v1 RESTRICT")
