"""Preserve the saved Mask staging reference through Labeling transfer."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0431"
down_revision = "bo0430"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_FUNCTION = (
    "CREATE FUNCTION aap_labeling_v1.annotation_transfer_command(p_request jsonb)"
)


def _apply(sql: str) -> None:
    op.execute(sa.text(sql.replace(":", r"\:")).execution_options(no_parameters=True))


def upgrade() -> None:
    _apply((_SQL / "0431_labeling_mask_staging.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    source = (_SQL / "0351_labeling_annotation_transfer.sql").read_text(
        encoding="utf-8"
    )
    matches = [
        part for part in source.split("-- aap-statement-break") if _FUNCTION in part
    ]
    if len(matches) != 1:
        raise RuntimeError("Frozen Labeling annotation transfer function is not unique")
    _apply(
        matches[0].replace(
            _FUNCTION,
            _FUNCTION.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"),
            1,
        )
    )
