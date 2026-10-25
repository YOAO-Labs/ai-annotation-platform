"""Responsibility survives source outages without changing content authority."""

import asyncio
from datetime import datetime, timezone
import hashlib
import uuid
from unittest.mock import Mock

from fastapi import HTTPException
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.dataset_asset import StorageLocation
from app.db.models.dataset_source import DatasetSource, DatasetScanRun
from app.db.models.notification import Notification
from app.db.models.user import User
from app.schemas.dataset_source import SourceExternalProtectionUpdate
from app.services import dataset_content as content, dataset_scans as scans
from app.services import dataset_source_protection as protection
from app.services import filesystem_storage as fs
from tests.test_dataset_content import (
    content_world as content_world,
    create_content_world,
    headers,
)
from tests.test_dataset_source_access import managed_world as managed_world


def payload(**overrides):
    return {
        "expected_revision": 0,
        "responsible_party": "数据运维组",
        "recovery_method": "external_backup",
        "recovery_reference": "OPS:restore-source",
        **overrides,
    }


def url(source):
    return f"/api/v1/dataset-sources/{source.id}/external-protection"


async def test_declaration_preserves_verified_read_and_active_scan(
    content_world, db_session, httpx_client, super_admin, monkeypatch
):
    _, raw, actor, _, location, source, scan, entry = content_world
    verified = await content.scan_snapshot(
        db_session, scan.id, entry.path_digest, actor, verify=True
    )
    verified.close()
    assert (await scans.source_out(db_session, source))["external_protection"] is None
    active, _ = await scans.start_scan(db_session, source.id, actor)
    original = (source.config_revision, source.generation, source.policy_revision)
    forbid = Mock(side_effect=AssertionError("declaration must not access source"))
    with monkeypatch.context() as patch:
        patch.setattr(fs, "location_directory", forbid)
        response = await httpx_client.put(
            url(source), headers=headers(super_admin), json=payload()
        )
    assert response.status_code == 200, response.text
    record = response.json()["external_protection"]
    assert record["revision"] == 1 and record["responsible_party"] == "数据运维组"
    assert record["recorded_by_id"] == str(actor.id) and record["recorded_at"]
    assert not any(key in record for key in ("backed_up", "restored", "verified"))
    assert (
        source.config_revision,
        source.generation,
        source.policy_revision,
    ) == original
    await db_session.refresh(active)
    assert active.status == "pending"
    forbid.assert_not_called()
    response = await httpx_client.get(
        f"/api/v1/dataset-scans/{scan.id}/entries/{entry.path_digest}/content",
        params={"sha256": hashlib.sha256(raw).hexdigest()},
        headers=headers(super_admin),
    )
    assert response.status_code == 200 and response.content == raw
    summary = await httpx_client.get(
        f"/api/v1/storage-locations/{location.id}/source-summary",
        headers=headers(super_admin),
    )
    assert summary.status_code == 200
    assert summary.json()["items"][0]["external_protection"] == record


@pytest.mark.parametrize(
    "method", ["external_backup", "reacquire", "unrecoverable", "unknown"]
)
async def test_declared_methods_and_stale_revision(
    content_world, httpx_client, super_admin, method
):
    source = content_world[5]
    response = await httpx_client.put(
        url(source),
        headers=headers(super_admin),
        json=payload(recovery_method=method, recovery_reference=None),
    )
    assert response.status_code == 200, response.text
    first = response.json()["external_protection"]
    assert first["recovery_method"] == method and first["recovery_reference"] is None
    stale = await httpx_client.put(
        url(source), headers=headers(super_admin), json=payload()
    )
    assert stale.status_code == 409
    latest = await httpx_client.get(
        f"/api/v1/dataset-sources/{source.id}", headers=headers(super_admin)
    )
    assert latest.json()["external_protection"] == first
    updated = await httpx_client.put(
        url(source),
        headers=headers(super_admin),
        json=payload(expected_revision=1, responsible_party="接手团队"),
    )
    assert (
        updated.status_code == 200
        and updated.json()["external_protection"]["revision"] == 2
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"responsible_party": "  "},
        {"responsible_party": "private\nvalue"},
        {"recovery_reference": "https://private.invalid/secret"},
        {"recovery_reference": "/private/root"},
        {"recorded_by_id": str(uuid.uuid4())},
        {"recovery_method": "verified_backup"},
    ],
)
async def test_invalid_declaration_never_persists(
    content_world, httpx_client, super_admin, changes
):
    source = content_world[5]
    response = await httpx_client.put(
        url(source), headers=headers(super_admin), json=payload(**changes)
    )
    assert response.status_code == 422
    assert (
        source.external_protection is None and source.external_protection_revision == 0
    )


async def test_two_sources_at_same_location_keep_distinct_responsibility(
    content_world, db_session
):
    _, _, actor, dataset, location, first, _, _ = content_world
    second = DatasetSource(
        dataset_id=dataset.id,
        location_id=location.id,
        name="Other team",
        config=first.config,
    )
    db_session.add(second)
    await db_session.flush()
    for source, party in ((first, "team-a"), (second, "team-b")):
        await protection.update(
            db_session,
            source.id,
            SourceExternalProtectionUpdate(**payload(responsible_party=party)),
            actor,
        )
    assert first.external_protection["responsible_party"] == "team-a"
    assert second.external_protection["responsible_party"] == "team-b"


@pytest.mark.parametrize(
    "lifecycle", ["paused", "unregistered", "archived", "orphaned"]
)
async def test_declaration_survives_unavailable_and_retained_source(
    managed_world, db_session, httpx_client, project_admin, super_admin, lifecycle
):
    world, actor = managed_world
    dataset, location, source = world[3:6]
    first = await httpx_client.put(
        url(source), headers=headers(project_admin), json=payload()
    )
    assert first.status_code == 200, first.text
    if lifecycle == "archived":
        dataset.archived_at = datetime.now(timezone.utc)
        dataset.lifecycle_revision += 1
    elif lifecycle == "orphaned":
        await db_session.execute(delete(Dataset).where(Dataset.id == dataset.id))
    else:
        source.enabled = False
        if lifecycle == "unregistered":
            source.unregistered_at = datetime.now(timezone.utc)
    await db_session.commit()
    auth = headers(super_admin if lifecycle == "orphaned" else project_admin)
    response = await httpx_client.put(
        url(source),
        headers=auth,
        json=payload(expected_revision=1, recovery_method="unrecoverable"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["external_protection"]["revision"] == 2
    if lifecycle == "orphaned":
        assert response.json()["dataset_id"] is None
        assert (
            await httpx_client.get(
                f"/api/v1/dataset-sources/{source.id}", headers=headers(project_admin)
            )
        ).status_code == 404
    summary = (
        await httpx_client.get(
            f"/api/v1/storage-locations/{location.id}/source-summary", headers=auth
        )
    ).json()
    assert (
        summary["items"][0]["external_protection"]
        == response.json()["external_protection"]
    )


async def test_owner_or_location_revocation_denies_update(
    managed_world, db_session, httpx_client, project_admin
):
    world, _ = managed_world
    source = world[5]
    world[4].config = {**world[4].config, "use_user_ids": []}
    await db_session.commit()
    response = await httpx_client.put(
        url(source), headers=headers(project_admin), json=payload()
    )
    assert response.status_code == 404 and source.external_protection is None


async def test_write_scope_and_rotated_key_are_enforced(
    managed_world, db_session, httpx_client, project_admin, monkeypatch
):
    world, actor = managed_world
    key_response = await httpx_client.post(
        "/api/v1/me/api-keys",
        headers=headers(project_admin),
        json={"name": "protection", "scopes": ["datasets:read"]},
    )
    result = await httpx_client.put(
        url(world[5]),
        headers={"Authorization": "Bearer " + key_response.json()["plaintext"]},
        json=payload(),
    )
    assert result.status_code == 403
    key = await db_session.get(ApiKey, uuid.UUID(key_response.json()["id"]))
    key.scopes = ["datasets:write"]
    await db_session.flush()
    original = scans.source_context

    async def rotate(*args):
        result = await original(*args)
        key.key_hash = "rotated-while-waiting"
        await db_session.flush()
        return result

    monkeypatch.setattr(scans, "source_context", rotate)
    with pytest.raises(HTTPException) as caught:
        await protection.update(
            db_session,
            world[5].id,
            SourceExternalProtectionUpdate(**payload()),
            actor,
            api_key_id=key.id,
        )
    assert caught.value.status_code == 403 and world[5].external_protection is None


async def test_concurrent_declarations_have_one_winner(
    test_engine, tmp_path, monkeypatch
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        actor = User(
            id=uuid.uuid4(),
            email=f"protection-{uuid.uuid4().hex}@example.test",
            name="Declaration",
            role="super_admin",
            password_hash="test-only",
            is_active=True,
        )
        db.add(actor)
        await db.flush()
        world = await create_content_world(db, actor, tmp_path, monkeypatch)
        source_id, dataset_id, location_id, actor_id = (
            world[5].id,
            world[3].id,
            world[4].id,
            actor.id,
        )
        await db.commit()
    try:

        async def write(party):
            async with maker() as db:
                actor = await db.get(User, actor_id)
                try:
                    row = await protection.update(
                        db,
                        source_id,
                        SourceExternalProtectionUpdate(
                            **payload(responsible_party=party)
                        ),
                        actor,
                    )
                    await db.commit()
                    return row.external_protection["responsible_party"]
                except HTTPException as exc:
                    assert exc.status_code == 409
                    return "conflict"

        results = await asyncio.wait_for(
            asyncio.gather(write("a"), write("b")), timeout=8
        )
        assert results.count("conflict") == 1
        async with maker() as db:
            row = await db.get(DatasetSource, source_id)
            assert row.external_protection_revision == 1
            assert row.external_protection["responsible_party"] in results
    finally:
        async with maker() as db:
            await db.execute(
                delete(DatasetScanRun).where(DatasetScanRun.source_id == source_id)
            )
            await db.execute(delete(DatasetSource).where(DatasetSource.id == source_id))
            await db.execute(
                delete(Notification).where(Notification.user_id == actor_id)
            )
            await db.execute(delete(AsyncJob).where(AsyncJob.user_id == actor_id))
            await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
            await db.execute(
                delete(StorageLocation).where(StorageLocation.id == location_id)
            )
            await db.execute(delete(User).where(User.id == actor_id))
            await db.commit()
