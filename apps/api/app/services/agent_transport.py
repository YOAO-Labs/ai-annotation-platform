"""Outbound Agent I/O receipts. Business checkpoint ownership stays with callers."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import stat
import tempfile
import time
import uuid

from yoao_site_agent_contract import digest, validate_command, validate_result
import redis.asyncio as aioredis
from sqlalchemy import inspect, select, text

from app.config import settings
from app.db.models.site_agent_command import SiteAgentCommand as Command
from app.services import filesystem_storage as fs, storage_access
from app.services.content_snapshot import ContentError
from app.services.site_agents import (
    SiteAgentError,
    get_identity,
    raise_identity_error,
    validate_current,
)
from app.services.storage_registry_data import get_location

MAX_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
DISPATCH_LOCK = 7164521385097
SHA = re.compile(r"[a-f0-9]{64}\Z")

_RECEIPT = (
    select(Command)
    .from_statement(
        text("SELECT * FROM yoao_assets_storage_v1.agent_command_receipt(:id)")
    )
    .execution_options(populate_existing=True)
)
_ENQUEUE = (
    select(Command)
    .from_statement(
        text(
            "SELECT * FROM yoao_assets_storage_v1.enqueue_agent_command"
            "(:id,:location,:agent,:revision,:operation,CAST(:payload AS jsonb),:digest,:expires)"
        )
    )
    .execution_options(populate_existing=True)
)
_OBSERVE = text(
    "SELECT yoao_assets_storage_v1.observe_agent"
    "(:agent,:revision,:verifier,:session,CAST(:roots AS jsonb),CAST(:limits AS jsonb))"
)
_CANDIDATES = (
    select(Command)
    .from_statement(
        text(
            "SELECT * FROM yoao_assets_storage_v1.agent_lease_candidates(:agent,:revision,:verifier,:session)"
        )
    )
    .execution_options(populate_existing=True)
)
_CLAIM = (
    select(Command)
    .from_statement(
        text(
            "SELECT * FROM yoao_assets_storage_v1.claim_agent_command"
            "(:id,:agent,:revision,:verifier,:session,:rejection)"
        )
    )
    .execution_options(populate_existing=True)
)
_LOCK_COMMAND = (
    select(Command)
    .from_statement(
        text(
            "SELECT * FROM yoao_assets_storage_v1.lock_agent_command(:id,:agent,:revision,:verifier)"
        )
    )
    .execution_options(populate_existing=True)
)
_CHUNK = text(
    "SELECT yoao_assets_storage_v1.record_agent_chunk(:id,:agent,:revision,:verifier,:digest,:offset,:size,:checksum)"
)
_RESULT = (
    select(Command)
    .from_statement(
        text(
            "SELECT * FROM yoao_assets_storage_v1.record_agent_result"
            "(:id,:agent,:revision,:verifier,:digest,CAST(:result AS jsonb),:result_digest,:rejection)"
        )
    )
    .execution_options(populate_existing=True)
)
_LOCK_ACK = (
    select(Command)
    .from_statement(text("SELECT * FROM yoao_assets_storage_v1.lock_agent_ack(:id)"))
    .execution_options(populate_existing=True)
)
_ACK = text(
    "SELECT yoao_assets_storage_v1.acknowledge_agent_result(:id,:digest,:accepted,:code)"
)
_CLEANUP = text("SELECT * FROM yoao_assets_storage_v1.cleanup_agent_relays()")
_RELEASE = (
    select(Command)
    .from_statement(
        text("SELECT * FROM yoao_assets_storage_v1.release_agent_relay(:id,:digest)")
    )
    .execution_options(populate_existing=True)
)
_EXPIRE = (
    select(Command)
    .from_statement(
        text("SELECT * FROM yoao_assets_storage_v1.expire_agent_command(:id)")
    )
    .execution_options(populate_existing=True)
)
_ABANDON = (
    select(Command)
    .from_statement(
        text(
            "SELECT * FROM yoao_assets_storage_v1.abandon_agent_command(:id,:operation,:code)"
        )
    )
    .execution_options(populate_existing=True)
)


def now():
    return datetime.now(timezone.utc)


def failure(code, status=409, *, unsettled=False):
    exc = ContentError(code, status)
    return storage_access.mark_unsettled(exc) if unsettled else exc


def redis_client():
    return aioredis.from_url(
        settings.redis_url, socket_connect_timeout=1, socket_timeout=1
    )


def relay_key(command_id):
    return f"yoao:site-agent:relay:{command_id}"


async def get_receipt(db, command_id):
    return await db.scalar(_RECEIPT, {"id": command_id})


def _proof(credential):
    return {
        "agent": credential.agent_id,
        "revision": credential.revision,
        "verifier": credential.verifier,
    }


async def _identity(db, row):
    agent, credential = await get_identity(
        db, row.agent_id, revision=row.credential_revision
    )
    validate_current(agent, credential)
    if agent.draining and row.lease_session_id is None:
        raise SiteAgentError("agent_draining", 409)
    roots = {(r["root_handle"], r["root_revision"]) for r in agent.roots}
    if (row.payload["root_handle"], row.payload["root_revision"]) not in roots:
        raise SiteAgentError("agent_root_revision_changed", 409)
    return agent


async def _authority(db, row):
    await _identity(db, row)
    location = await get_location(db, row.location_id)
    fence = row.payload["fence"]
    if (
        location is None
        or not location.enabled
        or location.kind != "agent"
        or location.config_revision != fence["location_revision"]
        or location.config.get("agent_id") != str(row.agent_id)
        or location.config.get("root_handle") != row.payload["root_handle"]
        or location.config.get("root_revision") != row.payload["root_revision"]
    ):
        raise failure("agent_location_changed")
    await storage_access.check(
        db, location.id, expected_revision=fence["maintenance_revision"]
    )
    if now() >= row.expires_at:
        raise failure("agent_command_expired")
    return location


async def enqueue(
    db,
    location,
    operation,
    arguments,
    *,
    fence,
    operation_id,
    max_bytes=MAX_BYTES,
    ttl_seconds=60,
):
    """Caller has authorized the business operation; commit before waiting."""
    if location.kind != "agent" or not location.enabled:
        raise failure("reference_backend_not_supported")
    agent, credential = await get_identity(
        db, uuid.UUID(location.config["agent_id"]), lock="share"
    )
    if agent is None:
        raise failure("agent_unavailable", 503)
    validate_current(agent, credential)
    issued = int(time.time())
    expires = min(
        issued + min(120, ttl_seconds), int(credential.expires_at.timestamp())
    )
    config = location.config
    arguments = dict(arguments)
    arguments["relative_path"] = "/".join(
        p
        for p in [config.get("relative_path", ""), arguments.get("relative_path", "")]
        if p
    )
    payload = validate_command(
        {
            "protocol_version": 1,
            "command_id": str(uuid.uuid4()),
            "agent_id": str(agent.id),
            "credential_revision": agent.credential_revision,
            "root_handle": config["root_handle"],
            "root_revision": config["root_revision"],
            "operation": operation,
            "arguments": arguments,
            "issued_at": issued,
            "expires_at": expires,
            "max_bytes": min(max_bytes, MAX_BYTES, agent.limits["max_bytes"]),
            "bandwidth_bytes_per_second": min(
                agent.limits["bandwidth_bytes_per_second"],
                (agent.reported_limits or {}).get(
                    "bandwidth_bytes_per_second",
                    agent.limits["bandwidth_bytes_per_second"],
                ),
            ),
            "timeout_seconds": 10,
            "fence": {
                **fence,
                "location_id": str(location.id),
                "location_revision": location.config_revision,
            },
        }
    )
    row = await db.scalar(
        _ENQUEUE,
        {
            "id": uuid.UUID(payload["command_id"]),
            "agent": agent.id,
            "revision": agent.credential_revision,
            "location": location.id,
            "operation": operation_id,
            "payload": json.dumps(payload),
            "digest": digest(payload),
            "expires": datetime.fromtimestamp(expires, timezone.utc),
        },
    )
    if row is None:
        raise failure("agent_location_changed")
    return row


async def observe(db, agent, credential, payload):
    if (
        payload.agent_id != agent.id
        or payload.credential_revision != credential.revision
    ):
        raise SiteAgentError("agent_identity_mismatch", 403)
    raise_identity_error(
        await db.scalar(
            _OBSERVE,
            {
                **_proof(credential),
                "session": payload.session_id,
                "roots": json.dumps([root.model_dump() for root in payload.roots]),
                "limits": payload.limits.model_dump_json(),
            },
        )
    )


async def lease(db, agent_id, session_id, *, credential):
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": DISPATCH_LOCK})
    agent, current = await get_identity(
        db,
        agent_id,
        revision=credential.revision,
        verifier=credential.verifier,
        lock="share",
    )
    validate_current(agent, current)
    await cleanup_terminal(db)
    proof = {**_proof(credential), "session": session_id}
    rows = (await db.scalars(_CANDIDATES, proof)).all()
    if rows and rows[0].lease_session_id is not None:
        prior = rows[0]
        if now().timestamp() >= prior.payload["expires_at"]:
            return prior.payload
        try:
            await _authority(db, prior)
        except (ContentError, SiteAgentError):
            return None
        return prior.payload
    for row in rows:
        try:
            await _authority(db, row)
        except (ContentError, SiteAgentError) as exc:
            await db.scalar(_CLAIM, {**proof, "id": row.id, "rejection": exc.code})
            continue
        if await db.scalar(_CLAIM, {**proof, "id": row.id, "rejection": None}):
            return row.payload
    return None


async def _command(db, command_id, agent, credential):
    if credential.agent_id != agent.id:
        raise failure("agent_command_not_found", 404)
    row = await db.scalar(_LOCK_COMMAND, {"id": command_id, **_proof(credential)})
    if row is None:
        raise failure("agent_command_not_found", 404)
    if row.lease_session_id is None:
        raise failure("agent_command_not_leased")
    return row


async def receive_chunk(
    db, command_id, agent, credential, command_digest, offset, checksum, data
):
    row = await _command(db, command_id, agent, credential)
    if (
        row.command_digest != command_digest
        or len(data) > CHUNK_BYTES
        or not data
        or not SHA.fullmatch(checksum)
        or hashlib.sha256(data).hexdigest() != checksum
    ):
        raise failure("agent_chunk_mismatch", 422)
    await _authority(db, row)
    if row.payload["operation"] != "read" or row.state not in {
        "leased",
        "received",
        "applied",
    }:
        raise failure("agent_chunk_not_expected")
    prior = row.chunks.get(str(offset))
    receipt = {"sha256": checksum, "size": len(data)}
    if prior is not None:
        if prior != receipt:
            raise failure("agent_chunk_replay_conflict")
        return {
            "command_id": str(row.id),
            "command_digest": row.command_digest,
            "offset": offset + len(data),
            "sha256": checksum,
        }
    if (
        row.state != "leased"
        or offset != row.received_bytes
        or offset + len(data) > row.payload["max_bytes"]
        or len(row.chunks) >= 1024
    ):
        raise failure("agent_chunk_offset_invalid")
    client = redis_client()
    try:
        key = relay_key(row.id)
        existing = await client.hget(key, str(offset))
        if existing is not None and existing != data:
            raise failure("agent_chunk_replay_conflict")
        await client.hset(key, str(offset), data)
        await client.expireat(key, int(row.expires_at.timestamp()) + 30)
    except aioredis.RedisError:
        raise failure("agent_relay_unavailable", 503) from None
    finally:
        await client.aclose()
    if not await db.scalar(
        _CHUNK,
        {
            **_proof(credential),
            "id": row.id,
            "digest": command_digest,
            "offset": offset,
            "size": len(data),
            "checksum": checksum,
        },
    ):
        raise failure("agent_chunk_not_expected")
    return {
        "command_id": str(row.id),
        "command_digest": row.command_digest,
        "offset": row.received_bytes,
        "sha256": checksum,
    }


def _int(value, low=0):
    if type(value) is not int or not low <= value <= 2**63 - 1:
        raise failure("agent_result_invalid", 422)


def _pair(value):
    if type(value) is not list or len(value) != 2:
        raise failure("agent_result_invalid", 422)
    for part in value:
        _int(part)


def validate_payload(command, result):
    """Do not expose the trusted local-producer manifest seam to a remote peer."""
    if result["status"] == "failed":
        return
    payload = result["result"]
    operation = command["operation"]
    if operation == "read":
        if (
            set(payload) != {"sha256", "size_bytes", "observation"}
            or not isinstance(payload["sha256"], str)
            or not SHA.fullmatch(payload["sha256"])
        ):
            raise failure("agent_result_invalid", 422)
        _int(payload["size_bytes"])
        if payload["size_bytes"] > command["max_bytes"]:
            raise failure("agent_result_invalid", 422)
        args = command["arguments"]
        if (
            args["expected_sha256"] is not None
            and payload["sha256"] != args["expected_sha256"]
        ) or (
            args["expected_size"] is not None
            and payload["size_bytes"] != args["expected_size"]
        ):
            raise failure("content_mismatch")
        checked = validate_command(
            {**command, "arguments": {**args, "expected": payload["observation"]}}
        )
        if checked["arguments"]["expected"].get("size") != payload["size_bytes"]:
            raise failure("agent_result_invalid", 422)
        if args["expected"] and any(
            payload["observation"].get(k) != v for k, v in args["expected"].items()
        ):
            raise failure("source_changed")
    elif operation == "scan":
        if (
            set(payload) != {"identity", "digest", "entries"}
            or type(payload["entries"]) is not list
            or len(payload["entries"]) > 5000
        ):
            raise failure("agent_result_invalid", 422)
        _pair(payload["identity"])
        names = []
        for entry in payload["entries"]:
            if type(entry) is not dict or set(entry) != {
                "name",
                "kind",
                "identity",
                "mode",
                "size",
                "mtime_ns",
                "ctime_ns",
            }:
                raise failure("agent_result_invalid", 422)
            name = fs.relative_path(entry["name"])
            if (
                not name
                or "/" in name
                or type(entry["kind"]) is not str
                or entry["kind"] not in {"file", "directory", "symlink", "special"}
            ):
                raise failure("agent_result_invalid", 422)
            _pair(entry["identity"])
            for key in ("mode", "size", "mtime_ns", "ctime_ns"):
                _int(entry[key])
            expected_kind = (
                "directory"
                if stat.S_ISDIR(entry["mode"])
                else "file"
                if stat.S_ISREG(entry["mode"])
                else "symlink"
                if stat.S_ISLNK(entry["mode"])
                else "special"
            )
            if expected_kind != entry["kind"]:
                raise failure("agent_result_invalid", 422)
            names.append(name)
        checksum = hashlib.sha256(
            json.dumps(
                [payload["identity"], payload["entries"]],
                sort_keys=True,
                ensure_ascii=True,
            ).encode()
        ).hexdigest()
        if names != sorted(set(names)) or payload["digest"] != checksum:
            raise failure("agent_manifest_mismatch", 422)
    elif operation == "browse":
        if (
            set(payload) != {"names", "has_more", "snapshot"}
            or type(payload["names"]) is not list
            or len(payload["names"]) > command["arguments"]["limit"]
            or type(payload["has_more"]) is not bool
            or not isinstance(payload["snapshot"], str)
            or not SHA.fullmatch(payload["snapshot"])
        ):
            raise failure("agent_result_invalid", 422)
        for name in payload["names"]:
            if not fs.relative_path(name) or "/" in name:
                raise failure("agent_result_invalid", 422)
        if payload["names"] != sorted(set(payload["names"])):
            raise failure("agent_result_invalid", 422)
    elif operation == "probe":
        if (
            not {"ok", "code", "capabilities", "anchor"} <= payload.keys()
            or payload.keys() - {"ok", "code", "capabilities", "anchor", "host_id"}
            or type(payload["ok"]) is not bool
        ):
            raise failure("agent_result_invalid", 422)
        capabilities = payload["capabilities"]
        if (
            type(capabilities) is not dict
            or set(capabilities)
            != {"list", "read", "range", "write", "delete", "presign"}
            or any(
                v not in {"verified", "unknown", "unsupported"}
                for v in capabilities.values()
            )
            or any(
                capabilities[k] != "unsupported" for k in ("write", "delete", "presign")
            )
        ):
            raise failure("agent_result_invalid", 422)
        validate_command(
            {
                **command,
                "arguments": {
                    **command["arguments"],
                    "expected_anchor": payload["anchor"],
                },
            }
        )
        if payload["ok"] and payload["anchor"] is None:
            raise failure("agent_result_invalid", 422)


def acknowledgement(row):
    state = row.state if row.state in {"applied", "rejected"} else "received"
    return {
        "protocol_version": 1,
        "command_id": str(row.id),
        "command_digest": row.command_digest,
        "result_digest": row.result_digest,
        "state": state,
        "code": (row.error_code or "agent_result_rejected")
        if state == "rejected"
        else None,
    }


async def receive_result(db, command_id, agent, credential, value):
    result = validate_result(value)
    row = await _command(db, command_id, agent, credential)
    if (
        result["command_id"] != str(row.id)
        or result["command_digest"] != row.command_digest
    ):
        raise failure("agent_result_identity_mismatch")
    result_digest = digest(result)
    rejected = None
    if row.result_digest is not None:
        if row.result_digest != result_digest:
            raise failure("agent_result_replay_conflict")
        if row.state != "received":
            return acknowledgement(row)
    else:
        validate_payload(row.payload, result)
    try:
        await _authority(db, row)
    except (ContentError, SiteAgentError) as exc:
        rejected = exc.code
    if (
        row.result_digest is None
        and not rejected
        and row.payload["operation"] == "read"
        and result["status"] == "completed"
        and row.received_bytes != result["result"]["size_bytes"]
    ):
        raise failure("agent_content_incomplete")
    row = await db.scalar(
        _RESULT,
        {
            **_proof(credential),
            "id": row.id,
            "digest": row.command_digest,
            "result": json.dumps(result),
            "result_digest": result_digest,
            "rejection": rejected,
        },
    )
    if row is None:
        raise failure("agent_result_replay_conflict")
    return acknowledgement(row)


@dataclass
class RelayStage:
    file: object
    sha256: str
    size: int
    observation: dict

    def close(self):
        self.file.close()


async def read_stage(row):
    expected = row.result["result"]
    body = tempfile.TemporaryFile(mode="w+b")
    client = redis_client()
    try:
        checksum, size = hashlib.sha256(), 0
        for offset, receipt in sorted(
            row.chunks.items(), key=lambda entry: int(entry[0])
        ):
            data = await client.hget(relay_key(row.id), offset)
            if (
                int(offset) != size
                or data is None
                or len(data) != receipt["size"]
                or hashlib.sha256(data).hexdigest() != receipt["sha256"]
            ):
                raise failure("agent_relay_incomplete", 503)
            body.write(data)
            checksum.update(data)
            size += len(data)
        if size != expected["size_bytes"] or checksum.hexdigest() != expected["sha256"]:
            raise failure("content_mismatch")
        body.seek(0)
        return RelayStage(body, checksum.hexdigest(), size, expected["observation"])
    except BaseException:
        body.close()
        raise
    finally:
        await client.aclose()


async def acknowledge(db, row, *, accepted, code=None):
    """Call after business fences, in the same transaction as the owner checkpoint."""
    identity = inspect(row).identity if row is not None else None
    if identity is None:
        raise failure("agent_command_not_found", 404)
    expected_digest = inspect(row).dict.get("result_digest")
    current = await db.scalar(_LOCK_ACK, {"id": identity[0]})
    if (
        current is None
        or (expected_digest is not None and current.result_digest != expected_digest)
        or current.closed is not True
    ):
        raise failure("agent_result_unsettled", 503, unsettled=True)
    if current.state == "rejected" and accepted:
        raise failure(current.error_code or "agent_result_rejected")
    if current.state in {"applied", "rejected"}:
        return
    if accepted:
        await _authority(db, current)
    if not await db.scalar(
        _ACK,
        {
            "id": current.id,
            "digest": current.result_digest,
            "accepted": accepted,
            "code": code,
        },
    ):
        raise failure("agent_result_unsettled", 503, unsettled=True)


async def cleanup_terminal(db):
    """Only delete relay bytes after the owner transaction made its ACK durable."""
    rows = (await db.execute(_CLEANUP)).mappings().all()
    if not rows:
        return
    client = redis_client()
    try:
        for row in rows:
            await client.delete(relay_key(row.id))
            await db.scalar(_RELEASE, {"id": row.id, "digest": row.result_digest})
    except aioredis.RedisError:
        raise failure("agent_relay_unavailable", 503) from None
    finally:
        await client.aclose()


async def wait(db, command_id):
    """No source locks span network waiting. Return a receipt, not a new task."""
    while True:
        row = await get_receipt(db, command_id)
        if row is None:
            raise failure("agent_command_not_found", 404)
        if row.result is not None:
            await db.commit()
            if not row.closed:
                raise failure("storage_operation_unsettled", 503, unsettled=True)
            if row.state == "rejected" or row.result["status"] == "failed":
                raise failure(row.error_code or "agent_operation_failed", 503)
            return row
        if now() >= row.expires_at:
            row = await db.scalar(_EXPIRE, {"id": command_id})
            await db.commit()
            if row is None:
                continue
            raise failure(
                "agent_command_expired", 504, unsettled=row.lease_session_id is not None
            )
        await db.commit()
        await asyncio.sleep(0.1)


async def exchange(
    db,
    location,
    operation,
    arguments,
    *,
    fence,
    purpose,
    node_id=None,
    expected_revision=None,
    ttl_seconds=45,
    on_acquired=None,
):
    """Obtain a remote result with independent physical I/O admission.

    The caller must commit its business transaction before this operation,
    revalidate its owner fences afterward and acknowledge in that transaction.
    """
    command_id = None
    async with await storage_access.acquire(
        db, location.id, purpose, expected_revision=expected_revision, node_id=node_id
    ) as permit:
        try:
            if on_acquired is not None:
                await on_acquired()
            row = await enqueue(
                db,
                location,
                operation,
                arguments,
                fence={
                    **fence,
                    "maintenance_revision": permit.revision,
                    "purpose": purpose,
                },
                operation_id=permit.id,
                ttl_seconds=ttl_seconds,
            )
            command_id = row.id
            await db.commit()
            return await wait(db, command_id)
        except BaseException as exc:
            if command_id is not None:
                storage_access.mark_unsettled(exc)
            await db.rollback()
            if command_id is not None:
                row = await db.scalar(
                    _ABANDON,
                    {
                        "id": command_id,
                        "operation": permit.id,
                        "code": getattr(exc, "code", "agent_command_interrupted"),
                    },
                )
                await db.commit()
                if row is None or row.closed is True or row.lease_session_id is None:
                    await permit._settle()
                    if getattr(exc, "storage_operation_unsettled", False):
                        del exc.storage_operation_unsettled
            if isinstance(exc, SiteAgentError):
                raise failure(
                    exc.code,
                    exc.status,
                    unsettled=getattr(exc, "storage_operation_unsettled", False),
                ) from None
            raise
