from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.config import settings
from app.db.models.dataset_asset import (
    StorageBinding,
    StorageLocation,
    StorageLocationProbe,
)
from app.services import storage_location as svc
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.storage import storage_service


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "root"
    path.mkdir()
    monkeypatch.setattr(settings, "storage_filesystem_roots", {"ceph": str(path)})
    return path


def headers(actor):
    return {"Authorization": f"Bearer {actor[1]}"}


async def reviewed_patch(client, url, *, headers, json):
    """Explicitly review location edits; preserve the preview's validation failures."""
    preview = await client.post(url + "/change-preview", headers=headers, json=json)
    if preview.status_code != 200:
        return preview
    return await client.patch(
        url,
        headers=headers,
        json={**json, "impact_token": preview.json()["impact_token"]},
    )


async def create(client, admin, **kwargs):
    response = await client.post(
        "/api/v1/storage-locations",
        headers=headers(admin),
        json={
            "name": f"location-{uuid.uuid4().hex[:8]}",
            "root_handle": "ceph",
            **kwargs,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_registration_permissions_dynamic_children_and_private_paths(
    httpx_client, super_admin, project_admin, annotator, root
):
    location = await create(httpx_client, super_admin)
    base = f"/api/v1/storage-locations/{location['id']}"
    assert str(root) not in str(location)
    assert (
        await httpx_client.get(base, headers=headers(project_admin))
    ).status_code == 404
    assert (await httpx_client.get(base, headers=headers(annotator))).status_code == 403
    response = await reviewed_patch(
        httpx_client,
        base,
        headers=headers(super_admin),
        json={"expected_revision": 1, "use_user_ids": [str(project_admin[0].id)]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["config_revision"] == 2
    (root / "new_dataset").mkdir()
    response = await httpx_client.get(
        base + "/directories", headers=headers(project_admin)
    )
    assert response.json()["entries"] == [
        {"name": "new_dataset", "relative_path": "new_dataset"}
    ]
    assert str(root) not in response.text
    assert (
        await httpx_client.post(
            "/api/v1/storage-locations",
            headers=headers(project_admin),
            json={"name": "forbidden", "root_handle": "ceph"},
        )
    ).status_code == 403
    assert (
        await reviewed_patch(
            httpx_client,
            base,
            headers=headers(super_admin),
            json={"expected_revision": 1, "enabled": False},
        )
    ).status_code == 409
    response = await reviewed_patch(
        httpx_client,
        base,
        headers=headers(super_admin),
        json={"expected_revision": 2, "use_user_ids": []},
    )
    assert response.status_code == 200
    assert (
        await httpx_client.get(base + "/directories", headers=headers(project_admin))
    ).status_code == 404


async def test_directory_cursor_is_scoped_versioned_and_detects_change(
    httpx_client, super_admin, root
):
    for name in ["a", "b", "c"]:
        (root / name).mkdir()
    location = await create(httpx_client, super_admin)
    base = f"/api/v1/storage-locations/{location['id']}"
    response = await httpx_client.get(
        base + "/directories", headers=headers(super_admin), params={"limit": 1}
    )
    cursor = response.json()["next_cursor"]
    response = await httpx_client.get(
        base + "/directories",
        headers=headers(super_admin),
        params={"limit": 1, "cursor": cursor},
    )
    assert response.json()["entries"][0]["name"] == "b"
    for params in [
        {"cursor": cursor + "x"},
        {"cursor": cursor, "path": "a"},
        {"path": "../"},
        {"path": "/etc"},
    ]:
        assert (
            await httpx_client.get(
                base + "/directories", headers=headers(super_admin), params=params
            )
        ).status_code == 409
    (root / "new").mkdir()
    assert (
        await httpx_client.get(
            base + "/directories",
            headers=headers(super_admin),
            params={"cursor": cursor},
        )
    ).status_code == 409


async def test_node_probe_is_separate_expiring_and_invalidated_by_revision(
    httpx_client, super_admin, root, db_session, monkeypatch
):
    location = await create(httpx_client, super_admin)
    base = f"/api/v1/storage-locations/{location['id']}"
    response = await httpx_client.post(
        base + "/probe", headers=headers(super_admin), json={}
    )
    assert response.status_code == 200, response.text
    assert response.json()["capabilities"]["list"] == "verified"
    assert response.json()["capabilities"]["read"] == "unknown"
    monkeypatch.setattr(
        "app.api.v1.storage_locations._probe_worker",
        lambda *args: {"ok": False, "code": "node_timeout", "capabilities": {}},
    )
    response = await httpx_client.post(
        base + "/probe", headers=headers(super_admin), json={"node_id": "offline@host"}
    )
    assert response.json()["ok"] is False
    row = await db_session.get(StorageLocation, uuid.UUID(location["id"]))
    with pytest.raises(HTTPException) as exc:
        await svc.require_scan_node(db_session, row, "worker:offline@host")
    assert exc.value.status_code == 409
    await reviewed_patch(
        httpx_client,
        base,
        headers=headers(super_admin),
        json={"expected_revision": 1, "enabled": False},
    )
    response = await httpx_client.get(base + "/probes", headers=headers(super_admin))
    assert all(
        not probe["ok"] and probe["code"] == "stale" for probe in response.json()
    )


async def test_long_chinese_path_can_follow_its_own_cursor(
    httpx_client, super_admin, root
):
    parts = ["目录" * 40 for _ in range(6)]
    directory = root.joinpath(*parts)
    directory.mkdir(parents=True)
    for name in ["一" * 80, "二" * 80]:
        (directory / name).mkdir()
    location = await create(httpx_client, super_admin)
    url = f"/api/v1/storage-locations/{location['id']}/directories"
    params = {"path": "/".join(parts), "limit": 1}
    first = await httpx_client.get(url, headers=headers(super_admin), params=params)
    assert first.status_code == 200, first.text
    cursor = first.json()["next_cursor"]
    assert len(cursor) < 4096
    second = await httpx_client.get(
        url, headers=headers(super_admin), params={**params, "cursor": cursor}
    )
    assert second.status_code == 200, second.text
    assert second.json()["entries"][0]["name"] != first.json()["entries"][0]["name"]


async def test_worker_name_cannot_exceed_persisted_node_id(
    httpx_client, super_admin, root
):
    location = await create(httpx_client, super_admin)
    response = await httpx_client.post(
        f"/api/v1/storage-locations/{location['id']}/probe",
        headers=headers(super_admin),
        json={"node_id": "x" * 249},
    )
    assert response.status_code == 422


async def test_failed_or_late_node_result_never_rebinds_pinned_root(
    db_session, httpx_client, super_admin, root
):
    data = await create(httpx_client, super_admin)
    location = await db_session.get(StorageLocation, uuid.UUID(data["id"]))
    now = datetime.now(timezone.utc)
    original = {"root": [1, 2], "base": [1, 3], "path_digest": "old"}
    await svc.store_probe(
        db_session,
        location,
        "worker:test",
        {
            "ok": True,
            "code": "ok",
            "anchor": original,
            "capabilities": {"list": "verified"},
        },
        now,
    )
    await svc.store_probe(
        db_session,
        location,
        "worker:test",
        {"ok": False, "code": "node_timeout", "capabilities": {}},
        now + timedelta(seconds=1),
    )
    await svc.store_probe(
        db_session,
        location,
        "worker:test",
        {
            "ok": True,
            "code": "ok",
            "anchor": {"other": 1},
            "capabilities": {"list": "verified"},
        },
        now + timedelta(seconds=2),
    )
    row = await db_session.get(
        StorageLocationProbe, (location.id, "worker:test"), populate_existing=True
    )
    assert (
        row.result["anchor"] == original
        and row.result["code"] == "root_identity_changed"
    )
    await svc.store_probe(
        db_session,
        location,
        "worker:test",
        {"ok": True, "code": "ok", "capabilities": {"list": "verified"}},
        now - timedelta(minutes=1),
    )
    await db_session.refresh(row)
    assert not row.result["ok"]
    row.observed_at = now - timedelta(minutes=6)
    assert svc.probe_out(row, location)["code"] == "stale"


async def test_bindings_get_is_read_only_and_keeps_nine_shared_bucket_purposes(
    httpx_client, super_admin, db_session, monkeypatch
):
    # Resource tests may have committed bootstrap bindings independently.
    # This SAVEPOINT owns the empty-registry precondition and restores them.
    await db_session.execute(delete(StorageBinding))
    monkeypatch.setattr(
        storage_service, "export_bucket", storage_service.datasets_bucket
    )

    def no_storage_io(*args, **kwargs):
        raise AssertionError("Binding metadata must not contact object storage")

    monkeypatch.setattr(storage_service.client, "head_bucket", no_storage_io)
    monkeypatch.setattr(storage_service.client, "list_objects_v2", no_storage_io)
    response = await httpx_client.get(
        "/api/v1/storage-locations/bindings", headers=headers(super_admin)
    )
    assert len(response.json()) == 9 and {row["state"] for row in response.json()} == {
        "missing"
    }
    assert (
        await db_session.scalar(select(func.count()).select_from(StorageBinding)) == 0
    )
    assert all(row["location_config_revision"] is None for row in response.json())
    await ensure_legacy_bindings(db_session)
    response = await httpx_client.get(
        "/api/v1/storage-locations/bindings", headers=headers(super_admin)
    )
    assert len(response.json()) == 9 and {row["state"] for row in response.json()} == {
        "configured"
    }
    buckets = {row["purpose"]: row["bucket"] for row in response.json()}
    assert buckets["datasets"] == buckets["export"]
    assert all(row["location_config_revision"] == 1 for row in response.json())
    assert {row["read_policy"] for row in response.json()} == {
        "consumer_owned_compatibility"
    }
    assert {row["write_policy"] for row in response.json()} == {
        "deployment_mapping",
        "current_binding",
    }
    assert {row["change_policy"] for row in response.json()} == {
        "legacy_mapping_locked",
        "reviewed_avatar_switch",
        "reviewed_bug_attachment_switch",
        "reviewed_audit_archive_switch",
    }
    training = await db_session.get(StorageBinding, "training")
    training.generation = 2
    location = await db_session.get(StorageLocation, training.location_id)
    location.config_revision = 7
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/storage-locations/bindings", headers=headers(super_admin)
    )
    rows = {row["purpose"]: row for row in response.json()}
    assert len(rows) == 9
    assert rows["training"]["generation"] == 2
    assert rows["training"]["location_config_revision"] == 7
    assert {row["state"] for row in rows.values()} == {"mismatch"}
    assert rows["avatars"]["change_policy"] == "reviewed_avatar_switch"
    assert {
        row["change_policy"]
        for key, row in rows.items()
        if key not in {"avatars", "bug-reports", "audit-archive"}
    } == {"legacy_mapping_locked"}
    await db_session.refresh(training)
    await db_session.refresh(location)
    assert training.generation == 2 and location.config_revision == 7


async def test_binding_policies_remain_super_admin_only(
    httpx_client, project_admin, annotator
):
    for actor in (project_admin, annotator):
        response = await httpx_client.get(
            "/api/v1/storage-locations/bindings", headers=headers(actor)
        )
        assert response.status_code == 403
        assert "read_policy" not in response.text


async def test_connector_location_preserves_scope_revision_and_reference_guards(
    httpx_client, project_admin, super_admin, db_session, monkeypatch
):
    from cryptography.fernet import Fernet
    from app.db.models.storage_connection import StorageConnection

    monkeypatch.setattr(
        settings, "connector_encryption_key", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(settings, "connector_host_allowlist", ["8.8.8.8/32"])
    payload = {
        "name": "private connector",
        "kind": "s3",
        "config": {"endpoint": "http://8.8.8.8:9000", "bucket": "data"},
        "secret": {"access_key": "KEY", "secret_key": "SECRET"},
        "scope": "owner",
    }
    response = await httpx_client.post(
        "/api/v1/storage-connections", headers=headers(project_admin), json=payload
    )
    assert response.status_code == 201, response.text
    connection_id = uuid.UUID(response.json()["id"])
    location_id = svc.connector_location_id(connection_id)
    base = f"/api/v1/storage-locations/{location_id}"
    response = await httpx_client.get(base, headers=headers(project_admin))
    assert response.status_code == 200 and response.json()["connection_id"] == str(
        connection_id
    )
    assert "SECRET" not in response.text and "8.8.8.8" not in response.text
    from app.services import storage_connection as connections

    async def probe(*args, **kwargs):
        return True, "ok", 0

    monkeypatch.setattr(connections, "_probe", probe)
    updated = await httpx_client.patch(
        f"/api/v1/storage-connections/{connection_id}",
        headers=headers(project_admin),
        json={
            "expected_revision": 1,
            "config": {"endpoint": "http://8.8.8.8:9000", "bucket": "other"},
        },
    )
    response = await httpx_client.get(base, headers=headers(project_admin))
    assert updated.status_code == 200, updated.text
    assert response.json()["config_revision"] == 2
    binding = StorageBinding(
        purpose="test-pinned", location_id=location_id, bucket="pinned"
    )
    db_session.add(binding)
    await db_session.flush()
    for method, data in [
        ("delete", None),
        ("patch", {"expected_revision": 2, "config": payload["config"]}),
    ]:
        kwargs = {"json": data} if data else {}
        response = await httpx_client.request(
            method,
            f"/api/v1/storage-connections/{connection_id}",
            headers=headers(project_admin),
            **kwargs,
        )
        assert response.status_code == 409
    await db_session.delete(binding)
    await db_session.flush()
    response = await httpx_client.delete(
        f"/api/v1/storage-connections/{connection_id}", headers=headers(project_admin)
    )
    assert response.status_code == 204, response.text
    assert await db_session.get(StorageConnection, connection_id) is None
    assert await db_session.get(StorageLocation, location_id) is None


async def test_no_deployment_roots_exposes_no_host_paths(
    httpx_client, super_admin, monkeypatch
):
    monkeypatch.setattr(settings, "storage_filesystem_roots", {})
    assert (
        await httpx_client.get(
            "/api/v1/storage-locations/roots", headers=headers(super_admin)
        )
    ).json() == []
    response = await httpx_client.post(
        "/api/v1/storage-locations",
        headers=headers(super_admin),
        json={"name": "unconfigured", "root_handle": "ceph"},
    )
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "root_not_configured"
    )
