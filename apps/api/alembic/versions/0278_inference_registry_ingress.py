"""Add fixed Registry ingress functions and immutable sync receipts."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0278"
down_revision = "bo0277"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0278_inference_registry_ingress.sql"
)
_FUNCTIONS = (
    "aap_inference_registry_ingress_v1.record_sync_result(uuid, text, integer)",
    "aap_inference_registry_ingress_v1.sync_result(uuid, text)",
    "aap_inference_registry_ingress_v1.tag_seed_registry(uuid, text)",
    "aap_inference_registry_ingress_v1.disconnect_env_registry(uuid, text)",
    "aap_inference_registry_ingress_v1.apply_observation(uuid, text, text, text, boolean, text, timestamptz, text, boolean, text)",
    "aap_inference_registry_ingress_v1.create_singleton_pool(uuid, uuid, uuid, text, text, text)",
    "aap_inference_registry_ingress_v1.singleton_pool(uuid)",
    "aap_inference_registry_ingress_v1.create_registry(uuid, text, text, text, boolean, text)",
    "aap_inference_registry_ingress_v1.registry_candidates(text)",
    "aap_inference_registry_ingress_v1.registry_by_url(text)",
    "aap_inference_registry_ingress_v1.registry_ingress_policy()",
)


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for statement in _SQL_PATH.read_text(encoding="utf-8").split(
        "-- aap-statement-break"
    ):
        if statement.strip():
            _execute(statement.strip())


def downgrade() -> None:
    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION {signature} RESTRICT")
    _execute("DROP TABLE aap_inference_registry_ingress_v1.env_sync_receipts RESTRICT")
    _execute("DROP SCHEMA aap_inference_registry_ingress_v1 RESTRICT")
    _execute("ALTER FUNCTION public.sync_gpu_backend_membership() RESET search_path")
