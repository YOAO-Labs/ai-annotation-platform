"""Adapt an outbound receipt to the existing verified image snapshot owner."""

from contextlib import contextmanager
from dataclasses import dataclass
import uuid

from app.db.models.dataset_asset import StorageLocation
from app.db.models.site_agent import AgentCredential, AgentRegistration
from app.services import agent_transport as transport, content_snapshot as snapshots
from app.services.filesystem_storage import FilesystemStorageError
from app.services.site_agents import SiteAgentError, validate_current
from app.services import agent_availability


@dataclass(frozen=True)
class AgentAccess:
    location_id: uuid.UUID
    anchor: dict | None


async def configuration(db, location):
    try:
        agent = await db.get(
            AgentRegistration,
            uuid.UUID(location.config["agent_id"]),
            populate_existing=True,
        )
        credential = (
            await db.get(
                AgentCredential,
                (agent.id, agent.credential_revision),
                populate_existing=True,
            )
            if agent
            else None
        )
        validate_current(agent, credential)
    except SiteAgentError as exc:
        raise FilesystemStorageError(exc.code) from None
    status = (await agent_availability.for_locations(db, [location.id])).get(
        location.id
    )
    if status is None or status["reason_code"] != "agent_online":
        raise FilesystemStorageError(
            status["reason_code"] if status else "agent_unavailable"
        )
    return AgentAccess(location.id, location.config.get("agent_anchor"))


async def capture(
    db,
    config,
    base,
    path,
    *,
    expected_anchor,
    expected,
    expected_source_identity,
    expected_sha256=None,
    expected_revision=None,
    node_id=None,
    purpose="reference_read",
    fence=None,
    on_acquired=None,
    inspect_quality=False,
):
    location = await db.get(StorageLocation, config.location_id, populate_existing=True)
    # The original caller has already committed its source/authority locks.
    receipt = await transport.exchange(
        db,
        location,
        "read",
        {
            "source_path": base,
            "path": path,
            "expected_anchor": expected_anchor,
            "expected": expected,
            "expected_source_identity": expected_source_identity,
            "expected_sha256": expected_sha256,
            "expected_size": (expected or {}).get("size"),
        },
        fence=fence or {},
        purpose=purpose,
        node_id=node_id,
        expected_revision=expected_revision,
        on_acquired=on_acquired,
    )
    stage = None
    try:
        stage = await transport.read_stage(receipt)

        @contextmanager
        def staged_file():
            stage.file.seek(0)
            yield stage.file, stage.observation

        # Reuse the four-slot image decoder and complete SHA proof. The second
        # bounded copy avoids introducing an unaccounted snapshot lifetime.
        snapshot = await (
            snapshots.capture_for_quality(staged_file, expected_sha256)
            if inspect_quality
            else snapshots.capture(staged_file, expected_sha256)
        )
        snapshot._agent_receipt = receipt
        return snapshot
    except BaseException:
        await db.rollback()
        await transport.acknowledge(
            db, receipt, accepted=False, code="agent_content_not_accepted"
        )
        await db.commit()
        raise
    finally:
        if stage is not None:
            stage.close()


async def accept(db, snapshot):
    receipt = getattr(snapshot, "_agent_receipt", None)
    if receipt is not None:
        await transport.acknowledge(db, receipt, accepted=True)


async def reject(db, snapshot):
    receipt = getattr(snapshot, "_agent_receipt", None)
    if receipt is not None:
        await db.rollback()
        await transport.acknowledge(
            db, receipt, accepted=False, code="agent_authority_changed"
        )
        await db.commit()
