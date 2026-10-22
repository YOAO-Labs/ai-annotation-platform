"""Bounded live capacity sample for the private Workspace module assembly."""

from __future__ import annotations

import asyncio
import math
import os
import secrets
import time
import uuid
from collections import Counter

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_identity.security import create_access_token
from app.db.models.user import User
from tests.factory import create_user
from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    create_guide_storage_scope,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.workspace_module_assembly_helpers import WorkspaceModuleStack


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_WORKSPACE_CAPACITY_LIVE") != "1",
        reason="requires the explicitly enabled owned Workspace capacity verifier",
    ),
]

_PHASE_SECONDS = 15.0
_EXPECTED_LIVE_SERVICES = 9


def _token(user: User, secret: str) -> str:
    return create_access_token(
        str(user.id), user.role, secret=secret, expire_minutes=60
    )


async def _seed_users(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with maker.begin() as db:
        actors = {
            "owner": await create_user(
                db,
                "project_admin",
                f"workspace-capacity-owner-{suffix}@example.test",
                "Workspace capacity owner",
            ),
            "successor": await create_user(
                db,
                "project_admin",
                f"workspace-capacity-successor-{suffix}@example.test",
                "Workspace capacity successor",
            ),
            "member": await create_user(
                db,
                "employee",
                f"workspace-capacity-member-{suffix}@example.test",
                "Workspace capacity member",
            ),
            "revoked": await create_user(
                db,
                "employee",
                f"workspace-capacity-revoked-{suffix}@example.test",
                "Workspace capacity revoked member",
            ),
            "super_admin": await create_user(
                db,
                "super_admin",
                f"workspace-capacity-super-{suffix}@example.test",
                "Workspace capacity test actor",
            ),
        }
    return maker, actors


def _new_metrics() -> dict:
    return {
        "requests": 0,
        "operations": Counter(),
        "statuses": Counter(),
        "operation_statuses": Counter(),
        "latencies_seconds": [],
        "errors": [],
        "completed_cycles": 0,
    }


async def _request(
    client: httpx.AsyncClient,
    metrics: dict,
    operation: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    expected_status: int | tuple[int, ...],
    json: dict | None = None,
):
    expected = (
        (expected_status,) if isinstance(expected_status, int) else expected_status
    )
    started = time.monotonic()
    metrics["requests"] += 1
    metrics["operations"][operation] += 1
    try:
        response = await client.request(
            method, path, headers=headers, json=json, timeout=15
        )
    except Exception as exc:
        metrics["errors"].append({"operation": operation, "error": type(exc).__name__})
        raise
    finally:
        metrics["latencies_seconds"].append(time.monotonic() - started)

    metrics["statuses"][str(response.status_code)] += 1
    metrics["operation_statuses"][f"{operation}:{response.status_code}"] += 1
    if response.status_code not in expected:
        metrics["errors"].append(
            {
                "operation": operation,
                "status": response.status_code,
                "body": response.text[:500],
            }
        )
        raise AssertionError(
            f"{operation} returned {response.status_code}: {response.text}"
        )
    return response


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _phase_report(metrics: dict, elapsed: float, concurrency: int) -> dict:
    latencies = metrics["latencies_seconds"]
    return {
        "concurrency": concurrency,
        "target_seconds": _PHASE_SECONDS,
        "actual_seconds": round(elapsed, 3),
        "requests": metrics["requests"],
        "operation_counts": dict(metrics["operations"]),
        "status_counts": dict(metrics["statuses"]),
        "operation_status_counts": dict(metrics["operation_statuses"]),
        "completed_cycles": metrics["completed_cycles"],
        "requests_per_second": round(metrics["requests"] / elapsed, 3)
        if elapsed
        else 0.0,
        "cycles_per_second": round(metrics["completed_cycles"] / elapsed, 3)
        if elapsed
        else 0.0,
        "latency_ms": {
            "p50": round((_percentile(latencies, 0.50) or 0.0) * 1000, 2),
            "p95": round((_percentile(latencies, 0.95) or 0.0) * 1000, 2),
            "max": round(max(latencies, default=0.0) * 1000, 2),
        },
        "errors": metrics["errors"],
    }


async def _wait_for_service_connections_to_drain(test_engine) -> None:
    owner_role = test_engine.url.username
    deadline = time.monotonic() + 20
    remaining = []
    while time.monotonic() < deadline:
        async with test_engine.connect() as db:
            remaining = (
                (
                    await db.execute(
                        text(
                            "SELECT usename, count(*) AS connections "
                            "FROM pg_catalog.pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND backend_type = 'client backend' "
                            "AND usename IS DISTINCT FROM :owner_role "
                            "GROUP BY usename ORDER BY usename"
                        ),
                        {"owner_role": owner_role},
                    )
                )
                .mappings()
                .all()
            )
        if not remaining:
            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"Workspace service connections did not drain: {remaining}")


async def _verify_final_database(maker, lanes: list[dict], revoked_project_id):
    async with maker() as db:
        for lane in lanes:
            project = (
                (
                    await db.execute(
                        text("SELECT name, owner_id FROM public.projects WHERE id=:id"),
                        {"id": lane["project_id"]},
                    )
                )
                .mappings()
                .one()
            )
            assert project["name"] == lane["expected_name"]
            assert project["owner_id"] == lane["owner_id"]
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.project_members "
                        "WHERE project_id=:project_id AND user_id=:user_id"
                    ),
                    {
                        "project_id": lane["project_id"],
                        "user_id": lane["member_id"],
                    },
                )
                == 0
            )
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM public.project_members "
                    "WHERE project_id=:project_id"
                ),
                {"project_id": revoked_project_id},
            )
            == 0
        )


async def test_workspace_module_bounded_capacity_and_revocation_under_load(
    test_engine, tmp_path, record_property
):
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    _verify_postgres(test_engine.url)
    async with test_engine.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM public.users")) == 0
        assert await db.scalar(text("SELECT count(*) FROM public.projects")) == 0

    resources, guide_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    maker, actors = await _seed_users(test_engine)
    api_secret = secrets.token_urlsafe(32)
    control_token, inference_token, guide_token = [
        secrets.token_urlsafe(32) for _ in range(3)
    ]
    guide_scope, guide_destroy = create_guide_storage_scope(
        resources,
        owner_s3,
        guide_bucket,
        f"outside-guide/{uuid.uuid4()}.png",
    )
    stack = None
    lanes: list[dict] = []
    revoked_project_id = None
    phases: dict[str, dict] = {}
    runtime_samples: list[dict] = []

    try:
        stack = WorkspaceModuleStack(
            test_engine,
            tmp_path,
            api_secret=api_secret,
            control_token=control_token,
            inference_token=inference_token,
            guide_token=guide_token,
            guide_bucket=guide_bucket,
            guide_access_key=guide_scope["access_key"],
            guide_secret_key=guide_scope["secret_key"],
        )
        await stack.start()
        record_property("workspace_capacity_services", stack.inspection_evidence)
        tokens = {key: _token(user, api_secret) for key, user in actors.items()}
        headers = {
            key: {"Authorization": f"Bearer {token}"} for key, token in tokens.items()
        }
        record_property(
            "workspace_capacity_auth_profile",
            "super_admin JWT for the mixed load; actual employee JWT for membership and revocation checks",
        )

        async with httpx.AsyncClient(
            base_url=stack.api_origin,
            trust_env=False,
            timeout=15,
        ) as client:
            suffix = uuid.uuid4().hex[:10]
            for lane_index in range(8):
                template_name = f"Capacity {suffix} template {lane_index}"
                template_response = await client.post(
                    "/api/v1/project-templates",
                    headers=headers["owner"],
                    json={
                        "name": template_name,
                        "description": "bounded capacity fixture",
                        "type_label": "Image",
                        "type_key": "image-det",
                        "scope": "private",
                        "classes": ["Car"],
                    },
                )
                assert template_response.status_code == 201, template_response.text
                template_id = uuid.UUID(template_response.json()["id"])
                project_name = f"Capacity {suffix} project {lane_index} seed"
                project_response = await client.post(
                    "/api/v1/projects",
                    headers=headers["owner"],
                    json={"name": project_name, "template_id": str(template_id)},
                )
                assert project_response.status_code == 200, project_response.text
                project_id = uuid.UUID(project_response.json()["id"])
                lanes.append(
                    {
                        "index": lane_index,
                        "template_id": template_id,
                        "project_id": project_id,
                        "member_id": actors["member"].id,
                        "owner_id": actors["owner"].id,
                        "sequence": 0,
                        "expected_name": project_name,
                    }
                )

            revoked_create = await client.post(
                "/api/v1/projects",
                headers=headers["owner"],
                json={
                    "name": f"Capacity {suffix} revocation probe",
                    "type_label": "Image",
                    "type_key": "image-det",
                },
            )
            assert revoked_create.status_code == 200, revoked_create.text
            revoked_project_id = uuid.UUID(revoked_create.json()["id"])
            revoked_add = await client.post(
                f"/api/v1/projects/{revoked_project_id}/members",
                headers=headers["owner"],
                json={"user_id": str(actors["revoked"].id), "role": "annotator"},
            )
            assert revoked_add.status_code == 201, revoked_add.text
            revoked_membership_id = uuid.UUID(revoked_add.json()["id"])
            employee_detail = await client.get(
                f"/api/v1/projects/{revoked_project_id}",
                headers=headers["revoked"],
            )
            employee_access = await client.get(
                f"/api/v1/projects/{revoked_project_id}/access",
                headers=headers["revoked"],
            )
            assert employee_detail.status_code == 200, employee_detail.text
            assert employee_access.status_code == 200, employee_access.text
            assert employee_access.json()["access_kind"] == "member"
            preview = await client.post(
                f"/api/v1/projects/{revoked_project_id}/members/{revoked_membership_id}/role/preview",
                headers=headers["owner"],
                json={"project_role": "reviewer"},
            )
            assert preview.status_code == 200, preview.text
            preview_data = preview.json()

            async def sample_runtime(label: str) -> None:
                snapshot = await asyncio.to_thread(stack.sample_runtime)
                services = snapshot["services"]
                assert len(services) == _EXPECTED_LIVE_SERVICES
                if runtime_samples:
                    previous = runtime_samples[-1]["services"]
                    for service, current in services.items():
                        assert (
                            current["container_id"] == previous[service]["container_id"]
                        )
                        assert current["started_at"] == previous[service]["started_at"]
                runtime_samples.append(snapshot)
                record_property(label, snapshot)
                if len(runtime_samples) < 4:
                    await asyncio.sleep(2)

            await sample_runtime("workspace_capacity_runtime_baseline")

            async def run_lane(
                lane: dict,
                phase_name: str,
                metrics: dict,
                deadline: float,
                ready_callback,
            ) -> None:
                first_request = True
                while time.monotonic() < deadline:
                    lane["sequence"] += 1
                    sequence = lane["sequence"]
                    project_id = lane["project_id"]
                    name = (
                        f"Capacity {suffix} {phase_name} lane {lane['index']} "
                        f"sequence {sequence}"
                    )
                    response = await _request(
                        client,
                        metrics,
                        "configuration_project_update",
                        "PATCH",
                        f"/api/v1/projects/{project_id}",
                        headers=headers["super_admin"],
                        expected_status=200,
                        json={"name": name},
                    )
                    assert response.json()["name"] == name
                    lane["expected_name"] = name
                    if first_request:
                        first_request = False
                        ready_callback()

                    detail = await _request(
                        client,
                        metrics,
                        "project_detail",
                        "GET",
                        f"/api/v1/projects/{project_id}",
                        headers=headers["super_admin"],
                        expected_status=200,
                    )
                    assert detail.json()["name"] == name

                    templates = await _request(
                        client,
                        metrics,
                        "private_template_list",
                        "GET",
                        "/api/v1/project-templates?scope=private",
                        headers=headers["super_admin"],
                        expected_status=200,
                    )
                    assert any(
                        row["id"] == str(lane["template_id"])
                        for row in templates.json()
                    )

                    member = await _request(
                        client,
                        metrics,
                        "membership_add",
                        "POST",
                        f"/api/v1/projects/{project_id}/members",
                        headers=headers["super_admin"],
                        expected_status=201,
                        json={
                            "user_id": str(lane["member_id"]),
                            "role": "annotator",
                        },
                    )
                    membership_id = uuid.UUID(member.json()["id"])
                    assert member.json()["role"] == "annotator"

                    members = await _request(
                        client,
                        metrics,
                        "directory_member_list",
                        "GET",
                        f"/api/v1/projects/{project_id}/members",
                        headers=headers["super_admin"],
                        expected_status=200,
                    )
                    assert any(
                        row["id"] == str(membership_id) for row in members.json()
                    )

                    removed = await _request(
                        client,
                        metrics,
                        "membership_remove",
                        "DELETE",
                        f"/api/v1/projects/{project_id}/members/{membership_id}",
                        headers=headers["super_admin"],
                        expected_status=204,
                    )
                    assert not removed.content

                    target_owner = (
                        actors["successor"].id
                        if lane["owner_id"] == actors["owner"].id
                        else actors["owner"].id
                    )
                    transferred = await _request(
                        client,
                        metrics,
                        "owner_transfer",
                        "POST",
                        f"/api/v1/projects/{project_id}/transfer",
                        headers=headers["super_admin"],
                        expected_status=200,
                        json={"new_owner_id": str(target_owner)},
                    )
                    assert transferred.json()["owner_id"] == str(target_owner)
                    lane["owner_id"] = target_owner
                    metrics["completed_cycles"] += 1

            async def run_phase(
                concurrency: int,
                *,
                revoke_while_active: bool = False,
            ) -> dict:
                phase_name = f"c{concurrency}"
                metrics = _new_metrics()
                started = time.monotonic()
                deadline = started + _PHASE_SECONDS
                ready_count = 0
                ready = asyncio.Event()

                def mark_ready() -> None:
                    nonlocal ready_count
                    ready_count += 1
                    if ready_count == concurrency:
                        ready.set()

                tasks = [
                    asyncio.create_task(
                        run_lane(
                            lanes[index],
                            phase_name,
                            metrics,
                            deadline,
                            mark_ready,
                        )
                    )
                    for index in range(concurrency)
                ]
                revocation_metrics = _new_metrics()
                revocation_elapsed = 0.0
                try:
                    if revoke_while_active:
                        await asyncio.wait_for(ready.wait(), timeout=10)
                        assert all(not task.done() for task in tasks)
                        revocation_started = time.monotonic()
                        revoked_headers = headers["owner"]
                        await _request(
                            client,
                            revocation_metrics,
                            "revocation_remove",
                            "DELETE",
                            f"/api/v1/projects/{revoked_project_id}/members/{revoked_membership_id}",
                            headers=revoked_headers,
                            expected_status=204,
                        )
                        revoked_reads = await asyncio.gather(
                            *(
                                _request(
                                    client,
                                    revocation_metrics,
                                    "revoked_project_detail",
                                    "GET",
                                    f"/api/v1/projects/{revoked_project_id}",
                                    headers=headers["revoked"],
                                    expected_status=404,
                                )
                                for _ in range(2)
                            )
                        )
                        assert all(
                            response.status_code == 404 for response in revoked_reads
                        )
                        stale = await _request(
                            client,
                            revocation_metrics,
                            "revoked_stale_role_change",
                            "PATCH",
                            f"/api/v1/projects/{revoked_project_id}/members/{revoked_membership_id}/role",
                            headers=revoked_headers,
                            expected_status=404,
                            json={
                                "project_role": "reviewer",
                                "expected_version": preview_data["current_version"],
                                "preview_token": preview_data["preview_token"],
                                "reason": "capacity test stale preview after revocation",
                            },
                        )
                        assert stale.status_code == 404
                        assert all(not task.done() for task in tasks)
                        revocation_elapsed = time.monotonic() - revocation_started
                    await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise

                elapsed = time.monotonic() - started
                report = _phase_report(metrics, elapsed, concurrency)
                if revoke_while_active:
                    report["concurrent_revocation"] = _phase_report(
                        revocation_metrics,
                        max(0.001, revocation_elapsed),
                        1,
                    )
                    report["revocation_completed_while_lane_tasks_active"] = True
                assert metrics["errors"] == []
                return report

            phases["concurrency_1"] = await run_phase(1)
            await sample_runtime("workspace_capacity_runtime_after_c1")
            phases["concurrency_4"] = await run_phase(4)
            await sample_runtime("workspace_capacity_runtime_after_c4")
            phases["concurrency_8"] = await run_phase(8, revoke_while_active=True)
            await sample_runtime("workspace_capacity_runtime_after_c8")
            phases["concurrency_8"]["revocation_detail_statuses"] = dict(
                phases["concurrency_8"]["concurrent_revocation"]["status_counts"]
            )

            await _verify_final_database(maker, lanes, revoked_project_id)
            record_property("workspace_capacity_phases", phases)
            record_property(
                "workspace_capacity_runtime_samples",
                {
                    "sample_count": len(runtime_samples),
                    "services": _EXPECTED_LIVE_SERVICES,
                    "stable_container_ids_and_started_at": True,
                    "snapshots": runtime_samples,
                },
            )
    except Exception:
        if stack is not None:
            record_property(
                "workspace_capacity_failure_diagnostics",
                stack.capture_diagnostics(),
            )
        if phases:
            record_property("workspace_capacity_partial_phases", phases)
        raise
    finally:
        try:
            if stack is not None:
                await stack.stop()
        finally:
            guide_destroy(guide_scope)
            if stack is not None:
                await _wait_for_service_connections_to_drain(test_engine)
