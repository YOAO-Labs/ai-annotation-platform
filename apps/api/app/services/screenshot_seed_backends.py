"""Discover and bind ML backends required by the screenshots seed profile."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from aap_inference.registry_ingress import (
    get_or_create_seed,
    refresh_candidates as refresh_registry_candidates,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_object_session

from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.services.workspace_seed_data import (
    register_seed_project_projection,
    seed_projects,
)
from app.config import settings
from app.services.ml_backend import MLBackendService
from app.services.ml_client import MLBackendClient
from app.services.inference_limits import capability_limits_from_settings
from app.services.inference_registry_ingress_data import InferenceRegistryIngressData
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


def _capabilities(backend: MLBackendRegistry) -> dict[str, Any]:
    value = (backend.health_meta or {}).get("capabilities")
    return value if isinstance(value, dict) else {}


def _is_owned_stub(backend: MLBackendRegistry) -> bool:
    marker = (backend.extra_params or {}).get("seed")
    return bool(
        isinstance(marker, dict)
        and marker.get("managed_by") == SEED_MANAGED_BY
        and marker.get("profile") == "screenshots"
    )


def selected_tracker(
    capabilities: dict[str, Any], requirement: BackendRequirement
) -> str | None:
    supported = set(capabilities.get("supported_trackers") or [])
    return next(
        (tracker for tracker in requirement.tracker_priority if tracker in supported),
        None,
    )


def backend_requirement_issues(
    backend: MLBackendRegistry, requirement: BackendRequirement
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
    model_tasks = {model.get("task") for model in models if isinstance(model, dict)}
    prompts = set(capabilities.get("supported_prompts") or [])
    inputs = set(capabilities.get("supported_inputs") or [])
    geometries = set(capabilities.get("supported_geometric_outputs") or [])
    output_attributes = {
        attribute
        for model in models
        if isinstance(model, dict)
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
    backends: list[MLBackendRegistry], requirement: BackendRequirement
) -> MLBackendRegistry | None:
    matches = [
        backend
        for backend in backends
        if not backend_requirement_issues(backend, requirement)
    ]
    if not matches:
        return None

    def sort_key(backend: MLBackendRegistry) -> tuple[int, str, str]:
        tracker = selected_tracker(_capabilities(backend), requirement)
        tracker_rank = (
            requirement.tracker_priority.index(tracker)
            if tracker in requirement.tracker_priority
            else len(requirement.tracker_priority)
        )
        return tracker_rank, backend.url.casefold(), str(backend.id)

    return min(matches, key=sort_key)


async def _refresh_candidates(
    backends: list[MLBackendRegistry],
) -> list[MLBackendRegistry]:
    session = async_object_session(backends[0]) if backends else None
    return await refresh_registry_candidates(
        InferenceRegistryIngressData(session),
        backends,
        client_factory=MLBackendClient,
        capability_limits_factory=capability_limits_from_settings,
        clock=lambda: datetime.now(UTC),
    )


def _format_discovery_failure(
    requirement: BackendRequirement, backends: list[MLBackendRegistry]
) -> str:
    observed = []
    for backend in sorted(backends, key=lambda item: item.url.casefold()):
        details = ", ".join(backend_requirement_issues(backend, requirement))
        observed.append(f"{backend.url} ({details or 'matched'})")
    suffix = (
        "; observed: " + "; ".join(observed) if observed else "; no backends registered"
    )
    return f"{requirement.key} requires {requirement.description}{suffix}"


async def _live_candidates(db: AsyncSession) -> list[MLBackendRegistry]:
    rows = await InferenceRegistryIngressData(db).candidates("live")
    return await _refresh_candidates(
        [backend for backend in rows if not _is_owned_stub(backend)]
    )


async def _stub_candidate(db: AsyncSession, stub_url: str) -> list[MLBackendRegistry]:
    url = stub_url.rstrip("/")
    backend = await get_or_create_seed(
        InferenceRegistryIngressData(db),
        url,
        STUB_BACKEND_NAME,
        {
            "managed_by": SEED_MANAGED_BY,
            "profile": "screenshots",
            "revision": SEED_REVISION,
        },
    )
    candidates = await _refresh_candidates([backend])
    if backend.name != STUB_BACKEND_NAME:
        raise ScreenshotSeedBackendError(
            f"stub mode expected {STUB_BACKEND_NAME} at {url}, got {backend.name}"
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

    selected: dict[str, MLBackendRegistry] = {}
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
    for logical_key, spec in PROJECT_SPECS.items():
        project = await db.scalar(
            select(seed_projects).where(seed_projects.display_id == spec.display_id)
        )
        if project is None:
            raise ScreenshotSeedBackendError(
                f"screenshot project {spec.display_id} is missing"
            )
        register_seed_project_projection(db, project.id)
        await db.execute(
            delete(ProjectMLBackendPool).where(
                ProjectMLBackendPool.project_id == project.id
            )
        )
        project.ml_backend_pool_id = None
        if spec.required_backend not in required:
            project.preannotate_pipeline = []
            continue
        backend = selected[spec.required_backend]
        await service.set_enabled(project.id, backend.id, True)
        # v0.23.3 ADR-0050 · 项目主绑定存 pool id; set_enabled 内部已解析 registry→pool。
        pool = await service._pool_for_registry(backend.id)
        register_seed_project_projection(db, project.id)
        project.ml_backend_pool_id = pool.id if pool is not None else None
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
