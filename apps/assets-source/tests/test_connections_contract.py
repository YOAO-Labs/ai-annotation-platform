from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_assets_source.connections_contract import (
    CONNECTION_NATIVE_ARGUMENTS,
    CONNECTION_NATIVE_OPERATIONS,
    CONNECTION_NATIVE_SIGNATURES,
    CONNECTION_OPERATIONS,
    CONNECTIONS_SCHEMA,
    ConnectionFailure,
    ConnectionProbeResult,
    ConnectionReceiptLookup,
    ConnectionReceiptRequest,
    ConnectionRecord,
    CreateConnectionRequest,
    EmptyConnectionIntent,
    ListConnectionsRequest,
    NativeConnectionFacts,
    NativeConnectionMutation,
    NativeConnectionsPage,
    ReadConnectionRequest,
    NativeConnectionRead,
    connections_contract_document,
    decode_connection_reply,
    require_connection_scope,
)


def _facts(connection_id=None):
    return NativeConnectionFacts(
        id=connection_id or uuid4(),
        revision=1,
        name="connection",
        kind="s3",
        config={"endpoint": "http://8.8.8.8", "bucket": "data"},
        scope="owner",
        project_id=None,
        secret_set=True,
        created_by=uuid4(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def test_connection_contract_freezes_nine_native_and_six_public_operations():
    assert len(CONNECTION_NATIVE_SIGNATURES) == 9
    assert len(CONNECTION_OPERATIONS) == 6
    for name, signature in CONNECTION_NATIVE_SIGNATURES.items():
        assert len(signature.split(",")) == len(CONNECTION_NATIVE_ARGUMENTS[name])
    expected = connections_contract_document()
    assert expected["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert expected["$id"] == "urn:aap:assets-connections:v1"
    for request, result in (
        *CONNECTION_NATIVE_OPERATIONS.values(),
        *CONNECTION_OPERATIONS.values(),
    ):
        assert request.__name__ in expected["$defs"]
        assert result.__name__ in expected["$defs"]
    package = (
        Path(__file__).resolve().parents[1]
        / "src/aap_assets_source/connections_contract.schema.json"
    )
    assert json.loads(package.read_text()) == expected
    native = (
        Path(__file__).resolve().parents[3]
        / "apps/api/alembic/sql/0371_assets_owner_main_integration.schema.json"
    )
    if native.exists():
        assert json.loads(native.read_text()) == expected


def test_connection_reply_rejects_extra_fields_bad_errors_and_wrong_scope():
    actor_id, connection_id = uuid4(), uuid4()
    request = ReadConnectionRequest(actor_id=actor_id, connection_id=connection_id)
    reply = NativeConnectionRead(
        actor_id=actor_id, connection_id=connection_id, connection=_facts(connection_id)
    )
    payload = {
        "schema": CONNECTIONS_SCHEMA,
        "ok": True,
        "data": reply.model_dump(mode="json"),
    }
    parsed = decode_connection_reply(payload, NativeConnectionRead)
    require_connection_scope(request, parsed)
    with pytest.raises(ValueError):
        decode_connection_reply(
            {**payload, "secret": "never-return"}, NativeConnectionRead
        )
    with pytest.raises(ValueError):
        decode_connection_reply(
            {"schema": CONNECTIONS_SCHEMA, "ok": False, "error": {"code": "unknown"}},
            NativeConnectionRead,
        )
    with pytest.raises(ValueError):
        require_connection_scope(
            request, parsed.model_copy(update={"connection_id": uuid4()})
        )
    with pytest.raises(ConnectionFailure) as failure:
        decode_connection_reply(
            ConnectionFailure("connection_missing").payload(), NativeConnectionRead
        )
    assert failure.value.code == "connection_missing"


def test_connection_receipt_rejects_a_cached_result_from_another_actor():
    operation_id, actor_id, connection_id = uuid4(), uuid4(), uuid4()
    request = ConnectionReceiptRequest(
        operation_id=operation_id,
        actor_id=actor_id,
        connection_id=connection_id,
        action="delete",
        intent=EmptyConnectionIntent(),
    )
    cached = NativeConnectionMutation(
        operation_id=operation_id,
        actor_id=uuid4(),
        connection_id=connection_id,
        action="delete",
        connection=None,
    )
    lookup = ConnectionReceiptLookup(
        operation_id=operation_id,
        actor_id=actor_id,
        connection_id=connection_id,
        action="delete",
        result=cached,
    )
    with pytest.raises(ValueError):
        require_connection_scope(request, lookup)


def test_public_connection_rejects_legacy_embedded_credentials():
    raw = _facts().model_dump()
    raw["config"] = {**raw["config"], "password": "must-not-cross-wire"}
    with pytest.raises(ValidationError):
        ConnectionRecord.model_validate(raw)
    raw["config"] = {"endpoint": "http://user:password@example.test", "bucket": "data"}
    with pytest.raises(ValidationError):
        ConnectionRecord.model_validate(raw)


def test_connection_input_rejects_non_json_values_and_hides_credential_repr():
    values = dict(
        operation_id=uuid4(),
        actor_id=uuid4(),
        name="valid",
        kind="s3",
        config={"endpoint": "http://8.8.8.8", "bucket": "data"},
        secret={"password": "do-not-echo"},
    )
    request = CreateConnectionRequest(**values)
    assert "do-not-echo" not in repr(request)
    with pytest.raises(ValidationError):
        CreateConnectionRequest(**{**values, "secret": {"password": float("nan")}})


def test_native_connection_rejects_unsupported_scope():
    raw = _facts().model_dump()
    with pytest.raises(ValidationError):
        NativeConnectionFacts.model_validate({**raw, "scope": "unsupported"})


def test_connection_pages_require_progress_and_correct_completion():
    actor_id = uuid4()
    connection = _facts()
    request = ListConnectionsRequest(actor_id=actor_id)
    page = NativeConnectionsPage(
        actor_id=actor_id,
        after=None,
        next_cursor=None,
        snapshot_digest="a" * 64,
        total=1,
        complete=True,
        connections=[connection],
    )
    require_connection_scope(request, page)
    with pytest.raises(ValueError):
        require_connection_scope(request, page.model_copy(update={"complete": False}))
    with pytest.raises(ValueError):
        require_connection_scope(
            request, page.model_copy(update={"connections": [connection, connection]})
        )


@pytest.mark.parametrize(
    "message",
    [
        "连接失败: https://secret@example.test",
        "连接失败: password=secret",
        "连接失败: line\nsecret",
    ],
)
def test_probe_failure_never_transports_raw_exception_details(message):
    with pytest.raises(ValidationError):
        ConnectionProbeResult(ok=False, message=message, sample_count=None)
    assert (
        ConnectionProbeResult(
            ok=False, message="连接失败: ClientError", sample_count=None
        ).ok
        is False
    )
