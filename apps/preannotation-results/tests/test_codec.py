from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from uuid import UUID

import pytest

from aap_preannotation_results import contract as c
from aap_preannotation_results.codec import (
    PayloadAssembler,
    decode_part,
    encode_part,
    snapshot_file,
)


JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_DIGEST = "a" * 64


def test_snapshot_is_sorted_compact_utf8_and_streams_bounded_parts():
    source = {
        "z": "mask-雪",
        "id": UUID("22222222-2222-4222-8222-222222222222"),
        "created_at": datetime(2026, 10, 20, 1, 2, 3, tzinfo=timezone.utc),
        "a": [1, True, None],
    }
    expected = (
        '{"a":[1,true,null],"created_at":"2026-10-20T01:02:03.000000+00:00",'
        '"id":"22222222-2222-4222-8222-222222222222","z":"mask-雪"}'
    ).encode("utf-8")
    with snapshot_file(source) as snapshot:
        parts = tuple(snapshot.iter_parts())
        assert b"".join(parts) == expected
        assert snapshot.sha256 == hashlib.sha256(expected).hexdigest()
        assert snapshot.byte_size == len(expected)
        assert snapshot.part_count == 1
        assert all(0 < len(part) <= c.MAX_PART_BYTES for part in parts)
        assert decode_part(encode_part(parts[0])) == expected


def test_snapshot_part_count_and_reads_are_deterministic():
    source = {"body": "x" * (c.MAX_PART_BYTES * 2 + 19)}
    with snapshot_file(source) as snapshot:
        assert snapshot.part_count == 3
        parts = tuple(snapshot.iter_parts())
        assert tuple(map(len, parts)) == (c.MAX_PART_BYTES, c.MAX_PART_BYTES, 30)
        assert snapshot.read_part(1) == parts[1]
        assert sum(map(len, parts)) == snapshot.byte_size
        assert hashlib.sha256(b"".join(parts)).hexdigest() == snapshot.sha256

    with pytest.raises(IndexError):
        snapshot.read_part(3)


def test_part_codec_rejects_noncanonical_or_oversized_content():
    with pytest.raises(ValueError):
        encode_part(b"")
    with pytest.raises(ValueError):
        encode_part(b"x" * (c.MAX_PART_BYTES + 1))
    with pytest.raises(ValueError):
        decode_part("Y Q==")
    with pytest.raises(ValueError):
        decode_part("!!!!")


def test_payload_assembler_checks_order_identity_size_and_digest():
    body = b'{"result":"' + b"x" * (c.MAX_PART_BYTES + 23) + b'"}'
    digest = hashlib.sha256(body).hexdigest()
    scope = c.Scope(
        job_id=JOB_ID,
        actor_id=UUID("33333333-3333-4333-8333-333333333333"),
        project_id=UUID("44444444-4444-4444-8444-444444444444"),
        request_digest=REQUEST_DIGEST,
    )
    request = c.ReadBatchResultRequest(
        **scope.model_dump(), ordinal=1, stage="raw", request_index=0
    )
    result = c.Result(
        result_id=UUID("55555555-5555-4555-8555-555555555555"),
        job_id=JOB_ID,
        ordinal=1,
        task_id=UUID("66666666-6666-4666-8666-666666666666"),
        result_count=1,
        raw_digest="a" * 64,
        prepared_digest=None,
        raw_complete=True,
        prepared_complete=False,
        state="captured",
        context={},
        receipt_count=0,
    )
    payload = c.Payload(
        payload_id=UUID("77777777-7777-4777-8777-777777777777"),
        request_index=0,
        prediction_id=UUID("88888888-8888-4888-8888-888888888888"),
        prediction_created_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
        stage="raw",
        sha256=digest,
        byte_size=len(body),
        part_count=2,
        complete=True,
        tool_unit_id=None,
        receipt=None,
    )
    metadata = c.ReadBatchResultResult(result=result, payload=payload, content=None)
    parts = (body[: c.MAX_PART_BYTES], body[c.MAX_PART_BYTES :])

    with PayloadAssembler(request, metadata) as assembler:
        with pytest.raises(c.BatchResultFailure) as error:
            assembler.add_part(
                request.model_copy(update={"part_index": 1}),
                c.ReadBatchResultResult(
                    result=result, payload=payload, content=encode_part(parts[1])
                ),
            )
        assert error.value.code == "invalid_response"

        for index, part in enumerate(parts):
            part_request = request.model_copy(update={"part_index": index})
            part_response = c.ReadBatchResultResult(
                result=result,
                payload=payload,
                content=encode_part(part),
            )
            assembler.add_part(part_request, part_response)

        assert assembler.complete
        assert assembler.byte_size == len(body)
        assert assembler.sha256 == digest
        assert b"".join(assembler.iter_parts()) == body

    with PayloadAssembler(request, metadata) as assembler:
        assembler.add_part(
            request.model_copy(update={"part_index": 0}),
            c.ReadBatchResultResult(
                result=result,
                payload=payload,
                content=encode_part(parts[0]),
            ),
        )
        corrupted = bytearray(parts[1])
        corrupted[-1] ^= 1
        with pytest.raises(c.BatchResultFailure) as error:
            assembler.add_part(
                request.model_copy(update={"part_index": 1}),
                c.ReadBatchResultResult(
                    result=result,
                    payload=payload,
                    content=encode_part(bytes(corrupted)),
                ),
            )
        assert error.value.code == "invalid_response"


def test_bundle_hash_chain_matches_native_int8_and_uuid_send_encoding():
    scope = c.Scope(
        job_id=JOB_ID,
        actor_id=UUID("33333333-3333-4333-8333-333333333333"),
        project_id=UUID("44444444-4444-4444-8444-444444444444"),
        request_digest=REQUEST_DIGEST,
    )
    digests = ["01" * 32, "02" * 32, "fe" * 32]
    state = hashlib.sha256(
        c.SCHEMA.encode()
        + b"\0raw\0"
        + JOB_ID.bytes
        + (9).to_bytes(8, "big", signed=True)
        + (len(digests)).to_bytes(8, "big", signed=True)
        + bytes.fromhex(REQUEST_DIGEST)
    ).digest()
    for index, digest in enumerate(digests):
        state = hashlib.sha256(
            state + index.to_bytes(8, "big", signed=True) + bytes.fromhex(digest)
        ).digest()
    assert c.bundle_digest(scope, 9, "raw", digests) == state.hex()
    assert c.bundle_digest(scope, 9, "raw", iter(digests)) == state.hex()
    assert c.bundle_digest(scope, 9, "raw", []) != c.bundle_digest(
        scope, 9, "prepared", []
    )
    assert c.bundle_digest(scope, 9, "raw", digests) != c.bundle_digest(
        scope, 9, "raw", list(reversed(digests))
    )


def test_bundle_hash_chain_accepts_aggregate_bodies_over_interactive_limit():
    payload = "x" * (1024 * 1024)
    total = 0
    digests = []
    for _index in range(17):
        with snapshot_file({"predictions": payload}) as snapshot:
            assert snapshot.byte_size < 2 * 1024 * 1024
            parts = tuple(snapshot.iter_parts())
            assert all(len(part) <= c.MAX_PART_BYTES for part in parts)
            assert all(
                len(encode_part(part)) <= c.MAX_PART_BASE64_CHARS for part in parts
            )
            total += snapshot.byte_size
            digests.append(snapshot.sha256)
    assert total > 16 * 1024 * 1024

    scope = c.Scope(
        job_id=JOB_ID,
        actor_id=UUID("33333333-3333-4333-8333-333333333333"),
        project_id=UUID("44444444-4444-4444-8444-444444444444"),
        request_digest=REQUEST_DIGEST,
    )
    assert len(c.bundle_digest(scope, 1, "raw", digests)) == 64


def test_snapshot_rejects_non_json_and_nonfinite_values():
    with pytest.raises(ValueError):
        with snapshot_file({"value": float("nan")}):
            pass
    with pytest.raises(ValueError):
        with snapshot_file({1: "non-string key"}):
            pass
    with pytest.raises(ValueError):
        with snapshot_file({"value": object()}):
            pass
