"""Capture legacy PostgreSQL temporal decoding and Project read failures once."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import create_access_token
from tests.factory import create_project, create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_PROJECT_QUERY_TEMPORAL_CAPTURE") != "1",
        reason="requires the explicit owned legacy Project temporal capture",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "86f4ff527446a1932e585f6fda381520860161c9"
_CAPTURE_PATH = _ROOT / ".worktree/workspace-project-temporal-extremes-legacy.json"
_PROJECTS_PATH = "/api/v1/projects"
_FIXED_DATE = "DATE '2000-01-02'"
_FIXED_TIMESTAMP = "TIMESTAMPTZ '2000-01-02 03:04:05+00'"
_SOURCE_FILES = (
    "apps/api/app/api/v1/projects.py",
    "apps/api/app/db/models/project.py",
    "apps/api/app/db/models/user.py",
    "apps/api/app/schemas/project.py",
    "apps/api/app/schemas/user.py",
    "apps/api/app/deps.py",
    "apps/api/app/core/security.py",
    "apps/api/app/services/identity_contracts.py",
    "apps/api/app/services/identity_gateway.py",
    "apps/api/app/services/identity_session_data.py",
    "apps/api/app/services/project_access.py",
    "apps/api/app/services/user_brief.py",
    "apps/identity/src/aap_identity/contract.py",
    "apps/identity/src/aap_identity/actor_contract.py",
    "apps/identity/src/aap_identity/actor_reader.py",
    "apps/identity/src/aap_identity/authenticator.py",
    "apps/identity/src/aap_identity/postgres.py",
    "apps/workspace/src/aap_workspace/access_contract.py",
    "apps/workspace/src/aap_workspace/configuration_values.py",
    "apps/_shared/workspace_contract/src/aap_workspace_contract/__init__.py",
    "apps/_shared/workspace_contract/src/aap_workspace_contract/projections.py",
)
_DATE_VALUES = (
    ("python_min", "DATE '0001-01-01'"),
    ("python_max", "DATE '9999-12-31'"),
    ("finite_before_python_min", "DATE '0001-01-01 BC'"),
    ("finite_after_python_max", "DATE '10000-01-01'"),
    ("negative_infinity", "DATE '-infinity'"),
    ("positive_infinity", "DATE 'infinity'"),
)
_TIMESTAMP_VALUES = (
    ("python_min", "TIMESTAMPTZ '0001-01-01 00:00:00+00'"),
    ("python_max", "TIMESTAMPTZ '9999-12-31 23:59:59.999999+00'"),
    ("finite_before_python_min", "TIMESTAMPTZ '0001-01-01 00:00:00 BC'"),
    ("finite_after_python_max", "TIMESTAMPTZ '10000-01-01 00:00:00+00'"),
    ("negative_infinity", "TIMESTAMPTZ '-infinity'"),
    ("positive_infinity", "TIMESTAMPTZ 'infinity'"),
)
_COLUMN_NAMES = {"due_date", "created_at", "updated_at"}
_RELATION_REF = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INTO)\s+(?:ONLY\s+)?"
    r"(?:(?:\"?[a-z_][a-z_0-9]*\"?)\.)?\"?([a-z_][a-z_0-9]*)\"?",
    re.IGNORECASE,
)
_FUNCTION_REF = re.compile(
    r"\b((?:aap_[a-z_0-9]+\.)+[a-z_][a-z_0-9]*)\s*\(", re.IGNORECASE
)


def _legacy_runtime_url(test_engine) -> str:
    value = os.environ.get("AAP_MODULE_DATA_LEGACY_RUNTIME_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.username == "legacy_runtime"
    assert parsed.database and parsed.database.endswith("_test")
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False
    )


def _source_metadata() -> dict:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    for relative in _SOURCE_FILES:
        baseline = subprocess.run(
            ["git", "show", f"{_BASE_COMMIT}:{relative}"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
        ).stdout
        assert baseline == (_ROOT / relative).read_bytes(), relative
    return {
        "captured_head": head,
        "pre_port_base": _BASE_COMMIT,
        "capture_driver_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "runtime_source_sha256": {
            relative: hashlib.sha256((_ROOT / relative).read_bytes()).hexdigest()
            for relative in _SOURCE_FILES
        },
    }


def _error_record(error: BaseException) -> dict:
    chain = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(
            {
                "class": type(current).__name__,
                "message": str(current),
                "sqlstate": getattr(current, "sqlstate", None),
            }
        )
        current = (
            getattr(current, "orig", None) or current.__cause__ or current.__context__
        )
    return {
        "class": type(error).__name__,
        "message": str(error),
        "sqlstate": getattr(error, "sqlstate", None)
        or getattr(getattr(error, "orig", None), "sqlstate", None),
        "chain": chain,
    }


def _driver_value(value) -> dict:
    return {
        "outcome": "value",
        "python_type": type(value).__name__,
        "value": value.isoformat() if hasattr(value, "isoformat") else str(value),
    }


async def _set_read_gucs(db) -> None:
    await db.execute(text("SET LOCAL DateStyle = 'ISO, YMD'"))
    await db.execute(text("SET LOCAL TIME ZONE 'UTC'"))


async def _probe_expression(maker, *, label: str, expression: str) -> dict:
    binary: dict
    async with maker() as db:
        await _set_read_gucs(db)
        try:
            value = await db.scalar(text(f"SELECT {expression} AS temporal_value"))
        except Exception as error:
            binary = {"outcome": "error", "error": _error_record(error)}
            try:
                await db.rollback()
            except Exception as rollback_error:
                binary["rollback_error"] = _error_record(rollback_error)
        else:
            binary = _driver_value(value)
            await db.rollback()

    server_encodings: dict
    async with maker() as db:
        await _set_read_gucs(db)
        try:
            row = (
                await db.execute(
                    text(
                        f"SELECT ({expression})::text AS text_value, "
                        f"pg_catalog.to_jsonb({expression})::text AS json_text"
                    )
                )
            ).one()
        except Exception as error:
            server_encodings = {"outcome": "error", "error": _error_record(error)}
            try:
                await db.rollback()
            except Exception as rollback_error:
                server_encodings["rollback_error"] = _error_record(rollback_error)
        else:
            raw_json = row._mapping["json_text"]
            try:
                parsed_json = json.loads(raw_json)
            except (TypeError, ValueError):
                parsed_json = None
            server_encodings = {
                "outcome": "value",
                "text": row._mapping["text_value"],
                "json_text": raw_json,
                "json_value": parsed_json,
            }
            await db.rollback()

    return {
        "label": label,
        "expression": expression,
        "binary_driver": binary,
        "server_text_and_json": server_encodings,
    }


async def _probe_project_column(maker, *, project_id, column: str) -> dict:
    assert column in _COLUMN_NAMES
    result: dict = {"column": column}
    async with maker() as db:
        await _set_read_gucs(db)
        try:
            value = await db.scalar(
                text(
                    f"SELECT {column} FROM public.projects "
                    "WHERE id = CAST(:project_id AS uuid)"
                ),
                {"project_id": project_id},
            )
        except Exception as error:
            result["binary_driver"] = {
                "outcome": "error",
                "error": _error_record(error),
            }
            try:
                await db.rollback()
            except Exception as rollback_error:
                result["binary_driver"]["rollback_error"] = _error_record(
                    rollback_error
                )
        else:
            result["binary_driver"] = _driver_value(value)
            await db.rollback()

    async with maker() as db:
        await _set_read_gucs(db)
        try:
            row = (
                await db.execute(
                    text(
                        f"SELECT {column}::text AS text_value, "
                        f"pg_catalog.to_jsonb({column})::text AS json_text "
                        "FROM public.projects "
                        "WHERE id = CAST(:project_id AS uuid)"
                    ),
                    {"project_id": project_id},
                )
            ).one()
        except Exception as error:
            result["server_text_and_json"] = {
                "outcome": "error",
                "error": _error_record(error),
            }
            try:
                await db.rollback()
            except Exception as rollback_error:
                result["server_text_and_json"]["rollback_error"] = _error_record(
                    rollback_error
                )
        else:
            raw_json = row._mapping["json_text"]
            try:
                parsed_json = json.loads(raw_json)
            except (TypeError, ValueError):
                parsed_json = None
            result["server_text_and_json"] = {
                "outcome": "value",
                "text": row._mapping["text_value"],
                "json_text": raw_json,
                "json_value": parsed_json,
            }
            await db.rollback()
    return result


@contextmanager
def _observe_sql(engine):
    statements: list[str] = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)


def _sql_summary(statements: list[str]) -> list[dict]:
    summary = []
    for statement in statements:
        match = re.match(r"\s*([a-z]+)", statement, re.IGNORECASE)
        summary.append(
            {
                "verb": match.group(1).upper() if match else None,
                "relations": list(dict.fromkeys(_RELATION_REF.findall(statement))),
                "qualified_functions": list(
                    dict.fromkeys(_FUNCTION_REF.findall(statement))
                ),
                "sql": " ".join(statement.split()),
                "sha256": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
            }
        )
    return summary


async def _http_attempt(app, token: str, path: str, *, raise_app_exceptions: bool):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://project-temporal-capture", timeout=30
    ) as client:
        response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
    try:
        body = response.json() if response.content else None
    except ValueError:
        body = response.text
    return {
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "body": body,
    }


async def _capture_http_read(app, engine, *, token: str, path: str) -> dict:
    attempts = []
    statements: list[str] = []
    try:
        with _observe_sql(engine) as statements:
            first_response = await _http_attempt(
                app, token, path, raise_app_exceptions=True
            )
    except Exception as error:
        attempts.append(
            {
                "raise_app_exceptions": True,
                "exception": _error_record(error),
                "sql": _sql_summary(statements),
            }
        )
        replay_statements: list[str] = []
        try:
            with _observe_sql(engine) as replay_statements:
                public_response = await _http_attempt(
                    app, token, path, raise_app_exceptions=False
                )
            replay_error = None
        except Exception as replay_error_value:
            public_response = None
            replay_error = _error_record(replay_error_value)
        attempts.append(
            {
                "raise_app_exceptions": False,
                "exception": replay_error,
                "response": public_response,
                "sql": _sql_summary(replay_statements),
            }
        )
        return {
            "response": public_response,
            "exception_attempt": attempts[0]["exception"],
            "attempts": attempts,
        }

    attempts.append(
        {
            "raise_app_exceptions": True,
            "exception": None,
            "response": first_response,
            "sql": _sql_summary(statements),
        }
    )
    return {
        "response": first_response,
        "exception_attempt": None,
        "attempts": attempts,
    }


def _normalise_response_value(
    value,
    *,
    actor_id,
    project_id,
    project_display_id: str,
    project_name: str,
):
    if isinstance(value, list):
        return [
            _normalise_response_value(
                item,
                actor_id=actor_id,
                project_id=project_id,
                project_display_id=project_display_id,
                project_name=project_name,
            )
            for item in value
        ]
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if key == "id" and item == str(project_id):
                normalized[key] = "$project"
            elif key == "owner_id" and item == str(actor_id):
                normalized[key] = "$actor"
            elif key == "display_id" and item == project_display_id:
                normalized[key] = "$project_display_id"
            elif key == "name" and item == project_name:
                normalized[key] = "Project temporal probe $capture_suffix"
            else:
                normalized[key] = _normalise_response_value(
                    item,
                    actor_id=actor_id,
                    project_id=project_id,
                    project_display_id=project_display_id,
                    project_name=project_name,
                )
        return normalized
    return value


def _normalise_attempt_responses(
    attempts: list[dict], *, actor_id, project_id, project_display_id, project_name
) -> list[dict]:
    normalized = []
    for attempt in attempts:
        item = dict(attempt)
        if "response" in item:
            item["response"] = _normalise_response_value(
                item["response"],
                actor_id=actor_id,
                project_id=project_id,
                project_display_id=project_display_id,
                project_name=project_name,
            )
        normalized.append(item)
    return normalized


async def _reset_project_times(owner_maker, project_id) -> None:
    async with owner_maker.begin() as db:
        result = await db.execute(
            text(
                "UPDATE public.projects SET due_date = "
                f"{_FIXED_DATE}, created_at = {_FIXED_TIMESTAMP}, "
                f"updated_at = {_FIXED_TIMESTAMP} "
                "WHERE id = CAST(:project_id AS uuid)"
            ),
            {"project_id": project_id},
        )
        assert result.rowcount == 1


async def _set_project_time(owner_maker, project_id, *, column: str, expression: str):
    assert column in _COLUMN_NAMES
    async with owner_maker.begin() as db:
        await db.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        result = await db.execute(
            text(
                f"UPDATE public.projects SET {column} = {expression} "
                "WHERE id = CAST(:project_id AS uuid)"
            ),
            {"project_id": project_id},
        )
        assert result.rowcount == 1


async def _cleanup_owned(owner_maker, *, project_id, actor_id) -> None:
    async with owner_maker.begin() as db:
        if project_id is not None:
            await db.execute(
                text("DELETE FROM public.projects WHERE id = :project_id"),
                {"project_id": project_id},
            )
        if actor_id is not None:
            await db.execute(
                text("DELETE FROM public.users WHERE id = :actor_id"),
                {"actor_id": actor_id},
            )


def _write_capture(capture: dict) -> None:
    _CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _CAPTURE_PATH.open("x", encoding="utf-8") as stream:
        json.dump(capture, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


async def test_capture_legacy_project_temporal_extremes(test_engine, app_module):
    from app.deps import get_db
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    assert not _CAPTURE_PATH.exists(), f"refusing to overwrite {_CAPTURE_PATH}"
    _verify_postgres(test_engine.url)
    source = _source_metadata()
    runtime_url = _legacy_runtime_url(test_engine)
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_engine = create_async_engine(runtime_url, hide_parameters=True)
    runtime_maker = async_sessionmaker(runtime_engine, expire_on_commit=False)
    actor_id = None
    project_id = None
    project_display_id = None
    project_name = None
    token = None
    capture = {
        "capture_kind": "actual-pre-extraction-project-temporal-extremes",
        "identity_normalization": {
            "response_actor_id": "$actor",
            "response_project_id": "$project",
            "response_project_display_id": "$project_display_id",
            "response_project_name": "Project temporal probe $capture_suffix",
            "database_name": "$owned_test_database",
            "exception_text": "preserved verbatim and may contain run-specific values",
        },
        "source": source,
        "database_identity": None,
        "driver_expression_probes": [],
        "project_column_probes": [],
        "public_reads": [],
    }
    previous_override = app_module.dependency_overrides.get(get_db)

    async def request_db():
        async with runtime_maker() as db:
            yield db

    app_module.dependency_overrides[get_db] = request_db
    try:
        async with runtime_engine.connect() as connection:
            runtime_role, runtime_database = (
                await connection.execute(
                    text("SELECT current_user, current_database()")
                )
            ).one()
            server_version = await connection.scalar(text("SELECT version()"))
        assert runtime_role == "legacy_runtime"
        assert runtime_database == test_engine.url.database
        capture["database_identity"] = {
            "runtime_role": runtime_role,
            "database_alias": "$owned_test_database",
            "database_name_ends_test": runtime_database.endswith("_test"),
            "server_version": server_version,
        }
        capture["runtime_versions"] = {
            "python": sys.version.split()[0],
            "asyncpg": importlib.metadata.version("asyncpg"),
            "sqlalchemy": importlib.metadata.version("SQLAlchemy"),
            "fastapi": importlib.metadata.version("fastapi"),
        }

        suffix = uuid.uuid4().hex[:12]
        async with owner_maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"project-temporal-extremes-{suffix}@example.test",
                "Project temporal probe",
            )
            actor_id = actor.id
            project = await create_project(
                db,
                owner_id=actor.id,
                name=f"Project temporal probe {suffix}",
                type_key="image-det",
                type_label="Temporal boundary probe",
                classes=["probe"],
            )
            project_id = project.id
            project_display_id = project.display_id
            project_name = project.name
            project.due_date = date(2000, 1, 2)
            project.created_at = project.updated_at = datetime(
                2000, 1, 2, 3, 4, 5, tzinfo=timezone.utc
            )
        token = create_access_token(subject=str(actor_id), role="project_admin")
        capture["actor"] = {
            "alias": "$actor",
            "role": "project_admin",
            "active": True,
        }
        capture["project"] = {
            "alias": "$project",
            "display_id": "$project_display_id",
            "name": "Project temporal probe $capture_suffix",
        }

        async with runtime_engine.connect() as connection:
            await connection.execute(text("SET LOCAL DateStyle = 'ISO, YMD'"))
            await connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            capture["database_identity"]["date_style"] = await connection.scalar(
                text("SHOW DateStyle")
            )
            capture["database_identity"]["time_zone"] = await connection.scalar(
                text("SHOW TimeZone")
            )
            await connection.rollback()

        for label, expression in _DATE_VALUES:
            capture["driver_expression_probes"].append(
                {
                    "postgres_type": "date",
                    **await _probe_expression(
                        runtime_maker, label=label, expression=expression
                    ),
                }
            )
        for label, expression in _TIMESTAMP_VALUES:
            capture["driver_expression_probes"].append(
                {
                    "postgres_type": "timestamptz",
                    **await _probe_expression(
                        runtime_maker, label=label, expression=expression
                    ),
                }
            )

        api_cases = (
            (
                "due_date_finite_before_python_min",
                "due_date",
                "DATE '0001-01-01 BC'",
                (("detail", f"{_PROJECTS_PATH}/{project_id}"),),
            ),
            (
                "due_date_finite_after_python_max",
                "due_date",
                "DATE '10000-01-01'",
                (
                    ("detail", f"{_PROJECTS_PATH}/{project_id}"),
                    ("list", _PROJECTS_PATH),
                ),
            ),
            (
                "due_date_positive_infinity",
                "due_date",
                "DATE 'infinity'",
                (("detail", f"{_PROJECTS_PATH}/{project_id}"),),
            ),
            (
                "created_at_finite_after_python_max",
                "created_at",
                "TIMESTAMPTZ '10000-01-01 00:00:00+00'",
                (
                    ("detail", f"{_PROJECTS_PATH}/{project_id}"),
                    ("stats", f"{_PROJECTS_PATH}/stats"),
                ),
            ),
            (
                "updated_at_finite_before_python_min",
                "updated_at",
                "TIMESTAMPTZ '0001-01-01 00:00:00 BC'",
                (("detail", f"{_PROJECTS_PATH}/{project_id}"),),
            ),
            (
                "updated_at_positive_infinity",
                "updated_at",
                "TIMESTAMPTZ 'infinity'",
                (("detail", f"{_PROJECTS_PATH}/{project_id}"),),
            ),
        )
        for case_name, column, expression, endpoints in api_cases:
            await _reset_project_times(owner_maker, project_id)
            await _set_project_time(
                owner_maker, project_id, column=column, expression=expression
            )
            column_observation = await _probe_project_column(
                runtime_maker, project_id=project_id, column=column
            )
            column_observation.update({"case": case_name, "expression": expression})
            capture["project_column_probes"].append(column_observation)

            for endpoint, path in endpoints:
                observation = await _capture_http_read(
                    app_module, runtime_engine, token=token, path=path
                )
                capture["public_reads"].append(
                    {
                        "case": case_name,
                        "column": column,
                        "expression": expression,
                        "endpoint": endpoint,
                        "method": "GET",
                        "path": path.replace(str(project_id), "$project"),
                        "actor_role": "project_admin",
                        "response": _normalise_response_value(
                            observation["response"],
                            actor_id=actor_id,
                            project_id=project_id,
                            project_display_id=project_display_id,
                            project_name=project_name,
                        ),
                        "exception_attempt": observation["exception_attempt"],
                        "attempts": _normalise_attempt_responses(
                            observation["attempts"],
                            actor_id=actor_id,
                            project_id=project_id,
                            project_display_id=project_display_id,
                            project_name=project_name,
                        ),
                    }
                )
    finally:
        if previous_override is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_override
        try:
            if actor_id is not None or project_id is not None:
                await _cleanup_owned(
                    owner_maker, project_id=project_id, actor_id=actor_id
                )
        finally:
            await runtime_engine.dispose()

    _write_capture(capture)
