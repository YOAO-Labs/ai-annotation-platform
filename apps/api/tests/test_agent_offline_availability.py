"""Heartbeat projection preserves metadata and independent retained content."""

from datetime import timedelta
import uuid

import pytest
from sqlalchemy import delete, func, select

from app.db.models.dataset_asset import StorageLocation
from app.db.models.dataset_source import DatasetSourceItem, DatasetSourcePreview
from app.db.models.site_agent import AgentCredential, AgentRegistration, Site
from app.services import (
    agent_availability,
    agent_storage,
    agent_transport,
    dataset_workbench,
)
from app.services.filesystem_storage import FilesystemStorageError
from app.services.site_agents import HEARTBEAT_TTL
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_changes import accept_all_files
from tests.test_reference_original_cache import (
    open_image,
    referenced_cache as referenced_cache,
    seed_cache,
)


async def attach_agent(db, location, actor):
    stamp = await db.scalar(select(func.now()))
    site = Site(name=f"Remote-{uuid.uuid4()}", created_by=actor.id)
    db.add(site)
    await db.flush()
    agent = AgentRegistration(
        site_id=site.id,
        name="Remote Agent",
        last_seen=stamp,
        roots=[{"root_handle": "images", "root_revision": 3}],
        limits={},
        reported_limits={},
        created_by=actor.id,
    )
    db.add(agent)
    await db.flush()
    credential = AgentCredential(
        agent_id=agent.id,
        revision=agent.credential_revision,
        token_digest=uuid.uuid4().hex * 2,
        expires_at=stamp + timedelta(days=1),
    )
    db.add(credential)
    location.kind = "agent"
    location.config = {
        **location.config,
        "agent_id": str(agent.id),
        "root_handle": "images",
        "root_revision": 3,
    }
    await db.flush()
    return agent, credential, site


@pytest.mark.parametrize("preview", [False, True])
async def test_offline_sources_keep_search_pagination_and_independent_preview(
    db_session, content_world, super_admin, httpx_client, monkeypatch, preview
):
    db, world = db_session, content_world
    await accept_all_files(db, world)
    location, source = world[4:6]
    if preview:
        source.cache_policy = "preview"
        item = await db.scalar(
            select(DatasetSourceItem).where(DatasetSourceItem.source_id == source.id)
        )
        db.add(
            DatasetSourcePreview(
                source_id=source.id,
                path_digest=item.path_digest,
                asset_id=item.asset_id,
                source_sha256=item.content_sha256,
                spec="test",
                policy_revision=source.policy_revision,
                state="ready",
            )
        )
    agent, _, site = await attach_agent(db, location, world[2])
    agent.last_seen = None
    await db.commit()

    async def no_source_io(*args, **kwargs):
        pytest.fail("Metadata lists must not dispatch remote operations")

    monkeypatch.setattr(agent_transport, "exchange", no_source_io)
    auth = headers(super_admin)
    source_page = await httpx_client.get(
        f"/api/v1/dataset-sources?dataset_id={world[3].id}&limit=1", headers=auth
    )
    assert source_page.status_code == 200, source_page.text
    source_status = source_page.json()["items"][0]["agent_status"]
    assert source_status["state"] == "offline"
    assert source_status["site_name"] == site.name
    assert source_status["agent_name"] == agent.name
    assert source_status["last_seen"] is None
    assert set(source_status) == {
        "state",
        "reason_code",
        "site_name",
        "agent_name",
        "last_seen",
        "measured_at",
    }
    state = "preview_only" if preview else "unavailable"
    url = f"/api/v1/datasets/{world[3].id}"
    page = await httpx_client.get(
        url + f"/items?availability={state}&search=图片&limit=1", headers=auth
    )
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 1
    item = page.json()["items"][0]
    assert item["file_name"] == "图片 空格.png"
    availability = item["availability"]
    assert availability["state"] == state
    assert availability["reason_code"] == "agent_offline"
    assert availability["agent_status"]["state"] == "offline"
    assert availability["preview_available"] is preview
    assert ("read_preview" in availability["allowed_actions"]) is preview
    assert "read_original" not in availability["allowed_actions"]
    assert not availability["original_cache_recorded"]
    counts = (await httpx_client.get(url + "/workbench", headers=auth)).json()
    assert counts["availability_counts"][state] == 1
    next_page = await httpx_client.get(url + "/items?limit=1&offset=1", headers=auth)
    assert next_page.json()["total"] == 1 and next_page.json()["items"] == []
    assert str(world[0]) not in str(item)


async def test_current_heartbeat_credentials_and_bound_root_share_one_projection(
    db_session, content_world, project_admin
):
    db, world = db_session, content_world
    await accept_all_files(db, world)
    agent, credential, _ = await attach_agent(db, world[4], world[2])
    stamp = await db.scalar(select(func.now()))

    async def current():
        await db.flush()
        return (await agent_availability.for_locations(db, [world[4].id]))[world[4].id]

    assert (await current())["state"] == "online"
    assert (await agent_storage.configuration(db, world[4])).location_id == world[4].id
    agent.last_seen = stamp - HEARTBEAT_TTL
    assert (await current())["reason_code"] == "agent_offline"
    with pytest.raises(FilesystemStorageError, match="agent_offline"):
        await agent_storage.configuration(db, world[4])
    agent.last_seen = stamp - HEARTBEAT_TTL + timedelta(microseconds=1)
    assert (await current())["state"] == "online"
    agent.draining = True
    assert (await current())["reason_code"] == "agent_draining"
    with pytest.raises(FilesystemStorageError, match="agent_draining"):
        await agent_storage.configuration(db, world[4])
    agent.draining = False
    agent.roots = [{"root_handle": "other", "root_revision": 3}]
    assert (await current())["reason_code"] == "agent_root_revision_changed"
    with pytest.raises(FilesystemStorageError, match="agent_root_revision_changed"):
        await agent_storage.configuration(db, world[4])
    agent.roots = [{"root_handle": "images", "root_revision": 2}]
    assert (await current())["reason_code"] == "agent_root_revision_changed"
    agent.roots = [{"root_handle": "images", "root_revision": 3}]
    credential.expires_at = stamp
    assert (await current())["reason_code"] == "credential_expired"
    with pytest.raises(FilesystemStorageError, match="credential_expired"):
        await agent_storage.configuration(db, world[4])
    credential.expires_at = stamp + timedelta(days=1)
    agent.revoked_at = stamp
    assert (await current())["reason_code"] == "agent_revoked"
    with pytest.raises(FilesystemStorageError, match="agent_revoked"):
        await agent_storage.configuration(db, world[4])

    observations = dataset_workbench.observations(world[3].id, project_admin[0])
    hidden = (await db.execute(select(observations))).mappings().one()
    projected = dataset_workbench.availability(hidden)
    assert projected["agent_status"] is None
    assert projected["source_name"] is None
    world[4].kind = "filesystem"
    world[4].config = {**world[4].config, "agent_id": "not-a-uuid"}
    assert await agent_availability.for_locations(db, [world[4].id]) == {}


async def test_ready_same_content_cache_is_still_readable_with_offline_agent(
    referenced_cache, monkeypatch
):
    world = referenced_cache
    await seed_cache(world)
    async with world.factory() as db:
        location = await db.get(StorageLocation, world.location_id)
        agent, _, site = await attach_agent(db, location, world.manager)
        agent.last_seen = None
        agent_id, site_id = agent.id, site.id
        await db.commit()

    async def no_source_io(*args, **kwargs):
        pytest.fail("A complete same-content cache must not dispatch source I/O")

    monkeypatch.setattr(agent_transport, "exchange", no_source_io)
    try:
        async with world.factory() as db:
            ids = await dataset_workbench.available_cache_ids(
                db, world.dataset_id, world.manager
            )
            obs = dataset_workbench.observations(
                world.dataset_id, world.manager, cache_entry_ids=ids
            )
            row = (await db.execute(select(obs))).mappings().one()
            projected = dataset_workbench.availability(row)
            assert projected["state"] == "unavailable"
            assert projected["reason_code"] == "agent_offline"
            assert projected["original_cache_recorded"]
            assert "read_original" in projected["allowed_actions"]
        image = await open_image(world)
        try:
            assert image.file.read() == world.data and image.sha256 == world.sha
        finally:
            image.close()
        from app.services.training_dataset_sources import training_reference_snapshot

        sample = {
            "source_key": world.task.file_path,
            "dataset_item_id": str(world.item_id),
            "task_id": str(world.task.id),
            "dataset_id": str(world.dataset_id),
        }
        async with world.factory() as db:
            image, identity, _ = await training_reference_snapshot(
                db, sample, world.actor, freeze_lineage=True
            )
            try:
                assert image.file.read() == world.data
                assert identity["sha256"] == world.sha
            finally:
                image.close()
    finally:
        async with world.factory() as db:
            await db.execute(
                delete(AgentCredential).where(AgentCredential.agent_id == agent_id)
            )
            await db.execute(
                delete(AgentRegistration).where(AgentRegistration.id == agent_id)
            )
            await db.execute(delete(Site).where(Site.id == site_id))
            await db.commit()


async def test_offline_uncached_original_blocks_freeze_without_dispatch(
    referenced_cache, monkeypatch
):
    from app.services.dataset_references import reference_context
    from app.services.training_dataset_bridge import TrainingDatasetError
    from app.services.training_dataset_sources import training_reference_snapshot

    world = referenced_cache
    async with world.factory() as db:
        context = await reference_context(db, world.item_id, world.manager)
        location = await db.get(StorageLocation, world.location_id)
        agent, _, site = await attach_agent(db, location, world.manager)
        agent.last_seen = None
        location.config = {**location.config, "agent_anchor": context["origin_anchor"]}
        agent_id, site_id = agent.id, site.id
        await db.commit()

    async def no_source_io(*args, **kwargs):
        pytest.fail("An offline source cannot dispatch freeze reads")

    monkeypatch.setattr(agent_transport, "exchange", no_source_io)
    sample = {
        "source_key": world.task.file_path,
        "dataset_item_id": str(world.item_id),
        "task_id": str(world.task.id),
        "dataset_id": str(world.dataset_id),
    }
    try:
        async with world.factory() as db:
            with pytest.raises(TrainingDatasetError) as error:
                await training_reference_snapshot(
                    db, sample, world.actor, freeze_lineage=True
                )
            assert error.value.code == "reference_media_unavailable"
            assert error.value.__cause__.code == "agent_offline"
    finally:
        async with world.factory() as db:
            await db.execute(
                delete(AgentCredential).where(AgentCredential.agent_id == agent_id)
            )
            await db.execute(
                delete(AgentRegistration).where(AgentRegistration.id == agent_id)
            )
            await db.execute(delete(Site).where(Site.id == site_id))
            await db.commit()
