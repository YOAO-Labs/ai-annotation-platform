from __future__ import annotations

import uuid

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.presence_contract import (
    PRESENCE_PROTOCOL,
    decode_presence_reply,
    decode_presence_request,
)


def test_presence_request_requires_exact_schema_and_timezone_aware_cutoff():
    request = decode_presence_request(
        {"schema": PRESENCE_PROTOCOL, "cutoff": "2026-10-20T01:02:03+08:00"}
    )
    assert request.cutoff.isoformat() == "2026-10-19T17:02:03+00:00"

    with pytest.raises(IdentityUnavailable):
        decode_presence_request({"schema": "wrong", "cutoff": "2026-10-20T01:02:03Z"})
    with pytest.raises(IdentityUnavailable):
        decode_presence_request(
            {"schema": PRESENCE_PROTOCOL, "cutoff": "2026-10-20T01:02:03"}
        )
    with pytest.raises(IdentityUnavailable):
        decode_presence_request(
            {
                "schema": PRESENCE_PROTOCOL,
                "cutoff": "2026-10-20T01:02:03Z",
                "extra": True,
            }
        )


@pytest.mark.parametrize("affected", [True, -1, 2**63, "1"])
def test_presence_reply_requires_strict_nonnegative_int64(affected):
    with pytest.raises(IdentityUnavailable):
        decode_presence_reply({"schema": PRESENCE_PROTOCOL, "affected": affected})


def test_presence_reply_has_only_the_frozen_fields():
    reply = decode_presence_reply({"schema": PRESENCE_PROTOCOL, "affected": 42})
    assert reply.model_dump(mode="json", by_alias=True) == {
        "schema": PRESENCE_PROTOCOL,
        "affected": 42,
    }
    with pytest.raises(IdentityUnavailable):
        decode_presence_reply(
            {
                "schema": PRESENCE_PROTOCOL,
                "affected": 1,
                "request_id": str(uuid.uuid4()),
            }
        )
