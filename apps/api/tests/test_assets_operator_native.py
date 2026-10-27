"""Native Assets operator registration receipts, CAS and source ownership."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.dataset import Dataset, DatasetItem, Scene
from aap_assets_source.operator_contract import (
    OPERATOR_NATIVE_ARGUMENTS,
    OPERATOR_NATIVE_SIGNATURES,
    AppendOperatorItemsRequest,
    CreateOperatorImageDatasetRequest,
    EnsureOperatorNuScenesDatasetRequest,
    NuScenesDatasetSpec,
    OperatorCountResult,
    OperatorDatasetMutation,
    OperatorItemsMutation,
    OperatorItemsPage,
    OperatorSourceEntry,
    OperatorSourcePatch,
    decode_operator_reply,
    operator_contract_document,
)
from tests.test_assets_catalog_native import _seed_actor, _seed_dataset

pytestmark = pytest.mark.asyncio

_CREATE = text(
    "SELECT aap_assets_v1.create_operator_image_dataset("
    "CAST(:operation_id AS uuid), CAST(:created_by AS uuid), "
    "CAST(:name AS text), CAST(:description AS text))"
)
_ENSURE = text(
    "SELECT aap_assets_v1.ensure_operator_nuscenes_dataset("
    "CAST(:operation_id AS uuid), CAST(:owner_id AS uuid), CAST(:spec AS jsonb))"
)
_APPEND = text(
    "SELECT aap_assets_v1.append_operator_items("
    "CAST(:operation_id AS uuid), CAST(:dataset_id AS uuid), "
    "CAST(:entries AS jsonb))"
)
_PATCH = text(
    "SELECT aap_assets_v1.patch_operator_items("
    "CAST(:operation_id AS uuid), CAST(:dataset_id AS uuid), "
    "CAST(:entries AS jsonb))"
)
_RECOUNT = text(
    "SELECT aap_assets_v1.recount_operator_items("
    "CAST(:operation_id AS uuid), CAST(:dataset_id AS uuid))"
)
_READ = text(
    "SELECT aap_assets_v1.read_operator_items("
    "CAST(:dataset_id AS uuid), CAST(:after_item_id AS uuid), "
    "CAST(:page_size AS integer))"
)


def _wire_value(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire_value(item) for item in value]
    return value


def _wire(value) -> str:
    return json.dumps(_wire_value(value), ensure_ascii=False, allow_nan=False)


def _body(value) -> dict:
    result = json.loads(value) if isinstance(value, str) else value
    assert isinstance(result, dict)
    return result


def _error(value, code: str) -> None:
    assert _body(value) == {
        "schema": "aap-assets-operator/v1",
        "ok": False,
        "error": {"code": code},
    }


async def _call(db, statement, values: dict):
    return await db.scalar(statement, values)


def _entry(
    item_id: UUID,
    *,
    name: str = "frame.png",
    path: str = "sources/frame.png",
    file_size: int | None = 7,
    content_hash: str | None = "hash",
    metadata: dict | None = None,
) -> OperatorSourceEntry:
    return OperatorSourceEntry(
        id=item_id,
        file_name=name,
        file_path=path,
        file_type="image",
        file_size=file_size,
        content_hash=content_hash,
        metadata=metadata or {},
    )


def _item_insert(
    *,
    item_id: UUID,
    dataset_id: UUID,
    file_name: str = "existing.pcd",
    file_path: str = "legacy/existing.pcd",
    file_type: str = "point_cloud",
    metadata: dict | None = None,
    width: int | None = None,
    height: int | None = None,
    file_size: int | None = 7,
    content_hash: str | None = "old-hash",
    scene_id: UUID | None = None,
    frame_index: int | None = None,
    thumbnail_path: str | None = None,
    blurhash: str | None = None,
) -> DatasetItem:
    return DatasetItem(
        id=item_id,
        dataset_id=dataset_id,
        file_name=file_name,
        file_path=file_path,
        file_type=file_type,
        file_size=file_size,
        content_hash=content_hash,
        width=width,
        height=height,
        metadata_=metadata or {},
        scene_id=scene_id,
        frame_index=frame_index,
        thumbnail_path=thumbnail_path,
        blurhash=blurhash,
    )


async def _seed_existing_dataset(
    db,
    *,
    actor_id: UUID,
    display_id: str,
    name: str,
    data_type: str = "point_cloud",
    is_temporal: bool = False,
    file_count: int = 0,
    metadata: dict | None = None,
) -> UUID:
    dataset_id = uuid4()
    db.add(
        Dataset(
            id=dataset_id,
            display_id=display_id,
            name=name,
            description="preserve this description",
            data_type=data_type,
            is_temporal=is_temporal,
            file_count=file_count,
            created_by=actor_id,
            metadata_=metadata or {},
        )
    )
    await db.flush()
    return dataset_id


async def test_operator_schema_copies_and_native_signature_manifest_match():
    root = Path(__file__).parents[1] / "alembic" / "sql"
    migration_schema = json.loads(
        (root / "0218_assets_operator.schema.json").read_text(encoding="utf-8")
    )
    package_schema_path = (
        Path(__file__).parents[3]
        / "apps/assets-source/src/aap_assets_source/operator_contract.schema.json"
    )
    package_schema = json.loads(package_schema_path.read_text(encoding="utf-8"))
    assert migration_schema == package_schema == operator_contract_document()
    native = migration_schema["x-native"]
    assert set(native) == set(OPERATOR_NATIVE_SIGNATURES)
    assert {
        name: tuple(item["arguments"]) for name, item in native.items()
    } == OPERATOR_NATIVE_ARGUMENTS
    assert all(
        item["signature"].endswith(f"({OPERATOR_NATIVE_SIGNATURES[name]})")
        for name, item in native.items()
    )


async def test_image_dataset_uses_trusted_credential_and_receipt_survives_deletion(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="viewer", active=True)
    operation_id = uuid4()
    request = CreateOperatorImageDatasetRequest(
        operation_id=operation_id,
        created_by=actor_id,
        name="operator image import",
        description="created by an active nonmanager operator attribution",
    )
    values = {
        "operation_id": request.operation_id,
        "created_by": request.created_by,
        "name": request.name,
        "description": request.description,
    }
    first = decode_operator_reply(
        await _call(db_session, _CREATE, values), OperatorDatasetMutation
    )
    assert first.action == "create_image_dataset"
    assert first.dataset_id == operation_id == first.dataset.id
    assert first.dataset.display_id == f"DS-{operation_id.hex[:6].upper()}"
    assert first.dataset.created_by == actor_id
    assert first.dataset.data_type == "image" and not first.dataset.is_temporal
    assert first.storage_root is None and first.needs_pcd_upgrade is None

    replay = decode_operator_reply(
        await _call(db_session, _CREATE, values), OperatorDatasetMutation
    )
    assert replay == first
    _error(
        await _call(
            db_session,
            _CREATE,
            {**values, "name": "different image intent"},
        ),
        "operation_conflict",
    )

    await db_session.execute(
        text("DELETE FROM public.datasets WHERE id=:id"), {"id": operation_id}
    )
    replay_after_delete = decode_operator_reply(
        await _call(db_session, _CREATE, values), OperatorDatasetMutation
    )
    assert replay_after_delete == first


async def test_new_nuscenes_dataset_is_sorted_axis_tagged_and_replayable(db_session):
    actor_id = await _seed_actor(db_session, role="super_admin")
    operation_id = uuid4()
    request = EnsureOperatorNuScenesDatasetRequest(
        operation_id=operation_id,
        owner_id=actor_id,
        spec=NuScenesDatasetSpec(
            display_id=f"DS-OP-{operation_id.hex[:8]}",
            name=f"operator-nuscenes-{operation_id.hex[:8]}",
            version="v1.0-mini",
            scene_names=["scene-z", "scene-a"],
            axis_convention="iso_8855",
            frame="ego",
            pcd_encoding="float32",
        ),
    )
    values = {
        "operation_id": request.operation_id,
        "owner_id": request.owner_id,
        "spec": _wire(request.spec),
    }
    first = decode_operator_reply(
        await _call(db_session, _ENSURE, values), OperatorDatasetMutation
    )
    assert first.action == "ensure_nuscenes_dataset"
    assert first.dataset.display_id == request.spec.display_id
    assert first.dataset.name == request.spec.name
    assert first.dataset.created_by == actor_id
    assert first.dataset.data_type == "point_cloud" and first.dataset.is_temporal
    assert first.needs_pcd_upgrade is False
    assert first.storage_root == f"__aap_trusted_nuscenes__-{first.dataset_id.hex}"
    assert first.dataset.metadata["axis_convention"] == "iso_8855"
    assert first.dataset.metadata["source"] == {
        "format": "nuscenes",
        "version": "v1.0-mini",
        "scenes": ["scene-a", "scene-z"],
        "pcd_encoding": "float32",
        "storage_root": first.storage_root,
        "storage_layout": "uuid_v1",
    }
    await db_session.execute(
        text("DELETE FROM public.datasets WHERE id=:id"), {"id": first.dataset_id}
    )
    assert (
        decode_operator_reply(
            await _call(db_session, _ENSURE, values), OperatorDatasetMutation
        )
        == first
    )


async def test_existing_nuscenes_ensure_preserves_dataset_identity_and_unrelated_metadata(
    db_session,
):
    owner_id = await _seed_actor(db_session, role="super_admin")
    operator_id = await _seed_actor(db_session, role="viewer", active=True)
    operation_id = uuid4()
    display_id = f"DS-EX-{operation_id.hex[:8]}"
    name = f"legacy-root-{operation_id.hex[:8]}"
    original_metadata = {
        "axis_convention": "iso_8855",
        "kept": {"nested": [1, "user value"]},
        "source": {"format": "legacy", "pcd_encoding": "ascii"},
    }
    dataset_id = await _seed_existing_dataset(
        db_session,
        actor_id=owner_id,
        display_id=display_id,
        name=name,
        data_type="image",
        file_count=11,
        metadata=original_metadata,
    )
    request = EnsureOperatorNuScenesDatasetRequest(
        operation_id=operation_id,
        owner_id=operator_id,
        spec=NuScenesDatasetSpec(
            display_id=display_id,
            name="new requested name is not an update",
            version="v1.0-mini",
            scene_names=["scene-2", "scene-1"],
            axis_convention="iso_8855",
            frame="ego",
            pcd_encoding="float32",
        ),
    )
    result = decode_operator_reply(
        await _call(
            db_session,
            _ENSURE,
            {
                "operation_id": request.operation_id,
                "owner_id": request.owner_id,
                "spec": _wire(request.spec),
            },
        ),
        OperatorDatasetMutation,
    )
    assert result.dataset_id == dataset_id
    assert result.dataset.name == name
    assert result.dataset.data_type == "image"
    assert result.dataset.created_by == owner_id
    assert result.dataset.file_count == 11
    assert result.storage_root == request.spec.name
    assert result.needs_pcd_upgrade is True
    assert result.dataset.metadata["axis_convention"] == "iso_8855"
    assert result.dataset.metadata["kept"] == original_metadata["kept"]
    assert result.dataset.metadata["source"] == {
        "format": "nuscenes",
        "version": "v1.0-mini",
        "scenes": ["scene-1", "scene-2"],
        "pcd_encoding": "float32",
        "storage_root": request.spec.name,
        "storage_layout": "legacy_name",
    }


async def test_existing_nuscenes_ensure_propagates_inactive_catalog_owner(db_session):
    active_owner = await _seed_actor(db_session, role="super_admin")
    inactive_owner = await _seed_actor(db_session, role="viewer", active=False)
    operation_id = uuid4()
    display_id = f"DS-IN-{operation_id.hex[:8]}"
    metadata = {
        "axis_convention": "iso_8855",
        "source": {"format": "legacy", "pcd_encoding": "ascii"},
        "preserve": "unchanged",
    }
    dataset_id = await _seed_existing_dataset(
        db_session,
        actor_id=active_owner,
        display_id=display_id,
        name=f"inactive-owner-{operation_id.hex[:8]}",
        metadata=metadata,
    )
    request = EnsureOperatorNuScenesDatasetRequest(
        operation_id=operation_id,
        owner_id=inactive_owner,
        spec=NuScenesDatasetSpec(
            display_id=display_id,
            name="ignored on existing Dataset",
            version="v1.0-mini",
            scene_names=["scene-1"],
            axis_convention="iso_8855",
            frame="ego",
            pcd_encoding="float32",
        ),
    )
    _error(
        await _call(
            db_session,
            _ENSURE,
            {
                "operation_id": request.operation_id,
                "owner_id": request.owner_id,
                "spec": _wire(request.spec),
            },
        ),
        "account_not_active",
    )
    stored = await db_session.scalar(
        text("SELECT metadata FROM public.datasets WHERE id=:id"),
        {"id": dataset_id},
    )
    assert stored == metadata


async def test_ensure_rejects_legacy_root_collision_and_axis_mixing_without_writes(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="super_admin")
    operation_id = uuid4()
    display_id = f"DS-CO-{operation_id.hex[:8]}"
    root = f"shared-root-{operation_id.hex[:8]}"
    metadata = {
        "axis_convention": "iso_8855",
        "source": {"format": "legacy", "pcd_encoding": "ascii"},
        "preserve": {"marker": True},
    }
    dataset_id = await _seed_existing_dataset(
        db_session,
        actor_id=actor_id,
        display_id=display_id,
        name=root,
        metadata=metadata,
    )
    other_dataset_id = await _seed_dataset(
        db_session, actor_id=actor_id, name=f"other-{operation_id.hex[:8]}"
    )
    db_session.add(
        _item_insert(
            item_id=uuid4(),
            dataset_id=other_dataset_id,
            file_path=f"{root}/scene-1/lidar/frame.pcd",
        )
    )
    await db_session.flush()
    spec = NuScenesDatasetSpec(
        display_id=display_id,
        name=root,
        version="v1.0-mini",
        scene_names=["scene-1"],
        axis_convention="iso_8855",
        frame="ego",
        pcd_encoding="float32",
    )
    _error(
        await _call(
            db_session,
            _ENSURE,
            {
                "operation_id": operation_id,
                "owner_id": actor_id,
                "spec": _wire(spec),
            },
        ),
        "storage_root_conflict",
    )
    stored = await db_session.scalar(
        text("SELECT metadata FROM public.datasets WHERE id=:id"),
        {"id": dataset_id},
    )
    assert stored == metadata

    axis_dataset_id = await _seed_existing_dataset(
        db_session,
        actor_id=actor_id,
        display_id=f"DS-AX-{operation_id.hex[:8]}",
        name=f"axis-conflict-{operation_id.hex[:8]}",
        metadata={"axis_convention": "apollo", "preserve": True},
    )
    axis_spec = spec.model_copy(update={"display_id": f"DS-AX-{operation_id.hex[:8]}"})
    _error(
        await _call(
            db_session,
            _ENSURE,
            {
                "operation_id": uuid4(),
                "owner_id": actor_id,
                "spec": _wire(axis_spec),
            },
        ),
        "axis_conflict",
    )
    assert await db_session.scalar(
        text("SELECT metadata FROM public.datasets WHERE id=:id"),
        {"id": axis_dataset_id},
    ) == {"axis_convention": "apollo", "preserve": True}


async def test_append_preserves_input_order_increments_count_and_rejects_prefix_collision(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="super_admin")
    dataset_id = await _seed_dataset(
        db_session, actor_id=actor_id, name=f"append-{uuid4().hex[:8]}"
    )
    operation_id = uuid4()
    entries = [
        _entry(UUID(int=200), name="two.png", metadata={"ordinal": 2}),
        _entry(UUID(int=100), name="one.png", metadata={"ordinal": 1}),
    ]
    request = AppendOperatorItemsRequest(
        operation_id=operation_id, dataset_id=dataset_id, entries=entries
    )
    result = decode_operator_reply(
        await _call(
            db_session,
            _APPEND,
            {
                "operation_id": operation_id,
                "dataset_id": dataset_id,
                "entries": _wire(request.entries),
            },
        ),
        OperatorItemsMutation,
    )
    assert result.action == "append_items"
    assert [item.id for item in result.items] == [entry.id for entry in entries]
    assert result.file_count == 2 and result.dimensions_backfilled == 0
    assert [item.metadata["ordinal"] for item in result.items] == [2, 1]
    replay = decode_operator_reply(
        await _call(
            db_session,
            _APPEND,
            {
                "operation_id": operation_id,
                "dataset_id": dataset_id,
                "entries": _wire(request.entries),
            },
        ),
        OperatorItemsMutation,
    )
    assert replay == result

    collision_dataset = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"append-collision-{uuid4().hex[:8]}",
        file_count=1,
    )
    existing_id, prefix_id = uuid4(), uuid4()
    db_session.add(_item_insert(item_id=existing_id, dataset_id=collision_dataset))
    await db_session.flush()
    collision_entries = [
        _entry(prefix_id, name="would-be-prefix.png"),
        _entry(existing_id, name="already-registered.png"),
    ]
    _error(
        await _call(
            db_session,
            _APPEND,
            {
                "operation_id": uuid4(),
                "dataset_id": collision_dataset,
                "entries": _wire(collision_entries),
            },
        ),
        "source_changed",
    )
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.dataset_items WHERE dataset_id=:id"),
            {"id": collision_dataset},
        )
        == 1
    )
    assert (
        await db_session.scalar(
            text("SELECT file_count FROM public.datasets WHERE id=:id"),
            {"id": collision_dataset},
        )
        == 1
    )
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM public.dataset_items WHERE id=:id"),
            {"id": prefix_id},
        )
        == 0
    )


async def test_late_operator_receipt_failure_rolls_back_item_and_count_prefix(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="super_admin")
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"receipt-failure-{uuid4().hex[:8]}",
        file_count=0,
    )
    operation_id, item_id = uuid4(), uuid4()
    suffix = uuid4().hex[:12]
    function_name = f"operator_receipt_failure_{suffix}"
    trigger_name = f"operator_receipt_failure_{suffix}"
    created_function = False
    created_trigger = False
    try:
        await db_session.execute(
            text(
                f"CREATE FUNCTION public.{function_name}() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                f"IF NEW.operation_id = '{operation_id}'::uuid THEN "
                "RAISE EXCEPTION 'operator receipt failure'; "
                "END IF; RETURN NEW; END $$"
            )
        )
        created_function = True
        await db_session.execute(
            text(
                f"CREATE TRIGGER {trigger_name} BEFORE INSERT "
                "ON public.dataset_operator_operations "
                f"FOR EACH ROW EXECUTE FUNCTION public.{function_name}()"
            )
        )
        created_trigger = True
        _error(
            await _call(
                db_session,
                _APPEND,
                {
                    "operation_id": operation_id,
                    "dataset_id": dataset_id,
                    "entries": _wire([_entry(item_id)]),
                },
            ),
            "storage_unavailable",
        )
        assert (
            await db_session.scalar(
                text("SELECT count(*) FROM public.dataset_items WHERE id=:id"),
                {"id": item_id},
            )
            == 0
        )
        assert (
            await db_session.scalar(
                text("SELECT file_count FROM public.datasets WHERE id=:id"),
                {"id": dataset_id},
            )
            == 0
        )
        assert (
            await db_session.scalar(
                text(
                    "SELECT count(*) FROM public.dataset_operator_operations "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            == 0
        )
    finally:
        if created_trigger:
            await db_session.execute(
                text(
                    f"DROP TRIGGER {trigger_name} ON public.dataset_operator_operations"
                )
            )
        if created_function:
            await db_session.execute(text(f"DROP FUNCTION public.{function_name}()"))


async def test_patch_cas_merges_only_observations_and_preserves_scene_metadata(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="super_admin")
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"patch-{uuid4().hex[:8]}",
        data_type="point_cloud",
        file_count=1,
    )
    scene_id, item_id = uuid4(), uuid4()
    db_session.add(
        Scene(
            id=scene_id,
            display_id=f"SC{scene_id.hex[:10]}",
            dataset_id=dataset_id,
            name="scene-0001",
            source_format="nuscenes",
            source_metadata={"keep": True},
            created_by=actor_id,
        )
    )
    await db_session.flush()
    db_session.add(
        _item_insert(
            item_id=item_id,
            dataset_id=dataset_id,
            file_name="camera.jpg",
            file_path="root/scene-0001/camera/front.jpg",
            file_type="image",
            metadata={
                "calibration": {"intrinsic": [1, 2, 3]},
                "user_note": "keep me",
                "nuscenes_export": {"version": "old"},
            },
            width=0,
            height=480,
            file_size=10,
            content_hash="old-hash",
            scene_id=scene_id,
            frame_index=7,
            thumbnail_path="thumbnail/keep.png",
            blurhash="blurhash-keep",
        )
    )
    await db_session.flush()
    original_page = decode_operator_reply(
        await _call(
            db_session,
            _READ,
            {
                "dataset_id": dataset_id,
                "after_item_id": None,
                "page_size": 10,
            },
        ),
        OperatorItemsPage,
    )
    original = original_page.items[0]
    patch = OperatorSourcePatch(
        id=item_id,
        source_snapshot=original.source_snapshot,
        nuscenes_export={"version": "new", "source_bytes": 99},
        dimensions={"width": 800, "height": 600},
        file_size=99,
        content_hash="new-hash",
    )
    patched = decode_operator_reply(
        await _call(
            db_session,
            _PATCH,
            {
                "operation_id": uuid4(),
                "dataset_id": dataset_id,
                "entries": _wire([patch]),
            },
        ),
        OperatorItemsMutation,
    )
    assert patched.dimensions_backfilled == 1
    item = patched.items[0]
    assert item.width == 800 and item.height == 600
    assert item.file_size == 99 and item.content_hash == "new-hash"
    assert item.scene_id == scene_id and item.frame_index == 7
    assert item.metadata["calibration"] == {"intrinsic": [1, 2, 3]}
    assert item.metadata["user_note"] == "keep me"
    assert item.metadata["nuscenes_export"] == {
        "version": "new",
        "source_bytes": 99,
    }
    stored_media = (
        await db_session.execute(
            text(
                "SELECT thumbnail_path,blurhash,media_source_generation,"
                "media_materialized_generation FROM public.dataset_items "
                "WHERE id=:id"
            ),
            {"id": item_id},
        )
    ).one()
    # The operator never owns derived media. Changing the admitted source facts
    # (file_size/content_hash) drives the Assets media authority
    # (trg_dataset_items_media_source_generation then
    # trg_dataset_items_image_source_invalidated), which bumps the source
    # generation and drops the now-stale thumbnail rather than keeping it.
    assert stored_media.thumbnail_path is None
    assert stored_media.blurhash is None
    assert stored_media.media_source_generation == 2
    assert stored_media.media_materialized_generation == 0

    _error(
        await _call(
            db_session,
            _PATCH,
            {
                "operation_id": uuid4(),
                "dataset_id": dataset_id,
                "entries": _wire(
                    [
                        OperatorSourcePatch(
                            id=item_id,
                            source_snapshot=original.source_snapshot,
                            file_size=500,
                        )
                    ]
                ),
            },
        ),
        "source_changed",
    )
    assert (
        await db_session.scalar(
            text("SELECT file_size FROM public.dataset_items WHERE id=:id"),
            {"id": item_id},
        )
        == 99
    )

    current_page = decode_operator_reply(
        await _call(
            db_session,
            _READ,
            {
                "dataset_id": dataset_id,
                "after_item_id": None,
                "page_size": 10,
            },
        ),
        OperatorItemsPage,
    )
    no_op = decode_operator_reply(
        await _call(
            db_session,
            _PATCH,
            {
                "operation_id": uuid4(),
                "dataset_id": dataset_id,
                "entries": _wire(
                    [
                        OperatorSourcePatch(
                            id=item_id,
                            source_snapshot=current_page.items[0].source_snapshot,
                        )
                    ]
                ),
            },
        ),
        OperatorItemsMutation,
    )
    assert no_op.dimensions_backfilled == 0
    assert no_op.items[0].file_size == 99


async def test_operator_paging_has_stable_full_scope_digest_and_recount_replays_first_count(
    db_session,
):
    actor_id = await _seed_actor(db_session, role="super_admin")
    dataset_id = await _seed_dataset(
        db_session,
        actor_id=actor_id,
        name=f"paging-{uuid4().hex[:8]}",
        file_count=91,
    )
    ids = [UUID(int=100), UUID(int=200), UUID(int=300)]
    for index, item_id in enumerate(ids):
        db_session.add(
            _item_insert(
                item_id=item_id,
                dataset_id=dataset_id,
                file_name=f"frame-{index}.pcd",
                file_path=f"root/frame-{index}.pcd",
                metadata={"index": index},
            )
        )
    await db_session.flush()

    first = decode_operator_reply(
        await _call(
            db_session,
            _READ,
            {"dataset_id": dataset_id, "after_item_id": None, "page_size": 2},
        ),
        OperatorItemsPage,
    )
    assert [item.id for item in first.items] == ids[:2]
    assert first.total == 3 and first.complete is False
    assert first.next_item_id == ids[1]
    second = decode_operator_reply(
        await _call(
            db_session,
            _READ,
            {
                "dataset_id": dataset_id,
                "after_item_id": first.next_item_id,
                "page_size": 2,
            },
        ),
        OperatorItemsPage,
    )
    assert [item.id for item in second.items] == ids[2:]
    assert second.total == 3 and second.complete is True
    assert second.next_item_id is None
    assert second.snapshot_digest == first.snapshot_digest

    operation_id = uuid4()
    count_values = {
        "operation_id": operation_id,
        "dataset_id": dataset_id,
    }
    first_count = decode_operator_reply(
        await _call(db_session, _RECOUNT, count_values), OperatorCountResult
    )
    assert first_count.file_count == 3
    db_session.add(
        _item_insert(
            item_id=UUID(int=400),
            dataset_id=dataset_id,
            file_name="later.pcd",
            file_path="root/later.pcd",
        )
    )
    await db_session.flush()
    replay = decode_operator_reply(
        await _call(db_session, _RECOUNT, count_values), OperatorCountResult
    )
    assert replay == first_count
    assert (
        await db_session.scalar(
            text("SELECT file_count FROM public.datasets WHERE id=:id"),
            {"id": dataset_id},
        )
        == 3
    )


async def test_operator_write_is_fenced_by_existing_source_delete_dataset_lock(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as setup:
        actor_id = await _seed_actor(setup, role="super_admin")
        dataset_id = await _seed_dataset(
            setup, actor_id=actor_id, name=f"delete-lock-{uuid4().hex[:8]}"
        )
        await setup.commit()

    deleting = maker()
    writing = maker()
    operation_id = uuid4()
    try:
        assert (
            await deleting.scalar(
                text("SELECT aap_assets_v1.lock_dataset_reference(:id)"),
                {"id": dataset_id},
            )
            == dataset_id
        )
        entry = _entry(uuid4())
        _error(
            await _call(
                writing,
                _APPEND,
                {
                    "operation_id": operation_id,
                    "dataset_id": dataset_id,
                    "entries": _wire([entry]),
                },
            ),
            "operation_busy",
        )
    finally:
        await writing.rollback()
        await writing.close()
        await deleting.rollback()
        await deleting.close()

    async with maker() as retry:
        inserted = decode_operator_reply(
            await _call(
                retry,
                _APPEND,
                {
                    "operation_id": operation_id,
                    "dataset_id": dataset_id,
                    "entries": _wire([entry]),
                },
            ),
            OperatorItemsMutation,
        )
        assert [item.id for item in inserted.items] == [entry.id]
        await retry.commit()
