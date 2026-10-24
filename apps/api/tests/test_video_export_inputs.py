"""Video metadata-only freezing; DB cases require the root disposable launcher."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.dataset import Dataset, DatasetItem, VideoSegment
from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.video_track_quality import VideoTrackQualityRun
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.content_snapshot import ContentError
from app.services.exporting import video_inputs as inputs
from app.services.exporting.video_frozen import FrozenVideo
from app.services.mask_formats.contracts import canonical_digest
from app.services.video_track_quality import _snapshot
from app.services.video_canonical import VideoBoundaryUnreconciledError
from tests.factory import build_tool_bindings
from tests.test_lidar_export_inputs import input_db as input_db
from tests.test_original_sources import add_link


@pytest.fixture
async def video_db(input_db, test_engine):
    """Extend the already-owned random schema; never select a database URL."""
    db = input_db
    schema = await db.scalar(text("SELECT current_schema()"))
    assert (
        schema.startswith("lidar_input_test_")
        and schema.removeprefix("lidar_input_test_").isalnum()
    )
    async with test_engine.connect() as connection:
        source = await connection.scalar(text("SELECT current_schema()"))
    quoted_source = '"' + source.replace('"', '""') + '"'
    for table in ("video_segments", "video_track_quality_runs"):
        await db.execute(
            text(
                f'CREATE TABLE "{schema}"."{table}" (LIKE {quoted_source}."{table}" INCLUDING ALL)'
            )
        )
    for child, column, parent in (
        ("tasks", "project_id", "projects"),
        ("tasks", "dataset_item_id", "dataset_items"),
        ("dataset_items", "dataset_id", "datasets"),
        ("video_segments", "dataset_item_id", "dataset_items"),
        ("video_track_quality_runs", "task_id", "tasks"),
        ("video_track_quality_runs", "left_segment_id", "video_segments"),
        ("video_track_quality_runs", "right_segment_id", "video_segments"),
        ("predictions", "task_id", "tasks"),
    ):
        await db.execute(
            text(
                f'ALTER TABLE "{schema}"."{child}" ADD FOREIGN KEY ("{column}") REFERENCES "{schema}"."{parent}" (id)'
            )
        )
    await db.commit()
    yield db


async def setup(db, *, collaboration=False, primary=False):
    """Return (project,item,asset,replica,task,annotation), with real SQL owners."""
    project = Project(
        id=uuid.uuid4(),
        display_id="video-" + uuid.uuid4().hex[:14],
        name="video",
        type_key="video-track",
        type_label="Video",
        data_type="video",
        owner_id=uuid.uuid4(),
        video_sampling={"mode": "step", "frame_step": 3},
        video_collaboration={
            "enabled": collaboration,
            "overlap_frames": 2 if collaboration else 0,
        },
        tool_bindings=build_tool_bindings(["car"], unit="bbox"),
    )
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id="DV-" + uuid.uuid4().hex[:14],
        name="video dataset",
        data_type="video",
        created_by=project.owner_id,
    )
    db.add_all([project, dataset])
    await db.flush()
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="clip.mp4",
        file_path="saved/" + uuid.uuid4().hex,
        file_type="video",
        file_size=5,
        content_hash="old-md5",
        width=24,
        height=16,
        metadata_={
            "video": {"fps": 999, "frame_count": 999999},
            "storage_relative_path": "clip.mp4",
        },
    )
    db.add(item)
    await db.flush()
    asset = DatasetAsset(
        id=uuid.uuid4(),
        dataset_item_id=item.id,
        media_type="video",
        size_bytes=5,
        checksum_algorithm="sha256",
        checksum_value=hashlib.sha256(b"saved").hexdigest(),
        verification_state="verified",
    )
    db.add(asset)
    await db.flush()
    replica = DatasetAssetLocation(
        asset_id=asset.id,
        location_id=LEGACY_LOCATION_ID,
        bucket="saved-video-originals",
        object_key=item.file_path,
        role="managed_original",
        reference_state="present",
        etag="historical-video-etag",
        verified_at=datetime.now(timezone.utc),
    )
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        dataset_item_id=None if primary else item.id,
        display_id="TV-" + uuid.uuid4().hex[:14],
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="video",
    )
    db.add_all([replica, task])
    await db.flush()
    if primary:
        await add_link(db, task.id, item.id)
    annotation = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        class_name="car",
        annotation_type="video_track_bbox",
        tool_unit_id="bbox",
        track_id="trk_video_a",
        source="manual",
        version=1,
        geometry={
            "type": "video_track_bbox",
            "track_id": "trk_video_a",
            "keyframes": [
                {"frame_index": 0, "bbox": {"x": 0, "y": 0, "w": 0.2, "h": 0.2}},
                {"frame_index": 9, "bbox": {"x": 0.5, "y": 0, "w": 0.2, "h": 0.2}},
            ],
            "outside": [],
        },
    )
    db.add(annotation)
    if collaboration:
        segments = [
            VideoSegment(
                id=uuid.uuid4(),
                dataset_item_id=item.id,
                segment_index=index,
                start_frame=index * 5,
                end_frame=index * 5 + 4,
                status="completed",
            )
            for index in range(2)
        ]
        db.add_all(segments)
        await db.flush()
        annotation.video_segment_id = segments[0].id
        annotation.geometry = deepcopy(annotation.geometry)
        annotation.geometry["keyframes"][1]["frame_index"] = 4
        other = Annotation(
            id=uuid.uuid4(),
            task_id=task.id,
            project_id=project.id,
            video_segment_id=segments[1].id,
            annotation_type="video_track_bbox",
            tool_unit_id="bbox",
            class_name="car",
            source="manual",
            version=1,
            track_id="trk_video_b",
            geometry={
                "type": "video_track_bbox",
                "track_id": "trk_video_b",
                "keyframes": [
                    {"frame_index": 5, "bbox": {"x": 0.5, "y": 0, "w": 0.2, "h": 0.2}},
                    {"frame_index": 9, "bbox": {"x": 0.8, "y": 0, "w": 0.2, "h": 0.2}},
                ],
                "outside": [],
            },
        )
        db.add(other)
        await db.flush()
        frames = [3, 6]
        raw = sorted(
            [annotation, other], key=lambda row: (row.video_segment_id, row.id)
        )
        run = VideoTrackQualityRun(
            id=uuid.uuid4(),
            task_id=task.id,
            left_segment_id=segments[0].id,
            right_segment_id=segments[1].id,
            status="accepted",
            input_snapshot=_snapshot(raw),
            sampling_digest=canonical_digest(frames),
            input_digest=canonical_digest(
                {
                    "annotations": _snapshot(raw),
                    "sampling_digest": canonical_digest(frames),
                }
            ),
            metrics={"sampling_frames": frames},
            pairs=[
                {
                    "left_annotation_id": str(annotation.id),
                    "right_annotation_id": str(other.id),
                    "decision": "same_track",
                }
            ],
        )
        db.add(run)
    await db.commit()
    return project, item, asset, replica, task, annotation


def test_snapshot_and_sources_are_independent_copies():
    data = {"schema": 4, "probe_contract": 1, "annotations": []}
    frozen = inputs.Inputs(data, {"task:video": "canonical"})
    expected = frozen.digest
    data["annotations"].append("changed")
    frozen.snapshot["annotations"].append("changed again")
    assert frozen.digest == expected
    with pytest.raises(TypeError):
        frozen.sources["other"] = "forbidden"


@pytest.mark.parametrize("field", ["keyframes", "outside"])
def test_structure_rejects_before_clipping_or_mask_io(monkeypatch, field):
    monkeypatch.setattr(inputs, "MAX_TRACK_KEYFRAMES", 1)
    monkeypatch.setattr(inputs, "MAX_TRACK_OUTSIDE", 1)
    row = SimpleNamespace(geometry={field: [{}, {}]})
    with pytest.raises(ContentError, match="input_budget_exceeded"):
        inputs.geometry_budget([row])


def test_mask_budget_deduplicates_and_rejects_declared_run_overflow(monkeypatch):
    reference = {
        "object_key": "raster-masks/sha256/" + "a" * 64 + ".json",
        "bytes": 30,
        "runs": 3,
        "size": [3, 4],
    }
    row = SimpleNamespace(
        geometry={
            "type": "video_track_mask",
            "keyframes": [{"mask": reference}, {"mask": reference}],
        }
    )
    assert inputs._mask_budget([row], []) == 30
    monkeypatch.setattr(inputs, "MAX_MASK_RUNS", 2)
    with pytest.raises(ContentError, match="input_budget_exceeded"):
        inputs._mask_budget([row], [])


@pytest.mark.parametrize("primary", [False, True])
async def test_database_canonical_owner_and_raw_metadata_only_freeze(
    video_db, monkeypatch, primary
):
    db = video_db
    project, item, _, replica, task, annotation = await setup(db, primary=primary)
    from app.services import managed_original_reader

    capture = AsyncMock(side_effect=AssertionError("HTTP must not read bytes"))
    monkeypatch.setattr(managed_original_reader, "capture", capture)
    frozen = await inputs.collect(
        db, project.id, None, ["video_json", "aap_json"], {}, lock=True
    )
    data = frozen.snapshot
    assert (
        data["schema"] == 4 and data["probe_contract"] == 1 and data["references"] == {}
    )
    assert data["media"][0]["source"]["bucket"] == replica.bucket
    assert data["media"][0]["source"]["item_id"] == str(item.id)
    assert "video" not in data["items"][0]["metadata_"]
    assert data["annotations"][0]["id"] == str(annotation.id)
    assert (await inputs.check(db, data)).digest == frozen.digest
    assert (await inputs.check_source(db, data["media"][0])).sha256 == data["media"][0][
        "source"
    ]["sha256"]
    capture.assert_not_awaited()


async def test_database_empty_project_has_zero_media(video_db):
    db = video_db
    project, _, _, _, task, annotation = await setup(db)
    await db.delete(annotation)
    await db.delete(task)
    await db.commit()
    frozen = await inputs.collect(db, project.id, None, ["video_json"], {}, lock=True)
    assert frozen.snapshot["tasks"] == frozen.snapshot["media"] == []
    assert FrozenVideo(frozen.snapshot).metadata_by_task == {}


@pytest.mark.parametrize("mutation", ["annotation", "segment", "pair", "sampling"])
async def test_database_final_check_rejects_temporal_drift(video_db, mutation):
    db = video_db
    project, _, _, _, _, annotation = await setup(db, collaboration=True)
    frozen = await inputs.collect(db, project.id, None, ["video_json"], {})
    if mutation == "annotation":
        annotation.version += 1
    elif mutation == "segment":
        segment = await db.get(VideoSegment, annotation.video_segment_id)
        segment.end_frame -= 1
    elif mutation == "pair":
        run = await db.scalar(select(VideoTrackQualityRun))
        run.pairs = []
    else:
        project.video_sampling = {"mode": "step", "frame_step": 2}
    await db.commit()
    with pytest.raises((ContentError, VideoBoundaryUnreconciledError)):
        await inputs.check(db, frozen.snapshot, lock=True)


async def test_database_final_parent_fences_segment_and_quality_insert(video_db):
    db = video_db
    project, item, _, _, task, _ = await setup(db, collaboration=True)
    project_id, item_id, task_id = project.id, item.id, task.id
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as other:
        await inputs.collect(db, project_id, None, ["video_json"], {}, lock=True)
        await other.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(DBAPIError) as caught:
            await other.execute(
                text(
                    "INSERT INTO video_segments(id,dataset_item_id,segment_index,start_frame,end_frame,status) VALUES (:id,:item,2,10,14,'open')"
                ),
                {"id": uuid.uuid4(), "item": item_id},
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        await other.rollback()
        await other.execute(text("SET LOCAL lock_timeout = '100ms'"))
        segments = list(
            await db.scalars(select(VideoSegment).order_by(VideoSegment.segment_index))
        )
        with pytest.raises(DBAPIError) as caught:
            await other.execute(
                text(
                    "INSERT INTO video_track_quality_runs(id,task_id,left_segment_id,right_segment_id,input_snapshot,input_digest,sampling_digest,status,metrics,pairs) VALUES (:id,:task,:left,:right,'[]',:sha,:sha,'pending','{}','[]')"
                ),
                {
                    "id": uuid.uuid4(),
                    "task": task_id,
                    "left": segments[0].id,
                    "right": segments[1].id,
                    "sha": "a" * 64,
                },
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        await other.rollback()
    await db.rollback()


async def test_database_restore_is_locked_and_raw_filtered_class_affects_freshness(
    video_db,
):
    db = video_db
    project, _, _, _, task, annotation = await setup(db, collaboration=True)
    removed = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        video_segment_id=annotation.video_segment_id,
        class_name="removed",
        annotation_type="video_track_bbox",
        tool_unit_id="bbox",
        track_id="trk_removed",
        geometry=deepcopy(annotation.geometry),
        version=1,
        is_active=False,
        was_cancelled=False,
    )
    db.add(removed)
    await db.commit()
    project_id, removed_id = project.id, removed.id
    await inputs.collect(db, project_id, None, ["video_json"], {}, lock=True)
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as other:
        await other.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(DBAPIError) as caught:
            await other.execute(
                update(Annotation)
                .where(Annotation.id == removed_id)
                .values(is_active=True)
            )
        assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        await other.rollback()
    await db.rollback()
    await db.execute(
        update(Annotation).where(Annotation.id == removed_id).values(is_active=True)
    )
    await db.commit()
    with pytest.raises(VideoBoundaryUnreconciledError):
        await inputs.collect(db, project_id, None, ["video_json"], {})
