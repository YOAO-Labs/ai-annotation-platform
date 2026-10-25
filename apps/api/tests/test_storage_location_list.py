"""Location list summaries reuse persisted, scoped evidence in bounded queries."""

from datetime import timedelta
import uuid

import pytest
from sqlalchemy import event, func, select

from app.db.models.dataset_asset import StorageLocation, StorageLocationProbe
from app.db.models.storage_connection import StorageConnection
from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.services import filesystem_storage as fs
from app.services.storage import storage_service
from app.services.storage_location_list import list_items
from app.services.storage_source_usage import read_usage, read_usage_page
from tests.test_dataset_content import (
    content_world as content_world,
    create_content_world,
    headers,
)
from tests.test_dataset_source_changes import accept_all_files
from tests.test_storage_source_usage import (
    accepted as accepted,
    duplicate_fs,
    incomplete_legacy_head,
)


def location(name, *, kind="filesystem", enabled=True, **kwargs):
    return StorageLocation(
        id=uuid.uuid4(),
        name=f"list-{name}-{uuid.uuid4().hex[:8]}",
        kind=kind,
        enabled=enabled,
        config_revision=2,
        config={"scope": "global", "root_handle": "safe", "relative_path": ""},
        **kwargs,
    )


def forbid_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Reading a location page must not access physical storage")

    for name in ("scan_directory", "content_file", "probe_directory"):
        monkeypatch.setattr(fs, name, forbidden)
    monkeypatch.setattr(storage_service.client, "_make_api_call", forbidden)


async def test_saved_health_counts_share_one_time_and_keep_enabled_separate(
    db_session, super_admin, monkeypatch
):
    db = db_session
    mixed, disabled, empty = (
        location("mixed"),
        location("disabled", enabled=False),
        location("empty"),
    )
    db.add_all([mixed, disabled, empty])
    await db.flush()
    tick = await db.scalar(select(func.transaction_timestamp()))
    for owner, node, revision, age, ok in (
        (mixed, "success", 2, 10, True),
        (mixed, "failure", 2, 1, False),
        (mixed, "expired", 2, 300, True),
        (mixed, "old-config", 1, 2, True),
        (disabled, "disabled-success", 2, 5, True),
    ):
        db.add(
            StorageLocationProbe(
                location_id=owner.id,
                node_id=node,
                config_revision=revision,
                observed_at=tick - timedelta(seconds=age),
                result={
                    "ok": ok,
                    "code": "verified" if ok else "authentication_failed",
                    "capabilities": {"list": "verified" if ok else "unknown"},
                    "internal_path": "private-sentinel",
                },
            )
        )
    await db.flush()
    forbid_io(monkeypatch)
    result = await list_items(db, [mixed, disabled, empty], super_admin[0])
    first = result[0]["health"]
    assert first["measured_at"] == tick
    assert [
        first[key]
        for key in (
            "observed_node_count",
            "current_success_count",
            "current_failure_count",
            "stale_node_count",
        )
    ] == [4, 1, 1, 2]
    assert first["latest"]["node_id"] == "failure"
    assert first["latest"]["code"] == "authentication_failed"
    assert first["fresh_until"] == tick + timedelta(seconds=290)
    assert result[1]["enabled"] is False
    assert result[1]["health"]["current_success_count"] == 1
    assert result[1]["health"]["latest"]["code"] == "verified"
    assert result[2]["health"]["latest"] is None
    assert result[2]["health"]["observed_node_count"] == 0
    assert result[2]["health"]["fresh_until"] is None
    assert "private-sentinel" not in str(result)


async def test_latest_stale_evidence_is_not_a_fresh_failure(db_session, super_admin):
    db = db_session
    row = location("stale")
    db.add(row)
    await db.flush()
    tick = await db.scalar(select(func.transaction_timestamp()))
    db.add(
        StorageLocationProbe(
            location_id=row.id,
            node_id="old",
            config_revision=1,
            observed_at=tick - timedelta(seconds=1),
            result={
                "ok": False,
                "code": "authentication_failed",
                "capabilities": {"read": "unknown"},
            },
        )
    )
    await db.flush()
    health = (await list_items(db, [row], super_admin[0]))[0]["health"]
    assert health["current_failure_count"] == health["current_success_count"] == 0
    assert health["stale_node_count"] == 1
    assert health["latest"]["code"] == "stale"
    assert health["fresh_until"] is None


async def test_page_enrichment_batches_connections_gates_probes_and_usage(
    db_session, super_admin, monkeypatch
):
    db = db_session
    rows = []
    for index in range(8):
        connection = StorageConnection(
            id=uuid.uuid4(),
            name=f"connection-{index}",
            kind="s3",
            scope="global",
            config={
                "endpoint": "private-endpoint-sentinel",
                "bucket": "private-bucket-sentinel",
            },
            secret_enc=b"private-secret-sentinel",
            created_by=super_admin[0].id,
        )
        db.add(connection)
        await db.flush()
        row = location(str(index), kind="s3", connection_id=connection.id)
        rows.append(row)
        db.add(row)
    await db.flush()
    db.add(StorageMaintenanceGate(location_id=rows[-1].id, state="stopped", revision=4))
    await db.flush()
    forbid_io(monkeypatch)
    counts, results = [], []
    for page in ([rows[0]], rows):
        statements = []

        def seen(*args):
            statements.append(args[2])

        event.listen(db.bind.sync_engine, "before_cursor_execute", seen)
        try:
            results.append(await list_items(db, page, super_admin[0]))
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", seen)
        counts.append(len(statements))
    assert counts == [5, 5]
    assert results[1][-1]["name"] == "connection-7"
    assert results[1][-1]["maintenance_state"] == "stopped"
    assert results[1][-1]["maintenance_revision"] == 4
    assert "browse" not in results[1][-1]["allowed_actions"]
    assert "private-" not in str(results)


async def test_list_is_page_scoped_and_preserves_detail_contract(
    db_session, httpx_client, super_admin, project_admin, annotator, monkeypatch
):
    db = db_session
    first, second, hidden = location("a"), location("b"), location("hidden")
    hidden.config = {"scope": "restricted", "use_user_ids": []}
    db.add_all([first, second, hidden])
    await db.flush()
    tick = await db.scalar(select(func.transaction_timestamp()))
    db.add(
        StorageLocationProbe(
            location_id=hidden.id,
            node_id="hidden-node-sentinel",
            config_revision=2,
            observed_at=tick,
            result={"ok": True, "code": "verified", "capabilities": {}},
        )
    )
    await db.flush()
    forbid_io(monkeypatch)
    path = "/api/v1/storage-locations"
    first_page = await httpx_client.get(
        path, headers=headers(project_admin), params={"limit": 1}
    )
    second_page = await httpx_client.get(
        path, headers=headers(project_admin), params={"limit": 1, "offset": 1}
    )
    assert first_page.status_code == second_page.status_code == 200
    one, two = first_page.json(), second_page.json()
    assert one["total"] == two["total"] == 2
    assert one["items"][0]["id"] != two["items"][0]["id"]
    assert {one["items"][0]["id"], two["items"][0]["id"]} == {
        str(first.id),
        str(second.id),
    }
    assert "hidden" not in str([one, two])
    assert one["items"][0]["source_usage"]["total_size_bytes"] == 0
    assert (await httpx_client.get(path, headers=headers(annotator))).status_code == 403
    detail = await httpx_client.get(f"{path}/{first.id}", headers=headers(super_admin))
    assert detail.status_code == 200
    assert "health" not in detail.json() and "source_usage" not in detail.json()
    empty = await httpx_client.get(
        path, headers=headers(project_admin), params={"offset": 20}
    )
    assert empty.json() == {"items": [], "total": 2}


async def test_batch_source_scale_keeps_location_domains_and_visible_heads(
    db_session, accepted, project_admin, tmp_path, monkeypatch
):
    db, first = db_session, accepted
    await duplicate_fs(db, first)
    other_path = tmp_path / "other"
    other_path.mkdir()
    other = await create_content_world(db, first[2], other_path, monkeypatch)
    await accept_all_files(db, other)
    await db.commit()
    await incomplete_legacy_head(db, first, "host")
    empty = location("unknown", kind="legacy_s3")
    db.add(empty)
    await db.flush()
    owners = [first[4], other[4], empty]
    forbid_io(monkeypatch)
    statements = []

    def seen(*args):
        statements.append(args[2])

    event.listen(db.bind.sync_engine, "before_cursor_execute", seen)
    try:
        batch = await read_usage_page(db, owners, first[2])
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", seen)
    assert len(statements) == 1
    assert batch[first[4].id]["total_size_bytes"] is None
    assert batch[other[4].id]["file_count"] == 1
    assert batch[other[4].id]["total_size_bytes"] == len(other[1])
    assert batch[empty.id]["total_size_bytes"] is None
    for row in owners:
        single = await read_usage(db, row, first[2])
        assert {k: v for k, v in single.items() if k != "measured_at"} == {
            k: v for k, v in batch[row.id].items() if k != "measured_at"
        }
    scoped = await read_usage_page(db, owners, project_admin[0])
    assert all(value["reference_count"] == 0 for value in scoped.values())
    assert await read_usage_page(db, [], first[2]) == {}


async def test_batch_retains_cross_dataset_deduplication(
    db_session, accepted, monkeypatch
):
    db, world = db_session, accepted
    await duplicate_fs(db, world)
    forbid_io(monkeypatch)
    usage = (await read_usage_page(db, [world[4]], world[2]))[world[4].id]
    assert usage["reference_count"] == 2
    assert usage["file_count"] == usage["duplicate_reference_count"] == 1
    assert usage["total_size_bytes"] == len(world[1])
