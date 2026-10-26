"""One-time pairing and current authority, separate from transport operations."""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
import uuid

from sqlalchemy import func, select

from app.config import settings
from app.db.enums import UserRole
from app.db.models.api_key import ApiKey
from app.db.models.site_agent import AgentCredential, AgentRegistration, Site
from app.db.models.user import User
from app.schemas.site_agent import AgentLimits, AgentRoot
from app.services.dataset_content import key_authority

PROTOCOL_VERSION = 1
PAIR_TTL = timedelta(minutes=10)
CREDENTIAL_TTL = timedelta(days=30)
HEARTBEAT_TTL = timedelta(seconds=60)
_SECRET = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")


class SiteAgentError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


async def now(db):
    return await db.scalar(select(func.clock_timestamp()))


def _digest(value, purpose):
    return hmac.new(
        settings.secret_key.encode(),
        ("site-agent:" + purpose + ":" + value).encode(),
        hashlib.sha256,
    ).hexdigest()


def _protocol(version):
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise SiteAgentError("agent_protocol_incompatible", 426)


def validate_current(agent, credential, now=None):
    """Check current authority without taking database or business locks."""
    if agent is None or credential is None:
        raise SiteAgentError("agent_unauthorized", 401)
    _protocol(agent.protocol_version)
    stamp = now or datetime.now(timezone.utc)
    if credential.agent_id != agent.id:
        raise SiteAgentError("agent_unauthorized", 401)
    if agent.revoked_at is not None:
        raise SiteAgentError("agent_revoked", 403)
    if agent.pair_digest is not None:
        raise SiteAgentError("agent_unpaired", 403)
    if credential.revoked_at is not None:
        raise SiteAgentError("agent_unauthorized", 401)
    if credential.expires_at <= stamp:
        raise SiteAgentError("credential_expired", 401)
    if credential.revision != agent.credential_revision:
        raise SiteAgentError("credential_revision_changed", 409)


async def authenticate(db, token, protocol_version=1, allow_retired_receipt=False):
    """Retired tokens authenticate only evidence; callers must reject all grants.

    No registration lock is acquired here. Transport owns Location -> business
    owner -> command locking, then repeats current authority before publication.
    """
    _protocol(protocol_version)
    if type(token) is not str or _SECRET.fullmatch(token) is None:
        raise SiteAgentError("agent_unauthorized", 401)
    row = (
        await db.execute(
            select(AgentRegistration, AgentCredential)
            .join(AgentCredential, AgentCredential.agent_id == AgentRegistration.id)
            .where(AgentCredential.token_digest == _digest(token, "credential"))
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise SiteAgentError("agent_unauthorized", 401)
    agent, credential = row
    _protocol(agent.protocol_version)
    if not allow_retired_receipt:
        validate_current(agent, credential, now=await now(db))
    return agent, credential


async def lock_current_admin(db, actor, api_key_id=None):
    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    current = await db.scalar(
        select(User)
        .where(User.id == actor.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if current is None or not current.is_active or current.role != UserRole.SUPER_ADMIN:
        raise SiteAgentError("site_agent_admin_required", 403)
    if api_key_id is not None:
        await db.scalar(
            select(ApiKey)
            .where(ApiKey.id == uuid.UUID(str(api_key_id)))
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    if await key_authority(db, current, api_key_id, "datasets:write") != proof:
        raise SiteAgentError("api_key_authority_revoked", 403)
    return current


async def create_site(db, payload, actor):
    site = Site(name=payload.name, region=payload.region, created_by=actor.id)
    db.add(site)
    await db.flush()
    return site


async def list_sites(db, *, limit=100, offset=0):
    total = await db.scalar(select(func.count()).select_from(Site))
    rows = list(
        await db.scalars(
            select(Site)
            .order_by(Site.created_at.desc(), Site.id)
            .offset(offset)
            .limit(limit)
        )
    )
    return {"items": rows, "total": total}


async def _agent(db, agent_id):
    agent = await db.scalar(
        select(AgentRegistration)
        .where(AgentRegistration.id == agent_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if agent is None:
        raise SiteAgentError("agent_not_found", 404)
    return agent


def _revision(agent, expected):
    if expected is not None and agent.credential_revision != expected:
        raise SiteAgentError("credential_revision_changed", 409)


def _pairing(agent, stamp):
    code = secrets.token_urlsafe(32)
    agent.pair_digest = _digest(code, "pair")
    agent.pair_expires_at = stamp + PAIR_TTL
    return {
        "agent_id": agent.id,
        "pair_code": code,
        "expires_at": agent.pair_expires_at,
    }


async def create_agent(db, site_id, payload, actor):
    site = await db.scalar(
        select(Site).where(Site.id == site_id).with_for_update(read=True)
    )
    if site is None:
        raise SiteAgentError("site_not_found", 404)
    agent = AgentRegistration(
        site_id=site.id,
        name=payload.name,
        protocol_version=PROTOCOL_VERSION,
        credential_revision=1,
        roots=[],
        limits=payload.limits.model_dump(),
        created_by=actor.id,
    )
    db.add(agent)
    await db.flush()
    result = _pairing(agent, await now(db))
    await db.flush()
    return result


async def _retire_credentials(db, agent, stamp):
    rows = list(
        await db.scalars(
            select(AgentCredential)
            .where(AgentCredential.agent_id == agent.id)
            .order_by(AgentCredential.revision)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    for credential in rows:
        credential.revoked_at = credential.revoked_at or stamp


async def rotate(db, agent_id, *, expected_revision=None):
    from app.db.models.site_agent_command import SiteAgentCommand

    agent = await _agent(db, agent_id)
    _revision(agent, expected_revision)
    if agent.revoked_at is not None:
        raise SiteAgentError("agent_revoked", 403)
    active = await db.scalar(
        select(SiteAgentCommand.id)
        .where(
            SiteAgentCommand.agent_id == agent.id,
            SiteAgentCommand.state.in_({"leased", "received", "unsettled"}),
        )
        .limit(1)
    )
    if not agent.draining or active is not None:
        raise SiteAgentError("agent_tasks_not_drained", 409)
    stamp = await now(db)
    await _retire_credentials(db, agent, stamp)
    agent.credential_revision += 1
    agent.session_id = agent.last_seen = agent.reported_limits = None
    agent.roots = []
    result = _pairing(agent, stamp)
    await db.flush()
    return result


async def revoke(db, agent_id, *, expected_revision=None):
    agent = await _agent(db, agent_id)
    _revision(agent, expected_revision)
    stamp = await now(db)
    agent.revoked_at = agent.revoked_at or stamp
    agent.draining = True
    agent.pair_digest = agent.pair_expires_at = None
    await _retire_credentials(db, agent, stamp)
    await db.flush()
    return agent


async def set_draining(db, agent_id, *, expected_revision, draining):
    agent = await _agent(db, agent_id)
    _revision(agent, expected_revision)
    if agent.revoked_at is not None:
        raise SiteAgentError("agent_revoked", 403)
    agent.draining = draining
    await db.flush()
    return agent


async def pair(db, payload):
    _protocol(payload.protocol_version)
    agent = await _agent(db, payload.agent_id)
    if agent.revoked_at is not None:
        raise SiteAgentError("agent_revoked", 403)
    stamp = await now(db)
    if (
        agent.pair_digest is None
        or agent.pair_expires_at is None
        or agent.pair_expires_at <= stamp
        or not hmac.compare_digest(
            agent.pair_digest, _digest(payload.pair_code, "pair")
        )
    ):
        raise SiteAgentError("pair_code_invalid", 401)
    _protocol(agent.protocol_version)
    token = secrets.token_urlsafe(32)
    expires = stamp + CREDENTIAL_TTL
    db.add(
        AgentCredential(
            agent_id=agent.id,
            revision=agent.credential_revision,
            token_digest=_digest(token, "credential"),
            expires_at=expires,
        )
    )
    agent.pair_digest = agent.pair_expires_at = None
    await db.flush()
    return {
        "agent_id": agent.id,
        "credential_revision": agent.credential_revision,
        "token": token,
        "expires_at": int(expires.timestamp()),
    }


def state(agent, measured_at=None):
    stamp = measured_at or datetime.now(timezone.utc)
    if agent.revoked_at is not None:
        return "revoked"
    if agent.pair_digest is not None:
        return "pending"
    if agent.last_seen is None or agent.last_seen + HEARTBEAT_TTL <= stamp:
        return "offline"
    if agent.draining or not agent.roots or agent.reported_limits is None:
        return "degraded"
    return "online"


def agent_out(agent, measured_at=None):
    # Project only approved public fields even if a future transport stores more.
    return {
        **{
            field: getattr(agent, field)
            for field in (
                "id",
                "site_id",
                "name",
                "protocol_version",
                "credential_revision",
                "draining",
                "revoked_at",
                "pair_expires_at",
                "session_id",
                "last_seen",
                "created_by",
                "created_at",
            )
        },
        "state": state(agent, measured_at),
        "roots": [
            AgentRoot.model_validate(
                {
                    "root_handle": root["root_handle"],
                    "root_revision": root["root_revision"],
                }
            ).model_dump()
            for root in agent.roots
        ],
        "limits": AgentLimits.model_validate(agent.limits).model_dump(),
        "reported_limits": AgentLimits.model_validate(
            {
                key: agent.reported_limits[key]
                for key in AgentLimits.model_fields
                if key in agent.reported_limits
            }
        ).model_dump()
        if agent.reported_limits is not None
        else None,
    }


async def list_agents(db, *, site_id=None, limit=100, offset=0):
    query = select(AgentRegistration)
    count = select(func.count()).select_from(AgentRegistration)
    if site_id is not None:
        query = query.where(AgentRegistration.site_id == site_id)
        count = count.where(AgentRegistration.site_id == site_id)
    total = await db.scalar(count)
    rows = list(
        await db.scalars(
            query.order_by(AgentRegistration.created_at.desc(), AgentRegistration.id)
            .offset(offset)
            .limit(limit)
        )
    )
    stamp = await now(db)
    return {"items": [agent_out(row, stamp) for row in rows], "total": total}
