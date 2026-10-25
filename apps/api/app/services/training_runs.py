"""One durable training state machine, projected into transient AsyncJobs."""

from __future__ import annotations

from aap_task_contract import normalize_task, legacy_training_task
from training_contract.catalog import definition_for_identity, instance_rle_capability
import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from training_contract import definition, resolve, schema_identity, verify_native
from training_contract.validation import CANONICAL, configuration_for_data_contract
from training_contract.dataset import DETECTION, SEGMENTATION, is_instance_rle
from training_contract.models import (
    ULTRALYTICS_VERSION,
    LEGACY_ULTRALYTICS_VERSION,
    model_spec,
    supports_version,
)
from training_contract.observability import failure_receipt, redact_text
from training_contract.assets import AssetReference
from training_contract.input_membership import (
    contract_for as input_contract_for,
    declared_contract as input_contract,
    verify_receipt as verify_input_receipt,
)

from app.config import settings
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import (
    TrainingAttempt,
    TrainingEvent,
    TrainingResourceLease,
    TrainingRun,
)
from app.schemas.training_run import ExecutorFence, TrainingRunCreate
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal
from app.services.storage import storage_service as storage
from app.services import training_dataset_storage
from app.services import training_run_storage
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    contract_of,
)
from app.services.training_datasets import assert_training_authority
from app.services.training_adapters import ADAPTERS, capabilities, fixed_options
from app.services import training_initialization as initialization

ACTIVE = {
    "queued",
    "preparing",
    "training",
    "evaluating",
    "uploading",
    "cancelling",
    "lost",
}
TERMINAL = {"succeeded", "failed", "cancelled"}
SCOPES = frozenset({"datasets:read", "datasets:write", "annotations:read"})


def now():
    return datetime.now(timezone.utc)


def resource_id():
    return f"{settings.training_node_id}/{settings.training_gpu_uuid}"


def catalog():
    configured = settings.training_catalog_json
    enabled = bool(
        settings.training_enabled
        and len(settings.training_executor_token) >= 32
        and settings.training_gpu_uuid.startswith("GPU-")
    )
    items = []
    for framework, entry in configured.items():
        if framework not in ADAPTERS:
            continue
        version = entry.get("version")
        image = entry.get("image_digest", "")
        models = entry.get("base_models", {})
        if (
            version
            not in (
                {ULTRALYTICS_VERSION, LEGACY_ULTRALYTICS_VERSION}
                if framework == "ultralytics"
                else {ADAPTERS[framework]["version"]}
            )
            or not image.startswith("sha256:")
            or len(image) != 71
        ):
            continue
        valid_models = []
        for name, model in models.items():
            try:
                spec = model_spec(framework, name)
                if not supports_version(spec, version):
                    continue
                task = definition(framework, name, version=version)["task"]
            except ValueError:
                continue
            sha = model.get("sha256", "")
            filename = model.get("filename", "")
            try:
                AssetReference.from_mapping(model)
            except (ValueError, KeyError, TypeError):
                continue
            object_key = model.get("object_key")
            size_bytes = model.get("size_bytes")
            if object_key is not None and (
                object_key != f"model-assets/sha256/{sha}/weights.pt"
                or type(size_bytes) is not int
                or not 0 < size_bytes <= 2 * 1024**3
            ):
                continue
            if (
                len(sha) == 64
                and all(c in "0123456789abcdef" for c in sha)
                and filename
                and "/" not in filename
                and "\\" not in filename
                and ".." not in filename
            ):
                valid_models.append(
                    {
                        "id": name,
                        "sha256": sha,
                        "filename": filename,
                        "task": task,
                        "model_family": spec.family,
                        "model_size": spec.scale,
                        **(
                            {"object_key": object_key, "size_bytes": size_bytes}
                            if object_key
                            else {}
                        ),
                        "capabilities": {
                            **capabilities(framework, task, name),
                            **(
                                {"instance_rle": instance_rle_capability(version)}
                                if task == SEGMENTATION
                                else {}
                            ),
                        },
                    }
                )
        if valid_models:
            items.append(
                {
                    "framework": framework,
                    "version": version,
                    "image_digest": image,
                    "base_models": valid_models,
                    "capabilities": {
                        "tasks": sorted({base["task"] for base in valid_models}),
                        **{
                            key: any(base["capabilities"][key] for base in valid_models)
                            for key in (
                                "resume",
                                "artifact_download",
                                "model_registration",
                            )
                        },
                    },
                    "parameter_schemas": parameter_summaries(
                        framework, valid_models, version=version
                    ),
                }
            )
    return {
        "enabled": enabled and bool(items),
        "resource_id": resource_id() if enabled else None,
        "items": items,
    }


def parameter_summaries(framework, models, *, version=None):
    schemas = {}
    for base in models:
        try:
            schema = definition(framework, base["id"], version=version)
        except ValueError:
            continue
        identity = schema_identity(schema)
        identity["field_count"] = len(schema["fields"])
        identity["editable_count"] = sum(
            field["status"] == "editable" for field in schema["fields"]
        )
        schemas[schema["schema_id"]] = identity
    return list(schemas.values())


def annotate_readiness(result, runtime, *, online):
    """Project exact model/node readiness without treating one sibling as ready."""
    for item in result["items"]:
        for base in item["base_models"]:
            schema = next(
                (
                    schema
                    for schema in item["parameter_schemas"]
                    if schema["task"] == base["task"]
                    and schema["model_family"] == base["model_family"]
                ),
                None,
            )
            identity = {
                key: value
                for key, value in (schema or {}).items()
                if key not in {"field_count", "editable_count", "compatible"}
            }
            candidate = {
                "framework": item["framework"],
                "framework_version": item["version"],
                "image_digest": item["image_digest"],
                "base_model": base,
                "adapter_revision": 2,
                "configuration": {"identity": identity},
            }
            ready = (
                bool(
                    schema
                    and runtime.get("gpu_uuid") == settings.training_gpu_uuid
                    and runtime_compatible(candidate, runtime)
                )
                if online
                else None
            )
            base["compatible"] = ready
            base["unavailable_reason"] = (
                "训练节点离线"
                if not online
                else "该节点尚未准备此基础模型权重"
                if requires_prepared_asset(item["framework"], item["version"], base)
                and base["sha256"] not in runtime.get("base_asset_sha256", [])
                else "该节点的镜像、框架或模型参数身份不兼容"
                if not ready
                else None
            )
        for schema in item["parameter_schemas"]:
            schema["compatible"] = (
                any(
                    base["compatible"]
                    for base in item["base_models"]
                    if base["task"] == schema["task"]
                    and base["model_family"] == schema["model_family"]
                )
                if online
                else None
            )
        legacy_ready = (
            online
            and item["version"] != ULTRALYTICS_VERSION
            and runtime.get("gpu_uuid") == settings.training_gpu_uuid
            and any(
                runtime_compatible(
                    {
                        "framework": item["framework"],
                        "framework_version": item["version"],
                        "image_digest": item["image_digest"],
                        "base_model": base,
                        "adapter_revision": 1,
                    },
                    runtime,
                )
                for base in item["base_models"]
                if base["task"] == DETECTION
            )
        )
        item["compatible"] = (
            bool(
                legacy_ready or any(base["compatible"] for base in item["base_models"])
            )
            if online
            else None
        )
    return result


def resolve_configuration(framework, base_model, configuration):
    try:
        return resolve(framework, base_model, configuration)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def controlled_template(framework, base_model):
    options = catalog()
    template = next((x for x in options["items"] if x["framework"] == framework), None)
    base = next(
        (x for x in (template or {}).get("base_models", []) if x["id"] == base_model),
        None,
    )
    if not options["enabled"] or not template or not base:
        raise HTTPException(status_code=409, detail="训练器或基础权重尚未配置")
    return {
        "framework": framework,
        "framework_version": template["version"],
        "image_digest": template["image_digest"],
        "base_model": {
            key: base[key]
            for key in ("id", "sha256", "filename", "object_key", "size_bytes")
            if key in base
        },
        "resource_id": options["resource_id"],
    }


def requires_prepared_asset(framework, version, base):
    return bool(base.get("object_key")) or (
        framework == "ultralytics" and version == ULTRALYTICS_VERSION
    )


def input_runtime_compatible(config, runtime):
    try:
        inputs = input_contract(config)
        return (
            inputs is None
            or runtime.get("input_receipt_policy_digest") == inputs["policy_digest"]
        )
    except (ValueError, KeyError, TypeError):
        return False


def runtime_compatible(config, runtime):
    if not input_runtime_compatible(config, runtime):
        return False
    framework = config["framework"]
    if requires_prepared_asset(
        framework, config["framework_version"], config.get("base_model", {})
    ) and config["base_model"]["sha256"] not in runtime.get("base_asset_sha256", []):
        return False
    if is_instance_rle(config.get("data_contract", {})) and runtime.get(
        "instance_rle_consumer"
    ) != instance_rle_capability(config["framework_version"]):
        return False
    if config.get("study_budget"):
        from training_contract.budget import POLICY_DIGEST

        if (
            config["study_budget"].get("policy_digest") != POLICY_DIGEST
            or runtime.get("native_budget_policy_digest") != POLICY_DIGEST
        ):
            return False
    if config["framework_version"] != runtime.get("frameworks", {}).get(framework):
        return False
    if "initialization" in config:
        from training_contract.initialization import compatible_runtime

        try:
            if not compatible_runtime(config, runtime):
                return False
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
    revision = config.get("adapter_revision", 1)
    protocol = runtime.get("executor_protocol")
    if protocol is None:
        supported = revision == 1 and "configuration" not in config
    elif protocol == 2:
        try:
            schema = definition(
                framework,
                config["base_model"]["id"],
                version=config["framework_version"],
            )
            family = schema["model_family"]
        except (KeyError, ValueError):
            return False
        identity = {
            "framework": framework,
            "framework_version": config["framework_version"],
            "adapter_revision": revision,
            "task": DETECTION,
            "model_family": family,
        }
        if revision == 2:
            identity = config.get("configuration", {}).get("identity")
            try:
                definition_for_identity(framework, config["base_model"]["id"], identity)
            except (ValueError, TypeError):
                return False
        elif schema["task"] != DETECTION:
            return False
        supported = identity in runtime.get("supported_configs", [])
        if revision == 1 and not supported:
            supported = {
                **identity,
                "task": legacy_training_task(identity["task"]),
            } in runtime.get("supported_configs", [])
    else:
        return False
    if not supported:
        return False
    if config["image_digest"] == runtime.get("image_digest"):
        return True
    entry = settings.training_catalog_json.get(framework, {})
    if (
        revision == 2
        and protocol == 2
        and normalize_task(config["configuration"]["identity"]["task"])
        in {DETECTION, SEGMENTATION}
        and entry.get("version") == config["framework_version"]
        and entry.get("image_digest") == runtime.get("image_digest")
        and config.get("configuration", {}).get("identity")
        in entry.get("compatible_config_images", {}).get(config["image_digest"], [])
    ):
        return True
    return bool(
        revision == 1
        and "configuration" not in config
        and protocol == 2
        and entry.get("version") == config["framework_version"]
        and entry.get("image_digest") == runtime.get("image_digest")
        and config["image_digest"] in entry.get("legacy_images", [])
    )


async def authority(db, run):
    try:
        await assert_training_authority(
            db,
            project_id=run.project_id,
            actor_id=run.creator_id,
            api_key_id=run.authorization.get("api_key_id"),
            required_scopes=SCOPES,
        )
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=403, detail={"code": exc.code, "message": str(exc)}
        ) from exc


async def frozen_config(
    db, project_id, body: TrainingRunCreate, *, observe_members=True
):
    options = catalog()
    if not options["enabled"]:
        raise HTTPException(status_code=409, detail="训练执行器尚未配置，请联系管理员")
    template = next(
        (item for item in options["items"] if item["framework"] == body.framework), None
    )
    base = next(
        (
            item
            for item in (template or {}).get("base_models", [])
            if item["id"] == body.base_model
        ),
        None,
    )
    if template is None or base is None:
        raise HTTPException(status_code=422, detail="训练器或基础权重不在受控目录中")
    if requires_prepared_asset(body.framework, template["version"], base):
        lease = await db.get(TrainingResourceLease, resource_id())
        if not (
            lease
            and lease.last_seen_at
            and lease.last_seen_at > now() - timedelta(seconds=30)
            and base["sha256"] in lease.runtime.get("base_asset_sha256", [])
        ):
            raise HTTPException(
                status_code=409, detail="该训练节点尚未准备并校验所选基础模型权重"
            )
    dataset = await db.scalar(
        select(TrainingDatasetVersion)
        .where(
            TrainingDatasetVersion.id == body.dataset_id,
            TrainingDatasetVersion.project_id == project_id,
        )
        .with_for_update(read=True)
    )
    if dataset is None:
        raise HTTPException(status_code=404, detail="训练数据版本不存在")
    if dataset.status != "ready" or not dataset.package_sha256:
        raise HTTPException(status_code=409, detail="训练数据版本尚未就绪")
    try:
        contract = contract_of(dataset.manifest)
        schema = definition(
            body.framework, body.base_model, version=template["version"]
        )
    except (TrainingDatasetError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.task is not None and body.task != normalize_task(contract["task"]):
        raise HTTPException(status_code=422, detail="显式训练任务与数据版本不匹配")
    if normalize_task(contract["task"]) != normalize_task(schema["task"]):
        raise HTTPException(
            status_code=422, detail="训练数据任务与基础模型或参数定义不匹配"
        )
    if normalize_task(contract["task"]) == SEGMENTATION and body.configuration is None:
        raise HTTPException(
            status_code=422, detail="分割训练必须提交完整的任务参数配置"
        )
    if template["version"] == ULTRALYTICS_VERSION and body.configuration is None:
        raise HTTPException(status_code=422, detail="该训练器版本必须提交完整参数配置")
    config = {
        "task": normalize_task(contract["task"]),
        "media_type": "image",
        "format": contract.get("format", "ultralytics-yolo"),
        "adapter_revision": ADAPTERS[body.framework]["revision"],
        "framework": body.framework,
        "framework_version": template["version"],
        "image_digest": template["image_digest"],
        "base_model": {
            key: base[key]
            for key in ("id", "sha256", "filename", "object_key", "size_bytes")
            if key in base
        },
        "dataset_id": str(dataset.id),
        "dataset_sha256": dataset.package_sha256,
        "dataset_digest": dataset.manifest["content_digest"],
        "validation_digest": dataset.manifest["validation_digest"],
        "classes": dataset.manifest["classes"],
        "parameters": body.parameters.model_dump(),
        "resource_id": resource_id(),
        "fixed_options": fixed_options(body.framework),
    }
    if normalize_task(contract["task"]) == SEGMENTATION or contract["schema"] == 3:
        config["data_contract"] = contract
    if body.configuration is not None:
        configuration = configuration_for_data_contract(
            resolve_configuration(
                body.framework, body.base_model, body.configuration.model_dump()
            ),
            contract,
        )
        if configuration["identity"]["framework_version"] != template["version"]:
            raise HTTPException(
                status_code=409, detail="训练器版本已变化，请重新读取目录与参数定义"
            )
        config.update(
            adapter_revision=2,
            configuration=configuration,
            parameters={
                key: configuration["effective_values"][key] for key in CANONICAL
            },
            fixed_options={
                key: value
                for key, value in configuration["effective_values"].items()
                if configuration["origins"][key].split(":", 1)[0] == "platform"
            },
        )
    pin = await initialization.resolve(
        db,
        project_id,
        body.initialization,
        body.framework,
        body.base_model,
        config["classes"],
    )
    if pin is not None:
        if (
            not config.get("configuration", {})
            .get("effective_values", {})
            .get("pretrained", True)
        ):
            raise HTTPException(
                status_code=422, detail="从零训练不能同时选择项目模型权重"
            )
        config["initialization"] = pin
    if config.get("configuration"):
        from training_contract import verify_frozen

        try:
            verify_frozen(config)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        inputs = input_contract_for(dataset.manifest) if observe_members else None
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="训练数据成员清单无效") from exc
    if inputs is not None:
        config["input_contract"] = inputs
    return config


def storage_owner(run):
    """Translate a pinned run locator at HTTP-oriented service boundaries."""
    try:
        return training_run_storage.resolve(run)
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None


def attempt_storage_owner(run, attempt, key=None):
    owner = storage_owner(run)
    try:
        if attempt is None or attempt.run_id != run.id:
            raise TrainingDatasetError(
                "training_storage_attempt_mismatch",
                "产物不属于该训练执行",
                status_code=409,
            )
        if key is not None:
            training_run_storage.expect_attempt(owner, attempt.id, key)
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    return owner


def artifact_key(owner, attempt_id, relative_name):
    try:
        key = owner.key(f"attempts/{attempt_id}/{relative_name}")
        training_run_storage.expect_attempt(owner, attempt_id, key)
        return key
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None


async def allocate_storage(db, run):
    """Only explicit new-run creation calls this before its first attempt."""
    try:
        return await training_run_storage.allocate(db, run)
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None


async def new_attempt(db, run, *, resume_from=None):
    storage_owner(run)
    ordinal = (
        await db.scalar(
            select(func.max(TrainingAttempt.ordinal)).where(
                TrainingAttempt.run_id == run.id
            )
        )
        or 0
    ) + 1
    attempt = TrainingAttempt(
        run_id=run.id, ordinal=ordinal, resume_from_id=resume_from
    )
    db.add(attempt)
    await db.flush()
    job = await jobs.create_job(
        db,
        kind="training_run",
        user_id=run.creator_id,
        project_id=run.project_id,
        payload={
            "run_id": str(run.id),
            "attempt_id": str(attempt.id),
            "name": run.name,
        },
    )
    run.current_attempt_id = attempt.id
    run.async_job_id = job.id
    run.status = "queued"
    run.metrics = {}
    run.error_message = None
    run.completed_at = None
    return attempt


async def lock_resource(db, identity=None):
    identity = identity or resource_id()
    await db.execute(
        insert(TrainingResourceLease)
        .values(resource_id=identity)
        .on_conflict_do_nothing()
    )
    return await db.scalar(
        select(TrainingResourceLease)
        .where(TrainingResourceLease.resource_id == identity)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def mark_terminal(db, run, attempt, status, error=None):
    if (
        attempt.native_budget
        and attempt.resource_id is None
        and attempt.native_accounting is None
    ):
        from training_contract.budget import receipt

        attempt.native_accounting = receipt(
            attempt.native_budget, "not_started", seconds=0.0
        )
    error = (
        redact_text(error, (settings.training_executor_token,))[:1000]
        if error
        else error
    )
    attempt.status = run.status = status
    attempt.completed_at = run.completed_at = now()
    attempt.error_message = run.error_message = error
    if status == "succeeded":
        await jobs.mark_complete(
            db,
            run.async_job_id,
            result={"run_id": str(run.id), "attempt_id": str(attempt.id)},
        )
    elif status == "cancelled":
        await jobs.mark_cancelled(db, run.async_job_id)
    else:
        await jobs.mark_failed(db, run.async_job_id, error=error or "训练失败")
    await notify_job_terminal(db, job_id=run.async_job_id)


async def observe_lost(db, lease):
    if not lease.attempt_id:
        return
    preliminary = await db.get(TrainingAttempt, lease.attempt_id)
    if preliminary is None:
        return
    run = await db.scalar(
        select(TrainingRun)
        .where(TrainingRun.id == preliminary.run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    attempt = await db.scalar(
        select(TrainingAttempt)
        .where(TrainingAttempt.id == lease.attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        attempt
        and attempt.status in ACTIVE - {"lost", "cancelling"}
        and attempt.heartbeat_at
        and attempt.heartbeat_at
        < now() - timedelta(seconds=settings.training_heartbeat_timeout_seconds)
    ):
        if run.current_attempt_id == attempt.id:
            attempt.status = run.status = "lost"
            run.error_message = "执行器心跳失联；GPU 租约保留，等待确认旧进程退出"
            attempt.failure = failure_receipt(
                "execution_lost",
                "execution",
                run.config_digest,
                run.error_message,
                source="platform",
            )
            # Lost is not a terminal state: retain the lease and allow recovery.
            await jobs.update_progress(
                db, run.async_job_id, 0, extra_payload={"execution_lost": True}
            )


async def fenced(db, attempt_id, fence: ExecutorFence, *, allow_terminal=False):
    # Resource first, then actor/project and run/attempt. Public cancellation never
    # acquires the resource lock and cannot release an active lease.
    preliminary = await db.get(TrainingAttempt, attempt_id)
    if preliminary is None or not preliminary.resource_id:
        raise HTTPException(status_code=404, detail="执行尝试不存在")
    lease = await lock_resource(db, preliminary.resource_id)
    run = await db.scalar(
        select(TrainingRun)
        .where(TrainingRun.id == preliminary.run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    attempt = await db.scalar(
        select(TrainingAttempt)
        .where(TrainingAttempt.id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        run.current_attempt_id != attempt.id
        or attempt.executor_id != fence.executor_id
        or attempt.boot_id != fence.boot_id
        or not secrets.compare_digest(
            attempt.lease_digest or "",
            hashlib.sha256(fence.lease_token.encode()).hexdigest(),
        )
        or (
            not allow_terminal
            and (lease.attempt_id != attempt.id or attempt.status in TERMINAL)
        )
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_attempt",
                "message": "执行尝试或租约已变化，拒绝迟到回报",
            },
        )
    return lease, run, attempt


def internal_download(key, *, bucket=None, audience="internal"):
    return storage.generate_download_url(
        key,
        bucket=storage.training_bucket if bucket is None else bucket,
        expires_in=600,
        align=False,
        audience=audience,
    )


def claim_token(attempt, claim_id):
    return hmac.new(
        settings.training_executor_token.encode(),
        f"{attempt.id}:{claim_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


async def assignment(db, run, attempt, token):
    run_owner = attempt_storage_owner(run, attempt)
    dataset = await db.get(TrainingDatasetVersion, run.dataset_id)
    try:
        if dataset is None or dataset.project_id != run.project_id:
            raise TrainingDatasetError(
                "training_storage_dataset_mismatch",
                "冻结训练版本不属于当前项目",
                status_code=409,
            )
        owner = training_dataset_storage.resolve(dataset)
        owner.contains(dataset.package_key)
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    result = {
        "run_id": str(run.id),
        "attempt_id": str(attempt.id),
        "lease_token": token,
        "config": run.config,
        "config_digest": run.config_digest,
        "dataset_url": internal_download(dataset.package_key, bucket=owner.bucket),
        "resume": None,
    }
    if attempt.native_budget:
        result["native_budget"] = attempt.native_budget
    if attempt.resume_from_id:
        parent = await db.get(TrainingAttempt, attempt.resume_from_id)
        if parent is None or parent.run_id != run.id:
            raise HTTPException(status_code=409, detail="恢复来源不属于该训练实验")
        checkpoint = next(
            (
                item
                for item in parent.artifacts.values()
                if item["role"] == "checkpoint"
            ),
            None,
        )
        if checkpoint is None:
            raise HTTPException(status_code=409, detail="恢复来源缺少已核验检查点")
        attempt_storage_owner(run, parent, checkpoint["key"])
        result["resume"] = {
            **checkpoint,
            "url": internal_download(checkpoint["key"], bucket=run_owner.bucket),
        }
    elif run.config.get("initialization"):
        pin = run.config["initialization"]
        source_owner = await initialization.source_storage_owner(db, pin)
        try:
            training_run_storage.expect_attempt(
                source_owner, pin["attempt_id"], pin["artifact"]["key"]
            )
        except TrainingDatasetError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from None
        result["initialization"] = {
            **pin["artifact"],
            "source_digest": pin["source_digest"],
            "url": internal_download(
                pin["artifact"]["key"], bucket=source_owner.bucket
            ),
        }
    return result


async def claim(db, body):
    if (
        not catalog()["enabled"]
        or body.resource_id != resource_id()
        or body.runtime.get("gpu_uuid") != settings.training_gpu_uuid
    ):
        raise HTTPException(status_code=409, detail="执行器物理资源与配置不一致")
    lease = await lock_resource(db)
    lease.executor_id = body.executor_id
    lease.boot_id = body.boot_id
    lease.runtime = body.runtime
    lease.last_seen_at = now()
    await observe_lost(db, lease)
    if lease.attempt_id:
        attempt = await db.get(TrainingAttempt, lease.attempt_id)
        if (
            attempt.executor_id == body.executor_id
            and attempt.boot_id == body.boot_id
            and attempt.claim_id == body.claim_id
        ):
            token = claim_token(attempt, body.claim_id)
            if not secrets.compare_digest(
                attempt.lease_digest, hashlib.sha256(token.encode()).hexdigest()
            ):
                raise HTTPException(
                    status_code=409, detail="执行器密钥已变更，请先协调旧租约"
                )
            run = await db.get(TrainingRun, attempt.run_id)
            if not input_runtime_compatible(run.config, body.runtime):
                raise HTTPException(
                    status_code=409, detail="执行器不支持冻结的训练输入合同"
                )
            return await assignment(db, run, attempt, token)
        return None
    if body.recover_only:
        return None
    candidates = (
        await db.scalars(
            select(TrainingRun)
            .where(TrainingRun.status == "queued")
            .order_by(TrainingRun.created_at)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for run in candidates:
        config = run.config
        if config["resource_id"] != body.resource_id or not runtime_compatible(
            config, body.runtime
        ):
            continue
        attempt = await db.get(TrainingAttempt, run.current_attempt_id)
        if attempt is None or attempt.run_id != run.id:
            raise HTTPException(status_code=409, detail="执行尝试不属于当前训练实验")
        try:
            attempt_storage_owner(run, attempt)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
            await mark_terminal(
                db, run, attempt, "failed", "训练产物存储归属不可用，请核验后重试"
            )
            continue
        try:
            await authority(db, run)
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            await mark_terminal(db, run, attempt, "failed", "训练启动权限已撤销")
            continue
        from app.services.learning_policies import claim_allowed

        if not await claim_allowed(db, run):
            await mark_terminal(
                db, run, attempt, "cancelled", "自动策略已关闭、变化或项目已停用"
            )
            continue
        if run.study_id:
            from app.services import training_studies
            from training_contract.budget import validate_budget

            if not await training_studies.claim_allowed(db, run):
                await mark_terminal(
                    db, run, attempt, "cancelled", "参数搜索已停止或项目已停用"
                )
                continue
            try:
                validate_budget(attempt.native_budget, attempt.id, run.config_digest)
                previous = list(
                    await db.scalars(
                        select(TrainingAttempt).where(
                            TrainingAttempt.run_id == run.id,
                            TrainingAttempt.id != attempt.id,
                        )
                    )
                )
                from training_contract.budget import validate_receipt

                consumed = 0.0
                for prior in previous:
                    if (
                        prior.native_accounting is None
                        and prior.resource_id is None
                        and prior.status in TERMINAL
                    ):
                        continue
                    validate_receipt(prior.native_accounting, prior.native_budget)
                    if prior.native_accounting["state"] == "unknown":
                        raise ValueError("Previous native time is unknown")
                    consumed += prior.native_accounting["native_seconds"]
                if (
                    consumed + attempt.native_budget["allowance_seconds"]
                    > config["study_budget"]["max_native_seconds"] + 1e-6
                ):
                    raise ValueError("Attempt allowance exceeds cumulative child limit")
            except (ValueError, KeyError, TypeError, AttributeError):
                await mark_terminal(
                    db,
                    run,
                    attempt,
                    "failed",
                    "试验预算或先前执行计时无法核实，拒绝启动",
                )
                continue
        if config.get("initialization"):
            try:
                from training_contract.initialization import validate_pin

                pin = validate_pin(config["initialization"])
                if run.initialization_model_id != initialization.model_id(pin) or pin[
                    "project_id"
                ] != str(run.project_id):
                    raise ValueError("初始化来源引用与冻结配置不符")
                if not attempt.resume_from_id:
                    await initialization.validate_accepted(
                        db,
                        run.project_id,
                        pin,
                        config["framework"],
                        config["base_model"]["id"],
                        config["classes"],
                    )
            except (HTTPException, ValueError, KeyError, TypeError) as exc:
                reason = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                await mark_terminal(
                    db, run, attempt, "failed", "初始化来源校验失败：" + reason[:400]
                )
                attempt.failure = failure_receipt(
                    "input_mismatch",
                    "preparing",
                    run.config_digest,
                    run.error_message,
                    source="platform",
                )
                continue
        attempt.claim_id = body.claim_id
        token = claim_token(attempt, body.claim_id)
        attempt.executor_id = body.executor_id
        attempt.boot_id = body.boot_id
        attempt.lease_digest = hashlib.sha256(token.encode()).hexdigest()
        attempt.resource_id = body.resource_id
        attempt.runtime = body.runtime
        attempt.started_at = attempt.heartbeat_at = now()
        attempt.status = run.status = "preparing"
        lease.attempt_id = attempt.id
        await jobs.mark_running(db, run.async_job_id)
        return await assignment(db, run, attempt, token)
    return None


async def heartbeat(db, attempt_id, body):
    lease, run, attempt = await fenced(db, attempt_id, body)
    lease.last_seen_at = now()
    await observe_lost(db, lease)
    lost = attempt.status == "lost"
    try:
        await authority(db, run)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        run.status = attempt.status = "cancelling"
        run.error_message = "训练权限已撤销，正在停止执行"
        attempt.failure = failure_receipt(
            "authority_lost",
            "execution",
            run.config_digest,
            run.error_message,
            source="platform",
        )
    attempt.heartbeat_at = now()
    for event in body.events:
        existing = await db.get(TrainingEvent, (attempt.id, event.sequence))
        payload = event.model_dump(exclude={"sequence", "kind"})
        if existing:
            if existing.kind != event.kind or existing.payload != payload:
                raise HTTPException(
                    status_code=409, detail="同一事件序号不能用于不同内容"
                )
            continue
        last = (
            await db.scalar(
                select(func.max(TrainingEvent.sequence)).where(
                    TrainingEvent.attempt_id == attempt.id
                )
            )
            or 0
        )
        if event.sequence != last + 1:
            raise HTTPException(status_code=409, detail="事件序号不连续")
        if event.epoch and event.epoch > run.config["parameters"]["epochs"]:
            raise HTTPException(status_code=422, detail="训练轮次超出冻结配置")
        db.add(
            TrainingEvent(
                attempt_id=attempt.id,
                sequence=event.sequence,
                kind=event.kind,
                payload=payload,
            )
        )
        await db.flush()
        if run.status not in {"cancelling", "lost"}:
            run.status = attempt.status = (
                "training" if event.kind == "epoch" else event.kind
            )
        if event.kind == "epoch":
            run.metrics = {
                "epoch": event.epoch,
                "values": event.metrics,
                "validation_digest": run.config["validation_digest"],
            }
            await jobs.update_progress(
                db,
                run.async_job_id,
                min(95, int(event.epoch / run.config["parameters"]["epochs"] * 95)),
            )
    return {
        "stop": lost or run.status == "cancelling",
        "reason": "heartbeat_expired" if lost else run.error_message,
    }


async def authorize_upload(db, attempt_id, body):
    _, run, attempt = await fenced(db, attempt_id, body)
    owner = attempt_storage_owner(run, attempt)
    if len({item.name for item in body.artifacts}) != len(body.artifacts):
        raise HTTPException(status_code=422, detail="产物名称不能重复")
    inventory = {item.name: item.model_dump() for item in body.artifacts}
    if sum(item["size_bytes"] for item in inventory.values()) > 4 * 1024**3:
        raise HTTPException(status_code=422, detail="单次产物超过大小限制")
    if attempt.upload_inventory and attempt.upload_inventory != inventory:
        raise HTTPException(status_code=409, detail="已授权产物不能改写")
    attempt.upload_inventory = inventory
    if run.status not in {"lost", "cancelling"}:
        run.status = attempt.status = "uploading"
        await jobs.update_progress(
            db, run.async_job_id, 95, extra_payload={"stage": "uploading"}
        )
    result = {}
    for name, spec in inventory.items():
        key = artifact_key(owner, attempt.id, name)
        metadata = {"sha256": spec["sha256"], "attempt-id": str(attempt.id)}
        url = storage.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": owner.bucket,
                "Key": key,
                "ContentLength": spec["size_bytes"],
                "Metadata": metadata,
            },
            ExpiresIn=600,
        )
        result[name] = {
            "url": url,
            "headers": {
                "x-amz-meta-sha256": spec["sha256"],
                "x-amz-meta-attempt-id": str(attempt.id),
            },
        }
    return result


def verify_artifact(key, spec, attempt_id, *, bucket):
    response = storage.client.get_object(Bucket=bucket, Key=key)
    try:
        if response["ContentLength"] != spec["size_bytes"] or response.get(
            "Metadata", {}
        ).get("attempt-id") != str(attempt_id):
            raise ValueError("Artifact size or identity mismatch")
        digest = hashlib.sha256()
        total = 0
        for chunk in iter(lambda: response["Body"].read(1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
            if total > spec["size_bytes"]:
                raise ValueError("Artifact size exceeded")
        if total != spec["size_bytes"] or digest.hexdigest() != spec["sha256"]:
            raise ValueError("Artifact digest mismatch")
    finally:
        response["Body"].close()


def verify_native_artifact(run, attempt, artifacts):
    receipt = artifacts.get("native-config.json")
    if (
        not receipt
        or receipt["role"] != "config"
        or receipt["size_bytes"] > 1024 * 1024
    ):
        raise ValueError(
            "v2 success requires a bounded native configuration observation"
        )
    owner = attempt_storage_owner(run, attempt, receipt["key"])
    response = storage.client.get_object(Bucket=owner.bucket, Key=receipt["key"])
    try:
        blob = json.loads(response["Body"].read(1024 * 1024 + 1))
    finally:
        response["Body"].close()
    if blob["config_digest"] != run.config_digest or bool(
        blob["runtime_options"]["resume"]
    ) != bool(attempt.resume_from_id):
        raise ValueError(
            "Native observation is not bound to this frozen configuration/attempt"
        )
    expected = verify_native(run.config, blob["observed"], blob["runtime_options"])
    if blob["verification"] != expected:
        raise ValueError("Native configuration verification receipt mismatch")
    if run.config.get("initialization"):
        from training_contract.initialization import verify_evidence

        if run.initialization_model_id != initialization.model_id(
            run.config["initialization"]
        ):
            raise ValueError("Native initialization model reference mismatch")
        verify_evidence(
            run.config,
            blob.get("evidence", {}).get("initialization"),
            resumed=bool(attempt.resume_from_id),
        )


def verify_input_artifact(run, attempt, artifacts):
    """Verify actual native admission through the existing saved-owner manifest."""
    if input_contract(run.config) is None:
        return
    spec = artifacts.get("manifest.json")
    if not spec or spec["role"] != "manifest":
        raise ValueError("Successful training requires its execution manifest")
    from app.services.training_artifacts import read_json_artifact

    owner = attempt_storage_owner(run, attempt, spec["key"])
    manifest = read_json_artifact(spec, attempt.id, bucket=owner.bucket)
    if (
        manifest.get("run_id") != str(run.id)
        or manifest.get("attempt_id") != str(attempt.id)
        or manifest.get("config_digest") != run.config_digest
        or content_digest(manifest.get("config")) != run.config_digest
    ):
        raise ValueError("Execution manifest differs from its frozen attempt")
    verify_input_receipt(
        manifest.get("input_membership"), run.config, run.id, attempt.id
    )


async def finish(db, attempt_id, body):
    lease, run, attempt = await fenced(db, attempt_id, body, allow_terminal=True)
    request_digest = content_digest(body.request_payload())
    if attempt.status in TERMINAL:
        if attempt.finish_request_digest != request_digest:
            raise HTTPException(status_code=409, detail="已结束的执行不能改写终态")
        return attempt
    if lease.attempt_id != attempt.id:
        raise HTTPException(status_code=409, detail="GPU 租约已变化")
    if body.failure and body.failure.config_digest != run.config_digest:
        raise HTTPException(status_code=422, detail="故障诊断与冻结配置不符")
    if attempt.native_budget:
        from training_contract.budget import validate_receipt

        try:
            validate_receipt(body.native_accounting, attempt.native_budget)
            if (
                body.status == "succeeded"
                and body.native_accounting["state"] != "exact"
            ):
                raise ValueError("Successful native execution needs exact accounting")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise HTTPException(
                status_code=422, detail="原生计算时间回执缺失或与冻结预算不符"
            ) from exc
    elif body.native_accounting is not None:
        raise HTTPException(status_code=422, detail="未预留预算的执行不能提交计时账本")
    status = "cancelled" if run.status == "cancelling" else body.status
    if status == "succeeded":
        await authority(db, run)
        # An exact fenced completion receipt can resolve a lost heartbeat after
        # an upload outage. No successor can start while this lease is retained.
    if attempt.run_id != run.id:
        raise HTTPException(status_code=409, detail="执行尝试不属于当前训练实验")
    owner = None
    if status == "succeeded" or getattr(run, "storage_ref", None) is not None:
        try:
            owner = attempt_storage_owner(run, attempt)
        except HTTPException as exc:
            if status == "succeeded" or exc.status_code != 409:
                raise
            # An exact failed/stopped receipt settles the lease without guessing
            # where an old execution wrote. Its upload inventory remains durable.
    artifacts = {}
    for name, spec in attempt.upload_inventory.items() if owner else ():
        try:
            key = artifact_key(owner, attempt.id, name)
        except HTTPException:
            if status == "succeeded":
                raise
            continue
        try:
            await asyncio.to_thread(
                verify_artifact, key, spec, attempt.id, bucket=owner.bucket
            )
        except Exception as exc:
            if status == "succeeded":
                raise HTTPException(
                    status_code=422, detail="产物尚未完整上传或摘要不匹配"
                ) from exc
            continue
        artifacts[name] = {**spec, "key": key}
        if spec["role"] == "telemetry":
            from app.services.training_observability import verify_summary

            try:
                await asyncio.to_thread(verify_summary, run, attempt, artifacts[name])
            except (ValueError, KeyError, TypeError) as exc:
                if status == "succeeded":
                    raise HTTPException(
                        status_code=422, detail="观测摘要产物与执行尝试不符"
                    ) from exc
                artifacts.pop(name)
    if status == "succeeded" and not {
        "weights",
        "checkpoint",
        "metrics",
        "config",
        "log",
    } <= {item["role"] for item in artifacts.values()}:
        raise HTTPException(status_code=422, detail="成功训练缺少必要产物")
    if status == "succeeded" and run.config.get("adapter_revision") == 2:
        try:
            await asyncio.to_thread(verify_native_artifact, run, attempt, artifacts)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(
                status_code=422, detail="原生参数观测缺失或与冻结配置不符"
            ) from exc
    if status == "succeeded":
        try:
            await asyncio.to_thread(verify_input_artifact, run, attempt, artifacts)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise HTTPException(
                status_code=422, detail="训练实际接纳成员回执缺失或与冻结清单不符"
            ) from exc
    attempt.artifacts = artifacts
    attempt.native_accounting = body.native_accounting
    attempt.finish_request_digest = request_digest
    if body.failure:
        attempt.failure = {
            **body.failure.model_dump(mode="json"),
            "diagnostic": redact_text(
                body.failure.diagnostic,
                (settings.training_executor_token, body.lease_token),
            )[:1000],
        }
    error = (
        redact_text(
            body.error_message, (settings.training_executor_token, body.lease_token)
        )
        if body.error_message
        else None
    )
    await mark_terminal(db, run, attempt, status, error)
    lease.attempt_id = None
    return attempt
