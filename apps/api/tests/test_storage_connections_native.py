"""Behavioral contracts for the Assets-owned storage connection capabilities."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import json
import os
import re
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.storage_connection import StorageConnection
from app.db.models.dataset_import import DatasetImportOperation
from tests.test_assets_catalog_native import _call, _seed_actor, _seed_dataset

pytestmark = pytest.mark.asyncio

_SCHEMA = "aap-assets-connections/v1"
_LIST = text(
    "SELECT aap_assets_v1.list_storage_connections("
    "CAST(:actor_id AS uuid), CAST(:after_created_at AS timestamptz), "
    "CAST(:after_id AS uuid), CAST(:page_size AS integer))"
)
_READ = text(
    "SELECT aap_assets_v1.read_storage_connection("
    "CAST(:actor_id AS uuid), CAST(:connection_id AS uuid))"
)
_POLICY = text(
    "SELECT aap_assets_v1.read_storage_connection_policy("
    "CAST(:actor_id AS uuid), CAST(:fallback_allowlist AS jsonb))"
)
_CAPTURE = text(
    "SELECT aap_assets_v1.capture_storage_connection("
    "CAST(:actor_id AS uuid), CAST(:connection_id AS uuid), CAST(:access AS text))"
)
_CREATE = text(
    "SELECT aap_assets_v1.create_storage_connection("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:intent AS jsonb), CAST(:config AS jsonb), CAST(:secret_enc AS text), "
    "CAST(:policy_digest AS text), CAST(:fallback_allowlist AS jsonb), "
    "CAST(:audit_context AS jsonb))"
)
_UPDATE = text(
    "SELECT aap_assets_v1.update_storage_connection_authorized("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:connection_id AS uuid), CAST(:intent AS jsonb), CAST(:config AS jsonb), "
    "CAST(:secret_enc AS text), CAST(:source_snapshot AS text), "
    "CAST(:policy_digest AS text), CAST(:fallback_allowlist AS jsonb), "
    "CAST(:audit_context AS jsonb), NULL::uuid, '{}'::text[], NULL::jsonb)"
)
_DELETE = text(
    "SELECT aap_assets_v1.delete_storage_connection("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:connection_id AS uuid), CAST(:audit_context AS jsonb))"
)
_RECEIPT = text(
    "SELECT aap_assets_v1.read_storage_connection_receipt("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), CAST(:action AS text), "
    "CAST(:connection_id AS uuid), CAST(:intent AS jsonb))"
)
_SECRET = base64.b64encode(b"sealed-test-ciphertext").decode("ascii")
_FALLBACK = "[]"


def _reply(value: object) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    assert isinstance(value, dict)
    assert value.get("schema") == _SCHEMA
    return value


def _data(value: object) -> dict:
    reply = _reply(value)
    assert set(reply) == {"schema", "ok", "data"}
    assert reply["ok"] is True
    assert isinstance(reply["data"], dict)
    return reply["data"]


def _error(value: object, code: str) -> None:
    assert _reply(value) == {
        "schema": _SCHEMA,
        "ok": False,
        "error": {"code": code},
    }


async def _policy(db_session, actor_id: UUID) -> dict:
    return _data(
        await _call(
            db_session,
            _POLICY,
            {"actor_id": actor_id, "fallback_allowlist": _FALLBACK},
        )
    )


async def _capture(db_session, actor_id: UUID, connection_id: UUID, access="admin"):
    return await _call(
        db_session,
        _CAPTURE,
        {"actor_id": actor_id, "connection_id": connection_id, "access": access},
    )


def _s3_config(endpoint: str = "https://objects.example.test") -> dict:
    return {
        "endpoint": endpoint,
        "bucket": "connection-fixture",
        "region": "test-1",
        "base_prefix": "inputs/",
        "use_ssl": True,
    }


async def _seed_connection(
    db_session,
    *,
    actor_id: UUID,
    scope: str = "owner",
    kind: str = "s3",
    config: dict | None = None,
    secret: bytes = b"opaque-test-ciphertext",
    name: str = "storage connection fixture",
) -> UUID:
    connection_id = uuid4()
    db_session.add(
        StorageConnection(
            id=connection_id,
            name=name,
            kind=kind,
            config=config or _s3_config(),
            secret_enc=secret,
            scope=scope,
            project_id=None,
            created_by=actor_id,
        )
    )
    await db_session.flush()
    return connection_id


async def test_native_connection_reads_preserve_visibility_and_keep_ciphertext_out_of_facts(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="project_admin")
    other_id = await _seed_actor(db_session, role="project_admin")
    first_id = await _seed_connection(
        db_session,
        actor_id=actor_id,
        name="owner-first",
        secret=b"private-ciphertext-one",
    )
    foreign_id = await _seed_connection(
        db_session,
        actor_id=other_id,
        name="foreign-owner",
        secret=b"private-ciphertext-two",
    )
    global_id = await _seed_connection(
        db_session,
        actor_id=other_id,
        scope="global",
        name="visible-global",
        secret=b"private-ciphertext-three",
    )

    page_one = _data(
        await _call(
            db_session,
            _LIST,
            {
                "actor_id": actor_id,
                "after_created_at": None,
                "after_id": None,
                "page_size": 1,
            },
        )
    )
    assert page_one["actor_id"] == str(actor_id)
    assert page_one["total"] == 2
    assert page_one["complete"] is False
    assert page_one["next_cursor"] is not None

    page_two = _data(
        await _call(
            db_session,
            _LIST,
            {
                "actor_id": actor_id,
                "after_created_at": datetime.fromisoformat(
                    page_one["next_cursor"]["created_at"]
                ),
                "after_id": UUID(page_one["next_cursor"]["id"]),
                "page_size": 1,
            },
        )
    )
    assert page_two["total"] == page_one["total"]
    assert page_two["snapshot_digest"] == page_one["snapshot_digest"]
    assert page_two["complete"] is True
    visible_ids = {
        item["id"] for item in (*page_one["connections"], *page_two["connections"])
    }
    assert visible_ids == {str(first_id), str(global_id)}
    facts = page_one["connections"] + page_two["connections"]
    assert all("secret_enc" not in connection for connection in facts)
    assert all(isinstance(connection["secret_set"], bool) for connection in facts)
    assert not any(
        b"private-ciphertext".decode() in json.dumps(connection) for connection in facts
    )

    hidden = await _call(
        db_session, _READ, {"actor_id": actor_id, "connection_id": foreign_id}
    )
    _error(hidden, "connection_missing")

    captured = _data(await _capture(db_session, actor_id, global_id, "use"))
    assert set(captured) == {
        "actor_id",
        "platform_role",
        "connection_id",
        "access",
        "connection",
        "secret_enc",
        "source_snapshot",
    }
    assert captured["actor_id"] == str(actor_id)
    assert captured["connection_id"] == str(global_id)
    assert (
        captured["secret_enc"] == base64.b64encode(b"private-ciphertext-three").decode()
    )
    assert re.fullmatch(r"[0-9a-f]{64}", captured["source_snapshot"])

    private_admin = await _capture(db_session, actor_id, foreign_id, "admin")
    _error(private_admin, "connection_management_forbidden")
    private_use = await _capture(db_session, actor_id, foreign_id, "use")
    _error(private_use, "connection_missing")
    global_admin = await _capture(db_session, actor_id, global_id, "admin")
    _error(global_admin, "global_management_forbidden")


async def test_native_connection_replay_survives_creator_role_change_and_delete(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="project_admin")
    operation_id = uuid4()
    config = _s3_config()
    intent = {
        "name": "created by manager",
        "kind": "s3",
        "config": config,
        "scope": "owner",
        "secret_digest": "a" * 64,
    }
    policy = await _policy(db_session, actor_id)
    arguments = {
        "operation_id": operation_id,
        "actor_id": actor_id,
        "intent": json.dumps(intent),
        "config": json.dumps(config),
        "secret_enc": _SECRET,
        "policy_digest": policy["policy_digest"],
        "fallback_allowlist": _FALLBACK,
        "audit_context": "{}",
    }
    created = _data(await _call(db_session, _CREATE, arguments))
    assert created["operation_id"] == str(operation_id)
    assert created["connection_id"] == str(operation_id)
    assert created["connection"]["id"] == str(operation_id)
    assert created["connection"]["project_id"] is None
    assert created["connection"]["scope"] == "owner"
    assert "secret_enc" not in created["connection"]

    replay = _data(
        await _call(
            db_session,
            _CREATE,
            {
                **arguments,
                "secret_enc": base64.b64encode(b"fresh randomized ciphertext").decode(),
                "audit_context": '{"method":"POST"}',
            },
        )
    )
    assert replay == created
    changed_intent = {**intent, "name": "different intent"}
    conflict = await _call(
        db_session, _CREATE, {**arguments, "intent": json.dumps(changed_intent)}
    )
    _error(conflict, "operation_conflict")

    initial_capture = _data(await _capture(db_session, actor_id, operation_id))
    previous_revision = initial_capture["connection"]["revision"]
    noop_intent = {"name": None, "config": None, "secret_digest": None}
    noop = _data(
        await _call(
            db_session,
            _UPDATE,
            {
                "operation_id": uuid4(),
                "actor_id": actor_id,
                "connection_id": operation_id,
                "intent": json.dumps(noop_intent),
                "config": json.dumps(config),
                "secret_enc": None,
                "source_snapshot": initial_capture["source_snapshot"],
                "policy_digest": None,
                # A name-only/no-op update does not consult Settings.
                "fallback_allowlist": "null",
                "audit_context": "{}",
            },
        )
    )
    assert noop["connection"]["revision"] == previous_revision + 1

    await db_session.execute(
        text("UPDATE public.users SET role='viewer' WHERE id=:id"), {"id": actor_id}
    )
    # Owner creation replay uses the saved owner scope and current creator identity.
    assert _data(await _call(db_session, _CREATE, arguments)) == created

    update_operation = uuid4()
    update_capture = _data(await _capture(db_session, actor_id, operation_id))
    update_intent = {
        "name": "creator after downgrade",
        "config": None,
        "secret_digest": None,
    }
    updated = _data(
        await _call(
            db_session,
            _UPDATE,
            {
                "operation_id": update_operation,
                "actor_id": actor_id,
                "connection_id": operation_id,
                "intent": json.dumps(update_intent),
                "config": json.dumps(config),
                "secret_enc": None,
                "source_snapshot": update_capture["source_snapshot"],
                "policy_digest": None,
                "fallback_allowlist": "[]",
                "audit_context": "{}",
            },
        )
    )
    assert updated["connection"]["name"] == "creator after downgrade"

    delete_operation = uuid4()
    deleted = _data(
        await _call(
            db_session,
            _DELETE,
            {
                "operation_id": delete_operation,
                "actor_id": actor_id,
                "connection_id": operation_id,
                "audit_context": "{}",
            },
        )
    )
    assert deleted["connection"] is None
    assert (
        _data(
            await _call(
                db_session,
                _DELETE,
                {
                    "operation_id": delete_operation,
                    "actor_id": actor_id,
                    "connection_id": operation_id,
                    "audit_context": '{"path":"/replay"}',
                },
            )
        )
        == deleted
    )
    receipt = _data(
        await _call(
            db_session,
            _RECEIPT,
            {
                "operation_id": delete_operation,
                "actor_id": actor_id,
                "action": "delete",
                "connection_id": operation_id,
                "intent": "{}",
            },
        )
    )
    assert receipt["result"] == deleted
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.storage_connection_operations WHERE operation_id=:id"
            ),
            {"id": delete_operation},
        )
        == 1
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.audit_logs "
                "WHERE action='storage_connection.delete' AND target_id=:id"
            ),
            {"id": str(operation_id)},
        )
        == 1
    )


async def test_native_connection_source_and_policy_snapshots_fence_replacement(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="project_admin")
    connection_id = await _seed_connection(db_session, actor_id=actor_id)
    stale_capture = _data(await _capture(db_session, actor_id, connection_id))
    policy = await _policy(db_session, actor_id)

    await db_session.execute(
        text(
            "UPDATE public.storage_connections SET name='owner-side-change' WHERE id=:id"
        ),
        {"id": connection_id},
    )
    stale_intent = {"name": "must not apply", "config": None, "secret_digest": None}
    stale_operation = uuid4()
    stale = await _call(
        db_session,
        _UPDATE,
        {
            "operation_id": stale_operation,
            "actor_id": actor_id,
            "connection_id": connection_id,
            "intent": json.dumps(stale_intent),
            "config": json.dumps(_s3_config()),
            "secret_enc": None,
            "source_snapshot": stale_capture["source_snapshot"],
            "policy_digest": None,
            "fallback_allowlist": "[]",
            "audit_context": "{}",
        },
    )
    _error(stale, "source_changed")
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.storage_connection_operations WHERE operation_id=:id"
            ),
            {"id": stale_operation},
        )
        == 0
    )
    assert (
        await db_session.scalar(
            text("SELECT name FROM public.storage_connections WHERE id=:id"),
            {"id": connection_id},
        )
        == "owner-side-change"
    )
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.audit_logs WHERE target_id=:id"),
            {"id": str(connection_id)},
        )
        == 0
    )

    fresh_capture = _data(await _capture(db_session, actor_id, connection_id))
    replacement_host = f"replacement-{uuid4().hex}.example.test"
    replacement = _s3_config(f"https://{replacement_host}")
    replacement_intent = {
        "name": None,
        "config": replacement,
        "secret_digest": None,
    }
    # A current Settings row now supersedes the older environment/database policy.
    await db_session.execute(
        text(
            "INSERT INTO public.system_settings(key, value_type, value_json) "
            "VALUES ('connector_host_allowlist', 'json', CAST(:value AS jsonb)) "
            "ON CONFLICT (key) DO UPDATE SET value_json=EXCLUDED.value_json"
        ),
        {"value": json.dumps([replacement_host])},
    )
    policy_operation = uuid4()
    changed_policy = await _call(
        db_session,
        _UPDATE,
        {
            "operation_id": policy_operation,
            "actor_id": actor_id,
            "connection_id": connection_id,
            "intent": json.dumps(replacement_intent),
            "config": json.dumps(replacement),
            "secret_enc": None,
            "source_snapshot": fresh_capture["source_snapshot"],
            "policy_digest": policy["policy_digest"],
            "fallback_allowlist": _FALLBACK,
            "audit_context": "{}",
        },
    )
    _error(changed_policy, "policy_changed")
    assert (
        await db_session.scalar(
            text("SELECT config FROM public.storage_connections WHERE id=:id"),
            {"id": connection_id},
        )
        == _s3_config()
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM public.storage_connection_operations "
                "WHERE operation_id=:id"
            ),
            {"id": policy_operation},
        )
        == 0
    )
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.audit_logs WHERE target_id=:id"),
            {"id": str(connection_id)},
        )
        == 0
    )


async def test_native_connection_config_patch_replaces_the_complete_config(db_session):
    actor_id = await _seed_actor(db_session, role="project_admin")
    connection_id = await _seed_connection(db_session, actor_id=actor_id)
    captured = _data(await _capture(db_session, actor_id, connection_id))
    policy = await _policy(db_session, actor_id)
    replacement = {
        "endpoint": "https://replacement.example.test",
        "bucket": "replacement-bucket",
    }
    intent = {"name": None, "config": replacement, "secret_digest": None}
    updated = _data(
        await _call(
            db_session,
            _UPDATE,
            {
                "operation_id": uuid4(),
                "actor_id": actor_id,
                "connection_id": connection_id,
                "intent": json.dumps(intent),
                "config": json.dumps(replacement),
                "secret_enc": None,
                "source_snapshot": captured["source_snapshot"],
                "policy_digest": policy["policy_digest"],
                "fallback_allowlist": _FALLBACK,
                "audit_context": "{}",
            },
        )
    )
    assert updated["connection"]["config"] == replacement
    assert "region" not in updated["connection"]["config"]
    assert "base_prefix" not in updated["connection"]["config"]
    assert "use_ssl" not in updated["connection"]["config"]


async def test_native_connection_secret_only_sftp_patch_infers_missing_auth_type(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="project_admin")
    connection_id = await _seed_connection(
        db_session,
        actor_id=actor_id,
        kind="sftp",
        config={"host": "sftp.example.test", "username": "fixture"},
    )
    captured = _data(await _capture(db_session, actor_id, connection_id))
    intent = {"name": None, "config": None, "secret_digest": "b" * 64}
    candidate = {
        "host": "sftp.example.test",
        "username": "fixture",
        "auth_type": "password",
    }
    result = _data(
        await _call(
            db_session,
            _UPDATE,
            {
                "operation_id": uuid4(),
                "actor_id": actor_id,
                "connection_id": connection_id,
                "intent": json.dumps(intent),
                "config": json.dumps(candidate),
                "secret_enc": base64.b64encode(b"new-sealed-ciphertext").decode(),
                "source_snapshot": captured["source_snapshot"],
                "policy_digest": None,
                "fallback_allowlist": "null",
                "audit_context": "{}",
            },
        )
    )
    assert result["connection"]["config"] == candidate
    assert result["connection"]["secret_set"] is True


async def test_native_connection_delete_retains_import_snapshot_and_fences_new_import(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="project_admin")
    dataset_id = await _seed_dataset(db_session, actor_id=actor_id)
    connection_id = await _seed_connection(db_session, actor_id=actor_id)
    job_id = uuid4()
    db_session.add(
        DatasetImportOperation(
            job_id=job_id,
            actor_id=actor_id,
            dataset_id=dataset_id,
            connection_id=connection_id,
            connection_name="retained source snapshot",
            connection_kind="s3",
            connection_scope="owner",
            connection_created_by=actor_id,
            connection_updated_at=datetime.now(UTC),
            connection_config=_s3_config(),
            connection_secret_enc=b"retained-encrypted-snapshot",
            request={"source_path": "imports/"},
            settings_snapshot={},
            stage="enumerating",
        )
    )
    await db_session.flush()

    deleted = _data(
        await _call(
            db_session,
            _DELETE,
            {
                "operation_id": uuid4(),
                "actor_id": actor_id,
                "connection_id": connection_id,
                "audit_context": "{}",
            },
        )
    )
    assert deleted["connection"] is None
    retained = await db_session.execute(
        text(
            "SELECT connection_id, connection_secret_enc "
            "FROM public.dataset_import_operations WHERE job_id=:job_id"
        ),
        {"job_id": job_id},
    )
    snapshot = retained.one()
    assert snapshot.connection_id == connection_id
    assert snapshot.connection_secret_enc == b"retained-encrypted-snapshot"

    later_import = await db_session.scalar(
        text(
            "SELECT aap_assets_v1.prepare_dataset_import("
            "CAST(:actor_id AS uuid), CAST(:dataset_id AS uuid), CAST(:connection_id AS uuid))"
        ),
        {
            "actor_id": actor_id,
            "dataset_id": dataset_id,
            "connection_id": connection_id,
        },
    )
    assert later_import["schema"] == "aap-dataset-import/v1"
    assert later_import["ok"] is False
    assert later_import["error"]["code"] == "connection_missing"


@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the owned disposable migration database",
)
async def test_native_connection_receipt_failure_rolls_back_connection_and_audit(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as owner:
        database = await owner.scalar(text("SELECT current_database()"))
        if not database or not database.endswith("_test"):
            pytest.fail(
                "refusing receipt-failure probe outside the owned test database"
            )
        actor_id = await _seed_actor(owner, role="project_admin")
        await owner.commit()

    operation_id = uuid4()
    function_name = f"_conn_op_fail_{uuid4().hex}"
    trigger_name = f"conn_op_fail_{uuid4().hex}"
    trigger_created = False
    try:
        async with test_engine.begin() as owner:
            await owner.exec_driver_sql(
                f"CREATE FUNCTION aap_assets_v1.{function_name}() "
                "RETURNS trigger LANGUAGE plpgsql AS $probe$ "
                "BEGIN RAISE EXCEPTION USING ERRCODE='22000', "
                "MESSAGE='native storage connection receipt failure probe'; "
                "END; $probe$"
            )
            await owner.exec_driver_sql(
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT "
                "ON public.storage_connection_operations FOR EACH ROW "
                f"EXECUTE FUNCTION aap_assets_v1.{function_name}()"
            )
            trigger_created = True
            policy = _data(
                await owner.scalar(
                    _POLICY,
                    {"actor_id": actor_id, "fallback_allowlist": _FALLBACK},
                )
            )
            config = _s3_config()
            intent = {
                "name": "receipt failure rollback",
                "kind": "s3",
                "config": config,
                "scope": "owner",
                "secret_digest": "c" * 64,
            }
            failed = await owner.scalar(
                _CREATE,
                {
                    "operation_id": operation_id,
                    "actor_id": actor_id,
                    "intent": json.dumps(intent),
                    "config": json.dumps(config),
                    "secret_enc": _SECRET,
                    "policy_digest": policy["policy_digest"],
                    "fallback_allowlist": _FALLBACK,
                    "audit_context": "{}",
                },
            )
            _error(failed, "storage_unavailable")
            assert (
                await owner.scalar(
                    text(
                        "SELECT count(*) FROM public.storage_connections WHERE id=:id"
                    ),
                    {"id": operation_id},
                )
                == 0
            )
            assert (
                await owner.scalar(
                    text(
                        "SELECT count(*) FROM public.storage_connection_operations "
                        "WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 0
            )
            assert (
                await owner.scalar(
                    text("SELECT count(*) FROM public.audit_logs WHERE target_id=:id"),
                    {"id": str(operation_id)},
                )
                == 0
            )
            await owner.exec_driver_sql(
                f"DROP TRIGGER {trigger_name} ON public.storage_connection_operations"
            )
            await owner.exec_driver_sql(
                f"DROP FUNCTION aap_assets_v1.{function_name}()"
            )
            trigger_created = False
    finally:
        if trigger_created:
            async with test_engine.begin() as owner:
                await owner.exec_driver_sql(
                    f"DROP TRIGGER IF EXISTS {trigger_name} "
                    "ON public.storage_connection_operations"
                )
                await owner.exec_driver_sql(
                    f"DROP FUNCTION IF EXISTS aap_assets_v1.{function_name}()"
                )
        async with maker.begin() as owner:
            await owner.execute(
                text("DELETE FROM public.users WHERE id=:id"), {"id": actor_id}
            )
