"""Compare embedded Project class rename with frozen legacy behavior."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import os
import uuid

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.labeling_class_query_data import LabelingClassQueryData
from app.services.project_access import get_workspace_project
from tests import test_project_class_rename_legacy as legacy
from tests.labeling_class_query_helpers import (
    api_db_binding,
    clear_local_identity,
    make_request_runtime,
)
from tests.project_class_rename_owner_helpers import (
    aliased_uuid,
    cleanup_lock_world,
    cleanup_world,
    locks_fixture,
    override_rename_projection,
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

_LOCK_SCENARIOS = {
    "account_update_blocks_changed": ("users", "UPDATE", "changed"),
    "account_update_blocks_noop": ("users", "UPDATE", "noop"),
    "project_update_blocks_changed": ("projects", "UPDATE", "changed"),
    "project_key_share_allows_noop": ("projects", "KEY SHARE", "noop"),
    "project_key_share_blocks_upgrade": ("projects", "KEY SHARE", "changed"),
}
_RESPONSE_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")


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
    actual = legacy._normalise(_response_body(response), aliases)
    if response.status_code == 200:
        assert actual["id"] == expected["body"]["id"]
        assert actual["tool_bindings"] == expected["body"]["tool_bindings"]
    elif isinstance(expected["body"].get("detail"), dict):
        assert actual["detail"] == expected["body"]["detail"]


def _sample_path(sample: dict, aliases: dict[str, str]) -> str:
    return _denormalise(sample["request"]["path"], aliases)


def _business_snapshot(value, aliases):
    result = legacy._normalise(value, aliases)
    if result is not None:
        result["annotations"] = sorted(result["annotations"], key=lambda row: row["id"])
    return result


@contextmanager
def _observe_api_commits(runtime_engine):
    engine = runtime_engine.sync_engine
    state = {"count": 0, "active": False}

    def count_commit(_connection):
        if state["active"]:
            state["count"] += 1

    event.listen(engine, "commit", count_commit)
    try:
        yield state
    finally:
        event.remove(engine, "commit", count_commit)


async def _tracked_request(commit_state, request):
    commit_state["count"] = 0
    commit_state["active"] = True
    try:
        response = await request
    finally:
        commit_state["active"] = False
    return response, commit_state["count"]


async def _replay_lock_cases(client, test_engine, maker, world, fixture):
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
                response = await client.post(
                    f"{legacy._ROUTE}/{project_id}/classes/rename",
                    headers={"Authorization": "Bearer " + world["token"]},
                    json={
                        "old_name": "Car",
                        "new_name": new_name,
                        "tool_unit_id": "bbox",
                    },
                )
                _assert_response(sample["response"], response, aliases)
                current = await legacy._snapshot(maker, project_id)
                assert legacy._normalise(current, aliases) == legacy._normalise(
                    before, aliases
                )


async def _replay_http_samples(
    client, test_engine, maker, world, fixture, commit_state
):
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

        project_id = uuid.UUID(path.split("/")[4])
        fail_projection = sample["label"] == "commit_then_projection_unavailable"
        gate = (
            override_rename_projection(
                test_engine, project_id=project_id, statement="RETURN NULL"
            )
            if fail_projection
            else nullcontext()
        )
        async with gate:
            response, commits = await _tracked_request(
                commit_state,
                client.request(request["method"], path, headers=headers, json=body),
            )
        assert commits == sample["observation"]["commit_count"], sample["label"]
        if fail_projection:
            assert response.json() == sample["response"]["body"]

        _assert_response(sample["response"], response, aliases)
        if "post_response_state" in sample:
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
        configuration = await execute_configuration(
            caller,
            operation="update_project",
            actor_id=actor_id,
            target_id=project_id,
            payload={"annotation_guide": "caller-only guide update"},
        )
        assert configuration.resource_id == project_id
        result = await execute_configuration(
            caller,
            operation="rename_project_class",
            actor_id=actor_id,
            target_id=project_id,
            payload={"old_name": "Car", "new_name": "Truck", "tool_unit_id": "bbox"},
        )
        assert result.resource_id == project_id
        assert result.rename_result is not None and result.rename_result.changed
        assert caller.in_transaction()
        project = await get_workspace_project(
            caller, actor_id=actor_id, project_id=project_id
        )
        assert project.tool_bindings["bbox"]["classes"][0]["name"] == "Truck"
        assert project.annotation_guide == "caller-only guide update"
        assert await LabelingClassQueryData(caller).class_rows(project_id) == [
            ("Truck", 1)
        ]
        noop = await execute_configuration(
            caller,
            operation="rename_project_class",
            actor_id=actor_id,
            target_id=project_id,
            payload={"old_name": "Truck", "new_name": "Truck", "tool_unit_id": "bbox"},
        )
        assert noop.rename_result is not None and not noop.rename_result.changed
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


async def test_project_class_rename_matches_frozen_legacy_on_caller_session(
    test_engine, app_module, monkeypatch, tmp_path
):
    samples = samples_fixture()
    lock_samples = locks_fixture()
    world = await seed_legacy_world(test_engine, app_module, monkeypatch, tmp_path)
    lock_world = None
    runtime_engine = None
    try:
        lock_world = await seed_lock_world(test_engine)
        clear_local_identity(monkeypatch)
        runtime_engine, runtime_maker = await make_request_runtime(
            test_engine, application_name="class-rename-caller"
        )
        maker = async_sessionmaker(test_engine, expire_on_commit=False)
        async with api_db_binding(app_module, runtime_maker):
            transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://class-rename-owner-replay",
                timeout=30,
            ) as client:
                await _replay_lock_cases(
                    client, test_engine, maker, lock_world, lock_samples
                )
                with _observe_api_commits(runtime_engine) as commit_state:
                    await _replay_http_samples(
                        client, test_engine, maker, world, samples, commit_state
                    )
                await _verify_borrowed_caller_rollback(maker, runtime_maker, world)
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
