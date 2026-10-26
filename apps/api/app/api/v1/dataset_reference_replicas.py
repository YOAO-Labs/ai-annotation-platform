"""Manager publication of directed, same-content physical replica grants."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.user import User
from app.deps import assert_request_scopes, get_db, require_roles
from app.schemas.dataset_reference_replica import (
    ReferenceReplicaCandidateList,
    ReferenceReplicaCreate,
    ReferenceReplicaList,
    ReferenceReplicaOut,
    ReferenceReplicaUpdate,
)
from app.services import dataset_reference_replicas as service
from app.services.audit import AuditService
from app.services.content_snapshot import ContentError

manager = require_roles(UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)


async def scopes(request: Request, actor: User = Depends(manager)):
    assert_request_scopes(
        request, "datasets:read" if request.method == "GET" else "datasets:write"
    )


router = APIRouter(dependencies=[Depends(scopes)])


async def audit(db, actor, request, item_id, action, replica_id):
    await AuditService.log(
        db,
        actor=actor,
        request=request,
        action="dataset_reference.replica_" + action,
        target_type="dataset_item",
        target_id=str(item_id),
        detail={"replica_id": str(replica_id)},
        status_code=200,
    )


@router.get(
    "/dataset-items/{item_id}/reference-replicas", response_model=ReferenceReplicaList
)
async def list_replicas(
    item_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    try:
        return await service.list_replicas(
            db, item_id, actor, limit=limit, offset=offset
        )
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None


@router.get(
    "/dataset-items/{item_id}/reference-replica-candidates",
    response_model=ReferenceReplicaCandidateList,
)
async def candidates(
    item_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    try:
        return await service.candidates(db, item_id, actor, limit=limit, offset=offset)
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None


@router.post(
    "/dataset-items/{item_id}/reference-replicas",
    response_model=ReferenceReplicaOut,
    status_code=201,
)
async def create(
    item_id: uuid.UUID,
    payload: ReferenceReplicaCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    try:
        result = await service.create(
            db,
            item_id,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    await audit(db, actor, request, item_id, "publish", result["id"])
    await db.commit()
    return result


@router.patch(
    "/dataset-items/{item_id}/reference-replicas/{replica_id}",
    response_model=ReferenceReplicaOut,
)
async def update(
    item_id: uuid.UUID,
    replica_id: uuid.UUID,
    payload: ReferenceReplicaUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    try:
        result = await service.update(
            db,
            item_id,
            replica_id,
            payload,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    await audit(db, actor, request, item_id, "prioritize", replica_id)
    await db.commit()
    return result


@router.delete(
    "/dataset-items/{item_id}/reference-replicas/{replica_id}", status_code=204
)
async def revoke(
    item_id: uuid.UUID,
    replica_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(manager),
):
    try:
        await service.revoke(
            db,
            item_id,
            replica_id,
            actor,
            api_key_id=getattr(request.state, "api_key_id", None),
        )
    except ContentError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    await audit(db, actor, request, item_id, "revoke", replica_id)
    await db.commit()
    return Response(status_code=204)
