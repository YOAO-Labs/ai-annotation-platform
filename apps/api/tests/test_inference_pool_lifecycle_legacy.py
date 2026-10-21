"""Capture and replay legacy service-pool admin HTTP and database behavior."""

from __future__ import annotations

import contextvars
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from ipaddress import IPv4Address, IPv6Address
import json
import os
from pathlib import Path
import subprocess
import uuid
import warnings

import httpx
import pytest
import redis.asyncio as redis
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.core.security import create_access_token
from app.db.models.audit_log import AuditLog
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.user import User
from aap_inference.pool_capability import (
    canonicalize_capability,
    capability_fingerprint,
)
from app.services.ml_routing.ledger import RoutingLedger
from tests.factory import create_project, create_user
from tests.labeling_class_query_helpers import (
    api_db_binding,
    assert_owned_test_database,
    clear_local_identity,
    make_request_runtime,
)
from tests.test_identity_sessions_live import _verify_redis


_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "653b4e142f683c1441874d3e356f334ef60f3da6"
_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures/inference_pool_lifecycle_legacy/samples.json"
)
_RESPONSE_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_TIMESTAMP_FIELDS = {"created_at", "updated_at", "last_checked_at"}
_MISSING = object()
_SOURCE_FILES = """
apps/api/app/api/v1/admin_ml_integrations.py
apps/api/app/api/v1/router.py
apps/api/app/config.py
apps/api/app/core/security.py
apps/api/app/db/models/audit_log.py
apps/api/app/db/models/ml_backend_pool.py
apps/api/app/db/models/ml_backend_registry.py
apps/api/app/db/models/project.py
apps/api/app/db/models/user.py
apps/api/app/deps.py
apps/api/app/middleware/audit.py
apps/api/app/schemas/ml_backend.py
apps/api/app/services/audit.py
apps/api/app/services/identity_contracts.py
apps/api/app/services/ml_backend.py
apps/api/app/services/ml_client.py
apps/api/app/services/ml_routing/capability.py
apps/api/app/services/ml_routing/contracts.py
apps/api/app/services/ml_routing/ledger.py
apps/api/app/services/ml_routing/router.py
apps/api/app/services/ml_routing/safety.py
apps/api/tests/conftest.py
apps/api/tests/factory.py
apps/api/tests/labeling_class_query_helpers.py
apps/api/tests/test_identity_sessions_live.py
apps/api/tests/test_ml_routing_admin_pool_api.py
""".split()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"the owned service-pool verifier did not provide {name}"
    return value


def _capture_path() -> Path:
    requested = Path(_required_env("AAP_INFERENCE_POOLS_LEGACY_OUTPUT"))
    assert requested.is_absolute(), "capture output must be an absolute path"
    worktree = (_ROOT / ".worktree").resolve()
    output = requested.resolve()
    assert worktree in output.parents, "capture output must be inside Root .worktree"
    assert output.parent.is_dir(), "capture output parent must already exist"
    assert (
        not requested.is_symlink() and not output.exists() and not output.is_symlink()
    ), f"refusing to overwrite {output}"
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
        assert current == baseline, f"legacy pool source changed: {relative}"
        hashes[relative] = hashlib.sha256(current).hexdigest()
    return {
        "captured_head": head,
        "pre_extraction_base": _BASE_COMMIT,
        "capture_driver_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "runtime_source_sha256": hashes,
    }


def _replace_actual(value: str, aliases: dict[str, str]) -> str:
    for actual, alias in sorted(
        aliases.items(), key=lambda item: len(item[0]), reverse=True
    ):
        value = value.replace(actual, alias)
    return value


def _normalise(value, aliases: dict[str, str], *, field: str | None = None):
    if isinstance(value, (IPv4Address, IPv6Address)):
        return str(value)
    if field in _TIMESTAMP_FIELDS and value is not None:
        return "<dynamic-timestamp>"
    if field == "request_id" and value is not None:
        return "<request-id>"
    if isinstance(value, uuid.UUID):
        value = str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, str):
        return _replace_actual(value, aliases)
    if isinstance(value, (list, tuple)):
        return [_normalise(item, aliases) for item in value]
    if isinstance(value, dict):
        return {
            key: _normalise(item, aliases, field=key) for key, item in value.items()
        }
    return value


def _normalise_tables(value: dict, aliases: dict[str, str]) -> dict:
    result = _normalise(value, aliases)
    for table in (
        "ml_backend_registry",
        "ml_backend_service_pools",
        "ml_backend_pool_members",
        "project_ml_backend_pool",
    ):
        result[table].sort(key=lambda row: row["id"])
    return result


def _raw_capabilities(model_id: str) -> dict:
    return {"model_ids": [model_id], "supported_trackers": ["sam3_video"]}


def _health_meta(model_id: str) -> dict:
    return {"capabilities": _raw_capabilities(model_id), "gpu_info": {"name": "test"}}


def _pool_facts(name: str, model_id: str, *, enabled: bool, legacy_id, generation: int):
    snapshot = canonicalize_capability(_raw_capabilities(model_id))
    return MLBackendServicePool(
        id=uuid.uuid4(),
        name=name,
        enabled=enabled,
        routing_policy="smooth_weighted_round_robin",
        legacy_instance_id=legacy_id,
        routing_generation=generation,
        capability_fingerprint=capability_fingerprint(snapshot),
        capability_snapshot=snapshot,
    )


async def _seed_world(test_engine) -> dict:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    prefix = f"inference-pool-{uuid.uuid4().hex[:12]}"
    world = {
        "prefix": prefix,
        "aliases": {prefix: "$run"},
        "tokens": {},
        "actors": {},
        "actor_ids": [],
        "project_ids": [],
        "registry_ids": {},
        "pool_ids": set(),
        "pool_names": {},
        "project_pool_ids": [],
        "samples": [],
        "redis_keys": set(),
        "routing_trace": [],
        "probe_trace": [],
        "probe_outcomes": [
            {"healthy": True, "model_id": "drift-new"},
            {"healthy": False, "model_id": None},
            {"healthy": True, "model_id": "drift-new"},
        ],
        "probe_count": 0,
    }

    async with maker.begin() as db:
        admin = await create_user(
            db,
            "super_admin",
            f"{prefix}-admin@example.test",
            "Pool lifecycle admin",
        )
        employee = await create_user(
            db,
            "employee",
            f"{prefix}-employee@example.test",
            "Pool lifecycle employee",
        )
        world["actors"] = {"admin": admin, "employee": employee}
        world["actor_ids"] = [admin.id, employee.id]
        world["aliases"].update(
            {
                str(admin.id): "$actor_admin",
                str(employee.id): "$actor_employee",
            }
        )
        world["tokens"] = {
            alias: create_access_token(str(actor.id), actor.role)
            for alias, actor in world["actors"].items()
        }

        seed_registry = MLBackendRegistry(
            name=f"{prefix}-seed-registry",
            url=f"http://{prefix}-seed.test:9999",
            state="connected",
            is_interactive=False,
            auth_method="none",
            auth_token=None,
            extra_params={},
            health_meta=_health_meta("sam3"),
            source="manual",
        )
        drift_registry = MLBackendRegistry(
            name=f"{prefix}-drift-registry",
            url=f"http://{prefix}-drift.test:9999",
            state="connected",
            is_interactive=False,
            auth_method="none",
            auth_token=None,
            extra_params={},
            health_meta=_health_meta("drift-new"),
            source="manual",
        )
        candidates = {
            "candidate": _health_meta("sam3"),
            "peer": _health_meta("sam3"),
            "mismatch": _health_meta("yolo"),
            "no_capabilities": None,
        }
        registries = {"seed": seed_registry, "drift": drift_registry}
        for alias, health_meta in candidates.items():
            registries[alias] = MLBackendRegistry(
                name=f"{prefix}-{alias}-registry",
                url=f"http://{prefix}-{alias}.test:9999",
                state="connected",
                is_interactive=False,
                auth_method="none",
                auth_token=None,
                extra_params={},
                health_meta=health_meta,
                source="manual",
            )
        db.add_all(registries.values())
        await db.flush()
        world["registry_ids"] = {key: row.id for key, row in registries.items()}
        for alias, row in registries.items():
            world["aliases"][str(row.id)] = f"$registry_{alias}"

        seed_pool = _pool_facts(
            f"{prefix}-seed-pool",
            "sam3",
            enabled=True,
            legacy_id=seed_registry.id,
            generation=5,
        )
        seed_pool.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        seed_pool.updated_at = seed_pool.created_at
        drift_pool = _pool_facts(
            f"{prefix}-drift-pool",
            "sam3",
            enabled=False,
            legacy_id=drift_registry.id,
            generation=9,
        )
        drift_pool.created_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
        drift_pool.updated_at = drift_pool.created_at
        db.add_all([seed_pool, drift_pool])
        await db.flush()
        world["pool_ids"].update({seed_pool.id, drift_pool.id})
        world["pool_names"] = {
            seed_pool.name: "$pool_seed",
            drift_pool.name: "$pool_drift",
        }
        world["aliases"].update(
            {
                str(seed_pool.id): "$pool_seed",
                str(drift_pool.id): "$pool_drift",
            }
        )
        db.add_all(
            [
                MLBackendPoolMember(
                    pool_id=seed_pool.id,
                    registry_id=seed_registry.id,
                    traffic_state="active",
                    weight=1,
                ),
                MLBackendPoolMember(
                    pool_id=drift_pool.id,
                    registry_id=drift_registry.id,
                    traffic_state="disabled",
                    weight=3,
                ),
            ]
        )
        await db.flush()

        project = await create_project(
            db,
            owner_id=admin.id,
            name=f"{prefix} project",
        )
        project.ml_backend_pool_id = seed_pool.id
        association = ProjectMLBackendPool(
            id=uuid.uuid4(),
            project_id=project.id,
            pool_id=seed_pool.id,
            enabled=True,
            default_variants={"model": "preserved"},
        )
        db.add(association)
        await db.flush()
        world["project_ids"].append(project.id)
        world["project_pool_ids"].append(association.id)
        world["aliases"].update(
            {
                str(project.id): "$project_seed",
                str(association.id): "$project_pool_seed",
            }
        )

    world["pool_names"].update(
        {
            f"{prefix}-pool-empty": "$pool_empty",
            f"{prefix}-pool-main": "$pool_main",
        }
    )
    return world


async def _snapshot(maker, world: dict, *, normalize: bool = True) -> dict:
    async with maker() as db:
        pool_rows = (
            (
                await db.execute(
                    text(
                        "SELECT id, name, enabled, routing_policy, legacy_instance_id, "
                        "routing_generation, capability_fingerprint, capability_snapshot, "
                        "created_at, updated_at "
                        "FROM public.ml_backend_service_pools "
                        "WHERE name LIKE :prefix ORDER BY name, id"
                    ),
                    {"prefix": world["prefix"] + "%"},
                )
            )
            .mappings()
            .all()
        )
        for row in pool_rows:
            pool_id = uuid.UUID(str(row["id"]))
            world["pool_ids"].add(pool_id)
            alias = world["pool_names"].get(row["name"])
            if alias is not None:
                world["aliases"][str(pool_id)] = alias

        registry_rows = (
            (
                await db.execute(
                    text(
                        "SELECT id, name, url, state, is_interactive, auth_method, "
                        "extra_params, gpu_resource_id, vram_budget_mb, eviction_priority, "
                        "health_meta, source, error_message, last_checked_at, created_at, updated_at "
                        "FROM public.ml_backend_registry "
                        "WHERE name LIKE :prefix ORDER BY name, id"
                    ),
                    {"prefix": world["prefix"] + "%"},
                )
            )
            .mappings()
            .all()
        )
        registry_ids = []
        for row in registry_rows:
            registry_id = uuid.UUID(str(row["id"]))
            registry_ids.append(str(registry_id))
            alias = next(
                (
                    key
                    for key, value in world["registry_ids"].items()
                    if value == registry_id
                ),
                None,
            )
            if alias is not None:
                world["aliases"][str(registry_id)] = f"$registry_{alias}"

        pool_ids = [str(value) for value in world["pool_ids"]]
        member_rows = (
            (
                await db.execute(
                    text(
                        "SELECT id, pool_id, registry_id, traffic_state, weight, created_at, updated_at "
                        "FROM public.ml_backend_pool_members "
                        "WHERE pool_id = ANY(CAST(:pool_ids AS uuid[])) "
                        "OR registry_id = ANY(CAST(:registry_ids AS uuid[])) "
                        "ORDER BY pool_id, registry_id"
                    ),
                    {"pool_ids": pool_ids, "registry_ids": registry_ids},
                )
            )
            .mappings()
            .all()
        )
        for row in member_rows:
            pool_alias = world["aliases"].get(str(row["pool_id"]), "$pool_unknown")
            registry_alias = world["aliases"].get(
                str(row["registry_id"]), "$registry_unknown"
            )
            stable_alias = (
                f"$member_{pool_alias.removeprefix('$')}_"
                f"{registry_alias.removeprefix('$')}"
            )
            world["aliases"][str(row["id"])] = stable_alias
        project_pool_rows = (
            (
                await db.execute(
                    text(
                        "SELECT id, project_id, pool_id, enabled, default_variants, "
                        "created_at, updated_at FROM public.project_ml_backend_pool "
                        "WHERE project_id = ANY(CAST(:project_ids AS uuid[])) "
                        "ORDER BY project_id, pool_id"
                    ),
                    {"project_ids": [str(value) for value in world["project_ids"]]},
                )
            )
            .mappings()
            .all()
        )
        audit_rows = (
            (
                await db.execute(
                    text(
                        "SELECT actor_id, actor_email, actor_role, action, target_type, "
                        "target_id, method, path, status_code, ip, detail_json, request_id, created_at "
                        "FROM public.audit_logs "
                        "WHERE actor_id = ANY(CAST(:actor_ids AS uuid[])) "
                        "AND action LIKE 'ml_service_pool.%' ORDER BY id"
                    ),
                    {"actor_ids": [str(value) for value in world["actor_ids"]]},
                )
            )
            .mappings()
            .all()
        )
        result = {
            "ml_backend_registry": [dict(row) for row in registry_rows],
            "ml_backend_service_pools": [dict(row) for row in pool_rows],
            "ml_backend_pool_members": [dict(row) for row in member_rows],
            "project_ml_backend_pool": [dict(row) for row in project_pool_rows],
            "business_audit": [dict(row) for row in audit_rows],
        }
        return _normalise_tables(result, world["aliases"]) if normalize else result


def _updated_at_changes(before: dict, after: dict, aliases: dict[str, str]) -> dict:
    changes = {}
    for table in (
        "ml_backend_registry",
        "ml_backend_service_pools",
        "ml_backend_pool_members",
        "project_ml_backend_pool",
    ):
        previous = {str(row["id"]): row for row in before[table]}
        current = {str(row["id"]): row for row in after[table]}
        changes[table] = {
            aliases.get(row_id, row_id): previous[row_id]["updated_at"]
            != current[row_id]["updated_at"]
            for row_id in previous.keys() & current.keys()
        }
    return changes


def _check_status(label: str, response: httpx.Response, expected_status) -> None:
    if isinstance(expected_status, int):
        assert response.status_code == expected_status, (
            f"{label}: expected {expected_status}, got {response.status_code}: "
            f"{response.text}"
        )
    elif expected_status is not None:
        lower, upper = expected_status
        assert lower <= response.status_code <= upper, (
            f"{label}: expected status in {expected_status}, got "
            f"{response.status_code}: {response.text}"
        )


def _assert_generation(body: dict, expected: int, label: str) -> None:
    assert body["routing_generation"] == expected, (
        f"{label}: expected generation {expected}, got {body['routing_generation']}"
    )


def _assert_updated_at_changed(world: dict, table: str, alias: str, expected: bool):
    observed = world["samples"][-1]["updated_at_changed"][table][alias]
    assert observed is expected, (
        f"{world['samples'][-1]['label']}: expected {alias} updated_at change "
        f"to be {expected}, got {observed}"
    )


async def _capture_sample(
    client: httpx.AsyncClient,
    maker,
    world: dict,
    *,
    label: str,
    method: str,
    path: str,
    actor: str | None,
    body=_MISSING,
    response_alias: str | None = None,
    expected_status: int | tuple[int, int] | None = None,
    expected_error_code: str | None = None,
    expected_detail_contains: str | None = None,
    ledger_factory=None,
) -> tuple[httpx.Response, object]:
    before_raw = await _snapshot(maker, world, normalize=False)
    probe_start = len(world["probe_trace"])
    routing_start = len(world["routing_trace"])
    headers = {}
    if actor is not None:
        headers["Authorization"] = f"Bearer {world['tokens'][actor]}"

    from app.services.ml_routing import safety

    previous_factory = safety.make_ledger_from_settings
    if ledger_factory is not None:
        safety.make_ledger_from_settings = ledger_factory
    try:
        request_options = {"headers": headers}
        if body is not _MISSING:
            request_options["json"] = body
        response = await client.request(
            method,
            path,
            **request_options,
        )
    finally:
        safety.make_ledger_from_settings = previous_factory

    try:
        payload = response.json() if response.content else None
    except ValueError:
        payload = response.text
    if (
        response_alias
        and isinstance(payload, dict)
        and isinstance(payload.get("id"), str)
    ):
        pool_id = uuid.UUID(payload["id"])
        world["pool_ids"].add(pool_id)
        world["aliases"][str(pool_id)] = f"${response_alias}"

    after_raw = await _snapshot(maker, world, normalize=False)
    before = _normalise_tables(before_raw, world["aliases"])
    after = _normalise_tables(after_raw, world["aliases"])
    sample_request = {
        "method": method,
        "path": _normalise(path, world["aliases"]),
        "actor_alias": actor or "anonymous",
    }
    if body is not _MISSING:
        sample_request["body"] = _normalise(body, world["aliases"])
    sample = {
        "label": label,
        "request": sample_request,
        "response": {
            "status": response.status_code,
            "headers": {
                name: response.headers[name]
                for name in _RESPONSE_HEADERS
                if name in response.headers
            },
            "body": _normalise(payload, world["aliases"]),
        },
        "tables_before": before,
        "tables_after": after,
        "updated_at_changed": _updated_at_changes(
            before_raw, after_raw, world["aliases"]
        ),
        "controlled_health_probe": _normalise(
            world["probe_trace"][probe_start:], world["aliases"]
        ),
        "routing_ledger": _normalise(
            world["routing_trace"][routing_start:], world["aliases"]
        ),
    }
    world["samples"].append(sample)
    _check_status(label, response, expected_status)
    if expected_error_code is not None:
        assert isinstance(payload, dict) and isinstance(payload.get("detail"), dict), (
            f"{label}: expected structured error {expected_error_code}, got {payload!r}"
        )
        assert payload["detail"].get("error_code") == expected_error_code, (
            f"{label}: expected error {expected_error_code}, got {payload!r}"
        )
    if expected_detail_contains is not None:
        assert isinstance(payload, dict) and expected_detail_contains in str(
            payload.get("detail")
        ), (
            f"{label}: expected detail containing {expected_detail_contains!r}, got {payload!r}"
        )
    return response, payload


async def _fake_check_health(self, registry_id: uuid.UUID, **_kwargs) -> bool:
    world = _ACTIVE_WORLD.get()
    assert world is not None
    index = world["probe_count"]
    assert index < len(world["probe_outcomes"]), "unexpected health probe"
    outcome = world["probe_outcomes"][index]
    world["probe_count"] += 1
    world["probe_trace"].append(
        {
            "source": "controlled-MLBackendService.check_health-stub",
            "registry_id": str(registry_id),
            **outcome,
        }
    )
    registry = await self.get(registry_id)
    if registry is None:
        return False
    registry.state = "connected" if outcome["healthy"] else "error"
    registry.last_checked_at = datetime.now(timezone.utc)
    registry.health_meta = (
        _health_meta(outcome["model_id"]) if outcome["healthy"] else None
    )
    await self.db.flush()
    if outcome["healthy"]:
        await self._reconcile_pool_capability(
            registry_id, registry.health_meta["capabilities"]
        )
        await self.db.flush()
    return bool(outcome["healthy"])


_ACTIVE_WORLD = contextvars.ContextVar(
    "inference_pool_legacy_active_world", default=None
)


class _UnavailableLedger:
    def __init__(self, world: dict) -> None:
        self.world = world
        world["routing_trace"].append(
            {"event": "ledger_created", "kind": "unavailable-ledger"}
        )

    async def healthcheck(self) -> None:
        self.world["routing_trace"].append({"event": "healthcheck", "result": "error"})
        raise ConnectionError("controlled unavailable ledger")

    async def member_inflight(self, pool_id: str, registry_id: str) -> int:
        self.world["routing_trace"].append(
            {
                "event": "member_inflight",
                "pool_id": pool_id,
                "registry_id": registry_id,
                "result": "unexpected",
            }
        )
        raise AssertionError("unavailable healthcheck must stop before member lookup")

    async def aclose(self) -> None:
        self.world["routing_trace"].append({"event": "aclose"})


class _TracingRedisLedger:
    def __init__(self, world: dict) -> None:
        assert settings.redis_url == os.environ["REDIS_URL"]
        self.world = world
        self.ledger = RoutingLedger.from_url(settings.redis_url)
        world["routing_trace"].append(
            {"event": "ledger_created", "kind": "owned-redis"}
        )

    async def healthcheck(self) -> None:
        await self.ledger.healthcheck()
        self.world["routing_trace"].append({"event": "healthcheck", "result": "ok"})

    async def member_inflight(self, pool_id: str, registry_id: str) -> int:
        count = await self.ledger.member_inflight(pool_id, registry_id)
        self.world["redis_keys"].add(
            f"ml-router:v1:pool:{pool_id}:member:{registry_id}:leases"
        )
        self.world["routing_trace"].append(
            {
                "event": "member_inflight",
                "pool_id": pool_id,
                "registry_id": registry_id,
                "inflight": count,
            }
        )
        return count

    async def aclose(self) -> None:
        await self.ledger.aclose()
        self.world["routing_trace"].append({"event": "aclose"})


def _ledger_identity() -> dict:
    url = _verify_redis()
    assert settings.redis_url == os.environ["REDIS_URL"], (
        "the router ledger must use the verifier's owned disposable Redis"
    )
    return {
        "host": url.host,
        "port": url.port,
        "container_name": _required_env("AAP_MODULE_DATA_REDIS_CONTAINER_NAME"),
        "owner_label": _required_env("AAP_MODULE_DATA_REDIS_OWNER"),
        "namespace": "ml-router:v1",
    }


async def _cleanup_owned_redis(world: dict) -> None:
    if not world["redis_keys"]:
        return
    _verify_redis()
    assert settings.redis_url == os.environ["REDIS_URL"]
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        keys = sorted(world["redis_keys"])
        # Delete only exact pool/member lease keys observed during this test.
        await client.delete(*keys)
    finally:
        await client.aclose()


async def _cleanup_world(test_engine, world: dict) -> None:
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
        actor_ids = world["actor_ids"]
        project_ids = world["project_ids"]
        pool_ids = list(world["pool_ids"])
        registry_ids = list(world["registry_ids"].values())
        if actor_ids:
            await db.execute(
                delete(AuditLog).where(
                    AuditLog.actor_id.in_(actor_ids),
                    AuditLog.action.like("ml_service_pool.%"),
                )
            )
        if world["project_pool_ids"]:
            await db.execute(
                delete(ProjectMLBackendPool).where(
                    ProjectMLBackendPool.id.in_(world["project_pool_ids"])
                )
            )
        if project_ids:
            await db.execute(delete(Project).where(Project.id.in_(project_ids)))
        if pool_ids or registry_ids:
            await db.execute(
                delete(MLBackendPoolMember).where(
                    MLBackendPoolMember.pool_id.in_(pool_ids)
                    | MLBackendPoolMember.registry_id.in_(registry_ids)
                )
            )
        if pool_ids:
            await db.execute(
                delete(MLBackendServicePool).where(
                    MLBackendServicePool.id.in_(pool_ids)
                )
            )
        if registry_ids:
            await db.execute(
                delete(MLBackendRegistry).where(MLBackendRegistry.id.in_(registry_ids))
            )
        if actor_ids:
            await db.execute(delete(User).where(User.id.in_(actor_ids)))


async def _run_scenarios(client, maker, world, monkeypatch) -> list[dict]:
    from app.services.ml_backend import MLBackendService

    token = _ACTIVE_WORLD.set(world)
    monkeypatch.setattr(MLBackendService, "check_health", _fake_check_health)
    base = "/api/v1/admin/ml-integrations/service-pools"

    async def request(label, method, path, actor="admin", *, body=_MISSING, **kwargs):
        _response, payload = await _capture_sample(
            client,
            maker,
            world,
            label=label,
            method=method,
            path=path,
            actor=actor,
            body=body,
            **kwargs,
        )
        return payload

    try:
        await request(
            "list_without_authentication",
            "GET",
            base,
            actor=None,
            expected_status=401,
        )
        await request(
            "list_for_employee",
            "GET",
            base,
            actor="employee",
            expected_status=403,
        )

        missing_pool = uuid.uuid4()
        missing_registry = uuid.uuid4()
        world["aliases"].update(
            {
                str(missing_pool): "$pool_missing",
                str(missing_registry): "$registry_missing",
            }
        )
        await request(
            "get_missing_pool",
            "GET",
            f"{base}/{missing_pool}",
            expected_status=404,
        )
        await request(
            "patch_missing_pool",
            "PATCH",
            f"{base}/{missing_pool}",
            body={"name": "ignored"},
            expected_status=404,
        )
        await request(
            "delete_missing_pool",
            "DELETE",
            f"{base}/{missing_pool}",
            expected_status=404,
        )

        seed_registry = world["registry_ids"]["seed"]
        seed_pool = next(
            pool_id
            for pool_id in world["pool_ids"]
            if world["aliases"].get(str(pool_id)) == "$pool_seed"
        )
        drift_registry = world["registry_ids"]["drift"]
        drift_pool = next(
            pool_id
            for pool_id in world["pool_ids"]
            if world["aliases"].get(str(pool_id)) == "$pool_drift"
        )
        candidate_registry = world["registry_ids"]["candidate"]
        mismatch_registry = world["registry_ids"]["mismatch"]
        peer_registry = world["registry_ids"]["peer"]
        no_cap_registry = world["registry_ids"]["no_capabilities"]

        invalid_legacy_id = uuid.uuid4()
        world["aliases"][str(invalid_legacy_id)] = "$registry_unowned"
        await request(
            "create_rejects_unowned_legacy_registry",
            "POST",
            base,
            body={
                "name": f"{world['prefix']}-invalid-legacy",
                "legacy_instance_id": str(invalid_legacy_id),
            },
            expected_status=409,
            expected_detail_contains="legacy_instance_id cannot be set",
        )

        empty_name = f"{world['prefix']}-pool-empty"
        world["pool_names"][empty_name] = "$pool_empty"
        empty = await request(
            "create_empty_disabled_pool",
            "POST",
            base,
            body={"name": empty_name},
            response_alias="pool_empty",
            expected_status=201,
        )
        _assert_generation(empty, 1, "empty pool create")
        empty_id = uuid.UUID(empty["id"])
        empty_path = f"{base}/{empty_id}"
        await request(
            "get_empty_pool",
            "GET",
            empty_path,
            expected_status=200,
        )
        enable_empty = await request(
            "empty_pool_cannot_be_enabled",
            "PATCH",
            empty_path,
            body={"enabled": True},
            expected_status=409,
            expected_detail_contains="cannot enable a pool with no legacy instance",
        )
        assert enable_empty["detail"] == (
            "cannot enable a pool with no legacy instance; add a member first (D15)"
        )
        noop_patch = await request(
            "empty_patch_is_noop",
            "PATCH",
            empty_path,
            body={},
            expected_status=200,
        )
        _assert_generation(noop_patch, 1, "empty no-op patch")
        _assert_updated_at_changed(
            world, "ml_backend_service_pools", "$pool_empty", False
        )
        null_patch = await request(
            "null_patch_fields_are_noop",
            "PATCH",
            empty_path,
            body={"name": None, "enabled": None},
            expected_status=200,
        )
        _assert_generation(null_patch, 1, "null patch")
        _assert_updated_at_changed(
            world, "ml_backend_service_pools", "$pool_empty", False
        )
        empty_renamed = f"{world['prefix']}-pool-empty-renamed"
        world["pool_names"][empty_renamed] = "$pool_empty"
        renamed = await request(
            "patch_pool_name_increments_generation",
            "PATCH",
            empty_path,
            body={"name": empty_renamed},
            expected_status=200,
        )
        _assert_generation(renamed, 2, "changed pool name")
        _assert_updated_at_changed(
            world, "ml_backend_service_pools", "$pool_empty", True
        )
        unchanged_name = await request(
            "repeat_same_name_does_not_increment_generation",
            "PATCH",
            empty_path,
            body={"name": empty_renamed},
            expected_status=200,
        )
        _assert_generation(unchanged_name, 2, "same pool name")
        _assert_updated_at_changed(
            world, "ml_backend_service_pools", "$pool_empty", False
        )

        main_name = f"{world['prefix']}-pool-main"
        world["pool_names"][main_name] = "$pool_main"
        main = await request(
            "create_second_empty_pool",
            "POST",
            base,
            body={"name": main_name},
            response_alias="pool_main",
            expected_status=201,
        )
        main_id = uuid.UUID(main["id"])
        _assert_generation(main, 1, "second empty pool create")
        main_path = f"{base}/{main_id}"
        first_no_cap = await request(
            "first_member_without_capability_becomes_legacy",
            "PUT",
            f"{main_path}/members/{no_cap_registry}",
            body={"weight": 1},
            expected_status=200,
        )
        _assert_generation(first_no_cap, 2, "first member on empty pool")
        assert first_no_cap["legacy_instance_id"] == str(no_cap_registry)
        assert first_no_cap["capability_fingerprint"] is None
        no_cap_rejected = await request(
            "second_member_rejected_without_capability_baseline",
            "PUT",
            f"{main_path}/members/{peer_registry}",
            body={"weight": 1},
            expected_status=409,
            expected_error_code="ml_backend_pool_capability_unavailable",
        )
        assert no_cap_rejected["detail"]["error_code"] == (
            "ml_backend_pool_capability_unavailable"
        )

        seed_path = f"{base}/{seed_pool}"
        first_candidate = await request(
            "matching_capability_member_added",
            "PUT",
            f"{seed_path}/members/{candidate_registry}",
            body={"weight": 1},
            expected_status=200,
        )
        _assert_generation(first_candidate, 6, "first matching member")
        assert first_candidate["legacy_instance_id"] == str(seed_registry)
        assert first_candidate["capability_fingerprint"]
        repeated_candidate = await request(
            "identical_member_upsert_does_not_change_generation",
            "PUT",
            f"{seed_path}/members/{candidate_registry}",
            body={"weight": 1},
            expected_status=200,
        )
        _assert_generation(repeated_candidate, 6, "idempotent member upsert")
        _assert_updated_at_changed(
            world, "ml_backend_service_pools", "$pool_seed", False
        )
        drained_candidate = await request(
            "drain_member",
            "POST",
            f"{seed_path}/members/{candidate_registry}/drain",
            expected_status=200,
        )
        _assert_generation(drained_candidate, 7, "drain member")
        weighted_candidate = await request(
            "weight_upsert_preserves_draining_state",
            "PUT",
            f"{seed_path}/members/{candidate_registry}",
            body={"weight": 7},
            expected_status=200,
        )
        _assert_generation(weighted_candidate, 8, "weight update while draining")
        candidate_member = next(
            item
            for item in weighted_candidate["members"]
            if item["registry_id"] == str(candidate_registry)
        )
        assert candidate_member["traffic_state"] == "draining"
        assert candidate_member["weight"] == 7
        repeated_weight = await request(
            "identical_draining_weight_upsert_is_noop",
            "PUT",
            f"{seed_path}/members/{candidate_registry}",
            body={"weight": 7},
            expected_status=200,
        )
        _assert_generation(repeated_weight, 8, "idempotent draining weight")
        repeated_member = next(
            item
            for item in repeated_weight["members"]
            if item["registry_id"] == str(candidate_registry)
        )
        assert repeated_member == candidate_member
        resumed_candidate = await request(
            "resume_draining_member",
            "POST",
            f"{seed_path}/members/{candidate_registry}/resume",
            expected_status=200,
        )
        _assert_generation(resumed_candidate, 9, "resume member")
        resumed_member = next(
            item
            for item in resumed_candidate["members"]
            if item["registry_id"] == str(candidate_registry)
        )
        assert resumed_member["traffic_state"] == "active"
        mismatch, _ = await _capture_sample(
            client,
            maker,
            world,
            label="mismatched_capability_rejected_with_structured_diff",
            method="PUT",
            path=f"{seed_path}/members/{mismatch_registry}",
            actor="admin",
            body={"weight": 1},
            expected_status=409,
            expected_error_code="ml_backend_pool_capability_mismatch",
        )
        assert isinstance(mismatch.json()["detail"].get("differing_fields"), list)

        stale_review = await request(
            "drift_preview_missing_member",
            "GET",
            f"{seed_path}/members/{missing_registry}/capability-drift",
            expected_status=404,
        )
        preview = await request(
            "drift_preview_reports_reviewable_difference",
            "GET",
            f"{base}/{drift_pool}/members/{drift_registry}/capability-drift",
            expected_status=200,
        )
        assert preview["has_drift"] is True and preview["can_accept"] is True
        assert preview["differing_fields"] == ["model_ids"]
        stale_review = await request(
            "stale_drift_review_rejected_after_health_probe",
            "POST",
            f"{base}/{drift_pool}/members/{drift_registry}/capability-drift/accept",
            body={"expected_candidate_fingerprint": "0" * 64, "enable_pool": True},
            expected_status=409,
            expected_error_code="ml_backend_capability_changed_during_review",
        )
        assert stale_review["detail"]["error_code"] == (
            "ml_backend_capability_changed_during_review"
        )
        unhealthy_review = await request(
            "unhealthy_drift_probe_rejected",
            "POST",
            f"{base}/{drift_pool}/members/{drift_registry}/capability-drift/accept",
            body={
                "expected_candidate_fingerprint": preview["candidate_fingerprint"],
                "enable_pool": True,
            },
            expected_status=409,
            expected_error_code="ml_backend_capability_revalidation_failed",
        )
        assert unhealthy_review["detail"]["error_code"] == (
            "ml_backend_capability_revalidation_failed"
        )
        accepted_drift = await request(
            "fresh_drift_review_accepted",
            "POST",
            f"{base}/{drift_pool}/members/{drift_registry}/capability-drift/accept",
            body={
                "expected_candidate_fingerprint": preview["candidate_fingerprint"],
                "enable_pool": True,
            },
            expected_status=200,
        )
        _assert_generation(accepted_drift, 10, "accepted capability drift")

        await request(
            "registry_cannot_be_owned_by_two_pools",
            "PUT",
            f"{main_path}/members/{candidate_registry}",
            body={"weight": 1},
            expected_status=409,
            expected_error_code="ml_backend_pool_member_conflict",
        )
        await request(
            "pool_delete_blocked_while_members_remain",
            "DELETE",
            seed_path,
            expected_status=409,
            expected_detail_contains="remove every service-pool member",
        )

        monkeypatch.setattr(settings, "ml_backend_router_mode", "enforce")
        drained_legacy = await request(
            "drain_legacy_member_before_removal",
            "POST",
            f"{seed_path}/members/{seed_registry}/drain",
            expected_status=200,
        )
        _assert_generation(drained_legacy, 10, "drain legacy member")
        unavailable = await request(
            "unavailable_ledger_fails_closed_and_closes",
            "DELETE",
            f"{seed_path}/members/{seed_registry}",
            expected_status=503,
            expected_error_code="ml_backend_router_unavailable",
            ledger_factory=lambda: _UnavailableLedger(world),
        )
        assert unavailable["detail"]["error_code"] == "ml_backend_router_unavailable"
        assert world["routing_trace"][-3:] == [
            {"event": "ledger_created", "kind": "unavailable-ledger"},
            {"event": "healthcheck", "result": "error"},
            {"event": "aclose"},
        ]

        _ledger_identity()
        removed = await request(
            "real_owned_redis_legacy_removal_commits_before_response_500",
            "DELETE",
            f"{seed_path}/members/{seed_registry}",
            expected_status=200,
            ledger_factory=lambda: _TracingRedisLedger(world),
        )
        assert removed["legacy_instance_id"] is None
        assert removed["enabled"] is False
        last = world["samples"][-1]["tables_after"]["ml_backend_service_pools"]
        owned_seed = next(row for row in last if row["id"] == "$pool_seed")
        assert owned_seed["legacy_instance_id"] is None
        assert owned_seed["enabled"] is False
        prior = world["samples"][-1]["tables_before"]["ml_backend_service_pools"]
        prior_seed = next(row for row in prior if row["id"] == "$pool_seed")
        assert owned_seed["routing_generation"] == prior_seed["routing_generation"] + 1
        before_association = world["samples"][-1]["tables_before"][
            "project_ml_backend_pool"
        ]
        after_association = world["samples"][-1]["tables_after"][
            "project_ml_backend_pool"
        ]
        assert after_association == before_association
        removal_sample = world["samples"][-1]
        assert len(removal_sample["tables_after"]["business_audit"]) == (
            len(removal_sample["tables_before"]["business_audit"]) + 1
        )
        assert removal_sample["tables_after"]["business_audit"][-1]["action"] == (
            "ml_service_pool.member_removed"
        )
        assert world["routing_trace"][-7:] == [
            {"event": "ledger_created", "kind": "unavailable-ledger"},
            {"event": "healthcheck", "result": "error"},
            {"event": "aclose"},
            {"event": "ledger_created", "kind": "owned-redis"},
            {"event": "healthcheck", "result": "ok"},
            {
                "event": "member_inflight",
                "pool_id": str(seed_pool),
                "registry_id": str(seed_registry),
                "inflight": 0,
            },
            {"event": "aclose"},
        ]

        readback = await request(
            "fresh_get_confirms_removal_committed_despite_response_failure",
            "GET",
            seed_path,
            expected_status=200,
        )
        assert readback["legacy_instance_id"] is None
        assert readback["enabled"] is False
        assert readback["routing_generation"] == owned_seed["routing_generation"]

        await request(
            "delete_empty_pool_succeeds",
            "DELETE",
            empty_path,
            expected_status=204,
        )
        await request(
            "second_pool_delete_blocked_with_member",
            "DELETE",
            main_path,
            expected_status=409,
            expected_detail_contains="remove every service-pool member",
        )
        assert len(world["samples"]) >= 30
        assert world["probe_count"] == 3
        await request(
            "list_service_pools_as_admin",
            "GET",
            base,
            expected_status=200,
        )
        await request(
            "get_pool_after_legacy_removal",
            "GET",
            seed_path,
            expected_status=200,
        )
        return world["samples"]
    finally:
        _ACTIVE_WORLD.reset(token)


async def _exercise_owned_http(test_engine, app_module, monkeypatch) -> dict:
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_engine = None
    world = None
    active_error = None
    result = None
    await assert_owned_test_database(test_engine)
    ledger_identity = _ledger_identity()
    clear_local_identity(monkeypatch)
    try:
        world = await _seed_world(test_engine)
        runtime_engine, runtime_maker = await make_request_runtime(
            test_engine,
            application_name="aap-inference-pool-legacy-baseline",
        )
        async with runtime_engine.connect() as connection:
            role, database = (
                await connection.execute(
                    text("SELECT current_user, current_database()")
                )
            ).one()
        assert role == "legacy_runtime"
        assert database == test_engine.url.database
        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        async with api_db_binding(app_module, runtime_maker):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                timeout=30,
            ) as client:
                samples = await _run_scenarios(client, owner_maker, world, monkeypatch)
        result = {
            "samples": samples,
            "runtime_identity": {
                "database_role": role,
                "database": "$owned_test_database",
                "postgres_container_id": _required_env(
                    "AAP_MODULE_DATA_PG_CONTAINER_ID"
                ),
                "postgres_container_identity": _required_env(
                    "AAP_MODULE_DATA_PG_CONTAINER_IDENTITY"
                ),
            },
            "redis_identity": ledger_identity,
        }
    except BaseException as error:
        active_error = error
        raise
    finally:
        cleanup_errors = []
        if world is not None:
            for cleanup in (_cleanup_owned_redis,):
                try:
                    await cleanup(world)
                except Exception as error:
                    cleanup_errors.append(error)
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
                    f"inference-pool legacy capture/replay cleanup failed: {error!r}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        elif cleanup_errors:
            raise cleanup_errors[0]
    assert result is not None
    return result


def _fixture_data() -> dict:
    if not _FIXTURE_PATH.is_file():
        pytest.skip("Root has not frozen the service-pool legacy samples fixture yet")
    raw = _FIXTURE_PATH.read_bytes()
    expected = os.environ.get("AAP_INFERENCE_POOLS_LEGACY_SHA256")
    assert expected, "Root verifier must pin AAP_INFERENCE_POOLS_LEGACY_SHA256"
    assert hashlib.sha256(raw).hexdigest() == expected
    value = json.loads(raw)
    assert value.get("capture_kind") == "actual-legacy-inference-pool-http-database"
    assert isinstance(value.get("samples"), list) and len(value["samples"]) >= 30
    return value


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
    or os.environ.get("AAP_INFERENCE_POOLS_LEGACY_CAPTURE") != "1",
    reason="requires explicit capture against owned disposable PostgreSQL and Redis",
)
async def test_capture_legacy_inference_pool_lifecycle_http_database(
    test_engine, app_module, monkeypatch
):
    output = _capture_path()
    source = _source_metadata()
    live = await _exercise_owned_http(test_engine, app_module, monkeypatch)
    capture = {
        "capture_kind": "actual-legacy-inference-pool-http-database",
        "source": source,
        "runtime_identity": live["runtime_identity"],
        "redis_identity": live["redis_identity"],
        "health_probe": {
            "kind": "controlled-MLBackendService.check_health stub",
            "network_calls": 0,
        },
        "observations": [
            "public HTTP response status, selected headers, and JSON body",
            "before/after rows from ml_backend_registry, ml_backend_service_pools, ",
            "ml_backend_pool_members, project_ml_backend_pool, and business AuditLog",
            "real owned Redis healthcheck/member_inflight for legacy removal",
            "UnavailableLedger fail-closed response and aclose trace",
        ],
        "samples": live["samples"],
    }
    encoded = json.dumps(capture, ensure_ascii=False, indent=2) + "\n"
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(encoded)
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires owned disposable PostgreSQL and Redis for legacy replay",
)
async def test_replay_frozen_legacy_inference_pool_lifecycle(
    test_engine, app_module, monkeypatch
):
    fixture = _fixture_data()
    live = await _exercise_owned_http(test_engine, app_module, monkeypatch)
    expected = deepcopy(fixture["samples"])
    by_label = {sample["label"]: sample for sample in expected}
    removal = by_label["real_owned_redis_legacy_removal_commits_before_response_500"]
    assert removal["response"] == {
        "status": 500,
        "headers": {"content-type": "text/plain; charset=utf-8"},
        "body": "Internal Server Error",
    }
    readback = by_label["fresh_get_confirms_removal_committed_despite_response_failure"]
    assert removal["tables_after"] == readback["tables_before"]
    # The explicit postcommit refresh fix changes only this response. Keep the
    # captured error bytes and all old data/Audit/lease observations immutable.
    removal["response"] = deepcopy(readback["response"])
    assert live["samples"] == expected
