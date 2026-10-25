"""Real function-only Identity LOGINS against a verifier-owned test database."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from tests.test_identity_auth_native import _cleanup, _seed


SURFACES = {
    "AUTH": {
        "authentication_policy",
        "read_actor_context",
        "read_jwt_principal",
        "read_api_key_candidates",
        "confirm_api_key_principal",
    },
    "COMMAND": {
        "get_credential_generation",
        "advance_credential_generation",
        "revoke_jti",
        "is_jti_revoked",
        "retire_account_credentials",
    },
    "FENCE": {"lock_current_principal"},
    "OPERATOR": {"purge_expired_revocations"},
}


def _dsn(kind):
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity LOGIN acceptance requires the owned module-data verifier")
    value = os.environ.get(f"AAP_MODULE_DATA_IDENTITY_{kind}_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database.endswith("_test") and parsed.host in {
        "127.0.0.1",
        "localhost",
    }
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SURFACES)
async def test_identity_logins_have_exact_functions_and_no_private_or_ddl_access(
    test_engine, kind
):
    fixture = await _seed(test_engine)
    connection = await asyncpg.connect(_dsn(kind))
    cleanup_jtis = []
    try:
        actual = await connection.fetch("""
            SELECT namespace.nspname AS schema, function.proname AS name
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace
            WHERE left(namespace.nspname,4)='aap_'
              AND pg_catalog.has_function_privilege(current_user,function.oid,'EXECUTE')
        """)
        assert {(row["schema"], row["name"]) for row in actual} == {
            ("aap_identity_v1", name) for name in SURFACES[kind]
        }
        assert not await connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=relation.relnamespace
                WHERE namespace.nspname='public' AND relation.relkind IN ('r','p','v','m')
                AND (pg_catalog.has_table_privilege(current_user,relation.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')
                    OR pg_catalog.has_any_column_privilege(current_user,relation.oid,'SELECT,INSERT,UPDATE,REFERENCES'))
            )
        """)
        assert not await connection.fetchval(
            "SELECT has_database_privilege(current_user,current_database(),'CREATE,TEMP')"
        )
        assert not await connection.fetchval(
            "SELECT pg_has_role(current_user,'identity_code','USAGE')"
        )
        for sql in (
            "SELECT password_hash FROM public.users LIMIT 1",
            "CREATE TEMP TABLE identity_forbidden(id integer)",
            "SET ROLE identity_code",
            "SELECT aap_identity_v1._admission_failure('credential_invalid')",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
        if kind != "AUTH":
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(
                    "SELECT aap_identity_v1.read_actor_context(NULL::uuid,'super_admin')"
                )

        actor = fixture["actor_id"]
        jti = uuid.uuid4()
        expires = datetime.now(UTC) + timedelta(minutes=15)
        if kind == "AUTH":
            policy = json.loads(
                await connection.fetchval(
                    "SELECT aap_identity_v1.authentication_policy()"
                )
            )
            assert policy["issuer"] == "aap-identity/v1"
            context = json.loads(
                await connection.fetchval(
                    "SELECT aap_identity_v1.read_actor_context($1,$2)",
                    actor,
                    "employee",
                )
            )
            assert context["data"]["id"] == str(actor)
            assert context["data"]["role"] == "employee"
            assert context["data"]["is_active"] is True
            assert context["data"]["weekly_target_default"] is None
            result = json.loads(
                await connection.fetchval(
                    "SELECT aap_identity_v1.read_jwt_principal($1,$2,0,$3,false)",
                    actor,
                    jti,
                    expires,
                )
            )
            assert result["data"]["actor_id"] == str(actor)
            page = json.loads(
                await connection.fetchval(
                    "SELECT aap_identity_v1.read_api_key_candidates($1,NULL,100)",
                    "ak_123456789",
                )
            )
            candidate = next(
                row for row in page["items"] if row["id"] == str(fixture["key_id"])
            )
            key = json.loads(
                await connection.fetchval(
                    "SELECT aap_identity_v1.confirm_api_key_principal($1,$2)",
                    fixture["key_id"],
                    candidate["key_hash"],
                )
            )
            assert key["data"]["credential_type"] == "api_key"
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(
                    "SELECT aap_identity_v1.advance_credential_generation($1)", actor
                )
        elif kind == "COMMAND":
            cleanup_jtis.append(jti)
            assert (
                await connection.fetchval(
                    "SELECT aap_identity_v1.get_credential_generation($1)", actor
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT aap_identity_v1.advance_credential_generation($1)", actor
                )
                == 1
            )
            assert await connection.fetchval(
                "SELECT aap_identity_v1.revoke_jti($1,$2)", jti, expires
            )
            assert await connection.fetchval(
                "SELECT aap_identity_v1.is_jti_revoked($1)", jti
            )
            retired = json.loads(
                await connection.fetchval(
                    "SELECT aap_identity_v1.retire_account_credentials($1)", actor
                )
            )
            assert retired["generation"] == 2 and retired["revoked_key_ids"] == [
                str(fixture["key_id"])
            ]
        elif kind == "FENCE":
            async with connection.transaction():
                fenced = json.loads(
                    await connection.fetchval(
                        "SELECT aap_identity_v1.lock_current_principal($1,'jwt',$2,0,$3,ARRAY[]::text[])",
                        actor,
                        jti,
                        expires,
                    )
                )
                assert fenced["data"]["actor_id"] == str(actor)
        else:
            removed = await connection.fetchval(
                "SELECT aap_identity_v1.purge_expired_revocations(1)"
            )
            assert 0 <= removed <= 1
    finally:
        if connection is not None:
            await connection.close()
        await _cleanup(test_engine, fixture, cleanup_jtis)
