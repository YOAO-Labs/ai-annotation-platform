"""Owned Native acceptance for coordinated Jobs and Inference restore."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_inference.restore import VideoRestoreManager
from aap_inference.restore_contract import RestoreCommand
from aap_inference.restore_postgres import VideoRestorePostgresProvider
from aap_owner_data.postgres import verify_function_only_role
from aap_preannotation_results.video_execution import (
    ClaimRequest,
    RenewRequest,
    VideoExecutionFailure,
)
from aap_preannotation_results_runtime.video_execution_database import (
    VideoExecutionPostgresData,
)
from tests.test_identity_recovery import _alembic, _postgres_tool
from tests.test_inference_backend_dependency_reads_restore import (
    _assert_clone_identity,
    _create_owned_clone,
    _database_acl,
    _drop_owned_clone,
)
from tests.test_labeling_video_writer_restore import (
    ROOT,
    _apply_current_owner_grants,
    _revoke_public_temp,
    _run_psql_file,
)
from tests.test_module_data_roles import _role_url
from tests.test_video_window_journal_native import _prepared_execution

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned disposable PostgreSQL resources",
    ),
]

_RESTORE_ROLE = "video_restore_operator_client"
_RESTORE_FUNCTIONS = {("video_restore_step", "text, text")}
_RESTORE_SCHEMA = "aap_inference_routing_v1"
_RESTORE_GRANTS = ROOT / "infra/docker/video-restore-grants.sql"
_EXECUTION_GRANTS = ROOT / "infra/docker/video-execution-grants.sql"


def _role_dsn(environment: str, role: str, database: str) -> str:
    parsed = make_url(_role_url(environment, role))
    return parsed.set(drivername="postgresql", database=database).render_as_string(
        hide_password=False
    )


def _apply_video_execution_grants(test_engine, database: str) -> None:
    _run_psql_file(
        test_engine,
        database,
        _EXECUTION_GRANTS,
        {
            "expected_database": database,
            "migration_role": "owner",
            "jobs_owner": "jobs_code",
            "video_execution_role": "video_execution_client",
        },
    )


def _apply_restore_grants(test_engine, database: str) -> None:
    _run_psql_file(
        test_engine,
        database,
        _RESTORE_GRANTS,
        {
            "expected_database": database,
            "migration_role": "owner",
            "inference_owner": "inference_code",
            "video_restore_operator_role": _RESTORE_ROLE,
        },
    )


def _apply_owned_grants(test_engine, database: str) -> None:
    _apply_current_owner_grants(test_engine, database)
    _run_psql_file(
        test_engine,
        database,
        ROOT / "infra/docker/inference-routing-grants.sql",
        {
            "expected_database": database,
            "migration_role": "owner",
            "inference_owner": "inference_code",
            "inference_routing_role": "inference_routing_client",
        },
    )
    _apply_video_execution_grants(test_engine, database)
    _apply_restore_grants(test_engine, database)


async def _verify_restore_role(dsn: str) -> None:
    connection = await asyncpg.connect(dsn, timeout=5, command_timeout=15)
    try:
        role, database = await connection.fetchrow(
            "SELECT current_user,current_database()"
        )
        assert role == _RESTORE_ROLE
        assert database == make_url(dsn).database
        await verify_function_only_role(
            connection,
            schema=_RESTORE_SCHEMA,
            functions=_RESTORE_FUNCTIONS,
            role=_RESTORE_ROLE,
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetch("SELECT id FROM public.users LIMIT 1")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.execute(
                "CREATE TEMP TABLE forbidden_restore_data(value int)"
            )
    finally:
        await connection.close()


def _json_value(value):
    return json.loads(value) if isinstance(value, str) else value


async def _operator_identity(provider: VideoRestorePostgresProvider) -> dict[str, str]:
    reply = _json_value(await provider.step("inspect", {}))
    assert reply["schema"] == "aap-video-restore-data/v1"
    assert reply["status"] == "ok"
    identity = reply["identity"]
    assert set(identity) == {
        "source_jobs_realm",
        "target_jobs_realm",
        "source_routing_realm",
        "target_routing_realm",
        "source_jobs_secret_sha256",
        "source_routing_secret_sha256",
    }
    assert all(type(value) is str and value for value in identity.values())
    for key, value in identity.items():
        if key.endswith("secret_sha256"):
            assert len(value) == 64
            assert all(character in "0123456789abcdef" for character in value)
    return identity


def _command(identity: dict[str, str], dump: Path) -> RestoreCommand:
    return RestoreCommand(
        restore_id=str(uuid.uuid4()),
        backup_sha256=hashlib.sha256(dump.read_bytes()).hexdigest(),
        fence_sha256=hashlib.sha256(
            f"owned-acceptance-fence:{uuid.uuid4()}".encode()
        ).hexdigest(),
        **identity,
    )


def _identity_fields(command: RestoreCommand) -> dict[str, str]:
    fields = asdict(command)
    return fields


async def _restore_call(connection, action: str, arguments: dict):
    raw = await connection.fetchval(
        f"SELECT {_RESTORE_SCHEMA}.video_restore_step($1::text,$2::text)",
        action,
        json.dumps(
            arguments,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
    )
    return _json_value(raw)


async def _create_source_database(test_engine, admin, database, marker, acl, created):
    await _create_owned_clone(admin, test_engine, database, marker, acl, created)
    await _revoke_public_temp(admin, test_engine, database)
    engine = create_async_engine(
        test_engine.url.set(database=database), hide_parameters=True
    )
    migration = _alembic(engine.url, "upgrade", "head")
    assert migration.returncode == 0, "owned source database migration failed"
    _apply_owned_grants(test_engine, database)
    await _assert_clone_identity(engine, database, marker)
    return engine


async def _create_restored_database(
    test_engine, admin, source_engine, database, marker, created, dump: Path
):
    await _create_owned_clone(
        admin,
        test_engine,
        database,
        marker,
        await _database_acl(source_engine),
        created,
    )
    await _revoke_public_temp(admin, test_engine, database)
    _postgres_tool(
        test_engine,
        ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", database],
        body=dump.read_bytes(),
    )
    engine = create_async_engine(
        test_engine.url.set(database=database), hide_parameters=True
    )
    _apply_owned_grants(test_engine, database)
    await _assert_clone_identity(engine, database, marker)
    return engine


async def test_restore_operator_inspect_prepare_replay_conflict_and_role_boundary(
    test_engine, tmp_path
):
    """The operator can inspect and prepare a copied authority through one function."""
    source_database = f"aap_video_restore_source_{uuid.uuid4().hex[:8]}_test"
    restored_database = f"aap_video_restore_target_{uuid.uuid4().hex[:8]}_test"
    source_marker = f"video-restore-source:{uuid.uuid4()}"
    restored_marker = f"video-restore-target:{uuid.uuid4()}"
    created: set[str] = set()
    source_engine = restored_engine = None
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    dump = tmp_path / "video-restore-native.dump"
    provider = None
    try:
        acl = await _database_acl(test_engine)
        source_engine = await _create_source_database(
            test_engine,
            admin,
            source_database,
            source_marker,
            acl,
            created,
        )

        dump.touch(mode=0o600, exist_ok=False)
        dump.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", source_database],
            )
        )
        assert dump.read_bytes().startswith(b"PGDMP")

        restored_engine = await _create_restored_database(
            test_engine,
            admin,
            source_engine,
            restored_database,
            restored_marker,
            created,
            dump,
        )

        operator_dsn = _role_dsn(
            "AAP_MODULE_DATA_VIDEO_RESTORE_URL", _RESTORE_ROLE, restored_database
        )
        await _verify_restore_role(operator_dsn)
        provider = VideoRestorePostgresProvider(operator_dsn)
        identity = await _operator_identity(provider)
        assert identity["source_jobs_realm"] != identity["target_jobs_realm"]
        assert identity["source_routing_realm"] != identity["target_routing_realm"]

        command = _command(identity, dump)
        command_payload = _identity_fields(command)

        # Secret-bound Native authority refuses snapshotting isolation and
        # Serializable, since either can preserve a pre-restore snapshot across
        # the realm rotation.
        video_execution_dsn = _role_dsn(
            "AAP_MODULE_DATA_VIDEO_EXECUTION_URL",
            "video_execution_client",
            restored_database,
        )
        for isolation in ("repeatable_read", "serializable"):
            connection = await asyncpg.connect(
                video_execution_dsn, timeout=5, command_timeout=15
            )
            try:
                transaction = connection.transaction(isolation=isolation)
                await transaction.start()
                with pytest.raises(VideoExecutionFailure) as isolation_error:
                    await VideoExecutionPostgresData.bind(connection)
                assert isolation_error.value.code == "storage_unavailable"
                await transaction.rollback()
            finally:
                await connection.close()

            connection = await asyncpg.connect(
                operator_dsn, timeout=5, command_timeout=15
            )
            try:
                transaction = connection.transaction(isolation=isolation)
                await transaction.start()
                unsupported = await _restore_call(
                    connection, "prepare", {"command": command_payload}
                )
                assert unsupported["status"] == "rejected"
                assert unsupported["reason"] == "unsupported_isolation"
                await transaction.rollback()
            finally:
                await connection.close()

        first = _json_value(
            await provider.step("prepare", {"command": command_payload})
        )
        replay = _json_value(
            await provider.step("prepare", {"command": command_payload})
        )
        assert first["schema"] == replay["schema"] == "aap-video-restore-data/v1"
        assert first["status"] == replay["status"] == "ok"
        assert first["restore"] == replay["restore"]
        assert first["restore"]["phase"] == "prepared"

        changed = dict(command_payload)
        changed["fence_sha256"] = hashlib.sha256(b"conflicting-fence").hexdigest()
        conflict = _json_value(await provider.step("prepare", {"command": changed}))
        assert conflict["schema"] == "aap-video-restore-data/v1"
        assert conflict["status"] == "rejected"
        assert conflict["reason"] == "restore_conflict"
        after_conflict = _json_value(
            await provider.step("status", {"restore_id": command.restore_id})
        )
        assert after_conflict["restore"] == first["restore"]
        assert after_conflict["restore"]["phase"] == "prepared"

        manager = VideoRestoreManager(provider)
        assert (await manager.status(command.restore_id)).state == "pending"
    finally:
        if provider is not None:
            await provider.close()
        if restored_engine is not None:
            await restored_engine.dispose()
        if source_engine is not None:
            await source_engine.dispose()
        if restored_database in created:
            await _drop_owned_clone(
                admin, test_engine, restored_database, restored_marker
            )
        if source_database in created:
            await _drop_owned_clone(admin, test_engine, source_database, source_marker)
        await admin.dispose()


async def test_same_identity_rotation_waits_for_writer_and_invalidates_old_proof(
    test_engine, tmp_path
):
    database = f"aap_video_restore_same_{uuid.uuid4().hex[:8]}_test"
    marker = f"video-restore-same:{uuid.uuid4()}"
    created: set[str] = set()
    engine = None
    connection = None
    provider = None
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    try:
        engine = await _create_source_database(
            test_engine,
            admin,
            database,
            marker,
            await _database_acl(test_engine),
            created,
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as db:
            job, old_proof, plan = await _prepared_execution(db)
        dump = tmp_path / "same-identity.dump"
        dump.touch(mode=0o600, exist_ok=False)
        dump.write_bytes(
            _postgres_tool(test_engine, ["pg_dump", "-U", "owner", "-Fc", database])
        )
        provider = VideoRestorePostgresProvider(
            _role_dsn("AAP_MODULE_DATA_VIDEO_RESTORE_URL", _RESTORE_ROLE, database)
        )
        identity = await _operator_identity(provider)
        assert identity["source_jobs_realm"] == identity["target_jobs_realm"]
        assert identity["source_routing_realm"] == identity["target_routing_realm"]
        command = _command(identity, dump)
        connection = await asyncpg.connect(
            _role_dsn(
                "AAP_MODULE_DATA_VIDEO_EXECUTION_URL",
                "video_execution_client",
                database,
            ),
            timeout=5,
            command_timeout=15,
        )
        transaction = connection.transaction()
        await transaction.start()
        try:
            data = await VideoExecutionPostgresData.bind(connection)
            await data.step("renew", RenewRequest(**old_proof.model_dump()))
            busy = await provider.step("prepare", {"command": command.as_dict()})
            assert busy["status"] == "rejected"
            assert busy["reason"] == "execution_busy"
        finally:
            await transaction.rollback()
        missing = await provider.step("status", {"restore_id": command.restore_id})
        assert missing["status"] == "rejected"
        assert missing["reason"] == "restore_missing"

        completed = await VideoRestoreManager(provider).execute(command)
        assert completed.state == "completed", completed.reason
        assert completed.record["counts"]["executions_invalidated"] == 1
        after_identity = await _operator_identity(provider)
        assert (
            after_identity["source_jobs_secret_sha256"]
            != identity["source_jobs_secret_sha256"]
        )
        assert (
            after_identity["source_routing_secret_sha256"]
            != identity["source_routing_secret_sha256"]
        )
        async with connection.transaction():
            data = await VideoExecutionPostgresData.bind(connection)
            with pytest.raises(VideoExecutionFailure) as stale:
                await data.step("renew", RenewRequest(**old_proof.model_dump()))
            assert stale.value.code == "lease_lost"
            current = await data.step(
                "claim", ClaimRequest(job_id=job.id, worker_id=uuid.uuid4())
            )
            assert current["execution"].run_id == old_proof.run_id
            assert current["execution"].generation == old_proof.generation + 1
        async with engine.connect() as owner:
            assert (
                await owner.scalar(
                    text(
                        "SELECT plan_digest FROM aap_jobs_v1.video_execution_journals "
                        "WHERE job_id=:job"
                    ),
                    {"job": job.id},
                )
                == plan.plan_digest
            )
    finally:
        if connection is not None:
            await connection.close()
        if provider is not None:
            await provider.close()
        if engine is not None:
            await engine.dispose()
        if database in created:
            await _drop_owned_clone(admin, test_engine, database, marker)
        await admin.dispose()
