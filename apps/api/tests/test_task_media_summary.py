import uuid

import pytest
from sqlalchemy import delete, update

from app.api.v1.tasks._shared import (
    _attach_dimensions,
    _attach_dimensions_batch,
)
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.assets_media_summary import SqlAlchemyTaskMediaSummaryReader


async def _create_item(db_session, user, *, file_type="video", metadata=None):
    suffix = uuid.uuid4().hex[:10]
    dataset = Dataset(
        display_id=f"D-MEDIA-{suffix}",
        name="media summary test",
        data_type=file_type,
        created_by=user.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        dataset_id=dataset.id,
        file_name=f"{suffix}.mp4",
        file_path=f"media/{suffix}.mp4",
        file_type=file_type,
        metadata_=metadata or {},
    )
    db_session.add(item)
    await db_session.flush()
    return dataset, item


async def test_media_summary_detaches_nested_video_json(db_session, super_admin):
    user, _ = super_admin
    _dataset, item = await _create_item(
        db_session,
        user,
        metadata={
            "video": {
                "frame_count": 7,
                "extra": {"markers": [{"name": "source"}]},
            }
        },
    )
    item.width = 640
    item.height = 360
    item.thumbnail_path = "thumbnails/source.webp"
    item.blurhash = "source-blurhash"
    await db_session.flush()
    reader = SqlAlchemyTaskMediaSummaryReader(db_session)

    single = await reader.one(item.id)
    batch = await reader.many((item.id,))

    assert single is not None
    assert single == (
        640,
        360,
        "thumbnails/source.webp",
        "source-blurhash",
        item.metadata_["video"],
    )
    assert batch[item.id] == single
    assert single[4] == batch[item.id][4]
    assert single[4] is not item.metadata_["video"]
    single[4]["extra"]["markers"][0]["name"] = "response mutation"
    assert item.metadata_["video"]["extra"]["markers"][0]["name"] == "source"
    assert batch[item.id][4]["extra"]["markers"][0]["name"] == "source"

    _other_dataset, unknown_only_item = await _create_item(
        db_session, user, metadata={"video": {"future_field": {"value": 1}}}
    )
    unknown_only = await reader.one(unknown_only_item.id)
    assert unknown_only is not None
    assert unknown_only[4] == {"future_field": {"value": 1}}


async def test_media_summary_keeps_raw_video_conversion_and_frame_count_rules(
    db_session, super_admin
):
    user, _ = super_admin
    _dataset, invalid_video = await _create_item(
        db_session, user, metadata={"video": [1]}
    )
    reader = SqlAlchemyTaskMediaSummaryReader(db_session)

    with pytest.raises(TypeError):
        await reader.one(invalid_video.id)
    with pytest.raises(TypeError):
        await reader.many((invalid_video.id,))

    _other_dataset, image_item = await _create_item(
        db_session,
        user,
        file_type="image",
        metadata={"video": {"frame_count": "9"}},
    )
    assert await reader.video_frame_count(image_item.id) == 9

    image_item.metadata_ = {"video": {"frame_count": "unknown"}}
    await db_session.flush()
    assert await reader.video_frame_count(image_item.id) is None


async def test_missing_source_keeps_single_and_batch_task_fallbacks_distinct(
    db_session, super_admin
):
    user, _ = super_admin
    dataset, item = await _create_item(db_session, user, file_type="image")
    project = Project(
        display_id=f"P-MEDIA-{uuid.uuid4().hex[:10]}",
        name="media summary task project",
        type_key="image-det",
        type_label="Image",
        owner_id=user.id,
    )
    db_session.add(project)
    await db_session.flush()
    task = Task(
        project_id=project.id,
        dataset_item_id=item.id,
        display_id=f"T-MEDIA-{uuid.uuid4().hex[:10]}",
        file_name=item.file_name,
        file_path=item.file_path,
        file_type="image",
        status="pending",
        thumbnail_path="tasks/fallback.webp",
        blurhash="task-blurhash",
    )
    db_session.add(task)
    await db_session.flush()

    source_id = item.id
    await db_session.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(dataset_item_id=None)
        .execution_options(synchronize_session=False)
    )
    await db_session.execute(
        delete(DatasetItem)
        .where(DatasetItem.id == source_id)
        .execution_options(synchronize_session=False)
    )
    if item in db_session:
        db_session.expunge(item)

    # This in-memory Task is intentionally stale, like a request's loaded row
    # after another write detached and removed its DatasetItem.
    assert task.dataset_item_id == source_id
    assert await db_session.get(DatasetItem, source_id) is None
    single = await _attach_dimensions(db_session, task)
    batch = await _attach_dimensions_batch(db_session, [task])
    assert single == (None, None, task.thumbnail_path, task.blurhash, None)
    assert batch[task.id] == (None, None, None, None, None)

    source_less_task = Task(
        id=uuid.uuid4(),
        thumbnail_path="tasks/source-less.webp",
        blurhash="source-less-blurhash",
    )
    assert await _attach_dimensions(db_session, source_less_task) == (
        None,
        None,
        source_less_task.thumbnail_path,
        source_less_task.blurhash,
        None,
    )
    assert (await _attach_dimensions_batch(db_session, [source_less_task]))[
        source_less_task.id
    ] == (
        None,
        None,
        source_less_task.thumbnail_path,
        source_less_task.blurhash,
        None,
    )
