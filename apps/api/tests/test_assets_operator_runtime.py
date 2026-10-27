"""Actual operator consumers against owned PostgreSQL and object storage."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.test_asset_storage_cleanup_runtime import (
    _required_env,
    _verified_resource_manifest,
    _verify_bucket_owner,
    _verify_postgres,
)
from tests.test_assets_catalog_native import _seed_actor
from tests.test_import_nuscenes_lite import _write_fake_nuscenes

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable module-data verifier",
    ),
]
ROOT = Path(__file__).resolve().parents[3]


def _resources(test_engine):
    from app.services.storage import storage_service

    role_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_OPERATOR_URL"))
    assert role_url.username == "asset_operator_client"
    assert role_url.database == test_engine.url.database
    assert role_url.database.endswith("_test")
    _verify_postgres(role_url)
    resources = _verified_resource_manifest(test_engine, role_url)
    bucket = resources["buckets"]["MINIO_DATASETS_BUCKET"]
    assert storage_service.datasets_bucket == bucket
    _verify_bucket_owner(storage_service.client, bucket, resources["owner"])
    return resources, storage_service


def _consumer_engine(test_engine):
    engine = create_async_engine(test_engine.url, hide_parameters=True)

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def no_private_source_writes(
        _conn, _cursor, statement, _parameters, _context, _many
    ):
        if re.search(
            r'\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:public\.)?"?(?:datasets|dataset_items)(?:"|\s|\()',
            statement,
            re.I,
        ):
            raise AssertionError("operator consumer bypassed the Assets source writer")

    return engine


async def _count(engine, statement, **values):
    async with engine.connect() as db:
        return await db.scalar(text(statement), values)


def _evidence(name, values):
    output = ROOT / ".worktree/assets-operator-consumer-runtime.json"
    output.parent.mkdir(exist_ok=True)
    current = json.loads(output.read_text()) if output.exists() else {}
    current[name] = values
    output.write_text(json.dumps(current, indent=2) + "\n")


async def test_local_image_import_uses_native_rows_and_retains_uploads_on_sql_failure(
    test_engine, app_module, tmp_path, monkeypatch
):
    from scripts import import_images
    from app.services.assets_operator import AssetsOperatorGateway

    _, storage = _resources(test_engine)
    engine = _consumer_engine(test_engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with async_sessionmaker(test_engine, expire_on_commit=False).begin() as owner:
        await _seed_actor(owner, role="viewer")
    directory = tmp_path / "images"
    (directory / "nested").mkdir(parents=True)
    for relative in ("first.png", "nested/second.jpg", "nested/第三张.png"):
        Image.new("RGB", (3, 2), (30, 60, 90)).save(directory / relative)
    name = "operator-image-" + uuid4().hex
    prefix = "operator-image-source/" + uuid4().hex
    monkeypatch.setattr(import_images, "engine", engine)
    monkeypatch.setattr(import_images, "Session", maker)
    monkeypatch.setattr(import_images, "get_s3_client", lambda: storage.client)
    try:
        await import_images.import_to_dataset(str(directory), name, prefix, None)
        async with test_engine.connect() as owner:
            dataset = (
                await owner.execute(
                    text("SELECT id,file_count FROM public.datasets WHERE name=:name"),
                    {"name": name},
                )
            ).one()
            rows = (
                await owner.execute(
                    text(
                        "SELECT file_path,file_size FROM public.dataset_items WHERE dataset_id=:id"
                    ),
                    {"id": dataset.id},
                )
            ).all()
        assert dataset.file_count == len(rows) == 3
        assert {row.file_path for row in rows} == {
            f"{prefix}/{relative}"
            for relative in ("first.png", "nested/second.jpg", "nested/第三张.png")
        }
        for row in rows:
            assert (
                storage.client.head_object(
                    Bucket=storage.datasets_bucket, Key=row.file_path
                )["ContentLength"]
                == row.file_size
            )

        receipts_before = await _count(
            test_engine, "SELECT count(*) FROM public.dataset_operator_operations"
        )
        failed_name = "operator-image-rollback-" + uuid4().hex
        failed_prefix = "operator-image-source/" + uuid4().hex
        append = AssetsOperatorGateway.append_operator_items

        async def fail_after_native_insert(gateway, request):
            await append(gateway, request)
            raise RuntimeError("owned operator consumer late failure")

        with monkeypatch.context() as failing:
            failing.setattr(
                AssetsOperatorGateway, "append_operator_items", fail_after_native_insert
            )
            with pytest.raises(
                RuntimeError, match="owned operator consumer late failure"
            ):
                await import_images.import_to_dataset(
                    str(directory), failed_name, failed_prefix, None
                )
        assert (
            await _count(
                test_engine,
                "SELECT count(*) FROM public.datasets WHERE name=:name",
                name=failed_name,
            )
            == 0
        )
        assert (
            await _count(
                test_engine, "SELECT count(*) FROM public.dataset_operator_operations"
            )
            == receipts_before
        )
        retained = storage.client.list_objects_v2(
            Bucket=storage.datasets_bucket, Prefix=failed_prefix + "/"
        )
        assert retained["KeyCount"] == 3
        _evidence(
            "local_images",
            {
                "status": "passed",
                "real_s3": True,
                "items": 3,
                "relative_paths": True,
                "no_private_dml": True,
                "outer_rollback": True,
                "uncertain_objects_retained": 3,
            },
        )
    finally:
        await engine.dispose()
        # Rows, immutable receipts and objects remain until this owned DB/bucket teardown.


async def test_nuscenes_native_source_writes_preserve_reimport_and_cross_owner_rollback(
    test_engine, app_module, tmp_path, monkeypatch
):
    from scripts.import_nuscenes_scene import import_nuscenes
    from app.config import settings
    from app.services.dataset import DatasetService

    _, storage = _resources(test_engine)
    engine = _consumer_engine(test_engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with async_sessionmaker(test_engine, expire_on_commit=False).begin() as owner:
        actor_id = await _seed_actor(owner, role="super_admin")
    source = tmp_path / "nuscenes"
    _write_fake_nuscenes(source, scenes=1, samples_per=2)
    suffix = uuid4().hex[:10]
    name = "operator-nuscenes-" + suffix
    monkeypatch.setattr(settings, "assets_catalog_url", "")
    try:
        async with maker() as caller:
            result = await import_nuscenes(
                caller,
                nuscenes_root=source,
                scene_names=["scene-0000"],
                dataset_name=name,
                owner_id=actor_id,
                dataset_display_id="DS-OP-" + suffix,
                project_display_id="P-OP-" + suffix,
            )
            await caller.commit()
        dataset_id, project_id = result["dataset_id"], result["project_id"]
        assert (
            await _count(
                test_engine,
                "SELECT count(*) FROM public.dataset_items WHERE dataset_id=:id",
                id=dataset_id,
            )
            == 5
        )
        assert (
            await _count(
                test_engine,
                "SELECT file_count FROM public.datasets WHERE id=:id",
                id=dataset_id,
            )
            == 5
        )
        assert (
            await _count(
                test_engine,
                "SELECT count(*) FROM public.tasks WHERE project_id=:id",
                id=project_id,
            )
            == 2
        )
        async with test_engine.begin() as owner:
            await owner.execute(
                text(
                    "UPDATE public.dataset_items SET width=NULL,height=NULL WHERE dataset_id=:id AND file_type='image'"
                ),
                {"id": dataset_id},
            )
        async with maker() as caller:
            repeated = await import_nuscenes(
                caller,
                nuscenes_root=source,
                scene_names=["scene-0000"],
                dataset_name=name,
                owner_id=actor_id,
                dataset_display_id="DS-OP-" + suffix,
                project_display_id="P-OP-" + suffix,
            )
            await caller.commit()
        assert (
            repeated["dataset_id"] == dataset_id
            and repeated["project_id"] == project_id
        )
        assert repeated["total_items"] == 0 and repeated["scenes"][0]["skipped"] is True
        assert repeated["scenes"][0]["backfilled_camera_dimensions"] == 2
        assert (
            await _count(
                test_engine,
                "SELECT count(*) FROM public.dataset_items WHERE dataset_id=:id",
                id=dataset_id,
            )
            == 5
        )
        assert (
            await _count(
                test_engine,
                "SELECT count(*) FROM public.tasks WHERE project_id=:id",
                id=project_id,
            )
            == 2
        )

        failure_suffix = uuid4().hex[:10]
        failed_display = "DS-OP-" + failure_suffix
        failed_project_display = "P-OP-" + failure_suffix
        receipts_before = await _count(
            test_engine, "SELECT count(*) FROM public.dataset_operator_operations"
        )

        async def reject_provenance(*_args, **_kwargs):
            raise RuntimeError("owned operator final provenance failure")

        with monkeypatch.context() as failing:
            failing.setattr(
                DatasetService, "record_nuscenes_calibration_digests", reject_provenance
            )
            async with maker() as caller:
                with pytest.raises(
                    RuntimeError, match="owned operator final provenance failure"
                ):
                    await import_nuscenes(
                        caller,
                        nuscenes_root=source,
                        scene_names=["scene-0000"],
                        dataset_name="operator-nuscenes-failed-" + failure_suffix,
                        owner_id=actor_id,
                        dataset_display_id=failed_display,
                        project_display_id=failed_project_display,
                    )
                failed_dataset = (
                    await caller.execute(
                        text(
                            "SELECT id,metadata->'source'->>'storage_root' AS root FROM public.datasets WHERE display_id=:display"
                        ),
                        {"display": failed_display},
                    )
                ).one()
                assert (
                    await _count(
                        test_engine,
                        "SELECT count(*) FROM public.datasets WHERE id=:id",
                        id=failed_dataset.id,
                    )
                    == 0
                )
                assert (
                    await caller.scalar(
                        text(
                            "SELECT count(*) FROM public.tasks WHERE project_id IN (SELECT id FROM public.projects WHERE display_id=:display)"
                        ),
                        {"display": failed_project_display},
                    )
                ) == 2
                await caller.rollback()
        assert (
            await _count(
                test_engine,
                "SELECT count(*) FROM public.datasets WHERE display_id=:display",
                display=failed_display,
            )
            == 0
        )
        assert (
            await _count(
                test_engine,
                "SELECT count(*) FROM public.projects WHERE display_id=:display",
                display=failed_project_display,
            )
            == 0
        )
        assert (
            await _count(
                test_engine, "SELECT count(*) FROM public.dataset_operator_operations"
            )
            == receipts_before
        )
        retained = storage.client.list_objects_v2(
            Bucket=storage.datasets_bucket,
            Prefix=f"__aap_managed_datasets__/{failed_dataset.id}/",
        )
        assert retained["KeyCount"] > 0
        _evidence(
            "nuscenes",
            {
                "status": "passed",
                "real_s3": True,
                "items": 5,
                "tasks": 2,
                "reimport_no_duplicates": True,
                "missing_camera_dimensions_backfilled": 2,
                "no_private_dml": True,
                "outer_cross_owner_rollback": True,
                "failed_import_objects_retained": retained["KeyCount"],
            },
        )
    finally:
        await engine.dispose()


async def test_restricted_operator_pages_large_sources_and_rolls_back_oversized_patch(
    test_engine,
):
    from aap_assets_source.operator_contract import (
        OPERATOR_PAGE_BYTES,
        OPERATOR_REPLY_BYTES,
        CreateOperatorImageDatasetRequest,
        OperatorFailure,
        OperatorSourceEntry,
        OperatorSourcePatch,
    )
    from app.services.assets_operator import AssetsOperatorGateway, _json
    from uuid import UUID
    import time

    _resources(test_engine)
    operator_url = make_url(_required_env("AAP_MODULE_DATA_ASSET_OPERATOR_URL"))
    engine = create_async_engine(operator_url, hide_parameters=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with async_sessionmaker(test_engine, expire_on_commit=False).begin() as owner:
        actor_id = await _seed_actor(owner, role="viewer", active=False)
    operation_id = uuid4()
    first_id = uuid4().int & ~0xFFFF
    metadata_value = "元" * 300_000
    entries = [
        OperatorSourceEntry(
            id=UUID(int=first_id + index),
            file_name=f"item-{index}.jpg",
            file_path=f"operator-budget/{operation_id}/{index}.jpg",
            file_type="image",
            metadata={"value": metadata_value} if index < 22 else {},
        )
        for index in range(523)
    ]
    try:
        started = time.monotonic()
        async with maker() as caller:
            gateway = AssetsOperatorGateway(caller)
            created = await gateway.create_operator_image_dataset(
                CreateOperatorImageDatasetRequest(
                    operation_id=operation_id,
                    created_by=actor_id,
                    name="operator-budget-" + operation_id.hex,
                    description="restricted operator attribution remains independent of actor state",
                )
            )
            assert created.dataset.created_by == actor_id
            saved = await gateway.append_all_items(operation_id, reversed(entries))
            assert [item.id for item in saved] == [
                item.id for item in reversed(entries)
            ]
            await caller.commit()
        pages = []
        async with maker() as caller:
            gateway = AssetsOperatorGateway(caller)
            read_page = gateway.read_operator_items

            async def capture(request):
                page = await read_page(request)
                pages.append(len(_json(page.items).encode("utf-8")))
                return page

            gateway.read_operator_items = capture
            observed = await gateway.read_all_items(operation_id)
            assert [item.id for item in observed] == [item.id for item in entries]
            assert all(
                item.metadata == entry.metadata
                for item, entry in zip(observed, entries, strict=True)
            )
        record_bytes = sum(pages)
        assert record_bytes > OPERATOR_REPLY_BYTES
        assert len(pages) >= 4 and max(pages) <= OPERATOR_PAGE_BYTES

        # Legacy rows can exceed the new write budget; reject a late oversized
        # reply without committing the preceding source observations.
        oversized_id = uuid4()
        async with async_sessionmaker(
            test_engine, expire_on_commit=False
        ).begin() as owner:
            await owner.execute(
                text(
                    "INSERT INTO public.dataset_items "
                    "(id,dataset_id,file_name,file_path,file_type,file_size,metadata) "
                    "VALUES (:id,:dataset_id,'legacy.jpg',:path,'image',1, "
                    "jsonb_build_object('legacy',repeat('x',:size)))"
                ),
                {
                    "id": oversized_id,
                    "dataset_id": operation_id,
                    "path": f"operator-budget/{operation_id}/legacy.jpg",
                    "size": OPERATOR_REPLY_BYTES,
                },
            )
            snapshot = await owner.scalar(
                text(
                    "SELECT aap_assets_v1._operator_source_snapshot(item) "
                    "FROM public.dataset_items AS item WHERE item.id=:id"
                ),
                {"id": oversized_id},
            )
        receipts_before = await _count(
            test_engine, "SELECT count(*) FROM public.dataset_operator_operations"
        )
        async with maker() as caller:
            with pytest.raises(OperatorFailure) as failed:
                await AssetsOperatorGateway(caller).patch_all_items(
                    operation_id,
                    [
                        OperatorSourcePatch(
                            id=oversized_id, source_snapshot=snapshot, file_size=99
                        )
                    ],
                )
            assert failed.value.code == "resource_budget_exceeded"
            await caller.commit()
        assert (
            await _count(
                test_engine,
                "SELECT file_size FROM public.dataset_items WHERE id=:id",
                id=oversized_id,
            )
            == 1
        )
        assert (
            await _count(
                test_engine, "SELECT count(*) FROM public.dataset_operator_operations"
            )
            == receipts_before
        )
        _evidence(
            "restricted_budget",
            {
                "status": "passed",
                "operator_login": "asset_operator_client",
                "inactive_nonmanager_attribution": True,
                "items": len(entries),
                "record_bytes": record_bytes,
                "pages": len(pages),
                "largest_page_record_bytes": max(pages),
                "seconds": round(time.monotonic() - started, 3),
                "late_reply_budget_rollback": True,
            },
        )
    finally:
        await engine.dispose()
