"""Caller-owned Workspace project maintenance bindings."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from aap_workspace.project_maintenance_contract import (
    PROTOCOL,
    ProjectMaintenanceUnavailable,
)
from app.db.models.project import Project
from app.services.project_workspace import (
    create_point_cloud_seed_project,
    create_import_project,
    find_import_project,
    set_point_cloud_quality_config,
    set_nuscenes_seed_defaults,
)
import pytest
from sqlalchemy import inspect


def _db(payload=None, *, autoflush=True):
    return SimpleNamespace(
        autoflush=autoflush,
        scalar=AsyncMock(return_value=payload),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )


def _import_project_reply(display_id, project_id):
    return {
        "schema": PROTOCOL,
        "display_id": display_id,
        "project_id": str(project_id) if project_id is not None else None,
    }


def _quality_write_reply(project_id, updated_at):
    return {
        "schema": PROTOCOL,
        "project_id": str(project_id),
        "updated_at": updated_at.isoformat(),
    }


def _assert_caller_owns_transaction(db):
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.parametrize("autoflush", [True, False])
async def test_import_lookup_preserves_autoflush_and_returns_only_the_project_id(
    autoflush,
):
    project_id = uuid.uuid4()
    display_id = "P-NU-雪山"
    db = _db(_import_project_reply(display_id, project_id), autoflush=autoflush)

    assert await find_import_project(db, display_id) == project_id

    assert db.flush.await_count == int(autoflush)
    query, params = db.scalar.await_args.args
    assert "aap_workspace_v1.read_import_project(" in str(query)
    assert params == {"display_id": display_id}
    _assert_caller_owns_transaction(db)


async def test_import_lookup_returns_none_for_a_valid_missing_reference():
    display_id = "P-NU-not-created"
    db = _db(_import_project_reply(display_id, None), autoflush=False)

    assert await find_import_project(db, display_id) is None

    db.flush.assert_not_awaited()
    _assert_caller_owns_transaction(db)


async def test_import_creator_flushes_and_binds_exact_project_identity(monkeypatch):
    project_id = uuid.UUID(int=45)
    owner_id = uuid.UUID(int=46)
    display_id = "P-NU-stable"
    db = _db(autoflush=False)
    calls = []

    async def flush():
        calls.append("flush")

    async def scalar(query, params):
        calls.append("write")
        assert "aap_workspace_v1.create_import_project(" in str(query)
        assert params["project_id"] == project_id
        assert params["display_id"] == display_id
        assert params["name"] == "nuScenes sample"
        assert params["owner_id"] == owner_id
        assert json.loads(params["tool_bindings"]) == {}
        return _import_project_reply(display_id, project_id)

    monkeypatch.setattr("app.services.project_workspace.uuid.uuid4", lambda: project_id)
    db.flush.side_effect = flush
    db.scalar.side_effect = scalar

    assert (
        await create_import_project(
            db,
            display_id=display_id,
            name="nuScenes sample",
            owner_id=owner_id,
            tool_bindings=None,
        )
        == project_id
    )

    assert calls == ["flush", "write"]
    _assert_caller_owns_transaction(db)


async def test_import_creator_rejects_a_reply_for_another_project(monkeypatch):
    requested_id = uuid.UUID(int=51)
    display_id = "P-NU-stable"
    db = _db(_import_project_reply(display_id, uuid.UUID(int=52)), autoflush=False)
    monkeypatch.setattr(
        "app.services.project_workspace.uuid.uuid4", lambda: requested_id
    )

    with pytest.raises(ProjectMaintenanceUnavailable):
        await create_import_project(
            db,
            display_id=display_id,
            name="nuScenes sample",
            owner_id=uuid.UUID(int=53),
            tool_bindings={"review": {"enabled": True}},
        )

    db.flush.assert_awaited_once()
    _assert_caller_owns_transaction(db)


async def test_import_lookup_rejects_malformed_owner_reply_without_recovery():
    db = _db(None)

    with pytest.raises(ProjectMaintenanceUnavailable):
        await find_import_project(db, "P-NU-stable")

    assert db.scalar.await_count == 1
    _assert_caller_owns_transaction(db)


async def test_quality_writer_updates_identity_map_and_keeps_caller_transaction():
    project_id = uuid.uuid4()
    updated_at = datetime(2026, 10, 22, 12, 30, tzinfo=UTC)
    config = {"enabled": True, "config_revision": 4}
    project = Project(
        id=project_id,
        point_cloud_quality_config={"enabled": False, "config_revision": 3},
    )
    db = _db(_quality_write_reply(project_id, updated_at), autoflush=False)
    calls = []

    async def flush():
        calls.append("flush")

    async def scalar(query, params):
        calls.append("write")
        assert "aap_workspace_v1.write_point_cloud_quality_config(" in str(query)
        assert params["project_id"] == project_id
        assert json.loads(params["config"]) == config
        return _quality_write_reply(project_id, updated_at)

    db.flush.side_effect = flush
    db.scalar.side_effect = scalar

    assert (
        await set_point_cloud_quality_config(db, project=project, config=config)
        == updated_at
    )

    assert calls == ["flush", "write"]
    assert project.point_cloud_quality_config == config
    assert project.updated_at == updated_at
    assert not inspect(project).attrs.point_cloud_quality_config.history.has_changes()
    assert not inspect(project).attrs.updated_at.history.has_changes()
    _assert_caller_owns_transaction(db)


async def test_quality_writer_rejects_an_unbound_write_reply():
    project_id = uuid.UUID(int=61)
    project = Project(
        id=project_id,
        point_cloud_quality_config={"enabled": False, "config_revision": 3},
    )
    db = _db(
        _quality_write_reply(uuid.UUID(int=62), datetime(2026, 10, 22, tzinfo=UTC)),
        autoflush=False,
    )

    with pytest.raises(ProjectMaintenanceUnavailable):
        await set_point_cloud_quality_config(
            db,
            project=project,
            config={"enabled": True, "config_revision": 4},
        )

    db.flush.assert_awaited_once()
    _assert_caller_owns_transaction(db)


@pytest.mark.parametrize("valid", [True, False])
async def test_seed_writer_synchronizes_cache_only_after_a_bound_reply(valid):
    from aap_workspace.seed_project_contract import PROTOCOL as SEED_PROTOCOL

    project_id = uuid.uuid4()
    timestamp = datetime(2026, 10, 23, tzinfo=UTC)
    project = Project(
        id=project_id,
        name="old",
        scene_mode=False,
        prefer_same_scene_continuation=False,
        tool_bindings={},
    )
    db = _db(
        {
            "schema": SEED_PROTOCOL,
            "project_id": str(project_id if valid else uuid.uuid4()),
            "updated_at": timestamp.isoformat(),
        },
        autoflush=False,
    )
    bindings = {"lidar_box_3d": {"enabled": True}}
    if valid:
        assert (
            await set_nuscenes_seed_defaults(
                db, project=project, name="scene", tool_bindings=bindings
            )
            == timestamp
        )
        for key, value in {
            "name": "scene",
            "scene_mode": True,
            "prefer_same_scene_continuation": True,
            "tool_bindings": bindings,
            "updated_at": timestamp,
        }.items():
            assert getattr(project, key) == value
            assert not inspect(project).attrs[key].history.has_changes()
    else:
        with pytest.raises(ProjectMaintenanceUnavailable):
            await set_nuscenes_seed_defaults(
                db, project=project, name="scene", tool_bindings=bindings
            )
        assert project.name == "old" and project.scene_mode is False
        assert project.tool_bindings == {}
    db.flush.assert_awaited_once()
    _assert_caller_owns_transaction(db)


async def test_directory_seed_creator_rejects_mismatched_id_without_committing(
    monkeypatch,
):
    from aap_workspace.seed_project_contract import PROTOCOL as SEED_PROTOCOL

    project_id = uuid.uuid4()
    monkeypatch.setattr("app.services.project_workspace.uuid.uuid4", lambda: project_id)
    db = _db(
        {
            "schema": SEED_PROTOCOL,
            "display_id": "P-seed",
            "project_id": str(uuid.UUID(int=1)),
        },
        autoflush=False,
    )
    with pytest.raises(ProjectMaintenanceUnavailable):
        await create_point_cloud_seed_project(
            db,
            display_id="P-seed",
            name="seed",
            owner_id=uuid.UUID(int=2),
            tool_bindings={},
        )
    db.flush.assert_awaited_once()
    _assert_caller_owns_transaction(db)
