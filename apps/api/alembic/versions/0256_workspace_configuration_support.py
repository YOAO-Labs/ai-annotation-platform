"""Add separately scoped facts for Workspace configuration commands."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0256"
down_revision = "bo0255"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0256_workspace_configuration_support.sql"
)
_FUNCTIONS = (
    "aap_labeling_v1.workspace_configuration_video_items(uuid)",
    "aap_labeling_v1.workspace_configuration_tracker_count(uuid)",
    "aap_labeling_v1.workspace_configuration_annotation_count(uuid)",
    "aap_labeling_v1.workspace_configuration_task_count(uuid)",
    "aap_inference_v1.workspace_configuration_source_pool(uuid)",
    "aap_inference_v1.workspace_configuration_backend(uuid)",
    "aap_assets_v1.workspace_configuration_existing_item_ids(uuid[])",
    "aap_assets_v1.workspace_configuration_dataset_scenes(uuid)",
    "aap_assets_v1.workspace_configuration_dataset_rows(uuid)",
)


def _execute(statement):
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade():
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade():
    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
