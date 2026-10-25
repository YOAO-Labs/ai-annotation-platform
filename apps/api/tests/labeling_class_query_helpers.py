"""Seed-only legacy scenarios and current Labeling query-owner test bindings."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID

import httpx
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from tests import test_labeling_class_usage_reads_legacy as legacy_capture
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import _port, _start
from tests.test_module_data_roles import _role_url
from tests.workspace_project_query_helpers import (
    api_project_query_binding,
    project_query_dsn,
    project_query_provider,
)


ROOT = Path(__file__).resolve().parents[3]
FIXTURE_PATH = (
    Path(__file__).parent / "fixtures/labeling_class_usage_reads_legacy/samples.json"
)
FIXTURE_SHA256 = "c2e4517d3f103156c20c0eda0f52f804601073bf3e73d0c91e62c8b7176e647e"
RESPONSE_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
CLASS_QUERY_LOCK_KEY = 74185296301234567
_MISSING = object()


class _SeedOnlyComplete(Exception):
    """Stop the legacy capture after its committed seed, before an HTTP request."""


def fixture_data() -> dict:
    raw = FIXTURE_PATH.read_bytes()
    import hashlib

    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256
    value = json.loads(raw)
    assert value["capture_kind"] == "actual-legacy-labeling-class-usage-http"
    assert len(value["samples"]) == 16
    return value


async def seed_legacy_world(test_engine, app_module, monkeypatch, tmp_path) -> dict:
    """Reuse the frozen capture's committed seed, without recapturing or writing."""
    captured: dict = {}
    preserve_seed = {"value": False}
    original_cleanup = legacy_capture._cleanup_owned

    async def stop_before_request(
        _client,
        capture,
        _state,
        aliases,
        actors,
        tokens,
        token_roles,
        **_kwargs,
    ):
        preserve_seed["value"] = True
        captured.update(
            capture=capture,
            aliases=dict(aliases),
            actors=dict(actors),
            tokens=dict(tokens),
            token_roles=dict(token_roles),
        )
        raise _SeedOnlyComplete

    async def cleanup_unless_captured(maker, *, actor_ids, project_ids, task_ids):
        if preserve_seed["value"]:
            return
        await original_cleanup(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            task_ids=task_ids,
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            legacy_capture,
            "_capture_path",
            lambda: tmp_path / "seed-only-no-output.json",
        )
        patch.setattr(
            legacy_capture, "_source_metadata", lambda: {"seed_only_reuse": True}
        )
        patch.setattr(
            legacy_capture,
            "_write_capture",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("seed-only replay must not write a capture")
            ),
        )
        patch.setattr(legacy_capture, "_request", stop_before_request)
        patch.setattr(legacy_capture, "_cleanup_owned", cleanup_unless_captured)
        try:
            await legacy_capture.test_capture_legacy_labeling_class_usage(
                test_engine, app_module, monkeypatch
            )
        except _SeedOnlyComplete:
            pass

    try:
        assert captured, "legacy seed harness did not reach its first request"
        assert captured["capture"]["samples"] == []
        required_actors = {
            sample["request"]["actor"]["alias"] for sample in fixture_data()["samples"]
        }
        assert required_actors <= set(captured["actors"])
        eligibility = captured["capture"]["observations"]["seed_eligibility"]
        assert eligibility["invalid_platform_role"]["seeded"]
        assert eligibility["invalid_project_membership_role"]["seeded"]
        assert eligibility["foreign_task_with_target_annotation_project"]["seeded"]
        assert eligibility["large_json_numeric_decode_case"]["seeded"]
        return captured
    except BaseException:
        if captured:
            await cleanup_world(test_engine, captured)
        raise


def aliased_uuid(world: dict, alias: str) -> UUID:
    target = f"${alias}"
    for value, current_alias in world["aliases"].items():
        if current_alias == target:
            return UUID(value)
    raise AssertionError(f"missing seeded UUID alias {target}")


def world_cleanup_ids(world: dict) -> dict[str, list[UUID]]:
    return {
        "actor_ids": [actor.id for actor in world["actors"].values()],
        "project_ids": [
            UUID(value)
            for value, alias in world["aliases"].items()
            if alias.startswith("$project_")
            and not alias.startswith("$project_display_")
        ],
        "task_ids": [
            UUID(value)
            for value, alias in world["aliases"].items()
            if alias.startswith("$task_") and not alias.startswith("$task_display_")
        ],
    }


async def cleanup_world(test_engine, world: dict) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    await legacy_capture._cleanup_owned(maker, **world_cleanup_ids(world))


async def make_request_runtime(test_engine, *, application_name: str):
    url = legacy_capture._legacy_runtime_url(test_engine)
    engine = create_async_engine(
        url,
        hide_parameters=True,
        connect_args={"server_settings": {"application_name": application_name}},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def api_db_binding(app_module, maker):
    from app.deps import get_db

    previous = app_module.dependency_overrides.get(get_db, _MISSING)

    async def request_db():
        async with maker() as db:
            yield db

    app_module.dependency_overrides[get_db] = request_db
    try:
        yield
    finally:
        if previous is _MISSING:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous


def clear_local_identity(monkeypatch) -> None:
    for name in (
        "identity_url",
        "identity_auth_database_url",
        "identity_auth_database_url_file",
    ):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "audit_async", True)
    monkeypatch.setattr("app.middleware.audit._enqueue_audit", lambda _payload: True)


def labeling_class_query_dsn(test_engine) -> str:
    raw = _role_url(
        "AAP_MODULE_DATA_LABELING_CLASS_QUERY_URL", "labeling_class_query_client"
    )
    parsed = make_url(raw)
    assert parsed.username == "labeling_class_query_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    assert parsed.database and parsed.database.endswith("_test")
    _verify_postgres(parsed)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def installed_workspace_query_provider(dsn, token):
    interpreter = Path(os.environ["AAP_WORKSPACE_QUERY_PYTHON"])
    assert interpreter.is_file()
    previous = os.environ.get("AAP_WORKSPACE_RUNTIME_PYTHON")
    os.environ["AAP_WORKSPACE_RUNTIME_PYTHON"] = str(interpreter)
    try:
        async with project_query_provider(dsn, token) as url:
            yield url
    finally:
        if previous is None:
            os.environ.pop("AAP_WORKSPACE_RUNTIME_PYTHON", None)
        else:
            os.environ["AAP_WORKSPACE_RUNTIME_PYTHON"] = previous


@asynccontextmanager
async def installed_labeling_query_provider(dsn, token):
    assert os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") == "1"
    parsed = make_url(dsn)
    assert parsed.username == "labeling_class_query_client"
    assert parsed.database and parsed.database.endswith("_test")
    _verify_postgres(parsed)
    interpreter = Path(os.environ["AAP_LABELING_CLASS_QUERY_PYTHON"])
    assert interpreter.is_file()
    with tempfile.TemporaryDirectory(
        prefix="labeling-class-query-", dir=ROOT / ".worktree"
    ) as temporary:
        directory = Path(temporary)
        port = _port()
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LABELING_CLASS_QUERY_DATABASE_URL": dsn,
            "LABELING_CLASS_QUERY_CONTROL_TOKEN": token,
            "LABELING_CLASS_QUERY_HOST": "127.0.0.1",
            "LABELING_CLASS_QUERY_PORT": str(port),
            "LABELING_CLASS_QUERY_TIMEOUT_SECONDS": "20",
        }
        owner = await asyncio.to_thread(
            _start,
            [str(interpreter), "-I", "-m", "aap_labeling"],
            environment,
            directory,
            directory / "owner.log",
            port,
            "/ready",
        )
        try:
            yield owner.origin
        finally:
            owner.stop()


@asynccontextmanager
async def bind_query_owners(
    stack: AsyncExitStack,
    test_engine,
    *,
    binding: str,
    workspace_token: str,
    labeling_token: str,
):
    workspace_url = workspace_dsn = None
    labeling_url = labeling_dsn = None
    if binding == "dedicated":
        workspace_dsn = project_query_dsn(test_engine)
        labeling_dsn = labeling_class_query_dsn(test_engine)
    elif binding == "http":
        workspace_url = await stack.enter_async_context(
            installed_workspace_query_provider(
                project_query_dsn(test_engine), workspace_token
            )
        )
        labeling_url = await stack.enter_async_context(
            installed_labeling_query_provider(
                labeling_class_query_dsn(test_engine), labeling_token
            )
        )
    elif binding != "borrowed":
        raise AssertionError(f"unknown owner binding: {binding}")

    changes = {
        "labeling_class_query_url": labeling_url or "",
        "labeling_class_query_database_url": labeling_dsn or "",
        "labeling_class_query_database_url_file": "",
        "labeling_class_query_control_token": labeling_token if labeling_url else "",
        "labeling_class_query_control_token_file": "",
        "labeling_class_query_timeout_seconds": 20,
    }
    original = {name: getattr(settings, name) for name in changes}
    try:
        async with api_project_query_binding(
            url=workspace_url,
            dsn=workspace_dsn,
            token=workspace_token,
        ):
            for name, value in changes.items():
                setattr(settings, name, value)
            try:
                yield
            finally:
                for name, value in original.items():
                    setattr(settings, name, value)
    finally:
        for name, value in original.items():
            setattr(settings, name, value)


@asynccontextmanager
async def bound_api_client(
    test_engine,
    app_module,
    monkeypatch,
    *,
    binding: str,
    workspace_token: str,
    labeling_token: str,
    application_name: str = "labeling-class-query-replay",
):
    clear_local_identity(monkeypatch)
    runtime_engine, runtime_maker = await make_request_runtime(
        test_engine, application_name=application_name
    )
    try:
        async with api_db_binding(app_module, runtime_maker):
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(
                    bind_query_owners(
                        stack,
                        test_engine,
                        binding=binding,
                        workspace_token=workspace_token,
                        labeling_token=labeling_token,
                    )
                )
                transport = httpx.ASGITransport(
                    app=app_module, raise_app_exceptions=False
                )
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test", timeout=30
                ) as client:
                    if binding == "borrowed":
                        with observe_borrowed_labeling_rpc(runtime_engine) as rpc:
                            yield client, runtime_engine, rpc
                    else:
                        yield client, runtime_engine, None
    finally:
        await runtime_engine.dispose()


def _request_path(frozen: dict, aliases: dict[str, str]) -> str:
    path = frozen["request"]["path"]
    for actual, alias in aliases.items():
        path = path.replace(alias, actual)
    return path


def _public_sample(label: str, path: str, actor_alias: str, response) -> dict:
    try:
        body = response.json() if response.content else None
    except ValueError:
        body = response.text
    return {
        "label": label,
        "request": {"method": "GET", "path": path, "actor_alias": actor_alias},
        "response": {
            "status": response.status_code,
            "headers": {
                name: response.headers[name]
                for name in RESPONSE_HEADERS
                if name in response.headers
            },
            "body": legacy_capture._normalise(body, {}),
        },
    }


async def request_sample(
    client: httpx.AsyncClient,
    world: dict,
    frozen: dict,
    *,
    native_rpc=None,
) -> dict:
    actor_alias = frozen["request"]["actor"]["alias"]
    path = _request_path(frozen, world["aliases"])
    if native_rpc is not None:
        native_rpc["current"] = []
    try:
        response = await client.get(
            path,
            headers={"Authorization": f"Bearer {world['tokens'][actor_alias]}"},
        )
    finally:
        phases = None if native_rpc is None else native_rpc["current"]
        if native_rpc is not None:
            native_rpc["current"] = None
    sample = _public_sample(
        frozen["label"],
        legacy_capture._normalise(path, world["aliases"]),
        actor_alias,
        response,
    )
    if phases is not None:
        sample["borrowed_labeling_rpc_phases"] = list(phases)
    return sample


@contextmanager
def observe_borrowed_labeling_rpc(engine):
    state = {"current": None}

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        current = state["current"]
        if current is None:
            return
        compact = " ".join(statement.casefold().split())
        if "aap_labeling_class_query_v1.class_query_policy()" in compact:
            current.append("class_query_policy")
        elif "aap_labeling_class_query_v1.class_counts(" in compact:
            current.append("class_counts")
        elif "aap_labeling_class_query_v1.attribute_rows(" in compact:
            current.append("attribute_rows")

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        yield state
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)


async def add_frozen_interleave_row(test_engine, world: dict) -> UUID:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as writer:
        annotation = await legacy_capture._create_annotation(
            writer,
            project_id=aliased_uuid(world, "project_target"),
            task_id=aliased_uuid(world, "task_target"),
            user_id=world["actors"]["owner"].id,
            class_name="",
            attributes={"between_reads": "committed second session"},
        )
        return annotation.id


async def _wait_for_blocked_class_query(test_engine, timeout_seconds=20) -> dict:
    statement = text(
        "SELECT pid, application_name, wait_event_type, wait_event, query "
        "FROM pg_catalog.pg_stat_activity "
        "WHERE datname=current_database() AND pid <> pg_backend_pid() "
        "AND wait_event_type='Lock' "
        "AND query ILIKE '%aap_labeling_class_query_v1.class_counts%' "
        "ORDER BY query_start DESC LIMIT 1"
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    async with test_engine.connect() as observer:
        while loop.time() < deadline:
            cached = (await observer.execute(statement)).mappings().first()
            await observer.execute(text("SELECT pg_catalog.pg_stat_clear_snapshot()"))
            row = (await observer.execute(statement)).mappings().first()
            if row is not None:
                return {
                    **dict(row),
                    "stats_snapshot_refreshed": True,
                    "cached_snapshot_missed_rpc": cached is None,
                }
            await asyncio.sleep(0.05)
    raise AssertionError("no class_counts RPC blocked on the test advisory lock")


async def assert_owned_test_database(test_engine) -> dict:
    runtime_url = legacy_capture._legacy_runtime_url(test_engine)
    parsed = make_url(runtime_url)
    _verify_postgres(parsed)
    async with test_engine.connect() as connection:
        current_user, database, can_create = (
            await connection.execute(
                text(
                    "SELECT current_user, current_database(), "
                    "pg_catalog.has_schema_privilege(current_user, "
                    "'aap_labeling_class_query_v1', 'CREATE')"
                )
            )
        ).one()
    assert database == test_engine.url.database and database.endswith("_test")
    assert current_user not in {
        "legacy_runtime",
        "labeling_class_query_client",
        "workspace_project_query_client",
    }
    assert can_create
    return {"database": database, "owner_role": current_user}


async def _function_metadata(connection) -> tuple[int, str | None, int, str]:
    row = (
        (
            await connection.execute(
                text(
                    "SELECT proc.oid, proc.proacl::text AS acl, proc.proowner, "
                    "pg_catalog.pg_get_functiondef(proc.oid) AS definition "
                    "FROM pg_catalog.pg_proc AS proc "
                    "WHERE proc.oid = CAST(:signature AS regprocedure)"
                ),
                {"signature": "aap_labeling_class_query_v1.class_counts(uuid)"},
            )
        )
        .mappings()
        .one()
    )
    return row["oid"], row["acl"], row["proowner"], row["definition"]


@asynccontextmanager
async def gated_class_counts(test_engine):
    await assert_owned_test_database(test_engine)
    async with test_engine.connect() as connection:
        (
            original_oid,
            original_acl,
            original_owner,
            original_definition,
        ) = await _function_metadata(connection)
    needle = "    GROUP BY annotation.class_name;\nEND;"
    assert original_definition.count(needle) == 1
    patched_definition = original_definition.replace(
        needle,
        "    GROUP BY annotation.class_name;\n\n"
        f"    PERFORM pg_catalog.pg_advisory_xact_lock({CLASS_QUERY_LOCK_KEY}::bigint);\n"
        "END;",
        1,
    )
    try:
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(patched_definition)
            oid, acl, owner, _definition = await _function_metadata(connection)
            assert (oid, acl, owner) == (original_oid, original_acl, original_owner)
        yield
    finally:
        async with test_engine.begin() as connection:
            await connection.exec_driver_sql(original_definition)
            oid, acl, owner, definition = await _function_metadata(connection)
            assert (oid, acl, owner, definition) == (
                original_oid,
                original_acl,
                original_owner,
                original_definition,
            )


async def run_read_committed_phase_probe(
    test_engine,
    client: httpx.AsyncClient,
    world: dict,
    *,
    actor_alias: str = "owner",
) -> dict:
    project_id = aliased_uuid(world, "project_target")
    task_id = aliased_uuid(world, "task_target")
    user_id = world["actors"][actor_alias].id
    request_task = None
    async with test_engine.connect() as gate:
        await gate.scalar(
            text("SELECT pg_catalog.pg_advisory_lock(:lock_key)"),
            {"lock_key": CLASS_QUERY_LOCK_KEY},
        )
        await gate.commit()
        try:
            request_task = asyncio.create_task(
                client.get(
                    f"/api/v1/projects/{project_id}/class-usage",
                    headers={"Authorization": f"Bearer {world['tokens'][actor_alias]}"},
                )
            )
            blocked = await _wait_for_blocked_class_query(test_engine)
            async with async_sessionmaker(
                test_engine, expire_on_commit=False
            ).begin() as writer:
                await legacy_capture._create_annotation(
                    writer,
                    project_id=project_id,
                    task_id=task_id,
                    user_id=user_id,
                    class_name="phase-new-class",
                    attributes={"phase_committed_key": "visible to second read"},
                )
            unlocked = await gate.scalar(
                text("SELECT pg_catalog.pg_advisory_unlock(:lock_key)"),
                {"lock_key": CLASS_QUERY_LOCK_KEY},
            )
            assert unlocked is True
            await gate.commit()
            response = await asyncio.wait_for(request_task, timeout=30)
            return {
                "status": response.status_code,
                "headers": {
                    name: response.headers[name]
                    for name in RESPONSE_HEADERS
                    if name in response.headers
                },
                "body": response.json(),
                "blocked_rpc": blocked,
            }
        finally:
            still_locked = await gate.scalar(
                text("SELECT pg_catalog.pg_advisory_unlock(:lock_key)"),
                {"lock_key": CLASS_QUERY_LOCK_KEY},
            )
            if still_locked:
                await gate.commit()
            if request_task is not None and not request_task.done():
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
