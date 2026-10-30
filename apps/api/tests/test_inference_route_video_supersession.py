"""Real video claim retains its pinned route across a health observation."""

from __future__ import annotations

import json
import os
import uuid

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference.routing_runtime import RoutingRuntime
from aap_inference_contract.routing import (
    RoutingBeginResult,
    RoutingRejectedResult,
    make_routing_command,
)
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_routing_native import _cleanup, _connect, _seed
from tests.test_inference_routing_owner_integration import _start_uvicorn, _stop_uvicorn
from tests.test_video_execution_native import _cleanup_claimed_scope
from tests.test_video_window_journal_native import _prepared_execution

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


async def test_video_owned_route_remains_pinned_after_health_refresh(test_engine):
    await assert_owned_test_database(test_engine)
    owner = await _connect(test_engine)
    runtime = RoutingRuntime(os.environ["AAP_MODULE_DATA_INFERENCE_ROUTING_URL"])
    app = FastAPI()
    calls = []

    @app.post("/predict")
    async def predict():
        calls.append("predict")
        return {"ok": True}

    server, task, listener, url = await _start_uvicorn(app)
    world = await _seed(owner)
    before = set(
        await owner.fetchval(
            "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) FROM aap_inference_routing_v1.command_receipts"
        )
    )
    fixture = None
    scope_id = None
    try:
        async with async_sessionmaker(
            test_engine, expire_on_commit=False
        ).begin() as db:
            job, proof, _plan = await _prepared_execution(db)
            row = (
                await db.execute(
                    text(
                        "SELECT s.project_id,s.task_id,s.origin_actor_id,j.dataset_item_id,i.dataset_id "
                        "FROM public.video_tracker_input_scopes s JOIN public.video_tracker_jobs j ON j.id=s.tracker_job_id "
                        "JOIN public.dataset_items i ON i.id=j.dataset_item_id WHERE s.tracker_job_id=:job"
                    ),
                    {"job": job.id},
                )
            ).one()
            fixture = dict(
                project_id=row.project_id,
                task_id=row.task_id,
                actor_id=row.origin_actor_id,
                item_id=row.dataset_item_id,
                dataset_id=row.dataset_id,
                job_id=job.id,
            )
        await owner.execute(
            "INSERT INTO public.project_ml_backend_pool(id,project_id,pool_id,enabled) VALUES($1,$2,$3,true)",
            uuid.uuid4(),
            fixture["project_id"],
            world["pool_id"],
        )
        health = {
            "capabilities": {"model_version": "routing-test"},
            "residency": {"boot_id": "video-boot"},
        }
        await owner.execute(
            "UPDATE public.ml_backend_registry SET url=$2,health_meta=$3::jsonb,last_checked_at=now() WHERE id=$1",
            world["registry_id"],
            url,
            json.dumps(health),
        )
        await owner.execute(
            "UPDATE public.ml_backend_registry SET state='connected',health_meta=$2::jsonb,last_checked_at=now() WHERE id=$1",
            world["registry_id"],
            json.dumps(health),
        )
        row = await owner.fetchrow(
            "SELECT auth_method,auth_token,extra_params FROM public.ml_backend_registry WHERE id=$1",
            world["registry_id"],
        )
        extra = (
            json.loads(row["extra_params"])
            if isinstance(row["extra_params"], str)
            else row["extra_params"]
        )
        identity = {
            "url": url,
            "auth_method": row["auth_method"],
            "auth_token": row["auth_token"],
            "gpu_resource_id": None,
            "extra_params": extra,
        }
        acquire = make_routing_command(
            "video_acquire",
            {
                "pool_id": str(world["pool_id"]),
                "registry_id": None,
                "project_id": str(fixture["project_id"]),
                "mode": "off",
                "required_instance_id": str(world["registry_id"]),
                "owner": f"tracker:{fixture['job_id']}",
                "operation": "tracker",
                "execution": proof.model_dump(mode="json"),
            },
        )
        try:
            result = await runtime.execute(acquire)
        except Exception as error:
            causes = []
            while error is not None:
                causes.append(
                    (
                        type(error).__name__,
                        getattr(error, "sqlstate", None),
                        getattr(error, "message", None),
                        getattr(error, "reason", None),
                    )
                )
                error = error.__context__
            pytest.fail(f"video acquire owner error: {causes}")
        assert result.state == "committed" and not isinstance(
            result.result, RoutingRejectedResult
        )
        original = result.result
        scope_id = original.id
        assert original.state == "reserved"
        health["probe"] = {"challenge": uuid.uuid4().hex}
        await owner.execute(
            "UPDATE public.ml_backend_registry SET health_meta=$2::jsonb,last_checked_at=now() WHERE id=$1",
            world["registry_id"],
            json.dumps(health),
        )
        attempt_id = uuid.uuid4()
        async with runtime.transport_subject_issuer(
            parent_kind="scope",
            parent_id=scope_id,
            capability=original.capability,
            attempt_id=attempt_id,
            transport_identity=identity,
        ) as subject:
            command = make_routing_command(
                "begin",
                {
                    "scope_id": str(scope_id),
                    "capability": original.capability,
                    "attempt_id": str(attempt_id),
                    "method": "POST",
                    "path": "/predict",
                    "transport_subject": subject,
                },
            )
            begun = await runtime.begin_model(
                command, member_id=world["member_id"], boot_id="video-boot"
            )
        assert begun.state == "committed" and isinstance(
            begun.result, RoutingBeginResult
        )
        assert begun.result.scope.id == scope_id
        assert begun.result.scope.eligibility_revision > original.eligibility_revision
        assert begun.result.scope.owner == f"tracker:{fixture['job_id']}"
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (
                await client.post(url + "/predict", headers=begun.result.headers)
            ).json() == {"ok": True}
        assert calls == ["predict"]
        settled = await runtime.execute(
            make_routing_command(
                "settle",
                {
                    "scope_id": str(scope_id),
                    "capability": original.capability,
                    "attempt_id": str(attempt_id),
                    "kind": "response",
                    "status_code": 200,
                    "reason": None,
                },
            )
        )
        assert settled.state == "committed" and settled.result.state == "response"
        assert await owner.fetchval(
            "SELECT aap_jobs_v1._video_execution_scope_is_current($1,false)", scope_id
        )
    finally:
        try:
            after = set(
                await owner.fetchval(
                    "SELECT COALESCE(array_agg(id),ARRAY[]::uuid[]) FROM aap_inference_routing_v1.command_receipts"
                )
            )
            await owner.execute(
                "DELETE FROM aap_inference_routing_v1.command_receipts WHERE id=ANY($1::uuid[])",
                list(after - before),
            )
            await _cleanup(owner, world)
            if fixture and scope_id:
                await owner.execute(
                    "DELETE FROM aap_jobs_v1.video_execution_journals WHERE job_id=$1",
                    fixture["job_id"],
                )
                await _cleanup_claimed_scope(test_engine, **fixture, scope_id=scope_id)
        finally:
            await runtime.close()
            await owner.close()
            await _stop_uvicorn(server, task, listener)
