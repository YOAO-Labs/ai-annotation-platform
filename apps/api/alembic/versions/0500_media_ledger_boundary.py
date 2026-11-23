"""Install the transaction-borrowing legacy media ledger capabilities."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0500"
down_revision = "bo0499"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def upgrade() -> None:
    for family in (
        "thumbnail_outputs",
        "image_pyramids",
        "video_derived",
        "video_chapters",
        "media_cache_binding_history",
    ):
        source = (_SQL / f"0500_{family}.sql").read_text(encoding="utf-8")
        for definition in source.split("-- aap-statement-break"):
            declaration = definition.strip()
            if not declaration:
                continue
            if not declaration.startswith("CREATE FUNCTION "):
                raise RuntimeError(f"Unexpected bo0500 definition in {family}")
            name = declaration.removeprefix("CREATE FUNCTION ").split("(", 1)[0].strip()
            op.execute(
                sa.text(definition.replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )
            op.execute(f"REVOKE ALL ON FUNCTION {name} FROM PUBLIC")


def downgrade() -> None:
    raise NotImplementedError(
        "Media ledger capabilities are forward-only; restore a coordinated backup"
    )
