"""Require durable video writer retirement before project/data deletion."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0300"
down_revision = "bo0299"
branch_labels = depends_on = None

_SQL_DIRECTORY = Path(__file__).resolve().parents[1] / "sql"
_SQL_PATH = _SQL_DIRECTORY / "0300_video_deletion_retirement.sql"


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def _frozen_coordinator_definition(filename: str, function_name: str) -> str:
    source = (_SQL_DIRECTORY / filename).read_text(encoding="utf-8")
    marker = f"CREATE FUNCTION aap_workspace_v1.{function_name}("
    start = source.find(marker)
    if start < 0 or source.find(marker, start + len(marker)) >= 0:
        raise RuntimeError(f"Expected one frozen {function_name} function definition")
    end = source.find("-- aap-statement-break", start)
    if end < 0:
        raise RuntimeError(
            f"Frozen {function_name} definition has no statement boundary"
        )
    definition = source[start:end].strip()
    return definition.replace("CREATE FUNCTION ", "CREATE OR REPLACE FUNCTION ", 1)


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    _execute(
        """DO $guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM aap_jobs_v1.video_deletion_clearances) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'cannot remove video deletion clearance evidence';
            END IF;
        END
        $guard$"""
    )
    _execute(
        _frozen_coordinator_definition("0199_project_deletion.sql", "delete_project")
    )
    _execute(
        _frozen_coordinator_definition("0205_dataset_unlink.sql", "unlink_dataset")
    )
    _execute(
        "DROP FUNCTION aap_labeling_v1.guard_dataset_video_deletion(uuid, uuid) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_labeling_v1.guard_project_video_deletion(uuid) RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_jobs_v1.guard_video_execution_deletion(uuid[], uuid[], uuid) RESTRICT"
    )
    _execute("DROP TABLE aap_jobs_v1.video_deletion_clearances RESTRICT")
