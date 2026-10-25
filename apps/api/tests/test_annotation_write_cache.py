from datetime import datetime, timezone
import uuid

import pytest

from app.db.models.annotation import Annotation
from app.db.models.scene_track import SceneTrack
from app.services.annotation import AnnotationService


class _NoAutoflush:
    def __init__(self, database):
        self.database = database

    def __enter__(self):
        self.database.no_autoflush_count += 1

    def __exit__(self, *_args):
        return False


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self.rows)

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class _Session:
    def __init__(self, items):
        self.identity_map = {index: item for index, item in enumerate(items)}


class _Database:
    def __init__(self, items):
        self.sync_session = _Session(items)
        self.annotation_rows = []
        self.scene_track_rows = []
        self.no_autoflush_count = 0

    @property
    def no_autoflush(self):
        return _NoAutoflush(self)

    async def execute(self, statement):
        columns = list(statement.selected_columns)
        table = columns[0].table.name
        source = (
            self.annotation_rows if table == "annotations" else self.scene_track_rows
        )
        fields = [column.key for column in columns]
        return _Result([{field: row[field] for field in fields} for row in source])


def _annotation(annotation_id, task_id, project_id, updated_at, attributes, **fields):
    values = {
        "source": "manual",
        "annotation_type": "bbox",
        "tool_unit_id": "bbox",
        "class_name": "person",
        "geometry": {"type": "bbox"},
        "temporal_role": "sample",
        "is_active": True,
        "was_cancelled": False,
        "version": 1,
        **fields,
    }
    return Annotation(
        id=annotation_id,
        task_id=task_id,
        project_id=project_id,
        updated_at=updated_at,
        attributes=attributes,
        **values,
    )


@pytest.mark.asyncio
async def test_native_cache_refresh_only_commits_owner_fields_and_skips_autoflush():
    task_id, scene_track_id = uuid.uuid4(), uuid.uuid4()
    annotation_id, camera_id, child_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    other_id = uuid.uuid4()
    before = datetime(2026, 1, 1, tzinfo=timezone.utc)
    after = datetime(2026, 1, 2, tzinfo=timezone.utc)
    project_id = uuid.uuid4()
    main = _annotation(
        annotation_id,
        task_id,
        project_id,
        before,
        {"pending": "main"},
        annotation_type="box_3d",
        tool_unit_id="bbox3d",
        geometry={"type": "box_3d"},
        track_id="trk_1",
        scene_track_id=scene_track_id,
        temporal_role="keyframe",
    )
    camera = _annotation(
        camera_id,
        task_id,
        project_id,
        before,
        {"pending": "camera"},
        track_id="trk_1",
        scene_track_id=scene_track_id,
        sensor_role="camera_left",
        version=3,
    )
    child = _annotation(
        child_id,
        task_id,
        project_id,
        before,
        {"pending": "child"},
        parent_annotation_id=annotation_id,
    )
    other = _annotation(other_id, task_id, project_id, before, {"pending": "other"})
    track = SceneTrack(
        id=scene_track_id,
        project_id=main.project_id,
        scene_id=uuid.uuid4(),
        track_id="trk_1",
        class_name="person",
        revision=4,
        updated_at=before,
        attributes={"pending": "track"},
    )
    database = _Database([main, camera, child, other, track])
    service = AnnotationService(database)
    database.annotation_rows = [
        {"id": annotation_id, "is_active": False, "version": 2, "updated_at": after},
        {"id": camera_id, "is_active": False, "version": 4, "updated_at": after},
        {"id": child_id, "is_active": False, "version": 2, "updated_at": after},
    ]

    await service._refresh_deleted_annotation_cache(
        main, deleted_camera_members=1, deleted_children=1
    )
    database.scene_track_rows = [
        {
            "id": scene_track_id,
            "class_name": "vehicle",
            "revision": 5,
            "updated_at": after,
        }
    ]
    await service._refresh_loaded_scene_track_fields(
        scene_track_id, ("class_name", "revision", "updated_at")
    )
    database.annotation_rows = [
        {
            "id": camera_id,
            "class_name": "vehicle",
            "version": 5,
            "updated_at": after,
        }
    ]
    await service._refresh_loaded_scene_track_cameras(scene_track_id)

    assert (main.is_active, main.version, main.updated_at) == (False, 2, after)
    assert (child.is_active, child.version, child.updated_at) == (False, 2, after)
    assert (camera.class_name, camera.version, camera.updated_at) == (
        "vehicle",
        5,
        after,
    )
    assert (track.class_name, track.revision, track.updated_at) == ("vehicle", 5, after)
    assert other.is_active is True and other.version == 1
    assert [item.attributes for item in (main, camera, child, other, track)] == [
        {"pending": "main"},
        {"pending": "camera"},
        {"pending": "child"},
        {"pending": "other"},
        {"pending": "track"},
    ]
    assert database.no_autoflush_count == 3
