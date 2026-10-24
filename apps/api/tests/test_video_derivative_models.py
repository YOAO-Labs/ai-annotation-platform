"""Database-enforced video ownership, one-shot leases and cascade regressions.

The repository fixtures apply migrations only to the verified disposable test
database. These tests perform no provider I/O and start no services.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import uuid

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.db.models.dataset import Dataset, DatasetItem, VideoChunk, VideoFrameCache
from app.db.models.video_derivative import VideoDerivativeOutput as Output


def _storage_ref(identifier):
    return {
        "schema": 1,
        "purpose": "media-cache",
        "location_id": "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7",
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": "http://saved-video.test:9000",
        "bucket": "saved-video-cache",
        "prefix": f"video-derivatives/objects/{identifier}/",
        "storage_identity": "a" * 64,
    }


def _values(**changes):
    identifier = changes.get("id", uuid.uuid4())
    kind = changes.get("kind", "metadata")
    return {
        "id": identifier,
        "dataset_item_id": uuid.uuid4(),
        "kind": kind,
        "spec": {"profile": "bounded-test"},
        "original_ref": {"schema": 1, "sha256": "b" * 64},
        "auth_ref": {"schema": 1, "mode": "system"},
        "metadata_output_id": None if kind == "metadata" else uuid.uuid4(),
        "storage_ref": _storage_ref(identifier),
        "state": "building",
        **changes,
    }


async def _new(db, **changes):
    row = Output(**_values(**changes))
    db.add(row)
    await db.flush()
    return row


async def _claim(db, row):
    row.lease_token = uuid.uuid4()
    row.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    await db.flush()


def _publication(row):
    return {
        "inventory": {
            "schema": 1,
            "objects": {
                row.storage_ref["prefix"] + "verified.bin": {
                    "sha256": "c" * 64,
                    "size_bytes": 17,
                }
            },
        },
        "result": {"video": {"fps": 30, "frame_count": 3}, "frames": []},
    }


async def _ready(db, row):
    await _claim(db, row)
    values = _publication(row)
    row.inventory, row.result = values["inventory"], values["result"]
    row.state = "ready"
    await db.flush()


async def _settled(db, row):
    row.state = "retiring"
    await db.flush()
    row.state = "deleted"
    await db.flush()


async def _reject_update(db, row, **values):
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                update(Output)
                .where(Output.id == row.id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )


@pytest.mark.parametrize("kind", ["metadata", "frame", "chunk"])
async def test_attempt_retains_inputs_without_a_business_owner(db_session, kind):
    row = await _new(db_session, kind=kind)
    await db_session.refresh(row)
    assert row.kind == kind
    assert row.dataset_item_id is not None
    assert row.spec == {"profile": "bounded-test"}
    assert row.auth_ref == {"schema": 1, "mode": "system"}
    assert row.original_ref["sha256"] == "b" * 64
    assert (row.metadata_output_id is None) == (kind == "metadata")
    assert row.lease_token is None and row.lease_expires_at is None


@pytest.mark.parametrize("state", ["ready", "retiring", "deleted", "failed"])
async def test_new_attempt_cannot_skip_initial_allocation(db_session, state):
    values = _values(state=state)
    if state == "ready":
        values.update(
            lease_token=uuid.uuid4(),
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            inventory={"schema": 1, "objects": {}},
            result={},
        )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(Output(**values))
            await db_session.flush()


async def test_new_attempt_cannot_arrive_preclaimed(db_session):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _new(
                db_session,
                lease_token=uuid.uuid4(),
                lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )


@pytest.mark.parametrize("column", ["spec", "original_ref", "auth_ref"])
@pytest.mark.parametrize("invalid", [None, [], "not-an-object"])
async def test_frozen_input_receipts_require_json_objects(db_session, column, invalid):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _new(db_session, **{column: invalid})


@pytest.mark.parametrize(
    "kind,metadata_output_id",
    [("metadata", uuid.uuid4()), ("frame", None), ("chunk", None)],
)
async def test_only_metadata_attempt_has_no_metadata_generation(
    db_session, kind, metadata_output_id
):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _new(db_session, kind=kind, metadata_output_id=metadata_output_id)


async def test_unknown_derivative_kind_cannot_allocate_an_owner(db_session):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _new(db_session, kind="poster")


@pytest.mark.parametrize(
    "column,invalid",
    [
        ("schema", "1"),
        ("purpose", "annotations"),
        ("location_id", str(uuid.uuid4())),
        ("config_revision", 2),
        ("binding_generation", 2),
        ("endpoint_url", "http://user:password@host"),
        ("endpoint_url", "http://host/path"),
        ("bucket", "bucket/path"),
        ("bucket", ""),
        ("prefix", f"video-derivatives/objects/{uuid.uuid4()}/"),
        ("storage_identity", "g" * 64),
        ("unexpected", "unfrozen"),
    ],
)
async def test_saved_storage_anchor_and_attempt_prefix_are_enforced(
    db_session, column, invalid
):
    values = _values()
    values["storage_ref"][column] = invalid
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(Output(**values))
            await db_session.flush()


@pytest.mark.parametrize(
    "column",
    [
        "id",
        "dataset_item_id",
        "kind",
        "spec",
        "original_ref",
        "auth_ref",
        "metadata_output_id",
        "storage_ref",
        "created_at",
    ],
)
async def test_attempt_facts_cannot_be_rewritten(db_session, column):
    row = await _new(db_session, kind="frame")
    identifier = uuid.uuid4()
    changed = {
        "id": identifier,
        "dataset_item_id": uuid.uuid4(),
        "kind": "chunk",
        "spec": {"profile": "changed"},
        "original_ref": {"schema": 1, "sha256": "d" * 64},
        "auth_ref": {"schema": 1, "mode": "http"},
        "metadata_output_id": uuid.uuid4(),
        "storage_ref": {**row.storage_ref, "bucket": "another-saved-cache"},
        "created_at": row.created_at - timedelta(days=1),
    }[column]
    values = {column: changed}
    if column == "id":
        values["storage_ref"] = _storage_ref(identifier)
    await _reject_update(db_session, row, **values)


@pytest.mark.parametrize("part", ["token", "expiry", "both"])
async def test_lease_cannot_be_replaced_extended_or_cleared(db_session, part):
    row = await _new(db_session)
    await _claim(db_session, row)
    values = {
        "token": {"lease_token": uuid.uuid4()},
        "expiry": {"lease_expires_at": row.lease_expires_at + timedelta(minutes=1)},
        "both": {"lease_token": None, "lease_expires_at": None},
    }[part]
    await _reject_update(db_session, row, **values)


@pytest.mark.parametrize("part", ["token", "expiry"])
async def test_lease_claim_requires_the_complete_pair(db_session, part):
    row = await _new(db_session)
    values = (
        {"lease_token": uuid.uuid4()}
        if part == "token"
        else {"lease_expires_at": datetime.now(timezone.utc) + timedelta(minutes=10)}
    )
    await _reject_update(db_session, row, **values)


async def test_retired_attempt_cannot_be_claimed(db_session):
    row = await _new(db_session)
    row.state = "retiring"
    await db_session.flush()
    await _reject_update(
        db_session,
        row,
        lease_token=uuid.uuid4(),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )


async def test_publish_requires_an_already_claimed_attempt(db_session):
    row = await _new(db_session)
    await _reject_update(db_session, row, state="ready", **_publication(row))
    await _reject_update(
        db_session,
        row,
        state="ready",
        lease_token=uuid.uuid4(),
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        **_publication(row),
    )


@pytest.mark.parametrize("column", ["inventory", "result"])
async def test_publication_cannot_publish_only_one_half(db_session, column):
    row = await _new(db_session)
    await _claim(db_session, row)
    values = _publication(row)
    values[column] = None
    await _reject_update(db_session, row, state="ready", **values)


@pytest.mark.parametrize(
    "column,invalid",
    [
        ("inventory", []),
        ("result", []),
        ("inventory", {"schema": 2, "objects": {}}),
        ("inventory", {"schema": 1, "objects": []}),
        ("inventory", {"schema": 1, "objects": {}, "extra": True}),
    ],
)
async def test_publication_requires_shaped_inventory_and_object_result(
    db_session, column, invalid
):
    row = await _new(db_session)
    await _claim(db_session, row)
    values = _publication(row)
    values[column] = invalid
    await _reject_update(db_session, row, state="ready", **values)


@pytest.mark.parametrize("column", ["inventory", "result"])
async def test_published_receipts_cannot_be_rewritten(db_session, column):
    row = await _new(db_session)
    await _ready(db_session, row)
    changed = deepcopy(getattr(row, column))
    if column == "result":
        changed["video"]["fps"] = 60
    else:
        first = next(iter(changed["objects"].values()))
        first["sha256"] = "e" * 64
    await _reject_update(db_session, row, **{column: changed})


@pytest.mark.parametrize(
    "initial,target",
    [
        ("building", "deleted"),
        ("ready", "building"),
        ("retiring", "ready"),
        ("deleted", "retiring"),
    ],
)
async def test_attempt_state_cannot_skip_retirement_or_move_backwards(
    db_session, initial, target
):
    row = await _new(db_session)
    if initial == "ready":
        await _ready(db_session, row)
    elif initial in {"retiring", "deleted"}:
        row.state = "retiring"
        await db_session.flush()
        if initial == "deleted":
            row.state = "deleted"
            await db_session.flush()
    await _reject_update(db_session, row, state=target)


async def test_publication_survives_forward_retirement_and_terminal_delete(db_session):
    row = await _new(db_session)
    await _ready(db_session, row)
    saved = deepcopy(row.inventory), deepcopy(row.result), deepcopy(row.original_ref)
    row.state, row.error_code = "retiring", "expired"
    await db_session.flush()
    row.state = "deleted"
    await db_session.flush()
    await db_session.refresh(row)
    assert (row.inventory, row.result, row.original_ref) == saved
    await _reject_update(
        db_session, row, updated_at=row.updated_at + timedelta(seconds=1)
    )
    await db_session.execute(delete(Output).where(Output.id == row.id))
    assert await db_session.scalar(select(Output.id).where(Output.id == row.id)) is None


async def test_error_can_be_recorded_while_building_and_retiring_only(db_session):
    row = await _new(db_session)
    row.error_code = "waiting"
    await db_session.flush()
    row.error_code = None
    await db_session.flush()
    await _ready(db_session, row)
    await _reject_update(db_session, row, error_code="late-error")
    row.state, row.error_code = "retiring", "late-error"
    await db_session.flush()
    assert row.error_code == "late-error"


@pytest.mark.parametrize("state", ["building", "ready", "retiring"])
async def test_unsettled_physical_responsibility_cannot_be_deleted(db_session, state):
    row = await _new(db_session)
    if state == "ready":
        await _ready(db_session, row)
    elif state == "retiring":
        row.state = "retiring"
        await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(delete(Output).where(Output.id == row.id))


@pytest.fixture
async def video_item(db_session, super_admin):
    user, _ = super_admin
    dataset = Dataset(
        display_id="D-VD-" + uuid.uuid4().hex[:10],
        name="Disposable video model responsibility",
        data_type="video",
        created_by=user.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name="source.mp4",
        file_path="test/video-models/source.mp4",
        file_type="video",
    )
    db_session.add(item)
    await db_session.flush()
    return dataset, item


async def _business_pointer(db, item, row, kind):
    if kind == "metadata":
        item.video_output_id = row.id
        value = item
    elif kind == "frame":
        value = VideoFrameCache(
            dataset_item_id=item.id,
            frame_index=1,
            width=512,
            format="webp",
            output_id=row.id,
            status="pending",
        )
        db.add(value)
    else:
        value = VideoChunk(
            dataset_item_id=item.id,
            chunk_id=0,
            start_frame=0,
            end_frame=2,
            output_id=row.id,
            status="pending",
        )
        db.add(value)
    await db.flush()
    return value


@pytest.mark.parametrize("kind", ["metadata", "frame", "chunk"])
async def test_business_reference_restricts_even_terminal_owner_deletion(
    db_session, video_item, kind
):
    _, item = video_item
    row = await _new(db_session, dataset_item_id=item.id, kind=kind)
    await _business_pointer(db_session, item, row, kind)
    await _settled(db_session, row)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(delete(Output).where(Output.id == row.id))


async def test_dataset_cascade_retains_all_physical_inputs_and_publications(
    db_session, video_item
):
    dataset, item = video_item
    rows = []
    for kind in ("metadata", "frame", "chunk"):
        row = await _new(db_session, dataset_item_id=item.id, kind=kind)
        await _ready(db_session, row)
        await _business_pointer(db_session, item, row, kind)
        rows.append(row)
    item_id, dataset_id = item.id, dataset.id
    identifiers = [row.id for row in rows]
    expected = {
        row.id: (
            deepcopy(row.original_ref),
            deepcopy(row.auth_ref),
            deepcopy(row.inventory),
        )
        for row in rows
    }
    await db_session.execute(delete(Dataset).where(Dataset.id == dataset_id))
    for model in (DatasetItem, VideoFrameCache, VideoChunk):
        predicate = (
            model.id == item_id
            if model is DatasetItem
            else model.dataset_item_id == item_id
        )
        assert await db_session.scalar(select(model.id).where(predicate)) is None
    retained = (
        await db_session.scalars(select(Output).where(Output.id.in_(identifiers)))
    ).all()
    assert len(retained) == 3
    for row in retained:
        assert row.state == "ready"
        assert (row.original_ref, row.auth_ref, row.inventory) == expected[row.id]


async def test_metadata_lineage_uuid_is_evidence_not_a_business_lifetime_fk(db_session):
    metadata = await _new(db_session)
    frame = await _new(db_session, kind="frame", metadata_output_id=metadata.id)
    identifier = metadata.id
    await _settled(db_session, metadata)
    await db_session.execute(delete(Output).where(Output.id == identifier))
    await db_session.refresh(frame)
    assert frame.metadata_output_id == identifier


@pytest.mark.parametrize("settled", [False, True])
async def test_downgrade_refuses_saved_responsibility_before_ddl(
    db_session, monkeypatch, settled
):
    row = await _new(db_session)
    if settled:
        await _settled(db_session, row)
    path = (
        Path(__file__).parents[1] / "alembic/versions/0233_video_derivative_outputs.py"
    )
    spec = importlib.util.spec_from_file_location("video_derivative_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def refused(connection):
        with monkeypatch.context() as patch:
            patch.setattr(module.op, "get_bind", lambda: connection)
            with pytest.raises(RuntimeError, match="Cannot discard saved video"):
                module.downgrade()

    await (await db_session.connection()).run_sync(refused)
