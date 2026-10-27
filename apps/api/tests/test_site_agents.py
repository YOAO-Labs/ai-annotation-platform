"""Independent identities and closure-only retired credentials on disposable DBs."""

from datetime import datetime, timedelta, timezone
import re
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from app.db.models.site_agent import AgentCredential, AgentRegistration
from app.db.models.user import User
from app.schemas.site_agent import AgentCreate, AgentPairRequest, SiteCreate
from app.services import api_key_service
from app.services import site_agents as svc

BASE = "/api/v1/site-agents"


@pytest.mark.parametrize(
    "agent,credential", [(None, None), (None, object()), (object(), None)]
)
def test_missing_current_identity_is_an_authorization_error(agent, credential):
    with pytest.raises(svc.SiteAgentError, match="agent_unauthorized") as caught:
        svc.validate_current(agent, credential)
    assert caught.value.status == 401


def headers(actor):
    return {"Authorization": "Bearer " + actor[1]}


async def create_pending(client, actor, *, site_id=None):
    if site_id is None:
        response = await client.post(
            BASE + "/sites",
            headers=headers(actor),
            json={"name": "site-" + uuid.uuid4().hex, "region": "test"},
        )
        assert response.status_code == 201, response.text
        site_id = response.json()["id"]
    response = await client.post(
        BASE + f"/sites/{site_id}/agents",
        headers=headers(actor),
        json={"name": "agent-" + uuid.uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return site_id, response.json()


async def pair(client, pending, *, version=1):
    return await client.post(
        BASE + "/pair",
        json={
            "agent_id": pending["agent_id"],
            "pair_code": pending["pair_code"],
            "protocol_version": version,
        },
    )


async def provision(client, actor):
    site_id, pending = await create_pending(client, actor)
    response = await pair(client, pending)
    assert response.status_code == 200, response.text
    return site_id, pending, response.json()


async def test_pair_is_one_time_and_never_exposes_plaintext_on_list(
    httpx_client, super_admin, db_session
):
    site_id, pending = await create_pending(httpx_client, super_admin)
    agent = await db_session.get(AgentRegistration, uuid.UUID(pending["agent_id"]))
    assert agent.pair_digest != pending["pair_code"] and len(agent.pair_digest) == 64
    expires = datetime.fromisoformat(pending["expires_at"].replace("Z", "+00:00"))
    assert (
        timedelta(minutes=9)
        < expires - await svc.now(db_session)
        <= timedelta(minutes=10)
    )
    before = await httpx_client.get(
        BASE + "/agents", headers=headers(super_admin), params={"site_id": site_id}
    )
    assert before.status_code == 200 and before.json()["items"][0]["state"] == "pending"
    response = await pair(httpx_client, pending)
    assert response.status_code == 200, response.text
    credentials = response.json()
    assert set(credentials) == {
        "agent_id",
        "credential_revision",
        "token",
        "expires_at",
    }
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,256}", credentials["token"])
    assert type(credentials["expires_at"]) is int
    assert (
        timedelta(days=29)
        < datetime.fromtimestamp(credentials["expires_at"], timezone.utc)
        - await svc.now(db_session)
        <= timedelta(days=30)
    )
    assert (await pair(httpx_client, pending)).status_code == 401
    authenticated, credential = await svc.authenticate(db_session, credentials["token"])
    assert authenticated.id == agent.id and credential.revision == 1
    assert credential.token_digest != credentials["token"]
    await db_session.refresh(agent)
    assert agent.pair_digest is None and agent.pair_expires_at is None
    listed = await httpx_client.get(
        BASE + "/agents", headers=headers(super_admin), params={"site_id": site_id}
    )
    assert listed.status_code == 200
    body = listed.json()
    assert body["items"][0]["state"] == "offline"
    for secret in [
        credentials["token"],
        credential.token_digest,
        pending["pair_code"],
        "pair_digest",
        "token_digest",
    ]:
        assert secret not in listed.text


async def test_pair_wrong_code_protocol_and_expiry_do_not_consume_code(
    httpx_client, super_admin, db_session
):
    _, pending = await create_pending(httpx_client, super_admin)
    wrong = dict(pending, pair_code="x" * 43)
    assert (await pair(httpx_client, wrong)).status_code == 401
    assert (await pair(httpx_client, pending, version=2)).status_code == 426
    assert (await pair(httpx_client, pending, version=True)).status_code == 422
    agent = await db_session.get(AgentRegistration, uuid.UUID(pending["agent_id"]))
    original = agent.pair_digest
    agent.pair_expires_at = await svc.now(db_session) - timedelta(seconds=1)
    await db_session.flush()
    assert (await pair(httpx_client, pending)).status_code == 401
    await db_session.refresh(agent)
    assert agent.pair_digest == original
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AgentCredential)
            .where(AgentCredential.agent_id == agent.id)
        )
        == 0
    )


async def test_rotation_retires_old_token_and_repair_does_not_reuse_identity(
    httpx_client, super_admin, db_session
):
    _, pending, first = await provision(httpx_client, super_admin)
    agent_id = first["agent_id"]
    url = BASE + f"/agents/{agent_id}/rotate"
    assert (
        await httpx_client.post(
            url, headers=headers(super_admin), json={"expected_revision": 2}
        )
    ).status_code == 409
    not_drained = await httpx_client.post(
        url, headers=headers(super_admin), json={"expected_revision": 1}
    )
    assert (
        not_drained.status_code == 409
        and not_drained.json()["detail"]["code"] == "agent_tasks_not_drained"
    )
    await svc.set_draining(
        db_session, uuid.UUID(agent_id), expected_revision=1, draining=True
    )
    response = await httpx_client.post(
        url, headers=headers(super_admin), json={"expected_revision": 1}
    )
    assert response.status_code == 200, response.text
    fresh = response.json()
    assert fresh["agent_id"] == agent_id and fresh["pair_code"] != pending["pair_code"]
    with pytest.raises(svc.SiteAgentError):
        await svc.authenticate(db_session, first["token"])
    agent, retired = await svc.authenticate(
        db_session, first["token"], allow_retired_receipt=True
    )
    assert agent.credential_revision == 2 and retired.revoked_at is not None
    assert (await pair(httpx_client, pending)).status_code == 401
    response = await pair(httpx_client, fresh)
    assert response.status_code == 200, response.text
    second = response.json()
    assert second["credential_revision"] == 2 and second["token"] != first["token"]
    agent, current = await svc.authenticate(db_session, second["token"])
    assert current.revision == agent.credential_revision == 2
    history = list(
        await db_session.scalars(
            select(AgentCredential)
            .where(AgentCredential.agent_id == agent.id)
            .order_by(AgentCredential.revision)
        )
    )
    assert [row.revision for row in history] == [1, 2]
    assert history[0].revoked_at is not None and history[1].revoked_at is None


async def test_revoke_is_terminal_but_retains_closure_authentication(
    httpx_client, super_admin, db_session
):
    _, pending, credentials = await provision(httpx_client, super_admin)
    url = BASE + f"/agents/{credentials['agent_id']}"
    response = await httpx_client.post(url + "/revoke", headers=headers(super_admin))
    assert response.status_code == 200 and response.json()["state"] == "revoked"
    with pytest.raises(svc.SiteAgentError, match="agent_revoked"):
        await svc.authenticate(db_session, credentials["token"])
    agent, credential = await svc.authenticate(
        db_session, credentials["token"], allow_retired_receipt=True
    )
    assert agent.revoked_at and credential.revoked_at
    assert (await pair(httpx_client, pending)).status_code == 403
    assert (
        await httpx_client.post(url + "/rotate", headers=headers(super_admin))
    ).status_code == 403
    assert (
        await httpx_client.post(
            url + "/drain",
            headers=headers(super_admin),
            json={"expected_revision": 1, "draining": False},
        )
    ).status_code == 403
    assert (
        await httpx_client.post(url + "/revoke", headers=headers(super_admin))
    ).status_code == 200


async def test_pending_identity_revoke_invalidates_pairing(httpx_client, super_admin):
    _, pending = await create_pending(httpx_client, super_admin)
    response = await httpx_client.post(
        BASE + f"/agents/{pending['agent_id']}/revoke", headers=headers(super_admin)
    )
    assert response.status_code == 200
    assert (await pair(httpx_client, pending)).status_code == 403


async def test_expired_credentials_only_authenticate_explicit_closure_path(
    httpx_client, super_admin, db_session
):
    _, _, credentials = await provision(httpx_client, super_admin)
    credential = await db_session.get(
        AgentCredential, (uuid.UUID(credentials["agent_id"]), 1)
    )
    credential.expires_at = await svc.now(db_session) - timedelta(seconds=1)
    await db_session.flush()
    with pytest.raises(svc.SiteAgentError, match="credential_expired") as caught:
        await svc.authenticate(db_session, credentials["token"])
    assert caught.value.status == 401
    agent, known = await svc.authenticate(
        db_session, credentials["token"], allow_retired_receipt=True
    )
    assert known.agent_id == agent.id
    with pytest.raises(svc.SiteAgentError, match="agent_protocol_incompatible"):
        await svc.authenticate(
            db_session,
            credentials["token"],
            protocol_version=2,
            allow_retired_receipt=True,
        )


@pytest.mark.parametrize("token", [None, "", "unpaired", "x" * 43, "x" * 257, True])
async def test_unknown_tokens_never_authenticate_even_for_receipts(db_session, token):
    with pytest.raises(svc.SiteAgentError, match="agent_unauthorized"):
        await svc.authenticate(db_session, token, allow_retired_receipt=True)


async def test_protocol_revision_and_pairing_guards_are_explicit(
    httpx_client, super_admin, db_session
):
    _, _, credentials = await provision(httpx_client, super_admin)
    agent, credential = await svc.authenticate(db_session, credentials["token"])
    agent.credential_revision += 1
    with pytest.raises(svc.SiteAgentError, match="credential_revision_changed"):
        svc.validate_current(agent, credential)
    agent.credential_revision -= 1
    agent.pair_digest = "f" * 64
    with pytest.raises(svc.SiteAgentError, match="agent_unpaired"):
        svc.validate_current(agent, credential)
    agent.pair_digest = None
    for version in [True, "1", 0, 2]:
        with pytest.raises(svc.SiteAgentError, match="agent_protocol_incompatible"):
            await svc.authenticate(
                db_session, credentials["token"], protocol_version=version
            )


async def test_drain_expected_revision_and_heartbeat_state_projection(
    httpx_client, super_admin, db_session
):
    site_id, _, credentials = await provision(httpx_client, super_admin)
    agent, _ = await svc.authenticate(db_session, credentials["token"])
    stamp = await svc.now(db_session)
    agent.last_seen = stamp
    agent.session_id = uuid.uuid4()
    agent.roots = [
        {"root_handle": "photos", "root_revision": 1, "absolute_path": "/not-public"}
    ]
    agent.reported_limits = dict(agent.limits, private_path="/not-public")
    await db_session.flush()
    assert svc.state(agent, stamp) == "online"
    assert svc.state(agent, stamp + timedelta(seconds=60)) == "offline"
    listed = await httpx_client.get(
        BASE + "/agents", headers=headers(super_admin), params={"site_id": site_id}
    )
    assert listed.status_code == 200 and listed.json()["items"][0]["state"] == "online"
    assert "/not-public" not in listed.text and "absolute_path" not in listed.text
    url = BASE + f"/agents/{agent.id}/drain"
    assert (
        await httpx_client.post(
            url,
            headers=headers(super_admin),
            json={"expected_revision": 2, "draining": True},
        )
    ).status_code == 409
    assert (
        await httpx_client.post(
            url, headers=headers(super_admin), json={"draining": True}
        )
    ).status_code == 422
    response = await httpx_client.post(
        url,
        headers=headers(super_admin),
        json={"expected_revision": 1, "draining": True},
    )
    assert response.status_code == 200 and response.json()["state"] == "degraded"
    assert (
        await httpx_client.post(
            url,
            headers=headers(super_admin),
            json={"expected_revision": 1, "draining": False},
        )
    ).json()["state"] == "online"


async def test_admin_only_routes_and_read_write_key_scopes(
    httpx_client, super_admin, project_admin, annotator, db_session
):
    for actor in [project_admin, annotator]:
        assert (
            await httpx_client.get(BASE + "/sites", headers=headers(actor))
        ).status_code == 403
        assert (
            await httpx_client.post(
                BASE + "/sites", headers=headers(actor), json={"name": "forbidden"}
            )
        ).status_code == 403
    _, read_token = await api_key_service.create_key(
        db_session, super_admin[0], "agent-read", ["datasets:read"]
    )
    read_headers = {"Authorization": "Bearer " + read_token}
    assert (
        await httpx_client.get(BASE + "/sites", headers=read_headers)
    ).status_code == 200
    assert (
        await httpx_client.post(
            BASE + "/sites", headers=read_headers, json={"name": "forbidden"}
        )
    ).status_code == 403
    _, write_token = await api_key_service.create_key(
        db_session, super_admin[0], "agent-write", ["datasets:write"]
    )
    write_headers = {"Authorization": "Bearer " + write_token}
    assert (
        await httpx_client.get(BASE + "/agents", headers=write_headers)
    ).status_code == 403
    assert (
        await httpx_client.post(
            BASE + "/sites",
            headers=write_headers,
            json={"name": "write-" + uuid.uuid4().hex},
        )
    ).status_code == 201


async def test_admin_mutation_rechecks_actor_and_api_key_under_locks(
    db_session, super_admin
):
    actor = super_admin[0]
    key, _ = await api_key_service.create_key(
        db_session, actor, "agent-write", ["datasets:write"]
    )
    key.revoked_at = await svc.now(db_session)
    await db_session.flush()
    with pytest.raises(HTTPException) as caught:
        await svc.lock_current_admin(db_session, actor, key.id)
    assert caught.value.status_code == 403
    await db_session.execute(
        update(User).where(User.id == actor.id).values(role="employee")
    )
    with pytest.raises(svc.SiteAgentError, match="site_agent_admin_required"):
        await svc.lock_current_admin(db_session, actor)


async def test_site_names_pagination_filtering_and_input_limits(
    httpx_client, super_admin
):
    name = "unique-" + uuid.uuid4().hex
    response = await httpx_client.post(
        BASE + "/sites", headers=headers(super_admin), json={"name": name}
    )
    assert response.status_code == 201
    site_id = response.json()["id"]
    assert (
        await httpx_client.post(
            BASE + "/sites", headers=headers(super_admin), json={"name": name}
        )
    ).status_code == 409
    assert (
        await httpx_client.get(
            BASE + "/sites", headers=headers(super_admin), params={"limit": 1}
        )
    ).status_code == 200
    assert (
        await httpx_client.get(
            BASE + "/agents",
            headers=headers(super_admin),
            params={"site_id": str(uuid.uuid4())},
        )
    ).json() == {"items": [], "total": 0}
    for limits in [
        {"max_concurrency": True},
        {"max_bytes": 0},
        {"bandwidth_bytes_per_second": 1023},
        {"root_path": "/private"},
    ]:
        response = await httpx_client.post(
            BASE + f"/sites/{site_id}/agents",
            headers=headers(super_admin),
            json={"name": "bad", "limits": limits},
        )
        assert response.status_code == 422


async def test_rotation_invalidates_an_unconsumed_pair_code(db_session, super_admin):
    site = await svc.create_site(
        db_session, SiteCreate(name="pending-" + uuid.uuid4().hex), super_admin[0]
    )
    old = await svc.create_agent(
        db_session, site.id, AgentCreate(name="pending"), super_admin[0]
    )
    await svc.set_draining(
        db_session, old["agent_id"], expected_revision=1, draining=True
    )
    fresh = await svc.rotate(db_session, old["agent_id"])
    with pytest.raises(svc.SiteAgentError, match="pair_code_invalid"):
        await svc.pair(
            db_session,
            AgentPairRequest(
                agent_id=old["agent_id"], pair_code=old["pair_code"], protocol_version=1
            ),
        )
    credentials = await svc.pair(
        db_session,
        AgentPairRequest(
            agent_id=fresh["agent_id"], pair_code=fresh["pair_code"], protocol_version=1
        ),
    )
    assert credentials["credential_revision"] == 2


async def test_authentication_query_does_not_acquire_registration_lock(db_session):
    statements = []
    original = db_session.execute

    async def observe(statement, *args, **kwargs):
        statements.append(str(statement.compile()))
        return await original(statement, *args, **kwargs)

    from unittest.mock import patch

    with patch.object(db_session, "execute", observe):
        with pytest.raises(svc.SiteAgentError, match="agent_unauthorized"):
            await svc.authenticate(db_session, "x" * 43)
    assert statements and all("FOR UPDATE" not in sql.upper() for sql in statements)


@pytest.mark.parametrize(
    "command_state",
    ["leased", "received", "unsettled", "queued", "applied", "rejected"],
)
async def test_rotation_waits_for_remote_responsibility_but_revoke_is_unconditional(
    httpx_client, super_admin, db_session, command_state
):
    # The transport model/migration is integrated by the parent worktree.
    from app.db.models.dataset_asset import StorageLocation
    from app.db.models.site_agent_command import SiteAgentCommand

    _, _, credentials = await provision(httpx_client, super_admin)
    agent_id = uuid.UUID(credentials["agent_id"])
    location = StorageLocation(
        id=uuid.uuid4(),
        name="agent-test-" + uuid.uuid4().hex,
        kind="agent",
        config={},
        config_revision=1,
        enabled=True,
    )
    db_session.add(location)
    await db_session.flush()
    command = SiteAgentCommand(
        id=uuid.uuid4(),
        agent_id=agent_id,
        credential_revision=1,
        location_id=location.id,
        payload={},
        command_digest="a" * 64,
        state=command_state,
        priority=0,
        expires_at=await svc.now(db_session) + timedelta(seconds=60),
    )
    db_session.add(command)
    await db_session.flush()
    await svc.set_draining(db_session, agent_id, expected_revision=1, draining=True)
    if command_state in {"leased", "received", "unsettled"}:
        with pytest.raises(svc.SiteAgentError, match="agent_tasks_not_drained"):
            await svc.rotate(db_session, agent_id, expected_revision=1)
        revoked = await svc.revoke(db_session, agent_id)
        assert revoked.revoked_at is not None
    else:
        fresh = await svc.rotate(db_session, agent_id, expected_revision=1)
        assert fresh["agent_id"] == agent_id
    retained = await db_session.get(SiteAgentCommand, command.id)
    assert retained is not None and retained.state == command_state
