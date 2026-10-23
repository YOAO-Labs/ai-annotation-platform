from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from aap_identity.account_lock_contract import (
    ACCOUNT_LOCK_PROTOCOL,
    AccountLockFacts,
    AccountLockReply,
    AccountLockRequest,
    decode_account_lock_reply,
)
from aap_identity.contract import IdentityUnavailable


def _request(account_ids, *, mode="share", nowait=True):
    return AccountLockRequest(
        schema=ACCOUNT_LOCK_PROTOCOL,
        account_ids=account_ids,
        mode=mode,
        nowait=nowait,
    )


def _account(
    account_id,
    *,
    name="Raw Name",
    email="raw@example.test",
    role="employee",
    active=True,
):
    return AccountLockFacts(
        id=account_id,
        name=name,
        email=email,
        role=role,
        is_active=active,
    )


def _reply(accounts):
    return AccountLockReply(schema=ACCOUNT_LOCK_PROTOCOL, accounts=accounts).model_dump(
        mode="json", by_alias=True
    )


def test_request_allows_duplicate_unordered_ids_and_all_lock_modes():
    account_ids = [uuid.uuid4(), uuid.uuid4()]
    for mode in ("share", "update", "key_share"):
        request = _request([account_ids[1], account_ids[0], account_ids[1]], mode=mode)
        assert request.mode == mode
        assert request.nowait is True
        assert request.account_ids == [account_ids[1], account_ids[0], account_ids[1]]


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "read"},
        {"nowait": 1},
        {"nowait": "true"},
        {"protocol": ACCOUNT_LOCK_PROTOCOL},
        {"extra": "not a SQL parameter"},
    ],
)
def test_request_is_closed_and_mode_boolean_are_strict(overrides):
    values = {
        "schema": ACCOUNT_LOCK_PROTOCOL,
        "account_ids": [str(uuid.uuid4())],
        "mode": "share",
        "nowait": True,
    }
    values.update(overrides)
    with pytest.raises(ValidationError):
        AccountLockRequest.model_validate_json(json.dumps(values))


def test_request_has_no_artificial_account_count_cap():
    account_ids = [uuid.uuid4() for _ in range(1200)]
    request = _request(account_ids, mode="key_share", nowait=False)
    assert len(request.account_ids) == 1200
    assert request.nowait is False


def test_reply_returns_raw_inactive_and_historical_account_facts():
    requested = [uuid.uuid4() for _ in range(4)]
    request = _request([requested[2], requested[0], requested[1], requested[2]])
    facts = [
        _account(requested[0], name="  \u540d\u79f0 \U0001f600 ", email=" e@x "),
        _account(
            requested[2],
            name="\u505c\u7528 \u00df owner",
            role="historical/reviewer",
            active=False,
        ),
    ]
    facts.sort(key=lambda account: str(account.id))

    decoded = decode_account_lock_reply(_reply(facts), request)

    assert decoded == facts
    decoded_by_id = {account.id: account for account in decoded}
    assert decoded_by_id[requested[0]].name == "  \u540d\u79f0 \U0001f600 "
    assert decoded_by_id[requested[0]].email == " e@x "
    assert decoded_by_id[requested[2]].role == "historical/reviewer"
    assert decoded_by_id[requested[2]].is_active is False


def test_reply_requires_sorted_unique_subset_of_requested_ids():
    ids = sorted([uuid.uuid4(), uuid.uuid4()], key=str)
    request = _request(ids)
    for accounts in (
        [_account(ids[1]), _account(ids[0])],
        [_account(ids[0]), _account(ids[0])],
        [_account(uuid.uuid4())],
    ):
        with pytest.raises(IdentityUnavailable):
            decode_account_lock_reply(_reply(accounts), request)


def test_reply_allows_missing_accounts_and_requires_only_the_five_frozen_fields():
    requested = [uuid.uuid4() for _ in range(3)]
    request = _request(requested)
    present = _account(requested[1], role="legacy-role", active=False)

    assert decode_account_lock_reply(_reply([present]), request) == [present]

    malformed = _reply([present])
    malformed["accounts"][0]["avatar_ref"] = None
    with pytest.raises(IdentityUnavailable):
        decode_account_lock_reply(malformed, request)

    malformed = _reply([present])
    malformed["accounts"][0]["is_active"] = 1
    with pytest.raises(IdentityUnavailable):
        decode_account_lock_reply(malformed, request)


@pytest.mark.parametrize(
    "payload",
    [
        {"protocol": ACCOUNT_LOCK_PROTOCOL, "accounts": []},
        {"schema": "aap-identity-other/v1", "accounts": []},
        {"schema": ACCOUNT_LOCK_PROTOCOL, "accounts": [], "ok": True},
        b'{"schema":"aap-identity-account-locks/v1","accounts":[],"accounts":[]}',
        b'{"schema":"aap-identity-account-locks/v1","accounts":[],"extra":NaN}',
    ],
)
def test_reply_rejects_aliases_extra_fields_duplicates_and_nonfinite(payload):
    with pytest.raises(IdentityUnavailable):
        decode_account_lock_reply(payload, _request([uuid.uuid4()]))


def test_reply_rejects_deep_malformed_json_without_leaking_parser_errors():
    request = _request([])
    nested = b"[" * 3000 + b"0" + b"]" * 3000
    payload = (
        b'{"schema":"aap-identity-account-locks/v1","accounts":[],"extra":'
        + nested
        + b"}"
    )
    with pytest.raises(IdentityUnavailable):
        decode_account_lock_reply(payload, request)
