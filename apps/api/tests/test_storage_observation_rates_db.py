"""Real independent PostgreSQL transactions and authorized rate summaries."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, event, func, insert, literal_column, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.dataset import Dataset
from app.db.models.dataset_asset import StorageLocation
from app.db.models.dataset_source import DatasetSource
from app.db.models.storage_fault import StorageFaultState
from app.db.models.storage_observation_rate import StorageObservationBucket as Bucket
from app.db.models.user import User
from app.services import storage_observation_rates as rates
from tests.factory import make_user_dict


@pytest.fixture
async def counters(test_engine):
    location, source = uuid.uuid4(), uuid.uuid4()
    try:
        yield location, source
    finally:
        async with test_engine.begin() as connection:
            await connection.execute(
                delete(Bucket).where(Bucket.location_id == location)
            )


def admin():
    return SimpleNamespace(role="super_admin", is_active=True)


async def observed(db, location, source, metric="original_read", outcome="success"):
    attempt = rates.begin(location, source, metric)
    assert await rates.finish(db, attempt, outcome)
    return attempt


async def test_first_healthy_observation_has_denominator_without_fault_row(
    db_session, test_engine, counters
):
    location, source = counters
    empty = await rates.visible_rates(db_session, SimpleNamespace(id=location), admin())
    assert empty is not None
    assert empty["original_read"]["total_count"] == 0
    assert empty["original_read"]["failure_rate"] is None
    assert empty["preview_generate"]["failure_rate"] is None
    await observed(db_session, location, source)
    result = await rates.visible_rates(
        db_session, SimpleNamespace(id=location), admin()
    )
    assert result["original_read"] == {
        "success_count": 1,
        "failure_count": 0,
        "total_count": 1,
        "failure_rate": 0.0,
    }
    assert result["preview_generate"]["total_count"] == 0
    assert result["window_start"].hour == result["window_start"].minute == 0
    assert result["window_start"].utcoffset().total_seconds() == 0
    assert result["window_start"] <= result["measured_at"] == result["window_end"]
    async with test_engine.connect() as connection:
        assert not await connection.scalar(
            select(StorageFaultState.id).where(
                StorageFaultState.location_id == location
            )
        )


async def test_parallel_completions_duplicate_settlement_and_late_result(
    test_engine, counters
):
    location, source = counters
    async with AsyncSession(test_engine) as db:
        old = rates.begin(location, source, "original_read")
        new = rates.begin(location, source, "original_read")
        assert await rates.finish(db, new, "success")
        assert await rates.finish(db, old, "failure")
        duplicate = rates.begin(location, source, "original_read")
        results = await asyncio.gather(
            *(rates.finish(db, duplicate, "success") for _ in range(10))
        )
        assert sum(results) == 1
        assert all(
            await asyncio.gather(
                *(
                    observed(
                        db, location, source, outcome="failure" if i % 2 else "success"
                    )
                    for i in range(20)
                )
            )
        )
        summary = await rates.visible_rates(db, SimpleNamespace(id=location), admin())
    assert summary["original_read"] == {
        "success_count": 12,
        "failure_count": 11,
        "total_count": 23,
        "failure_rate": 11 / 23,
    }


async def test_locked_bucket_skips_bounded_observation_without_retry(
    db_session, test_engine, counters
):
    location, source = counters
    await observed(db_session, location, source)
    async with AsyncSession(test_engine) as blocker:
        await blocker.scalar(
            select(Bucket).where(Bucket.location_id == location).with_for_update()
        )
        attempt = rates.begin(location, source, "original_read")
        assert not await rates.finish(db_session, attempt, "failure")
        await blocker.rollback()
    assert not await rates.finish(db_session, attempt, "failure")
    summary = await rates.visible_rates(
        db_session, SimpleNamespace(id=location), admin()
    )
    assert summary["original_read"]["total_count"] == 1
    assert await db_session.scalar(select(1)) == 1


@pytest.mark.parametrize("session_zone", ["Pacific/Kiritimati", "Pacific/Pago_Pago"])
async def test_completion_day_is_utc_not_session_zone_and_other_days_are_excluded(
    db_session, test_engine, counters, monkeypatch, session_zone
):
    location, source = counters
    original_bounds = rates._bounds

    async def shifted(connection, **kwargs):
        await original_bounds(connection, **kwargs)
        await connection.execute(
            text("SELECT set_config('TimeZone', :zone, true)"), {"zone": session_zone}
        )

    monkeypatch.setattr(rates, "_bounds", shifted)
    await observed(db_session, location, source)
    async with test_engine.begin() as connection:
        row = (
            (
                await connection.execute(
                    select(Bucket).where(Bucket.location_id == location)
                )
            )
            .mappings()
            .one()
        )
        expected_day = await connection.scalar(
            text("SELECT (statement_timestamp() AT TIME ZONE 'UTC')::date")
        )
        assert row["day"] == expected_day
        for shift in (-1, 1):
            stamp = row["first_observed_at"] + timedelta(days=shift)
            await connection.execute(
                insert(Bucket).values(
                    day=expected_day + timedelta(days=shift),
                    location_id=location,
                    source_id=source,
                    metric="original_read",
                    success_count=0,
                    failure_count=90,
                    first_observed_at=stamp,
                    last_observed_at=stamp,
                )
            )
    result = await rates.visible_rates(
        db_session, SimpleNamespace(id=location), admin()
    )
    assert result["original_read"]["total_count"] == 1
    assert result["original_read"]["failure_count"] == 0
    assert result["measured_at"].date() == expected_day


async def test_counter_survives_caller_rollback_without_committing_business(
    db_session, test_engine, super_admin, counters
):
    location, source = counters
    dataset = Dataset(
        display_id=uuid.uuid4().hex[:20],
        name="uncommitted",
        created_by=super_admin[0].id,
    )
    db_session.add(dataset)
    await db_session.flush()
    await observed(db_session, location, source)
    async with test_engine.connect() as connection:
        assert (
            await connection.scalar(select(Dataset.id).where(Dataset.id == dataset.id))
            is None
        )
        assert (
            await connection.scalar(
                select(Bucket.success_count).where(Bucket.location_id == location)
            )
            == 1
        )
    assert dataset.name == "uncommitted"
    await db_session.rollback()
    assert (
        await rates.visible_rates(db_session, SimpleNamespace(id=location), admin())
    )["original_read"]["total_count"] == 1


async def test_observer_sql_failure_does_not_poison_caller_or_retry(
    db_session, counters, monkeypatch
):
    location, source = counters
    original_bounds = rates._bounds

    async def failed(connection, **kwargs):
        await original_bounds(connection, **kwargs)
        await connection.execute(text("SELECT private_missing_rate_column"))

    monkeypatch.setattr(rates, "_bounds", failed)
    attempt = rates.begin(location, source, "original_read")
    assert not await rates.finish(db_session, attempt, "failure")
    assert not await rates.finish(db_session, attempt, "success")
    assert (
        await rates.visible_rates(db_session, SimpleNamespace(id=location), admin())
        is None
    )
    assert await db_session.scalar(select(1)) == 1


async def test_rate_query_is_one_select_in_independent_readonly_transaction(
    db_session, test_engine, counters
):
    location, source = counters
    await observed(db_session, location, source)
    statements = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert await rates.visible_rates(
            db_session, SimpleNamespace(id=location), admin()
        )
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", capture)
    assert "SET TRANSACTION READ ONLY" in statements
    reads = [value for value in statements if value.startswith(("SELECT", "WITH"))]
    assert len(reads) == 1 and reads[0].count("statement_timestamp()") == 1
    assert not any(
        value.startswith(("INSERT", "UPDATE", "DELETE")) for value in statements
    )


@pytest.fixture
async def committed_world(test_engine):
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        actors = [
            User(**make_user_dict(role, f"{uuid.uuid4().hex}@rates.invalid", role))
            for role in ("super_admin", "project_admin", "project_admin")
        ]
        db.add_all(actors)
        await db.flush()
        location = StorageLocation(
            id=uuid.uuid4(),
            name=uuid.uuid4().hex,
            kind="filesystem",
            config={"scope": "global"},
        )
        db.add(location)
        datasets = [
            Dataset(display_id=uuid.uuid4().hex[:20], name="Rate", created_by=actor.id)
            for actor in actors[1:]
        ]
        db.add_all(datasets)
        await db.flush()
        sources = [
            DatasetSource(
                dataset_id=dataset.id, location_id=location.id, name="Rate", config={}
            )
            for dataset in datasets
        ]
        db.add_all(sources)
        await db.commit()
    try:
        yield actors, location, sources
    finally:
        async with test_engine.begin() as connection:
            await connection.execute(
                delete(Bucket).where(Bucket.location_id == location.id)
            )
            await connection.execute(
                delete(StorageFaultState).where(
                    StorageFaultState.location_id == location.id
                )
            )
            await connection.execute(
                delete(DatasetSource).where(DatasetSource.location_id == location.id)
            )
            await connection.execute(
                delete(Dataset).where(Dataset.id.in_([row.id for row in datasets]))
            )
            await connection.execute(
                delete(StorageLocation).where(StorageLocation.id == location.id)
            )
            await connection.execute(
                delete(User).where(User.id.in_([row.id for row in actors]))
            )


async def test_acl_orphan_visibility_and_same_day_configuration_changes(
    db_session, committed_world
):
    actors, location, sources = committed_world
    await observed(db_session, location.id, sources[0].id, outcome="failure")
    await observed(db_session, location.id, sources[1].id)
    await observed(db_session, location.id, uuid.uuid4(), outcome="failure")
    for actor, total, failures in (
        (actors[0], 3, 2),
        (actors[1], 1, 1),
        (actors[2], 1, 0),
    ):
        result = await rates.visible_rates(db_session, location, actor)
        assert result["original_read"]["total_count"] == total
        assert result["original_read"]["failure_count"] == failures
    async with AsyncSession(db_session.bind.engine) as independent:
        await independent.execute(
            update(DatasetSource)
            .where(DatasetSource.id == sources[0].id)
            .values(config_revision=2)
        )
        await independent.commit()
    await observed(db_session, location.id, sources[0].id)
    result = await rates.visible_rates(db_session, location, actors[1])
    assert result["original_read"]["failure_rate"] == 0.5
    actors[0].is_active = False
    assert (await rates.visible_rates(db_session, location, actors[0]))[
        "original_read"
    ]["total_count"] == 0


def headers(actor):
    from app.core.security import create_access_token

    return {"Authorization": "Bearer " + create_access_token(str(actor.id), actor.role)}


async def test_fault_filters_and_pagination_do_not_change_authorized_rates(
    db_session, httpx_client, committed_world
):
    actors, location, sources = committed_world
    await observed(db_session, location.id, sources[0].id)
    await observed(
        db_session,
        location.id,
        sources[0].id,
        metric="preview_generate",
        outcome="failure",
    )
    for index, (source, state) in enumerate(
        ((sources[0], "active"), (sources[0], "recovered"), (sources[1], "active"))
    ):
        db_session.add(
            StorageFaultState(
                id=uuid.uuid4(),
                scope_key=uuid.uuid4().hex * 2,
                location_id=location.id,
                source_id=source.id,
                category="root_unavailable",
                operation="scan_content",
                node_id="rates-test",
                state=state,
                first_failed_at=func.statement_timestamp(),
                last_failed_at=func.statement_timestamp(),
                recovered_at=func.statement_timestamp()
                if state == "recovered"
                else None,
                failure_count=1,
                last_sequence=index + 1,
                recovered_sequence=index + 1 if state == "recovered" else None,
                scope_facts={"common": {}},
                evidence={},
            )
        )
    await db_session.flush()
    for state, offset, total, items in (
        ("active", 0, 1, 1),
        ("recovered", 0, 1, 1),
        ("all", 0, 2, 1),
        ("all", 100, 2, 0),
    ):
        response = await httpx_client.get(
            f"/api/v1/storage-locations/{location.id}/faults",
            params={"state": state, "limit": 1, "offset": offset},
            headers=headers(actors[1]),
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert len(result["items"]) == items and result["total"] == total
        assert result["counts"] == {"active": 1, "recovered": 1}
        assert result["rates"]["original_read"]["total_count"] == 1
        assert result["rates"]["preview_generate"]["failure_rate"] == 1
    await db_session.rollback()


async def test_rate_query_failure_keeps_faults_response_and_caller_transaction(
    db_session, httpx_client, committed_world, monkeypatch
):
    actors, location, _sources = committed_world
    monkeypatch.setattr(
        rates,
        "_visible_statement",
        lambda *_: select(literal_column("private_missing_rate_column")),
    )
    response = await httpx_client.get(
        f"/api/v1/storage-locations/{location.id}/faults", headers=headers(actors[0])
    )
    assert response.status_code == 200 and response.json()["rates"] is None
    assert await db_session.scalar(select(1)) == 1


async def test_final_role_revocation_rejects_rate_result(
    db_session, httpx_client, committed_world, monkeypatch
):
    actors, location, _sources = committed_world
    original = rates.visible_rates

    async def revoked(db, target, actor):
        value = await original(db, target, actor)
        await db.execute(
            update(User).where(User.id == actor.id).values(role="employee")
        )
        return value

    monkeypatch.setattr(rates, "visible_rates", revoked)
    response = await httpx_client.get(
        f"/api/v1/storage-locations/{location.id}/faults", headers=headers(actors[0])
    )
    assert response.status_code == 403
    await db_session.rollback()
