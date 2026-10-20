from __future__ import annotations

import uuid

import pytest

from aap_identity.actor_contract import (
    ACTOR_PROTOCOL,
    ActorContext,
    ActorContextRequest,
    actor_failure_reply,
    actor_reply,
    decode_actor_request,
)
from aap_identity.actor_reader import ActorContextReader
from aap_identity.contract import IdentityFailure, IdentityUnavailable


class Data:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    async def read_actor_context(self, actor_id, admitted_role):
        self.calls.append((actor_id, admitted_role))
        if self.error is not None:
            raise self.error
        return self.reply


def _request(actor_id: uuid.UUID, role: str = "employee") -> ActorContextRequest:
    return decode_actor_request(
        {"schema": ACTOR_PROTOCOL, "actor_id": str(actor_id), "admitted_role": role}
    )


@pytest.mark.asyncio
async def test_reader_passes_only_admitted_id_and_role_and_returns_current_context():
    actor_id = uuid.uuid4()
    request = _request(actor_id)
    actor = ActorContext(
        id=actor_id,
        name="Fresh name",
        email="fresh@example.test",
        role="employee",
        is_active=True,
        weekly_target_default=200,
    )
    data = Data(actor_reply(actor))

    assert await ActorContextReader(data).read(request) == actor
    assert data.calls == [(actor_id, "employee")]


@pytest.mark.asyncio
async def test_reader_propagates_only_typed_owner_failures():
    request = _request(uuid.uuid4())
    data = Data(actor_failure_reply("credential_invalid"))
    with pytest.raises(IdentityFailure) as error:
        await ActorContextReader(data).read(request)
    assert error.value.code == "credential_invalid"

    unavailable = Data(error=IdentityUnavailable())
    with pytest.raises(IdentityUnavailable):
        await ActorContextReader(unavailable).read(request)


@pytest.mark.asyncio
async def test_reader_rejects_wrong_actor_binding_as_unavailable():
    request = _request(uuid.uuid4())
    actor = ActorContext(
        id=uuid.uuid4(),
        name="Wrong account",
        email="wrong@example.test",
        role="employee",
        is_active=True,
        weekly_target_default=None,
    )
    with pytest.raises(IdentityUnavailable):
        await ActorContextReader(Data(actor_reply(actor))).read(request)


@pytest.mark.asyncio
async def test_reader_rejects_untyped_requests_without_calling_owner():
    data = Data(
        actor_reply(
            ActorContext(
                id=uuid.uuid4(),
                name="No call",
                email="no-call@example.test",
                role="employee",
                is_active=True,
                weekly_target_default=None,
            )
        )
    )
    with pytest.raises(IdentityUnavailable):
        await ActorContextReader(data).read({})
    assert data.calls == []
