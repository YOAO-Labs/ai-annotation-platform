"""Administrator-owned identities and unauthenticated one-time pairing only."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.models.user import User
from app.deps import assert_request_scopes, get_db, require_roles
from app.schemas.site_agent import (
    AgentCreate,
    AgentCredentialsOut,
    AgentDrainRequest,
    AgentList,
    AgentOut,
    AgentPairingOut,
    AgentPairRequest,
    AgentRevisionRequest,
    SiteCreate,
    SiteList,
    SiteOut,
)
from app.services import site_agents as svc
from app.services.audit import AuditService

router = APIRouter(prefix="/site-agents", tags=["site-agents"])
super_admin = require_roles(UserRole.SUPER_ADMIN)


def http_error(exc):
    return HTTPException(exc.status, detail={"code": exc.code})


async def read_actor(request: Request, actor: User = Depends(super_admin)):
    assert_request_scopes(request, "datasets:read")
    return actor


async def write_actor(
    request: Request,
    actor: User = Depends(super_admin),
    db: AsyncSession = Depends(get_db),
):
    assert_request_scopes(request, "datasets:write")
    try:
        return await svc.lock_current_admin(
            db, actor, getattr(request.state, "api_key_id", None)
        )
    except svc.SiteAgentError as exc:
        raise http_error(exc) from None


async def audit(db, actor, request, action, identity):
    await AuditService.log(
        db,
        actor=actor,
        request=request,
        action=action,
        target_type="site_agent",
        target_id=str(identity),
        status_code=200,
    )


@router.get("/sites", response_model=SiteList)
async def list_sites(
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(read_actor),
):
    return await svc.list_sites(db, limit=limit, offset=offset)


@router.post("/sites", response_model=SiteOut, status_code=201)
async def create_site(
    payload: SiteCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(write_actor),
):
    try:
        site = await svc.create_site(db, payload, actor)
        await audit(db, actor, request, "site_agent.site_create", site.id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, detail={"code": "site_name_conflict"}) from None
    return site


@router.get("/agents", response_model=AgentList)
async def list_agents(
    site_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(read_actor),
):
    return await svc.list_agents(db, site_id=site_id, limit=limit, offset=offset)


@router.post("/sites/{site_id}/agents", response_model=AgentPairingOut, status_code=201)
async def create_agent(
    site_id: uuid.UUID,
    payload: AgentCreate,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(write_actor),
):
    try:
        result = await svc.create_agent(db, site_id, payload, actor)
    except svc.SiteAgentError as exc:
        raise http_error(exc) from None
    await audit(db, actor, request, "site_agent.create", result["agent_id"])
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/agents/{agent_id}/rotate", response_model=AgentPairingOut)
async def rotate_agent(
    agent_id: uuid.UUID,
    request: Request,
    response: Response,
    payload: AgentRevisionRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(write_actor),
):
    try:
        result = await svc.rotate(
            db,
            agent_id,
            expected_revision=payload.expected_revision if payload else None,
        )
    except svc.SiteAgentError as exc:
        raise http_error(exc) from None
    await audit(db, actor, request, "site_agent.rotate", agent_id)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/agents/{agent_id}/revoke", response_model=AgentOut)
async def revoke_agent(
    agent_id: uuid.UUID,
    request: Request,
    payload: AgentRevisionRequest | None = None,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(write_actor),
):
    try:
        agent = await svc.revoke(
            db,
            agent_id,
            expected_revision=payload.expected_revision if payload else None,
        )
    except svc.SiteAgentError as exc:
        raise http_error(exc) from None
    result = svc.agent_out(agent, await svc.now(db))
    await audit(db, actor, request, "site_agent.revoke", agent_id)
    await db.commit()
    return result


@router.post("/agents/{agent_id}/drain", response_model=AgentOut)
async def drain_agent(
    agent_id: uuid.UUID,
    payload: AgentDrainRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(write_actor),
):
    try:
        agent = await svc.set_draining(
            db,
            agent_id,
            expected_revision=payload.expected_revision,
            draining=payload.draining,
        )
    except svc.SiteAgentError as exc:
        raise http_error(exc) from None
    result = svc.agent_out(agent, await svc.now(db))
    await audit(db, actor, request, "site_agent.drain", agent_id)
    await db.commit()
    return result


@router.post("/pair", response_model=AgentCredentialsOut)
async def pair_agent(
    payload: AgentPairRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await svc.pair(db, payload)
    except svc.SiteAgentError as exc:
        raise http_error(exc) from None
    await audit(db, None, request, "site_agent.pair", result["agent_id"])
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return result
