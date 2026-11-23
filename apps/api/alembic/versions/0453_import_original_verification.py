"""Persist exact Source-owned Dataset import verification receipts."""

from importlib.resources import files

from alembic import op
import sqlalchemy as sa

revision = "bo0453"
down_revision = "bo0452"
branch_labels = depends_on = None
_SCHEMA_ARTIFACTS = (
    "0453_import_original_completion.schema.json",
    "import_verification_contract.schema.json",
)


def upgrade() -> None:
    source = (
        files("yoao_assets_source")
        .joinpath("0453_import_original_verification.sql")
        .read_text(encoding="utf-8")
    )
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise RuntimeError("Cannot discard retained import verification receipts")
