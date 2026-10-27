"""Qualify M1 video delivery in an already verified, owned test namespace.

Run through dev-worktree.mjs exec --mode test, after integration and migrations.
This command provisions no resources, changes no image/dependency configuration,
and never adopts another checkout's environment. Controlled CPU HTTP responses
qualify transport and persistence only, not model accuracy or GPU execution.
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
import re
import subprocess
import tempfile
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

import verify_worktree_managed_predictions as plumbing

ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT, FRAMES, FPS = 128, 96, 6, 3
MAX_FIXTURE_BYTES = 8 * 1024**2
require = plumbing.require


def setup_contract():
    return {
        "name": "controlled-video-acceptance",
        "version": "1",
        "protocol_version": "2",
        "task_contract_revision": 1,
        "infra": "onnx",
        "models": [
            {
                "id": identity,
                "task": "tracker",
                "infra": "onnx",
                "supported_inputs": ["video"],
                "supported_geometric_outputs": [geometry],
                "supported_prompts": ["none"],
                "resource_profile": {"device": "cpu", "batchable": True},
                "classes": [{"index": 0, "name": "car"}],
            }
            for identity, geometry in (
                ("track", "bbox"),
                ("sam-polygon", "polygon"),
                ("sam-mask", "mask"),
            )
        ],
    }


def _process(arguments, *, maximum):
    result = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    require(result.returncode == 0, "真实视频生成或解码失败；不输出可能含位置的 stderr")
    require(len(result.stdout) <= maximum, "受控视频子进程输出超过预算")
    return result.stdout


def make_video(rgb):
    """Produce a small self-contained video, never a fabricated MP4 header."""
    with tempfile.TemporaryDirectory(prefix="aap-video-source-") as directory:
        target = Path(directory) / "source.mp4"
        color = "0x" + "".join(f"{component:02x}" for component in rgb)
        _process(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s={WIDTH}x{HEIGHT}:r={FPS}",
                "-frames:v",
                str(FRAMES),
                "-an",
                "-c:v",
                "mpeg4",
                "-q:v",
                "2",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(target),
            ],
            maximum=0,
        )
        raw = target.read_bytes()
        require(0 < len(raw) <= MAX_FIXTURE_BYTES, "真实视频种子大小不符合预算")
        return raw


def inspect_video(raw, expected):
    """Probe and decode the complete bytes obtained through the real signed GET."""
    from app.services.exporting.video_probe import inspect

    with tempfile.TemporaryDirectory(prefix="aap-video-download-") as directory:
        target = Path(directory) / "received.mp4"
        target.write_bytes(raw)
        with target.open("rb") as file:
            probe = inspect(file)
        video = probe["video"]
        require(
            video["width"] == WIDTH
            and video["height"] == HEIGHT
            and video["frame_count"] == FRAMES
            and abs(video["fps"] - FPS) < 0.001,
            "模型实际下载视频的解码事实错误",
        )
        pixels = _process(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-threads",
                "1",
                "-i",
                str(target),
                "-map",
                "0:v:0",
                "-an",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            maximum=WIDTH * HEIGHT * 3 * FRAMES,
        )
        require(len(pixels) == WIDTH * HEIGHT * 3 * FRAMES, "未真实解码全部视频帧")
        for frame in range(FRAMES):
            offset = (frame * WIDTH * HEIGHT + HEIGHT // 2 * WIDTH + WIDTH // 2) * 3
            pixel = pixels[offset : offset + 3]
            require(
                max(abs(a - b) for a, b in zip(pixel, expected["rgb"], strict=True))
                <= 8,
                "实际视频像素属于错误桶或错误来源",
            )
        return probe


def output_shapes(model):
    if model == "track":
        return [
            {
                "type": "video_track_bbox",
                "track_id": track,
                "class_name": "car",
                "score": 0.9,
                "keyframes": [
                    {
                        "frame_index": frame,
                        "bbox": {"x": x, "y": 0.25, "w": 0.25, "h": 0.5},
                        "score": 0.9,
                    }
                    for frame in (0, FRAMES - 1)
                ],
            }
            for track, x in ((17, 0.125), (29, 0.625))
        ]
    geometry = (
        {"type": "polygon", "points": [[0.25, 0.25], [0.75, 0.25], [0.25, 0.75]]}
        if model == "sam-polygon"
        else {
            "type": "mask",
            "rle": {
                "encoding": "coco_rle",
                "size": [HEIGHT, WIDTH],
                "counts": [0, HEIGHT, HEIGHT * (WIDTH - 1)],
            },
        }
    )
    return [
        {
            "frame_index": frame,
            "instance_id": "1",
            "class_name": "car",
            "geometry": deepcopy(geometry),
            "confidence": 0.9,
            "outside": False,
            "primary": True,
        }
        for frame in (0, 2, FRAMES - 1)
    ]


class VideoBackend(plumbing.CPUBackend):
    """Reuse the bounded HTTP server while verifying actual whole-video bytes."""

    def predict(self, payload):
        import httpx

        context = payload.get("context")
        model = context.get("model_id") if context is not None else "track"
        require(model in {"track", "sam-polygon", "sam-mask"}, "非预期视频模型")
        if self.scenario.startswith("sync-"):
            require(
                "context" not in payload, "同步 predict-test 新增了 context 或模型选择"
            )
        else:
            require(context is not None, "异步视频没有冻结模型上下文")
        tasks = payload.get("tasks")
        require(isinstance(tasks, list) and len(tasks) == 1, "视频请求任务范围错误")
        task = tasks[0]
        identity = task["id"]
        require(identity in self.expected, "backend 收到未知任务")
        expected = self.expected[identity]
        parsed = urlsplit(task["file_path"])
        require(f"{parsed.scheme}://{parsed.netloc}" == self.origin, "视频签名来源错误")
        query = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
        versions = query.get("versionid", [])
        require(
            bool(query.get("x-amz-signature"))
            and len(versions) == 1
            and plumbing.exact_version(versions[0]),
            "视频输入没有固定真实非 null VersionId",
        )
        require(0 < int(query["x-amz-expires"][0]) <= 3600, "视频签名期限错误")
        match = re.fullmatch(
            re.escape(self.bucket)
            + r"/prediction-inputs/([0-9a-f-]{36})/(video/[0-9a-f]{64}\.(?:mov|mkv|avi|asf|flv))",
            unquote(parsed.path).lstrip("/"),
        )
        require(match is not None, "视频输入不属于独立版本化责任")
        lease_id, relative = match.groups()
        with httpx.Client(
            trust_env=False, timeout=30, follow_redirects=False
        ) as client:
            with client.stream("GET", task["file_path"]) as response:
                require(response.status_code == 200, "真实视频 MinIO GET 失败")
                require(
                    response.headers.get("x-amz-version-id") == versions[0],
                    "真实视频 GET 版本不一致",
                )
                require(
                    int(response.headers["content-length"]) == expected["size_bytes"],
                    "视频响应长度错误",
                )
                etag = response.headers.get("etag")
                require(isinstance(etag, str) and bool(etag), "视频 GET 缺少 ETag")
                content = bytearray()
                for block in response.iter_bytes():
                    content.extend(block)
                    require(len(content) <= MAX_FIXTURE_BYTES, "验收视频下载超过预算")
        raw = bytes(content)
        digest = hashlib.sha256(raw).hexdigest()
        require(
            len(raw) == expected["size_bytes"]
            and digest == expected["sha256"]
            and Path(relative).stem == digest,
            "实际视频供图未保留原件完整 SHA 和长度",
        )
        probe = inspect_video(raw, expected)
        with self.lock:
            self.events.append(
                {
                    "scenario": self.scenario,
                    "model": model,
                    "task_id": identity,
                    "lease_id": lease_id,
                    "relative": relative,
                    "version_id": versions[0],
                    "etag": etag,
                    "sha256": digest,
                    "size_bytes": len(raw),
                    "probe": probe,
                }
            )
            gated, failed = self.gated, self.mode == "root_failure"
            self.gated = False
        if gated:
            self.entered.set()
            require(self.release.wait(45), "视频受控响应门控超时")
        return [
            {
                "task": identity,
                "result": output_shapes(model),
                "score": 0.9,
                "model_version": "controlled-video-cpu",
                "inference_time_ms": 1,
            }
        ], failed


async def seed(factory, storage, backend, run_id):
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

    require(storage.bucket != storage.datasets_bucket, "验收要求保存桶与当前数据桶分离")
    password = "Owned-video-" + uuid.uuid4().hex
    async with factory() as db:
        actor = User(
            email=f"video-{run_id}@acceptance.invalid",
            name="Video acceptance",
            role="super_admin",
            password_hash=hash_password(password),
            is_active=True,
            email_verified_at=datetime.now(timezone.utc),
        )
        db.add(actor)
        await db.flush()
        tools = {
            name: {
                "enabled": True,
                "classes": [{"name": "car", "order": 0}],
                "attribute_schema": {"fields": []},
            }
            for name in ("bbox", "region")
        }
        project = Project(
            display_id="P-VM-" + run_id[:12],
            name="Whole-video acceptance",
            owner_id=actor.id,
            type_key="video-track",
            type_label="Video tracking",
            data_type="video",
            ai_enabled=True,
            ai_interactive_enabled=False,
            tool_bindings=tools,
            total_tasks=3,
        )
        db.add(project)
        await db.flush()
        batch = TaskBatch(
            project_id=project.id,
            display_id="B-VM-" + run_id[:12],
            name="Video acceptance",
            status="active",
            created_by=actor.id,
            total_tasks=3,
        )
        dataset = Dataset(
            display_id="D-VM-" + run_id[:12],
            name="Canonical video acceptance",
            created_by=actor.id,
            file_count=2,
        )
        registry = MLBackendRegistry(
            name="Video CPU " + run_id[:8],
            url=backend.url,
            source="manual",
            auth_method="none",
            state="disconnected",
        )
        db.add_all([batch, dataset, registry])
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
        db.add_all(
            [
                MLBackendPoolMember(
                    pool_id=pool.id,
                    registry_id=registry.id,
                    traffic_state="active",
                    weight=1,
                ),
                ProjectMLBackendPool(
                    project_id=project.id, pool_id=pool.id, enabled=True
                ),
            ]
        )
        project.ml_backend_pool_id = pool.id
        await ensure_legacy_bindings(db)
        await db.commit()
        require(
            await MLBackendService(db).check_health(registry.id),
            "视频 CPU 健康探针失败",
        )
        await db.refresh(registry)
        require(
            registry.health_meta["capabilities"]["task_contract_revision"] == 1,
            "真实健康探针未登记视频能力",
        )
        await db.commit()
        binding = await db.get(StorageBinding, "annotations")
        require(
            binding is not None and binding.bucket == storage.bucket,
            "未保存准确 annotations 位置",
        )
        cases = []
        trap = await asyncio.to_thread(make_video, (0, 0, 0))
        for index, (name, rgb) in enumerate(
            (
                ("task_original", (210, 40, 30)),
                ("direct_item", (30, 190, 70)),
                ("primary_link", (30, 70, 210)),
            )
        ):
            raw = await asyncio.to_thread(make_video, rgb)
            case = {
                "case": name,
                "rgb": rgb,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
            task = Task(
                project_id=project.id,
                batch_id=batch.id,
                display_id=f"T-VM-{run_id[:12]}-{index}",
                file_name=name + ".mp4",
                file_path=f"video-acceptance/{run_id}/{name}.mp4",
                file_type="video",
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
                    content_type="video/mp4",
                )
                task.file_path = allocation["object_key"]
                await asyncio.to_thread(
                    storage.client.put_object,
                    Bucket=allocation["staging_bucket"],
                    Key=allocation["staging_key"],
                    Body=raw,
                    ContentType="video/mp4",
                )
                created = await creation.complete_direct(db, allocation)
                owner = await task_originals.publish(db, task, allocation, created)
                case["content_id"], case["bucket"] = owner.id, owner.bucket
                task.status = "pending"
            else:
                put = await asyncio.to_thread(
                    storage.client.put_object,
                    Bucket=binding.bucket,
                    Key=task.file_path,
                    Body=raw,
                    IfNoneMatch="*",
                    ContentType="video/mp4",
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
                        "视频种子完整回读失败",
                    )
                item = DatasetItem(
                    dataset_id=dataset.id,
                    file_name=task.file_name,
                    file_path=task.file_path,
                    file_type="video",
                    file_size=len(raw),
                    width=1,
                    height=2,
                    metadata_={
                        "video": {
                            "fps": 1000,
                            "frame_count": 99999,
                            "width": 1,
                            "height": 2,
                        }
                    },
                )
                db.add(item)
                await db.flush()
                asset = await register_item_asset(
                    db,
                    item,
                    sha256=case["sha256"],
                    size_bytes=len(raw),
                    etag=put["ETag"],
                    binding=binding,
                )
                case["content_id"], case["bucket"] = asset.id, binding.bucket
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
            await asyncio.to_thread(
                storage.client.put_object,
                Bucket=storage.datasets_bucket,
                Key=task.file_path,
                Body=trap,
                IfNoneMatch="*",
                ContentType="video/mp4",
            )
            case.update(task_id=task.id, object_key=task.file_path)
            cases.append(case)
            backend.expected[str(task.id)] = case
        await db.commit()
        return {
            "actor_id": actor.id,
            "project_id": project.id,
            "batch_id": batch.id,
            "backend_id": registry.id,
            "location_id": binding.location_id,
            "email": actor.email,
            "password": password,
            "cases": cases,
        }


def pipeline(backend_id, model):
    return [
        {
            "stage": 7,
            "parent_stage": None,
            "ml_backend_id": str(backend_id),
            "model_id": model,
            "task": "tracker",
        }
    ]


async def check_prediction(factory, case, job, *, kind="video_track_bbox"):
    from app.services.raster_mask_storage import collect_mask_references, load_coco_rle

    matches = [
        (row, meta)
        for row, meta in await plumbing.predictions(factory, case["task_id"])
        if (meta.extra or {}).get("original_video_input", {}).get("job_id")
        == str(job.id)
    ]
    require(len(matches) == 1, "视频没有原子发布唯一 Prediction/Meta")
    prediction, metadata = matches[0]
    public = metadata.extra["original_video_input"]
    plumbing.safe_evidence(public)
    original = public["original"]
    require(
        original["sha256"] == case["sha256"]
        and original["size_bytes"] == case["size_bytes"]
        and original["content_id"] == str(case["content_id"]),
        "安全视频溯源没有绑定准确原件",
    )
    require(
        original["owner_kind"]
        == ("task_original" if case["case"] == "task_original" else "dataset_asset"),
        "安全视频溯源原件类型错误",
    )
    probe = public["probe"]["video"]
    require(
        probe["width"] == WIDTH
        and probe["height"] == HEIGHT
        and probe["frame_count"] == FRAMES,
        "视频候选使用了未经核验的旧元数据",
    )
    proof = metadata.extra.get("video_prediction_proof")
    require(
        isinstance(proof, dict)
        and proof["kind"] == "video_prediction"
        and proof["evidence"]["original"]["sha256"] == case["sha256"]
        and proof["evidence"]["execution_snapshot_id"] == str(job.execution_snapshot_id)
        and bool(proof.get("candidate_digest")),
        "视频丢失私有候选证明或不可变执行快照",
    )
    require(
        prediction.result
        and all(shape["geometry"]["type"] == kind for shape in prediction.result)
        and all(shape.get("pipeline_shape_id") for shape in prediction.result),
        "视频模型结果未适配为真实轨迹和非零 root 谱系",
    )
    require(
        [
            row["stage"]
            for row in metadata.extra["pipeline"]["selections"]
            if row["executed"]
        ]
        == [7],
        "视频非零 root 阶段未执行",
    )
    for shape in prediction.result:
        require(
            shape["geometry"]["keyframes"][-1]["frame_index"] == FRAMES - 1,
            "视频轨迹末帧不属于实际原件",
        )
    if kind == "video_track_mask":
        for reference in collect_mask_references(prediction.result):
            rle = await load_coco_rle(reference)
            require(
                rle["size"] == [HEIGHT, WIDTH]
                and rle["counts"] == [0, HEIGHT, HEIGHT * (WIDTH - 1)],
                "真实 Mask owner 的完整 RLE 与受控输出不同",
            )
    return prediction.id


async def sources_intact(storage, cases):
    for case in cases:
        result = await asyncio.to_thread(
            storage.client.get_object, Bucket=case["bucket"], Key=case["object_key"]
        )
        with result["Body"] as body:
            raw = body.read(MAX_FIXTURE_BYTES + 1)
            require(
                len(raw) <= MAX_FIXTURE_BYTES and body.read(1) == b"",
                "canonical 视频读取超过预算",
            )
        require(
            hashlib.sha256(raw).hexdigest() == case["sha256"]
            and len(raw) == case["size_bytes"],
            "原 canonical 视频被修改或回收",
        )
        trap = await asyncio.to_thread(
            storage.client.get_object,
            Bucket=storage.datasets_bucket,
            Key=case["object_key"],
        )
        with trap["Body"] as body:
            raw = body.read(MAX_FIXTURE_BYTES + 1)
            require(
                len(raw) <= MAX_FIXTURE_BYTES and body.read(1) == b"",
                "陷阱视频读取超过预算",
            )
        require(
            hashlib.sha256(raw).hexdigest() != case["sha256"], "当前桶同 key 陷阱失效"
        )


async def cleanup_inputs(factory, storage, fixtures, backend):
    from sqlalchemy import select
    from app.db.models.prediction_input import PredictionInputLease
    from app.services import prediction_originals as common
    from tests.prediction_input_live_assertions import (
        retire_known_inputs,
        assert_retired_input_versions,
    )

    task_ids = [case["task_id"] for case in fixtures["cases"]]
    async with factory() as db:
        initial = list(
            await db.scalars(
                select(PredictionInputLease).where(
                    PredictionInputLease.task_id.in_(task_ids)
                )
            )
        )
    require(
        initial
        and {str(row.id) for row in initial}
        == {event["lease_id"] for event in backend.events},
        "v3 输入账本与真实 GET 不匹配",
    )
    immutable = {}
    for row in initial:
        require(
            row.contract_version == 3 and row.state in {"deleted", "cleanup_pending"},
            "视频责任未按 v3 正常封存",
        )
        require(
            (row.expires_at - row.created_at).total_seconds() == 7200,
            "首次视频有效期被改写",
        )
        require((row.retain_until - row.expires_at).days == 7, "视频恢复期限不正确")
        events = [event for event in backend.events if event["lease_id"] == str(row.id)]
        require(len(row.inventory["objects"]) == 1, "v3 输入不是单独的整视频对象")
        for relative, entry in row.inventory["objects"].items():
            require(
                entry["state"] == "verified"
                and plumbing.exact_version(entry["version_id"])
                and all(
                    event["relative"] == relative
                    and all(
                        event[field] == entry[field]
                        for field in (
                            "sha256",
                            "size_bytes",
                            "etag",
                            "version_id",
                            "probe",
                        )
                    )
                    for event in events
                ),
                "视频真实固定版本 GET 与不可变库存不一致",
            )
        immutable[row.id] = common.encoded(
            {
                "inventory": row.inventory,
                "source_ref": row.source_ref,
                "created_at": row.created_at,
                "expires_at": row.expires_at,
                "retain_until": row.retain_until,
            }
        )
    final = []
    for task_id in task_ids:
        await retire_known_inputs(factory, task_id, wait_seconds=180)
        observed = {
            (row.bucket, f"prediction-inputs/{row.id}/" + relative, entry["version_id"])
            for row in initial
            if row.task_id == task_id
            for relative, entry in row.inventory["objects"].items()
        }
        final.extend(
            await assert_retired_input_versions(
                factory, task_id, storage, observed=observed
            )
        )
    for row in final:
        require(
            immutable[row.id]
            == common.encoded(
                {
                    "inventory": row.inventory,
                    "source_ref": row.source_ref,
                    "created_at": row.created_at,
                    "expires_at": row.expires_at,
                    "retain_until": row.retain_until,
                }
            ),
            "清理改写了视频库存、来源或首次期限",
        )
    return {
        "deleted_tombstones": len(final),
        "fixed_version_gets": len(backend.events),
        "temporary_versions_and_delete_markers": 0,
        "unsettled_input_operations": 0,
        "real_next_cleanup_at_respected": True,
        "immutable_receipts_and_deadlines": True,
    }


async def scenarios(backend, worker, storage, report, expected_database):
    import httpx
    from sqlalchemy import func, select, text
    from app.db.base import async_session as factory
    from app.db.models.annotation import Annotation
    from app.db.models.async_job import AsyncJob
    from app.db.models.dataset_asset import StorageLocation
    from app.db.models.prediction import FailedPrediction
    from app.db.models.prediction_execution import PredictionExecutionSnapshot
    from app.db.models.storage_maintenance import StorageOperation
    from app.main import app

    logging.getLogger("httpx").setLevel(logging.WARNING)
    require(not app.dependency_overrides, "视频验收拒绝任何 ASGI dependency override")
    async with factory() as db:
        require(
            await db.scalar(text("SELECT current_database()")) == expected_database,
            "实际连接不属于已验证 test 数据库",
        )
        require(
            await db.scalar(select(func.count()).select_from(StorageOperation)) == 0,
            "验收前已有未结算存储操作",
        )
    fixtures = await seed(factory, storage, backend, report["run_id"])
    project_id, backend_id = fixtures["project_id"], fixtures["backend_id"]
    standalone, direct, primary = fixtures["cases"]
    report["fixtures"] = {
        "project_id": str(project_id),
        "canonical_cases": [
            {
                key: str(case[key]) if key == "task_id" else case[key]
                for key in ("case", "task_id", "sha256", "size_bytes")
            }
            for case in fixtures["cases"]
        ],
        "actual_dimensions": [WIDTH, HEIGHT],
        "decoded_frames": FRAMES,
        "legacy_item_dimensions": [1, 2],
    }
    await asyncio.to_thread(worker.start)
    report["worker"] = {
        "node": worker.node,
        "cpu_queue": worker.queue,
        "registered": [plumbing.BATCH_TASK, plumbing.RETRY_TASK, plumbing.CLEANUP_TASK],
    }
    jobs, checks = [], report["checks"]
    transport = httpx.ASGITransport(
        app=app,
        client=(
            f"127.5.{int(report['run_id'][:2], 16)}.{int(report['run_id'][2:4], 16)}",
            0,
        ),
    )
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://video-acceptance.local", timeout=90
        ) as client:
            login = await plumbing.request(
                client,
                "POST",
                "/api/v1/auth/login",
                json={"email": fixtures["email"], "password": fixtures["password"]},
            )
            auth = {"Authorization": "Bearer " + login["access_token"]}

            async def submit(
                name,
                cases,
                *,
                scope="bulk",
                model="track",
                mode="success",
                headers=None,
                gate=False,
            ):
                require(await asyncio.to_thread(worker.idle), "前一视频作业尚未空闲")
                backend.begin(name, mode, gate=gate)
                response = await plumbing.request(
                    client,
                    "POST",
                    f"/api/v1/projects/{project_id}/preannotate",
                    headers=headers or auth,
                    json={
                        "ml_backend_id": str(backend_id),
                        "task_ids": [str(case["task_id"]) for case in cases],
                        "execution_scope": scope,
                        "predict_mode": "overwrite",
                        "model_id": model,
                        "output_mode": "box" if model == "track" else "mask",
                        "pipeline_stages": pipeline(backend_id, model),
                    },
                )
                identity = uuid.UUID(response["job_id"])
                jobs.append(identity)
                async with factory() as db:
                    job = await db.get(AsyncJob, identity)
                    proof = (job.payload or {}).get("video_prediction_authority")
                    require(
                        proof
                        and proof["media_type"] == "video"
                        and proof["schema"] == 1
                        and job.execution_snapshot_id is not None
                        and job.celery_task_id == str(identity),
                        "HTTP 没有持久冻结准确视频作业与 delivery",
                    )
                    require(
                        {row["task_id"] for row in proof["tasks"]}
                        == {str(case["task_id"]) for case in cases}
                        and all(
                            row["original"]["schema"] == 3
                            and row["original"]["bucket"] == storage.bucket
                            for row in proof["tasks"]
                        ),
                        "HTTP 视频任务范围或 saved 原件位置漂移",
                    )
                return identity

            async def completed(identity):
                job = await plumbing.job_done(factory, identity, worker, timeout=180)
                require(
                    job.status == "completed",
                    "真实视频 Worker 未完成；详见私有 Worker 日志",
                )
                return job

            async def accept(
                case,
                prediction_id,
                *,
                shape_index=None,
                headers=None,
                expected_count=None,
            ):
                result = await plumbing.request(
                    client,
                    "POST",
                    f"/api/v1/tasks/{case['task_id']}/predictions/{prediction_id}/accept",
                    headers=headers or auth,
                    params={"shape_index": shape_index}
                    if shape_index is not None
                    else {},
                )
                require(
                    isinstance(result, list) and bool(result),
                    "普通视频采纳没有返回 Annotation",
                )
                if expected_count is not None:
                    require(
                        len(result) == expected_count,
                        "single/all accept 没有精确选择候选",
                    )
                async with factory() as db:
                    rows = list(
                        await db.scalars(
                            select(Annotation).where(
                                Annotation.parent_prediction_id == prediction_id
                            )
                        )
                    )
                    require(
                        {str(row.id) for row in rows} >= {row["id"] for row in result}
                        and all(row.task_id == case["task_id"] for row in rows),
                        "采纳响应和真实 Annotation 原子落库不一致",
                    )
                plumbing.safe_evidence([row.get("attributes", {}) for row in result])
                return result

            bulk = await completed(
                await submit("bulk-three-video-owners", fixtures["cases"])
            )
            require(bulk.result["success_count"] == 3, "三 canonical 视频没有完整执行")
            prediction_ids = {
                case["case"]: await check_prediction(factory, case, bulk)
                for case in fixtures["cases"]
            }
            await accept(
                standalone,
                prediction_ids[standalone["case"]],
                shape_index=0,
                expected_count=1,
            )
            await accept(
                standalone, prediction_ids[standalone["case"]], expected_count=1
            )
            await accept(direct, prediction_ids[direct["case"]], expected_count=2)
            await accept(primary, prediction_ids[primary["case"]], expected_count=2)
            checks["three_canonical_bulk_yolo_single_and_all_accept"] = "passed"

            mask_key = await plumbing.request(
                client,
                "POST",
                "/api/v1/me/api-keys",
                headers=auth,
                status=201,
                json={"name": "Video mask reader", "scopes": ["annotations:read"]},
            )
            mask_read_headers = {"Authorization": "Bearer " + mask_key["plaintext"]}

            async def read_mask(path):
                value = await plumbing.request(
                    client, "GET", path, headers=mask_read_headers
                )
                require(
                    value["size"] == [HEIGHT, WIDTH]
                    and value["counts"] == [0, HEIGHT, HEIGHT * (WIDTH - 1)],
                    "只读视频 Mask HTTP 内容与真实非方形 RLE 不一致",
                )

            for case, model, kind in (
                (direct, "sam-polygon", "video_track_polygon"),
                (standalone, "sam-mask", "video_track_mask"),
                (direct, "sam-mask", "video_track_mask"),
                (primary, "sam-mask", "video_track_mask"),
            ):
                job = await completed(
                    await submit(
                        "workbench-" + model + "-" + case["case"],
                        [case],
                        scope="workbench",
                        model=model,
                    )
                )
                prediction_id = await check_prediction(factory, case, job, kind=kind)
                if kind == "video_track_mask":
                    for frame in (0, 2, FRAMES - 1):
                        await read_mask(
                            f"/api/v1/tasks/{case['task_id']}/predictions/"
                            f"{prediction_id}/mask-content/0/{frame}"
                        )
                accepted = await accept(case, prediction_id, expected_count=1)
                require(
                    accepted[0]["geometry"]["type"] == kind, "SAM 普通采纳丢失实际几何"
                )
                if kind == "video_track_mask":
                    for frame in (0, 2, FRAMES - 1):
                        await read_mask(
                            f"/api/v1/annotations/{accepted[0]['id']}/mask-content/{frame}"
                        )
                    checks[
                        case["case"] + "_readonly_candidate_and_annotation_rle_get"
                    ] = "passed"
                    async with factory() as db:
                        row = await db.get(Annotation, uuid.UUID(accepted[0]["id"]))
                        # A committed fixture simulates the retained annotation
                        # after parent unlink and a legitimate manual keyframe.
                        # This does not qualify an editor or tracker write API.
                        geometry = deepcopy(row.geometry)
                        geometry["keyframes"][1]["source"] = "manual"
                        row.geometry = geometry
                        row.parent_prediction_id = None
                        row.version += 1
                        await db.commit()
                    await read_mask(
                        f"/api/v1/annotations/{accepted[0]['id']}/mask-content/2"
                    )
                    checks[
                        case["case"] + "_retained_lineage_and_manual_keyframe_read"
                    ] = "passed"
                checks[model + "_" + case["case"] + "_workbench_and_normal_accept"] = (
                    "passed"
                )

            for case, retry_entry in ((direct, "single"), (primary, "job")):
                before = {
                    row.id
                    for row, _ in await plumbing.predictions(factory, case["task_id"])
                }
                identity = await submit(
                    "failure-" + retry_entry,
                    [case],
                    scope="workbench",
                    mode="root_failure",
                )
                original = await plumbing.job_done(factory, identity, worker)
                failed_ids = original.result.get("failed_prediction_ids", [])
                require(
                    original.status == "failed" and len(failed_ids) == 1,
                    "视频真实失败没有唯一可重试记录",
                )
                failure_id = uuid.UUID(failed_ids[0])
                require(
                    {
                        row.id
                        for row, _ in await plumbing.predictions(
                            factory, case["task_id"]
                        )
                    }
                    == before,
                    "视频失败提前删除旧候选",
                )
                async with factory() as db:
                    failure = await db.get(FailedPrediction, failure_id)
                    require(
                        failure is not None
                        and failure.execution_snapshot_id
                        == original.execution_snapshot_id
                        and "original_video_input" in failure.extra
                        and "video_execution" in failure.extra,
                        "视频失败丢失独立来源与执行快照",
                    )
                    frozen = deepcopy(failure.extra["original_video_input"]["original"])
                    if retry_entry == "single":
                        snapshot = await db.get(
                            PredictionExecutionSnapshot, original.execution_snapshot_id
                        )
                        require(snapshot is not None, "原执行快照缺失")
                        original_digest = snapshot.digest
                        old_job = await db.get(AsyncJob, identity)
                        await db.delete(old_job)
                        await db.commit()
                        snapshot = await db.get(
                            PredictionExecutionSnapshot, original.execution_snapshot_id
                        )
                        require(
                            snapshot is not None and snapshot.digest == original_digest,
                            "删除旧 Job 改变不可变执行快照",
                        )
                backend.begin("retry-" + retry_entry)
                route = (
                    f"/api/v1/admin/failed-predictions/{failure_id}/retry"
                    if retry_entry == "single"
                    else f"/api/v1/async-jobs/{identity}/retry-failed"
                )
                await plumbing.request(client, "POST", route, headers=auth, status=202)
                async with factory() as db:
                    retry = await db.scalar(
                        select(AsyncJob).where(
                            AsyncJob.project_id == project_id,
                            AsyncJob.kind == "prediction_retry",
                            AsyncJob.payload["failed_prediction_id"].astext
                            == str(failure_id),
                        )
                    )
                    require(retry is not None, "视频 retry HTTP 未创建独立 attempt")
                    proof = retry.payload["video_prediction_authority"]
                    require(
                        proof["tasks"][0]["original"] == frozen
                        and retry.execution_snapshot_id
                        == original.execution_snapshot_id,
                        "视频 retry 替换旧来源或旧快照",
                    )
                    retry_id = retry.id
                jobs.append(retry_id)
                retry = await completed(retry_id)
                require(
                    retry.result["status"] == "succeeded", "视频 retry 没有成功终态"
                )
                await check_prediction(factory, case, retry)
                async with factory() as db:
                    require(
                        await db.get(FailedPrediction, failure_id) is None,
                        "视频 retry 未原子移除原失败",
                    )
                checks[
                    retry_entry
                    + "_http_retry"
                    + ("_without_original_job" if retry_entry == "single" else "")
                ] = "passed"

            old = await plumbing.request(
                client,
                "POST",
                "/api/v1/me/api-keys",
                headers=auth,
                status=201,
                json={"name": "Video old generator", "scopes": ["*"]},
            )
            old_auth = {"Authorization": "Bearer " + old["plaintext"]}
            generated = await completed(
                await submit(
                    "old-key-generator",
                    [standalone],
                    scope="workbench",
                    headers=old_auth,
                )
            )
            candidate_id = await check_prediction(factory, standalone, generated)
            await plumbing.request(
                client,
                "DELETE",
                f"/api/v1/me/api-keys/{old['id']}",
                headers=auth,
                status=204,
            )
            denied = await client.post(
                f"/api/v1/tasks/{standalone['task_id']}/predictions/{candidate_id}/accept",
                headers=old_auth,
            )
            require(denied.status_code in {401, 403}, "撤销的生成 Key 仍可采纳视频候选")
            fresh = await plumbing.request(
                client,
                "POST",
                "/api/v1/me/api-keys",
                headers=auth,
                status=201,
                json={"name": "Video current accept", "scopes": ["annotations:write"]},
            )
            await accept(
                standalone,
                candidate_id,
                headers={"Authorization": "Bearer " + fresh["plaintext"]},
                expected_count=2,
            )
            checks["new_write_key_accepts_after_generator_key_revocation"] = "passed"

            readonly = await plumbing.request(
                client,
                "POST",
                "/api/v1/me/api-keys",
                headers=auth,
                status=201,
                json={"name": "Video read-only", "scopes": ["predictions:read"]},
            )
            denied = await client.post(
                f"/api/v1/tasks/{standalone['task_id']}/predictions/{candidate_id}/accept",
                headers={"Authorization": "Bearer " + readonly["plaintext"]},
            )
            require(denied.status_code == 403, "只读 Key 获得了视频采纳写权限")
            checks["readonly_key_cannot_accept"] = "passed"

            key = await plumbing.request(
                client,
                "POST",
                "/api/v1/me/api-keys",
                headers=auth,
                status=201,
                json={"name": "Video mid-get revoke", "scopes": ["*"]},
            )
            previous = {
                row.id
                for row, _ in await plumbing.predictions(factory, standalone["task_id"])
            }
            guarded = await submit(
                "key-revoked-after-real-video-get",
                [standalone],
                scope="workbench",
                headers={"Authorization": "Bearer " + key["plaintext"]},
                gate=True,
            )
            try:
                require(
                    await asyncio.to_thread(backend.entered.wait, 45),
                    "视频真实 GET 门控未触发",
                )
                await plumbing.request(
                    client,
                    "DELETE",
                    f"/api/v1/me/api-keys/{key['id']}",
                    headers=auth,
                    status=204,
                )
            finally:
                backend.release.set()
            rejected = await plumbing.job_done(factory, guarded, worker)
            require(rejected.status == "failed", "视频真实读取后撤 Key 仍发布成功")
            async with factory() as db:
                failures = rejected.result.get("failed_prediction_ids", [])
                require(len(failures) == 1, "撤权没有精确失败记录")
                failure = await db.get(FailedPrediction, uuid.UUID(failures[0]))
                require(
                    failure is not None
                    and failure.message
                    in {"prediction_authority_revoked", "api_key_authority_revoked"},
                    "视频失败原因不是当前凭据守卫",
                )
            require(
                {
                    row.id
                    for row, _ in await plumbing.predictions(
                        factory, standalone["task_id"]
                    )
                }
                == previous,
                "撤权破坏了旧候选",
            )
            checks["revocation_after_actual_version_get_preserves_old_candidates"] = (
                "passed"
            )

            offline = await completed(
                await submit(
                    "offline-reject", [direct], scope="workbench", model="sam-polygon"
                )
            )
            offline_id = await check_prediction(
                factory, direct, offline, kind="video_track_polygon"
            )
            async with factory() as db:
                location = await db.get(StorageLocation, fixtures["location_id"])
                require(location.enabled, "原件位置初始不可用")
                location.enabled = False
                await db.commit()
            try:
                await plumbing.request(
                    client,
                    "POST",
                    f"/api/v1/tasks/{direct['task_id']}/predictions/{offline_id}/reject",
                    headers=auth,
                    status=204,
                )
            finally:
                async with factory() as db:
                    location = await db.get(StorageLocation, fixtures["location_id"])
                    location.enabled = True
                    await db.commit()
            checks["offline_source_can_reject_candidate"] = "passed"

            # This path deliberately has no model/context selection or setup.
            for case in fixtures["cases"]:
                backend.begin("sync-" + case["case"])
                value = await plumbing.request(
                    client,
                    "POST",
                    f"/api/v1/projects/{project_id}/ml-backends/{backend_id}/predict-test",
                    params={"task_id": str(case["task_id"])},
                    headers=auth,
                )
                require(
                    len(value["results"]) == 1
                    and value["results"][0]["task_id"] == str(case["task_id"]),
                    "同步视频 predict-test 改变原响应",
                )
            checks["sync_three_origins_fixed_version_without_context"] = "passed"
            public = await plumbing.request(
                client, "GET", f"/api/v1/async-jobs/{bulk.id}", headers=auth
            )
            require(
                "video_prediction_authority" not in public["payload"],
                "公开视频 Job 泄露私有证明",
            )

        require(await asyncio.to_thread(worker.idle), "视频 worker 最终未空闲")
        require(not backend.errors, "受控 backend 记录了视频输入验证错误")
        report["cleanup"] = await cleanup_inputs(factory, storage, fixtures, backend)
        await sources_intact(storage, fixtures["cases"])
        async with factory() as db:
            require(
                await db.scalar(select(func.count()).select_from(StorageOperation))
                == 0,
                "视频验收残留未结算物理操作",
            )
        report["canonical_sources_and_current_bucket_traps"] = (
            "full-sha-length-unchanged"
        )
        report["real_jobs_including_deleted_original"] = len(jobs)
        report["real_signed_video_gets"] = dict(
            Counter(event["model"] for event in backend.events)
        )
    finally:
        backend.release.set()


async def qualify(backend, worker, storage, report, expected_database):
    from app.db.base import engine

    try:
        await scenarios(backend, worker, storage, report, expected_database)
    finally:
        await engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    options = parser.parse_args(argv)
    require(ROOT == plumbing.ROOT, "拒绝从另一工作树加载验收 plumbing")
    os.environ.update(DEBUG="false", LOG_LEVEL="WARNING", ML_BACKEND_STORAGE_HOST="")
    # The reused bounded server resolves its setup function in its own module.
    plumbing.setup_contract = setup_contract
    backend = worker = storage = resources = None
    try:
        with plumbing.private_report(options.report) as report:
            report.update(
                qualification="real-asgi-celery-minio-ffmpeg-video-controlled-cpu-responses",
                exclusions=[
                    "real-SAM-YOLO-accuracy-GPU-production-and-scale-performance",
                    "M2-interactive-propagation-tracker-jobs-and-mask-repair",
                    "provider-unknown-close-and-crash-fault-injection",
                    "complete-authorization-race-matrix-covered-by-owned-DB-regressions",
                    "independent-TCP-API-and-browser",
                    "offline-source-destroyed-recovery-and-migration-roundtrip",
                ],
            )
            try:
                report["phase"] = "verify-owned-test-environment"
                resources = plumbing.verified_environment()
                from app.config import settings
                from app.services.storage import StorageService

                storage = StorageService()
                report["phase"] = "qualify-owned-import-versioning"
                report["storage_contract"] = plumbing.lifecycle_and_versions(
                    storage, report["run_id"], resources
                )
                backend = VideoBackend(
                    f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
                    storage.import_bucket,
                )
                backend.thread.start()
                worker = plumbing.Worker(options.report.with_suffix(".worker.log"))
                report["phase"] = "real-video-http-worker-and-versioned-storage"
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
                                    "视频独立 worker 没有优雅空闲退出",
                                )
                        except BaseException as exc:
                            errors.append(
                                {"owner": name, "error_type": type(exc).__name__}
                            )
                if errors:
                    report["shutdown_errors"] = errors
                    raise plumbing.WorktreeError("视频验收句柄未确认全部关闭")
        print(
            "PASS: real video ASGI/Celery/MinIO/ffmpeg transport; controlled CPU responses only."
        )
        return 0
    except BaseException as exc:
        import sys

        print(
            f"FAIL: video acceptance ({type(exc).__name__}); inspect private report/log.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
