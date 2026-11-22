"""Forward-qualify upload retry history, key exclusion and installed permissions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.services.managed_original_creation import bootstrap

pytestmark = pytest.mark.asyncio
API_ROOT = Path(__file__).resolve().parents[1]
_RESERVED_KEY = "dataset-sources/retry/dataset/item/publication/file.png"
_STAGING_KEY = "dataset-uploads/item/source"
_FROZEN_ADMISSION = "aap_assets_v1.begin_operator_auxiliary(jsonb)"
_CURRENT_ADMISSION = "yoao_assets_v1.begin_operator_auxiliary(jsonb)"


def _config(url):
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    config.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    return config


@pytest.fixture
async def retry_migration_database(test_engine):
    assert test_engine.url.host in {"localhost", "127.0.0.1", "::1"}
    assert test_engine.url.database.endswith("_test")
    async with test_engine.connect() as source:
        assert await source.scalar(
            text(
                "SELECT rolsuper OR rolcreatedb FROM pg_roles WHERE rolname=current_user"
            )
        ), "forward migration qualification requires a disposable database owner"
        acl = (
            await source.execute(
                text(
                    "SELECT COALESCE(role.rolname,'PUBLIC'), entry.privilege_type, "
                    "entry.is_grantable FROM pg_database AS database "
                    "CROSS JOIN LATERAL aclexplode(COALESCE(database.datacl, "
                    "acldefault('d',database.datdba))) AS entry "
                    "LEFT JOIN pg_roles AS role ON role.oid=entry.grantee "
                    "WHERE database.datname=current_database()"
                )
            )
        ).all()
    name = f"aap_upload_retry_{uuid4().hex[:12]}_test"
    ownership = f"upload-retry-migration:{uuid4()}"
    target = test_engine.url.set(database=name)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    engine = create_async_engine(target)
    created = False
    tagged = False
    try:
        async with admin.connect() as connection:
            await connection.execute(
                text(f'CREATE DATABASE "{name}" TEMPLATE template0')
            )
            created = True
            await connection.execute(
                text(f"COMMENT ON DATABASE \"{name}\" IS '{ownership}'")
            )
            tagged = True
            quote = connection.dialect.identifier_preparer.quote_identifier
            await connection.execute(
                text(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC')
            )
            for grantee, privilege, grantable in acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                suffix = " WITH GRANT OPTION" if grantable else ""
                await connection.execute(
                    text(f'GRANT {privilege} ON DATABASE "{name}" TO {role}{suffix}')
                )
        config = _config(target)
        await asyncio.to_thread(command.upgrade, config, "bo0457")
        yield config, engine
    finally:
        await engine.dispose()
        try:
            if created:
                async with admin.connect() as connection:
                    marker = await connection.scalar(
                        text(
                            "SELECT shobj_description(oid,'pg_database') "
                            "FROM pg_database WHERE datname=:name"
                        ),
                        {"name": name},
                    )
                    assert marker == ownership or (not tagged and marker is None)
                    await connection.execute(text(f'DROP DATABASE "{name}"'))
        finally:
            await admin.dispose()


async def _installed_contract(connection, signature):
    function = (
        await connection.execute(
            text(
                "SELECT pg_get_userbyid(proowner) AS owner,prosecdef,proconfig FROM pg_proc "
                "WHERE oid=CAST(:signature AS regprocedure)"
            ),
            {"signature": signature},
        )
    ).one()
    function_acl = (
        await connection.execute(
            text(
                "SELECT COALESCE(role.rolname,'PUBLIC') AS grantee,"
                "entry.privilege_type,entry.is_grantable FROM pg_proc AS proc "
                "CROSS JOIN LATERAL aclexplode(COALESCE(proc.proacl, "
                "acldefault('f',proc.proowner))) AS entry "
                "LEFT JOIN pg_roles AS role ON role.oid=entry.grantee "
                "WHERE proc.oid=CAST(:signature AS regprocedure) "
                "ORDER BY grantee,entry.privilege_type,entry.is_grantable"
            ),
            {"signature": signature},
        )
    ).all()
    table_acl = (
        await connection.execute(
            text(
                "SELECT COALESCE(role.rolname,'PUBLIC') AS grantee,"
                "entry.privilege_type,entry.is_grantable FROM pg_class AS relation "
                "CROSS JOIN LATERAL aclexplode(COALESCE(relation.relacl, "
                "acldefault('r',relation.relowner))) AS entry "
                "LEFT JOIN pg_roles AS role ON role.oid=entry.grantee "
                "WHERE relation.oid='public.managed_original_creations'::regclass "
                "ORDER BY grantee,entry.privilege_type,entry.is_grantable"
            )
        )
    ).all()
    settings = dict(setting.split("=", 1) for setting in function.proconfig or [])
    assert function.prosecdef
    assert settings["search_path"] == "pg_catalog, pg_temp"
    assert all(row.grantee != "PUBLIC" for row in function_acl)
    assert all(row.grantee != "PUBLIC" for row in table_acl)
    return function.owner, function.prosecdef, settings, function_acl, table_acl


async def _rows(connection):
    return (
        await connection.scalars(
            text(
                "SELECT to_jsonb(creation) FROM public.managed_original_creations AS creation "
                "ORDER BY id"
            )
        )
    ).all()


def _scope(label):
    item, publication = uuid4(), uuid4()
    return {
        "dataset": uuid4(),
        "location": uuid4(),
        "actor": uuid4(),
        "object_key": f"{_RESERVED_KEY}/{label}",
        "staging_key": f"{_STAGING_KEY}/{label}",
        "authority": {
            "kind": "dataset_upload",
            "item_id": str(item),
            "publication_id": str(publication),
        },
        "item": item,
        "publication": publication,
    }


async def _insert_creation(connection, scope, status, **overrides):
    creation_id = uuid4()
    await connection.execute(
        text(
            "INSERT INTO public.managed_original_creations ("
            "id,owner_kind,dataset_id,location_id,storage_identity,bucket,object_key,"
            "logical_path,origin,max_bytes,timeout_seconds,content_type,requested_by,"
            "authority,configuration,input_fingerprint,maintenance_revision,"
            "staging_bucket,staging_key,status,completed_at,error_code) VALUES ("
            ":id,'dataset',:dataset,:location,:identity,:bucket,:object_key,"
            ":logical_path,'direct',1048576,60,'image/png',:actor,CAST(:authority AS jsonb),"
            "'{}'::jsonb,:fingerprint,1,:staging_bucket,:staging_key,:status,"
            "CASE WHEN :terminal THEN now() ELSE NULL END,:error)"
        ),
        {
            **{
                key: scope[key]
                for key in ("dataset", "location", "actor", "object_key", "staging_key")
            },
            "id": creation_id,
            "identity": scope.get("identity", "a" * 64),
            "bucket": scope.get("bucket", "retry-acceptance"),
            "staging_bucket": scope.get("staging_bucket", "retry-staging"),
            "fingerprint": "b" * 64,
            "logical_path": scope["object_key"],
            "authority": json.dumps(scope["authority"]),
            "status": status,
            "terminal": status in {"failed", "conflict", "unknown"},
            "error": f"test_{status}" if status != "allocated" else None,
            **overrides,
        },
    )
    return creation_id


async def _admit(connection, scope, **changes):
    request = {
        "dataset_id": str(scope["dataset"]),
        "kind": "role_media",
        "item_id": str(scope["item"]),
        "publication_id": str(scope["publication"]),
        "file_path": scope["object_key"],
        "authority": scope["authority"],
        "origin": "direct",
        "requested_by": str(scope["actor"]),
        "max_bytes": 1048576,
        "content_type": "image/png",
        "staging": {"key": scope["staging_key"]},
        **changes,
    }
    reply = await connection.scalar(
        text("SELECT yoao_assets_v1.begin_operator_auxiliary(CAST(:request AS jsonb))"),
        {"request": json.dumps(request)},
    )
    return json.loads(reply) if isinstance(reply, str) else reply


async def _allocate(connection, scope):
    reply = await _admit(connection, scope)
    assert reply["ok"] is True, reply
    facts = reply["data"]
    assert facts["publication_id"] == str(scope["publication"])
    assert facts["object_key"] == scope["object_key"]
    assert facts["staging"]["key"] == scope["staging_key"]
    scope.update(
        location=UUID(facts["location_id"]),
        identity=facts["storage_identity"],
        bucket=facts["bucket"],
        staging_bucket=facts["staging"]["bucket"],
    )
    return UUID(facts["creation_id"])


async def _failure_reply(connection, creation_id, outcome, evidence):
    reply = await connection.scalar(
        text(
            "SELECT yoao_assets_v1.fail_operator_original("
            "CAST(:id AS uuid),:outcome,:error,CAST(:evidence AS jsonb))"
        ),
        {
            "id": creation_id,
            "outcome": outcome,
            "error": f"test_{outcome}",
            "evidence": json.dumps(evidence),
        },
    )
    return json.loads(reply) if isinstance(reply, str) else reply


async def _attempt_state(connection, creation_id):
    creation = await connection.scalar(
        text(
            "SELECT to_jsonb(creation) FROM public.managed_original_creations AS creation "
            "WHERE id=:id"
        ),
        {"id": creation_id},
    )
    operation = await connection.scalar(
        text(
            "SELECT to_jsonb(operation) FROM public.storage_operations AS operation "
            "WHERE id=CAST(:id AS uuid)"
        ),
        {"id": creation["operation_id"]},
    )
    mutation = await connection.scalar(
        text(
            "SELECT to_jsonb(mutation) FROM public.storage_usage_mutations AS mutation "
            "WHERE owner_id=:id AND producer='managed_original_creation' AND kind='create' "
            "ORDER BY started_at DESC,id DESC LIMIT 1"
        ),
        {"id": creation_id},
    )
    return {"creation": creation, "operation": operation, "mutation": mutation}


async def _settle_failure(connection, creation_id, outcome, evidence):
    before = await _attempt_state(connection, creation_id)
    assert before["operation"] is not None
    assert before["mutation"]["state"] == "pending"
    reply = await _failure_reply(connection, creation_id, outcome, evidence)
    assert reply["ok"] is True, reply
    after = await _attempt_state(connection, creation_id)
    assert after["creation"]["failure_evidence"] == evidence
    assert (after["creation"]["settled_at"] is not None) == evidence["physical_settled"]
    assert after["mutation"]["id"] == before["mutation"]["id"]
    for key, value in before["mutation"]["evidence"].items():
        assert after["mutation"]["evidence"][key] == value
    if evidence["physical_settled"]:
        assert after["operation"] is None
        assert after["mutation"]["finished_at"] is not None
        assert after["mutation"]["physical_outcome"] == evidence["physical_outcome"]
        if evidence["physical_outcome"] == "not_written":
            assert after["mutation"]["state"] == "confirmed"
            assert after["mutation"]["current_delta_bytes"] == 0
        else:
            assert after["mutation"]["state"] == "unknown"
            assert after["mutation"]["current_delta_bytes"] is None
    else:
        assert after["operation"] == before["operation"]
        assert after["mutation"]["state"] == "pending"
        assert after["mutation"]["physical_outcome"] == "unknown"
        assert after["mutation"]["finished_at"] is None
        assert after["mutation"]["current_delta_bytes"] is None


async def _mark_writing(connection, creation_id):
    opened = await connection.scalar(
        text(
            "SELECT yoao_assets_v1.open_operator_storage_operation(CAST(:id AS uuid))"
        ),
        {"id": creation_id},
    )
    opened = json.loads(opened) if isinstance(opened, str) else opened
    assert opened["ok"] is True, opened
    assert opened["data"]["mode"] == "write"
    content = b"abc"
    prepared = await connection.scalar(
        text(
            "SELECT yoao_assets_v1.record_operator_original_prepared("
            "CAST(:id AS uuid),CAST(:prepared AS jsonb))"
        ),
        {
            "id": creation_id,
            "prepared": json.dumps(
                {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "md5": hashlib.md5(content, usedforsecurity=False).hexdigest(),
                    "size_bytes": len(content),
                }
            ),
        },
    )
    prepared = json.loads(prepared) if isinstance(prepared, str) else prepared
    assert prepared["ok"] is True, prepared
    writing = await connection.scalar(
        text("SELECT yoao_assets_v1.mark_operator_original_writing(CAST(:id AS uuid))"),
        {"id": creation_id},
    )
    writing = json.loads(writing) if isinstance(writing, str) else writing
    assert writing["ok"] is True, writing
    state = await _attempt_state(connection, creation_id)
    assert state["creation"]["status"] == "writing"
    assert state["creation"]["receipt"] is None
    assert state["creation"]["operation_id"] == opened["data"]["operation_id"]
    assert state["operation"]["id"] == opened["data"]["operation_id"]
    assert state["operation"]["location_id"] == state["creation"]["location_id"]
    assert state["operation"]["revision"] == state["creation"]["maintenance_revision"]
    assert state["operation"]["purpose"] == "managed_original_creation"
    assert state["mutation"]["id"] == writing["data"]["mutation_id"]
    assert state["mutation"]["owner_id"] == str(creation_id)
    assert state["mutation"]["object_key"] == state["creation"]["object_key"]
    assert state["mutation"]["state"] == "pending"
    assert state["mutation"]["physical_outcome"] == "unknown"
    assert state["mutation"]["finished_at"] is None


async def _retry_schema(connection):
    columns = (
        await connection.execute(
            text(
                "SELECT column_name,data_type,column_default,is_nullable "
                "FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name='managed_original_creations' ORDER BY ordinal_position"
            )
        )
    ).all()
    indexes = (
        await connection.scalars(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
                "AND tablename='managed_original_creations' ORDER BY indexname"
            )
        )
    ).all()
    definitions = (
        await connection.scalars(
            text(
                "SELECT pg_get_functiondef(proc.oid) FROM pg_proc AS proc "
                "JOIN pg_namespace AS namespace ON namespace.oid=proc.pronamespace "
                "WHERE namespace.nspname='aap_assets_v1' "
                "AND proc.proname IN ('begin_operator_auxiliary','fail_operator_original') "
                "ORDER BY proc.proname"
            )
        )
    ).all()
    return columns, indexes, definitions


async def test_upload_retry_forward_migration_preserves_history_and_permissions(
    retry_migration_database,
):
    config, engine = retry_migration_database
    scopes = {
        status: _scope(status)
        for status in ("failed", "conflict", "unknown", "allocated")
    }
    history_ids = {}
    async with engine.begin() as connection:
        assert (
            await connection.scalar(
                text("SELECT version_num FROM public.alembic_version")
            )
            == "bo0457"
        )
        before_contract = await _installed_contract(connection, _FROZEN_ADMISSION)
        for status, scope in scopes.items():
            history_ids[status] = await _insert_creation(connection, scope, status)
        history = await _rows(connection)
        with pytest.raises(IntegrityError) as blocked:
            async with connection.begin_nested():
                await _insert_creation(connection, scopes["failed"], "allocated")
        assert blocked.value.orig.sqlstate == "23505"
        assert await _rows(connection) == history

    await engine.dispose()
    await asyncio.to_thread(command.upgrade, config, "head")
    async with engine.begin() as connection:
        assert (
            await connection.scalar(
                text("SELECT version_num FROM public.alembic_version")
            )
            == ScriptDirectory.from_config(config).get_current_head()
        )
        current_contract = await _installed_contract(connection, _CURRENT_ADMISSION)
        assert current_contract == before_contract
        legacy_after = await _rows(connection)
        assert [
            {key: value for key, value in row.items() if key != "failure_evidence"}
            for row in legacy_after
        ] == history
        assert all(row["failure_evidence"] is None for row in legacy_after)
        for status in ("failed", "conflict", "unknown"):
            reply = await _admit(connection, scopes[status])
            assert reply["ok"] is False
            assert reply["error"]["code"] == "creation_terminal"
    async with AsyncSession(bind=engine) as session:
        await bootstrap(session)

    contradictory = scopes["contradictory-evidence"] = _scope("contradictory-evidence")
    async with engine.begin() as connection:
        contradictory_id = await _allocate(connection, contradictory)
        await _mark_writing(connection, contradictory_id)
    async with engine.begin() as connection:
        contradictory_state = await _attempt_state(connection, contradictory_id)
        before_rows = await _rows(connection)
        assert contradictory_state["creation"]["receipt"] is None
        rejected = await _failure_reply(
            connection,
            contradictory_id,
            "failed",
            {
                "physical_settled": True,
                "physical_outcome": "not_written",
                "receipt": {"etag": "observed"},
            },
        )
        assert rejected["ok"] is False
        assert rejected["error"]["code"] == "invalid_request"
        assert await _attempt_state(connection, contradictory_id) == contradictory_state
        assert await _rows(connection) == before_rows
        active = await _admit(connection, contradictory)
        assert active["ok"] is True, active
        assert active["data"]["creation_id"] == str(contradictory_id)
        assert active["data"]["status"] == "writing"
        assert await _attempt_state(connection, contradictory_id) == contradictory_state
        assert await _rows(connection) == before_rows

    for outcome in ("failed", "conflict"):
        label = f"proven-{outcome}"
        scope = scopes[label] = _scope(label)
        async with engine.begin() as connection:
            creation_id = await _allocate(connection, scope)
            await _mark_writing(connection, creation_id)
            history_ids[label] = creation_id
        async with engine.begin() as connection:
            await _settle_failure(
                connection,
                creation_id,
                outcome,
                {
                    "physical_settled": True,
                    "physical_outcome": "not_written",
                },
            )
        async with engine.begin() as connection:
            for changes in (
                {"max_bytes": 1048577},
                {"requested_by": str(uuid4())},
                {"file_path": "changed.png"},
                {"publication_id": str(uuid4())},
            ):
                changed = await _admit(connection, scope, **changes)
                assert changed["ok"] is False
                assert changed["error"]["code"] == "creation_configuration_changed"
            retry_id = await _allocate(connection, scope)
            assert retry_id != creation_id
        async with engine.begin() as connection:
            reply = await _admit(connection, scope)
            assert reply["ok"] is True, reply
            assert reply["data"]["creation_id"] == str(retry_id)
            assert reply["data"]["status"] == "allocated"

    for label, outcome, evidence in (
        (
            "written-failure",
            "failed",
            {
                "physical_settled": True,
                "physical_outcome": "written",
                "physical_size_bytes": 3,
                "receipt": {"etag": "possibly-written"},
            },
        ),
        (
            "unsettled",
            "unknown",
            {
                "physical_settled": False,
                "physical_outcome": "unknown",
            },
        ),
    ):
        scope = scopes[label] = _scope(label)
        async with engine.begin() as connection:
            creation_id = await _allocate(connection, scope)
            await _mark_writing(connection, creation_id)
            history_ids[label] = creation_id
        async with engine.begin() as connection:
            await _settle_failure(connection, creation_id, outcome, evidence)
        async with engine.begin() as connection:
            reply = await _admit(connection, scope)
            assert reply["ok"] is False
            assert reply["error"]["code"] == "creation_terminal"

    async with engine.begin() as connection:
        with_retries = await _rows(connection)
        assert len(with_retries) == len(history) + 7
        assert all(row in with_retries for row in legacy_after)
        for scope in scopes.values():
            for override in (
                {"staging_key": f"{_STAGING_KEY}/{uuid4()}"},
                {"object_key": f"{_RESERVED_KEY}/{uuid4()}"},
            ):
                with pytest.raises(IntegrityError) as blocked:
                    async with connection.begin_nested():
                        await _insert_creation(
                            connection, scope, "allocated", **override
                        )
                assert blocked.value.orig.sqlstate == "23505"
                assert await _rows(connection) == with_retries
        for label, creation_id in history_ids.items():
            if label == "allocated":
                continue
            for statement in (
                "UPDATE public.managed_original_creations SET error_code='rewritten' "
                "WHERE id=:id",
                "DELETE FROM public.managed_original_creations WHERE id=:id",
                "UPDATE public.managed_original_creations SET failure_evidence="
                "jsonb_build_object('physical_settled',true,"
                "'physical_outcome','not_written','tampered',true) "
                "WHERE id=:id",
            ):
                with pytest.raises(IntegrityError) as immutable:
                    async with connection.begin_nested():
                        await connection.execute(text(statement), {"id": creation_id})
                assert immutable.value.orig.sqlstate == "23514"
                assert await _rows(connection) == with_retries
        assert await _attempt_state(connection, contradictory_id) == contradictory_state


@pytest.mark.parametrize("locator", ["reserved", "staging"])
async def test_forward_upgrade_refuses_unproven_shared_keys_atomically(
    retry_migration_database,
    locator,
):
    config, engine = retry_migration_database
    await asyncio.to_thread(command.upgrade, config, "bo0458")
    async with engine.begin() as connection:
        first, second = _scope("legacy-failure"), _scope("legacy-retry")
        await _insert_creation(connection, first, "failed")
        shared = (
            {"object_key": first["object_key"]}
            if locator == "reserved"
            else {"staging_key": first["staging_key"]}
        )
        await _insert_creation(connection, second, "allocated", **shared)
        before_rows = await _rows(connection)
        before_schema = await _retry_schema(connection)
        before_contract = await _installed_contract(connection, _FROZEN_ADMISSION)

    await engine.dispose()
    with pytest.raises(IntegrityError) as refused:
        await asyncio.to_thread(command.upgrade, config, "head")
    assert refused.value.orig.sqlstate == "23505"
    async with engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT version_num FROM public.alembic_version")
            )
            == "bo0458"
        )
        assert await _rows(connection) == before_rows
        assert await _retry_schema(connection) == before_schema
        recovered_contract = await _installed_contract(connection, _FROZEN_ADMISSION)
        assert recovered_contract == before_contract
