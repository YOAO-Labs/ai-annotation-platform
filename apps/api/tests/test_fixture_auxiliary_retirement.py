"""Real fixture auxiliary retirement, rollback, and exact reclaim boundaries."""

import asyncio
from contextlib import closing
from io import BytesIO
import struct
import uuid

from botocore.exceptions import ClientError
from PIL import Image
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.api.v1 import _test_seed
from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.db.models.managed_original_retirement import ManagedOriginalRetirement
from app.db.models.task import Task
from app.db.models.user import User
from app.services import auxiliary_originals, fixture_run_ownership as ownership
from app.services.assets_operator import AssetsOperatorGateway
from tests.identity_e2e_support import (
    committed_owner as committed_owner,
    e2e_consumer as e2e_consumer,
    fenced_http as fenced_http,
    fixture_and_fenced_http as fixture_and_fenced_http,
)
from tests.seed_owned_scenarios import A, B, _build_owned, _cleanup_owned
from tests.seed_owner_support import _read_versions, fixture_original_reader
from tests.test_auxiliary_originals import map_source, raw_source
from tests.test_dataset_source_deletion import _wait_for_lock_wait


async def _assert_untrusted_session_denied(test_engine, consumer, command, params):
    role = consumer.url.username
    quoted_role = test_engine.dialect.identifier_preparer.quote_identifier(role)
    name = "fixture_aux_forward_" + uuid.uuid4().hex
    quoted_name = test_engine.dialect.identifier_preparer.quote_identifier(name)
    signature = "yoao_fixture_v1.retire_auxiliary_original(uuid,uuid,uuid)"
    forward = f"public.{quoted_name}"

    async with consumer.connect() as connection:
        assert await connection.scalar(text("SELECT session_user")) == role
        with pytest.raises(DBAPIError) as denied:
            async with connection.begin_nested():
                await connection.execute(command, params)
        assert denied.value.orig.sqlstate == "42501"

    async with test_engine.begin() as owner:
        assert await owner.scalar(
            text(
                "SELECT pg_has_role(current_user, relowner, 'MEMBER') "
                "FROM pg_class WHERE oid='yoao_fixture_v1.auxiliary_retirement_guard'::regclass"
            )
        )
        await owner.exec_driver_sql(
            f"GRANT USAGE ON SCHEMA yoao_fixture_v1 TO {quoted_role}"
        )
        await owner.exec_driver_sql(
            f"GRANT EXECUTE ON FUNCTION {signature} TO {quoted_role}"
        )
        await owner.exec_driver_sql(
            f"CREATE FUNCTION {forward}(uuid,uuid,uuid) RETURNS boolean "
            "LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp "
            "AS $forward$ SELECT yoao_fixture_v1.retire_auxiliary_original($1,$2,$3) $forward$"
        )
        await owner.exec_driver_sql(
            f"REVOKE ALL ON FUNCTION {forward}(uuid,uuid,uuid) FROM PUBLIC"
        )
        await owner.exec_driver_sql(
            f"GRANT EXECUTE ON FUNCTION {forward}(uuid,uuid,uuid) TO {quoted_role}"
        )
    try:
        async with consumer.connect() as connection:
            assert await connection.scalar(text("SELECT session_user")) == role
            assert not await connection.scalar(
                text(
                    "SELECT pg_has_role(session_user, relowner, 'MEMBER') "
                    "FROM pg_class WHERE oid='yoao_fixture_v1.auxiliary_retirement_guard'::regclass"
                )
            )
            assert await connection.scalar(
                text(
                    "SELECT has_function_privilege(current_user, :signature, 'EXECUTE')"
                ),
                {"signature": signature},
            )
            forwarded = text(
                f"SELECT {forward}(CAST(:run AS uuid), CAST(:dataset AS uuid), CAST(:owner AS uuid))"
            )
            for statement in (command, forwarded):
                with pytest.raises(DBAPIError) as denied:
                    async with connection.begin_nested():
                        await connection.execute(statement, params)
                assert denied.value.orig.sqlstate == "42501"
                assert (
                    "fixture auxiliary retirement requires the private owner session"
                    in str(denied.value.orig)
                )
    finally:
        async with test_engine.begin() as owner:
            await owner.exec_driver_sql(f"DROP FUNCTION {forward}(uuid,uuid,uuid)")
            await owner.exec_driver_sql(
                f"REVOKE EXECUTE ON FUNCTION {signature} FROM {quoted_role}"
            )
            await owner.exec_driver_sql(
                f"REVOKE USAGE ON SCHEMA yoao_fixture_v1 FROM {quoted_role}"
            )


async def _assert_root_precedes_source_lock(test_engine, root_id, command, params):
    async with (
        test_engine.connect() as blocker,
        test_engine.connect() as waiter,
        test_engine.connect() as observer,
    ):
        blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        await blocker.execute(
            text("SELECT id FROM yoao_fixture_v1.fixture_runs WHERE id=:id FOR UPDATE"),
            {"id": root_id},
        )
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        pending = asyncio.create_task(waiter.scalar(command, params))
        try:
            assert await _wait_for_lock_wait(test_engine, waiter_pid)
            assert await observer.scalar(
                text("SELECT :blocker = ANY(pg_blocking_pids(:waiter))"),
                {"blocker": blocker_pid, "waiter": waiter_pid},
            )
            assert (
                await observer.scalar(
                    text(
                        "SELECT id FROM public.dataset_auxiliary_originals WHERE id=:id FOR UPDATE NOWAIT"
                    ),
                    {"id": params["owner"]},
                )
                == params["owner"]
            )
            assert (
                await observer.scalar(
                    text(
                        "SELECT id FROM public.managed_original_creations WHERE id=:id FOR UPDATE NOWAIT"
                    ),
                    {"id": params["owner"]},
                )
                == params["owner"]
            )
        finally:
            await observer.rollback()
            await blocker.rollback()
            result = await asyncio.wait_for(pending, timeout=5)
            await waiter.rollback()
        assert result is False  # the Dataset still exists


@pytest.mark.asyncio
async def test_fixture_auxiliary_retirement_rolls_back_and_reclaims_only_its_run(
    fixture_and_fenced_http, committed_owner, e2e_consumer, test_engine, monkeypatch
):
    from app.services import managed_original_creation
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async with async_sessionmaker(test_engine)() as owner:
        await managed_original_creation.bootstrap(owner)
    client, db = fixture_and_fenced_http, committed_owner
    monkeypatch.setattr(settings, "storage_maintenance_protocol_enabled", True)
    runs = [client.owner_suite.new_run(namespace) for namespace in (A, B)]
    image = BytesIO()
    Image.new("RGB", (1, 1), (17, 31, 47)).save(image, format="PNG")
    payloads = {
        "map": image.getvalue(),
        "raw_lidar": struct.pack("<5f", 1, 2, 3, 0.5, 7),
    }
    versions = []
    datasets = []
    try:
        for run in runs:
            fixture = await _build_owned(client, run)
            dataset_id = await db.scalar(
                select(DatasetItem.dataset_id)
                .join(Task, Task.dataset_item_id == DatasetItem.id)
                .where(Task.id == uuid.UUID(fixture["task_ids"][0]))
            )
            actor_id = await db.scalar(
                select(User.id).where(User.email == fixture["admin_email"])
            )
            datasets.append(dataset_id)
            admission = await ownership.admit(db, run.run_id, "auxiliary-retirement")
            await db.commit()
            ids = []
            for kind, contract in (("map", map_source()), ("raw_lidar", raw_source())):
                owner = await AssetsOperatorGateway(db).ensure_auxiliary_original(
                    dataset_id=dataset_id,
                    kind=kind,
                    source_contract=contract,
                    logical_path=contract["filename"]
                    if kind == "map"
                    else contract["sample_data"]["filename"],
                    data=payloads[kind],
                    content_type="image/png"
                    if kind == "map"
                    else "application/octet-stream",
                    requested_by=actor_id,
                    fixture_scope=(run.run_id, admission),
                )
                ids.append(uuid.UUID(str(owner.id)))
            await ownership.settle(db, run.run_id, admission)
            await db.commit()
            rows = (
                (
                    await db.execute(
                        select(
                            ManagedOriginalCreation.id,
                            ManagedOriginalCreation.bucket,
                            ManagedOriginalCreation.object_key,
                            ManagedOriginalCreation.version_id,
                            ManagedOriginalCreation.sha256,
                            ManagedOriginalCreation.size_bytes,
                        ).where(ManagedOriginalCreation.id.in_(ids))
                    )
                )
                .mappings()
                .all()
            )
            versions.append(rows)
            await db.rollback()

        params = {
            "run": runs[0].run_id,
            "dataset": datasets[0],
            "owner": versions[0][0]["id"],
        }
        command = text(
            "SELECT yoao_fixture_v1.retire_auxiliary_original("
            "CAST(:run AS uuid), CAST(:dataset AS uuid), CAST(:owner AS uuid))"
        )
        await _assert_untrusted_session_denied(
            test_engine, e2e_consumer, command, params
        )
        assert await db.scalar(command, params) is False  # open run, live Dataset
        await db.rollback()
        assert not await ownership.seal(db, runs[0].run_id, family="owned")
        await db.commit()
        await _assert_root_precedes_source_lock(
            test_engine, runs[0].suite_id, command, params
        )
        with closing(fixture_original_reader()) as reader:
            own_bytes = _read_versions(reader, versions[0])
            neighbor_bytes = _read_versions(reader, versions[1])

        class LogicalAbort(Exception):
            pass

        class RollbackChangedEvidence(Exception):
            pass

        original_retire = auxiliary_originals.retire_fixture_owners

        async def check_native_boundary(session, *, run_id, dataset_id, owner_ids):
            owner_id = owner_ids[0]
            facts = {"run": run_id, "dataset": dataset_id, "owner": owner_id}
            assert (
                await session.scalar(command, {**facts, "run": runs[1].run_id}) is False
            )
            with pytest.raises(DBAPIError) as denied:
                async with session.begin_nested():
                    await session.execute(
                        text("SET LOCAL yoao.restore_training_storage = 'on'")
                    )
                    await session.execute(
                        text(
                            "DELETE FROM public.dataset_auxiliary_originals WHERE id=:owner"
                        ),
                        facts,
                    )
            assert denied.value.orig.sqlstate == "23514"
            with pytest.raises(RollbackChangedEvidence):
                async with session.begin_nested():
                    original_size = await session.scalar(
                        select(ManagedOriginalRetirement.size_bytes).where(
                            ManagedOriginalRetirement.auxiliary_original_id == owner_id
                        )
                    )
                    changed_size = await session.scalar(
                        text(
                            "UPDATE public.managed_original_retirements SET size_bytes=size_bytes+1 "
                            "WHERE auxiliary_original_id=:owner RETURNING size_bytes"
                        ),
                        facts,
                    )
                    assert changed_size == original_size + 1
                    assert await session.scalar(command, facts) is False
                    raise RollbackChangedEvidence()
            for state in ("active", "unknown"):
                with pytest.raises(RollbackChangedEvidence):
                    async with session.begin_nested():
                        await session.execute(
                            text(
                                "INSERT INTO yoao_fixture_v1.fixture_run_admissions "
                                "(id,run_id,kind,state) VALUES (:id,:run,'test',:state)"
                            ),
                            {"id": uuid.uuid4(), "run": run_id, "state": state},
                        )
                        assert await session.scalar(command, facts) is False
                        raise RollbackChangedEvidence()
            assert (
                await session.scalar(
                    select(DatasetAuxiliaryOriginal.id).where(
                        DatasetAuxiliaryOriginal.id == owner_id
                    )
                )
                == owner_id
            )
            await original_retire(
                session, run_id=run_id, dataset_id=dataset_id, owner_ids=owner_ids
            )
            assert (
                await session.scalar(
                    select(DatasetAuxiliaryOriginal.id).where(
                        DatasetAuxiliaryOriginal.id == owner_id
                    )
                )
                is None
            )
            raise LogicalAbort()

        with monkeypatch.context() as patch:
            patch.setattr(
                auxiliary_originals, "retire_fixture_owners", check_native_boundary
            )
            with pytest.raises(LogicalAbort):
                await _test_seed._cleanup_owned_e2e_fixtures(db, runs[0].scope())
        ids = {row["id"] for row in versions[0]}
        assert await db.get(Dataset, datasets[0]) is not None
        assert (
            set(
                await db.scalars(
                    select(DatasetAuxiliaryOriginal.id).where(
                        DatasetAuxiliaryOriginal.id.in_(ids)
                    )
                )
            )
            == ids
        )
        assert not list(
            await db.scalars(
                select(ManagedOriginalRetirement.id).where(
                    ManagedOriginalRetirement.creation_id.in_(ids)
                )
            )
        )
        await db.rollback()

        class InterruptedReclaim(Exception):
            pass

        async def interrupt(*_args):
            raise InterruptedReclaim()

        with monkeypatch.context() as patch:
            patch.setattr(_test_seed, "_reclaim_owned_originals", interrupt)
            with pytest.raises(InterruptedReclaim):
                await _test_seed._cleanup_owned_e2e_fixtures(db, runs[0].scope())
        assert not list(
            await db.scalars(
                select(DatasetAuxiliaryOriginal.id).where(
                    DatasetAuxiliaryOriginal.id.in_(ids)
                )
            )
        )
        retired = list(
            await db.scalars(
                select(ManagedOriginalRetirement).where(
                    ManagedOriginalRetirement.creation_id.in_(ids)
                )
            )
        )
        assert {row.auxiliary_original_id for row in retired} == ids
        assert all(
            row.owner_type == "dataset_auxiliary"
            and row.dataset_item_id is None
            and row.asset_id is None
            for row in retired
        )
        with closing(fixture_original_reader()) as reader:
            assert _read_versions(reader, versions[0]) == own_bytes
        await db.rollback()
        response = await _cleanup_owned(client, runs[0])
        assert response.status_code == 200, response.text
        assert (
            set(
                await db.scalars(
                    select(ManagedOriginalCreation.id).where(
                        ManagedOriginalCreation.id.in_(ids)
                    )
                )
            )
            == ids
        )
        assert (
            set(
                await db.scalars(
                    select(ManagedOriginalRetirement.auxiliary_original_id).where(
                        ManagedOriginalRetirement.creation_id.in_(ids),
                        ManagedOriginalRetirement.owner_type == "dataset_auxiliary",
                    )
                )
            )
            == ids
        )
        with closing(fixture_original_reader()) as reader:
            for row in versions[0]:
                with pytest.raises(ClientError) as absent:
                    reader.head_object(
                        Bucket=row["bucket"],
                        Key=row["object_key"],
                        VersionId=row["version_id"],
                    )
                assert (
                    absent.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
                )
            assert _read_versions(reader, versions[1]) == neighbor_bytes
    finally:
        await db.rollback()
        for run in runs:
            response = await _cleanup_owned(client, run)
            assert response.status_code == 200, response.text
