"""One-time pairing and current authority, separate from transport operations."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
import uuid

from sqlalchemy import Uuid, column, func, select, table, text

from app.config import settings
from app.db.enums import UserRole
from app.schemas.site_agent import AgentLimits, AgentRoot
from app.services.dataset_source_access import key_authority
from app.services.identity_account_lock_data import IdentityAccountLockData

PROTOCOL_VERSION = 1
HEARTBEAT_TTL = timedelta(seconds=60)
_SECRET = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")

_SITE = table(
    "site_facts",
    column("id", Uuid),
    column("name"),
    column("region"),
    column("created_by", Uuid),
    column("created_at"),
    schema="yoao_assets_storage_v1",
)
_AGENT = table(
    "agent_facts",
    column("id", Uuid),
    column("site_id", Uuid),
    column("name"),
    column("protocol_version"),
    column("credential_revision"),
    column("pair_expires_at"),
    column("pairing_pending"),
    column("revoked_at"),
    column("draining"),
    column("session_id", Uuid),
    column("last_seen"),
    column("roots"),
    column("limits"),
    column("reported_limits"),
    column("created_by", Uuid),
    column("created_at"),
    schema="yoao_assets_storage_v1",
)
_IDENTITY = text(
    "SELECT (a.agent).*, a.authenticated_revision, a.credential_expires_at, "
    "a.credential_revoked_at FROM yoao_assets_storage_v1.agent_identity"
    "(:id, :revision, :verifier, :lock) AS a"
)
_CREATE_SITE = text(
    "SELECT * FROM yoao_assets_storage_v1.create_site(:actor,:id,:name,:region)"
)
_REGISTER = text(
    "SELECT yoao_assets_storage_v1.register_agent"
    "(:actor,:site,:id,:name,CAST(:limits AS jsonb),:pair_digest)"
)
_ROTATE = text(
    "SELECT * FROM yoao_assets_storage_v1.rotate_agent(:id,:revision,:pair_digest)"
)
_CONTROL = text(
    "SELECT a.code, (a.agent).* "
    "FROM yoao_assets_storage_v1.control_agent(:id,:revision,:revoke,:draining) AS a"
)
_PAIR = text(
    "SELECT * FROM yoao_assets_storage_v1.pair_agent(:id,:pair_digest,:token_digest)"
)


@dataclass(frozen=True, slots=True)
class CredentialFacts:
    agent_id: uuid.UUID
    revision: int
    expires_at: datetime
    revoked_at: datetime | None
    verifier: str | None = field(repr=False)


@dataclass(slots=True)
class AgentFacts:
    id: uuid.UUID
    site_id: uuid.UUID
    name: str
    protocol_version: int
    credential_revision: int
    pair_expires_at: datetime | None
    pairing_pending: bool
    revoked_at: datetime | None
    draining: bool
    session_id: uuid.UUID | None
    last_seen: datetime | None
    roots: list
    limits: dict
    reported_limits: dict | None
    created_by: uuid.UUID
    created_at: datetime


class SiteAgentError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def raise_identity_error(code):
    if code:
        raise SiteAgentError(
            code,
            {
                "agent_unauthorized": 401,
                "credential_expired": 401,
                "pair_code_invalid": 401,
                "agent_revoked": 403,
                "agent_unpaired": 403,
                "agent_not_found": 404,
                "site_not_found": 404,
                "agent_protocol_incompatible": 426,
            }.get(code, 409),
        )


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
    if agent.pairing_pending:
        raise SiteAgentError("agent_unpaired", 403)
    if credential.revoked_at is not None:
        raise SiteAgentError("agent_unauthorized", 401)
    if credential.expires_at <= stamp:
        raise SiteAgentError("credential_expired", 401)
    if credential.revision != agent.credential_revision:
        raise SiteAgentError("credential_revision_changed", 409)


async def get_identity(db, agent_id, *, revision=None, verifier=None, lock=None):
    if db.autoflush:
        await db.flush()
    result = await db.execute(
        _IDENTITY,
        {"id": agent_id, "revision": revision, "verifier": verifier, "lock": lock},
    )
    row = result.mappings().one_or_none()
    if row is None:
        return None, None
    data = dict(row)
    authenticated_revision = data.pop("authenticated_revision")
    expires_at = data.pop("credential_expires_at")
    revoked_at = data.pop("credential_revoked_at")
    return (
        AgentFacts(**data),
        CredentialFacts(
            data["id"], authenticated_revision, expires_at, revoked_at, verifier
        )
        if authenticated_revision is not None
        else None,
    )


async def authenticate(db, token, protocol_version=1, allow_retired_receipt=False):
    """Retired tokens authenticate only evidence; callers must reject all grants.

    No registration lock is acquired here. Transport owns Location -> business
    owner -> command locking, then repeats current authority before publication.
    """
    _protocol(protocol_version)
    if type(token) is not str or _SECRET.fullmatch(token) is None:
        raise SiteAgentError("agent_unauthorized", 401)
    agent, credential = await get_identity(
        db, None, verifier=_digest(token, "credential")
    )
    if agent is None or credential is None:
        raise SiteAgentError("agent_unauthorized", 401)
    _protocol(agent.protocol_version)
    if not allow_retired_receipt:
        validate_current(agent, credential, now=await now(db))
    return agent, credential


async def lock_current_admin(db, actor, api_key_id=None):
    proof = await key_authority(db, actor, api_key_id, "datasets:write")
    current = (
        await IdentityAccountLockData(db).lock_accounts(
            (actor.id,), mode="share", nowait=False
        )
    ).get(actor.id)
    if current is None or not current.is_active or current.role != UserRole.SUPER_ADMIN:
        raise SiteAgentError("site_agent_admin_required", 403)
    if await key_authority(db, current, api_key_id, "datasets:write") != proof:
        raise SiteAgentError("api_key_authority_revoked", 403)
    return current


async def create_site(db, payload, actor):
    row = await db.execute(
        _CREATE_SITE,
        {
            "actor": actor.id,
            "id": uuid.uuid4(),
            "name": payload.name,
            "region": payload.region,
        },
    )
    return row.mappings().one()


async def list_sites(db, *, limit=100, offset=0):
    total = await db.scalar(select(func.count()).select_from(_SITE))
    rows = await db.execute(
        select(_SITE)
        .order_by(_SITE.c.created_at.desc(), _SITE.c.id)
        .offset(offset)
        .limit(limit)
    )
    return {"items": rows.mappings().all(), "total": total}


async def create_agent(db, site_id, payload, actor):
    agent_id, code = uuid.uuid4(), secrets.token_urlsafe(32)
    expires = await db.scalar(
        _REGISTER,
        {
            "actor": actor.id,
            "site": site_id,
            "id": agent_id,
            "name": payload.name,
            "limits": payload.limits.model_dump_json(),
            "pair_digest": _digest(code, "pair"),
        },
    )
    if expires is None:
        raise SiteAgentError("site_not_found", 404)
    return {"agent_id": agent_id, "pair_code": code, "expires_at": expires}


async def rotate(db, agent_id, *, expected_revision=None):
    code = secrets.token_urlsafe(32)
    row = (
        (
            await db.execute(
                _ROTATE,
                {
                    "id": agent_id,
                    "revision": expected_revision,
                    "pair_digest": _digest(code, "pair"),
                },
            )
        )
        .mappings()
        .one()
    )
    raise_identity_error(row["code"])
    return {"agent_id": agent_id, "pair_code": code, "expires_at": row["expires_at"]}


async def _control(db, agent_id, expected_revision, *, revoke, draining):
    result = await db.execute(
        _CONTROL,
        {
            "id": agent_id,
            "revision": expected_revision,
            "revoke": revoke,
            "draining": draining,
        },
    )
    data = dict(result.mappings().one())
    raise_identity_error(data.pop("code"))
    return AgentFacts(**data)


async def revoke(db, agent_id, *, expected_revision=None):
    return await _control(db, agent_id, expected_revision, revoke=True, draining=None)


async def set_draining(db, agent_id, *, expected_revision, draining):
    return await _control(
        db, agent_id, expected_revision, revoke=False, draining=draining
    )


async def pair(db, payload):
    _protocol(payload.protocol_version)
    await db.flush()
    token = secrets.token_urlsafe(32)
    row = (
        (
            await db.execute(
                _PAIR,
                {
                    "id": payload.agent_id,
                    "pair_digest": _digest(payload.pair_code, "pair"),
                    "token_digest": _digest(token, "credential"),
                },
            )
        )
        .mappings()
        .one()
    )
    raise_identity_error(row["code"])
    return {
        "agent_id": payload.agent_id,
        "credential_revision": row["credential_revision"],
        "token": token,
        "expires_at": int(row["expires_at"].timestamp()),
    }


def state(agent, measured_at=None):
    stamp = measured_at or datetime.now(timezone.utc)
    if agent.revoked_at is not None:
        return "revoked"
    if agent.pairing_pending:
        return "pending"
    if agent.last_seen is None or agent.last_seen + HEARTBEAT_TTL <= stamp:
        return "offline"
    if agent.draining or not agent.roots or agent.reported_limits is None:
        return "degraded"
    return "online"


def agent_out(agent, measured_at=None):
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


async def list_agents(db, *, site_id=None, agent_id=None, limit=100, offset=0):
    filters = []
    if site_id is not None:
        filters.append(_AGENT.c.site_id == site_id)
    if agent_id is not None:
        filters.append(_AGENT.c.id == agent_id)
    total = await db.scalar(select(func.count()).select_from(_AGENT).where(*filters))
    rows = await db.execute(
        select(_AGENT)
        .where(*filters)
        .order_by(_AGENT.c.created_at.desc(), _AGENT.c.id)
        .offset(offset)
        .limit(limit)
    )
    stamp = await now(db)
    return {
        "items": [agent_out(AgentFacts(**row), stamp) for row in rows.mappings()],
        "total": total,
    }
