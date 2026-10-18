"""Two restricted Catalog sessions racing a calibration update."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_assets_catalog.spatial_contract import (
    CalibrationMutationResult,
    CalibrationPage,
    canonical_calibration_document,
    normalize_calibration_record,
)
from aap_assets_catalog.spatial_values import calibration_digest
from app.db.models.dataset import SensorCalibrationRevision
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_assets_catalog_native import _CATALOG_URL, _ROLE_ACCEPTANCE, _call
from tests.test_assets_spatial_calibration_native import (
    _calibration,
    _seed_camera_fixture,
    _spatial_error,
    _spatial_result,
    _update_values,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _ROLE_ACCEPTANCE,
        reason="requires the disposable module-data boundary verifier",
    ),
]

_READ_CALIBRATIONS = text(
    "SELECT aap_assets_v1.read_spatial_calibrations("
    "CAST(:dataset_item_ids AS uuid[]), CAST(:include_history AS boolean), "
    "CAST(:after_item_id AS uuid), CAST(:after_revision AS integer), "
    "CAST(:page_size AS integer))"
)
# Keep the frozen argument order explicit: new canonical document, then the
# current document captured with the row snapshot.
_UPDATE_CALIBRATION = text(
    "SELECT aap_assets_v1.update_spatial_calibration("
    "CAST(:operation_id AS uuid), CAST(:actor_id AS uuid), "
    "CAST(:task_id AS uuid), CAST(:camera_role AS text), "
    "CAST(:expected_revision AS integer), CAST(:expected_digest AS text), "
    "CAST(:calibration AS jsonb), CAST(:canonical_document AS text), "
    "CAST(:current_canonical_document AS text), CAST(:source_snapshot AS jsonb), "
    "CAST(:audit_context AS jsonb))"
)


async def _current_record(connection, item_id: UUID):
    reply = await _call(
        connection,
        _READ_CALIBRATIONS,
        {
            "dataset_item_ids": [item_id],
            "include_history": False,
            "after_item_id": None,
            "after_revision": None,
            "page_size": 10,
        },
    )
    page = _spatial_result(reply, CalibrationPage)
    assert page.dataset_item_ids == [item_id]
    assert page.total == 1
    assert page.complete is True
    assert len(page.records) == 1
    return page.records[0]


async def _cleanup_fixture(
    test_engine,
    fixture: dict,
    operation_a: UUID,
    operation_b: UUID,
) -> None:
    async with test_engine.begin() as owner:
        await owner.execute(
            text(
                "DELETE FROM public.dataset_spatial_operations "
                "WHERE operation_id IN (:operation_a, :operation_b)"
            ),
            {"operation_a": operation_a, "operation_b": operation_b},
        )
        await owner.execute(
            text(
                "DELETE FROM public.sensor_calibration_revisions "
                "WHERE dataset_item_id=:item_id"
            ),
            {"item_id": fixture["dataset_item_id"]},
        )
        await owner.execute(
            text("DELETE FROM public.task_dataset_item_links WHERE task_id=:task_id"),
            {"task_id": fixture["task_id"]},
        )
        await owner.execute(
            text("DELETE FROM public.tasks WHERE id=:task_id"),
            {"task_id": fixture["task_id"]},
        )
        await owner.execute(
            text("DELETE FROM public.dataset_items WHERE id=:item_id"),
            {"item_id": fixture["dataset_item_id"]},
        )
        await owner.execute(
            text("DELETE FROM public.datasets WHERE id=:dataset_id"),
            {"dataset_id": fixture["dataset_id"]},
        )
        await owner.execute(
            text("DELETE FROM public.projects WHERE id=:project_id"),
            {"project_id": fixture["project"].id},
        )
        await owner.execute(
            # The verifier drops the owned database with immutable audit evidence.
            text(
                "DELETE FROM public.users WHERE id=:actor_id "
                "AND NOT EXISTS (SELECT 1 FROM public.audit_logs WHERE actor_id=:actor_id)"
            ),
            {"actor_id": fixture["actor_id"]},
        )


async def test_calibration_concurrent_writers_observe_busy_then_revision_conflict(
    test_engine,
) -> None:
    raw_url = os.environ.get(_CATALOG_URL)
    if not raw_url:
        pytest.fail(f"the module-data verifier did not provide {_CATALOG_URL}")
    try:
        catalog_url = make_url(raw_url)
    except Exception:
        pytest.fail("the restricted Catalog connection URL is invalid", pytrace=False)
    if not (
        catalog_url.get_backend_name() == "postgresql"
        and catalog_url.username == "asset_catalog_client"
        and catalog_url.password
        and catalog_url.host == "127.0.0.1"
        and catalog_url.port is not None
        and catalog_url.database
        and catalog_url.database.endswith("_test")
        and test_engine.url.database == catalog_url.database
    ):
        pytest.fail("Catalog LOGIN URL did not target the owned test database")

    async with test_engine.connect() as owner:
        owner_database = await owner.scalar(text("SELECT current_database()"))
    if owner_database != catalog_url.database or not owner_database.endswith("_test"):
        pytest.fail("owner connection did not target the Catalog test database")

    # Validate the exact verifier-owned container, identity labels, and loopback
    # port before committing fixtures or opening restricted Catalog sessions.
    _verify_postgres(catalog_url)

    operation_a, operation_b = uuid4(), uuid4()
    fixture: dict | None = None
    catalog_engine = create_async_engine(catalog_url, hide_parameters=True)
    try:
        async with async_sessionmaker(test_engine, expire_on_commit=False)() as owner:
            fixture = await _seed_camera_fixture(owner)
            initial_calibration = fixture["calibration"]
            initial_digest = calibration_digest(initial_calibration)
            owner.add(
                SensorCalibrationRevision(
                    dataset_item_id=fixture["dataset_item_id"],
                    revision=1,
                    digest=initial_digest,
                    calibration=initial_calibration.model_dump(mode="json"),
                    created_by=fixture["actor_id"],
                )
            )
            await owner.commit()

        async with catalog_engine.connect() as connection_a:
            async with catalog_engine.connect() as connection_b:
                identity_a = (
                    await connection_a.execute(
                        text(
                            "SELECT current_user, session_user, current_database(), "
                            "pg_backend_pid()"
                        )
                    )
                ).one()
                identity_b = (
                    await connection_b.execute(
                        text(
                            "SELECT current_user, session_user, current_database(), "
                            "pg_backend_pid()"
                        )
                    )
                ).one()
                assert identity_a[:3] == (
                    "asset_catalog_client",
                    "asset_catalog_client",
                    catalog_url.database,
                )
                assert identity_b[:3] == identity_a[:3]
                assert identity_a[3] != identity_b[3]
                await connection_a.commit()
                await connection_b.commit()

                initial_record = await _current_record(
                    connection_b, fixture["dataset_item_id"]
                )
                initial = normalize_calibration_record(initial_record)
                assert initial.revision == 1
                assert initial.digest == initial_digest
                await connection_b.commit()

                updated = _calibration(scale=2.0)
                intent_a = _update_values(
                    operation_id=operation_a,
                    actor_id=fixture["actor_id"],
                    task_id=fixture["task_id"],
                    calibration=updated,
                    expected_revision=initial.revision,
                    expected_digest=initial.digest,
                    current_canonical_document=canonical_calibration_document(
                        initial.calibration
                    ),
                    source_snapshot=initial.source_snapshot,
                )
                intent_b = _update_values(
                    operation_id=operation_b,
                    actor_id=fixture["actor_id"],
                    task_id=fixture["task_id"],
                    calibration=updated,
                    expected_revision=initial.revision,
                    expected_digest=initial.digest,
                    current_canonical_document=canonical_calibration_document(
                        initial.calibration
                    ),
                    source_snapshot=initial.source_snapshot,
                )
                transaction_a = await connection_a.begin()
                transaction_b = None
                try:
                    reply_a = await _call(connection_a, _UPDATE_CALIBRATION, intent_a)
                    result_a = _spatial_result(reply_a, CalibrationMutationResult)
                    assert result_a.state.revision == 2
                    assert result_a.state.digest == calibration_digest(updated)

                    transaction_b = await connection_b.begin()
                    busy = await _call(connection_b, _UPDATE_CALIBRATION, intent_b)
                    _spatial_error(busy, "source_busy")
                    await transaction_b.rollback()
                    transaction_b = None

                    await transaction_a.commit()

                    transaction_b = await connection_b.begin()
                    current_record = await _current_record(
                        connection_b, fixture["dataset_item_id"]
                    )
                    current = normalize_calibration_record(current_record)
                    assert current.revision == 2
                    assert current.digest == result_a.state.digest
                    assert current.source_snapshot["revision"] == 2
                    assert current.source_snapshot["digest"] == result_a.state.digest
                    assert current_record.calibration == updated.model_dump(mode="json")

                    stale_intent = _update_values(
                        operation_id=operation_b,
                        actor_id=fixture["actor_id"],
                        task_id=fixture["task_id"],
                        calibration=updated,
                        expected_revision=initial.revision,
                        expected_digest=initial.digest,
                        current_canonical_document=canonical_calibration_document(
                            current.calibration
                        ),
                        source_snapshot=current.source_snapshot,
                    )
                    assert (
                        stale_intent["expected_revision"]
                        == intent_b["expected_revision"]
                    )
                    assert (
                        stale_intent["expected_digest"] == intent_b["expected_digest"]
                    )
                    conflict = await _call(
                        connection_b, _UPDATE_CALIBRATION, stale_intent
                    )
                    _spatial_error(conflict, "calibration_revision_conflict")
                    await transaction_b.commit()
                    transaction_b = None
                finally:
                    if transaction_b is not None and transaction_b.is_active:
                        await transaction_b.rollback()
                    if transaction_a.is_active:
                        await transaction_a.rollback()

        assert fixture is not None
        async with test_engine.connect() as owner:
            rows = (
                await owner.execute(
                    text(
                        "SELECT revision, digest, calibration "
                        "FROM public.sensor_calibration_revisions "
                        "WHERE dataset_item_id=:item_id ORDER BY revision"
                    ),
                    {"item_id": fixture["dataset_item_id"]},
                )
            ).all()
            assert len(rows) == 2
            assert rows[0].revision == 1
            assert rows[0].digest == initial_digest
            assert rows[0].calibration == initial_calibration.model_dump(mode="json")
            assert rows[1].revision == 2
            assert rows[1].digest == calibration_digest(updated)
            assert rows[1].calibration == updated.model_dump(mode="json")

            audit_counts = (
                await owner.execute(
                    text(
                        "SELECT "
                        "count(*) FILTER (WHERE request_id=:operation_a), "
                        "count(*) FILTER (WHERE request_id=:operation_b), "
                        "count(*) "
                        "FROM public.audit_logs "
                        "WHERE action='sensor_calibration.update' "
                        "AND target_type='dataset_item' AND target_id=:item_id"
                    ),
                    {
                        "operation_a": str(operation_a),
                        "operation_b": str(operation_b),
                        "item_id": str(fixture["dataset_item_id"]),
                    },
                )
            ).one()
            assert audit_counts == (1, 0, 1)

            receipt_counts = (
                await owner.execute(
                    text(
                        "SELECT "
                        "count(*) FILTER (WHERE operation_id=:operation_a), "
                        "count(*) FILTER (WHERE operation_id=:operation_b), "
                        "count(*) "
                        "FROM public.dataset_spatial_operations "
                        "WHERE operation_id IN (:operation_a, :operation_b)"
                    ),
                    {
                        "operation_a": operation_a,
                        "operation_b": operation_b,
                    },
                )
            ).one()
            assert receipt_counts == (1, 0, 1)

            metadata = await owner.scalar(
                text("SELECT metadata FROM public.dataset_items WHERE id=:item_id"),
                {"item_id": fixture["dataset_item_id"]},
            )
            assert metadata["calibration"] == updated.model_dump(mode="json")
            assert metadata["unrelated"] == {"preserve": True}
    finally:
        try:
            if fixture is not None:
                await _cleanup_fixture(test_engine, fixture, operation_a, operation_b)
        finally:
            await catalog_engine.dispose()
