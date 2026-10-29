"""Fence new Learning references and retain retired deployment evidence."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0389"
down_revision = "bo0388"
branch_labels = depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_trained_model_deployment_status", "trained_model_deployments", type_="check"
    )
    op.create_check_constraint(
        "ck_trained_model_deployment_status",
        "trained_model_deployments",
        "status IN ('checking','ready','failed','draining','retired')",
    )
    directory = Path(__file__).resolve().parents[1] / "sql"
    for name in (
        "0389_inference_deployment_usage.sql",
        "0389_labeling_deployment_usage.sql",
        "0389_learning_deployment_retirement.sql",
    ):
        for statement in (
            (directory / name)
            .read_text(encoding="utf-8")
            .split("-- aap-statement-break")
        ):
            if statement.strip():
                op.execute(
                    sa.text(statement.strip().replace(":", r"\:")).execution_options(
                        no_parameters=True
                    )
                )


def downgrade() -> None:
    # Retired evidence cannot be silently changed back to a runnable deployment.
    op.execute(
        "DROP FUNCTION aap_jobs_v1.frozen_deployment_usage(uuid,uuid,uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_labeling_v1.retryable_deployment_usage(uuid,uuid,uuid) RESTRICT"
    )
    op.execute(
        "DROP FUNCTION aap_inference_pipelines_v1.frozen_snapshot_uses_deployment(uuid,uuid) RESTRICT"
    )
    op.drop_constraint(
        "ck_trained_model_deployment_status", "trained_model_deployments", type_="check"
    )
    op.create_check_constraint(
        "ck_trained_model_deployment_status",
        "trained_model_deployments",
        "status IN ('checking','ready','failed')",
    )
