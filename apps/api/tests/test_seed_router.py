"""_test_seed router 烟测：owned 夹具与 login 端点契约。"""

from __future__ import annotations

import pytest

from tests import seed_router_scenarios as scenarios
from tests.identity_e2e_support import (
    committed_owner as committed_owner,
    fenced_http as fenced_http,
    fixture_and_fenced_http as fixture_and_fenced_http,
    owner_fault_http as owner_fault_http,
)
from tests.test_training_dataset_references import (
    committed_rle_bindings as committed_rle_bindings,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _require_owned_rle_bindings(request):
    if request.node.originalname in {
        "test_seed_raster_mask_project_opt_in_and_content_fixtures",
        "test_seed_raster_mask_media_canvas_preserves_item_dimensions",
    }:
        # Independent raster allocation must see committed static bindings,
        # before the seeded image pins its identity in the business SAVEPOINT.
        request.getfixturevalue("committed_rle_bindings")


@pytest.mark.parametrize(
    ("environment", "enabled", "database_name", "allowed"),
    [
        ("development", True, "annotation_test", True),
        ("staging", True, "ANNOTATION_E2E", True),
        ("development", False, "annotation_test", False),
        ("production", True, "annotation_test", False),
        ("development", True, "annotation", False),
        ("development", True, "annotation_test_copy", False),
    ],
)
async def test_seed_router_runtime_guard_matrix(
    monkeypatch, environment, enabled, database_name, allowed
):
    from fastapi import HTTPException

    from app.services.test_seed_access import (
        assert_isolated_test_seed_database,
        settings,
    )

    class DatabaseName:
        async def scalar(self, _statement):
            return database_name

    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "e2e_seed_enabled", enabled)
    if allowed:
        await assert_isolated_test_seed_database(DatabaseName())
    else:
        with pytest.raises(HTTPException) as error:
            await assert_isolated_test_seed_database(DatabaseName())
        assert error.value.status_code == 403


async def test_seed_router_uses_configured_test_database(
    owner_fault_http, db_session, test_db_url
):
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    database_name = await db_session.scalar(text("SELECT current_database()"))
    assert database_name == make_url(test_db_url).database

    response = await owner_fault_http.get("/api/v1/__test/seed/peek")
    assert response.status_code == 200, response.text


async def test_seed_owned_returns_fixture_payload(fixture_and_fenced_http):
    await scenarios.seed_owned_returns_fixture_payload(fixture_and_fenced_http)


async def test_filtering_seed_route_is_guarded_and_hidden_from_openapi(
    fixture_and_fenced_http, app_module
):
    run = fixture_and_fenced_http.owner_suite.new_run("filterroute")
    response = await fixture_and_fenced_http.post(
        "/api/v1/__test/seed/filtering", json=run.payload()
    )
    assert response.status_code == 200, response.text
    from app.api.v1._test_seed import router

    route = next(
        route
        for route in router.routes
        if getattr(route, "path", None) == "/seed/filtering"
    )
    assert route.include_in_schema is False
    assert "/api/v1/__test/seed/filtering" not in app_module.openapi()["paths"]


async def test_project_roles_seed_route_is_guarded_and_hidden_from_openapi(
    fixture_and_fenced_http, app_module
):
    run = fixture_and_fenced_http.owner_suite.new_run(scenarios.NAMESPACE)
    response = await fixture_and_fenced_http.post(
        "/api/v1/__test/seed/project-roles",
        json=run.payload(),
    )
    assert response.status_code == 200, response.text
    from app.api.v1._test_seed import router

    route = next(
        route
        for route in router.routes
        if getattr(route, "path", None) == "/seed/project-roles"
    )
    assert route.include_in_schema is False
    assert "/api/v1/__test/seed/project-roles" not in app_module.openapi()["paths"]


async def test_project_roles_seed_builds_multi_project_employee_matrix(
    fixture_and_fenced_http, committed_owner
):
    await scenarios.project_roles_seed_builds_multi_project_employee_matrix(
        fixture_and_fenced_http, committed_owner
    )


async def test_project_roles_seed_rebuilds_after_cleanup(
    fixture_and_fenced_http, committed_owner
):
    await scenarios.project_roles_seed_rebuilds_after_cleanup(
        fixture_and_fenced_http, committed_owner
    )


async def test_seed_owned_rebuilds_with_singleton_pool(
    fixture_and_fenced_http, committed_owner
):
    await scenarios.seed_owned_rebuilds_with_singleton_pool(
        fixture_and_fenced_http, committed_owner
    )


async def test_seed_cleanup_is_idempotent_and_preserves_non_e2e_data(
    fixture_and_fenced_http, committed_owner
):
    await scenarios.seed_cleanup_is_idempotent_and_preserves_non_e2e_data(
        fixture_and_fenced_http, committed_owner
    )


async def test_seed_login_after_owned_returns_jwt(fixture_and_fenced_http):
    await scenarios.seed_login_after_owned_returns_jwt(fixture_and_fenced_http)


async def test_seed_login_unknown_email_404(fixture_and_fenced_http):
    await scenarios.seed_login_unknown_email_404(fixture_and_fenced_http)


async def test_seed_raster_mask_project_opt_in_and_content_fixtures(
    fixture_and_fenced_http, committed_owner
):
    await scenarios.seed_raster_mask_project_opt_in_and_content_fixtures(
        fixture_and_fenced_http, committed_owner
    )


async def test_seed_raster_mask_media_canvas_preserves_item_dimensions(
    fixture_and_fenced_http, committed_owner
):
    await scenarios.seed_raster_mask_media_canvas_preserves_item_dimensions(
        fixture_and_fenced_http, committed_owner
    )


async def test_smart_scribble_masks_use_vehicle_silhouettes():
    from app.api.v1._test_seed import _make_smart_scribble_mask

    for refined in (False, True):
        width, height = 64, 48
        pixels = _make_smart_scribble_mask(width, height, refined=refined)
        foreground = [index for index, value in enumerate(pixels) if value]
        xs = [index % width for index in foreground]
        ys = [index // width for index in foreground]
        bounding_area = (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)

        assert bounding_area * 0.45 < len(foreground) < bounding_area * 0.85


async def test_seed_owned_preserves_dev_data(fixture_and_fenced_http, committed_owner):
    await scenarios.seed_owned_preserves_dev_data(
        fixture_and_fenced_http, committed_owner
    )


async def test_seed_video_webcodecs_rejects_unknown_fixture(fixture_and_fenced_http):
    from uuid import uuid4

    run = fixture_and_fenced_http.owner_suite.new_run("videoguard")
    response = await fixture_and_fenced_http.post(
        "/api/v1/__test/seed/video-webcodecs",
        json={
            **run.payload(),
            "project_id": str(uuid4()),
            "fixture": "not-a-real-fixture",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "unknown_fixture"


async def _video_issue_feedback(client, engine, invalid=None):
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models.annotation_feedback import AnnotationFeedback
    from app.db.models.user import User
    from tests.seed_owned_scenarios import _build_owned

    run = client.owner_suite.new_run("issuehistory")
    world = await _build_owned(client, run)
    video_run, video_world = run, world
    other = None
    if invalid in {"other_run", "cross_project"}:
        other_run = client.owner_suite.new_run("issueneigh")
        other = await _build_owned(client, other_run)
        if invalid == "other_run":
            video_run, video_world = other_run, other

    if invalid == "image":
        task_id = UUID(world["task_ids"][0])
    else:
        video = await client.post(
            "/api/v1/__test/seed/video-task",
            json={**video_run.payload(), "project_id": video_world["project_id"]},
        )
        assert video.status_code == 200, video.text
        task_id = UUID(video.json()["task_id"])
    project_id = UUID(
        other["project_id"] if invalid == "cross_project" else video_world["project_id"]
    )
    anchor = {"x": 0.4, "y": 0.6, "legacy_key": "kept"}
    if invalid != "no_frame":
        anchor["frame"] = 120
    async with async_sessionmaker(engine, expire_on_commit=False).begin() as db:
        author_id = await db.scalar(
            select(User.id).where(User.email == video_world["admin_email"])
        )
        assert author_id is not None
        feedback = AnnotationFeedback(
            project_id=project_id,
            task_id=task_id,
            author_id=author_id,
            kind="issue",
            anchor_type="task" if invalid == "task_anchor" else "pixel",
            is_active=invalid != "inactive",
            body="Keep this body",
            anchor_position=None if invalid == "task_anchor" else anchor,
        )
        db.add(feedback)
        await db.flush()
        feedback_id = feedback.id
    return run, feedback_id


async def _feedback_snapshot(engine, feedback_id):
    from copy import deepcopy

    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.models.annotation_feedback import AnnotationFeedback

    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        feedback = await db.get(AnnotationFeedback, feedback_id)
        assert feedback is not None
        return {
            column.key: deepcopy(getattr(feedback, column.key))
            for column in inspect(type(feedback)).column_attrs
        }


async def test_video_issue_context_history_preserves_the_original_feedback(
    fixture_and_fenced_http, test_engine
):
    from uuid import uuid4

    client = fixture_and_fenced_http
    run, feedback_id = await _video_issue_feedback(client, test_engine)
    before = await _feedback_snapshot(test_engine, feedback_id)
    response = await client.post(
        "/api/v1/__test/seed/video-issue-context-history",
        json={**run.payload(), "feedback_id": str(feedback_id)},
    )
    assert response.status_code == 200, response.text
    expected_anchor = {
        "x": 0.4,
        "y": 0.6,
        "frame": 120,
        "legacy_key": "kept",
        "video_context": {
            "schema_version": 999,
            "viewport": {"future_camera": "unsupported"},
            "frame_range": {"from_frame": 900, "to_frame": 901},
        },
    }
    after = await _feedback_snapshot(test_engine, feedback_id)
    assert response.json()["feedback_id"] == str(feedback_id)
    assert response.json()["anchor_position"] == expected_anchor
    assert after.pop("anchor_position") == expected_anchor
    before.pop("anchor_position")
    after.pop("updated_at")
    before.pop("updated_at")
    assert after == before
    current = await _feedback_snapshot(test_engine, feedback_id)
    missing = await client.post(
        "/api/v1/__test/seed/video-issue-context-history",
        json={**run.payload(), "feedback_id": str(uuid4())},
    )
    assert missing.status_code == 404, missing.text
    assert await _feedback_snapshot(test_engine, feedback_id) == current


@pytest.mark.parametrize(
    "invalid",
    ["other_run", "image", "cross_project", "task_anchor", "no_frame", "inactive"],
)
async def test_video_issue_context_history_rejects_other_records(
    invalid, fixture_and_fenced_http, test_engine
):
    from sqlalchemy import delete

    from app.db.models.annotation_feedback import AnnotationFeedback

    client = fixture_and_fenced_http
    run, feedback_id = await _video_issue_feedback(client, test_engine, invalid)
    try:
        before = await _feedback_snapshot(test_engine, feedback_id)
        response = await client.post(
            "/api/v1/__test/seed/video-issue-context-history",
            json={**run.payload(), "feedback_id": str(feedback_id)},
        )
        assert response.status_code == 422, response.text
        assert await _feedback_snapshot(test_engine, feedback_id) == before
    finally:
        if invalid == "cross_project":
            # The deliberately malformed test row crosses two owned projects.
            async with test_engine.begin() as db:
                await db.execute(
                    delete(AnnotationFeedback).where(
                        AnnotationFeedback.id == feedback_id
                    )
                )


async def test_video_issue_context_history_input_cannot_choose_arbitrary_fields():
    from uuid import uuid4

    from pydantic import ValidationError

    from app.api.v1 import _test_seed

    with pytest.raises(ValidationError):
        _test_seed.SeedVideoIssueContextHistoryRequest(
            namespace="routeguard",
            suite_id=uuid4(),
            run_id=uuid4(),
            feedback_id=uuid4(),
            video_context={"schema_version": 1},
        )
    with pytest.raises(ValidationError):
        _test_seed.SeedVideoWebCodecsRequest(
            namespace="routeguard",
            suite_id=uuid4(),
            run_id=uuid4(),
            project_id=str(uuid4()),
            batch_id="invalid",
        )


@pytest.mark.parametrize("fixture", ["webcodecs", "filtering"])
async def test_fixture_object_cleanup_paginates_batches_and_verifies(fixture):
    from types import SimpleNamespace

    from app.api.v1._test_seed import (
        _delete_filtering_seed_objects,
        _delete_webcodecs_seed_objects,
    )

    prefix = "e2e/video/webcodecs/" if fixture == "webcodecs" else "e2e/filtering/"
    cleanup = (
        _delete_webcodecs_seed_objects
        if fixture == "webcodecs"
        else _delete_filtering_seed_objects
    )
    keys = [f"{prefix}{index}.mp4" for index in range(1001)]

    class Client:
        def __init__(self):
            self.list_calls = 0
            self.deleted_batches: list[list[str]] = []

        def list_objects_v2(self, **kwargs):
            assert kwargs["Bucket"] == "datasets"
            assert kwargs["Prefix"] == prefix
            self.list_calls += 1
            if kwargs.get("MaxKeys") == 1:
                return {"Contents": []}
            if kwargs.get("ContinuationToken"):
                return {"Contents": [{"Key": keys[-1]}], "IsTruncated": False}
            return {
                "Contents": [{"Key": key} for key in keys[:1000]],
                "IsTruncated": True,
                "NextContinuationToken": "page-2",
            }

        def delete_objects(self, **kwargs):
            self.deleted_batches.append(
                [item["Key"] for item in kwargs["Delete"]["Objects"]]
            )
            return {}

    client = Client()
    cleanup(SimpleNamespace(client=client, datasets_bucket="datasets"))
    assert [len(batch) for batch in client.deleted_batches] == [1000, 1]
    assert client.list_calls == 3


async def test_comment_attachment_cleanup_only_removes_resolved_fixture_annotation_prefix():
    from types import SimpleNamespace
    from uuid import uuid4

    from app.api.v1._test_seed import _delete_comment_attachment_seed_objects

    fixture_annotation = uuid4()
    other_annotation = uuid4()
    prefix = f"comment-attachments/{fixture_annotation}/"
    keys = {
        f"{prefix}completed.txt",
        f"{prefix}unfinished-upload.txt",
        f"comment-attachments/{other_annotation}/keep.txt",
        "e2e/video/webcodecs/keep.mp4",
    }

    class Client:
        def list_objects_v2(self, **kwargs):
            assert kwargs["Bucket"] == "task-owned-annotations"
            assert kwargs["Prefix"] == prefix
            return {
                "Contents": [{"Key": key} for key in keys if key.startswith(prefix)],
                "IsTruncated": False,
            }

        def delete_objects(self, **kwargs):
            assert kwargs["Bucket"] == "task-owned-annotations"
            for item in kwargs["Delete"]["Objects"]:
                assert item["Key"].startswith(prefix)
                keys.remove(item["Key"])
            return {}

    storage = SimpleNamespace(client=Client(), bucket="task-owned-annotations")
    _delete_comment_attachment_seed_objects(storage, [fixture_annotation])
    _delete_comment_attachment_seed_objects(storage, [fixture_annotation])
    assert keys == {
        f"comment-attachments/{other_annotation}/keep.txt",
        "e2e/video/webcodecs/keep.mp4",
    }


async def test_comment_attachment_prefix_never_deletes_canonical_keys_without_owner():
    from types import SimpleNamespace
    from uuid import uuid4
    from fastapi import HTTPException
    from app.api.v1._test_seed import _delete_comment_attachment_seed_objects

    annotation = uuid4()
    canonical = f"comment-attachments/{annotation}/{uuid4()}"
    legacy = f"comment-attachments/{annotation}/legacy.txt"

    class Client:
        def list_objects_v2(self, **kwargs):
            return {
                "Contents": [{"Key": canonical}, {"Key": legacy}],
                "IsTruncated": False,
            }

        def delete_objects(self, **kwargs):
            raise AssertionError(
                "An unpinned canonical key must never enter prefix deletion"
            )

    with pytest.raises(HTTPException) as failure:
        _delete_comment_attachment_seed_objects(
            SimpleNamespace(client=Client(), bucket="fixture"), [annotation]
        )
    assert failure.value.status_code == 409
    assert failure.value.detail["code"] == "e2e_seed_attachment_canonical_residual"


async def test_webcodecs_object_cleanup_fails_on_partial_delete():
    from types import SimpleNamespace

    from app.api.v1._test_seed import _delete_webcodecs_seed_objects

    class Client:
        def list_objects_v2(self, **kwargs):
            return {
                "Contents": [{"Key": "e2e/video/webcodecs/a.mp4"}],
                "IsTruncated": False,
            }

        def delete_objects(self, **kwargs):
            return {"Errors": [{"Key": "redacted", "Code": "AccessDenied"}]}

    with pytest.raises(RuntimeError, match="delete failed for 1 object"):
        _delete_webcodecs_seed_objects(
            SimpleNamespace(client=Client(), datasets_bucket="datasets")
        )


async def test_webcodecs_object_cleanup_fails_when_verification_finds_residual():
    from types import SimpleNamespace

    from app.api.v1._test_seed import _delete_webcodecs_seed_objects

    class Client:
        def list_objects_v2(self, **kwargs):
            return {
                "Contents": [{"Key": "e2e/video/webcodecs/a.mp4"}],
                "IsTruncated": False,
            }

        def delete_objects(self, **kwargs):
            return {}

    with pytest.raises(RuntimeError, match="left MinIO objects"):
        _delete_webcodecs_seed_objects(
            SimpleNamespace(client=Client(), datasets_bucket="datasets")
        )
