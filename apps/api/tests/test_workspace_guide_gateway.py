from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from aap_workspace.configuration_contract import (
    configuration_guide_success_reply,
    configuration_request,
)
from aap_workspace.configuration import ConfigurationPostCommitFailure
from app.services.workspace_configuration import ConfigurationExecution
from app.api.v1 import guide_assets
from app.schemas.guide_asset import GuideAssetUploadInitRequest

pytestmark = pytest.mark.asyncio


def _identity(actor_id):
    return SimpleNamespace(id=actor_id, role="project_admin")


def _guide_success(operation, actor_id, project_id, payload, data, *, refreshed):
    request = configuration_request(
        operation,
        actor_id,
        project_id,
        payload,
    )
    reply = configuration_guide_success_reply(
        request,
        resource_id=project_id,
        data=data,
        refreshed=refreshed,
    )
    execution = ConfigurationExecution(
        resource_id=project_id,
        template_id=None,
        operation_id=request.operation_id,
        operation=request.operation,
        actor_id=actor_id,
        guide_result=reply,
        guide_request=request,
    )
    return request, reply, execution


async def test_public_admission_uses_manager_and_visible_project_query_operations(
    monkeypatch,
):
    calls = []
    project_id = uuid.uuid4()
    actor = _identity(uuid.uuid4())

    async def select_project(db, *, operation, actor, target_id):
        calls.append((db, operation, actor, target_id))
        return {"project_id": target_id}

    monkeypatch.setattr(guide_assets, "execute_project_query", select_project)
    db = object()

    assert (
        await guide_assets._require_project_manager(project_id, db, actor) == project_id
    )
    assert (
        await guide_assets._require_project_visible(project_id, db, actor) == project_id
    )
    assert [(call[1], call[3]) for call in calls] == [
        ("require_project_manager", project_id),
        ("require_project_visible", project_id),
    ]


async def test_upload_complete_missing_payload_stops_before_configuration_selection(
    monkeypatch,
):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Missing payload must be rejected before configuration")

    monkeypatch.setattr(guide_assets, "_execute_guide_operation", forbidden)
    with pytest.raises(HTTPException) as caught:
        await guide_assets.guide_asset_upload_complete(
            {"key": "key", "original_name": "", "content_type": "image/png"},
            uuid.uuid4(),
            object(),
            _identity(uuid.uuid4()),
        )
    assert caught.value.status_code == 400
    assert caught.value.detail == "Missing required fields"


async def test_upload_complete_passes_truthy_wrong_types_without_coercion(monkeypatch):
    captured = {}

    class StopAfterCapture(Exception):
        pass

    async def capture(db, **kwargs):
        captured.update(kwargs)
        raise StopAfterCapture

    monkeypatch.setattr(guide_assets, "_execute_guide_operation", capture)
    raw = {"key": 7, "original_name": ["raw"], "content_type": {"raw": True}}
    with pytest.raises(StopAfterCapture):
        await guide_assets.guide_asset_upload_complete(
            raw, uuid.uuid4(), object(), _identity(uuid.uuid4())
        )
    assert captured["payload"] == raw
    assert type(captured["payload"]["key"]) is int
    assert type(captured["payload"]["original_name"]) is list
    assert type(captured["payload"]["content_type"]) is dict


async def test_upload_init_and_sign_do_not_commit_or_refresh(monkeypatch):
    actor_id, project_id = uuid.uuid4(), uuid.uuid4()
    events = []

    class Session:
        async def commit(self):
            events.append("commit")

    async def make_execution(db, *, operation, actor, project_id, payload):
        if operation == "guide_asset_upload_init":
            data = {
                "key": f"projects/{project_id}/guide/{uuid.uuid4()}-hint.png",
                "upload_url": "https://storage.example.test/upload",
                "expires_in": 900,
            }
        else:
            data = {"url": "https://storage.example.test/download", "expires_in": 3600}
        return _guide_success(
            operation,
            actor.id,
            project_id,
            payload,
            data,
            refreshed=False,
        )[2]

    monkeypatch.setattr(guide_assets, "_execute_guide_operation", make_execution)

    init = await guide_assets.guide_asset_upload_init(
        GuideAssetUploadInitRequest(
            filename="hint.png", content_type="image/png", size=100
        ),
        project_id,
        Session(),
        _identity(actor_id),
    )
    sign = await guide_assets.guide_asset_sign_url(
        f"projects/{project_id}/guide/{uuid.uuid4()}-registered.png",
        project_id,
        Session(),
        _identity(actor_id),
    )

    assert init.expires_in == 900
    assert sign.expires_in == 3600
    assert events == []


async def test_borrowed_upload_commit_precedes_full_row_refresh_and_public_reply(
    monkeypatch,
):
    actor_id, project_id = uuid.uuid4(), uuid.uuid4()
    key = f"projects/{project_id}/guide/{uuid.uuid4()}-new.png"
    request, reply, execution = _guide_success(
        "guide_asset_upload_complete",
        actor_id,
        project_id,
        {"key": key, "original_name": "new.png", "content_type": "image/png"},
        {
            "key": key,
            "original_name": "new.png",
            "content_type": "image/png",
            "size": 100,
            "uploaded_at": datetime(2026, 10, 21, tzinfo=timezone.utc).isoformat(),
        },
        refreshed=False,
    )
    events = []

    class Session:
        async def commit(self):
            events.append("commit")

    class Manager:
        async def refresh_guide(self, observed):
            events.append("refresh")
            assert observed is request
            return {"id": str(project_id), "guide_assets": [key]}

    monkeypatch.setattr(
        guide_assets,
        "_execute_guide_operation",
        lambda *args, **kwargs: _return(execution),
    )
    monkeypatch.setattr(guide_assets, "configuration_manager", lambda data: Manager())

    response = await guide_assets.guide_asset_upload_complete(
        {"key": key, "original_name": "new.png", "content_type": "image/png"},
        project_id,
        Session(),
        _identity(actor_id),
    )

    assert events == ["commit", "refresh"]
    assert response.key == key
    assert response.size == 100
    assert execution.guide_result is reply


async def test_independent_upload_commit_skips_duplicate_refresh(monkeypatch):
    actor_id, project_id = uuid.uuid4(), uuid.uuid4()
    key = f"projects/{project_id}/guide/{uuid.uuid4()}-new.png"
    _request, _reply, execution = _guide_success(
        "guide_asset_upload_complete",
        actor_id,
        project_id,
        {"key": key, "original_name": "new.png", "content_type": "image/png"},
        {
            "key": key,
            "original_name": "new.png",
            "content_type": "image/png",
            "size": 100,
            "uploaded_at": datetime(2026, 10, 21, tzinfo=timezone.utc).isoformat(),
        },
        refreshed=True,
    )
    events = []

    class Session:
        async def commit(self):
            events.append("commit")

    class ForbiddenManager:
        async def refresh_guide(self, _request):
            raise AssertionError("An independently refreshed result must not reread")

    monkeypatch.setattr(
        guide_assets,
        "_execute_guide_operation",
        lambda *args, **kwargs: _return(execution),
    )
    monkeypatch.setattr(
        guide_assets, "configuration_manager", lambda data: ForbiddenManager()
    )

    response = await guide_assets.guide_asset_upload_complete(
        {"key": key, "original_name": "new.png", "content_type": "image/png"},
        project_id,
        Session(),
        _identity(actor_id),
    )

    assert events == ["commit"]
    assert response.key == key


async def test_postcommit_refresh_failure_keeps_commit_and_surfaces_plain_error(
    monkeypatch,
):
    actor_id, project_id = uuid.uuid4(), uuid.uuid4()
    key = f"projects/{project_id}/guide/{uuid.uuid4()}-new.png"
    request, _reply, execution = _guide_success(
        "guide_asset_upload_complete",
        actor_id,
        project_id,
        {"key": key, "original_name": "new.png", "content_type": "image/png"},
        {
            "key": key,
            "original_name": "new.png",
            "content_type": "image/png",
            "size": 100,
            "uploaded_at": datetime(2026, 10, 21, tzinfo=timezone.utc).isoformat(),
        },
        refreshed=False,
    )
    events = []

    class Session:
        async def commit(self):
            events.append("commit")

    class Manager:
        async def refresh_guide(self, observed):
            events.append("refresh")
            assert observed is request
            raise ConfigurationPostCommitFailure()

    monkeypatch.setattr(
        guide_assets,
        "_execute_guide_operation",
        lambda *args, **kwargs: _return(execution),
    )
    monkeypatch.setattr(guide_assets, "configuration_manager", lambda data: Manager())

    with pytest.raises(ConfigurationPostCommitFailure):
        await guide_assets.guide_asset_upload_complete(
            {"key": key, "original_name": "new.png", "content_type": "image/png"},
            project_id,
            Session(),
            _identity(actor_id),
        )
    assert events == ["commit", "refresh"]


async def _return(value):
    return value
