"""Behavior of the read-only native GC authority in a disposable database."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.test_mask_gc_native import (
    _candidate,
    _key,
    _native_call,
    _stage_one_old_candidate,
)


def _page(keys, *, complete=True, after_key=None):
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    ordered = sorted(keys)
    return {
        "items": [_candidate(key, old) for key in ordered],
        "after_key": after_key
        if after_key is not None
        else (ordered[-1] if ordered else None),
        "complete": complete,
    }


async def _snapshot(db):
    values = {}
    for table in (
        "mask_gc_roots",
        "mask_gc_key_epochs",
        "mask_gc_intents",
        "mask_gc_sweeps",
        "mask_gc_write_permit_secrets",
        "raster_mask_uploads",
    ):
        values[table] = (
            await db.execute(
                text(
                    f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), '[]'::jsonb) FROM public.{table} AS t"
                )
            )
        ).scalar_one()
    return values


async def test_preview_works_in_a_read_only_transaction_without_changing_gc_state(
    test_engine,
):
    key = _key()
    async with test_engine.connect() as connection:
        async with connection.begin():
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            before = await _snapshot(connection)
            reply = await _native_call(
                connection,
                "inspect_mask_gc_page",
                {
                    "page": _page([key]),
                    "include_reference_count": True,
                },
            )
            assert reply["ok"] is True, reply
            assert reply["data"]["scanned"] == reply["data"]["eligible"] == 1
            assert reply["data"]["retained"] == 0
            assert isinstance(reply["data"]["referenced"], int)
            assert await _snapshot(connection) == before


async def test_preview_retains_producers_active_intents_and_unknown_roots(db_session):
    baseline = await _native_call(
        db_session,
        "inspect_mask_gc_page",
        {
            "page": _page([]),
            "include_reference_count": True,
        },
    )
    assert baseline["ok"] is True, baseline
    orphan, producer, active, unknown = [_key() for _ in range(4)]
    started = await _native_call(
        db_session, "begin_mask_content_write", {"object_keys": [producer]}
    )
    assert started["ok"] is True, started
    await _stage_one_old_candidate(db_session, active)
    for root_id in ("first", "second"):
        await db_session.execute(
            text(
                "INSERT INTO mask_gc_roots(root_kind,root_id,object_key) VALUES ('unknown-future-kind',:root,:key)"
            ),
            {"root": root_id, "key": unknown},
        )
    before = await _snapshot(db_session)
    reply = await _native_call(
        db_session,
        "inspect_mask_gc_page",
        {
            "page": _page([orphan, producer, active, unknown]),
            "include_reference_count": True,
        },
    )
    assert reply["ok"] is True, reply
    assert reply["data"] == {
        "scanned": 4,
        "eligible": 1,
        "retained": 3,
        "referenced": baseline["data"]["referenced"] + 1,
    }
    assert await _snapshot(db_session) == before


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("null_bool", "invalid_request"),
        ("numeric_bool", "invalid_request"),
        ("unknown_field", "invalid_request"),
        ("invalid_late_candidate", "invalid_request"),
        ("nonprogressing_cursor", "cursor_conflict"),
        ("backwards_final_cursor", "cursor_conflict"),
    ],
)
async def test_preview_rejects_invalid_pages_without_partial_effects(
    db_session, mutation, code
):
    keys = sorted([_key(), _key()])
    request = {"page": _page(keys)}
    if mutation == "null_bool":
        request["include_reference_count"] = None
    elif mutation == "numeric_bool":
        request["include_reference_count"] = 0
    elif mutation == "unknown_field":
        request["cutoff"] = "2100-01-01T00:00:00Z"
    elif mutation == "invalid_late_candidate":
        request["page"]["items"][-1]["last_modified"] = "invalid"
    else:
        request["after_key"] = keys[-1]
        request["page"] = _page(
            [],
            complete=mutation == "backwards_final_cursor",
            after_key=keys[0] if mutation == "backwards_final_cursor" else keys[-1],
        )
    original = deepcopy(request)
    before = await _snapshot(db_session)
    reply = await _native_call(db_session, "inspect_mask_gc_page", request)
    assert reply["error"]["code"] == code
    assert request == original
    assert await _snapshot(db_session) == before


async def test_preview_defaults_and_physical_cursors_match_the_byte_contract(
    db_session,
):
    cursor = "raster-masks/sha256/non-candidate/中"
    reply = await _native_call(
        db_session,
        "inspect_mask_gc_page",
        {
            "page": _page([], complete=False, after_key=cursor),
        },
    )
    assert reply["ok"] is True, reply
    assert reply["data"] == {
        "scanned": 0,
        "eligible": 0,
        "retained": 0,
        "referenced": None,
    }
    reply = await _native_call(
        db_session,
        "inspect_mask_gc_page",
        {
            "after_key": cursor,
            "page": _page([], complete=True, after_key=cursor),
        },
    )
    assert reply["ok"] is True, reply


@pytest.mark.parametrize(
    "etag,valid",
    [
        ("!#$~", True),
        ('"quoted"', False),
        ("white space", False),
        ("é", False),
    ],
)
async def test_native_preview_etags_follow_the_byte_wire_contract(
    db_session, etag, valid
):
    page = _page([_key()])
    page["items"][0]["etag"] = etag
    before = await _snapshot(db_session)
    reply = await _native_call(db_session, "inspect_mask_gc_page", {"page": page})
    assert reply["ok"] is valid, reply
    if not valid:
        assert reply["error"]["code"] == "invalid_request"
    assert await _snapshot(db_session) == before
