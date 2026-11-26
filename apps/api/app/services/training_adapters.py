"""Qualified adapter capabilities, configured catalog and frozen configuration."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from fastapi import HTTPException
from training_contract import (
    ADAPTERS,
    definition,
    resolve,
    schema_identity,
)
from training_contract.assets import AssetReference
from training_contract.catalog import instance_rle_capability
from training_contract.dataset import DETECTION, SEGMENTATION
from training_contract.models import (
    LEGACY_ULTRALYTICS_VERSION,
    ULTRALYTICS_VERSION,
    model_spec,
    supports_version,
)
from training_contract.trainer_plugin import (
    TRAINER_PROTOCOL_VERSION,
    canonical_json_digest,
)
from yoao_inference_contract.topology import to_record
from yoao_task_contract import normalize_task

from app.config import settings
from app.schemas.trainer_plugin import TrainerPluginRecord
from app.services.trainer_plugins_data import TrainerPluginsPostgresData

FRAMEWORK_ID_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
BRIDGE_ID_PATTERN = r"^[a-z][a-z0-9_-]{0,127}$"
_FRAMEWORK_ID_RE = re.compile(FRAMEWORK_ID_PATTERN)

_BUILTIN_STUDY_OBJECTIVES: dict[str, frozenset[str]] = {
    "ultralytics": frozenset(
        {
            "metrics/mAP50-95",
            "metrics/mAP50",
            "metrics/precision",
            "metrics/recall",
        }
    ),
    "libreyolo": frozenset(
        {
            "metrics/mAP50-95",
            "metrics/mAP50",
            "metrics/mAP75",
            "metrics/AR100",
        }
    ),
}
_BUILTIN_MANAGED_EVALUATION_FRAMEWORKS: frozenset[str] = frozenset({"ultralytics"})
_BUILTIN_DATASET_BRIDGES: dict[str, frozenset[str]] = {
    DETECTION: frozenset({"ultralytics-bbox-1"}),
    SEGMENTATION: frozenset(
        {"ultralytics-simple-polygon-1", "ultralytics-instance-rle-1"}
    ),
}


def registered_frameworks() -> frozenset[str]:
    return frozenset(ADAPTERS.keys())


def validate_registered_framework(value: str) -> str:
    if not isinstance(value, str) or not _FRAMEWORK_ID_RE.fullmatch(value):
        raise ValueError("训练框架标识格式无效")
    if value not in registered_frameworks():
        raise ValueError(f"未注册的训练框架: {value}")
    return value


def supported_dataset_bridges(task: str) -> frozenset[str]:
    return _BUILTIN_DATASET_BRIDGES.get(normalize_task(task), frozenset())


def supported_study_objectives(framework: str) -> frozenset[str] | None:
    return _BUILTIN_STUDY_OBJECTIVES.get(framework)


def supports_task_evaluation(framework: str, task: str) -> bool:
    normalized = normalize_task(task)
    if normalized == DETECTION:
        return True
    if framework not in ADAPTERS:
        return True
    return normalized in capabilities(framework)["tasks"]


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
    if framework in _BUILTIN_MANAGED_EVALUATION_FRAMEWORKS:
        return True
    return backend_supports_managed_evaluation(backend, framework)


def capabilities(framework, task=None, base_model=None):
    adapter = ADAPTERS[framework]
    try:
        segment_return = (
            framework == "ultralytics"
            and model_spec(framework, base_model).task == SEGMENTATION
        )
    except (ValueError, TypeError):
        segment_return = False
    return {
        "tasks": [task]
        if task
        else ([DETECTION, SEGMENTATION] if framework == "ultralytics" else [DETECTION]),
        "resume": True,
        "artifact_download": True,
        "model_registration": adapter["model_registration"]
        and (task != SEGMENTATION or segment_return),
    }


def fixed_options(framework):
    common = {
        "device": 0,
        "workers": 2,
        "amp": False,
        "cache": False,
        "deterministic": True,
        "optimizer": "SGD",
    }
    if framework == "libreyolo":
        common.update(
            device="cuda:0",
            eval_interval=1,
            save_period=0,
            save_plots=False,
            cuda_graph=False,
            compile=False,
            allow_download_scripts=False,
            opencv_threads=1,
            torch_threads=4,
            torch_interop_threads=4,
        )
    return common


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
        schema_id = str(
            raw.get("schema_id") or f"{record.framework_id}/{task}/{model_family}/v1"
        )
        schema_digest = str(raw.get("schema_digest") or canonical_json_digest(raw))
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
            "fields": list(raw["fields"]),
        }
    fields: list[dict[str, Any]] = []
    if isinstance(raw, dict) and isinstance(raw.get("properties"), dict):
        for key, prop in raw["properties"].items():
            if not isinstance(prop, dict):
                continue
            rules = {
                rule_key: prop[rule_key]
                for rule_key in (
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
    image_digest = (
        record.artifact.split("@", 1)[1]
        if record.artifact and "@sha256:" in record.artifact
        else None
    )
    supported_dataset_contracts = list(caps.get("supported_dataset_contracts", []))
    can_register = bool(caps.get("serving_compatibility")) or (
        "export_weights" in record.capabilities
    )
    base_models: list[dict[str, Any]] = []
    schemas: dict[str, dict[str, Any]] = {}
    has_schema = isinstance(caps.get("parameter_schema"), dict) and bool(
        caps.get("parameter_schema")
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
            schemas[schema["schema_id"]] = {
                "schema_id": schema["schema_id"],
                "schema_digest": schema["schema_digest"],
                "framework": schema["framework"],
                "framework_version": schema["framework_version"],
                "adapter_revision": schema["adapter_revision"],
                "task": schema["task"],
                "model_family": schema["model_family"],
                "field_count": len(schema["fields"]),
                "editable_count": sum(
                    field.get("status") == "editable" for field in schema["fields"]
                ),
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
    legacy = catalog()
    plugin_items: list[dict[str, Any]] = []
    if db is not None and hasattr(db, "scalar"):
        enabled_plugins = await TrainerPluginsPostgresData(db).list_plugins(
            include_retired=False, status="enabled"
        )
        plugin_items = [plugin_catalog_item(record) for record in enabled_plugins]
    plugin_frameworks = {item["framework"] for item in plugin_items}
    merged_items = [
        *plugin_items,
        *[
            item
            for item in legacy["items"]
            if item["framework"] not in plugin_frameworks
        ],
    ]
    configured = training_executor_configured()
    return {
        "enabled": bool(configured and merged_items),
        "resource_id": resource_id() if configured else None,
        "items": merged_items,
    }


async def ensure_known_framework(
    db: Any, framework: str, *, require_enabled_plugin: bool = False
) -> None:
    if framework in ADAPTERS:
        return
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


def catalog():
    configured = settings.training_catalog_json
    gpu_configured = any(
        slot["device_kind"] == "gpu" for slot in configured_resource_slots()
    )
    enabled = bool(
        settings.training_enabled
        and len(settings.training_executor_token) >= 32
        and gpu_configured
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
