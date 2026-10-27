"""Qualified adapter capabilities, configured catalog and frozen configuration."""

from fastapi import HTTPException
from training_contract import (
    ADAPTERS,  # re-export the shared legacy identity
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

from app.config import settings


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
