"""Discover and bind ML backends required by the screenshots seed profile."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from aap_inference.registry_ingress import get_or_create_seed
from aap_inference_contract.topology import RegistryPublicFacts
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.inference_registry_ingress_data import InferenceRegistryIngressData
from app.services.ml_backend import MLBackendService
from app.services.workspace_seed_data import (
    register_seed_project_projection,
    seed_projects,
)

from app.services.screenshot_seed_spec import (
    BACKEND_REQUIREMENTS,
    PROJECT_SPECS,
    SEED_MANAGED_BY,
    SEED_REVISION,
    BackendRequirement,
    parse_backend_requirements,
)


DEFAULT_SCREENSHOT_STUB_URL = "http://172.17.0.1:9100"
STUB_BACKEND_NAME = "mock-v2-backend"


class ScreenshotSeedBackendError(RuntimeError):
    pass


def default_screenshot_stub_url() -> str:
    """Reuse the configured Docker gateway host while keeping a distinct port."""
    configured = settings.ml_backend_storage_host
    if not configured and settings.ml_backend_default_url:
        configured = urlsplit(settings.ml_backend_default_url).netloc
    host = urlsplit(f"//{configured}").hostname if configured else None
    if not host:
        return DEFAULT_SCREENSHOT_STUB_URL
    rendered_host = f"[{host}]" if ":" in host else host
    return f"http://{rendered_host}:9100"


def _capabilities(backend: RegistryPublicFacts) -> Mapping[str, Any]:
    value = (backend.health_meta or {}).get("capabilities")
    return value if isinstance(value, Mapping) else {}


def _is_owned_stub(backend: RegistryPublicFacts) -> bool:
    marker = (backend.extra_params or {}).get("seed")
    return bool(
        isinstance(marker, Mapping)
        and marker.get("managed_by") == SEED_MANAGED_BY
        and marker.get("profile") == "screenshots"
    )


def selected_tracker(
    capabilities: Mapping[str, Any], requirement: BackendRequirement
) -> str | None:
    supported = set(capabilities.get("supported_trackers") or [])
    return next(
        (tracker for tracker in requirement.tracker_priority if tracker in supported),
        None,
    )


def backend_requirement_issues(
    backend: RegistryPublicFacts, requirement: BackendRequirement
) -> list[str]:
    issues: list[str] = []
    if backend.state != "connected":
        issues.append("state is not connected")
    if requirement.interactive and not backend.is_interactive:
        issues.append("backend is not interactive")

    capabilities = _capabilities(backend)
    if not capabilities:
        return [*issues, "capabilities snapshot is missing"]

    models = capabilities.get("models") or []
    model_tasks = {model.get("task") for model in models if isinstance(model, Mapping)}
    prompts = set(capabilities.get("supported_prompts") or [])
    inputs = set(capabilities.get("supported_inputs") or [])
    geometries = set(capabilities.get("supported_geometric_outputs") or [])
    output_attributes = {
        attribute
        for model in models
        if isinstance(model, Mapping)
        for attribute in (model.get("output_attribute_types") or [])
    }
    missing_prompts = sorted(set(requirement.required_prompts) - prompts)
    missing_tasks = sorted(set(requirement.required_tasks) - model_tasks)
    missing_inputs = sorted(set(requirement.required_inputs) - inputs)
    missing_geometries = sorted(set(requirement.required_geometries) - geometries)
    missing_attributes = sorted(
        set(requirement.required_output_attributes) - output_attributes
    )
    if missing_prompts:
        issues.append(f"missing prompts {missing_prompts}")
    if missing_tasks:
        issues.append(f"missing model tasks {missing_tasks}")
    if missing_inputs:
        issues.append(f"missing inputs {missing_inputs}")
    if missing_geometries:
        issues.append(f"missing geometries {missing_geometries}")
    if missing_attributes:
        issues.append(f"missing output attributes {missing_attributes}")
    if (
        requirement.tracker_priority
        and selected_tracker(capabilities, requirement) is None
    ):
        issues.append(f"missing one of trackers {list(requirement.tracker_priority)}")
    return issues


def select_backend_for_requirement(
    backends: list[RegistryPublicFacts], requirement: BackendRequirement
) -> RegistryPublicFacts | None:
    matches = [
        backend
        for backend in backends
        if not backend_requirement_issues(backend, requirement)
    ]
    if not matches:
        return None

    def sort_key(backend: RegistryPublicFacts) -> tuple[int, str, str]:
        tracker = selected_tracker(_capabilities(backend), requirement)
        tracker_rank = (
            requirement.tracker_priority.index(tracker)
            if tracker in requirement.tracker_priority
            else len(requirement.tracker_priority)
        )
        return tracker_rank, backend.url.casefold(), str(backend.id)

    return min(matches, key=sort_key)


async def _refresh_candidates(
    db: AsyncSession, backends: list[RegistryPublicFacts]
) -> list[RegistryPublicFacts]:
    if not backends:
        return []
    from app.services.inference_backend_control import BackendControlService
    from app.services.inference_topology_data import InferenceTopologyData

    control = BackendControlService(db)
    observations: dict[Any, tuple[bool, str | None, Mapping[str, Any] | None]] = {}
    for backend in backends:
        try:
            healthy = await control.check_health(backend.id)
            if not healthy:
                observations[backend.id] = (False, "health check failed", None)
                continue
            setup_result = await control.setup(backend.id)
            setup = setup_result.setup
            if setup_result.error is not None or not isinstance(setup, Mapping):
                observations[backend.id] = (
                    False,
                    str(setup_result.error or "setup response is unavailable"),
                    None,
                )
                continue
            observations[backend.id] = (True, None, setup)
        except Exception as exc:
            observations[backend.id] = (
                False,
                f"{type(exc).__name__}: {exc}",
                None,
            )

    fresh_rows = await InferenceTopologyData(db).registry_catalog()
    current = {fact.id: fact for fact in fresh_rows}
    service = MLBackendService(db)
    refreshed = []
    for backend in backends:
        fact = current.get(backend.id, backend)
        healthy, error, setup = observations[backend.id]
        if healthy and setup is not None:
            name = str(setup.get("name") or fact.name)
            if name != fact.name:
                fact = await service.update(fact.id, name=name) or fact
        elif not healthy:
            fact = replace(fact, state="error", error_message=error)
        refreshed.append(fact)
    return refreshed


def _format_discovery_failure(
    requirement: BackendRequirement, backends: list[RegistryPublicFacts]
) -> str:
    observed = []
    for backend in sorted(backends, key=lambda item: item.url.casefold()):
        details = ", ".join(backend_requirement_issues(backend, requirement))
        observed.append(f"{backend.url} ({details or 'matched'})")
    suffix = (
        "; observed: " + "; ".join(observed) if observed else "; no backends registered"
    )
    return f"{requirement.key} requires {requirement.description}{suffix}"


async def _live_candidates(db: AsyncSession) -> list[RegistryPublicFacts]:
    from app.services.inference_topology_data import InferenceTopologyData

    rows = await InferenceTopologyData(db).registry_catalog()
    candidates = [
        backend
        for backend in rows
        if backend.source != "seed" and not _is_owned_stub(backend)
    ]
    return await _refresh_candidates(db, candidates)


async def _stub_candidate(db: AsyncSession, stub_url: str) -> list[RegistryPublicFacts]:
    url = stub_url.rstrip("/")
    registry = await get_or_create_seed(
        InferenceRegistryIngressData(db),
        url,
        STUB_BACKEND_NAME,
        {
            "managed_by": SEED_MANAGED_BY,
            "profile": "screenshots",
            "revision": SEED_REVISION,
        },
    )
    from app.services.inference_topology_data import InferenceTopologyData

    backend = await InferenceTopologyData(db).registry_facts(registry.id)
    if backend is None:
        raise ScreenshotSeedBackendError(f"stub Registry {registry.id} is missing")
    candidates = await _refresh_candidates(db, [backend])
    if candidates and candidates[0].name != STUB_BACKEND_NAME:
        raise ScreenshotSeedBackendError(
            f"stub mode expected {STUB_BACKEND_NAME} at {url}, got {candidates[0].name}"
        )
    return candidates


async def reconcile_screenshot_backends(
    db: AsyncSession,
    *,
    mode: str,
    stub_url: str | None = None,
    backend_requirements: str | None = None,
) -> dict[str, Any]:
    if mode not in {"live", "stub"}:
        raise ScreenshotSeedBackendError(f"unsupported ML backend mode: {mode}")
    required = parse_backend_requirements(backend_requirements)
    candidates = (
        []
        if not required
        else (
            await _live_candidates(db)
            if mode == "live"
            else await _stub_candidate(db, stub_url or default_screenshot_stub_url())
        )
    )

    selected: dict[str, RegistryPublicFacts] = {}
    failures: list[str] = []
    for key, requirement in BACKEND_REQUIREMENTS.items():
        if key not in required:
            continue
        backend = select_backend_for_requirement(candidates, requirement)
        if backend is None:
            failures.append(_format_discovery_failure(requirement, candidates))
        else:
            selected[key] = backend
    if failures:
        hint = (
            "start the screenshot stub and rerun with --ml-backend-mode stub"
            if mode == "live"
            else "verify the screenshot stub is reachable from the API and workers"
        )
        raise ScreenshotSeedBackendError("; ".join([*failures, hint]))

    service = MLBackendService(db)
    bindings: dict[str, dict[str, str]] = {}
    from app.services.inference_topology_data import InferenceTopologyData

    topology_data = InferenceTopologyData(db)
    for logical_key, spec in PROJECT_SPECS.items():
        project = await db.scalar(
            select(seed_projects).where(seed_projects.display_id == spec.display_id)
        )
        if project is None:
            raise ScreenshotSeedBackendError(
                f"screenshot project {spec.display_id} is missing"
            )
        register_seed_project_projection(db, project.id)
        await topology_data.delete_project_associations(project.id)
        project.ml_backend_pool_id = None
        if spec.required_backend not in required:
            project.preannotate_pipeline = []
            continue
        backend = selected[spec.required_backend]
        await service.set_enabled(project.id, backend.id, True)
        # v0.23.3 ADR-0050 · 项目主绑定存 pool id; set_enabled 内部已解析 registry→pool。
        pool_id = await service.pool_id_for_registry(backend.id)
        register_seed_project_projection(db, project.id)
        project.ml_backend_pool_id = pool_id
        requirement = BACKEND_REQUIREMENTS[spec.required_backend]
        if spec.default_pipeline_model_id:
            project.preannotate_pipeline = [
                {
                    "stage": 0,
                    "ml_backend_id": str(backend.id),
                    "model_id": spec.default_pipeline_model_id,
                    "task": requirement.required_tasks[0],
                }
            ]
        binding = {
            "backend_id": str(backend.id),
            "backend_name": backend.name,
            "requirement": requirement.key,
        }
        tracker = selected_tracker(_capabilities(backend), requirement)
        if tracker:
            binding["tracker"] = tracker
        bindings[logical_key] = binding

    await db.commit()
    return {"mode": mode, "bindings": bindings}
