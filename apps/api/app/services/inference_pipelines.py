"""Route saved-pipeline work through the selected Inference owner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
import uuid

from aap_inference.pipeline_client import PipelineHttpClient
from aap_inference.pipeline_contract import (
    PipelineCommandRequest,
    PipelineFailure,
    PipelineInvalidRequest,
    PipelinePostCommitFailure,
    PipelineRejected,
    PipelineResult,
    PipelineStatusSuccess,
    PipelineSuccess,
    PipelineUnavailable,
    decode_pipeline_reply,
    pipeline_command_request,
    pipeline_query_request,
    pipeline_status_request,
)
from aap_inference.pipeline_manager import PipelineManager
from aap_inference.pipeline_postgres import PipelinePostgresData
from aap_workspace.config import secret_source
from fastapi import HTTPException
import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.services.identity_contracts import IdentityBriefsUnavailable
from app.services.user_brief import resolve_briefs


@dataclass(frozen=True, slots=True)
class _Owner:
    mode: Literal["borrowed", "database", "http"]
    url: str = ""
    dsn: str = field(default="", repr=False)
    token: str = field(default="", repr=False)
    timeout: float = 30.0


@dataclass(frozen=True, slots=True)
class PipelineMutation:
    """A committed or caller-pending command and its receipt-bound evidence."""

    command: PipelineCommandRequest
    resource_id: uuid.UUID
    commit_state: Literal["caller_pending", "committed"]
    _owner: _Owner = field(repr=False)


def _role(actor) -> str:
    return getattr(actor.role, "value", actor.role)


def _select_owner() -> _Owner:
    url = settings.inference_pipelines_url.strip()
    try:
        dsn = secret_source(
            settings.inference_pipelines_database_url,
            settings.inference_pipelines_database_url_file,
        )
    except ValueError:
        raise PipelineUnavailable() from None

    if url and dsn:
        raise PipelineUnavailable()
    if url:
        try:
            token = secret_source(
                settings.inference_pipelines_token,
                settings.inference_pipelines_token_file,
            )
        except ValueError:
            raise PipelineUnavailable() from None
        if not token:
            raise PipelineUnavailable()
        return _Owner(
            "http",
            url=url,
            token=token,
            timeout=settings.inference_pipelines_timeout_seconds,
        )
    if dsn:
        return _Owner(
            "database", dsn=dsn, timeout=settings.inference_pipelines_timeout_seconds
        )
    return _Owner("borrowed")


async def _invoke(owner: _Owner, db, operation: str, request):
    if owner.mode == "borrowed":
        from app.services.inference_pipelines_data import InferencePipelinesData

        manager = PipelineManager(InferencePipelinesData(db))
        if operation == "execute":
            return await manager.execute(request)
        if operation == "query":
            return await manager.query(request)
        return await manager.status(request)

    if owner.mode == "http":
        client = PipelineHttpClient(
            owner.url,
            owner.token,
            timeout_seconds=owner.timeout,
        )
        try:
            if operation == "execute":
                return await client.execute(request)
            if operation == "query":
                return await client.query(request)
            return await client.status(request)
        finally:
            await client.close()

    data = PipelinePostgresData(owner.dsn, command_timeout=owner.timeout)
    try:
        manager = PipelineManager(data)
        if operation == "execute":
            reply = await manager.execute(request)
            if isinstance(reply, PipelineFailure) and reply.commit_state == "unknown":
                reply = await _resolve_database_commit(manager, request)
            return reply
        if operation == "query":
            return await manager.query(request)
        return await manager.status(request)
    finally:
        await data.close()


async def _resolve_database_commit(manager, request):
    try:
        status = await manager.status(pipeline_status_request(request))
    except (PipelineRejected, PipelineUnavailable):
        raise PipelineUnavailable() from None
    if not isinstance(status, PipelineStatusSuccess) or status.state != "committed":
        raise PipelineUnavailable()
    return decode_pipeline_reply(
        request,
        PipelineSuccess(
            schema="aap-inference-pipelines/v1",
            ok=True,
            operation_id=request.operation_id,
            operation=request.operation,
            actor_id=request.admitted_actor.id,
            target_id=request.target_id,
            result=PipelineResult(resource_id=status.resource_id),
            commit_state="committed",
        ),
        require_committed=True,
    )


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="编排服务暂不可用")


async def _raise_reply_error(
    reply, db, owner: _Owner, *, postcommit: bool = False
) -> None:
    if reply.ok:
        return
    error = reply.error
    if not postcommit and error.rollback and owner.mode == "borrowed":
        await db.rollback()
    if postcommit or error.status_code == 500:
        raise RuntimeError("Internal Server Error") from None
    raise HTTPException(
        status_code=error.status_code,
        detail=error.detail,
        headers=error.headers,
    )


async def _call(owner: _Owner, db, operation: str, request, *, postcommit=False):
    try:
        reply = await _invoke(owner, db, operation, request)
        await _raise_reply_error(reply, db, owner, postcommit=postcommit)
        return reply
    except HTTPException:
        raise
    except PipelineRejected as exc:
        if not postcommit and exc.rollback and owner.mode == "borrowed":
            await db.rollback()
        if postcommit or exc.status_code == 500:
            raise RuntimeError("Internal Server Error") from None
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        ) from None
    except PipelinePostCommitFailure:
        raise RuntimeError("Internal Server Error") from None
    except (PipelineInvalidRequest, PipelineUnavailable):
        if postcommit:
            raise RuntimeError("Internal Server Error") from None
        raise _unavailable() from None
    except (httpx.HTTPError, httpx.InvalidURL, SQLAlchemyError, OSError, TimeoutError):
        if postcommit:
            raise RuntimeError("Internal Server Error") from None
        if owner.mode == "borrowed":
            raise
        raise _unavailable() from None


async def execute_pipeline_command(
    db,
    *,
    operation: str,
    actor,
    target_id=None,
    payload,
) -> PipelineMutation:
    """Execute one command without committing the caller-owned transaction."""
    try:
        owner = _select_owner()
        request = pipeline_command_request(
            operation,
            actor_id=actor.id,
            actor_role=_role(actor),
            target_id=target_id,
            payload=payload,
        )
        reply = await _call(owner, db, "execute", request)
    except PipelineInvalidRequest:
        raise HTTPException(status_code=422, detail="无效的编排请求") from None
    except PipelineUnavailable:
        raise _unavailable() from None
    return PipelineMutation(
        command=request,
        resource_id=reply.result.resource_id,
        commit_state=reply.commit_state,
        _owner=owner,
    )


async def commit_pipeline_command(db, mutation: PipelineMutation) -> None:
    """Commit exactly once only when the selected owner borrowed the session."""
    if mutation.commit_state == "caller_pending":
        await db.commit()


async def _query(
    db,
    *,
    operation: str,
    actor,
    target_id=None,
    filters=None,
    evidence=None,
    owner: _Owner | None = None,
    postcommit: bool = False,
):
    try:
        selected = owner or _select_owner()
    except PipelineUnavailable:
        if postcommit:
            raise RuntimeError("Internal Server Error") from None
        raise _unavailable() from None
    request = pipeline_query_request(
        operation,
        actor_id=actor.id,
        actor_role=_role(actor),
        target_id=target_id,
        filters=filters,
        evidence=evidence,
    )
    return await _call(selected, db, "query", request, postcommit=postcommit)


async def list_pipeline_rows(
    db, *, actor, scope=None, project_id=None, organization_id=None
):
    reply = await _query(
        db,
        operation="list_pipelines",
        actor=actor,
        filters={
            "scope": scope,
            "project_id": project_id,
            "organization_id": organization_id,
        },
    )
    creator_ids = {row.created_by for row in reply.rows if row.created_by is not None}
    briefs = await _resolve_pipeline_briefs(db, creator_ids)
    return [_pipeline_output(row, briefs) for row in reply.rows]


async def get_pipeline_row(db, *, actor, pipeline_id):
    reply = await _query(
        db,
        operation="get_pipeline",
        actor=actor,
        target_id=pipeline_id,
    )
    briefs = await _resolve_pipeline_briefs(db, [reply.rows[0].created_by])
    return _pipeline_output(reply.rows[0], briefs)


async def get_pipeline_projection(db, mutation: PipelineMutation, *, actor):
    try:
        reply = await _query(
            db,
            operation="get_pipeline_projection",
            actor=actor,
            target_id=mutation.resource_id,
            evidence=mutation.command,
            owner=mutation._owner,
            postcommit=True,
        )
        briefs = (
            {}
            if mutation.command.operation == "apply_pipeline"
            else await _resolve_pipeline_briefs(db, [reply.rows[0].created_by])
        )
        return _pipeline_output(reply.rows[0], briefs)
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("Internal Server Error") from None


async def _resolve_pipeline_briefs(db, user_ids):
    if not user_ids:
        return {}
    try:
        return await resolve_briefs(db, user_ids)
    except (IdentityBriefsUnavailable, SQLAlchemyError):
        raise RuntimeError("Internal Server Error") from None


def _pipeline_output(row, briefs):
    data = row.model_dump(mode="python", by_alias=True)
    brief = briefs.get(str(row.created_by)) if row.created_by is not None else None
    data["created_by_name"] = brief.name if brief is not None else None
    return data
