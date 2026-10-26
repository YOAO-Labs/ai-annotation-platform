"""Bounded live capacity sample for the private Workspace module assembly."""

from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import shutil
import subprocess
import time
import uuid
from collections import Counter

import httpx
import pytest
from sqlalchemy import text

from tests.project_guide_asset_owner_helpers import (
    assert_workspace_identity,
    create_guide_storage_scope,
)
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_workspace_module_assembly_live import (
    _assert_restricted_api_consumer,
    _seed_users,
    _token,
)
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


async def _node_patch(origin: str, path: str, headers: dict, body: dict) -> dict:
    node = shutil.which("node")
    assert node, "the independent Workspace consumer requires installed Node.js"
    script = """
let input = '';
for await (const chunk of process.stdin) input += chunk;
const request = JSON.parse(input);
const response = await fetch(request.origin + request.path, {
  method: 'PATCH',
  headers: {...request.headers, 'Content-Type': 'application/json'},
  body: JSON.stringify(request.body),
  redirect: 'error',
});
process.stdout.write(JSON.stringify({
  status: response.status, body: await response.json(), node_version: process.version,
}));
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [node, "--input-type=module", "-e", script],
        input=json.dumps(
            {"origin": origin, "path": path, "headers": headers, "body": body}
        ),
        env={name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ},
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _new_metrics() -> dict:
    return {
        "started_monotonic": time.monotonic(),
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
        "started_monotonic": metrics["started_monotonic"],
        "finished_monotonic": metrics["started_monotonic"] + elapsed,
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


async def _database_snapshot(test_engine) -> list[dict]:
    async with test_engine.connect() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT usename, state, wait_event_type, count(*) AS connections "
                        "FROM pg_catalog.pg_stat_activity "
                        "WHERE datname=current_database() "
                        "AND backend_type='client backend' AND usename<>:owner "
                        "GROUP BY usename, state, wait_event_type "
                        "ORDER BY usename, state, wait_event_type"
                    ),
                    {"owner": test_engine.url.username},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _control_state(test_engine, project_id, actor_id) -> tuple:
    async with test_engine.connect() as db:
        row = (
            await db.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM public.project_members WHERE project_id=:project), "
                    "(SELECT count(*) FROM public.workspace_membership_operation_receipts "
                    " WHERE project_id=:project), "
                    "(SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor)"
                ),
                {"project": project_id, "actor": actor_id},
            )
        ).one()
    return tuple(row)


async def _verify_final_database(maker, lanes: list[dict], revoked_project_id):
    async with maker() as db:
        for lane in lanes:
            project = (
                (
                    await db.execute(
                        text(
                            "SELECT name, owner_id, preannotate_pipeline "
                            "FROM public.projects WHERE id=:id"
                        ),
                        {"id": lane["project_id"]},
                    )
                )
                .mappings()
                .one()
            )
            assert project["name"] == lane["expected_name"]
            assert project["owner_id"] == lane["owner_id"]
            if "expected_pipeline" in lane:
                assert project["preannotate_pipeline"] == lane["expected_pipeline"]
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
            updates = await db.scalar(
                text(
                    "SELECT count(*) FROM public.workspace_configuration_operation_receipts "
                    "WHERE operation='update_project' AND target_id=:id"
                ),
                {"id": lane["project_id"]},
            )
            transfers = await db.scalar(
                text(
                    "SELECT count(*) FROM public.workspace_owner_transfer_operation_receipts "
                    "WHERE project_id=:id"
                ),
                {"id": lane["project_id"]},
            )
            memberships = (
                await db.execute(
                    text(
                        "SELECT operation, count(*) AS receipts, "
                        "count(audit.id) AS retained_audits, "
                        "count(DISTINCT receipt.audit_id) AS distinct_audits "
                        "FROM public.workspace_membership_operation_receipts AS receipt "
                        "LEFT JOIN public.audit_logs AS audit ON audit.id=receipt.audit_id "
                        "WHERE receipt.project_id=:id GROUP BY operation"
                    ),
                    {"id": lane["project_id"]},
                )
            ).mappings()
            expected = lane["sequence"]
            assert updates == expected + lane.get("extra_updates", 0)
            assert transfers == expected
            assert {
                row["operation"]: (
                    row["receipts"],
                    row["retained_audits"],
                    row["distinct_audits"],
                )
                for row in memberships
            } == {
                "add_member": (expected, expected, expected),
                "remove_member": (expected, expected, expected),
            }
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
        for table in (
            "workspace_configuration_operation_states",
            "workspace_configuration_operation_attempts",
        ):
            assert await db.scalar(text(f"SELECT count(*) FROM public.{table}")) == 0


async def test_workspace_module_bounded_capacity_and_revocation_under_load(
    test_engine, tmp_path, record_property
):
    assert test_engine.url.database and test_engine.url.database.endswith("_test")
    _verify_postgres(test_engine.url)
    resources, guide_bucket, owner_s3 = await assert_workspace_identity(test_engine)
    await _assert_restricted_api_consumer(test_engine)
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
            "independent super_admin/account set per load lane; separate actual employee JWT for membership and revocation checks",
        )

        async with httpx.AsyncClient(
            base_url=stack.api_origin,
            trust_env=False,
            timeout=15,
        ) as client:
            suffix = uuid.uuid4().hex[:10]
            for lane_index in range(8):
                _, lane_actors = await _seed_users(test_engine)
                lane_owner_headers = {
                    "Authorization": f"Bearer {_token(lane_actors['owner'], api_secret)}"
                }
                lane_load_headers = {
                    "Authorization": f"Bearer {_token(lane_actors['super_admin'], api_secret)}"
                }
                template_name = f"Capacity {suffix} template {lane_index}"
                template_response = await client.post(
                    "/api/v1/project-templates",
                    headers=lane_owner_headers,
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
                    headers=lane_owner_headers,
                    json={"name": project_name, "template_id": str(template_id)},
                )
                assert project_response.status_code == 200, project_response.text
                project_id = uuid.UUID(project_response.json()["id"])
                lanes.append(
                    {
                        "index": lane_index,
                        "template_id": template_id,
                        "project_id": project_id,
                        "member_id": lane_actors["member"].id,
                        "owner_id": lane_actors["owner"].id,
                        "owner_candidates": (
                            lane_actors["owner"].id,
                            lane_actors["successor"].id,
                        ),
                        "headers": lane_load_headers,
                        "sequence": 0,
                        "expected_name": project_name,
                    }
                )

            # Exercise the actual owner policy before the timed workload.
            # A numeric stage string retains the existing normalization behavior.
            pipeline = [{"stage": "17", "ml_backend_id": str(uuid.UUID(int=1))}]
            canonical_pipeline = [{**pipeline[0], "stage": 17}]
            pipeline_project = lanes[0]
            pipeline_path = f"/api/v1/projects/{pipeline_project['project_id']}"
            saved = await _node_patch(
                stack.api_origin,
                pipeline_path,
                headers["super_admin"],
                {"preannotate_pipeline": pipeline},
            )
            assert saved["status"] == 200, saved
            assert saved["body"]["preannotate_pipeline"] == canonical_pipeline
            pipeline_project["expected_pipeline"] = canonical_pipeline
            pipeline_project["extra_updates"] = 1
            invalid = await client.patch(
                pipeline_path,
                headers=headers["super_admin"],
                json={
                    "name": "Invalid pipeline must roll back this name",
                    "preannotate_pipeline": [
                        *pipeline,
                        {"stage": 18, "ml_backend_id": str(uuid.UUID(int=2))},
                    ],
                },
            )
            assert invalid.status_code == 422, invalid.text
            assert invalid.json()["detail"] == (
                "preannotate_pipeline 须恰有一个源阶段 (parent_stage=None)"
            )
            unchanged = await client.get(pipeline_path, headers=headers["super_admin"])
            assert unchanged.status_code == 200, unchanged.text
            assert unchanged.json()["name"] == pipeline_project["expected_name"]
            assert unchanged.json()["preannotate_pipeline"] == canonical_pipeline
            record_property(
                "workspace_pipeline_contract_consumer",
                {
                    "producer": "node",
                    "node_version": saved["node_version"],
                    "saved_status": 200,
                    "rejected_status": 422,
                    "partial_write": False,
                },
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

            async def sample_runtime(label: str, live_tasks=None) -> None:
                snapshot = await asyncio.to_thread(stack.sample_runtime)
                snapshot["database_connections"] = await _database_snapshot(test_engine)
                snapshot["label"] = label
                snapshot["active_lanes"] = (
                    sum(not task.done() for task in live_tasks)
                    if live_tasks is not None
                    else 0
                )
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
                        headers=lane["headers"],
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
                        headers=lane["headers"],
                        expected_status=200,
                    )
                    assert detail.json()["name"] == name

                    templates = await _request(
                        client,
                        metrics,
                        "private_template_list",
                        "GET",
                        "/api/v1/project-templates?scope=private",
                        headers=lane["headers"],
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
                        headers=lane["headers"],
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
                        headers=lane["headers"],
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
                        headers=lane["headers"],
                        expected_status=204,
                    )
                    assert not removed.content

                    target_owner = (
                        lane["owner_candidates"][1]
                        if lane["owner_id"] == lane["owner_candidates"][0]
                        else lane["owner_candidates"][0]
                    )
                    transferred = await _request(
                        client,
                        metrics,
                        "owner_transfer",
                        "POST",
                        f"/api/v1/projects/{project_id}/transfer",
                        headers=lane["headers"],
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
                metrics["started_monotonic"] = started
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
                stop_observer = asyncio.Event()

                async def observe_load():
                    while not stop_observer.is_set():
                        await sample_runtime(
                            f"workspace_capacity_runtime_{phase_name}_{len(runtime_samples)}",
                            live_tasks=tasks,
                        )
                        try:
                            await asyncio.wait_for(stop_observer.wait(), timeout=2)
                        except TimeoutError:
                            pass

                observer = asyncio.create_task(observe_load())
                revocation_metrics = _new_metrics()
                revocation_elapsed = 0.0
                try:
                    if revoke_while_active:
                        await asyncio.wait_for(ready.wait(), timeout=10)
                        assert all(not task.done() for task in tasks)
                        revocation_started = time.monotonic()
                        revocation_metrics["started_monotonic"] = revocation_started
                        revoked_headers = headers["owner"]
                        control_before_busy = await _control_state(
                            test_engine, revoked_project_id, actors["owner"].id
                        )
                        async with test_engine.begin() as blocker:
                            await blocker.execute(
                                text(
                                    "SELECT id FROM public.users WHERE id=:id FOR UPDATE"
                                ),
                                {"id": actors["member"].id},
                            )
                            busy = await _request(
                                client,
                                revocation_metrics,
                                "membership_account_busy",
                                "POST",
                                f"/api/v1/projects/{revoked_project_id}/members",
                                headers=revoked_headers,
                                expected_status=409,
                                json={
                                    "user_id": str(actors["member"].id),
                                    "role": "annotator",
                                },
                            )
                            assert busy.json()["detail"] == "账号正在更新，请刷新后重试"
                        assert (
                            await _control_state(
                                test_engine, revoked_project_id, actors["owner"].id
                            )
                            == control_before_busy
                        )
                        report_busy = {
                            "status": 409,
                            "memberships_receipts_and_actor_audit_unchanged": True,
                        }
                        record_property(
                            "workspace_capacity_account_contention", report_busy
                        )
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
                    stop_observer.set()
                    await observer
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    stop_observer.set()
                    await asyncio.gather(observer, return_exceptions=True)
                    phases[f"concurrency_{concurrency}"] = {
                        **_phase_report(
                            metrics, time.monotonic() - started, concurrency
                        ),
                        "incomplete": True,
                    }
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
                assert metrics["completed_cycles"] >= concurrency
                assert any(
                    snapshot["label"].startswith(
                        f"workspace_capacity_runtime_{phase_name}_"
                    )
                    and snapshot["active_lanes"] > 0
                    for snapshot in runtime_samples
                )
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
