"""Focused, side-effect-free checks for the saved-pipeline API bindings."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from aap_inference.pipeline_contract import (
    PIPELINE_PROTOCOL,
    PipelineErrorDetails,
    PipelineFailure,
    PipelineQuerySuccess,
    PipelineRow,
    PipelineResult,
    PipelineRejected,
    PipelineSuccess,
    PipelineUnavailable,
    pipeline_command_request,
)
from app.services import inference_pipelines as gateway
from app.services.inference_pipelines_data import InferencePipelinesData


class _BorrowedSession:
    def __init__(self, *, autoflush=True):
        self.autoflush = autoflush
        self.flushes = 0
        self.commits = 0
        self.rollbacks = 0
        self.statements = []
        self.no_autoflush = nullcontext()

    async def flush(self):
        self.flushes += 1

    async def scalar(self, statement, parameters):
        self.statements.append((statement, parameters))
        return {"ok": True}

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _actor():
    return SimpleNamespace(id=uuid.uuid4(), role="project_admin")


def _command(actor, *, operation="create_pipeline", target_id=None, payload=None):
    return pipeline_command_request(
        operation,
        actor_id=actor.id,
        actor_role=actor.role,
        target_id=target_id,
        payload={} if payload is None else payload,
    )


def test_owner_selection_defaults_to_borrowed_and_requires_http_credentials(
    monkeypatch,
):
    def configured(**values):
        defaults = {
            "inference_pipelines_url": "",
            "inference_pipelines_token": "",
            "inference_pipelines_token_file": "",
            "inference_pipelines_timeout_seconds": 30,
        }
        return SimpleNamespace(**(defaults | values))

    monkeypatch.setattr(gateway, "settings", configured())
    assert gateway._select_owner().mode == "borrowed"

    monkeypatch.setattr(
        gateway,
        "settings",
        configured(inference_pipelines_url="http://inference"),
    )
    with pytest.raises(PipelineUnavailable):
        gateway._select_owner()

    monkeypatch.setattr(
        gateway,
        "settings",
        configured(
            inference_pipelines_url="http://inference",
            inference_pipelines_token="owner-token",
        ),
    )
    selected = gateway._select_owner()
    assert selected.mode == "http"
    assert selected.token == "owner-token"


def test_borrowed_adapter_preserves_flush_boundaries():
    async def check():
        actor = _actor()
        request = _command(actor)
        session = _BorrowedSession()
        data = InferencePipelinesData(session)

        async with data.transaction(readonly=False) as transaction:
            assert transaction.owns_commit is False
            await transaction.step(request, "begin", {})
            assert session.flushes == 0
            await transaction.step(
                request, "organization_ids", {"actor_id": str(actor.id)}
            )
            assert session.flushes == 1
            await transaction.step(
                request, "project_access", {"phase": "lock_actor_share"}
            )
            assert session.flushes == 1
            update_request = _command(
                actor,
                operation="update_pipeline",
                target_id=uuid.uuid4(),
            )
            await transaction.step(
                update_request,
                "pipeline_update",
                {"pipeline_id": str(update_request.target_id)},
                columns=(),
                values=(),
            )
            assert session.flushes == 2
            assert session.statements[-1][1]["columns"] == []
            assert session.statements[-1][1]["values"] == []
            await transaction.step(
                request, "finish", {"resource_id": str(uuid.uuid4())}
            )
            assert session.flushes == 2

        session_without_autoflush = _BorrowedSession(autoflush=False)
        data_without_autoflush = InferencePipelinesData(session_without_autoflush)
        await data_without_autoflush.step(request, "pipeline_insert", {})
        assert session_without_autoflush.flushes == 1

    import asyncio

    asyncio.run(check())


@pytest.mark.parametrize(
    ("phase", "sqlstate", "reason"),
    [
        ("lock_actor_share", "55P03", "account_busy"),
        ("lock_project_key_share", "40001", "project_busy"),
        ("lock_member_share", "40P01", "membership_busy"),
    ],
)
@pytest.mark.asyncio
async def test_borrowed_authority_contention_retains_phase_conflict(
    phase, sqlstate, reason
):
    class DriverError(Exception):
        pass

    class BusySession(_BorrowedSession):
        async def scalar(self, _statement, _parameters):
            error = DriverError("private driver diagnostics")
            error.sqlstate = sqlstate
            raise DBAPIError(None, None, error)

    request = _command(_actor())
    with pytest.raises(PipelineRejected) as caught:
        await InferencePipelinesData(BusySession()).step(
            request, "project_access", {"phase": phase}
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == {"reason": reason}
    assert caught.value.rollback is True


def test_borrowed_command_commits_once_and_uses_receipt_bound_projection(monkeypatch):
    async def check():
        actor = _actor()
        owner = gateway._Owner("borrowed")
        monkeypatch.setattr(gateway, "_select_owner", lambda: owner)
        resource_id = uuid.uuid4()
        db = _BorrowedSession()
        seen = []

        async def invoke(selected, _db, operation, request):
            assert selected is owner
            seen.append((operation, request))
            if operation == "execute":
                return PipelineSuccess(
                    schema=PIPELINE_PROTOCOL,
                    ok=True,
                    operation_id=request.operation_id,
                    operation=request.operation,
                    actor_id=actor.id,
                    target_id=None,
                    result=PipelineResult(resource_id=resource_id),
                    commit_state="caller_pending",
                )
            now = datetime.now(UTC)
            return PipelineQuerySuccess(
                schema=PIPELINE_PROTOCOL,
                ok=True,
                request_id=request.request_id,
                operation=request.operation,
                actor_id=actor.id,
                target_id=resource_id,
                rows=[
                    PipelineRow(
                        id=resource_id,
                        scope="private",
                        project_id=uuid.uuid4(),
                        organization_id=None,
                        name="sample",
                        stages=[],
                        is_default=False,
                        created_by=actor.id,
                        usage_count=0,
                        created_at=now,
                        updated_at=now,
                    )
                ],
            )

        async def briefs(_db, user_ids):
            assert list(user_ids) == [actor.id]
            return {}

        monkeypatch.setattr(gateway, "_invoke", invoke)
        monkeypatch.setattr(gateway, "_resolve_pipeline_briefs", briefs)
        mutation = await gateway.execute_pipeline_command(
            db,
            operation="create_pipeline",
            actor=actor,
            payload={"name": "sample"},
        )
        await gateway.commit_pipeline_command(db, mutation)
        result = await gateway.get_pipeline_projection(db, mutation, actor=actor)

        assert db.commits == 1
        assert result["id"] == resource_id
        assert result["created_by_name"] is None
        operation, projection = seen[1]
        assert operation == "query"
        assert projection.operation == "get_pipeline_projection"
        assert projection.evidence == mutation.command

    import asyncio

    asyncio.run(check())


def test_committed_owner_does_not_commit_api_session(monkeypatch):
    async def check():
        actor = _actor()
        owner = gateway._Owner("http", url="http://inference", token="private")
        monkeypatch.setattr(gateway, "_select_owner", lambda: owner)
        resource_id = uuid.uuid4()

        async def invoke(_selected, _db, operation, request):
            assert operation == "execute"
            return PipelineSuccess(
                schema=PIPELINE_PROTOCOL,
                ok=True,
                operation_id=request.operation_id,
                operation=request.operation,
                actor_id=actor.id,
                target_id=None,
                result=PipelineResult(resource_id=resource_id),
                commit_state="committed",
            )

        monkeypatch.setattr(gateway, "_invoke", invoke)
        db = _BorrowedSession()
        mutation = await gateway.execute_pipeline_command(
            db,
            operation="create_pipeline",
            actor=actor,
            payload={"name": "sample"},
        )
        await gateway.commit_pipeline_command(db, mutation)
        assert mutation.commit_state == "committed"
        assert db.commits == 0

    import asyncio

    asyncio.run(check())


def test_apply_projection_keeps_legacy_null_creator_name(monkeypatch):
    async def check():
        actor = _actor()
        project_id = uuid.uuid4()
        resource_id = uuid.uuid4()
        command = _command(
            actor,
            operation="apply_pipeline",
            target_id=project_id,
            payload={"pipeline_id": str(uuid.uuid4()), "set_default": False},
        )
        mutation = gateway.PipelineMutation(
            command=command,
            resource_id=resource_id,
            commit_state="committed",
            _owner=gateway._Owner("http", url="http://inference", token="private"),
        )
        now = datetime.now(UTC)
        row = PipelineRow(
            id=resource_id,
            scope="private",
            project_id=project_id,
            organization_id=None,
            name="copy",
            stages=[],
            is_default=False,
            created_by=actor.id,
            usage_count=0,
            created_at=now,
            updated_at=now,
        )

        async def query(*_args, **_kwargs):
            return SimpleNamespace(rows=[row])

        async def briefs(*_args, **_kwargs):
            raise AssertionError("Apply responses keep created_by_name null")

        monkeypatch.setattr(gateway, "_query", query)
        monkeypatch.setattr(gateway, "_resolve_pipeline_briefs", briefs)
        result = await gateway.get_pipeline_projection(None, mutation, actor=actor)
        assert result["created_by_name"] is None

    import asyncio

    asyncio.run(check())


def test_http_client_owns_its_ambiguous_commit_probe(monkeypatch):
    async def check():
        actor = _actor()
        request = _command(actor)
        calls = {"execute": 0, "status": 0, "close": 0}

        class _Client:
            def __init__(self, _url, _token, *, timeout_seconds):
                assert timeout_seconds > 0

            async def execute(self, command):
                calls["execute"] += 1
                return PipelineFailure(
                    schema=PIPELINE_PROTOCOL,
                    ok=False,
                    operation_id=command.operation_id,
                    operation=command.operation,
                    actor_id=command.admitted_actor.id,
                    target_id=command.target_id,
                    error=PipelineErrorDetails(
                        status_code=503,
                        detail="结果暂不可确认",
                        headers=None,
                        rollback=False,
                    ),
                    commit_state="unknown",
                )

            async def status(self, _status):
                calls["status"] += 1
                raise AssertionError("The API must not duplicate the HTTP client probe")

            async def close(self):
                calls["close"] += 1

        monkeypatch.setattr(gateway, "PipelineHttpClient", _Client)
        reply = await gateway._invoke(
            gateway._Owner("http", url="http://inference", token="private"),
            None,
            "execute",
            request,
        )
        assert reply.commit_state == "unknown"
        assert calls == {"execute": 1, "status": 0, "close": 1}

    import asyncio

    asyncio.run(check())
