"""Qualify the retained Mask GC operator assembly and installed entrypoint."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from functools import partial
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

import asyncpg
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from yoao_assets_mask_content.codec import build_rle_reference
from yoao_assets_mask_content.contract import RleContent
from yoao_assets_mask_content.gc_client import MaskGcClient
from yoao_assets_mask_content.gc_contract import DeleteOperationRequest
from yoao_labeling_mask_gc import contract as c
from yoao_labeling_mask_gc_runtime.database import AsyncpgMaskGcData
from app.config import settings
from app.services import mask_gc_collection as collection
from tests.notifications_publisher_acceptance_helpers import (
    connect_owned_dependency,
    disconnect_owned_dependency,
    inspect_docker,
)
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _object_exists,
    _required_env,
    _verify_postgres,
)
from tests.test_assets_media_containers import _memory_peak
from tests.test_labeling_module_api_cold import (
    _minio_dependency,
    _verify_minio_identity,
)
from tests.test_mask_gc_collection_live import _inventory
from tests.test_mask_gc_native import _native_call
from tests.test_mask_gc_preview_authority import _snapshot


pytestmark = [
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_MASK_CONTENT_ACCEPTANCE") != "1"
        or os.getenv("YOAO_WORKTREE_MODE") != "test",
        reason="requires verifier-owned DB/S3 resources",
    ),
]


ROOT = Path(__file__).resolve().parents[3]

_GC = "labeling-mask-gc"
_CONTENT = "assets-mask-content"
_CLEANUP = "assets-mask-cleanup"
_SERVICES = (_CONTENT, _CLEANUP, _GC)
_PORTS = {_GC: 8771, _CONTENT: 8770, _CLEANUP: 8770}
_MEMORY_MAX = 256 * 1024 * 1024
_PACKAGES = {_GC: "yoao_labeling_mask_gc", _CONTENT: "yoao_assets_mask_content"}
_SECRET_NAMES = {
    _GC: ("mask_gc_database_url", "mask_gc_token", "mask_cleanup_token"),
    _CONTENT: (
        "mask_content_token",
        "mask_content_s3_access_key",
        "mask_content_s3_secret_key",
    ),
    _CLEANUP: (
        "mask_cleanup_token",
        "mask_cleanup_s3_access_key",
        "mask_cleanup_s3_secret_key",
    ),
}


def _runtime_package(service):
    return _PACKAGES[_GC if service == _GC else _CONTENT]


def _images():
    identity = _required_env("AAP_MASK_IMAGE_ACCEPTANCE_ID")
    revision = _required_env("AAP_MASK_IMAGE_SOURCE_REVISION")
    assert re.fullmatch(r"[a-f0-9]{24}", identity)
    assert re.fullmatch(r"[a-f0-9]{40}", revision)
    inputs = [".dockerignore"]
    for service in (_GC, _CONTENT):
        inputs.extend(
            f"apps/{service}/{name}"
            for name in ("Dockerfile", "pyproject.toml", "uv.lock", "src")
        )
    subprocess.run(
        ["git", "diff", "--exit-code", "--quiet", revision, "--", *inputs],
        cwd=ROOT,
        check=True,
        timeout=15,
    )
    assert not subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *inputs],
        cwd=ROOT,
        timeout=15,
    ), "Mask images do not cover untracked build inputs"

    images = {
        _GC: _required_env("AAP_MASK_GC_IMAGE"),
        _CONTENT: _required_env("AAP_MASK_CONTENT_IMAGE"),
    }
    for image in images.values():
        assert re.fullmatch(r"sha256:[a-f0-9]{64}", image)
        details = inspect_docker("image", image)
        assert details["Id"] == image
        labels = details["Config"]["Labels"]
        assert labels["org.aap.acceptance"] == "module-data-boundary"
        assert labels["org.aap.acceptance-id"] == identity
        assert labels["org.aap.source-revision"] == revision
    images[_CLEANUP] = images[_CONTENT]
    return images, identity, revision


def _compose_config(directory, images, project, labels, dsn, minio, bucket, tokens):
    environment = {
        "MASK_CONTENT_S3_ENDPOINT_URL": (
            f"{'https' if minio['ssl'] else 'http'}://mask-minio:9000"
        ),
        "MASK_CONTENT_BUCKET": bucket,
        "MASK_CONTENT_REGION": "us-east-1",
    }
    secret_files = {}

    def secret(name, selector, value):
        path = directory / name
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o444)
        secret_files[name] = path
        environment[selector] = str(path)

    secret(
        "mask_gc_database_url",
        "LABELING_RUNTIME_MASK_GC_DATABASE_URL_FILE",
        dsn.set(
            drivername="postgresql", host="mask-postgres", port=5432
        ).render_as_string(hide_password=False),
    )
    secret(
        "mask_gc_token",
        "LABELING_RUNTIME_MASK_GC_CONTROL_TOKEN_FILE",
        tokens[_GC],
    )
    for service, role in ((_CONTENT, "content"), (_CLEANUP, "cleanup")):
        prefix = f"MASK_{role.upper()}"
        secret(f"mask_{role}_token", prefix + "_TOKEN_FILE", tokens[service])
        for key in ("ACCESS_KEY", "SECRET_KEY"):
            selector = f"{prefix}_S3_{key}"
            secret(
                selector.lower(),
                selector + "_FILE",
                _required_env("AAP_MODULE_DATA_" + selector),
            )

    env_file = directory / "compose.env"
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in environment.items()),
        encoding="utf-8",
    )
    compose = (
        "compose",
        "--project-name",
        project,
        "--env-file",
        str(env_file),
        "--profile",
        "mask-gc",
        "-f",
        str(ROOT / "docker-compose.assets-mask-content.yml"),
        "-f",
        str(ROOT / "docker-compose.labeling-mask-gc.yml"),
    )
    spec = json.loads(_docker(*compose, "config", "--format", "json"))
    assert set(spec["services"]) == set(_SERVICES)
    assert {key: Path(value["file"]) for key, value in spec["secrets"].items()} == (
        secret_files
    )
    environments = {}
    for service in _SERVICES:
        spec_service = spec["services"][service]
        build_owner = _GC if service == _GC else _CONTENT
        assert spec_service["build"]["dockerfile"] == f"apps/{build_owner}/Dockerfile"
        assert spec_service["build"]["context"] == str(ROOT)
        assert {item["source"] for item in spec_service["secrets"]} == set(
            _SECRET_NAMES[service]
        )
        environments[service] = spec_service["environment"]
    operator = spec["services"][_GC]
    assert operator["profiles"] == ["mask-gc"]
    assert operator["depends_on"][_CLEANUP]["condition"] == "service_healthy"
    assert environments[_GC] == {
        "MASK_GC_DATABASE_URL_FILE": "/run/secrets/mask_gc_database_url",
        "MASK_GC_TOKEN_FILE": "/run/secrets/mask_gc_token",
        "MASK_GC_CONTENT_URL": f"http://{_CLEANUP}:8770",
        "MASK_GC_CONTENT_TOKEN_FILE": "/run/secrets/mask_cleanup_token",
        "MASK_GC_POLL_ENABLED": "false",
        "MASK_GC_HOST": "0.0.0.0",
        "MASK_GC_PORT": "8771",
    }
    for service, role in ((_CONTENT, "content"), (_CLEANUP, "cleanup")):
        token, access_key, secret_key = _SECRET_NAMES[service]
        assert environments[service] == {
            "MASK_CONTENT_ROLE": role,
            "MASK_CONTENT_TOKEN_FILE": f"/run/secrets/{token}",
            "MASK_CONTENT_S3_ACCESS_KEY_FILE": f"/run/secrets/{access_key}",
            "MASK_CONTENT_S3_SECRET_KEY_FILE": f"/run/secrets/{secret_key}",
            "MASK_CONTENT_S3_ENDPOINT_URL": environment["MASK_CONTENT_S3_ENDPOINT_URL"],
            "MASK_CONTENT_BUCKET": bucket,
            "MASK_CONTENT_REGION": "us-east-1",
            "MASK_CONTENT_HOST": "0.0.0.0",
            "MASK_CONTENT_PORT": "8770",
        }

    override = directory / "acceptance.yml"
    override.write_text(
        "services:\n"
        + "".join(
            f"  {service}:\n"
            f"    image: {images[service]}\n"
            "    build: !reset null\n"
            '    restart: "no"\n'
            f"    labels: {json.dumps(labels)}\n"
            f'    ports: !override ["127.0.0.1::{_PORTS[service]}"]\n'
            for service in _SERVICES
        )
        + f"networks:\n  default: {{external: true, name: {project}}}\n",
        encoding="utf-8",
    )
    return (*compose, "-f", str(override)), secret_files, environments


def _owned_service(container, project, labels, images, secrets_by_name, environments):
    details = _inspect_docker(container)
    config = details["Config"]
    observed = config["Labels"]
    service = observed["com.docker.compose.service"]
    assert observed["com.docker.compose.project"] == project
    assert {key: observed[key] for key in labels} == labels
    assert details["Image"] == images[service]
    assert config["User"] == ("10003:10003" if service == _GC else "10005:10005")
    assert not config["Entrypoint"]
    expected = ["python", "-m", _runtime_package(service) + "_runtime"]
    if observed["com.docker.compose.oneoff"].lower() == "true":
        assert service == _GC
        expected.append("--once")
        assert not details["HostConfig"]["PortBindings"]
    assert config["Cmd"] == expected
    assert details["HostConfig"]["ReadonlyRootfs"] is True
    assert details["HostConfig"]["Memory"] == _MEMORY_MAX
    assert details["HostConfig"]["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in details["HostConfig"]["SecurityOpt"]
    assert not details["State"]["OOMKilled"] and details["RestartCount"] == 0
    assert set(details["NetworkSettings"]["Networks"]) == {project}

    mounts = {mount["Destination"]: mount for mount in details["Mounts"]}
    mounts.pop("/tmp", None)
    assert set(mounts) == {f"/run/secrets/{name}" for name in _SECRET_NAMES[service]}
    for name in _SECRET_NAMES[service]:
        mount = mounts[f"/run/secrets/{name}"]
        assert mount["Type"] == "bind" and not mount["RW"]
        assert Path(mount["Source"]) == secrets_by_name[name]
    env = dict(item.split("=", 1) for item in config["Env"])
    assert not any(
        key.startswith(("DATABASE_", "REDIS_", "CELERY_", "MINIO_", "AWS_"))
        for key in env
    )
    assert {
        key: value
        for key, value in env.items()
        if key.startswith(("MASK_GC_", "MASK_CONTENT_"))
    } == environments[service]
    return details


def _remove_project(compose, project, labels, images):
    for container in _docker(
        "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"
    ).splitlines():
        details = _inspect_docker(container)
        observed = details["Config"]["Labels"]
        assert {key: observed[key] for key in labels} == labels
        assert details["Image"] == images[observed["com.docker.compose.service"]]
    _docker(*compose, "down", "--timeout", "10", "--remove-orphans", timeout=60)


@contextmanager
def _operator_compose(directory, images, dsn, world):
    _verify_postgres(dsn)
    postgres = _required_env("AAP_MODULE_DATA_PG_CONTAINER_ID")
    minio = _minio_dependency(world["resources"])
    run = uuid4().hex[:24]
    project = f"mask-gc-{run}"
    labels = {
        "org.aap.acceptance": "module-data-boundary",
        "org.aap.acceptance-id": run,
        "org.yoao.worktree-owner": world["resources"]["owner"],
    }
    tokens = {service: secrets.token_urlsafe(32) for service in _SERVICES}
    compose, secret_files, environments = _compose_config(
        directory, images, project, labels, dsn, minio, world["bucket"], tokens
    )
    with ExitStack() as cleanup:
        network = _docker(
            "network",
            "create",
            "--driver",
            "bridge",
            *(
                arg
                for key, value in labels.items()
                for arg in ("--label", f"{key}={value}")
            ),
            project,
        )
        observed = inspect_docker("network", network)
        assert observed["Name"] == project and observed["Id"] == network
        assert observed["Labels"] == labels and not observed.get("Containers")
        cleanup.callback(_docker, "network", "rm", network)
        for container, alias in (
            (postgres, "mask-postgres"),
            (minio["id"], "mask-minio"),
        ):
            cleanup.callback(disconnect_owned_dependency, project, network, container)
            connect_owned_dependency(project, network, container, alias)
        _verify_minio_identity(_inspect_docker(minio["id"]), minio)
        cleanup.callback(_remove_project, compose, project, labels, images)

        _docker(
            *compose,
            "up",
            "--detach",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "110",
            _CONTENT,
            _GC,
            timeout=120,
        )
        check = partial(
            _owned_service,
            project=project,
            labels=labels,
            images=images,
            secrets_by_name=secret_files,
            environments=environments,
        )
        containers = {
            service: _docker(*compose, "ps", "--quiet", service)
            for service in _SERVICES
        }
        origins = {}
        for service, container in containers.items():
            details = check(container)
            assert details["State"]["Health"]["Status"] == "healthy"
            port = details["NetworkSettings"]["Ports"][f"{_PORTS[service]}/tcp"]
            assert len(port) == 1 and port[0]["HostIp"] == "127.0.0.1"
            origins[service] = f"http://127.0.0.1:{port[0]['HostPort']}"
            _verify_kernel_ceiling(container)
        yield compose, containers, origins, tokens, check


def _verify_kernel_ceiling(container):
    limit = _docker(
        "exec",
        container,
        "sh",
        "-c",
        "cat /sys/fs/cgroup/memory.max 2>/dev/null || "
        "cat /sys/fs/cgroup/memory/memory.limit_in_bytes",
    )
    assert int(limit) == _MEMORY_MAX


def _assert_installed(container, service):
    package = _runtime_package(service)
    forbidden = ["app", "sqlalchemy", "celery", "redis"]
    if service == _GC:
        forbidden.append("boto3")
    _docker(
        "exec",
        container,
        "python",
        "-I",
        "-c",
        (
            "import importlib.util,os,sys; from pathlib import Path; "
            f"import {package} as contract, {package}_runtime as runtime; "
            f"assert os.getuid()=={10003 if service == _GC else 10005}; "
            "assert not Path('/app').exists(); "
            "assert Path(runtime.__file__).is_relative_to(sys.prefix); "
            "assert Path(contract.__file__).is_relative_to(sys.prefix); "
            "assert Path(contract.__file__).with_name('protocol.schema.json').is_file(); "
            f"assert all(importlib.util.find_spec(m) is None for m in {forbidden!r})"
        ),
    )


async def _content_roundtrip(world, origin, token, cleanup_token):
    counts = tuple(value + 1 for value in uuid4().bytes)
    content = RleContent(encoding="coco_rle", size=(1, sum(counts)), counts=counts)
    key = build_rle_reference(content).object_key
    assert not _object_exists(world["s3"], world["bucket"], key)
    url = origin + "/v1/mask-content/"
    headers = {"Authorization": "Bearer " + token}
    async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
        try:
            response = await client.post(
                url + "put",
                json={"content": content.model_dump(mode="json")},
                headers=headers,
            )
            assert response.status_code == 200
            reference = response.json()["data"]["reference"]
            assert reference["object_key"] == key
            read = await client.post(
                url + "get", json={"reference": reference}, headers=headers
            )
            assert read.status_code == 200
            assert RleContent.model_validate(read.json()["data"]["content"]) == content
            forbidden = await client.post(
                url + "list", json={"limit": 1}, headers=headers
            )
            assert forbidden.status_code == 403
            denied = await client.post(
                url + "get",
                json={"reference": reference},
                headers={"Authorization": "Bearer " + cleanup_token},
            )
            assert denied.status_code == 401
        finally:
            world["s3"].delete_object(Bucket=world["bucket"], Key=key)


async def _claim_and_leave_unacknowledged(dsn, world, origin, token):
    pool = await asyncpg.create_pool(
        dsn.set(drivername="postgresql").render_as_string(hide_password=False),
        min_size=1,
        max_size=1,
    )
    try:
        async with pool.acquire() as connection:
            assert await connection.fetchval("SELECT current_user") == dsn.username
        result = await AsyncpgMaskGcData(pool).call(
            "claim_mask_gc_deletes",
            c.ClaimMaskGcDeletesRequest(worker_id=uuid4(), limit=1, lease_seconds=5),
        )
    finally:
        await pool.close()
    (claim,) = result.items
    assert claim.object_key in world["orphans"]
    with MaskGcClient(origin, token) as peer:
        receipt = peer.execute(
            DeleteOperationRequest(
                operation_id=claim.operation_id,
                object_key=claim.object_key,
                etag=claim.etag,
                last_modified=claim.last_modified,
            )
        )
    assert receipt.state == "completed" and receipt.outcome == "deleted"
    assert not _object_exists(world["s3"], world["bucket"], claim.object_key)
    return claim


def _assert_cycle_ok(cycle):
    assert cycle.scan_errors == cycle.data_errors == cycle.finish_errors == 0
    assert cycle.durable_uncertainties == 0


async def _collect_and_recover(stack, dsn, world):
    compose, containers, origins, tokens, check = stack
    for service in (_GC, _CONTENT):
        _assert_installed(containers[service], service)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(settings, "labeling_mask_gc_url", origins[_GC])
        monkeypatch.setattr(settings, "labeling_mask_gc_token", tokens[_GC])
        monkeypatch.setattr(settings, "labeling_mask_gc_timeout_seconds", 90)
        async with world["maker"]() as db:
            before = await _snapshot(db)
        journals = _journal_count(world)
        first = await collection.collect_raster_masks(
            None, c.RunRequest(dry_run=True, scan_pages=1, delete_limit=1)
        )
        assert first.scanned == 1000 and first.scan_complete is False
        assert first.referenced >= 1001 and first.next_after_key is not None
        second = await collection.collect_raster_masks(
            None,
            c.RunRequest(
                dry_run=True,
                scan_pages=1,
                delete_limit=1,
                after_key=first.next_after_key,
            ),
        )
        assert second.scanned == 8 and second.scan_complete is True
        _assert_cycle_ok(first)
        _assert_cycle_ok(second)
        assert first.eligible == second.eligible == 0
        async with world["maker"]() as db:
            assert await _snapshot(db) == before
        assert _journal_count(world) == journals

        fixture_worker = uuid4()
        async with world["maker"].begin() as db:
            claimed = await _native_call(
                db, "claim_mask_gc_sweep", {"worker_id": str(fixture_worker)}
            )
            assert claimed["ok"] and claimed["data"]["sweep"] is not None
            changed = await db.execute(
                text(
                    "UPDATE mask_gc_sweeps SET cutoff=clock_timestamp()+interval '1 day', "
                    "lease_expires_at=clock_timestamp()-interval '1 second' "
                    "WHERE sweep_id=CAST(:sweep AS uuid) AND worker_id=:worker"
                ),
                {
                    "sweep": claimed["data"]["sweep"]["sweep_id"],
                    "worker": fixture_worker,
                },
            )
            assert changed.rowcount == 1
        for pages in (2, 1):
            result = await collection.collect_raster_masks(
                None, c.RunRequest(scan_pages=pages, delete_limit=3)
            )
            _assert_cycle_ok(result)
            assert result.deleted == result.delete_claims == 3
            if pages == 2:
                assert result.scanned == 1008 and result.eligible == 7

    claim = await _claim_and_leave_unacknowledged(
        dsn, world, origins[_CLEANUP], tokens[_CLEANUP]
    )
    gc_peak = _memory_peak(containers[_GC])
    check(containers[_GC])
    _docker(*compose, "stop", "--timeout", "10", _GC)
    assert check(containers[_GC])["State"]["Status"] == "exited"
    async with world["maker"]() as db:
        lease_seconds = await db.scalar(
            text(
                "SELECT EXTRACT(epoch FROM (CAST(:expiry AS timestamptz) - clock_timestamp()))"
            ),
            {"expiry": claim.lease_expires_at},
        )
    await asyncio.sleep(max(0, float(lease_seconds)) + 0.1)

    once_container = _docker(
        *compose,
        "run",
        "--detach",
        "--no-deps",
        "--pull",
        "never",
        "-T",
        _GC,
        "python",
        "-m",
        "yoao_labeling_mask_gc_runtime",
        "--once",
    )
    assert once_container != containers[_GC]
    exit_code = _docker("wait", once_container, timeout=120)
    output = _docker("logs", once_container)
    assert check(once_container)["State"]["ExitCode"] == int(exit_code) == 0, output
    once = c.CycleStats.model_validate_json(output)
    _assert_cycle_ok(once)
    assert once.deleted == once.completed == once.delete_claims == 1
    assert _journal_count(world) == journals + 2 * len(world["orphans"])
    async with world["maker"]() as db:
        outcome = (
            await db.execute(
                text(
                    "SELECT state,outcome,claim_generation FROM mask_gc_intents "
                    "WHERE operation_id=:operation"
                ),
                {"operation": claim.operation_id},
            )
        ).one()
    assert outcome == ("completed", "deleted", claim.generation + 1)
    assert all(
        _object_exists(world["s3"], world["bucket"], key) for key in world["retained"]
    )
    assert not any(
        _object_exists(world["s3"], world["bucket"], key) for key in world["orphans"]
    )
    await _content_roundtrip(
        world, origins[_CONTENT], tokens[_CONTENT], tokens[_CLEANUP]
    )
    for service in (_CONTENT, _CLEANUP):
        check(containers[service])
    return [_memory_peak(containers[_CLEANUP]), gc_peak]


def _journal_count(world):
    return world["s3"].list_objects_v2(
        Bucket=world["bucket"], Prefix="raster-mask-gc/v1/"
    )["KeyCount"]


@pytest.fixture(scope="module")
async def mask_operator_result(test_db_url, apply_migrations):
    images, identity, revision = _images()
    engine = create_async_engine(test_db_url)
    try:
        async with _inventory(engine, retained=1001, orphans=7) as world:
            dsn: URL = make_url(_required_env("AAP_MODULE_DATA_LABELING_MASK_GC_URL"))
            assert dsn.username == "yoao_labeling_mask_gc_client"
            assert dsn.host == "127.0.0.1" and not dsn.query
            assert dsn.database == world["resources"]["database"]
            with (
                TemporaryDirectory(
                    prefix="mask-operator-", dir=_required_env("TMPDIR")
                ) as directory,
                _operator_compose(Path(directory), images, dsn, world) as stack,
            ):
                started = time.monotonic()
                peaks = await _collect_and_recover(stack, dsn, world)
                return {
                    "retained_prefix": 1001,
                    "eligible_objects": 7,
                    "delete_budget": 3,
                    "cycle_deleted": "3,3,1",
                    "preview_pages": "1000,8",
                    "wall_seconds": time.monotonic() - started,
                    "controlled_fixture_sweep_cutoff": True,
                    "installed_gc_image": images[_GC],
                    "installed_assets_mask_image": images[_CONTENT],
                    "image_acceptance_id": identity,
                    "image_source_revision": revision,
                    "hard_memory_limit_bytes": _MEMORY_MAX,
                    "cold_one_shot_recovered_claim": True,
                    "cgroup_memory_peak_bytes": json.dumps(peaks),
                    "cgroup_memory_peak_services": f"{_CLEANUP},{_GC}",
                }
    finally:
        await engine.dispose()


def test_mask_gc_operator_cold(mask_operator_result, record_property):
    for key, value in mask_operator_result.items():
        record_property(key, value)


def test_installed_gc_progresses_past_retained_prefix_with_independent_budgets(
    mask_operator_result, record_property
):
    for key, value in mask_operator_result.items():
        record_property(key, value)
    peaks = json.loads(mask_operator_result["cgroup_memory_peak_bytes"])
    assert all(peak is not None and 0 < peak < _MEMORY_MAX for peak in peaks), (
        "Strict GC/cleanup peak qualification requires measured cgroup v2 memory.peak "
        f"below {_MEMORY_MAX} bytes (measured {peaks}); the operational result is separate"
    )
