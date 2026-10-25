from __future__ import annotations

from contextlib import asynccontextmanager
import json
import uuid

import pytest

from aap_labeling.class_queries import (
    LabelingClassQueryManager,
    materialize_labeling_class_query_data,
)
from aap_labeling.class_query_contract import (
    LABELING_CLASS_QUERY_PROTOCOL,
    LabelingClassQueryInvalidRequest,
    LabelingClassQueryUnavailable,
    decode_labeling_class_query_policy,
    decode_labeling_class_query_reply,
    decode_labeling_class_query_request,
    labeling_class_query_request,
    labeling_class_query_schema,
)

PROJECT_ID = uuid.UUID(int=22)
REQUEST_ID = uuid.UUID(int=23)


def request():
    return labeling_class_query_request(PROJECT_ID, request_id=REQUEST_ID)


def reply_payload(command, data_json):
    return {
        "schema": LABELING_CLASS_QUERY_PROTOCOL,
        "ok": True,
        "request": command.model_dump(mode="json", by_alias=True),
        "data_json": data_json,
    }


class Data:
    def __init__(self, classes, attributes, *, after_classes=None):
        self.classes = classes
        self.attributes = list(attributes)
        self.after_classes = after_classes
        self.events = []

    @asynccontextmanager
    async def transaction(self):
        self.events.append("transaction")
        yield self

    async def class_rows(self, project_id):
        self.events.append(("class_rows", project_id))
        if self.after_classes is not None:
            self.after_classes(self)
        return self.classes

    async def attribute_rows(self, project_id):
        self.events.append(("attribute_rows", project_id))
        return self.attributes


@pytest.mark.asyncio
async def test_manager_preserves_two_reads_falsy_classes_full_json_decode_and_live_attribute_read():
    data = Data(
        classes=[(None, 3), ("", 1), ("car", 2), ("truck", 1)],
        attributes=[
            '{"color":"red","nested":{"not_counted":true},"_system":{},"雪":1,"":0}',
            '"scalar-string"',
            "9007199254740993",
            "null",
            "[1,2]",
            None,
        ],
        after_classes=lambda owner: owner.attributes.append('{"between_reads":1}'),
    )
    reply = await LabelingClassQueryManager(data).execute(request())

    assert data.events == [
        "transaction",
        ("class_rows", PROJECT_ID),
        ("attribute_rows", PROJECT_ID),
    ]
    assert reply.request == request()
    assert json.loads(reply.data_json) == {
        "classes": {"car": 2, "truck": 1},
        "attributes": {
            "": 1,
            "_system": 1,
            "between_reads": 1,
            "color": 1,
            "nested": 1,
            "雪": 1,
        },
    }
    assert materialize_labeling_class_query_data(reply) == json.loads(reply.data_json)


@pytest.mark.asyncio
async def test_late_json_decode_failure_propagates_after_class_read_without_reply():
    data = Data(classes=[("car", 1)], attributes=['{"color":"red"}', "{"])
    with pytest.raises(json.JSONDecodeError):
        await LabelingClassQueryManager(data).execute(request())
    assert data.events == [
        "transaction",
        ("class_rows", PROJECT_ID),
        ("attribute_rows", PROJECT_ID),
    ]


@pytest.mark.asyncio
async def test_5000_digit_numeric_attribute_keeps_json_decoder_value_error_after_classes():
    data = Data(classes=[("car", 1)], attributes=["9" * 5000])
    with pytest.raises(ValueError, match="integer string conversion"):
        await LabelingClassQueryManager(data).execute(request())
    assert data.events[1] == ("class_rows", PROJECT_ID)
    assert data.events[2] == ("attribute_rows", PROJECT_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [("car", True)],
        [("car", 0)],
        [("car", -1)],
        [("car", 1), ("car", 2)],
        [(3, 1)],
        [("car", "1")],
    ],
)
async def test_invalid_class_aggregate_rows_are_unavailable_before_attribute_query(
    rows,
):
    data = Data(classes=rows, attributes=[])
    with pytest.raises(LabelingClassQueryUnavailable):
        await LabelingClassQueryManager(data).execute(request())
    assert data.events == ["transaction", ("class_rows", PROJECT_ID)]


def test_request_is_closed_duplicate_safe_and_binds_project_and_request_ids():
    command = request()
    assert (
        decode_labeling_class_query_request(
            command.model_dump(mode="json", by_alias=True)
        )
        == command
    )
    with pytest.raises(LabelingClassQueryInvalidRequest):
        decode_labeling_class_query_request(
            {
                **command.model_dump(mode="json", by_alias=True),
                "actor_id": str(PROJECT_ID),
            }
        )
    duplicate = (
        b'{"schema":"aap-labeling-class-query/v1",'
        b'"schema":"aap-labeling-class-query/v1",'
        b'"request_id":"00000000-0000-0000-0000-000000000017",'
        b'"operation":"get_project_class_usage",'
        b'"project_id":"00000000-0000-0000-0000-000000000016"}'
    )
    with pytest.raises(LabelingClassQueryInvalidRequest):
        decode_labeling_class_query_request(duplicate)


def test_policy_is_only_the_declared_schema_field():
    assert decode_labeling_class_query_policy({"schema": LABELING_CLASS_QUERY_PROTOCOL})
    with pytest.raises(LabelingClassQueryUnavailable):
        decode_labeling_class_query_policy(
            {"schema": LABELING_CLASS_QUERY_PROTOCOL, "database_url": "secret"}
        )


@pytest.mark.parametrize(
    "data_json",
    [
        '{"classes":{},"attributes":{},"extra":{}}',
        '{"classes":{"":1},"attributes":{}}',
        '{"classes":{"car":true},"attributes":{}}',
        '{"classes":{"car":0},"attributes":{}}',
        '{"classes":{},"attributes":{"雪":-1}}',
        '{"classes":{},"attributes":{"雪":1,"雪":2}}',
        '{"classes":{},"attributes":{},"attributes":{}}',
        '{"classes":{},"attributes":{"雪":1}}',
    ],
)
def test_reply_data_requires_exact_maps_and_positive_strict_counts(data_json):
    command = request()
    with pytest.raises(LabelingClassQueryUnavailable):
        decode_labeling_class_query_reply(command, reply_payload(command, data_json))


def test_reply_allows_empty_unicode_attribute_keys_and_rejects_wrong_request_binding():
    command = request()
    data_json = json.dumps(
        {"classes": {"汽车": 4}, "attributes": {"": 2, "雪": 1}},
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    decoded = decode_labeling_class_query_reply(
        command, reply_payload(command, data_json)
    )
    assert decoded.request.project_id == PROJECT_ID

    changed = reply_payload(command, data_json)
    changed["request"]["request_id"] = str(uuid.UUID(int=24))
    with pytest.raises(LabelingClassQueryUnavailable):
        decode_labeling_class_query_reply(command, changed)

    changed = reply_payload(command, data_json)
    changed["request"]["project_id"] = str(uuid.UUID(int=25))
    with pytest.raises(LabelingClassQueryUnavailable):
        decode_labeling_class_query_reply(command, changed)


def test_generated_protocol_schema_is_distributed_and_describes_map_constraints():
    from importlib.resources import files

    schema = labeling_class_query_schema()
    packaged = json.loads(
        files("aap_labeling").joinpath("class_query_protocol.schema.json").read_text()
    )
    assert packaged == schema
    assert schema["x-data-json-schema"]["properties"]["classes"]["propertyNames"] == {
        "minLength": 1
    }
