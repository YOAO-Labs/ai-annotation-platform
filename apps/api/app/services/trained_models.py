"""Controlled artifacts and immutable prediction identities."""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import NoReturn

from aap_task_contract import normalize_task_fields
from training_contract.dataset import config_task

from fastapi import HTTPException
from sqlalchemy import select
from training_contract.model_return import registration_eligibility

from app.services.project_access import get_workspace_project
from app.db.models.trained_model import (
    ModelDeployment,
    ModelRelease,
    ProjectModelSelection,
    TrainedModelVersion,
)
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.services.ml_backend import MLBackendService
from app.services.storage import storage_service as storage
from app.services.training_dataset_bridge import content_digest
from app.services.training_runs import internal_download
from app.services import training_artifacts as artifact_service
from app.services.training_evaluations import parse_evaluation
from app.services.trained_model_reference_contract import (
    FreezeModelReferenceError,
    FreezeModelReferencePort,
    FreezeModelReferenceRequest,
    FrozenModelReference,
    FrozenModelReferenceFacts,
    ValidateFrozenModelReferenceRequest,
    build_frozen_model_reference,
)


def project_classes(project):
    binding = (project.tool_bindings or {}).get("bbox", {})
    values = binding.get("classes", []) if binding.get("enabled") else []
    return [v["name"] if isinstance(v, dict) else v for v in values]


def validate_bindings(project, classes, bindings):
    validate_project_class_bindings(project_classes(project), classes, bindings)


def validate_project_class_bindings(allowed, classes, bindings):
    if (
        not classes
        or len(bindings) != len(classes)
        or any(b not in allowed for b in bindings)
    ):
        raise HTTPException(
            status_code=422, detail="须为每个原生类别绑定当前项目的矩形框类别"
        )


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
    classes = run.config["classes"]
    validate_bindings(project, classes, body.bindings)
    try:
        await asyncio.to_thread(
            artifact_service.verify_artifact, weights["key"], weights, attempt.id
        )
        evaluation = await asyncio.to_thread(
            artifact_service.read_json_artifact, metrics, attempt.id
        )
        if run.config["framework"] == "libreyolo":
            checked = parse_evaluation(
                evaluation, run, attempt, metrics, weights, 0, len(classes)
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


async def begin_deployment_check(db, version, registry_id, deployment=None):
    if deployment is None:
        deployment = ModelDeployment(
            model_version_id=version.id, registry_id=registry_id
        )
        db.add(deployment)
    check_id = uuid.uuid4()
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
        "task": "detection",
    }
    if version.framework == "libreyolo":
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


async def enabled_backend(db, project_id, registry_id):
    topology = MLBackendService(db)
    backend = await topology.get_registry(registry_id)
    if not backend or not await topology.is_enabled(project_id, registry_id):
        raise HTTPException(status_code=404, detail="推理实例未在当前项目启用")
    from app.services import inference_backend_control

    credential = await inference_backend_control.backend_control_service(
        db
    ).credential_policy(registry_id)
    if (
        credential is None
        or credential.auth_method != "token"
        or not credential.auth_token_length_valid
    ):
        raise HTTPException(status_code=422, detail="受控模型需要配置已认证的推理实例")
    return backend


async def assert_routable_backend(db, project_id, registry_id):
    from app.services.ml_routing.contracts import RouterMode
    from app.services.ml_routing.router import MLBackendRouter, _router_mode

    topology = MLBackendService(db)
    pool_id = await topology.pool_id_for_registry(registry_id)
    pool = await topology.get_pool(pool_id) if pool_id else None
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


def validate_and_map_results(results, ref: FrozenModelReference):
    from aap_inference_contract.execution import ModelTaskResult

    identity = {k: ref[k] for k in ("model_version_id", "sha256", "classes_digest")}
    mapped_results = []
    for result in results:
        if not isinstance(result, ModelTaskResult):
            raise TypeError("Controlled model results must be immutable task results")
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
            if value.get("rectanglelabels") not in (
                [ref["classes"][index]],
                (ref["classes"][index],),
            ):
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


def download_url(key):
    return storage.rewrite_host_for_ml_backend(internal_download(key))


def _frozen_model_reference(version, deployment, release=None) -> FrozenModelReference:
    receipt = expected_receipt(version)
    return build_frozen_model_reference(
        FrozenModelReferenceFacts(
            project_id=version.project_id,
            registry_id=deployment.registry_id,
            deployment_id=deployment.id,
            binding_digest=version.binding_digest,
            bindings=version.bindings,
            provenance=version.provenance,
            release_id=release.id if release else None,
            release_generation=release.generation if release else None,
        ),
        receipt,
    )


def wire_context(ref: FrozenModelReference, params=None):
    return {
        "task": "detect",
        "model_id": f"trained:{ref['model_version_id']}",
        "model_variants": {},
        "params": params or {},
        "frozen_model_ref": {
            k: ref[k] for k in ("model_version_id", "sha256", "classes_digest")
        },
    }


def is_controlled(model_id):
    return bool(
        model_id and (model_id.startswith("trained:") or model_id == "project-release")
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
        or prompt
        or semantic.get("task") not in {None, "detect"}
        or semantic.get("prompt_type")
        or model_variants
        or len(pipeline_stages or []) > 1
        or execution_unit not in {None, "image", "task"}
    ):
        raise HTTPException(
            status_code=422, detail="受控模型当前仅支持单阶段图片矩形框检测"
        )
    for stage in pipeline_stages or []:
        stage = stage.model_dump() if hasattr(stage, "model_dump") else stage
        source = stage.get("source") or {}
        if (
            stage.get("parent_stage") is not None
            or (stage.get("write") or {}).get("target") not in {None, "geometry"}
            or (stage.get("input") or {}).get("mode") not in {None, "full_image"}
            or stage.get("roi")
            or stage.get("parent_class_filter")
            or source.get("kind") not in {None, "dataset"}
            or source.get("data_type") not in {None, "image"}
        ):
            raise HTTPException(
                status_code=422, detail="受控模型只支持整图输入与矩形框输出"
            )


def _reference_http_error(code: str, exc: HTTPException) -> FreezeModelReferenceError:
    return FreezeModelReferenceError(
        code,
        str(exc.detail),
        status_code=exc.status_code,
        detail=exc.detail,
        headers=exc.headers,
    )


def raise_frozen_model_reference_error(exc: FreezeModelReferenceError) -> NoReturn:
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
            model_id.startswith("trained:") or model_id == "project-release"
        ):
            return None
        release = None
        if model_id == "project-release":
            selection = await self.db.get(ProjectModelSelection, request.project_id)
            release = (
                await self.db.get(ModelRelease, selection.release_id)
                if selection and selection.release_id
                else None
            )
            if not release or not release.model_version_id:
                raise FreezeModelReferenceError(
                    "release_not_selected",
                    "项目尚未启用受控模型发布",
                    status_code=409,
                    detail="项目尚未启用受控模型发布",
                )
            version_id = release.model_version_id
        else:
            try:
                version_id = uuid.UUID(model_id.removeprefix("trained:"))
            except ValueError as exc:
                detail = "模型版本标识无效"
                raise FreezeModelReferenceError(
                    "model_version_id_invalid",
                    detail,
                    status_code=422,
                    detail=detail,
                ) from exc
        try:
            version = await load_version(self.db, request.project_id, version_id)
        except HTTPException as exc:
            raise _reference_http_error("model_version_unavailable", exc) from exc
        try:
            validate_project_class_bindings(
                request.project_classes, version.classes, version.bindings
            )
        except HTTPException as exc:
            raise _reference_http_error("model_bindings_invalid", exc) from exc
        deployment = await self.db.scalar(
            select(ModelDeployment).where(
                ModelDeployment.model_version_id == version.id,
                ModelDeployment.registry_id == request.registry_id,
                ModelDeployment.status == "ready",
            )
        )
        if not deployment or (release and release.deployment_id != deployment.id):
            detail = "所选实例没有该模型版本的就绪部署"
            raise FreezeModelReferenceError(
                "deployment_not_ready",
                detail,
                status_code=409,
                detail=detail,
            )
        try:
            check_receipt(version, deployment.receipt)
        except HTTPException as exc:
            raise _reference_http_error("deployment_receipt_mismatch", exc) from exc
        try:
            await enabled_backend(self.db, request.project_id, request.registry_id)
        except HTTPException as exc:
            raise _reference_http_error("backend_unavailable", exc) from exc
        try:
            await assert_routable_backend(
                self.db, request.project_id, request.registry_id
            )
        except HTTPException as exc:
            raise _reference_http_error("deployment_not_routable", exc) from exc
        try:
            return _frozen_model_reference(version, deployment, release)
        except HTTPException as exc:
            raise _reference_http_error("model_receipt_invalid", exc) from exc

    async def validate(self, request: ValidateFrozenModelReferenceRequest) -> uuid.UUID:

        project_id, ref = request.project_id, request.reference
        project = await get_workspace_project(self.db, project_id=project_id)
        try:
            assert_image_scope(project)
        except HTTPException as exc:
            raise _reference_http_error("project_scope_invalid", exc) from exc
        if not isinstance(ref, dict) or ref.get("project_id") != str(project_id):
            message = "Controlled model project identity mismatch"
            raise FreezeModelReferenceError("project_identity_mismatch", message)
        payload = {
            key: value for key, value in ref.items() if key != "reference_digest"
        }
        if content_digest(payload) != ref.get("reference_digest"):
            message = "Controlled model reference digest mismatch"
            raise FreezeModelReferenceError("reference_digest_mismatch", message)
        try:
            version_id = uuid.UUID(ref["model_version_id"])
        except ValueError as exc:
            raise FreezeModelReferenceError(
                "model_version_id_invalid", str(exc)
            ) from exc
        try:
            version = await load_version(self.db, project_id, version_id)
        except HTTPException as exc:
            raise _reference_http_error("model_version_unavailable", exc) from exc
        try:
            validate_bindings(project, version.classes, version.bindings)
        except HTTPException as exc:
            raise _reference_http_error("model_bindings_invalid", exc) from exc
        try:
            deployment_id = uuid.UUID(ref["deployment_id"])
        except ValueError as exc:
            raise FreezeModelReferenceError("deployment_id_invalid", str(exc)) from exc
        deployment = await self.db.get(ModelDeployment, deployment_id)
        if (
            not deployment
            or deployment.model_version_id != version.id
            or deployment.status != "ready"
        ):
            message = "Controlled deployment is unavailable"
            raise FreezeModelReferenceError("deployment_unavailable", message)
        try:
            release_id = uuid.UUID(ref["release_id"]) if ref.get("release_id") else None
        except ValueError as exc:
            raise FreezeModelReferenceError("release_id_invalid", str(exc)) from exc
        release = (
            await self.db.get(ModelRelease, release_id)
            if release_id is not None
            else None
        )
        try:
            current = _frozen_model_reference(version, deployment, release)
        except HTTPException as exc:
            raise _reference_http_error("model_receipt_invalid", exc) from exc
        if current != ref:
            message = "Controlled model reference changed"
            raise FreezeModelReferenceError("reference_changed", message)
        try:
            await enabled_backend(self.db, project_id, deployment.registry_id)
        except HTTPException as exc:
            raise _reference_http_error("backend_unavailable", exc) from exc
        return deployment.registry_id


def bind_frozen_model_reference(db) -> FreezeModelReferencePort:
    """Bind the typed reference operations to the caller-owned SQL transaction."""
    return _SqlFreezeModelReferencePort(db)


async def publish(db, project, user, body):
    digest = content_digest(body.model_dump(mode="json"))
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
        )
        .with_for_update()
    )
    if not selection:
        selection = ProjectModelSelection(project_id=project.id, generation=0)
        db.add(selection)
        await db.flush()
    if selection.generation != body.expected_generation:
        raise HTTPException(status_code=409, detail="发布选择已变化，请刷新后重试")
    version = deployment = None
    if body.model_version_id:
        version = await load_version(db, project.id, body.model_version_id)
        validate_bindings(project, version.classes, version.bindings)
        deployment = await db.get(ModelDeployment, body.deployment_id)
        if (
            not deployment
            or deployment.model_version_id != version.id
            or deployment.status != "ready"
        ):
            raise HTTPException(
                status_code=409, detail="模型尚未完成加载检查与样本试用"
            )
        await enabled_backend(db, project.id, deployment.registry_id)
        await assert_routable_backend(db, project.id, deployment.registry_id)
        from app.services import inference_backend_control

        try:
            live = await inference_backend_control.backend_control_service(
                db
            ).trained_model_receipt(deployment.registry_id, str(version.id))
        except inference_backend_control.BackendControlOperationError as exc:
            raise backend_control_http_exception(exc) from exc
        check_receipt(version, live)
    if body.rollback_from_id:
        previous = await db.get(ModelRelease, body.rollback_from_id)
        if (
            not previous
            or previous.project_id != project.id
            or previous.model_version_id != body.model_version_id
            or previous.deployment_id != body.deployment_id
        ):
            raise HTTPException(status_code=422, detail="回退目标与历史发布不一致")
    release = ModelRelease(
        project_id=project.id,
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
        release.snapshot = _frozen_model_reference(version, deployment, release)
    selection.release_id = release.id
    selection.generation = release.generation
    return release


def now():
    return datetime.now(timezone.utc)
