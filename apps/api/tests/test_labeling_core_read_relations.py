import uuid
import re
from pathlib import Path

import pytest

from sqlalchemy import func, inspect, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm.util import identity_key

from aap_labeling.core_read_contract import CORE_READ_COLUMNS, CORE_READ_RELATIONS
from app.db.models.task import Task
from app.db.models.project import Project
from app.services.data_management.task_metrics import (
    low_confidence_pending_prediction_shapes_expr,
    pending_prediction_shapes_expr,
    pending_tracker_jobs_expr,
)
from app.services.labeling_core_read_relations import (
    alias_core_row,
    annotation_row,
    batch_row,
    failed_prediction_row,
    get_core_row,
    task_dataset_item_link_row,
    task_lock_row,
    prediction_meta_row,
    prediction_row,
    scene_track_interval_row,
    scene_track_row,
    task_row,
    video_segment_row,
    video_tracker_job_row,
    lock_annotation_rows_for_core_read,
    lock_batch_task_rows_for_core_read,
    lock_batch_rows_for_core_read,
    lock_task_for_core_read,
    lock_task_rows_for_core_read,
    lock_video_segment_for_core_read,
    lock_feedback_task_rows_for_share,
    lock_active_annotation_rows_for_share,
)
from app.services.assets_spatial_views import labeling_dataset_item_facts
from app.services.labeling_discussion_relations import task_event_row

_CORE_READ_ROWS = {
    "task_rows": task_row,
    "batch_rows": batch_row,
    "annotation_rows": annotation_row,
    "prediction_rows": prediction_row,
    "prediction_meta_rows": prediction_meta_row,
    "failed_prediction_rows": failed_prediction_row,
    "task_lock_rows": task_lock_row,
    "task_dataset_item_link_rows": task_dataset_item_link_row,
    "scene_track_rows": scene_track_row,
    "scene_track_interval_rows": scene_track_interval_row,
    "video_segment_rows": video_segment_row,
    "video_tracker_job_rows": video_tracker_job_row,
}


def test_core_rows_bind_only_the_published_relation_columns():
    assert tuple(_CORE_READ_ROWS) == CORE_READ_RELATIONS
    for relation, row in _CORE_READ_ROWS.items():
        selectable = inspect(row).selectable
        assert selectable.schema == "aap_labeling_v1"
        assert tuple(selectable.c.keys()) == CORE_READ_COLUMNS[relation]


def test_named_and_anonymous_core_row_aliases_keep_public_source_and_identity():
    for relation, row in _CORE_READ_ROWS.items():
        for name in ("candidate", None):
            alias = alias_core_row(row, name=name)
            assert inspect(alias).mapper.class_ is inspect(row).mapper.class_
            sql = str(select(alias).compile(dialect=postgresql.dialect()))
            assert f"FROM aap_labeling_v1.{relation} AS " in sql
    with pytest.raises(ValueError, match="published Labeling core-read relation"):
        alias_core_row(Task, name="private_task")


def test_composed_task_annotation_read_stays_on_labeling_relations():
    statement = (
        select(task_row.id, task_row.sequence_order, func.count(annotation_row.id))
        .select_from(task_row)
        .outerjoin(batch_row, batch_row.id == task_row.batch_id)
        .outerjoin(annotation_row, annotation_row.task_id == task_row.id)
        .where(task_row.project_id == uuid.UUID("00000000-0000-0000-0000-000000000001"))
        .group_by(task_row.id)
        .order_by(func.coalesce(task_row.sequence_order, 2147483647), task_row.id)
        .limit(50)
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "aap_labeling_v1.task_rows" in sql
    assert "aap_labeling_v1.batch_rows" in sql
    assert "aap_labeling_v1.annotation_rows" in sql
    assert "FROM public.tasks" not in sql
    assert "FROM public.annotations" not in sql


def test_video_and_scene_rows_use_only_the_fixed_public_relations():
    rows = (
        scene_track_row,
        scene_track_interval_row,
        video_segment_row,
        video_tracker_job_row,
    )
    for row in rows:
        sql = str(select(row).compile(dialect=postgresql.dialect()))
        assert "FROM aap_labeling_v1." in sql
        assert "FROM public." not in sql


def test_active_learning_candidate_query_uses_public_core_and_asset_facts():
    from app.services.labeling_active_learning_sources import (
        SqlAlchemyLabelingActiveLearningSources,
    )

    query = SqlAlchemyLabelingActiveLearningSources(
        None, dataset_bucket="datasets", task_bucket="tasks"
    )._candidate_query(uuid.uuid4())
    sql = str(query.compile(dialect=postgresql.dialect()))

    for relation in (
        "aap_labeling_v1.task_rows",
        "aap_labeling_v1.batch_rows",
        "aap_labeling_v1.annotation_rows",
        "aap_labeling_v1.annotation_draft_rows",
        "aap_labeling_v1.task_lock_rows",
        "aap_assets_v1.labeling_dataset_item_facts",
    ):
        assert relation in sql
    for private_relation in (
        "FROM tasks",
        "FROM task_batches",
        "FROM annotations",
        "FROM annotation_drafts",
        "FROM task_locks",
        "FROM dataset_items",
    ):
        assert private_relation not in sql
    assert tuple(inspect(labeling_dataset_item_facts).selectable.c.keys()) == (
        "id",
        "dataset_id",
        "file_path",
        "file_type",
        "file_size",
        "content_hash",
        "width",
        "height",
    )


def test_task_event_rows_bind_to_the_existing_public_facts_view():
    sql = str(select(task_event_row).compile(dialect=postgresql.dialect()))
    assert "FROM aap_labeling_v1.task_event_facts" in sql
    assert "FROM task_events" not in sql


def test_new_compatibility_views_and_lock_functions_match_packaged_sql():
    api_sql = (
        Path(__file__).resolve().parents[1]
        / "alembic/sql/0361_labeling_compatibility_reads.sql"
    )
    package_sql = (
        Path(__file__).resolve().parents[3]
        / "apps/labeling/src/aap_labeling/sql/0361_labeling_compatibility_reads.sql"
    )
    sql = api_sql.read_text(encoding="utf-8")
    assert sql == package_sql.read_text(encoding="utf-8")
    assert "CREATE VIEW aap_labeling_v1.annotation_draft_rows" in sql
    assert "SELECT id, task_id\nFROM public.annotation_drafts" in sql
    assert "CREATE VIEW aap_assets_v1.labeling_dataset_item_facts" in sql
    assert "LIMIT 4097\n    FOR SHARE" in sql
    assert sql.count("FOR SHARE") == 2
    assert "CREATE FUNCTION aap_labeling_v1.next_batch_display_id()" in sql
    assert "'BT-' || pg_catalog.nextval" in sql
    assert "'public.display_seq_batches'::pg_catalog.regclass" in sql

    for function, alias, table_name, relation in (
        (
            "lock_active_learning_feedback_task_rows",
            "task",
            "tasks",
            "task_rows",
        ),
        (
            "lock_active_learning_feedback_annotation_rows",
            "annotation",
            "annotations",
            "annotation_rows",
        ),
    ):
        function_sql = sql.split(f"CREATE FUNCTION aap_labeling_v1.{function}", 1)[1]
        selected = re.search(
            rf"RETURN QUERY\s+SELECT(.*?)\s+FROM public\.{table_name} AS {alias}",
            function_sql,
            re.DOTALL,
        )
        assert selected is not None
        assert (
            tuple(re.findall(rf"\b{alias}\.(\w+)\b", selected.group(1)))
            == (CORE_READ_COLUMNS[relation])
        )


def test_new_core_read_views_publish_exact_ordered_columns():
    api_sql = (
        Path(__file__).resolve().parents[1] / "alembic/sql/0352_labeling_core_read.sql"
    )
    package_sql = (
        Path(__file__).resolve().parents[3]
        / "apps/labeling/src/aap_labeling/sql/0352_labeling_core_read.sql"
    )
    sql = api_sql.read_text(encoding="utf-8")
    assert sql == package_sql.read_text(encoding="utf-8")

    base_tables = {
        "scene_track_rows": "scene_tracks",
        "scene_track_interval_rows": "scene_track_intervals",
        "video_segment_rows": "video_segments",
        "video_tracker_job_rows": "video_tracker_jobs",
    }
    for relation, base_table in base_tables.items():
        view = re.search(
            rf"CREATE VIEW aap_labeling_v1\.{relation}\s+"
            rf"WITH \(security_barrier = true\)\s+AS\s+SELECT(.*?)"
            rf"\s+FROM public\.{base_table};",
            sql,
            re.DOTALL,
        )
        assert view is not None
        assert (
            tuple(column.strip() for column in view.group(1).split(","))
            == (CORE_READ_COLUMNS[relation])
        )


def test_task_metrics_compose_only_public_labeling_rows():
    statement = select(
        task_row.id,
        pending_prediction_shapes_expr(),
        low_confidence_pending_prediction_shapes_expr(),
        pending_tracker_jobs_expr(None, Project(id=uuid.uuid4())),
    ).select_from(task_row)
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "aap_labeling_v1.task_rows" in sql
    assert "aap_labeling_v1.prediction_rows" in sql
    assert "aap_labeling_v1.annotation_rows" in sql
    assert "aap_labeling_v1.video_tracker_job_rows" in sql
    assert (
        "aap_labeling_v1.prediction_rows.task_id = aap_labeling_v1.task_rows.id" in sql
    )
    assert (
        "aap_labeling_v1.video_tracker_job_rows.task_id = aap_labeling_v1.task_rows.id"
        in sql
    )
    assert "FROM public.tasks" not in sql
    assert "FROM public.predictions" not in sql
    assert "FROM public.video_tracker_jobs" not in sql


def test_data_manager_filters_keep_public_task_and_annotation_rows():
    from app.services.data_management.entities import task_dataset_item_id_expr
    from app.services.data_management.entity_filters import compile_entity_filter
    from app.services.data_management.task_filters import (
        _TASK_FIELD_MAP,
        compile_filter,
    )
    from app.services.data_management.views import _scene_total_frames_sq

    project = Project(
        id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        display_id="P-CORE-READ-FILTER",
        name="Core read filter contract",
        type_label="Image",
        type_key="image-det",
        data_type="image",
        scene_mode=False,
        tool_bindings={"bbox": {"enabled": True, "classes": ["car"]}},
    )
    filter_json = {
        "op": "and",
        "rules": [{"field": "annotation.class_name", "op": "eq", "value": "car"}],
    }
    statement = (
        select(
            task_row.id,
            _TASK_FIELD_MAP["task.assignee"],
            _TASK_FIELD_MAP["task.reviewer"],
            task_dataset_item_id_expr(),
            _scene_total_frames_sq(),
        )
        .select_from(task_row)
        .where(compile_filter(filter_json, project=project))
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "FROM aap_labeling_v1.task_rows" in sql
    assert "FROM aap_labeling_v1.batch_rows" in sql
    assert "FROM aap_labeling_v1.annotation_rows AS" in sql
    assert "FROM aap_labeling_v1.task_dataset_item_link_rows" in sql
    assert "FROM tasks" not in sql
    assert "FROM annotations" not in sql
    assert "FROM task_batches" not in sql

    entity_clause = compile_entity_filter(
        filter_json["rules"][0], annotation_row, project=project
    )
    entity_sql = str(
        select(annotation_row.id)
        .select_from(annotation_row)
        .where(entity_clause)
        .compile(dialect=postgresql.dialect())
    )
    assert "FROM aap_labeling_v1.annotation_rows" in entity_sql
    assert "FROM annotations" not in entity_sql


@pytest.mark.asyncio
async def test_task_event_batch_read_uses_one_public_task_query(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from app.schemas.task_event import TaskEventIn
    from app.services import task_event_ingestion

    task_id = uuid.UUID("00000000-0000-0000-0000-000000000007")
    project_id = uuid.UUID("00000000-0000-0000-0000-000000000008")
    actor_id = uuid.UUID("00000000-0000-0000-0000-000000000009")
    task = Task(
        id=task_id,
        project_id=project_id,
        display_id="T-EVENT-1",
        file_name="frame.jpg",
        file_path="frame.jpg",
    )

    class TaskResult:
        def scalars(self):
            return iter((task,))

    class TaskReadSession:
        def __init__(self):
            self.sql = []

        async def execute(self, statement):
            self.sql.append(str(statement.compile(dialect=postgresql.dialect())))
            return TaskResult()

    async def allow_event(_db, **kwargs):
        return None

    monkeypatch.setattr(task_event_ingestion, "assert_task_event_access", allow_event)
    now = datetime.now(timezone.utc)
    event = TaskEventIn(
        task_id=task_id,
        project_id=project_id,
        kind="annotate",
        started_at=now - timedelta(seconds=2),
        ended_at=now - timedelta(seconds=1),
        duration_ms=1000,
    )
    session = TaskReadSession()

    accepted, rejected = await task_event_ingestion.validate_api_events(
        session,
        user=SimpleNamespace(id=actor_id),
        events=[event],
        now=now,
    )

    assert len(accepted) == 1
    assert rejected == []
    assert len(session.sql) == 1
    assert "FROM aap_labeling_v1.task_rows" in session.sql[0]
    assert "FROM tasks" not in session.sql[0]


class _ReadResult:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


class _ReadSession:
    autoflush = False

    def __init__(self):
        self.sql = []

    async def execute(self, statement):
        self.sql.append(str(statement.compile(dialect=postgresql.dialect())))
        return _ReadResult()


@pytest.mark.asyncio
async def test_row_locks_use_only_fixed_owner_lock_functions():
    session = _ReadSession()
    row_id = uuid.UUID("00000000-0000-0000-0000-000000000001")

    await lock_task_for_core_read(
        session, row_id, project_id=row_id, lock_mode="share", nowait=True
    )
    await lock_batch_rows_for_core_read(
        session, [row_id], project_id=row_id, nowait=True
    )
    await lock_annotation_rows_for_core_read(
        session, [row_id], task_id=row_id, project_id=row_id, nowait=True
    )
    await lock_video_segment_for_core_read(session, row_id, row_id)
    await lock_batch_task_rows_for_core_read(
        session, [row_id], project_id=row_id, batch_id=row_id
    )

    assert "aap_labeling_v1.lock_task_for_core_read" in session.sql[0]
    assert "aap_labeling_v1.lock_batch_rows_for_core_read" in session.sql[1]
    assert "aap_labeling_v1.lock_annotation_rows_for_core_read" in session.sql[2]
    assert "aap_labeling_v1.lock_video_segment_for_core_read" in session.sql[3]
    assert "aap_labeling_v1.lock_task_rows_for_core_read" in session.sql[4]
    assert all("FOR UPDATE" not in statement for statement in session.sql)


@pytest.mark.asyncio
async def test_active_learning_feedback_share_locks_use_fixed_owner_functions():
    session = _ReadSession()
    project_id = uuid.UUID("00000000-0000-0000-0000-000000000012")
    task_ids = [
        uuid.UUID("00000000-0000-0000-0000-000000000014"),
        uuid.UUID("00000000-0000-0000-0000-000000000013"),
    ]

    await lock_feedback_task_rows_for_share(session, project_id, task_ids)
    await lock_active_annotation_rows_for_share(session, task_ids)

    assert len(session.sql) == 2
    assert "aap_labeling_v1.lock_active_learning_feedback_task_rows" in session.sql[0]
    assert (
        "aap_labeling_v1.lock_active_learning_feedback_annotation_rows"
        in session.sql[1]
    )
    assert all("ORDER BY" in statement for statement in session.sql)
    assert all("FROM public.tasks" not in statement for statement in session.sql)
    assert all("FROM public.annotations" not in statement for statement in session.sql)


@pytest.mark.asyncio
async def test_task_rows_lock_accepts_id_only_scope_in_one_query():
    session = _ReadSession()
    task_ids = [
        uuid.UUID("00000000-0000-0000-0000-000000000005"),
        uuid.UUID("00000000-0000-0000-0000-000000000006"),
    ]

    assert await lock_task_rows_for_core_read(session, task_ids) == []
    assert len(session.sql) == 1
    assert "aap_labeling_v1.lock_task_rows_for_core_read" in session.sql[0]
    assert "ORDER BY" in session.sql[0]
    assert "FOR UPDATE" not in session.sql[0]


@pytest.mark.asyncio
async def test_batch_task_lock_keeps_select_autoflush_for_empty_ids():
    class AutoflushSession(_ReadSession):
        autoflush = True

        def __init__(self):
            super().__init__()
            self.flush_count = 0

        async def flush(self):
            self.flush_count += 1

    session = AutoflushSession()
    scope_id = uuid.UUID("00000000-0000-0000-0000-000000000004")

    assert (
        await lock_batch_task_rows_for_core_read(
            session, [], project_id=scope_id, batch_id=scope_id
        )
        == []
    )
    assert session.flush_count == 1
    assert "aap_labeling_v1.lock_task_rows_for_core_read" in session.sql[0]


@pytest.mark.asyncio
async def test_get_core_row_keeps_an_unexpired_identity_map_hit():
    row_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    task = Task(
        id=row_id,
        project_id=uuid.uuid4(),
        display_id="T-1",
        file_name="x",
        file_path="x",
    )

    class IdentityMapSession:
        identity_map = {identity_key(Task, ident=row_id): task}

    assert await get_core_row(IdentityMapSession(), task_row, row_id) is task


@pytest.mark.asyncio
async def test_get_core_row_queries_the_public_relation_when_uncached():
    session = _ReadSession()
    row_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    session.identity_map = {}

    assert await get_core_row(session, task_row, row_id) is None
    assert "FROM aap_labeling_v1.task_rows" in session.sql[0]
    assert "FROM public.tasks" not in session.sql[0]


@pytest.mark.asyncio
async def test_get_core_annotation_row_queries_the_public_relation_when_uncached():
    session = _ReadSession()
    session.identity_map = {}
    row_id = uuid.UUID("00000000-0000-0000-0000-000000000010")

    assert await get_core_row(session, annotation_row, row_id) is None
    assert "FROM aap_labeling_v1.annotation_rows" in session.sql[0]


@pytest.mark.asyncio
async def test_video_tracker_task_lock_uses_fixed_nowait_owner_helper(monkeypatch):
    from app.services.video_tracking import runner

    task_id = uuid.UUID("00000000-0000-0000-0000-000000000011")
    db = object()
    locked_task = object()
    call = {}

    async def lock_task(db_arg, task_id_arg, **kwargs):
        call.update(db=db_arg, task_id=task_id_arg, **kwargs)
        return locked_task

    monkeypatch.setattr(runner, "lock_task_for_core_read", lock_task)

    assert await runner._lock_task_bounded(db, task_id) is locked_task
    assert call == {
        "db": db,
        "task_id": task_id,
        "nowait": True,
        "populate_existing": True,
    }
