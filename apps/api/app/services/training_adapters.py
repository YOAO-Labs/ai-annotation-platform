"""Plugin-backed trainer catalog, resource slot selection, and parameter resolution."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from fastapi import HTTPException
from training_contract.dataset import DETECTION, SEGMENTATION, is_instance_rle
from training_contract.trainer_plugin import (
    TRAINER_PROTOCOL_VERSION,
    canonical_json_digest,
)
from training_contract.validation import validate_value
from yoao_inference_contract.topology import to_record
from yoao_task_contract import normalize_task

from app.config import settings
from app.schemas.trainer_plugin import TrainerPluginRecord
from app.services.trainer_plugins_data import TrainerPluginsPostgresData

FRAMEWORK_ID_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
BRIDGE_ID_PATTERN = r"^[a-z][a-z0-9_-]{0,127}$"
_FRAMEWORK_ID_RE = re.compile(FRAMEWORK_ID_PATTERN)
_JSON_SCHEMA_PRIMITIVE_TYPES = frozenset(
    {"null", "boolean", "integer", "number", "string", "array"}
)

_BUILTIN_DATASET_BRIDGES: dict[str, frozenset[str]] = {
    DETECTION: frozenset({"ultralytics-bbox-1"}),
    SEGMENTATION: frozenset(
        {"ultralytics-simple-polygon-1", "ultralytics-instance-rle-1"}
    ),
}


def supported_dataset_bridges(task: str) -> frozenset[str]:
    return _BUILTIN_DATASET_BRIDGES.get(normalize_task(task), frozenset())


def supports_task_evaluation(framework: str, task: str) -> bool:
    if not isinstance(framework, str) or not _FRAMEWORK_ID_RE.fullmatch(framework):
        return False
    return normalize_task(task) in {DETECTION, SEGMENTATION}


def supports_deployment_retirement(framework: str) -> bool:
    return isinstance(framework, str) and bool(_FRAMEWORK_ID_RE.fullmatch(framework))


def backend_supports_managed_evaluation(backend: Any, framework: str) -> bool:
    if backend is None:
        return False
    raw_health = getattr(backend, "health_meta", None)
    if not isinstance(raw_health, Mapping):
        return False
    raw_caps = raw_health.get("capabilities")
    if not isinstance(raw_caps, Mapping):
        return False
    caps = to_record(raw_caps)
    if not isinstance(caps, dict) or caps.get("managed_evaluation") is not True:
        return False
    observed = caps.get("managed_model_compatibility")
    if not isinstance(observed, list):
        return False
    raw_params = getattr(backend, "extra_params", None)
    params = to_record(raw_params) if isinstance(raw_params, Mapping) else {}
    plugin = params.get("plugin") if isinstance(params, dict) else None
    if plugin is not None:
        if not isinstance(plugin, dict) or not isinstance(plugin.get("manifest"), dict):
            return False
        manifest = plugin["manifest"]
        approved_capabilities = manifest.get("capabilities")
        if (
            not isinstance(approved_capabilities, list)
            or "managed_evaluation" not in approved_capabilities
        ):
            return False
        approved = manifest.get("managed_model_compatibility")
        if not isinstance(approved, list):
            return False
        return any(
            isinstance(entry, dict)
            and entry.get("framework") == framework
            and entry in observed
            for entry in approved
        )
    return any(
        isinstance(entry, dict) and entry.get("framework") == framework
        for entry in observed
    )


def supports_managed_evaluation(framework: str, *, backend: Any = None) -> bool:
    return backend_supports_managed_evaluation(backend, framework)


def configured_resource_slots() -> tuple[dict[str, Any], ...]:
    node = settings.training_node_id
    slots: list[dict[str, Any]] = []
    seen_gpu: set[str] = set()
    candidates: list[str] = []
    primary = settings.training_gpu_uuid.strip()
    if primary:
        candidates.append(primary)
    for item in settings.training_gpu_uuids.split(","):
        cleaned = item.strip()
        if cleaned:
            candidates.append(cleaned)
    for gpu_uuid in candidates:
        if gpu_uuid.startswith("GPU-") and gpu_uuid not in seen_gpu:
            seen_gpu.add(gpu_uuid)
            slots.append(
                {
                    "resource_id": f"{node}/{gpu_uuid}",
                    "device_kind": "gpu",
                    "gpu_uuid": gpu_uuid,
                }
            )
    for index in range(max(0, int(settings.training_cpu_slots))):
        slots.append(
            {
                "resource_id": f"{node}/cpu-{index}",
                "device_kind": "cpu",
                "gpu_uuid": None,
            }
        )
    return tuple(slots)


def training_executor_configured() -> bool:
    return bool(
        settings.training_enabled
        and len(settings.training_executor_token) >= 32
        and configured_resource_slots()
    )


def select_resource_slot(*, requires_gpu: bool) -> dict[str, Any] | None:
    slots = configured_resource_slots()
    if requires_gpu:
        return next((slot for slot in slots if slot["device_kind"] == "gpu"), None)
    cpu_slot = next((slot for slot in slots if slot["device_kind"] == "cpu"), None)
    if cpu_slot is not None:
        return cpu_slot
    return next((slot for slot in slots if slot["device_kind"] == "gpu"), None)


def resource_id():
    primary = settings.training_gpu_uuid.strip()
    if primary.startswith("GPU-"):
        return f"{settings.training_node_id}/{primary}"
    slots = configured_resource_slots()
    if slots:
        return slots[0]["resource_id"]
    return f"{settings.training_node_id}/{settings.training_gpu_uuid}"


def plugin_requires_gpu(record: TrainerPluginRecord) -> bool:
    extension = (
        record.manifest.get("extension") if isinstance(record.manifest, dict) else None
    )
    if isinstance(extension, dict) and isinstance(extension.get("requires_gpu"), bool):
        return extension["requires_gpu"]
    return True


def plugin_image_digest(record: TrainerPluginRecord) -> str | None:
    if record.artifact and "@sha256:" in record.artifact:
        return record.artifact.split("@", 1)[1]
    return None


def plugin_parameter_schema_payload(
    record: TrainerPluginRecord,
    *,
    base_model: str,
    task: str,
    model_family: str,
) -> dict[str, Any]:
    caps = record.capabilities_snapshot
    framework_version = str(caps.get("framework_version") or record.version)
    raw = caps.get("parameter_schema")
    source_label = f"{record.plugin_id}@{record.version}"
    if isinstance(raw, dict) and isinstance(raw.get("fields"), list):
        sub_schemas = raw.get("schemas")
        sub_entry = (
            sub_schemas.get(f"{model_family}:{task}")
            if isinstance(sub_schemas, dict)
            else None
        )
        selected_fields = (
            list(sub_entry["fields"])
            if isinstance(sub_entry, dict) and isinstance(sub_entry.get("fields"), list)
            else list(raw["fields"])
        )
        schema_id = str(
            (sub_entry.get("schema_id") if isinstance(sub_entry, dict) else None)
            or raw.get("schema_id")
            or f"{record.framework_id}/{task}/{model_family}/v1"
        )
        schema_digest = str(
            (sub_entry.get("schema_digest") if isinstance(sub_entry, dict) else None)
            or raw.get("schema_digest")
            or canonical_json_digest(raw)
        )
        return {
            "schema_id": schema_id,
            "schema_digest": schema_digest,
            "framework": record.framework_id,
            "framework_version": framework_version,
            "adapter_revision": 3,
            "task": task,
            "model_family": model_family,
            "source": str(raw.get("source") or source_label),
            "source_sha256": str(
                raw.get("source_sha256") or record.capabilities_digest
            ),
            "groups": (
                dict(raw["groups"])
                if isinstance(raw.get("groups"), dict)
                else {"general": "General"}
            ),
            "fields": selected_fields,
        }
    fields: list[dict[str, Any]] = []
    if isinstance(raw, dict) and isinstance(raw.get("properties"), dict):
        for key, prop in raw["properties"].items():
            if not isinstance(prop, dict):
                continue
            rules = {
                rule_key: prop[rule_key]
                for rule_key in (
                    "type",
                    "minimum",
                    "maximum",
                    "exclusiveMinimum",
                    "exclusiveMaximum",
                    "enum",
                    "multipleOf",
                )
                if rule_key in prop
            }
            default_value = prop.get("default")
            fields.append(
                {
                    "key": str(key),
                    "label": str(prop.get("title") or key),
                    "group": "general",
                    "status": "editable",
                    "native_type": str(prop.get("type") or "string"),
                    "default": default_value,
                    "native_default": default_value,
                    "defined_in": source_label,
                    "reason": str(prop.get("description") or ""),
                    "rules": rules,
                    "aliases": [],
                }
            )
    schema_digest = (
        canonical_json_digest(raw)
        if isinstance(raw, dict) and raw
        else record.capabilities_digest
    )
    return {
        "schema_id": f"{record.framework_id}/{task}/{model_family}/v1",
        "schema_digest": schema_digest,
        "framework": record.framework_id,
        "framework_version": framework_version,
        "adapter_revision": 3,
        "task": task,
        "model_family": model_family,
        "source": source_label,
        "source_sha256": record.capabilities_digest,
        "groups": {"general": "General"},
        "fields": fields,
    }


def plugin_catalog_item(record: TrainerPluginRecord) -> dict[str, Any]:
    caps = record.capabilities_snapshot
    framework_id = record.framework_id
    version = str(caps.get("framework_version") or record.version)
    requires_gpu = plugin_requires_gpu(record)
    image_digest = plugin_image_digest(record)
    supported_dataset_contracts = list(caps.get("supported_dataset_contracts", []))
    can_register = bool(caps.get("serving_compatibility")) or (
        "export_weights" in record.capabilities
    )
    base_models: list[dict[str, Any]] = []
    schemas: dict[str, dict[str, Any]] = {}
    raw_param_schema = caps.get("parameter_schema")
    has_schema = isinstance(raw_param_schema, dict) and bool(raw_param_schema)
    sub_schemas = (
        raw_param_schema.get("schemas")
        if isinstance(raw_param_schema, dict)
        and isinstance(raw_param_schema.get("schemas"), dict)
        else {}
    )
    for arch in caps.get("architectures", []):
        tasks = list(arch.get("tasks", []))
        primary_task = tasks[0] if tasks else DETECTION
        base_models.append(
            {
                "id": arch["id"],
                "sha256": arch.get("base_asset_sha256"),
                "filename": arch.get("base_asset_filename"),
                "task": primary_task,
                "model_family": arch["family"],
                "model_size": None,
                "capabilities": {
                    "tasks": tasks,
                    "resume": True,
                    "artifact_download": True,
                    "model_registration": can_register,
                },
            }
        )
        if has_schema:
            schema = plugin_parameter_schema_payload(
                record,
                base_model=arch["id"],
                task=primary_task,
                model_family=arch["family"],
            )
            sub_entry = sub_schemas.get(f"{arch['family']}:{primary_task}")
            field_count = (
                int(sub_entry["field_count"])
                if isinstance(sub_entry, dict)
                and isinstance(sub_entry.get("field_count"), int)
                else len(schema["fields"])
            )
            editable_count = (
                int(sub_entry["editable_count"])
                if isinstance(sub_entry, dict)
                and isinstance(sub_entry.get("editable_count"), int)
                else sum(
                    field.get("status") == "editable" for field in schema["fields"]
                )
            )
            schemas[schema["schema_id"]] = {
                "schema_id": schema["schema_id"],
                "schema_digest": schema["schema_digest"],
                "framework": schema["framework"],
                "framework_version": schema["framework_version"],
                "adapter_revision": schema["adapter_revision"],
                "task": schema["task"],
                "model_family": schema["model_family"],
                "field_count": field_count,
                "editable_count": editable_count,
            }
    return {
        "framework": framework_id,
        "version": version,
        "image_digest": image_digest,
        "plugin_id": record.plugin_id,
        "execution_mode": record.execution_mode,
        "protocol_version": record.protocol_version or TRAINER_PROTOCOL_VERSION,
        "requires_gpu": requires_gpu,
        "supported_dataset_contracts": supported_dataset_contracts,
        "base_models": base_models,
        "capabilities": {
            "tasks": list(caps.get("supported_tasks", [])),
            "resume": True,
            "artifact_download": True,
            "model_registration": can_register,
        },
        "parameter_schemas": list(schemas.values()),
    }


async def resolve_catalog(db: Any) -> dict[str, Any]:
    plugin_items: list[dict[str, Any]] = []
    if db is not None and hasattr(db, "scalar"):
        enabled_plugins = await TrainerPluginsPostgresData(db).list_plugins(
            include_retired=False, status="enabled"
        )
        plugin_items = [plugin_catalog_item(record) for record in enabled_plugins]
    configured = training_executor_configured()
    return {
        "enabled": bool(configured and plugin_items),
        "resource_id": resource_id() if configured else None,
        "items": plugin_items,
    }


async def ensure_known_framework(
    db: Any, framework: str, *, require_enabled_plugin: bool = False
) -> None:
    if not isinstance(framework, str) or not _FRAMEWORK_ID_RE.fullmatch(framework):
        raise HTTPException(status_code=422, detail="训练框架标识格式无效")
    if db is None or not hasattr(db, "scalar"):
        raise HTTPException(status_code=422, detail=f"未注册的训练框架: {framework}")
    store = TrainerPluginsPostgresData(db)
    if require_enabled_plugin:
        if await store.get_enabled_by_framework(framework) is None:
            raise HTTPException(
                status_code=422, detail=f"未注册的训练框架: {framework}"
            )
        return
    plugins = await store.list_plugins(include_retired=True)
    if not any(record.framework_id == framework for record in plugins):
        raise HTTPException(status_code=422, detail=f"未注册的训练框架: {framework}")


async def _enabled_plugin_and_arch(
    db: Any, framework: str, base_model: str
) -> tuple[TrainerPluginRecord, dict[str, Any]]:
    if db is None or not hasattr(db, "scalar"):
        raise LookupError("训练器插件存储不可用")
    plugin = await TrainerPluginsPostgresData(db).get_enabled_by_framework(framework)
    if plugin is None:
        raise LookupError(f"未启用训练器插件: {framework}")
    arch = next(
        (
            item
            for item in plugin.capabilities_snapshot.get("architectures", [])
            if item.get("id") == base_model
        ),
        None,
    )
    if arch is None:
        raise LookupError(f"基础权重不在当前训练器目录中: {base_model}")
    return plugin, arch


async def plugin_template(db: Any, framework: str, base_model: str) -> dict[str, Any]:
    if not training_executor_configured():
        raise HTTPException(status_code=409, detail="训练器或基础权重尚未配置")
    try:
        plugin, arch = await _enabled_plugin_and_arch(db, framework, base_model)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail="训练器或基础权重尚未配置") from exc
    slot = select_resource_slot(requires_gpu=plugin_requires_gpu(plugin))
    if slot is None:
        raise HTTPException(status_code=409, detail="没有满足训练器资源需求的执行槽位")
    tasks = list(
        arch.get("tasks")
        or plugin.capabilities_snapshot.get("supported_tasks")
        or [DETECTION]
    )
    return {
        "framework": plugin.framework_id,
        "framework_version": str(
            plugin.capabilities_snapshot.get("framework_version") or plugin.version
        ),
        "image_digest": plugin_image_digest(plugin),
        "base_model": {
            "id": arch["id"],
            "family": arch["family"],
            "sha256": arch.get("base_asset_sha256"),
            "filename": arch.get("base_asset_filename"),
        },
        "resource_id": slot["resource_id"],
        "task": tasks[0],
        "model_family": arch["family"],
        "plugin_id": plugin.plugin_id,
        "execution_mode": plugin.execution_mode,
        "capabilities_digest": plugin.capabilities_digest,
    }


async def plugin_parameter_schema(
    db: Any, framework: str, base_model: str
) -> dict[str, Any]:
    try:
        plugin, arch = await _enabled_plugin_and_arch(db, framework, base_model)
    except LookupError as exc:
        raise HTTPException(
            status_code=422, detail="基础权重不在当前受控目录中"
        ) from exc
    tasks = list(
        arch.get("tasks")
        or plugin.capabilities_snapshot.get("supported_tasks")
        or [DETECTION]
    )
    return plugin_parameter_schema_payload(
        plugin,
        base_model=arch["id"],
        task=tasks[0],
        model_family=arch["family"],
    )


def _validate_field_value(field: Mapping[str, Any], value: Any) -> None:
    raw_rules = field.get("rules")
    rule = dict(raw_rules) if isinstance(raw_rules, Mapping) else {}
    if not rule:
        declared = field.get("type") or field.get("native_type")
        if declared in _JSON_SCHEMA_PRIMITIVE_TYPES:
            rule = {"type": declared}
    elif "type" not in rule and "enum" not in rule and "anyOf" not in rule:
        declared = field.get("type") or field.get("native_type")
        if declared in _JSON_SCHEMA_PRIMITIVE_TYPES:
            rule["type"] = declared
    if rule:
        exclusive_max = rule.pop("exclusiveMaximum", None)
        validate_value(value, rule)
        if exclusive_max is not None and not (value < exclusive_max):
            raise ValueError(f"不满足 exclusiveMaximum={exclusive_max}")


def resolve_plugin_configuration_for_schema(
    schema: Mapping[str, Any],
    configuration: Mapping[str, Any],
    *,
    data_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(configuration, Mapping):
        raise ValueError("参数配置格式无效")
    if (
        configuration.get("schema_id") != schema["schema_id"]
        or configuration.get("schema_digest") != schema["schema_digest"]
    ):
        raise ValueError("参数配置与当前训练器定义不匹配")
    submitted = configuration.get("values")
    if not isinstance(submitted, Mapping):
        raise ValueError("参数配置 values 必须为对象")
    fields = schema.get("fields")
    if not isinstance(fields, list):
        raise ValueError("训练器参数定义缺少字段列表")

    fields_by_key: dict[str, dict[str, Any]] = {}
    alias_to_key: dict[str, str] = {}
    for item in fields:
        if not isinstance(item, Mapping) or "key" not in item:
            continue
        field = dict(item)
        key = str(field["key"])
        fields_by_key[key] = field
        for alias in field.get("aliases") or ():
            alias_to_key[str(alias)] = key

    user_values: dict[str, Any] = {}
    for raw_key, value in submitted.items():
        key = str(raw_key)
        canonical_key = key if key in fields_by_key else alias_to_key.get(key)
        if canonical_key is None:
            raise ValueError(f"参数 {key} 不在当前训练器参数定义中")
        if canonical_key in user_values:
            raise ValueError(f"参数 {canonical_key} 不能通过别名重复提交")
        field = fields_by_key[canonical_key]
        status = field.get("status", "editable")
        if status != "editable" or field.get("editable") is False:
            raise ValueError(f"参数 {canonical_key} 为只读字段，不允许覆盖")
        try:
            _validate_field_value(field, value)
        except ValueError as exc:
            raise ValueError(f"{canonical_key}: {exc}") from exc
        user_values[canonical_key] = value

    effective_values: dict[str, Any] = {}
    origins: dict[str, str] = {}
    inactive_fields: list[str] = []
    for key, field in fields_by_key.items():
        status = field.get("status", "editable")
        default_value = field.get("default")
        if key in user_values:
            effective_values[key] = user_values[key]
            origins[key] = "user"
        elif status == "platform-managed":
            effective_values[key] = default_value
            origins[key] = "platform"
        elif status == "editable":
            effective_values[key] = default_value
            origins[key] = "native_default"
        else:
            effective_values[key] = default_value
            origins[key] = "inactive"
            inactive_fields.append(key)

    if data_contract and is_instance_rle(data_contract):
        if "workers" in user_values:
            raise ValueError("Instance-RLE 数据契约禁止覆盖 workers")
        if "workers" in effective_values:
            effective_values["workers"] = 0
            origins["workers"] = "platform:instance_rle"

    identity = {
        key: schema[key]
        for key in (
            "schema_id",
            "schema_digest",
            "framework",
            "framework_version",
            "adapter_revision",
            "task",
            "model_family",
        )
    }
    return {
        "identity": identity,
        "user_values": user_values,
        "effective_values": effective_values,
        "origins": origins,
        "inactive_fields": inactive_fields,
    }


async def resolve_plugin_configuration(
    db: Any,
    framework: str,
    base_model: str,
    configuration: Mapping[str, Any],
    *,
    data_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    schema = await plugin_parameter_schema(db, framework, base_model)
    try:
        return resolve_plugin_configuration_for_schema(
            schema, configuration, data_contract=data_contract
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
