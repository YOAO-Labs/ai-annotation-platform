"""Dataset pagination bounds detail work without shrinking filter membership."""

from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import event, select

from app.db.models.dataset import DatasetItem, ProjectDataset, Scene
from app.db.models.dataset_asset import StorageLocation
from app.schemas.dataset_view import DatasetItemFilters
from app.services import dataset_workbench as svc
from tests.factory import create_project, create_task
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_changes import accept_all_files
from tests.test_dataset_views import dataset
from tests.training_original_fixtures import register_original


async def indexed_dataset(db, actor, count=64):
    row = await dataset(db, actor)
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    items = [
        DatasetItem(
            id=uuid.uuid5(row.id, str(index)),
            dataset_id=row.id,
            file_name=f"{'keep' if index < 53 else 'skip'}-{index:03}.png",
            file_path=f"query-test/{row.id}/{index}.png",
            width=48,
            height=32,
            created_at=timestamp,
        )
        for index in range(count)
    ]
    db.add_all(items)
    await db.flush()
    return row, sorted(items, key=lambda item: item.id.int)


@pytest.mark.parametrize("size", [64, 256])
async def test_page_detail_relation_only_contains_current_ordered_members(
    db_session, super_admin, monkeypatch, size
):
    db, actor = db_session, super_admin[0]
    ds, ordered = await indexed_dataset(db, actor, size)
    original = svc.observations
    full_relations = []

    def observe(*args, **kwargs):
        relation = original(*args, **kwargs)
        if "original_cache_recorded" in relation.c:
            full_relations.append(relation)
        return relation

    monkeypatch.setattr(svc, "observations", observe)
    page, total = await svc.item_page(db, ds.id, actor, limit=2, offset=31)
    expected = [item.id for item in ordered[31:33]]
    assert total == size and [item["id"] for item in page] == expected
    assert full_relations
    # Execute the actual detail relation, rather than trusting a spy's arguments.
    for relation in full_relations:
        relation_ids = list(await db.scalars(select(relation.c.item_id)))
        assert set(relation_ids) == set(expected)

    full_relations.clear()
    empty, total = await svc.item_page(db, ds.id, actor, limit=2, offset=size + 10)
    assert empty == [] and total == size
    for relation in full_relations:
        assert list(await db.scalars(select(relation.c.item_id))) == []


@pytest.mark.parametrize("state", [None, "unverified"])
async def test_membership_count_omits_display_only_relations(
    db_session, super_admin, state
):
    db, actor = db_session, super_admin[0]
    ds, _ = await indexed_dataset(db, actor)
    statements = []
    connection = (await db.connection()).sync_connection

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(connection, "before_cursor_execute", record)
    try:
        page, total = await svc.item_page(
            db, ds.id, actor, limit=2, offset=9, state=state
        )
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert total == 64 and len(page) == 2
    counts = [sql for sql in statements if sql.lstrip().startswith("select count(")]
    assert counts
    assert "original_cache_entries" not in counts[0]
    if state is None:
        assert "dataset_source_changes" not in counts[0]
        assert "dataset_source_previews" not in counts[0]
    else:
        assert "dataset_source_changes" in counts[0]
        assert "dataset_source_previews" in counts[0]
    # The listener must also see the real full-detail path retaining cache facts.
    assert any("original_cache_entries" in sql for sql in statements)


async def test_state_aggregate_omits_cache_details_but_retains_state_evidence(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    ds, _ = await indexed_dataset(db, actor)
    statements = []
    connection = (await db.connection()).sync_connection

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(connection, "before_cursor_execute", record)
    try:
        result = await svc.workbench(db, ds.id, actor)
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert result["availability_counts"]["unverified"] == 64
    aggregates = [sql for sql in statements if "group by" in sql]
    assert len(aggregates) == 1
    assert "original_cache_entries" not in aggregates[0]
    assert "dataset_source_changes" in aggregates[0]
    assert "dataset_source_previews" in aggregates[0]
    full = svc.observations(ds.id, actor)
    assert "original_cache_entries" in str(select(full)).lower()


@pytest.mark.parametrize("offset", [0, 9999])
@pytest.mark.parametrize(
    "condition", ["hidden_source", "foreign_scene", "missing_scene"]
)
async def test_empty_search_and_page_do_not_bypass_filter_authority(
    db_session, content_world, project_admin, httpx_client, condition, offset
):
    db, world = db_session, content_world
    query = {"search": "never-matches-a-file", "limit": 2, "offset": offset}
    if condition == "hidden_source":
        query["source_id"] = str(world[5].id)
    elif condition == "missing_scene":
        query["scene_id"] = str(uuid.uuid4())
    else:
        other = await dataset(db, world[2])
        scene = Scene(
            dataset_id=other.id, display_id=f"Q-{uuid.uuid4().hex[:10]}", name="Other"
        )
        db.add(scene)
        await db.flush()
        query["scene_id"] = str(scene.id)
    await db.commit()
    response = await httpx_client.get(
        f"/api/v1/datasets/{world[3].id}/items",
        params=query,
        headers=headers(project_admin),
    )
    assert response.status_code == 404, response.text


async def test_project_filter_keeps_all_matching_members_beyond_page_size(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    ds, _ = await indexed_dataset(db, actor)
    project = await create_project(db, owner_id=actor.id)
    db.add(ProjectDataset(project_id=project.id, dataset_id=ds.id))
    await db.flush()
    filters = DatasetItemFilters(
        search="keep-", extension="png", availability="unverified"
    )
    selection = await svc.project_filtered_item_ids(
        db, project.id, ds.id, filters, actor
    )
    all_members = list(await db.scalars(selection))
    page, total = await svc.item_page(
        db,
        ds.id,
        actor,
        limit=1,
        offset=10,
        search=filters.search,
        state=filters.availability,
        filters=filters,
    )
    assert len(all_members) == total == 53
    assert len(page) == 1 and page[0]["id"] in all_members


async def test_state_projection_matches_full_reference_and_managed_evidence(
    db_session, content_world, super_admin, project_admin, annotator
):
    db, world = db_session, content_world
    await accept_all_files(db, world)
    project = await create_project(db, owner_id=world[2].id)
    managed = []
    for index in range(2):
        item = DatasetItem(
            dataset_id=world[3].id,
            file_name=f"managed-{index}.png",
            file_path=f"query-managed/{uuid.uuid4()}.png",
        )
        db.add(item)
        await db.flush()
        task = await create_task(db, project_id=project.id)
        task.dataset_item_id = item.id
        _, _, replica = await register_original(db, task, world[2])
        replica.verified_at = datetime.now(timezone.utc) if index == 0 else None
        managed.append((item, replica))
    await db.flush()

    async def compare(actor):
        full = svc.observations(world[3].id, actor)
        lean = svc.observations(world[3].id, actor, state_only=True)
        full_rows = dict((await db.execute(select(full.c.item_id, full.c.state))).all())
        lean_rows = dict((await db.execute(select(lean.c.item_id, lean.c.state))).all())
        assert lean_rows == full_rows
        return lean_rows

    for enabled in (True, False):
        world[5].enabled = enabled
        await db.flush()
        for actor, _ in (super_admin, project_admin, annotator):
            states = await compare(actor)
            assert states[managed[0][0].id] == "verified"
            assert states[managed[1][0].id] == "unverified"
    location = await db.get(StorageLocation, managed[0][1].location_id)
    location.enabled = False
    await db.flush()
    assert (await compare(world[2]))[managed[0][0].id] == "unverified"
