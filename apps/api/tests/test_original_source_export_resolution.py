"""Bounded export resolution without changing canonical source authority."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import DatasetItem
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services import original_sources as sources, task_originals
from app.services.content_snapshot import ContentError
from tests.test_original_sources import add_link, dataset_rows
from tests.test_task_originals import (
    original_client as original_client,
    original_db as original_db,
    verified,
)


class ReadDB:
    def __init__(self):
        self.no_autoflush = nullcontext()
        self.events = []
        self.commit_error = None

    async def connection(self):
        return self

    async def begin_nested(self):
        self.events.append("begin")
        return self

    async def rollback(self):
        self.events.append("rollback")

    async def commit(self):
        self.events.append("commit")
        if self.commit_error is not None:
            error, self.commit_error = self.commit_error, None
            raise error


def source(*, media="image", item_id=None, **changes):
    item_id = item_id or uuid.uuid4()
    asset_id = uuid.uuid4()
    return sources.OriginalSource(
        **{
            "owner_kind": "dataset_asset",
            "content_id": asset_id,
            "sha256": "a" * 64,
            "size_bytes": 5,
            "media_type": media,
            "item_id": item_id,
            "relation": None,
            "bucket": "saved-original",
            "object_key": f"originals/{item_id}",
            "endpoint_url": "http://saved.test:9000",
            "location_id": uuid.uuid4(),
            "location_revision": 1,
            "maintenance_revision": 1,
            "ownership_facts": sources.AssetFacts(
                asset_id, item_id, media, "a" * 64, 5
            ),
            "item_facts": sources.ItemFacts(
                item_id, uuid.uuid4(), f"originals/{item_id}", media, None, None
            ),
            "replica_facts": None,
            "location_facts": None,
            **changes,
        }
    )


def relation(primary, *, cameras=(), task_id=None, **changes):
    task_id = task_id or uuid.uuid4()
    primary_link = sources.LinkFact(
        uuid.uuid4(), task_id, primary.item_id, "primary_lidar", "lidar"
    )
    camera_links = tuple(
        sources.LinkFact(
            uuid.uuid4(), task_id, camera.item_id, f"camera_{index}", str(index)
        )
        for index, camera in enumerate(cameras)
    )
    return sources.TaskRelation(
        **{
            "task_id": task_id,
            "project_id": uuid.uuid4(),
            "batch_id": uuid.uuid4(),
            "file_path": primary.object_key,
            "file_type": primary.media_type,
            "original_id": None,
            "direct_item_id": primary.item_id,
            "primary_item_id": primary.item_id,
            "primary_link_id": primary_link.id,
            "item_id": primary.item_id,
            "links": (primary_link, *camera_links),
            **changes,
        }
    )


def fake_sources(monkeypatch, relations, items):
    calls = {"relations": [], "items": []}

    async def read_relation(db, task_id, lock, **options):
        calls["relations"].append((task_id, lock, options))
        return relations[task_id]

    async def read_item(db, item_id, lock):
        calls["items"].append((item_id, lock))
        return items[item_id]

    monkeypatch.setattr(sources, "_relation", read_relation)
    monkeypatch.setattr(sources, "_item", read_item)
    return calls


@pytest.mark.parametrize("lock", [False, True])
@pytest.mark.parametrize("camera_first", [False, True])
async def test_cache_reuses_shared_item_and_full_relations_across_tasks(
    monkeypatch, lock, camera_first
):
    primary, camera = source(media="point_cloud"), source()
    first = relation(primary, cameras=(camera, camera))
    second = relation(primary, cameras=(camera,))
    calls = fake_sources(
        monkeypatch,
        {row.task_id: row for row in (first, second)},
        {row.item_id: row for row in (primary, camera)},
    )
    db = ReadDB()
    resolver = sources.CachedResolver(db, lock=lock)
    selected = first.links[1]
    if camera_first:
        assert await resolver.link(first.task_id, selected.id) == replace(
            camera, relation=first
        )
    for current in (first, second):
        assert await resolver.task(current.task_id) == replace(
            primary, relation=current
        )
        for link in current.links[1:]:
            assert await resolver.link(current.task_id, link.id) == replace(
                camera, relation=current
            )
    assert await resolver.task(first.task_id) == replace(primary, relation=first)
    assert [call[0] for call in calls["relations"]] == [first.task_id, second.task_id]
    assert sorted(calls["items"]) == sorted(
        [(primary.item_id, lock), (camera.item_id, lock)]
    )
    assert all(call[1] is lock for call in calls["relations"])
    if lock:
        assert db.events == ["begin", "commit"] * (7 if camera_first else 6)
    else:
        assert db.events == []


async def test_camera_only_cache_does_not_create_a_primary_source(monkeypatch):
    camera = source()
    current = relation(
        camera,
        cameras=(camera,),
        direct_item_id=None,
        primary_item_id=None,
        primary_link_id=None,
        item_id=None,
    )
    current = replace(current, links=current.links[1:])
    calls = fake_sources(
        monkeypatch, {current.task_id: current}, {camera.item_id: camera}
    )
    resolver = sources.CachedResolver(ReadDB(), lock=True)
    selected = current.links[0]
    expected = replace(camera, relation=current)
    assert await resolver.link(current.task_id, selected.id) == expected
    with pytest.raises(ContentError, match="no_readable_original"):
        await resolver.task(current.task_id)
    assert await resolver.link(current.task_id, selected.id) == expected
    assert calls["relations"] == [(current.task_id, True, {"allow_missing": True})]
    assert calls["items"] == [(camera.item_id, True)]


@pytest.mark.parametrize("change", ["path", "type"])
async def test_cached_item_still_checks_primary_binding(monkeypatch, change):
    camera = source()
    current = relation(
        camera,
        cameras=(camera,),
        **({"file_path": "wrong-key"} if change == "path" else {"file_type": "video"}),
    )
    calls = fake_sources(
        monkeypatch, {current.task_id: current}, {camera.item_id: camera}
    )
    resolver = sources.CachedResolver(ReadDB())
    await resolver.link(current.task_id, current.links[1].id)
    with pytest.raises(ContentError, match="content_binding_changed"):
        await resolver.task(current.task_id)
    assert calls["items"] == [(camera.item_id, False)]


async def test_cached_links_preserve_exact_task_role_and_camera_media(monkeypatch):
    primary, camera = source(media="point_cloud"), source()
    first, second = relation(primary, cameras=(camera,)), relation(primary)
    invalid = sources.LinkFact(
        uuid.uuid4(), first.task_id, camera.item_id, "camera_", None
    )
    first = replace(first, links=(*first.links, invalid))
    calls = fake_sources(
        monkeypatch,
        {row.task_id: row for row in (first, second)},
        {row.item_id: row for row in (primary, camera)},
    )
    resolver = sources.CachedResolver(ReadDB(), lock=True)
    await resolver.task(first.task_id)
    await resolver.task(second.task_id)
    for selected in (first.links[0], invalid):
        with pytest.raises(ContentError, match="original_link_invalid"):
            await resolver.link(first.task_id, selected.id)
    with pytest.raises(ContentError) as error:
        await resolver.link(second.task_id, first.links[1].id)
    assert error.value.code == "content_link_missing" and error.value.status == 404
    assert calls["items"] == [(primary.item_id, True)]
    wrong = replace(primary, media_type="video")
    third = relation(primary, cameras=(wrong,))
    calls = fake_sources(monkeypatch, {third.task_id: third}, {wrong.item_id: wrong})
    resolver = sources.CachedResolver(ReadDB(), lock=True)
    with pytest.raises(ContentError, match="camera_original_not_image"):
        await resolver.link(third.task_id, third.links[1].id)
    assert calls["items"] == [(wrong.item_id, True)]


@pytest.mark.parametrize("failure", ["busy", "invalid", "interrupted", "commit"])
async def test_failed_reads_never_cache_facts_from_a_rolled_back_savepoint(
    monkeypatch, failure
):
    camera = source()
    current = relation(camera, cameras=(camera,))
    calls = fake_sources(
        monkeypatch, {current.task_id: current}, {camera.item_id: camera}
    )
    read_item = sources._item
    db = ReadDB()
    attempts = 0
    if failure == "busy":
        error = DBAPIError("SELECT", {}, SimpleNamespace(sqlstate="55P03"))
    elif failure == "invalid":
        error = ContentError("content_unverified")
    else:
        error = KeyboardInterrupt()
    if failure == "commit":
        db.commit_error = error

    async def maybe_fail(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1 and failure != "commit":
            raise error
        return await read_item(*args)

    monkeypatch.setattr(sources, "_item", maybe_fail)
    resolver = sources.CachedResolver(db, lock=True)
    with pytest.raises(
        ContentError if failure in {"busy", "invalid"} else BaseException
    ):
        await resolver.link(current.task_id, current.links[1].id)
    assert not resolver._relations and not resolver._items and not resolver._links
    assert await resolver.link(current.task_id, current.links[1].id) == replace(
        camera, relation=current
    )
    assert len(calls["relations"]) == attempts == 2
    assert db.events == [
        "begin",
        "commit" if failure == "commit" else "rollback",
        "begin",
        "commit",
    ]


class RelationDB(ReadDB):
    def __init__(self, task, rows, *, any_links=False):
        super().__init__()
        self.task, self.rows, self.any_links = task, rows, any_links
        self.statements = []

    async def scalar(self, statement):
        self.statements.append(statement)
        if statement.column_descriptions[0].get("entity") is Task:
            return self.task
        return self.any_links

    async def scalars(self, statement):
        self.statements.append(statement)
        return self.rows


@pytest.mark.parametrize("selected", [False, True])
async def test_compact_query_restricts_link_hydration_before_loading(selected):
    primary = source(media="point_cloud")
    current = relation(primary, cameras=(source(), source()))
    task = SimpleNamespace(
        id=current.task_id,
        project_id=current.project_id,
        batch_id=current.batch_id,
        file_path=current.file_path,
        file_type=current.file_type,
        original_id=None,
        dataset_item_id=current.direct_item_id,
    )
    rows = [
        SimpleNamespace(
            id=link.id,
            task_id=link.task_id,
            dataset_item_id=link.item_id,
            role=link.role,
            sensor_name=link.sensor_name,
        )
        for link in current.links[: 2 if selected else 1]
    ]
    db = RelationDB(task, rows)
    await sources._relation(
        db,
        task.id,
        True,
        compact_relation=True,
        link_id=current.links[1].id if selected else None,
    )
    query = db.statements[1]
    compiled = query.compile(dialect=postgresql.dialect())
    assert "task_dataset_item_links.role =" in str(compiled)
    assert "primary_lidar" in compiled.params.values()
    assert (current.links[1].id in compiled.params.values()) is selected
    assert query._for_update_arg.read and query._for_update_arg.nowait


async def test_compact_independent_owner_detects_unhydrated_nonprimary_links():
    task = SimpleNamespace(
        id=uuid.uuid4(),
        original_id=uuid.uuid4(),
        dataset_item_id=None,
    )
    db = RelationDB(task, [], any_links=True)
    with pytest.raises(ContentError, match="original_relation_conflict"):
        await sources._relation(db, task.id, True, compact_relation=True)
    exists = db.statements[-1].compile(dialect=postgresql.dialect())
    assert "EXISTS (SELECT" in str(exists)
    assert "primary_lidar" not in exists.params.values()
    assert task.id in exists.params.values()


async def test_database_compact_matches_full_primary_and_selected_facts(original_db):
    db = original_db
    primary, _, _, task = await dataset_rows(db, file_type="point_cloud")
    camera, _, _, _ = await dataset_rows(db)
    main = await add_link(db, task.id, primary.id)
    selected = await add_link(db, task.id, camera.id, role="camera_front")
    neighbor = await add_link(db, task.id, camera.id, role="camera_back")
    full = await sources.resolve_task(db, task.id, lock=True)
    compact = await sources.resolve_task(db, task.id, lock=True, compact_relation=True)
    assert compact == replace(
        full,
        relation=replace(
            full.relation,
            links=tuple(row for row in full.relation.links if row.id == main.id),
        ),
    )
    full_camera = await sources.resolve_task_link(db, task.id, selected.id, lock=True)
    compact_camera = await sources.resolve_task_link(
        db, task.id, selected.id, lock=True, compact_relation=True
    )
    assert compact_camera == replace(
        full_camera,
        relation=replace(
            full_camera.relation,
            links=tuple(
                row for row in full_camera.relation.links if row.id != neighbor.id
            ),
        ),
    )
    neighbor.sensor_name = "changed-neighbor"
    await db.flush()
    assert await sources.resolve_task_link(db, task.id, selected.id) != full_camera
    assert (
        await sources.resolve_task_link(db, task.id, selected.id, compact_relation=True)
        == compact_camera
    )


async def test_database_cache_resolves_each_relation_and_item_once(
    original_db, monkeypatch
):
    db = original_db
    primary, _, _, task = await dataset_rows(db, file_type="point_cloud")
    camera, _, _, camera_task = await dataset_rows(db)
    first = await add_link(db, task.id, camera.id, role="camera_front")
    second = await add_link(db, task.id, camera.id, role="camera_back")
    expected = await sources.resolve_task(db, task.id, lock=True)
    expected_camera = await sources.resolve_task_link(db, task.id, first.id, lock=True)
    expected_camera_task = await sources.resolve_task(db, camera_task.id, lock=True)
    original_relation, original_item = sources._relation, sources._item
    calls = {"relations": [], "items": []}

    async def read_relation(db, task_id, lock, **options):
        calls["relations"].append(task_id)
        return await original_relation(db, task_id, lock, **options)

    async def read_item(db, item_id, lock):
        calls["items"].append(item_id)
        return await original_item(db, item_id, lock)

    monkeypatch.setattr(sources, "_relation", read_relation)
    monkeypatch.setattr(sources, "_item", read_item)
    resolver = sources.CachedResolver(db, lock=True)
    assert await resolver.link(task.id, first.id) == expected_camera
    assert await resolver.task(task.id) == expected
    assert await resolver.link(task.id, second.id) == expected_camera
    assert await resolver.task(camera_task.id) == expected_camera_task
    assert await resolver.task(task.id) == expected
    assert calls == {
        "relations": [task.id, camera_task.id],
        "items": [camera.id, primary.id],
    }


async def test_database_cache_busy_retry_preserves_pending_writes(
    original_db, monkeypatch
):
    db = original_db
    _, _, _, task = await dataset_rows(db, file_type="point_cloud")
    camera, _, _, _ = await dataset_rows(db)
    link = await add_link(db, task.id, camera.id, role="camera_front")
    await db.commit()
    task_id, link_id, camera_id = task.id, link.id, camera.id
    pending = DatasetItem(
        id=uuid.uuid4(), dataset_id=uuid.uuid4(), file_name="not-flushed"
    )
    db.add(pending)
    resolver = sources.CachedResolver(db, lock=True)
    original_relation = sources._relation
    reads = 0

    async def read_relation(*args, **kwargs):
        nonlocal reads
        reads += 1
        return await original_relation(*args, **kwargs)

    monkeypatch.setattr(sources, "_relation", read_relation)
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as blocker:
        await blocker.scalar(
            select(DatasetItem)
            .where(DatasetItem.id == camera_id)
            .with_for_update(nowait=True)
        )
        try:
            with pytest.raises(ContentError, match="storage_original_busy"):
                await resolver.link(task_id, link_id)
            assert pending in db.new
            with db.no_autoflush:
                assert await db.scalar(text("SELECT 1")) == 1
        finally:
            await blocker.rollback()
    assert (await resolver.link(task_id, link_id)).item_id == camera_id
    assert reads == 2 and pending in db.new
    db.expunge(pending)


async def test_database_compact_does_not_lock_unrelated_camera_rows(original_db):
    db = original_db
    _, _, _, task = await dataset_rows(db, file_type="point_cloud")
    camera, _, _, _ = await dataset_rows(db)
    selected = await add_link(db, task.id, camera.id, role="camera_front")
    neighbor = await add_link(db, task.id, camera.id, role="camera_back")
    await db.commit()
    task_id, selected_id, neighbor_id = task.id, selected.id, neighbor.id
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as blocker:
        await blocker.scalar(
            select(TaskDatasetItemLink)
            .where(TaskDatasetItemLink.id == neighbor_id)
            .with_for_update(nowait=True)
        )
        try:
            with pytest.raises(ContentError, match="storage_original_busy"):
                await sources.resolve_task_link(db, task_id, selected_id, lock=True)
            result = await sources.resolve_task_link(
                db, task_id, selected_id, lock=True, compact_relation=True
            )
            assert {row.id for row in result.relation.links} == {selected_id}
            assert await db.scalar(select(1)) == 1
        finally:
            await blocker.rollback()


async def test_database_compact_task_original_rejects_any_link(
    original_db, original_client
):
    db = original_db
    task, allocation, created = await verified(db, original_client)
    await task_originals.publish(db, task, allocation, created)
    expected = await sources.resolve_task(db, task.id, lock=True)
    assert (
        await sources.resolve_task(db, task.id, lock=True, compact_relation=True)
        == expected
    )
    resolver = sources.CachedResolver(db, lock=True)
    calls = list(original_client.calls)
    assert await resolver.task(task.id) == expected
    assert await resolver.task(task.id) == expected
    assert original_client.calls == calls
    camera, _, _, _ = await dataset_rows(db)
    # The per-test schema owns its triggers. Inject legacy corruption explicitly
    # because the current writer already rejects this independent-owner clash.
    await db.execute(
        text(
            "ALTER TABLE task_dataset_item_links "
            "DISABLE TRIGGER trg_task_original_dataset_link"
        )
    )
    try:
        await add_link(db, task.id, camera.id, role="camera_front")
    finally:
        await db.execute(
            text(
                "ALTER TABLE task_dataset_item_links "
                "ENABLE TRIGGER trg_task_original_dataset_link"
            )
        )
    for compact in (False, True):
        with pytest.raises(ContentError, match="original_relation_conflict"):
            await sources.resolve_task(db, task.id, lock=True, compact_relation=compact)
        assert await db.scalar(select(1)) == 1
