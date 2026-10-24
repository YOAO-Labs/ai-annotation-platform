"""Frozen serializers must not re-read rows while an archive is being built."""

from copy import deepcopy
import json
import uuid

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.content_snapshot import ContentError
from app.services.exporting import lidar_inputs as inputs
from app.services.exporting.lidar_frozen import FrozenLidar, detached
from tests.test_lidar_export_inputs import input_db as input_db, capture, setup


class NoDatabaseReads:
    def __getattr__(self, name):
        pytest.fail("a frozen serializer tried to access live database: " + name)


async def test_aba_edits_cannot_affect_frozen_geometry_or_project(input_db):
    db = input_db
    project, _, _, _, _, _, _, annotation = await setup(db)
    frozen = await capture(db, project, ("pointmask", "aap_json"))
    view = FrozenLidar(frozen.snapshot)
    original_geometry = deepcopy(annotation.geometry)
    annotation.geometry = {**annotation.geometry, "point_indices": [1]}
    project.name = "temporary edit"
    await db.commit()
    document = json.loads(
        await view.service(NoDatabaseReads()).export_aap_json(project.id)
    )
    assert document["project"]["name"] == "LiDAR"
    assert document["tasks"][0]["annotations"][0]["geometry"] == original_geometry
    annotation.geometry = original_geometry
    project.name = "LiDAR"
    await db.commit()
    assert (await inputs.check(db, frozen.snapshot)).digest == frozen.digest


async def test_primary_link_axis_and_chunks_come_from_frozen_rows(input_db):
    db = input_db
    project, dataset, item, _, _, task, _, _ = await setup(db, direct=False)
    frozen = await capture(db, project)
    view = FrozenLidar(frozen.snapshot)
    dataset.metadata_ = {"axis_convention": "different"}
    task.file_name = "renamed"
    await db.commit()
    assert view.axis_convention(task.id) == "iso8855"
    assert view.links_by_task[task.id]["primary_lidar"][1].id == item.id
    chunks = [
        chunk
        async for chunk in view.service(NoDatabaseReads()).iter_export_chunks(
            project.id
        )
    ]
    assert len(chunks) == 1
    assert chunks[0][0][0].file_name == "original.bin"
    assert chunks[0][0][0].dataset_item_id is None
    assert chunks[0][1][task.id][0].geometry["point_indices"] == [0, 2]


async def test_materialized_view_has_no_mutable_json_aliases(input_db):
    db = input_db
    project, *_ = await setup(db)
    snapshot = (await capture(db, project)).snapshot
    view = FrozenLidar(snapshot)
    snapshot["project"]["tool_bindings"].clear()
    snapshot["annotations"][0]["geometry"]["point_indices"].clear()
    assert view.project.tool_bindings
    assert view.annotations[0].geometry["point_indices"] == [0, 2]


async def test_frozen_service_rejects_scope_substitution(input_db):
    db = input_db
    project, *_ = await setup(db)
    view = FrozenLidar((await capture(db, project)).snapshot)
    service = view.service(NoDatabaseReads())
    with pytest.raises(ContentError, match="lidar_export_inputs_changed"):
        await service.export_aap_json(uuid.uuid4())
    with pytest.raises(ContentError, match="lidar_export_inputs_changed"):
        await service.export_aap_json(project.id, batch_id=uuid.uuid4())
    with pytest.raises(ContentError, match="lidar_export_inputs_changed"):
        _ = [
            chunk
            async for chunk in service.iter_export_chunks(project.id, chunk_size=0)
        ]


async def test_guarded_hydration_rechecks_budget_in_its_own_statement(
    input_db, monkeypatch
):
    db = input_db
    project, *_ = await setup(db)
    measured = inputs._Budget.measure
    changed = False
    monkeypatch.setattr(inputs, "MAX_JSON_BYTES", 50_000)

    async def change_after_measure(self, session, model, predicate, max_rows):
        nonlocal changed
        result = await measured(self, session, model, predicate, max_rows)
        if model is Project and not changed:
            changed = True
            async with async_sessionmaker(db.bind, expire_on_commit=False)() as writer:
                await writer.execute(
                    update(Project)
                    .where(Project.id == project.id)
                    .values(rendering_config={"large": "x" * 100_000})
                )
                await writer.commit()
        return result

    monkeypatch.setattr(inputs._Budget, "measure", change_after_measure)
    with pytest.raises(ContentError, match="lidar_export_input_budget_exceeded"):
        await capture(db, project)
    # The rejected row was not hydrated into the existing ORM instance.
    assert changed and project.rendering_config == {}


async def test_jsonb_numeric_normalization_keeps_persisted_digest_verifiable(input_db):
    db = input_db
    project, _, _, _, _, _, _, annotation = await setup(db)
    annotation.confidence = -0.0
    await db.commit()
    frozen = await capture(db, project)
    from sqlalchemy import bindparam, cast, select
    from sqlalchemy.dialects.postgresql import JSONB

    persisted = await db.scalar(
        select(cast(bindparam("snapshot", frozen.snapshot, type_=JSONB), JSONB))
    )
    assert inputs.digest(persisted) == frozen.digest
    assert (await inputs.check(db, persisted)).digest == frozen.digest


def test_detached_typing_rejects_unknown_columns():
    row = detached(
        Task,
        {
            "id": str(uuid.UUID(int=1)),
            "created_at": "2026-10-24T11:00:00+00:00",
            "tags": ["x"],
        },
    )
    assert (
        row.id == uuid.UUID(int=1) and row.created_at.utcoffset().total_seconds() == 0
    )
    with pytest.raises(ContentError, match="lidar_export_contract_missing"):
        detached(Annotation, {"unknown": 1})


@pytest.mark.parametrize("targets", [("pointmask", "aap_json"), ("coco-multicamera",)])
async def test_real_frozen_service_builds_archive_without_business_rereads(
    input_db, targets
):
    db = input_db
    project, _, item, _, _, task, _, annotation = await setup(db, direct=False)
    frozen = await capture(db, project, targets)
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    import struct
    import zipfile
    from app.services.exporting.packaging import build_export_zip

    class Delivery(FrozenLidar):
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)

        def entry(self, task_id, role):
            binding = next(
                row
                for row in frozen.snapshot["media"]
                if row["task_id"] == str(task_id) and row["role"] == role
            )
            return {
                "presigned_url": "https://saved-export.test/owned-copy",
                "expires_at": self.expires_at.isoformat(),
                "sha256": binding["source"]["sha256"],
                "size_bytes": binding["source"]["size_bytes"],
            }

        def point_count(self, task_id):
            assert "pointmask" in targets
            return 3

    view = Delivery(frozen.snapshot)
    annotation.geometry = {**annotation.geometry, "point_indices": [1]}
    item.metadata_ = {"point_count": 999}
    await db.commit()
    path = None
    try:
        path, count, size = await build_export_zip(
            NoDatabaseReads(),
            project.id,
            batch_id=None,
            targets=list(targets),
            include_attributes=True,
            video_frame_mode="keyframes",
            format_options=frozen.snapshot["options"],
            lidar_media=view,
        )
        assert count >= 1 and Path(path).stat().st_size == size
        with zipfile.ZipFile(path) as archive:
            if "pointmask" in targets:
                labels = [
                    name for name in archive.namelist() if name.endswith(".label")
                ]
                assert len(labels) == 1 and struct.unpack(
                    "<3I", archive.read(labels[0])
                ) == (1, 0, 1)
                manifest_name = "pointmask/pointclouds_manifest.json"
                manifest = json.loads(archive.read(manifest_name))
                assert manifest["expires_at"] == view.expires_at.isoformat()
                assert (
                    manifest["pointclouds"][0]["sha256"]
                    == frozen.sources[str(task.id) + ":primary_lidar"].sha256
                )
                assert "fetch_images.py" in " ".join(archive.namelist())
            else:
                manifest = json.loads(archive.read("media_manifest.json"))
                assert len(manifest["media"]) == 2
                assert all(
                    row["presigned_url"] == "https://saved-export.test/owned-copy"
                    for row in manifest["media"]
                )
    finally:
        if path is not None:
            Path(path).unlink()
