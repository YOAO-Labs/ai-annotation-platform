"""Fence Native GPU workload admission against inactive plugin installations."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0390"
down_revision = "bo0389"
branch_labels = None
depends_on = None


def _apply(filename: str) -> None:
    payload = (Path(__file__).resolve().parents[1] / "sql" / filename).read_text(
        encoding="utf-8"
    )
    for statement in payload.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(sa.text(statement.strip()).execution_options(no_parameters=True))


def upgrade() -> None:
    _apply("0390_inference_gpu.sql")


def downgrade() -> None:
    _apply("0370_inference_gpu.sql")
    payload = (Path(__file__).resolve().parents[1] / "sql" / "0369_inference_gpu.sql").read_text(encoding="utf-8")
    policy = next(
        statement.strip()
        for statement in payload.split("-- aap-statement-break")
        if statement.strip().startswith("CREATE FUNCTION aap_inference_gpu_v1.gpu_authority_policy()")
    )
    op.execute(sa.text(policy.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)).execution_options(no_parameters=True))
    op.execute("DROP TABLE aap_inference_gpu_v1.installation_terminal_intents")
    op.execute("DROP TABLE aap_inference_gpu_v1.installation_control_intents")
