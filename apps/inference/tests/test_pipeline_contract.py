from __future__ import annotations

from datetime import UTC, datetime
import json
import uuid

import pytest
from aap_workspace_contract.project_query_contract import ProjectQueryUnavailable
from aap_workspace_contract.project_query_values import decode_postgres_timestamp

from aap_inference.pipeline_contract import (
    PIPELINE_ACTIONS,
    PIPELINE_INSERT_COLUMNS,
    PIPELINE_POLICY_PROTOCOL,
    PIPELINE_PROTOCOL,
    PIPELINE_ROW_COLUMNS,
    PIPELINE_STEP_PROTOCOL,
    PIPELINE_UPDATE_COLUMNS,
    PipelineInvalidRequest,
    PipelineRow,
    PipelineUnavailable,
    decode_pipeline_command_request,
    decode_pipeline_policy,
    decode_pipeline_query_reply,
    decode_pipeline_reply,
    decode_pipeline_status_reply,
    decode_pipeline_step_reply,
    pipeline_command_request,
    pipeline_policy,
    pipeline_query_request,
    pipeline_schema,
    pipeline_status_request,
)
from aap_inference.pipeline_values import (
    decode_pipeline_row,
    decode_pipeline_rows,
    dirty_pipeline_payload,
    encode_pipeline_columns,
)


def _command(operation="update_pipeline", *, actor_id=None, target_id=None, payload=None):
    actor_id = actor_id or uuid.uuid4()
    if operation != "create_pipeline" and target_id is None:
        target_id = uuid.uuid4()
    return pipeline_command_request(
        operation,
        actor_id=actor_id,
        actor_role="employee",
        target_id=target_id,
        payload={} if payload is None else payload,
        operation_id=uuid.uuid4(),
    )


def _row_payload(*, row_id=None, created_at="2026-01-02T03:04:05+00:00"):
    return {
        "id": str(row_id or uuid.uuid4()),
        "scope": "historical-raw-scope",
        "project_id": str(uuid.uuid4()),
        "organization_id": None,
        "name": "pipe-雪",
        "stages": None,
        "is_default": False,
        "created_by": str(uuid.uuid4()),
        "usage_count": 3,
        "created_at": created_at,
        "updated_at": None,
    }


def _query_reply(request, row):
    return {
        "schema": PIPELINE_PROTOCOL,
        "ok": True,
        "request_id": str(request.request_id),
        "operation": request.operation,
        "actor_id": str(request.admitted_actor.id),
        "target_id": str(request.target_id),
        "rows": [row],
    }


def test_command_wire_preserves_omission_explicit_null_and_saved_payload_values():
    actor_id = uuid.uuid4()
    target_id = uuid.uuid4()
    omitted = _command(actor_id=actor_id, target_id=target_id, payload={})
    explicit_null = _command(
        actor_id=actor_id, target_id=target_id, payload={"name": None}
    )
    assert omitted.payload_ascii == "{}"
    assert explicit_null.payload_ascii == '{"name":null}'

    # Public schema/stage rules have already run at the route boundary; this
    # opaque wire codec does not silently apply a second, different policy.
    saved = pipeline_command_request(
        "create_pipeline",
        actor_id=actor_id,
        actor_role="super_admin",
        payload={
            "scope": "historical-value",
            "project_id": None,
            "organization_id": None,
            "stages": {"legacy_scalar": True},
        },
    )
    assert json.loads(saved.payload_ascii)["stages"] == {"legacy_scalar": True}
    assert decode_pipeline_command_request(
        explicit_null.model_dump(mode="json", by_alias=True)
    ) == explicit_null


def test_command_operation_target_and_apply_payload_are_closed():
    with pytest.raises(PipelineInvalidRequest):
        pipeline_command_request(
            "create_pipeline",
            actor_id=uuid.uuid4(),
            actor_role="employee",
            target_id=uuid.uuid4(),
            payload={},
        )
    with pytest.raises(PipelineInvalidRequest):
        pipeline_command_request(
            "delete_pipeline",
            actor_id=uuid.uuid4(),
            actor_role="employee",
            target_id=uuid.uuid4(),
            payload={"extra": True},
        )
    with pytest.raises(PipelineInvalidRequest):
        pipeline_command_request(
            "apply_pipeline",
            actor_id=uuid.uuid4(),
            actor_role="project_admin",
            target_id=uuid.uuid4(),
            payload={"pipeline_id": "bad-uuid", "set_default": True},
        )


def test_actor_role_is_an_opaque_admitted_snapshot_string():
    actor_id = uuid.uuid4()
    for role in ("viewer", "reviewer", "legacy_platform_role"):
        request = pipeline_command_request(
            "create_pipeline",
            actor_id=actor_id,
            actor_role=role,
            payload={},
        )
        assert request.admitted_actor.role == role


def test_projection_request_binds_full_command_actor_and_resource():
    actor_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    evidence = _command(
        actor_id=actor_id, target_id=pipeline_id, payload={"name": "renamed"}
    )
    request = pipeline_query_request(
        "get_pipeline_projection",
        actor_id=actor_id,
        actor_role="employee",
        target_id=pipeline_id,
        evidence=evidence,
    )
    assert request.evidence == evidence

    with pytest.raises(PipelineInvalidRequest):
        pipeline_query_request(
            "get_pipeline_projection",
            actor_id=actor_id,
            actor_role="project_admin",
            target_id=pipeline_id,
            evidence=evidence,
        )
    with pytest.raises(PipelineInvalidRequest):
        pipeline_query_request(
            "get_pipeline_projection",
            actor_id=uuid.uuid4(),
            actor_role="employee",
            target_id=pipeline_id,
            evidence=evidence,
        )
    with pytest.raises(PipelineInvalidRequest):
        pipeline_query_request(
            "get_pipeline_projection",
            actor_id=actor_id,
            actor_role="employee",
            target_id=pipeline_id,
            evidence=_command(
                "delete_pipeline", actor_id=actor_id, target_id=pipeline_id, payload={}
            ),
        )


def test_list_query_requires_all_nullable_filter_keys():
    request = pipeline_query_request(
        "list_pipelines",
        actor_id=uuid.uuid4(),
        actor_role="viewer",
        filters={"scope": None, "project_id": None, "organization_id": None},
    )
    assert request.filters is not None
    assert request.filters.scope is None
    with pytest.raises(PipelineInvalidRequest):
        pipeline_query_request(
            "list_pipelines",
            actor_id=uuid.uuid4(),
            actor_role="viewer",
            filters={"scope": None, "project_id": None},
        )


def test_command_reply_binds_actor_target_and_known_result_id():
    command = _command()
    target_id = command.target_id
    reply = {
        "schema": PIPELINE_PROTOCOL,
        "ok": True,
        "operation_id": str(command.operation_id),
        "operation": command.operation,
        "actor_id": str(command.admitted_actor.id),
        "target_id": str(target_id),
        "result": {"resource_id": str(target_id)},
        "commit_state": "committed",
    }
    decoded = decode_pipeline_reply(command, reply, require_committed=True)
    assert decoded.result.resource_id == target_id

    wrong_result = dict(reply)
    wrong_result["result"] = {"resource_id": str(uuid.uuid4())}
    with pytest.raises(PipelineUnavailable):
        decode_pipeline_reply(command, wrong_result)

    with pytest.raises(PipelineUnavailable):
        decode_pipeline_reply(command, dict(reply, ok=1))

    pending = dict(reply, commit_state="caller_pending")
    with pytest.raises(PipelineUnavailable):
        decode_pipeline_reply(command, pending, require_committed=True)

    ambiguous_failure = {
        "schema": PIPELINE_PROTOCOL,
        "ok": False,
        "operation_id": str(command.operation_id),
        "operation": command.operation,
        "actor_id": str(command.admitted_actor.id),
        "target_id": str(target_id),
        "error": {
            "status_code": 503,
            "detail": "owner unavailable",
            "headers": None,
            "rollback": False,
        },
        "commit_state": "unknown",
    }
    assert (
        decode_pipeline_reply(
            command, ambiguous_failure, require_committed=True
        ).commit_state
        == "unknown"
    )
    pending_failure = dict(ambiguous_failure, commit_state="caller_pending")
    with pytest.raises(PipelineUnavailable):
        decode_pipeline_reply(command, pending_failure, require_committed=True)


def test_status_reply_binds_nested_original_command_and_never_invents_resource():
    command = _command("create_pipeline", payload={"name": "p"})
    status = pipeline_status_request(command)
    reply = {
        "schema": PIPELINE_PROTOCOL,
        "ok": True,
        "request_id": str(status.request_id),
        "operation": "get_operation_status",
        "operation_id": str(command.operation_id),
        "command_operation": "create_pipeline",
        "actor_id": str(command.admitted_actor.id),
        "target_id": None,
        "state": "unknown",
        "resource_id": None,
    }
    assert decode_pipeline_status_reply(status, reply).state == "unknown"

    mismatched = dict(reply, target_id=str(uuid.uuid4()))
    with pytest.raises(PipelineUnavailable):
        decode_pipeline_status_reply(status, mismatched)


def test_query_reply_decodes_raw_rows_and_binds_single_resource():
    pipeline_id = uuid.uuid4()
    request = pipeline_query_request(
        "get_pipeline",
        actor_id=uuid.uuid4(),
        actor_role="super_admin",
        target_id=pipeline_id,
    )
    row = _row_payload(row_id=pipeline_id)
    raw_reply = _query_reply(request, row)
    reply = decode_pipeline_query_reply(request, raw_reply)
    assert reply.rows[0].id == pipeline_id
    assert reply.rows[0].scope == "historical-raw-scope"
    assert reply.rows[0].stages is None
    assert reply.rows[0].created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert raw_reply["request_id"] == str(request.request_id)
    assert raw_reply["actor_id"] == str(request.admitted_actor.id)
    assert raw_reply["target_id"] == str(pipeline_id)
    assert raw_reply["rows"][0]["id"] == str(pipeline_id)

    wrong_row = _row_payload(row_id=uuid.uuid4())
    with pytest.raises(PipelineUnavailable):
        decode_pipeline_query_reply(request, _query_reply(request, wrong_row))


def test_step_reply_binds_request_actor_target_action_and_known_result_id():
    command = _command("update_pipeline")
    reply = {
        "schema": PIPELINE_STEP_PROTOCOL,
        "ok": True,
        "request_id": str(command.operation_id),
        "operation": command.operation,
        "action": "pipeline_update",
        "actor_id": str(command.admitted_actor.id),
        "target_id": str(command.target_id),
        "facts": {"resource_id": str(command.target_id)},
    }
    decoded = decode_pipeline_step_reply(command, "pipeline_update", reply)
    assert decoded.facts.resource_id == command.target_id

    with pytest.raises(PipelineUnavailable):
        decode_pipeline_step_reply(command, "pipeline_delete", reply)
    wrong_result = dict(reply, facts={"resource_id": str(uuid.uuid4())})
    with pytest.raises(PipelineUnavailable):
        decode_pipeline_step_reply(command, "pipeline_update", wrong_result)


def test_policy_exports_the_fixed_native_contract_and_rejects_drift():
    policy = pipeline_policy()
    assert policy.protocol == PIPELINE_POLICY_PROTOCOL
    assert policy.request_schema == PIPELINE_PROTOCOL
    assert policy.step_schema == PIPELINE_STEP_PROTOCOL
    assert policy.pipeline_columns == list(PIPELINE_ROW_COLUMNS)
    assert policy.insert_columns == list(PIPELINE_INSERT_COLUMNS)
    assert policy.update_columns == list(PIPELINE_UPDATE_COLUMNS)
    assert policy.actions == list(PIPELINE_ACTIONS)
    assert decode_pipeline_policy(policy.model_dump(mode="json", by_alias=True)) == policy

    drift = policy.model_dump(mode="json", by_alias=True)
    drift["actions"].reverse()
    with pytest.raises(PipelineUnavailable):
        decode_pipeline_policy(drift)


@pytest.mark.parametrize(
    "stages",
    [None, "primitive", 7, True, {"kind": "object"}, [{"stage": 0}]],
)
def test_pipeline_row_preserves_json_null_and_scalar_stages(stages):
    row = _row_payload()
    row["stages"] = stages
    decoded = decode_pipeline_row(json.dumps(row, ensure_ascii=True))
    assert decoded.stages == stages
    assert type(decoded.stages) is type(stages)


def test_row_decoder_requires_exact_columns_and_ordered_result_arrays():
    first = _row_payload(row_id=uuid.uuid4())
    second = _row_payload(row_id=uuid.uuid4())
    rows = decode_pipeline_rows(json.dumps([first, second], ensure_ascii=True))
    assert [row.id for row in rows] == [uuid.UUID(first["id"]), uuid.UUID(second["id"])]

    first["creator_name"] = "not a stored column"
    with pytest.raises(PipelineUnavailable):
        decode_pipeline_row(json.dumps(first, ensure_ascii=True))


def test_pipeline_timestamps_keep_infinity_extrema_and_finite_overflow_phase():
    row = _row_payload(created_at="infinity")
    row["updated_at"] = "-infinity"
    decoded = decode_pipeline_row(json.dumps(row, ensure_ascii=True))
    assert decoded.created_at == datetime.max and decoded.created_at.tzinfo is None
    assert decoded.updated_at == datetime.min and decoded.updated_at.tzinfo is None

    row["created_at"] = "10000-01-01T00:00:00+00:00"
    with pytest.raises(OverflowError):
        decode_pipeline_row(json.dumps(row, ensure_ascii=True))
    with pytest.raises(ProjectQueryUnavailable):
        decode_postgres_timestamp("invalid timestamp")


def test_pipeline_schema_and_dto_roundtrip_cover_timestamp_infinity_extrema():
    extrema = (
        "infinity",
        "-infinity",
        datetime.max.isoformat(),
        datetime.min.isoformat(),
    )
    row_schema = pipeline_schema()["$defs"]["PipelineRow"]["properties"]
    for field in ("created_at", "updated_at"):
        alternatives = row_schema[field]["anyOf"]
        assert {"type": "string", "format": "date-time"} in alternatives
        assert {"type": "string", "enum": list(extrema)} in alternatives
        assert {"type": "null"} in alternatives

    for encoded, normalized in (
        ("infinity", datetime.max),
        ("-infinity", datetime.min),
        (datetime.max.isoformat(), datetime.max),
        (datetime.min.isoformat(), datetime.min),
    ):
        payload = _row_payload(created_at=encoded)
        decoded = decode_pipeline_row(json.dumps(payload, ensure_ascii=True))
        assert decoded.created_at == normalized
        dto_json = json.dumps(decoded.model_dump(mode="json", by_alias=True))
        assert PipelineRow.model_validate_json(dto_json).created_at == normalized


def test_oversized_stored_json_integer_keeps_the_driver_value_error_phase():
    row = _row_payload()
    raw = json.dumps(row, ensure_ascii=True)
    assert '"stages": null' in raw
    raw = raw.replace('"stages": null', '"stages": ' + "7" * 5000)
    with pytest.raises(ValueError):
        decode_pipeline_row(raw)


def test_write_codec_separates_json_null_from_sql_null_and_keeps_column_order():
    columns, values = encode_pipeline_columns(
        {
            "scope": "private",
            "stages": None,
            "project_id": None,
            "is_default": False,
        }
    )
    assert columns == ["scope", "stages", "project_id", "is_default"]
    assert values == ["private", "null", None, "false"]


def test_dirty_comparison_uses_python_json_equality_and_uuid_value_equality():
    project_id = uuid.uuid4()
    current = PipelineRow(
        id=uuid.uuid4(),
        scope="private",
        project_id=project_id,
        organization_id=None,
        name="same",
        stages={"enabled": True},
        is_default=False,
        created_by=uuid.uuid4(),
        usage_count=0,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert dirty_pipeline_payload(
        current,
        {
            "project_id": str(project_id),
            "name": "same",
            "stages": {"enabled": 1},
        },
    ) == {}
    assert dirty_pipeline_payload(current, {"name": "changed"}) == {
        "name": "changed"
    }
