"""Capture actual pre-extraction Inference registry and project binding behavior."""

from __future__ import annotations

import base64
from datetime import date, datetime
import hashlib
from ipaddress import IPv4Address, IPv6Address
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import warnings

import httpx
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.core.security import create_access_token
from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.ml_backend import MLBackendService
from tests.factory import create_membership, create_project, create_user
from tests.labeling_class_query_helpers import (
    api_db_binding,
    assert_owned_test_database,
    clear_local_identity,
    make_request_runtime,
)
from tests.test_labeling_class_usage_reads_legacy import _legacy_runtime_url

_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "653b4e142f683c1441874d3e356f334ef60f3da6"
_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures/inference_registry_legacy/samples.json"
)
_RESPONSE_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_TIMESTAMP_FIELDS = {"created_at", "updated_at", "last_checked_at", "started_at"}
_SECRET_FIELDS = {"auth_token", "access_token", "password", "password_hash"}
_BUSINESS_AUDIT_ACTIONS = {
    "ml_registry.created",
    "ml_registry.updated",
    "ml_registry.deleted",
    "ml_backend.created",
    "ml_backend.updated",
    "ml_backend.enablement",
    "ml_backend.deleted",
    "ml_backend_pool.enablement",
    "project.update",
}
_MISSING = object()
_SOURCE_FILES = """
apps/api/app/api/v1/admin_ml_integrations.py
apps/api/app/api/v1/ml_backends.py
apps/api/app/api/v1/projects.py
apps/api/app/api/v1/router.py
apps/api/app/application/ml_registry_deletion.py
apps/api/app/config.py
apps/api/app/core/security.py
apps/api/app/db/enums.py
apps/api/app/db/models/async_job.py
apps/api/app/db/models/audit_log.py
apps/api/app/db/models/gpu_backend_fence.py
apps/api/app/db/models/gpu_backend_membership.py
apps/api/app/db/models/ml_backend_pool.py
apps/api/app/db/models/ml_backend_registry.py
apps/api/app/db/models/project.py
apps/api/app/db/models/project_member.py
apps/api/app/db/models/user.py
apps/api/app/deps.py
apps/api/app/schemas/ml_backend.py
apps/api/app/schemas/project.py
apps/api/app/services/audit.py
apps/api/app/services/gpu_arbitration/policy.py
apps/api/app/services/gpu_arbitration/retirement.py
apps/api/app/services/identity_account_lock_data.py
apps/api/app/services/identity_commands.py
apps/api/app/services/identity_contracts.py
apps/api/app/services/identity_gateway.py
apps/api/app/services/ml_backend.py
apps/api/app/services/ml_routing/ledger.py
apps/api/app/services/ml_routing/router.py
apps/api/app/services/ml_routing/safety.py
apps/api/app/services/project_access.py
apps/api/app/services/project_workspace.py
apps/api/tests/conftest.py
apps/api/tests/factory.py
apps/api/tests/labeling_class_query_helpers.py
apps/api/tests/test_identity_recovery.py
apps/api/tests/test_labeling_class_usage_reads_legacy.py
apps/api/tests/test_module_data_roles.py
apps/api/alembic/versions/0108_global_ml_backend_registry.py
apps/api/alembic/versions/0122_ml_backend_gpu_claim.py
apps/api/alembic/versions/0123_gpu_backend_fence.py
apps/api/alembic/versions/0124_gpu_backend_membership.py
apps/api/alembic/versions/0132_ml_backend_service_pools.py
""".split()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"the owned Inference registry verifier did not provide {name}"
    return value


def _capture_path() -> Path:
    requested = Path(_required_env("AAP_INFERENCE_REGISTRY_LEGACY_OUTPUT"))
    assert requested.is_absolute(), "capture output must be an absolute path"
    assert not requested.exists() and not requested.is_symlink(), (
        f"refusing to overwrite {requested}"
    )
    worktree = (_ROOT / ".worktree").resolve()
    output = requested.resolve()
    assert worktree in output.parents, "capture output must be inside Root .worktree"
    assert output.parent.is_dir(), "capture output parent must already exist"
    assert not output.exists() and not output.is_symlink(), (
        f"refusing to overwrite {output}"
    )
    return output


def _source_metadata() -> dict:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    hashes = {}
    for relative in _SOURCE_FILES:
        baseline = subprocess.run(
            ["git", "show", f"{_BASE_COMMIT}:{relative}"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
        ).stdout
        current = (_ROOT / relative).read_bytes()
        assert current == baseline, (
            f"legacy Inference registry source changed: {relative}"
        )
        hashes[relative] = hashlib.sha256(current).hexdigest()
    return {
        "captured_head": head,
        "pre_extraction_base": _BASE_COMMIT,
        "capture_driver_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "runtime_source_sha256": hashes,
    }


def _normalise_text(value: str, aliases: dict[str, str]) -> str:
    for actual, alias in sorted(
        aliases.items(), key=lambda item: len(item[0]), reverse=True
    ):
        value = value.replace(actual, alias)
    return value


def _redact_secret_text(value: str, world: dict) -> str:
    for secret, alias in sorted(
        world["secret_aliases"].items(), key=lambda item: len(item[0]), reverse=True
    ):
        value = value.replace(secret, alias)
    return value


def _normalise(value, aliases: dict[str, str], *, field: str | None = None):
    if isinstance(value, (IPv4Address, IPv6Address)):
        return str(value)
    if field in _SECRET_FIELDS:
        return "<redacted>" if value is not None else None
    if field in _TIMESTAMP_FIELDS and value is not None:
        return "<dynamic-timestamp>"
    if isinstance(value, uuid.UUID):
        return _normalise_text(str(value), aliases)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        return _normalise_text(value, aliases)
    if isinstance(value, (list, tuple)):
        return [_normalise(item, aliases) for item in value]
    if isinstance(value, dict):
        return {
            key: _normalise(item, aliases, field=key) for key, item in value.items()
        }
    return value


def _register_entity(world: dict, entity: str, value, alias: str) -> None:
    identifier = str(value)
    world[f"{entity}_ids"].add(uuid.UUID(identifier))
    world[f"{entity}_aliases"][identifier] = alias
    world["aliases"][identifier] = f"${alias}"


def _register_secret(world: dict, secret: str, alias: str) -> None:
    world["secret_values"].append(secret)
    world["secret_aliases"][secret] = alias
    world["aliases"][secret] = alias


def _model_row(model, row, aliases: dict[str, str]) -> dict:
    return _normalise(
        {column.name: getattr(row, column.name) for column in model.__table__.columns},
        aliases,
    )


def _sorted_snapshot_rows(rows: list[dict], *keys: str) -> list[dict]:
    selected_keys = keys or ("id",)
    return sorted(rows, key=lambda row: tuple(str(row[key]) for key in selected_keys))


async def _discover_backend_relations(db, world: dict) -> None:
    registry_ids = list(world["registry_ids"])
    if registry_ids:
        members = (
            await db.execute(
                select(MLBackendPoolMember)
                .where(MLBackendPoolMember.registry_id.in_(registry_ids))
                .order_by(MLBackendPoolMember.id)
            )
        ).scalars()
        for member in members:
            registry_key = str(member.registry_id)
            registry_alias = world["registry_aliases"].get(
                registry_key, "registry_unaliased"
            )
            pool_alias = f"pool_{registry_alias.removeprefix('registry_')}"
            member_alias = f"member_{registry_alias.removeprefix('registry_')}"
            _register_entity(world, "pool", member.pool_id, pool_alias)
            _register_entity(world, "member", member.id, member_alias)

    project_ids = list(world["project_ids"])
    pool_ids = list(world["pool_ids"])
    if not project_ids or not pool_ids:
        return
    associations = (
        await db.execute(
            select(ProjectMLBackendPool)
            .where(
                ProjectMLBackendPool.project_id.in_(project_ids),
                ProjectMLBackendPool.pool_id.in_(pool_ids),
            )
            .order_by(ProjectMLBackendPool.id)
        )
    ).scalars()
    for association in associations:
        project_alias = world["project_aliases"].get(
            str(association.project_id), "project_unaliased"
        )
        pool_alias = world["pool_aliases"].get(
            str(association.pool_id), "pool_unaliased"
        )
        _register_entity(
            world,
            "binding",
            association.id,
            f"binding_{project_alias}_{pool_alias}",
        )


async def _owned_snapshot(maker, world: dict) -> dict:
    async with maker() as db:
        await _discover_backend_relations(db, world)
        registries = (
            (
                await db.execute(
                    select(MLBackendRegistry)
                    .where(MLBackendRegistry.id.in_(list(world["registry_ids"])))
                    .order_by(MLBackendRegistry.name)
                )
            )
            .scalars()
            .all()
            if world["registry_ids"]
            else []
        )
        pools = (
            (
                await db.execute(
                    select(MLBackendServicePool)
                    .where(MLBackendServicePool.id.in_(list(world["pool_ids"])))
                    .order_by(MLBackendServicePool.name)
                )
            )
            .scalars()
            .all()
            if world["pool_ids"]
            else []
        )
        members = (
            (
                await db.execute(
                    select(MLBackendPoolMember)
                    .where(MLBackendPoolMember.id.in_(list(world["member_ids"])))
                    .order_by(
                        MLBackendPoolMember.registry_id,
                        MLBackendPoolMember.pool_id,
                    )
                )
            )
            .scalars()
            .all()
            if world["member_ids"]
            else []
        )
        bindings = (
            (
                await db.execute(
                    select(ProjectMLBackendPool)
                    .where(ProjectMLBackendPool.id.in_(list(world["binding_ids"])))
                    .order_by(
                        ProjectMLBackendPool.project_id,
                        ProjectMLBackendPool.pool_id,
                    )
                )
            )
            .scalars()
            .all()
            if world["binding_ids"]
            else []
        )
        projects = (
            (
                await db.execute(
                    select(
                        Project.id,
                        Project.name,
                        Project.owner_id,
                        Project.ml_backend_pool_id,
                        Project.default_variants,
                        Project.updated_at,
                    )
                    .where(Project.id.in_(list(world["project_ids"])))
                    .order_by(Project.id)
                )
            )
            .mappings()
            .all()
            if world["project_ids"]
            else []
        )
        fence_rows = (
            (
                await db.execute(
                    select(GPUBackendFence)
                    .where(
                        GPUBackendFence.backend_registry_id.in_(
                            list(world["fence_registry_ids"])
                        )
                    )
                    .order_by(GPUBackendFence.backend_registry_id)
                )
            )
            .scalars()
            .all()
            if world["fence_registry_ids"]
            else []
        )
        jobs = (
            (
                await db.execute(
                    select(AsyncJob)
                    .where(AsyncJob.id.in_(list(world["async_job_ids"])))
                    .order_by(AsyncJob.id)
                )
            )
            .scalars()
            .all()
            if world["async_job_ids"]
            else []
        )
        audits = (
            (
                await db.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.actor_id.in_(list(world["actor_ids"])),
                        AuditLog.action.in_(sorted(_BUSINESS_AUDIT_ACTIONS)),
                    )
                    .order_by(AuditLog.id)
                )
            )
            .scalars()
            .all()
            if world["actor_ids"]
            else []
        )

        audit_rows = []
        for ordinal, audit in enumerate(audits):
            if audit.request_id is not None:
                request_alias = world["request_aliases"].setdefault(
                    audit.request_id,
                    f"$business_request_{len(world['request_aliases'])}",
                )
                world["aliases"][audit.request_id] = request_alias
            audit_rows.append(
                {
                    "id": f"$business_audit_{ordinal}",
                    "actor_id": audit.actor_id,
                    "actor_email": audit.actor_email,
                    "actor_role": audit.actor_role,
                    "action": audit.action,
                    "target_type": audit.target_type,
                    "target_id": audit.target_id,
                    "method": audit.method,
                    "path": audit.path,
                    "status_code": audit.status_code,
                    "ip": audit.ip,
                    "detail_json": audit.detail_json,
                    "request_id": audit.request_id,
                    "created_at": audit.created_at,
                }
            )

        aliases = world["aliases"]
        return {
            "ml_backend_registry": _sorted_snapshot_rows(
                [_model_row(MLBackendRegistry, row, aliases) for row in registries]
            ),
            "ml_backend_service_pools": _sorted_snapshot_rows(
                [_model_row(MLBackendServicePool, row, aliases) for row in pools]
            ),
            "ml_backend_pool_members": _sorted_snapshot_rows(
                [_model_row(MLBackendPoolMember, row, aliases) for row in members],
                "registry_id",
                "pool_id",
                "id",
            ),
            "project_ml_backend_pool": _sorted_snapshot_rows(
                [_model_row(ProjectMLBackendPool, row, aliases) for row in bindings],
                "project_id",
                "pool_id",
                "id",
            ),
            "project_primary_bindings": _sorted_snapshot_rows(
                [_normalise(dict(row), aliases) for row in projects]
            ),
            "gpu_fences": _sorted_snapshot_rows(
                [_model_row(GPUBackendFence, row, aliases) for row in fence_rows],
                "backend_registry_id",
            ),
            "running_jobs": _sorted_snapshot_rows(
                [_model_row(AsyncJob, row, aliases) for row in jobs]
            ),
            "business_audit": [_normalise(row, aliases) for row in audit_rows],
        }


async def _request(
    client: httpx.AsyncClient,
    maker,
    world: dict,
    *,
    label: str,
    method: str,
    path: str,
    actor: str,
    body=_MISSING,
    params: dict | None = None,
    response_alias: str | None = None,
    response_entity: str = "registry",
    expected_status: int | None = None,
    metadata: dict | None = None,
) -> tuple[httpx.Response, object]:
    before = await _owned_snapshot(maker, world)
    request_kwargs = {
        "headers": {"Authorization": f"Bearer {world['tokens'][actor]}"},
        "params": params,
    }
    if body is not _MISSING:
        request_kwargs["json"] = body
    response = await client.request(method, path, **request_kwargs)
    try:
        payload = response.json() if response.content else None
    except ValueError:
        payload = response.text

    raw_headers = response.headers.raw
    header_bytes = b"\r\n".join(name + b": " + value for name, value in raw_headers)
    echoed_secret_aliases = [
        world["secret_aliases"][secret]
        for secret in world["secret_values"]
        if secret.encode("utf-8") in response.content + b"\r\n" + header_bytes
    ]
    body_secret_echoes = [
        secret
        for secret in world["secret_values"]
        if secret.encode("utf-8") in response.content
    ]

    if (
        response_alias is not None
        and response_entity == "registry"
        and isinstance(payload, dict)
        and isinstance(payload.get("id"), str)
    ):
        identifier = uuid.UUID(payload["id"])
        _register_entity(world, "registry", identifier, response_alias)

    after = await _owned_snapshot(maker, world)
    request = {
        "method": method,
        "path": _normalise_text(path, world["aliases"]),
        "actor_alias": actor,
    }
    if body is not _MISSING:
        request["body"] = _normalise(body, world["aliases"])
    if params is not None:
        request["query"] = _normalise(params, world["aliases"])
    sample = {
        "label": label,
        "request": request,
        "response": {
            "status": response.status_code,
            "headers": {
                name: _redact_secret_text(response.headers[name], world)
                for name in _RESPONSE_HEADERS
                if name in response.headers
            },
            "raw_headers": [
                [
                    name.decode("latin-1"),
                    _redact_secret_text(value.decode("latin-1"), world),
                ]
                for name, value in raw_headers
            ],
            "body_bytes_base64": (
                None
                if body_secret_echoes
                else base64.b64encode(response.content).decode("ascii")
            ),
            "body_length": len(response.content),
            "body_sha256": hashlib.sha256(response.content).hexdigest(),
            "body_bytes_redacted_for_synthetic_secret_echo": bool(body_secret_echoes),
            "raw_headers_sha256": hashlib.sha256(header_bytes).hexdigest(),
            "raw_headers_redacted_for_synthetic_secret_echo": (
                any(
                    secret.encode("utf-8") in header_bytes
                    for secret in world["secret_values"]
                )
            ),
            "redacted_secret_echoes": echoed_secret_aliases,
            "body": _normalise(payload, world["aliases"]),
        },
        "database_before": before,
        "database_after": after,
        "metadata": _normalise(metadata or {}, world["aliases"]),
    }
    world["samples"].append(sample)
    if expected_status is not None:
        assert response.status_code == expected_status, (
            f"{label}: expected {expected_status}, got {response.status_code}: "
            f"{response.text}"
        )
    return response, payload


async def _drain_registry_member(maker, registry_id: uuid.UUID) -> uuid.UUID:
    async with maker.begin() as db:
        service = MLBackendService(db)
        pool = await service._pool_for_registry(registry_id)
        assert pool is not None
        await service.drain_pool_member(pool.id, registry_id)
        return pool.id


async def _insert_managed_fence(maker, world: dict, registry_id: uuid.UUID) -> None:
    async with maker.begin() as db:
        db.add(
            GPUBackendFence(
                backend_registry_id=registry_id,
                runtime_epoch_high_water=1,
            )
        )
    world["fence_registry_ids"].add(registry_id)


async def _remove_managed_fence(maker, world: dict, registry_id: uuid.UUID) -> None:
    await assert_owned_test_database(world["test_engine"])
    assert registry_id in world["fence_registry_ids"]
    async with maker.begin() as db:
        # Reset only synthetic fixture state; rollback also restores this exact
        # trigger if any cleanup statement fails.
        await db.execute(
            text(
                "ALTER TABLE public.gpu_backend_fences DISABLE TRIGGER "
                "trg_validate_gpu_backend_fence_delete"
            )
        )
        await db.execute(
            delete(GPUBackendFence).where(
                GPUBackendFence.backend_registry_id == registry_id
            )
        )
        await db.execute(
            text(
                "ALTER TABLE public.gpu_backend_fences ENABLE TRIGGER "
                "trg_validate_gpu_backend_fence_delete"
            )
        )
    world["fence_registry_ids"].discard(registry_id)


async def _insert_running_prediction(
    maker,
    world: dict,
    *,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    registry_id: uuid.UUID,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    async with maker.begin() as db:
        db.add(
            AsyncJob(
                id=job_id,
                kind="batch_predict",
                project_id=project_id,
                user_id=actor_id,
                status="running",
                payload={"ml_backend_id": str(registry_id)},
            )
        )
    world["async_job_ids"].add(job_id)
    world["aliases"][str(job_id)] = "$job_running_prediction"
    return job_id


async def _remove_running_prediction(maker, world: dict, job_id: uuid.UUID) -> None:
    async with maker.begin() as db:
        await db.execute(delete(AsyncJob).where(AsyncJob.id == job_id))
    world["async_job_ids"].discard(job_id)


async def _restore_registry_fields(maker, registry_id: uuid.UUID, **fields) -> None:
    async with maker.begin() as db:
        await db.execute(
            update(MLBackendRegistry)
            .where(MLBackendRegistry.id == registry_id)
            .values(**fields)
            .execution_options(synchronize_session=False)
        )


async def _seed_world(test_engine) -> dict:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    prefix = f"inference-registry-{uuid.uuid4().hex[:10]}"
    world = {
        "test_engine": test_engine,
        "prefix": prefix,
        "aliases": {prefix: "$run"},
        "actors": {},
        "tokens": {},
        "secret_values": [],
        "secret_aliases": {},
        "actor_ids": set(),
        "project_ids": set(),
        "project_aliases": {},
        "project_member_ids": set(),
        "registry_ids": set(),
        "registry_aliases": {},
        "pool_ids": set(),
        "pool_aliases": {},
        "member_ids": set(),
        "member_aliases": {},
        "binding_ids": set(),
        "binding_aliases": {},
        "fence_registry_ids": set(),
        "async_job_ids": set(),
        "request_aliases": {},
        "samples": [],
    }

    async with maker.begin() as db:
        actors = {
            "super_admin": await create_user(
                db,
                "super_admin",
                f"{prefix}-super@example.test",
                "Registry capture super administrator",
            ),
            "project_owner": await create_user(
                db,
                "project_admin",
                f"{prefix}-owner@example.test",
                "Registry capture project owner",
            ),
            "annotator": await create_user(
                db,
                "employee",
                f"{prefix}-annotator@example.test",
                "Registry capture annotator",
            ),
            "viewer": await create_user(
                db,
                "viewer",
                f"{prefix}-viewer@example.test",
                "Registry capture viewer",
            ),
            "outsider": await create_user(
                db,
                "employee",
                f"{prefix}-outsider@example.test",
                "Registry capture outsider",
            ),
        }
        world["actors"] = actors
        for alias, actor in actors.items():
            world["actor_ids"].add(actor.id)
            world["aliases"][str(actor.id)] = f"$actor_{alias}"
            token = create_access_token(str(actor.id), actor.role)
            world["tokens"][alias] = token
            _register_secret(world, token, f"$token_{alias}")

        project = await create_project(
            db,
            owner_id=actors["project_owner"].id,
            name=f"{prefix}-project",
            type_key="image-det",
        )
        world["project"] = project
        world["project_ids"].add(project.id)
        world["project_aliases"][str(project.id)] = "project_main"
        world["aliases"][str(project.id)] = "$project_main"
        world["aliases"][project.display_id] = "$project_display_main"

        for alias, role in (("annotator", "annotator"), ("viewer", "viewer")):
            membership = await create_membership(
                db,
                project_id=project.id,
                user_id=actors[alias].id,
                role=role,
                assigned_by=actors["project_owner"].id,
            )
            world["project_member_ids"].add(membership.id)

    return world


async def _cleanup_world(test_engine, world: dict) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    await assert_owned_test_database(test_engine)
    for registry_id in tuple(world["fence_registry_ids"]):
        await _remove_managed_fence(maker, world, registry_id)
    async with maker.begin() as db:
        await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
        if world["actor_ids"]:
            await db.execute(
                delete(AuditLog).where(AuditLog.actor_id.in_(world["actor_ids"]))
            )
        if world["async_job_ids"]:
            await db.execute(
                delete(AsyncJob).where(AsyncJob.id.in_(world["async_job_ids"]))
            )
        if world["project_ids"] or world["pool_ids"]:
            await db.execute(
                delete(ProjectMLBackendPool).where(
                    ProjectMLBackendPool.project_id.in_(world["project_ids"])
                    | ProjectMLBackendPool.pool_id.in_(world["pool_ids"])
                )
            )
        if world["project_member_ids"]:
            await db.execute(
                delete(ProjectMember).where(
                    ProjectMember.id.in_(world["project_member_ids"])
                )
            )
        if world["member_ids"] or world["pool_ids"] or world["registry_ids"]:
            await db.execute(
                delete(MLBackendPoolMember).where(
                    MLBackendPoolMember.id.in_(world["member_ids"])
                    | MLBackendPoolMember.pool_id.in_(world["pool_ids"])
                    | MLBackendPoolMember.registry_id.in_(world["registry_ids"])
                )
            )
        if world["pool_ids"]:
            await db.execute(
                delete(MLBackendServicePool).where(
                    MLBackendServicePool.id.in_(world["pool_ids"])
                )
            )
        if world["registry_ids"]:
            await db.execute(
                delete(MLBackendRegistry).where(
                    MLBackendRegistry.id.in_(world["registry_ids"])
                )
            )
        if world["project_ids"]:
            await db.execute(
                delete(Project).where(Project.id.in_(world["project_ids"]))
            )
        if world["actor_ids"]:
            await db.execute(delete(User).where(User.id.in_(world["actor_ids"])))

    async with maker() as db:
        for table, column, values in (
            ("ml_backend_registry", "id", world["registry_ids"]),
            ("ml_backend_service_pools", "id", world["pool_ids"]),
            ("ml_backend_pool_members", "id", world["member_ids"]),
            ("project_ml_backend_pool", "id", world["binding_ids"]),
            ("projects", "id", world["project_ids"]),
            ("users", "id", world["actor_ids"]),
        ):
            if values:
                remaining = await db.scalar(
                    text(
                        f"SELECT count(*) FROM public.{table} "
                        f"WHERE {column}=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [str(value) for value in values]},
                )
                assert remaining == 0, (table, remaining)


async def _exercise_scenarios(client, maker, world, monkeypatch) -> None:
    prefix = world["prefix"]
    project_id = world["project"].id
    admin_prefix = "/api/v1/admin/ml-integrations"
    project_prefix = f"/api/v1/projects/{project_id}/ml-backends"
    initial_url = f"https://registry-{prefix}.example.test:8443/v1/"
    conflict_url = f"https://conflict-{prefix}.example.test:8443/"
    updated_url = f"https://registry-{prefix}.example.test:9443/v2/"
    await _request(
        client,
        maker,
        world,
        label="registry_list_superadmin_before",
        method="GET",
        path=f"{admin_prefix}/all",
        actor="super_admin",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="registry_list_project_admin",
        method="GET",
        path=f"{admin_prefix}/all",
        actor="project_owner",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="registry_create_project_admin_forbidden",
        method="POST",
        path=f"{admin_prefix}/registry",
        actor="project_owner",
        body={"name": f"{prefix}-forbidden", "url": conflict_url},
        expected_status=403,
    )
    await _request(
        client,
        maker,
        world,
        label="registry_create_fastapi_validation_422",
        method="POST",
        path=f"{admin_prefix}/registry",
        actor="super_admin",
        body={"url": f"https://missing-name-{prefix}.example.test:8443/"},
        expected_status=422,
    )

    gpu_resource_id = f"node-a/GPU-registry-{uuid.uuid4().hex[:12]}"
    gpu_invalid_secret = f"capture-only-invalid-auth-{uuid.uuid4().hex}"
    _register_secret(world, gpu_invalid_secret, "$auth_invalid_gpu_request")
    world["aliases"][gpu_resource_id] = "$gpu_unconfigured"
    await _request(
        client,
        maker,
        world,
        label="registry_create_stable_gpu_claim_422",
        method="POST",
        path=f"{admin_prefix}/registry",
        actor="super_admin",
        body={
            "name": f"{prefix}-unknown-gpu",
            "url": f"https://unknown-gpu-{prefix}.example.test:8443/",
            "gpu_resource_id": gpu_resource_id,
            "vram_budget_mb": 8192,
            "auth_method": "token",
            "auth_token": gpu_invalid_secret,
        },
        expected_status=422,
        metadata={"synthetic_auth_token_in_request": True},
    )

    first_secret = f"registry-auth-{uuid.uuid4().hex}"
    _register_secret(world, first_secret, "$auth_registry_primary")
    _, first = await _request(
        client,
        maker,
        world,
        label="registry_create_primary",
        method="POST",
        path=f"{admin_prefix}/registry",
        actor="super_admin",
        body={
            "name": f"{prefix}-primary",
            "url": initial_url,
            "is_interactive": True,
            "auth_method": "token",
            "auth_token": first_secret,
            "extra_params": {"max_concurrency": 2, "capture": "registry"},
        },
        response_alias="registry_primary",
        expected_status=201,
    )
    primary_id = uuid.UUID(first["id"])
    world["primary_registry_id"] = primary_id
    world["registry_aliases"][str(primary_id)] = "registry_primary"

    await _request(
        client,
        maker,
        world,
        label="registry_create_duplicate_normalized_url",
        method="POST",
        path=f"{admin_prefix}/registry",
        actor="super_admin",
        body={
            "name": f"{prefix}-duplicate",
            "url": initial_url.rstrip("/"),
        },
        expected_status=409,
    )
    _, second = await _request(
        client,
        maker,
        world,
        label="registry_create_conflict_target",
        method="POST",
        path=f"{admin_prefix}/registry",
        actor="super_admin",
        body={
            "name": f"{prefix}-conflict-target",
            "url": conflict_url,
            "auth_method": "none",
            "extra_params": {},
        },
        response_alias="registry_conflict_target",
        expected_status=201,
    )
    conflict_id = uuid.UUID(second["id"])
    world["conflict_registry_id"] = conflict_id
    world["registry_aliases"][str(conflict_id)] = "registry_conflict_target"

    await _request(
        client,
        maker,
        world,
        label="registry_update_url_conflict",
        method="PUT",
        path=f"{admin_prefix}/registry/{primary_id}",
        actor="super_admin",
        body={"url": conflict_url},
        expected_status=409,
    )
    _, updated_primary = await _request(
        client,
        maker,
        world,
        label="registry_update_normalizes_url_and_clears_auth_token",
        method="PUT",
        path=f"{admin_prefix}/registry/{primary_id}",
        actor="super_admin",
        body={
            "name": f"{prefix}-renamed",
            "url": updated_url,
            "auth_token": None,
            "extra_params": {"max_concurrency": 4, "capture": "updated"},
        },
        expected_status=200,
    )
    assert updated_primary["url"] == updated_url.rstrip("/")
    await _request(
        client,
        maker,
        world,
        label="registry_update_empty_sparse_payload",
        method="PUT",
        path=f"{admin_prefix}/registry/{primary_id}",
        actor="super_admin",
        body={},
        expected_status=200,
    )

    reused_secret = f"ignored-project-auth-{uuid.uuid4().hex}"
    _register_secret(world, reused_secret, "$auth_project_reuse")
    _, reused = await _request(
        client,
        maker,
        world,
        label="project_backend_create_reuses_registry_url",
        method="POST",
        path=project_prefix,
        actor="super_admin",
        body={
            "name": f"{prefix}-ignored-name",
            "url": updated_url,
            "is_interactive": False,
            "auth_method": "basic",
            "auth_token": reused_secret,
            "extra_params": {"capture": "ignored"},
        },
        response_alias="registry_primary",
        expected_status=201,
    )
    assert uuid.UUID(reused["id"]) == primary_id
    await _request(
        client,
        maker,
        world,
        label="project_backend_create_project_admin_forbidden",
        method="POST",
        path=project_prefix,
        actor="project_owner",
        body={
            "name": f"{prefix}-project-admin-create",
            "url": f"https://forbidden-{prefix}.example.test:8443/",
        },
        expected_status=403,
    )

    created_url = f"https://project-created-{prefix}.example.test:9443/v3/"
    _, project_created = await _request(
        client,
        maker,
        world,
        label="project_backend_create_new_global_registry",
        method="POST",
        path=project_prefix,
        actor="super_admin",
        body={
            "name": f"{prefix}-created-through-project",
            "url": created_url,
            "is_interactive": True,
            "auth_method": "none",
            "extra_params": {"max_concurrency": 3},
        },
        response_alias="registry_project_created",
        expected_status=201,
    )
    project_created_id = uuid.UUID(project_created["id"])
    world["project_created_registry_id"] = project_created_id

    await _request(
        client,
        maker,
        world,
        label="registry_list_superadmin_after_create",
        method="GET",
        path=f"{admin_prefix}/all",
        actor="super_admin",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="registry_list_project_admin_after_create",
        method="GET",
        path=f"{admin_prefix}/all",
        actor="project_owner",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="project_enabled_list_superadmin",
        method="GET",
        path=project_prefix,
        actor="super_admin",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="project_enabled_list_annotator",
        method="GET",
        path=project_prefix,
        actor="annotator",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="project_enabled_list_viewer_forbidden",
        method="GET",
        path=project_prefix,
        actor="viewer",
        expected_status=403,
    )
    await _request(
        client,
        maker,
        world,
        label="project_enabled_list_outside_scope",
        method="GET",
        path=project_prefix,
        actor="outsider",
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="project_available_list_superadmin",
        method="GET",
        path=f"{project_prefix}/available",
        actor="super_admin",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="project_available_list_owner",
        method="GET",
        path=f"{project_prefix}/available",
        actor="project_owner",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="project_available_list_viewer_forbidden",
        method="GET",
        path=f"{project_prefix}/available",
        actor="viewer",
        expected_status=403,
    )
    await _request(
        client,
        maker,
        world,
        label="project_available_list_outside_scope",
        method="GET",
        path=f"{project_prefix}/available",
        actor="outsider",
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="project_backend_detail_owner",
        method="GET",
        path=f"{project_prefix}/{primary_id}",
        actor="project_owner",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="project_backend_detail_annotator",
        method="GET",
        path=f"{project_prefix}/{primary_id}",
        actor="annotator",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="project_backend_detail_viewer_forbidden",
        method="GET",
        path=f"{project_prefix}/{primary_id}",
        actor="viewer",
        expected_status=403,
    )
    await _request(
        client,
        maker,
        world,
        label="project_backend_detail_unenabled_is_hidden",
        method="GET",
        path=f"{project_prefix}/{conflict_id}",
        actor="project_owner",
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="project_backend_detail_missing",
        method="GET",
        path=f"{project_prefix}/{uuid.uuid4()}",
        actor="project_owner",
        expected_status=404,
    )

    await _request(
        client,
        maker,
        world,
        label="project_backend_global_update_superadmin",
        method="PUT",
        path=f"{project_prefix}/{primary_id}",
        actor="super_admin",
        body={"extra_params": {"max_concurrency": 5, "capture": "project-path"}},
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="project_backend_global_update_project_admin_forbidden",
        method="PUT",
        path=f"{project_prefix}/{primary_id}",
        actor="project_owner",
        body={"name": f"{prefix}-role-boundary"},
        expected_status=403,
    )

    for field, reset_value in (
        ("name", f"{prefix}-conflict-target"),
        ("is_interactive", False),
        ("auth_method", "none"),
        ("extra_params", {}),
    ):
        await _request(
            client,
            maker,
            world,
            label=f"registry_update_explicit_null_{field}",
            method="PUT",
            path=f"{admin_prefix}/registry/{conflict_id}",
            actor="super_admin",
            body={field: None},
            metadata={
                "captured_status_without_assumed_outcome": True,
                "seed_field_reset_after_snapshot": reset_value,
            },
        )
        await _restore_registry_fields(maker, conflict_id, **{field: reset_value})

    await _insert_managed_fence(maker, world, conflict_id)
    await _request(
        client,
        maker,
        world,
        label="registry_update_managed_gpu_fence_blocks_protected_change",
        method="PUT",
        path=f"{admin_prefix}/registry/{conflict_id}",
        actor="super_admin",
        body={"url": f"https://managed-fence-{prefix}.example.test/"},
        expected_status=409,
        metadata={"seeded_runtime_epoch_high_water": 1},
    )
    await _remove_managed_fence(maker, world, conflict_id)

    enable_path = f"{project_prefix}/{conflict_id}/enablement"
    await _request(
        client,
        maker,
        world,
        label="registry_enablement_create_without_default_variants",
        method="PUT",
        path=enable_path,
        actor="project_owner",
        body={"enabled": True},
        expected_status=200,
    )
    variants = {"sam_variant": "large", "dino_variant": "base"}
    await _request(
        client,
        maker,
        world,
        label="registry_enablement_set_default_variants",
        method="PUT",
        path=enable_path,
        actor="project_owner",
        body={"enabled": False, "default_variants": variants},
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="registry_enablement_null_default_variants_preserves_existing",
        method="PUT",
        path=enable_path,
        actor="project_owner",
        body={"enabled": True, "default_variants": None},
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="registry_enablement_invalid_default_variants_shape",
        method="PUT",
        path=enable_path,
        actor="project_owner",
        body={"enabled": True, "default_variants": ["large"]},
        expected_status=422,
    )
    await _request(
        client,
        maker,
        world,
        label="registry_enablement_missing_backend",
        method="PUT",
        path=f"{project_prefix}/{uuid.uuid4()}/enablement",
        actor="project_owner",
        body={"enabled": True},
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="registry_enablement_annotator_forbidden",
        method="PUT",
        path=f"{project_prefix}/{primary_id}/enablement",
        actor="annotator",
        body={"enabled": False},
        expected_status=403,
    )

    pool_path = f"{project_prefix}/pools/available"
    await _request(
        client,
        maker,
        world,
        label="pool_available_owner",
        method="GET",
        path=pool_path,
        actor="project_owner",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="pool_available_viewer_visibility",
        method="GET",
        path=pool_path,
        actor="viewer",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="pool_available_outside_scope",
        method="GET",
        path=pool_path,
        actor="outsider",
        expected_status=404,
    )
    primary_pool_id = next(
        uuid.UUID(identifier)
        for identifier, alias in world["pool_aliases"].items()
        if alias == "pool_primary"
    )
    pool_enablement_path = f"{project_prefix}/pools/{primary_pool_id}/enablement"
    await _request(
        client,
        maker,
        world,
        label="pool_enablement_disable_without_override",
        method="PUT",
        path=pool_enablement_path,
        actor="project_owner",
        body={"enabled": False},
        expected_status=200,
    )
    pool_variants = {"sam_variant": "base"}
    await _request(
        client,
        maker,
        world,
        label="pool_enablement_set_default_variants",
        method="PUT",
        path=pool_enablement_path,
        actor="project_owner",
        body={"enabled": True, "default_variants": pool_variants},
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="pool_enablement_null_override_preserves_existing",
        method="PUT",
        path=pool_enablement_path,
        actor="project_owner",
        body={"enabled": False, "default_variants": None},
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="pool_enablement_invalid_default_variants_shape",
        method="PUT",
        path=pool_enablement_path,
        actor="project_owner",
        body={"enabled": True, "default_variants": ["base"]},
        expected_status=422,
    )
    await _request(
        client,
        maker,
        world,
        label="pool_enablement_missing_pool",
        method="PUT",
        path=f"{project_prefix}/pools/{uuid.uuid4()}/enablement",
        actor="project_owner",
        body={"enabled": True},
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="pool_enablement_annotator_forbidden",
        method="PUT",
        path=pool_enablement_path,
        actor="annotator",
        body={"enabled": True},
        expected_status=403,
    )

    await _request(
        client,
        maker,
        world,
        label="project_backend_delete_disables_conflict_registry",
        method="DELETE",
        path=f"{project_prefix}/{conflict_id}",
        actor="project_owner",
        expected_status=204,
    )
    await _request(
        client,
        maker,
        world,
        label="project_enabled_list_after_disable",
        method="GET",
        path=project_prefix,
        actor="project_owner",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="project_available_after_disable",
        method="GET",
        path=f"{project_prefix}/available",
        actor="project_owner",
        expected_status=200,
    )

    project_path = f"/api/v1/projects/{project_id}"
    _, project_bound = await _request(
        client,
        maker,
        world,
        label="project_primary_backend_binding_set",
        method="PATCH",
        path=project_path,
        actor="project_owner",
        body={"ml_backend_id": str(primary_id)},
        expected_status=200,
    )
    assert project_bound["ml_backend_id"] == str(primary_id)

    job_id = await _insert_running_prediction(
        maker,
        world,
        project_id=project_id,
        actor_id=world["actors"]["annotator"].id,
        registry_id=primary_id,
    )
    await _request(
        client,
        maker,
        world,
        label="registry_delete_blocked_by_running_prediction",
        method="DELETE",
        path=f"{admin_prefix}/registry/{primary_id}",
        actor="super_admin",
        expected_status=409,
        metadata={"running_job_alias": "$job_running_prediction"},
    )
    await _remove_running_prediction(maker, world, job_id)

    await _insert_managed_fence(maker, world, primary_id)
    await _request(
        client,
        maker,
        world,
        label="registry_delete_blocked_by_managed_gpu_fence",
        method="DELETE",
        path=f"{admin_prefix}/registry/{primary_id}",
        actor="super_admin",
        expected_status=409,
        metadata={"seeded_runtime_epoch_high_water": 1},
    )
    await _remove_managed_fence(maker, world, primary_id)

    drained_pool_id = await _drain_registry_member(maker, primary_id)
    assert drained_pool_id == primary_pool_id
    monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
    response, _ = await _request(
        client,
        maker,
        world,
        label="registry_delete_after_drain_and_real_redis_quiescence",
        method="DELETE",
        path=f"{admin_prefix}/registry/{primary_id}",
        actor="super_admin",
        expected_status=204,
        metadata={
            "member_drained_in_committed_owner_session": True,
            "router_mode": "enforce",
            "delete_runs_real_owned_redis_ledger_quiescence_check": True,
        },
    )
    assert response.status_code == 204
    final_snapshot = world["samples"][-1]["database_after"]
    assert final_snapshot["project_primary_bindings"] == [
        {
            "id": "$project_main",
            "name": "$run-project",
            "owner_id": "$actor_project_owner",
            "ml_backend_pool_id": None,
            "default_variants": {},
            "updated_at": "<dynamic-timestamp>",
        }
    ]
    assert any(
        row["pool_id"] == "$pool_primary"
        for row in final_snapshot["project_ml_backend_pool"]
    ), "global registry removal should leave the project-pool association row"


async def _exercise_owned_http(
    test_engine,
    app_module,
    monkeypatch,
    *,
    application_name: str = "inference-registry-legacy-capture",
) -> dict:
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    world = None
    runtime_engine = None
    active_error = None
    result = None
    runtime_url = _legacy_runtime_url(test_engine)
    clear_local_identity(monkeypatch)
    try:
        world = await _seed_world(test_engine)
        runtime_engine, runtime_maker = await make_request_runtime(
            test_engine, application_name=application_name
        )
        async with runtime_engine.connect() as connection:
            role, database = (
                await connection.execute(
                    text("SELECT current_user, current_database()")
                )
            ).one()
        assert role == "legacy_runtime"
        assert database == test_engine.url.database
        runtime_identity = {
            "database_role": role,
            "database": "$owned_test_database",
            "postgres_container_id": _required_env("AAP_MODULE_DATA_PG_CONTAINER_ID"),
            "postgres_container_identity": _required_env(
                "AAP_MODULE_DATA_PG_CONTAINER_IDENTITY"
            ),
        }
        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        async with api_db_binding(app_module, runtime_maker):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test", timeout=45
            ) as client:
                await _exercise_scenarios(client, owner_maker, world, monkeypatch)
        result = {
            "samples": world["samples"],
            "runtime_identity": runtime_identity,
            "runtime_url": runtime_url,
            "tokens": list(world["tokens"].values()),
            "secret_values": world["secret_values"],
        }
    except BaseException:
        active_error = sys.exc_info()[1]
        raise
    finally:
        cleanup_errors = []
        if world is not None:
            try:
                await _cleanup_world(test_engine, world)
            except Exception as error:
                cleanup_errors.append(error)
        if runtime_engine is not None:
            try:
                await runtime_engine.dispose()
            except Exception as error:
                cleanup_errors.append(error)
        if active_error is not None:
            for error in cleanup_errors:
                warnings.warn(
                    f"inference registry legacy capture cleanup failed: {error!r}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        elif cleanup_errors:
            raise cleanup_errors[0]
    assert result is not None
    return result


def _validate_and_enrich_public_responses(live: dict) -> None:
    """Check credential handling and annotate the supplied-token validation case."""
    for sample in live["samples"]:
        response = sample["response"]
        if sample["metadata"].get("synthetic_auth_token_in_request"):
            assert set(response["redacted_secret_echoes"]) <= {
                "$auth_invalid_gpu_request"
            }
            sample["metadata"]["validation_response_echoed_supplied_token"] = (
                "$auth_invalid_gpu_request" in response["redacted_secret_echoes"]
            )
            continue
        assert not response["redacted_secret_echoes"], (
            f"unexpected public response credential echo: {sample['label']}"
        )
        assert not response["body_bytes_redacted_for_synthetic_secret_echo"], (
            f"unexpected public response credential echo: {sample['label']}"
        )
        response_bytes = base64.b64decode(response["body_bytes_base64"], validate=True)
        assert all(
            secret.encode("utf-8") not in response_bytes
            for secret in (*live["tokens"], *live["secret_values"])
        ), f"a public response leaked a synthetic credential: {sample['label']}"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
    or os.environ.get("AAP_INFERENCE_REGISTRY_LEGACY_CAPTURE") != "1",
    reason="requires an explicit owned legacy Inference registry capture",
)
async def test_capture_legacy_inference_registry_http_and_database(
    test_engine, app_module, monkeypatch
):
    output_path = _capture_path()
    source = _source_metadata()
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    live = await _exercise_owned_http(test_engine, app_module, monkeypatch)
    _validate_and_enrich_public_responses(live)
    capture = {
        "capture_kind": "actual-legacy-inference-registry-http-database-audit",
        "source": source,
        "runtime_identity": live["runtime_identity"],
        "observations": {
            "http_uses_real_asgi_routes": True,
            "api_sessions_use_verified_legacy_runtime": True,
            "database_snapshots_use_independent_owner_sessions": True,
            "business_audit_excludes_http_middleware_noise": True,
            "raw_response_body_bytes_are_retained_unless_they_echo_a_synthetic_secret": True,
            "raw_response_sha256_is_always_retained": True,
            "response_arrays_preserve_actual_route_order": True,
            "database_cleanup_scope": (
                "exact generated actors, project, registry/pool/member/binding IDs, "
                "business audit actor IDs, and explicit seeded guard rows"
            ),
        },
        "samples": live["samples"],
    }
    serialized = json.dumps(capture, ensure_ascii=False, sort_keys=True)
    assert live["runtime_url"] not in serialized
    assert all(secret not in serialized for secret in live["tokens"])
    assert all(secret not in serialized for secret in live["secret_values"])
    for marker in ("password_hash", "authorization", "bearer "):
        assert marker not in serialized.casefold()
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            output.write(
                json.dumps(capture, ensure_ascii=False, indent=2, sort_keys=True)
            )
            output.write("\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_frozen_fixture() -> dict:
    raw = _FIXTURE_PATH.read_bytes()
    expected_sha = _required_env("AAP_INFERENCE_REGISTRY_LEGACY_SHA256")
    assert len(expected_sha) == 64 and all(
        character in "0123456789abcdef" for character in expected_sha
    ), "AAP_INFERENCE_REGISTRY_LEGACY_SHA256 must be a lowercase SHA-256 hex digest"
    assert hashlib.sha256(raw).hexdigest() == expected_sha
    fixture = json.loads(raw)
    assert fixture.get("capture_kind") == (
        "actual-legacy-inference-registry-http-database-audit"
    )
    assert fixture.get("source", {}).get("pre_extraction_base") == _BASE_COMMIT
    assert isinstance(fixture.get("samples"), list) and fixture["samples"]
    return fixture


_REPLAY_TRANSPORT_ARTIFACTS = {
    "body_bytes_base64",
    "body_length",
    "body_sha256",
    "raw_headers",
    "raw_headers_sha256",
}


def _replay_semantics(sample: dict) -> dict:
    response = sample["response"]
    semantics = {
        "label": sample["label"],
        "request": sample["request"],
        "response": {
            key: value
            for key, value in response.items()
            if key not in _REPLAY_TRANSPORT_ARTIFACTS
        },
        "database_before": sample["database_before"],
        "database_after": sample["database_after"],
        "metadata": sample["metadata"],
    }
    missing_targets = {
        "project_backend_detail_missing": "$missing_registry",
        "registry_enablement_missing_backend": "$missing_registry",
        "pool_enablement_missing_pool": "$missing_pool",
    }
    if sample["label"] in missing_targets:
        assert response["status"] == 404
        identifiers = []
        for segment in sample["request"]["path"].split("/"):
            try:
                identifier = uuid.UUID(segment)
            except ValueError:
                continue
            assert str(identifier) == segment
            identifiers.append(segment)
        assert len(identifiers) == 1
        missing = identifiers[0]
        for snapshot in (sample["database_before"], sample["database_after"]):
            assert missing not in json.dumps(snapshot)
        return _normalise(semantics, {missing: missing_targets[sample["label"]]})
    return semantics


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
    or not _FIXTURE_PATH.is_file(),
    reason=(
        "requires an explicit owned acceptance run and a frozen actual-legacy "
        "Inference registry fixture"
    ),
)
async def test_replay_legacy_inference_registry_http_and_database(
    test_engine, app_module, monkeypatch
):
    fixture = _read_frozen_fixture()
    live = await _exercise_owned_http(
        test_engine,
        app_module,
        monkeypatch,
        application_name="inference-registry-legacy-replay",
    )
    _validate_and_enrich_public_responses(live)
    expected_samples = fixture["samples"]
    assert len(live["samples"]) == len(expected_samples)
    for observed, expected in zip(live["samples"], expected_samples, strict=True):
        assert _replay_semantics(observed) == _replay_semantics(expected), observed[
            "label"
        ]
