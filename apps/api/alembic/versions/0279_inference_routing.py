"""Add durable, database-owned Inference routing authority."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0279"
down_revision = "bo0278"
branch_labels = depends_on = None

_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "0279_inference_routing.sql"
_TRIGGERS = (
    ("aap_route_registry_after_insert", "public.ml_backend_registry"),
    ("aap_route_registry_before_change", "public.ml_backend_registry"),
    ("aap_route_pool_before_change", "public.ml_backend_service_pools"),
    ("aap_route_pool_after_generation", "public.ml_backend_service_pools"),
    ("aap_route_member_before_change", "public.ml_backend_pool_members"),
    ("aap_route_project_pool_before_change", "public.project_ml_backend_pool"),
)
_FUNCTIONS = (
    "aap_inference_routing_v1.routing_policy()",
    "aap_inference_routing_v1.routing_step(text, text)",
    "aap_inference_routing_v1._routing_exact_keys(jsonb, text[])",
    "aap_inference_routing_v1._routing_uuid(text)",
    "aap_inference_routing_v1._routing_integer(jsonb, bigint, bigint)",
    "aap_inference_routing_v1._routing_reply(text, jsonb)",
    "aap_inference_routing_v1._routing_rejected(text)",
    "aap_inference_routing_v1._routing_receipt_safe(jsonb)",
    "aap_inference_routing_v1._routing_realm()",
    "aap_inference_routing_v1._routing_secret()",
    "aap_inference_routing_v1._routing_base64url(bytea)",
    "aap_inference_routing_v1._routing_unbase64url(text)",
    "aap_inference_routing_v1._routing_sign_snapshot(jsonb)",
    "aap_inference_routing_v1._routing_open_snapshot(text)",
    "aap_inference_routing_v1._routing_scope_capability(uuid)",
    "aap_inference_routing_v1._routing_control_capability(uuid)",
    "aap_inference_routing_v1._routing_scope_view(uuid, boolean)",
    "aap_inference_routing_v1._routing_control_view(uuid, boolean)",
    "aap_inference_routing_v1._routing_transport_identity(uuid)",
    "aap_inference_routing_v1._routing_max_concurrency(uuid)",
    "aap_inference_routing_v1._routing_sign_admission(uuid, uuid, uuid, text, text, text)",
    "aap_inference_routing_v1._routing_lock_admission_pool(uuid)",
    "aap_inference_routing_v1._routing_has_unresolved(uuid)",
    "aap_inference_routing_v1._routing_has_control(uuid)",
    "aap_inference_routing_v1._routing_registry_after_insert()",
    "aap_inference_routing_v1._routing_registry_before_change()",
    "aap_inference_routing_v1._routing_pool_before_change()",
    "aap_inference_routing_v1._routing_pool_after_generation()",
    "aap_inference_routing_v1._routing_member_before_change()",
    "aap_inference_routing_v1._routing_project_pool_before_change()",
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
    _execute(
        """DO $guard$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM aap_inference_routing_v1.scopes
                WHERE state IN ('reserved', 'active', 'uncertain')
            ) OR EXISTS (
                SELECT 1
                FROM aap_inference_routing_v1.controls
                WHERE phase IN ('prepared', 'draining', 'uncertain')
            ) OR EXISTS (
                SELECT 1
                FROM aap_inference_routing_v1.attempts
                WHERE state IN ('active', 'uncertain')
            ) OR EXISTS (
                SELECT 1
                FROM aap_inference_routing_v1.control_attempts
                WHERE state IN ('active', 'uncertain')
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'cannot remove routing authority with unresolved work';
            END IF;
        END
        $guard$"""
    )
    for trigger, table in _TRIGGERS:
        _execute(f"DROP TRIGGER {trigger} ON {table} RESTRICT")
    for signature in _FUNCTIONS:
        _execute(f"DROP FUNCTION {signature} RESTRICT")
    for table in (
        "control_attempts",
        "attempts",
        "command_receipts",
        "controls",
        "scopes",
        "weights",
        "registry_state",
        "authority_secret",
    ):
        _execute(f"DROP TABLE aap_inference_routing_v1.{table} RESTRICT")
    _execute("DROP SCHEMA aap_inference_routing_v1 RESTRICT")
