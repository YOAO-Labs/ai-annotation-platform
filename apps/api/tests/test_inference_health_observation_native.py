"""Owned PostgreSQL acceptance for transaction-bound health observations."""

from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest

from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_SCHEMA = "aap_inference_health_observation_v1"
_POLICY = f"{_SCHEMA}.health_observation_policy()"
_CAPTURE = f"{_SCHEMA}.capture_health_observation_subject"
_OPEN = f"{_SCHEMA}.open_health_observation_subject"
_SEAL = f"{_SCHEMA}.seal_health_observation"
_VERIFY = f"{_SCHEMA}.verify_health_observation"
_KEY_TABLE = f"{_SCHEMA}.observation_secret"
_ENDPOINT_SCHEMA = "aap_inference_backend_endpoint_v1"
_ENDPOINT_CAPTURE = f"{_ENDPOINT_SCHEMA}.capture_backend_endpoint_subject"
_ENDPOINT_PROTOCOL = "aap-inference-backend-endpoint/v1"
_ORIGIN_ROLE = "inference_health_origin_client"
_PROVIDER_ROLE = "inference_health_observation_client"
_API_ROLE = "legacy_runtime"
_FUNCTIONS = (
    f"{_SCHEMA}.health_observation_policy()",
    f"{_SCHEMA}.capture_health_observation_subject(uuid,uuid,text,text,double precision)",
    f"{_SCHEMA}.open_health_observation_subject(text,uuid,uuid)",
    f"{_SCHEMA}.seal_health_observation(text,uuid,uuid,text)",
    f"{_SCHEMA}.verify_health_observation(text,uuid,uuid,text)",
)
_CATALOG_FUNCTIONS = (
    f"{_SCHEMA}.health_observation_policy()",
    f"{_SCHEMA}.capture_health_observation_subject(uuid, uuid, text, text, double precision)",
    f"{_SCHEMA}.open_health_observation_subject(text, uuid, uuid)",
    f"{_SCHEMA}.seal_health_observation(text, uuid, uuid, text)",
    f"{_SCHEMA}.verify_health_observation(text, uuid, uuid, text)",
)
_POLICY_VALUE = {
    "schema": "aap-inference-health-observation-native/v1",
    "observation_protocol": "aap-inference-health-observation/v1",
    "subject_protocol": "aap-inference-health-observation-subject/v1",
    "evidence_protocol": "aap-inference-health-observation-evidence/v1",
    "origin_transaction": "in_progress",
    "evidence_protection": "pgcrypto-pgp-aes256-mdc",
    "functions": list(_FUNCTIONS),
    "ready": True,
}


def _owner_dsn(test_engine) -> str:
    value = test_engine.url.set(drivername="postgresql")
    assert value.database and value.database.endswith(("_test", "_e2e"))
    return value.render_as_string(hide_password=False)


async def _connect(test_engine, *, role: str | None = None):
    connection = await asyncpg.connect(
        _owner_dsn(test_engine), timeout=5, command_timeout=10
    )
    if role is not None:
        await connection.execute(f"SET ROLE {role}")
        assert await connection.fetchval("SELECT current_user") == role
    return connection


async def _seed_backend(owner, registry_id, url, token, extra_params) -> None:
    """Seed the registered endpoint through the real owner connection."""
    await owner.execute(
        "INSERT INTO public.ml_backend_registry "
        "(id,name,url,state,auth_method,auth_token,extra_params) "
        "VALUES ($1,$2,$3,'disconnected','token',$4,CAST($5 AS jsonb))",
        registry_id,
        f"Health observation {registry_id}",
        url,
        token,
        json.dumps(extra_params),
    )


async def _rotate_backend(owner, registry_id, token) -> None:
    await owner.execute(
        "UPDATE public.ml_backend_registry SET auth_token=$2 WHERE id=$1",
        registry_id,
        token,
    )


async def _delete_backend(owner, registry_id) -> None:
    await owner.execute(
        "DELETE FROM public.ml_backend_registry WHERE id=$1", registry_id
    )


async def _endpoint_capture(connection, registry_id, request_id) -> str:
    raw = await connection.fetchval(
        f"SELECT {_ENDPOINT_CAPTURE}($1::uuid,$2::uuid,$3::text,$4::text)",
        registry_id,
        request_id,
        "health_observation",
        None,
    )
    assert type(raw) is str
    captured = json.loads(raw)
    assert captured["status"] == "ok"
    assert type(captured["subject"]) is str
    return captured["subject"]


def _endpoint_envelope(registry_id, request_id, subject) -> dict:
    return {
        "schema": _ENDPOINT_PROTOCOL,
        "request_id": str(request_id),
        "registry_id": str(registry_id),
        "purpose": "health_observation",
        "authority": None,
        "subject": subject,
    }


def _observation_backend(registry_id, endpoint_subject) -> str:
    return json.dumps(
        {"id": str(registry_id), "endpoint_subject": endpoint_subject},
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def _capture(
    connection,
    registry_id: uuid.UUID,
    request_id: uuid.UUID,
    backend_json: str,
    *,
    challenge: str | None = None,
    timeout: float = 2.0,
) -> str:
    return await connection.fetchval(
        f"SELECT {_CAPTURE}($1::uuid,$2::uuid,$3::text,$4::text,$5::double precision)",
        registry_id,
        request_id,
        backend_json,
        challenge,
        timeout,
    )


async def _open(
    connection,
    subject: str,
    registry_id: uuid.UUID,
    request_id: uuid.UUID,
) -> str:
    return await connection.fetchval(
        f"SELECT {_OPEN}($1::text,$2::uuid,$3::uuid)",
        subject,
        registry_id,
        request_id,
    )


async def _seal(
    connection,
    subject: str,
    registry_id: uuid.UUID,
    request_id: uuid.UUID,
    observation_json: str,
) -> str:
    return await connection.fetchval(
        f"SELECT {_SEAL}($1::text,$2::uuid,$3::uuid,$4::text)",
        subject,
        registry_id,
        request_id,
        observation_json,
    )


async def _verify(
    connection,
    subject: str,
    registry_id: uuid.UUID,
    request_id: uuid.UUID,
    evidence: str,
) -> str:
    return await connection.fetchval(
        f"SELECT {_VERIFY}($1::text,$2::uuid,$3::uuid,$4::text)",
        subject,
        registry_id,
        request_id,
        evidence,
    )


async def _assert_invalid(
    connection,
    statement: str,
    *args,
    message: str,
) -> None:
    with pytest.raises(asyncpg.PostgresError) as caught:
        await connection.fetchval(statement, *args)
    assert caught.value.sqlstate == "22023"
    assert caught.value.message == message


async def _assert_invalid_in_transaction(
    connection,
    statement: str,
    *args,
    message: str,
) -> None:
    await connection.execute("SAVEPOINT health_observation_invalid")
    try:
        await _assert_invalid(connection, statement, *args, message=message)
    finally:
        await connection.execute("ROLLBACK TO SAVEPOINT health_observation_invalid")
        await connection.execute("RELEASE SAVEPOINT health_observation_invalid")


def _tamper(value: str) -> str:
    index = 4
    replacement = "A" if value[index] != "A" else "B"
    return value[:index] + replacement + value[index + 1 :]


async def test_health_observation_round_trip_is_bound_to_origin_and_keeps_raw_text(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    api = await _connect(test_engine, role=_API_ROLE)
    origin = await _connect(test_engine, role=_ORIGIN_ROLE)
    other_origin = await _connect(test_engine, role=_ORIGIN_ROLE)
    provider = await _connect(test_engine, role=_PROVIDER_ROLE)
    registry_id = uuid.uuid4()
    request_id = uuid.uuid4()
    stale_request_id = uuid.uuid4()
    url = f"https://health-observation-{registry_id.hex}.example.test/prefix"
    auth_token = f"health-observation-secret-{uuid.uuid4().hex}"
    rotated_token = f"health-observation-rotated-{uuid.uuid4().hex}"
    challenge = "0123456789abcdef" * 4
    # The raw observation text keeps non-standard JSON number tokens; Native
    # transport must return the exact text so the legacy decoder keeps its phase.
    observation_json = (
        ' { "kind":"observed", "meta":{"sample":NaN}, "setup":Infinity }\n'
    )
    api_transaction = None
    api_started = False
    origin_transaction = None
    origin_started = False
    other_transaction = None
    other_started = False
    try:
        await _seed_backend(owner, registry_id, url, auth_token, {"max_concurrency": 5})

        # Endpoint capture belongs to the ordinary API caller transaction, which
        # must stay live only while the observation owner opens the snapshot.
        api_transaction = api.transaction()
        await api_transaction.start()
        api_started = True
        stale_endpoint = await _endpoint_capture(api, registry_id, stale_request_id)
        await api_transaction.rollback()
        api_started = False

        origin_transaction = origin.transaction()
        await origin_transaction.start()
        origin_started = True
        stale_subject = await _capture(
            origin,
            registry_id,
            stale_request_id,
            _observation_backend(
                registry_id,
                _endpoint_envelope(registry_id, stale_request_id, stale_endpoint),
            ),
            challenge=challenge,
        )
        # A rolled-back endpoint capture can never be opened from the still-live
        # observation origin transaction.
        await _assert_invalid(
            provider,
            f"SELECT {_OPEN}($1::text,$2::uuid,$3::uuid)",
            stale_subject,
            registry_id,
            stale_request_id,
            message="invalid health observation endpoint",
        )

        api_transaction = api.transaction()
        await api_transaction.start()
        api_started = True
        endpoint_subject = await _endpoint_capture(api, registry_id, request_id)
        backend_json = _observation_backend(
            registry_id,
            _endpoint_envelope(registry_id, request_id, endpoint_subject),
        )
        subject = await _capture(
            origin,
            registry_id,
            request_id,
            backend_json,
            challenge=challenge,
            timeout=-2.5,
        )
        assert subject.startswith("v1.")
        assert url not in subject
        assert auth_token not in subject
        assert endpoint_subject not in subject

        # Rotation after capture must not change the sealed endpoint snapshot.
        await _rotate_backend(owner, registry_id, rotated_token)

        opened = json.loads(await _open(provider, subject, registry_id, request_id))
        assert opened["gpu_health_challenge"] == challenge
        assert opened["timeout_seconds"] == -2.5
        assert json.loads(opened["backend_json"]) == {
            "id": str(registry_id),
            "url": url,
            "auth_method": "token",
            "auth_token": auth_token,
            "gpu_resource_id": None,
            "vram_budget_mb": None,
            "extra_params": {"max_concurrency": 5},
        }

        evidence = await _seal(
            provider, subject, registry_id, request_id, observation_json
        )
        assert evidence.startswith("v1.")
        assert url not in evidence
        assert auth_token not in evidence
        assert observation_json not in evidence
        assert await _verify(origin, subject, registry_id, request_id, evidence) == (
            observation_json
        )

        await _assert_invalid(
            provider,
            f"SELECT {_OPEN}($1::text,$2::uuid,$3::uuid)",
            subject,
            registry_id,
            uuid.uuid4(),
            message="invalid health observation subject",
        )
        await _assert_invalid(
            provider,
            f"SELECT {_OPEN}($1::text,$2::uuid,$3::uuid)",
            _tamper(subject),
            registry_id,
            request_id,
            message="invalid health observation subject",
        )
        await _assert_invalid(
            provider,
            f"SELECT {_SEAL}($1::text,$2::uuid,$3::uuid,$4::text)",
            subject,
            registry_id,
            uuid.uuid4(),
            observation_json,
            message="invalid health observation subject",
        )
        await _assert_invalid_in_transaction(
            origin,
            f"SELECT {_VERIFY}($1::text,$2::uuid,$3::uuid,$4::text)",
            subject,
            registry_id,
            request_id,
            _tamper(evidence),
            message="invalid health observation evidence",
        )

        # A second still-open transaction cannot consume this response even while
        # the real origin transaction remains active.
        other_transaction = other_origin.transaction()
        await other_transaction.start()
        other_started = True
        await _assert_invalid_in_transaction(
            other_origin,
            f"SELECT {_VERIFY}($1::text,$2::uuid,$3::uuid,$4::text)",
            subject,
            registry_id,
            request_id,
            evidence,
            message="invalid health observation evidence",
        )
        await other_transaction.rollback()
        other_started = False

        await origin_transaction.commit()
        origin_started = False

        await _assert_invalid(
            provider,
            f"SELECT {_OPEN}($1::text,$2::uuid,$3::uuid)",
            subject,
            registry_id,
            request_id,
            message="invalid health observation subject",
        )
        await _assert_invalid(
            origin,
            f"SELECT {_VERIFY}($1::text,$2::uuid,$3::uuid,$4::text)",
            subject,
            registry_id,
            request_id,
            evidence,
            message="invalid health observation subject",
        )
    finally:
        if other_started:
            await other_transaction.rollback()
        if origin_started:
            await origin_transaction.rollback()
        if api_started:
            await api_transaction.rollback()
        await _delete_backend(owner, registry_id)
        await provider.close()
        await other_origin.close()
        await origin.close()
        await api.close()
        await owner.close()


async def test_health_observation_rejects_invalid_snapshot_and_nonfinite_timeout(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    origin = await _connect(test_engine, role=_ORIGIN_ROLE)
    provider = await _connect(test_engine, role=_PROVIDER_ROLE)
    registry_id = uuid.uuid4()
    request_id = uuid.uuid4()
    endpoint_subject = "v1.AAAA"
    envelope = _endpoint_envelope(registry_id, request_id, endpoint_subject)
    valid_backend = _observation_backend(registry_id, envelope)
    capture = (
        f"SELECT {_CAPTURE}($1::uuid,$2::uuid,$3::text,$4::text,$5::double precision)"
    )
    try:
        await _assert_invalid(
            origin,
            capture,
            None,
            request_id,
            valid_backend,
            None,
            2.0,
            message="invalid health observation subject",
        )
        await _assert_invalid(
            origin,
            capture,
            registry_id,
            request_id,
            json.dumps(
                {
                    "id": str(registry_id),
                    "url": "https://health-observation.example.test",
                    "auth_method": "token",
                    "auth_token": "secret",
                    "gpu_resource_id": None,
                    "vram_budget_mb": None,
                    "extra_params": {"max_concurrency": 4},
                },
                separators=(",", ":"),
            ),
            None,
            2.0,
            message="invalid health observation subject",
        )
        await _assert_invalid(
            origin,
            capture,
            registry_id,
            request_id,
            _observation_backend(
                registry_id,
                _endpoint_envelope(registry_id, uuid.uuid4(), endpoint_subject),
            ),
            None,
            2.0,
            message="invalid health observation subject",
        )
        await _assert_invalid(
            origin,
            capture,
            registry_id,
            request_id,
            _observation_backend(registry_id, {**envelope, "purpose": "setup"}),
            None,
            2.0,
            message="invalid health observation subject",
        )
        await _assert_invalid(
            origin,
            capture,
            registry_id,
            request_id,
            _observation_backend(
                registry_id,
                {
                    **envelope,
                    "authority": {
                        "kind": "scope",
                        "scope_id": str(uuid.uuid4()),
                        "control_id": None,
                        "attempt_id": str(request_id),
                    },
                },
            ),
            None,
            2.0,
            message="invalid health observation subject",
        )
        await _assert_invalid(
            origin,
            capture,
            registry_id,
            request_id,
            _observation_backend(registry_id, {**envelope, "subject": "opaque"}),
            None,
            2.0,
            message="invalid health observation subject",
        )
        await _assert_invalid(
            origin,
            capture,
            registry_id,
            request_id,
            _observation_backend(registry_id, {**envelope, "unexpected": "field"}),
            None,
            2.0,
            message="invalid health observation subject",
        )
        await _assert_invalid(
            origin,
            capture,
            registry_id,
            request_id,
            json.dumps(
                {"id": str(registry_id), "endpoint_subject": endpoint_subject},
                separators=(",", ":"),
            ),
            None,
            2.0,
            message="invalid health observation subject",
        )
        await _assert_invalid(
            provider,
            f"SELECT {_OPEN}($1::text,$2::uuid,$3::uuid)",
            None,
            registry_id,
            request_id,
            message="invalid health observation subject",
        )
        await _assert_invalid(
            origin,
            capture,
            registry_id,
            request_id,
            valid_backend,
            None,
            float("nan"),
            message="invalid health observation subject",
        )
        await _assert_invalid(
            origin,
            capture,
            registry_id,
            request_id,
            valid_backend,
            None,
            float("inf"),
            message="invalid health observation subject",
        )
        await _assert_invalid(
            origin,
            capture,
            registry_id,
            request_id,
            valid_backend,
            "A" * 64,
            2.0,
            message="invalid health observation subject",
        )
    finally:
        await origin.close()
        await provider.close()


async def test_health_observation_roles_receive_only_their_fixed_functions(
    test_engine,
):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    origin = await _connect(test_engine, role=_ORIGIN_ROLE)
    provider = await _connect(test_engine, role=_PROVIDER_ROLE)
    try:
        policy = await origin.fetchval(f"SELECT {_POLICY}")
        assert json.loads(policy) == _POLICY_VALUE
        assert json.loads(await provider.fetchval(f"SELECT {_POLICY}")) == _POLICY_VALUE

        signatures = await owner.fetch(
            """SELECT namespace.nspname || '.' || function.proname || '(' ||
                          pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature,
                      pg_catalog.pg_get_userbyid(function.proowner) AS owner,
                      function.prosecdef AS security_definer,
                      function.proconfig AS settings,
                      EXISTS (
                          SELECT 1
                          FROM pg_catalog.aclexplode(COALESCE(
                              function.proacl,
                              pg_catalog.acldefault('f', function.proowner)
                          )) AS privilege
                          WHERE privilege.grantee = 0
                            AND privilege.privilege_type = 'EXECUTE'
                      ) AS public_execute
               FROM pg_catalog.pg_proc AS function
               JOIN pg_catalog.pg_namespace AS namespace
                 ON namespace.oid = function.pronamespace
               WHERE namespace.nspname = $1
                 AND function.prokind = 'f'
               ORDER BY signature""",
            _SCHEMA,
        )
        assert [row["signature"] for row in signatures] == sorted(_CATALOG_FUNCTIONS)
        for function in signatures:
            assert function["owner"] == "inference_code"
            assert function["security_definer"] is True
            assert function["public_execute"] is False
            settings = function["settings"]
            assert settings is not None
            assert "search_path=pg_catalog, pg_temp" in settings
            assert "TimeZone=UTC" in settings
            assert "DateStyle=ISO, YMD" in settings
            assert "extra_float_digits=3" in settings

        origin_allowed = {
            _CATALOG_FUNCTIONS[0],
            _CATALOG_FUNCTIONS[1],
            _CATALOG_FUNCTIONS[4],
        }
        provider_allowed = {
            _CATALOG_FUNCTIONS[0],
            _CATALOG_FUNCTIONS[2],
            _CATALOG_FUNCTIONS[3],
        }
        for role, allowed in (
            (_ORIGIN_ROLE, origin_allowed),
            (_PROVIDER_ROLE, provider_allowed),
        ):
            actual = await owner.fetch(
                """SELECT namespace.nspname || '.' || function.proname || '(' ||
                              pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
                   FROM pg_catalog.pg_proc AS function
                   JOIN pg_catalog.pg_namespace AS namespace
                     ON namespace.oid = function.pronamespace
                   WHERE namespace.nspname = $1
                     AND pg_catalog.has_function_privilege($2, function.oid, 'EXECUTE')""",
                _SCHEMA,
                role,
            )
            assert {row["signature"] for row in actual} == allowed
            assert await owner.fetchval(
                "SELECT pg_catalog.has_schema_privilege($1, $2, 'USAGE')",
                role,
                _SCHEMA,
            )
            assert not await owner.fetchval(
                "SELECT pg_catalog.has_table_privilege($1, $2, 'SELECT')",
                role,
                _KEY_TABLE,
            )
            assert not await owner.fetchval(
                "SELECT pg_catalog.has_table_privilege("
                "$1, 'public.ml_backend_registry', 'SELECT')",
                role,
            )
            assert not await owner.fetchval(
                "SELECT pg_catalog.pg_has_role($1, 'inference_code', 'MEMBER')",
                role,
            )

        key = await owner.fetchrow(
            f"""SELECT secret.singleton,
                       pg_catalog.octet_length(secret.secret) AS key_bytes,
                       pg_catalog.pg_get_userbyid(class.relowner) AS owner
                FROM {_KEY_TABLE} AS secret
                JOIN pg_catalog.pg_class AS class
                  ON class.oid = pg_catalog.to_regclass($1)""",
            _KEY_TABLE,
        )
        assert key["singleton"] is True
        assert key["key_bytes"] == 32
        assert key["owner"] == "inference_code"
    finally:
        await provider.close()
        await origin.close()
        await owner.close()
