"""Qualify canonical image repair with real ASGI, media Celery and MinIO.

Run only through the owned launcher test mode with a private report path. The
fixture seeds QC source masks; all repair plans, attempts, candidate reads,
acceptance and rollback use production APIs and the actual media queue. A
controlled HTTP backend verifies signed original bytes, not real ML accuracy.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import sys
import time
import uuid

import verify_worktree_managed_interactive as native
import verify_worktree_managed_predictions as plumbing


require = plumbing.require
FORWARD = "app.workers.mask_repair.run_mask_repair"
ROLLBACK = "app.workers.mask_repair.rollback_mask_repair"


class MediaWorker(plumbing.Worker):
    def __init__(self, path):
        super().__init__(path)
        self.queue = "media"

    def start(self):
        super().start()
        require(
            {FORWARD, ROLLBACK} <= set(self.inspect("registered").get(self.node, [])),
            "独立 media Worker 未注册正向和回滚任务",
        )


class RepairBackend(native.InteractiveBackend):
    def respond(self, payload):
        # This performs a real version-pinned HTTP GET, exact bytes/SHA/size
        # and non-square image/pixel validation before returning controlled RLE.
        result = super().respond(payload)
        require("task" in payload, "修复必须使用同步原生 Mask 协议")
        if self.mode == "repair_failure":
            raise RuntimeError("controlled repair backend failure")
        return result


def assert_public(value):
    private = {
        "original_input",
        "original_annotation",
        "original_annotations",
        "original_model",
        "repair_authority",
        "credentials",
        "endpoint_url",
        "bucket",
        "storage_identity",
        "key_proof",
    }
    if isinstance(value, dict):
        require(not private.intersection(value), "公开修复响应泄露内部来源或凭据")
        for child in value.values():
            assert_public(child)
    elif isinstance(value, list):
        for child in value:
            assert_public(child)


def same_reference(actual, expected):
    return (
        {key: actual.get(key) for key in expected} == expected
        and set(actual) - set(expected) <= {"storage_encoding"}
        and actual.get("storage_encoding") in {None, "identity"}
    )


async def seed(factory, storage, backend, run_id):
    from app.core.security import hash_password
    from app.db.models.annotation import Annotation
    from app.db.models.mask_qc import MaskQCIssue
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.user import User
    from app.services.raster_mask_storage import (
        build_rle_reference,
        store_coco_rle,
        reserve_raster_mask_upload,
        lock_raster_mask_references,
    )

    fixtures = await plumbing.seed(factory, storage, backend, run_id)
    source = {"encoding": "coco_rle", "size": [96, 128], "counts": [1, 2, 12284, 1]}
    island = {"encoding": "coco_rle", "size": [96, 128], "counts": [12287, 1]}
    async with factory() as db:
        project = await db.get(Project, fixtures["project_id"])
        project.type_key, project.type_label = "image-seg", "图像分割"
        project.raster_mask_native_editing_enabled = True
        project.ai_interactive_enabled = False
        project.tool_bindings = {
            "region": {
                "enabled": True,
                "classes": [{"name": "car"}],
                "attribute_schema": {"fields": []},
            }
        }
        author = User(
            email=f"repair-author-{run_id}@acceptance.invalid",
            name="Repair source author",
            role="employee",
            is_active=True,
            password_hash=hash_password(uuid.uuid4().hex),
        )
        db.add(author)
        await db.flush()
        for rle in (source, island):
            await store_coco_rle(rle, db=db)
        for case in fixtures["cases"]:
            task = await db.get(Task, case["task_id"])
            task.status, task.reviewer_id = "review", fixtures["actor_id"]
            task.annotation_contributor_ids = [str(author.id)]
            task.review_contributor_ids = [str(author.id)]
            task.review_submitter_id, task.review_round_id = author.id, uuid.uuid4()
            annotation = Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=author.id,
                source="manual",
                annotation_type="raster_mask",
                tool_unit_id="region",
                class_name="car",
                geometry={"type": "raster_mask", "mask": build_rle_reference(source)},
                is_active=True,
                was_cancelled=False,
                is_locked=False,
                version=1,
            )
            db.add(annotation)
            await db.flush()
            await reserve_raster_mask_upload(
                db,
                task_id=task.id,
                object_key=build_rle_reference(source)["object_key"],
                limit=100,
            )
            await lock_raster_mask_references(db, annotation.geometry, task_id=task.id)
            ref = build_rle_reference(island)
            issue = MaskQCIssue(
                project_id=project.id,
                task_id=task.id,
                annotation_id=annotation.id,
                annotation_version=1,
                related_annotation_ids=[annotation.id],
                source_versions={str(annotation.id): 1},
                code="small_island",
                severity="warning",
                severity_rank=1,
                status="open",
                metric={"area_pixels": 1},
                threshold={"small_component_pixels": 32},
                region_bbox={"x0": 0.05, "y0": 0.05, "x1": 0.9, "y1": 0.9},
                region_mask_ref=ref,
                region_digest=ref["sha256"],
                dedupe_key=hashlib.sha256((run_id + case["case"]).encode()).hexdigest(),
                source={"kind": "single_frame"},
            )
            db.add(issue)
            await db.flush()
            case.update(annotation_id=annotation.id, issue_id=issue.id)
        await db.commit()
    return fixtures, source


async def cleanup_inputs(factory, storage, fixtures, backend):
    from botocore.exceptions import ClientError
    from sqlalchemy import func, select
    from app.db.models.prediction_input import PredictionInputLease
    from app.db.models.storage_maintenance import StorageOperation
    from app.services.prediction_inputs import maintain

    async def rows():
        async with factory() as db:
            return list(
                await db.scalars(
                    select(PredictionInputLease).where(
                        PredictionInputLease.task_id.in_(
                            [case["task_id"] for case in fixtures["cases"]]
                        )
                    )
                )
            )

    initial = await rows()
    events = {event["lease_id"]: event for event in backend.events}
    require(
        len(initial) == len(events) == len(backend.events) > 0,
        "SAM 输入账本与实际签名 GET 不一一对应",
    )
    require({str(row.id) for row in initial} == set(events), "输入责任范围不符")
    for row in initial:
        event = events[str(row.id)]
        require(
            row.owner_kind == "mask_repair" and row.contract_version == 2,
            "修复借用其他用途的输入责任",
        )
        require(row.state in {"cleanup_pending", "deleted"}, "输入没有真实关闭")
        require(len(row.inventory["objects"]) == 1, "SAM 上传了多余输入")
        require(
            row.source_ref["binding"]["original"]["sha256"] == event["sha256"],
            "输入来源改变",
        )
        entry = row.inventory["objects"].get(event["relative"], {})
        require(
            entry.get("state") == "verified"
            and all(
                entry.get(key) == event[key]
                for key in ("version_id", "etag", "sha256", "size_bytes")
            ),
            "真实签名 GET 与独立版本回执不符",
        )
    deadline, calls = time.monotonic() + 180, 0
    while time.monotonic() < deadline:
        pending = [row for row in await rows() if row.state != "deleted"]
        if not pending:
            break
        require(
            all(row.next_cleanup_at is not None for row in pending), "回收缺少实际期限"
        )
        due = min(row.next_cleanup_at for row in pending)
        if due <= datetime.now(timezone.utc):
            await maintain(factory)
            calls += 1
        await asyncio.sleep(0.2)
    else:
        raise plumbing.WorktreeError("真实清理期限到达后输入仍未回收")
    for row in await rows():
        require(row.deleted_at is not None, "输入墓碑不完整")
        prefix = row.storage_ref["key_prefix"]
        listing = await asyncio.to_thread(
            storage.client.list_object_versions, Bucket=row.bucket, Prefix=prefix
        )
        require(
            not listing.get("Versions")
            and not listing.get("DeleteMarkers")
            and not listing.get("IsTruncated"),
            "SAM 临时版本或标记仍存在",
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
                    "版本删除结果不明确",
                )
            else:
                raise plumbing.WorktreeError("精确输入版本仍可读取")
    async with factory() as db:
        require(
            await db.scalar(select(func.count()).select_from(StorageOperation)) == 0,
            "真实操作尚未全部收尾",
        )
    return dict(
        deleted_tombstones=len(initial),
        signed_gets=len(events),
        maintenance_calls=calls,
        temporary_versions_and_markers=0,
        waited_real_next_cleanup_at=True,
    )


async def qualify(backend, worker, storage, report, expected_database):
    import httpx
    from sqlalchemy import select, text
    from app.db.base import async_session as factory
    from app.db.models.annotation import Annotation
    from app.db.models.api_key import ApiKey
    from app.db.models.async_job import AsyncJob
    from app.db.models.mask_qc import MaskQCIssue
    from app.db.models.mask_repair_batch import MaskRepairBatch
    from app.db.models.prediction import Prediction
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.main import app
    from app.services.raster_mask_storage import build_rle_reference, load_coco_rle

    logging.getLogger("httpx").setLevel(logging.WARNING)
    require(not app.dependency_overrides, "真实验收禁止覆盖认证或数据库依赖")
    async with factory() as db:
        require(
            await db.scalar(text("SELECT current_database()")) == expected_database,
            "真实应用连接了错误数据库",
        )
    fixtures, source = await seed(factory, storage, backend, report["run_id"])
    report["fixtures"] = {
        "project_id": str(fixtures["project_id"]),
        "canonical_cases": [case["case"] for case in fixtures["cases"]],
    }
    await asyncio.to_thread(worker.start)
    require(worker.process.poll() is None, "media Worker 没有真实启动")
    report["worker"] = {
        "node": worker.node,
        "queue": worker.queue,
        "registered_forward_and_rollback": True,
    }
    transport = httpx.ASGITransport(
        app=app,
        client=(
            f"127.4.{int(report['run_id'][:2], 16)}.{int(report['run_id'][2:4], 16)}",
            0,
        ),
    )
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

        async def plan(case, *, sam=False, headers=None):
            async with factory() as db:
                project = await db.get(Project, fixtures["project_id"])
                project.ai_interactive_enabled = sam
                task = await db.get(Task, case["task_id"])
                annotation = await db.get(Annotation, case["annotation_id"])
                previous = await db.get(MaskQCIssue, case["issue_id"])
                # A new QC observation follows rollback; retain the resolved
                # issue as history instead of rewriting its repair outcome.
                issue = MaskQCIssue(
                    project_id=project.id,
                    task_id=task.id,
                    annotation_id=annotation.id,
                    annotation_version=annotation.version,
                    related_annotation_ids=[annotation.id],
                    source_versions={str(annotation.id): annotation.version},
                    code="boundary_noise" if sam else "small_island",
                    severity="warning",
                    severity_rank=1,
                    status="open",
                    metric=deepcopy(previous.metric),
                    threshold=deepcopy(previous.threshold),
                    region_bbox=deepcopy(previous.region_bbox),
                    region_mask_ref=deepcopy(previous.region_mask_ref),
                    region_digest=previous.region_digest,
                    dedupe_key=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                    source={"kind": "single_frame"},
                )
                db.add(issue)
                await db.flush()
                case["issue_id"] = issue.id
                task.status = "review"
                await db.commit()
            body = await plumbing.request(
                client,
                "POST",
                f"/api/v1/projects/{fixtures['project_id']}/mask-qc/repairs:dry-run",
                headers=headers or auth,
                json={
                    "actions": [
                        {
                            "issue_id": str(case["issue_id"]),
                            "kind": "rerun_local_sam"
                            if sam
                            else "delete_small_islands",
                            **(
                                {"backend_id": str(fixtures["backend_id"])}
                                if sam
                                else {}
                            ),
                        }
                    ]
                },
            )
            require(
                body["summary"]["executable_count"] == 1, "真实修复计划未生成可执行项"
            )
            assert_public(body)
            return body

        async def execute(planned, *, headers=None):
            result = await plumbing.request(
                client,
                "POST",
                f"/api/v1/projects/{fixtures['project_id']}/mask-qc/repairs",
                status=202,
                headers=headers or auth,
                json={
                    "receipt": planned["receipt"],
                    "plan_digest": planned["plan_digest"],
                },
            )
            assert_public(result)
            return result

        async def done(batch, *, key="async_job_id", expected="completed"):
            job = await plumbing.job_done(factory, uuid.UUID(batch[key]), worker)
            require(job.status == expected, "真实修复 Job 终态不符")
            out = await plumbing.request(
                client, "GET", f"/api/v1/mask-qc/repairs/{batch['id']}", headers=auth
            )
            assert_public(out)
            return out

        candidate_rle = {
            "encoding": "coco_rle",
            "size": [96, 128],
            "counts": [1, 2, 12285],
        }
        for case in fixtures["cases"]:
            batch = await execute(await plan(case))
            completed = await done(batch)
            require(not backend.events, "确定性修复不应创建或消费 ML 输入")
            async with factory() as db:
                annotation = await db.get(Annotation, case["annotation_id"])
                require(
                    annotation.version == 2
                    and same_reference(
                        annotation.geometry["mask"], build_rle_reference(candidate_rle)
                    ),
                    "确定性修复实际像素或版本错误",
                )
                require(
                    await load_coco_rle(annotation.geometry["mask"], db=db)
                    == candidate_rle,
                    "确定性结果实际对象不匹配",
                )
            rolled = await plumbing.request(
                client,
                "POST",
                f"/api/v1/mask-qc/repairs/{batch['id']}/rollback",
                status=202,
                headers=auth,
                json={"expected_result_digest": completed["result_digest"]},
            )
            final = await done(rolled, key="rollback_async_job_id")
            require(final["status"] == "rolled_back", "严格回滚未完成")
            async with factory() as db:
                annotation = await db.get(Annotation, case["annotation_id"])
                require(
                    annotation.version == 3
                    and same_reference(
                        annotation.geometry["mask"], build_rle_reference(source)
                    ),
                    "回滚没有恢复精确原 Mask",
                )
        report["checks"]["three_origins_deterministic_ai_off_and_strict_rollback"] = (
            "passed"
        )

        for case in fixtures["cases"]:
            backend.begin("sam-" + case["case"])
            batch = await execute(await plan(case, sam=True))
            await done(batch)
            async with factory() as db:
                prediction = await db.scalar(
                    select(Prediction).where(Prediction.task_id == case["task_id"])
                )
                require(
                    prediction is not None and prediction.source == "mask_repair",
                    "SAM 候选未落库",
                )
                prediction_id = prediction.id
            base = f"/api/v1/tasks/{case['task_id']}/predictions/{prediction_id}"
            content = await plumbing.request(
                client, "GET", base + "/mask-content/0", headers=auth
            )
            require(content == candidate_rle, "候选实际内容读取失败")
            accepted = await plumbing.request(
                client, "POST", base + "/accept", headers=auth
            )
            require(len(accepted) == 1, "候选采纳未生成一个标注")
            readable = await plumbing.request(
                client,
                "GET",
                f"/api/v1/annotations/{accepted[0]['id']}/mask-content",
                headers=auth,
            )
            require(readable == candidate_rle, "已接受 Mask 的实际读取不匹配")
        report["checks"]["three_origins_sam_preview_accept_and_accepted_read"] = (
            "passed"
        )

        case = fixtures["cases"][0]
        key = await plumbing.request(
            client,
            "POST",
            "/api/v1/me/api-keys",
            status=201,
            headers=auth,
            json={"name": "Repair original request", "scopes": ["annotations:write"]},
        )
        scoped = {"Authorization": "Bearer " + key["plaintext"]}
        backend.begin("revoke-after-actual-get", gate=True)
        failed_batch = await execute(
            await plan(case, sam=True, headers=scoped), headers=scoped
        )
        require(
            await asyncio.to_thread(backend.entered.wait, 30), "真实 SAM GET 未到达门控"
        )
        async with factory() as db:
            (await db.get(ApiKey, uuid.UUID(key["id"]))).revoked_at = datetime.now(
                timezone.utc
            )
            frozen = deepcopy(
                (await db.get(MaskRepairBatch, uuid.UUID(failed_batch["id"]))).plan_json
            )
            await db.commit()
        backend.release.set()
        await done(failed_batch, expected="failed")
        fresh = await plumbing.request(
            client,
            "POST",
            "/api/v1/me/api-keys",
            status=201,
            headers=auth,
            json={"name": "Repair resumed request", "scopes": ["annotations:write"]},
        )
        async with factory() as db:
            (await db.get(Task, case["task_id"])).version += 1
            await db.commit()
        backend.begin("fresh-request-resume")
        resumed = await plumbing.request(
            client,
            "POST",
            f"/api/v1/mask-qc/repairs/{failed_batch['id']}/resume",
            status=202,
            headers={"Authorization": "Bearer " + fresh["plaintext"]},
        )
        require(
            resumed["async_job_id"] != failed_batch["async_job_id"], "续做未创建新尝试"
        )
        await done(resumed)
        async with factory() as db:
            batch = await db.get(MaskRepairBatch, uuid.UUID(resumed["id"]))
            require(batch.plan_json == frozen, "续做改写了冻结的来源或模型")
        report["checks"]["original_write_only_key_revocation_and_fresh_resume"] = (
            "passed"
        )
        report["checks"]["public_responses_hide_private_proofs"] = "passed"
    async with factory() as db:
        jobs = list(
            await db.scalars(
                select(AsyncJob).where(AsyncJob.project_id == fixtures["project_id"])
            )
        )
        statuses = dict(Counter(row.status for row in jobs))
        require(statuses == {"completed": 10, "failed": 1}, "真实修复作业数或终态不符")
        report["job_statuses"] = statuses
        predictions = list(
            await db.scalars(
                select(Prediction).where(
                    Prediction.project_id == fixtures["project_id"]
                )
            )
        )
        require(
            len(predictions) == 4
            and all(row.source == "mask_repair" for row in predictions),
            "撤权后留下半候选，或续做重复生成",
        )
        report["persisted_sam_candidates"] = len(predictions)
    require(not backend.errors, "受控 HTTP 后端发生输入核验错误")
    require(await asyncio.to_thread(worker.idle), "真实 media Worker 尚未空闲")
    report["input_cleanup"] = await cleanup_inputs(factory, storage, fixtures, backend)
    await native.source_versions_intact(storage, fixtures["cases"])
    report["checks"]["canonical_source_bytes_preserved_after_exact_input_cleanup"] = (
        "passed"
    )
    report["cleanup"] = (
        "input-versions-removed-owned-fixtures-retained-for-launcher-destruction"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    options = parser.parse_args(argv)
    os.environ.update(
        DEBUG="false",
        LOG_LEVEL="WARNING",
        ML_BACKEND_STORAGE_HOST="",
        RASTER_MASK_CREATE_ENABLED="true",
        RASTER_MASK_READ_ENABLED="true",
    )
    backend = worker = resources = storage = None
    try:
        with plumbing.private_report(options.report) as report:
            report["qualification"] = (
                "real-asgi-http-media-celery-minio-controlled-native-responses"
            )
            report["exclusions"] = [
                "real-model-and-GPU",
                "independent-TCP-API-and-browser",
                "production-deployment",
            ]
            try:
                report["phase"] = "verify-owned-test-environment"
                resources = plumbing.verified_environment()
                from app.config import settings
                from app.services.storage import StorageService

                storage = StorageService()
                report["storage_contract"] = plumbing.lifecycle_and_versions(
                    storage, report["run_id"], resources
                )
                backend = RepairBackend(
                    f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
                    storage.import_bucket,
                )
                backend.thread.start()
                worker = MediaWorker(options.report.with_suffix(".worker.log"))
                report["phase"] = "real-media-worker-repair-scenarios"
                asyncio.run(
                    qualify(
                        backend,
                        worker,
                        storage,
                        report,
                        resources.resources["database"],
                    )
                )
                report["status"] = "passed"
            finally:
                if backend is not None:
                    backend.release.set()
                errors = []
                for name, operation in (
                    ("worker", worker.stop if worker else None),
                    ("backend", backend.close if backend else None),
                    ("storage", storage.client.close if storage else None),
                    ("resources", resources.s3.close if resources else None),
                ):
                    if operation is not None:
                        try:
                            result = operation()
                            if name == "worker":
                                report["worker_shutdown"] = result
                                require(
                                    not result["started"]
                                    or (
                                        result["idle_before_stop"]
                                        and result["graceful"]
                                        and result["exit_code"] == 0
                                    ),
                                    "media Worker 未空闲优雅退出",
                                )
                        except BaseException as exc:
                            errors.append(
                                {"owner": name, "error_type": type(exc).__name__}
                            )
                if errors:
                    report["shutdown_errors"] = errors
                    raise plumbing.WorktreeError("验收进程或句柄未全部确认关闭")
        print(
            "PASS: managed repair ASGI/HTTP/media Celery/MinIO; controlled native responses."
        )
        return 0
    except BaseException as exc:
        print(
            f"FAIL: managed repair acceptance ({type(exc).__name__}); inspect private report.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
