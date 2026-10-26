"""Complete portable Inference topology, endpoint and GPU capabilities."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "0369"
down_revision = "0368"
branch_labels = None
depends_on = None

_SQL_NAMES = (
    "0369_inference_topology.sql",
    "0369_inference_seed_ingress.sql",
    "0369_inference_fixtures.sql",
    "0369_inference_backend_endpoint.sql",
    "0369_inference_health.sql",
    "0369_inference_gpu.sql",
)


def upgrade() -> None:
    directory = Path(__file__).resolve().parents[1] / "sql"
    for name in _SQL_NAMES:
        for statement in (
            (directory / name)
            .read_text(encoding="utf-8")
            .split("-- aap-statement-break")
        ):
            if statement.strip():
                op.execute(
                    sa.text(statement.strip()).execution_options(no_parameters=True)
                )
    # Existing invoker triggers resolve their public owner tables under Native calls.
    for name in (
        "validate_gpu_backend_membership",
        "validate_gpu_backend_fence_delete",
    ):
        op.execute(
            f"ALTER FUNCTION public.{name}() "
            "SET search_path = pg_catalog, public, pg_temp"
        )


def downgrade() -> None:
    for schema in (
        "aap_inference_gpu_caller_v1",
        "aap_inference_gpu_collector_v1",
        "aap_inference_gpu_v1",
        "aap_inference_backend_endpoint_v1",
        "aap_inference_topology_v1",
    ):
        op.execute(f"DROP SCHEMA {schema} CASCADE")
    for name in (
        "validate_gpu_backend_membership",
        "validate_gpu_backend_fence_delete",
    ):
        op.execute(f"ALTER FUNCTION public.{name}() RESET search_path")
