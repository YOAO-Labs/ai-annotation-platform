"""Only outbound Agent connections; no platform user token substitution."""

import asyncio
import time
import uuid

from aap_site_agent_contract import ProtocolError, parse_json
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.schemas.site_agent_transport import (
    AgentChunkAck,
    AgentCommandAck,
    AgentPollRequest,
    AgentPollResponse,
)
from app.services import agent_transport as svc
from app.services.filesystem_storage import FilesystemStorageError
from app.services.content_snapshot import ContentError
from app.services.site_agents import SiteAgentError, authenticate

router = APIRouter(prefix="/site-agents")


def public_error(exc):
    return HTTPException(getattr(exc, "status", 422), detail={"code": exc.code})


async def identity(db, authorization, protocol, *, retired=False):
    if (
        not authorization
        or not authorization.startswith("Bearer ")
        or len(authorization) > 263
    ):
        raise HTTPException(401, detail={"code": "agent_unauthorized"})
    try:
        return await authenticate(
            db,
            authorization[7:],
            protocol_version=int(protocol),
            allow_retired_receipt=retired,
        )
    except ValueError as exc:
        if isinstance(exc, SiteAgentError):
            raise public_error(exc) from None
        raise HTTPException(
            426, detail={"code": "agent_protocol_incompatible"}
        ) from None
    except SiteAgentError as exc:
        raise public_error(exc) from None


async def bounded_body(request, limit):
    result = bytearray()
    async for part in request.stream():
        if len(result) + len(part) > limit:
            raise HTTPException(413, detail={"code": "agent_message_too_large"})
        result.extend(part)
    return bytes(result)


@router.post("/commands/poll", response_model=AgentPollResponse)
async def poll(
    payload: AgentPollRequest,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_agent_protocol: str = Header(default="1"),
):
    agent, credential = await identity(db, authorization, x_agent_protocol)
    try:
        await svc.observe(db, agent, credential, payload)
        await db.commit()
        until = time.monotonic() + 15
        while True:
            command = await svc.lease(db, agent.id, payload.session_id)
            await db.commit()
            if command is not None or time.monotonic() >= until:
                return {"protocol_version": 1, "command": command}
            await asyncio.sleep(0.25)
    except (ContentError, SiteAgentError, ProtocolError, FilesystemStorageError) as exc:
        await db.rollback()
        raise public_error(exc) from None


@router.post(
    "/commands/{command_id}/chunks",
    response_model=AgentChunkAck,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def chunk(
    command_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_agent_protocol: str = Header(default="1"),
    x_command_digest: str = Header(),
    x_chunk_offset: int = Header(ge=0),
    x_chunk_sha256: str = Header(),
):
    agent, credential = await identity(db, authorization, x_agent_protocol)
    body = await bounded_body(request, svc.CHUNK_BYTES)
    try:
        result = await svc.receive_chunk(
            db,
            command_id,
            agent,
            credential,
            x_command_digest,
            x_chunk_offset,
            x_chunk_sha256,
            body,
        )
        await db.commit()
        return result
    except (ContentError, SiteAgentError, ProtocolError, FilesystemStorageError) as exc:
        await db.rollback()
        raise public_error(exc) from None


@router.post("/commands/{command_id}/result", response_model=AgentCommandAck)
async def result(
    command_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_agent_protocol: str = Header(default="1"),
):
    # Authentication of a retained receipt cannot issue new reads or receive bytes.
    agent, credential = await identity(
        db, authorization, x_agent_protocol, retired=True
    )
    try:
        payload = parse_json(await bounded_body(request, 2 * 1024 * 1024))
        response = await svc.receive_result(db, command_id, agent, credential, payload)
        await db.commit()
        return response
    except (ContentError, SiteAgentError, ProtocolError, FilesystemStorageError) as exc:
        await db.rollback()
        raise public_error(exc) from None
