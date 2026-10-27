"""Add fixed, typed foreign-owned facts for Workspace project reads."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0260"
down_revision = "bo0259"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0260_workspace_project_query_support.sql"
)
_FUNCTIONS = (
    "aap_inference_v1.workspace_project_query_pool(uuid)",
    "aap_jobs_v1.workspace_project_query_latest_job_status(uuid)",
    "aap_jobs_v1.workspace_project_query_job_status_counts(uuid)",
    "aap_labeling_v1.workspace_project_query_batch_task_counts(uuid)",
    "aap_labeling_v1.workspace_project_query_batches(uuid)",
    "aap_labeling_v1.workspace_project_query_task_count(uuid)",
    "aap_labeling_v1.workspace_project_query_linked_task_count(uuid, uuid[])",
    "aap_assets_v1.workspace_project_query_dataset_item_ids(uuid[])",
    "aap_assets_v1.workspace_project_query_dataset_item_count(uuid[])",
    "aap_assets_v1.workspace_project_query_dataset_ids(uuid)",
    "aap_labeling_v1.workspace_project_query_annotation_series(uuid[], timestamptz[])",
    "aap_labeling_v1.workspace_project_query_task_series(uuid[], timestamptz[])",
    "aap_labeling_v1.workspace_project_query_annotation_count(uuid[], boolean)",
    "aap_labeling_v1.workspace_project_query_ai_counts(uuid[])",
    "aap_labeling_v1.workspace_project_query_counters(uuid[])",
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
