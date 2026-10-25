"""Retain accepted tracker inputs until the current Jobs run is retired."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0312"
down_revision = "0311"
branch_labels = depends_on = None

_SQL_DIRECTORY = Path(__file__).resolve().parents[1] / "sql"
_SQL_PATH = _SQL_DIRECTORY / "0312_video_tracker_input_retention.sql"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def _frozen_labeling_retention_definition() -> str:
    source = (_SQL_DIRECTORY / "0282_video_execution_checkpoints.sql").read_text(
        encoding="utf-8"
    )
    marker = (
        "CREATE OR REPLACE FUNCTION "
        "aap_labeling_v1.video_tracker_input_referenced(p_input_id uuid)"
    )
    start = source.find(marker)
    if start < 0 or source.find(marker, start + len(marker)) >= 0:
        raise RuntimeError("Expected one frozen video tracker input retention function")
    end = source.find("-- aap-statement-break", start)
    if end < 0:
        raise RuntimeError(
            "Frozen video tracker input retention function has no boundary"
        )
    return source[start:end].strip()


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    _execute(_frozen_labeling_retention_definition())
    _execute(
        "DROP FUNCTION "
        "aap_jobs_v1.video_tracker_input_retention_facts(uuid, uuid) RESTRICT"
    )
