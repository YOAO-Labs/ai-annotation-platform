"""Real disposable PostgreSQL/Redis receipts; no source or development data."""

from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import stat
import uuid

from aap_site_agent_contract import digest
import pytest

from app.db.models.dataset_asset import StorageLocation
from app.db.models.site_agent import AgentCredential, AgentRegistration
from app.schemas.site_agent_transport import AgentPollRequest
from app.services import agent_transport as svc
from app.services.content_snapshot import ContentError


@pytest.fixture
async def remote(db_session, httpx_client, auth_headers):
    response = await httpx_client.post(
        "/api/v1/site-agents/sites",
        headers=auth_headers,
        json={"name": "remote-" + uuid.uuid4().hex},
    )
    assert response.status_code == 201
    site_id = response.json()["id"]
    response = await httpx_client.post(
        f"/api/v1/site-agents/sites/{site_id}/agents",
        headers=auth_headers,
        json={"name": "remote"},
    )
    assert response.status_code == 201
    pairing = response.json()
    response = await httpx_client.post(
        "/api/v1/site-agents/pair",
        json={
            "agent_id": pairing["agent_id"],
            "pair_code": pairing["pair_code"],
            "protocol_version": 1,
        },
    )
    assert response.status_code == 200
    credentials = response.json()
    agent = await db_session.get(AgentRegistration, uuid.UUID(credentials["agent_id"]))
    credential = await db_session.get(
        AgentCredential, (agent.id, credentials["credential_revision"])
    )
    agent.roots = [{"root_handle": "images", "root_revision": 1}]
    agent.reported_limits = {
        "max_concurrency": 1,
        "bandwidth_bytes_per_second": 8 * 1024 * 1024,
        "max_bytes": svc.MAX_BYTES,
    }
    location = StorageLocation(
        id=uuid.uuid4(),
        name="agent-" + uuid.uuid4().hex,
        kind="agent",
        config_revision=1,
        enabled=True,
        config={
            "agent_id": str(agent.id),
            "root_handle": "images",
            "root_revision": 1,
            "relative_path": "",
        },
    )
    db_session.add(location)
    await db_session.flush()
    rows = []
    try:
        yield (
            db_session,
            httpx_client,
            agent,
            credential,
            location,
            {"Authorization": "Bearer " + credentials["token"]},
            rows,
        )
    finally:
        client = svc.redis_client()
        try:
            for command_id in rows:
                await client.delete(svc.relay_key(command_id))
        finally:
            await client.aclose()


async def queued(remote, operation="read"):
    db, _, _, _, location, _, rows = remote
    args = {"path": "image.bin"} if operation in {"read", "scan"} else {}
    if operation == "scan":
        args["path"] = ""
    row = await svc.enqueue(
        db,
        location,
        operation,
        args,
        fence={"maintenance_revision": 1, "purpose": "reference_read"},
    )
    rows.append(row.id)
    await db.commit()
    return row


async def leased(remote, operation="read"):
    db, _, agent, _, _, _, _ = remote
    row = await queued(remote, operation)
    payload = await svc.lease(db, agent.id, uuid.uuid4())
    assert payload["command_id"] == str(row.id)
    await db.commit()
    return row


def completed(row, data=b"image pixels"):
    result = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "observation": {
            "identity": [1, 2],
            "size": len(data),
            "mode": stat.S_IFREG | 0o644,
            "mtime_ns": 10,
            "ctime_ns": 10,
            "source_identity": [1, 3],
        },
    }
    return {
        "protocol_version": 1,
        "command_id": str(row.id),
        "command_digest": row.command_digest,
        "status": "completed",
        "result": result,
        "error_code": None,
        "closed": True,
    }


async def send_chunk(remote, row, data=b"image pixels", *, offset=0):
    db, client, _, _, _, headers, _ = remote
    response = await client.post(
        f"/api/v1/site-agents/commands/{row.id}/chunks",
        headers={
            **headers,
            "Content-Type": "application/octet-stream",
            "X-Command-Digest": row.command_digest,
            "X-Chunk-Offset": str(offset),
            "X-Chunk-SHA256": hashlib.sha256(data).hexdigest(),
        },
        content=data,
    )
    await db.refresh(row)
    return response


async def send_result(remote, row, value):
    db, client, _, _, _, headers, _ = remote
    response = await client.post(
        f"/api/v1/site-agents/commands/{row.id}/result", headers=headers, json=value
    )
    await db.refresh(row)
    return response


async def test_metadata_first_then_exact_chunks_and_business_ack(remote):
    db, _, _, _, _, _, _ = remote
    row = await leased(remote)
    value = completed(row)
    response = await send_result(remote, row, value)
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "agent_content_incomplete"
    )
    assert (await send_chunk(remote, row)).status_code == 200
    assert (await send_chunk(remote, row)).status_code == 200
    assert row.received_bytes == len(b"image pixels")
    response = await send_result(remote, row, value)
    assert response.status_code == 200 and response.json()["state"] == "received"
    stage = await svc.read_stage(row)
    try:
        assert stage.file.read() == b"image pixels"
        assert stage.sha256 == value["result"]["sha256"]
    finally:
        stage.close()
    await svc.acknowledge(db, row, accepted=True)
    await db.commit()
    response = await send_result(remote, row, value)
    assert response.json()["state"] == "applied" and response.json()[
        "result_digest"
    ] == digest(value)
    await svc.cleanup_terminal(db)
    client = svc.redis_client()
    try:
        assert not await client.exists(svc.relay_key(row.id))
    finally:
        await client.aclose()


async def test_replayed_offset_different_bytes_and_sparse_offsets_rejected(remote):
    row = await leased(remote)
    assert (await send_chunk(remote, row, b"first")).status_code == 200
    assert (await send_chunk(remote, row, b"other")).status_code == 409
    assert (await send_chunk(remote, row, b"next", offset=99)).status_code == 409


async def test_wrong_identity_cannot_submit_result(remote):
    _, client, _, _, _, _, _ = remote
    row = await leased(remote)
    value = completed(row)
    response = await client.post(
        f"/api/v1/site-agents/commands/{row.id}/result",
        headers={"Authorization": "Bearer " + "unknown" * 8},
        json=value,
    )
    assert response.status_code == 401


@pytest.mark.parametrize("retired", ["revoked", "expired", "old_revision"])
async def test_retired_identity_cannot_send_bytes_but_can_report_closed_result(
    remote, retired
):
    db, _, agent, credential, _, _, _ = remote
    row = await leased(remote)
    value = completed(row)
    if retired == "revoked":
        agent.revoked_at = svc.now()
    elif retired == "expired":
        credential.expires_at = svc.now() - timedelta(seconds=1)
    else:
        agent.credential_revision += 1
    await db.flush()
    assert (await send_chunk(remote, row)).status_code in {401, 403, 409}
    response = await send_result(remote, row, value)
    assert response.status_code == 200 and response.json()["state"] == "rejected"
    assert row.closed is True and row.received_bytes == 0


async def test_late_result_closes_receipt_but_cannot_revive_expired_grant(remote):
    db, _, _, _, _, _, _ = remote
    row = await leased(remote)
    row.expires_at = svc.now() - timedelta(seconds=1)
    await db.flush()
    response = await send_result(remote, row, completed(row))
    assert response.status_code == 200 and response.json()["state"] == "rejected"
    assert row.error_code == "agent_command_expired"


async def test_unsettled_result_is_retained_even_after_revocation(remote):
    db, _, agent, _, _, _, _ = remote
    row = await leased(remote)
    agent.revoked_at = svc.now()
    await db.flush()
    value = {
        **completed(row),
        "status": "failed",
        "result": {},
        "error_code": "source_close_failed",
        "closed": False,
    }
    response = await send_result(remote, row, value)
    assert response.status_code == 200 and response.json()["state"] == "received"
    assert row.state == "unsettled" and row.result == value
    with pytest.raises(ContentError, match="agent_result_unsettled"):
        await svc.acknowledge(db, row, accepted=True)


async def test_result_digest_conflict_does_not_replace_original_receipt(remote):
    row = await leased(remote)
    assert (await send_chunk(remote, row)).status_code == 200
    value = completed(row)
    assert (await send_result(remote, row, value)).status_code == 200
    changed = deepcopy(value)
    changed["result"]["observation"]["mtime_ns"] += 1
    response = await send_result(remote, row, changed)
    assert response.status_code == 409
    assert row.result_digest == digest(value)


async def test_read_priority_and_concurrency_limit(remote):
    db, _, agent, _, _, _, _ = remote
    scan = await queued(remote, "scan")
    read = await queued(remote, "read")
    assert (await svc.lease(db, agent.id, uuid.uuid4()))["command_id"] == str(read.id)
    assert await svc.lease(db, agent.id, uuid.uuid4()) is None
    assert scan.state == "queued"


async def test_lost_poll_response_redelivers_same_command_without_renewal(remote):
    db, _, agent, _, _, _, _ = remote
    row = await queued(remote)
    session_id = uuid.uuid4()
    first = await svc.lease(db, agent.id, session_id)
    await db.commit()
    assert await svc.lease(db, agent.id, uuid.uuid4()) is None
    second = await svc.lease(db, agent.id, session_id)
    assert first == second == row.payload
    assert row.expires_at.timestamp() >= second["expires_at"]


async def test_business_accept_cannot_revive_rejected_receipt(remote):
    db, _, agent, _, _, _, _ = remote
    row = await leased(remote)
    assert (await send_chunk(remote, row)).status_code == 200
    assert (await send_result(remote, row, completed(row))).status_code == 200
    row.expires_at = svc.now() - timedelta(seconds=1)
    await db.commit()
    await svc.lease(db, agent.id, uuid.uuid4())
    await db.commit()
    with pytest.raises(ContentError, match="agent_command_expired"):
        await svc.acknowledge(db, row, accepted=True)


def scan_result():
    identity = [1, 2]
    entries = [
        {
            "name": "safe.jpg",
            "kind": "file",
            "identity": [1, 3],
            "mode": stat.S_IFREG | 0o644,
            "size": 100,
            "mtime_ns": 10,
            "ctime_ns": 10,
        }
    ]
    checksum = hashlib.sha256(
        json.dumps([identity, entries], sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()
    return {"identity": identity, "entries": entries, "digest": checksum}


@pytest.mark.parametrize(
    "mutation", ["escape", "digest", "kind", "kind_container", "duplicate", "oversized"]
)
async def test_remote_scan_manifest_is_revalidated_before_existing_owner(
    remote, mutation
):
    row = await leased(remote, "scan")
    payload = scan_result()
    if mutation == "escape":
        payload["entries"][0]["name"] = "../outside"
    elif mutation == "digest":
        payload["digest"] = "0" * 64
    elif mutation == "kind":
        payload["entries"][0]["kind"] = "directory"
    elif mutation == "kind_container":
        payload["entries"][0]["kind"] = []
    elif mutation == "duplicate":
        payload["entries"] *= 2
    else:
        payload["entries"] *= 5001
    value = {**completed(row), "result": payload}
    response = await send_result(remote, row, value)
    assert response.status_code in {409, 422}
    assert row.result is None


async def test_valid_remote_scan_manifest_received_once(remote):
    row = await leased(remote, "scan")
    value = {**completed(row), "result": scan_result()}
    first = await send_result(remote, row, value)
    second = await send_result(remote, row, value)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


async def test_agent_location_registry_requires_current_advertised_root(
    remote, auth_headers
):
    _, client, agent, _, _, _, _ = remote
    body = {
        "name": "registered-" + uuid.uuid4().hex,
        "agent_id": str(agent.id),
        "root_handle": "images",
        "root_revision": 1,
        "relative_path": "datasets",
        "scope": "global",
    }
    response = await client.post(
        "/api/v1/storage-locations/agent", headers=auth_headers, json=body
    )
    assert response.status_code == 201, response.text
    value = response.json()
    assert (value["kind"], value["agent_id"], value["site_id"]) == (
        "agent",
        str(agent.id),
        str(agent.site_id),
    )
    assert value["relative_path"] == "datasets"
    assert value["declared_capabilities"]["write"] is False
    assert value["declared_capabilities"]["range"] is True
    body.update(name="stale-" + uuid.uuid4().hex, root_revision=2)
    response = await client.post(
        "/api/v1/storage-locations/agent", headers=auth_headers, json=body
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "agent_root_revision_changed"


async def test_root_revision_drift_rejects_queued_command(remote):
    db, _, agent, _, _, _, _ = remote
    row = await queued(remote)
    agent.roots = [{"root_handle": "images", "root_revision": 2}]
    await db.flush()
    assert await svc.lease(db, agent.id, uuid.uuid4()) is None
    assert row.state == "rejected"


async def test_revoked_agent_exchange_preserves_public_content_error(remote):
    db, _, agent, _, location, _, _ = remote
    agent.revoked_at = svc.now()
    await db.commit()
    with pytest.raises(ContentError, match="agent_revoked") as caught:
        await svc.exchange(
            db,
            location,
            "probe",
            {},
            fence={},
            purpose="probe",
        )
    assert caught.value.status == 403
    assert not getattr(caught.value, "storage_operation_unsettled", False)


async def test_oversized_chunk_is_rejected_before_redis_write(remote):
    row = await leased(remote)
    response = await send_chunk(remote, row, b"x" * (svc.CHUNK_BYTES + 1))
    assert response.status_code == 413 and row.received_bytes == 0


def test_poll_rejects_bool_protocol_version():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AgentPollRequest.model_validate(
            {
                "protocol_version": True,
                "agent_id": str(uuid.uuid4()),
                "credential_revision": 1,
                "session_id": str(uuid.uuid4()),
                "roots": [],
                "limits": {
                    "max_concurrency": 1,
                    "bandwidth_bytes_per_second": 1024,
                    "max_bytes": 1024,
                },
            }
        )
