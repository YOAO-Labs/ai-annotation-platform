"""Controlled artifacts and immutable prediction identities."""

from __future__ import annotations

from app.services import training_storage_io as storage_io

from aap_protocol_v2.backend_auth import managed_provider

from aap_task_contract import normalize_task, normalize_task_fields
from training_contract.dataset import config_task
from training_contract.inference_runtime import validate_inference_runtime

import math
import uuid
from collections.abc import Mapping
from dataclasses import replace

from aap_inference_contract.execution import ModelTaskResult, thaw_json
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
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.services.ml_backend import MLBackendService
from app.services.storage import storage_service as storage
from app.services import training_dataset_storage
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest
from app.services import training_run_storage
from app.services.training_runs import verify_artifact
from app.services.training_artifacts import read_json_artifact
from app.services.training_evaluations import parse_evaluation
from app.services.project_access import get_workspace_project
from app.services.trained_model_reference_contract import (
    FreezeModelReferenceError,
    FreezeModelReferencePort,
    FreezeModelReferenceRequest,
    FrozenModelReference,
    RegistryLearningUsage,
    ValidateFrozenModelReferenceRequest,
)


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


def validate_project_class_bindings(allowed, classes, bindings):
    if (
        not classes
        or len(bindings) != len(classes)
        or any(b not in allowed for b in bindings)
    ):
        raise HTTPException(
            status_code=422, detail="须为每个原生类别绑定当前项目的矩形框类别"
        )


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
        await storage_io.read(
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
            object_key=weights["key"],
            sha256=weights["sha256"],
            size_bytes=weights["size_bytes"],
        )
        evaluation = await storage_io.read(
            db,
            owner,
            "training_model_read",
            lambda transport: read_json_artifact(
                metrics, attempt.id, bucket=owner.bucket, transport=transport
            ),
            object_key=metrics["key"],
            sha256=metrics["sha256"],
            size_bytes=metrics["size_bytes"],
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


async def lock_deployment(db, version_id, registry_id):
    return await db.scalar(
        select(ModelDeployment)
        .where(
            ModelDeployment.model_version_id == version_id,
            ModelDeployment.registry_id == registry_id,
        )
        .with_for_update()
    )


async def registry_usage(db, registry_id: uuid.UUID) -> RegistryLearningUsage:
    """Lock Learning deployments and report live release and frozen-work facts.

    The caller must first hold Inference's registry lock in this transaction.
    """
    from app.services.jobs_deployment_usage import frozen_deployment_usage

    deployments = (await db.scalars(
        select(ModelDeployment)
        .where(ModelDeployment.registry_id == registry_id)
        .order_by(ModelDeployment.id)
        .with_for_update()
    )).all()
    ids = [deployment.id for deployment in deployments]
    if not ids:
        return RegistryLearningUsage(active_releases=0, frozen_executions=0)
    active_releases = await db.scalar(
        select(func.count())
        .select_from(ProjectModelSelection)
        .join(ModelRelease, ModelRelease.id == ProjectModelSelection.release_id)
        .where(ModelRelease.deployment_id.in_(ids))
    )
    frozen = 0
    for deployment in deployments:
        usage = await frozen_deployment_usage(
            db, deployment.id, deployment.registry_id, deployment.model_version_id
        )
        frozen += usage.active_jobs + usage.retryable_failures
    return RegistryLearningUsage(active_releases=int(active_releases), frozen_executions=frozen)


async def retire_deployment(db, project_id, version_id, deployment_id, *, terminal):
    """Stop new references first; terminal retirement only after dependencies drain."""
    from app.services.inference_topology_data import InferenceTopologyData
    from app.services.jobs_deployment_usage import frozen_deployment_usage

    version = await load_version(db, project_id, version_id)
    if version.framework != "libreyolo":
        raise HTTPException(status_code=422, detail="此操作仅适用于 LibreYOLO 部署")
    candidate = await db.get(ModelDeployment, deployment_id)
    if candidate is None or candidate.model_version_id != version.id:
        raise HTTPException(status_code=404, detail="模型部署不存在")
    if await InferenceTopologyData(db).lock_plugin_registry(candidate.registry_id) is None:
        raise HTTPException(status_code=404, detail="推理实例不存在")
    deployment = await db.scalar(
        select(ModelDeployment).where(ModelDeployment.id == deployment_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if deployment is None or deployment.model_version_id != version.id:
        raise HTTPException(status_code=404, detail="模型部署不存在")
    if terminal:
        if deployment.status == "retired":
            return deployment
        if deployment.status != "draining":
            raise HTTPException(status_code=409, detail="须先停止创建新引用")
        release_count = await db.scalar(
            select(func.count()).select_from(ProjectModelSelection)
            .join(ModelRelease, ModelRelease.id == ProjectModelSelection.release_id)
            .where(ModelRelease.deployment_id == deployment.id)
        )
        usage = await frozen_deployment_usage(
            db, deployment.id, deployment.registry_id, deployment.model_version_id
        )
        if release_count or usage.active_jobs or usage.retryable_failures:
            raise HTTPException(status_code=409, detail={
                "code": "deployment_in_use",
                "active_releases": int(release_count),
                "active_jobs": usage.active_jobs,
                "retryable_failures": usage.retryable_failures,
            })
        deployment.status = "retired"
    elif deployment.status == "ready":
        deployment.status = "draining"
        deployment.check_id = uuid.uuid4()
    elif deployment.status != "draining":
        raise HTTPException(status_code=409, detail="只有就绪部署可停止创建新引用")
    await db.flush()
    return deployment


async def begin_deployment_check(db, version, registry_id, deployment=None):
    if deployment is None:
        deployment = ModelDeployment(
            model_version_id=version.id, registry_id=registry_id
        )
        db.add(deployment)
    check_id = uuid.uuid4()
    if deployment.status in {"draining", "retired"}:
        raise HTTPException(status_code=409, detail="该部署已停止创建新引用")
    deployment.check_id = check_id
    deployment.status = "checking"
    deployment.error_message = None
    await db.flush()
    return deployment, check_id


async def complete_deployment_check(
    db,
    deployment_id,
    check_id,
    *,
    receipt: dict,
    trial: dict,
    error: str | None,
):
    deployment = await db.scalar(
        select(ModelDeployment)
        .where(ModelDeployment.id == deployment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if deployment.check_id != check_id:
        raise HTTPException(status_code=409, detail="该部署已有更新的检查，请刷新")
    deployment.status = "failed" if error else "ready"
    deployment.error_message = error
    if not error:
        deployment.receipt = receipt
        deployment.trial = trial
    deployment.checked_at = now()
    return deployment


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


class PinnedRuntimeConflict(HTTPException):
    def __init__(self, detail: str):
        super().__init__(status_code=409, detail=detail)


def check_trial_runtime(deployment, receipt):
    """Require the live execution profile to be the one used by the trial."""
    pinned = (deployment.trial or {}).get("inference_runtime")
    if pinned != (receipt or {}).get("inference_runtime"):
        raise HTTPException(status_code=409, detail="推理实例运行时与已试用的部署不一致")


def check_install_runtime(version, deployment, receipt):
    runtime = receipt.get("inference_runtime")
    if version.framework == "libreyolo" and runtime is None:
        raise HTTPException(status_code=409, detail="LibreYOLO 部署必须明确声明运行时")
    pinned = (deployment.trial or {}).get("inference_runtime")
    if pinned is not None and runtime != pinned:
        raise PinnedRuntimeConflict("已固定运行时不可原地更改，请使用新部署")
    if deployment.receipt and pinned is None and runtime is not None:
        # A legacy trial without a profile has CPU semantics, not a GPU trial.
        if version.framework == "libreyolo" and runtime.get("qualification", {}).get("device") != "cpu":
            raise PinnedRuntimeConflict("旧 CPU 部署不可原地切换运行时")


def check_declared_compatibility(backend, version, runtime=None):
    """Match approved and observed generic managed-model capabilities."""
    from aap_inference_contract.topology import to_record

    params = to_record(backend.extra_params or {})
    plugin = params.get("plugin")
    if plugin is None:
        return
    if not isinstance(plugin, dict) or not isinstance(plugin.get("manifest"), dict):
        raise HTTPException(status_code=409, detail="插件安装元数据无效")
    manifest = plugin.get("manifest", {})
    approved = manifest.get("managed_model_compatibility", [])
    capabilities = to_record((backend.health_meta or {}).get("capabilities") or {})
    observed = capabilities.get("managed_model_compatibility", [])
    device = (runtime or {}).get("qualification", {}).get("device")
    artifact_format = expected_receipt(version).get("artifact_format")
    if not isinstance(approved, list) or not isinstance(observed, list):
        raise HTTPException(status_code=409, detail="插件未声明受控模型兼容性")
    matches = [
        entry for entry in approved
        if isinstance(entry, dict)
        and entry.get("framework") == version.framework
        and entry.get("format") == artifact_format
        and entry.get("runtime_device") in {"cpu", "cuda"}
        and (device is None or entry["runtime_device"] == device)
        and entry in observed
    ]
    if not matches:
        raise HTTPException(status_code=409, detail="插件未声明兼容的框架、格式与运行时")


def backend_control_http_exception(error):
    """Translate the framework-neutral control failure at the API consumer edge."""
    failure = error.failure
    return HTTPException(
        status_code=failure.status_code or 503,
        detail=(
            failure.detail if failure.detail is not None else {"code": failure.code}
        ),
        headers={"Retry-After": failure.retry_after} if failure.retry_after else None,
    )


def validate_result_runtime(meta, ref):
    if "inference_runtime" not in ref and "inference_runtime" in (meta or {}):
        actual = validate_inference_runtime(ref, meta["inference_runtime"])
        if actual["version"] != ref.get("framework_version") or (
            ref.get("framework") == "libreyolo"
            and actual.get("qualification", {}).get("device") != "cpu"
        ):
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
    from app.services.inference_topology_data import InferenceTopologyData
    from aap_inference_contract.plugin import plugin_is_active

    facts = await InferenceTopologyData(db).lock_plugin_registry(registry_id)
    if facts is None:
        raise HTTPException(status_code=404, detail="推理实例不存在")
    if not plugin_is_active(facts.extra_params):
        raise HTTPException(status_code=409, detail="推理插件安装未启用")
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
    ref = thaw_json(ref)
    for result in results:
        if not isinstance(result, ModelTaskResult):
            raise TypeError("Controlled model results must be immutable task results")
        validate_result_runtime(thaw_json(result.meta), ref)
    if normalize_task(ref.get("task", "detection")) == "segment":
        from app.services.controlled_segmentation import validate_results

        return validate_results(results, ref)
    identity = {k: ref[k] for k in ("model_version_id", "sha256", "classes_digest")}
    mapped_results = []
    for result in results:
        if (result.meta or {}).get("trained_model") != identity:
            raise ValueError("Backend returned a different controlled model identity")
        mapped = []
        for item in result.result:
            if not isinstance(item, Mapping):
                raise ValueError("Controlled detection returned malformed geometry")
            index = item.get("native_class_index")
            if type(index) is not int or not 0 <= index < len(ref["classes"]):
                raise ValueError("Backend returned an unknown native class index")
            if item.get("type") != "rectanglelabels":
                raise ValueError("Controlled detection returned unsupported geometry")
            value = item.get("value", {})
            if not isinstance(value, Mapping):
                raise ValueError("Controlled detection returned malformed geometry")
            if value.get("rectanglelabels") != (ref["classes"][index],):
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
        mapped_results.append(
            replace(
                result,
                result=tuple(mapped),
                model_version=f"trained:{ref['model_version_id']}",
            )
        )
    return tuple(mapped_results)


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
    backend = await enabled_backend(db, project.id, registry_id)
    deployment = await db.scalar(
        select(ModelDeployment).where(
            ModelDeployment.model_version_id == version.id,
            ModelDeployment.registry_id == registry_id,
            ModelDeployment.status == "ready",
        ).with_for_update()
    )
    if not deployment or (release and release.deployment_id != deployment.id):
        raise HTTPException(status_code=409, detail="所选实例没有该模型版本的就绪部署")
    check_receipt(version, deployment.receipt)
    check_declared_compatibility(
        backend, version, (deployment.trial or {}).get("inference_runtime")
    )
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
    registry_id = uuid.UUID(ref["registry_id"])
    await enabled_backend(db, project_id, registry_id)
    deployment = await db.scalar(
        select(ModelDeployment).where(ModelDeployment.id == uuid.UUID(ref["deployment_id"]))
        .with_for_update().execution_options(populate_existing=True)
    )
    if (
        not deployment
        or deployment.model_version_id != version.id
        or deployment.registry_id != registry_id
        or deployment.status not in {"ready", "draining"}
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
    return deployment.registry_id


def _reference_http_error(code: str, exc: HTTPException) -> FreezeModelReferenceError:
    return FreezeModelReferenceError(
        code,
        str(exc.detail),
        status_code=exc.status_code,
        detail=exc.detail,
        headers=exc.headers,
    )


def raise_frozen_model_reference_error(exc: FreezeModelReferenceError) -> None:
    """Restore this operation's existing HTTPException/ValueError projection."""
    if exc.status_code is not None:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc
    raise ValueError(str(exc)) from exc


class _SqlFreezeModelReferencePort:
    """Learning-owned SQL adapter bound to the caller's current transaction."""

    def __init__(self, db) -> None:
        self.db = db

    async def freeze(
        self, request: FreezeModelReferenceRequest
    ) -> FrozenModelReference | None:
        model_id = request.model_id
        if not model_id or not (
            model_id.startswith("trained:") or model_id.startswith("project-release")
        ):
            return None
        project = await get_workspace_project(self.db, project_id=request.project_id)
        if project is None:
            raise FreezeModelReferenceError(
                "authority_revoked", "项目已不存在", status_code=403
            )
        try:
            return await resolve_reference(
                self.db,
                project,
                model_id,
                request.registry_id,
                allow_candidate=True,
            )
        except HTTPException as exc:
            raise _reference_http_error("model_reference_unavailable", exc) from exc

    async def validate(self, request: ValidateFrozenModelReferenceRequest) -> uuid.UUID:
        try:
            return await validate_frozen_reference(
                self.db, request.project_id, request.reference
            )
        except HTTPException as exc:
            raise _reference_http_error("model_reference_invalid", exc) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise FreezeModelReferenceError(
                "model_reference_invalid", str(exc)
            ) from exc


def bind_frozen_model_reference(db) -> FreezeModelReferencePort:
    """Bind the typed reference operations to the caller-owned SQL transaction."""
    return _SqlFreezeModelReferencePort(db)


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
        candidate = await db.get(ModelDeployment, body.deployment_id)
        if candidate is None:
            raise HTTPException(status_code=409, detail="模型尚未完成加载检查与样本试用")
        backend = await enabled_backend(db, project.id, candidate.registry_id)
        deployment = await db.scalar(
            select(ModelDeployment).where(ModelDeployment.id == body.deployment_id)
            .with_for_update()
        )
        if (
            not deployment
            or deployment.model_version_id != version.id
            or deployment.status != "ready"
        ):
            raise HTTPException(
                status_code=409, detail="模型尚未完成加载检查与样本试用"
            )
        if deployment.registry_id != candidate.registry_id:
            raise HTTPException(status_code=409, detail="模型部署身份已变化")
        check_declared_compatibility(
            backend, version, (deployment.trial or {}).get("inference_runtime")
        )
        await assert_routable_backend(db, project.id, deployment.registry_id)
        from app.services.trained_model_backend import live_receipt

        live = await live_receipt(db, deployment.registry_id, str(version.id))
        check_receipt(version, live)
        check_trial_runtime(deployment, live)
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
