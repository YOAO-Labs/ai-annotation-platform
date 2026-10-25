"""Labeling's current source read is scoped and refreshes per statement."""

from datetime import datetime, timezone
import uuid

import pytest
from sqlalchemy import event, update
from sqlalchemy.dialects import postgresql

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.services.labeling_evaluation_source import (
    SqlAlchemyLabelingEvaluationSourceReader,
)
from app.services.labeling_evaluation_source_contract import CurrentTaskSourceRead
from tests.factory import create_project, create_task


@pytest.mark.asyncio
async def test_reader_builds_one_query_from_published_labeling_and_asset_facts():
    class EmptyResult:
        def first(self):
            return None

    class CompileSession:
        autoflush = False

        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return EmptyResult()

    session = CompileSession()
    reader = SqlAlchemyLabelingEvaluationSourceReader(session)
    source = await reader.read_current_task_source(
        CurrentTaskSourceRead(
            project_id=uuid.uuid4(), task_id=uuid.uuid4(), annotation_limit=2
        )
    )

    assert source is None
    assert len(session.statements) == 1
    sql = session.statements[0]
    assert "FROM aap_labeling_v1.task_rows" in sql
    assert "aap_labeling_v1.annotation_rows" in sql
    assert "aap_assets_v1.labeling_dataset_item_facts" in sql
    for private_relation in ("FROM tasks", "FROM annotations", "FROM dataset_items"):
        assert private_relation not in sql


async def test_reader_scopes_caps_and_refreshes_each_current_source_read(
    db_session, super_admin
):
    owner, _token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    foreign_project = await create_project(db_session, owner_id=owner.id)
    task = await create_task(db_session, project_id=project.id)
    annotations = [
        Annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=owner.id,
            class_name=f"class-{index}",
            tool_unit_id="bbox",
            geometry={"type": "bbox", "x": index, "y": 0, "w": 1, "h": 1},
        )
        for index in range(3)
    ]
    db_session.add_all(annotations)
    await db_session.flush()

    reader = SqlAlchemyLabelingEvaluationSourceReader(db_session)
    request = CurrentTaskSourceRead(
        project_id=project.id,
        task_id=task.id,
        annotation_limit=2,
    )
    engine = db_session.bind.sync_engine
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        source = await reader.read_current_task_source(request)
        assert len(statements) == 1
        assert source is not None
        expected_ids = sorted(str(annotation.id) for annotation in annotations)[:2]
        assert [annotation["id"] for annotation in source["annotations"]] == (
            expected_ids
        )
        assert set(source["annotations"][0]["geometry"]) == {
            "type",
            "x",
            "y",
            "w",
            "h",
        }

        statements.clear()
        missing = await reader.read_current_task_source(
            CurrentTaskSourceRead(
                project_id=foreign_project.id,
                task_id=task.id,
                annotation_limit=2,
            )
        )
        assert len(statements) == 1
        assert missing is None

        changed_annotation = min(annotations, key=lambda annotation: str(annotation.id))
        await db_session.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                status="completed",
                version=Task.version + 1,
                reviewed_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
        await db_session.execute(
            update(Annotation)
            .where(Annotation.id == changed_annotation.id)
            .values(class_name="truck", geometry={"type": "bbox", "x": 0.75})
            .execution_options(synchronize_session=False)
        )

        statements.clear()
        refreshed = await reader.read_current_task_source(request)
        assert len(statements) == 1
        assert refreshed is not None
        assert refreshed["version"] == source["version"] + 1
        assert refreshed["status"] == "completed"
        assert refreshed["annotations"][0]["class_name"] == "truck"
        assert source["annotations"][0]["class_name"] != "truck"
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
