"""Lost-delivery closure and configured budgets in disposable SQL/Redis."""

from datetime import timedelta
import uuid

import pytest

from app.db.models.storage_maintenance import StorageMaintenanceGate
from app.services import agent_transport as svc
from app.services.content_snapshot import ContentError
from app.services.site_agents import SiteAgentError
from tests.test_agent_transport import (
    completed,
    queued,
    remote as remote,
    send_chunk,
    send_result,
)


@pytest.mark.parametrize("draining", [False, True])
@pytest.mark.parametrize("state", ["leased", "unsettled"])
async def test_expired_lost_delivery_replays_only_to_same_journal_for_closure(
    remote, monkeypatch, draining, state
):
    db, _, agent, _, _, _, _ = remote
    row = await queued(remote, "scan")
    session_id = uuid.uuid4()
    original = await svc.lease(db, agent.id, session_id)
    deadline = row.expires_at
    row.state = state
    agent.draining = draining
    await db.commit()
    monkeypatch.setattr(svc, "now", lambda: deadline + timedelta(seconds=1))
    assert await svc.lease(db, agent.id, uuid.uuid4()) is None
    assert await svc.lease(db, agent.id, session_id) == original
    assert row.expires_at == deadline and row.state == state and row.closed is None
    result = {
        **completed(row),
        "status": "failed",
        "result": {},
        "error_code": "command_expired",
    }
    response = await send_result(remote, row, result)
    assert response.status_code == 200
    assert response.json()["state"] == "rejected"
    assert row.closed is True and row.error_code == "agent_command_expired"
    with pytest.raises(ContentError, match="agent_command_expired"):
        await svc.acknowledge(db, row, accepted=True)


@pytest.mark.parametrize("change", ["location", "root", "maintenance"])
async def test_expired_replay_closes_even_after_source_authority_changes(
    remote, monkeypatch, change
):
    db, _, agent, _, location, _, _ = remote
    row = await queued(remote)
    session_id = uuid.uuid4()
    original = await svc.lease(db, agent.id, session_id)
    if change == "location":
        location.enabled = False
    elif change == "root":
        agent.roots = []
    else:
        gate = await db.get(StorageMaintenanceGate, location.id)
        if gate is None:
            gate = StorageMaintenanceGate(location_id=location.id)
            db.add(gate)
        gate.state, gate.revision = "stopped", 2
    await db.commit()
    assert await svc.lease(db, agent.id, session_id) is None
    deadline = row.expires_at
    monkeypatch.setattr(svc, "now", lambda: deadline + timedelta(seconds=1))
    assert await svc.lease(db, agent.id, session_id) == original


async def test_revoked_identity_cannot_recover_old_delivery_through_poll(remote):
    db, _, agent, _, _, _, _ = remote
    row = await queued(remote)
    session_id = uuid.uuid4()
    await svc.lease(db, agent.id, session_id)
    agent.revoked_at = svc.now()
    await db.commit()
    with pytest.raises(SiteAgentError, match="agent_revoked"):
        await svc.lease(db, agent.id, session_id)
    assert row.closed is None


async def test_drain_cleans_confirmed_relay_without_leasing_queued_work(remote):
    db, _, agent, _, _, _, _ = remote
    row = await queued(remote)
    await svc.lease(db, agent.id, uuid.uuid4())
    assert (await send_chunk(remote, row)).status_code == 200
    assert (await send_result(remote, row, completed(row))).status_code == 200
    await svc.acknowledge(db, row, accepted=True)
    agent.draining = True
    await db.commit()
    pending = await queued(remote, "scan")
    assert await svc.lease(db, agent.id, uuid.uuid4()) is None
    assert pending.state == "queued" and row.relay_released is True
    client = svc.redis_client()
    try:
        assert not await client.exists(svc.relay_key(row.id))
    finally:
        await client.aclose()


async def test_drain_redelivers_existing_grant_without_creating_another(remote):
    db, _, agent, _, _, _, _ = remote
    row = await queued(remote)
    session_id = uuid.uuid4()
    original = await svc.lease(db, agent.id, session_id)
    agent.draining = True
    await db.commit()
    assert await svc.lease(db, agent.id, session_id) == original
    assert row.lease_session_id == session_id


async def test_unconfirmed_physical_close_is_never_reexecuted_or_inferred(remote):
    db, _, agent, _, _, _, _ = remote
    row = await queued(remote)
    session_id = uuid.uuid4()
    await svc.lease(db, agent.id, session_id)
    result = {
        **completed(row),
        "status": "failed",
        "result": {},
        "error_code": "source_close_unconfirmed",
        "closed": False,
    }
    assert (await send_result(remote, row, result)).status_code == 200
    assert await svc.lease(db, agent.id, session_id) is None
    assert row.state == "unsettled" and row.closed is False


@pytest.mark.parametrize("admin,reported", [(2048, 8192), (8192, 2048)])
async def test_command_signs_the_lower_administrator_and_agent_rate(
    remote, admin, reported
):
    _, _, agent, _, _, _, _ = remote
    agent.limits = {**agent.limits, "bandwidth_bytes_per_second": admin}
    agent.reported_limits = {
        **agent.reported_limits,
        "bandwidth_bytes_per_second": reported,
    }
    row = await queued(remote)
    assert row.payload["bandwidth_bytes_per_second"] == 2048
