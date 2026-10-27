"""Add durable Inference scope retirement and admission-expiry fences."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0280"
down_revision = "bo0279"
branch_labels = depends_on = None

_SQL_PATH = (
    Path(__file__).resolve().parents[1] / "sql" / "0280_inference_scope_retirement.sql"
)


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


_OLD_SIGNER = """
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_sign_admission(
    p_registry_id uuid,
    p_scope_id uuid,
    p_attempt_id uuid,
    p_operation text,
    p_method text,
    p_path text
)
RETURNS text
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_state record;
    v_iat bigint;
    v_header text;
    v_payload text;
    v_unsigned text;
    v_signature text;
BEGIN
    SELECT state.registry_id, state.backend_incarnation, state.authority_epoch,
           state.epoch_key, state.realm, state.continuity
    INTO v_state
    FROM aap_inference_routing_v1.registry_state AS state
    WHERE state.registry_id = p_registry_id;
    IF NOT FOUND OR v_state.continuity IS DISTINCT FROM 'ready'
       OR v_state.epoch_key IS NULL OR v_state.backend_incarnation IS NULL
       OR v_state.realm IS DISTINCT FROM aap_inference_routing_v1._routing_realm() THEN
        RETURN NULL;
    END IF;
    v_iat := pg_catalog.floor(extract(epoch FROM pg_catalog.clock_timestamp()))::bigint;
    v_header := aap_inference_routing_v1._routing_base64url(
        pg_catalog.convert_to('{"alg":"HS256","typ":"JWT"}', 'UTF8')
    );
    v_payload := aap_inference_routing_v1._routing_base64url(
        pg_catalog.convert_to(pg_catalog.jsonb_build_object(
            'schema', 'aap-route-admission/v1',
            'aud', 'ml-route',
            'registry_id', p_registry_id,
            'backend_incarnation', v_state.backend_incarnation,
            'authority_epoch', v_state.authority_epoch,
            'scope_id', p_scope_id,
            'attempt_id', p_attempt_id,
            'operation', p_operation,
            'method', p_method,
            'path', p_path,
            'iat', v_iat,
            'exp', v_iat + 300
        )::text, 'UTF8')
    );
    v_unsigned := v_header || '.' || v_payload;
    v_signature := aap_inference_routing_v1._routing_base64url(
        public.hmac(pg_catalog.convert_to(v_unsigned, 'UTF8'), v_state.epoch_key, 'sha256')
    );
    RETURN v_unsigned || '.' || v_signature;
END;
$function$
"""

_OLD_UNRESOLVED = """
CREATE OR REPLACE FUNCTION aap_inference_routing_v1._routing_has_unresolved(
    p_registry_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    UPDATE aap_inference_routing_v1.scopes AS scope
    SET state = 'expired', closed = true, updated_at = pg_catalog.now()
    WHERE scope.registry_id = p_registry_id
      AND scope.state = 'reserved'
      AND scope.heartbeat_deadline <= v_now
      AND NOT EXISTS (
          SELECT 1 FROM aap_inference_routing_v1.attempts AS attempt
          WHERE attempt.scope_id = scope.id
      );
    RETURN EXISTS (
        SELECT 1
        FROM aap_inference_routing_v1.scopes AS scope
        WHERE scope.registry_id = p_registry_id
          AND (scope.state IN ('active', 'uncertain')
               OR (scope.state = 'reserved' AND scope.heartbeat_deadline > v_now))
    ) OR EXISTS (
        SELECT 1
        FROM aap_inference_routing_v1.attempts AS attempt
        JOIN aap_inference_routing_v1.scopes AS scope ON scope.id = attempt.scope_id
        WHERE scope.registry_id = p_registry_id
          AND attempt.state IN ('active', 'uncertain')
    );
END;
$function$
"""


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
                FROM aap_inference_routing_v1.scope_retirements
                WHERE phase IN ('prepared', 'draining')
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'cannot remove Native scope retirement with open work';
            END IF;
        END
        $guard$"""
    )
    # Restore the 0279 signer before removing the high-water column it now uses.
    _execute(_OLD_SIGNER)
    _execute(_OLD_UNRESOLVED)
    _execute(
        "DROP TRIGGER aap_route_scope_retirement_guard_state "
        "ON aap_inference_routing_v1.registry_state RESTRICT"
    )
    _execute(
        "DROP TRIGGER aap_route_scope_retirement_guard_control "
        "ON aap_inference_routing_v1.controls RESTRICT"
    )
    _execute(
        "DROP FUNCTION aap_inference_routing_v1.scope_retirement_step(text, text) RESTRICT"
    )
    for signature in (
        "aap_inference_routing_v1._scope_retirement_context(uuid)",
        "aap_inference_routing_v1._scope_retirement_view(uuid)",
        "aap_inference_routing_v1._scope_retirement_rejected(text)",
        "aap_inference_routing_v1._scope_retirement_result(text, jsonb)",
        "aap_inference_routing_v1._routing_scope_retirement_guard_state()",
        "aap_inference_routing_v1._routing_scope_retirement_guard_control()",
    ):
        _execute(f"DROP FUNCTION {signature} RESTRICT")
    _execute("DROP TABLE aap_inference_routing_v1.scope_retirements RESTRICT")
    _execute(
        "ALTER TABLE aap_inference_routing_v1.scopes "
        "DROP COLUMN admission_expires_at RESTRICT"
    )
