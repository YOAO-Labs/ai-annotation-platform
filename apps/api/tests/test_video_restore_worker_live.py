"""Resume a real video runner against a restored Jobs and Inference database."""

from __future__ import annotations

from hashlib import sha256
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_assets_video_inputs.tracker import (
    TrackerInputReadRequest,
    TrackerSourceReferenceRequest,
)
from aap_assets_video_inputs.tracker_client import TrackerVideoInputClient
from aap_inference.pipeline_server import PipelineHttpServer
from aap_inference.restore_contract import canonical_command_json
from aap_inference.restore_postgres import VideoRestorePostgresProvider
from aap_inference.routing_backend_control import BackendRoutingControl
from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.routing import make_routing_command
from app.services.ml_backend import MLBackendService
from tests.test_assets_media_live import _free_loopback_port, _stop_process
from tests.test_identity_recovery import _postgres_tool
from tests.test_inference_backend_dependency_reads_restore import (
    _database_acl,
    _drop_owned_clone,
)
from tests.test_tracker_input_live import _prepared, _seed_tracker
from tests.test_video_restore_native import (
    _command,
    _create_restored_database,
    _create_source_database,
    _json_value,
)
from tests.test_video_restore_live import _json_route_status
from tests.test_labeling_video_writer_restore import ROOT, _run_psql_file
from tests.test_legacy_image_runtime_live import _apply_lan_grants
from tests.test_video_window_recovery_live import (
    _bbox_artifact,
    _checkpoint_cpu_backend,
    _first_window_parts,
    _reserve_port,
    _scope_rows,
    _snapshot_claim,
    _start_runner_peer,
    _wait_backend_idle,
    _wait_entered,
    _wait_first_checkpoint,
    _worker_environment,
)
from tests.test_video_input_live import _video_bytes

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned PostgreSQL, roles, and versioned S3 buckets",
    ),
]

_ROUTING_ROLE = "inference_routing_client"
_RESTORE_ROLE = "video_restore_operator_client"
_WRITER_ROLE = "labeling_video_writer_client"
_LEGACY_ROLE = "legacy_runtime"


def _clone_role_dsn(environment: str, role: str, database: str) -> str:
    raw = os.environ.get(environment)
    if not raw:
        pytest.fail(f"the verifier did not provide {environment}")
    parsed = make_url(raw)
    if (
        parsed.username != role
        or not parsed.password
        or parsed.host != "127.0.0.1"
        or parsed.port is None
        or not parsed.database
        or not parsed.database.endswith("_test")
    ):
        pytest.fail(f"{environment} did not identify the verifier's loopback role")
    return parsed.set(database=database).render_as_string(hide_password=False)


def _retarget_media_roles(monkeypatch, database: str) -> None:
    for environment, role in (
        ("AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL", "asset_media_control_client"),
        ("AAP_MODULE_DATA_ASSET_MEDIA_WORKER_URL", "asset_media_worker_client"),
    ):
        monkeypatch.setenv(environment, _clone_role_dsn(environment, role, database))


async def _grant_worker_consumers(engine, monkeypatch):
    _run_psql_file(
        engine,
        engine.url.database,
        ROOT / "infra/docker/assets-media-grants.sql",
        {
            "expected_database": engine.url.database,
            "migration_role": "owner",
            "assets_owner": "assets_code",
            "asset_media_control_role": "asset_media_control_client",
            "asset_media_worker_role": "asset_media_worker_client",
        },
    )
    await _apply_lan_grants(engine, monkeypatch)


async def _operator_cli(tmp_path, dsn, *arguments):
    """Use the deployment entry point, optionally from a noneditable install."""
    executable = Path(
        os.environ.get(
            "AAP_VIDEO_RESTORE_CLI",
            str(Path(sys.executable).parent / "aap-video-restore"),
        )
    )
    assert executable.is_absolute() and executable.is_file()
    dsn_file = tmp_path / "restore-operator.dsn"
    dsn_file.write_text(dsn, encoding="utf-8")
    dsn_file.chmod(0o600)
    process = await asyncio.create_subprocess_exec(
        str(executable),
        "--database-url-file",
        str(dsn_file),
        *arguments,
        cwd=tmp_path,
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, "operator CLI refused the owned restore"
    assert not stderr
    assert len(stdout) <= 16384
    assert dsn.encode() not in stdout
    return json.loads(stdout)


async def _video_state(engine, job_id: uuid.UUID) -> dict:
    async with engine.connect() as connection:
        job = (
            (
                await connection.execute(
                    text(
                        "SELECT status,staged_result::text AS staged_result,error_message "
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
                        "SELECT run_id::text,plan_digest,plan_document,writer_terminal,"
                        "writer_terminal_outcome,execution_retired FROM "
                        "aap_jobs_v1.video_execution_journals WHERE job_id=:job"
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
                        "SELECT run_id::text,generation,state,route_request_id::text "
                        "FROM aap_jobs_v1.video_executions WHERE job_id=:job"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .one()
        )
        route_receipt = await connection.scalar(
            text(
                "SELECT to_jsonb(receipt)::text FROM "
                "aap_inference_routing_v1.command_receipts AS receipt "
                "WHERE id=CAST(:request AS uuid)"
            ),
            {"request": execution["route_request_id"]},
        )
        windows = (
            (
                await connection.execute(
                    text(
                        "SELECT window_ordinal,checkpoint_id,manifest_digest,manifest "
                        "FROM aap_jobs_v1.video_execution_windows WHERE job_id=:job "
                        "ORDER BY window_ordinal"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
        parts = (
            (
                await connection.execute(
                    text(
                        "SELECT window_ordinal,part_index,part_digest,part_bytes "
                        "FROM aap_jobs_v1.video_execution_window_parts WHERE job_id=:job "
                        "ORDER BY window_ordinal,part_index"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
        receipt = (
            await connection.execute(
                text(
                    "SELECT to_jsonb(receipt)::text FROM "
                    "aap_labeling_v1.video_writer_receipts AS receipt "
                    "WHERE job_id=:job"
                ),
                {"job": job_id},
            )
        ).scalar_one_or_none()
    return {
        "job": dict(job),
        "journal": dict(journal),
        "execution": dict(execution),
        "routing_receipt_digest": (
            sha256(route_receipt.encode("utf-8")).hexdigest()
            if route_receipt is not None
            else None
        ),
        "windows": [dict(row) for row in windows],
        "parts": [
            (
                row["window_ordinal"],
                row["part_index"],
                row["part_digest"],
                bytes(row["part_bytes"]),
            )
            for row in parts
        ],
        "receipt": json.loads(receipt) if isinstance(receipt, str) else receipt,
    }


async def _routing_receipt_digest(engine, request_id: str) -> str:
    async with engine.connect() as connection:
        receipt = await connection.scalar(
            text(
                "SELECT to_jsonb(receipt)::text FROM "
                "aap_inference_routing_v1.command_receipts AS receipt "
                "WHERE id=CAST(:request AS uuid)"
            ),
            {"request": request_id},
        )
    assert receipt is not None
    return sha256(receipt.encode("utf-8")).hexdigest()


async def _route_intent_rows(engine, job_id: uuid.UUID) -> list[dict]:
    async with engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT generation,scope_id::text,state FROM "
                        "aap_jobs_v1.video_execution_route_intents WHERE job_id=:job "
                        "ORDER BY generation"
                    ),
                    {"job": job_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _start_routing_owner(runtime: RoutingRuntime, token: str):
    port = _free_loopback_port()
    server = PipelineHttpServer(
        manager=None,
        data=None,
        control_token=None,
        host="127.0.0.1",
        port=port,
        routing_manager=runtime.manager,
        routing_control_token=token,
        scope_retirement_manager=runtime.scope_retirement_manager,
    )
    await server.start()
    return server, f"http://127.0.0.1:{port}"


async def _wait_peer(peer, *, timeout: float) -> int:
    async with asyncio.timeout(timeout):
        while peer.poll() is None:
            await asyncio.sleep(0.025)
    return int(peer.returncode)


def _worker_env_with_writer(
    *,
    database_url: str,
    media_url: str,
    media_token: str,
    routing_url: str,
    routing_token: str,
    writer_url: str,
) -> dict[str, str]:
    environment = _worker_environment(
        database_url=database_url,
        media_url=media_url,
        media_token=media_token,
        routing_url=routing_url,
        routing_token=routing_token,
    )
    environment.update(
        {
            "LABELING_VIDEO_WRITER_URL": "",
            "LABELING_VIDEO_WRITER_DATABASE_URL": writer_url,
            "LABELING_VIDEO_WRITER_DATABASE_URL_FILE": "",
            "LABELING_VIDEO_WRITER_CONTROL_TOKEN": "",
            "LABELING_VIDEO_WRITER_CONTROL_TOKEN_FILE": "",
        }
    )
    return environment


async def _target_source_reference(target_media, actor_id, task_id, job_id, ready):
    client = TrackerVideoInputClient(
        target_media.origin,
        target_media.token,
        client=target_media.http,
    )
    request = TrackerInputReadRequest(
        actor_id=actor_id,
        tracker_job_id=job_id,
        task_id=task_id,
    )
    observed = await client.input(request)
    assert observed.status == "ready"
    assert observed.input_id == ready.input_id
    assert observed.request_digest == ready.request_digest
    assert observed.plan_digest == ready.plan_digest
    reference = await client.reference(
        TrackerSourceReferenceRequest(
            **{
                **request.model_dump(),
                "input_id": observed.input_id,
                "request_digest": observed.request_digest,
                "plan_digest": observed.plan_digest,
            }
        )
    )
    return reference


async def test_video_restore_resumes_real_runner_from_exact_versioned_s3_prefix(
    test_engine, tmp_path, monkeypatch, request
):
    """Restart the same frozen runner through the restore operator and real owners."""
    request.node.user_properties.extend(
        [
            ("video_restore_worker", "real_runner_subprocess"),
            ("video_restore_source_bytes", "same_owned_s3_version_read_twice"),
            ("video_restore_object_backup", "not_tested"),
        ]
    )
    source_database = f"aap_video_restore_runner_source_{uuid.uuid4().hex[:8]}_test"
    restored_database = f"aap_video_restore_runner_target_{uuid.uuid4().hex[:8]}_test"
    source_marker = f"video-restore-runner-source:{uuid.uuid4()}"
    restored_marker = f"video-restore-runner-target:{uuid.uuid4()}"
    created: set[str] = set()
    source_engine = restored_engine = admin = None
    source_owner_runtime = target_owner_runtime = None
    source_owner_server = target_owner_server = None
    source_peer = target_peer = None
    dump = tmp_path / "video-restore-runner.dump"
    listener = None
    source_media_process = None
    target_media_process = None
    operator = None

    try:
        admin = create_async_engine(
            test_engine.url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
        )
        source_engine = await _create_source_database(
            test_engine,
            admin,
            source_database,
            source_marker,
            await _database_acl(test_engine),
            created,
        )
        await _grant_worker_consumers(source_engine, monkeypatch)

        # Validate the verifier's bucket and PostgreSQL ownership against its
        # original database, then permit the existing Media helper to bind
        # control/worker LOGINS to the separately marked source/target clones.
        from tests.test_asset_storage_cleanup_runtime import (
            _verified_resource_manifest,
        )
        from tests.test_video_input_live import _video_runtime
        import tests.test_video_input_live as video_input_live

        original_control_url = make_url(
            os.environ["AAP_MODULE_DATA_ASSET_MEDIA_CONTROL_URL"]
        )
        resource_manifest = _verified_resource_manifest(
            test_engine, original_control_url
        )
        allowed_databases = {source_database, restored_database}

        def verified_manifest_for_clone(clone_engine, control_url):
            if (
                clone_engine.url.database not in allowed_databases
                or control_url.database != clone_engine.url.database
                or control_url.host != original_control_url.host
                or control_url.port != original_control_url.port
            ):
                pytest.fail("Media runtime did not target an owned restore clone")
            return resource_manifest

        monkeypatch.setattr(
            video_input_live,
            "_verified_resource_manifest",
            verified_manifest_for_clone,
        )
        _retarget_media_roles(monkeypatch, source_database)

        async with _video_runtime(source_engine, tmp_path) as source_media:
            source_media_process = source_media.processes[0]
            video = await asyncio.to_thread(_video_bytes, tmp_path)
            artifact_bytes, artifact, artifact_digest = _bbox_artifact()
            assert artifact_bytes == json.dumps(
                artifact,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            listener, backend_url = _reserve_port()
            backend_token = f"video-restore-runner-{uuid.uuid4().hex}"
            (
                actor_id,
                project_id,
                _item_id,
                task_id,
                job_id,
                _source_key,
                _input_record,
            ) = await _seed_tracker(
                source_media,
                video,
                backend_url=backend_url,
                output_geometry="bbox",
            )
            async with source_media.sessions.begin() as db:
                pool_id = await db.scalar(
                    text("SELECT ml_backend_pool_id FROM public.projects WHERE id=:id"),
                    {"id": project_id},
                )
                registry_id = await db.scalar(
                    text(
                        "SELECT registry_id FROM public.ml_backend_pool_members "
                        "WHERE pool_id=:pool AND traffic_state='active'"
                    ),
                    {"pool": pool_id},
                )
                assert pool_id is not None and registry_id is not None
                await db.execute(
                    text(
                        "UPDATE public.ml_backend_registry SET auth_method='token', "
                        "auth_token=:token,state='connected',last_checked_at="
                        "pg_catalog.clock_timestamp(),"
                        "extra_params=CAST(:extra AS jsonb) "
                        "WHERE id=:registry"
                    ),
                    {
                        "token": backend_token,
                        "extra": json.dumps(
                            {"device": "cpu", "max_concurrency": 2},
                            separators=(",", ":"),
                        ),
                        "registry": registry_id,
                    },
                )

            async with _checkpoint_cpu_backend(
                listener,
                registry_id=registry_id,
                control_token=backend_token,
                storage_origin=source_media.endpoint,
                expected_source_sha256=sha256(video).hexdigest(),
                context_mode="none",
                artifact=artifact,
                weights_sha256=artifact_digest,
            ) as backend:
                listener = None
                async with source_media.sessions.begin() as health_db:
                    health_service = MLBackendService(health_db)
                    assert await health_service.check_health(registry_id)
                    selected = (
                        await health_service.get_tracker_backend_for_capabilities(
                            project_id, ["sam2_video"]
                        )
                    )
                    assert selected is not None and selected.id == registry_id

                source_input_client, input_request, ready = await _prepared(
                    source_media, tmp_path, actor_id, task_id, job_id
                )
                source_reference = await source_input_client.reference(
                    TrackerSourceReferenceRequest(
                        **input_request.model_dump(), plan_digest=ready.plan_digest
                    )
                )
                source_response = await source_media.http.get(source_reference.url)
                source_response.raise_for_status()
                assert source_response.content == video
                await source_response.aclose()
                source_version_id = source_reference.source_version.version_id
                assert (
                    source_reference.source_version.sha256 == sha256(video).hexdigest()
                )
                assert source_reference.source_version.byte_size == len(video)

                source_routing_dsn = _clone_role_dsn(
                    "AAP_MODULE_DATA_INFERENCE_ROUTING_URL",
                    _ROUTING_ROLE,
                    source_database,
                )
                source_legacy_dsn = _clone_role_dsn(
                    "AAP_MODULE_DATA_LEGACY_RUNTIME_URL",
                    _LEGACY_ROLE,
                    source_database,
                )
                source_writer_dsn = _clone_role_dsn(
                    "AAP_MODULE_DATA_LABELING_VIDEO_WRITER_URL",
                    _WRITER_ROLE,
                    source_database,
                )
                source_owner_runtime = RoutingRuntime(
                    source_routing_dsn,
                    backend_control=BackendRoutingControl(timeout_seconds=8),
                )
                assert await source_owner_runtime.readiness()
                source_routing_token = uuid.uuid4().hex
                source_owner_server, source_owner_url = await _start_routing_owner(
                    source_owner_runtime, source_routing_token
                )
                reconciled = await source_owner_runtime.execute(
                    make_routing_command(
                        "reconcile",
                        {
                            "registry_id": str(registry_id),
                            "control_id": str(uuid.uuid4()),
                        },
                    )
                )
                assert reconciled.state == "committed"
                assert reconciled.result.phase == "completed"
                assert backend.controller.phase == "bound"
                original_incarnation = str(backend.controller.backend_incarnation)

                source_environment = _worker_env_with_writer(
                    database_url=source_legacy_dsn,
                    media_url=source_media.origin,
                    media_token=source_media.token,
                    routing_url=source_owner_url,
                    routing_token=source_routing_token,
                    writer_url=source_writer_dsn,
                )
                source_peer = _start_runner_peer(job_id, source_environment)
                await _wait_entered(backend, 0, source_peer)
                assert backend.controller.active_requests == 1
                backend.release[0].set()
                await _wait_entered(backend, 1, source_peer)
                assert backend.controller.active_requests == 1
                first_window = await _wait_first_checkpoint(
                    source_media.sessions, job_id, source_peer
                )
                assert first_window["manifest"]["window_ordinal"] == 0
                first_parts = await _first_window_parts(source_media.sessions, job_id)
                assert first_parts
                source_state = await _video_state(source_engine, job_id)
                assert source_state["routing_receipt_digest"] is not None
                assert source_state["execution"]["route_request_id"] is not None

                source_scope_rows = await _scope_rows(source_media.sessions, job_id)
                assert len(source_scope_rows) == 1
                old_scope = source_scope_rows[0]
                assert old_scope["registry_id"] == registry_id
                async with source_media.sessions() as db:
                    frozen = (
                        (
                            await db.execute(
                                text(
                                    "SELECT plan_digest,plan_document FROM "
                                    "aap_jobs_v1.video_execution_journals WHERE job_id=:job"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    source_execution = (
                        (
                            await db.execute(
                                text(
                                    "SELECT run_id,generation FROM "
                                    "aap_jobs_v1.video_executions WHERE job_id=:job"
                                ),
                                {"job": job_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                    source_routing_state = (
                        (
                            await db.execute(
                                text(
                                    "SELECT authority_epoch::text,"
                                    "backend_incarnation::text "
                                    "FROM aap_inference_routing_v1.registry_state "
                                    "WHERE registry_id=:registry"
                                ),
                                {"registry": registry_id},
                            )
                        )
                        .mappings()
                        .one()
                    )
                frozen_plan = frozen["plan_document"]
                backend_identity = frozen_plan["backend_compatibility"][0]
                assert backend_identity["model_id"] == artifact["model_id"]
                assert (
                    backend_identity["weights_release_id"]
                    == artifact["weights_release_id"]
                )
                assert backend_identity["weights_sha256"] == artifact_digest
                assert backend_identity["backend_incarnation"] == original_incarnation
                assert (
                    str(old_scope["authority_epoch"])
                    == source_routing_state["authority_epoch"]
                )
                assert (
                    source_routing_state["backend_incarnation"] == original_incarnation
                )
                assert source_execution["generation"] == 1

                os.killpg(source_peer.pid, signal.SIGKILL)
                assert source_peer.wait(timeout=5) == -signal.SIGKILL
                source_peer = None
                assert backend.controller.active_requests == 1

                dump = tmp_path / "video-restore-real-runner.dump"
                dump.touch(mode=0o600, exist_ok=False)
                dump.write_bytes(
                    _postgres_tool(
                        test_engine,
                        ["pg_dump", "-U", "owner", "-Fc", source_database],
                    )
                )
                assert dump.read_bytes().startswith(b"PGDMP")
                await source_owner_server.close()
                source_owner_server = None
                await source_owner_runtime.close()
                source_owner_runtime = None
                await asyncio.to_thread(_stop_process, source_media_process)
                source_media_process = None

                restored_engine = await _create_restored_database(
                    test_engine,
                    admin,
                    source_engine,
                    restored_database,
                    restored_marker,
                    created,
                    dump,
                )
                await _grant_worker_consumers(restored_engine, monkeypatch)
                restored_before = await _video_state(restored_engine, job_id)
                assert (
                    restored_before["routing_receipt_digest"]
                    == source_state["routing_receipt_digest"]
                )
                assert restored_before["job"]["status"] == "running"
                assert (
                    restored_before["journal"]["plan_digest"] == frozen["plan_digest"]
                )
                assert restored_before["journal"]["plan_document"] == frozen_plan
                assert [
                    row["window_ordinal"] for row in restored_before["windows"]
                ] == [0]
                assert restored_before["parts"] == [
                    (0, index, digest, raw) for index, digest, raw in first_parts
                ]
                assert restored_before["execution"]["run_id"] == str(
                    source_execution["run_id"]
                )
                assert restored_before["execution"]["generation"] == 1
                assert (
                    restored_before["journal"]["plan_document"]["source"][
                        "source_version_id"
                    ]
                    == source_version_id
                )

                _retarget_media_roles(monkeypatch, restored_database)
                async with _video_runtime(restored_engine, tmp_path) as target_media:
                    target_media_process = target_media.processes[0]
                    target_reference = await _target_source_reference(
                        target_media, actor_id, task_id, job_id, ready
                    )
                    target_response = await target_media.http.get(target_reference.url)
                    target_response.raise_for_status()
                    assert target_response.content == video
                    await target_response.aclose()
                    assert (
                        target_reference.source_version.version_id == source_version_id
                    )
                    assert (
                        target_reference.source_version.sha256
                        == source_reference.source_version.sha256
                    )
                    assert target_reference.source_version.byte_size == len(video)

                    target_routing_dsn = _clone_role_dsn(
                        "AAP_MODULE_DATA_INFERENCE_ROUTING_URL",
                        _ROUTING_ROLE,
                        restored_database,
                    )
                    target_writer_dsn = _clone_role_dsn(
                        "AAP_MODULE_DATA_LABELING_VIDEO_WRITER_URL",
                        _WRITER_ROLE,
                        restored_database,
                    )
                    target_legacy_dsn = _clone_role_dsn(
                        "AAP_MODULE_DATA_LEGACY_RUNTIME_URL",
                        _LEGACY_ROLE,
                        restored_database,
                    )
                    restore_dsn = _clone_role_dsn(
                        "AAP_MODULE_DATA_VIDEO_RESTORE_URL",
                        _RESTORE_ROLE,
                        restored_database,
                    )
                    target_owner_runtime = RoutingRuntime(
                        target_routing_dsn,
                        backend_control=BackendRoutingControl(timeout_seconds=8),
                    )
                    assert not await target_owner_runtime.readiness()
                    operator = VideoRestorePostgresProvider(restore_dsn)
                    identity = await _operator_cli(tmp_path, restore_dsn, "--inspect")
                    assert (
                        identity["source_jobs_realm"].split(":", 2)[2]
                        == identity["target_jobs_realm"].split(":", 2)[2]
                    )
                    assert (
                        identity["source_jobs_realm"] != identity["target_jobs_realm"]
                    )
                    assert (
                        identity["source_routing_realm"]
                        != identity["target_routing_realm"]
                    )
                    command = _command(identity, dump)
                    command_file = tmp_path / "restore-command.json"
                    command_file.write_text(canonical_command_json(command))
                    pending = await _operator_cli(
                        tmp_path, restore_dsn, "--command-file", str(command_file)
                    )
                    assert pending["phase"] == "prepared"
                    assert backend.controller.active_requests == 1
                    assert backend.controller.phase == "draining"
                    assert (
                        str(backend.controller.backend_incarnation)
                        == original_incarnation
                    )

                    current = _json_value(
                        await operator.step(
                            "status", {"restore_id": command.restore_id}
                        )
                    )
                    assert current["restore"]["phase"] == "prepared"
                    assert len(current["contexts"]) == 1
                    restore_context = current["contexts"][0]
                    assert restore_context["registry_id"] == str(registry_id)
                    assert restore_context["phase"] == "prepared"
                    assert (
                        restore_context["backend_incarnation"] == original_incarnation
                    )
                    assert (
                        restore_context["retired_epoch"]
                        == source_routing_state["authority_epoch"]
                    )

                    backend.release[1].set()
                    await _wait_backend_idle(backend)
                    completed = await _operator_cli(
                        tmp_path, restore_dsn, "--command-file", str(command_file)
                    )
                    assert completed["phase"] == "completed"
                    assert await target_owner_runtime.readiness()
                    assert backend.controller.phase == "bound"
                    assert backend.controller.active_requests == 0
                    assert (
                        str(backend.controller.backend_incarnation)
                        == original_incarnation
                    )
                    assert (await _json_route_status(backend_url, backend_token))[
                        "authority_epoch"
                    ] != source_routing_state["authority_epoch"]
                    assert (
                        await _operator_cli(
                            tmp_path, restore_dsn, "--status", command.restore_id
                        )
                        == completed
                    )
                    assert (
                        await _operator_cli(
                            tmp_path, restore_dsn, "--command-file", str(command_file)
                        )
                        == completed
                    )

                    async with target_media.sessions.begin() as health_db:
                        health_service = MLBackendService(health_db)
                        assert await health_service.check_health(registry_id)
                        selected = (
                            await health_service.get_tracker_backend_for_capabilities(
                                project_id, ["sam2_video"]
                            )
                        )
                        assert selected is not None and selected.id == registry_id

                    target_routing_token = uuid.uuid4().hex
                    target_owner_server, target_owner_url = await _start_routing_owner(
                        target_owner_runtime, target_routing_token
                    )
                    target_environment = _worker_env_with_writer(
                        database_url=target_legacy_dsn,
                        media_url=target_media.origin,
                        media_token=target_media.token,
                        routing_url=target_owner_url,
                        routing_token=target_routing_token,
                        writer_url=target_writer_dsn,
                    )
                    target_peer = _start_runner_peer(job_id, target_environment)
                    assert await _wait_peer(target_peer, timeout=90) == 0
                    target_peer = None
                    assert backend.controller.active_requests == 0

                    final_state = await _video_state(restored_engine, job_id)
                    assert final_state["job"]["status"] == "pending_review"
                    assert (
                        final_state["journal"]["plan_digest"] == frozen["plan_digest"]
                    )
                    assert final_state["journal"]["plan_document"] == frozen_plan
                    assert final_state["journal"]["writer_terminal"] is True
                    assert (
                        final_state["journal"]["writer_terminal_outcome"] == "completed"
                    )
                    assert final_state["journal"]["execution_retired"] is True
                    assert [
                        row["window_ordinal"] for row in final_state["windows"]
                    ] == [0, 1, 2]
                    assert (
                        final_state["windows"][0]["manifest"]
                        == first_window["manifest"]
                    )
                    assert [part for part in final_state["parts"] if part[0] == 0] == [
                        (0, index, digest, raw) for index, digest, raw in first_parts
                    ]
                    staged = json.loads(final_state["job"]["staged_result"])
                    assert [row["frame_index"] for row in staged["results"]] == list(
                        range(6)
                    )
                    assert all(
                        row["geometry"] == {"type": "bbox", **artifact["bbox"]}
                        and row["confidence"] == 1.0
                        for row in staged["results"]
                    )
                    assert final_state["receipt"] is not None
                    assert final_state["receipt"]["outcome"] == "completed"
                    assert final_state["receipt"]["status"] == "pending_review"
                    assert (
                        await _routing_receipt_digest(
                            restored_engine,
                            source_state["execution"]["route_request_id"],
                        )
                        == source_state["routing_receipt_digest"]
                    )
                    assert len(backend.model_windows) == 4
                    assert [
                        (row["from_frame"], row["to_frame"])
                        for row in backend.model_windows
                    ] == [(0, 1), (2, 3), (2, 3), (4, 5)]
                    assert all(
                        row["job_id"] == str(job_id)
                        and row["source_sha256"]
                        == source_reference.source_version.sha256
                        and row["source_version_id"] == source_version_id
                        and row["source_size"] == len(video)
                        and row["admission_header_present"]
                        and row["authorization_bound"]
                        for row in backend.model_windows
                    )
                    target_claim = await _snapshot_claim(
                        async_sessionmaker(restored_engine, expire_on_commit=False),
                        job_id,
                    )
                    assert target_claim["generation"] == 2
                    assert [
                        row["generation"]
                        for row in await _route_intent_rows(restored_engine, job_id)
                    ] == [1, 2]
                    await target_owner_server.close()
                    target_owner_server = None
                    await target_owner_runtime.close()
                    target_owner_runtime = None
                    await asyncio.to_thread(_stop_process, target_media_process)
                    target_media_process = None
    finally:
        if source_peer is not None:
            try:
                os.killpg(source_peer.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.gather(
                asyncio.to_thread(source_peer.wait, timeout=5),
                return_exceptions=True,
            )
        if target_peer is not None:
            try:
                os.killpg(target_peer.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.gather(
                asyncio.to_thread(target_peer.wait, timeout=5),
                return_exceptions=True,
            )
        if source_owner_server is not None:
            await source_owner_server.close()
        if target_owner_server is not None:
            await target_owner_server.close()
        if source_owner_runtime is not None:
            await source_owner_runtime.close()
        if target_owner_runtime is not None:
            await target_owner_runtime.close()
        if source_media_process is not None:
            await asyncio.to_thread(_stop_process, source_media_process)
        if target_media_process is not None:
            await asyncio.to_thread(_stop_process, target_media_process)
        if operator is not None:
            await operator.close()
        if listener is not None:
            listener.close()
        if restored_engine is not None:
            await restored_engine.dispose()
        if source_engine is not None:
            await source_engine.dispose()
        if admin is not None:
            if restored_database in created:
                await _drop_owned_clone(
                    admin, test_engine, restored_database, restored_marker
                )
            if source_database in created:
                await _drop_owned_clone(
                    admin, test_engine, source_database, source_marker
                )
            await admin.dispose()
