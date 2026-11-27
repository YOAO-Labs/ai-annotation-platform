"""Freeze and lock model identity for either mask-repair source."""

import uuid

from yoao_inference_contract.topology import to_record

from app.services.content_snapshot import ContentError
from app.services.inference_topology_data import InferenceTopologyData
from app.services.mask_repair_ledger import canonical_digest
from app.services.ml_backend import MLBackendService
from app.services.ml_interaction_proxy import prepare_interactive_context
from app.services.prediction_execution import freeze_builtin_defaults


def declared_model(capabilities, model_id):
    capabilities = to_record(capabilities)
    return next(
        (
            row
            for row in (capabilities or {}).get("models", [])
            if row.get("id") == model_id
        ),
        None,
    )


def _prompt(item):
    box = item.get("region_bbox") or {}
    return {
        "type": "interactive_box",
        "bbox": [box.get(k) for k in ("x0", "y0", "x1", "y1")],
        "output_geometry": "mask",
        **(
            {"model_id": item["action"]["model_id"]}
            if item["action"].get("model_id")
            else {}
        ),
    }


async def freeze_model(db, task, item):
    """Freeze the declared model and effective defaults for either source kind."""
    service = MLBackendService(db)
    backend = await service.get_registry(uuid.UUID(str(item["action"]["backend_id"])))
    if backend is None or not await service.is_enabled(task.project_id, backend.id):
        raise ContentError("sam_backend_unavailable")
    capabilities = to_record((backend.health_meta or {}).get("capabilities") or {})
    context, model_id = prepare_interactive_context(
        _prompt(item),
        capabilities,
        task_id=str(task.id),
        requested_backend_id=str(backend.id),
    )
    model = declared_model(capabilities, model_id)
    stage = {
        "model_variants": context.get("model_variants"),
        "params": context.get("params"),
    }
    freeze_builtin_defaults(backend, model, stage)
    context.update(
        {key: stage[key] for key in ("model_variants", "params") if stage.get(key)}
    )
    return {
        "backend_id": str(backend.id),
        "pool_id": str(pool)
        if (pool := await service.pool_id_for_registry(backend.id))
        else None,
        "model_id": model_id,
        "context": context,
        "model_digest": canonical_digest(model),
        "identity_guarantee": "declared_configuration",
    }


async def lock_model(db, project_id, model, route):
    """Keep enablement and routing identity stable through the short write."""
    pool_id = uuid.UUID(model["pool_id"]) if model["pool_id"] else None
    if pool_id is None or route is None or route["pool_id"] != str(pool_id):
        raise ContentError("repair_model_changed")
    status = await InferenceTopologyData(db).lock_repair_model_rows(
        project_id,
        pool_id,
        (uuid.UUID(model["backend_id"]), uuid.UUID(route["instance_id"])),
    )
    if status == "backend_unavailable":
        raise ContentError("sam_backend_unavailable")
    if status != "ready":
        raise ContentError("repair_model_changed")
