"""One durable training state machine, projected into transient AsyncJobs."""

from __future__ import annotations

from app.services import training_storage_io as storage_io
from app.services import storage_access

from yoao_task_contract import normalize_task, legacy_training_task
from training_contract.catalog import definition_for_identity, instance_rle_capability
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from training_contract import definition, verify_native
from training_contract.validation import CANONICAL, configuration_for_data_contract
from training_contract.dataset import DETECTION, SEGMENTATION, is_instance_rle
from training_contract.models import ULTRALYTICS_VERSION
from training_contract.observability import failure_receipt, redact_text
from training_contract.input_membership import (
    contract_for as input_contract_for,
    declared_contract as input_contract,
    verify_receipt as verify_input_receipt,
)

from app.config import settings
from app.schemas.training_run import ExecutorFence, ExecutorRecovery, TrainingRunCreate
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal
from app.services.storage import storage_service as storage
from app.services import training_dataset_storage
from app.services import training_run_storage
from app.services import training_claims as claims
from app.services import training_dataset_data as datasets
from app.services import training_run_data as data
from app.services.identity_contracts import IdentityActorFacts
from app.services.project_write_guard import lock_project_manager
from app.services.training_authority_contract import TrainingAuthorityPort
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    contract_of,
)
from app.services.training_adapters import (
    ADAPTERS,
    catalog,
    configured_resource_slots,
    fixed_options,
    plugin_parameter_schema_payload,
    plugin_requires_gpu,
    resolve_catalog,
    resolve_configuration,
    resource_id,
    select_resource_slot,
    training_executor_configured,
)
from app.services.trainer_plugins_data import TrainerPluginsPostgresData
from training_contract.trainer_plugin import TRAINER_PROTOCOL_VERSION, TrainReceiptV1
from app.services import training_initialization as initialization
from app.services import training_recipes as recipes
from app.services.learning_context import LearningActorContext

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


async def stage_run_deletion(db, run: data.RunFacts) -> None:
    await data.delete_run(db, run.project_id, run.id)


async def load_run(db, project_id, run_id, *, lock=False):
    run = await data.get_run(db, project_id, run_id, lock=lock)
    if run is None:
        raise HTTPException(status_code=404, detail="训练实验不存在")
    return run


def annotate_readiness(result, runtime, *, online, slot_runtimes=None):
    """Project exact model/node readiness without treating one sibling as ready."""
    if slot_runtimes is None:
        effective_slots = (
            [
                {
                    "resource_id": resource_id(),
                    "device_kind": "gpu"
                    if settings.training_gpu_uuid.startswith("GPU-")
                    else "cpu",
                    "gpu_uuid": settings.training_gpu_uuid or None,
                    "online": bool(online),
                    "runtime": runtime or {},
                }
            ]
            if online
            else []
        )
        any_online = bool(online)
    else:
        effective_slots = [slot for slot in slot_runtimes if slot.get("online")]
        any_online = bool(effective_slots)

    for item in result["items"]:
        if item.get("plugin_id") is not None:
            for base in item["base_models"]:
                candidate = {
                    "adapter_revision": 3,
                    "trainer_protocol": item.get("protocol_version")
                    or TRAINER_PROTOCOL_VERSION,
                    "framework": item["framework"],
                    "framework_version": item["version"],
                    "image_digest": item.get("image_digest"),
                    "trainer_plugin": {
                        "plugin_id": item["plugin_id"],
                        "execution_mode": item.get("execution_mode"),
                        "requires_gpu": bool(item.get("requires_gpu", True)),
                    },
                    "base_model": base,
                }
                ready = (
                    any(
                        runtime_compatible(candidate, slot.get("runtime") or {})
                        for slot in effective_slots
                    )
                    if any_online
                    else None
                )
                base_sha = base.get("sha256")
                missing_asset = bool(
                    any_online
                    and base_sha
                    and not any(
                        base_sha
                        in (slot.get("runtime") or {}).get("base_asset_sha256", [])
                        for slot in effective_slots
                    )
                )
                base["compatible"] = ready
                base["unavailable_reason"] = (
                    "训练节点离线"
                    if not any_online
                    else "该节点尚未准备此基础模型权重"
                    if missing_asset
                    else "该节点未启用兼容的训练插件执行环境"
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
                    if any_online
                    else None
                )
            item["compatible"] = (
                any(base["compatible"] for base in item["base_models"])
                if any_online
                else None
            )
            continue

        gpu_slots = [
            slot
            for slot in effective_slots
            if slot.get("device_kind") == "gpu"
            and (slot.get("runtime") or {}).get("gpu_uuid") == slot.get("gpu_uuid")
        ]
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
                    and any(
                        runtime_compatible(candidate, slot.get("runtime") or {})
                        for slot in gpu_slots
                    )
                )
                if any_online
                else None
            )
            missing_asset = bool(
                any_online
                and requires_prepared_asset(item["framework"], item["version"], base)
                and not any(
                    base["sha256"]
                    in (slot.get("runtime") or {}).get("base_asset_sha256", [])
                    for slot in gpu_slots
                )
            )
            base["compatible"] = ready
            base["unavailable_reason"] = (
                "训练节点离线"
                if not any_online
                else "该节点尚未准备此基础模型权重"
                if missing_asset
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
                if any_online
                else None
            )
        legacy_ready = (
            any_online
            and item["version"] != ULTRALYTICS_VERSION
            and any(
                runtime_compatible(
                    {
                        "framework": item["framework"],
                        "framework_version": item["version"],
                        "image_digest": item["image_digest"],
                        "base_model": base,
                        "adapter_revision": 1,
                    },
                    slot.get("runtime") or {},
                )
                for slot in gpu_slots
                for base in item["base_models"]
                if base["task"] == DETECTION
            )
        )
        item["compatible"] = (
            bool(
                legacy_ready or any(base["compatible"] for base in item["base_models"])
            )
            if any_online
            else None
        )
    return result


async def build_catalog_response(db, *, include_policy_digests: bool = False) -> dict:
    result = await resolve_catalog(db)
    slots = configured_resource_slots()
    leases_by_id: dict[str, data.GpuLeaseFacts] = {}
    if db is not None and hasattr(db, "execute"):
        leases = await data.list_leases(db)
        leases_by_id = {lease.resource_id: lease for lease in leases}
    cutoff = now() - timedelta(seconds=30)
    resource_slots = []
    slot_runtimes = []
    for slot in slots:
        lease = leases_by_id.get(slot["resource_id"])
        slot_online = bool(lease and lease.last_seen_at and lease.last_seen_at > cutoff)
        slot_busy = bool(lease and lease.attempt_id)
        resource_slots.append(
            {
                "resource_id": slot["resource_id"],
                "device_kind": slot["device_kind"],
                "gpu_uuid": slot["gpu_uuid"],
                "online": slot_online,
                "busy": slot_busy,
                "executor_id": lease.executor_id if lease else None,
                "last_seen_at": lease.last_seen_at if lease else None,
            }
        )
        slot_runtimes.append(
            {
                **slot,
                "online": slot_online,
                "runtime": lease.runtime if lease else {},
            }
        )
    primary_id = resource_id()
    primary_lease = leases_by_id.get(primary_id)
    if primary_lease is None:
        online_slot = next((slot for slot in resource_slots if slot["online"]), None)
        if online_slot is not None:
            primary_lease = leases_by_id.get(online_slot["resource_id"])
    online = any(slot["online"] for slot in resource_slots)
    online_slots = [slot for slot in resource_slots if slot["online"]]
    if len(online_slots) > 1:
        busy = all(slot["busy"] for slot in online_slots)
    elif len(online_slots) == 1:
        busy = online_slots[0]["busy"]
    else:
        busy = bool(primary_lease and primary_lease.attempt_id)
    annotate_readiness(
        result,
        primary_lease.runtime if primary_lease else {},
        online=online,
        slot_runtimes=slot_runtimes,
    )
    payload = {
        **result,
        "online": online,
        "busy": busy,
        "executor_id": primary_lease.executor_id if primary_lease else None,
        "last_seen_at": primary_lease.last_seen_at if primary_lease else None,
        "resource_slots": resource_slots,
    }
    if include_policy_digests:
        primary_runtime = primary_lease.runtime if primary_lease else {}
        payload["initialization_policy_digest"] = primary_runtime.get(
            "initialization_policy_digest"
        )
        payload["native_budget_policy_digest"] = primary_runtime.get(
            "native_budget_policy_digest"
        )
    return payload


async def observe_all_lost(db) -> None:
    if not training_executor_configured():
        return
    for slot in configured_resource_slots():
        lease = await lock_resource(db, slot["resource_id"])
        await observe_lost(db, lease)
    await db.commit()


def requires_prepared_asset(framework, version, base):
    return bool(base.get("object_key")) or (
        framework == "ultralytics" and version == ULTRALYTICS_VERSION
    )


def input_runtime_compatible(config, runtime):
    try:
        from training_contract.initialization import saved_prefix_compatible

        inputs = input_contract(config)
        return saved_prefix_compatible(config, runtime) and (
            inputs is None
            or runtime.get("input_receipt_policy_digest") == inputs["policy_digest"]
        )
    except (ValueError, KeyError, TypeError):
        return False


def runtime_compatible(config, runtime):
    if not input_runtime_compatible(config, runtime):
        return False
    if config.get("study_budget"):
        from training_contract.budget import POLICY_DIGEST

        if (
            config["study_budget"].get("policy_digest") != POLICY_DIGEST
            or runtime.get("native_budget_policy_digest") != POLICY_DIGEST
        ):
            return False
    revision = config.get("adapter_revision", 1)
    if revision == 3 or config.get("trainer_protocol") == TRAINER_PROTOCOL_VERSION:
        if runtime.get("executor_protocol") != 3:
            return False
        protocols = runtime.get("trainer_protocols", [])
        if not isinstance(protocols, list) or TRAINER_PROTOCOL_VERSION not in protocols:
            return False
        plugin = config.get("trainer_plugin") or {}
        execution_mode = plugin.get("execution_mode")
        modes = runtime.get("execution_modes", [])
        if not isinstance(modes, list) or execution_mode not in modes:
            return False
        if execution_mode == "local_process":
            plugin_id = plugin.get("plugin_id")
            allowed = runtime.get("allowed_local_plugins", [])
            if (
                not isinstance(plugin_id, str)
                or not isinstance(allowed, list)
                or plugin_id not in allowed
            ):
                return False
        if plugin.get("requires_gpu", True):
            gpu_uuid = runtime.get("gpu_uuid")
            if not isinstance(gpu_uuid, str) or not gpu_uuid.startswith("GPU-"):
                return False
        base_sha = (config.get("base_model") or {}).get("sha256")
        if base_sha and base_sha not in runtime.get("base_asset_sha256", []):
            return False
        return True

    framework = config["framework"]
    if requires_prepared_asset(
        framework, config["framework_version"], config.get("base_model", {})
    ) and config["base_model"]["sha256"] not in runtime.get("base_asset_sha256", []):
        return False
    if is_instance_rle(config.get("data_contract", {})) and runtime.get(
        "instance_rle_consumer"
    ) != instance_rle_capability(config["framework_version"]):
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


async def authority(
    training_authority: TrainingAuthorityPort,
    *,
    project_id,
    actor_id,
    api_key_id,
) -> None:
    """Preserve executor-facing HTTP error projection without owning SQL access."""
    try:
        await training_authority.assert_authority(
            project_id=project_id,
            actor_id=actor_id,
            api_key_id=api_key_id,
            required_scopes=SCOPES,
        )
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=403, detail={"code": exc.code, "message": str(exc)}
        ) from exc


async def _frozen_plugin_config(
    db, project_id, body: TrainingRunCreate, plugin, *, observe_members=True
):
    if not training_executor_configured():
        raise HTTPException(status_code=409, detail="训练执行器尚未配置，请联系管理员")
    requires_gpu = plugin_requires_gpu(plugin)
    slot = select_resource_slot(requires_gpu=requires_gpu)
    if slot is None:
        raise HTTPException(status_code=409, detail="没有满足训练器资源需求的执行槽位")
    caps = plugin.capabilities_snapshot
    arch = next(
        (
            item
            for item in caps.get("architectures", [])
            if item["id"] == body.base_model
        ),
        None,
    )
    if arch is None:
        raise HTTPException(status_code=422, detail="训练器或基础权重不在受控目录中")
    base_sha = arch.get("base_asset_sha256")
    if base_sha:
        cutoff = now() - timedelta(seconds=30)
        leases = {lease.resource_id: lease for lease in await data.list_leases(db)}
        eligible_slots = [
            candidate
            for candidate in configured_resource_slots()
            if (candidate["device_kind"] == "gpu" if requires_gpu else True)
        ]
        prepared_slot = next(
            (
                candidate
                for candidate in eligible_slots
                if (
                    (lease := leases.get(candidate["resource_id"])) is not None
                    and lease.last_seen_at
                    and lease.last_seen_at > cutoff
                    and base_sha in lease.runtime.get("base_asset_sha256", [])
                )
            ),
            None,
        )
        if prepared_slot is None:
            raise HTTPException(
                status_code=409, detail="该训练节点尚未准备并校验所选基础模型权重"
            )
        slot = prepared_slot
    if not await datasets.hold_version(db, body.dataset_id, project_id):
        raise HTTPException(status_code=404, detail="训练数据版本不存在")
    read_dataset = (
        datasets.read_version if observe_members else datasets.read_version_facts
    )
    dataset = await read_dataset(db, body.dataset_id, project_id=project_id)
    if dataset is None:
        raise HTTPException(status_code=404, detail="训练数据版本不存在")
    if dataset.status != "ready" or not dataset.package_sha256:
        raise HTTPException(status_code=409, detail="训练数据版本尚未就绪")
    try:
        contract = contract_of(dataset.manifest)
    except (TrainingDatasetError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        dataset_task = normalize_task(contract["task"])
    except ValueError:
        dataset_task = str(contract["task"])
    if body.task is not None and body.task != dataset_task:
        raise HTTPException(status_code=422, detail="显式训练任务与数据版本不匹配")
    supported_tasks = set(caps.get("supported_tasks", []))
    arch_tasks = set(arch.get("tasks", []))
    if dataset_task not in supported_tasks or dataset_task not in arch_tasks:
        raise HTTPException(
            status_code=422, detail="训练数据任务与基础模型或训练器能力不匹配"
        )
    supported_contracts = list(caps.get("supported_dataset_contracts", []))
    candidate_contracts: list[str] = []
    if contract.get("bridge"):
        candidate_contracts.append(str(contract["bridge"]))
    elif contract.get("format", "ultralytics-yolo") == "ultralytics-yolo":
        if dataset_task == DETECTION:
            candidate_contracts.append("ultralytics-bbox-1")
        elif dataset_task == SEGMENTATION:
            candidate_contracts.append("ultralytics-simple-polygon-1")
    if contract.get("format"):
        candidate_contracts.append(str(contract["format"]))
    matched_dataset_contract = next(
        (item for item in candidate_contracts if item in supported_contracts), None
    )
    if matched_dataset_contract is None:
        raise HTTPException(status_code=422, detail="训练器不支持该训练数据集契约")

    parameters = body.parameters.model_dump()
    configuration_block = None
    if body.configuration is not None:
        expected_schema = plugin_parameter_schema_payload(
            plugin,
            base_model=arch["id"],
            task=dataset_task,
            model_family=arch["family"],
        )
        if (
            body.configuration.schema_id != expected_schema["schema_id"]
            or body.configuration.schema_digest != expected_schema["schema_digest"]
        ):
            raise HTTPException(
                status_code=422, detail="参数配置与当前训练器定义不匹配"
            )
        effective_values = {
            field["key"]: field.get("default")
            for field in expected_schema["fields"]
            if "key" in field
        }
        effective_values.update(body.configuration.values)
        epochs_value = effective_values.get("epochs")
        if type(epochs_value) is int and epochs_value >= 1:
            parameters["epochs"] = epochs_value
        configuration_block = {
            "identity": {
                key: expected_schema[key]
                for key in (
                    "schema_id",
                    "schema_digest",
                    "framework",
                    "framework_version",
                    "adapter_revision",
                    "task",
                    "model_family",
                )
            },
            "values": dict(body.configuration.values),
            "effective_values": effective_values,
        }

    image_digest = (
        plugin.artifact.split("@", 1)[1]
        if plugin.artifact and "@sha256:" in plugin.artifact
        else None
    )
    config = {
        "task": dataset_task,
        "media_type": "image",
        "format": contract.get("format", "ultralytics-yolo"),
        "dataset_contract": matched_dataset_contract,
        "adapter_revision": 3,
        "trainer_protocol": TRAINER_PROTOCOL_VERSION,
        "framework": plugin.framework_id,
        "framework_version": str(caps.get("framework_version") or plugin.version),
        "image_digest": image_digest,
        "trainer_plugin": {
            "plugin_id": plugin.plugin_id,
            "version": plugin.version,
            "execution_mode": plugin.execution_mode,
            "artifact": plugin.artifact,
            "entrypoint": list(plugin.entrypoint),
            "resource": dict(plugin.resource),
            "capabilities": list(plugin.capabilities),
            "capabilities_digest": plugin.capabilities_digest,
            "requires_gpu": requires_gpu,
        },
        "base_model": {
            "id": arch["id"],
            "family": arch["family"],
            "sha256": base_sha,
            "filename": arch.get("base_asset_filename"),
        },
        "dataset_id": str(dataset.id),
        "dataset_sha256": dataset.package_sha256,
        "dataset_digest": dataset.manifest["content_digest"],
        "validation_digest": dataset.manifest["validation_digest"],
        "classes": dataset.manifest["classes"],
        "parameters": parameters,
        "resource_id": slot["resource_id"],
        "fixed_options": {},
    }
    if dataset_task == SEGMENTATION or contract.get("schema", 1) >= 3:
        config["data_contract"] = contract
    if configuration_block is not None:
        config["configuration"] = configuration_block
    if body.initialization is not None and body.initialization.kind != "catalog":
        pin = await initialization.resolve(
            db,
            project_id,
            body.initialization,
            plugin.framework_id,
            arch["id"],
            config["classes"],
        )
        if pin is not None:
            config["initialization"] = pin
    try:
        inputs = input_contract_for(dataset.manifest) if observe_members else None
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="训练数据成员清单无效") from exc
    if inputs is not None:
        config["input_contract"] = inputs
    return config


async def frozen_config(
    db, project_id, body: TrainingRunCreate, *, observe_members=True
):
    enabled_plugin = None
    if db is not None and hasattr(db, "scalar") and hasattr(db, "execute"):
        enabled_plugin = await TrainerPluginsPostgresData(db).get_enabled_by_framework(
            body.framework
        )
    if enabled_plugin is not None:
        return await _frozen_plugin_config(
            db, project_id, body, enabled_plugin, observe_members=observe_members
        )
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
        lease = await data.get_lease(db, resource_id())
        if not (
            lease
            and lease.last_seen_at
            and lease.last_seen_at > now() - timedelta(seconds=30)
            and base["sha256"] in lease.runtime.get("base_asset_sha256", [])
        ):
            raise HTTPException(
                status_code=409, detail="该训练节点尚未准备并校验所选基础模型权重"
            )
    if not await datasets.hold_version(db, body.dataset_id, project_id):
        raise HTTPException(status_code=404, detail="训练数据版本不存在")
    read_dataset = (
        datasets.read_version if observe_members else datasets.read_version_facts
    )
    dataset = await read_dataset(db, body.dataset_id, project_id=project_id)
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
        if run.storage_ref is not None:
            return training_run_storage.resolve(run).ref
        ref = await training_run_storage.allocate(db, run)
        await data.pin_storage(db, run.id, ref)
        return ref
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None


async def new_attempt(db, run, *, resume_from=None):
    storage_owner(run)
    attempt_id = uuid.uuid4()
    job = await jobs.create_job(
        db,
        kind="training_run",
        user_id=run.creator_id,
        project_id=run.project_id,
        payload={
            "run_id": str(run.id),
            "attempt_id": str(attempt_id),
            "name": run.name,
        },
    )
    attempt = await data.enqueue_attempt(
        db, run.id, attempt_id, job.id, previous=resume_from
    )
    run.current_attempt_id = attempt.id
    run.async_job_id = job.id
    run.status = "queued"
    run.metrics = {}
    run.error_message = None
    run.completed_at = None
    return attempt


async def lock_resource(db, identity=None):
    return await data.get_lease(db, identity or resource_id(), lock=True)


async def mark_terminal(db, run, attempt, status, error=None, *, failure=None):
    accounting = attempt.native_accounting
    if (
        attempt.native_budget
        and attempt.resource_id is None
        and attempt.native_accounting is None
    ):
        from training_contract.budget import receipt

        accounting = receipt(attempt.native_budget, "not_started", seconds=0.0)
    error = (
        redact_text(error, (settings.training_executor_token,))[:1000]
        if error
        else error
    )
    at = now()
    await data.finish_queued_attempt(
        db, run.id, attempt.id, status, at, error, accounting, failure
    )
    attempt.native_accounting = accounting
    if failure is not None:
        attempt.failure = failure
    await _terminal_job(db, run, attempt, status, at, error)


async def _terminal_job(db, run, attempt, status, at, error):
    attempt.status = run.status = status
    attempt.completed_at = run.completed_at = at
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


async def observe_lost(db, lease, *, run=None, attempt=None):
    if not lease.attempt_id:
        return
    if run is None or attempt is None:
        preliminary = await data.get_attempt(db, lease.attempt_id)
        if preliminary is None:
            return
        run = await data.executor_run(db, preliminary.run_id, lock=True)
        attempt = await data.get_attempt(db, lease.attempt_id, lock=True)
    cutoff = now() - timedelta(seconds=settings.training_heartbeat_timeout_seconds)
    if (
        run
        and attempt
        and attempt.status in ACTIVE - {"lost", "cancelling"}
        and attempt.heartbeat_at
        and attempt.heartbeat_at < cutoff
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
            await data.lose_attempt(
                db, run.id, attempt.id, cutoff, run.error_message, attempt.failure
            )
            await jobs.update_progress(
                db, run.async_job_id, 0, extra_payload={"execution_lost": True}
            )


async def fenced(db, attempt_id, fence: ExecutorFence, *, allow_terminal=False):
    preliminary = await data.get_attempt(db, attempt_id)
    if preliminary is None or not preliminary.resource_id:
        raise HTTPException(status_code=404, detail="执行尝试不存在")
    lease = await lock_resource(db, preliminary.resource_id)
    run = await data.executor_run(db, preliminary.run_id, lock=True)
    attempt = await data.get_attempt(db, attempt_id, lock=True)
    if (
        run is None
        or attempt is None
        or run.current_attempt_id != attempt.id
        or not await data.attempt_matches(db, attempt.id, fence)
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


def internal_download(key, *, bucket=None, audience="internal", transport=None):
    transport = storage if transport is None else transport
    return storage_io.sign_download(
        transport,
        key,
        bucket=transport.training_bucket if bucket is None else bucket,
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
    dataset = await datasets.read_version_facts(
        db, run.dataset_id, project_id=run.project_id
    )
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
        "resume": None,
    }
    if attempt.native_budget:
        result["native_budget"] = attempt.native_budget
    if attempt.resume_from_id:
        parent = await data.get_attempt(db, attempt.resume_from_id)
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
        result["resume"] = {**checkpoint}
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
        }
    await db.commit()
    result["dataset_url"] = await storage_io.read(
        db,
        owner,
        "training_assignment",
        lambda transport: internal_download(
            dataset.package_key, bucket=owner.bucket, transport=transport
        ),
        object_key=dataset.package_key,
        sha256=dataset.package_sha256,
        size_bytes=dataset.size_bytes,
    )
    if result["resume"] is not None:
        result["resume"]["url"] = await storage_io.read(
            db,
            run_owner,
            "training_assignment",
            lambda transport: internal_download(
                checkpoint["key"], bucket=run_owner.bucket, transport=transport
            ),
            object_key=checkpoint["key"],
            sha256=checkpoint.get("sha256"),
            size_bytes=checkpoint.get("size_bytes"),
        )
    elif "initialization" in result:
        result["initialization"]["url"] = await storage_io.read(
            db,
            source_owner,
            "training_assignment",
            lambda transport: internal_download(
                pin["artifact"]["key"], bucket=source_owner.bucket, transport=transport
            ),
            object_key=pin["artifact"]["key"],
            sha256=pin["artifact"]["sha256"],
            size_bytes=pin["artifact"]["size_bytes"],
        )
    return result


async def claim(db, body, *, training_authority: TrainingAuthorityPort):
    slots = {slot["resource_id"]: slot for slot in configured_resource_slots()}
    slot = slots.get(body.resource_id)
    if (
        not training_executor_configured()
        or slot is None
        or (
            slot["device_kind"] == "gpu"
            and body.runtime.get("gpu_uuid") != slot["gpu_uuid"]
        )
        or (slot["device_kind"] == "cpu" and body.runtime.get("gpu_uuid") is not None)
    ):
        raise HTTPException(status_code=409, detail="执行器物理资源与配置不一致")
    lease = await lock_resource(db, body.resource_id)
    await data.observe_executor(db, body, now())
    await observe_lost(db, lease)
    if lease.attempt_id:
        attempt = await data.get_attempt(db, lease.attempt_id)
        if (
            attempt.executor_id == body.executor_id
            and attempt.boot_id == body.boot_id
            and attempt.claim_id == body.claim_id
        ):
            token = claim_token(attempt, body.claim_id)
            proof = ExecutorFence(
                executor_id=body.executor_id, boot_id=body.boot_id, lease_token=token
            )
            if not await data.attempt_matches(db, attempt.id, proof):
                raise HTTPException(
                    status_code=409, detail="执行器密钥已变更，请先协调旧租约"
                )
            run = await data.executor_run(db, attempt.run_id)
            if not input_runtime_compatible(run.config, body.runtime):
                raise HTTPException(
                    status_code=409, detail="执行器不支持冻结的训练输入合同"
                )
            return await assignment(db, run, attempt, token)
        return None
    if body.recover_only:
        return None
    candidates = await data.gpu_queue(db)
    for run in candidates:
        config = run.config
        if config["resource_id"] != body.resource_id or not runtime_compatible(
            config, body.runtime
        ):
            continue
        attempt = await data.get_attempt(db, run.current_attempt_id)
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
            await authority(
                training_authority,
                project_id=run.project_id,
                actor_id=run.creator_id,
                api_key_id=run.authorization.get("api_key_id"),
            )
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            await mark_terminal(db, run, attempt, "failed", "训练启动权限已撤销")
            continue
        if not await claims.policy_claim_allowed(db, run):
            await mark_terminal(
                db, run, attempt, "cancelled", "自动策略已关闭、变化或项目已停用"
            )
            continue
        if run.study_id:
            from training_contract.budget import validate_budget

            if not await claims.study_claim_allowed(db, run):
                await mark_terminal(
                    db, run, attempt, "cancelled", "参数搜索已停止或项目已停用"
                )
                continue
            try:
                validate_budget(attempt.native_budget, attempt.id, run.config_digest)
                previous = await data.list_attempts(db, run.id)
                from training_contract.budget import validate_receipt

                consumed = 0.0
                for prior in previous:
                    if prior.id == attempt.id:
                        continue
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
                error = "初始化来源校验失败：" + reason[:400]
                diagnostic = redact_text(error, (settings.training_executor_token,))
                await mark_terminal(
                    db,
                    run,
                    attempt,
                    "failed",
                    error,
                    failure=failure_receipt(
                        "input_mismatch",
                        "preparing",
                        run.config_digest,
                        diagnostic,
                        source="platform",
                    ),
                )
                continue
        attempt.claim_id = body.claim_id
        token = claim_token(attempt, body.claim_id)
        attempt.executor_id = body.executor_id
        attempt.boot_id = body.boot_id
        at = now()
        await data.claim_attempt(db, run.id, attempt.id, body, token, at)
        attempt.resource_id = body.resource_id
        attempt.runtime = body.runtime
        attempt.started_at = attempt.heartbeat_at = at
        attempt.status = run.status = "preparing"
        lease.attempt_id = attempt.id
        await jobs.mark_running(db, run.async_job_id)
        return await assignment(db, run, attempt, token)
    return None


async def heartbeat(db, attempt_id, body, *, training_authority: TrainingAuthorityPort):
    lease, run, attempt = await fenced(db, attempt_id, body)
    await observe_lost(db, lease, run=run, attempt=attempt)
    lost = attempt.status == "lost"
    try:
        await authority(
            training_authority,
            project_id=run.project_id,
            actor_id=run.creator_id,
            api_key_id=run.authorization.get("api_key_id"),
        )
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
    metrics = None
    for event in body.events:
        outcome = await data.record_event(
            db,
            attempt.id,
            body,
            event.sequence,
            event.kind,
            event.model_dump(exclude={"sequence", "kind"}),
        )
        if outcome == "replay":
            continue
        if outcome == "conflict":
            raise HTTPException(status_code=409, detail="同一事件序号不能用于不同内容")
        if outcome == "gap":
            raise HTTPException(status_code=409, detail="事件序号不连续")
        if outcome == "epoch_bounds":
            raise HTTPException(status_code=422, detail="训练轮次超出冻结配置")
        if outcome != "accepted":
            data._stale()
        if run.status not in {"cancelling", "lost"}:
            run.status = attempt.status = (
                "training" if event.kind == "epoch" else event.kind
            )
        if event.kind == "epoch":
            metrics = {
                "epoch": event.epoch,
                "values": event.metrics,
                "validation_digest": run.config["validation_digest"],
            }
            total_epochs = max(
                1, int((run.config.get("parameters") or {}).get("epochs") or 1)
            )
            await jobs.update_progress(
                db,
                run.async_job_id,
                min(95, int(event.epoch / total_epochs * 95)),
            )
    await data.heartbeat_attempt(db, run, attempt, body, metrics=metrics)
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
    await data.authorize_attempt_upload(db, run.id, attempt.id, body, inventory)
    if run.status not in {"lost", "cancelling"}:
        run.status = attempt.status = "uploading"
        await jobs.update_progress(
            db, run.async_job_id, 95, extra_payload={"stage": "uploading"}
        )
    await db.commit()
    return await storage_io.run(
        db,
        owner,
        "training_upload_urls",
        lambda transport: _upload_urls(transport, owner, attempt, inventory),
        source_boot=body.boot_id,
    )


def _upload_urls(transport, owner, attempt, inventory):
    result = {}
    for name, spec in inventory.items():
        key = artifact_key(owner, attempt.id, name)
        metadata = {"sha256": spec["sha256"], "attempt-id": str(attempt.id)}
        result[name] = storage_io.sign_upload(
            transport,
            owner.bucket,
            key,
            size=spec["size_bytes"],
            metadata=metadata,
            expires_in=600,
        )
    return result


def verify_artifact(key, spec, attempt_id, *, bucket, transport=None):
    transport = storage if transport is None else transport
    response = storage_io.get_object(transport, bucket, key)
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
        storage_access.close_registered(response["Body"])


def verify_native_artifact(run, attempt, artifacts, *, transport=None):
    transport = storage if transport is None else transport
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
    response = storage_io.get_object(transport, owner.bucket, receipt["key"])
    try:
        blob = json.loads(response["Body"].read(1024 * 1024 + 1))
    finally:
        storage_access.close_registered(response["Body"])
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


def verify_trainer_receipt_artifact(run, attempt, artifacts, *, transport=None):
    spec = artifacts.get("result.json")
    if not spec:
        raise ValueError("v3 success requires result.json trainer receipt")
    from app.services.training_artifacts import read_json_artifact

    owner = attempt_storage_owner(run, attempt, spec["key"])
    raw = read_json_artifact(spec, attempt.id, bucket=owner.bucket, transport=transport)
    receipt = TrainReceiptV1.model_validate(raw)
    if receipt.status != "succeeded":
        raise ValueError("Trainer receipt status must be succeeded")
    for item in receipt.artifacts:
        if item.role == "input_membership":
            continue
        uploaded = artifacts.get(item.filename)
        if (
            uploaded is None
            or uploaded.get("sha256") != item.sha256
            or uploaded.get("size_bytes") != item.size_bytes
        ):
            raise ValueError(f"Trainer receipt artifact mismatch: {item.filename}")


def verify_input_artifact(run, attempt, artifacts, *, transport=None):
    """Verify actual native admission through the existing saved-owner manifest."""
    if input_contract(run.config) is None:
        return
    spec = artifacts.get("manifest.json")
    if not spec or spec["role"] != "manifest":
        raise ValueError("Successful training requires its execution manifest")
    from app.services.training_artifacts import read_json_artifact

    owner = attempt_storage_owner(run, attempt, spec["key"])
    manifest = read_json_artifact(
        spec, attempt.id, bucket=owner.bucket, transport=transport
    )
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


async def finish(db, attempt_id, body, *, training_authority: TrainingAuthorityPort):
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
        await authority(
            training_authority,
            project_id=run.project_id,
            actor_id=run.creator_id,
            api_key_id=run.authorization.get("api_key_id"),
        )
    if attempt.run_id != run.id:
        raise HTTPException(status_code=409, detail="执行尝试不属于当前训练实验")
    owner = None
    if status == "succeeded" or getattr(run, "storage_ref", None) is not None:
        try:
            owner = attempt_storage_owner(run, attempt)
        except HTTPException as exc:
            if status == "succeeded" or exc.status_code != 409:
                raise
    artifacts = {}
    inventory = dict(attempt.upload_inventory) if owner else {}
    saved_ref = owner.ref if owner else None
    if owner and inventory:
        await db.commit()
    for name, spec in inventory.items():
        try:
            key = artifact_key(owner, attempt.id, name)
        except HTTPException:
            if status == "succeeded":
                raise
            continue
        try:
            await storage_io.run(
                db,
                owner,
                "training_artifact_verify",
                lambda transport: verify_artifact(
                    key, spec, attempt.id, bucket=owner.bucket, transport=transport
                ),
                source_boot=body.boot_id,
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
                await storage_io.run(
                    db,
                    owner,
                    "training_artifact_verify",
                    lambda transport: verify_summary(
                        run, attempt, artifacts[name], transport=transport
                    ),
                    source_boot=body.boot_id,
                )
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
            await storage_io.run(
                db,
                owner,
                "training_artifact_verify",
                lambda transport: verify_native_artifact(
                    run, attempt, artifacts, transport=transport
                ),
                source_boot=body.boot_id,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(
                status_code=422, detail="原生参数观测缺失或与冻结配置不符"
            ) from exc
    if status == "succeeded" and run.config.get("adapter_revision") == 3:
        try:
            await storage_io.run(
                db,
                owner,
                "training_artifact_verify",
                lambda transport: verify_trainer_receipt_artifact(
                    run, attempt, artifacts, transport=transport
                ),
                source_boot=body.boot_id,
            )
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise HTTPException(
                status_code=422, detail="训练器终态回执缺失或与已核验产物不符"
            ) from exc
    if status == "succeeded":
        try:
            await storage_io.run(
                db,
                owner,
                "training_artifact_verify",
                lambda transport: verify_input_artifact(
                    run, attempt, artifacts, transport=transport
                ),
                source_boot=body.boot_id,
            )
        except (ValueError, KeyError, TypeError, OSError) as exc:
            raise HTTPException(
                status_code=422, detail="训练实际接纳成员回执缺失或与冻结清单不符"
            ) from exc
    lease, run, attempt = await fenced(db, attempt_id, body, allow_terminal=True)
    if attempt.status in TERMINAL:
        if attempt.finish_request_digest != request_digest:
            raise HTTPException(status_code=409, detail="已结束的执行不能改写终态")
        return attempt
    if (
        lease.attempt_id != attempt.id
        or (owner is not None and attempt.upload_inventory != inventory)
        or (
            saved_ref is not None
            and attempt_storage_owner(run, attempt).ref != saved_ref
        )
    ):
        raise HTTPException(status_code=409, detail="产物核验期间执行归属或清单已变化")
    status = "cancelled" if run.status == "cancelling" else body.status
    if status == "succeeded":
        await authority(
            training_authority,
            project_id=run.project_id,
            actor_id=run.creator_id,
            api_key_id=run.authorization.get("api_key_id"),
        )
    failure = None
    if body.failure:
        failure = {
            **body.failure.model_dump(mode="json"),
            "diagnostic": redact_text(
                body.failure.diagnostic,
                (settings.training_executor_token, body.lease_token),
            )[:1000],
        }
    error = (
        redact_text(
            body.error_message, (settings.training_executor_token, body.lease_token)
        )[:1000]
        if body.error_message
        else None
    )
    at = now()
    await data.finish_executor_attempt(
        db,
        run.id,
        attempt.id,
        body,
        status,
        at,
        error,
        accounting=body.native_accounting,
        failure=failure,
        finish_digest=request_digest,
        artifacts=artifacts,
    )
    attempt.artifacts = artifacts
    attempt.native_accounting = body.native_accounting
    attempt.finish_request_digest = request_digest
    if failure is not None:
        attempt.failure = failure
    await _terminal_job(db, run, attempt, status, at, error)
    return attempt


async def admit(
    db,
    project_id,
    user: LearningActorContext | IdentityActorFacts,
    body: TrainingRunCreate,
    *,
    api_key_id=None,
):
    """Return the run and whether this admission created state to commit."""
    await claims.project_lock(db, project_id)
    await lock_project_manager(db, user.id, project_id)
    request_digest = content_digest(body.request_payload())
    existing = await data.run_request(db, project_id, body.request_id)
    if existing:
        if existing.request_digest != request_digest:
            raise HTTPException(status_code=409, detail="同一请求标识不能用于不同实验")
        return existing, False
    config = await frozen_config(db, project_id, body)
    origin = await recipes.origin_snapshot(db, project_id, body.origin, config)
    if body.preflight_digest != content_digest(config):
        raise HTTPException(
            status_code=409, detail="训练配置已变化或尚未检查，请重新预检"
        )
    run = await data.create_run(
        db,
        project_id=project_id,
        creator_id=user.id,
        dataset_id=body.dataset_id,
        initialization_model_id=initialization.model_id(config.get("initialization")),
        request_id=body.request_id,
        request_digest=request_digest,
        name=body.name,
        config=config,
        config_digest=content_digest(config),
        origin=origin,
        authorization={"api_key_id": api_key_id},
    )
    await allocate_storage(db, run)
    await new_attempt(db, run)
    return run, True


async def reserve_study_budget(db, attempt, run, allowance):
    from training_contract.budget import POLICY_DIGEST, validate_budget

    budget = {
        "policy_digest": POLICY_DIGEST,
        "reservation_id": str(uuid.uuid4()),
        "attempt_id": str(attempt.id),
        "config_digest": run.config_digest,
        "allowance_seconds": float(allowance),
    }
    validate_budget(budget, attempt.id, run.config_digest)
    await data.reserve_study_attempt(db, run.id, attempt.id, budget)
    attempt.native_budget = budget


async def create_study_child(
    db,
    *,
    project_id,
    creator_id,
    study_id,
    dataset_id,
    initialization_model_id,
    request_id,
    request_digest,
    name,
    config,
    config_digest,
    origin,
    authorization,
    allowance,
):
    run = await data.create_run(
        db,
        project_id=project_id,
        creator_id=creator_id,
        study_id=study_id,
        dataset_id=dataset_id,
        initialization_model_id=initialization_model_id,
        request_id=request_id,
        request_digest=request_digest,
        name=name,
        config=config,
        config_digest=config_digest,
        origin=origin,
        authorization=authorization,
    )
    await allocate_storage(db, run)
    attempt = await new_attempt(db, run)
    await reserve_study_budget(db, attempt, run, allowance)
    return run


async def create_policy_child(
    db,
    *,
    project_id,
    creator_id,
    dataset_id,
    initialization_model_id,
    auto_trigger_id,
    request_digest,
    name,
    config,
    config_digest,
    origin,
    authorization,
):
    run = await data.create_run(
        db,
        project_id=project_id,
        creator_id=creator_id,
        dataset_id=dataset_id,
        initialization_model_id=initialization_model_id,
        auto_trigger_id=auto_trigger_id,
        request_id=auto_trigger_id,
        request_digest=request_digest,
        name=name,
        config=config,
        config_digest=config_digest,
        origin=origin,
        authorization=authorization,
    )
    await allocate_storage(db, run)
    await new_attempt(db, run)
    return run


async def cancel_study_child(db, run):
    if run.status == "queued":
        attempt = await data.get_attempt(db, run.current_attempt_id)
        await mark_terminal(
            db, run, attempt, "cancelled", "参数搜索已停止，未启动的试验已取消"
        )
    elif run.status in ACTIVE:
        attempt = await data.get_attempt(db, run.current_attempt_id)
        await data.cancel_attempt(db, run, attempt)
        run.status = attempt.status = "cancelling"


async def mark_study_manual_resume(db, run: data.RunFacts) -> None:
    await data.manual_resume(db, run.id, run.current_attempt_id, study=True)
    run.authorization = {**run.authorization, "study_manual_resume": True}


async def mark_policy_manual_resume(db, run: data.RunFacts) -> None:
    await data.manual_resume(db, run.id, run.current_attempt_id, study=False)
    run.authorization = {**run.authorization, "manual_resume": True}


async def cancel(
    db, project_id, run_id, user: LearningActorContext | IdentityActorFacts
):
    await lock_project_manager(db, user.id, project_id)
    run = await load_run(db, project_id, run_id, lock=True)
    attempt = await data.get_attempt(db, run.current_attempt_id)
    if run.status in TERMINAL:
        raise HTTPException(status_code=409, detail="训练实验已结束")
    if run.status == "queued":
        await mark_terminal(db, run, attempt, "cancelled")
    else:
        await data.cancel_attempt(db, run, attempt)
        run.status = attempt.status = "cancelling"
    return run


async def recover(db, attempt_id, body: ExecutorRecovery) -> None:
    if body.new_boot_id == body.boot_id:
        raise HTTPException(status_code=422, detail="恢复需要新的执行器启动身份")
    lease, run, attempt = await fenced(db, attempt_id, body, allow_terminal=True)
    if attempt.status not in TERMINAL:
        if lease.attempt_id != attempt.id:
            raise HTTPException(status_code=409, detail="GPU 租约已变化")
        status = "cancelled" if run.status == "cancelling" else "failed"
        error = "执行器重启，已通过独占资源锁确认旧进程退出"
        at = now()
        failure = failure_receipt(
            "execution_lost",
            "execution",
            run.config_digest,
            error,
            source="platform",
        )
        await data.finish_executor_attempt(
            db,
            run.id,
            attempt.id,
            body,
            status,
            at,
            error,
            failure=failure,
            recovery_boot=body.new_boot_id,
        )
        await _terminal_job(db, run, attempt, status, at, error)
