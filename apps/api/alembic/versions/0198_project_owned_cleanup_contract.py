"""Add module-owned project cleanup capabilities.

Revision ID: bo0198
Revises: bo0197
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0198"
down_revision = "bo0197"
branch_labels = depends_on = None

_ARTIFACT_NAME = "0198_project_owned_cleanup_contract.sql"
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
    op.execute("DROP FUNCTION aap_jobs_v1.detach_project_references(uuid) RESTRICT")
    op.execute(
        "DROP FUNCTION aap_export_v1.clear_project_export_artifacts(uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_diagnostics_v1.detach_project_references(uuid) RESTRICT"
    )
    op.execute("DROP FUNCTION aap_insights_v1.clear_project_task_views(uuid) RESTRICT")
    op.execute("DROP FUNCTION aap_inference_v1.clear_project_pipelines(uuid) RESTRICT")
    op.execute(
        "DROP FUNCTION aap_inference_v1.clear_project_ml_backend_pool(uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_assets_v1.delete_project_owned_records(uuid) RESTRICT"
    )
    op.execute("DROP SCHEMA aap_export_v1 RESTRICT")
    op.execute("DROP SCHEMA aap_diagnostics_v1 RESTRICT")
    op.execute("DROP SCHEMA aap_insights_v1 RESTRICT")
    op.execute("DROP SCHEMA aap_inference_v1 RESTRICT")
