"""Frozen native previews with an independent CPU lease and expiring evidence."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import tempfile
from collections import Counter
from datetime import timedelta
from pathlib import PurePosixPath

from fastapi import HTTPException
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from training_contract.preview import (
    LIMITS,
    POLICY,
    POLICY_DIGEST,
    closed_mosaic,
    input_digest,
    native_values,
    validate_request,
)

from app.config import settings
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_preview import TrainingPreview, TrainingPreviewResourceLease
from app.schemas.training_preview import TrainingPreviewOut, TrainingPreviewReport
from app.schemas.training_run import TrainingRunCreate
from app.services import async_job as jobs, training_runs as training
from app.services.async_job_notify import notify_job_terminal
from app.services.storage import storage_service as storage
from app.services import training_dataset_storage, training_preview_storage
from app.services.training_artifacts import read_json_artifact
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    detection_labels,
)

ACTIVE = {"queued", "running", "cancelling", "lost"}
TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
_GENERATE_KEY = object()
now = training.now
log = logging.getLogger(__name__)


def resource_id():
    return settings.training_node_id + "/augmentation-preview/cpu-0"


def qualified_runtime(runtime):
    catalog = training.catalog()
    template = next(
        (item for item in catalog["items"] if item["framework"] == "ultralytics"), None
    )
    return bool(
        catalog["enabled"]
        and template
        and runtime.get("preview_protocol") == 1
        and runtime.get("preview_policy_digest") == POLICY_DIGEST
        and runtime.get("image_digest") == template["image_digest"]
        and runtime.get("gpu_uuid") == settings.training_gpu_uuid
        and runtime.get("frameworks", {}).get("ultralytics") == template["version"]
    )


async def capabilities(db):
    lease = await db.get(TrainingPreviewResourceLease, resource_id())
    qualified = bool(lease and qualified_runtime(lease.runtime))
    online = bool(
        qualified
        and lease.last_seen_at
        and lease.last_seen_at > now() - timedelta(seconds=30)
    )
    return dict(
        qualified=qualified,
        online=online,
        busy=bool(lease and lease.preview_id),
        resource_id=resource_id(),
        policy=POLICY,
        policy_digest=POLICY_DIGEST,
        reason=None if online else "受控执行器尚未上报已验证的原生 CPU 预览能力",
    )


def select_sources(version, seed):
    manifest, snapshot = version.manifest, version.snapshot
    if snapshot.get("classes") != manifest.get("classes"):
        raise ValueError("冻结类别映射缺失或与训练包不符")
    frozen = {str(sample["task_id"]): sample for sample in snapshot.get("samples", [])}
    members = [
        member
        for member in manifest.get("samples", [])
        if member.get("split") == "train"
    ]
    ranked = sorted(
        members,
        key=lambda member: hashlib.sha256(
            f"{seed}:{member['task_id']}:{member.get('sha256')}".encode()
        ).hexdigest(),
    )
    skipped, selected, inspected = Counter(), [], 0
    total_bytes = total_pixels = total_boxes = 0
    for member in ranked[: LIMITS["candidate_scan"]]:
        inspected += 1
        try:
            task_id = str(member["task_id"])
            source = frozen[task_id]
            width, height, size = (
                member[key] for key in ("width", "height", "size_bytes")
            )
            if any(type(v) is not int or v <= 0 for v in (width, height, size)):
                raise ValueError("frozen_metadata_unavailable")
            pixels = width * height
            if size > LIMITS["source_bytes"] or pixels > LIMITS["source_pixels"]:
                skipped["source_resource_bound"] += 1
                continue
            if not isinstance(source.get("annotations"), list):
                raise ValueError("frozen_labels_unavailable")
            if len(source["annotations"]) > LIMITS["source_boxes"]:
                skipped["source_object_bound"] += 1
                continue
            labels = detection_labels(source["annotations"], manifest["classes"])
            if hashlib.sha256(labels.encode()).hexdigest() != member["label_sha256"]:
                raise ValueError("frozen_label_digest")
            boxes = len(labels.splitlines())
            if (
                total_bytes + size > LIMITS["pool_bytes"]
                or total_pixels + pixels > LIMITS["pool_pixels"]
                or total_boxes + boxes > LIMITS["pool_boxes"]
            ):
                skipped["pool_resource_bound"] += 1
                continue
            image_path = f"images/train/{task_id}{PurePosixPath(member['image_path']).suffix.lower()}"
            if PurePosixPath(image_path).suffix not in {
                ".jpg",
                ".jpeg",
                ".png",
                ".bmp",
                ".tif",
                ".tiff",
                ".webp",
            }:
                raise ValueError("unqualified_image_format")
            sha = member["sha256"]
            if (
                not isinstance(sha, str)
                or len(sha) != 64
                or any(c not in "0123456789abcdef" for c in sha)
            ):
                raise ValueError("frozen_image_digest")
            selected.append(
                dict(
                    task_id=task_id,
                    image_path=image_path,
                    label_path=f"labels/train/{task_id}.txt",
                    media_key=f"projects/{version.project_id}/datasets/{version.id}/media/{task_id}",
                    sha256=sha,
                    label_sha256=member["label_sha256"],
                    size_bytes=size,
                    width=width,
                    height=height,
                    labels=labels,
                )
            )
            total_bytes += size
            total_pixels += pixels
            total_boxes += boxes
            if len(selected) == LIMITS["sources"]:
                break
        except (KeyError, TypeError, ValueError, TrainingDatasetError):
            skipped["frozen_metadata_or_labels_unavailable"] += 1
    if not selected:
        raise ValueError("冻结训练分区没有满足预览上限且保留完整标签的素材")
    return selected, dict(
        training_members=len(members),
        inspected=inspected,
        selected=len(selected),
        skipped=dict(skipped),
    )


async def create(db, project, user, body, api_key_id):
    request = body.model_dump(mode="json")
    request_digest = content_digest(request)
    existing = await db.scalar(
        select(TrainingPreview)
        .where(
            TrainingPreview.project_id == project.id,
            TrainingPreview.request_id == body.request_id,
        )
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if existing:
        if (
            existing.request_digest != request_digest
            or existing.creator_id != user.id
            or existing.authorization.get("api_key_id")
            != (str(api_key_id) if api_key_id else None)
        ):
            raise HTTPException(
                status_code=409, detail="同一预览请求标识不能用于不同输入或身份"
            )
        return existing
    if (
        await db.scalar(
            select(func.count())
            .select_from(TrainingPreview)
            .where(
                TrainingPreview.project_id == project.id,
                TrainingPreview.status.in_(ACTIVE),
            )
        )
        >= 3
    ):
        raise HTTPException(
            status_code=409, detail="项目已有三个预览待完成，请先取消或等待"
        )
    run_body = TrainingRunCreate(
        name="原生增强预览",
        dataset_id=body.dataset_id,
        framework=body.framework,
        base_model=body.base_model,
        configuration=body.configuration,
        request_id=body.request_id,
    )
    config = await training.frozen_config(db, project.id, run_body)
    try:
        validate_request(config, body.seed, body.epoch, body.count)
        version = await db.get(TrainingDatasetVersion, body.dataset_id)
        sources, summary = select_sources(version, body.seed)
    except (ValueError, TrainingDatasetError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    lease = await db.get(TrainingPreviewResourceLease, resource_id())
    caps = await capabilities(db)
    if not caps["online"] or not training.runtime_compatible(config, lease.runtime):
        raise HTTPException(
            status_code=409, detail=caps["reason"] or "预览镜像与当前训练配置不一致"
        )
    spec = dict(
        config=config,
        config_digest=content_digest(config),
        native_values=native_values(config),
        dataset_snapshot_digest=version.snapshot_digest,
        policy_digest=POLICY_DIGEST,
        seed=body.seed,
        epoch=body.epoch,
        count=body.count,
        sources=sources,
        pool_summary=summary,
    )
    preview = TrainingPreview(
        project_id=project.id,
        dataset_id=body.dataset_id,
        creator_id=user.id,
        request_id=body.request_id,
        request_digest=request_digest,
        request=request,
        input_digest=input_digest(spec),
        spec=spec,
        authorization={"api_key_id": str(api_key_id) if api_key_id else None},
        status="queued",
        resource_id=resource_id(),
        expires_at=now() + timedelta(seconds=LIMITS["ttl_seconds"]),
    )
    db.add(preview)
    await db.flush()
    try:
        await training_preview_storage.allocate(db, preview)
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    job = await jobs.create_job(
        db,
        kind="training_preview",
        user_id=user.id,
        project_id=project.id,
        payload={
            "preview_id": str(preview.id),
            "dataset_id": str(body.dataset_id),
            "stage": "queued",
        },
    )
    preview.async_job_id = job.id
    return preview


async def lock_resource(db):
    await db.execute(
        insert(TrainingPreviewResourceLease)
        .values(resource_id=resource_id(), runtime={})
        .on_conflict_do_nothing()
    )
    return await db.scalar(
        select(TrainingPreviewResourceLease)
        .where(TrainingPreviewResourceLease.resource_id == resource_id())
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def token_for(preview, claim_id):
    return hmac.new(
        settings.training_executor_token.encode(),
        f"preview:{preview.id}:{claim_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


async def terminal(db, preview, status, error=None):
    preview.status, preview.error_message, preview.completed_at = status, error, now()
    if status == "succeeded":
        await jobs.mark_complete(
            db, preview.async_job_id, result={"preview_id": str(preview.id)}
        )
    elif status == "cancelled":
        await jobs.mark_cancelled(db, preview.async_job_id)
    else:
        await jobs.mark_failed(db, preview.async_job_id, error=error or "原生预览失败")
    await notify_job_terminal(db, job_id=preview.async_job_id)


def due_to_stop(preview):
    if preview.expires_at <= now():
        return "原生预览已过期，正在停止执行"
    if (
        preview.started_at
        and preview.started_at + timedelta(seconds=LIMITS["execution_seconds"]) <= now()
    ):
        return "原生预览超过执行时限，正在停止执行"
    if preview.status == "cancelling":
        return preview.error_message or "已请求取消原生预览"
    return None


async def assignment(db, preview, *, stop=False):
    urls = {}
    if not stop:
        storage_owner(preview)
        version = await db.get(TrainingDatasetVersion, preview.dataset_id)
        try:
            if version is None or version.project_id != preview.project_id:
                raise TrainingDatasetError(
                    "training_storage_dataset_mismatch",
                    "冻结训练版本不属于当前项目",
                    status_code=409,
                )
            owner = training_dataset_storage.resolve(version)
            for source in preview.spec["sources"]:
                owner.contains(source["media_key"])
                if source["media_key"] != owner.key(f"media/{source['task_id']}"):
                    raise TrainingDatasetError(
                        "training_storage_invalid_key",
                        "预览图片与固定训练样本不一致",
                        status_code=409,
                    )
        except TrainingDatasetError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from None
        urls = {
            source["task_id"]: storage.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": owner.bucket, "Key": source["media_key"]},
                ExpiresIn=LIMITS["execution_seconds"],
            )
            for source in preview.spec["sources"]
        }
    return dict(
        preview_id=str(preview.id),
        lease_token=token_for(preview, preview.claim_id),
        input_digest=preview.input_digest,
        spec=preview.spec,
        source_urls=urls,
        stop=stop,
    )


async def claim(db, body):
    if body.resource_id != resource_id():
        raise HTTPException(status_code=409, detail="CPU 预览资源不属于当前节点")
    lease = await lock_resource(db)
    if lease.preview_id:
        preview = await db.get(TrainingPreview, lease.preview_id)
        if (preview.executor_id, preview.boot_id, preview.claim_id) == (
            body.executor_id,
            body.boot_id,
            body.claim_id,
        ):
            token = token_for(preview, body.claim_id)
            if not secrets.compare_digest(
                preview.lease_digest, hashlib.sha256(token.encode()).hexdigest()
            ):
                raise HTTPException(
                    status_code=409, detail="执行器密钥变化，CPU 租约需要协调"
                )
            # A lost response/restart may only recover this exact durable claim.
            stop = bool(due_to_stop(preview))
            try:
                await training.authority(db, preview)
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
                stop = True
            if not stop:
                try:
                    storage_owner(preview)
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                    stop = True
            return await assignment(db, preview, stop=stop)
        return None
    # Resolve an old durable claim before qualifying a new runtime. An idle
    # recovery must not advertise stale capabilities or acquire queued work.
    if body.recover_only:
        return None
    if not qualified_runtime(body.runtime):
        raise HTTPException(
            status_code=409, detail="执行器未通过当前 CPU 预览协议与镜像验证"
        )
    lease.executor_id, lease.boot_id, lease.runtime, lease.last_seen_at = (
        body.executor_id,
        body.boot_id,
        body.runtime,
        now(),
    )
    candidates = (
        await db.scalars(
            select(TrainingPreview)
            .where(
                TrainingPreview.status == "queued",
                TrainingPreview.resource_id == resource_id(),
            )
            .order_by(TrainingPreview.created_at)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for preview in candidates:
        if preview.expires_at <= now():
            await terminal(db, preview, "cancelled", "原生预览排队期间已过期")
            continue
        if not training.runtime_compatible(preview.spec["config"], body.runtime):
            continue
        try:
            storage_owner(preview)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            await terminal(db, preview, "failed", "预览产物存储归属不可用，请先核验")
            continue
        try:
            await training.authority(db, preview)
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            await terminal(db, preview, "failed", "原生预览启动权限已撤销")
            continue
        version = await db.get(TrainingDatasetVersion, preview.dataset_id)
        if (
            not version
            or version.snapshot_digest != preview.spec["dataset_snapshot_digest"]
            or version.package_sha256 != preview.spec["config"]["dataset_sha256"]
        ):
            await terminal(db, preview, "failed", "冻结数据身份发生变化")
            continue
        preview.executor_id, preview.boot_id, preview.claim_id = (
            body.executor_id,
            body.boot_id,
            body.claim_id,
        )
        preview.lease_digest = hashlib.sha256(
            token_for(preview, body.claim_id).encode()
        ).hexdigest()
        preview.runtime, preview.status = body.runtime, "running"
        preview.started_at = preview.heartbeat_at = now()
        lease.preview_id = preview.id
        await jobs.mark_running(db, preview.async_job_id)
        return await assignment(db, preview)
    return None


async def fenced(db, preview_id, body, *, allow_terminal=False):
    lease = await lock_resource(db)
    preliminary = await db.get(TrainingPreview, preview_id)
    if preliminary is None:
        raise HTTPException(status_code=404, detail="原生预览不存在")
    from app.services.project_write_guard import lock_actor_project_authority

    if preliminary.creator_id:
        await lock_actor_project_authority(
            db, preliminary.creator_id, preliminary.project_id
        )
    preview = await db.scalar(
        select(TrainingPreview)
        .where(TrainingPreview.id == preview_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        preview.executor_id != body.executor_id
        or preview.boot_id != body.boot_id
        or not secrets.compare_digest(
            preview.lease_digest or "",
            hashlib.sha256(body.lease_token.encode()).hexdigest(),
        )
        or (
            not allow_terminal
            and (lease.preview_id != preview.id or preview.status in TERMINAL)
        )
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_preview",
                "message": "CPU 预览租约已变化，拒绝迟到回报",
            },
        )
    return lease, preview


async def heartbeat(db, preview_id, body):
    lease, preview = await fenced(db, preview_id, body)
    stop = due_to_stop(preview)
    try:
        await training.authority(db, preview)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        stop = "原生预览权限已撤销，正在停止执行"
    if not stop:
        try:
            storage_owner(preview)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            stop = "原生预览产物存储归属不可用，正在停止执行"
    if stop:
        preview.status, preview.error_message = "cancelling", stop
        await jobs.request_cancel(db, preview.async_job_id)
    preview.heartbeat_at = lease.last_seen_at = now()
    return dict(stop=bool(stop), reason=stop)


def prefix(preview):
    return training_preview_storage.prefix_for(preview.project_id, preview.id)


def storage_owner(preview):
    """Resolve saved ownership before preview output access or new execution."""
    try:
        return training_preview_storage.resolve(preview)
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None


def artifact_key(owner, name, key=_GENERATE_KEY):
    try:
        return training_preview_storage.expect_artifact(
            owner, name, owner.key(name) if key is _GENERATE_KEY else key
        )
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None


async def uploads(db, preview_id, body):
    _, preview = await fenced(db, preview_id, body)
    await training.authority(db, preview)
    if due_to_stop(preview):
        raise HTTPException(
            status_code=409, detail="预览已取消或超过时限，不能授权上传"
        )
    owner = storage_owner(preview)
    inventory = {spec.name: spec.model_dump() for spec in body.artifacts}
    expected_names = {
        f"preview-{index}.png" for index in range(preview.spec["count"])
    } | {"report.json"}
    if (
        len(inventory) != len(body.artifacts)
        or inventory.keys() != expected_names
        or inventory["report.json"]["size_bytes"] > LIMITS["report_bytes"]
    ):
        raise HTTPException(status_code=422, detail="预览产物清单不完整或超过上限")
    if preview.upload_inventory and preview.upload_inventory != inventory:
        raise HTTPException(status_code=409, detail="已授权的预览产物不能改写")
    preview.upload_inventory = inventory
    preview.put_expires_at = now() + timedelta(seconds=LIMITS["put_seconds"])
    result = {}
    for name, spec in inventory.items():
        metadata = {
            "preview-id": str(preview.id),
            "input-digest": preview.input_digest,
            "sha256": spec["sha256"],
        }
        result[name] = dict(
            url=storage.client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": owner.bucket,
                    "Key": artifact_key(owner, name),
                    "ContentLength": spec["size_bytes"],
                    "Metadata": metadata,
                },
                ExpiresIn=LIMITS["put_seconds"],
            ),
            headers={"x-amz-meta-" + key: value for key, value in metadata.items()},
        )
    return result


def read_report(preview, artifacts=None):
    owner = storage_owner(preview)
    artifacts = preview.artifacts if artifacts is None else artifacts
    spec = artifacts.get("report.json")
    if not spec:
        raise ValueError("Missing preview report")
    artifact_key(owner, "report.json", spec["key"])
    raw = read_json_artifact(
        spec,
        preview.id,
        bucket=owner.bucket,
        metadata_key="preview-id",
        required_metadata={
            "input-digest": preview.input_digest,
            "sha256": spec["sha256"],
        },
        node_budget=200000,
        string_budget=64000,
    )
    return validate_report(preview, artifacts, raw)


def validate_report(preview, artifacts, raw):
    """Validate native report semantics against frozen inputs without storage I/O."""
    report = TrainingPreviewReport.model_validate(raw)
    frozen = preview.spec
    expected_transforms = {
        key: frozen["native_values"][key]
        for key in ("mosaic", "mixup", "cutmix", "copy_paste")
    }
    if closed_mosaic(frozen["config"], frozen["epoch"]):
        expected_transforms = {key: 0.0 for key in expected_transforms}
    if (
        report.preview_id != preview.id
        or report.input_digest != preview.input_digest
        or report.config_digest != frozen["config_digest"]
        or report.policy_digest != POLICY_DIGEST
        or report.policy != POLICY
        or report.seed != frozen["seed"]
        or report.epoch != frozen["epoch"]
        or report.mosaic_closed != closed_mosaic(frozen["config"], frozen["epoch"])
        or content_digest(report.native_parameters)
        != content_digest(frozen["native_values"])
        or content_digest(report.transform_parameters)
        != content_digest(expected_transforms)
        or [source.model_dump(mode="json") for source in report.source_pool]
        != frozen["sources"]
        or report.pool_summary != frozen["pool_summary"]
        or len(report.outputs) != frozen["count"]
        or report.runtime.get("framework_version")
        != frozen["config"]["framework_version"]
        or report.runtime.get("image_digest") != frozen["config"]["image_digest"]
        or report.runtime.get("adapter_revision")
        != frozen["config"]["adapter_revision"]
        or not isinstance(report.runtime.get("pipeline_sha256"), str)
        or len(report.runtime["pipeline_sha256"]) != 64
        or report.runtime.get("cuda_initialized") is not False
    ):
        raise ValueError("Native preview report does not bind frozen inputs")
    source_ids = {source.task_id for source in report.source_pool}
    if {output.index for output in report.outputs} != set(range(frozen["count"])):
        raise ValueError("Duplicate/missing preview outputs")
    for output in report.outputs:
        image = artifacts.get(output.image)
        if (
            output.image != f"preview-{output.index}.png"
            or not image
            or image["sha256"] != output.sha256
            or not set(output.source_draws) <= set(output.sources_read) <= source_ids
            or any(
                box.class_index >= len(frozen["config"]["classes"])
                for box in output.boxes
            )
            or output.filtered
            != sum(operation.filtered for operation in output.operations)
            or output.clipped
            != sum(operation.clipped for operation in output.operations)
        ):
            raise ValueError("Invalid native output/source evidence")
    return report


def read_image(preview, output, artifacts=None):
    owner = storage_owner(preview)
    artifacts = preview.artifacts if artifacts is None else artifacts
    spec = artifacts[output.image]
    key = artifact_key(owner, output.image, spec["key"])
    response = storage.client.get_object(Bucket=owner.bucket, Key=key)
    target = tempfile.SpooledTemporaryFile(max_size=8 * 1024**2, mode="w+b")
    try:
        metadata = response.get("Metadata", {})
        if (
            response["ContentLength"] != spec["size_bytes"]
            or metadata.get("preview-id") != str(preview.id)
            or metadata.get("input-digest") != preview.input_digest
            or metadata.get("sha256") != spec["sha256"]
        ):
            raise ValueError("Preview object identity mismatch")
        sha, size = hashlib.sha256(), 0
        for chunk in iter(lambda: response["Body"].read(1024**2), b""):
            size += len(chunk)
            if size > spec["size_bytes"] or size > LIMITS["output_bytes"]:
                raise ValueError("Preview object exceeds bound")
            sha.update(chunk)
            target.write(chunk)
        if size != spec["size_bytes"] or sha.hexdigest() != spec["sha256"]:
            raise ValueError("Preview object digest differs")
        target.seek(0)
        with Image.open(target) as image:
            if image.format != "PNG" or image.size != (output.width, output.height):
                raise ValueError("Native PNG dimensions differ from report")
            image.verify()
        target.seek(0)
        return target
    except BaseException:
        target.close()
        raise
    finally:
        response["Body"].close()


async def finish(db, preview_id, body):
    lease, preview = await fenced(db, preview_id, body, allow_terminal=True)
    receipt = content_digest(body.model_dump(mode="json"))
    if preview.status in TERMINAL:
        if preview.finish_digest != receipt:
            raise HTTPException(status_code=409, detail="已结束的预览不能改写终态")
        return preview
    if lease.preview_id != preview.id:
        raise HTTPException(status_code=409, detail="CPU 预览租约已变化")
    stop = due_to_stop(preview)
    status = "cancelled" if stop else body.status
    if status == "succeeded":
        await training.authority(db, preview)
        owner = storage_owner(preview)
        artifacts = {
            name: {**spec, "key": artifact_key(owner, name)}
            for name, spec in preview.upload_inventory.items()
        }
        try:
            report = await asyncio.to_thread(read_report, preview, artifacts)
            for output in report.outputs:
                image = await asyncio.to_thread(read_image, preview, output, artifacts)
                image.close()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail="预览产物缺失、损坏或与冻结输入不符"
            ) from exc
        preview.artifacts = artifacts
    preview.finish_digest = receipt
    await terminal(db, preview, status, stop or body.error_message)
    lease.preview_id = None
    return preview


async def cancel(db, preview):
    if preview.status == "queued":
        await terminal(db, preview, "cancelled", "已取消原生预览")
    elif preview.status in ACTIVE:
        preview.status, preview.error_message = (
            "cancelling",
            "已请求取消原生预览，等待确认进程退出",
        )
        await jobs.request_cancel(db, preview.async_job_id)


async def output(preview):
    expired = preview.expires_at <= now()
    report = None
    if preview.status == "succeeded" and not expired:
        try:
            report = await asyncio.to_thread(read_report, preview)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail="原生预览报告缺失、损坏或身份不符"
            ) from exc
    lost = (
        preview.status in {"running", "lost"}
        and preview.heartbeat_at
        and preview.heartbeat_at < now() - timedelta(seconds=30)
    )
    return TrainingPreviewOut(
        id=preview.id,
        project_id=preview.project_id,
        async_job_id=preview.async_job_id,
        request=preview.request,
        input_digest=preview.input_digest,
        config_digest=preview.spec["config_digest"],
        configuration=preview.spec["config"]["configuration"],
        status="expired" if expired else "lost" if lost else preview.status,
        error_message="执行器心跳失联；CPU 租约保留，等待确认进程退出"
        if lost
        else preview.error_message,
        created_at=preview.created_at,
        started_at=preview.started_at,
        completed_at=preview.completed_at,
        expires_at=preview.expires_at,
        report=report,
    )


async def cleanup(db):
    # Release the CPU fence before storage I/O, so unrelated cleanup cannot
    # starve the current subprocess heartbeat.
    lease = await lock_resource(db)
    stopping = 0
    if lease.preview_id:
        active = await db.scalar(
            select(TrainingPreview)
            .where(TrainingPreview.id == lease.preview_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            active
            and active.status == "running"
            and active.heartbeat_at
            and active.heartbeat_at < now() - timedelta(seconds=30)
        ):
            active.status, active.error_message = (
                "lost",
                "执行器心跳失联；CPU 租约保留，等待确认进程退出",
            )
        if active and active.expires_at <= now():
            active.status, active.error_message = (
                "cancelling",
                "原生预览已过期，等待确认进程退出",
            )
            await jobs.request_cancel(db, active.async_job_id)
            stopping += 1
    await db.commit()
    rows = (
        await db.scalars(
            select(TrainingPreview)
            .where(
                TrainingPreview.expires_at <= now(),
                TrainingPreview.status.in_(TERMINAL | {"queued"}),
            )
            .order_by(TrainingPreview.expires_at)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    ).all()
    removed = 0
    for preview in rows:
        if await db.scalar(
            select(TrainingPreviewResourceLease.resource_id)
            .where(TrainingPreviewResourceLease.preview_id == preview.id)
            .limit(1)
        ):
            continue
        if preview.status == "queued":
            await terminal(db, preview, "cancelled", "原生预览已过期")
        preview.status = "expired"
        if preview.put_expires_at and preview.put_expires_at >= now():
            continue
        try:
            owner = training_preview_storage.resolve(preview)
        except TrainingDatasetError as exc:
            log.warning("Preview cleanup blocked for %s (%s)", preview.id, exc.code)
            continue
        try:
            await asyncio.to_thread(
                storage.delete_prefix, owner.prefix, bucket=owner.bucket
            )
        except Exception:
            log.exception("Preview cleanup failed for %s", preview.id)
            continue
        await db.delete(preview)
        removed += 1
    return dict(removed=removed, stopping=stopping)
