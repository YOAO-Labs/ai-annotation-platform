"""Bounded source observations; never commit or lock a caller's business rows."""

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import uuid

from fastapi import HTTPException
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from app.services import storage_access
from app.services.storage_fault_events import append_observation

log = logging.getLogger(__name__)
ACTION = "storage.fault"
RECOVERED_ACTION = "storage.fault.recovered"

_READ_FACTS = text(
    "SELECT yoao_assets_v1.read_storage_fault_facts("
    ":location_id, :source_id, :node_id, :preview, :read_subject)"
)
_RESERVE = text(
    "SELECT * FROM yoao_assets_v1.reserve_storage_fault_observation("
    ":location_id, :source_id, :operation, :node_id, :scopes, :evidence)"
).bindparams(bindparam("scopes", type_=JSONB), bindparam("evidence", type_=JSONB))
_RECORD = text(
    "SELECT * FROM yoao_assets_v1.record_storage_fault_observation("
    ":sequence, :seal, :location_id, :source_id, :operation, :node_id, "
    ":scopes, :evidence, :failure_category)"
).bindparams(bindparam("scopes", type_=JSONB), bindparam("evidence", type_=JSONB))
_PAGE = text(
    "SELECT * FROM yoao_assets_v1.storage_fault_page("
    ":actor_id, :location_id, :state, :limit, :offset)"
)
_VISIBLE_LOCATION = text(
    "SELECT yoao_assets_v1.storage_fault_location_visible(:actor_id, :location_id)"
)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def _anchor(location, node_id, probe_anchor):
    config = location["config"]
    if location["kind"] == "agent":
        if config.get("agent_anchor") is not None:
            return config["agent_anchor"]
        node_id = f"agent:{config['agent_id']}"
    if node_id == config.get("registration_node"):
        return config["registration_anchor"]
    return probe_anchor


def _common_facts(location, source, node_id, anchor, maintenance_revision, facts):
    return {
        "location_revision": location["revision"],
        "source_revision": source["revision"],
        "connection_revision": facts["connection_revision"]
        if location["connection_id"] == facts["connection_id"]
        else None,
        "maintenance_revision": maintenance_revision,
        "node_id": node_id,
        "anchor": digest(anchor),
        "locator": digest([location["kind"], location["config"], source["config"]]),
    }


def preview_facts(location, source, facts):
    return {
        "source_policy_revision": source["policy_revision"],
        "source_policy": source["cache_policy"],
        "location_policy": location["config"].get("cache_policy", "preview"),
        "binding": facts["binding"],
    }


async def _read_facts(
    connection, location_id, source_id, node_id, preview, read_subject
):
    return await connection.scalar(
        _READ_FACTS,
        {
            "location_id": location_id,
            "source_id": source_id,
            "node_id": node_id,
            "preview": preview,
            "read_subject": read_subject,
        },
    )


def _scope_arguments(location_id, source_id, operation, node_id, scopes, evidence):
    return {
        "location_id": location_id,
        "source_id": source_id,
        "operation": operation,
        "node_id": node_id,
        "scopes": scopes,
        "evidence": evidence,
    }


@dataclass(frozen=True, repr=False)
class Observation:
    sequence: int
    seal: bytes
    location_id: uuid.UUID
    source_id: uuid.UUID
    operation: str
    node_id: str
    scopes: dict
    evidence: dict
    actor_id: str | None
    request_id: str | None


async def _bounds(connection):
    await connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
    await connection.execute(text("SET LOCAL statement_timeout = '1s'"))


async def prepare(
    db,
    *,
    location=None,
    source=None,
    location_id=None,
    source_id=None,
    operation,
    node_id,
    anchor=None,
    maintenance_revision,
    path_digest=None,
    expected=None,
    asset_id=None,
    target=None,
    preview_spec=None,
    root_only=False,
    actor_id=None,
    request_id=None,
    evidence=None,
    expected_context=None,
    preview_dependencies=None,
):
    """Reserve order before source I/O, using an independent caller Engine."""
    try:
        if location is not None and source is not None:
            if source.location_id != location.id:
                return None
            location_id, source_id = location.id, source.id
            frozen_location = {
                "kind": location.kind,
                "config": location.config,
                "revision": location.config_revision,
                "connection_id": str(location.connection_id)
                if location.connection_id
                else None,
            }
            frozen_source = {
                "config": source.config,
                "revision": source.config_revision,
                "dataset_id": str(source.dataset_id) if source.dataset_id else None,
                "policy_revision": source.policy_revision,
                "cache_policy": source.cache_policy,
            }
        elif location is None and source is None and location_id and source_id:
            frozen_location = frozen_source = None
        else:
            return None

        async with asyncio.timeout(2):
            async with storage_access.engine(db).begin() as connection:
                await _bounds(connection)
                facts = await _read_facts(
                    connection,
                    location_id,
                    source_id,
                    node_id,
                    preview_spec is not None and preview_dependencies is None,
                    frozen_location is None,
                )
                location = (
                    frozen_location
                    if frozen_location is not None
                    else facts["location"]
                )
                source = frozen_source if frozen_source is not None else facts["source"]
                if location is None or source is None:
                    return None
                if expected_context is not None and (
                    source["revision"] != expected_context["source_revision"]
                    or location["revision"] != expected_context["location_revision"]
                ):
                    return None
                if anchor is None:
                    anchor = _anchor(location, node_id, facts["probe_anchor"])
                common = _common_facts(
                    location, source, node_id, anchor, maintenance_revision, facts
                )
                subject = {"common": common, "path_digest": path_digest}
                categories = {"root_unavailable": {"common": common}}
                if not root_only:
                    categories["access_denied"] = {
                        **subject,
                        "operation": operation,
                        "target": digest(target),
                    }
                    if expected is not None:
                        categories["source_changed"] = {
                            **subject,
                            "content": digest(expected),
                            "expected_sha256": expected
                            if isinstance(expected, str)
                            else None,
                        }
                if preview_spec is not None:
                    categories = {
                        "preview_failed": {
                            **subject,
                            "asset_id": str(asset_id),
                            "expected_sha256": expected,
                            "spec": preview_spec,
                            "dependencies": preview_dependencies
                            if preview_dependencies is not None
                            else preview_facts(location, source, facts),
                        }
                    }
                scopes = {
                    category: {
                        "key": digest(
                            [str(location_id), str(source_id), category, values]
                        ),
                        "facts": values,
                    }
                    for category, values in categories.items()
                }
                details = dict(evidence or {})
                details.setdefault("dataset_id", source["dataset_id"])
                reservation = (
                    await connection.execute(
                        _RESERVE,
                        _scope_arguments(
                            location_id, source_id, operation, node_id, scopes, details
                        ),
                    )
                ).one()
        return Observation(
            reservation.observation_sequence,
            reservation.seal,
            location_id,
            source_id,
            operation,
            node_id,
            scopes,
            details,
            actor_id,
            request_id,
        )
    except Exception:
        log.warning("Storage fault observation could not be prepared")
        return None


async def _audit(connection, token, fault_id, category, code, transition):
    detail = {
        **token.evidence,
        "location_id": str(token.location_id),
        "source_id": str(token.source_id),
        "category": category,
        "code": code,
        "operation": token.operation,
        "fault_id": str(fault_id),
        "transition": transition,
    }
    if token.actor_id:
        detail["actor_id"] = token.actor_id
    await append_observation(
        connection,
        action=RECOVERED_ACTION if transition == "recovered" else ACTION,
        detail=detail,
        actor_id=token.actor_id,
        request_id=token.request_id,
    )


async def _record(db, token, fault):
    async with storage_access.engine(db).begin() as connection:
        await _bounds(connection)
        transitions = await connection.execute(
            _RECORD,
            {
                **_scope_arguments(
                    token.location_id,
                    token.source_id,
                    token.operation,
                    token.node_id,
                    token.scopes,
                    token.evidence,
                ),
                "sequence": token.sequence,
                "seal": token.seal,
                "failure_category": fault[0] if fault else None,
            },
        )
        for fault_id, category, transition in transitions:
            await _audit(
                connection,
                token,
                fault_id,
                category,
                fault[1] if fault else "recovered",
                transition,
            )


async def record(db, token, fault=None):
    if token is None or (fault is not None and fault[0] not in token.scopes):
        return False
    try:
        async with asyncio.timeout(2):
            await _record(db, token, fault)
        return True
    except Exception:
        log.warning("Storage fault observation could not be persisted")
        return False


def _scope_status(item):
    facts = item.pop("current_facts")
    saved_common = item.pop("saved_common")
    saved_dependencies = item.pop("saved_dependencies")
    content_status = item.pop("content_status")
    if facts is None:
        return "historical"
    location, source = facts["location"], facts["source"]
    node_id = item["node_id"]
    anchor = _anchor(location, node_id, facts["probe_anchor"])
    if (
        _common_facts(
            location, source, node_id, anchor, facts["maintenance_revision"], facts
        )
        != saved_common
    ):
        return "historical"
    if (
        item["category"] == "preview_failed"
        and preview_facts(location, source, facts) != saved_dependencies
    ):
        return "historical"
    return content_status


async def require_visible_location(db, location_id, actor_id):
    if db.autoflush:
        await db.flush()
    if not await db.scalar(
        _VISIBLE_LOCATION, {"actor_id": actor_id, "location_id": location_id}
    ):
        raise HTTPException(404, "Storage location not found")


async def visible_page(db, location_id, actor, *, state, limit, offset):
    if db.autoflush:
        await db.flush()
    page = (
        await db.execute(
            _PAGE,
            {
                "actor_id": actor.id,
                "location_id": location_id,
                "state": state,
                "limit": limit,
                "offset": offset,
            },
        )
    ).one_or_none()
    if page is None:
        raise HTTPException(404, "Storage location not found")
    counts = {"active": page.active_count, "recovered": page.recovered_count}
    for item in page.items:
        item["scope_status"] = _scope_status(item)
        item["responsible_role"] = (
            "platform_admin"
            if item["category"] in {"root_unavailable", "access_denied"}
            else "dataset_admin"
        )
        item["responsible_user_id"] = None
    return {
        "items": page.items,
        "total": sum(counts.values()) if state == "all" else counts[state],
        "counts": counts,
        "observation_only": True,
    }
