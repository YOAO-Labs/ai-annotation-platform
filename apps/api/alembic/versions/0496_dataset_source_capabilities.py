"""Move ordinary dataset and directory workflows to fixed Assets capabilities."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0496"
down_revision = "bo0495"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_BREAK = "-- aap-statement-break"
_SOURCES = (
    "0496_dataset_sources.sql",
    "0496_dataset_catalog_data.sql",
    "0496_dataset_geometry_fences.sql",
    "0496_source_scan.sql",
    "0496_source_registration.sql",
    "0496_directory_source_controls.sql",
    "0496_directory_source_usage.sql",
    "0496_directory_previews.sql",
    "0496_dataset_references.sql",
    "0496_dataset_workbench.sql",
    "0496_dataset_quality.sql",
)


def upgrade() -> None:
    for name in _SOURCES:
        for statement in (_SQL / name).read_text(encoding="utf-8").split(_BREAK):
            if statement.strip():
                op.execute(
                    sa.text(statement.strip().replace(":", r"\:")).execution_options(
                        no_parameters=True
                    )
                )


def downgrade() -> None:
    raise NotImplementedError(
        "Restore the previous dataset capability deployment as a unit"
    )
