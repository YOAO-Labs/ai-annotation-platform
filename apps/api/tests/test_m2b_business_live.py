r"""M2b default-host acceptance; run from a fresh checkout without a .env.

Use the module-data verifier's default LAN profile with its private MinIO
bootstrap owner (not development MinIO). AAP_M2B_CPU_PLUGIN_PYTHON points to an
independent author venv with the M1a color-region example, its SDK and matching
portable contract wheels installed non-editably. No platform source or app
credentials are given to the plugin. Example, with owned MinIO env already set:

    AAP_M2B_BUSINESS_LIVE=1 AAP_M2B_CPU_PLUGIN_PYTHON=/path/author-venv/bin/python \
      apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
      --workspace-consumer-profile default --test-file tests/test_m2b_business_live.py \
      --output /path/private/m2b-business.json
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from functools import partial
from hashlib import sha256
from io import BytesIO
import json
import os
import secrets
import time
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
from PIL import Image, ImageDraw
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.factory import create_user
from tests.m2b_business_stack import API_LOGIN, business_stack

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_M2B_BUSINESS_LIVE") != "1",
        reason="requires the explicitly enabled disposable M2b module-data verifier",
    ),
]

_API = "/api/v1"
_REGISTRY = "/admin/ml-integrations"
_FILENAME = "regions.png"
_EXPECTED_BOXES = {
    "red-region": (0.10, 0.10, 0.20, 0.30),
    "green-region": (0.50, 0.40, 0.30, 0.50),
}


def _image() -> bytes:
    with Image.new("RGB", (100, 100), "black") as image, BytesIO() as encoded:
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 29, 39), fill=(255, 0, 0))
        draw.rectangle((50, 40, 79, 89), fill=(0, 255, 0))
        image.save(encoded, format="PNG")
        return encoded.getvalue()


async def _api(client, method, path, *, status=200, raw=False, **kwargs):
    response = await client.request(method, _API + path, **kwargs)
    assert response.status_code == status, f"{method} {path}: {response.status_code}"
    return response.content if raw else response.json()


async def _seed_logins(test_engine, nonce, password):
    accounts = {}
    async with async_sessionmaker(test_engine, expire_on_commit=False).begin() as db:
        for name, role in (
            ("registry", "super_admin"),
            ("owner", "project_admin"),
            ("annotator", "employee"),
            ("reviewer", "employee"),
        ):
            email = f"m2b-{name}-{nonce}@example.test"
            user = await create_user(db, role, email, name, password=password)
            user.email_verified_at = datetime.now(UTC)
            accounts[name] = (email, str(user.id), role)
    return accounts


async def _assert_default_login(default_engine):
    async with default_engine.connect() as db:
        login = (
            await db.execute(
                text("""
                    SELECT current_user, session_user, db.oid::bigint, EXISTS (
                        SELECT 1 FROM pg_catalog.pg_stat_activity AS peer
                        WHERE peer.datid=db.oid AND peer.usename=session_user
                          AND peer.pid<>pg_catalog.pg_backend_pid()
                    )
                    FROM pg_catalog.pg_database AS db WHERE datname=current_database()
                """)
            )
        ).one()
        assert login == (
            API_LOGIN,
            API_LOGIN,
            int(os.environ["AAP_MODULE_DATA_PG_DATABASE_OID"]),
            True,
        )
        for operation in (
            "SELECT id FROM public.users LIMIT 0",
            "SELECT id FROM public.projects LIMIT 0",
            "SELECT id FROM public.datasets LIMIT 0",
            "SELECT id FROM public.tasks LIMIT 0",
            "SELECT id FROM public.predictions LIMIT 0",
            "SELECT id FROM public.notifications LIMIT 0",
            "SELECT auth_token FROM public.ml_backend_registry LIMIT 0",
            "SELECT * FROM yoao_jobs_v1.dispatch_intents LIMIT 0",
            "SELECT yoao_jobs_v1.claim_dispatches(NULL::jsonb)",
            "SELECT yoao_inference_health_observation_v1.open_health_observation_subject"
            "(NULL::text, NULL::uuid, NULL::uuid)",
        ):
            with pytest.raises(DBAPIError) as denied:
                async with db.begin_nested():
                    await db.execute(text(operation))
            assert denied.value.orig.sqlstate == "42501", operation


async def _wait_for_prediction(owner, project_id, job_id, stack):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        stack.assert_alive()
        job = await owner("GET", f"/async-jobs/{job_id}")
        if job["status"] in {"completed", "failed", "cancelled"}:
            assert job["status"] == "completed", (
                f"Preannotation {job_id}: {job['status']}"
            )
            assert job["kind"] == "batch_predict"
            assert job["project_id"] == project_id and job["celery_task_id"] == job_id
            assert job["started_at"] and job["completed_at"]
            assert job["result"]["success_count"] == 1
            assert job["result"]["failed_count"] == 0
            return
        await asyncio.sleep(0.25)
    raise AssertionError("Preannotation did not finish")


async def _login(client, accounts, password):
    callers = {}
    for name, (email, actor_id, role) in accounts.items():
        login = await _api(
            client, "POST", "/auth/login", json={"email": email, "password": password}
        )
        caller = partial(
            _api, client, headers={"Authorization": f"Bearer {login['access_token']}"}
        )
        who = await caller("GET", "/auth/me")
        assert (who["id"], who["role"]) == (actor_id, role)
        callers[name] = caller
    return callers


async def _import_task(client, owner, stack, nonce, project_id, image, digest):
    dataset = await owner(
        "POST",
        "/datasets",
        status=201,
        json={"name": f"M2b dataset {nonce}", "data_type": "image"},
    )
    dataset_path = f"/datasets/{dataset['id']}"
    upload = await owner(
        "POST",
        f"{dataset_path}/items/upload-init",
        json={"file_name": _FILENAME, "content_type": "image/png"},
    )
    put_url = urlsplit(upload["upload_url"])
    assert f"{put_url.scheme}://{put_url.netloc}" == stack.storage_origin
    assert "X-Amz-Signature" in parse_qs(put_url.query)
    try:
        put = await client.put(
            upload["upload_url"], content=image, headers={"Content-Type": "image/png"}
        )
    except httpx.RequestError:
        raise AssertionError("Signed upload could not reach the owned MinIO") from None
    assert put.status_code == 200
    item_id = upload["item_id"]
    completed = await owner("POST", f"{dataset_path}/items/upload-complete/{item_id}")
    assert completed == {"status": "ok", "item_id": item_id, "linked_tasks": 0}
    (item,) = (await owner("GET", f"{dataset_path}/items"))["items"]
    assert (item["id"], item["file_name"], item["file_size"]) == (
        item_id,
        _FILENAME,
        len(image),
    )

    linked = await owner(
        "POST", f"{dataset_path}/link", json={"project_id": project_id}
    )
    assert linked["status"] == "linked" and linked["created_tasks"] == 1
    (task,) = (
        await owner(
            "GET",
            "/tasks",
            params={"project_id": project_id, "dataset_id": dataset["id"]},
        )
    )["items"]
    assert (task["project_id"], task["file_name"], task["status"]) == (
        project_id,
        _FILENAME,
        "pending",
    )
    resource = f"/files/tasks/{task['id']}"
    original = await owner("GET", f"{resource}/original-identity")
    assert (original["sha256"], original["size_bytes"]) == (digest, len(image))
    downloaded = await owner(
        "GET",
        f"{resource}/original",
        raw=True,
        params={"content_id": original["content_id"], "sha256": digest},
    )
    assert downloaded == image
    return task["id"]


async def _activate_batch(
    owner, annotator, project_id, task_id, annotator_id, reviewer_id
):
    project_path = f"/projects/{project_id}"
    for user_id, role in ((annotator_id, "annotator"), (reviewer_id, "reviewer")):
        member = await owner(
            "POST",
            f"{project_path}/members",
            status=201,
            json={"user_id": user_id, "role": role},
        )
        assert (member["user_id"], member["role"]) == (user_id, role)
    (batch,) = await owner(
        "POST",
        f"{project_path}/batches/split",
        json={
            "strategy": "random",
            "n_batches": 1,
            "shuffle": False,
            "annotator_id": annotator_id,
            "reviewer_id": reviewer_id,
        },
    )
    assert batch["total_tasks"] == 1
    active = await owner(
        "POST",
        f"{project_path}/batches/{batch['id']}/transition",
        json={"target_status": "active"},
    )
    assert active["status"] == "active"
    assigned = await annotator("GET", f"/tasks/{task_id}")
    assert (assigned["batch_id"], assigned["assignee_id"]) == (
        batch["id"],
        annotator_id,
    )


async def _predict(stack, owner, registry, nonce, project_id, task_id):
    backend = await registry(
        "POST",
        f"{_REGISTRY}/registry",
        status=201,
        json={
            "name": f"M1a color regions {nonce}",
            "url": stack.author.origin,
            "auth_method": "token",
            "auth_token": stack.author_token,
        },
    )
    backend_id = backend["id"]
    health = await registry("POST", f"{_REGISTRY}/registry/{backend_id}/health")
    assert (health["status"], health["backend_id"]) == ("ok", backend_id)
    pools = await registry("GET", f"{_REGISTRY}/service-pools")
    (pool,) = [entry for entry in pools if entry["legacy_instance_id"] == backend_id]
    assert pool["capability_fingerprint"]
    pool_id = pool["id"]
    enabled = await registry(
        "PATCH", f"{_REGISTRY}/service-pools/{pool_id}", json={"enabled": True}
    )
    assert enabled["enabled"]
    project_path = f"/projects/{project_id}"
    binding = await owner(
        "PUT",
        f"{project_path}/ml-backends/pools/{pool_id}/enablement",
        json={"enabled": True},
    )
    assert binding["enabled"] and binding["pool"]["id"] == pool_id
    prediction_path = f"/tasks/{task_id}/predictions"
    assert await owner("GET", prediction_path) == []
    accepted = await owner(
        "POST",
        f"{project_path}/preannotate",
        json={
            "ml_backend_id": backend_id,
            "model_id": "detect",
            "task": "detect",
            "model_variants": {"series": "color-region", "size": "colors"},
            "task_ids": [task_id],
            "execution_scope": "bulk",
            "predict_mode": "overwrite",
        },
    )
    job_id = accepted["job_id"]
    assert accepted["status"] == "queued" and accepted["total_tasks"] == 1
    await _wait_for_prediction(owner, project_id, job_id, stack)
    (prediction,) = await owner("GET", prediction_path)
    assert (
        prediction["task_id"],
        prediction["project_id"],
        prediction["ml_backend_id"],
    ) == (task_id, project_id, backend_id)
    assert prediction["source"] == "ml_backend"
    result = prediction["result"]
    assert len(result) == 2
    assert {shape["class_name"] for shape in result} == set(_EXPECTED_BOXES)
    for shape in result:
        geometry = shape["geometry"]
        assert shape["confidence"] == 1.0
        w, h = geometry["w"], geometry["h"]
        if geometry["type"] == "rotated_bbox":
            assert geometry["angle"] == 0
            x, y = geometry["cx"] - w / 2, geometry["cy"] - h / 2
        else:
            assert geometry["type"] == "bbox"
            x, y = geometry["x"], geometry["y"]
        assert (x, y, w, h) == pytest.approx(
            _EXPECTED_BOXES[shape["class_name"]], abs=1e-6
        )
    assert stack.model_calls() == [
        {
            "pid": stack.author.process.pid,
            "task_id": task_id,
            "object_store": True,
            "signed_and_versioned": True,
            "bucket": stack.inputs_bucket,
        }
    ]
    return job_id, prediction["id"]


async def _annotate_and_review(
    annotator, reviewer, project_id, task_id, annotator_id, reviewer_id
):
    task_path = f"/tasks/{task_id}"
    lock = await annotator("POST", f"{task_path}/lock")
    assert (lock["task_id"], lock["user_id"]) == (task_id, annotator_id)
    manual_box = {"type": "bbox", "x": 0.11, "y": 0.12, "w": 0.19, "h": 0.28}
    annotation = await annotator(
        "POST",
        f"{task_path}/annotations",
        status=201,
        json={
            "annotation_type": "bbox",
            "tool_unit_id": "bbox",
            "class_name": "red-region",
            "geometry": manual_box,
        },
    )
    assert (annotation["task_id"], annotation["geometry"]) == (task_id, manual_box)
    submitted = await annotator("POST", f"{task_path}/submit")
    assert (submitted["status"], submitted["task_id"]) == ("submitted", task_id)

    async def approval_notifications():
        inbox = await annotator("GET", "/notifications")
        return [
            entry
            for entry in inbox["items"]
            if entry["type"] == "task.approved" and entry["target_id"] == task_id
        ]

    assert await approval_notifications() == []
    claim = await reviewer("POST", f"{task_path}/review/claim")
    assert (claim["task_id"], claim["reviewer_id"], claim["is_self"]) == (
        task_id,
        reviewer_id,
        True,
    )
    (to_review,) = await reviewer("GET", f"{task_path}/annotations")
    assert (to_review["id"], to_review["geometry"]) == (annotation["id"], manual_box)
    approved = await reviewer("POST", f"{task_path}/review/approve")
    assert approved == {"status": "approved", "task_id": task_id}
    final_task = await reviewer("GET", task_path)
    assert (
        final_task["status"],
        final_task["reviewer_id"],
        final_task["total_annotations"],
    ) == ("completed", reviewer_id, 1)
    (notification,) = await approval_notifications()
    assert (notification["target_type"], notification["read_at"]) == ("task", None)
    assert notification["payload"]["project_id"] == project_id
    assert notification["payload"]["actor_id"] == reviewer_id
    return notification["id"]


async def test_default_host_runs_one_public_business_flow(
    test_engine, tmp_path, record_property
):
    nonce = uuid4().hex[:12]
    password = secrets.token_urlsafe(24)
    image = _image()
    digest = sha256(image).hexdigest()

    with business_stack(test_engine, tmp_path) as stack:
        default_engine = create_async_engine(stack.database_url, hide_parameters=True)
        try:
            await _assert_default_login(default_engine)
            accounts = await _seed_logins(test_engine, nonce, password)
            async with httpx.AsyncClient(
                base_url=stack.api.origin, trust_env=False, timeout=30
            ) as client:
                callers = await _login(client, accounts, password)
                owner, annotator, reviewer = (
                    callers[name] for name in ("owner", "annotator", "reviewer")
                )
                annotator_id = accounts["annotator"][1]
                reviewer_id = accounts["reviewer"][1]
                project = await owner(
                    "POST",
                    "/projects",
                    json={
                        "name": f"M2b project {nonce}",
                        "type_label": "图像-检测",
                        "data_type": "image",
                        "classes": list(_EXPECTED_BOXES),
                    },
                )
                assert project["owner_id"] == accounts["owner"][1]
                project_id = project["id"]
                task_id = await _import_task(
                    client, owner, stack, nonce, project_id, image, digest
                )
                await _activate_batch(
                    owner, annotator, project_id, task_id, annotator_id, reviewer_id
                )
                job_id, prediction_id = await _predict(
                    stack, owner, callers["registry"], nonce, project_id, task_id
                )
                notification_id = await _annotate_and_review(
                    annotator, reviewer, project_id, task_id, annotator_id, reviewer_id
                )
                (persisted,) = await owner("GET", f"/tasks/{task_id}/predictions")
                assert persisted["id"] == prediction_id
            async with default_engine.connect() as db:
                proof = await db.scalar(
                    text("""
                        SELECT extra->'accepted_input'
                        FROM yoao_labeling_v1.prediction_meta_rows
                        WHERE prediction_id=:prediction_id
                    """),
                    {"prediction_id": UUID(prediction_id)},
                )
            assert proof is not None
            assert (
                proof["job_id"],
                proof["task_id"],
                proof["sha256"],
                proof["byte_size"],
            ) == (job_id, task_id, digest, len(image))
            stack.assert_alive()
            for key, value in (
                ("api_login", API_LOGIN),
                ("database_oid", int(os.environ["AAP_MODULE_DATA_PG_DATABASE_OID"])),
                ("process_ids", json.dumps(stack.process_ids(), sort_keys=True)),
                ("worker_queues", ",".join(stack.worker_queues)),
                ("reviewed_task_id", task_id),
                ("input_sha256", digest),
                ("prediction_job_id", job_id),
                ("prediction_id", prediction_id),
                ("approval_notification_id", notification_id),
            ):
                record_property(key, value)
        finally:
            await default_engine.dispose()
