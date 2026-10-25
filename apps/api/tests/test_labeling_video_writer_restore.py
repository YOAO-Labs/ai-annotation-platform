"""Owned restore of frozen video windows and an immutable Labeling publication.

This PostgreSQL-only case retains the exact accepted source version recorded in
the database. It does not assert that object storage bytes were backed up. The
seeded producer uses the existing mock-bbox Native window path, so this case has
no Mask objects or roots.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_labeling.video_writer import VideoWriterManager
from aap_labeling.video_writer_contract import (
    VideoWriterFailure,
    make_video_writer_command,
    video_writer_command_digest,
)
from aap_labeling.video_writer_postgres import VideoWriterPostgresProvider
from aap_owner_data.postgres import verify_function_only_role
from aap_preannotation_results.video_execution_journal import (
    CaptureWindowRequest,
    PutPartRequest,
    SealWindowRequest,
)
from aap_preannotation_results.video_execution_plan import (
    VideoContinuationPostState,
)
from aap_preannotation_results.video_window_checkpoint import (
    make_video_window_checkpoint_payload,
    snapshot_video_window_checkpoint,
)
from tests.test_identity_recovery import _alembic, _postgres_tool
from tests.test_inference_backend_dependency_reads_restore import (
    _assert_clone_identity,
    _create_owned_clone,
    _database_acl,
    _database_head,
    _drop_owned_clone,
)
from tests.test_module_data_roles import _role_url
from tests.test_video_window_journal_native import _begin, _call, _prepared_execution

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verifier-owned disposable PostgreSQL resources",
    ),
]

ROOT = Path(__file__).resolve().parents[3]
_WRITER_ROLE = "labeling_video_writer_client"
_WRITER_FUNCTIONS = {("video_writer_step", "text, text")}


def _role_dsn(database: str) -> str:
    parsed = make_url(
        _role_url("AAP_MODULE_DATA_LABELING_VIDEO_WRITER_URL", _WRITER_ROLE)
    )
    return parsed.set(drivername="postgresql", database=database).render_as_string(
        hide_password=False
    )


def _grant_variables(database: str) -> dict[str, str]:
    return {
        "expected_database": database,
        "migration_role": "owner",
        "identity_owner": "identity_code",
        "workspace_owner": "workspace_code",
        "assets_owner": "assets_code",
        "labeling_owner": "labeling_code",
        "jobs_owner": "jobs_code",
        "learning_owner": "learning_code",
        "inference_owner": "inference_code",
        "insights_owner": "insights_code",
        "diagnostics_owner": "diagnostics_code",
        "export_owner": "export_code",
        "audit_owner": "audit_code",
        "settings_owner": "settings_code",
        "notifications_owner": "notifications_code",
        "workflow_role": "append_client",
        "asset_cleanup_role": "asset_cleanup_client",
        "mask_gc_role": "labeling_mask_gc_client",
        "inference_registry_ingress_role": "inference_registry_ingress_client",
        "inference_routing_role": "inference_routing_client",
    }


def _run_psql_file(test_engine, database: str, path: Path, variables: dict[str, str]):
    command = [
        "psql",
        "-X",
        "-q",
        "-A",
        "-t",
        "-U",
        "owner",
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
    ]
    for name, value in variables.items():
        command.extend(("-v", f"{name}={value}"))
    command.extend(("-f", "-"))
    _postgres_tool(test_engine, command, body=path.read_bytes())


def _apply_current_owner_grants(test_engine, database: str) -> None:
    _run_psql_file(
        test_engine,
        database,
        ROOT / "infra/docker/module-data-grants.sql",
        _grant_variables(database),
    )
    _run_psql_file(
        test_engine,
        database,
        ROOT / "infra/docker/labeling-video-writer-grants.sql",
        {
            "expected_database": database,
            "migration_role": "owner",
            "labeling_owner": "labeling_code",
            "labeling_video_writer_role": _WRITER_ROLE,
        },
    )


async def _revoke_public_temp(admin, test_engine, database: str) -> None:
    quoted = test_engine.dialect.identifier_preparer.quote_identifier(database)
    async with admin.connect() as connection:
        await connection.execute(
            text(f"REVOKE TEMPORARY ON DATABASE {quoted} FROM PUBLIC")
        )


async def _seal_window_with_lexemes(db, proof, plan, ordinal, previous=None):
    attempt = (await _call(db, "begin_window", _begin(proof, plan, ordinal, previous)))[
        "attempt"
    ]
    window = plan.windows[ordinal]
    geometry = {"type": "bbox", "x": -0.0, "y": 1e-5, "w": 0.3, "h": 0.4}
    payload = make_video_window_checkpoint_payload(
        plan=plan,
        window_ordinal=ordinal,
        raw_response_digest=hashlib.sha256(f"restore-{ordinal}".encode()).hexdigest(),
        frame_results=[
            {
                "frame_index": frame,
                "geometry": geometry,
                "confidence": 1e-7,
                "outside": False,
                "instance_id": None,
                "primary": False,
            }
            for frame in range(window.from_frame, window.to_frame + 1)
        ],
        continuation=VideoContinuationPostState(
            last_geometry=geometry,
            last_geom_by_instance={},
            mp_prev_boundary={},
            mp_next_global=1,
        ),
        predecessor_checkpoint=previous,
    )
    with snapshot_video_window_checkpoint(
        plan, payload, predecessor_checkpoint=previous
    ) as snapshot:
        capture = await _call(
            db,
            "capture_window",
            CaptureWindowRequest(
                **proof.model_dump(),
                plan_digest=plan.plan_digest,
                window_ordinal=ordinal,
                capture_id=attempt.capture_id,
                manifest=snapshot.manifest,
            ),
        )
        assert capture["capture"].manifest == snapshot.manifest
        for part in snapshot.iter_parts():
            await _call(
                db,
                "put_part",
                PutPartRequest(
                    **proof.model_dump(),
                    capture_id=attempt.capture_id,
                    index=part.part_index,
                    sha256=part.part_digest,
                    data_base64=part.data_base64,
                ),
            )
        sealed = await _call(
            db,
            "seal_window",
            SealWindowRequest(**proof.model_dump(), capture_id=attempt.capture_id),
        )
        assert sealed["window"].manifest == snapshot.manifest
        return snapshot.manifest


async def _seed_and_publish(engine, writer_dsn: str) -> dict:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as db:
        job, proof, plan = await _prepared_execution(db)
        previous = None
        for ordinal in range(len(plan.windows)):
            previous = await _seal_window_with_lexemes(
                db, proof, plan, ordinal, previous
            )

    command = make_video_writer_command(
        proof,
        outcome="completed",
        expected_input={
            "input_id": str(plan.input_id),
            "request_digest": plan.request_digest,
            "plan_digest": plan.source_plan_digest,
            "source_version_id": plan.source.source_version_id,
            "source_sha256": plan.source.source_sha256,
        },
        frame_results=(),
    )
    manager = VideoWriterManager(VideoWriterPostgresProvider(writer_dsn))
    receipt = await manager.execute(command)
    assert receipt.outcome == "completed"
    assert receipt.status == "pending_review"
    assert (
        await manager.status(
            command.operation_id,
            command.execution,
            video_writer_command_digest(command),
        )
        == receipt
    )
    assert await manager.publication(command.execution) == receipt
    return {
        "job_id": job.id,
        "proof": proof,
        "operation_id": command.operation_id,
        "request_digest": video_writer_command_digest(command),
    }


async def _snapshot(engine, job_id: uuid.UUID) -> dict:
    async with engine.connect() as connection:
        job = (
            (
                await connection.execute(
                    text(
                        "SELECT status,revision,completed_at,"
                        "staged_result::text AS staged_result,error_message "
                        "FROM public.video_tracker_jobs WHERE id=:job"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .one()
        )
        journal = (
            (
                await connection.execute(
                    text(
                        "SELECT run_id::text,plan_digest,"
                        "plan_document::text AS plan_document,"
                        "writer_terminal,writer_terminal_outcome,execution_retired "
                        "FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .one()
        )
        execution = (
            (
                await connection.execute(
                    text(
                        "SELECT run_id::text,worker_id::text,generation,"
                        "pg_catalog.encode(public.digest(lease_token_hash,'sha256'),"
                        "'hex') AS lease_token_fingerprint,"
                        "lease_expires_at,state,realm,route_request_id::text "
                        "FROM aap_jobs_v1.video_executions WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .one()
        )
        windows = (
            (
                await connection.execute(
                    text(
                        "SELECT window_ordinal,checkpoint_id,manifest_digest,"
                        "manifest::text AS manifest "
                        "FROM aap_jobs_v1.video_execution_windows "
                        "WHERE job_id=:job ORDER BY window_ordinal"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
        window_parts = (
            (
                await connection.execute(
                    text(
                        "SELECT window_ordinal,part_index,part_digest,part_bytes "
                        "FROM aap_jobs_v1.video_execution_window_parts "
                        "WHERE job_id=:job ORDER BY window_ordinal,part_index"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
        attempts = (
            (
                await connection.execute(
                    text(
                        "SELECT generation,window_ordinal,window_digest,mode,state,"
                        "capture_id::text,seed_input_digest,predecessor_checkpoint_digest,"
                        "raw_response_digest "
                        "FROM aap_jobs_v1.video_execution_window_attempts "
                        "WHERE job_id=:job ORDER BY generation,window_ordinal"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
        captures = (
            (
                await connection.execute(
                    text(
                        "SELECT capture_id::text,generation,window_ordinal,manifest_digest,"
                        "manifest::text AS manifest,state "
                        "FROM aap_jobs_v1.video_execution_captures "
                        "WHERE job_id=:job ORDER BY window_ordinal"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
        capture_parts = (
            (
                await connection.execute(
                    text(
                        "SELECT capture.capture_id::text,part.part_index,part.part_digest,"
                        "part.part_bytes "
                        "FROM aap_jobs_v1.video_execution_capture_parts AS part "
                        "JOIN aap_jobs_v1.video_execution_captures AS capture "
                        "ON capture.capture_id=part.capture_id WHERE capture.job_id=:job "
                        "ORDER BY capture.window_ordinal,part.part_index"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
        spans = (
            (
                await connection.execute(
                    text(
                        "SELECT plan_digest,context_span_digest,"
                        "closed_generation,closed_at "
                        "FROM aap_jobs_v1.video_execution_span_closures WHERE job_id=:job "
                        "ORDER BY context_span_digest"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
        accepted = (
            await connection.execute(
                text(
                    "SELECT (to_jsonb(input) - ARRAY['lease_token','last_lease_token',"
                    "'lease_owner'])::text "
                    "FROM public.asset_accepted_video_inputs AS input "
                    "WHERE input.job_id=:job"
                ),
                {"job": job_id},
            )
        ).scalar_one()
        tracker_scope = (
            await connection.execute(
                text(
                    "SELECT to_jsonb(scope)::text "
                    "FROM public.video_tracker_input_scopes AS scope "
                    "WHERE scope.tracker_job_id=:job"
                ),
                {"job": job_id},
            )
        ).scalar_one()
        input_attempts = (
            (
                await connection.execute(
                    text(
                        "SELECT (to_jsonb(attempt) - 'lease_token')::text "
                        "FROM public.asset_accepted_video_input_attempts AS attempt "
                        "WHERE attempt.input_id=(SELECT input_id "
                        "FROM public.video_tracker_input_scopes "
                        "WHERE tracker_job_id=:job) ORDER BY attempt.generation"
                    ),
                    {"job": job_id},
                )
            )
            .scalars()
            .all()
        )
        frame_schedule = (
            (
                await connection.execute(
                    text(
                        "SELECT (to_jsonb(entry) - 'lease_token')::text "
                        "FROM public.asset_accepted_video_times AS entry "
                        "WHERE entry.input_id=(SELECT input_id "
                        "FROM public.video_tracker_input_scopes "
                        "WHERE tracker_job_id=:job) "
                        "ORDER BY entry.generation,entry.ordinal"
                    ),
                    {"job": job_id},
                )
            )
            .scalars()
            .all()
        )
        materialized_frames = (
            (
                await connection.execute(
                    text(
                        "SELECT (to_jsonb(frame) - ARRAY['lease_token','last_lease_token',"
                        "'lease_owner'])::text "
                        "FROM public.asset_accepted_video_frames AS frame "
                        "WHERE frame.input_id=(SELECT input_id "
                        "FROM public.video_tracker_input_scopes "
                        "WHERE tracker_job_id=:job) "
                        "ORDER BY frame.plan_digest,frame.ordinal"
                    ),
                    {"job": job_id},
                )
            )
            .scalars()
            .all()
        )
        receipts = (
            (
                await connection.execute(
                    text(
                        "SELECT to_jsonb(receipt)::text FROM "
                        "aap_labeling_v1.video_writer_receipts AS receipt "
                        "WHERE receipt.job_id=:job"
                    ),
                    {"job": job_id},
                )
            )
            .scalars()
            .all()
        )
        roots = (
            (
                await connection.execute(
                    text(
                        "SELECT root_kind,root_id,object_key,valid_until "
                        "FROM public.mask_gc_roots WHERE root_id=:job "
                        "ORDER BY root_kind,object_key"
                    ),
                    {"job": str(job_id)},
                )
            )
            .mappings()
            .all()
        )
        functions = (
            (
                await connection.execute(
                    text(
                        "SELECT namespace.nspname AS schema,proc.proname AS name,"
                        "pg_catalog.pg_get_function_identity_arguments(proc.oid) AS args,"
                        "pg_catalog.pg_get_userbyid(proc.proowner) AS owner,"
                        "proc.proacl::text AS acl,proc.prosecdef AS security_definer,"
                        "proc.proconfig::text AS settings,"
                        "pg_catalog.pg_get_functiondef(proc.oid) AS definition "
                        "FROM pg_catalog.pg_proc AS proc "
                        "JOIN pg_catalog.pg_namespace AS namespace "
                        "ON namespace.oid=proc.pronamespace "
                        "WHERE (namespace.nspname='aap_labeling_v1' AND "
                        "(proc.proname='video_writer_step' OR "
                        "pg_catalog.left(proc.proname,14)='_video_writer_')) "
                        "OR (namespace.nspname='aap_jobs_v1' AND "
                        "proc.proname='video_writer_authority') "
                        "ORDER BY namespace.nspname,proc.proname,"
                        "pg_catalog.pg_get_function_identity_arguments(proc.oid)"
                    )
                )
            )
            .mappings()
            .all()
        )
        relations = (
            (
                await connection.execute(
                    text(
                        "SELECT namespace.nspname AS schema,relation.relname AS name,"
                        "pg_catalog.pg_get_userbyid(relation.relowner) AS owner,"
                        "relation.relacl::text AS acl FROM pg_catalog.pg_class AS relation "
                        "JOIN pg_catalog.pg_namespace AS namespace "
                        "ON namespace.oid=relation.relnamespace WHERE "
                        "(namespace.nspname='aap_labeling_v1' AND "
                        "relation.relname='video_writer_receipts') OR "
                        "(namespace.nspname='aap_jobs_v1' AND relation.relname IN ("
                        "'video_executions','video_execution_journals',"
                        "'video_execution_windows','video_execution_window_parts',"
                        "'video_execution_window_attempts','video_execution_captures',"
                        "'video_execution_capture_parts',"
                        "'video_execution_span_closures')) OR "
                        "(namespace.nspname='public' AND relation.relname IN ("
                        "'video_tracker_jobs','video_tracker_input_scopes',"
                        "'asset_accepted_video_inputs',"
                        "'asset_accepted_video_input_attempts',"
                        "'asset_accepted_video_times','asset_accepted_video_frames',"
                        "'mask_gc_roots')) "
                        "ORDER BY namespace.nspname,relation.relname"
                    )
                )
            )
            .mappings()
            .all()
        )
        schemas = (
            (
                await connection.execute(
                    text(
                        "SELECT namespace.nspname AS name,"
                        "pg_catalog.pg_get_userbyid(namespace.nspowner) AS owner,"
                        "namespace.nspacl::text AS acl "
                        "FROM pg_catalog.pg_namespace AS namespace "
                        "WHERE namespace.nspname IN ('aap_labeling_v1','aap_jobs_v1') "
                        "ORDER BY namespace.nspname"
                    )
                )
            )
            .mappings()
            .all()
        )
        authority = (
            (
                await connection.execute(
                    text(
                        "SELECT authority.database_oid::text,authority.database_name,"
                        "authority.cluster_identifier::text,"
                        "database.oid::text AS local_oid,"
                        "database.datname AS local_name "
                        "FROM aap_jobs_v1.video_execution_authority AS authority "
                        "CROSS JOIN pg_catalog.pg_database AS database "
                        "WHERE authority.singleton AND database.datname=current_database()"
                    )
                )
            )
            .mappings()
            .one()
        )

    return {
        "job": dict(job),
        "journal": dict(journal),
        "execution": dict(execution),
        "windows": [dict(row) for row in windows],
        "window_parts": [
            (
                row["window_ordinal"],
                row["part_index"],
                row["part_digest"],
                bytes(row["part_bytes"]),
            )
            for row in window_parts
        ],
        "attempts": [dict(row) for row in attempts],
        "captures": [dict(row) for row in captures],
        "capture_parts": [
            (
                row["capture_id"],
                row["part_index"],
                row["part_digest"],
                bytes(row["part_bytes"]),
            )
            for row in capture_parts
        ],
        "spans": [dict(row) for row in spans],
        "accepted_input": accepted,
        "tracker_scope": tracker_scope,
        "input_attempts": list(input_attempts),
        "frame_schedule": list(frame_schedule),
        "materialized_frames": list(materialized_frames),
        "receipts": list(receipts),
        "roots": [dict(row) for row in roots],
        "functions": [dict(row) for row in functions],
        "relations": [dict(row) for row in relations],
        "schemas": [dict(row) for row in schemas],
        "authority": dict(authority),
    }


def _assert_restore_preserved(source: dict, restored: dict) -> None:
    assert {key: value for key, value in source.items() if key != "authority"} == {
        key: value for key, value in restored.items() if key != "authority"
    }
    for key in ("database_oid", "database_name", "cluster_identifier"):
        assert restored["authority"][key] == source["authority"][key]
    assert restored["authority"]["local_oid"] != source["authority"]["local_oid"]
    assert restored["authority"]["local_name"] != source["authority"]["local_name"]


def _assert_restorable_snapshot(snapshot: dict, *, source_realm: bool = True) -> None:
    assert snapshot["job"]["status"] == "pending_review"
    assert snapshot["job"]["staged_result"]
    assert snapshot["journal"]["plan_digest"]
    assert snapshot["journal"]["plan_document"]
    assert len(snapshot["windows"]) == 3
    assert len(snapshot["window_parts"]) >= 3
    assert len(snapshot["receipts"]) == 1
    assert snapshot["roots"] == []
    if source_realm:
        assert (
            snapshot["authority"]["database_oid"] == snapshot["authority"]["local_oid"]
        )
        assert (
            snapshot["authority"]["database_name"]
            == snapshot["authority"]["local_name"]
        )
    else:
        assert (
            snapshot["authority"]["database_oid"] != snapshot["authority"]["local_oid"]
        )
        assert (
            snapshot["authority"]["database_name"]
            != snapshot["authority"]["local_name"]
        )
    raw = b"".join(part[3] for part in snapshot["window_parts"])
    assert all(
        hashlib.sha256(part[3]).hexdigest() == part[2]
        for part in snapshot["window_parts"]
    )
    assert all(
        hashlib.sha256(part[3]).hexdigest() == part[2]
        for part in snapshot["capture_parts"]
    )
    assert b"-0.0" in raw
    assert b"1e-07" in raw or b"1e-7" in raw
    receipt = json.loads(snapshot["receipts"][0])
    assert receipt["outcome"] == "completed"
    assert receipt["status"] == "pending_review"
    assert receipt["staged_sha256"]
    assert receipt["staged_byte_size"] > 0


async def _assert_writer_role_profile(dsn: str) -> None:
    connection = await asyncpg.connect(dsn, timeout=5, command_timeout=15)
    try:
        user, database = await connection.fetchrow(
            "SELECT current_user,current_database()"
        )
        assert user == _WRITER_ROLE
        assert database == make_url(dsn).database
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions=_WRITER_FUNCTIONS,
            role=_WRITER_ROLE,
        )
    finally:
        await connection.close()


async def test_nonempty_video_writer_state_survives_owned_pg_restore(
    test_engine, tmp_path
):
    source_database = f"aap_video_writer_source_{uuid.uuid4().hex[:8]}_test"
    restored_database = f"aap_video_writer_restore_{uuid.uuid4().hex[:8]}_test"
    source_marker = f"video-writer-source:{uuid.uuid4()}"
    restored_marker = f"video-writer-restore:{uuid.uuid4()}"
    created: set[str] = set()
    source_engine = restored_engine = None
    dump = tmp_path / "labeling-video-writer.dump"
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    world = None
    source_before = restored_before = None
    try:
        acl = await _database_acl(test_engine)
        await _create_owned_clone(
            admin,
            test_engine,
            source_database,
            source_marker,
            acl,
            created,
        )
        await _revoke_public_temp(admin, test_engine, source_database)
        source_engine = create_async_engine(
            test_engine.url.set(database=source_database), hide_parameters=True
        )
        migration = _alembic(source_engine.url, "upgrade", "head")
        assert migration.returncode == 0, "owned source database migration failed"
        assert await _database_head(source_engine) == "0283"
        _apply_current_owner_grants(test_engine, source_database)
        await _assert_clone_identity(source_engine, source_database, source_marker)

        writer_dsn = _role_dsn(source_database)
        await _assert_writer_role_profile(writer_dsn)
        world = await _seed_and_publish(source_engine, writer_dsn)
        source_before = await _snapshot(source_engine, world["job_id"])
        _assert_restorable_snapshot(source_before)

        downgrade = _alembic(source_engine.url, "downgrade", "0282")
        assert downgrade.returncode != 0
        assert (
            b"cannot remove immutable video writer receipts"
            in (downgrade.stdout + downgrade.stderr).lower()
        )
        assert await _database_head(source_engine) == "0283"
        assert await _snapshot(source_engine, world["job_id"]) == source_before

        dump.touch(mode=0o600, exist_ok=False)
        dump.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", source_database],
            )
        )
        assert dump.read_bytes().startswith(b"PGDMP")
        assert stat.S_IMODE(dump.stat().st_mode) == 0o600

        await _create_owned_clone(
            admin,
            test_engine,
            restored_database,
            restored_marker,
            await _database_acl(source_engine),
            created,
        )
        await _revoke_public_temp(admin, test_engine, restored_database)
        _postgres_tool(
            test_engine,
            [
                "pg_restore",
                "-U",
                "owner",
                "--exit-on-error",
                "--dbname",
                restored_database,
            ],
            body=dump.read_bytes(),
        )
        restored_engine = create_async_engine(
            test_engine.url.set(database=restored_database), hide_parameters=True
        )
        await _assert_clone_identity(
            restored_engine, restored_database, restored_marker
        )
        _apply_current_owner_grants(test_engine, restored_database)
        assert await _database_head(restored_engine) == "0283"
        await _assert_writer_role_profile(_role_dsn(restored_database))

        restored_before = await _snapshot(restored_engine, world["job_id"])
        _assert_restorable_snapshot(restored_before, source_realm=False)
        _assert_restore_preserved(source_before, restored_before)
        assert (
            restored_before["authority"]["database_oid"]
            != restored_before["authority"]["local_oid"]
        )

        restored_manager = VideoWriterManager(
            VideoWriterPostgresProvider(_role_dsn(restored_database))
        )
        restored_policy = await VideoWriterPostgresProvider(
            _role_dsn(restored_database)
        ).policy()
        assert restored_policy.functions == (
            "aap_labeling_v1.video_writer_step(text,text)",
        )
        with pytest.raises(VideoWriterFailure) as status_error:
            await restored_manager.status(
                world["operation_id"],
                world["proof"],
                world["request_digest"],
            )
        assert status_error.value.code == "realm_mismatch"
        with pytest.raises(VideoWriterFailure) as publication_error:
            await restored_manager.publication(world["proof"])
        assert publication_error.value.code == "realm_mismatch"

        restored_after = await _snapshot(restored_engine, world["job_id"])
        source_after = await _snapshot(source_engine, world["job_id"])
        assert restored_after == restored_before
        assert source_after == source_before
    finally:
        if restored_engine is not None:
            await restored_engine.dispose()
        if source_engine is not None:
            await source_engine.dispose()
        if "restored_database" in created:
            await _drop_owned_clone(
                admin, test_engine, restored_database, restored_marker
            )
        if "source_database" in created:
            await _drop_owned_clone(admin, test_engine, source_database, source_marker)
        await admin.dispose()
