"""Nonempty routing restore quarantine for copied private authority state."""

from __future__ import annotations

import json
import os
import stat
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_inference.routing_postgres import RoutingPostgresData
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.routing import (
    make_routing_command,
    make_routing_runtime_request,
    make_routing_status_request,
    routing_command_sha256,
    RoutingUnavailable,
)
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_identity_recovery import _postgres_tool
from tests.test_inference_backend_dependency_reads_restore import (
    _assert_clone_identity,
    _create_owned_clone,
    _database_acl,
    _database_head,
    _drop_owned_clone,
)
from tests.test_inference_routing_native import (
    _cleanup,
    _connect,
    _identity_hash,
    _step,
)
from tests.test_module_data_roles import _role_url


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_SCHEMA = "aap_inference_routing_v1"
_ROLE = "inference_routing_client"


async def _routing_snapshot(engine, world: dict, receipt_ids: list[uuid.UUID]) -> dict:
    owner = await _connect(engine)
    registry_id = world["registry_id"]
    try:
        secret = await owner.fetchrow(
            f"""SELECT singleton,
                       pg_catalog.encode(public.digest(secret, 'sha256'), 'hex') AS fingerprint,
                       realm_database_oid::text AS realm_database_oid,
                       realm_database_name
                FROM {_SCHEMA}.authority_secret WHERE singleton"""
        )
        functions = await owner.fetch(
            """SELECT p.proname,
                       pg_catalog.pg_get_function_identity_arguments(p.oid) AS arguments,
                       pg_catalog.pg_get_functiondef(p.oid) AS definition,
                       pg_catalog.pg_get_userbyid(p.proowner) AS owner,
                       p.prosecdef AS security_definer,
                       p.proacl::text AS acl,
                       p.proconfig::text AS settings
                FROM pg_catalog.pg_proc AS p
                JOIN pg_catalog.pg_namespace AS n ON n.oid=p.pronamespace
                WHERE n.nspname=$1
                ORDER BY p.proname, arguments""",
            _SCHEMA,
        )
        public_rows = await owner.fetchrow(
            """SELECT
                   (SELECT pg_catalog.encode(public.digest(
                       pg_catalog.convert_to(pg_catalog.to_jsonb(r)::text, 'UTF8'), 'sha256'), 'hex')
                    FROM public.ml_backend_registry AS r WHERE r.id=$1) AS registry,
                   (SELECT pg_catalog.encode(public.digest(
                       pg_catalog.convert_to(pg_catalog.to_jsonb(p)::text, 'UTF8'), 'sha256'), 'hex')
                    FROM public.ml_backend_service_pools AS p WHERE p.id=$2) AS pool,
                   (SELECT pg_catalog.encode(public.digest(
                       pg_catalog.convert_to(pg_catalog.to_jsonb(m)::text, 'UTF8'), 'sha256'), 'hex')
                    FROM public.ml_backend_pool_members AS m WHERE m.id=$3) AS member""",
            registry_id,
            world["pool_id"],
            world["member_id"],
        )
        private_rows: dict[str, list[tuple[str, str]]] = {}
        queries = {
            "registry_state": (
                f"SELECT registry_id::text AS id, pg_catalog.encode(public.digest("
                f"pg_catalog.convert_to(pg_catalog.to_jsonb(row_data)::text, 'UTF8'), 'sha256'), 'hex') AS fingerprint "
                f"FROM (SELECT * FROM {_SCHEMA}.registry_state WHERE registry_id=$1) AS row_data"
            ),
            "scopes": (
                f"SELECT id::text AS id, pg_catalog.encode(public.digest("
                f"pg_catalog.convert_to(pg_catalog.to_jsonb(row_data)::text, 'UTF8'), 'sha256'), 'hex') AS fingerprint "
                f"FROM (SELECT * FROM {_SCHEMA}.scopes WHERE registry_id=$1) AS row_data"
            ),
            "scope_retirements": (
                f"SELECT retirement_id::text AS id, pg_catalog.encode(public.digest("
                f"pg_catalog.convert_to(pg_catalog.to_jsonb(row_data)::text, 'UTF8'), 'sha256'), 'hex') AS fingerprint "
                f"FROM (SELECT * FROM {_SCHEMA}.scope_retirements WHERE registry_id=$1) AS row_data"
            ),
            "attempts": (
                f"SELECT row_data.id::text AS id, pg_catalog.encode(public.digest("
                f"pg_catalog.convert_to(pg_catalog.to_jsonb(row_data)::text, 'UTF8'), 'sha256'), 'hex') AS fingerprint "
                f"FROM {_SCHEMA}.attempts AS row_data JOIN {_SCHEMA}.scopes AS scope "
                f"ON scope.id=row_data.scope_id WHERE scope.registry_id=$1"
            ),
            "controls": (
                f"SELECT id::text AS id, pg_catalog.encode(public.digest("
                f"pg_catalog.convert_to(pg_catalog.to_jsonb(row_data)::text, 'UTF8'), 'sha256'), 'hex') AS fingerprint "
                f"FROM (SELECT * FROM {_SCHEMA}.controls WHERE registry_id=$1) AS row_data"
            ),
            "control_attempts": (
                f"SELECT row_data.id::text AS id, pg_catalog.encode(public.digest("
                f"pg_catalog.convert_to(pg_catalog.to_jsonb(row_data)::text, 'UTF8'), 'sha256'), 'hex') AS fingerprint "
                f"FROM {_SCHEMA}.control_attempts AS row_data JOIN {_SCHEMA}.controls AS control "
                f"ON control.id=row_data.control_id WHERE control.registry_id=$1"
            ),
            "weights": (
                f"SELECT registry_id::text AS id, pg_catalog.encode(public.digest("
                f"pg_catalog.convert_to(pg_catalog.to_jsonb(row_data)::text, 'UTF8'), 'sha256'), 'hex') AS fingerprint "
                f"FROM (SELECT * FROM {_SCHEMA}.weights WHERE registry_id=$1) AS row_data"
            ),
        }
        for name, query in queries.items():
            rows = await owner.fetch(query, registry_id)
            private_rows[name] = sorted((row["id"], row["fingerprint"]) for row in rows)
        receipts = await owner.fetch(
            f"""SELECT id::text AS id, command_sha256, operation,
                       pg_catalog.encode(public.digest(
                           pg_catalog.convert_to(result::text, 'UTF8'), 'sha256'), 'hex') AS result_fingerprint,
                       creating_xid::text AS creating_xid, realm
                FROM {_SCHEMA}.command_receipts
                WHERE id=ANY($1::uuid[])
                ORDER BY id""",
            receipt_ids,
        )
        return {
            "secret": dict(secret),
            "functions": [dict(row) for row in functions],
            "public_rows": dict(public_rows),
            "private_rows": private_rows,
            "receipts": [dict(row) for row in receipts],
        }
    finally:
        await owner.close()


async def _seed_nonempty_routing_state(
    test_engine, owner, world: dict, *, include_retirement: bool = False
) -> dict:
    registry_id = world["registry_id"]
    async with owner.transaction():
        await owner.execute(
            """INSERT INTO public.ml_backend_registry (
                   id, name, url, state, is_interactive, auth_method, auth_token,
                   extra_params, gpu_resource_id, vram_budget_mb, eviction_priority,
                   health_meta, source, error_message, last_checked_at
               ) VALUES (
                   $1,$2,$3,'connected',false,'token',$4,$5::jsonb,
                   NULL,NULL,0,$6::jsonb,'manual',NULL,now()
               )""",
            registry_id,
            f"Routing restore {registry_id}",
            f"http://127.0.0.1/{registry_id}",
            f"routing-restore-secret-{uuid.uuid4().hex}",
            json.dumps({"max_concurrency": 2}),
            json.dumps({"capabilities": {"model_version": "restore-test"}}),
        )
        await owner.execute(
            """INSERT INTO public.ml_backend_service_pools (
                   id, name, enabled, routing_policy, legacy_instance_id,
                   routing_generation, capability_fingerprint, capability_snapshot
               ) VALUES ($1,$2,true,'smooth_weighted_round_robin',$3,1,NULL,NULL)""",
            world["pool_id"],
            f"Routing restore pool {world['pool_id']}",
            registry_id,
        )
        await owner.execute(
            """INSERT INTO public.ml_backend_pool_members (
                   id, pool_id, registry_id, traffic_state, weight
               ) VALUES ($1,$2,$3,'active',1)""",
            world["member_id"],
            world["pool_id"],
            registry_id,
        )
    epoch = uuid.uuid4()
    incarnation = uuid.uuid4()
    completed_control_id = uuid.uuid4()
    unresolved_control_id = uuid.uuid4()
    # Seed a source epoch only to create a source admission header in the copied
    # receipt. This test sends no source backend request; the active scope/attempt
    # and unresolved control below remain deliberately non-idle.
    await owner.execute(
        f"""UPDATE {_SCHEMA}.registry_state
            SET continuity='ready', authority_epoch=$2, backend_incarnation=$3,
                epoch_key=public.gen_random_bytes(32)
            WHERE registry_id=$1""",
        registry_id,
        epoch,
        incarnation,
    )
    realm = await owner.fetchval(
        "SELECT oid::text || ':' || datname FROM pg_catalog.pg_database "
        "WHERE datname=current_database()"
    )
    await owner.execute(
        f"""INSERT INTO {_SCHEMA}.controls (
                id, registry_id, kind, phase, new_epoch, retired_epoch, challenge,
                backend_incarnation, config_revision, realm, expected_topology
            ) VALUES ($1,$2,'epoch','completed',$3,NULL,$4,$5,1,$6,NULL)""",
        completed_control_id,
        registry_id,
        epoch,
        uuid.uuid4().hex * 2,
        incarnation,
        realm,
    )

    client = await _connect(test_engine, role=_ROLE)
    transaction = client.transaction()
    receipt_ids: list[uuid.UUID] = world["receipt_ids"]
    try:
        await transaction.start()
        acquire_id = uuid.uuid4()
        acquire_body = {
            "pool_id": None,
            "registry_id": str(registry_id),
            "project_id": None,
            "mode": "direct",
            "required_instance_id": str(registry_id),
            "owner": "restore-source-fixture",
            "operation": "predict",
        }
        acquire_command = make_routing_command(
            "acquire", acquire_body, request_id=acquire_id
        )
        world["acquire_command"] = acquire_command
        snapshot = await _step(
            client,
            "admission_snapshot",
            {
                "pool_id": None,
                "registry_id": str(registry_id),
                "project_id": None,
                "mode": "direct",
                "required_instance_id": str(registry_id),
                "health_max_age_seconds": 90,
            },
        )
        assert snapshot["status"] == "ok"
        scope_id = uuid.uuid4()
        reserved = await _step(
            client,
            "reserve_scope",
            {
                "snapshot": snapshot["snapshot"],
                "candidate_ids": [str(registry_id)],
                "scope_id": str(scope_id),
                "command_sha256": routing_command_sha256(acquire_command),
                "owner": acquire_body["owner"],
                "operation": acquire_body["operation"],
                "ttl_ms": 120_000,
            },
        )
        assert reserved["status"] == "ok"
        scope = reserved["scope"]
        assert type(scope.get("capability")) is str and len(scope["capability"]) == 64
        world.update(
            {
                "scope_id": scope_id,
                "scope_capability": scope["capability"],
            }
        )
        recorded_acquire = await _step(
            client,
            "record_command",
            {
                "request_id": str(acquire_id),
                "command_sha256": routing_command_sha256(acquire_command),
                "operation": "acquire",
                "result": scope,
            },
        )
        assert recorded_acquire["status"] == "ok"
        receipt_ids.append(acquire_id)

        attempt_id = uuid.uuid4()
        identity_hash = await _identity_hash(owner, registry_id)
        transport_identity = await owner.fetchrow(
            """SELECT url, auth_method, auth_token, gpu_resource_id, extra_params
               FROM public.ml_backend_registry WHERE id=$1""",
            registry_id,
        )
        extra_params = transport_identity["extra_params"]
        if type(extra_params) is str:
            extra_params = json.loads(extra_params)
        captured = await _step(
            client,
            "capture_transport_subject",
            {
                "parent_kind": "scope",
                "parent_id": str(scope_id),
                "capability": scope["capability"],
                "attempt_id": str(attempt_id),
                "transport_identity": {
                    **dict(transport_identity),
                    "extra_params": extra_params,
                },
            },
        )
        assert captured["status"] == "ok"
        begin_body = {
            "scope_id": str(scope_id),
            "capability": scope["capability"],
            "attempt_id": str(attempt_id),
            "method": "POST",
            "path": "/predict",
            "transport_subject": captured["subject"],
        }
        begin_id = uuid.uuid4()
        begin_command = make_routing_command("begin", begin_body, request_id=begin_id)
        world.update(
            {
                "attempt_id": attempt_id,
                "identity_hash": identity_hash,
                "begin_command": begin_command,
            }
        )
        began = await _step(
            client,
            "begin_attempt",
            {
                "scope_id": str(scope_id),
                "capability": scope["capability"],
                "attempt_id": str(attempt_id),
                "method": "POST",
                "path": "/predict",
                "transport_identity": {"identity_sha256": identity_hash},
            },
        )
        assert began["status"] == "ok"
        source_header_present = await owner.fetchval(
            "SELECT pg_catalog.jsonb_typeof($1::jsonb -> 'X-AAP-Route-Admission')="
            "'string' AND pg_catalog.char_length($1::jsonb ->> 'X-AAP-Route-Admission') > 0",
            json.dumps(began["headers"]),
        )
        assert source_header_present is True
        recorded_begin = await _step(
            client,
            "record_command",
            {
                "request_id": str(begin_id),
                "command_sha256": routing_command_sha256(begin_command),
                "operation": "begin",
                "result": {
                    "attempt_id": began["attempt_id"],
                    "scope": began["scope"],
                    "headers": began["headers"],
                },
            },
        )
        assert recorded_begin["status"] == "ok"
        receipt_ids.append(begin_id)
        await transaction.commit()
    except BaseException:
        await transaction.rollback()
        raise
    finally:
        await client.close()

    challenge = uuid.uuid4().hex
    world.update(
        {
            "completed_control_id": completed_control_id,
            "unresolved_control_id": unresolved_control_id,
        }
    )
    if include_retirement:
        from tests.test_inference_scope_retirement_native import _retirement_step

        client = await _connect(test_engine, role=_ROLE)
        retirement_id = uuid.uuid4()
        try:
            async with client.transaction():
                prepared = await _retirement_step(
                    client,
                    "prepare",
                    {
                        "retirement_id": str(retirement_id),
                        "scope_id": str(world["scope_id"]),
                        "capability": world["scope_capability"],
                    },
                )
                assert prepared["status"] == "ok"
        finally:
            await client.close()
        world["retirement_id"] = retirement_id
        world["retirement_reply"] = {
            **{
                key: prepared["retirement"][key]
                for key in (
                    "retirement_id",
                    "scope_id",
                    "registry_id",
                    "authority_epoch",
                    "backend_incarnation",
                )
            },
            "schema": "aap-backend-scope-retirement-reply/v1",
            "challenge": prepared["context"]["challenge"],
            "phase": "retired",
            "active_requests": 0,
        }
        return world
    await owner.execute(
        f"""INSERT INTO {_SCHEMA}.controls (
                id, registry_id, kind, phase, new_epoch, retired_epoch, challenge,
                backend_incarnation, config_revision, realm, expected_topology
            ) VALUES ($1,$2,'unload','uncertain',NULL,NULL,$3,$4,1,$5,$6::jsonb)""",
        unresolved_control_id,
        registry_id,
        challenge * 2,
        incarnation,
        realm,
        json.dumps(
            {
                "registry_id": str(registry_id),
                "member_id": str(world["member_id"]),
                "pool_id": str(world["pool_id"]),
                "routing_generation": 1,
                "traffic_state": "active",
            }
        ),
    )
    control_attempt_id = uuid.uuid4()
    world["control_attempt_id"] = control_attempt_id
    await owner.execute(
        f"""INSERT INTO {_SCHEMA}.control_attempts (
                id, control_id, method, path, transport_identity_sha256,
                state, status_code, reason
            ) VALUES ($1,$2,'POST','/unload',$3,'active',NULL,NULL)""",
        control_attempt_id,
        unresolved_control_id,
        await _identity_hash(owner, registry_id),
    )
    return world


def _clone_role_dsn(test_engine, clone: str) -> str:
    role_url = make_url(_role_url("AAP_MODULE_DATA_INFERENCE_ROUTING_URL", _ROLE))
    assert role_url.database == test_engine.url.database
    return role_url.set(drivername="postgresql", database=clone).render_as_string(
        hide_password=False
    )


@pytest.mark.parametrize("include_retirement", (False, True))
async def test_restored_routing_authority_is_quarantined_in_owned_clone(
    test_engine, tmp_path, include_retirement
):
    await assert_owned_test_database(test_engine)
    source_head = await _database_head(test_engine)
    clone = f"aap_routing_restore_{uuid.uuid4().hex[:12]}_test"
    ownership = f"inference-routing-restore:{uuid.uuid4()}"
    created: set[str] = set()
    restored = None
    clone_runtime = None
    clone_connection = None
    clone_transaction = None
    dump = tmp_path / "inference-routing.dump"
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    owner = await _connect(test_engine)
    world = {
        "registry_id": uuid.uuid4(),
        "pool_id": uuid.uuid4(),
        "member_id": uuid.uuid4(),
        "receipt_ids": [],
    }
    clone_before = None
    clone_after = None
    callback_calls: list[bool] = []

    async def forbidden_backend_control(_private_context):
        callback_calls.append(True)
        raise AssertionError("restored routing must not call private backend control")

    try:
        await _seed_nonempty_routing_state(
            test_engine, owner, world, include_retirement=include_retirement
        )
        source_before = await _routing_snapshot(
            test_engine, world, world["receipt_ids"]
        )
        assert source_before["secret"]["fingerprint"]
        assert (
            source_before["secret"]["realm_database_name"] == test_engine.url.database
        )
        assert source_before["public_rows"]["registry"]
        assert source_before["public_rows"]["pool"]
        assert source_before["public_rows"]["member"]
        assert len(source_before["private_rows"]["scopes"]) == 1
        assert len(source_before["private_rows"]["attempts"]) == 1
        assert len(source_before["private_rows"]["controls"]) == (
            1 if include_retirement else 2
        )
        assert len(source_before["private_rows"]["control_attempts"]) == (
            0 if include_retirement else 1
        )
        assert len(source_before["private_rows"]["scope_retirements"]) == (
            1 if include_retirement else 0
        )
        assert len(source_before["receipts"]) == 2
        assert all(
            row["owner"] == "inference_code" for row in source_before["functions"]
        )
        assert all(
            row["security_definer"] is True for row in source_before["functions"]
        )
        acquire_has_capability = await owner.fetchval(
            f"""SELECT pg_catalog.jsonb_typeof(result -> 'capability')='string'
                       AND pg_catalog.char_length(result ->> 'capability')=64
                FROM {_SCHEMA}.command_receipts WHERE id=$1""",
            world["acquire_command"].request_id,
        )
        begin_has_admission = await owner.fetchval(
            f"""SELECT pg_catalog.jsonb_typeof(
                           result -> 'headers' -> 'X-AAP-Route-Admission')='string'
                       AND pg_catalog.char_length(
                           result -> 'headers' ->> 'X-AAP-Route-Admission') > 0
                FROM {_SCHEMA}.command_receipts WHERE id=$1""",
            world["begin_command"].request_id,
        )
        assert acquire_has_capability is True
        assert begin_has_admission is True

        dump.touch(mode=0o600, exist_ok=False)
        dump.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
            )
        )
        assert dump.read_bytes().startswith(b"PGDMP")
        assert stat.S_IMODE(dump.stat().st_mode) == 0o600
        await _create_owned_clone(
            admin,
            test_engine,
            clone,
            ownership,
            await _database_acl(test_engine),
            created,
        )
        quoted_clone = test_engine.dialect.identifier_preparer.quote_identifier(clone)
        async with admin.connect() as connection:
            await connection.execute(
                text(f"REVOKE TEMPORARY ON DATABASE {quoted_clone} FROM PUBLIC")
            )
            has_public_temp = await connection.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_database AS database "
                    "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE("
                    "database.datacl, pg_catalog.acldefault('d',database.datdba)"
                    ")) AS entry WHERE database.datname=:name "
                    "AND entry.grantee=0 AND entry.privilege_type='TEMPORARY')"
                ),
                {"name": clone},
            )
        assert has_public_temp is False
        _postgres_tool(
            test_engine,
            ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", clone],
            body=dump.read_bytes(),
        )
        restored = create_async_engine(
            test_engine.url.set(database=clone), hide_parameters=True
        )
        await _assert_clone_identity(restored, clone, ownership)
        assert await _database_head(restored) == source_head
        clone_before = await _routing_snapshot(restored, world, world["receipt_ids"])
        assert clone_before == source_before
        clone_dsn = _clone_role_dsn(test_engine, clone)
        clone_connection = await asyncpg.connect(
            clone_dsn, timeout=5, command_timeout=15
        )
        current_user, current_database = await clone_connection.fetchrow(
            "SELECT current_user, current_database()"
        )
        assert current_user == _ROLE
        assert current_database == clone
        await clone_connection.close()
        clone_connection = None

        clone_runtime = RoutingRuntime(
            clone_dsn,
            backend_control=forbidden_backend_control,
        )
        # The restored secret is deliberately bound to its source database.
        # Public owner readiness and data binding remain closed in quarantine.
        assert await clone_runtime.readiness() is False
        for source_command in (world["begin_command"], world["acquire_command"]):
            with pytest.raises(RoutingUnavailable):
                await clone_runtime.status(make_routing_status_request(source_command))
        with pytest.raises(RoutingUnavailable):
            await clone_runtime.runtime(
                make_routing_runtime_request([world["registry_id"]])
            )

        clone_connection = await asyncpg.connect(
            clone_dsn, timeout=5, command_timeout=15
        )
        clone_transaction = clone_connection.transaction()
        await clone_transaction.start()
        with pytest.raises(RoutingUnavailable):
            await RoutingPostgresData.bind(clone_connection)
        # Core Native reads allow inspection without exposing copied receipts.
        for source_command in (world["begin_command"], world["acquire_command"]):
            status = await _step(
                clone_connection,
                "command_status",
                {
                    "request_id": str(source_command.request_id),
                    "command_sha256": routing_command_sha256(source_command),
                },
            )
            assert status["receipt"] is None
        runtime_view = await _step(
            clone_connection,
            "runtime_snapshot",
            {"registry_ids": [str(world["registry_id"])]},
        )
        assert len(runtime_view["registries"]) == 1
        quarantined = runtime_view["registries"][0]
        assert quarantined["registry_id"] == str(world["registry_id"])
        assert quarantined["continuity"] == "unverified"
        assert quarantined["authority_epoch"] is None
        assert quarantined["backend_incarnation"] is None
        assert quarantined["open_scopes"] == 1
        assert quarantined["active_attempts"] == 1
        assert quarantined["uncertain_attempts"] == 0
        assert quarantined["control_id"] == (
            None if include_retirement else str(world["unresolved_control_id"])
        )
        if include_retirement:
            from tests.test_inference_scope_retirement_native import _retirement_step

            arguments = {
                "retirement_id": str(world["retirement_id"]),
                "scope_id": str(world["scope_id"]),
                "capability": world["scope_capability"],
            }
            for action in ("prepare", "status", "finish"):
                request = {
                    **arguments,
                    **(
                        {"reply": world["retirement_reply"]}
                        if action == "finish"
                        else {}
                    ),
                }
                denied = await _retirement_step(clone_connection, action, request)
                assert denied == {
                    "schema": "aap-inference-scope-retirement-data/v1",
                    "status": "rejected",
                    "reason": "scope_unavailable",
                }

        class CloneNative:
            async def step(self, action, arguments):
                return await _step(clone_connection, action, arguments)

        clone_data = CloneNative()
        begin_rejection = await clone_data.step(
            "begin_attempt",
            {
                "scope_id": str(world["scope_id"]),
                "capability": world["scope_capability"],
                "attempt_id": str(uuid.uuid4()),
                "method": "POST",
                "path": "/predict",
                "transport_identity": {"identity_sha256": world["identity_hash"]},
            },
        )
        settle_rejection = await clone_data.step(
            "settle_attempt",
            {
                "scope_id": str(world["scope_id"]),
                "capability": world["scope_capability"],
                "attempt_id": str(world["attempt_id"]),
                "kind": "response",
                "status_code": 200,
                "reason": None,
            },
        )
        close_rejection = await clone_data.step(
            "close_scope",
            {
                "scope_id": str(world["scope_id"]),
                "capability": world["scope_capability"],
                "outcome": "cancel",
            },
        )
        for reply in (begin_rejection, settle_rejection, close_rejection):
            assert reply == {
                "schema": "aap-inference-routing-data/v1",
                "status": "rejected",
                "reason": "scope_unavailable",
            }
        await clone_transaction.commit()
        clone_transaction = None
        await clone_connection.close()
        clone_connection = None

        # A copied uncertain management hold is quarantined too. The owner must
        # refuse before invoking any backend callback; this test does not attempt
        # or claim a safe rebind/recovery procedure.
        reconcile = make_routing_command(
            "reconcile",
            {
                "registry_id": str(world["registry_id"]),
                "control_id": str(world["unresolved_control_id"]),
            },
        )
        try:
            reconcile_result = await clone_runtime.execute(reconcile)
        except RoutingUnavailable:
            reconcile_result = None
        assert callback_calls == []
        if reconcile_result is not None:
            assert (
                reconcile_result.state != "committed"
                or getattr(reconcile_result.result, "kind", None) == "rejected"
            )

        clone_after = await _routing_snapshot(restored, world, world["receipt_ids"])
        assert clone_after == clone_before
        source_after = await _routing_snapshot(test_engine, world, world["receipt_ids"])
        assert source_after == source_before
    finally:
        if clone_transaction is not None:
            await clone_transaction.rollback()
        if clone_connection is not None:
            await clone_connection.close()
        if clone_runtime is not None:
            await clone_runtime.close()
        if restored is not None:
            await restored.dispose()
        try:
            if clone in created:
                await _drop_owned_clone(admin, test_engine, clone, ownership)
        finally:
            await admin.dispose()
            try:
                cleanup_owner = await _connect(test_engine)
                try:
                    await cleanup_owner.execute(
                        f"DELETE FROM {_SCHEMA}.scope_retirements WHERE registry_id=$1",
                        world["registry_id"],
                    )
                    await cleanup_owner.execute(
                        f"DELETE FROM {_SCHEMA}.command_receipts WHERE id=ANY($1::uuid[])",
                        world["receipt_ids"],
                    )
                    await _cleanup(cleanup_owner, world)
                finally:
                    await cleanup_owner.close()
            finally:
                await owner.close()
                dump.unlink(missing_ok=True)
