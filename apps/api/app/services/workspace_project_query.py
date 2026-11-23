"""Read Project facts from the caller's transaction and retain write handoffs."""

from __future__ import annotations

from fastapi import HTTPException
from yoao_workspace.project_queries import (
    ProjectQueryManager,
    ProjectQueryProfilesUnavailable,
)
from yoao_workspace_contract.project_query_contract import (
    ProjectOwnerTransferEvidence,
    ProjectQueryUnavailable,
    decode_project_query_step,
    project_query_request,
)
from yoao_workspace_contract.project_query_values import (
    decode_project_row,
    materialize_project_query_data,
)
from app.services.identity_contracts import IdentityBriefsUnavailable
from app.services.user_brief import resolve_briefs
from app.services.workspace_project_query_data import WorkspaceProjectQueryData


class _ApiProfiles:
    def __init__(self, db):
        self.db = db

    async def resolve(self, ids):
        try:
            return await resolve_briefs(self.db, ids)
        except IdentityBriefsUnavailable:
            raise ProjectQueryProfilesUnavailable() from None


async def execute_project_query(
    db,
    *,
    operation,
    actor,
    target_id=None,
    filters=None,
    pagination=None,
    projection_evidence=None,
):
    request = project_query_request(
        operation,
        actor.id,
        getattr(actor.role, "value", actor.role),
        target_id=target_id,
        filters=filters,
        pagination=pagination,
        projection_evidence=projection_evidence,
    )
    try:
        reply = await ProjectQueryManager(
            WorkspaceProjectQueryData(db), _ApiProfiles(db)
        ).execute(request)
        if not reply.ok:
            raise HTTPException(
                status_code=reply.error.status_code,
                detail=reply.error.detail,
                headers=reply.error.headers,
            )
        return materialize_project_query_data(reply)
    except (ProjectQueryUnavailable, OSError):
        raise HTTPException(status_code=503, detail="项目查询服务暂不可用") from None


async def prepare_transfer_projection(db, *, actor, transfer):
    """Retain the legacy precommit row/decode phase before postcommit hydration."""
    evidence = ProjectOwnerTransferEvidence(
        source="owner_transfer",
        operation_id=transfer.operation_id,
        new_owner_id=transfer.new_owner_id,
    )
    request = project_query_request(
        "get_project_projection",
        actor.id,
        getattr(actor.role, "value", actor.role),
        target_id=transfer.project_id,
        projection_evidence=evidence,
    )
    try:
        with db.no_autoflush:
            data = WorkspaceProjectQueryData(db)
            async with data.transaction() as transaction:
                raw = await transaction.step(request, "projection_row")
            facts = decode_project_query_step(
                request, "projection_row", None, raw
            ).facts
            if (
                decode_project_row(facts.row_json, target_id=transfer.project_id)
                is None
            ):
                raise HTTPException(status_code=404, detail="项目不存在")
        return evidence
    except ProjectQueryUnavailable:
        raise HTTPException(status_code=503, detail="项目查询服务暂不可用") from None
