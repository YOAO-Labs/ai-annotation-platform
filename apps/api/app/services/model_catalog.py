"""Read-only projections of built-in contracts and immutable project models."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from aap_protocol_v2.backend_auth import managed_provider
from aap_protocol_v2.model_metadata import ModelMetadataResponse, classes_digest
from aap_inference_contract.topology import to_record
from aap_task_contract import normalize_task
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.config import settings
from app.db.models.model_decision import ModelDecision
from app.db.models.trained_model import (
    ModelDeployment,
    ModelRelease,
    ProjectModelSelection,
    TrainedModelVersion,
)
from app.schemas.ml_backend import ModelCapability
from app.schemas.model_catalog import (
    CatalogClassMetadata,
    CatalogCurrentRelease,
    CatalogEntry,
    CatalogOperation,
    CatalogPlacement,
    CatalogProject,
    ModelCatalogResponse,
)
from app.services import trained_models
from aap_inference.pool_capability import capability_fingerprint
from app.services.ml_routing.contracts import RouterMode
from app.services.ml_routing.router import _router_mode
from app.services.inference_topology_data import InferenceTopologyData
from app.services.inference_backend_control import backend_control_service
from app.services.workspace_read_relations import project_facts


def operation(allowed, code=None, reason=None):
    return CatalogOperation(
        allowed=allowed,
        reason_code=None if allowed else code,
        reason=None if allowed else reason,
    )


def cached_capabilities(backend):
    value = (backend.health_meta or {}).get("capabilities")
    return to_record(value) if isinstance(value, Mapping) else {}


def concrete_variants(model):
    axes = model.get("supported_variants") or []
    if not axes:
        return [{}]
    if not isinstance(axes, list) or any(not isinstance(axis, dict) for axis in axes):
        return []
    keys = [axis.get("key") for axis in axes]
    if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
        return []
    values = [
        set(
            v.get("value")
            for v in axis.get("variants", [])
            if isinstance(v, dict) and isinstance(v.get("value"), str)
        )
        for axis in axes
    ]
    combos = model.get("variant_combinations")
    if combos:
        return [
            dict(zip(keys, combo))
            for combo in combos
            if isinstance(combo, list)
            and len(combo) == len(keys)
            and all(v in valid for v, valid in zip(combo, values))
        ]
    if len(axes) == 1:
        return [{keys[0]: value} for value in sorted(values[0])]
    # An undeclared multi-axis product is a model configuration, not invented weights.
    defaults = model.get("default_variants") or {}
    return (
        [defaults]
        if set(defaults) == set(keys)
        and all(defaults[k] in valid for k, valid in zip(keys, values))
        else [{}]
    )


def backend_reason(backend, pool, member, *, enabled, member_count):
    if not enabled:
        return "project_disabled", "项目未启用此后端"
    if pool is None or not pool.enabled:
        return "pool_disabled", "服务池未启用"
    if member is None or member.traffic_state != "active":
        return "instance_not_routable", "推理实例未接收新任务"
    if (
        _router_mode() in {RouterMode.OFF, RouterMode.OBSERVE}
        and pool.legacy_instance_id != backend.id
    ):
        return "instance_not_routable", "当前路由模式未选择此实例"
    if backend.state != "connected":
        return "backend_unavailable", "推理实例不可用"
    checked = backend.last_checked_at
    if checked is None or checked < datetime.now(timezone.utc) - timedelta(
        seconds=settings.ml_backend_router_health_max_age_seconds
    ):
        return "health_stale", "推理实例健康状态尚未刷新"
    caps = cached_capabilities(backend)
    if (
        not isinstance(caps, dict)
        or (
            pool.capability_fingerprint
            and capability_fingerprint(caps) != pool.capability_fingerprint
        )
        or (not pool.capability_fingerprint and member_count > 1)
    ):
        return "capability_mismatch", "推理实例能力与服务池不一致"
    return None, None


def native_metadata(backend, model, variants):
    prompts = set(model.get("supported_prompts") or []) - {"none"}
    if prompts and not model.get("classes"):
        return CatalogClassMetadata(
            class_source="request_prompt", verification_status="request_defined"
        ), []
    fallback = model.get("classes") or []
    declared = CatalogClassMetadata(
        class_source="builtin_manifest",
        verification_status="declared",
        classes_digest=classes_digest([c["name"] for c in fallback])
        if fallback
        else None,
    )
    try:
        observed = ModelMetadataResponse.model_validate(
            (backend.health_meta or {}).get("model_metadata")
        )
    except (ValueError, TypeError):
        return declared, fallback
    item = next(
        (
            m
            for m in observed.models
            if m.model_id == model.get("id") and m.model_variants == variants
        ),
        None,
    )
    if item is None:
        return declared, fallback
    if item.verification_status not in {"verified", "mismatch"}:
        return declared, fallback
    return CatalogClassMetadata(
        class_source="checkpoint",
        verification_status=item.verification_status,
        classes_digest=item.classes_digest,
        artifact_sha256=item.artifact_sha256,
        inspected_at=item.inspected_at,
        loader_version=item.loader_version,
    ), [c.model_dump() for c in item.classes]


async def list_catalog(
    db,
    user,
    *,
    project=None,
    access=None,
    page=1,
    page_size=50,
    task=None,
    source=None,
    status=None,
    q=None,
):
    manager = access.is_manager if access else True
    project_relation = project_facts()
    project_query = select(project_relation)
    if project is not None:
        project_query = project_query.where(project_relation.c.id == project.id)
    elif user.role != "super_admin":
        project_query = project_query.where(project_relation.c.owner_id == user.id)
    projects = {row.id: row for row in (await db.execute(project_query)).all()}
    topology = InferenceTopologyData(db)
    registries = {row.id: row for row in await topology.registry_catalog()}
    pools = {row.id: row for row in await topology.pool_catalog()}
    members = {}
    for pool_id in pools:
        for member in await topology.pool_members(pool_id):
            members[member.registry_id] = member
    member_counts = defaultdict(int)
    for member in members.values():
        if member.traffic_state == "active":
            member_counts[member.pool_id] += 1
    associations = {}
    project_ids = list(projects)
    for start in range(0, len(project_ids), 500):
        for fact in await topology.project_backend_facts(
            project_ids[start : start + 500]
        ):
            if fact.association is not None:
                associations[fact.association.id] = fact.association
    links = associations.values()
    control = backend_control_service(db)
    credential_policies = {}
    enabled_pools = {
        link.pool_id
        for link in links
        if link.enabled and (project is None or link.project_id == project.id)
    }
    enabled_by_project = defaultdict(set)
    for link in links:
        if link.enabled:
            enabled_by_project[link.project_id].add(link.pool_id)
    entries = {}
    for backend in registries.values():
        caps = cached_capabilities(backend)
        if not isinstance(caps, dict):
            continue
        member = members.get(backend.id)
        pool = pools.get(member.pool_id) if member else None
        for raw in caps.get("models") or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                continue
            try:
                ModelCapability.model_validate(
                    {k: v for k, v in raw.items() if k in ModelCapability.model_fields}
                )
                if any(
                    not isinstance(c, dict) or not isinstance(c.get("name"), str)
                    for c in (raw.get("classes") or [])
                ):
                    continue
            except (ValidationError, TypeError):
                continue
            for variants in concrete_variants(raw):
                safe = {
                    k: v for k, v in raw.items() if k in ModelCapability.model_fields
                }
                class_meta, classes = native_metadata(backend, raw, variants)
                safe["classes"] = classes
                try:
                    capability = ModelCapability.model_validate(safe)
                except ValidationError:
                    continue
                selection = [
                    str(pool.id if pool else backend.id),
                    capability.task,
                    capability.id,
                    variants,
                ]
                key = (
                    "builtin:"
                    + hashlib.sha256(
                        json.dumps(selection, sort_keys=True).encode()
                    ).hexdigest()
                )
                code, reason = backend_reason(
                    backend,
                    pool,
                    member,
                    enabled=pool is not None and pool.id in enabled_pools,
                    member_count=member_counts[pool.id] if pool else 0,
                )
                if class_meta.verification_status == "mismatch":
                    code, reason = (
                        "class_metadata_mismatch",
                        "权重类别与后端声明不一致，须先核对配置",
                    )
                placement = CatalogPlacement(
                    registry_id=backend.id,
                    pool_id=pool.id if pool else None,
                    name=backend.name,
                    ready=code is None,
                    reason_code=code,
                    reason=reason,
                )
                if key not in entries:
                    entries[key] = CatalogEntry(
                        id=key,
                        source="builtin",
                        name=capability.display_name or capability.id,
                        task=capability.task,
                        media_type=capability.modality or "image",
                        framework=capability.infra,
                        model_id=capability.id,
                        model_variants=variants,
                        publication="builtin",
                        capabilities=capability,
                        output_classes=[c["name"] for c in classes],
                        class_metadata=class_meta,
                        placements=[],
                        operations={},
                    )
                entries[key].placements.append(placement)
                # Prefer a verified observation from an eligible placement.
                if placement.ready and class_meta.verification_status == "verified":
                    entries[key].class_metadata = class_meta
                    entries[key].capabilities = capability
    versions = (
        (
            await db.scalars(
                select(TrainedModelVersion).where(
                    TrainedModelVersion.project_id.in_(projects)
                )
            )
        ).all()
        if projects
        else []
    )
    version_ids = [v.id for v in versions]
    deployments = defaultdict(list)
    releases = defaultdict(list)
    decisions = {}
    if version_ids:
        for deployment in (
            await db.scalars(
                select(ModelDeployment).where(
                    ModelDeployment.model_version_id.in_(version_ids)
                )
            )
        ).all():
            deployments[deployment.model_version_id].append(deployment)
        for release in (
            await db.scalars(
                select(ModelRelease)
                .where(ModelRelease.model_version_id.in_(version_ids))
                .order_by(ModelRelease.generation.desc())
            )
        ).all():
            releases[release.model_version_id].append(release)
        for decision in (
            await db.scalars(
                select(ModelDecision)
                .where(ModelDecision.model_version_id.in_(version_ids))
                .order_by(ModelDecision.sequence.desc())
            )
        ).all():
            decisions.setdefault(decision.model_version_id, decision.disposition)
    selections = (
        {
            (s.project_id, s.task): s.release_id
            for s in (
                await db.scalars(
                    select(ProjectModelSelection).where(
                        ProjectModelSelection.project_id.in_(projects)
                    )
                )
            ).all()
        }
        if projects
        else {}
    )
    current_releases = {}
    for version in versions:
        history = releases[version.id]
        if not manager and not history:
            continue
        owner = projects[version.project_id]
        version_task = trained_models.model_task(version)
        current = next(
            (r for r in history if r.id == selections.get((owner.id, version_task))),
            None,
        )
        archived = decisions.get(version.id) in {"archived", "declined"}
        publication = (
            "current"
            if current
            else "archived"
            if archived
            else "previous"
            if history
            else "candidate"
        )
        class_meta = CatalogClassMetadata(
            class_source="training_config",
            verification_status="declared",
            classes_digest=classes_digest(version.classes),
            artifact_sha256=version.artifact.get("sha256"),
        )
        placements = []
        for deployment in deployments[version.id]:
            backend = registries.get(deployment.registry_id)
            if backend is None:
                continue
            member = members.get(backend.id)
            pool = pools.get(member.pool_id) if member else None
            code, reason = backend_reason(
                backend,
                pool,
                member,
                enabled=pool is not None
                and pool.id in enabled_by_project[version.project_id],
                member_count=member_counts[pool.id] if pool else 0,
            )
            if not code:
                if backend.id not in credential_policies:
                    credential_policies[backend.id] = await control.credential_policy(
                        backend.id
                    )
                policy = credential_policies[backend.id]
                if (
                    policy is None
                    or not policy.auth_token_length_valid
                    or (
                        policy.auth_method != "token"
                        and managed_provider(policy.auth_method) != "yolo"
                    )
                ):
                    code, reason = (
                        "authentication_required",
                        "推理实例未配置模型回流认证",
                    )
            try:
                trained_models.check_receipt(version, deployment.receipt)
                # Artifact inspection is a historical fact, independent of current health.
                class_meta.class_source = "checkpoint"
                class_meta.verification_status = "verified"
                class_meta.inspected_at = deployment.checked_at
                trained_models.validate_bindings(
                    owner, version.classes, version.bindings, version_task
                )
            except HTTPException:
                code, reason = (
                    "deployment_identity_invalid",
                    "部署校验或项目类别绑定已失效",
                )
            if deployment.status != "ready":
                code, reason = "deployment_not_ready", "模型尚未完成加载检查与样本试用"
            native_detect = next(
                (
                    m
                    for m in cached_capabilities(backend).get("models", [])
                    if isinstance(m, dict)
                    and m.get("id")
                    == ("segment" if version_task == "segment" else "detect")
                    and m.get("task") == version_task
                ),
                {},
            )
            placements.append(
                CatalogPlacement(
                    registry_id=backend.id,
                    pool_id=pool.id if pool else None,
                    name=backend.name,
                    deployment_id=deployment.id,
                    ready=code is None,
                    params=native_detect.get("params") or {},
                    reason_code=code,
                    reason=reason,
                )
            )
        qualified_backends = [registries[p.registry_id] for p in placements if p.ready]
        base = next(
            (
                m
                for backend in qualified_backends
                for m in cached_capabilities(backend).get("models", [])
                if isinstance(m, dict)
                and m.get("task") == version_task
                and m.get("id")
                == ("segment" if version_task == "segment" else "detect")
            ),
            {},
        )
        capability = ModelCapability(
            id=f"trained:{version.id}",
            display_name=version.name,
            task=normalize_task(version.provenance.get("task", "detect")),
            infra="pytorch",
            supported_prompts=["none"],
            supported_inputs=["full_image"]
            if version_task == "segment"
            else ["full_image", "crop"],
            supported_geometric_outputs=["mask"]
            if version_task == "segment"
            else ["bbox"],
            output_attribute_types=["class"],
            classes=[
                {"index": i, "name": name} for i, name in enumerate(version.classes)
            ],
            resource_profile={
                "device": trained_models.controlled_device(qualified_backends[0])
                or "gpu"
                if qualified_backends
                else "gpu",
                "batchable": True,
            },
            params=base.get("params") or {},
        )
        entry = CatalogEntry(
            id=f"trained:{version.id}",
            source="project_trained",
            name=version.name,
            task=capability.task,
            media_type="image",
            framework=version.framework,
            project=CatalogProject(id=owner.id, name=owner.name),
            model_id=capability.id,
            version_id=version.id,
            publication=publication,
            capabilities=capability,
            output_classes=version.bindings,
            class_metadata=class_meta,
            placements=placements,
            operations={},
        )
        current_placement = next(
            (
                p
                for p in placements
                if current and p.deployment_id == current.deployment_id
            ),
            None,
        )
        if (
            current
            and current_placement
            and current_placement.ready
            and project is not None
        ):
            current_releases[version_task] = CatalogCurrentRelease(
                entry_id=entry.id,
                task=version_task,
                model_id="project-release:segment"
                if version_task == "segment"
                else "project-release",
                registry_id=current_placement.registry_id,
                deployment_id=current.deployment_id,
                generation=current.generation,
            )
        entries[entry.id] = entry
    for entry in entries.values():
        eligible = next((p for p in entry.placements if p.ready), None)
        reason_placement = next(iter(entry.placements), None)
        code = reason_placement.reason_code if reason_placement else "no_deployment"
        reason = reason_placement.reason if reason_placement else "尚无已校验的模型部署"
        if entry.source == "builtin" and project is None:
            code, reason = "choose_project", "请先选择使用此模型的项目"
        elif entry.publication == "candidate":
            code, reason = "not_published", "尚未发布；管理员可在模型详情加载与试用"
        elif entry.publication == "archived":
            code, reason = "model_archived", "此模型已停止用于项目推理"
        elif eligible:
            code, reason = None, None
        selectable = code is None
        entry.operations = {
            "view": operation(manager, "permission_denied", "没有模型管理权限")
        }
        for name, permitted in (
            ("preannotate", manager),
            ("pipeline", manager),
            ("workbench", manager or bool(access and access.has("annotation.write"))),
        ):
            entry.operations[name] = operation(
                selectable and permitted,
                code if permitted else "permission_denied",
                reason if permitted else "没有此项目的操作权限",
            )
        entry.operations["trial"] = operation(
            manager and entry.source == "project_trained",
            "permission_denied",
            "仅项目管理员可加载与试用候选模型",
        )
        entry.operations["publish"] = operation(
            manager and entry.source == "project_trained" and eligible is not None,
            "deployment_not_ready",
            "须先完成加载检查与样本试用",
        )
    values = sorted(entries.values(), key=lambda e: (e.source, e.name.casefold(), e.id))
    values = [
        e
        for e in values
        if (not task or e.task == task)
        and (not source or e.source == source)
        and (not status or e.publication == status)
        and (
            not q
            or q.casefold()
            in (e.name + " " + " ".join(e.model_variants.values())).casefold()
        )
    ]
    task_counts = defaultdict(int)
    for entry in values:
        task_counts[entry.task] += 1
    return ModelCatalogResponse(
        items=values[(page - 1) * page_size : page * page_size],
        total=len(values),
        page=page,
        page_size=page_size,
        projects=[CatalogProject(id=p.id, name=p.name) for p in projects.values()],
        current_release=current_releases.get(task or "detect"),
        current_releases=current_releases,
        task_counts=dict(task_counts),
    )
