"""Controlled artifacts and immutable prediction identities."""

from __future__ import annotations

from app.services import training_storage_io as storage_io

from aap_protocol_v2.backend_auth import managed_provider

from aap_task_contract import normalize_task, normalize_task_fields
from training_contract.dataset import config_task
from training_contract.inference_runtime import validate_inference_runtime

import math
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import case, func, select
from training_contract.model_return import registration_eligibility

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.trained_model import (
    ModelDeployment,
    ModelRelease,
    ProjectModelSelection,
    TrainedModelVersion,
)
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.services.ml_backend import MLBackendService
from app.services.ml_client import MLBackendClient
from app.services.storage import storage_service as storage
from app.services import training_dataset_storage
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest
from app.services import training_run_storage
from app.services.training_runs import verify_artifact
from app.services.training_artifacts import read_json_artifact
from app.services.training_evaluations import parse_evaluation


def model_task(version):
    task = normalize_task((version.provenance or {}).get("task", "detect"))
    if task not in {"detect", "segment"}:
        raise HTTPException(status_code=409, detail="模型训练任务身份无效")
    return task


def model_task_expression():
    value = func.coalesce(TrainedModelVersion.provenance["task"].astext, "detect")
    return case(
        (value.in_(["detect", "detection", "image-detection"]), "detect"),
        (
            value.in_(
                [
                    "segment",
                    "segmentation",
                    "image-instance-segmentation",
                    "instance-segmentation",
                ]
            ),
            "segment",
        ),
        else_=value,
    )


def release_task(model_id):
    return {"project-release": "detect", "project-release:segment": "segment"}.get(
        model_id
    )


def project_classes(project, task="detect"):
    binding = (project.tool_bindings or {}).get(
        "region" if task == "segment" else "bbox", {}
    )
    values = binding.get("classes", []) if binding.get("enabled") else []
    return [v["name"] if isinstance(v, dict) else v for v in values]


def validate_bindings(project, classes, bindings, task="detect"):
    allowed = project_classes(project, task)
    if (
        not classes
        or len(bindings) != len(classes)
        or any(b not in allowed for b in bindings)
    ):
        raise HTTPException(
            status_code=422, detail="须为每个原生类别绑定当前项目对应工具的类别"
        )
    if task == "segment":
        from app.services.raster_mask_storage import assert_raster_mask_write_enabled

        assert_raster_mask_write_enabled(project)


def owner_for_attempt(run, attempt, *artifacts):
    """Resolve physical ownership without changing any frozen model payload."""
    if run is None or attempt is None or attempt.run_id != run.id:
        raise HTTPException(status_code=409, detail="训练产物的来源执行不一致")
    try:
        owner = training_run_storage.resolve(run)
        for artifact in artifacts:
            training_run_storage.expect_attempt(owner, attempt.id, artifact["key"])
        return owner
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    except (KeyError, TypeError):
        raise HTTPException(
            status_code=409, detail="训练产物缺少固定对象定位"
        ) from None


async def owner_for_model(db, version):
    """Models retain the referenced run namespace instead of duplicating it."""
    run = await db.scalar(
        select(TrainingRun).where(
            TrainingRun.id == version.run_id,
            TrainingRun.project_id == version.project_id,
        )
    )
    attempt = await db.get(TrainingAttempt, version.attempt_id)
    if (
        run is None
        or attempt is None
        or run.id != version.run_id
        or run.project_id != version.project_id
        or attempt.id != version.attempt_id
        or attempt.run_id != run.id
        or not isinstance(version.artifact, dict)
        or not isinstance(attempt.artifacts, dict)
        or version.artifact.get("role") != "weights"
        or not any(
            artifact == version.artifact
            for artifact in (attempt.artifacts or {}).values()
        )
    ):
        raise HTTPException(status_code=409, detail="模型缺少一致的保留权重来源")
    return owner_for_attempt(run, attempt, version.artifact)


async def register(db, project, user, body):
    request_digest = content_digest(body.model_dump(mode="json"))
    existing = await db.scalar(
        select(TrainedModelVersion).where(
            TrainedModelVersion.project_id == project.id,
            TrainedModelVersion.request_id == body.request_id,
        )
    )
    if existing:
        if existing.request_digest != request_digest:
            raise HTTPException(status_code=409, detail="注册请求标识已用于其他内容")
        return existing
    run = await db.scalar(
        select(TrainingRun)
        .where(
            TrainingRun.id == body.run_id,
            TrainingRun.project_id == project.id,
        )
        .with_for_update()
    )
    attempt = await db.get(TrainingAttempt, body.attempt_id)
    if not run or not attempt or attempt.run_id != run.id:
        raise HTTPException(status_code=404, detail="来源训练实验或执行尝试不存在")
    existing = await db.scalar(
        select(TrainedModelVersion).where(
            TrainedModelVersion.project_id == project.id,
            TrainedModelVersion.attempt_id == attempt.id,
        )
    )
    if existing:
        if existing.request_id == body.request_id:
            if existing.request_digest != request_digest:
                raise HTTPException(
                    status_code=409, detail="注册请求标识已用于其他内容"
                )
            return existing
        raise HTTPException(
            status_code=409, detail="该执行已回流到项目模型，请使用已添加的模型"
        )
    if attempt.status != "succeeded":
        raise HTTPException(status_code=409, detail="只能注册成功训练的权重")
    eligibility = registration_eligibility(run.config)
    if not eligibility["qualified"]:
        raise HTTPException(status_code=422, detail=eligibility["reason"])
    artifacts = list(attempt.artifacts.values())
    weight_artifacts = [a for a in artifacts if a.get("role") == "weights"]
    metric_artifacts = [a for a in artifacts if a.get("role") == "metrics"]
    if len(weight_artifacts) != 1 or len(metric_artifacts) != 1:
        raise HTTPException(
            status_code=409, detail="来源执行缺少唯一的已验证权重与最终评估"
        )
    weights, metrics = weight_artifacts[0], metric_artifacts[0]
    owner = owner_for_attempt(run, attempt, weights, metrics)
    classes = run.config["classes"]
    validate_bindings(project, classes, body.bindings, config_task(run.config))
    try:
        await storage_io.run(
            db,
            owner,
            "training_model_read",
            lambda transport: verify_artifact(
                weights["key"],
                weights,
                attempt.id,
                bucket=owner.bucket,
                transport=transport,
            ),
        )
        evaluation = await storage_io.run(
            db,
            owner,
            "training_model_read",
            lambda transport: read_json_artifact(
                metrics, attempt.id, bucket=owner.bucket, transport=transport
            ),
        )
        if (
            run.config["framework"] == "libreyolo"
            or config_task(run.config) == "segment"
        ):
            checked = parse_evaluation(
                evaluation, run, attempt, metrics, weights, 0, len(classes)
            )
            if (
                config_task(run.config) == "segment"
                and checked["identity_status"] != "verified"
            ):
                raise ValueError(
                    "Segmentation requires verified candidate and evaluator identity"
                )
            evaluation = {
                **evaluation,
                "summary": checked["summary"],
                "per_class_available": checked["per_class_available"],
                "comparison_identity_status": checked["identity_status"],
                "comparison_compatibility_digest": checked["compatibility_digest"],
                "per_class": [
                    {"index": row["index"], "name": row["name"], **row["metrics"]}
                    for row in checked["per_class"]
                ],
            }
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="来源产物校验失败，不能注册"
        ) from exc
    version = TrainedModelVersion(
        project_id=project.id,
        run_id=run.id,
        attempt_id=attempt.id,
        creator_id=user.id,
        request_id=body.request_id,
        request_digest=request_digest,
        name=body.name,
        framework=run.config["framework"],
        framework_version=run.config["framework_version"],
        artifact=weights,
        classes=classes,
        bindings=body.bindings,
        binding_digest=content_digest({"classes": classes, "bindings": body.bindings}),
        provenance={
            "task": config_task(run.config),
            "media_type": "image",
            "run_id": str(run.id),
            "attempt_id": str(attempt.id),
            "dataset_id": str(run.dataset_id),
            "dataset_digest": run.config["dataset_digest"],
            "validation_digest": run.config["validation_digest"],
            "config_digest": run.config_digest,
            "runtime": attempt.runtime,
            "base_model": run.config["base_model"],
            **(
                {"model_return": eligibility["qualification"]}
                if eligibility["qualification"]
                else {}
            ),
        },
        evaluation=evaluation,
    )
    db.add(version)
    await db.flush()
    return version


async def load_version(db, project_id, version_id):
    version = await db.scalar(
        select(TrainedModelVersion).where(
            TrainedModelVersion.id == version_id,
            TrainedModelVersion.project_id == project_id,
        )
    )
    if not version:
        raise HTTPException(status_code=404, detail="模型版本不存在")
    return version


def expected_receipt(version):
    receipt = {
        "model_version_id": str(version.id),
        "sha256": version.artifact["sha256"],
        "classes_digest": content_digest(version.classes),
        "classes": version.classes,
        "framework": version.framework,
        "framework_version": version.framework_version,
        "task": "segmentation" if model_task(version) == "segment" else "detection",
    }
    if model_task(version) == "segment":
        from training_contract.model_return import segmentation_identity

        try:
            qualification = segmentation_identity(
                version.provenance["base_model"]["id"], version.framework_version
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=409, detail="模型分割回流资格缺失") from exc
        if version.provenance.get("model_return") != qualification:
            raise HTTPException(status_code=409, detail="模型分割回流资格已变化")
        receipt.update(qualification)
    elif version.framework == "ultralytics" and version.framework_version != "8.4.177":
        from training_contract.model_return import ultralytics_identity

        try:
            qualification = ultralytics_identity(
                version.provenance["base_model"]["id"], version.framework_version
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=409, detail="模型回流资格缺失") from exc
        if version.provenance.get("model_return") != qualification:
            raise HTTPException(status_code=409, detail="模型回流资格已变化")
        receipt.update(qualification)
    elif version.framework == "libreyolo":
        qualification = registration_eligibility(
            {
                "framework": version.framework,
                "framework_version": version.framework_version,
                "base_model": version.provenance.get("base_model"),
            }
        )
        if (
            not qualification["qualified"]
            or version.provenance.get("model_return") != qualification["qualification"]
        ):
            raise HTTPException(status_code=409, detail="模型回流资格身份缺失或已变化")
        receipt.update(
            **qualification["qualification"],
            artifact_size_bytes=version.artifact["size_bytes"],
        )
    return receipt


def check_receipt(version, receipt):
    expected = expected_receipt(version)
    if not isinstance(receipt, dict) or any(
        receipt.get(k) != v for k, v in expected.items()
    ):
        raise HTTPException(
            status_code=409, detail="推理实例的模型、权重或类别身份不匹配"
        )

    if "inference_runtime" in receipt:
        try:
            validate_inference_runtime(expected, receipt["inference_runtime"])
        except ValueError as exc:
            raise HTTPException(
                status_code=409, detail="推理实例的训练与推理版本组合尚未通过资格检查"
            ) from exc


def validate_result_runtime(meta, ref):
    if "inference_runtime" not in ref and "inference_runtime" in (meta or {}):
        actual = validate_inference_runtime(ref, meta["inference_runtime"])
        if actual["version"] != ref.get("framework_version"):
            raise ValueError("Legacy frozen reference cannot change inference runtime")
    if "inference_runtime" in ref:
        expected = validate_inference_runtime(ref, ref["inference_runtime"])
        if (meta or {}).get("inference_runtime") != expected:
            raise ValueError("Backend returned a different inference runtime")


def inference_version(ref):
    if "inference_runtime" in ref:
        return validate_inference_runtime(ref, ref["inference_runtime"])["version"]
    return ref.get("framework_version")


def wire_reference(ref):
    keys = ["model_version_id", "sha256", "classes_digest"]
    if "inference_runtime" in ref:
        keys.append("inference_runtime")
    return {key: ref[key] for key in keys}


async def enabled_backend(db, project_id, registry_id):
    backend = await db.get(MLBackendRegistry, registry_id)
    if not backend or not await MLBackendService(db).is_enabled(
        project_id, registry_id
    ):
        raise HTTPException(status_code=404, detail="推理实例未在当前项目启用")
    if (
        (
            backend.auth_method != "token"
            and managed_provider(backend.auth_method) != "yolo"
        )
        or not backend.auth_token
        or len(backend.auth_token) < 32
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "推理实例需使用系统托管或手动 Token 认证。"
                "请在「模型市场 → 注册管理 → 实例 → 编辑」选择系统托管并保存；"
                "远程或自行配置的 YOLO 需填写与服务端 YOLO_MANAGED_MODELS_TOKEN "
                "一致且至少 32 个字符的 Token。"
            ),
        )
    return backend


async def assert_routable_backend(db, project_id, registry_id):
    from app.db.models.ml_backend_pool import MLBackendServicePool
    from app.services.ml_routing.contracts import RouterMode
    from app.services.ml_routing.router import MLBackendRouter, _router_mode

    pool_id = await MLBackendService(db).pool_id_for_registry(registry_id)
    pool = await db.get(MLBackendServicePool, pool_id) if pool_id else None
    mode = _router_mode()
    if (
        not pool
        or not pool.enabled
        or (
            mode in {RouterMode.OFF, RouterMode.OBSERVE}
            and pool.legacy_instance_id != registry_id
        )
    ):
        raise HTTPException(
            status_code=409, detail="该实例不满足当前服务池的精确路由条件"
        )
    candidates = await MLBackendRouter(db, mode=mode)._build_candidates(
        pool, required_instance_id=registry_id
    )
    if not any(
        c.instance_id == registry_id and c.health_fresh and c.fingerprint_ok
        for c in candidates
    ):
        raise HTTPException(status_code=409, detail="推理实例健康或能力校验尚未就绪")


def download_url(key, *, bucket, transport=None):
    transport = storage if transport is None else transport
    return transport.generate_download_url(
        key, bucket=bucket, expires_in=600, align=False, audience="ml"
    )


def frozen_sample_location(project_id, dataset, task_id):
    """Keep retained trial inputs under their dataset owner, apart from weights."""
    try:
        if dataset is None or dataset.project_id != project_id:
            raise TrainingDatasetError(
                "training_storage_dataset_mismatch",
                "冻结训练版本不属于当前项目",
                status_code=409,
            )
        owner = training_dataset_storage.resolve(dataset)
        return owner, owner.key(f"media/{task_id}")
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None


async def install_and_trial(db, project, version, deployment):
    weights_owner = await owner_for_model(db, version)
    dataset = await db.get(
        TrainingDatasetVersion, uuid.UUID(version.provenance["dataset_id"])
    )
    if dataset is None:
        raise HTTPException(status_code=409, detail="固定训练版本不可用")
    sample = next((s for s in dataset.manifest["samples"] if s["split"] == "val"), None)
    if not sample:
        raise HTTPException(status_code=409, detail="缺少固定验证样本")
    owner, sample_key = frozen_sample_location(project.id, dataset, sample["task_id"])
    backend = await enabled_backend(db, project.id, deployment.registry_id)
    await assert_routable_backend(db, project.id, deployment.registry_id)
    client = MLBackendClient(backend)
    weights_url = await storage_io.run(
        db,
        weights_owner,
        "training_model_url",
        lambda transport: download_url(
            version.artifact["key"], bucket=weights_owner.bucket, transport=transport
        ),
    )
    sample_url = await storage_io.run(
        db,
        owner,
        "training_model_url",
        lambda transport: download_url(
            sample_key, bucket=owner.bucket, transport=transport
        ),
    )
    receipt = await client.install_trained_model(
        {
            **expected_receipt(version),
            "size_bytes": version.artifact["size_bytes"],
            "download_url": weights_url,
        }
    )
    check_receipt(version, receipt)
    ref = frozen_reference(
        version, deployment, runtime=receipt.get("inference_runtime")
    )
    results = await client.predict(
        [
            {
                "id": sample["task_id"],
                "file_path": sample_url,
            },
        ],
        context=wire_context(ref, {"conf": 0.01}),
    )
    if len(results) != 1 or str(results[0].task_id) != sample["task_id"]:
        raise HTTPException(status_code=422, detail="样本试用返回的任务身份不匹配")
    validate_and_map_results(results, ref)
    if model_task(version) == "segment":
        from app.services.controlled_segmentation import materialize_masks

        dimensions = [sample["height"], sample["width"]]
        if results[0].meta["segmentation"]["dimensions"] != dimensions:
            raise ValueError("Trial masks differ from frozen source dimensions")
        await materialize_masks(db, results[0].result, results[0].meta)
    return receipt, {
        "task_id": sample["task_id"],
        "validation_digest": version.provenance["validation_digest"],
        "inference_time_ms": results[0].inference_time_ms,
        "result": results[0].result,
        "score": results[0].score,
        "sample_scope": "one_fixed_validation_image",
        **(
            {"inference_runtime": receipt["inference_runtime"]}
            if "inference_runtime" in receipt
            else {}
        ),
        **(
            {"segmentation": results[0].meta["segmentation"]}
            if model_task(version) == "segment"
            else {}
        ),
    }


def frozen_reference(
    version, deployment, release=None, *, include_runtime=True, runtime=None
):
    ref = {
        **expected_receipt(version),
        "project_id": str(version.project_id),
        "registry_id": str(deployment.registry_id),
        "deployment_id": str(deployment.id),
        "binding_digest": version.binding_digest,
        "bindings": version.bindings,
        "provenance": version.provenance,
        "release_id": str(release.id) if release else None,
        "release_generation": release.generation if release else None,
    }
    runtime = runtime or (deployment.receipt or {}).get("inference_runtime")
    if include_runtime and runtime is not None:
        ref["inference_runtime"] = validate_inference_runtime(ref, runtime)
    return {**ref, "reference_digest": content_digest(ref)}


def wire_context(ref, params=None):
    return {
        "task": normalize_task(ref.get("task", "detection")),
        "model_id": f"trained:{ref['model_version_id']}",
        "model_variants": {},
        "params": params or {},
        "frozen_model_ref": wire_reference(ref),
    }


def is_controlled(model_id):
    return bool(
        model_id
        and (model_id.startswith("trained:") or release_task(model_id) is not None)
    )


def controlled_device(backend):
    compute = (getattr(backend, "health_meta", None) or {}).get("compute") or {}
    return "cpu" if compute.get("effective_device") == "cpu" else None


def assert_image_scope(
    project,
    *,
    prompt=None,
    task_type=None,
    model_variants=None,
    pipeline_stages=None,
    execution_unit=None,
    task=None,
    prompt_type=None,
):
    semantic = normalize_task_fields(
        {"task": task, "task_type": task_type, "prompt_type": prompt_type}
    )
    if (
        project is None
        or project.data_type != "image"
        or execution_unit not in {None, "image", "task"}
    ):
        raise HTTPException(status_code=422, detail="受控模型仅支持图片检测或实例分割")
    stages = [
        s.model_dump() if hasattr(s, "model_dump") else s
        for s in (pipeline_stages or [])
    ]
    if not stages:
        stages = [
            {
                "model_id": "project-release",
                "task": semantic.get("task"),
                "prompt_type": semantic.get("prompt_type"),
                "model_variants": model_variants,
            }
        ]
    for stage in stages:
        if not is_controlled(stage.get("model_id")) and not stage.get(
            "frozen_model_ref"
        ):
            continue
        root = stage.get("parent_stage") is None
        source = stage.get("source") or {}
        mode = (
            (stage.get("input") or {}).get("mode")
            or (stage.get("roi") or {}).get("mode")
            or ("full_image" if root else "crop")
        )
        stage_task = normalize_task(
            (stage.get("frozen_model_ref") or {}).get("task")
            or stage.get("task")
            or release_task(stage.get("model_id"))
            or "detect"
        )
        if (
            stage_task not in {"detect", "segment"}
            or (
                stage.get("task") is not None
                and normalize_task(stage["task"]) != stage_task
            )
            or (stage_task == "segment" and (not root or mode != "full_image"))
            or stage.get("prompt_type")
            or stage.get("model_variants")
            or (root and (prompt or stage.get("roi")))
            or mode != ("full_image" if root else "crop")
            or (stage.get("write") or {}).get("target")
            not in {None, "geometry", "intermediate"}
            or source.get("kind") not in {None, "dataset"}
            or source.get("data_type") not in {None, "image"}
        ):
            raise HTTPException(
                status_code=422,
                detail="受控分割只支持整图 region 输出；受控检测支持整图或父框裁剪；均不支持交互提示",
            )


def validate_and_map_results(results, ref):
    for result in results:
        validate_result_runtime(result.meta, ref)
    if normalize_task(ref.get("task", "detection")) == "segment":
        from app.services.controlled_segmentation import validate_results

        return validate_results(results, ref)
    identity = {k: ref[k] for k in ("model_version_id", "sha256", "classes_digest")}
    for result in results:
        if (result.meta or {}).get("trained_model") != identity:
            raise ValueError("Backend returned a different controlled model identity")
        mapped = []
        for item in result.result:
            index = item.get("native_class_index")
            if type(index) is not int or not 0 <= index < len(ref["classes"]):
                raise ValueError("Backend returned an unknown native class index")
            if item.get("type") != "rectanglelabels":
                raise ValueError("Controlled detection returned unsupported geometry")
            value = item.get("value", {})
            if value.get("rectanglelabels") != [ref["classes"][index]]:
                raise ValueError("Backend class order differs from registered classes")
            coordinates = [value.get(k) for k in ("x", "y", "width", "height")]
            if any(
                type(v) not in {float, int} or not math.isfinite(v) for v in coordinates
            ):
                raise ValueError(
                    "Controlled boxes must have finite percent coordinates"
                )
            x, y, width, height = coordinates
            if not (
                0 <= x <= 100
                and 0 <= y <= 100
                and width > 0
                and height > 0
                and x + width <= 100.00001
                and y + height <= 100.00001
            ):
                raise ValueError("Controlled box is outside the image")
            score = item.get("score")
            if (
                type(score) not in {float, int}
                or not math.isfinite(score)
                or not 0 <= score <= 1
            ):
                raise ValueError("Controlled confidence is invalid")
            mapped.append(
                {
                    "type": "rectanglelabels",
                    "native_class_index": index,
                    "score": score,
                    "coordinate_units": "percent",
                    "value": {
                        "x": x,
                        "y": y,
                        "width": width,
                        "height": height,
                        "rectanglelabels": [ref["bindings"][index]],
                    },
                }
            )
        result.result = mapped
        result.model_version = f"trained:{ref['model_version_id']}"


async def resolve_reference(
    db, project, model_id, registry_id, *, allow_candidate=False
):
    if not is_controlled(model_id):
        return None
    release = None
    task = release_task(model_id)
    if task is not None:
        selection = await db.get(ProjectModelSelection, (project.id, task))
        release = (
            await db.get(ModelRelease, selection.release_id)
            if selection and selection.release_id
            else None
        )
        if not release or not release.model_version_id:
            raise HTTPException(status_code=409, detail="项目尚未启用受控模型发布")
        if release.task != task or release.project_id != project.id:
            raise HTTPException(status_code=409, detail="项目发布任务身份不一致")
        version_id = release.model_version_id
    else:
        try:
            version_id = uuid.UUID(model_id.removeprefix("trained:"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="模型版本标识无效") from exc
    version = await load_version(db, project.id, version_id)
    if task is not None and model_task(version) != task:
        raise HTTPException(status_code=409, detail="模型与项目发布的任务不一致")
    if release is None:
        from app.db.models.model_decision import ModelDecision

        published = await db.scalar(
            select(ModelRelease.id)
            .where(
                ModelRelease.project_id == project.id,
                ModelRelease.model_version_id == version.id,
            )
            .limit(1)
        )
        decision = await db.scalar(
            select(ModelDecision)
            .where(ModelDecision.model_version_id == version.id)
            .order_by(ModelDecision.sequence.desc())
            .limit(1)
        )
        if (not published and not allow_candidate) or (
            decision and decision.disposition == "archived"
        ):
            raise HTTPException(
                status_code=409,
                detail="普通推理只能使用已发布且未归档的模型；候选模型请先加载与试用",
            )
    validate_bindings(project, version.classes, version.bindings, model_task(version))
    deployment = await db.scalar(
        select(ModelDeployment).where(
            ModelDeployment.model_version_id == version.id,
            ModelDeployment.registry_id == registry_id,
            ModelDeployment.status == "ready",
        )
    )
    if not deployment or (release and release.deployment_id != deployment.id):
        raise HTTPException(status_code=409, detail="所选实例没有该模型版本的就绪部署")
    check_receipt(version, deployment.receipt)
    await enabled_backend(db, project.id, registry_id)
    await assert_routable_backend(db, project.id, registry_id)
    return frozen_reference(version, deployment, release)


async def validate_frozen_reference(db, project_id, ref):
    from app.db.models.project import Project

    project = await db.get(Project, project_id)
    assert_image_scope(project)
    if not isinstance(ref, dict) or ref.get("project_id") != str(project_id):
        raise ValueError("Controlled model project identity mismatch")
    payload = {k: v for k, v in ref.items() if k != "reference_digest"}
    if content_digest(payload) != ref.get("reference_digest"):
        raise ValueError("Controlled model reference digest mismatch")
    version = await load_version(db, project_id, uuid.UUID(ref["model_version_id"]))
    validate_bindings(project, version.classes, version.bindings, model_task(version))
    deployment = await db.get(ModelDeployment, uuid.UUID(ref["deployment_id"]))
    if (
        not deployment
        or deployment.model_version_id != version.id
        or deployment.status != "ready"
    ):
        raise ValueError("Controlled deployment is unavailable")
    release = (
        await db.get(ModelRelease, uuid.UUID(ref["release_id"]))
        if ref.get("release_id")
        else None
    )
    if ref.get("release_id") and (
        not release
        or release.project_id != project_id
        or release.task != model_task(version)
        or release.model_version_id != version.id
        or release.deployment_id != deployment.id
    ):
        raise ValueError(
            "Controlled release belongs to a different project, task or model"
        )
    if (
        frozen_reference(
            version, deployment, release, include_runtime="inference_runtime" in ref
        )
        != ref
    ):
        raise ValueError("Controlled model reference changed")
    await enabled_backend(db, project_id, deployment.registry_id)
    return deployment.registry_id


async def publish(db, project, user, body):
    digest = content_digest(body.request_payload())
    existing = await db.scalar(
        select(ModelRelease).where(
            ModelRelease.project_id == project.id,
            ModelRelease.request_id == body.request_id,
        )
    )
    if existing:
        if existing.request_digest != digest:
            raise HTTPException(status_code=409, detail="发布请求标识已用于其他内容")
        return existing
    selection = await db.scalar(
        select(ProjectModelSelection)
        .where(
            ProjectModelSelection.project_id == project.id,
            ProjectModelSelection.task == body.task,
        )
        .with_for_update()
    )
    if not selection:
        selection = ProjectModelSelection(
            project_id=project.id, task=body.task, generation=0
        )
        db.add(selection)
        await db.flush()
    if selection.generation != body.expected_generation:
        raise HTTPException(status_code=409, detail="发布选择已变化，请刷新后重试")
    current = (
        await db.get(ModelRelease, selection.release_id)
        if selection.release_id
        else None
    )
    if (current.model_version_id if current else None) == body.model_version_id and (
        current.deployment_id if current else None
    ) == body.deployment_id:
        raise HTTPException(
            status_code=409,
            detail="该模型及部署已是当前发布，无需重复发布"
            if body.model_version_id
            else "项目模型发布已停用，无需重复停用",
        )
    version = deployment = None
    if body.model_version_id:
        version = await load_version(db, project.id, body.model_version_id)
        if model_task(version) != body.task:
            raise HTTPException(status_code=422, detail="模型与发布任务不一致")
        validate_bindings(
            project, version.classes, version.bindings, model_task(version)
        )
        deployment = await db.get(ModelDeployment, body.deployment_id)
        if (
            not deployment
            or deployment.model_version_id != version.id
            or deployment.status != "ready"
        ):
            raise HTTPException(
                status_code=409, detail="模型尚未完成加载检查与样本试用"
            )
        backend = await enabled_backend(db, project.id, deployment.registry_id)
        await assert_routable_backend(db, project.id, deployment.registry_id)
        live = await MLBackendClient(backend).trained_model_receipt(str(version.id))
        check_receipt(version, live)
    if body.rollback_from_id:
        previous = await db.get(ModelRelease, body.rollback_from_id)
        if (
            not previous
            or previous.project_id != project.id
            or previous.task != body.task
            or previous.model_version_id != body.model_version_id
            or previous.deployment_id != body.deployment_id
        ):
            raise HTTPException(status_code=422, detail="回退目标与历史发布不一致")
    release = ModelRelease(
        project_id=project.id,
        task=body.task,
        creator_id=user.id,
        request_id=body.request_id,
        request_digest=digest,
        model_version_id=body.model_version_id,
        deployment_id=body.deployment_id,
        previous_release_id=selection.release_id,
        rollback_from_id=body.rollback_from_id,
        generation=selection.generation + 1,
        snapshot={},
    )
    db.add(release)
    await db.flush()
    if version:
        release.snapshot = frozen_reference(version, deployment, release)
    selection.release_id = release.id
    selection.generation = release.generation
    return release


def now():
    return datetime.now(timezone.utc)
