"""PostgreSQL bindings for Assets-owned Dataset upload intents."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import managed_original_native as native_lifecycle
from aap_assets_source.managed_original_contract import (
    ManagedOriginalMaterial,
)
from aap_assets_source.upload_contract import (
    DatasetUploadAcceptance,
    DatasetUploadCompletion,
    DatasetUploadDataPort,
    DatasetUploadDiscard,
    DatasetUploadDuplicate,
    DatasetUploadIntent,
    DatasetUploadReserve,
    canonical_content_hash,
    decode_dataset_upload_reply,
)


def _json(value) -> str:
    import json

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class DatasetUploadData(DatasetUploadDataPort):
    """Bind upload facts to the caller's transaction without ORM rows or commits."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def allocate(self, request: dict) -> ManagedOriginalMaterial:
        return await native_lifecycle.begin_admitted(self.db, request)

    async def open_operation(self, creation_id):
        return await native_lifecycle.open_operation(self.db, creation_id)

    async def close_operation(
        self, creation_id, operation_id, physical_settled
    ) -> None:
        await native_lifecycle.close_operation(
            self.db, creation_id, operation_id, physical_settled
        )

    async def original_prepared(self, creation_id, prepared) -> None:
        await native_lifecycle.prepared(self.db, creation_id, prepared)

    async def original_writing(self, creation_id) -> None:
        await native_lifecycle.writing(self.db, creation_id)

    async def original_written(self, creation_id, receipt) -> None:
        await native_lifecycle.written(self.db, creation_id, receipt)

    async def original_verified(self, creation_id, verification) -> None:
        await native_lifecycle.verified(self.db, creation_id, verification)

    async def fail_original(self, creation_id, outcome, error_code, evidence) -> None:
        await native_lifecycle.fail(self.db, creation_id, outcome, error_code, evidence)

    async def reserve(self, request: DatasetUploadReserve) -> DatasetUploadIntent:
        result = await self._call(
            "SELECT aap_assets_v1.reserve_dataset_upload("
            "CAST(:item AS uuid), CAST(:actor AS uuid), CAST(:dataset AS uuid), "
            ":file_name, :content_type)",
            {
                "item": request.item_id,
                "actor": request.actor_id,
                "dataset": request.dataset_id,
                "file_name": request.file_name,
                "content_type": request.content_type,
            },
            DatasetUploadIntent,
        )
        assert isinstance(result, DatasetUploadIntent)
        self._check_scope(
            result,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            item_id=request.item_id,
        )
        if (result.file_name, result.content_type) != (
            request.file_name,
            request.content_type,
        ):
            raise ValueError("Dataset upload reservation does not match its request")
        return result

    async def lock(
        self, *, actor_id: UUID, dataset_id: UUID, item_id: UUID
    ) -> DatasetUploadIntent:
        result = await self._call(
            "SELECT aap_assets_v1.lock_dataset_upload("
            "CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:item AS uuid))",
            {"actor": actor_id, "dataset": dataset_id, "item": item_id},
            DatasetUploadIntent,
        )
        assert isinstance(result, DatasetUploadIntent)
        self._check_scope(
            result,
            actor_id=actor_id,
            dataset_id=dataset_id,
            item_id=item_id,
        )
        return result

    async def find_duplicate(
        self,
        *,
        actor_id: UUID,
        dataset_id: UUID,
        item_id: UUID,
        content_hash: str | None,
    ) -> DatasetUploadDuplicate:
        content_hash = canonical_content_hash(content_hash)
        result = await self._call(
            "SELECT aap_assets_v1.find_dataset_upload_duplicate("
            "CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:item AS uuid), "
            "CAST(:content_hash AS text))",
            {
                "actor": actor_id,
                "dataset": dataset_id,
                "item": item_id,
                "content_hash": content_hash,
            },
            DatasetUploadDuplicate,
        )
        assert isinstance(result, DatasetUploadDuplicate)
        if result.duplicate_of == item_id:
            raise ValueError("Dataset upload duplicate cannot refer to its own item")
        return result

    async def complete(
        self, request: DatasetUploadCompletion
    ) -> DatasetUploadAcceptance:
        result = await self._call(
            "SELECT aap_assets_v1.complete_dataset_upload("
            "CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:item AS uuid), "
            "CAST(:publication AS uuid), CAST(:file_size AS bigint), "
            "CAST(:content_hash AS text), CAST(:width AS integer), "
            "CAST(:height AS integer))",
            {
                "actor": request.actor_id,
                "dataset": request.dataset_id,
                "item": request.item_id,
                "publication": request.publication_id,
                "file_size": request.file_size,
                "content_hash": request.content_hash,
                "width": request.width,
                "height": request.height,
            },
            DatasetUploadAcceptance,
        )
        assert isinstance(result, DatasetUploadAcceptance)
        self._check_scope(
            result,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            item_id=request.item_id,
        )
        if result.publication_id != request.publication_id:
            raise ValueError("Dataset upload completion uses another publication")
        return result

    async def discard(self, request: DatasetUploadDiscard) -> DatasetUploadIntent:
        result = await self._call(
            "SELECT aap_assets_v1.discard_dataset_upload("
            "CAST(:actor AS uuid), CAST(:dataset AS uuid), CAST(:item AS uuid), "
            "CAST(:reason AS text))",
            {
                "actor": request.actor_id,
                "dataset": request.dataset_id,
                "item": request.item_id,
                "reason": request.reason,
            },
            DatasetUploadIntent,
        )
        assert isinstance(result, DatasetUploadIntent)
        self._check_scope(
            result,
            actor_id=request.actor_id,
            dataset_id=request.dataset_id,
            item_id=request.item_id,
        )
        return result

    async def _call(self, statement: str, params: dict, model: type):
        payload = await self.db.scalar(text(statement), params)
        return decode_dataset_upload_reply(payload, model)

    @staticmethod
    def _check_scope(
        result: DatasetUploadIntent,
        *,
        actor_id: UUID,
        dataset_id: UUID,
        item_id: UUID,
    ) -> None:
        if (result.actor_id, result.dataset_id, result.item_id) != (
            actor_id,
            dataset_id,
            item_id,
        ):
            raise ValueError("Dataset upload receipt does not match its request scope")
