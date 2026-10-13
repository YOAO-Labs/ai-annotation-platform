"""v0.23.3 ADR-0050 §12.2 · project pool-binding API tests.

GET /projects/:id/ml-backends/pools/available
PUT /projects/:id/ml-backends/pools/:pool_id/enablement

off/observe: each pool is a singleton; behavior is equivalent to the registry-level
enablement endpoints. Full pool management UI is v0.23.4.
"""

from __future__ import annotations

import uuid

import pytest

from app.db.models.ml_backend_registry import ProjectMLBackendPool
from tests.conftest import create_registry_with_pool
from tests.factory import create_project


@pytest.mark.asyncio
async def test_list_pools_available_returns_all_pools_with_enablement(
    httpx_client, db_session, super_admin
) -> None:
    """All pools appear (LEFT JOIN); enablement reflects project_ml_backend_pool rows."""
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    backend1, pool1 = await create_registry_with_pool(db_session, name="bk1")
    backend2, pool2 = await create_registry_with_pool(db_session, name="bk2")
    # Enable pool1 for the project.
    db_session.add(
        ProjectMLBackendPool(project_id=proj.id, pool_id=pool1.id, enabled=True)
    )
    await db_session.flush()

    resp = await httpx_client.get(
        f"/api/v1/projects/{proj.id}/ml-backends/pools/available",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    pool_ids = {it["pool"]["id"] for it in items}
    assert str(pool1.id) in pool_ids
    assert str(pool2.id) in pool_ids
    # pool1 enabled, pool2 not.
    p1 = next(it for it in items if it["pool"]["id"] == str(pool1.id))
    p2 = next(it for it in items if it["pool"]["id"] == str(pool2.id))
    assert p1["enabled"] is True
    assert p2["enabled"] is False
    # singleton pools: member_count = 1, legacy_instance_id set.
    assert p1["pool"]["member_count"] == 1
    assert p1["pool"]["legacy_instance_id"] == str(backend1.id)


@pytest.mark.asyncio
async def test_enable_pool_for_project(httpx_client, db_session, super_admin) -> None:
    """PUT /pools/:pool_id/enablement creates the project binding."""
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    backend, pool = await create_registry_with_pool(db_session, name="bk")
    pool.enabled = True  # pool must be enabled to be project-enableable
    await db_session.flush()

    resp = await httpx_client.put(
        f"/api/v1/projects/{proj.id}/ml-backends/pools/{pool.id}/enablement",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["pool"]["id"] == str(pool.id)

    # The binding row now exists.
    from sqlalchemy import select

    assoc = (
        await db_session.execute(
            select(ProjectMLBackendPool).where(
                ProjectMLBackendPool.project_id == proj.id,
                ProjectMLBackendPool.pool_id == pool.id,
            )
        )
    ).scalar_one()
    assert assoc.enabled is True


@pytest.mark.asyncio
async def test_enable_disabled_pool_rejected(
    httpx_client, db_session, super_admin
) -> None:
    """A pool that is not enabled (pool.enabled=false) cannot be project-enabled (D15)."""
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    _backend, pool = await create_registry_with_pool(db_session, name="bk")
    # pool.enabled defaults to False (create_registry_with_pool does not enable the pool).
    await db_session.flush()

    resp = await httpx_client.put(
        f"/api/v1/projects/{proj.id}/ml-backends/pools/{pool.id}/enablement",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": True},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "ml_backend_pool_not_enabled"


@pytest.mark.asyncio
async def test_enable_unknown_pool_404(httpx_client, db_session, super_admin) -> None:
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    resp = await httpx_client.put(
        f"/api/v1/projects/{proj.id}/ml-backends/pools/{uuid.uuid4()}/enablement",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": True},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_disable_pool_clears_enablement(
    httpx_client, db_session, super_admin
) -> None:
    """PUT with enabled=false disables an existing binding."""
    user, token = super_admin
    proj = await create_project(db_session, owner_id=user.id)
    _backend, pool = await create_registry_with_pool(db_session, name="bk")
    pool.enabled = True
    db_session.add(
        ProjectMLBackendPool(project_id=proj.id, pool_id=pool.id, enabled=True)
    )
    await db_session.flush()

    resp = await httpx_client.put(
        f"/api/v1/projects/{proj.id}/ml-backends/pools/{pool.id}/enablement",
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
