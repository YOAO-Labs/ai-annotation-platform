"""Add stable Assets association read capabilities.

Revision ID: bo0212
Revises: bo0211
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0212"
down_revision = "bo0211"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0212_asset_association_reads.sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def _artifact_statements() -> list[str]:
    artifact = Path(__file__).resolve().parents[1] / "sql" / _ARTIFACT_NAME
    return [
        statement.strip()
        for statement in artifact.read_text(encoding="utf-8").split(
            _STATEMENT_SEPARATOR
        )
        if statement.strip()
    ]


def upgrade() -> None:
    for statement in _artifact_statements():
        op.execute(sa.text(statement))


def downgrade() -> None:
    for signature in (
        "aap_assets_v1.list_project_datasets(uuid, uuid)",
        "aap_assets_v1.list_dataset_projects(uuid, uuid)",
        "aap_assets_v1._association_read_failure(text)",
        "aap_labeling_v1.project_dataset_task_counts(uuid)",
        "aap_workspace_v1.read_project_visibility(uuid, uuid)",
    ):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
    for view in (
        "aap_assets_v1.project_dataset_item_sources",
        "aap_labeling_v1.project_catalog_counters",
        "aap_workspace_v1.project_catalog_configs",
    ):
        op.execute(f"DROP VIEW {view} RESTRICT")
    for signature in (
        "aap_workspace_v1._project_read_failure(text)",
        "aap_identity_v1.read_actor_facts(uuid)",
    ):
        op.execute(f"DROP FUNCTION {signature} RESTRICT")
