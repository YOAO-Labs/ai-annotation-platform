"""Exercise API CAS after real Native state changes with a controlled drain port."""
from uuid import UUID, uuid4

import pytest

from app.api.v1 import admin_plugin_installations as routes
from app.services.inference_topology_data import InferenceTopologyData
from test_plugin_installation_api import BASE, payload

pytestmark = pytest.mark.asyncio


async def prepared_launch(httpx_client, super_admin, monkeypatch):
    _, token = super_admin
    headers = {"Authorization": f"Bearer {token}"}
    body = payload(uuid4())
    first = await httpx_client.post(BASE + "/prepare", json=body, headers=headers)
    assert first.status_code == 200, first.text
    path = BASE + "/" + body["installation_id"]
    receipt = {"installation_id": body["installation_id"], "registry_id": first.json()["registry_id"],
               "artifact": body["manifest"]["artifact"], "image_id": "sha256:" + "c" * 64,
               "container_id": "d" * 64, "resource": body["resource"]}

    async def probe(db, installation, launch, qualification=None, *, for_drain=False):
        return {**launch.model_dump(mode="json"), "endpoint": body["url"],
                "boot_id": "boot-1", "control_epoch": None}

    monkeypatch.setattr(routes, "observe_plugin_endpoint", probe)
    activated = await httpx_client.post(path + "/activate", headers=headers,
                                        json={"expected_revision": 1, "receipt": receipt})
    assert activated.status_code == 200, activated.text
    disabled = await httpx_client.post(path + "/disable", headers=headers,
                                       json={"expected_revision": activated.json()["installation"]["revision"]})
    assert disabled.status_code == 200, disabled.text
    return path, headers, receipt, disabled.json()["installation"]["revision"]


async def test_cpu_quiescence_commits_terminal_proof_and_fences_reactivation(httpx_client, super_admin, monkeypatch):
    path, headers, receipt, revision = await prepared_launch(httpx_client, super_admin, monkeypatch)
    calls = []

    class Control:
        async def terminal_drain(self, registry_id, *, boot_id, operation_id):
            calls.append((registry_id, boot_id, operation_id))
            return {"registry_id": str(registry_id), "boot_id": boot_id, "operation_id": str(operation_id),
                    "control_epoch": None, "generation": None, "pool_ids": ["models"], "unloaded": True}

    monkeypatch.setattr(routes, "backend_control_service", lambda db: Control())
    response = await httpx_client.post(path + "/quiesce", headers=headers, json={"expected_revision": revision})
    assert response.status_code == 200, response.text
    assert response.json()["safe_to_stop"] is True
    proof_revision = response.json()["installation"]["revision"]
    repeat = await httpx_client.post(path + "/quiesce", headers=headers, json={"expected_revision": proof_revision})
    assert repeat.status_code == 200 and repeat.json()["safe_to_stop"] is True
    assert len(calls) == 1
    restart = await httpx_client.post(path + "/activate", headers=headers,
                                      json={"expected_revision": proof_revision, "receipt": receipt})
    assert restart.status_code == 409
    retired = await httpx_client.post(path + "/retire", headers=headers,
                                      json={"expected_revision": proof_revision})
    assert retired.status_code == 200 and retired.json()["installation"]["status"] == "retired"


async def test_revision_change_during_drain_never_authorizes_stop(httpx_client, super_admin, monkeypatch, db_session):
    path, headers, receipt, revision = await prepared_launch(httpx_client, super_admin, monkeypatch)

    class RacingControl:
        async def terminal_drain(self, registry_id, *, boot_id, operation_id):
            # A separate command changes the durable revision during remote I/O.
            await InferenceTopologyData(db_session).command_plugin_installation(
                UUID(receipt["installation_id"]), revision, "record_launch", receipt,
            )
            await db_session.commit()
            return {"registry_id": str(registry_id), "boot_id": boot_id, "operation_id": str(operation_id),
                    "control_epoch": None, "generation": None, "pool_ids": ["models"], "unloaded": True}

    monkeypatch.setattr(routes, "backend_control_service", lambda db: RacingControl())
    response = await httpx_client.post(path + "/quiesce", headers=headers, json={"expected_revision": revision})
    assert response.status_code == 409
    assert "safe_to_stop" not in response.json()


async def test_retirement_rejects_active_learning_dependencies(httpx_client, super_admin, monkeypatch):
    from app.services import trained_models
    from app.services.trained_model_reference_contract import RegistryLearningUsage

    path, headers, _, revision = await prepared_launch(httpx_client, super_admin, monkeypatch)

    async def active_usage(db, registry_id):
        return RegistryLearningUsage(active_releases=1, frozen_executions=2)

    monkeypatch.setattr(trained_models, "registry_usage", active_usage)
    response = await httpx_client.post(path + "/retire", headers=headers,
                                       json={"expected_revision": revision})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "plugin_installation_in_use"
    assert response.json()["detail"]["frozen_executions"] == 2
    current = await httpx_client.get(path, headers=headers)
    assert current.json()["installation"]["status"] == "disabled"
