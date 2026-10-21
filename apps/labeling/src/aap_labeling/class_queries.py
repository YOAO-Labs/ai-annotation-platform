"""Portable manager for the legacy two-read Project class-usage projection."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import AsyncContextManager, Protocol
import uuid

from .class_query_contract import (
    LABELING_CLASS_QUERY_PROTOCOL,
    LabelingClassQueryReply,
    LabelingClassQuerySuccess,
    LabelingClassQueryUnavailable,
    decode_labeling_class_query_reply,
    decode_labeling_class_query_request,
)


class LabelingClassQueryTransaction(Protocol):
    """Borrowed data port for the two legacy annotation projections."""

    async def class_rows(
        self, project_id: uuid.UUID
    ) -> Sequence[tuple[str | None, int]]:
        """Group active annotations by class_name and count annotation IDs."""
        ...

    async def attribute_rows(self, project_id: uuid.UUID) -> Sequence[str | None]:
        """Return full JSONB text for all active annotations in the Project."""
        ...


class LabelingClassQueryData(Protocol):
    """Provide a borrowed transaction; the query manager never commits it."""

    def transaction(self) -> AsyncContextManager[LabelingClassQueryTransaction]: ...


class LabelingClassQueryManager:
    """Run class aggregation before full attribute reads, without owning commit."""

    def __init__(self, data: LabelingClassQueryData):
        self.data = data

    async def execute(self, payload: object) -> LabelingClassQueryReply:
        request = decode_labeling_class_query_request(payload)
        async with self.data.transaction() as transaction:
            class_rows = await transaction.class_rows(request.project_id)
            classes = self._class_counts(class_rows)
            attribute_rows = await transaction.attribute_rows(request.project_id)
            decoded_attributes = self._decode_all_attributes(attribute_rows)
            attributes = self._attribute_counts(decoded_attributes)

        data_json = json.dumps(
            {"classes": classes, "attributes": attributes},
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        reply = LabelingClassQuerySuccess(
            schema=LABELING_CLASS_QUERY_PROTOCOL,
            ok=True,
            request=request,
            data_json=data_json,
        )
        return decode_labeling_class_query_reply(request, reply)

    @staticmethod
    def _class_counts(rows: Sequence[tuple[str | None, int]]) -> dict[str, int]:
        if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
            raise LabelingClassQueryUnavailable()
        result = {}
        for row in rows:
            if type(row) not in (tuple, list) or len(row) != 2:
                raise LabelingClassQueryUnavailable()
            class_name, count = row
            if class_name is not None and type(class_name) is not str:
                raise LabelingClassQueryUnavailable()
            if type(count) is not int or count <= 0:
                raise LabelingClassQueryUnavailable()
            if not class_name:
                continue
            if class_name in result:
                raise LabelingClassQueryUnavailable()
            result[class_name] = count
        return result

    @staticmethod
    def _decode_all_attributes(rows: Sequence[str | None]) -> list[object]:
        if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Sequence):
            raise LabelingClassQueryUnavailable()
        decoded = []
        for value in rows:
            if value is None:
                decoded.append(None)
            elif type(value) is str:
                decoded.append(
                    json.loads(
                        value, parse_constant=LabelingClassQueryManager._reject_constant
                    )
                )
            else:
                raise LabelingClassQueryUnavailable()
        return decoded

    @staticmethod
    def _attribute_counts(values: Sequence[object]) -> dict[str, int]:
        result = {}
        for value in values:
            if isinstance(value, dict):
                for key in value:
                    # JSON object member names are strings.
                    if type(key) is not str:
                        raise LabelingClassQueryUnavailable()
                    result[key] = result.get(key, 0) + 1
        return result

    @staticmethod
    def _reject_constant(value: str):
        raise ValueError(f"Invalid stored JSON number: {value}")


def materialize_labeling_class_query_data(reply: LabelingClassQueryReply) -> dict:
    reply = decode_labeling_class_query_reply(reply.request, reply)
    return json.loads(reply.data_json)
