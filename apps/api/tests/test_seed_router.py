"""v0.8.3 · _test_seed router 烟测：reset + login 端点契约。"""

from __future__ import annotations

import pytest

from tests import seed_router_scenarios as scenarios
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


async def test_e2e_seed_setting_defaults_off(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("E2E_SEED_ENABLED", raising=False)
    assert Settings(_env_file=None).e2e_seed_enabled is False


@pytest.mark.parametrize(
    ("environment", "enabled", "expected"),
    [
        ("development", False, False),
        ("development", True, True),
        ("staging", False, False),
        ("staging", True, True),
        ("production", False, False),
        ("production", True, False),
    ],
)
async def test_seed_router_mount_gate(
    monkeypatch, environment: str, enabled: bool, expected: bool
):
    from app.api.v1 import router as router_module

    monkeypatch.setattr(router_module._settings, "environment", environment)
    monkeypatch.setattr(router_module._settings, "e2e_seed_enabled", enabled)

    assert router_module._e2e_seed_routes_enabled() is expected


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
    monkeypatch,
    environment: str,
    enabled: bool,
    database_name: str,
    allowed: bool,
):
    from fastapi import HTTPException

    from app.api.v1 import _test_seed

    class StubSession:
        async def scalar(self, statement):
            assert str(statement) == "SELECT current_database()"
            return database_name

    monkeypatch.setattr(_test_seed.settings, "environment", environment)
    monkeypatch.setattr(_test_seed.settings, "e2e_seed_enabled", enabled)

    if allowed:
        await _test_seed._require_e2e_seed_database(StubSession())
    else:
        with pytest.raises(HTTPException) as exc_info:
            await _test_seed._require_e2e_seed_database(StubSession())
        assert exc_info.value.status_code == 403


async def test_all_seed_routes_have_database_guard():
    from fastapi.routing import APIRoute

    from app.api.v1 import _test_seed

    routes = [
        route for route in _test_seed.router.routes if isinstance(route, APIRoute)
    ]
    assert routes
    assert all(
        any(
            dependency.call is _test_seed._require_e2e_seed_database
            for dependency in route.dependant.dependencies
        )
        for route in routes
    )


async def test_seed_router_uses_configured_test_database(
    httpx_client, db_session, test_db_url
):
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    database_name = await db_session.scalar(text("SELECT current_database()"))
    assert database_name == make_url(test_db_url).database

    response = await httpx_client.get("/api/v1/__test/seed/peek")
    assert response.status_code == 200, response.text


async def test_seed_reset_returns_fixture_payload(httpx_client):
    await scenarios.seed_reset_returns_fixture_payload(httpx_client)


async def test_filtering_seed_route_is_guarded_and_hidden_from_openapi(
    httpx_client, app_module
):
    response = await httpx_client.post("/api/v1/__test/seed/filtering")
    assert response.status_code == 200, response.text
    route = next(
        route
        for route in app_module.routes
        if getattr(route, "path", None) == "/api/v1/__test/seed/filtering"
    )
    assert route.include_in_schema is False
    assert "/api/v1/__test/seed/filtering" not in app_module.openapi()["paths"]


async def test_project_roles_seed_route_is_guarded_and_hidden_from_openapi(
    httpx_client, app_module
):
    response = await httpx_client.post("/api/v1/__test/seed/project-roles")
    assert response.status_code == 200, response.text
    route = next(
        route
        for route in app_module.routes
        if getattr(route, "path", None) == "/api/v1/__test/seed/project-roles"
    )
    assert route.include_in_schema is False
    assert "/api/v1/__test/seed/project-roles" not in app_module.openapi()["paths"]


async def test_project_roles_seed_builds_multi_project_employee_matrix(
    httpx_client, db_session
):
    await scenarios.project_roles_seed_builds_multi_project_employee_matrix(
        httpx_client, db_session
    )


async def test_project_roles_seed_is_idempotent(httpx_client, db_session):
    await scenarios.project_roles_seed_is_idempotent(httpx_client, db_session)


async def test_seed_reset_is_idempotent_with_singleton_pool(httpx_client, db_session):
    await scenarios.seed_reset_is_idempotent_with_singleton_pool(
        httpx_client, db_session
    )


async def test_seed_cleanup_is_idempotent_and_preserves_non_e2e_data(
    httpx_client, db_session
):
    await scenarios.seed_cleanup_is_idempotent_and_preserves_non_e2e_data(
        httpx_client, db_session
    )


async def test_seed_login_after_reset_returns_jwt(httpx_client):
    await scenarios.seed_login_after_reset_returns_jwt(httpx_client)


async def test_seed_login_unknown_email_404(httpx_client):
    await scenarios.seed_login_unknown_email_404(httpx_client)


async def test_seed_raster_mask_project_opt_in_and_content_fixtures(httpx_client):
    await scenarios.seed_raster_mask_project_opt_in_and_content_fixtures(httpx_client)


async def test_seed_raster_mask_media_canvas_preserves_item_dimensions(
    httpx_client, db_session
):
    await scenarios.seed_raster_mask_media_canvas_preserves_item_dimensions(
        httpx_client, db_session
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


async def test_seed_reset_preserves_dev_data(httpx_client, db_session):
    await scenarios.seed_reset_preserves_dev_data(httpx_client, db_session)


async def test_seed_video_webcodecs_rejects_unknown_fixture():
    """未知 fixture 名在 DB 操作前被拒(422),不触碰数据库。"""
    from fastapi import HTTPException

    from app.api.v1 import _test_seed

    req = _test_seed.SeedVideoWebCodecsRequest(
        project_id="11111111-1111-4111-8111-111111111111",
        fixture="not-a-real-fixture",
    )
    with pytest.raises(HTTPException) as exc_info:
        await _test_seed.seed_video_webcodecs(req, db=None)
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "unknown_fixture"


async def _video_issue_feedback(db_session, invalid=None):
    from uuid import uuid4

    from app.db.models.annotation_feedback import AnnotationFeedback
    from tests.factory import create_project, create_task, create_user

    owner = await create_user(
        db_session, "employee", f"video-issue-{uuid4()}@example.com", "Issue author"
    )
    project = await create_project(
        db_session,
        owner_id=owner.id,
        name="Ordinary Project" if invalid == "non_e2e" else "E2E Demo Project",
    )
    if invalid == "non_e2e":
        project.display_id = f"P-ORDINARY-{uuid4().hex[:6]}"
    task_project = project
    if invalid == "cross_project":
        task_project = await create_project(db_session, owner_id=owner.id)
    task = await create_task(db_session, project_id=task_project.id)
    task.file_type = "image" if invalid == "image" else "video"
    anchor = {"x": 0.4, "y": 0.6, "legacy_key": "kept"}
    if invalid != "no_frame":
        anchor["frame"] = 120
    feedback = AnnotationFeedback(
        project_id=project.id,
        task_id=task.id,
        author_id=owner.id,
        kind="issue",
        anchor_type="task" if invalid == "task_anchor" else "pixel",
        is_active=invalid != "inactive",
        body="Keep this body",
        anchor_position=None if invalid == "task_anchor" else anchor,
    )
    db_session.add(feedback)
    await db_session.flush()
    await db_session.refresh(feedback)
    return feedback


async def test_video_issue_context_history_preserves_the_original_feedback(db_session):
    from copy import deepcopy

    from sqlalchemy import inspect

    from app.api.v1 import _test_seed

    feedback = await _video_issue_feedback(db_session)
    before = {
        column.key: deepcopy(getattr(feedback, column.key))
        for column in inspect(type(feedback)).column_attrs
        if column.key not in {"anchor_position", "updated_at"}
    }
    response = await _test_seed.seed_video_issue_context_history(
        _test_seed.SeedVideoIssueContextHistoryRequest(feedback_id=feedback.id),
        db_session,
    )
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
    await db_session.refresh(feedback)
    assert response.feedback_id == feedback.id
    assert response.anchor_position == expected_anchor
    assert feedback.anchor_position == expected_anchor
    assert {
        column.key: getattr(feedback, column.key)
        for column in inspect(type(feedback)).column_attrs
        if column.key not in {"anchor_position", "updated_at"}
    } == before


@pytest.mark.parametrize(
    "invalid",
    ["non_e2e", "image", "cross_project", "task_anchor", "no_frame", "inactive"],
)
async def test_video_issue_context_history_rejects_other_records(invalid, db_session):
    from copy import deepcopy

    from fastapi import HTTPException
    from sqlalchemy import inspect

    from app.api.v1 import _test_seed

    feedback = await _video_issue_feedback(db_session, invalid)
    before = {
        column.key: deepcopy(getattr(feedback, column.key))
        for column in inspect(type(feedback)).column_attrs
    }
    with pytest.raises(HTTPException) as error:
        await _test_seed.seed_video_issue_context_history(
            _test_seed.SeedVideoIssueContextHistoryRequest(feedback_id=feedback.id),
            db_session,
        )
    assert error.value.status_code == 422
    await db_session.refresh(feedback)
    assert {
        column.key: getattr(feedback, column.key)
        for column in inspect(type(feedback)).column_attrs
    } == before


async def test_video_issue_context_history_input_cannot_choose_arbitrary_fields():
    from uuid import uuid4

    from pydantic import ValidationError

    from app.api.v1 import _test_seed

    with pytest.raises(ValidationError):
        _test_seed.SeedVideoIssueContextHistoryRequest(
            feedback_id=uuid4(), video_context={"schema_version": 1}
        )
    with pytest.raises(ValidationError):
        _test_seed.SeedVideoWebCodecsRequest(
            project_id=str(uuid4()), batch_id="invalid"
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
