"""Capture and replay actual pre-extraction named-pipeline HTTP behavior."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import warnings

import httpx
import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.organization import Organization, OrganizationMember
from app.db.models.project import Project
from app.db.models.user import User
from tests.conftest import create_registry_with_pool
from tests.factory import create_membership, create_project, create_user
from tests.labeling_class_query_helpers import (
    api_db_binding,
    clear_local_identity,
    make_request_runtime,
)
from tests.test_labeling_class_usage_reads_legacy import _legacy_runtime_url


_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "6dee1a78753a302c5cd04c24a63c40f7e6040b6e"
_FIXTURE_PATH = Path(__file__).parent / "fixtures/named_pipeline_legacy/samples.json"
_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_TIMESTAMP_FIELDS = {"created_at", "updated_at", "joined_at"}
_MISSING = object()
_SOURCE_FILES = """
apps/api/app/api/v1/project_pipelines.py
apps/api/app/api/v1/projects.py
apps/api/app/api/v1/router.py
apps/api/app/application/project_pipeline.py
apps/api/app/core/security.py
apps/api/app/db/models/ml_backend_pool.py
apps/api/app/db/models/ml_backend_registry.py
apps/api/app/db/models/organization.py
apps/api/app/db/models/project.py
apps/api/app/db/models/project_member.py
apps/api/app/db/models/project_pipeline.py
apps/api/app/db/models/user.py
apps/api/app/deps.py
apps/api/app/schemas/project_pipeline.py
apps/api/app/services/identity_account_lock_data.py
apps/api/app/services/identity_contracts.py
apps/api/app/services/identity_gateway.py
apps/api/app/services/identity_organization_scope.py
apps/api/app/services/ml_backend.py
apps/api/app/services/pipeline_template.py
apps/api/app/services/project_access.py
apps/api/app/config.py
apps/api/tests/factory.py
apps/api/tests/conftest.py
apps/api/alembic/versions/0112_project_pipelines.py
apps/inference/src/aap_inference/pipeline.py
apps/inference/src/aap_inference/preannotate.py
apps/_shared/task_contract/src/aap_task_contract/__init__.py
""".split()


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"the owned named-pipeline verifier did not provide {name}"
    return value


def _capture_path() -> Path:
    requested = Path(_required_env("AAP_NAMED_PIPELINE_LEGACY_OUTPUT"))
    assert requested.is_absolute(), "capture output must be an absolute path"
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
        assert current == baseline, f"legacy named-pipeline source changed: {relative}"
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


def _normalise(value, aliases: dict[str, str], *, field: str | None = None):
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


async def _owned_pipeline_snapshot(maker, world: dict) -> list[dict]:
    async with maker() as db:
        discovered = await db.execute(
            text(
                "SELECT id, name FROM public.project_pipelines "
                "WHERE created_by = ANY(CAST(:actor_ids AS uuid[])) "
                "AND name LIKE :name_prefix"
            ),
            {
                "actor_ids": [str(value) for value in world["actor_ids"]],
                "name_prefix": world["prefix"] + "%",
            },
        )
        for row in discovered.mappings():
            pipeline_id = uuid.UUID(str(row["id"]))
            world["pipeline_ids"].add(pipeline_id)
            alias = world["pipeline_names"].get(row["name"])
            if alias and str(pipeline_id) not in world["aliases"]:
                world["aliases"][str(pipeline_id)] = alias

        if not world["pipeline_ids"]:
            return []
        rows = await db.execute(
            text(
                "SELECT id, scope, project_id, organization_id, name, stages, "
                "is_default, created_by, usage_count, created_at, updated_at "
                "FROM public.project_pipelines "
                "WHERE id = ANY(CAST(:pipeline_ids AS uuid[])) ORDER BY name, id"
            ),
            {"pipeline_ids": [str(value) for value in world["pipeline_ids"]]},
        )
        return [dict(row) for row in rows.mappings().all()]


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
    expected_status: int | tuple[int, int] | None = None,
) -> tuple[httpx.Response, object]:
    before_rows = await _owned_pipeline_snapshot(maker, world)
    response = await client.request(
        method,
        path,
        headers={"Authorization": f"Bearer {world['tokens'][actor]}"},
        json=None if body is _MISSING else body,
        params=params,
    )

    try:
        payload = response.json() if response.content else None
    except ValueError:
        payload = response.text
    if (
        response_alias
        and isinstance(payload, dict)
        and isinstance(payload.get("id"), str)
    ):
        pipeline_id = uuid.UUID(payload["id"])
        world["pipeline_ids"].add(pipeline_id)
        world["aliases"][str(pipeline_id)] = response_alias

    after_rows = await _owned_pipeline_snapshot(maker, world)
    sample_request = {
        "method": method,
        "path": _normalise_text(path, world["aliases"]),
        "actor_alias": actor,
    }
    if body is not _MISSING:
        sample_request["body"] = _normalise(body, world["aliases"])
    if params is not None:
        sample_request["query"] = _normalise(params, world["aliases"])
    before_by_id = {str(row["id"]): row for row in before_rows}
    after_by_id = {str(row["id"]): row for row in after_rows}
    timestamp_changes = {
        world["aliases"].get(pipeline_id, pipeline_id): (
            before_by_id[pipeline_id]["updated_at"]
            != after_by_id[pipeline_id]["updated_at"]
        )
        for pipeline_id in before_by_id.keys() & after_by_id.keys()
    }
    sample = {
        "label": label,
        "request": sample_request,
        "response": {
            "status": response.status_code,
            "headers": {
                name: response.headers[name]
                for name in _HEADERS
                if name in response.headers
            },
            "body": _normalise(payload, world["aliases"]),
        },
        "database_before": _normalise(before_rows, world["aliases"]),
        "database_after": _normalise(after_rows, world["aliases"]),
        "database_updated_at_changed": timestamp_changes,
    }
    world["samples"].append(sample)
    if isinstance(expected_status, int):
        assert response.status_code == expected_status, (
            f"{label}: expected {expected_status}, got {response.status_code}: "
            f"{response.text}"
        )
    elif expected_status is not None:
        lower, upper = expected_status
        assert lower <= response.status_code <= upper, (
            f"{label}: expected status in {expected_status}, "
            f"got {response.status_code}: {response.text}"
        )
    return response, payload


def _pipeline_stages(backend_id: uuid.UUID) -> list[dict]:
    return [
        {"stage": 0, "ml_backend_id": str(backend_id), "model_id": "detect"},
        {
            "stage": 1,
            "ml_backend_id": str(backend_id),
            "model_id": "vehicle-attr",
            "task_type": "classification",
            "parent_stage": 0,
            "roi": {"mode": "crop", "pad": 0.05},
            "write": {"target": "attributes", "keys": ["color"]},
        },
    ]


def _canonical_stages(backend_id: uuid.UUID) -> list[dict]:
    result = _pipeline_stages(backend_id)
    result[1]["task"] = "classify"
    result[1].pop("task_type")
    return result


async def _seed_world(test_engine) -> dict:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    prefix = f"named-pipeline-{uuid.uuid4().hex[:12]}"
    world = {
        "prefix": prefix,
        "aliases": {prefix: "$run"},
        "tokens": {},
        "actor_ids": [],
        "project_ids": [],
        "organization_ids": [],
        "organization_member_ids": [],
        "pipeline_ids": set(),
        "pipeline_names": {},
        "backend_ids": [],
        "pool_ids": [],
        "backend_binding_ids": [],
        "samples": [],
    }

    async with maker.begin() as db:
        actors = {
            "super_admin": await create_user(
                db,
                "super_admin",
                f"{prefix}-super-admin@example.test",
                "Pipeline super administrator",
            ),
            "owner": await create_user(
                db,
                "project_admin",
                f"{prefix}-owner@example.test",
                "Pipeline project owner",
            ),
            "other_owner": await create_user(
                db,
                "project_admin",
                f"{prefix}-other-owner@example.test",
                "Pipeline other owner",
            ),
            "org_member": await create_user(
                db,
                "employee",
                f"{prefix}-org-member@example.test",
                "Pipeline organization member",
            ),
            "outsider": await create_user(
                db,
                "employee",
                f"{prefix}-outsider@example.test",
                "Pipeline outsider",
            ),
            "demoted_owner": await create_user(
                db,
                "project_admin",
                f"{prefix}-demoted-owner@example.test",
                "Pipeline demoted creator",
            ),
        }
        world["actors"] = actors
        for actor_alias, actor in actors.items():
            world["actor_ids"].append(actor.id)
            world["aliases"][str(actor.id)] = f"$actor_{actor_alias}"
            world["tokens"][actor_alias] = create_access_token(
                str(actor.id), actor.role
            )

        projects = {
            "main": await create_project(
                db, owner_id=actors["owner"].id, name=f"{prefix} main"
            ),
            "target": await create_project(
                db, owner_id=actors["owner"].id, name=f"{prefix} target"
            ),
            "other": await create_project(
                db, owner_id=actors["other_owner"].id, name=f"{prefix} other"
            ),
            "demoted": await create_project(
                db, owner_id=actors["demoted_owner"].id, name=f"{prefix} demoted"
            ),
        }
        world["projects"] = projects
        for project_alias, project in projects.items():
            world["project_ids"].append(project.id)
            world["aliases"][str(project.id)] = f"$project_{project_alias}"

        await create_membership(
            db,
            project_id=projects["main"].id,
            user_id=actors["org_member"].id,
            role="annotator",
            assigned_by=actors["owner"].id,
        )

        organization = Organization(
            id=uuid.uuid4(),
            name=f"{prefix} organization",
            slug=prefix,
            created_by=actors["other_owner"].id,
        )
        db.add(organization)
        await db.flush()
        world["organization"] = organization
        world["organization_ids"].append(organization.id)
        world["aliases"][str(organization.id)] = "$organization_main"

        for actor_alias in ("other_owner", "org_member"):
            membership = OrganizationMember(
                id=uuid.uuid4(),
                organization_id=organization.id,
                user_id=actors[actor_alias].id,
                role="member",
            )
            db.add(membership)
            await db.flush()
            world["organization_member_ids"].append(membership.id)

        backend, pool = await create_registry_with_pool(
            db,
            name=f"{prefix} inference backend",
            url=f"http://{prefix}.test:9999",
            state="connected",
            is_interactive=False,
        )
        world["backend_ids"].append(backend.id)
        world["pool_ids"].append(pool.id)
        world["aliases"][str(backend.id)] = "$backend_enabled"
        for project_alias in ("main", "target"):
            binding = ProjectMLBackendPool(
                id=uuid.uuid4(),
                project_id=projects[project_alias].id,
                pool_id=pool.id,
                enabled=True,
            )
            db.add(binding)
            await db.flush()
            world["backend_binding_ids"].append(binding.id)

        missing_backend = uuid.uuid5(uuid.NAMESPACE_URL, prefix + ":missing-backend")
        world["missing_backend"] = missing_backend
        world["aliases"][str(missing_backend)] = "$backend_missing"

        historical_invalid = uuid.uuid4()
        historical_name = prefix + "-historical-invalid"
        await db.execute(
            text(
                "INSERT INTO public.project_pipelines "
                "(id, scope, project_id, organization_id, name, stages, is_default, "
                "created_by, usage_count) "
                "VALUES (CAST(:id AS uuid), 'public', NULL, NULL, :name, "
                "CAST(:stages AS jsonb), false, CAST(:creator AS uuid), 0)"
            ),
            {
                "id": str(historical_invalid),
                "name": historical_name,
                "stages": json.dumps(
                    [{"stage": 0, "ml_backend_id": "not-a-uuid"}],
                    ensure_ascii=True,
                    allow_nan=False,
                ),
                "creator": str(actors["super_admin"].id),
            },
        )
        world["pipeline_ids"].add(historical_invalid)
        world["pipeline_names"][historical_name] = "$pipeline_historical_invalid"
        world["aliases"][str(historical_invalid)] = "$pipeline_historical_invalid"

    return world


async def _cleanup_world(test_engine, world: dict) -> None:
    from app.db.models.ml_backend_pool import (
        MLBackendPoolMember,
        MLBackendServicePool,
    )
    from app.db.models.ml_backend_registry import MLBackendRegistry

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        discovered = await db.execute(
            text(
                "SELECT id FROM public.project_pipelines "
                "WHERE created_by = ANY(CAST(:actor_ids AS uuid[])) "
                "AND name LIKE :name_prefix"
            ),
            {
                "actor_ids": [str(value) for value in world["actor_ids"]],
                "name_prefix": world["prefix"] + "%",
            },
        )
        pipeline_ids = {uuid.UUID(str(row[0])) for row in discovered.all()} | set(
            world["pipeline_ids"]
        )
        if pipeline_ids:
            await db.execute(
                text(
                    "DELETE FROM public.project_pipelines "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [str(value) for value in pipeline_ids]},
            )
        if world["backend_binding_ids"]:
            await db.execute(
                delete(ProjectMLBackendPool).where(
                    ProjectMLBackendPool.id.in_(world["backend_binding_ids"])
                )
            )
        if world["organization_member_ids"]:
            await db.execute(
                delete(OrganizationMember).where(
                    OrganizationMember.id.in_(world["organization_member_ids"])
                )
            )
        if world["pool_ids"]:
            await db.execute(
                delete(MLBackendPoolMember).where(
                    MLBackendPoolMember.pool_id.in_(world["pool_ids"])
                )
            )
            await db.execute(
                delete(MLBackendServicePool).where(
                    MLBackendServicePool.id.in_(world["pool_ids"])
                )
            )
        if world["backend_ids"]:
            await db.execute(
                delete(MLBackendPoolMember).where(
                    MLBackendPoolMember.registry_id.in_(world["backend_ids"])
                )
            )
            await db.execute(
                delete(MLBackendRegistry).where(
                    MLBackendRegistry.id.in_(world["backend_ids"])
                )
            )
        if world["project_ids"]:
            await db.execute(
                delete(Project).where(Project.id.in_(world["project_ids"]))
            )
        if world["organization_ids"]:
            await db.execute(
                delete(Organization).where(
                    Organization.id.in_(world["organization_ids"])
                )
            )
        if world["actor_ids"]:
            await db.execute(delete(User).where(User.id.in_(world["actor_ids"])))

    async with maker() as db:
        if pipeline_ids:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.project_pipelines "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [str(value) for value in pipeline_ids]},
                )
                == 0
            )
        for table, values in (
            ("projects", world["project_ids"]),
            ("organizations", world["organization_ids"]),
            ("users", world["actor_ids"]),
        ):
            if values:
                count = await db.scalar(
                    text(
                        f"SELECT count(*) FROM public.{table} "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [str(value) for value in values]},
                )
                assert count == 0
        for table, values in (
            ("organization_members", world["organization_member_ids"]),
            ("project_ml_backend_pool", world["backend_binding_ids"]),
            ("ml_backend_service_pools", world["pool_ids"]),
            ("ml_backend_registry", world["backend_ids"]),
        ):
            if values:
                count = await db.scalar(
                    text(
                        f"SELECT count(*) FROM public.{table} "
                        "WHERE id = ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [str(value) for value in values]},
                )
                assert count == 0
        if world["pool_ids"] or world["backend_ids"]:
            member_count = await db.scalar(
                text(
                    "SELECT count(*) FROM public.ml_backend_pool_members "
                    "WHERE pool_id = ANY(CAST(:pool_ids AS uuid[])) "
                    "OR registry_id = ANY(CAST(:registry_ids AS uuid[]))"
                ),
                {
                    "pool_ids": [str(value) for value in world["pool_ids"]],
                    "registry_ids": [str(value) for value in world["backend_ids"]],
                },
            )
            assert member_count == 0


async def _create_pipeline(
    client,
    maker,
    world,
    *,
    label: str,
    actor: str,
    name: str,
    alias: str,
    scope: str,
    stages: list[dict],
    project_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    is_default: bool = False,
) -> tuple[httpx.Response, object]:
    world["pipeline_names"][name] = alias
    body = {
        "name": name,
        "scope": scope,
        "project_id": str(project_id) if project_id is not None else None,
        "organization_id": (
            str(organization_id) if organization_id is not None else None
        ),
        "stages": stages,
        "is_default": is_default,
    }
    response, payload = await _request(
        client,
        maker,
        world,
        label=label,
        method="POST",
        path="/api/v1/project-pipelines",
        actor=actor,
        body=body,
        response_alias=alias,
        expected_status=201,
    )
    assert isinstance(payload, dict) and isinstance(payload.get("id"), str)
    return response, uuid.UUID(payload["id"])


async def _run_scenarios(client, maker, world) -> list[dict]:
    prefix = world["prefix"]
    actors = world["actors"]
    projects = world["projects"]
    organization = world["organization"]
    missing_backend = world["missing_backend"]
    enabled_backend = world["backend_ids"][0]
    invalid_stage = [
        {
            "stage": 1,
            "parent_stage": 17,
            "ml_backend_id": str(missing_backend),
            "extra": True,
        }
    ]

    private_main_name = prefix + "-private-main"
    private_target_name = prefix + "-private-target"
    private_other_name = prefix + "-private-other"
    org_name = prefix + "-organization"
    public_missing_name = prefix + "-public-missing-backend"
    public_valid_name = prefix + "-public-valid-backend"
    demoted_name = prefix + "-demoted-private"

    _, private_main = await _create_pipeline(
        client,
        maker,
        world,
        label="create_private_default_main",
        actor="owner",
        name=private_main_name,
        alias="$pipeline_private_main",
        scope="private",
        project_id=projects["main"].id,
        stages=_pipeline_stages(missing_backend),
        is_default=True,
    )
    _, private_target = await _create_pipeline(
        client,
        maker,
        world,
        label="create_private_default_target",
        actor="owner",
        name=private_target_name,
        alias="$pipeline_private_target",
        scope="private",
        project_id=projects["target"].id,
        stages=_pipeline_stages(missing_backend),
        is_default=True,
    )
    _, private_other = await _create_pipeline(
        client,
        maker,
        world,
        label="create_private_other_owner",
        actor="other_owner",
        name=private_other_name,
        alias="$pipeline_private_other",
        scope="private",
        project_id=projects["other"].id,
        stages=_pipeline_stages(missing_backend),
    )
    _, organization_pipeline = await _create_pipeline(
        client,
        maker,
        world,
        label="create_organization_pipeline",
        actor="other_owner",
        name=org_name,
        alias="$pipeline_organization",
        scope="organization",
        organization_id=organization.id,
        stages=_pipeline_stages(missing_backend),
    )
    _, public_missing = await _create_pipeline(
        client,
        maker,
        world,
        label="create_public_without_backend_check",
        actor="super_admin",
        name=public_missing_name,
        alias="$pipeline_public_missing_backend",
        scope="public",
        stages=_pipeline_stages(missing_backend),
    )
    _, public_valid = await _create_pipeline(
        client,
        maker,
        world,
        label="create_public_apply_source",
        actor="super_admin",
        name=public_valid_name,
        alias="$pipeline_public_valid_backend",
        scope="public",
        stages=_pipeline_stages(enabled_backend),
    )

    await _request(
        client,
        maker,
        world,
        label="create_invalid_scope_enum_precedes_service_role",
        method="POST",
        path="/api/v1/project-pipelines",
        actor="outsider",
        body={
            "name": prefix + "-invalid-enum",
            "scope": "team",
            "project_id": None,
            "organization_id": None,
            "stages": [],
        },
        expected_status=422,
    )
    await _request(
        client,
        maker,
        world,
        label="create_scope_owner_pair_rejected_by_schema",
        method="POST",
        path="/api/v1/project-pipelines",
        actor="owner",
        body={
            "name": prefix + "-invalid-owner-pair",
            "scope": "private",
            "project_id": None,
            "organization_id": None,
            "stages": [],
        },
        expected_status=422,
    )
    await _request(
        client,
        maker,
        world,
        label="create_non_admin_rejected_before_stage_validation",
        method="POST",
        path="/api/v1/project-pipelines",
        actor="outsider",
        body={
            "name": prefix + "-employee-create",
            "scope": "private",
            "project_id": str(projects["main"].id),
            "organization_id": None,
            "stages": invalid_stage,
            "is_default": False,
        },
        expected_status=403,
    )
    await _request(
        client,
        maker,
        world,
        label="create_public_rejected_for_project_admin",
        method="POST",
        path="/api/v1/project-pipelines",
        actor="owner",
        body={
            "name": prefix + "-project-admin-public",
            "scope": "public",
            "project_id": None,
            "organization_id": None,
            "stages": _pipeline_stages(missing_backend),
        },
        expected_status=403,
    )
    await _request(
        client,
        maker,
        world,
        label="create_organization_rejected_before_stage_validation",
        method="POST",
        path="/api/v1/project-pipelines",
        actor="owner",
        body={
            "name": prefix + "-project-admin-foreign-org",
            "scope": "organization",
            "project_id": None,
            "organization_id": str(organization.id),
            "stages": invalid_stage,
        },
        expected_status=403,
    )
    await _request(
        client,
        maker,
        world,
        label="create_hidden_project_rejected_before_stage_validation",
        method="POST",
        path="/api/v1/project-pipelines",
        actor="owner",
        body={
            "name": prefix + "-hidden-project",
            "scope": "private",
            "project_id": str(projects["other"].id),
            "organization_id": None,
            "stages": invalid_stage,
        },
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="create_structurally_invalid_stages",
        method="POST",
        path="/api/v1/project-pipelines",
        actor="owner",
        body={
            "name": prefix + "-invalid-stages",
            "scope": "private",
            "project_id": str(projects["main"].id),
            "organization_id": None,
            "stages": invalid_stage,
        },
        expected_status=422,
    )

    def aliases_in(rows):
        return {world["aliases"].get(row["id"], row["id"]) for row in rows}

    _, list_admin = await _request(
        client,
        maker,
        world,
        label="list_admin_owner_visible_scopes",
        method="GET",
        path="/api/v1/project-pipelines",
        actor="owner",
        expected_status=200,
    )
    assert aliases_in(list_admin) == {
        "$pipeline_private_main",
        "$pipeline_private_target",
        "$pipeline_public_missing_backend",
        "$pipeline_public_valid_backend",
        "$pipeline_historical_invalid",
    }
    _, list_org_member = await _request(
        client,
        maker,
        world,
        label="list_org_member_visible_scopes",
        method="GET",
        path="/api/v1/project-pipelines",
        actor="org_member",
        expected_status=200,
    )
    assert aliases_in(list_org_member) == {
        "$pipeline_organization",
        "$pipeline_public_missing_backend",
        "$pipeline_public_valid_backend",
        "$pipeline_historical_invalid",
    }
    _, list_outsider = await _request(
        client,
        maker,
        world,
        label="list_nonmember_public_only",
        method="GET",
        path="/api/v1/project-pipelines",
        actor="outsider",
        expected_status=200,
    )
    assert aliases_in(list_outsider) == {
        "$pipeline_public_missing_backend",
        "$pipeline_public_valid_backend",
        "$pipeline_historical_invalid",
    }
    _, list_super_admin = await _request(
        client,
        maker,
        world,
        label="list_super_admin_all_scopes",
        method="GET",
        path="/api/v1/project-pipelines",
        actor="super_admin",
        expected_status=200,
    )
    assert aliases_in(list_super_admin) == {
        "$pipeline_private_main",
        "$pipeline_private_target",
        "$pipeline_private_other",
        "$pipeline_organization",
        "$pipeline_public_missing_backend",
        "$pipeline_public_valid_backend",
        "$pipeline_historical_invalid",
    }
    _, org_filtered = await _request(
        client,
        maker,
        world,
        label="list_organization_filter",
        method="GET",
        path="/api/v1/project-pipelines",
        actor="org_member",
        params={"scope": "organization", "organization_id": str(organization.id)},
        expected_status=200,
    )
    assert aliases_in(org_filtered) == {"$pipeline_organization"}
    _, private_filtered = await _request(
        client,
        maker,
        world,
        label="list_private_project_filter",
        method="GET",
        path="/api/v1/project-pipelines",
        actor="owner",
        params={"scope": "private", "project_id": str(projects["main"].id)},
        expected_status=200,
    )
    assert aliases_in(private_filtered) == {"$pipeline_private_main"}
    _, empty_org_filter = await _request(
        client,
        maker,
        world,
        label="list_nonmember_organization_filter_is_empty",
        method="GET",
        path="/api/v1/project-pipelines",
        actor="owner",
        params={"organization_id": str(organization.id)},
        expected_status=200,
    )
    assert empty_org_filter == []
    await _request(
        client,
        maker,
        world,
        label="list_invalid_scope_query",
        method="GET",
        path="/api/v1/project-pipelines",
        actor="outsider",
        params={"scope": "team"},
        expected_status=422,
    )

    await _request(
        client,
        maker,
        world,
        label="get_private_creator",
        method="GET",
        path=f"/api/v1/project-pipelines/{private_main}",
        actor="owner",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="get_private_hidden_before_edit_authority",
        method="GET",
        path=f"/api/v1/project-pipelines/{private_other}",
        actor="owner",
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="get_organization_member",
        method="GET",
        path=f"/api/v1/project-pipelines/{organization_pipeline}",
        actor="org_member",
        expected_status=200,
    )
    await _request(
        client,
        maker,
        world,
        label="get_organization_nonmember_hidden",
        method="GET",
        path=f"/api/v1/project-pipelines/{organization_pipeline}",
        actor="outsider",
        expected_status=404,
    )
    _, public_detail = await _request(
        client,
        maker,
        world,
        label="get_public_nonmember",
        method="GET",
        path=f"/api/v1/project-pipelines/{public_missing}",
        actor="outsider",
        expected_status=200,
    )
    assert public_detail["created_by_name"] == "Pipeline super administrator"
    missing_pipeline = uuid.uuid4()
    world["aliases"][str(missing_pipeline)] = "$pipeline_missing"
    await _request(
        client,
        maker,
        world,
        label="get_missing_pipeline",
        method="GET",
        path=f"/api/v1/project-pipelines/{missing_pipeline}",
        actor="owner",
        expected_status=404,
    )

    await _request(
        client,
        maker,
        world,
        label="update_private_hidden_before_edit_forbidden",
        method="PUT",
        path=f"/api/v1/project-pipelines/{private_other}",
        actor="owner",
        body={"name": prefix + "-hidden-private-update"},
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="update_hidden_pipeline_schema_422_precedes_visibility",
        method="PUT",
        path=f"/api/v1/project-pipelines/{private_other}",
        actor="outsider",
        body={"scope": "team"},
        expected_status=422,
    )
    await _request(
        client,
        maker,
        world,
        label="update_public_visible_but_not_editable",
        method="PUT",
        path=f"/api/v1/project-pipelines/{public_missing}",
        actor="outsider",
        body={"name": prefix + "-public-outsider-update"},
        expected_status=403,
    )
    organization_updated_name = prefix + "-organization-updated-by-member"
    world["pipeline_names"][organization_updated_name] = "$pipeline_organization"
    _, org_update = await _request(
        client,
        maker,
        world,
        label="update_organization_member_can_edit_shared_pipeline",
        method="PUT",
        path=f"/api/v1/project-pipelines/{organization_pipeline}",
        actor="org_member",
        body={"name": organization_updated_name},
        expected_status=200,
    )
    assert org_update["name"] == organization_updated_name
    await _request(
        client,
        maker,
        world,
        label="update_organization_nonmember_hidden",
        method="PUT",
        path=f"/api/v1/project-pipelines/{organization_pipeline}",
        actor="outsider",
        body={"name": prefix + "-organization-outsider-update"},
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="update_scope_owner_pair_rejected",
        method="PUT",
        path=f"/api/v1/project-pipelines/{private_main}",
        actor="owner",
        body={"scope": "public"},
        expected_status=422,
    )
    await _request(
        client,
        maker,
        world,
        label="update_structurally_invalid_stages_rolls_back",
        method="PUT",
        path=f"/api/v1/project-pipelines/{private_main}",
        actor="owner",
        body={"stages": invalid_stage},
        expected_status=422,
    )

    private_name_updated = prefix + "-private-main-name-updated"
    world["pipeline_names"][private_name_updated] = "$pipeline_private_main"
    _, omitted_fields_update = await _request(
        client,
        maker,
        world,
        label="update_omitted_fields_preserve_stages_and_default",
        method="PUT",
        path=f"/api/v1/project-pipelines/{private_main}",
        actor="owner",
        body={"name": private_name_updated},
        expected_status=200,
    )
    assert omitted_fields_update["stages"] == _canonical_stages(missing_backend)
    assert omitted_fields_update["is_default"] is True

    for label, body in (
        ("update_null_name_records_legacy_failure", {"name": None}),
        ("update_null_stages_records_legacy_failure", {"stages": None}),
        ("update_null_is_default_records_legacy_failure", {"is_default": None}),
    ):
        await _request(
            client,
            maker,
            world,
            label=label,
            method="PUT",
            path=f"/api/v1/project-pipelines/{private_main}",
            actor="owner",
            body=body,
            expected_status=500,
        )
        sample = world["samples"][-1]
        if "stages" in body:
            changed = next(
                row
                for row in sample["database_after"]
                if row["id"] == "$pipeline_private_main"
            )
            assert changed["stages"] is None
            assert sample["database_before"] != sample["database_after"]
            # JSONB None commits JSON null; restore only this seeded row for
            # the following independent invalid-field observations.
            async with maker.begin() as db:
                await db.execute(
                    text(
                        "UPDATE public.project_pipelines SET stages=CAST(:stages AS jsonb) WHERE id=:id"
                    ),
                    {
                        "id": private_main,
                        "stages": json.dumps(_canonical_stages(missing_backend)),
                    },
                )
        else:
            assert sample["database_before"] == sample["database_after"]

    await _request(
        client,
        maker,
        world,
        label="update_null_scope_private_is_owner_pair_422",
        method="PUT",
        path=f"/api/v1/project-pipelines/{private_main}",
        actor="owner",
        body={"scope": None},
        expected_status=422,
    )
    await _request(
        client,
        maker,
        world,
        label="update_null_private_project_is_owner_pair_422",
        method="PUT",
        path=f"/api/v1/project-pipelines/{private_main}",
        actor="owner",
        body={"project_id": None},
        expected_status=422,
    )
    await _request(
        client,
        maker,
        world,
        label="update_null_organization_owner_is_owner_pair_422",
        method="PUT",
        path=f"/api/v1/project-pipelines/{organization_pipeline}",
        actor="org_member",
        body={"organization_id": None},
        expected_status=422,
    )
    await _request(
        client,
        maker,
        world,
        label="update_null_public_scope_records_legacy_failure",
        method="PUT",
        path=f"/api/v1/project-pipelines/{public_missing}",
        actor="super_admin",
        body={"scope": None},
        expected_status=(400, 599),
    )
    sample = world["samples"][-1]
    assert sample["database_before"] == sample["database_after"]

    moved_default_response, moved_default = await _request(
        client,
        maker,
        world,
        label="update_move_default_omission_demotes_without_displacing_target",
        method="PUT",
        path=f"/api/v1/project-pipelines/{private_main}",
        actor="owner",
        body={"project_id": str(projects["target"].id)},
        expected_status=200,
    )
    assert moved_default_response.status_code == 200
    assert moved_default["project_id"] == str(projects["target"].id)
    assert moved_default["is_default"] is False
    snapshot_by_id = {row["id"]: row for row in world["samples"][-1]["database_after"]}
    assert snapshot_by_id["$pipeline_private_main"]["is_default"] is False
    assert snapshot_by_id["$pipeline_private_target"]["is_default"] is True
    _, moved_default_detail = await _request(
        client,
        maker,
        world,
        label="get_moved_default_row",
        method="GET",
        path=f"/api/v1/project-pipelines/{private_main}",
        actor="owner",
        expected_status=200,
    )
    assert moved_default_detail["is_default"] is False
    await _request(
        client,
        maker,
        world,
        label="update_same_name_omitting_default",
        method="PUT",
        path=f"/api/v1/project-pipelines/{private_target}",
        actor="owner",
        body={"name": private_target_name},
        expected_status=200,
    )
    same_name_sample = world["samples"][-1]
    assert isinstance(
        same_name_sample["database_updated_at_changed"]["$pipeline_private_target"],
        bool,
    )

    await _request(
        client,
        maker,
        world,
        label="update_same_default_explicit_true",
        method="PUT",
        path=f"/api/v1/project-pipelines/{private_target}",
        actor="owner",
        body={"is_default": True},
        expected_status=200,
    )
    same_default_sample = world["samples"][-1]
    assert isinstance(
        same_default_sample["database_updated_at_changed"]["$pipeline_private_target"],
        bool,
    )

    missing_source = uuid.uuid4()
    world["aliases"][str(missing_source)] = "$pipeline_missing_source"
    await _request(
        client,
        maker,
        world,
        label="apply_target_manager_required_before_source_lookup",
        method="POST",
        path=f"/api/v1/projects/{projects['main'].id}/pipelines/apply",
        actor="org_member",
        body={"pipeline_id": str(missing_source), "set_default": True},
        expected_status=403,
    )
    await _request(
        client,
        maker,
        world,
        label="apply_missing_source_precedes_stage_validation",
        method="POST",
        path=f"/api/v1/projects/{projects['main'].id}/pipelines/apply",
        actor="owner",
        body={"pipeline_id": str(missing_source), "set_default": True},
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="apply_private_source_hidden_from_target_manager",
        method="POST",
        path=f"/api/v1/projects/{projects['main'].id}/pipelines/apply",
        actor="owner",
        body={"pipeline_id": str(private_other), "set_default": True},
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="apply_historical_structural_error_precedes_backend_enablement",
        method="POST",
        path=f"/api/v1/projects/{projects['main'].id}/pipelines/apply",
        actor="owner",
        body={
            "pipeline_id": str(
                next(
                    value
                    for value, alias in world["aliases"].items()
                    if alias == "$pipeline_historical_invalid"
                )
            ),
            "set_default": True,
        },
        expected_status=422,
    )
    _, unenabled = await _request(
        client,
        maker,
        world,
        label="apply_unenabled_backend_returns_ordered_ids_without_mutation",
        method="POST",
        path=f"/api/v1/projects/{projects['main'].id}/pipelines/apply",
        actor="owner",
        body={"pipeline_id": str(public_missing), "set_default": True},
        expected_status=422,
    )
    assert unenabled["detail"]["unenabled_backends"] == [str(missing_backend)]
    assert (
        world["samples"][-1]["database_before"]
        == world["samples"][-1]["database_after"]
    )

    apply_default_sample_index = len(world["samples"])
    _, apply_copy = await _request(
        client,
        maker,
        world,
        label="apply_public_pipeline_copy_atomic_usage_default_and_null_creator_name",
        method="POST",
        path=f"/api/v1/projects/{projects['main'].id}/pipelines/apply",
        actor="owner",
        body={"pipeline_id": str(public_valid), "set_default": True},
        response_alias="$pipeline_applied_copy",
        expected_status=201,
    )
    assert apply_copy["scope"] == "private"
    assert apply_copy["project_id"] == str(projects["main"].id)
    assert apply_copy["is_default"] is True
    assert apply_copy["created_by_name"] is None
    assert apply_copy["stages"] == _canonical_stages(enabled_backend)
    apply_sample = world["samples"][apply_default_sample_index]
    before_rows = {row["id"]: row for row in apply_sample["database_before"]}
    after_rows = {row["id"]: row for row in apply_sample["database_after"]}
    assert before_rows["$pipeline_public_valid_backend"]["usage_count"] == 0
    assert after_rows["$pipeline_public_valid_backend"]["usage_count"] == 1
    assert after_rows["$pipeline_applied_copy"]["is_default"] is True

    _, apply_copy_no_default = await _request(
        client,
        maker,
        world,
        label="apply_set_default_false_preserves_existing_target_default",
        method="POST",
        path=f"/api/v1/projects/{projects['target'].id}/pipelines/apply",
        actor="owner",
        body={"pipeline_id": str(public_valid), "set_default": False},
        response_alias="$pipeline_applied_without_default",
        expected_status=201,
    )
    assert apply_copy_no_default["is_default"] is False
    assert apply_copy_no_default["created_by_name"] is None
    second_apply_sample = world["samples"][-1]
    second_before = {row["id"]: row for row in second_apply_sample["database_before"]}
    second_after = {row["id"]: row for row in second_apply_sample["database_after"]}
    assert second_before["$pipeline_public_valid_backend"]["usage_count"] == 1
    assert second_after["$pipeline_public_valid_backend"]["usage_count"] == 2
    _, target_default_after_apply_false = await _request(
        client,
        maker,
        world,
        label="get_existing_default_after_apply_false",
        method="GET",
        path=f"/api/v1/project-pipelines/{private_target}",
        actor="owner",
        expected_status=200,
    )
    assert target_default_after_apply_false["is_default"] is True

    source_name_updated = prefix + "-public-source-updated-after-apply"
    world["pipeline_names"][source_name_updated] = "$pipeline_public_valid_backend"
    await _request(
        client,
        maker,
        world,
        label="update_source_after_apply_does_not_rewrite_copy",
        method="PUT",
        path=f"/api/v1/project-pipelines/{public_valid}",
        actor="super_admin",
        body={"name": source_name_updated, "stages": _pipeline_stages(missing_backend)},
        expected_status=200,
    )
    _, copy_after_source_change = await _request(
        client,
        maker,
        world,
        label="read_copy_after_source_change",
        method="GET",
        path=f"/api/v1/project-pipelines/{apply_copy['id']}",
        actor="owner",
        expected_status=200,
    )
    assert copy_after_source_change["name"] == public_valid_name
    assert copy_after_source_change["stages"] == _canonical_stages(enabled_backend)

    copy_name_updated = prefix + "-applied-copy-updated-independently"
    world["pipeline_names"][copy_name_updated] = "$pipeline_applied_copy"
    _, changed_copy = await _request(
        client,
        maker,
        world,
        label="update_copy_does_not_rewrite_source",
        method="PUT",
        path=f"/api/v1/project-pipelines/{apply_copy['id']}",
        actor="owner",
        body={"name": copy_name_updated},
        expected_status=200,
    )
    assert changed_copy["name"] == copy_name_updated
    _, source_after_copy_change = await _request(
        client,
        maker,
        world,
        label="read_source_after_copy_change",
        method="GET",
        path=f"/api/v1/project-pipelines/{public_valid}",
        actor="super_admin",
        expected_status=200,
    )
    assert source_after_copy_change["name"] == source_name_updated
    assert source_after_copy_change["stages"] == _canonical_stages(missing_backend)

    org_member_id = world["organization_member_ids"][1]
    async with maker.begin() as db:
        await db.execute(
            update(OrganizationMember)
            .where(OrganizationMember.id == org_member_id)
            .values(deleted_at=datetime.now(timezone.utc))
        )
    await _request(
        client,
        maker,
        world,
        label="deleted_organization_membership_hides_detail",
        method="GET",
        path=f"/api/v1/project-pipelines/{organization_pipeline}",
        actor="org_member",
        expected_status=404,
    )
    _, list_after_membership_delete = await _request(
        client,
        maker,
        world,
        label="deleted_organization_membership_removes_org_scope_from_list",
        method="GET",
        path="/api/v1/project-pipelines",
        actor="org_member",
        expected_status=200,
    )
    assert aliases_in(list_after_membership_delete) == {
        "$pipeline_public_missing_backend",
        "$pipeline_public_valid_backend",
        "$pipeline_historical_invalid",
    }
    async with maker.begin() as db:
        await db.execute(
            update(OrganizationMember)
            .where(OrganizationMember.id == org_member_id)
            .values(deleted_at=None)
        )

    await _request(
        client,
        maker,
        world,
        label="delete_private_hidden_before_edit_forbidden",
        method="DELETE",
        path=f"/api/v1/project-pipelines/{private_other}",
        actor="owner",
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="delete_public_visible_but_not_editable",
        method="DELETE",
        path=f"/api/v1/project-pipelines/{public_missing}",
        actor="outsider",
        expected_status=403,
    )
    await _request(
        client,
        maker,
        world,
        label="delete_organization_nonmember_hidden",
        method="DELETE",
        path=f"/api/v1/project-pipelines/{organization_pipeline}",
        actor="outsider",
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="delete_organization_member_shared_pipeline",
        method="DELETE",
        path=f"/api/v1/project-pipelines/{organization_pipeline}",
        actor="org_member",
        expected_status=204,
    )

    async with maker.begin() as db:
        await db.execute(
            update(User)
            .where(User.id == actors["other_owner"].id)
            .values(is_active=False)
        )
    _, inactive_creator_detail = await _request(
        client,
        maker,
        world,
        label="inactive_creator_name_remains_in_detail",
        method="GET",
        path=f"/api/v1/project-pipelines/{private_other}",
        actor="super_admin",
        expected_status=200,
    )
    assert inactive_creator_detail["created_by_name"] == "Pipeline other owner"

    _, demoted_private = await _create_pipeline(
        client,
        maker,
        world,
        label="create_pipeline_for_later_demoted_creator",
        actor="demoted_owner",
        name=demoted_name,
        alias="$pipeline_demoted_owner",
        scope="private",
        project_id=projects["demoted"].id,
        stages=_pipeline_stages(missing_backend),
    )
    async with maker.begin() as db:
        await db.execute(
            update(User)
            .where(User.id == actors["demoted_owner"].id)
            .values(role="employee")
        )
    world["tokens"]["demoted_owner"] = create_access_token(
        str(actors["demoted_owner"].id), "employee"
    )
    demoted_update_name = prefix + "-demoted-owner-edit-after-role-change"
    world["pipeline_names"][demoted_update_name] = "$pipeline_demoted_owner"
    await _request(
        client,
        maker,
        world,
        label="demoted_creator_update_rechecks_project_visibility",
        method="PUT",
        path=f"/api/v1/project-pipelines/{demoted_private}",
        actor="demoted_owner",
        body={"name": demoted_update_name},
        expected_status=404,
    )
    await _request(
        client,
        maker,
        world,
        label="demoted_private_creator_can_delete_own_pipeline",
        method="DELETE",
        path=f"/api/v1/project-pipelines/{demoted_private}",
        actor="demoted_owner",
        expected_status=204,
    )
    await _request(
        client,
        maker,
        world,
        label="delete_private_creator",
        method="DELETE",
        path=f"/api/v1/project-pipelines/{private_other}",
        actor="super_admin",
        expected_status=204,
    )
    await _request(
        client,
        maker,
        world,
        label="delete_public_creator",
        method="DELETE",
        path=f"/api/v1/project-pipelines/{public_missing}",
        actor="super_admin",
        expected_status=204,
    )

    assert [sample["label"] for sample in world["samples"]] == EXPECTED_SAMPLE_LABELS
    return world["samples"]


EXPECTED_SAMPLE_LABELS = [
    "create_private_default_main",
    "create_private_default_target",
    "create_private_other_owner",
    "create_organization_pipeline",
    "create_public_without_backend_check",
    "create_public_apply_source",
    "create_invalid_scope_enum_precedes_service_role",
    "create_scope_owner_pair_rejected_by_schema",
    "create_non_admin_rejected_before_stage_validation",
    "create_public_rejected_for_project_admin",
    "create_organization_rejected_before_stage_validation",
    "create_hidden_project_rejected_before_stage_validation",
    "create_structurally_invalid_stages",
    "list_admin_owner_visible_scopes",
    "list_org_member_visible_scopes",
    "list_nonmember_public_only",
    "list_super_admin_all_scopes",
    "list_organization_filter",
    "list_private_project_filter",
    "list_nonmember_organization_filter_is_empty",
    "list_invalid_scope_query",
    "get_private_creator",
    "get_private_hidden_before_edit_authority",
    "get_organization_member",
    "get_organization_nonmember_hidden",
    "get_public_nonmember",
    "get_missing_pipeline",
    "update_private_hidden_before_edit_forbidden",
    "update_hidden_pipeline_schema_422_precedes_visibility",
    "update_public_visible_but_not_editable",
    "update_organization_member_can_edit_shared_pipeline",
    "update_organization_nonmember_hidden",
    "update_scope_owner_pair_rejected",
    "update_structurally_invalid_stages_rolls_back",
    "update_omitted_fields_preserve_stages_and_default",
    "update_null_name_records_legacy_failure",
    "update_null_stages_records_legacy_failure",
    "update_null_is_default_records_legacy_failure",
    "update_null_scope_private_is_owner_pair_422",
    "update_null_private_project_is_owner_pair_422",
    "update_null_organization_owner_is_owner_pair_422",
    "update_null_public_scope_records_legacy_failure",
    "update_move_default_omission_demotes_without_displacing_target",
    "get_moved_default_row",
    "update_same_name_omitting_default",
    "update_same_default_explicit_true",
    "apply_target_manager_required_before_source_lookup",
    "apply_missing_source_precedes_stage_validation",
    "apply_private_source_hidden_from_target_manager",
    "apply_historical_structural_error_precedes_backend_enablement",
    "apply_unenabled_backend_returns_ordered_ids_without_mutation",
    "apply_public_pipeline_copy_atomic_usage_default_and_null_creator_name",
    "apply_set_default_false_preserves_existing_target_default",
    "get_existing_default_after_apply_false",
    "update_source_after_apply_does_not_rewrite_copy",
    "read_copy_after_source_change",
    "update_copy_does_not_rewrite_source",
    "read_source_after_copy_change",
    "deleted_organization_membership_hides_detail",
    "deleted_organization_membership_removes_org_scope_from_list",
    "delete_private_hidden_before_edit_forbidden",
    "delete_public_visible_but_not_editable",
    "delete_organization_nonmember_hidden",
    "delete_organization_member_shared_pipeline",
    "inactive_creator_name_remains_in_detail",
    "create_pipeline_for_later_demoted_creator",
    "demoted_creator_update_rechecks_project_visibility",
    "demoted_private_creator_can_delete_own_pipeline",
    "delete_private_creator",
    "delete_public_creator",
]


async def _exercise_owned_http(
    test_engine, app_module, monkeypatch, *, application_name
):
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
                transport=transport, base_url="http://test", timeout=30
            ) as client:
                samples = await _run_scenarios(client, owner_maker, world)
        result = {
            "samples": samples,
            "runtime_identity": runtime_identity,
            "runtime_url": runtime_url,
            "tokens": list(world["tokens"].values()),
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
                    f"named-pipeline capture/replay cleanup failed: {error!r}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        elif cleanup_errors:
            raise cleanup_errors[0]
    assert result is not None
    return result


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
    or os.environ.get("AAP_NAMED_PIPELINE_LEGACY_CAPTURE") != "1",
    reason="requires an explicit owned legacy named-pipeline capture",
)
async def test_capture_legacy_named_pipeline_http_and_database(
    test_engine, app_module, monkeypatch
):
    capture_path = _capture_path()
    source = _source_metadata()
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    live = await _exercise_owned_http(
        test_engine,
        app_module,
        monkeypatch,
        application_name="named-pipeline-legacy-capture",
    )
    capture = {
        "capture_kind": "actual-legacy-named-project-pipeline-http-and-database",
        "source": source,
        "runtime_identity": live["runtime_identity"],
        "observations": {
            "http_uses_real_asgi_routes": True,
            "api_sessions_use_verified_legacy_runtime": True,
            "database_snapshots_use_independent_owner_sessions": True,
            "database_cleanup_scope": (
                "exact generated actors, projects, organization, backend rows, "
                "and pipeline name prefix"
            ),
        },
        "samples": live["samples"],
    }
    serialized = json.dumps(capture, ensure_ascii=False, sort_keys=True)
    assert live["runtime_url"] not in serialized
    assert all(token not in serialized for token in live["tokens"])
    for marker in ("password_hash", "authorization", "bearer "):
        assert marker not in serialized.casefold()
    with capture_path.open("x", encoding="utf-8") as stream:
        json.dump(capture, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
    or not _FIXTURE_PATH.is_file(),
    reason=(
        "requires an explicit owned acceptance run and a promoted immutable "
        "actual-legacy named-pipeline fixture"
    ),
)
async def test_replay_legacy_named_pipeline_http_and_database(
    test_engine, app_module, monkeypatch
):
    fixture = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture.get("capture_kind") == (
        "actual-legacy-named-project-pipeline-http-and-database"
    )
    expected_samples = fixture.get("samples")
    assert isinstance(expected_samples, list)
    assert [
        sample.get("label") for sample in expected_samples
    ] == EXPECTED_SAMPLE_LABELS
    assert fixture["source"].get("pre_extraction_base") == _BASE_COMMIT

    live = await _exercise_owned_http(
        test_engine,
        app_module,
        monkeypatch,
        application_name="named-pipeline-legacy-replay",
    )
    for observed, expected in zip(live["samples"], expected_samples, strict=True):
        # Snapshot SQL uses random UUID tie-breakers; only this diagnostic row
        # ordering is normalized. Public response array order remains exact.
        def view(sample):
            return {
                **sample,
                **{
                    key: sorted(sample[key], key=lambda row: (row["name"], row["id"]))
                    for key in ("database_before", "database_after")
                },
            }

        assert view(observed) == view(expected), observed["label"]
