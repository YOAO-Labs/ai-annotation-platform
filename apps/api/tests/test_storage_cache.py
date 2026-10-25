"""Public configuration, measured ownership and controlled cache retirement."""

from datetime import timedelta
import asyncio
from types import SimpleNamespace
import uuid

import httpx
from pydantic import ValidationError
import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.db.models.dataset_asset import StorageLocation
from app.db.models.api_key import ApiKey
from app.db.models.user import User
from app.db.models.original_cache import (
    OriginalCacheEntry as Entry,
    OriginalCacheReadLease,
)
from app.deps import get_db
from app.schemas.storage_location import FilesystemLocationCreate, StorageLocationUpdate
from app.services import directory_policy, original_cache as cache
from app.services.dataset_workbench import (
    availability,
    available_cache_ids,
    observations,
)
from tests.test_reference_original_cache import (
    referenced_cache as referenced_cache,
    seed_cache,
)
from tests.test_storage_locations_api import (
    root as root,
    create,
    headers,
    reviewed_patch,
)
from tests.test_dataset_source_changes import rescan, preflight


@pytest.mark.parametrize("source", ["none", "preview", "on_demand"])
@pytest.mark.parametrize("location", ["none", "preview", "on_demand"])
def test_effective_policy_is_the_lower_ceiling(source, location):
    values = ["none", "preview", "on_demand"]
    result = directory_policy.effective_cache_policy(
        SimpleNamespace(cache_policy=source),
        SimpleNamespace(config={"cache_policy": location}),
    )
    assert result == values[min(values.index(source), values.index(location))]
    assert directory_policy.permits_preview(result) == (
        source != "none" and location != "none"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("original_cache_quota_bytes", value)
        for value in (-1, True, "100", 1.5, 2**50 + 1)
    ]
    + [
        ("original_cache_ttl_seconds", value)
        for value in (59, 2592001, True, "86400", 60.5)
    ],
)
@pytest.mark.parametrize("schema", [FilesystemLocationCreate, StorageLocationUpdate])
def test_cache_budgets_reject_coercion_and_invalid_bounds(schema, field, value):
    identity = (
        {"name": "cache", "root_handle": "ceph"}
        if schema is FilesystemLocationCreate
        else {"expected_revision": 1}
    )
    with pytest.raises(ValidationError):
        schema.model_validate({**identity, field: value})


async def test_public_location_combines_patch_before_validating_budget(
    httpx_client, super_admin, project_admin, root
):
    location = await create(httpx_client, super_admin)
    assert location["original_cache_quota_bytes"] == 0
    assert location["original_cache_ttl_seconds"] == 86400
    path = f"/api/v1/storage-locations/{location['id']}"
    base = {"expected_revision": 1, "cache_policy": "on_demand"}
    assert (
        await reviewed_patch(
            httpx_client, path, headers=headers(super_admin), json=base
        )
    ).status_code == 422
    result = await reviewed_patch(
        httpx_client,
        path,
        headers=headers(super_admin),
        json={
            **base,
            "original_cache_quota_bytes": 1024,
            "original_cache_ttl_seconds": 61,
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["original_cache_ttl_seconds"] == 61
    assert result.json()["config_revision"] == 2
    assert (
        await reviewed_patch(
            httpx_client,
            path,
            headers=headers(super_admin),
            json={
                "expected_revision": 2,
                "original_cache_quota_bytes": 0,
            },
        )
    ).status_code == 422
    assert (
        await reviewed_patch(
            httpx_client,
            path,
            headers=headers(project_admin),
            json={
                "expected_revision": 2,
                "original_cache_quota_bytes": 2048,
            },
        )
    ).status_code == 403
    assert (
        await reviewed_patch(
            httpx_client,
            path,
            headers=headers(super_admin),
            json={
                "expected_revision": 1,
                "original_cache_quota_bytes": 2048,
            },
        )
    ).status_code == 409
    result = await reviewed_patch(
        httpx_client,
        path,
        headers=headers(super_admin),
        json={
            "expected_revision": 2,
            "cache_policy": "preview",
            "original_cache_quota_bytes": 0,
        },
    )
    assert (
        result.status_code == 200 and result.json()["original_cache_quota_bytes"] == 0
    )


@pytest.fixture
async def cache_http(referenced_cache, app_module):
    world = referenced_cache

    async def database():
        async with world.factory() as db:
            yield db

    previous = app_module.dependency_overrides.get(get_db)
    app_module.dependency_overrides[get_db] = database
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:
            yield (
                world,
                client,
                {
                    "Authorization": f"Bearer {create_access_token(subject=str(world.manager.id), role='super_admin')}"
                },
            )
    finally:
        if previous is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous


async def test_source_effective_policy_and_location_configuration_stay_separate(
    cache_http,
):
    world, client, auth = cache_http
    path = f"/api/v1/dataset-sources/{world.source_id}"
    source = (await client.get(path, headers=auth)).json()
    assert source["cache_policy"] == source["effective_cache_policy"] == "on_demand"
    result = await reviewed_patch(
        client,
        f"/api/v1/storage-locations/{world.location_id}",
        headers=auth,
        json={
            "expected_revision": world.content[4].config_revision,
            "cache_policy": "preview",
        },
    )
    assert result.status_code == 200
    source = (await client.get(path, headers=auth)).json()
    assert (
        source["cache_policy"] == "on_demand"
        and source["effective_cache_policy"] == "preview"
    )
    assert source["location_cache_policy"] == "preview"
    assert (
        await client.patch(
            path,
            headers=auth,
            json={
                "expected_revision": source["config_revision"],
                "original_cache_quota_bytes": 123,
            },
        )
    ).status_code == 422
    listed = (
        await client.get(
            f"/api/v1/dataset-sources?dataset_id={world.dataset_id}", headers=auth
        )
    ).json()
    assert listed["items"][0]["effective_cache_policy"] == "preview"


@pytest.mark.parametrize("leased", [False, True])
async def test_cleanup_seals_owned_generations_and_accounts_until_actual_delete(
    cache_http, leased
):
    world, client, auth = cache_http
    entry = await seed_cache(world)
    if leased:
        async with world.factory() as db:
            db.add(
                OriginalCacheReadLease(
                    id=uuid.uuid4(),
                    entry_id=entry.id,
                    expires_at=cache.now() + timedelta(seconds=60),
                )
            )
            await db.commit()
    path = f"/api/v1/storage-locations/{world.location_id}/original-cache"
    usage = (await client.get(path, headers=auth)).json()
    assert usage["charged_bytes"] == usage["ready"]["bytes"] == len(world.data)
    assert usage["ready"]["count"] == 1
    assert str(world.endpoint) not in str(usage) and world.bucket not in str(usage)
    payload = {
        "expected_revision": usage["expected_revision"],
        "recorded_before": usage["measured_at"],
    }
    assert (
        await client.post(
            path + "/cleanup", headers=auth, json={**payload, "expected_revision": 999}
        )
    ).status_code == 409
    denied = {
        "Authorization": f"Bearer {create_access_token(subject=str(world.actor.id), role='employee')}"
    }
    assert (await client.get(path, headers=denied)).status_code == 403
    assert (
        await client.post(path + "/cleanup", headers=denied, json=payload)
    ).status_code == 403
    result = await client.post(path + "/cleanup", headers=auth, json=payload)
    assert result.status_code == 202, result.text
    result = result.json()
    assert result["retired_count"] == int(not leased)
    assert result["skipped_leased_count"] == int(leased)
    assert result["usage"]["charged_bytes"] == len(world.data) and not world.deletions
    if not leased:
        world.fail_delete = True
        assert await cache.purge(world.factory, entry.id) == "deferred"
        failed = (await client.get(path, headers=auth)).json()
        assert failed["failed_count"] == 1 and failed["charged_bytes"] == len(
            world.data
        )
        world.fail_delete = False
        async with world.factory() as db:
            current = await db.get(Entry, entry.id)
            current.next_check_at = cache.now()
            await db.commit()
        assert await cache.purge(world.factory, entry.id) == "removed"
        assert (await client.get(path, headers=auth)).json()["charged_bytes"] == 0


async def test_cache_record_does_not_rewrite_source_missing_observation(
    referenced_cache,
):
    world = referenced_cache
    await seed_cache(world)
    (world.content[0] / world.content[7].relative_path).unlink()
    async with world.factory() as db:
        rescanned = await rescan(db, world.content)
        await preflight(db, rescanned)
    async with world.factory() as db:
        ids = await available_cache_ids(db, world.dataset_id, world.manager)
        obs = observations(world.dataset_id, world.manager, cache_entry_ids=ids)
        row = (
            (await db.execute(select(obs).where(obs.c.item_id == world.item_id)))
            .mappings()
            .one()
        )
        result = availability(row)
        assert result["state"] == "missing"
        assert (
            result["original_cache_recorded"]
            and "read_original" in result["allowed_actions"]
        )
        assert (
            result["original_cache_recorded_at"] < result["original_cache_expires_at"]
        )
        location = await db.get(StorageLocation, world.location_id)
        location.enabled = False
        await db.commit()
    async with world.factory() as db:
        ids = await available_cache_ids(db, world.dataset_id, world.manager)
        obs = observations(world.dataset_id, world.manager, cache_entry_ids=ids)
        row = (
            (await db.execute(select(obs).where(obs.c.item_id == world.item_id)))
            .mappings()
            .one()
        )
        assert (
            not row["original_cache_recorded"]
            and "read_original" not in availability(row)["allowed_actions"]
        )


@pytest.mark.parametrize("operation", ["patch", "cleanup"])
@pytest.mark.parametrize("change", ["key_rotation", "role", "disabled"])
async def test_admin_mutations_recheck_authority_after_waiting_for_locks(
    cache_http, monkeypatch, operation, change
):
    from app.api.v1 import storage_locations as routes

    world, client, auth = cache_http
    entry = await seed_cache(world)
    path = f"/api/v1/storage-locations/{world.location_id}"
    key = await client.post(
        "/api/v1/me/api-keys",
        headers=auth,
        json={"name": "cache lock test", "scopes": ["datasets:write"]},
    )
    assert key.status_code == 201
    credentials = key.json()
    key_id = uuid.UUID(credentials["id"])
    api_auth = {"Authorization": f"Bearer {credentials['plaintext']}"}
    reached = asyncio.Event()
    original = routes.key_authority

    async def observed(*args, **kwargs):
        proof = await original(*args, **kwargs)
        reached.set()
        return proof

    preview = await client.post(
        path + "/change-preview",
        headers=api_auth,
        json={
            "expected_revision": entry.location_revision,
            "original_cache_quota_bytes": 10000,
        },
    )
    assert preview.status_code == 200, preview.text
    monkeypatch.setattr(routes, "key_authority", observed)
    async with world.factory() as blocker:
        actor = await blocker.scalar(
            select(User).where(User.id == world.manager.id).with_for_update()
        )
        if operation == "patch":
            pending = asyncio.create_task(
                client.patch(
                    path,
                    headers=api_auth,
                    json={
                        "expected_revision": entry.location_revision,
                        "original_cache_quota_bytes": 10000,
                        "impact_token": preview.json()["impact_token"],
                    },
                )
            )
        else:
            pending = asyncio.create_task(
                client.post(
                    path + "/original-cache/cleanup",
                    headers=api_auth,
                    json={
                        "expected_revision": entry.location_revision,
                        "recorded_before": cache.now().isoformat(),
                    },
                )
            )
        try:
            await asyncio.wait_for(reached.wait(), timeout=5)
            if change == "key_rotation":
                async with world.factory() as db:
                    current = await db.get(ApiKey, key_id)
                    current.key_hash = "rotated-while-waiting"
                    await db.commit()
            elif change == "role":
                actor.role = "project_admin"
            else:
                actor.is_active = False
            await blocker.commit()
            result = await asyncio.wait_for(pending, timeout=10)
            assert result.status_code == 403
        finally:
            await blocker.rollback()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    async with world.factory() as db:
        assert (await db.get(Entry, entry.id)).state == "ready"
        assert (
            await db.get(StorageLocation, world.location_id)
        ).config_revision == entry.location_revision
