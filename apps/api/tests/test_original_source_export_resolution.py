"""Bounded export resolution without changing canonical source authority."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import event, inspect, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.dataset import DatasetItem
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageLocation,
)
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.db.models.task import Task
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.services import original_sources as sources, task_originals
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
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

    async def read_item(db, item_id, lock, **options):
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

    async def maybe_fail(*args, **options):
        nonlocal attempts
        attempts += 1
        if attempts == 1 and failure != "commit":
            raise error
        return await read_item(*args, **options)

    monkeypatch.setattr(sources, "_item", maybe_fail)
    resolver = sources.CachedResolver(db, lock=True, max_links=len(current.links))
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

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(one_or_none=lambda: self.task)

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.any_links

    async def scalars(self, statement):
        self.statements.append(statement)
        limit = statement._limit_clause
        return self.rows if limit is None else self.rows[: limit.value]


class ProjectedDB(ReadDB):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        table = next(iter(statement.selected_columns)).table.name
        rows = self.rows.get(table, [])
        if statement._limit_clause is not None:
            rows = rows[: statement._limit_clause.value]
        return SimpleNamespace(
            all=lambda: rows,
            one_or_none=lambda: rows[0] if rows else None,
        )


@pytest.mark.parametrize("lock", [False, True])
async def test_bounded_replica_query_masks_large_metadata_and_reads_at_most_two(lock):
    db = ProjectedDB({"dataset_asset_locations": [object(), object(), object()]})
    assert len(await sources._replicas(db, uuid.uuid4(), lock, bounded=True)) == 2
    query = db.statements[0]
    assert query._limit_clause.value == 2
    # Raw locator/ETag columns must never cross the bounded read boundary.
    assert all(
        column is not DatasetAssetLocation.__table__.c.etag
        and column is not DatasetAssetLocation.__table__.c.storage_ref
        for column in query.selected_columns
    )
    compiled = query.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "CASE WHEN (octet_length(dataset_asset_locations.etag) <=" in sql
    assert "THEN dataset_asset_locations.etag END AS etag" in sql
    assert "octet_length(dataset_asset_locations.etag) >" in sql
    # The saved JSON locator is masked by the same bounded CASE treatment.
    assert "THEN dataset_asset_locations.storage_ref END AS storage_ref" in sql
    assert 1024 in compiled.params.values()
    assert 8192 in compiled.params.values()
    assert "substring" not in sql and "substr(" not in sql
    if lock:
        assert query._for_update_arg.read and query._for_update_arg.nowait
    else:
        assert query._for_update_arg is None


@pytest.mark.parametrize("lock", [False, True])
async def test_bounded_location_query_returns_exact_match_without_json_hydration(lock):
    db = ProjectedDB({"storage_locations": [object()]})
    assert (
        len(await sources._locations(db, [LEGACY_LOCATION_ID], lock, bounded=True)) == 1
    )
    query = db.statements[0]
    assert query._limit_clause.value == 2
    assert {column.name for column in query.selected_columns} == {
        "id",
        "kind",
        "enabled",
        "config_revision",
        "connection_id",
        "config_matches",
    }
    assert all(
        column is not StorageLocation.__table__.c.config
        for column in query.selected_columns
    )
    compiled = query.compile(dialect=postgresql.dialect())
    assert "storage_locations.config =" in str(compiled)
    assert {
        "endpoint": settings.minio_endpoint,
        "use_ssl": settings.minio_use_ssl,
    } in compiled.params.values()
    if lock:
        assert query._for_update_arg.read and query._for_update_arg.nowait
    else:
        assert query._for_update_arg is None


@pytest.mark.parametrize("lock", [False, True])
async def test_bounded_gate_projects_only_admission_facts(lock):
    db = ProjectedDB(
        {"storage_maintenance_gates": [SimpleNamespace(state="active", revision=7)]}
    )
    assert await sources._gate(db, LEGACY_LOCATION_ID, lock, bounded=True) == 7
    query = db.statements[0]
    assert {column.name for column in query.selected_columns} == {"state", "revision"}
    assert all(
        column.table is StorageMaintenanceGate.__table__
        for column in query.selected_columns
    )
    if lock:
        assert query._for_update_arg.read and query._for_update_arg.nowait
    else:
        assert query._for_update_arg is None


@pytest.mark.parametrize("lock", [False, True])
async def test_bounded_asset_projects_only_verified_identity(lock):
    db = ProjectedDB({"dataset_items": [SimpleNamespace(id=uuid.uuid4())]})
    with pytest.raises(ContentError, match="content_unverified"):
        await sources._item(db, uuid.uuid4(), lock, bounded=True)
    query = db.statements[1]
    assert {column.name for column in query.selected_columns} == {
        "id",
        "media_type",
        "checksum_algorithm",
        "checksum_value",
        "size_bytes",
        "verification_state",
    }
    assert all(
        column.table is DatasetAsset.__table__ for column in query.selected_columns
    )


def task_row(current):
    return SimpleNamespace(
        id=current.task_id,
        project_id=current.project_id,
        batch_id=current.batch_id,
        file_path=current.file_path,
        file_type=current.file_type,
        original_id=current.original_id,
        dataset_item_id=current.direct_item_id,
    )


def link_rows(current):
    return [
        SimpleNamespace(
            id=link.id,
            task_id=link.task_id,
            dataset_item_id=link.item_id,
            role=link.role,
            sensor_name=link.sensor_name,
        )
        for link in current.links
    ]


@pytest.mark.parametrize("lock", [False, True])
@pytest.mark.parametrize("model", [Task, DatasetItem])
async def test_source_queries_project_only_required_scalar_facts(model, lock):
    primary = source(media="point_cloud")
    current = relation(primary, links=())
    if model is Task:
        db = RelationDB(task_row(current), [])
        await sources._relation(db, current.task_id, lock)
        expected = {
            "id",
            "project_id",
            "batch_id",
            "file_path",
            "file_type",
            "original_id",
            "dataset_item_id",
        }
    else:
        db = RelationDB(SimpleNamespace(id=primary.item_id), [], any_links=None)
        with pytest.raises(ContentError, match="content_unverified"):
            await sources._item(db, primary.item_id, lock)
        expected = {"id", "dataset_id", "file_path", "file_type", "width", "height"}
    query = db.statements[0]
    assert {column.name for column in query.selected_columns} == expected
    assert all(column.table is model.__table__ for column in query.selected_columns)
    compiled = query.compile(dialect=postgresql.dialect())
    assert "metadata" not in str(compiled) and "tags" not in str(compiled)
    if lock:
        assert query._for_update_arg.read and query._for_update_arg.nowait
    else:
        assert query._for_update_arg is None


@pytest.mark.parametrize("lock", [False, True])
@pytest.mark.parametrize("camera_first", [False, True])
async def test_bounded_relation_rejects_overflow_before_source_or_cache(
    monkeypatch, lock, camera_first
):
    primary, camera = source(media="point_cloud"), source()
    current = relation(primary, cameras=(camera, camera))
    db = RelationDB(task_row(current), link_rows(current))

    async def unexpected_item(*args):
        pytest.fail("overflow must be rejected before resolving any item")

    monkeypatch.setattr(sources, "_item", unexpected_item)
    resolver = sources.CachedResolver(db, lock=lock, max_links=1)
    with pytest.raises(
        ContentError, match="original_relation_budget_exceeded"
    ) as error:
        await (
            resolver.link(current.task_id, current.links[1].id)
            if camera_first
            else resolver.task(current.task_id)
        )
    assert error.value.status == 422
    assert not any(
        (resolver._relations, resolver._links, resolver._items, resolver._tasks)
    )
    query = db.statements[1]
    compiled = query.compile(dialect=postgresql.dialect())
    assert "LIMIT" in str(compiled) and query._limit_clause.value == 2
    assert tuple(query._order_by_clauses) == (TaskDatasetItemLink.id,)
    if lock:
        assert query._for_update_arg.read and query._for_update_arg.nowait
        assert db.events == ["begin", "rollback"]
    # The default canonical resolver still reads every link.
    full = await sources._relation(db, current.task_id, lock)
    assert full == current and db.statements[-1]._limit_clause is None


@pytest.mark.parametrize("camera_first", [False, True])
async def test_cache_link_budget_is_shared_across_tasks_and_cache_hits(
    monkeypatch, camera_first
):
    primary, camera = source(media="point_cloud"), source()
    first = relation(primary, cameras=(camera,))
    second = relation(primary, cameras=(camera, camera))
    calls = fake_sources(
        monkeypatch,
        {row.task_id: row for row in (first, second)},
        {row.item_id: row for row in (primary, camera)},
    )
    original_relation = sources._relation

    async def bounded_relation(db, task_id, lock, **options):
        current = await original_relation(db, task_id, lock, **options)
        if len(current.links) > options["max_links"]:
            raise ContentError("original_relation_budget_exceeded", 422)
        return current

    monkeypatch.setattr(sources, "_relation", bounded_relation)
    resolver = sources.CachedResolver(ReadDB(), max_links=4)
    if camera_first:
        await resolver.link(first.task_id, first.links[1].id)
    await resolver.task(first.task_id)
    await resolver.link(first.task_id, first.links[1].id)
    for _ in range(2):
        with pytest.raises(ContentError, match="original_relation_budget_exceeded"):
            await resolver.link(second.task_id, second.links[1].id)
    assert list(resolver._relations) == [first.task_id]
    assert len(resolver._links) == 2
    assert calls["relations"] == [
        (
            first.task_id,
            False,
            {"max_links": 4, **({"allow_missing": True} if camera_first else {})},
        ),
        (second.task_id, False, {"allow_missing": True, "max_links": 2}),
        (second.task_id, False, {"allow_missing": True, "max_links": 2}),
    ]


@pytest.mark.parametrize("limit", [True, False, -1, 1.5, "1"])
def test_cache_rejects_invalid_link_budget(limit):
    with pytest.raises(ValueError, match="max_links"):
        sources.CachedResolver(ReadDB(), max_links=limit)


async def test_zero_link_budget_allows_direct_source_but_not_a_link():
    current = relation(source(), links=(), primary_item_id=None, primary_link_id=None)
    db = RelationDB(task_row(current), [])
    assert await sources._relation(db, current.task_id, False, max_links=0) == current
    assert db.statements[-1]._limit_clause.value == 1
    db.rows = link_rows(relation(source(), task_id=current.task_id))
    with pytest.raises(ContentError, match="original_relation_budget_exceeded"):
        await sources._relation(db, current.task_id, False, max_links=0)


@pytest.mark.parametrize("limit", [2, 3])
async def test_bounded_relation_accepts_complete_facts_at_or_below_limit(limit):
    current = relation(source(media="point_cloud"), cameras=(source(),))
    db = RelationDB(task_row(current), link_rows(current))
    assert (
        await sources._relation(db, current.task_id, False, max_links=limit) == current
    )
    assert db.statements[-1]._limit_clause.value == limit + 1


@pytest.mark.parametrize("conflict", ["independent", "direct"])
async def test_bounded_relation_retains_canonical_conflict_rejections(conflict):
    current = relation(source(media="point_cloud"), cameras=(source(),))
    current = replace(
        current,
        **(
            {"original_id": uuid.uuid4()}
            if conflict == "independent"
            else {"direct_item_id": uuid.uuid4()}
        ),
    )
    db = RelationDB(task_row(current), link_rows(current))
    with pytest.raises(ContentError, match="original_relation_conflict"):
        await sources._relation(db, current.task_id, False, max_links=2)


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


@pytest.mark.parametrize("lock", [False, True])
async def test_database_projection_reads_fresh_facts_without_rehydrating_json(
    original_db, lock
):
    db = original_db
    item, _, _, task = await dataset_rows(db, file_type="point_cloud")
    item.metadata_ = {"frozen": "item"}
    task.tags = ["frozen-task"]
    await db.commit()
    task_id, item_id = task.id, item.id
    task_unloaded, item_unloaded = (
        set(inspect(task).unloaded),
        set(inspect(item).unloaded),
    )
    # The first, budgeted hydration is already in the caller's identity map.
    # A concurrent writer changes both a required scalar and excluded JSON.
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as writer:
        await writer.execute(
            update(Task).where(Task.id == task_id).values(tags=["x" * 131_072])
        )
        await writer.execute(
            update(DatasetItem)
            .where(DatasetItem.id == item_id)
            .values(width=31, metadata_={"large": "y" * 131_072})
        )
        await writer.commit()
    statements = []

    def record(conn, cursor, sql, parameters, context, executemany):
        if context.compiled is not None:
            statements.append(context.compiled.statement)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = await sources.CachedResolver(db, lock=lock, max_links=0).task(task_id)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert result.item_facts.width == 31
    assert task.tags == ["frozen-task"] and item.metadata_ == {"frozen": "item"}
    assert item.width == 24
    assert set(inspect(task).unloaded) == task_unloaded
    assert set(inspect(item).unloaded) == item_unloaded
    for model in (Task, DatasetItem):
        queries = [
            statement
            for statement in statements
            if getattr(statement, "is_select", False)
            and any(
                getattr(column, "table", None) is model.__table__
                for column in statement.selected_columns
            )
        ]
        assert len(queries) == 1
        assert all(
            column.type.__class__.__name__ != "JSONB"
            for column in queries[0].selected_columns
        )


@pytest.mark.parametrize("lock", [False, True])
@pytest.mark.parametrize("camera_first", [False, True])
async def test_database_link_growth_is_rejected_before_item_read_and_cache(
    original_db, monkeypatch, lock, camera_first
):
    db = original_db
    _, _, _, task = await dataset_rows(db, file_type="point_cloud")
    camera, _, _, _ = await dataset_rows(db)
    first = await add_link(db, task.id, camera.id, role="camera_front")
    admitted = list(
        await db.scalars(
            select(TaskDatasetItemLink).where(TaskDatasetItemLink.task_id == task.id)
        )
    )
    await db.commit()
    assert len(admitted) == 1
    task_id, link_id, camera_id = task.id, first.id, camera.id
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as writer:
        await add_link(writer, task_id, camera_id, role="camera_back")
        await writer.commit()
    original_item = sources._item

    async def unexpected_item(*args):
        pytest.fail("concurrent relation growth must fail before resolving items")

    monkeypatch.setattr(sources, "_item", unexpected_item)
    resolver = sources.CachedResolver(db, lock=lock, max_links=len(admitted))
    with pytest.raises(ContentError, match="original_relation_budget_exceeded"):
        await (
            resolver.link(task_id, link_id) if camera_first else resolver.task(task_id)
        )
    assert not any(
        (resolver._relations, resolver._links, resolver._items, resolver._tasks)
    )
    assert await db.scalar(select(1)) == 1
    monkeypatch.setattr(sources, "_item", original_item)
    # Existing unbounded callers preserve complete canonical relation facts.
    complete = await sources.CachedResolver(db, lock=lock).task(task_id)
    assert len(complete.relation.links) == 2


@pytest.mark.parametrize("lock", [False, True])
@pytest.mark.parametrize(
    "etag",
    [None, "saved-etag", "x" * 1024, "é" * 512],
    ids=["null", "saved", "ascii-limit", "utf8-limit"],
)
async def test_database_bounded_source_preserves_exact_valid_owner(
    original_db, lock, etag
):
    db = original_db
    _, _, replica, task = await dataset_rows(db, file_type="point_cloud")
    replica.etag = etag
    await db.commit()
    expected = await sources.CachedResolver(db, lock=lock).task(task.id)
    actual = await sources.CachedResolver(db, lock=lock, max_links=0).task(task.id)
    assert actual == expected and actual.replica_facts.etag == etag


@pytest.mark.parametrize("lock", [False, True])
@pytest.mark.parametrize(
    "etag", ["x" * 1025, "é" * 513], ids=["ascii-large", "utf8-large"]
)
async def test_database_bounded_source_rejects_large_etag_without_truncation(
    original_db, lock, etag
):
    db = original_db
    _, _, replica, task = await dataset_rows(db, file_type="point_cloud")
    replica.etag = etag
    await db.commit()
    resolver = sources.CachedResolver(db, lock=lock, max_links=0)
    with pytest.raises(ContentError, match="original_source_budget_exceeded") as error:
        await resolver.task(task.id)
    assert error.value.status == 422
    assert not any(
        (resolver._relations, resolver._links, resolver._items, resolver._tasks)
    )
    # Existing callers keep the exact saved identity; bounded mode never
    # accepts a truncated ETag or substitutes a current bucket locator.
    expected = await sources.CachedResolver(db, lock=lock).task(task.id)
    assert expected.replica_facts.etag == etag
    assert expected.bucket == replica.bucket


@pytest.mark.parametrize("lock", [False, True])
@pytest.mark.parametrize("invalid", ["large", "numeric_boolean"])
async def test_database_bounded_config_rejects_large_and_wrong_type_values(
    original_db, lock, invalid
):
    db = original_db
    _, _, _, task = await dataset_rows(db, file_type="point_cloud")
    config = {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl}
    if invalid == "large":
        config["large"] = "x" * 131_072
    else:
        config["use_ssl"] = int(settings.minio_use_ssl)
    await db.execute(
        update(StorageLocation)
        .where(StorageLocation.id == LEGACY_LOCATION_ID)
        .values(config=config)
    )
    await db.commit()
    for resolver in (
        sources.CachedResolver(db, lock=lock, max_links=0),
        sources.CachedResolver(db, lock=lock),
    ):
        with pytest.raises(ContentError, match="location_unavailable"):
            await resolver.task(task.id)
        assert not any(
            (resolver._relations, resolver._links, resolver._items, resolver._tasks)
        )


@pytest.mark.parametrize("lock", [False, True])
async def test_database_bounded_replicas_reject_ambiguity_after_two_rows(
    original_db, monkeypatch, lock
):
    db = original_db
    item, asset, _, task = await dataset_rows(db, file_type="point_cloud")
    db.add_all(
        [
            DatasetAssetLocation(
                asset_id=asset.id,
                location_id=LEGACY_LOCATION_ID,
                bucket=f"saved-copy-{index:03d}",
                object_key=item.file_path,
                role="managed_original",
                reference_state="present",
                etag="x" * 2048,
            )
            for index in range(32)
        ]
    )
    await db.commit()
    read = sources._replicas
    observed = []

    async def replicas(*args, **options):
        rows = await read(*args, **options)
        observed.append((len(rows), options["bounded"]))
        return rows

    monkeypatch.setattr(sources, "_replicas", replicas)
    resolver = sources.CachedResolver(db, lock=lock, max_links=0)
    with pytest.raises(ContentError, match="original_location_ambiguous"):
        await resolver.task(task.id)
    assert observed == [(2, True)] * (2 if lock else 1)
    assert not any(
        (resolver._relations, resolver._links, resolver._items, resolver._tasks)
    )
    observed.clear()
    with pytest.raises(ContentError, match="original_location_ambiguous"):
        await sources.CachedResolver(db, lock=lock).task(task.id)
    assert observed == [(33, False)] * (2 if lock else 1)


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
    resolver = sources.CachedResolver(db, lock=True, max_links=0)
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
