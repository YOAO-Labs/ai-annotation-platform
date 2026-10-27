"""Add encrypted, transaction-bound health observation evidence."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0276"
down_revision = "bo0275"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0276_inference_health_observation.sql"
)
_FUNCTIONS = (
    "aap_inference_health_observation_v1.verify_health_observation(text, uuid, uuid, text)",
    "aap_inference_health_observation_v1.seal_health_observation(text, uuid, uuid, text)",
    "aap_inference_health_observation_v1.open_health_observation_subject(text, uuid, uuid)",
    "aap_inference_health_observation_v1.capture_health_observation_subject(uuid, uuid, text, text, double precision)",
    "aap_inference_health_observation_v1.health_observation_policy()",
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
    _execute(
        "DROP TABLE aap_inference_health_observation_v1.observation_secret RESTRICT"
    )
    _execute("DROP SCHEMA aap_inference_health_observation_v1 RESTRICT")
