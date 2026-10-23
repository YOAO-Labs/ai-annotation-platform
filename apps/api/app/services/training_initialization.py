"""Resolve immutable, same-project source weights independently of serving."""

import asyncio
import uuid

from fastapi import HTTPException
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from training_contract.initialization import (
    policy_for_model,
    validate_pin,
)
from training_contract.models import model_spec
from aap_task_contract import normalize_task
from training_contract.dataset import (
    SEGMENTATION,
    config_task,
    data_contract,
    data_task,
)

from app.db.models.trained_model import TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.schemas.training_initialization import TrainingInitialization
from app.services.storage import storage_service as storage
from app.services.training_dataset_bridge import content_digest


def reference(pin):
    return (
        {
            "kind": "trained_model",
            "model_version_id": pin["model_version_id"],
            "source_digest": pin["source_digest"],
        }
        if pin
        else None
    )


def model_id(pin):
    return uuid.UUID(pin["model_version_id"]) if pin else None


def compatible_pin(pin, framework, base_model, classes=None, *, framework_version=None):
    validate_pin(pin)
    reasons = []
    expected_task = model_spec(framework, base_model).task
    if normalize_task(pin["task"]) != expected_task:
        reasons.append("初始化来源与目标训练任务不同")
    if framework != pin["framework"]:
        reasons.append("来源训练器与当前训练器不同")
    if framework_version is not None and framework_version != pin["framework_version"]:
        reasons.append("来源模型与目标训练器版本不同，尚未通过跨版本初始化资格检查")
    if base_model != pin["base_model"]:
        reasons.append("须使用来源模型相同的家族及规格，不能重映射模型结构")
    if classes is not None and classes != pin["classes"]:
        reasons.append("训练数据的原生类别及顺序须与来源模型完全一致")
    return reasons


async def source_pin(db, project_id, version_id):
    version = await db.scalar(
        select(TrainedModelVersion)
        .where(
            TrainedModelVersion.id == version_id,
            TrainedModelVersion.project_id == project_id,
        )
        .with_for_update(read=True, key_share=True)
    )
    if version is None:
        raise HTTPException(status_code=404, detail="初始化模型不存在或不属于当前项目")
    run = await db.scalar(
        select(TrainingRun).where(
            TrainingRun.id == version.run_id, TrainingRun.project_id == project_id
        )
    )
    attempt = await db.get(TrainingAttempt, version.attempt_id)
    dataset = (
        await db.scalar(
            select(TrainingDatasetVersion).where(
                TrainingDatasetVersion.id == run.dataset_id,
                TrainingDatasetVersion.project_id == project_id,
            )
        )
        if run
        else None
    )
    if (
        not run
        or not attempt
        or attempt.run_id != run.id
        or not dataset
        or attempt.status != "succeeded"
    ):
        raise ValueError("初始化模型缺少同项目的成功执行与冻结数据来源")
    config, provenance = run.config, version.provenance
    segmentation = config_task(config) == SEGMENTATION
    policy = policy_for_model(config["base_model"]["id"], version.framework_version)
    source_contract = data_contract(dataset.manifest) if segmentation else None
    if segmentation and (
        data_task(source_contract) != SEGMENTATION
        or config.get("data_contract") != source_contract
        or model_spec(version.framework, config["base_model"]["id"]).task
        != SEGMENTATION
    ):
        raise ValueError("分割初始化缺少一致的来源任务、架构和数据桥接")
    if (
        version.framework != policy["framework"]
        or version.framework_version != policy["framework_version"]
        or config.get("framework") != version.framework
        or config.get("framework_version") != version.framework_version
        or content_digest(config) != run.config_digest
        or str(dataset.id) != config.get("dataset_id")
        or version.classes != config.get("classes")
        or version.classes != dataset.manifest.get("classes")
        or any(
            provenance.get(key) != value
            for key, value in {
                "run_id": str(run.id),
                "attempt_id": str(attempt.id),
                "dataset_id": str(dataset.id),
                "dataset_digest": config.get("dataset_digest"),
                "validation_digest": config.get("validation_digest"),
                "config_digest": run.config_digest,
                "base_model": config.get("base_model"),
                "runtime": attempt.runtime,
            }.items()
        )
    ):
        raise ValueError("初始化模型的训练、类别或冻结来源身份不完整")
    weights = [
        value for value in attempt.artifacts.values() if value.get("role") == "weights"
    ]
    metrics = [
        value for value in attempt.artifacts.values() if value.get("role") == "metrics"
    ]
    if len(weights) != 1 or len(metrics) != 1 or weights[0] != version.artifact:
        raise ValueError("初始化模型不再对应来源执行的唯一已验证权重")
    from app.services.trained_models import owner_for_attempt

    owner = owner_for_attempt(run, attempt, weights[0], metrics[0])
    artifact = {
        key: version.artifact[key] for key in ("key", "role", "sha256", "size_bytes")
    }
    pin = {
        "kind": "trained_model",
        "policy_digest": content_digest(policy),
        "project_id": str(project_id),
        "model_version_id": str(version.id),
        "model_name": version.name,
        "run_id": str(run.id),
        "attempt_id": str(attempt.id),
        "dataset_id": str(dataset.id),
        "framework": version.framework,
        "framework_version": version.framework_version,
        "task": policy["task"],
        "model_family": policy["model_family"],
        "base_model": config["base_model"]["id"],
        "artifact": artifact,
        "classes": version.classes,
        "classes_digest": content_digest(version.classes),
        "bindings": version.bindings,
        "binding_digest": version.binding_digest,
        "config_digest": run.config_digest,
        "dataset_digest": config["dataset_digest"],
        "validation_digest": config["validation_digest"],
        "runtime": {
            key: attempt.runtime.get(key)
            for key in ("python", "torch", "torchvision", "cuda", "image_digest")
        },
        "evaluation": {
            "report_sha256": metrics[0]["sha256"],
            "report_digest": content_digest(version.evaluation),
            "validation_digest": config["validation_digest"],
        },
    }
    if segmentation:
        from app.services.training_artifacts import read_json_artifact
        from app.services.training_evaluations import parse_evaluation

        try:
            report = await asyncio.to_thread(
                read_json_artifact, metrics[0], attempt.id, bucket=owner.bucket
            )
        except (BotoCoreError, ClientError, OSError) as exc:
            raise ValueError("分割初始化的真实来源评估对象不可用") from exc
        checked = parse_evaluation(
            report, run, attempt, metrics[0], weights[0], 0, len(version.classes)
        )
        registered = normalized_registration_evaluation(report, checked)
        if checked["identity_status"] != "verified" or any(
            version.evaluation.get(key) != registered[key]
            for key in (
                "summary",
                "per_class_available",
                "comparison_identity_status",
                "comparison_compatibility_digest",
                "per_class",
            )
        ):
            raise ValueError("分割初始化模型的注册评估与真实来源报告语义不一致")
        pin["data_contract"] = source_contract
        pin["evaluation"] = {
            "report_sha256": metrics[0]["sha256"],
            "report_digest": content_digest(report),
            "normalized_model_digest": content_digest(version.evaluation),
            "descriptor_digest": content_digest(report["comparison"]),
            "weights_sha256": weights[0]["sha256"],
            "validation_digest": config["validation_digest"],
        }
    pin["source_digest"] = content_digest(pin)
    return validate_pin(pin), metrics[0]


async def source_storage_owner(db, pin):
    """Resolve the source model's retained owner independently of target output."""
    from app.services.trained_models import owner_for_model

    try:
        version = await db.get(TrainedModelVersion, uuid.UUID(pin["model_version_id"]))
        if (
            version is None
            or any(
                str(actual) != pin[key]
                for key, actual in (
                    ("project_id", version.project_id),
                    ("run_id", version.run_id),
                    ("attempt_id", version.attempt_id),
                )
            )
            or {
                key: version.artifact.get(key)
                for key in ("key", "role", "sha256", "size_bytes")
            }
            != pin["artifact"]
        ):
            raise ValueError("Initialization source does not match its retained model")
        return await owner_for_model(db, version)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(
            status_code=409, detail="初始化来源与固定模型不一致"
        ) from None


def verify_storage(pin, metrics, *, owner, full):
    from app.services.training_runs import verify_artifact
    from app.services.training_artifacts import read_json_artifact

    from app.services.training_run_storage import expect_attempt

    artifact = pin["artifact"]
    expect_attempt(owner, pin["attempt_id"], artifact["key"])
    expect_attempt(owner, pin["attempt_id"], metrics["key"])
    if full:
        verify_artifact(
            artifact["key"], artifact, pin["attempt_id"], bucket=owner.bucket
        )
        report = read_json_artifact(metrics, pin["attempt_id"], bucket=owner.bucket)
        if content_digest(report) != pin["evaluation"]["report_digest"]:
            raise ValueError("来源评估与候选注册时的报告不一致")
        if pin["task"] == SEGMENTATION and (
            content_digest(report.get("comparison"))
            != pin["evaluation"]["descriptor_digest"]
            or report.get("validation_digest") != pin["validation_digest"]
            or report.get("comparison", {}).get("candidate", {}).get("weights_sha256")
            != artifact["sha256"]
            or metrics["sha256"] != pin["evaluation"]["report_sha256"]
        ):
            raise ValueError("分割初始化真实报告的权重或评估器身份不一致")
    else:
        head = storage.client.head_object(Bucket=owner.bucket, Key=artifact["key"])
        if (
            head["ContentLength"] != artifact["size_bytes"]
            or head.get("Metadata", {}).get("attempt-id") != pin["attempt_id"]
        ):
            raise ValueError("来源对象大小或执行身份不符")


async def eligibility(
    db, project_id, version_id, framework, base_model, dataset_id=None
):
    # Resolve the same-project source before returning target availability details.
    try:
        pin, metrics = await source_pin(db, project_id, version_id)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return {
            "model_version_id": version_id,
            "eligible": False,
            "reasons": [str(exc)[:300]],
            "pin": None,
        }
    classes = None
    if dataset_id:
        dataset = await db.scalar(
            select(TrainingDatasetVersion).where(
                TrainingDatasetVersion.id == dataset_id,
                TrainingDatasetVersion.project_id == project_id,
            )
        )
        if dataset is None:
            raise HTTPException(status_code=404, detail="训练数据版本不存在")
        if dataset.status != "ready":
            return {
                "model_version_id": version_id,
                "eligible": False,
                "reasons": ["训练数据版本尚未就绪"],
                "pin": None,
            }
        classes = dataset.manifest.get("classes")
        if not isinstance(classes, list) or not classes:
            return {
                "model_version_id": version_id,
                "eligible": False,
                "reasons": ["训练数据版本缺少原生类别身份"],
                "pin": None,
            }
    try:
        from app.services.training_runs import controlled_template

        target = controlled_template(framework, base_model)
        reasons = compatible_pin(
            pin,
            framework,
            base_model,
            classes,
            framework_version=target["framework_version"],
        )
    except HTTPException as exc:
        return {
            "model_version_id": version_id,
            "eligible": False,
            "reasons": [str(exc.detail)[:300]],
            "pin": None,
        }
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return {
            "model_version_id": version_id,
            "eligible": False,
            "reasons": [str(exc)[:300]],
            "pin": None,
        }
    try:
        owner = await source_storage_owner(db, pin)
        await asyncio.to_thread(verify_storage, pin, metrics, owner=owner, full=False)
    except Exception:
        reasons.append("来源权重对象缺失或身份不符；不会使用其他权重替代")
    return {
        "model_version_id": version_id,
        "eligible": not reasons,
        "reasons": reasons,
        "pin": pin,
        "storage_verified": False,
    }


async def resolve(
    db,
    project_id,
    source,
    framework,
    base_model,
    classes=None,
    *,
    full=True,
    framework_version=None,
):
    if source is None or source.kind == "catalog":
        return None
    try:
        pin, metrics = await source_pin(db, project_id, source.model_version_id)
        if framework_version is None:
            from app.services.training_runs import controlled_template

            framework_version = controlled_template(framework, base_model)[
                "framework_version"
            ]
        reasons = compatible_pin(
            pin, framework, base_model, classes, framework_version=framework_version
        )
        if source.source_digest != pin["source_digest"]:
            reasons.append("初始化来源身份已变化，请明确重新选择并预检")
        if reasons:
            raise ValueError("；".join(reasons))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:500]) from exc
    try:
        owner = await source_storage_owner(db, pin)
        await asyncio.to_thread(verify_storage, pin, metrics, owner=owner, full=full)
    except Exception as exc:
        raise HTTPException(
            status_code=409, detail="初始化权重或评估对象校验失败，请检查保留的来源产物"
        ) from exc
    return pin


async def validate_accepted(
    db, project_id, pin, framework, base_model, classes=None, *, full=True
):
    try:
        validate_pin(pin)
        if pin["project_id"] != str(project_id):
            raise ValueError("初始化来源不属于当前项目")
        current = await resolve(
            db,
            project_id,
            TrainingInitialization(**reference(pin)),
            framework,
            base_model,
            classes,
            full=full,
            framework_version=pin["framework_version"],
        )
        if current != pin:
            raise ValueError("初始化来源与已接受的固定身份不符")
        return current
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)[:500]) from exc


def normalized_registration_evaluation(report, checked):
    """Compare registration semantics without conflating raw artifact bytes."""
    return {
        **report,
        "summary": checked["summary"],
        "per_class_available": checked["per_class_available"],
        "comparison_identity_status": checked["identity_status"],
        "comparison_compatibility_digest": checked["compatibility_digest"],
        "per_class": [
            {"index": row["index"], "name": row["name"], **row["metrics"]}
            for row in checked["per_class"]
        ],
    }
