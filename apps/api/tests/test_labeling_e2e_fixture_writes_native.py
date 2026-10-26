"""Exercise the test-only fixture capability with private Labeling DML denied."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError

from app.db.models.annotation import Annotation
from app.db.models.prediction import Prediction
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.labeling_core_read_relations import prediction_row
from app.services.labeling_e2e_fixture_writes import LabelingE2EFixtureWrites
from app.services.workspace_e2e_fixtures import create_fixture_project
from tests.factory import create_task, create_user
from tests.test_workspace_consumer_profile_native import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned disposable PostgreSQL",
    ),
]


async def test_fixture_defaults_and_caller_transaction_use_the_owner(test_engine):
    async with _world(test_engine) as (owner, api, role, _url):
        async with owner.begin() as db:
            actor = await create_user(
                db,
                "super_admin",
                f"labeling-fixture-{uuid.uuid4()}@e2e.test",
                "Fixture",
            )
            actor_id = actor.id
            project = await create_fixture_project(
                db,
                owner_id=actor_id,
                name=f"E2E fixture protocol {uuid.uuid4().hex[:10]}",
            )
            project_id = project.id

        statements: list[str] = []
        engine = api.kw["bind"].sync_engine

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.lower())

        event.listen(engine, "before_cursor_execute", capture)
        batch_id = uuid.uuid4()
        task_id = None
        parent_id = None
        child_id = None
        try:
            async with api() as db:
                assert await db.scalar(text("SELECT current_user")) == role
                assert await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user, "
                        "'aap_labeling_v1.e2e_fixture_command(jsonb)', 'EXECUTE')"
                    )
                )
                for table in ("tasks", "task_batches", "annotations"):
                    assert not await db.scalar(
                        text(
                            "SELECT has_table_privilege(current_user,:relation,"
                            "'SELECT,INSERT,UPDATE,DELETE') OR "
                            "has_any_column_privilege(current_user,:relation,"
                            "'SELECT,INSERT,UPDATE,REFERENCES')"
                        ),
                        {"relation": f"public.{table}"},
                    ), table

                writes = LabelingE2EFixtureWrites(db)
                batch = (
                    await writes.insert_batches(
                        [
                            TaskBatch(
                                id=batch_id,
                                project_id=project_id,
                                display_id=f"B-E2E-{batch_id.hex[:10]}",
                                name="Defaulted batch",
                                status=None,
                                total_tasks=None,
                                reviewer_id=None,
                            )
                        ]
                    )
                )[0]
                task = (
                    await writes.insert_tasks(
                        [
                            Task(
                                project_id=project_id,
                                batch_id=batch.id,
                                display_id=f"T-E2E-{uuid.uuid4().hex[:10]}",
                                file_name="default.png",
                                file_path="fixture/default.png",
                                status=None,
                                file_type=None,
                                created_at=None,
                                total_annotations=None,
                            )
                        ]
                    )
                )[0]
                task_id = task.id
                task = await writes.patch_task(task, status="completed")
                parent_id = uuid.uuid4()
                child_id = uuid.uuid4()
                annotations = await writes.insert_annotations(
                    [
                        Annotation(
                            id=child_id,
                            task_id=task.id,
                            project_id=project_id,
                            user_id=actor_id,
                            source="manual",
                            annotation_type="bbox",
                            tool_unit_id="bbox",
                            class_name="car",
                            geometry={
                                "type": "bbox",
                                "x": 0.1,
                                "y": 0.1,
                                "w": 0.2,
                                "h": 0.2,
                            },
                            parent_annotation_id=parent_id,
                        ),
                        Annotation(
                            id=parent_id,
                            task_id=task.id,
                            project_id=project_id,
                            user_id=actor_id,
                            source="manual",
                            annotation_type="bbox",
                            tool_unit_id="bbox",
                            class_name="car",
                            geometry={
                                "type": "bbox",
                                "x": 0.1,
                                "y": 0.1,
                                "w": 0.2,
                                "h": 0.2,
                            },
                            parent_annotation_id=None,
                        ),
                    ]
                )
                assert [row.id for row in annotations] == [child_id, parent_id]

                async with owner() as observer:
                    assert (
                        await observer.scalar(
                            text("SELECT count(*) FROM tasks WHERE id=:id"),
                            {"id": task_id},
                        )
                        == 0
                    )
                await db.commit()

            async with owner() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM task_batches WHERE id=:id "
                            "AND status='draft' AND total_tasks=0 AND reviewer_id IS NULL"
                        ),
                        {"id": batch_id},
                    )
                    == 1
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM tasks WHERE id=:id AND status='completed' "
                            "AND file_type='image' AND tags='[]'::jsonb AND total_annotations=0"
                        ),
                        {"id": task_id},
                    )
                    == 1
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT parent_annotation_id FROM annotations WHERE id=:id"
                        ),
                        {"id": child_id},
                    )
                    == parent_id
                )
        finally:
            event.remove(engine, "before_cursor_execute", capture)
            async with owner.begin() as db:
                if task_id is not None:
                    for annotation_id in (child_id, parent_id):
                        if annotation_id is not None:
                            await db.execute(
                                text("DELETE FROM annotations WHERE id=:id"),
                                {"id": annotation_id},
                            )
                    await db.execute(
                        text("DELETE FROM tasks WHERE id=:id"), {"id": task_id}
                    )
                await db.execute(
                    text("DELETE FROM task_batches WHERE id=:id"), {"id": batch_id}
                )
                await db.execute(
                    text(
                        "SELECT aap_workspace_fixture_v1.delete_projects(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [project_id]},
                )
                await db.execute(
                    text("DELETE FROM users WHERE id=:id"), {"id": actor_id}
                )

        assert any("aap_labeling_v1.e2e_fixture_command" in sql for sql in statements)
        assert not any(
            sql.lstrip().startswith(
                (
                    "insert into public.tasks",
                    "update public.tasks",
                    "insert into public.task_batches",
                    "update public.task_batches",
                )
            )
            for sql in statements
        )


async def _prediction_results(session_factory, prediction_ids):
    async with session_factory() as db:
        rows = (
            await db.scalars(
                select(prediction_row).where(prediction_row.id.in_(prediction_ids))
            )
        ).all()
        return {row.id: row.result for row in rows}


async def test_filtering_rendering_is_scoped_atomic_and_caller_transactional(
    test_engine,
):
    async with _world(test_engine) as (owner, api, role, _url):
        actor_id = None
        project_ids = []
        task_ids = []
        predictions = {}
        try:
            async with owner.begin() as db:
                actor = await create_user(
                    db,
                    "super_admin",
                    f"labeling-filter-render-{uuid.uuid4()}@e2e.test",
                    "Filtering render",
                )
                actor_id = actor.id
                project = await create_fixture_project(
                    db,
                    owner_id=actor_id,
                    name=f"Filtering render {uuid.uuid4().hex[:10]}",
                )
                foreign_project = await create_fixture_project(
                    db,
                    owner_id=actor_id,
                    name=f"Foreign filtering render {uuid.uuid4().hex[:10]}",
                )
                project_ids = [project.id, foreign_project.id]
                task = await create_task(
                    db,
                    project_id=project.id,
                    display_id=f"T-E2E-{uuid.uuid4().hex[:10]}",
                )
                task.file_type = "video"
                foreign_task = await create_task(
                    db,
                    project_id=foreign_project.id,
                    display_id=f"T-E2E-{uuid.uuid4().hex[:10]}",
                )
                foreign_task.file_type = "video"
                await db.flush()
                task_ids = [task.id, foreign_task.id]
                for name, prediction_task, model_version, confidence, with_score in (
                    ("low", task, "filter-detector-low", 0.2, True),
                    ("at", task, "filter-detector-at", 0.5, True),
                    ("above", task, "filter-detector-above", 0.9, True),
                    ("bad_at", task, "filter-detector-at", 0.7, False),
                    ("foreign_low", foreign_task, "filter-detector-low", 0.4, True),
                ):
                    prediction_id = uuid.uuid4()
                    shape = {"type": "rectanglelabels", "value": {}}
                    if with_score:
                        shape["score"] = confidence
                    predictions[name] = Prediction(
                        id=prediction_id,
                        task_id=prediction_task.id,
                        project_id=prediction_task.project_id,
                        model_version=model_version,
                        score=confidence,
                        result=[shape],
                    )
                db.add_all(predictions.values())
                await db.flush()

            prediction_ids = [row.id for row in predictions.values()]
            initial_results = await _prediction_results(owner, prediction_ids)
            assert len(initial_results) == 5
            async with api() as db:
                assert await db.scalar(text("SELECT current_user")) == role
                assert await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user, "
                        "'aap_labeling_v1.prepare_filtering_rendering(jsonb)', 'EXECUTE')"
                    )
                )
                for table in (
                    "tasks",
                    "task_batches",
                    "annotations",
                    "predictions",
                ):
                    assert not await db.scalar(
                        text(
                            "SELECT has_table_privilege(current_user,:relation,"
                            "'SELECT,INSERT,UPDATE,DELETE') OR "
                            "has_any_column_privilege(current_user,:relation,"
                            "'SELECT,INSERT,UPDATE,REFERENCES')"
                        ),
                        {"relation": f"public.{table}"},
                    ), table

                writes = LabelingE2EFixtureWrites(db)
                savepoint = await db.begin_nested()
                try:
                    with pytest.raises(DBAPIError) as ordinary_write:
                        await db.execute(
                            text(
                                "UPDATE public.predictions SET result='[]'::jsonb "
                                "WHERE id=:id"
                            ),
                            {"id": predictions["low"].id},
                        )
                    assert ordinary_write.value.orig.sqlstate == "42501"
                finally:
                    await savepoint.rollback()

                async def rejected_scope(**candidate_ids):
                    savepoint = await db.begin_nested()
                    try:
                        with pytest.raises(DBAPIError) as error:
                            await writes.prepare_filtering_rendering(
                                project_id=project.id, **candidate_ids
                            )
                        assert error.value.orig.sqlstate == "42501"
                    finally:
                        await savepoint.rollback()

                await rejected_scope(
                    low_prediction_id=predictions["low"].id,
                    at_prediction_id=predictions["low"].id,
                    above_prediction_id=predictions["above"].id,
                )
                await rejected_scope(
                    low_prediction_id=uuid.uuid4(),
                    at_prediction_id=predictions["at"].id,
                    above_prediction_id=predictions["above"].id,
                )
                await rejected_scope(
                    low_prediction_id=predictions["foreign_low"].id,
                    at_prediction_id=predictions["at"].id,
                    above_prediction_id=predictions["above"].id,
                )

                savepoint = await db.begin_nested()
                try:
                    with pytest.raises(DBAPIError) as partial_failure:
                        await writes.prepare_filtering_rendering(
                            project_id=project.id,
                            low_prediction_id=predictions["low"].id,
                            at_prediction_id=predictions["bad_at"].id,
                            above_prediction_id=predictions["above"].id,
                        )
                    assert partial_failure.value.orig.sqlstate == "22023"
                finally:
                    await savepoint.rollback()
                assert (
                    await _prediction_results(owner, prediction_ids) == initial_results
                )

                candidate_ids = [predictions[key].id for key in ("low", "at", "above")]
                tracked = (
                    await db.scalars(
                        select(prediction_row).where(
                            prediction_row.id.in_(candidate_ids)
                        )
                    )
                ).all()
                assert {row.id for row in tracked} == set(candidate_ids)
                await writes.prepare_filtering_rendering(
                    project_id=project.id,
                    low_prediction_id=predictions["low"].id,
                    at_prediction_id=predictions["at"].id,
                    above_prediction_id=predictions["above"].id,
                )
                assert all(
                    not db.is_modified(row, include_collections=True) for row in tracked
                )
                assert await _prediction_results(owner, candidate_ids) == {
                    prediction_id: initial_results[prediction_id]
                    for prediction_id in candidate_ids
                }
                await db.commit()

            rendered = await _prediction_results(
                owner, [predictions[key].id for key in ("low", "at", "above")]
            )
            for key, frame_index, confidence in (
                ("low", 0, 0.2),
                ("at", 0, 0.5),
                ("above", 10, 0.9),
            ):
                result = rendered[predictions[key].id]
                assert result == [
                    {
                        "type": "video_bbox",
                        "class_name": "car",
                        "geometry": {
                            "type": "video_bbox",
                            "frame_index": frame_index,
                            "x": 0.62,
                            "y": 0.62,
                            "w": 0.12,
                            "h": 0.15,
                        },
                        "confidence": confidence,
                        "tool_unit_id": "bbox",
                        "attributes": {},
                    }
                ]
        finally:
            async with owner.begin() as db:
                if predictions:
                    await db.execute(
                        text(
                            "DELETE FROM public.predictions "
                            "WHERE id = ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": [row.id for row in predictions.values()]},
                    )
                if task_ids:
                    await db.execute(
                        text(
                            "DELETE FROM public.tasks "
                            "WHERE id = ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": task_ids},
                    )
                if project_ids:
                    await db.execute(
                        text(
                            "SELECT aap_workspace_fixture_v1.delete_projects("
                            "CAST(:ids AS uuid[]))"
                        ),
                        {"ids": project_ids},
                    )
                if actor_id is not None:
                    await db.execute(
                        text("DELETE FROM users WHERE id=:id"), {"id": actor_id}
                    )
