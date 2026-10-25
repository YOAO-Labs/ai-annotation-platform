"""Exercise durable HTTP command recovery across an Inference owner restart."""

from __future__ import annotations

import hashlib
import json
import os
import uuid

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference.pipeline_client import PipelineHttpClient
from aap_inference.pipeline_contract import (
    PipelineFailure,
    PipelineStatusSuccess,
    PipelineSuccess,
    PipelineUnavailable,
    pipeline_command_request,
    pipeline_status_request,
)
from app.config import settings
from app.core.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_pipeline import ProjectPipeline
from app.db.models.user import User
from app.services import inference_pipelines as gateway
from tests.factory import create_project, create_user
from tests.inference_pipeline_owner_helpers import (
    api_pipeline_owner_binding,
    pipeline_dsn,
)
from tests.labeling_class_query_helpers import (
    assert_owned_test_database,
    api_db_binding,
    clear_local_identity,
    make_request_runtime,
)

_EXECUTE_PATH = "/v1/pipelines/execute"
_STATUS_PATH = "/v1/pipelines/status"


class _LoseOneCommittedResponse(httpx.AsyncBaseTransport):
    """Delegate to the real HTTP transport, then drop one committed execute ack."""

    def __init__(self, state: dict) -> None:
        self.state = state
        self.inner = httpx.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.state["paths"].append(path)
        self.state["requests"].append((path, json.loads(request.content)))
        response = await self.inner.handle_async_request(request)
        if (
            path == _EXECUTE_PATH
            and request.method == "POST"
            and response.status_code == 200
            and not self.state["ack_lost"]
        ):
            await response.aread()
            await response.aclose()
            self.state["ack_lost"] = True
            self.state["ack_body_read"] = response.is_stream_consumed
            self.state["ack_closed"] = response.is_closed
            raise httpx.ReadError(
                "test discarded a committed owner response", request=request
            )
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


class _RecordingPipelineClient:
    def __init__(self, url: str, token: str, *, timeout_seconds: float, state: dict):
        state["settings_match"] = (
            url == settings.inference_pipelines_url
            and token == settings.inference_pipelines_token
        )
        self.inner = PipelineHttpClient(
            url,
            token,
            timeout_seconds=timeout_seconds,
            transport=_LoseOneCommittedResponse(state),
        )
        self.state = state

    async def execute(self, command):
        self.state["commands"].append(command)
        return await self.inner.execute(command)

    async def query(self, query):
        return await self.inner.query(query)

    async def status(self, status):
        return await self.inner.status(status)

    async def close(self):
        await self.inner.close()


class _CountingOwnerTransport(httpx.AsyncBaseTransport):
    def __init__(self, paths: list[str], requests: list[tuple[str, dict]]) -> None:
        self.paths = paths
        self.requests = requests
        self.inner = httpx.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.requests.append((request.url.path, json.loads(request.content)))
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


async def _assert_single_pipeline_and_receipt(
    test_engine,
    *,
    actor_id,
    project_id,
    name,
    command,
    expected_pipeline_id,
    expected_default_id,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as connection:
        pipeline_rows = (
            (
                await connection.execute(
                    select(ProjectPipeline).where(
                        ProjectPipeline.created_by == actor_id,
                        ProjectPipeline.project_id == project_id,
                        ProjectPipeline.name == name,
                    )
                )
            )
            .scalars()
            .all()
        )
        receipt_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT operation_id, actor_id, operation, target_id, "
                        "request, resource_id FROM "
                        "aap_inference_pipelines_v1.operation_receipts "
                        "WHERE operation_id = :operation_id"
                    ),
                    {"operation_id": command.operation_id},
                )
            )
            .mappings()
            .all()
        )
        defaults = (
            (
                await connection.execute(
                    select(ProjectPipeline.id).where(
                        ProjectPipeline.project_id == project_id,
                        ProjectPipeline.is_default.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        project_rows = (
            (
                await connection.execute(
                    select(ProjectPipeline).where(
                        ProjectPipeline.project_id == project_id
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_default = (
            (
                await connection.execute(
                    select(ProjectPipeline).where(
                        ProjectPipeline.id == expected_default_id
                    )
                )
            )
            .scalars()
            .one_or_none()
        )

    assert len(pipeline_rows) == 1
    pipeline = pipeline_rows[0]
    assert pipeline.id == expected_pipeline_id
    assert pipeline.is_default is False
    assert pipeline.usage_count == 0
    assert defaults == [expected_default_id]
    assert len(project_rows) == 2
    assert {row.id for row in project_rows} == {
        expected_pipeline_id,
        expected_default_id,
    }
    assert {row.name for row in project_rows} == {
        name,
        f"{name}-existing-default",
    }
    assert existing_default is not None
    assert (
        existing_default.id,
        existing_default.scope,
        existing_default.project_id,
        existing_default.organization_id,
        existing_default.name,
        existing_default.stages,
        existing_default.is_default,
        existing_default.created_by,
        existing_default.usage_count,
    ) == (
        expected_default_id,
        "private",
        project_id,
        None,
        f"{name}-existing-default",
        [],
        True,
        actor_id,
        0,
    )

    assert len(receipt_rows) == 1
    receipt = receipt_rows[0]
    assert receipt["operation_id"] == command.operation_id
    assert receipt["actor_id"] == actor_id
    assert receipt["operation"] == "create_pipeline"
    assert receipt["target_id"] is None
    assert receipt["resource_id"] == expected_pipeline_id
    assert receipt["request"] == command.model_dump(mode="json", by_alias=True)


async def _cleanup_recovery_world(
    test_engine,
    *,
    actor_id,
    project_id,
    pipeline_names,
    operation_id,
):
    async with test_engine.begin() as connection:
        if operation_id is not None:
            await connection.execute(
                text(
                    "DELETE FROM aap_inference_pipelines_v1.operation_receipts "
                    "WHERE operation_id = :operation_id"
                ),
                {"operation_id": operation_id},
            )
        if actor_id is not None and project_id is not None:
            owned_pipeline_ids = (
                (
                    await connection.execute(
                        select(ProjectPipeline.id).where(
                            ProjectPipeline.created_by == actor_id,
                            ProjectPipeline.project_id == project_id,
                            ProjectPipeline.name.in_(pipeline_names),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for resource_id in owned_pipeline_ids:
                await connection.execute(
                    text(
                        "DELETE FROM aap_inference_pipelines_v1.operation_receipts "
                        "WHERE resource_id = :resource_id"
                    ),
                    {"resource_id": resource_id},
                )
            await connection.execute(
                delete(ProjectPipeline).where(
                    ProjectPipeline.created_by == actor_id,
                    ProjectPipeline.project_id == project_id,
                    ProjectPipeline.name.in_(pipeline_names),
                )
            )
            await connection.execute(delete(Project).where(Project.id == project_id))
        if actor_id is not None:
            await connection.execute(delete(User).where(User.id == actor_id))


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires explicit owned module-data role acceptance",
)
async def test_http_lost_ack_survives_restart_status_and_exact_replay(
    test_engine, app_module, monkeypatch, tmp_path
):
    clear_local_identity(monkeypatch)
    await assert_owned_test_database(test_engine)
    owner_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    project_id = None
    pipeline_name = f"inference-recovery-{uuid.uuid4().hex}"
    default_name = f"{pipeline_name}-existing-default"
    operation_id = None
    pipeline_id = None
    default_pipeline_id = None
    command = None
    runtime_engine = None
    first_dsn_fingerprint = hashlib.sha256(pipeline_dsn(test_engine).encode()).digest()

    try:
        async with owner_maker.begin() as db:
            actor = await create_user(
                db,
                "super_admin",
                f"{pipeline_name}@example.test",
                "Inference Recovery Owner",
            )
            project = await create_project(
                db,
                owner_id=actor.id,
                name=pipeline_name,
            )
            actor_id = actor.id
            project_id = project.id
            default_pipeline = ProjectPipeline(
                id=uuid.uuid4(),
                scope="private",
                project_id=project.id,
                organization_id=None,
                name=default_name,
                stages=[],
                is_default=True,
                created_by=actor.id,
                usage_count=0,
            )
            db.add(default_pipeline)
            await db.flush()
            default_pipeline_id = default_pipeline.id

        access_token = create_access_token(str(actor_id), "super_admin")
        runtime_engine, runtime_maker = await make_request_runtime(
            test_engine,
            application_name="inference-pipeline-http-recovery",
        )
        async with runtime_engine.connect() as connection:
            role, database = (
                await connection.execute(
                    text("SELECT current_user, current_database()")
                )
            ).one()
        assert role == "legacy_runtime"
        assert database == test_engine.url.database

        first_owner_state = {
            "paths": [],
            "ack_lost": False,
            "ack_body_read": False,
            "ack_closed": False,
            "commands": [],
            "requests": [],
        }
        with monkeypatch.context() as scoped:
            scoped.setattr(
                gateway,
                "PipelineHttpClient",
                lambda url, token, *, timeout_seconds: _RecordingPipelineClient(
                    url,
                    token,
                    timeout_seconds=timeout_seconds,
                    state=first_owner_state,
                ),
            )
            async with api_pipeline_owner_binding(
                test_engine,
                monkeypatch,
                "http",
                tmp_path,
            ):
                async with api_db_binding(app_module, runtime_maker):
                    transport = httpx.ASGITransport(
                        app=app_module,
                        raise_app_exceptions=False,
                    )
                    async with httpx.AsyncClient(
                        transport=transport,
                        base_url="http://test",
                        timeout=30,
                    ) as api_client:
                        response = await api_client.post(
                            "/api/v1/project-pipelines",
                            headers={"Authorization": f"Bearer {access_token}"},
                            json={
                                "name": pipeline_name,
                                "scope": "private",
                                "project_id": str(project_id),
                                "organization_id": None,
                                "stages": [],
                                "is_default": False,
                            },
                        )
                if first_owner_state["commands"]:
                    command = first_owner_state["commands"][0]
                    operation_id = command.operation_id
                assert response.status_code == 201
                assert first_owner_state["settings_match"] is True
                assert first_owner_state["ack_lost"] is True
                assert first_owner_state["ack_body_read"] is True
                assert first_owner_state["ack_closed"] is True
                assert len(first_owner_state["commands"]) == 1
                assert first_owner_state["paths"].count(_EXECUTE_PATH) == 1
                assert first_owner_state["paths"].count(_STATUS_PATH) == 1
                assert first_owner_state["paths"].count("/v1/pipelines/query") == 1
                assert command is not None
                status_body = next(
                    body
                    for path, body in first_owner_state["requests"]
                    if path == _STATUS_PATH
                )
                assert status_body["command"] == command.model_dump(
                    mode="json", by_alias=True
                )
                pipeline_id = uuid.UUID(response.json()["id"])
                assert command.operation == "create_pipeline"
                await _assert_single_pipeline_and_receipt(
                    test_engine,
                    actor_id=actor_id,
                    project_id=project_id,
                    name=pipeline_name,
                    command=command,
                    expected_pipeline_id=pipeline_id,
                    expected_default_id=default_pipeline_id,
                )

        assert command is not None
        if runtime_engine is not None:
            await runtime_engine.dispose()
            runtime_engine = None
        second_dsn_fingerprint = hashlib.sha256(
            pipeline_dsn(test_engine).encode()
        ).digest()
        assert second_dsn_fingerprint == first_dsn_fingerprint, (
            "owner restart changed the guarded DSN"
        )
        restart_paths = []
        restart_requests = []
        async with api_pipeline_owner_binding(
            test_engine,
            monkeypatch,
            "http",
            tmp_path,
        ):
            client = PipelineHttpClient(
                settings.inference_pipelines_url,
                settings.inference_pipelines_token,
                transport=_CountingOwnerTransport(restart_paths, restart_requests),
            )
            try:
                status = await client.status(pipeline_status_request(command))
                assert isinstance(status, PipelineStatusSuccess)
                assert status.state == "committed"
                assert status.resource_id == pipeline_id
                assert restart_requests[0][0] == _STATUS_PATH
                assert restart_requests[0][1]["command"] == command.model_dump(
                    mode="json", by_alias=True
                )

                replay = await client.execute(command)
                assert isinstance(replay, PipelineSuccess)
                assert replay.result.resource_id == pipeline_id
                assert replay.commit_state == "committed"
                assert restart_requests[1] == (
                    _EXECUTE_PATH,
                    command.model_dump(mode="json", by_alias=True),
                )
                await _assert_single_pipeline_and_receipt(
                    test_engine,
                    actor_id=actor_id,
                    project_id=project_id,
                    name=pipeline_name,
                    command=command,
                    expected_pipeline_id=pipeline_id,
                    expected_default_id=default_pipeline_id,
                )

                changed_payload = json.loads(command.payload_ascii)
                changed_payload["name"] = f"{pipeline_name}-changed"
                changed = pipeline_command_request(
                    command.operation,
                    actor_id=command.admitted_actor.id,
                    actor_role=command.admitted_actor.role,
                    target_id=command.target_id,
                    payload=changed_payload,
                    operation_id=command.operation_id,
                )
                assert changed.operation_id == command.operation_id
                assert changed.payload_ascii != command.payload_ascii
                conflict = None
                outcome_unknown = False
                try:
                    conflict = await client.execute(changed)
                except PipelineUnavailable:
                    outcome_unknown = True
                assert outcome_unknown or isinstance(conflict, PipelineFailure)
                if isinstance(conflict, PipelineFailure):
                    assert conflict.error.status_code == 409
                    assert conflict.error.detail == {"reason": "operation_conflict"}
                assert restart_paths.count(_EXECUTE_PATH) == 2
                assert restart_paths.count(_STATUS_PATH) in {1, 2}
            finally:
                await client.close()

        await _assert_single_pipeline_and_receipt(
            test_engine,
            actor_id=actor_id,
            project_id=project_id,
            name=pipeline_name,
            command=command,
            expected_pipeline_id=pipeline_id,
            expected_default_id=default_pipeline_id,
        )
    finally:
        try:
            if runtime_engine is not None:
                await runtime_engine.dispose()
        finally:
            await _cleanup_recovery_world(
                test_engine,
                actor_id=actor_id,
                project_id=project_id,
                pipeline_names={pipeline_name, default_name},
                operation_id=operation_id,
            )
