"""Fence historical tracker tombstones and align Jobs retention with deletion."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0313"
down_revision = "bo0312"
branch_labels = depends_on = None

_SQL_DIRECTORY = Path(__file__).resolve().parents[1] / "sql"
_SQL_PATH = _SQL_DIRECTORY / "0313_video_tracker_input_garbage_retention.sql"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def _frozen_function_definition(filename: str, marker: str) -> str:
    source = (_SQL_DIRECTORY / filename).read_text(encoding="utf-8")
    start = source.find(marker)
    if start < 0 or source.find(marker, start + len(marker)) >= 0:
        raise RuntimeError(f"Expected one frozen function definition: {marker}")
    end = source.find("-- aap-statement-break", start)
    if end < 0:
        raise RuntimeError(f"Frozen function definition has no boundary: {marker}")
    definition = source[start:end].strip()
    if definition.startswith("CREATE FUNCTION "):
        definition = definition.replace(
            "CREATE FUNCTION ", "CREATE OR REPLACE FUNCTION ", 1
        )
    return definition


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    _execute(
        _frozen_function_definition(
            "0225_video_inputs_garbage.sql",
            "CREATE FUNCTION aap_assets_v1.claim_accepted_video_input_garbage(",
        )
    )
    _execute(
        _frozen_function_definition(
            "0312_video_tracker_input_retention.sql",
            "CREATE FUNCTION aap_jobs_v1.video_tracker_input_retention_facts(",
        )
    )
