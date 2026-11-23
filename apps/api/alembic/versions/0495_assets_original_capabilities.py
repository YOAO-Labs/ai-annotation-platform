"""Bind original identity and lifecycle calls to the Assets owner."""

from importlib.resources import files

from alembic import op
import sqlalchemy as sa

revision = "bo0495"
down_revision = "bo0494"
branch_labels = depends_on = None


def upgrade() -> None:
    source = files("yoao_assets_source").joinpath("0495_original_capabilities.sql")
    for statement in source.read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def downgrade() -> None:
    raise RuntimeError("Cannot downgrade the function-only original owner boundary")
