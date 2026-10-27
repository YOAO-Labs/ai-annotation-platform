"""Bind synchronous Dataset source ingest to Assets-owned SQL capabilities."""

from __future__ import annotations

import json
from uuid import UUID

from app.services import managed_original_native as native_lifecycle
from aap_assets_source.managed_original_contract import (
    ManagedOriginalMaterial,
)
from aap_assets_source.ingest_contract import (
    IngestBatchPlan,
    IngestBatchResult,
    IngestCommand,
    IngestContext,
    IngestDescriptor,
    IngestEntryCompletion,
    IngestFailure,
    IngestFinish,
    IngestLayoutPage,
    IngestReceipt,
    IngestSourceEntry,
    decode_ingest_reply,
    require_ingest_scope,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession


class DatasetSourceIngestData:
    """Call only the five owner functions, validating every scoped reply."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def allocate(self, request: dict) -> ManagedOriginalMaterial:
        try:
            return await native_lifecycle.begin_admitted(self._db, request)
        except ValueError:
            raise IngestFailure("invalid_response") from None

    async def open_operation(self, creation_id):
        try:
            return await native_lifecycle.open_operation(self._db, creation_id)
        except ValueError:
            raise IngestFailure("invalid_response") from None

    async def close_operation(
        self, creation_id, operation_id, physical_settled
    ) -> None:
        try:
            await native_lifecycle.close_operation(
                self._db, creation_id, operation_id, physical_settled
            )
        except ValueError:
            raise IngestFailure("invalid_response") from None

    async def original_prepared(self, creation_id, prepared) -> None:
        await native_lifecycle.prepared(self._db, creation_id, prepared)

    async def original_writing(self, creation_id) -> None:
        await native_lifecycle.writing(self._db, creation_id)

    async def original_written(self, creation_id, receipt) -> None:
        await native_lifecycle.written(self._db, creation_id, receipt)

    async def original_verified(self, creation_id, verification) -> None:
        await native_lifecycle.verified(self._db, creation_id, verification)

    async def fail_original(self, creation_id, outcome, error_code, evidence) -> None:
        await native_lifecycle.fail(
            self._db, creation_id, outcome, error_code, evidence
        )

    async def begin(
        self, command: IngestCommand, descriptor: IngestDescriptor
    ) -> IngestContext:
        result = await self._call(
            """
            SELECT aap_assets_v1.begin_dataset_ingest(
                CAST(:operation_id AS uuid), CAST(:actor_id AS uuid),
                CAST(:dataset_id AS uuid), CAST(:descriptor AS jsonb)
            )
            """,
            {
                **_scope_params(command),
                "descriptor": _json(descriptor),
            },
            IngestContext,
            command,
        )
        if result.descriptor != descriptor:
            raise IngestFailure("invalid_response")
        return result

    async def prepare_batch(
        self,
        command: IngestCommand,
        batch_id: UUID,
        entries: list[IngestSourceEntry],
    ) -> IngestBatchPlan:
        result = await self._call(
            """
            SELECT aap_assets_v1.prepare_dataset_ingest_batch(
                CAST(:operation_id AS uuid), CAST(:actor_id AS uuid),
                CAST(:dataset_id AS uuid), CAST(:batch_id AS uuid),
                CAST(:entries AS jsonb)
            )
            """,
            {
                **_scope_params(command),
                "batch_id": batch_id,
                "entries": _json_list(entries),
            },
            IngestBatchPlan,
            command,
        )
        if result.batch_id != batch_id or len(result.entries) != len(entries):
            raise IngestFailure("invalid_response")
        fields = set(IngestSourceEntry.model_fields)
        for requested, planned in zip(entries, result.entries, strict=True):
            if (
                IngestSourceEntry.model_validate(planned.model_dump(include=fields))
                != requested
            ):
                raise IngestFailure("invalid_response")
        return result

    async def complete_batch(
        self,
        command: IngestCommand,
        batch_id: UUID,
        entries: list[IngestEntryCompletion],
    ) -> IngestBatchResult:
        result = await self._call(
            """
            SELECT aap_assets_v1.complete_dataset_ingest_batch(
                CAST(:operation_id AS uuid), CAST(:actor_id AS uuid),
                CAST(:dataset_id AS uuid), CAST(:batch_id AS uuid),
                CAST(:entries AS jsonb)
            )
            """,
            {
                **_scope_params(command),
                "batch_id": batch_id,
                "entries": _json_list(entries),
            },
            IngestBatchResult,
            command,
        )
        if result.batch_id != batch_id:
            raise IngestFailure("invalid_response")
        return result

    async def read_layout(
        self,
        command: IngestCommand,
        *,
        after_item_id: UUID | None,
        limit: int = 500,
    ) -> IngestLayoutPage:
        result = await self._call(
            """
            SELECT aap_assets_v1.read_dataset_ingest_layout(
                CAST(:operation_id AS uuid), CAST(:actor_id AS uuid),
                CAST(:dataset_id AS uuid), CAST(:after_item_id AS uuid),
                CAST(:limit AS integer)
            )
            """,
            {
                **_scope_params(command),
                "after_item_id": after_item_id,
                "limit": limit,
            },
            IngestLayoutPage,
            command,
        )
        if after_item_id is not None and any(
            item.id <= after_item_id for item in result.items
        ):
            raise IngestFailure("invalid_response")
        return result

    async def finish(
        self, command: IngestCommand, finish: IngestFinish
    ) -> IngestReceipt:
        return await self._call(
            """
            SELECT aap_assets_v1.finish_dataset_ingest(
                CAST(:operation_id AS uuid), CAST(:actor_id AS uuid),
                CAST(:dataset_id AS uuid), CAST(:finish AS jsonb)
            )
            """,
            {
                **_scope_params(command),
                "finish": _json(finish),
            },
            IngestReceipt,
            command,
        )

    async def _call(self, statement, parameters, result_type, command):
        try:
            payload = await self._db.scalar(text(statement), parameters)
        except SQLAlchemyError as exc:
            raise IngestFailure("service_unavailable") from exc
        result = decode_ingest_reply(payload, result_type)
        if result is None:
            raise IngestFailure("invalid_response")
        require_ingest_scope(command, result)
        return result


def _scope_params(command: IngestCommand) -> dict[str, UUID]:
    return {
        "operation_id": command.operation_id,
        "actor_id": command.actor_id,
        "dataset_id": command.dataset_id,
    }


def _json(value) -> str:
    return json.dumps(
        value.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    )


def _json_list(values: list) -> str:
    return json.dumps(
        [value.model_dump(mode="json") for value in values],
        ensure_ascii=False,
        separators=(",", ":"),
    )
