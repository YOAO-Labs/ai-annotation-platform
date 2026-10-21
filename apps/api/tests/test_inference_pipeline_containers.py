"""Exercise the installed Inference owner image against owned local resources."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import statistics
import time
import uuid
import warnings
from contextlib import asynccontextmanager, contextmanager

import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference.pipeline_client import PipelineHttpClient
from aap_inference.pipeline_contract import (
    PipelineQuerySuccess,
    PipelineStatusSuccess,
    pipeline_command_request,
    pipeline_query_request,
    pipeline_status_request,
)
from app.config import settings
from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.user import User
from tests.factory import create_user
from tests.inference_pipeline_owner_helpers import pipeline_dsn
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
)
from tests.test_named_pipeline_legacy import (
    test_replay_legacy_named_pipeline_http_and_database as replay_legacy_samples,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_INFERENCE_PIPELINE_CONTAINER_LIVE") != "1",
        reason="requires an explicitly qualified owned Inference pipeline image",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_IMAGE_ACCEPTANCE = "inference-pipeline-owner"
_RUN_ACCEPTANCE = "module-data-boundary"
_PACKAGE_SOURCE_PATHS = {
    "aap_inference": "apps/inference/src/aap_inference",
    "aap_owner_data": "apps/_shared/owner_data/src/aap_owner_data",
    "aap_workspace_contract": (
        "apps/_shared/workspace_contract/src/aap_workspace_contract"
    ),
    "aap_task_contract": "apps/_shared/task_contract/src/aap_task_contract",
    "aap_protocol_v2": "apps/_shared/protocol_v2/src/aap_protocol_v2",
}


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"the owned Inference verifier did not provide {name}"
    return value


def _package_file_hashes(root: Path) -> dict[str, str]:
    assert root.is_dir(), f"missing source package {root.name}"
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json"}
    }


def _source_package_hashes() -> dict[str, dict[str, str]]:
    return {
        package: _package_file_hashes(_ROOT / relative)
        for package, relative in _PACKAGE_SOURCE_PATHS.items()
    }


def _container_package_inventory(container: str) -> dict:
    script = r"""
import hashlib
import importlib.util
import json
from pathlib import Path

packages = {}
for name in (
    "aap_inference",
    "aap_owner_data",
    "aap_workspace_contract",
    "aap_task_contract",
    "aap_protocol_v2",
):
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("installed owner package is missing")
    root = Path(next(iter(spec.submodule_search_locations))).resolve()
    if not str(root).startswith("/opt/venv/"):
        raise RuntimeError("owner package is not installed in the image environment")
    packages[name] = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json"}
    }
forbidden = {
    name: importlib.util.find_spec(name) is not None
    for name in ("app", "aap_api", "aap_workspace", "celery", "sqlalchemy")
}
print(json.dumps({"packages": packages, "forbidden_runtime_modules": forbidden,
                  "fastapi_transitive_dependency_retained": importlib.util.find_spec("fastapi") is not None}, sort_keys=True))
"""
    raw = _docker("exec", container, "python", "-I", "-c", script, timeout=30)
    value = json.loads(raw)
    assert set(value) == {
        "packages",
        "forbidden_runtime_modules",
        "fastapi_transitive_dependency_retained",
    }
    return value


@contextmanager
def _select_http_owner(monkeypatch, url: str, token: str):
    values = {
        "inference_pipelines_url": url,
        "inference_pipelines_token": token,
        "inference_pipelines_token_file": "",
        "inference_pipelines_database_url": "",
        "inference_pipelines_database_url_file": "",
        "inference_pipelines_timeout_seconds": 30,
    }
    with monkeypatch.context() as scoped:
        for name, value in values.items():
            scoped.setattr(settings, name, value)
        yield


async def _wait_ready(origin: str, container: str, *, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
        while True:
            try:
                response = await client.get(f"{origin}/health")
                if response.status_code == 200 and response.json() == {
                    "status": "ready"
                }:
                    return
            except (httpx.HTTPError, json.JSONDecodeError):
                pass
            assert _inspect_docker(container)["State"]["Running"]
            assert time.monotonic() < deadline, (
                "Inference owner image did not become ready"
            )
            await asyncio.sleep(0.2)


@asynccontextmanager
async def _inference_container(test_engine, directory: Path, token: str):
    image_reference = _required_env("AAP_INFERENCE_PIPELINE_IMAGE")
    expected_identity = _required_env("AAP_INFERENCE_PIPELINE_IDENTITY")
    image = _inspect_docker(image_reference)
    image_labels = image.get("Config", {}).get("Labels") or {}
    assert image_labels.get("org.aap.acceptance") == _IMAGE_ACCEPTANCE
    assert image_labels.get("org.aap.acceptance-id") == expected_identity
    assert image.get("Config", {}).get("User") == "10008:10008"
    assert image.get("Config", {}).get("Cmd") == ["python", "-m", "aap_inference"]

    parsed = make_url(pipeline_dsn(test_engine))
    container_dsn = parsed.set(host="host.docker.internal").render_as_string(
        hide_password=False
    )
    identity = secrets.token_hex(12)
    directory.mkdir(mode=0o700)
    dsn_file = directory / "inference-database-url"
    token_file = directory / "inference-control-token"
    container = None
    active_error: BaseException | None = None
    try:
        for path, value in ((dsn_file, container_dsn), (token_file, token)):
            path.write_text(value, encoding="utf-8")
            path.chmod(0o444)

        container = _docker(
            "run",
            "--detach",
            "--pull=never",
            "--name",
            f"aap-inference-pipeline-{identity}",
            "--label",
            f"org.aap.acceptance={_RUN_ACCEPTANCE}",
            "--label",
            f"org.aap.acceptance-id={identity}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=64",
            "--memory=256m",
            "--cpus=1",
            "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m",
            "--publish",
            "127.0.0.1::8768",
            "--mount",
            f"type=bind,src={dsn_file},dst=/run/secrets/inference_pipelines_database_url,readonly",
            "--mount",
            f"type=bind,src={token_file},dst=/run/secrets/inference_control_token,readonly",
            "--env",
            "INFERENCE_HOST=0.0.0.0",
            "--env",
            "INFERENCE_PORT=8768",
            "--env",
            "INFERENCE_MAX_CONCURRENCY=8",
            "--env",
            "INFERENCE_PIPELINES_DATABASE_URL_FILE=/run/secrets/inference_pipelines_database_url",
            "--env",
            "INFERENCE_CONTROL_TOKEN_FILE=/run/secrets/inference_control_token",
            image["Id"],
            timeout=120,
        ).strip()

        observed = _inspect_docker(container)
        labels = observed.get("Config", {}).get("Labels") or {}
        assert observed.get("Id") == container
        assert labels.get("org.aap.acceptance") == _RUN_ACCEPTANCE
        assert labels.get("org.aap.acceptance-id") == identity
        assert observed["Config"]["User"] == "10008:10008"
        assert observed["Config"]["Cmd"] == ["python", "-m", "aap_inference"]
        host = observed["HostConfig"]
        assert host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in host["SecurityOpt"]
        assert (host["PidsLimit"], host["Memory"], host["NanoCpus"]) == (
            64,
            256 * 1024 * 1024,
            1_000_000_000,
        )
        assert any(
            size in host["Tmpfs"]["/tmp"] for size in ("size=32m", "size=33554432")
        )
        binds = [mount for mount in observed["Mounts"] if mount["Type"] == "bind"]
        assert len(binds) == 2 and all(mount["RW"] is False for mount in binds)
        assert {
            mount["Destination"]: Path(mount["Source"]).resolve() for mount in binds
        } == {
            "/run/secrets/inference_pipelines_database_url": dsn_file.resolve(),
            "/run/secrets/inference_control_token": token_file.resolve(),
        }
        port = observed["NetworkSettings"]["Ports"]["8768/tcp"]
        assert len(port) == 1 and port[0]["HostIp"] == "127.0.0.1"
        environment = dict(
            item.split("=", 1) for item in observed["Config"].get("Env", [])
        )
        assert environment["INFERENCE_PIPELINES_DATABASE_URL_FILE"] == (
            "/run/secrets/inference_pipelines_database_url"
        )
        assert environment["INFERENCE_CONTROL_TOKEN_FILE"] == (
            "/run/secrets/inference_control_token"
        )
        assert "INFERENCE_PIPELINES_DATABASE_URL" not in environment
        assert "INFERENCE_CONTROL_TOKEN" not in environment
        assert not any(
            "MEMBERSHIP" in name or "TRANSFER" in name for name in environment
        )

        origin = f"http://127.0.0.1:{port[0]['HostPort']}"
        await _wait_ready(origin, container)
        yield origin, container, image["Id"], container_dsn
        assert _inspect_docker(container)["State"]["OOMKilled"] is False
    except BaseException as error:
        active_error = error
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        if container is not None:
            try:
                logs = _docker("logs", container, timeout=15)
                assert container_dsn not in logs and token not in logs
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                await asyncio.to_thread(_stop_remove_container, container, identity)
            except BaseException as error:
                cleanup_errors.append(error)
        for path in (dsn_file, token_file):
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                cleanup_errors.append(error)
        try:
            directory.rmdir()
        except OSError as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            if active_error is None:
                raise AssertionError(
                    "Inference container cleanup or log redaction failed"
                ) from cleanup_errors[0]
            warnings.warn(
                "Inference container cleanup or log redaction also failed",
                RuntimeWarning,
                stacklevel=2,
            )


async def _owned_test_rows(maker, actor_id, operation_ids, prefix):
    async with maker() as db:
        pipeline_count = await db.scalar(
            text(
                "SELECT count(*) FROM public.project_pipelines "
                "WHERE created_by = :actor_id AND name LIKE :prefix"
            ),
            {"actor_id": actor_id, "prefix": f"{prefix}%"},
        )
        receipt_count = await db.scalar(
            text(
                "SELECT count(*) FROM aap_inference_pipelines_v1.operation_receipts "
                "WHERE operation_id = ANY(CAST(:operation_ids AS uuid[]))"
            ),
            {"operation_ids": [str(value) for value in operation_ids]},
        )
    return int(pipeline_count or 0), int(receipt_count or 0)


async def test_inference_pipeline_container_image_replay_restart_and_local_profile(
    test_engine, app_module, monkeypatch, tmp_path
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    token = secrets.token_urlsafe(32)
    actor_id = None
    operation_ids: list[uuid.UUID] = []
    pipeline_ids: list[uuid.UUID] = []
    latencies: list[float] = []
    owner_identity = _required_env("AAP_INFERENCE_PIPELINE_IDENTITY")
    prefix = f"inference-container-{uuid.uuid4().hex[:12]}"
    created_name = f"{prefix}-created"
    updated_name = f"{prefix}-updated"
    create_operation_id = uuid.uuid4()
    update_operation_id = uuid.uuid4()
    operation_ids.extend((create_operation_id, update_operation_id))
    profile = None
    active_error: BaseException | None = None
    try:
        async with _inference_container(
            test_engine, tmp_path / f"inference-container-{uuid.uuid4().hex}", token
        ) as (origin, container, image_id, _container_dsn):
            expected_packages = _source_package_hashes()
            assert sum(len(files) for files in expected_packages.values()) == 39
            installed = _container_package_inventory(container)
            assert installed["packages"] == expected_packages
            assert not any(installed["forbidden_runtime_modules"].values())

            with _select_http_owner(monkeypatch, origin, token):
                await replay_legacy_samples(test_engine, app_module, monkeypatch)

            async with maker.begin() as db:
                actor = await create_user(
                    db,
                    "super_admin",
                    f"inference-container-{uuid.uuid4().hex}@example.test",
                    "Inference container acceptance",
                )
                actor_id = actor.id

            command = pipeline_command_request(
                "create_pipeline",
                actor_id=actor_id,
                actor_role="super_admin",
                payload={
                    "scope": "public",
                    "project_id": None,
                    "organization_id": None,
                    "name": created_name,
                    "stages": [],
                    "is_default": False,
                },
                operation_id=create_operation_id,
            )
            owner = PipelineHttpClient(origin, token, timeout_seconds=15)
            try:
                created = await owner.execute(command)
                assert created.ok and created.commit_state == "committed"
                pipeline_id = created.result.resource_id
                pipeline_ids.append(pipeline_id)

                update = pipeline_command_request(
                    "update_pipeline",
                    actor_id=actor_id,
                    actor_role="super_admin",
                    target_id=pipeline_id,
                    payload={"name": updated_name},
                    operation_id=update_operation_id,
                )
                changed = await owner.execute(update)
                assert changed.ok and changed.result.resource_id == pipeline_id

                gate = asyncio.Semaphore(4)

                async def read_once() -> None:
                    request = pipeline_query_request(
                        "get_pipeline",
                        actor_id=actor_id,
                        actor_role="super_admin",
                        target_id=pipeline_id,
                    )
                    async with gate:
                        started = time.perf_counter()
                        reply = await owner.query(request)
                        latencies.append((time.perf_counter() - started) * 1000)
                    assert isinstance(reply, PipelineQuerySuccess)
                    assert len(reply.rows) == 1
                    assert reply.rows[0].id == pipeline_id
                    assert reply.rows[0].name == updated_name

                started = time.perf_counter()
                await asyncio.gather(*(read_once() for _ in range(16)))
                elapsed_ms = (time.perf_counter() - started) * 1000
            finally:
                await owner.close()

            await asyncio.to_thread(_docker, "restart", container, timeout=60)
            published = _inspect_docker(container)["NetworkSettings"]["Ports"][
                "8768/tcp"
            ]
            assert len(published) == 1 and published[0]["HostIp"] == "127.0.0.1"
            origin = f"http://127.0.0.1:{published[0]['HostPort']}"
            await _wait_ready(origin, container)
            assert _inspect_docker(container)["State"]["OOMKilled"] is False

            owner = PipelineHttpClient(origin, token, timeout_seconds=15)
            try:
                committed = await owner.status(pipeline_status_request(command))
                assert isinstance(committed, PipelineStatusSuccess)
                assert committed.state == "committed"
                assert committed.resource_id == pipeline_id

                replayed = await owner.execute(command)
                assert replayed.ok and replayed.commit_state == "committed"
                assert replayed.result.resource_id == pipeline_id

                projection_request = pipeline_query_request(
                    "get_pipeline_projection",
                    actor_id=actor_id,
                    actor_role="super_admin",
                    target_id=pipeline_id,
                    evidence=command,
                )
                projection = await owner.query(projection_request)
                assert isinstance(projection, PipelineQuerySuccess)
                assert len(projection.rows) == 1
                assert projection.rows[0].id == pipeline_id
                assert projection.rows[0].name == updated_name
            finally:
                await owner.close()

            pipeline_count, receipt_count = await _owned_test_rows(
                maker, actor_id, operation_ids, prefix
            )
            assert (pipeline_count, receipt_count) == (1, 2)
            profile = {
                "image_id": image_id,
                "image_identity": owner_identity,
                "installed_package_hashes": installed["packages"],
                "package_file_count": sum(
                    len(files) for files in installed["packages"].values()
                ),
                "forbidden_runtime_modules_present": installed[
                    "forbidden_runtime_modules"
                ],
                "fastapi_transitive_dependency_retained": installed[
                    "fastapi_transitive_dependency_retained"
                ],
                "requests": 16,
                "concurrency": 4,
                "successes": len(latencies),
                "failures": 0,
                "elapsed_ms": elapsed_ms,
                "p50_ms": statistics.median(latencies),
                "p95_ms": max(latencies),
                "restarted_status": committed.state,
                "restart_port_reinspected": True,
                "replay_resource_id_unchanged": replayed.result.resource_id
                == pipeline_id,
                "fresh_projection_reflects_later_update": (
                    projection.rows[0].name == updated_name
                ),
                "pipeline_count_after_replay": pipeline_count,
                "receipt_count_after_replay": receipt_count,
                "oom_killed": False,
                "scope": "Local fixture only; not a production capacity claim.",
            }
        assert profile is not None
        profile["container_and_secrets_removed"] = True
        report = (
            _ROOT
            / ".worktree"
            / f"inference-pipeline-container-profile-{uuid.uuid4().hex}.json"
        )
        with report.open("x", encoding="utf-8") as output:
            json.dump(profile, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
    except BaseException as error:
        active_error = error
        raise
    finally:
        if actor_id is not None:
            try:
                async with maker.begin() as db:
                    await db.execute(
                        text(
                            "DELETE FROM aap_inference_pipelines_v1.operation_receipts "
                            "WHERE operation_id = ANY(CAST(:operation_ids AS uuid[]))"
                        ),
                        {"operation_ids": [str(value) for value in operation_ids]},
                    )
                    if pipeline_ids:
                        await db.execute(
                            delete(ProjectPipeline).where(
                                ProjectPipeline.id.in_(pipeline_ids)
                            )
                        )
                    else:
                        await db.execute(
                            delete(ProjectPipeline).where(
                                ProjectPipeline.created_by == actor_id,
                                ProjectPipeline.name.like(f"{prefix}%"),
                            )
                        )
                    await db.execute(delete(User).where(User.id == actor_id))
            except Exception:
                if active_error is None:
                    raise
                warnings.warn(
                    "Inference pipeline container database cleanup also failed",
                    RuntimeWarning,
                    stacklevel=2,
                )
