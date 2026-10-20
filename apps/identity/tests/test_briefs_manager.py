from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest

from aap_identity.briefs_contract import (
    BRIEFS_PROTOCOL,
    NATIVE_BRIEFS_PROTOCOL,
    BriefsFailure,
    decode_briefs_request,
    decode_native_briefs_command,
)
from aap_identity.briefs_manager import BriefsManager
from aap_identity.contract import IdentityUnavailable
from briefs_helpers import (
    briefs_request,
    legacy_fixture,
    native_briefs_success,
)


class Transaction:
    def __init__(self, owner):
        self.owner = owner

    async def execute_briefs_command(self, payload):
        command = decode_native_briefs_command(payload)
        self.owner.commands.append(command)
        if self.owner.result is not None:
            return self.owner.result(command)
        return native_briefs_success(command)


class Data:
    def __init__(self, result=None):
        self.result = result
        self.commands = []
        self.transactions = 0
        self.committed = False
        self.rolled_back = False

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        try:
            yield Transaction(self)
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True


@pytest.mark.asyncio
async def test_global_and_project_reads_match_actual_legacy_response_fixture():
    fixture = legacy_fixture()
    ids = [uuid.UUID(item["id"]) for item in fixture["inputs"]]
    ids.extend((ids[1], uuid.uuid4()))
    data = Data()
    manager = BriefsManager(data)

    global_reply = await manager.execute(briefs_request("get_user_briefs", ids))
    project_reply = await manager.execute(
        briefs_request(
            "get_project_user_briefs",
            ids,
            project_id=fixture["project_id"],
        )
    )

    expected_global = sorted(
        fixture["responses"]["global"].values(), key=lambda user: user["id"]
    )
    expected_project = sorted(
        fixture["responses"]["project"].values(), key=lambda user: user["id"]
    )
    assert [user.model_dump(mode="json") for user in global_reply.data.users] == (
        expected_global
    )
    assert [user.model_dump(mode="json") for user in project_reply.data.users] == (
        expected_project
    )
    assert global_reply.data.project_id is None
    assert str(project_reply.data.project_id) == fixture["project_id"]
    assert data.transactions == 2
    assert data.committed and not data.rolled_back
    assert data.commands[0].operation == "get_user_briefs"
    assert data.commands[1].operation == "get_project_user_briefs"


@pytest.mark.asyncio
async def test_empty_id_list_returns_empty_without_transaction_or_remote_io():
    data = Data()
    manager = BriefsManager(data)

    global_reply = await manager.execute(briefs_request("get_user_briefs", []))
    project_reply = await manager.execute(
        briefs_request("get_project_user_briefs", [], project_id=uuid.uuid4())
    )

    assert global_reply.data.project_id is None and global_reply.data.users == []
    assert project_reply.data.users == []
    assert data.transactions == 0


@pytest.mark.asyncio
async def test_duplicate_ids_and_more_than_five_hundred_ids_are_preserved_as_request():
    fixture = legacy_fixture()
    requested_id = uuid.UUID(fixture["inputs"][1]["id"])
    ids = [uuid.uuid4() for _ in range(1200)] + [requested_id, requested_id]
    data = Data()
    manager = BriefsManager(data)

    reply = await manager.execute(briefs_request("get_user_briefs", ids))

    assert len(data.commands[0].payload.user_ids) == len(ids)
    assert len(reply.data.users) == 1
    assert reply.data.users[0].id == requested_id


@pytest.mark.asyncio
async def test_native_invalid_request_is_owner_unavailability_not_partial_data():
    data = Data(
        lambda command: {
            "schema": NATIVE_BRIEFS_PROTOCOL,
            "ok": False,
            "error": {"code": "invalid_request"},
        }
    )
    manager = BriefsManager(data)

    with pytest.raises(IdentityUnavailable):
        await manager.execute(briefs_request("get_user_briefs", [uuid.uuid4()]))

    assert data.rolled_back and not data.committed


@pytest.mark.asyncio
async def test_wrong_project_extra_id_or_unsorted_native_reply_rolls_back():
    first, second = uuid.UUID(int=1), uuid.UUID(int=2)
    request = briefs_request("get_user_briefs", [first, second])
    good = [
        {
            "id": str(first),
            "name": "First",
            "email": "first@example.test",
            "role": "employee",
            "avatar_ref": None,
        },
        {
            "id": str(second),
            "name": "Second",
            "email": "second@example.test",
            "role": "viewer",
            "avatar_ref": "preset:pixel-1",
        },
    ]
    invalid_results = (
        lambda command: native_briefs_success(
            command, good, project_id=uuid.UUID(int=99)
        ),
        lambda command: native_briefs_success(
            command, [*good, {**good[0], "id": str(uuid.UUID(int=3))}]
        ),
        lambda command: native_briefs_success(command, list(reversed(good))),
    )

    for native_result in invalid_results:
        data = Data(native_result)
        with pytest.raises(IdentityUnavailable):
            await BriefsManager(data).execute(request)
        assert data.rolled_back and not data.committed


def test_request_decoder_rejects_alias_extra_fields_and_null_ids():
    request = briefs_request("get_user_briefs", [uuid.uuid4()]).model_dump(
        mode="json", by_alias=True
    )
    alias = {
        "protocol": request["schema"],
        **{k: v for k, v in request.items() if k != "schema"},
    }
    with pytest.raises(BriefsFailure):
        decode_briefs_request(alias)

    extra = {**request, "unexpected": True}
    with pytest.raises(BriefsFailure):
        decode_briefs_request(extra)

    null_id = {
        "schema": BRIEFS_PROTOCOL,
        "operation": "get_user_briefs",
        "payload": {"user_ids": [None]},
    }
    with pytest.raises(BriefsFailure):
        decode_briefs_request(null_id)


def test_wire_reply_requires_python_compatible_avatar_initial():
    from aap_identity.briefs_contract import decode_briefs_reply

    request = briefs_request("get_user_briefs", [uuid.UUID(int=1)])
    bad = {
        "schema": BRIEFS_PROTOCOL,
        "ok": True,
        "operation": "get_user_briefs",
        "data": {
            "project_id": None,
            "users": [
                {
                    "id": str(uuid.UUID(int=1)),
                    "name": "ßeta",
                    "email": "s@example.test",
                    "role": "reviewer",
                    "avatar_initial": "ß",
                    "avatar_ref": None,
                }
            ],
        },
    }
    with pytest.raises(IdentityUnavailable):
        decode_briefs_reply(request, bad)


def test_avatar_initial_matches_unicode_and_whitespace_legacy_behavior():
    from aap_identity.briefs_contract import avatar_initial

    fixture = legacy_fixture()
    for key, expected in (
        ("empty", "E"),
        ("inactive", "?"),
        ("historical", "SS"),
        ("combining", "\u0301"),
        ("staff", "标"),
    ):
        user = next(item for item in fixture["inputs"] if item["key"] == key)
        assert avatar_initial(user["name"], user["email"]) == expected
