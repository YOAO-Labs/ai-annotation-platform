"""Fence DatasetItem deletion against scene history and retained Assets work.

Revision ID: bo0230
Revises: bo0229
"""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0230"
down_revision = "bo0229"
branch_labels = depends_on = None

_ARTIFACT = (
    Path(__file__).resolve().parents[1]
    / "sql"
    / "0230_dataset_source_aux_retention.sql"
)
_STATEMENT_SEPARATOR = "-- aap-statement-break"


def _statements(filename: Path) -> list[str]:
    return [
        statement.strip()
        for statement in filename.read_text(encoding="utf-8").split(
            _STATEMENT_SEPARATOR
        )
        if statement.strip()
    ]


def _base_function(artifact: str, signature: str) -> str:
    source = (Path(__file__).resolve().parents[1] / "sql" / artifact).read_text(
        encoding="utf-8"
    )
    marker = f"CREATE FUNCTION {signature}"
    start = source.index(marker)
    end = source.index("$function$;", start) + len("$function$;")
    return source[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def upgrade() -> None:
    for statement in _statements(_ARTIFACT):
        op.execute(sa.text(statement.replace(":", r"\:")))


def downgrade() -> None:
    for artifact, signature in (
        (
            "0220_assets_images_state.sql",
            "aap_assets_v1._cancel_image_source_work(",
        ),
        ("0209_dataset_source_deletion.sql", "aap_assets_v1._delete_dataset_source("),
        (
            "0209_dataset_source_deletion.sql",
            "aap_assets_v1._lock_dataset_source_deletion(",
        ),
        (
            "0206_dataset_source_guards.sql",
            "aap_labeling_v1.guard_dataset_source_deletion(",
        ),
    ):
        op.execute(_base_function(artifact, signature))
    for signature in (
        "aap_assets_v1.guard_dataset_source_auxiliary_retention(uuid, uuid)",
        "aap_assets_v1.dataset_item_scene_references(uuid, uuid)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
