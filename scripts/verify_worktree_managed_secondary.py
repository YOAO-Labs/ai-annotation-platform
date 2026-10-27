"""Qualify managed secondary inference in an already owned test namespace.

Run through dev-worktree exec --mode test. Real ASGI auth, HTTP model transport
and MinIO are used; model outputs are controlled CPU responses. Fixtures remain
only in that disposable namespace for launcher destruction, never in dev.
"""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

import verify_worktree_managed_predictions as plumbing
from verify_worktree_managed_interactive import source_versions_intact


require = plumbing.require


def setup_contract():
    contract = plumbing.setup_contract()
    contract["name"] = "controlled-secondary-acceptance"
    contract["models"] = [
        model for model in contract["models"] if model["id"] in {"detect", "classify"}
    ]
    for model in contract["models"]:
        model["supported_inputs"] = ["crop"]
    return contract


class SecondaryBackend(plumbing.CPUBackend):
    """Download exact crop versions and verify all corners of a marked ROI."""

    def __init__(self, endpoint, bucket):
        super().__init__(endpoint, bucket)
        original = self.server.RequestHandlerClass

        class Handler(original):
            def do_GET(self):
                if urlsplit(self.path).path == "/setup":
                    self.reply(200, setup_contract())
                else:
                    super().do_GET()

        self.server.RequestHandlerClass = Handler
        self.current_case = None
        self.expected_downloads = 1
        self.first_download = threading.Event()

    def begin(self, name, case, *, gate=False, downloads=1, mode="success"):
        super().begin(name, mode, gate=gate)
        self.first_download.clear()
        with self.lock:
            self.current_case = deepcopy(case)
            self.expected_downloads = downloads

    def predict(self, payload):
        import httpx
        from PIL import Image

        with self.lock:
            case, scenario, mode, gate, expected_downloads = (
                self.current_case,
                self.scenario,
                self.mode,
                self.gated,
                self.expected_downloads,
            )
        require(case is not None, "未登记当前二次推理验收来源")
        tasks = payload.get("tasks")
        model = (payload.get("context") or {}).get("model_id")
        require(isinstance(tasks, list) and len(tasks) == 1, "单框投递数量错误")
        require(model in {"classify", "detect"}, "二次推理模型未按请求冻结")
        task = tasks[0]
        require(str(task["id"]) == "0", "ROI 索引改变")
        parsed = urlsplit(task["file_path"])
        require(f"{parsed.scheme}://{parsed.netloc}" == self.origin, "裁剪地址来源错误")
        query = {key.lower(): value for key, value in parse_qs(parsed.query).items()}
        require(bool(query.get("x-amz-signature")), "裁剪没有真实签名")
        versions = query.get("versionid", [])
        require(
            len(versions) == 1 and plumbing.exact_version(versions[0]), "裁剪未固定版本"
        )
        match = re.fullmatch(
            re.escape(self.bucket)
            + r"/prediction-inputs/([0-9a-f-]{36})/(crops/.+\.jpg)",
            unquote(parsed.path).lstrip("/"),
        )
        require(match is not None, "裁剪未使用独立输入归属")
        lease_id, relative = match.groups()
        with httpx.Client(
            trust_env=False, timeout=15, follow_redirects=False
        ) as client:
            response = client.get(task["file_path"])
        require(response.status_code == 200, "真实裁剪 GET 失败")
        require(response.headers.get("x-amz-version-id") == versions[0], "GET 版本错误")
        raw = response.content
        require(0 < len(raw) <= 1024 * 1024, "裁剪超过验收预算")
        digest = hashlib.sha256(raw).hexdigest()
        require(relative.endswith("-" + digest + ".jpg"), "裁剪完整摘要与对象名不一致")
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            require(image.size == (64, 48), "裁剪没有使用实际非方形图片尺寸")
            rgb = image.convert("RGB")
            try:
                for point in [(4, 4), (59, 4), (4, 43), (59, 43), (32, 24)]:
                    require(
                        max(
                            abs(a - b)
                            for a, b in zip(
                                rgb.getpixel(point), case["roi_rgb"], strict=True
                            )
                        )
                        <= 3,
                        "裁剪像素不是已登记原图的准确 ROI",
                    )
            finally:
                rgb.close()
        with self.lock:
            self.events.append(
                dict(
                    scenario=scenario,
                    task_id=str(case["task_id"]),
                    lease_id=lease_id,
                    relative=relative,
                    version_id=versions[0],
                    etag=response.headers.get("etag"),
                    sha256=digest,
                    size_bytes=len(raw),
                    dimensions=[64, 48],
                )
            )
            downloads = sum(event["scenario"] == scenario for event in self.events)
        self.first_download.set()
        if gate:
            if downloads >= expected_downloads:
                self.entered.set()
            require(self.release.wait(40), "真实裁剪 GET 后响应门控超时")
        if mode == "empty":
            shapes = []
        elif model == "classify":
            shapes = [
                {"attributes": {"color": "attempted-ai", "brand": "ai-" + case["case"]}}
            ]
        else:
            shape = plumbing.rectangle()
            if mode == "unknown-class":
                shape["value"]["rectanglelabels"] = ["unregistered-detector-class"]
            shapes = [shape]
        return [
            {
                "task": "0",
                "result": shapes,
                "score": 1.0,
                "model_version": "controlled-secondary",
            }
        ], False


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
        len(events) == len(backend.events) == len(initial), "请求输入没有一对一独立归属"
    )
    require(
        {str(row.id) for row in initial} == set(events), "输入账本与真实 GET 不匹配"
    )
    for row in initial:
        event = events[str(row.id)]
        case = next(
            case for case in fixtures["cases"] if case["task_id"] == row.task_id
        )
        require(
            row.owner_kind == "secondary_inference" and row.contract_version == 2,
            "输入责任借用了其他用途",
        )
        require(row.state in {"cleanup_pending", "deleted"}, "请求结束后输入仍活动")
        require(
            row.source_ref["original"]["sha256"] == case["sha256"], "裁剪丢失原件来源"
        )
        require(len(row.inventory["objects"]) == 1, "上传了无用途整图或多余输入")
        entry = row.inventory["objects"].get(event["relative"], {})
        require(
            entry.get("state") == "verified"
            and all(
                entry.get(key) == event[key]
                for key in ["version_id", "etag", "sha256", "size_bytes"]
            ),
            "裁剪回读收据与真实 GET 不一致",
        )
    deadline, calls = time.monotonic() + 180, 0
    while time.monotonic() < deadline:
        current = await rows()
        pending = [row for row in current if row.state != "deleted"]
        if not pending:
            break
        wait = (
            min(row.next_cleanup_at for row in pending) - datetime.now(timezone.utc)
        ).total_seconds()
        if wait > 0:
            await asyncio.sleep(min(1, wait))
        else:
            await maintain(factory)
            calls += 1
            await asyncio.sleep(0.1)
    else:
        raise plumbing.WorktreeError("真实期限后的裁剪清理未完成")
    for row in await rows():
        require(row.deleted_at is not None, "删除责任未保留 tombstone")
        require(
            row.inventory == next(old.inventory for old in initial if old.id == row.id),
            "回收改写了不可变收据",
        )
        prefix = "prediction-inputs/" + str(row.id) + "/"
        listing = await asyncio.to_thread(
            storage.client.list_object_versions, Bucket=row.bucket, Prefix=prefix
        )
        require(
            not listing.get("Versions")
            and not listing.get("DeleteMarkers")
            and not listing.get("IsTruncated"),
            "裁剪仍有物理版本或删除标记",
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
                raise plumbing.WorktreeError("裁剪准确版本仍可读取")
    async with factory() as db:
        require(
            await db.scalar(select(func.count()).select_from(StorageOperation)) == 0,
            "裁剪仍有未收尾存储操作",
        )
    return dict(
        deleted_tombstones=len(initial),
        signed_gets=len(backend.events),
        maintenance_calls=calls,
        temporary_objects_versions_markers=0,
        unsettled_operations=0,
        waited_real_next_cleanup_at=True,
    )


async def scenarios(backend, storage, report, expected_database):
    import httpx
    from sqlalchemy import func, select, text
    from app.db.base import async_session as factory
    from app.db.models.annotation import Annotation
    from app.db.models.async_job import AsyncJob
    from app.db.models.prediction import Prediction
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.task_dataset_item_link import TaskDatasetItemLink
    from app.main import app

    logging.getLogger("httpx").setLevel(logging.WARNING)
    require(not app.dependency_overrides, "验收禁止鉴权或 DB 依赖覆盖")
    async with factory() as db:
        require(
            await db.scalar(text("SELECT current_database()")) == expected_database,
            "应用未连接已验证测试库",
        )
    fixtures = await plumbing.seed(
        factory, storage, backend, report["run_id"], roi_marker=True
    )
    async with factory() as db:
        project = await db.get(Project, fixtures["project_id"])
        project.ai_interactive_enabled = False
        bindings = deepcopy(project.tool_bindings)
        bindings["bbox"]["attribute_schema"]["fields"].append(
            {"key": "brand", "label": "Brand", "type": "text"}
        )
        project.tool_bindings = bindings
        await db.commit()
    report["fixtures"] = {
        "project_id": str(fixtures["project_id"]),
        "canonical_cases": [case["case"] for case in fixtures["cases"]],
    }
    transport = httpx.ASGITransport(
        app=app,
        client=(
            f"127.3.{int(report['run_id'][:2], 16)}.{int(report['run_id'][2:4], 16)}",
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
        parents = {}
        for case in fixtures["cases"]:
            parent = await plumbing.request(
                client,
                "POST",
                f"/api/v1/tasks/{case['task_id']}/annotations",
                status=201,
                headers=auth,
                json={
                    "annotation_type": "bbox",
                    "class_name": "car",
                    "tool_unit_id": "bbox",
                    "geometry": {
                        "type": "bbox",
                        "x": 0.25,
                        "y": 0.25,
                        "w": 0.5,
                        "h": 0.5,
                    },
                    "attributes": {"color": "human", "brand": "old-ai"},
                },
            )
            parents[case["case"]] = parent["id"]
            async with factory() as db:
                annotation = await db.get(Annotation, uuid.UUID(parent["id"]))
                annotation.attributes_meta = {
                    "brand": {
                        "origin": "ai",
                        "model_ref": {
                            "backend_id": str(fixtures["backend_id"]),
                            "model_id": "old",
                        },
                    }
                }
                await db.commit()

        async def infer(case, *, geometry=False, headers=None):
            return await client.post(
                f"/api/v1/tasks/{case['task_id']}/annotations/{parents[case['case']]}/secondary-inference",
                headers=headers or auth,
                json={
                    "ml_backend_id": str(fixtures["backend_id"]),
                    "write_target": "geometry" if geometry else "attributes",
                    "model_id": "detect" if geometry else "classify",
                    "task": "detect" if geometry else "classify",
                    "pad": 0,
                },
            )

        for case in fixtures["cases"]:
            backend.begin("attributes-" + case["case"], case)
            response = await infer(case)
            require(response.status_code == 201, "三来源属性二次推理未成功")
            body = response.json()
            require(
                not body["created_children"]
                and body["annotation"]["attributes"]
                == {"color": "human", "brand": "ai-" + case["case"]},
                "人工属性被覆盖或 AI 属性未更新",
            )
            require(
                body["annotation"]["attributes_meta"]["brand"]["origin"] == "ai",
                "AI 属性溯源丢失",
            )
            backend.begin("geometry-" + case["case"], case)
            response = await infer(case, geometry=True)
            require(response.status_code == 201, "三来源几何二次推理未成功")
            children = response.json()["created_children"]
            require(
                len(children) == 1
                and children[0]["parent_annotation_id"] == parents[case["case"]]
                and children[0]["source"] == "prediction_based",
                "子框谱系错误",
            )
            geometry = children[0]["geometry"]
            require(
                all(
                    abs(geometry[key] - value) < 1e-9
                    for key, value in {
                        "x": 0.375,
                        "y": 0.375,
                        "w": 0.25,
                        "h": 0.25,
                    }.items()
                ),
                "ROI 坐标未按真实图片回映",
            )
        report["checks"]["three_sources_attributes_geometry_marked_roi"] = "passed"
        report["checks"]["interactive_disabled_does_not_disable_secondary"] = "passed"

        case = fixtures["cases"][0]
        backend.begin("empty-detection", case, mode="empty")
        response = await infer(case, geometry=True)
        require(
            response.status_code == 201 and not response.json()["created_children"],
            "无检出没有保留成功空结果语义",
        )
        backend.begin("unknown-class", case, mode="unknown-class")
        response = await infer(case, geometry=True)
        require(response.status_code == 201, "未知模型类别未按原合同回退")
        unknown = response.json()["created_children"]
        require(
            len(unknown) == 1
            and unknown[0]["class_name"] == "__unknown"
            and unknown[0]["parent_annotation_id"] == parents[case["case"]],
            "未知类别回退破坏子框谱系",
        )
        report["checks"]["empty_detection_and_unknown_class_fallback"] = "passed"

        backend.begin("same-parent-concurrent", case, gate=True, downloads=2)
        pending = [asyncio.create_task(infer(case, geometry=True))]
        try:
            require(
                await asyncio.to_thread(backend.first_download.wait, 25),
                "首个并发请求没有完成真实裁剪 GET",
            )
            pending.append(asyncio.create_task(infer(case, geometry=True)))
            require(
                await asyncio.to_thread(backend.entered.wait, 25),
                "同父标注两个请求未实际独立下载",
            )
            recent = [
                event
                for event in backend.events
                if event["scenario"] == "same-parent-concurrent"
            ]
            require(
                len(recent) == 2 and len({event["lease_id"] for event in recent}) == 2,
                "并发请求复用了裁剪对象",
            )
        finally:
            backend.release.set()
            concurrent = await asyncio.gather(*pending)
        require(
            all(response.status_code in {201, 409} for response in concurrent)
            and any(response.status_code == 201 for response in concurrent),
            "并发写入未遵守成功或冲突语义",
        )
        report["checks"]["same_parent_concurrent_independent_inputs"] = "passed"

        async def gated(case, name, mutation, *, headers=None):
            async with factory() as db:
                count_before = await db.scalar(
                    select(func.count())
                    .select_from(Annotation)
                    .where(Annotation.task_id == case["task_id"])
                )
            backend.begin(name, case, gate=True)
            pending = asyncio.create_task(infer(case, geometry=True, headers=headers))
            try:
                require(
                    await asyncio.to_thread(backend.entered.wait, 25),
                    "真实裁剪 GET 未进入响应门控",
                )
                await mutation()
            finally:
                backend.release.set()
                response = await asyncio.wait_for(asyncio.shield(pending), 45)
            async with factory() as db:
                require(
                    await db.scalar(
                        select(func.count())
                        .select_from(Annotation)
                        .where(Annotation.task_id == case["task_id"])
                    )
                    == count_before,
                    "被拒推理留下部分子框",
                )
            return response

        key = await plumbing.request(
            client,
            "POST",
            "/api/v1/me/api-keys",
            headers=auth,
            status=201,
            json={"name": "Secondary only write", "scopes": ["annotations:write"]},
        )
        key_headers = {"Authorization": "Bearer " + key["plaintext"]}
        backend.begin("write-scope-only", case)
        require(
            (await infer(case, headers=key_headers)).status_code == 201,
            "二次推理增加了额外读取 scope",
        )

        async def revoke():
            await plumbing.request(
                client,
                "DELETE",
                f"/api/v1/me/api-keys/{key['id']}",
                headers=auth,
                status=204,
            )

        require(
            (
                await gated(case, "revoked-after-real-get", revoke, headers=key_headers)
            ).status_code
            == 403,
            "撤 Key 后仍写入结果",
        )
        report["checks"]["write_scope_only_and_revocation_after_get"] = "passed"

        async def edit_parent():
            async with factory() as db:
                parent = await db.get(Annotation, uuid.UUID(parents[case["case"]]))
                parent.attributes = {**parent.attributes, "brand": "concurrent-human"}
                parent.attributes_meta = {}
                parent.version += 1
                await db.commit()

        require(
            (await gated(case, "parent-edited-after-real-get", edit_parent)).status_code
            == 409,
            "父标注变化后仍提交旧结果",
        )
        async with factory() as db:
            parent = await db.get(Annotation, uuid.UUID(parents[case["case"]]))
            require(
                parent.attributes["brand"] == "concurrent-human"
                and not parent.attributes_meta,
                "并发人工属性被覆盖",
            )
        report["checks"]["parent_edit_after_get_preserved_without_partial_children"] = (
            "passed"
        )

        case = fixtures["cases"][1]

        async def rebind():
            async with factory() as db:
                primary = await db.scalar(
                    select(TaskDatasetItemLink).where(
                        TaskDatasetItemLink.task_id == fixtures["cases"][2]["task_id"],
                        TaskDatasetItemLink.role == "primary_lidar",
                    )
                )
                task = await db.get(Task, case["task_id"])
                task.dataset_item_id, task.file_path = (
                    primary.dataset_item_id,
                    fixtures["cases"][2]["object_key"],
                )
                await db.commit()

        require(
            (await gated(case, "rebound-after-real-get", rebind)).status_code == 409,
            "原件改绑后仍写入结果",
        )
        report["checks"]["canonical_rebind_after_real_get"] = "passed"
    require(not backend.errors, "实际裁剪下载或像素核验失败")
    async with factory() as db:
        require(
            await db.scalar(
                select(func.count())
                .select_from(Prediction)
                .where(
                    Prediction.task_id.in_(
                        [case["task_id"] for case in fixtures["cases"]]
                    )
                )
            )
            == 0,
            "同步二次推理生成了额外候选",
        )
        require(
            await db.scalar(
                select(func.count())
                .select_from(AsyncJob)
                .where(AsyncJob.project_id == fixtures["project_id"])
            )
            == 0,
            "同步二次推理生成了后台作业",
        )
    report["input_cleanup"] = await cleanup_inputs(factory, storage, fixtures, backend)
    await source_versions_intact(storage, fixtures["cases"])
    report["checks"]["originals_survived_cleanup"] = "passed"
    report["cleanup"] = (
        "exact-crop-versions-deleted-original-fixtures-retained-for-launcher-destruction"
    )


async def qualify(backend, storage, report, expected_database):
    from app.db.base import engine

    try:
        await scenarios(backend, storage, report, expected_database)
    finally:
        await engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    options = parser.parse_args(argv)
    os.environ.update(DEBUG="false", LOG_LEVEL="WARNING", ML_BACKEND_STORAGE_HOST="")
    backend = resources = storage = None
    try:
        with plumbing.private_report(options.report) as report:
            report.update(
                qualification="real-asgi-http-minio-secondary-controlled-cpu-responses",
                celery_started=False,
                exclusions=[
                    "real-model-accuracy-and-GPU",
                    "independent-TCP-API-and-browser",
                    "provider-unknown-close-and-crash-fault-injection",
                    "reference-and-video-consumers",
                ],
            )
            try:
                resources = plumbing.verified_environment()
                from app.config import settings
                from app.services.storage import StorageService

                storage = StorageService()
                report["storage_contract"] = plumbing.lifecycle_and_versions(
                    storage, report["run_id"], resources
                )
                backend = SecondaryBackend(
                    f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
                    storage.import_bucket,
                )
                backend.thread.start()
                asyncio.run(
                    qualify(backend, storage, report, resources.resources["database"])
                )
                report["status"] = "passed"
            finally:
                errors = []
                for close in [
                    backend.close if backend else None,
                    storage.client.close if storage else None,
                    resources.s3.close if resources else None,
                ]:
                    if close is not None:
                        try:
                            close()
                        except BaseException as exc:
                            errors.append(type(exc).__name__)
                if errors:
                    report["shutdown_errors"] = errors
                    raise plumbing.WorktreeError("二次推理验收句柄未确认全部关闭")
        print("PASS: managed secondary ASGI/HTTP/MinIO; controlled CPU responses only.")
        return 0
    except BaseException as exc:
        print(
            f"FAIL: managed secondary ({type(exc).__name__}); inspect private report.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
