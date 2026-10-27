"""Qualify synchronous managed images in an already owned launcher test mode.

Run through ``dev-worktree.mjs exec --mode test`` with an absolute private
``--report`` path. This provisions nothing, starts no Celery worker and destroys
no namespace. Real ASGI authentication, canonical originals, MinIO copies and
native acceptance execute against the verified disposable environment. A local
HTTP backend verifies actual signed bytes and returns controlled native Masks;
this does not qualify a real model, GPU, independent TCP API or browser.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
import uuid

import verify_worktree_managed_predictions as plumbing


ROOT = Path(__file__).resolve().parents[1]
require = plumbing.require


def setup_contract():
    contract = plumbing.setup_contract()
    contract["name"] = "controlled-managed-interactive-acceptance"
    contract["is_interactive"] = True
    contract["models"].append(
        {
            "id": "native-interactive",
            "task": "interactive_seg",
            "is_interactive": True,
            "supported_prompts": ["point", "interactive_box", "mask", "scribble"],
            "supported_inputs": [
                "full_image",
                "point_prompt",
                "bbox_prompt",
                "mask_prompt",
                "scribble_prompt",
            ],
            "supported_geometric_outputs": ["polygon", "mask"],
            "resource_profile": {"device": "cpu", "batchable": False},
            "classes": [{"index": 0, "name": "car"}],
        }
    )
    return contract


class InteractiveBackend(plumbing.CPUBackend):
    """Reuse the qualified signed-GET verifier, with a synchronous HTTP wire."""

    def __init__(self, endpoint, bucket):
        self.origin, self.bucket = endpoint, bucket
        self.expected, self.leases, self.events, self.errors = {}, {}, [], []
        self.lock = threading.Lock()
        self.mode, self.scenario = "success", "bootstrap"
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.gated = False
        self.interactions = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, value):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/setup":
                    self.reply(200, setup_contract())
                elif self.path == "/health":
                    self.reply(
                        200,
                        {
                            "status": "ok",
                            "model_version": "controlled-native-cpu",
                            "compute": {"effective_device": "cpu"},
                        },
                    )
                else:
                    self.reply(404, {"detail": "unknown controlled endpoint"})

            def do_POST(self):
                try:
                    require(self.path == "/predict", "非预期 backend 路径")
                    size = int(self.headers.get("Content-Length", 0))
                    require(0 < size <= 256 * 1024, "交互请求超过验收预算")
                    payload = json.loads(self.rfile.read(size))
                    self.reply(200, owner.respond(payload))
                except Exception as exc:
                    with owner.lock:
                        owner.errors.append(type(exc).__name__)
                    self.reply(500, {"detail": "controlled input verification failed"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def respond(self, payload):
        from aap_protocol_v2 import (
            CocoRlePayload,
            decode_low_res_mask,
            encode_low_res_mask,
            native_mask_candidate_id,
        )
        import numpy as np

        interactive = "task" in payload
        tasks = [payload["task"]] if interactive else payload.get("tasks")
        # The inherited verifier performs the real version-pinned HTTP GET,
        # complete SHA/length checks, canonical fixture pixels and gate control.
        results, failed = super().predict(
            {"tasks": tasks, "context": {"model_id": "detect"}}
        )
        require(not failed, "同步验收意外进入后台失败模式")
        if not interactive:
            return {"results": results}
        context = payload["context"]
        require(context.get("model_id") == "native-interactive", "交互模型选择错误")
        require(context.get("output_geometry") == "mask", "交互没有请求原生 Mask")
        continuation = context.get("mask_input")
        if continuation is not None:
            decoded = decode_low_res_mask(continuation)
            require(
                decoded.shape == (1, 256, 256) and bool(np.all(decoded == 0)),
                "加密 logits 未经校验还原到原 backend 输入",
            )
        source = context.get("mask_prompt")
        refined = source is not None
        if refined:
            from app.services.raster_mask_storage import build_rle_reference

            original_mask = {
                "encoding": "coco_rle",
                "size": [96, 128],
                "counts": [1, 2, 12285],
            }
            require(
                source.get("rle") == original_mask
                and source.get("source_digest")
                == build_rle_reference(original_mask)["sha256"],
                "backend 未收到已接受 Mask 的准确像素与摘要",
            )
        rle = CocoRlePayload(
            encoding="coco_rle",
            size=[96, 128],
            counts=[1, 4, 12283] if refined else [1, 2, 12285],
        )
        require(isinstance(context.get("prompt_revision"), str), "缺少平台提示摘要")
        with self.lock:
            self.interactions.append(
                {
                    "scenario": self.scenario,
                    "task_id": tasks[0]["id"],
                    "continuation": continuation is not None,
                    "refine": refined,
                    "prompt_source_digest": source.get("source_digest")
                    if refined
                    else None,
                }
            )
        return {
            "result": [
                {
                    "type": "mask",
                    "value": {
                        "rle": rle.model_dump(mode="json"),
                        "masklabels": ["car"],
                    },
                    "candidate_id": native_mask_candidate_id(
                        rle,
                        prompt_revision=context["prompt_revision"],
                        candidate_index=0,
                    ),
                    "score": 0.9,
                }
            ],
            "score": 0.9,
            "model_version": "controlled-native-cpu",
            "inference_time_ms": 1,
            "cache_hit": False,
            "model_load_ms": 0,
            "mask_input_next": encode_low_res_mask(
                np.zeros((256, 256), dtype=np.float32)
            ),
        }


def point_context(**changes):
    return {
        "type": "point",
        "points": [[0.3, 0.4]],
        "labels": [1],
        "output_geometry": "mask",
        "model_id": "native-interactive",
        **changes,
    }


def accept_body(response):
    candidate = response["result"][0]
    return {
        "idempotency_key": "managed-live-" + uuid.uuid4().hex,
        "class_name": "car",
        "target": {"mode": "create"},
        "candidate": {
            "candidate": candidate,
            "candidate_index": 0,
            "prompt_revision": response["prompt_revision"],
            "receipt": response["accept_receipts"][candidate["candidate_id"]],
        },
        "routing": response["routing"],
        "prompt_summary": response["prompt_summary"],
        "inference": {
            key: response[key]
            for key in (
                "model_version",
                "inference_time_ms",
                "cache_hit",
                "model_load_ms",
            )
        },
    }


def candidate_identity(response, case):
    from app.services.ai_mask_receipt import verify_ai_mask_receipt
    from app.services.ai_mask_session import verify_ai_mask_session
    from app.services.managed_image_inference import normalize_identity

    candidate = response["result"][0]
    claims = verify_ai_mask_receipt(
        response["accept_receipts"][candidate["candidate_id"]]
    )
    require(claims.get("reference_input") is None, "托管来源伪装成引用")
    identity = normalize_identity(claims.get("original_input"))
    require(
        identity["task_id"] == str(case["task_id"])
        and identity["content_id"] == str(case["content_id"])
        and identity["owner_kind"]
        == ("task_original" if case["case"] == "task_original" else "dataset_asset")
        and identity["sha256"] == case["sha256"]
        and identity["size_bytes"] == case["size_bytes"]
        and (identity["width"], identity["height"]) == (128, 96),
        "候选来源或真实非方形尺寸不匹配",
    )
    require(
        verify_ai_mask_session(response["mask_input_next"])["original_input"]
        == identity,
        "候选和 logits 未绑定同一规范原件",
    )
    plumbing.safe_evidence(identity)
    return identity


async def source_versions_intact(storage, cases):
    for case in cases:
        response = await asyncio.to_thread(
            storage.client.get_object,
            Bucket=storage.bucket,
            Key=case["object_key"],
        )
        with response["Body"] as body:
            raw = body.read(case["size_bytes"] + 1)
            require(
                len(raw) == case["size_bytes"]
                and hashlib.sha256(raw).hexdigest() == case["sha256"]
                and body.read(1) == b"",
                "临时输入回收影响了规范原件",
            )


async def cleanup_inputs(factory, storage, project_id, backend):
    """Wait real retry dates, then run the actual maintenance service inline."""
    from botocore.exceptions import ClientError
    from sqlalchemy import func, select
    from app.db.models.prediction_input import PredictionInputLease
    from app.db.models.storage_maintenance import StorageOperation
    from app.db.models.task import Task
    from app.services.prediction_inputs import maintain

    async def rows():
        async with factory() as db:
            return list(
                await db.scalars(
                    select(PredictionInputLease).where(
                        PredictionInputLease.task_id.in_(
                            select(Task.id).where(Task.project_id == project_id)
                        )
                    )
                )
            )

    expected_leases = {event["lease_id"] for event in backend.events}
    initial = await rows()
    require(
        len(initial) == len(expected_leases)
        and {str(row.id) for row in initial} == expected_leases,
        "输入责任与实际模型下载不精确匹配",
    )
    require(
        initial and all(row.state == "cleanup_pending" for row in initial),
        "同 purpose 来源未保护首次回收，或输入没有封存",
    )
    for row in initial:
        require(row.next_cleanup_at is not None, "缺少真实回收重试时间")
        events = [event for event in backend.events if event["lease_id"] == str(row.id)]
        require(
            set(row.inventory["objects"]) == {event["relative"] for event in events},
            "输入清单缺失或增加模型未消费的对象",
        )
        require(
            row.source_ref["task_id"] == str(row.task_id)
            and row.source_ref["original"]["sha256"] == events[0]["sha256"],
            "输入账本丢失冻结来源",
        )
        for relative, entry in row.inventory["objects"].items():
            require(
                entry["state"] == "verified"
                and plumbing.exact_version(entry.get("version_id"))
                and all(
                    event["version_id"] == entry["version_id"]
                    and event["etag"] == entry["etag"]
                    and event["sha256"] == entry["sha256"]
                    and event["size_bytes"] == entry["size_bytes"]
                    for event in events
                    if event["relative"] == relative
                ),
                "真实签名 GET 和冻结版本收据不匹配",
            )
    deadline, maintenance_calls = time.monotonic() + 180, 0
    while time.monotonic() < deadline:
        current = await rows()
        pending = [row for row in current if row.state != "deleted"]
        if not pending:
            break
        due = min(row.next_cleanup_at for row in pending)
        wait = (due - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            await asyncio.sleep(min(1, wait))
            continue
        await maintain(factory)
        maintenance_calls += 1
        await asyncio.sleep(0.1)
    else:
        raise plumbing.WorktreeError("真实 next_cleanup_at 到期后仍未精确回收")
    final = await rows()
    require(
        len(final) == len(initial)
        and all(row.state == "deleted" and row.deleted_at is not None for row in final),
        "回收没有保留输入责任墓碑",
    )
    for row in final:
        require(
            row.inventory == next(old.inventory for old in initial if old.id == row.id),
            "回收改写了不可变物理收据",
        )
        prefix = "prediction-inputs/" + str(row.id) + "/"
        listing = await asyncio.to_thread(
            storage.client.list_object_versions, Bucket=row.bucket, Prefix=prefix
        )
        require(
            not listing.get("Versions")
            and not listing.get("DeleteMarkers")
            and not listing.get("IsTruncated"),
            "输入墓碑仍有实际版本或删除标记",
        )
        for relative, entry in row.inventory["objects"].items():
            try:
                await asyncio.to_thread(
                    storage.client.head_object,
                    Bucket=row.bucket,
                    Key=prefix + relative,
                    VersionId=entry["version_id"],
                )
            except ClientError as exc:
                require(
                    exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404,
                    "准确版本删除后状态不明确",
                )
            else:
                raise plumbing.WorktreeError("临时输入冻结版本仍可读取")
    async with factory() as db:
        require(
            await db.scalar(select(func.count()).select_from(StorageOperation)) == 0,
            "同步请求残留未结算操作",
        )
    return {
        "deleted_tombstones": len(final),
        "signed_gets": len(backend.events),
        "version_receipts_equal_signed_gets": True,
        "temporary_objects_versions_markers": 0,
        "unsettled_operations": 0,
        "waited_real_next_cleanup_at": True,
        "inline_maintenance_calls": maintenance_calls,
        "celery_started": False,
    }


async def _scenarios(backend, storage, report, *, expected_database):
    import httpx
    from sqlalchemy import func, select, text
    from app.db.base import async_session as factory
    from app.db.models.annotation import Annotation
    from app.db.models.async_job import AsyncJob
    from app.db.models.prediction import Prediction, PredictionMeta
    from app.db.models.project import Project
    from app.db.models.storage_maintenance import StorageOperation
    from app.db.models.task import Task
    from app.db.models.task_dataset_item_link import TaskDatasetItemLink
    from app.main import app
    from app.services.raster_mask_storage import load_coco_rle

    logging.getLogger("httpx").setLevel(logging.WARNING)
    require(not app.dependency_overrides, "验收拒绝 ASGI 依赖覆盖")
    async with factory() as db:
        require(
            await db.scalar(text("SELECT current_database()")) == expected_database,
            "应用连接不属于已验证 test 数据库",
        )
        require(
            await db.scalar(select(func.count()).select_from(StorageOperation)) == 0,
            "test 环境已有存储操作",
        )
    fixtures = await plumbing.seed(factory, storage, backend, report["run_id"])
    project_id = fixtures["project_id"]
    async with factory() as db:
        project = await db.get(Project, project_id)
        project.raster_mask_native_editing_enabled = True
        project.tool_bindings = {
            **project.tool_bindings,
            "region": {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            },
        }
        await db.commit()
    report["fixtures"] = {
        "project_id": str(project_id),
        "canonical_cases": [
            {
                name: str(case[name]) if name == "task_id" else case[name]
                for name in ("case", "task_id", "sha256", "size_bytes")
            }
            for case in fixtures["cases"]
        ],
    }
    base = f"/api/v1/projects/{project_id}/ml-backends/{fixtures['backend_id']}"
    responses = {}
    transport = httpx.ASGITransport(
        app=app,
        client=(
            f"127.2.{int(report['run_id'][:2], 16)}.{int(report['run_id'][2:4], 16)}",
            0,
        ),
    )
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://acceptance.local", timeout=60
        ) as client:
            login = await plumbing.request(
                client,
                "POST",
                "/api/v1/auth/login",
                json={"email": fixtures["email"], "password": fixtures["password"]},
            )
            auth = {"Authorization": "Bearer " + login["access_token"]}

            async def infer(case, name, *, test=False, context=None, headers=None):
                backend.begin(name)
                if test:
                    return await plumbing.request(
                        client,
                        "POST",
                        base + "/predict-test",
                        params={"task_id": str(case["task_id"])},
                        headers=headers or auth,
                    )
                return await plumbing.request(
                    client,
                    "POST",
                    base + "/interactive-annotating",
                    headers=headers or auth,
                    json={
                        "task_id": str(case["task_id"]),
                        "context": context or point_context(),
                    },
                )

            for case in fixtures["cases"]:
                tested = await infer(case, "predict-test-" + case["case"], test=True)
                require(len(tested["results"]) == 1, "predict-test 返回范围错误")
                response = await infer(case, "interactive-" + case["case"])
                candidate_identity(response, case)
                responses[case["case"]] = response
            report["checks"]["both_endpoints_three_canonical_owners"] = "passed"

            for case in (fixtures["cases"][0], fixtures["cases"][2]):
                first = responses[case["case"]]
                origin = candidate_identity(first, case)
                continued = await infer(
                    case,
                    "continue-" + case["case"],
                    context=point_context(
                        points=[[0.4, 0.4]], mask_input=first["mask_input_next"]
                    ),
                )
                require(
                    candidate_identity(continued, case) == origin, "续点改变规范来源"
                )
                url = f"/api/v1/tasks/{case['task_id']}/ai-mask-candidates/accept"
                create = accept_body(first)
                accepted = await plumbing.request(
                    client, "POST", url, headers=auth, json=create
                )
                annotation = accepted["annotation"]
                require(
                    annotation["geometry"]["mask"]["size"] == [96, 128],
                    "实际原生尺寸错误",
                )
                require(
                    await load_coco_rle(annotation["geometry"]["mask"])
                    == first["result"][0]["value"]["rle"],
                    "create未持久化实际原生Mask字节",
                )
                replayed = await plumbing.request(
                    client, "POST", url, headers=auth, json=create
                )
                require(
                    replayed["replayed"] and replayed["annotation"] == annotation,
                    "create精确重放失败",
                )
                refined = await infer(
                    case,
                    "refine-" + case["case"],
                    context=point_context(
                        mask_prompt_source={
                            "annotation_id": annotation["id"],
                            "source_version": annotation["version"],
                        }
                    ),
                )
                require(candidate_identity(refined, case) == origin, "精修改变规范来源")
                refine = accept_body(refined)
                refine["target"] = {
                    "mode": "refine",
                    "source_annotation_id": annotation["id"],
                    "source_version": annotation["version"],
                }
                refined_headers = {**auth, "If-Match": str(annotation["version"])}
                saved = await plumbing.request(
                    client, "POST", url, headers=refined_headers, json=refine
                )
                require(
                    saved["annotation"]["id"] == annotation["id"]
                    and saved["result_version"] == annotation["version"] + 1,
                    "精修没有原位原子更新",
                )
                require(
                    await load_coco_rle(saved["annotation"]["geometry"]["mask"])
                    == refined["result"][0]["value"]["rle"],
                    "refine未持久化更新后的实际Mask字节",
                )
                replay = await plumbing.request(
                    client, "POST", url, headers=refined_headers, json=refine
                )
                require(
                    replay["replayed"] and replay["annotation"] == saved["annotation"],
                    "refine精确重放失败",
                )
                late = await plumbing.request(
                    client, "POST", url, headers=auth, json=create
                )
                require(
                    late["replayed"] and late["annotation"] == annotation,
                    "正常编辑后同源create重放失效",
                )
                async with factory() as db:
                    require(
                        await db.scalar(
                            select(func.count())
                            .select_from(Annotation)
                            .where(Annotation.task_id == case["task_id"])
                        )
                        == 1,
                        "原生重放产生重复标注",
                    )
                    predictions = list(
                        await db.scalars(
                            select(Prediction).where(
                                Prediction.task_id == case["task_id"]
                            )
                        )
                    )
                    require(len(predictions) == 2, "原生create/refine候选数量错误")
                    for prediction in predictions:
                        metadata = await db.scalar(
                            select(PredictionMeta).where(
                                PredictionMeta.prediction_id == prediction.id
                            )
                        )
                        require(metadata is not None, "原生预测丢失溯源")
                        proof = metadata.extra["original_input"]
                        require(
                            all(
                                proof.get(key) == value for key, value in origin.items()
                            ),
                            "持久原生来源与签名候选不同",
                        )
                        plumbing.safe_evidence(proof)
                report["checks"]["native_create_refine_replay_" + case["case"]] = (
                    "passed"
                )

            async def gated_request(case, name, *, test=False, headers=None, mutation):
                backend.begin(name, gate=True)
                path = base + ("/predict-test" if test else "/interactive-annotating")
                pending = asyncio.create_task(
                    client.post(
                        path,
                        headers=headers or auth,
                        **(
                            {"params": {"task_id": str(case["task_id"])}}
                            if test
                            else {
                                "json": {
                                    "task_id": str(case["task_id"]),
                                    "context": point_context(),
                                }
                            }
                        ),
                    )
                )
                try:
                    require(
                        await asyncio.to_thread(backend.entered.wait, 30),
                        "未在真实signed GET后触发门控",
                    )
                    await mutation()
                finally:
                    backend.release.set()
                    if not pending.done():
                        await asyncio.wait_for(asyncio.shield(pending), 45)
                return await pending

            key = await plumbing.request(
                client,
                "POST",
                "/api/v1/me/api-keys",
                headers=auth,
                status=201,
                json={"name": "Synchronous revoke acceptance", "scopes": ["*"]},
            )

            async def revoke():
                await plumbing.request(
                    client,
                    "DELETE",
                    f"/api/v1/me/api-keys/{key['id']}",
                    headers=auth,
                    status=204,
                )

            denied = await gated_request(
                fixtures["cases"][0],
                "revoked-after-real-get",
                headers={"Authorization": "Bearer " + key["plaintext"]},
                mutation=revoke,
            )
            require(
                denied.status_code == 403
                and denied.json()["detail"].get("code") == "api_key_authority_revoked",
                "撤销原Key后仍交付交互结果",
            )
            report["checks"]["key_revoked_after_real_signed_get"] = "passed"

            async def rebind():
                async with factory() as db:
                    primary = await db.scalar(
                        select(TaskDatasetItemLink).where(
                            TaskDatasetItemLink.task_id
                            == fixtures["cases"][2]["task_id"],
                            TaskDatasetItemLink.role == "primary_lidar",
                        )
                    )
                    task = await db.get(Task, fixtures["cases"][1]["task_id"])
                    task.dataset_item_id = primary.dataset_item_id
                    task.file_path = fixtures["cases"][2]["object_key"]
                    task.file_name = "rebound.png"
                    await db.commit()

            denied = await gated_request(
                fixtures["cases"][1],
                "rebound-after-real-get",
                test=True,
                mutation=rebind,
            )
            require(
                denied.status_code == 409
                and denied.json()["detail"].get("code") == "content_binding_changed",
                "改绑规范原件后仍交付predict-test结果",
            )
            report["checks"]["canonical_rebind_after_real_signed_get"] = "passed"

        require(not backend.errors, "受控backend实际签名输入核验失败")
        require(
            sum(event["continuation"] for event in backend.interactions) == 2,
            "未真实消费两次logits续点",
        )
        require(
            sum(event["refine"] for event in backend.interactions) == 2,
            "未真实消费两次原生提示精修",
        )
        async with factory() as db:
            require(
                await db.scalar(
                    select(func.count())
                    .select_from(AsyncJob)
                    .where(AsyncJob.project_id == project_id)
                )
                == 0,
                "同步入口创建了伪后台作业",
            )
            require(
                await db.scalar(select(func.count()).select_from(StorageOperation))
                == 0,
                "同步原图/目标许可没有真实结算",
            )
        report["input_cleanup"] = await cleanup_inputs(
            factory, storage, project_id, backend
        )
        await source_versions_intact(storage, fixtures["cases"])
        report["checks"]["source_originals_survived_cleanup"] = "passed"
        report["checks"]["no_dependency_overrides_or_background_jobs"] = "passed"
        report["interactions"] = deepcopy(backend.interactions)
        report["cleanup"] = (
            "known-input-versions-deleted-original-fixtures-retained-for-launcher-destruction"
        )
    finally:
        backend.release.set()


async def qualify(backend, storage, report, *, expected_database):
    from app.db.base import engine

    try:
        return await _scenarios(
            backend, storage, report, expected_database=expected_database
        )
    finally:
        await engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    options = parser.parse_args(argv)
    os.environ.update(
        DEBUG="false",
        LOG_LEVEL="WARNING",
        ML_BACKEND_STORAGE_HOST="",
        RASTER_MASK_CREATE_ENABLED="true",
    )
    backend = resources = storage = None
    try:
        with plumbing.private_report(options.report) as report:
            report.update(
                qualification="real-asgi-http-minio-synchronous-controlled-native-responses",
                celery_started=False,
                exclusions=[
                    "real-model-accuracy-and-GPU",
                    "independent-TCP-API-and-browser",
                    "provider-unknown-close-and-crash-fault-injection",
                    "reference-images-and-video-consumers",
                ],
            )
            try:
                report["phase"] = "verify-owned-environment"
                resources = plumbing.verified_environment()
                from app.config import settings
                from app.services.storage import StorageService

                storage = StorageService()
                report["phase"] = "real-minio-version-contract"
                report["storage_contract"] = plumbing.lifecycle_and_versions(
                    storage, report["run_id"], resources
                )
                backend = InteractiveBackend(
                    f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
                    storage.import_bucket,
                )
                backend.thread.start()
                report["phase"] = "real-synchronous-http-native-scenarios"
                asyncio.run(
                    qualify(
                        backend,
                        storage,
                        report,
                        expected_database=resources.resources["database"],
                    )
                )
                report["status"] = "passed"
            finally:
                errors = []
                for name, operation in (
                    ("backend", backend.close if backend is not None else None),
                    ("storage", storage.client.close if storage is not None else None),
                    (
                        "resources",
                        resources.s3.close if resources is not None else None,
                    ),
                ):
                    if operation is not None:
                        try:
                            operation()
                        except BaseException as exc:
                            errors.append(
                                {"owner": name, "error_type": type(exc).__name__}
                            )
                if errors:
                    report["shutdown_errors"] = errors
                    raise plumbing.WorktreeError("同步验收句柄未确认全部关闭")
        print(
            "PASS: managed synchronous ASGI/HTTP/MinIO; controlled native responses only."
        )
        return 0
    except BaseException as exc:
        print(
            f"FAIL: managed synchronous acceptance ({type(exc).__name__}); inspect private report.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
