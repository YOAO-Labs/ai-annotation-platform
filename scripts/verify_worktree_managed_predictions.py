"""Live managed-image prediction plumbing in an already owned test environment.

Run only through ``dev-worktree.mjs exec --mode test``. This command provisions
nothing and destroys no namespace. Its committed fixture belongs to that test
namespace; the launcher owner removes it after inspecting the private report.
The HTTP CPU backend returns controlled shapes, never real model inference.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

from worktree_env import (  # noqa: E402
    WorktreeError,
    application_environment,
    atomic_json,
    load_identity,
    topology,
)
from worktree_resources import LocalResources, migration_graph  # noqa: E402
from worktree_runtime import (  # noqa: E402
    require_owner,
    validate_inventory,
    validate_revisions,
    worker_queues_match,
)

BATCH_TASK = "app.workers.tasks.batch_predict"
RETRY_TASK = "app.workers.predictions_retry.retry_failed_prediction"
CLEANUP_TASK = "app.workers.prediction_input_cleanup.recover_prediction_inputs"
TERMINAL = {"completed", "failed", "cancelled"}
PRIVATE_KEYS = {
    "endpoint_url",
    "bucket",
    "object_key",
    "file_path",
    "file_name",
    "facts",
    "credentials",
    "storage_identity",
    "key_proof",
    "api_key_id",
    "token",
}


def require(value, message):
    if not value:
        raise WorktreeError(message)


def exact_version(value):
    return (
        isinstance(value, str)
        and value != "null"
        and 0 < len(value) <= 1024
        and all(32 < ord(char) < 127 for char in value)
    )


def verified_environment():
    """Check actual markers, all target overrides and the existing migration head."""
    from app.config import settings
    from sqlalchemy.engine import make_url

    require(__debug__, "验收拒绝 Python 优化模式")
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "仅允许 owned test 环境")
    resources = topology(ROOT, load_identity(ROOT), "test")
    backend = LocalResources(resources, settings, code_root=ROOT)
    try:
        observed = backend.inspect()
        validate_inventory(resources, observed)
        for name, row in (
            (resources["database"], observed["database"]),
            (resources["redis_name"], observed["redis"]),
            *observed["buckets"].items(),
        ):
            require(require_owner(name, row, resources["owner"]), "验收资源尚未就绪")
        require(observed["redis"]["running"], "owned Redis 未运行")
        expected = backend.environment()
        for key in (
            "DATABASE_URL",
            "MIGRATION_DATABASE_URL",
            "TEST_DATABASE_URL",
            "REDIS_URL",
            "CELERY_BROKER_URL",
            "CELERY_BROKER_READ_URL",
            "CELERY_BROKER_WRITE_URL",
            "CELERY_RESULT_BACKEND",
            "MINIO_ENDPOINT",
            "TMPDIR",
            *resources["buckets"],
        ):
            require(os.environ.get(key) == expected[key], "exec 环境与已验证资源不一致")
        require(
            make_url(settings.database_url).database == resources["database"],
            "应用数据库不属于当前工作树 test 模式",
        )
        validate_revisions(
            *migration_graph(ROOT), asyncio.run(backend.revisions()), skip=True
        )
        return backend
    except BaseException:
        backend.s3.close()
        raise


def setup_contract():
    models = []
    for identity, task, inputs, geometry in (
        ("detect", "detect", ["full_image"], ["bbox"]),
        ("geometry", "detect", ["bbox_prompt"], ["bbox"]),
        ("classify", "classify", ["crop"], []),
    ):
        models.append(
            {
                "id": identity,
                "task": task,
                "infra": "onnx",
                "supported_inputs": inputs,
                "supported_geometric_outputs": geometry,
                "supported_prompts": ["bbox"] if identity == "geometry" else ["none"],
                "output_attribute_types": ["class"] if identity == "classify" else [],
                "resource_profile": {"device": "cpu", "batchable": True},
                "classes": [{"index": 0, "name": "car"}],
            }
        )
    return {
        "name": "controlled-prediction-acceptance",
        "version": "1",
        "protocol_version": "2",
        "task_contract_revision": 1,
        "infra": "onnx",
        "models": models,
    }


def rectangle():
    return {
        "type": "rectanglelabels",
        "value": {
            "x": 25,
            "y": 25,
            "width": 50,
            "height": 50,
            "rectanglelabels": ["car"],
        },
    }


class CPUBackend:
    """Actually download every signed input; never substitute local fixture bytes."""

    def __init__(self, endpoint, bucket):
        self.origin, self.bucket = endpoint, bucket
        self.expected, self.leases, self.events, self.errors = {}, {}, [], []
        self.lock = threading.Lock()
        self.mode, self.scenario = "success", "bootstrap"
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.gated = False
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
                path = urlsplit(self.path).path
                if path == "/setup":
                    self.reply(200, setup_contract())
                elif path == "/health":
                    self.reply(
                        200,
                        {
                            "status": "ok",
                            "model_version": "controlled-cpu",
                            "compute": {"effective_device": "cpu"},
                        },
                    )
                else:
                    self.reply(404, {"detail": "unknown acceptance endpoint"})

            def do_POST(self):
                try:
                    require(self.path == "/predict", "非预期 backend 路径")
                    size = int(self.headers.get("Content-Length", 0))
                    require(0 < size <= 256 * 1024, "backend 请求超过验收预算")
                    payload = json.loads(self.rfile.read(size))
                    result, failed = owner.predict(payload)
                    self.reply(
                        503 if failed else 200,
                        {"detail": "controlled failure"}
                        if failed
                        else {"results": result},
                    )
                except Exception as exc:
                    with owner.lock:
                        owner.errors.append(type(exc).__name__)
                    self.reply(500, {"detail": "acceptance input verification failed"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def begin(self, name, mode="success", *, gate=False):
        with self.lock:
            self.scenario, self.mode, self.gated = name, mode, gate
            self.entered.clear()
            self.release.clear() if gate else self.release.set()

    def predict(self, payload):
        import httpx
        from PIL import Image

        model = (payload.get("context") or {}).get("model_id")
        require(model in {"detect", "geometry", "classify"}, "模型选择未冻结")
        tasks = payload.get("tasks")
        require(
            isinstance(tasks, list) and 0 < len(tasks) <= 3, "backend task 范围错误"
        )
        results = []
        for task in tasks:
            parsed = urlsplit(task["file_path"])
            require(
                f"{parsed.scheme}://{parsed.netloc}" == self.origin, "签名地址来源错误"
            )
            query = {
                key.lower(): value for key, value in parse_qs(parsed.query).items()
            }
            require(bool(query.get("x-amz-signature")), "输入不是实际签名 URL")
            require(0 < int(query["x-amz-expires"][0]) <= 3600, "签名期限错误")
            versions = query.get("versionid", [])
            require(
                len(versions) == 1 and exact_version(versions[0]),
                "输入 URL 未固定非 null 的准确版本",
            )
            version_id = versions[0]
            path = unquote(parsed.path).lstrip("/")
            match = re.fullmatch(
                re.escape(self.bucket)
                + r"/prediction-inputs/([0-9a-f-]{36})/(original|crops)/(.+)",
                path,
            )
            require(match is not None, "backend 收到未归属的输入")
            lease, kind, member = match.groups()
            with httpx.Client(
                trust_env=False, timeout=15, follow_redirects=False
            ) as client:
                with client.stream("GET", task["file_path"]) as response:
                    require(response.status_code == 200, "真实 MinIO 输入下载失败")
                    require(
                        response.headers.get("x-amz-version-id") == version_id,
                        "真实 GET 返回版本与签名 URL 不同",
                    )
                    etag = response.headers.get("etag")
                    require(isinstance(etag, str) and bool(etag), "真实 GET 缺少 ETag")
                    raw = bytearray()
                    for block in response.iter_bytes():
                        raw.extend(block)
                        require(len(raw) <= 1024 * 1024, "验收图片超过预算")
            raw = bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            require(
                member.endswith(digest + (".png" if kind == "original" else ".jpg")),
                "签名副本完整 SHA 与 key 不一致",
            )
            with self.lock:
                if kind == "original":
                    require(task["id"] in self.expected, "未知原件任务")
                    self.leases[lease] = task["id"]
                identity = self.leases[lease]
                expected = self.expected[identity]
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                rgb = image.convert("RGB")
                try:
                    dimensions = (128, 96) if kind == "original" else (64, 48)
                    require(rgb.size == dimensions, "真实原图或 ROI 尺寸错误")
                    pixel = rgb.getpixel((rgb.width // 2, rgb.height // 2))
                    require(
                        max(
                            abs(a - b)
                            for a, b in zip(pixel, expected["rgb"], strict=True)
                        )
                        <= 3,
                        "backend 读到了错误桶或错误 ROI 像素",
                    )
                finally:
                    rgb.close()
            if kind == "original":
                require(
                    digest == expected["sha256"] and len(raw) == expected["size_bytes"],
                    "backend 未读取完整规范原件字节",
                )
                require(model in {"detect", "geometry"}, "分类未使用 ROI 副本")
            else:
                require(model == "classify", "ROI 被错误模型消费")
            if model == "geometry":
                require(bool(task.get("prompts")), "geometry 阶段缺少父几何")
            with self.lock:
                self.events.append(
                    {
                        "scenario": self.scenario,
                        "model": model,
                        "task_id": identity,
                        "lease_id": lease,
                        "relative": kind + "/" + member,
                        "sha256": digest,
                        "size_bytes": len(raw),
                        "dimensions": dimensions,
                        "version_id": version_id,
                        "etag": etag,
                    }
                )
                gated = self.gated and model == "detect"
                if gated:
                    self.gated = False
                failed = (self.mode == "root_failure" and model == "detect") or (
                    self.mode == "crop_failure" and model == "classify"
                )
            if gated:
                self.entered.set()
                require(self.release.wait(45), "验收响应门控超时")
            if model == "classify":
                shapes = [{"attributes": {"color": expected["case"]}, "score": 1.0}]
            else:
                shape = rectangle()
                if model == "geometry":
                    shape["parent_box_idx"] = 0
                shapes = [shape]
            results.append(
                {
                    "task": task["id"],
                    "result": shapes,
                    "score": 1.0,
                    "model_version": "controlled-cpu",
                    "inference_time_ms": 1,
                }
            )
        return results, failed

    def close(self):
        self.release.set()
        if self.thread.ident is not None:
            self.server.shutdown()
        self.server.server_close()
        if self.thread.ident is not None:
            self.thread.join(timeout=5)
        require(not self.thread.is_alive(), "验收 backend 未停止")


class Worker:
    def __init__(self, log_path):
        from app.config import settings
        from app.workers.celery_app import celery_app

        self.app, self.queue = celery_app, settings.preannotate_cpu_queue
        self.node = "managed-acceptance-" + uuid.uuid4().hex + "@local"
        self.log_path, self.process, self.stream = log_path, None, None

    def inspect(self, method, *, destination=True):
        inspector = self.app.control.inspect(
            destination=[self.node] if destination else None, timeout=1
        )
        return getattr(inspector, method)() or {}

    def start(self):
        existing = self.inspect("active_queues", destination=False)
        require(
            not any(
                any(row["name"] == self.queue for row in queues)
                for queues in existing.values()
            ),
            "CPU 队列已有消费者；拒绝混用",
        )
        self.stream = self.log_path.open("xb")
        os.chmod(self.log_path, 0o600)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.workers.celery_app",
                "worker",
                "--pool=solo",
                "--concurrency=1",
                "--loglevel=WARNING",
                "--hostname=" + self.node,
                "--queues=" + self.queue,
            ],
            cwd=ROOT / "apps/api",
            env=application_environment(dict(os.environ)),
            stdout=self.stream,
            stderr=self.stream,
            start_new_session=True,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            require(
                self.process.poll() is None, "独立 CPU worker 提前退出；详见私有日志"
            )
            registered = set(self.inspect("registered").get(self.node, []))
            queues = self.inspect("active_queues").get(self.node, [])
            ping = self.inspect("ping").get(self.node, {})
            if {BATCH_TASK, RETRY_TASK, CLEANUP_TASK} <= registered and ping.get(
                "ok"
            ) == "pong":
                require(
                    worker_queues_match(self.node, self.queue, queues),
                    "worker 队列不匹配",
                )
                return
            time.sleep(0.2)
        raise WorktreeError("独立 CPU worker 注册超时；详见私有日志")

    def idle(self, timeout=30):
        deadline, consecutive = time.monotonic() + timeout, 0
        while time.monotonic() < deadline:
            require(
                self.process is not None and self.process.poll() is None,
                "CPU worker 在完成检查前退出",
            )
            snapshots = [
                self.inspect(method) for method in ("active", "reserved", "scheduled")
            ]
            if all(self.node in rows and not rows[self.node] for rows in snapshots):
                consecutive += 1
                if consecutive >= 2:
                    return True
            else:
                consecutive = 0
            time.sleep(0.2)
        return False

    def stop(self):
        idle, graceful = False, True
        try:
            if self.process is not None and self.process.poll() is None:
                try:
                    idle = self.idle()
                except Exception:
                    graceful = False
                finally:
                    if self.process.poll() is None:
                        os.killpg(self.process.pid, signal.SIGTERM)
                        try:
                            self.process.wait(timeout=60)
                        except subprocess.TimeoutExpired:
                            graceful = False
                            os.killpg(self.process.pid, signal.SIGKILL)
                            self.process.wait(timeout=10)
        finally:
            if self.stream is not None:
                self.stream.close()
            self.app.close()
        return {
            "started": self.process is not None,
            "idle_before_stop": idle,
            "graceful": graceful,
            "exit_code": self.process.returncode if self.process else None,
        }


def lifecycle_and_versions(storage, run_id, resources):
    """Qualify immutable version ownership, independently of DELETE IfMatch."""
    from botocore.exceptions import ClientError

    storage.ensure_all_buckets()
    owned = resources.resources
    expected_bucket = owned["buckets"]["MINIO_IMPORT_BUCKET"]
    require(
        owned["mode"] == "test"
        and os.environ.get("AAP_WORKTREE_MODE") == "test"
        and storage.import_bucket == expected_bucket,
        "拒绝更改非本工作树 test 导入桶的版本控制",
    )
    require(
        require_owner(
            expected_bucket, resources.bucket_info(expected_bucket), owned["owner"]
        ),
        "导入桶实际归属未确认，拒绝开启版本控制",
    )
    storage.client.put_bucket_versioning(
        Bucket=expected_bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    require(
        storage.client.get_bucket_versioning(Bucket=expected_bucket).get("Status")
        == "Enabled",
        "owned test 导入桶版本控制未实际启用",
    )
    try:
        rules = storage.client.get_bucket_lifecycle_configuration(
            Bucket=storage.import_bucket
        )["Rules"]
    except ClientError as exc:
        require(
            exc.response["Error"]["Code"] == "NoSuchLifecycleConfiguration",
            "读取导入桶生命周期失败",
        )
        rules = []
    require(len(rules) <= 32, "导入桶生命周期超出验收预算")
    for rule in rules:
        condition = rule.get("Filter", {"Prefix": rule.get("Prefix", "")})
        nested = condition.get("And", condition)
        prefix = nested.get("Prefix", "")
        tagged = bool(nested.get("Tags") or nested.get("Tag"))
        applies = not tagged and (
            "prediction-inputs/".startswith(prefix)
            or prefix.startswith("prediction-inputs/")
        )
        require(
            not (
                rule.get("Status") == "Enabled"
                and applies
                and ("Expiration" in rule or "NoncurrentVersionExpiration" in rule)
            ),
            "导入桶生命周期绕过 prediction input 所有权",
        )
    key = "prediction-acceptance/" + run_id + "/versioned.bin"
    versions = {}

    def listed():
        response = storage.client.list_object_versions(
            Bucket=expected_bucket, Prefix=key
        )
        require(
            not response.get("IsTruncated") and not response.get("DeleteMarkers"),
            "版本探针出现额外页或删除标记",
        )
        rows = response.get("Versions", [])
        require(
            len(rows) <= 2
            and all(
                row["Key"] == key and exact_version(row.get("VersionId"))
                for row in rows
            ),
            "版本探针出现未知对象或版本",
        )
        return rows

    def put(raw, *, first=False):
        response = storage.client.put_object(
            Bucket=expected_bucket,
            Key=key,
            Body=raw,
            ContentType="application/octet-stream",
            **({"IfNoneMatch": "*"} if first else {}),
        )
        identity = response.get("VersionId")
        require(
            exact_version(identity) and identity not in versions,
            "版本探针 PUT 未返回新的非 null VersionId",
        )
        versions[identity] = {"etag": response["ETag"], "raw": raw}
        return identity

    def read(identity, *, current=False):
        expected = versions[identity]
        response = storage.client.get_object(
            Bucket=expected_bucket,
            Key=key,
            IfMatch=expected["etag"],
            **({} if current else {"VersionId": identity}),
        )
        with response["Body"] as body:
            require(
                response.get("VersionId") == identity
                and response["ETag"] == expected["etag"]
                and response["ContentLength"] == len(expected["raw"])
                and body.read(len(expected["raw"]) + 1) == expected["raw"]
                and body.read(1) == b"",
                "版本探针未完整读取预期准确版本",
            )

    def absent(identity):
        try:
            storage.client.head_object(
                Bucket=expected_bucket, Key=key, VersionId=identity
            )
        except ClientError as exc:
            require(
                exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404,
                "版本探针删除后的准确版本状态不明确",
            )
        else:
            raise WorktreeError("已删除的准确版本仍可读取")

    def delete(identity):
        # IfMatch is supplementary; immutable VersionId supplies ownership.
        storage.client.delete_object(
            Bucket=expected_bucket,
            Key=key,
            VersionId=identity,
            IfMatch=versions[identity]["etag"],
        )

    require(not listed(), "拒绝覆盖既有版本探针对象")
    try:
        first = put(b"versioned-probe-original", first=True)
        read(first)
        read(first, current=True)
        nonexistent = str(uuid.uuid4())
        require(nonexistent not in versions, "探针版本身份冲突")
        try:
            storage.client.delete_object(
                Bucket=expected_bucket,
                Key=key,
                VersionId=nonexistent,
                IfMatch=versions[first]["etag"],
            )
        except ClientError as exc:
            require(
                exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404,
                "不存在版本的 DELETE 未按准确版本语义处理",
            )
        read(first)
        read(first, current=True)
        require(
            {row["VersionId"] for row in listed()} == {first},
            "删除不存在版本影响了已知版本",
        )
        second = put(b"versioned-probe-later-different-bytes")
        read(first)
        read(second, current=True)
        require(
            {row["VersionId"] for row in listed()} == {first, second},
            "同 key 两版本未完整保留",
        )
        delete(first)
        absent(first)
        read(second)
        read(second, current=True)
        require(
            {row["VersionId"] for row in listed()} == {second},
            "精确删除旧版本影响了新版本",
        )
        delete(second)
        absent(second)
        require(not listed(), "探针仍存在版本或删除标记")
    finally:
        # Retain unknown responses for namespace-owner inspection; never infer a
        # version from current HEAD and never use an unversioned DELETE fallback.
        rows = listed()
        require(
            {row["VersionId"] for row in rows} <= set(versions),
            "探针存在未知版本，拒绝盲删",
        )
        for row in rows:
            identity = row["VersionId"]
            read(identity)
            delete(identity)
            absent(identity)
        require(not listed(), "版本探针清理未完成")
    try:
        storage.client.head_object(Bucket=expected_bucket, Key=key)
    except ClientError as exc:
        require(
            exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404,
            "精确版本删除后的当前对象状态不明确",
        )
    else:
        raise WorktreeError("全部已知版本删除后对象仍存在")
    return {
        "bucket_versioning": "enabled-only-for-verified-owned-test-import",
        "delete_ownership": "immutable-non-null-exact-version-id",
        "if_match": "not-qualified-as-ownership-prior-live-minio-ignored-it",
        "nonexistent_version_delete_preserved_original": True,
        "old_version_delete_preserved_current_version_and_bytes": True,
        "probe_versions_and_delete_markers": 0,
        "import_expiration_overlap": False,
        "import_lifecycle_rule_count": len(rules),
    }


async def seed(factory, storage, backend, run_id, *, roi_marker=False):
    from PIL import Image
    from sqlalchemy import select
    from app.core.security import hash_password
    from app.db.models.dataset import Dataset, DatasetItem
    from app.db.models.dataset_asset import StorageBinding
    from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
    from app.db.models.ml_backend_registry import (
        MLBackendRegistry,
        ProjectMLBackendPool,
    )
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.task_batch import TaskBatch
    from app.db.models.task_dataset_item_link import TaskDatasetItemLink
    from app.db.models.user import User
    from app.services import managed_original_creation as creation, task_originals
    from app.services.dataset_assets import ensure_legacy_bindings, register_item_asset
    from app.services.ml_backend import MLBackendService

    password = "Owned-acceptance-" + uuid.uuid4().hex
    async with factory() as db:
        actor = User(
            email=f"managed-{run_id}@acceptance.invalid",
            name="Managed acceptance",
            role="super_admin",
            password_hash=hash_password(password),
            is_active=True,
            email_verified_at=datetime.now(timezone.utc),
        )
        db.add(actor)
        await db.flush()
        project = Project(
            display_id="P-MI-" + run_id[:12],
            name="Managed input acceptance",
            owner_id=actor.id,
            type_label="Object detection",
            type_key="image-det",
            data_type="image",
            ai_enabled=True,
            tool_bindings={
                "bbox": {
                    "enabled": True,
                    "classes": [{"name": "car", "order": 0}],
                    "attribute_schema": {
                        "fields": [{"key": "color", "label": "Color", "type": "text"}]
                    },
                }
            },
            total_tasks=3,
        )
        db.add(project)
        await db.flush()
        batch = TaskBatch(
            project_id=project.id,
            display_id="B-MI-" + run_id[:12],
            name="Managed acceptance",
            status="active",
            created_by=actor.id,
            total_tasks=3,
        )
        dataset = Dataset(
            display_id="D-MI-" + run_id[:12],
            name="Managed source acceptance",
            created_by=actor.id,
            file_count=2,
        )
        db.add_all([batch, dataset])
        await db.flush()
        registry = MLBackendRegistry(
            name="Managed CPU acceptance " + run_id[:8],
            url=backend.url,
            source="manual",
            auth_method="none",
            state="disconnected",
        )
        db.add(registry)
        await db.flush()
        pool = MLBackendServicePool(
            name=registry.name,
            enabled=True,
            legacy_instance_id=registry.id,
            routing_generation=1,
            routing_policy="smooth_weighted_round_robin",
        )
        db.add(pool)
        await db.flush()
        db.add(
            MLBackendPoolMember(
                pool_id=pool.id,
                registry_id=registry.id,
                traffic_state="active",
                weight=1,
            )
        )
        db.add(
            ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True)
        )
        project.ml_backend_pool_id = pool.id
        await ensure_legacy_bindings(db)
        await db.commit()
        require(
            await MLBackendService(db).check_health(registry.id),
            "受控 CPU 健康探针失败",
        )
        await db.refresh(registry)
        caps = registry.health_meta["capabilities"]
        require(
            caps["task_contract_revision"] == 1
            and all(
                model["resource_profile"]["device"] == "cpu" for model in caps["models"]
            ),
            "实际健康探针未保存 CPU 能力",
        )
        await db.commit()
        binding = await db.get(StorageBinding, "annotations")
        cases = []
        for index, (case, color) in enumerate(
            (
                ("task_original", (210, 40, 30)),
                ("direct_item", (30, 190, 70)),
                ("primary_link", (30, 70, 210)),
            )
        ):
            with io.BytesIO() as output, Image.new("RGB", (128, 96), color) as image:
                marker = tuple(240 - component // 2 for component in color)
                if roi_marker:
                    image.paste(marker, (32, 24, 96, 72))
                image.save(output, "PNG")
                raw = output.getvalue()
            digest = hashlib.sha256(raw).hexdigest()
            task = Task(
                project_id=project.id,
                batch_id=batch.id,
                display_id=f"T-MI-{run_id[:12]}-{index}",
                file_name=case + ".png",
                file_path="prediction-acceptance/" + run_id + "/" + case + ".png",
                file_type="image",
                status="uploading" if index == 0 else "pending",
            )
            db.add(task)
            await db.flush()
            if index == 0:
                allocation = await creation.allocate_task(
                    db,
                    project_id=project.id,
                    task_id=task.id,
                    logical_path=task.file_name,
                    max_bytes=len(raw),
                    requested_by=actor.id,
                    content_type="image/png",
                )
                task.file_path = allocation["object_key"]
                await asyncio.to_thread(
                    storage.client.put_object,
                    Bucket=allocation["staging_bucket"],
                    Key=allocation["staging_key"],
                    Body=raw,
                    ContentType="image/png",
                )
                created = await creation.complete_direct(db, allocation)
                owner = await task_originals.publish(db, task, allocation, created)
                content_id = owner.id
                task.status = "pending"
            else:
                # Admission records the actual saved annotations replica, after
                # a complete physical read; it does not infer the current bucket.
                put = await asyncio.to_thread(
                    storage.client.put_object,
                    Bucket=binding.bucket,
                    Key=task.file_path,
                    Body=raw,
                    IfNoneMatch="*",
                    ContentType="image/png",
                )
                read = await asyncio.to_thread(
                    storage.client.get_object,
                    Bucket=binding.bucket,
                    Key=task.file_path,
                    IfMatch=put["ETag"],
                )
                with read["Body"] as body:
                    require(
                        body.read(len(raw) + 1) == raw and body.read(1) == b"",
                        "种子原件读回不完整",
                    )
                item = DatasetItem(
                    dataset_id=dataset.id,
                    file_name=task.file_name,
                    file_path=task.file_path,
                    file_type="image",
                    file_size=len(raw),
                    width=128,
                    height=96,
                )
                db.add(item)
                await db.flush()
                asset = await register_item_asset(
                    db,
                    item,
                    sha256=digest,
                    size_bytes=len(raw),
                    etag=put["ETag"],
                    binding=binding,
                )
                content_id = asset.id
                if index == 1:
                    task.dataset_item_id = item.id
                else:
                    db.add(
                        TaskDatasetItemLink(
                            task_id=task.id,
                            dataset_item_id=item.id,
                            role="primary_lidar",
                            sensor_name="primary",
                        )
                    )
                with (
                    io.BytesIO() as output,
                    Image.new("RGB", (128, 96), "black") as image,
                ):
                    image.save(output, "PNG")
                    trap = output.getvalue()
                await asyncio.to_thread(
                    storage.client.put_object,
                    Bucket=storage.datasets_bucket,
                    Key=task.file_path,
                    Body=trap,
                    IfNoneMatch="*",
                    ContentType="image/png",
                )
            await db.flush()
            cases.append(
                {
                    "task_id": task.id,
                    "case": case,
                    "content_id": content_id,
                    "sha256": digest,
                    "size_bytes": len(raw),
                    "rgb": color,
                    "roi_rgb": marker if roi_marker else color,
                    "object_key": task.file_path,
                    "item_id": task.dataset_item_id,
                }
            )
            backend.expected[str(task.id)] = cases[-1]
        await db.commit()
        require(
            len(
                list(
                    await db.scalars(
                        select(Task.id).where(Task.project_id == project.id)
                    )
                )
            )
            == 3,
            "种子任务范围错误",
        )
        return {
            "actor_id": actor.id,
            "project_id": project.id,
            "batch_id": batch.id,
            "backend_id": registry.id,
            "email": actor.email,
            "password": password,
            "cases": cases,
        }


def pipeline(backend_id):
    shared = {"ml_backend_id": str(backend_id)}
    return [
        {
            **shared,
            "stage": 7,
            "parent_stage": None,
            "model_id": "detect",
            "task": "detect",
        },
        {
            **shared,
            "stage": 9,
            "parent_stage": 7,
            "model_id": "geometry",
            "task": "detect",
            "input": {"mode": "geometry"},
            "write": {"target": "geometry"},
        },
        {
            **shared,
            "stage": 11,
            "parent_stage": 9,
            "model_id": "classify",
            "task": "classify",
            "input": {"mode": "crop"},
            "roi": {"mode": "crop", "pad": 0},
            "write": {"target": "attributes", "keys": ["color"]},
        },
    ]


async def request(client, method, path, *, status=200, **kwargs):
    response = await client.request(method, path, **kwargs)
    require(
        response.status_code == status,
        f"真实 HTTP {method} {path.split('/')[3]} 失败（状态 {response.status_code}）",
    )
    return response.json() if response.content else None


async def job_done(factory, identity, worker, *, timeout=120):
    from app.db.models.async_job import AsyncJob

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        require(worker.process.poll() is None, "CPU worker 执行期间退出")
        async with factory() as db:
            row = await db.get(AsyncJob, identity)
            if row and row.status in TERMINAL:
                return row
        await asyncio.sleep(0.2)
    raise WorktreeError("真实 Celery 作业终态超时")


async def predictions(factory, task_id):
    from sqlalchemy import select
    from app.db.models.prediction import Prediction, PredictionMeta

    async with factory() as db:
        return list(
            (
                await db.execute(
                    select(Prediction, PredictionMeta)
                    .join(PredictionMeta, PredictionMeta.prediction_id == Prediction.id)
                    .where(Prediction.task_id == task_id)
                    .order_by(Prediction.created_at, Prediction.id)
                )
            ).all()
        )


async def check_guard_failure(factory, job, expected):
    from app.db.models.prediction import FailedPrediction

    identities = job.result.get("failed_prediction_ids", [])
    require(len(identities) == 1, "发布守卫未记录真实失败")
    async with factory() as db:
        failure = await db.get(FailedPrediction, uuid.UUID(identities[0]))
        require(
            failure is not None
            and failure.error_type == "ContentError"
            and failure.message in expected
            and "original_input" in failure.extra,
            "作业未因预期的当前授权/来源守卫拒绝",
        )


def safe_evidence(value):
    if isinstance(value, dict):
        require(not (set(value) & PRIVATE_KEYS), "公开原件溯源泄露私有字段")
        for child in value.values():
            safe_evidence(child)
    elif isinstance(value, list):
        for child in value:
            safe_evidence(child)
    elif isinstance(value, str):
        require(
            "://" not in value and "signature=" not in value.lower(), "公开溯源泄露 URL"
        )


async def check_prediction(factory, case, job):
    rows = await predictions(factory, case["task_id"])
    matches = [
        (prediction, meta)
        for prediction, meta in rows
        if (meta.extra or {}).get("original_input", {}).get("job_id") == str(job.id)
    ]
    require(len(matches) == 1, "真实流水线未原子发布唯一候选")
    prediction, metadata = matches[0]
    evidence = metadata.extra["original_input"]
    safe_evidence(evidence)
    original = evidence["original"]
    require(
        original["sha256"] == case["sha256"]
        and original["size_bytes"] == case["size_bytes"]
        and original["content_id"] == str(case["content_id"]),
        "持久溯源未绑定规范原件",
    )
    require(
        original["owner_kind"]
        == ("task_original" if case["case"] == "task_original" else "dataset_asset"),
        "持久溯源所有者类型错误",
    )
    require(
        evidence["execution_snapshot_id"] == str(job.execution_snapshot_id),
        "执行快照不一致",
    )
    selected = metadata.extra["pipeline"]["selections"]
    require(
        [row["stage"] for row in selected if row["executed"]] == [7, 9, 11],
        "非零根阶段链未执行完整",
    )
    enriched = [
        shape
        for shape in prediction.result
        if shape.get("attributes", {}).get("color") == case["case"]
    ]
    lineage = metadata.extra["pipeline"]["shape_lineage"]
    root = [entry for entry in lineage if entry["stage"] == 7]
    child = [entry for entry in lineage if entry["stage"] == 9]
    require(
        len(enriched) == len(root) == len(child) == 1
        and enriched[0]["pipeline_shape_id"] == child[0]["shape_id"]
        and child[0]["parent_shape_id"] == root[0]["shape_id"]
        and child[0]["parent_stage"] == 7,
        "裁图属性或已交付子几何谱系不一致",
    )
    return prediction.id


async def cleanup_inputs(factory, storage, worker, jobs, backend):
    from botocore.exceptions import ClientError
    from sqlalchemy import select, func
    from app.db.models.prediction_input import PredictionInputLease
    from app.db.models.storage_maintenance import StorageOperation

    deadline, dispatched = time.monotonic() + 150, 0
    while time.monotonic() < deadline:
        async with factory() as db:
            rows = list(
                await db.scalars(
                    select(PredictionInputLease).where(
                        PredictionInputLease.owner_id.in_(jobs)
                    )
                )
            )
            require(
                len(rows)
                == sum(1 for _ in set(event["lease_id"] for event in backend.events)),
                "临时输入责任数量与真实下载不匹配",
            )
            if rows and all(row.state == "deleted" for row in rows):
                require(
                    all(row.deleted_at is not None for row in rows), "缺少临时输入墓碑"
                )
                require(
                    await db.scalar(select(func.count()).select_from(StorageOperation))
                    == 0,
                    "真实流程残留未结算操作",
                )
                for row in rows:
                    prefix = f"prediction-inputs/{row.id}/"
                    listed = await asyncio.to_thread(
                        storage.client.list_object_versions,
                        Bucket=row.bucket,
                        Prefix=prefix,
                    )
                    require(
                        not listed.get("Versions")
                        and not listed.get("DeleteMarkers")
                        and not listed.get("IsTruncated"),
                        "墓碑仍有临时对象或版本",
                    )
                    seen = {
                        event["relative"]
                        for event in backend.events
                        if event["lease_id"] == str(row.id)
                    }
                    require(
                        set(row.inventory["objects"]) == seen,
                        "墓碑丢失实际交付对象责任",
                    )
                    require(
                        all(
                            entry["state"] == "verified"
                            and exact_version(entry.get("version_id"))
                            for entry in row.inventory["objects"].values()
                        ),
                        "完成作业留下未确认或 null 版本输入",
                    )
                    for relative, entry in row.inventory["objects"].items():
                        downloads = [
                            event
                            for event in backend.events
                            if event["lease_id"] == str(row.id)
                            and event["relative"] == relative
                        ]
                        require(
                            downloads
                            and all(
                                event["version_id"] == entry["version_id"]
                                and event["etag"] == entry["etag"]
                                and event["sha256"] == entry["sha256"]
                                and event["size_bytes"] == entry["size_bytes"]
                                for event in downloads
                            ),
                            "真实签名 URL/GET 与冻结版本收据不一致",
                        )
                        try:
                            await asyncio.to_thread(
                                storage.client.head_object,
                                Bucket=row.bucket,
                                Key=prefix + relative,
                                VersionId=entry["version_id"],
                            )
                        except ClientError as exc:
                            require(
                                exc.response["ResponseMetadata"]["HTTPStatusCode"]
                                == 404,
                                "墓碑准确版本的物理状态不明确",
                            )
                        else:
                            raise WorktreeError("临时输入墓碑仍可读取其冻结版本")
                return {
                    "deleted_tombstones": len(rows),
                    "unsettled_operations": 0,
                    "temporary_objects_and_versions": 0,
                    "signed_get_versions_equal_frozen_receipts": True,
                    "nonnull_receipt_versions": sum(
                        len(row.inventory["objects"]) for row in rows
                    ),
                    "maintenance_dispatches": dispatched,
                }
            due = max(
                (row.next_cleanup_at for row in rows if row.state != "deleted"),
                default=None,
            )
        # Managed sources deliberately still held the same-purpose read permit
        # during close_inputs' first purge. Wait the real retry deadline.
        if due is not None and due > datetime.now(timezone.utc):
            await asyncio.sleep(
                min(1, (due - datetime.now(timezone.utc)).total_seconds())
            )
            continue
        result = worker.app.send_task(CLEANUP_TASK, queue=worker.queue)
        try:
            await asyncio.to_thread(result.get, timeout=60)
        finally:
            result.forget()
        dispatched += 1
        await asyncio.sleep(0.2)
    raise WorktreeError("临时输入真实维护回收超时")


async def qualify(backend, worker, storage, report, *, expected_database):
    import httpx
    from sqlalchemy import select, text, func
    from app.db.base import async_session as factory, engine
    from app.db.models.async_job import AsyncJob
    from app.db.models.prediction import FailedPrediction
    from app.db.models.storage_maintenance import StorageOperation
    from app.db.models.task import Task
    from app.main import app

    # Signed input URLs belong only to the controlled HTTP request, not logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    require(not app.dependency_overrides, "验收拒绝 ASGI dependency override")
    async with factory() as db:
        require(
            await db.scalar(select(func.count()).select_from(StorageOperation)) == 0,
            "test 环境已有未结算存储操作",
        )
        require(
            await db.scalar(text("SELECT current_database()")) == expected_database,
            "实际应用连接不是已验证的 owned test 数据库",
        )
    fixtures = await seed(factory, storage, backend, report["run_id"])
    report["fixtures"] = {
        "project_id": str(fixtures["project_id"]),
        "canonical_cases": [
            {
                "case": case["case"],
                "task_id": str(case["task_id"]),
                "sha256": case["sha256"],
                "size_bytes": case["size_bytes"],
            }
            for case in fixtures["cases"]
        ],
    }
    await asyncio.to_thread(worker.start)
    report["worker"] = {
        "registered": [BATCH_TASK, RETRY_TASK, CLEANUP_TASK],
        "cpu_queue": worker.queue,
    }
    jobs, checks = [], report["checks"]
    try:
        transport = httpx.ASGITransport(
            app=app,
            client=(
                f"127.1.{int(report['run_id'][:2], 16)}.{int(report['run_id'][2:4], 16)}",
                0,
            ),
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://acceptance.local", timeout=30
        ) as client:
            login = await request(
                client,
                "POST",
                "/api/v1/auth/login",
                json={"email": fixtures["email"], "password": fixtures["password"]},
            )
            auth = {"Authorization": "Bearer " + login["access_token"]}

            async def submit(
                name, cases, *, scope="bulk", mode="success", headers=None, gate=False
            ):
                require(await asyncio.to_thread(worker.idle), "前一真实任务未空闲")
                backend.begin(name, mode, gate=gate)
                queued = await request(
                    client,
                    "POST",
                    f"/api/v1/projects/{fixtures['project_id']}/preannotate",
                    headers=headers or auth,
                    json={
                        "ml_backend_id": str(fixtures["backend_id"]),
                        "task_ids": [str(case["task_id"]) for case in cases],
                        "execution_scope": scope,
                        "predict_mode": "overwrite",
                        "pipeline_stages": pipeline(fixtures["backend_id"]),
                    },
                )
                identity = uuid.UUID(queued["job_id"])
                jobs.append(identity)
                async with factory() as db:
                    job = await db.get(AsyncJob, identity)
                    proof = job.payload["prediction_authority"]
                    require(
                        proof["schema"] == 2 and job.celery_task_id == str(identity),
                        "HTTP 未冻结真实 managed 作业",
                    )
                    require(
                        {entry["task_id"] for entry in proof["tasks"]}
                        == {str(case["task_id"]) for case in cases},
                        "HTTP 作业范围漂移",
                    )
                    require(
                        all(
                            entry["original"]["bucket"] == storage.bucket
                            for entry in proof["tasks"]
                        ),
                        "请求未冻结 annotations 中保存的原件",
                    )
                return identity

            bulk = await submit("bulk-three-owners", fixtures["cases"])
            job = await job_done(factory, bulk, worker)
            require(
                job.status == "completed" and job.result["success_count"] == 3,
                "三类规范原件真实批量执行失败",
            )
            for case in fixtures["cases"]:
                await check_prediction(factory, case, job)
            checks["bulk_three_owners"] = "passed"
            standalone, direct, primary = fixtures["cases"]
            workbench = await submit(
                "workbench-task-original", [standalone], scope="workbench"
            )
            job = await job_done(factory, workbench, worker)
            require(job.status == "completed", "工作台真实执行失败")
            await check_prediction(factory, standalone, job)
            checks["workbench"] = "passed"

            for case, mode, retry_entry in (
                (direct, "root_failure", "single"),
                (primary, "crop_failure", "job"),
            ):
                before = {
                    prediction.id
                    for prediction, _ in await predictions(factory, case["task_id"])
                }
                original_id = await submit(mode, [case], mode=mode)
                original = await job_done(factory, original_id, worker)
                require(
                    original.status == "failed"
                    and original.result["failed_count"] == 1,
                    "真实失败类型错误",
                )
                failed_ids = original.result.get("failed_prediction_ids", [])
                require(len(failed_ids) == 1, "真实 worker 未记录唯一可重试失败")
                failure_id = uuid.UUID(failed_ids[0])
                async with factory() as db:
                    failure = await db.get(FailedPrediction, failure_id)
                    require(
                        failure.execution_snapshot_id == original.execution_snapshot_id
                        and "original_input" in failure.extra,
                        "失败丢失原件/执行快照",
                    )
                    frozen_original = failure.extra["original_input"]["original"]
                    partial_ids = failure.extra.get("partial_prediction_ids", [])
                    require(
                        bool(partial_ids) == (mode == "crop_failure"),
                        "partial 失败未保留真实候选",
                    )
                    if partial_ids:
                        require(
                            failure.error_type == "PipelinePartialFailure",
                            "真实下游失败未记录 partial 类型",
                        )
                    task = await db.get(Task, case["task_id"])
                    task.version += 1
                    await db.commit()
                if mode == "root_failure":
                    require(
                        {
                            prediction.id
                            for prediction, _ in await predictions(
                                factory, case["task_id"]
                            )
                        }
                        == before,
                        "全失败提前删除旧候选",
                    )
                else:
                    partial_rows = [
                        (pred, meta)
                        for pred, meta in await predictions(factory, case["task_id"])
                        if str(pred.id) in partial_ids
                    ]
                    require(
                        len(partial_rows) == len(partial_ids)
                        and all(
                            meta.extra["pipeline"].get("partial") is True
                            for _, meta in partial_rows
                        ),
                        "partial 候选缺少失败溯源",
                    )
                    for _, metadata in partial_rows:
                        safe_evidence(metadata.extra["original_input"])
                backend.begin("retry-" + retry_entry)
                path = (
                    f"/api/v1/admin/failed-predictions/{failure_id}/retry"
                    if retry_entry == "single"
                    else f"/api/v1/async-jobs/{original_id}/retry-failed"
                )
                await request(client, "POST", path, headers=auth, status=202)
                async with factory() as db:
                    # The real worker may finish and delete FailedPrediction
                    # before this independent connection observes the response.
                    retry = await db.scalar(
                        select(AsyncJob).where(
                            AsyncJob.project_id == fixtures["project_id"],
                            AsyncJob.kind == "prediction_retry",
                            AsyncJob.payload["failed_prediction_id"].astext
                            == str(failure_id),
                        )
                    )
                    require(retry is not None, "重试请求未提交独立作业")
                    retry_id = retry.id
                    require(
                        retry.payload["prediction_authority"]["tasks"][0]["original"]
                        == frozen_original,
                        "重试替换了历史原件",
                    )
                jobs.append(retry_id)
                retry = await job_done(factory, retry_id, worker)
                require(
                    retry.status == "completed"
                    and retry.result["status"] == "succeeded",
                    "真实失败重试未完成",
                )
                await check_prediction(factory, case, retry)
                async with factory() as db:
                    require(
                        await db.get(FailedPrediction, failure_id) is None,
                        "重试未原子移除旧失败",
                    )
                if partial_ids:
                    superseded = [
                        (pred, meta)
                        for pred, meta in await predictions(factory, case["task_id"])
                        if str(pred.id) in partial_ids
                    ]
                    require(
                        all(
                            pred.rejected_shape_indexes
                            and meta.extra.get("superseded_by_prediction_id")
                            for pred, meta in superseded
                        )
                        and len(superseded) == len(partial_ids),
                        "重试未原子取代 partial 候选",
                    )
                checks[mode + "_" + retry_entry + "_retry"] = "passed"

            key = await request(
                client,
                "POST",
                "/api/v1/me/api-keys",
                headers=auth,
                status=201,
                json={"name": "Managed acceptance revoke", "scopes": ["*"]},
            )
            previous = {
                pred.id for pred, _ in await predictions(factory, standalone["task_id"])
            }
            revoked = await submit(
                "key-revoked-after-real-get",
                [standalone],
                gate=True,
                headers={"Authorization": "Bearer " + key["plaintext"]},
            )
            try:
                require(
                    await asyncio.to_thread(backend.entered.wait, 30),
                    "真实模型门控未触发",
                )
                await request(
                    client,
                    "DELETE",
                    f"/api/v1/me/api-keys/{key['id']}",
                    headers=auth,
                    status=204,
                )
            finally:
                backend.release.set()
            revoked = await job_done(factory, revoked, worker)
            require(revoked.status == "failed", "已撤销凭据仍发布预测")
            await check_guard_failure(
                factory,
                revoked,
                {"prediction_authority_revoked", "api_key_authority_revoked"},
            )
            require(
                {
                    pred.id
                    for pred, _ in await predictions(factory, standalone["task_id"])
                }
                == previous,
                "凭据撤销失败破坏旧候选",
            )
            checks["revocation_after_real_source_get"] = "passed"

            previous = {
                pred.id for pred, _ in await predictions(factory, direct["task_id"])
            }
            drifted = await submit("binding-drift-after-real-get", [direct], gate=True)
            try:
                require(
                    await asyncio.to_thread(backend.entered.wait, 30),
                    "真实来源漂移门控未触发",
                )
                async with factory() as db:
                    # Rebind to the other existing verified DatasetItem, with
                    # no mutation of immutable Asset/Replica facts.
                    task = await db.get(Task, direct["task_id"])
                    linked = await db.scalar(
                        text(
                            "SELECT dataset_item_id FROM task_dataset_item_links WHERE task_id=:id"
                        ),
                        {"id": primary["task_id"]},
                    )
                    task.dataset_item_id, task.file_path, task.file_name = (
                        linked,
                        primary["object_key"],
                        "primary_link.png",
                    )
                    task.version += 1
                    await db.commit()
            finally:
                backend.release.set()
            drifted = await job_done(factory, drifted, worker)
            require(drifted.status == "failed", "原件绑定漂移后仍发布预测")
            await check_guard_failure(
                factory,
                drifted,
                {"prediction_scope_changed", "content_binding_changed"},
            )
            require(
                {pred.id for pred, _ in await predictions(factory, direct["task_id"])}
                == previous,
                "来源漂移失败破坏旧候选",
            )
            checks["binding_drift_after_real_source_get"] = "passed"

            public = await request(
                client, "GET", f"/api/v1/async-jobs/{bulk}", headers=auth
            )
            require(
                "prediction_authority" not in public["payload"],
                "公开 Job 泄露内部原件证明",
            )
        require(not backend.errors, "受控 backend 发现输入验证失败")
        report["cleanup"] = await cleanup_inputs(
            factory, storage, worker, jobs, backend
        )
        report["real_jobs"] = len(jobs)
        report["real_signed_gets"] = dict(
            Counter(event["model"] for event in backend.events)
        )
        report["full_image_and_crop_sha_pixels"] = "passed"
        for case in fixtures["cases"][1:]:
            trap = await asyncio.to_thread(
                storage.client.get_object,
                Bucket=storage.datasets_bucket,
                Key=case["object_key"],
            )
            with trap["Body"] as body:
                raw = body.read(1024 * 1024 + 1)
                require(
                    len(raw) <= 1024 * 1024
                    and body.read(1) == b""
                    and hashlib.sha256(raw).hexdigest() != case["sha256"],
                    "当前桶陷阱对象意外丢失或被替换",
                )
        report["source_bucket_traps"] = "two-different-current-bucket-images-avoided"
    finally:
        backend.release.set()
        await engine.dispose()


@contextmanager
def private_report(path):
    require(path.is_absolute(), "报告路径必须为绝对路径")
    require(
        not any(parent.is_symlink() for parent in (path, *path.parents)),
        "报告路径不能含符号链接",
    )
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    require(
        path.parent.stat().st_uid == os.getuid()
        and not path.parent.stat().st_mode & 0o077,
        "报告目录必须为当前用户私有",
    )
    require(not path.exists(), "拒绝覆盖既有验收报告")
    report = {
        "schema": 1,
        "status": "failed",
        "run_id": uuid.uuid4().hex,
        "qualification": "real-http-celery-minio-plumbing-controlled-cpu-responses",
        "real_ml_inference": "not-qualified",
        "checks": {},
        "cleanup": "pending",
        "fixture_disposition": "retained-only-in-owned-test-namespace-for-launcher-destruction",
        "exclusions": [
            "real-ML-accuracy-and-GPU",
            "provider-unknown-close-fault-injection",
            "planned-write-crash-and-foreign-nonce-fault-injection",
        ],
    }
    try:
        yield report
    except BaseException as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        if isinstance(exc, WorktreeError):
            report["error_message"] = str(exc)
        code = getattr(exc, "code", None)
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            report["error_code"] = code
        report["frames"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno}
            for frame in traceback.extract_tb(exc.__traceback__)
            if str(ROOT) in frame.filename
        ][-8:]
        raise
    finally:
        atomic_json(path, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    options = parser.parse_args()
    # Process-local diagnostic/host routing only; never write shared .env files.
    os.environ.update(DEBUG="false", LOG_LEVEL="WARNING", ML_BACKEND_STORAGE_HOST="")
    backend = worker = resources = storage = None
    try:
        with private_report(options.report) as report:
            try:
                report["phase"] = "verify-owned-environment"
                resources = verified_environment()
                from app.config import settings
                from app.services.storage import StorageService

                storage = StorageService()
                report["phase"] = "real-minio-contract"
                report["storage_contract"] = lifecycle_and_versions(
                    storage, report["run_id"], resources
                )
                backend = CPUBackend(
                    f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
                    storage.import_bucket,
                )
                backend.thread.start()
                worker = Worker(options.report.with_suffix(".worker.log"))
                report["phase"] = "real-http-celery-scenarios"
                asyncio.run(
                    qualify(
                        backend,
                        worker,
                        storage,
                        report,
                        expected_database=resources.resources["database"],
                    )
                )
                report["status"] = "passed"
            finally:
                if backend is not None:
                    backend.release.set()
                shutdown_errors = []
                for name, operation in (
                    ("worker", worker.stop if worker is not None else None),
                    ("backend", backend.close if backend is not None else None),
                    ("storage", storage.client.close if storage is not None else None),
                    (
                        "resources",
                        resources.s3.close if resources is not None else None,
                    ),
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
                                    "独立 worker 未空闲优雅退出",
                                )
                        except BaseException as exc:
                            shutdown_errors.append(
                                {"owner": name, "error_type": type(exc).__name__}
                            )
                if shutdown_errors:
                    report["shutdown_errors"] = shutdown_errors
                    raise WorktreeError("验收进程或句柄未确认全部关闭")
        print(
            "PASS: real managed-image HTTP/Celery/MinIO plumbing; controlled CPU responses only."
        )
        return 0
    except BaseException as exc:
        print(
            f"FAIL: managed-image acceptance ({type(exc).__name__}); inspect private report/log.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
