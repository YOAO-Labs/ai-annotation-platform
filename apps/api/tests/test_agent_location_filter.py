"""Agent impact lists retain the registry's current visibility and pagination."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.db.models.dataset_asset import StorageLocation, StorageLocationProbe
from app.services import storage_location as locations
from tests.test_agent_transport import remote as remote


async def test_agent_filter_keeps_authority_and_total_before_pagination(
    remote, auth_headers, project_admin
):
    db, client, agent, _, location, _, _ = remote
    location.config = {**location.config, "scope": "restricted", "use_user_ids": []}
    allowed = StorageLocation(
        id=uuid.uuid4(),
        name="allowed-" + uuid.uuid4().hex,
        kind="agent",
        enabled=True,
        config_revision=1,
        config={**location.config, "use_user_ids": [str(project_admin[0].id)]},
    )
    other = StorageLocation(
        id=uuid.uuid4(),
        name="other-" + uuid.uuid4().hex,
        kind="agent",
        enabled=True,
        config_revision=1,
        config={**allowed.config, "agent_id": str(uuid.uuid4())},
    )
    db.add_all([allowed, other])
    await db.flush()
    params = {"agent_id": str(agent.id), "limit": 1, "offset": 1}
    response = await client.get(
        "/api/v1/storage-locations", headers=auth_headers, params=params
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2 and len(response.json()["items"]) == 1
    response = await client.get(
        "/api/v1/storage-locations",
        params=params,
        headers={"Authorization": "Bearer " + project_admin[1]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1 and response.json()["items"] == []
    params["offset"] = 0
    response = await client.get(
        "/api/v1/storage-locations",
        params=params,
        headers={"Authorization": "Bearer " + project_admin[1]},
    )
    assert [row["id"] for row in response.json()["items"]] == [str(allowed.id)]


async def test_agent_id_filter_intersects_site_and_keeps_total(remote, auth_headers):
    _, client, agent, _, _, _, _ = remote
    path = "/api/v1/site-agents/agents"
    response = await client.get(
        path, headers=auth_headers, params={"agent_id": str(agent.id), "offset": 1}
    )
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 1}
    response = await client.get(
        path,
        headers=auth_headers,
        params={"agent_id": str(agent.id), "site_id": str(uuid.uuid4())},
    )
    assert response.json() == {"items": [], "total": 0}


@pytest.mark.parametrize("node_id", ["api", "separate-worker@central"])
async def test_agent_probe_returns_the_remote_node_record(
    remote, auth_headers, monkeypatch, node_id
):
    db, client, agent, _, location, _, _ = remote
    result = {
        "ok": True,
        "code": "ok",
        "anchor": {"root": [1, 2], "base": [1, 3], "path_digest": "owned"},
        "capabilities": {"list": "verified", "read": "verified"},
    }
    monkeypatch.setattr(locations, "perform_probe", AsyncMock(return_value=result))
    response = await client.post(
        f"/api/v1/storage-locations/{location.id}/probe",
        headers=auth_headers,
        json={"node_id": node_id},
    )
    assert response.status_code == 200, response.text
    assert response.json()["node_id"] == f"agent:{agent.id}"
    assert response.json()["ok"] is True
    stored = await db.get(StorageLocationProbe, (location.id, f"agent:{agent.id}"))
    assert stored.result["anchor"] == result["anchor"]
