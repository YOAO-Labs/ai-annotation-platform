from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models.ml_backend_pool import MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.user import User
from app.services.ml_backend import MLBackendService
from app.services.screenshot_seed_backends import (
    _stub_candidate,
    backend_requirement_issues,
    reconcile_screenshot_backends,
    select_backend_for_requirement,
)
from app.services.screenshot_seed_spec import BACKEND_REQUIREMENTS, PROJECT_SPECS
from tests.conftest import create_registry_with_pool


pytestmark = pytest.mark.asyncio


def _backend(
    *,
    url: str,
    prompts: list[str] | None = None,
    trackers: list[str] | None = None,
    models: list[dict] | None = None,
    inputs: list[str] | None = None,
    geometries: list[str] | None = None,
    interactive: bool = False,
) -> MLBackendRegistry:
    return MLBackendRegistry(
        id=uuid.uuid4(),
        name=url.rsplit("/", 1)[-1],
        url=url,
        source="env",
        state="connected",
        is_interactive=interactive,
        health_meta={
            "capabilities": {
                "supported_prompts": prompts or [],
                "supported_trackers": trackers or [],
                "supported_inputs": inputs or [],
                "supported_geometric_outputs": geometries or [],
                "models": models or [],
            }
        },
    )


async def _persist_backend(
    db, backend: MLBackendRegistry
) -> tuple[MLBackendRegistry, MLBackendServicePool]:
    """Persist a fixture backend with the owner-created singleton pool."""
    kwargs = {
        "id": backend.id,
        "name": backend.name,
        "url": backend.url,
        "state": backend.state,
        "is_interactive": backend.is_interactive,
        "source": backend.source,
        "health_meta": backend.health_meta,
    }
    if backend.extra_params is not None:
        kwargs["extra_params"] = backend.extra_params
    return await create_registry_with_pool(db, **kwargs)


async def _singleton_pool(db, registry_id: uuid.UUID):
    service = MLBackendService(db)
    pool_id = await service.pool_id_for_registry(registry_id)
    if pool_id is None:
        return None
    return await service.get_pool(pool_id)


async def test_image_requirement_rejects_backend_without_exemplar() -> None:
    requirement = BACKEND_REQUIREMENTS["image_interactive"]
    sam2 = _backend(
        url="http://backend.test:8001",
        prompts=["point", "interactive_box"],
        models=[{"task": "interactive_seg"}],
        geometries=["polygon"],
        interactive=True,
    )
    sam3 = _backend(
        url="http://backend.test:8002",
        prompts=["point", "interactive_box", "exemplar"],
        models=[{"task": "interactive_seg"}],
        geometries=["polygon"],
        interactive=True,
    )

    assert "missing prompts ['exemplar']" in backend_requirement_issues(
        sam2, requirement
    )
    assert select_backend_for_requirement([sam2, sam3], requirement) is sam3


async def test_tracker_requirement_uses_declared_priority() -> None:
    requirement = BACKEND_REQUIREMENTS["video_tracker"]
    sam2 = _backend(
        url="http://backend.test:8001",
        trackers=["sam2_video"],
        models=[{"task": "tracker"}],
    )
    sam3 = _backend(
        url="http://backend.test:8002",
        trackers=["sam3_video_interactive"],
        models=[{"task": "tracker"}],
    )

    assert select_backend_for_requirement([sam2, sam3], requirement) is sam3


async def test_stub_candidate_creates_singleton_service_pool(
    db_session, monkeypatch
) -> None:
    async def fake_refresh(db, backends):
        return backends

    monkeypatch.setattr(
        "app.services.screenshot_seed_backends._refresh_candidates",
        fake_refresh,
    )

    candidates = await _stub_candidate(db_session, "http://stub.test:9100/")

    assert len(candidates) == 1
    backend = candidates[0]
    pool = await _singleton_pool(db_session, backend.id)
    assert pool is not None
    assert pool.legacy_instance_id == backend.id


async def test_reconcile_creates_exact_primary_and_enabled_bindings(
    db_session, monkeypatch
) -> None:
    owner = User(
        id=uuid.uuid4(),
        email="backend-seed-owner",
        name="owner",
        password_hash="test",
        role="super_admin",
        is_active=True,
    )
    db_session.add(owner)
    await db_session.flush()

    projects: dict[str, Project] = {}
    for logical_key, spec in PROJECT_SPECS.items():
        project = Project(
            id=uuid.uuid4(),
            display_id=spec.display_id,
            name=logical_key,
            type_label=logical_key,
            type_key=logical_key,
            data_type=spec.data_type,
            owner_id=owner.id,
            ai_enabled=spec.required_backend is not None,
            ai_interactive_enabled=spec.required_backend == "image_interactive",
        )
        db_session.add(project)
        projects[logical_key] = project

    image = _backend(
        url="http://backend.test:8002",
        prompts=["point", "interactive_box", "exemplar"],
        trackers=["sam3_video_interactive"],
        models=[{"task": "interactive_seg"}, {"task": "tracker"}],
        geometries=["polygon"],
        interactive=True,
    )
    ocr = _backend(
        url="http://backend.test:8005",
        models=[{"task": "ocr", "output_attribute_types": ["text"]}],
        inputs=["full_image"],
        geometries=["polygon"],
    )
    stale = _backend(url="http://backend.test:8999")
    owned_stub = _backend(url="http://backend.test:9100")
    owned_stub.source = "seed"
    owned_stub.extra_params = {
        "seed": {"managed_by": "screenshot-seed", "profile": "screenshots"}
    }
    # v0.23.3 ADR-0050 · 每 registry 须有 singleton pool 才能被项目启用 / 主绑定。
    image, image_pool = await _persist_backend(db_session, image)
    ocr, ocr_pool = await _persist_backend(db_session, ocr)
    stale, stale_pool = await _persist_backend(db_session, stale)
    owned_stub, owned_stub_pool = await _persist_backend(db_session, owned_stub)
    svc = MLBackendService(db_session)
    db_session.add(
        ProjectMLBackendPool(
            project_id=projects["pointcloud_demo"].id,
            pool_id=stale_pool.id,
            enabled=True,
        )
    )
    projects["pointcloud_demo"].ml_backend_pool_id = stale_pool.id
    await db_session.flush()

    async def fake_live_candidates(_db):
        return [image, ocr, stale]

    monkeypatch.setattr(
        "app.services.screenshot_seed_backends._live_candidates",
        fake_live_candidates,
    )

    report = await reconcile_screenshot_backends(db_session, mode="live")

    assert report["bindings"]["image_demo"]["backend_id"] == str(image.id)
    assert report["bindings"]["video_demo"]["tracker"] == ("sam3_video_interactive")
    assert report["bindings"]["ocr_demo"]["backend_id"] == str(ocr.id)
    assert projects["ocr_demo"].preannotate_pipeline == [
        {
            "stage": 0,
            "ml_backend_id": str(ocr.id),
            "model_id": "ocr-e2e",
            "task": "ocr",
        }
    ]
    assert projects["pointcloud_demo"].ml_backend_pool_id is None
    assert await db_session.get(MLBackendRegistry, owned_stub.id) is owned_stub
    assert (await svc.get_pool(owned_stub_pool.id)).legacy_instance_id == owned_stub.id
    associations = list(
        (await db_session.execute(select(ProjectMLBackendPool))).scalars()
    )
    assert {
        (association.project_id, association.pool_id) for association in associations
    } == {
        (projects["image_demo"].id, image_pool.id),
        (projects["video_demo"].id, image_pool.id),
        (projects["ocr_demo"].id, ocr_pool.id),
    }

    async def ocr_only(_db):
        return [ocr]

    monkeypatch.setattr(
        "app.services.screenshot_seed_backends._live_candidates", ocr_only
    )
    report = await reconcile_screenshot_backends(
        db_session, mode="live", backend_requirements="ocr"
    )
    assert set(report["bindings"]) == {"ocr_demo"}
    assert projects["image_demo"].ml_backend_pool_id is None
    assert projects["video_demo"].ml_backend_pool_id is None

    async def unexpected_probe(*_args):
        raise AssertionError("Manual capture must not probe or create ML backends")

    monkeypatch.setattr(
        "app.services.screenshot_seed_backends._live_candidates", unexpected_probe
    )
    monkeypatch.setattr(
        "app.services.screenshot_seed_backends._stub_candidate", unexpected_probe
    )
    for mode in ("live", "stub"):
        report = await reconcile_screenshot_backends(
            db_session, mode=mode, backend_requirements="none"
        )
        assert report["bindings"] == {}
        assert projects["ocr_demo"].preannotate_pipeline == []
        assert all(project.ml_backend_pool_id is None for project in projects.values())
