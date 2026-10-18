"""Caller-transaction bindings for the trusted Assets operator SQL protocol."""

from __future__ import annotations

import json
from typing import Any, Iterable, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.operator_contract import (
    OPERATOR_BATCH_SIZE,
    OPERATOR_NATIVE_ARGUMENTS,
    OPERATOR_NATIVE_OPERATIONS,
    OPERATOR_NATIVE_SIGNATURES,
    OPERATOR_REPLY_BYTES,
    OPERATOR_REQUEST_BYTES,
    AppendOperatorItemsRequest,
    CreateOperatorImageDatasetRequest,
    EnsureOperatorNuScenesDatasetRequest,
    OperatorCountResult,
    OperatorDatasetMutation,
    OperatorFailure,
    OperatorItem,
    OperatorItemsMutation,
    OperatorItemsPage,
    OperatorSourceEntry,
    OperatorSourcePatch,
    PatchOperatorItemsRequest,
    ReadOperatorItemsRequest,
    RecountOperatorItemsRequest,
    decode_operator_reply,
    require_operator_scope,
)

_Result = TypeVar("_Result", bound=BaseModel)
_Entry = TypeVar("_Entry", OperatorSourceEntry, OperatorSourcePatch)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, allow_nan=False)


def _batches(entries: Iterable[_Entry]) -> Iterable[list[_Entry]]:
    batch: list[_Entry] = []
    size = 512
    for entry in entries:
        try:
            entry_size = len(_json(entry).encode("utf-8")) + 2
        except (ValueError, TypeError, UnicodeError):
            raise OperatorFailure("invalid_request") from None
        if entry_size + 512 > OPERATOR_REQUEST_BYTES:
            raise OperatorFailure("resource_budget_exceeded")
        if batch and (
            len(batch) == OPERATOR_BATCH_SIZE
            or size + entry_size > OPERATOR_REQUEST_BYTES
        ):
            yield batch
            batch, size = [], 512
        batch.append(entry)
        size += entry_size
    if batch:
        yield batch


def _restore_failure(exc: OperatorFailure) -> None:
    if exc.code == "storage_root_conflict":
        raise ValueError(
            "nuScenes dataset storage_root does not match its UUID, or legacy "
            "nuScenes storage root is shared by another dataset; migrate it before re-import"
        ) from None
    if exc.code == "axis_conflict":
        raise ValueError(
            "dataset axis_convention cannot accept the requested --frame; "
            "use a different --dataset-name to avoid mixing sensor and ego frames"
        ) from None
    raise exc


class AssetsOperatorGateway:
    """Keep operator registration on published SQL in the caller's transaction."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def _call(
        self, name: str, request: BaseModel, result_type: type[_Result]
    ) -> _Result:
        if OPERATOR_NATIVE_OPERATIONS.get(name) != (type(request), result_type):
            raise TypeError("invalid operator request/result pair")
        try:
            if len(_json(request).encode("utf-8")) > OPERATOR_REQUEST_BYTES:
                raise OperatorFailure("resource_budget_exceeded")
            types = OPERATOR_NATIVE_SIGNATURES[name].split(",")
            names = OPERATOR_NATIVE_ARGUMENTS[name]
            placeholders = ", ".join(
                f"CAST(:{argument} AS {kind})"
                for argument, kind in zip(names, types, strict=True)
            )
            parameters = {
                argument: _json(getattr(request, argument))
                if kind == "jsonb"
                else getattr(request, argument)
                for argument, kind in zip(names, types, strict=True)
            }
        except (TypeError, ValueError, UnicodeError):
            raise OperatorFailure("invalid_request") from None
        try:
            # Preserve pending changes in the same outer transaction before a
            # text-only native call, which does not trigger ORM autoflush.
            await self.db.flush()
            result = await self.db.execute(
                text(f"SELECT aap_assets_v1.{name}({placeholders})"), parameters
            )
            raw = result.scalar_one()
        except SQLAlchemyError as exc:
            raise OperatorFailure("storage_unavailable") from exc
        try:
            if isinstance(raw, bytes):
                if len(raw) > OPERATOR_REPLY_BYTES:
                    raise ValueError("oversized operator reply")
                raw = raw.decode("utf-8")
            body = raw if isinstance(raw, str) else _json(raw)
            if len(body.encode("utf-8")) > OPERATOR_REPLY_BYTES:
                raise ValueError("oversized operator reply")
            decoded = decode_operator_reply(raw, result_type)
            require_operator_scope(request, decoded)
            return decoded
        except OperatorFailure as exc:
            _restore_failure(exc)
        except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
            raise OperatorFailure("invalid_response") from None

    async def create_operator_image_dataset(
        self, request: CreateOperatorImageDatasetRequest
    ) -> OperatorDatasetMutation:
        return await self._call(
            "create_operator_image_dataset", request, OperatorDatasetMutation
        )

    async def ensure_operator_nuscenes_dataset(
        self, request: EnsureOperatorNuScenesDatasetRequest
    ) -> OperatorDatasetMutation:
        return await self._call(
            "ensure_operator_nuscenes_dataset", request, OperatorDatasetMutation
        )

    async def append_operator_items(
        self, request: AppendOperatorItemsRequest
    ) -> OperatorItemsMutation:
        return await self._call("append_operator_items", request, OperatorItemsMutation)

    async def patch_operator_items(
        self, request: PatchOperatorItemsRequest
    ) -> OperatorItemsMutation:
        return await self._call("patch_operator_items", request, OperatorItemsMutation)

    async def recount_operator_items(
        self, request: RecountOperatorItemsRequest
    ) -> OperatorCountResult:
        return await self._call("recount_operator_items", request, OperatorCountResult)

    async def read_operator_items(
        self, request: ReadOperatorItemsRequest
    ) -> OperatorItemsPage:
        return await self._call("read_operator_items", request, OperatorItemsPage)

    async def append_all_items(
        self, dataset_id: UUID, entries: Iterable[OperatorSourceEntry]
    ) -> tuple[OperatorItem, ...]:
        result = []
        for batch in _batches(entries):
            saved = await self.append_operator_items(
                AppendOperatorItemsRequest(
                    operation_id=uuid4(), dataset_id=dataset_id, entries=batch
                )
            )
            result.extend(saved.items)
        return tuple(result)

    async def patch_all_items(
        self, dataset_id: UUID, entries: Iterable[OperatorSourcePatch]
    ) -> int:
        filled = 0
        for batch in _batches(entries):
            saved = await self.patch_operator_items(
                PatchOperatorItemsRequest(
                    operation_id=uuid4(), dataset_id=dataset_id, entries=batch
                )
            )
            filled += saved.dimensions_backfilled
        return filled

    async def read_all_items(self, dataset_id: UUID) -> tuple[OperatorItem, ...]:
        cursor = None
        first: OperatorItemsPage | None = None
        items = []
        previous_id = None
        while True:
            page = await self.read_operator_items(
                ReadOperatorItemsRequest(dataset_id=dataset_id, after_item_id=cursor)
            )
            if first is None:
                first = page
            elif (page.snapshot_digest, page.total) != (
                first.snapshot_digest,
                first.total,
            ):
                raise OperatorFailure("source_changed")
            for item in page.items:
                if previous_id is not None and item.id <= previous_id:
                    raise OperatorFailure("invalid_response")
                items.append(item)
                previous_id = item.id
            if len(items) > first.total:
                raise OperatorFailure("invalid_response")
            if page.complete:
                if len(items) != first.total:
                    raise OperatorFailure("invalid_response")
                return tuple(items)
            if page.next_item_id is None or page.next_item_id == cursor:
                raise OperatorFailure("invalid_response")
            cursor = page.next_item_id
