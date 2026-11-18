"""Frozen legacy data and owner bindings for Project class rename replay."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
import warnings
from uuid import UUID, uuid4

import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.project import Project
from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from tests import test_project_class_rename_legacy as legacy_capture
from tests.test_asset_storage_cleanup_runtime import (
    _docker,
    _inspect_docker,
    _stop_remove_container,
    _verify_postgres,
)
from tests.factory import create_project, create_task, create_user
from tests.workspace_configuration_helpers import ROOT, configuration_dsn
from tests.workspace_project_query_helpers import (
    project_query_briefs_dsn,
    project_query_dsn,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures/project_class_rename_legacy"
SAMPLES_SHA256 = "976b5b1d7c0508c17e7102b0d712db8e8dacdacb0876a2189ca60d3055335ea7"
LOCKS_SHA256 = "6e7abbf1a55c48a8eab27e1ec864e9f63a9065c5b9fcd2779b9f05d4a6a763ef"


class _SeedOnlyComplete(Exception):
    """Stop the legacy capture after committing its seed and before HTTP."""


@dataclass(frozen=True)
class ClassRenameBinding:
    mode: str
    url: str | None


def _fixture(name: str, expected_sha256: str, capture_kind: str) -> dict:
    raw = (FIXTURE_ROOT / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected_sha256
    value = json.loads(raw)
    assert value["capture_kind"] == capture_kind
    return value


def samples_fixture() -> dict:
    value = _fixture(
        "samples.json",
        SAMPLES_SHA256,
        "actual-pre-extraction-project-class-rename-http",
    )
    assert len(value["samples"]) == 21
    return value


def locks_fixture() -> dict:
    value = _fixture(
        "locks.json",
        LOCKS_SHA256,
        "actual-pre-extraction-project-class-rename-locks",
    )
    assert len(value["samples"]) == 5
    return value


async def seed_legacy_world(test_engine, app_module, monkeypatch, tmp_path) -> dict:
    """Reuse only the frozen capture's committed seed; never capture responses."""
    captured: dict = {}
    preserve_seed = {"value": False}
    original_cleanup = legacy_capture._cleanup_owned

    async def stop_before_request(
        _client, capture, _state, aliases, actors, tokens, **_kwargs
    ):
        preserve_seed["value"] = True
        captured.update(
            capture=capture,
            aliases=dict(aliases),
            actors=dict(actors),
            tokens=dict(tokens),
        )
        raise _SeedOnlyComplete

    async def cleanup_unless_captured(maker, *, actor_ids, project_ids, task_ids):
        if preserve_seed["value"]:
            return
        await original_cleanup(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            task_ids=task_ids,
        )

    no_output = tmp_path / "seed-only-must-not-be-written.json"
    with monkeypatch.context() as patch:
        patch.setattr(legacy_capture, "_capture_path", lambda: no_output)
        patch.setattr(
            legacy_capture, "_source_metadata", lambda: {"seed_only_reuse": True}
        )
        patch.setattr(legacy_capture, "_request", stop_before_request)
        patch.setattr(legacy_capture, "_cleanup_owned", cleanup_unless_captured)
        try:
            await legacy_capture.test_capture_pre_extraction_project_class_rename(
                test_engine, app_module, monkeypatch
            )
        except _SeedOnlyComplete:
            pass

    try:
        assert captured, "legacy seed harness did not reach its first request"
        assert captured["capture"]["samples"] == []
        assert not no_output.exists()
        required_actors = {
            sample["request"]["actor"]["alias"]
            for sample in samples_fixture()["samples"]
        }
        assert required_actors <= set(captured["actors"])
        # The old capture allocated this deliberately absent UUID only when
        # reaching the later missing-project request, after seed-only reuse ends.
        missing_project = uuid4()
        assert "$project_missing_project" not in captured["aliases"].values()
        async with async_sessionmaker(test_engine)() as observer:
            assert await observer.get(Project, missing_project) is None
        captured["aliases"][str(missing_project)] = "$project_missing_project"
        return captured
    except BaseException:
        if captured:
            await cleanup_world(test_engine, captured)
        raise


def aliased_uuid(world: dict, alias: str) -> UUID:
    target = f"${alias}"
    for value, current_alias in world["aliases"].items():
        if current_alias == target:
            return UUID(value)
    raise AssertionError(f"missing seeded UUID alias {target}")


def cleanup_ids(world: dict) -> dict[str, list[UUID]]:
    return {
        "actor_ids": [actor.id for actor in world["actors"].values()],
        "project_ids": [
            UUID(value)
            for value, alias in world["aliases"].items()
            if alias.startswith("$project_")
        ],
        "task_ids": [
            UUID(value)
            for value, alias in world["aliases"].items()
            if alias.startswith("$task_")
        ],
    }


async def cleanup_world(test_engine, world: dict) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    await legacy_capture._cleanup_owned(maker, **cleanup_ids(world))


async def seed_lock_world(test_engine) -> dict:
    """Reproduce the independent lock fixture's exact single-project seed."""
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids: list[UUID] = []
    project_ids: list[UUID] = []
    task_ids: list[UUID] = []
    try:
        async with maker.begin() as seed:
            actor = await create_user(
                seed,
                "project_admin",
                f"rename-lock-{uuid.uuid4().hex}@example.test",
                "Rename lock capture",
            )
            actor_ids.append(actor.id)
            project = await create_project(
                seed, owner_id=actor.id, name="Rename lock capture"
            )
            project_ids.append(project.id)
            project.tool_bindings = {"bbox": legacy_capture._binding(["Car"])}
            task = await create_task(seed, project_id=project.id)
            task_ids.append(task.id)
            annotation = Annotation(
                id=uuid.uuid4(),
                project_id=project.id,
                task_id=task.id,
                user_id=actor.id,
                annotation_type="bbox",
                tool_unit_id="bbox",
                source="manual",
                class_name="Car",
                geometry={"type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                attributes={},
                is_active=True,
            )
            seed.add(annotation)

        aliases = {
            str(actor.id): "$actor_owner",
            actor.email: "$email_owner",
            str(project.id): "$project_target",
            project.display_id: "$display_target",
            str(task.id): "$task_target",
            str(annotation.id): "$annotation_target",
        }
        return {
            "actor_id": actor.id,
            "project_id": project.id,
            "task_id": task.id,
            "annotation_id": annotation.id,
            "token": create_access_token(subject=str(actor.id), role=actor.role),
            "aliases": aliases,
            "actor_ids": actor_ids,
            "project_ids": project_ids,
            "task_ids": task_ids,
        }
    except BaseException:
        await legacy_capture._cleanup_owned(
            maker, actor_ids=actor_ids, project_ids=project_ids, task_ids=task_ids
        )
        raise


async def cleanup_lock_world(test_engine, world: dict) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    await legacy_capture._cleanup_owned(
        maker,
        actor_ids=world["actor_ids"],
        project_ids=world["project_ids"],
        task_ids=world["task_ids"],
    )


def _restore_settings(previous: dict[str, object]) -> None:
    for name, value in previous.items():
        setattr(settings, name, value)


@asynccontextmanager
async def _installed_configuration_provider(
    test_engine, token: str, *, temporary_prefix: str
):
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    image = _inspect_docker(os.environ["AAP_WORKSPACE_MODULE_IMAGE"])
    image_config = image["Config"]
    labels = image_config["Labels"]
    assert labels["org.aap.acceptance"] == "workspace-module-owner"
    assert (
        labels["org.aap.acceptance-id"] == os.environ["AAP_WORKSPACE_MODULE_IDENTITY"]
    )
    assert re.fullmatch(r"[0-9a-f]{7,64}", labels["org.aap.source-revision"])
    assert image_config["User"] == "10004:10004"
    assert image_config["Cmd"] == ["python", "-m", "yoao_workspace"]
    assert image_config.get("Entrypoint") in (None, [])
    image_id = image["Id"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)

    dsns = (
        (
            "WORKSPACE_CONFIGURATION_DATABASE_URL_FILE",
            "configuration",
            configuration_dsn(test_engine),
            "yoao_workspace_configuration_client",
        ),
        (
            "WORKSPACE_PROJECT_QUERY_DATABASE_URL_FILE",
            "project-query",
            project_query_dsn(test_engine),
            "yoao_workspace_project_query_client",
        ),
        (
            "WORKSPACE_PROJECT_QUERY_IDENTITY_BRIEFS_DATABASE_URL_FILE",
            "identity-briefs",
            project_query_briefs_dsn(test_engine),
            "yoao_identity_briefs_client",
        ),
    )
    postgres_id = os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"]
    postgres_name = os.environ["AAP_MODULE_DATA_PG_CONTAINER_NAME"]
    _verify_postgres(test_engine.url)
    identity = secrets.token_hex(12)
    network_name = f"aap-class-rename-{identity}"
    container_name = f"aap-class-rename-owner-{identity}"
    network_id = None
    connected = False
    container_id = None

    with tempfile.TemporaryDirectory(
        prefix=f"{temporary_prefix}-", dir=ROOT / ".worktree"
    ) as temporary:
        directory = Path(temporary)
        secrets_to_write = []
        for variable, filename, dsn, username in dsns:
            parsed = make_url(dsn)
            assert parsed.username == username
            assert (parsed.host, parsed.port, parsed.database) == (
                test_engine.url.host,
                test_engine.url.port,
                test_engine.url.database,
            )
            container_dsn = parsed.set(host=postgres_name, port=5432)
            secrets_to_write.append(
                (
                    variable,
                    filename,
                    container_dsn.render_as_string(hide_password=False),
                )
            )
        secrets_to_write.append(
            ("WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE", "control-token", token)
        )
        arguments = []
        for variable, filename, value in secrets_to_write:
            path = directory / filename
            path.write_text(value, encoding="utf-8")
            path.chmod(0o444)
            destination = f"/run/secrets/{filename}"
            arguments.extend(
                (
                    "--mount",
                    f"type=bind,src={path},dst={destination},readonly",
                    "--env",
                    f"{variable}={destination}",
                )
            )

        try:
            network_id = _docker(
                "network",
                "create",
                "--label",
                "org.aap.acceptance=module-data-boundary",
                "--label",
                f"org.aap.acceptance-id={identity}",
                network_name,
            )
            _docker(
                "network", "connect", "--gw-priority", "-1", network_id, postgres_id
            )
            connected = True
            container_id = _docker(
                "create",
                "--name",
                container_name,
                "--pull=never",
                "--network",
                network_id,
                "--label",
                "org.aap.acceptance=module-data-boundary",
                "--label",
                f"org.aap.acceptance-id={identity}",
                "--user=10004:10004",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--pids-limit=64",
                "--memory=256m",
                "--cpus=1",
                "--no-healthcheck",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=32m",
                "--publish",
                "127.0.0.1::8174",
                *arguments,
                "--env",
                "WORKSPACE_HOST=0.0.0.0",
                "--env",
                "WORKSPACE_PORT=8174",
                "--env",
                "WORKSPACE_TIMEOUT_SECONDS=15",
                "--env",
                "WORKSPACE_CONFIGURATION_SETUP_TIMEOUT_SECONDS=10",
                "--env",
                "WORKSPACE_PROJECT_QUERY_IDENTITY_TIMEOUT_SECONDS=5",
                image_id,
                timeout=90,
            )
            try:
                _docker("start", container_id)
                metadata = _inspect_docker(container_id)
                config = metadata["Config"]
                host = metadata["HostConfig"]
                assert metadata["Id"] == container_id and metadata["Image"] == image_id
                assert config["Labels"]["org.aap.acceptance"] == "module-data-boundary"
                assert config["Labels"]["org.aap.acceptance-id"] == identity
                assert config["User"] == "10004:10004"
                assert host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"]
                assert host.get("CapAdd") in (None, [])
                assert "no-new-privileges:true" in host["SecurityOpt"]
                assert host["PidsLimit"] == 64
                assert host["NanoCpus"] == 1_000_000_000
                assert host["Memory"] == 256 * 1024 * 1024
                tmpfs = host["Tmpfs"]["/tmp"]
                assert all(option in tmpfs for option in ("noexec", "nosuid", "nodev"))
                assert "size=32m" in tmpfs or "size=33554432" in tmpfs
                mounts = metadata["Mounts"]
                binds = [mount for mount in mounts if mount["Type"] == "bind"]
                expected_mounts = {
                    f"/run/secrets/{filename}": (directory / filename).resolve()
                    for _, filename, _ in secrets_to_write
                }
                assert {
                    mount["Destination"]: Path(mount["Source"]).resolve()
                    for mount in binds
                } == expected_mounts
                assert all(not mount["RW"] for mount in binds)
                assert {mount["Destination"] for mount in mounts} <= (
                    set(expected_mounts) | {"/tmp"}
                )
                environment = dict(
                    entry.split("=", 1) for entry in config["Env"] if "=" in entry
                )
                for variable, filename, _ in secrets_to_write:
                    assert environment[variable] == f"/run/secrets/{filename}"
                    assert variable.removesuffix("_FILE") not in environment
                ports = metadata["NetworkSettings"]["Ports"]["8174/tcp"]
                assert len(ports) == 1 and ports[0]["HostIp"] == "127.0.0.1"
                origin = f"http://127.0.0.1:{int(ports[0]['HostPort'])}"
                deadline = time.monotonic() + 40
                async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                    while True:
                        try:
                            if (await client.get(origin + "/ready")).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        state = _inspect_docker(container_id)["State"]
                        assert state["Running"], (
                            "Workspace owner exited before readiness"
                        )
                        assert time.monotonic() < deadline, "Workspace owner not ready"
                        await asyncio.sleep(0.1)
            except BaseException as error:
                try:
                    result = subprocess.run(
                        ["docker", "logs", "--tail", "120", container_id],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    diagnostic = result.stdout + result.stderr
                    for _, _, value in secrets_to_write:
                        diagnostic = diagnostic.replace(value, "<redacted>")
                    for _, _, dsn, _ in dsns:
                        password = make_url(dsn).password
                        if password:
                            diagnostic = diagnostic.replace(password, "<redacted>")
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        prefix="class-rename-startup-",
                        suffix=".log",
                        dir=ROOT / ".worktree",
                        delete=False,
                    ) as log:
                        log.write(diagnostic)
                    error.add_note(f"Private Workspace startup diagnostics: {log.name}")
                except Exception as diagnostic_error:
                    error.add_note(
                        f"Startup diagnostics unavailable: {diagnostic_error!r}"
                    )
                raise
            yield origin
        finally:
            active_error = sys.exc_info()[1]
            cleanup_errors = []
            if container_id is not None:
                try:
                    _stop_remove_container(container_id, identity)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if network_id is not None:
                try:
                    network = json.loads(_docker("network", "inspect", network_id))[0]
                    assert (
                        network["Id"] == network_id and network["Name"] == network_name
                    )
                    assert (
                        network["Labels"]["org.aap.acceptance"]
                        == "module-data-boundary"
                    )
                    assert network["Labels"]["org.aap.acceptance-id"] == identity
                    if connected:
                        _verify_postgres(test_engine.url)
                        _docker("network", "disconnect", network_id, postgres_id)
                    _docker("network", "rm", network_id)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if active_error is not None:
                for exc in cleanup_errors:
                    warnings.warn(
                        f"Workspace owner cleanup failed: {exc!r}", RuntimeWarning
                    )
            elif cleanup_errors:
                raise cleanup_errors[0]


@asynccontextmanager
async def class_rename_binding(monkeypatch, test_engine, *, mode, tmp_path):
    """Select Configuration owner while keeping API ProjectQuery borrowed."""
    if mode not in {"borrowed", "dedicated", "http"}:
        raise ValueError(f"unknown class-rename binding: {mode}")

    token = secrets.token_urlsafe(32)
    url = None
    dsn = None
    async with AsyncExitStack() as stack:
        if mode == "dedicated":
            dsn = configuration_dsn(test_engine)
        elif mode == "http":
            url = await stack.enter_async_context(
                _installed_configuration_provider(
                    test_engine,
                    token,
                    temporary_prefix=f"class-rename-{tmp_path.name}",
                )
            )

        changes: dict[str, object] = {
            "workspace_configuration_url": url or "",
            "workspace_configuration_database_url": dsn or "",
            "workspace_configuration_database_url_file": "",
            "workspace_configuration_timeout_seconds": 20,
            "workspace_membership_control_token": token if url else "",
            "workspace_membership_control_token_file": "",
            "workspace_project_query_url": "",
            "workspace_project_query_database_url": "",
            "workspace_project_query_database_url_file": "",
            "workspace_project_query_timeout_seconds": 20,
        }
        previous = {name: getattr(settings, name) for name in changes}
        try:
            for name, value in changes.items():
                monkeypatch.setattr(settings, name, value)
            yield ClassRenameBinding(mode=mode, url=url)
        finally:
            _restore_settings(previous)
