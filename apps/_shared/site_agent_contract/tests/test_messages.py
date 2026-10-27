"""Wire boundaries are checked without API imports, filesystem access or clocks."""

from copy import deepcopy
import hashlib
import json
import uuid

import pytest

from aap_site_agent_contract import (
    MAX_CONTENT_BYTES,
    MAX_MESSAGE_BYTES,
    ProtocolError,
    canonical_json,
    digest,
    parse_json,
    validate_ack,
    validate_command,
    validate_result,
)

COMMAND_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
AGENT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LOCATION_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SHA = "a" * 64


def anchor():
    return {"path_digest": SHA, "root": [1, 2], "base": [1, 3]}


def observation():
    return {
        "identity": [1, 4],
        "size": 128,
        "mode": 33188,
        "mtime_ns": 123,
        "ctime_ns": 456,
        "source_identity": [1, 3],
    }


def command(operation="scan", arguments=None):
    defaults = {
        "probe": {},
        "browse": {"path": "", "limit": 200},
        "scan": {"path": ""},
        "read": {"path": "images/猫.png"},
    }
    return {
        "protocol_version": 1,
        "command_id": COMMAND_ID,
        "agent_id": AGENT_ID,
        "credential_revision": 1,
        "root_handle": "photos_1-A",
        "root_revision": 2,
        "operation": operation,
        "arguments": defaults[operation] if arguments is None else arguments,
        "issued_at": 1800000000,
        "expires_at": 1800000120,
        "max_bytes": MAX_CONTENT_BYTES,
        "timeout_seconds": 60,
        "fence": {
            "location_id": LOCATION_ID,
            "location_revision": 1,
            "maintenance_revision": 1,
            "purpose": "scan",
        },
    }


def test_optional_bandwidth_preserves_legacy_envelope_and_digest():
    legacy = validate_command(command())
    assert "bandwidth_bytes_per_second" not in legacy
    assert validate_command(legacy) == legacy
    limited = validate_command({**legacy, "bandwidth_bytes_per_second": 1024})
    assert limited == {**legacy, "bandwidth_bytes_per_second": 1024}
    assert digest(legacy) != digest(limited)
    assert validate_command(limited) == limited


@pytest.mark.parametrize("rate", [None, True, 1.5, "1024", 1023, 1024**3 + 1])
def test_command_bandwidth_is_a_strict_bounded_integer(rate):
    with pytest.raises(ProtocolError, match="invalid_command"):
        validate_command({**command(), "bandwidth_bytes_per_second": rate})


def test_command_bandwidth_accepts_the_maximum():
    assert (
        validate_command({**command(), "bandwidth_bytes_per_second": 1024**3})[
            "bandwidth_bytes_per_second"
        ]
        == 1024**3
    )


@pytest.mark.parametrize("path", ["../outside", "/outside", "a//b", "a\\b"])
def test_read_source_path_is_relative_and_bounded(path):
    rejects(
        validate_command, command("read", {"path": "image.png", "source_path": path})
    )


def result():
    return {
        "protocol_version": 1,
        "command_id": COMMAND_ID,
        "command_digest": digest(validate_command(command())),
        "status": "completed",
        "result": {"digest": SHA, "entries": [{"name": "猫.png"}]},
        "error_code": None,
        "closed": True,
    }


def ack():
    response = result()
    return {
        "protocol_version": 1,
        "command_id": COMMAND_ID,
        "command_digest": response["command_digest"],
        "result_digest": digest(validate_result(response)),
        "state": "received",
        "code": None,
    }


def rejects(call, value, code=None):
    with pytest.raises(ProtocolError) as caught:
        call(value)
    if code is not None:
        assert caught.value.code == code
        assert str(caught.value) == code


def test_canonical_bytes_are_deterministic_ascii_escaped_and_compact():
    first = {"z": [True, None, 1.5], "a": "猫"}
    second = {"a": "猫", "z": [True, None, 1.5]}
    expected = b'{"a":"\\u732b","z":[true,null,1.5]}'
    assert canonical_json(first) == canonical_json(second) == expected
    assert digest(first) == hashlib.sha256(expected).hexdigest()
    assert parse_json(expected) == first
    assert digest({"a": 1}) != digest({"a": 1.0})


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        {"nested": float("nan")},
        {1: "key"},
        {"a": b"bytes"},
        {"a": (1, 2)},
        {"a": {1, 2}},
        "\ud800",
        {"\udfff": "key"},
        object(),
        uuid.UUID(COMMAND_ID),
    ],
)
def test_canonical_rejects_non_json_and_invalid_unicode(value):
    rejects(canonical_json, value, "invalid_json")


def test_canonical_rejects_cycles_without_leaking_python_errors():
    value = []
    value.append(value)
    rejects(canonical_json, value, "invalid_json")


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"outer":{"a":1,"a":2}}',
        b'{"a":1,"\\u0061":2}',
        b'[{"a":1,"a":2}]',
    ],
)
def test_parse_rejects_duplicate_keys_at_any_depth(raw):
    rejects(parse_json, raw, "duplicate_json_key")


@pytest.mark.parametrize(
    "raw",
    [
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b"1e9999",
        b"{",
        b"",
        b"{} {}",
        b'{"key":true,}',
        b'"\xff"',
        b'"\\ud800"',
        b"\xef\xbb\xbf{}",
        "{}",
        bytearray(b"{}"),
        None,
    ],
)
def test_parse_rejects_invalid_or_non_utf8_json(raw):
    rejects(parse_json, raw, "invalid_json")


@pytest.mark.parametrize("limit", [True, False, 0, -1, 2.0, "2", None])
def test_parse_rejects_invalid_limits(limit):
    with pytest.raises(ProtocolError, match="invalid_message_limit"):
        parse_json(b"{}", max_bytes=limit)


def test_parse_measures_actual_wire_bytes_and_accepts_exact_limit():
    raw = '{"猫":0}'.encode()
    assert parse_json(raw, max_bytes=len(raw)) == {"猫": 0}
    with pytest.raises(ProtocolError, match="message_too_large"):
        parse_json(raw, max_bytes=len(raw) - 1)
    rejects(parse_json, b" " * (MAX_MESSAGE_BYTES + 1), "message_too_large")


@pytest.mark.parametrize(
    "validator,factory",
    [(validate_command, command), (validate_result, result), (validate_ack, ack)],
)
def test_all_messages_reject_missing_and_unknown_fields(validator, factory):
    original = factory()
    for field in original:
        value = deepcopy(original)
        del value[field]
        rejects(validator, value)
    value = factory()
    value["target_url"] = "https://untrusted.invalid"
    rejects(validator, value)


@pytest.mark.parametrize("validator", [validate_command, validate_result, validate_ack])
@pytest.mark.parametrize("value", [[], None, True, "message", 1])
def test_top_level_messages_must_be_objects(validator, value):
    rejects(validator, value)


@pytest.mark.parametrize(
    "validator,factory",
    [(validate_command, command), (validate_result, result), (validate_ack, ack)],
)
@pytest.mark.parametrize("version", [True, False, 0, 2, "1", 1.0, None, []])
def test_protocol_version_is_a_strict_integer(validator, factory, version):
    value = factory()
    value["protocol_version"] = version
    rejects(validator, value, "unsupported_protocol_version")


@pytest.mark.parametrize(
    "field,bad",
    [
        ("command_id", None),
        ("agent_id", 2),
        ("command_id", "no-uuid"),
        ("credential_revision", True),
        ("credential_revision", 0),
        ("credential_revision", "1"),
        ("root_revision", -1),
        ("root_revision", 1.0),
        ("root_handle", ""),
        ("root_handle", "a" * 65),
        ("root_handle", "../root"),
        ("root_handle", "root/path"),
        ("root_handle", "https://example.com"),
        ("root_handle", "root\n"),
        ("operation", "exec"),
        ("operation", "stat"),
        ("operation", {}),
        ("issued_at", True),
        ("issued_at", -1),
        ("issued_at", 1.5),
        ("expires_at", "1800000100"),
        ("expires_at", False),
        ("max_bytes", 0),
        ("max_bytes", MAX_CONTENT_BYTES + 1),
        ("max_bytes", True),
        ("max_bytes", "64"),
        ("timeout_seconds", 0),
        ("timeout_seconds", 61),
        ("timeout_seconds", True),
        ("timeout_seconds", 10.0),
        ("fence", []),
        ("arguments", None),
    ],
)
def test_command_rejects_wrong_types_or_bounds(field, bad):
    value = command()
    value[field] = bad
    rejects(validate_command, value)


@pytest.mark.parametrize("lifetime", [-1, 0, 121])
def test_command_lifetime_is_positive_and_at_most_120_seconds(lifetime):
    value = command()
    value["expires_at"] = value["issued_at"] + lifetime
    rejects(validate_command, value, "invalid_command")


@pytest.mark.parametrize("lifetime", [1, 120])
def test_command_structural_expiry_has_no_implicit_current_clock(lifetime):
    value = command()
    value.update(issued_at=0, expires_at=lifetime, max_bytes=1, timeout_seconds=1)
    assert validate_command(value)["expires_at"] == lifetime


def test_command_normalizes_uuids_defaults_and_full_digest():
    source = command()
    source["command_id"] = COMMAND_ID.upper()
    normalized = validate_command(source)
    assert normalized["command_id"] == COMMAND_ID
    assert normalized["arguments"] == {
        "relative_path": "",
        "expected_anchor": None,
        "path": "",
        "expected_identity": None,
    }
    assert validate_command(normalized) == normalized
    assert digest(normalized) != digest(source)
    reordered = dict(reversed(list(normalized.items())))
    assert digest(normalized) == digest(reordered)
    for field, changed in [
        ("max_bytes", 1),
        ("root_revision", 3),
        ("command_id", AGENT_ID),
    ]:
        other = deepcopy(normalized)
        other[field] = changed
        assert digest(other) != digest(normalized)
    assert set(normalized["fence"]) == set(source["fence"])


@pytest.mark.parametrize(
    "field", ["location_id", "location_revision", "maintenance_revision", "purpose"]
)
def test_fence_requires_authority_fields(field):
    value = command()
    del value["fence"][field]
    rejects(validate_command, value, "invalid_fence")


@pytest.mark.parametrize(
    "field,bad",
    [
        ("url", "https://untrusted.invalid"),
        ("location_id", "invalid"),
        ("source_id", None),
        ("run_id", 42),
        ("job_id", "invalid"),
        ("asset_id", False),
        ("user_id", "invalid"),
        ("location_revision", True),
        ("maintenance_revision", 0),
        ("generation", 0),
        ("generation", 1.0),
        ("purpose", ""),
        ("purpose", "a" * 65),
        ("purpose", "read/url"),
        ("purpose", "arbitrary command"),
        ("purpose", "Read"),
        ("task_id", ""),
        ("task_id", "x" * 129),
        ("task_id", "x\n"),
        ("lease_token", None),
        ("lease_token", True),
        ("lease_token", "x y"),
    ],
)
def test_fence_is_a_closed_typed_schema(field, bad):
    value = command()
    value["fence"][field] = bad
    rejects(validate_command, value, "invalid_fence")


def test_all_optional_fence_fields_are_preserved_and_normalized():
    value = command()
    for field in ("source_id", "run_id", "job_id", "asset_id", "user_id"):
        value["fence"][field] = AGENT_ID.upper()
    value["fence"].update(
        task_id="task:123@node", generation=2, lease_token="lease-123"
    )
    output = validate_command(value)["fence"]
    assert output["source_id"] == AGENT_ID
    assert output["generation"] == 2
    assert output["lease_token"] == "lease-123"


@pytest.mark.parametrize("operation", ["probe", "browse", "scan", "read"])
@pytest.mark.parametrize(
    "field", ["url", "target_url", "command", "shell", "root", "root_handle"]
)
def test_each_operation_rejects_arbitrary_arguments(operation, field):
    value = command(operation)
    value["arguments"][field] = "untrusted"
    rejects(validate_command, value, "invalid_arguments")


@pytest.mark.parametrize(
    "path",
    [
        "/absolute",
        "../outside",
        "a/../b",
        "./a",
        "a/./b",
        "a//b",
        "a/",
        "//x",
        "a\\b",
        "a\x00b",
        "a" * 1001,
        "/".join(["a"] * 33),
    ],
)
@pytest.mark.parametrize("field", ["path", "relative_path"])
def test_paths_reject_escape_or_ambiguous_segments(path, field):
    value = command("scan")
    value["arguments"][field] = path
    rejects(validate_command, value, "invalid_path")


@pytest.mark.parametrize(
    "path", ["photos", "目录/猫.png", "a b/x-y_2.png", "/".join(["a"] * 32), "a" * 1000]
)
def test_paths_preserve_valid_linux_relative_names(path):
    value = command("read")
    value["arguments"]["path"] = path
    assert validate_command(value)["arguments"]["path"] == path


@pytest.mark.parametrize("operation", ["browse", "scan", "read"])
def test_path_is_required_on_path_operations(operation):
    value = command(operation)
    del value["arguments"]["path"]
    rejects(validate_command, value, "invalid_arguments")


@pytest.mark.parametrize("operation", ["browse", "scan"])
def test_directory_path_can_be_empty(operation):
    assert validate_command(command(operation))["arguments"]["path"] == ""


def test_read_path_cannot_be_empty():
    rejects(validate_command, command("read", {"path": ""}), "invalid_path")


@pytest.mark.parametrize("sample", ["", "../image", "/image", "x//y", 1, False])
def test_probe_sample_is_an_optional_nonempty_relative_file_path(sample):
    rejects(validate_command, command("probe", {"sample_file": sample}), "invalid_path")


def test_probe_defaults_and_sample():
    assert validate_command(command("probe"))["arguments"] == {
        "relative_path": "",
        "expected_anchor": None,
        "sample_file": None,
    }
    assert (
        validate_command(command("probe", {"sample_file": "images/x.png"}))[
            "arguments"
        ]["sample_file"]
        == "images/x.png"
    )


@pytest.mark.parametrize("limit", [0, 201, True, 1.0, None])
def test_browse_limit_is_strict_and_bounded(limit):
    rejects(
        validate_command,
        command("browse", {"path": "", "limit": limit}),
        "invalid_arguments",
    )


@pytest.mark.parametrize(
    "field,bad",
    [
        ("after", ""),
        ("after", "a/b"),
        ("after", ".."),
        ("snapshot", "b" * 63),
        ("snapshot", "A" * 64),
        ("snapshot", True),
    ],
)
def test_browse_cursor_parts_are_typed_and_bounded(field, bad):
    value = command("browse")
    value["arguments"][field] = bad
    rejects(validate_command, value)


def test_browse_preserves_cursor_and_requires_limit():
    value = command(
        "browse", {"path": "images", "limit": 1, "after": "猫.png", "snapshot": SHA}
    )
    assert validate_command(value)["arguments"]["after"] == "猫.png"
    del value["arguments"]["limit"]
    rejects(validate_command, value, "invalid_arguments")


@pytest.mark.parametrize(
    "bad",
    [
        [],
        {},
        {"path_digest": SHA, "root": [1, 2]},
        {"path_digest": "A" * 64, "root": [1, 2], "base": [1, 3]},
        {"path_digest": SHA, "root": [1, 2], "base": [1, 3], "url": "x"},
    ],
)
def test_anchor_requires_exact_digest_and_root_pairs(bad):
    value = command()
    value["arguments"]["expected_anchor"] = bad
    rejects(validate_command, value, "invalid_anchor")


@pytest.mark.parametrize(
    "pair",
    [
        [],
        [1],
        [1, 2, 3],
        [True, 2],
        [0, 2],
        [-1, 2],
        [1.0, 2],
        [1, False],
        [1, "2"],
        [1, 2.0],
        "1,2",
    ],
)
@pytest.mark.parametrize(
    "target",
    [
        "anchor_root",
        "anchor_base",
        "expected_identity",
        "expected_source_identity",
        "observation_identity",
        "observation_source_identity",
    ],
)
def test_numeric_pairs_use_positive_first_and_strict_integer_second(pair, target):
    value = command(
        "read"
        if target
        in {
            "expected_source_identity",
            "observation_identity",
            "observation_source_identity",
        }
        else "scan"
    )
    if target.startswith("anchor_"):
        value["arguments"]["expected_anchor"] = anchor()
        value["arguments"]["expected_anchor"][target.removeprefix("anchor_")] = pair
    elif target.startswith("observation_"):
        value["arguments"]["expected"] = observation()
        value["arguments"]["expected"][target.removeprefix("observation_")] = pair
    else:
        value["arguments"][target] = pair
    rejects(validate_command, value)


@pytest.mark.parametrize("second", [-1, 0, 1])
def test_numeric_pair_second_is_an_integer_without_an_invented_sign_constraint(second):
    value = command("scan", {"path": "", "expected_identity": [1, second]})
    assert validate_command(value)["arguments"]["expected_identity"] == [1, second]


@pytest.mark.parametrize(
    "field,bad",
    [
        ("identity", None),
        ("size", True),
        ("size", -1),
        ("size", MAX_CONTENT_BYTES + 1),
        ("mode", -1),
        ("mode", False),
        ("mtime_ns", 1.0),
        ("ctime_ns", "1"),
        ("unexpected", 1),
    ],
)
def test_read_expected_observation_is_closed_and_strict(field, bad):
    expected = observation()
    expected[field] = bad
    rejects(
        validate_command,
        command("read", {"path": "a.png", "expected": expected}),
        "invalid_observation",
    )


@pytest.mark.parametrize(
    "field,bad",
    [
        ("expected", []),
        ("expected_sha256", "A" * 64),
        ("expected_sha256", "g" * 64),
        ("expected_sha256", "a" * 63),
        ("expected_sha256", 1),
        ("expected_size", -1),
        ("expected_size", MAX_CONTENT_BYTES + 1),
        ("expected_size", True),
        ("expected_size", "1"),
    ],
)
def test_read_content_identity_and_bounds(field, bad):
    value = command("read")
    value["arguments"][field] = bad
    rejects(validate_command, value)


def test_read_observation_can_be_partial_and_preserves_negative_timestamps():
    value = command(
        "read",
        {
            "path": "a.png",
            "expected": {"size": 0, "mtime_ns": -1},
            "expected_size": 0,
            "expected_sha256": SHA,
        },
    )
    output = validate_command(value)
    assert output["arguments"]["expected"] == {"size": 0, "mtime_ns": -1}
    assert output["arguments"]["expected_source_identity"] is None


@pytest.mark.parametrize(
    "field,bad",
    [
        ("command_id", 1),
        ("command_digest", "A" * 64),
        ("command_digest", "b" * 63),
        ("status", "running"),
        ("status", True),
        ("result", []),
        ("error_code", "oops"),
        ("closed", False),
        ("closed", 1),
    ],
)
def test_result_rejects_invalid_identity_types_and_completion_evidence(field, bad):
    value = result()
    value[field] = bad
    rejects(validate_result, value)


@pytest.mark.parametrize("closed", [True, False])
def test_failed_result_can_preserve_unsettled_close_evidence(closed):
    value = result()
    value.update(status="failed", result={}, error_code="source_changed", closed=closed)
    assert validate_result(value)["closed"] is closed


@pytest.mark.parametrize("error", [None, "", "UPPER", "contains space", "x" * 65, True])
def test_failed_result_requires_safe_error_code(error):
    value = result()
    value.update(status="failed", result={}, error_code=error)
    rejects(validate_result, value, "invalid_result")


def test_failed_result_does_not_publish_partial_success_payload():
    value = result()
    value.update(status="failed", error_code="source_changed")
    rejects(validate_result, value, "invalid_result")


def test_opaque_result_is_bounded_native_json_and_digest_covers_close_evidence():
    value = result()
    value["result"] = {"future_schema": {"x": [True, None, 1.25, "猫"]}}
    checked = validate_result(value)
    assert checked["result"] == value["result"]
    value["result"] = {"blob": "x" * MAX_MESSAGE_BYTES}
    rejects(validate_result, value, "message_too_large")
    value = result()
    value.update(status="failed", error_code="read_failed", result={})
    before = digest(validate_result(value))
    value["closed"] = False
    assert digest(validate_result(value)) != before


@pytest.mark.parametrize("state", ["received", "applied"])
def test_ack_distinguishes_received_from_applied_without_changing_result_digest(state):
    value = ack()
    value["state"] = state
    checked = validate_ack(value)
    assert checked["state"] == state
    assert checked["result_digest"] == digest(validate_result(result()))


@pytest.mark.parametrize(
    "field,bad",
    [
        ("command_id", "bad"),
        ("command_digest", "A" * 64),
        ("result_digest", "a" * 63),
        ("state", "completed"),
        ("state", True),
        ("code", "unexpected"),
    ],
)
def test_ack_rejects_invalid_identity_state_or_code(field, bad):
    value = ack()
    value[field] = bad
    rejects(validate_ack, value)


@pytest.mark.parametrize("code", [None, "", "bad code", "x" * 65, False])
def test_rejected_ack_requires_a_safe_code(code):
    value = ack()
    value.update(state="rejected", code=code)
    rejects(validate_ack, value, "invalid_ack")


def test_rejected_ack_is_valid_with_a_safe_code():
    value = ack()
    value.update(state="rejected", code="lease_expired")
    assert validate_ack(value)["code"] == "lease_expired"


def test_validated_command_is_detached_in_both_directions():
    value = command(
        "read",
        {"path": "a.png", "expected_anchor": anchor(), "expected": observation()},
    )
    original = deepcopy(value)
    checked = validate_command(value)
    checked["arguments"]["expected_anchor"]["root"][0] = 99
    checked["arguments"]["expected"]["identity"][0] = 99
    checked["fence"]["purpose"] = "changed"
    assert value == original
    value["arguments"]["expected"]["source_identity"][0] = 100
    assert checked["arguments"]["expected"]["source_identity"][0] == 1


def test_validated_results_and_acks_are_detached_and_wire_roundtrip_is_stable():
    value = result()
    checked = validate_result(value)
    checked["result"]["entries"][0]["name"] = "changed"
    assert value["result"]["entries"][0]["name"] == "猫.png"
    response = ack()
    detached = validate_ack(response)
    detached["state"] = "applied"
    assert response["state"] == "received"
    for validator, factory in [
        (validate_command, command),
        (validate_result, result),
        (validate_ack, ack),
    ]:
        normalized = validator(factory())
        assert validator(parse_json(canonical_json(normalized))) == normalized


def test_non_json_nested_values_are_rejected_before_hashing_messages():
    value = result()
    value["result"]["bad"] = float("inf")
    rejects(validate_result, value, "invalid_json")
    assert json.loads(canonical_json(validate_command(command()))) == validate_command(
        command()
    )
