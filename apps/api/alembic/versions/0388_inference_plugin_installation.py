"""Add the Inference Registry installation transaction boundary."""
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0388"
down_revision = "bo0387"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"


def _execute_file(name: str) -> None:
    for statement in (_SQL / name).read_text(encoding="utf-8").split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip().replace(":", r"\:")).execution_options(no_parameters=True))


def upgrade() -> None:
    _execute_file("0388_inference_plugin_installation.sql")
    _execute_file("0388_inference_plugin_ingress.sql")


def downgrade() -> None:
    _execute_file("0388_inference_plugin_ingress_down.sql")
    for signature in (
        "command_plugin_installation(uuid,integer,text,text)",
        "plugin_installation_evidence(uuid)",
        "plugin_installations()",
        "plugin_installation(uuid)",
        "prepare_plugin_installation(text)",
        "lock_plugin_registry(uuid)",
    ):
        op.execute(f"DROP FUNCTION aap_inference_topology_v1.{signature} RESTRICT")
