"""Replay frozen Project class-rename behavior across Configuration owners."""

from __future__ import annotations

from contextlib import contextmanager
import os
import secrets
import uuid

import httpx
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models.annotation import Annotation
from app.db.models.project import Project
from tests import test_project_class_rename_legacy as legacy
from tests.labeling_class_query_helpers import (
    api_db_binding,
    make_request_runtime,
)
from tests.project_class_rename_owner_helpers import (
    aliased_uuid,
    class_rename_binding,
    cleanup_lock_world,
    cleanup_world,
    locks_fixture,
    samples_fixture,
    seed_lock_world,
    seed_legacy_world,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_PROJECT_CLASS_RENAME_OWNER_REPLAY") != "1",
        reason="requires the explicit disposable class-rename owner replay",
    ),
]

_MODES = ("borrowed", "dedicated", "http")
_LOCK_SCENARIOS = {
    "account_update_blocks_changed": ("users", "UPDATE", "changed"),
    "account_update_blocks_noop": ("users", "UPDATE", "noop"),
    "project_update_blocks_changed": ("projects", "UPDATE", "changed"),
    "project_key_share_allows_noop": ("projects", "KEY SHARE", "noop"),
    "project_key_share_blocks_upgrade": ("projects", "KEY SHARE", "changed"),
}
_RESPONSE_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_CHANGED_LABELS = {
    "scoped_rename_cross_unit_annotations",
    "all_units_including_disabled",
    "commit_then_projection_unavailable",
}
_NOOP_LABELS = {
    "noop_unknown_tool_unit",
    "noop_missing_old_class",
    "super_admin_noop",
}


def _denormalise(value, aliases: dict[str, str]):
    if isinstance(value, str):
        for actual, alias in sorted(
            aliases.items(), key=lambda item: len(item[1]), reverse=True
        ):
            value = value.replace(alias, actual)
        return value
    if isinstance(value, list):
        return [_denormalise(item, aliases) for item in value]
    if isinstance(value, dict):
        return {key: _denormalise(item, aliases) for key, item in value.items()}
    return value


def _response_body(response):
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _assert_response(expected: dict, response: httpx.Response, aliases: dict[str, str]):
    actual_headers = {
        name: response.headers[name]
        for name in _RESPONSE_HEADERS
        if name in response.headers
    }
    assert response.status_code == expected["status"]
    assert actual_headers == expected["headers"]
    assert legacy._normalise(_response_body(response), aliases) == expected["body"]


def _sample_path(sample: dict, aliases: dict[str, str]) -> str:
    return _denormalise(sample["request"]["path"], aliases)


def _business_snapshot(value, aliases):
    result = legacy._normalise(value, aliases)
    if result is not None:
        # The internal capture sorted by freshly generated row UUIDs. Their
        # relative order is not a public behavior; compare every frozen row by
        # its stable seed alias while preserving each row's complete contents.
        result["annotations"] = sorted(result["annotations"], key=lambda row: row["id"])
    return result


@contextmanager
def _observe_api_commits(engine):
    state = {"active": False, "count": 0}

    def after_commit(session):
        if state["active"] and session.bind is engine.sync_engine:
            state["count"] += 1

    event.listen(Session, "after_commit", after_commit)
    try:
        yield state
    finally:
        event.remove(Session, "after_commit", after_commit)


async def _tracked_request(commit_state, awaitable):
    commit_state["count"] = 0
    commit_state["active"] = True
    try:
        response = await awaitable
    finally:
        commit_state["active"] = False
    return response, commit_state["count"]


async def _replay_lock_cases(client, test_engine, maker, world, fixture, commit_state):
    aliases = world["aliases"]
    project_id = world["project_id"]
    actor_id = world["actor_id"]
    before = await legacy._snapshot(maker, project_id)
    assert before["tool_bindings"]["bbox"]["classes"][0]["name"] == "Car"
    assert all(
        row["class_name"] == "Car"
        for row in before["annotations"]
        if row["tool_unit_id"] == "bbox"
    )

    for sample in fixture["samples"]:
        table, lock_mode, operation = _LOCK_SCENARIOS[sample["label"]]
        assert sample["held_row"] == {"table": table, "mode": lock_mode}
        target_id = actor_id if table == "users" else project_id
        new_name = "Car" if operation == "noop" else "Truck"
        async with test_engine.connect() as holder:
            async with holder.begin():
                locked = await holder.scalar(
                    text(f"SELECT id FROM public.{table} WHERE id=:id FOR {lock_mode}"),
                    {"id": target_id},
                )
                assert locked == target_id
                response, api_commits = await _tracked_request(
                    commit_state,
                    client.post(
                        f"{legacy._ROUTE}/{project_id}/classes/rename",
                        headers={"Authorization": "Bearer " + world["token"]},
                        json={
                            "old_name": "Car",
                            "new_name": new_name,
                            "tool_unit_id": "bbox",
                        },
                    ),
                )
                _assert_response(sample["response"], response, aliases)
                assert api_commits == 0, sample["label"]
                current = await legacy._snapshot(maker, project_id)
                assert legacy._normalise(current, aliases) == legacy._normalise(
                    before, aliases
                )


async def _replay_http_samples(client, maker, world, fixture, commit_state):
    aliases = world["aliases"]
    for sample in fixture["samples"]:
        request = sample["request"]
        actor = request["actor"]
        token_alias = actor["alias"]
        if actor["token_role"] != actor["current_role"]:
            token_alias = f"{token_alias}_stale_role_claim"
        headers = {"Authorization": f"Bearer {world['tokens'][token_alias]}"}
        body = _denormalise(request["body"], aliases)
        path = _sample_path(sample, aliases)

        previous_query = None
        if sample["label"] == "commit_then_projection_unavailable":
            previous_query = {
                name: getattr(settings, name)
                for name in (
                    "workspace_project_query_url",
                    "workspace_project_query_database_url",
                    "workspace_project_query_database_url_file",
                    "workspace_membership_control_token",
                )
            }
            settings.workspace_project_query_url = "http://127.0.0.1:1"
            settings.workspace_project_query_database_url = ""
            settings.workspace_project_query_database_url_file = ""
            if not settings.workspace_membership_control_token:
                settings.workspace_membership_control_token = secrets.token_urlsafe(24)
        try:
            response, api_commits = await _tracked_request(
                commit_state,
                client.request(request["method"], path, headers=headers, json=body),
            )
        finally:
            if previous_query is not None:
                for name, value in previous_query.items():
                    setattr(settings, name, value)

        _assert_response(sample["response"], response, aliases)
        if sample["label"] in _CHANGED_LABELS:
            assert api_commits == 1, sample["label"]
        elif sample["label"] in _NOOP_LABELS:
            assert api_commits == 0, sample["label"]
        if "post_response_state" in sample:
            project_id = uuid.UUID(path.split("/")[4])
            current = await legacy._snapshot(maker, project_id)
            assert _business_snapshot(current, aliases) == _business_snapshot(
                sample["post_response_state"], {}
            ), sample["label"]


async def _verify_borrowed_caller_rollback(maker, runtime_maker, world):
    from app.services.workspace_configuration import execute_configuration

    project_id = aliased_uuid(world, "project_caller_rollback")
    actor_id = world["actors"]["owner"].id
    before = await legacy._snapshot(maker, project_id)
    assert before["tool_bindings"]["bbox"]["classes"][0]["name"] == "Car"
    async with runtime_maker() as caller:
        result = await execute_configuration(
            caller,
            operation="rename_project_class",
            actor_id=actor_id,
            target_id=project_id,
            payload={"old_name": "Car", "new_name": "Truck", "tool_unit_id": "bbox"},
        )
        assert result.resource_id == project_id
        assert caller.in_transaction()
        project = await caller.scalar(
            select(Project)
            .where(Project.id == project_id)
            .execution_options(populate_existing=True)
        )
        annotations = (
            await caller.scalars(
                select(Annotation)
                .where(Annotation.project_id == project_id)
                .order_by(Annotation.id)
            )
        ).all()
        assert project.tool_bindings["bbox"]["classes"][0]["name"] == "Truck"
        assert [row.class_name for row in annotations] == ["Truck"]
        await caller.flush()
        assert legacy._normalise(
            await legacy._snapshot(maker, project_id), world["aliases"]
        ) == legacy._normalise(before, world["aliases"])
        await caller.rollback()
        assert not caller.in_transaction()

    after = await legacy._snapshot(maker, project_id)
    assert legacy._normalise(after, world["aliases"]) == legacy._normalise(
        before, world["aliases"]
    )


@pytest.mark.parametrize("mode", _MODES)
async def test_project_class_rename_matches_frozen_legacy_across_owners(
    mode, test_engine, app_module, monkeypatch, tmp_path
):
    samples = samples_fixture()
    lock_samples = locks_fixture()
    world = await seed_legacy_world(test_engine, app_module, monkeypatch, tmp_path)
    lock_world = None
    runtime_engine = None
    try:
        lock_world = await seed_lock_world(test_engine)
        async with class_rename_binding(
            monkeypatch, test_engine, mode=mode, tmp_path=tmp_path
        ) as binding:
            assert binding.mode == mode
            assert bool(binding.url) == (mode == "http")
            runtime_engine, runtime_maker = await make_request_runtime(
                test_engine,
                application_name=f"class-rename-owner-{mode}",
            )
            maker = async_sessionmaker(test_engine, expire_on_commit=False)
            async with api_db_binding(app_module, runtime_maker):
                transport = httpx.ASGITransport(
                    app=app_module, raise_app_exceptions=False
                )
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://class-rename-owner-replay",
                    timeout=30,
                ) as client:
                    with _observe_api_commits(runtime_engine) as commit_state:
                        await _replay_lock_cases(
                            client,
                            test_engine,
                            maker,
                            lock_world,
                            lock_samples,
                            commit_state,
                        )
                        await _replay_http_samples(
                            client, maker, world, samples, commit_state
                        )
                        if mode == "borrowed":
                            await _verify_borrowed_caller_rollback(
                                maker, runtime_maker, world
                            )
        assert len(samples["samples"]) == 21
        assert len(lock_samples["samples"]) == 5
    finally:
        try:
            if runtime_engine is not None:
                await runtime_engine.dispose()
        finally:
            try:
                if lock_world is not None:
                    await cleanup_lock_world(test_engine, lock_world)
            finally:
                await cleanup_world(test_engine, world)
