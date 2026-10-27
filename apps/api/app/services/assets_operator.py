"""Caller-transaction bindings for the trusted Assets operator SQL protocol."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Iterable, Mapping, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from aap_assets_source.managed_original_contract import (
    ManagedOriginalFailureEvidence,
    ManagedOriginalOperation,
    decode_managed_original_reply,
    require_managed_original_ok,
)
from aap_assets_source.operator_contract import (
    OPERATOR_BATCH_SIZE,
    OPERATOR_ERROR_CODES,
    OPERATOR_NATIVE_ARGUMENTS,
    OPERATOR_NATIVE_OPERATIONS,
    OPERATOR_NATIVE_SIGNATURES,
    OPERATOR_PAGE_BYTES,
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


@event.listens_for(Session, "after_soft_rollback")
def _forget_verified_receipts_on_rollback(session, _previous_transaction):
    """A rolled-back savepoint released its writes, so its proofs no longer hold."""
    session.info.pop("aap_verified_receipts", None)


@event.listens_for(Session, "after_rollback")
def _forget_verified_receipts_on_outer_rollback(session):
    session.info.pop("aap_verified_receipts", None)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, allow_nan=False)


# The owner emits managed-original codes; the operator contract has its own frozen
# vocabulary. Map one to the other instead of parsing or widening either shape.
_MANAGED_ORIGINAL_FAILURES = {
    "creation_missing": "operation_conflict",
    "creation_scope_mismatch": "source_changed",
    "creation_configuration_changed": "storage_unavailable",
    "creation_target_unavailable": "storage_unavailable",
    "creation_key_conflict": "operation_conflict",
    "creation_state_conflict": "operation_conflict",
    "creation_evidence_mismatch": "source_changed",
    "creation_terminal": "operation_conflict",
    "creation_budget_exceeded": "invalid_request",
    "creation_byte_budget_exceeded": "invalid_request",
    "operation_unsettled": "storage_unavailable",
}


def _operator_failure(code: Any) -> OperatorFailure:
    if not isinstance(code, str):
        return OperatorFailure("invalid_response")
    mapped = _MANAGED_ORIGINAL_FAILURES.get(code, code)
    if mapped not in OPERATOR_ERROR_CODES:
        mapped = "invalid_response"
    return OperatorFailure(mapped)


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


class _SharedClient:
    def __init__(self, client):
        self._client = client

    def __enter__(self):
        return self._client

    def __exit__(self, *exc):
        return False


class AssetsOperatorGateway:
    """Keep operator registration on published SQL in the caller's transaction."""

    def __init__(self, db: AsyncSession):
        self.db = db
        # Verified receipts proved by this gateway inside one caller transaction.
        self._receipt_transaction = None
        self._verified_receipts: dict[UUID, tuple[str, int]] = {}
        self._catalog_ready = False

    async def _ensure_owner_catalog(self) -> None:
        """Seed committed storage catalog facts before any native allocation.

        A fresh database has no legacy location/binding rows until an owner
        bootstrap runs; allocations then fail with target-unavailable. Reuse the
        existing owner bootstrap so standalone imports initialize it too.
        """
        if self._catalog_ready:
            return
        from app.services import managed_original_creation as originals

        await originals.bootstrap(self.db)
        self._catalog_ready = True

    def _active_caller_transaction(self):
        """Return the transaction that owns this caller's row locks.

        When the session is bound to a connection (the request/test UOW), that
        connection's transaction is stable across inner savepoint commits, so a
        replay in the same held UOW finds the receipts proved earlier. A plain
        engine-bound session falls back to its own transaction.
        """
        bind = self.db.get_bind()
        getter = getattr(bind, "get_transaction", None)
        if getter is not None:
            transaction = getter()
            if transaction is not None:
                return transaction
        transaction = self.db.get_nested_transaction()
        if transaction is None:
            transaction = self.db.get_transaction()
        return transaction

    def _sync_receipt_transaction(self):
        """Share receipts per caller transaction, keyed by object identity.

        The state lives on the session so a replay builds a new gateway over the
        same held transaction and still reuses the proved receipt. Holding the
        transaction object makes commit/rollback invalidate the cache.
        """
        transaction = self._active_caller_transaction()
        info = self.db.info
        if transaction is None:
            info.pop("aap_verified_receipts", None)
            self._receipt_transaction = None
            self._verified_receipts = {}
            return None
        state = info.get("aap_verified_receipts")
        if not isinstance(state, dict) or state.get("transaction") is not transaction:
            state = {"transaction": transaction, "receipts": {}}
            info["aap_verified_receipts"] = state
        self._receipt_transaction = transaction
        self._verified_receipts = state["receipts"]
        return transaction

    def _same_uow_receipt(self, creation_id: UUID) -> tuple[str, int] | None:
        if self._sync_receipt_transaction() is None:
            return None
        return self._verified_receipts.get(creation_id)

    def _remember_receipt(
        self, creation_id: UUID, sha256: str, size_bytes: int
    ) -> None:
        if self._sync_receipt_transaction() is None:
            return
        self._verified_receipts[creation_id] = (sha256, size_bytes)

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
            if (
                isinstance(decoded, OperatorItemsPage)
                and len(_json(decoded.items).encode("utf-8")) > OPERATOR_PAGE_BYTES
            ):
                raise ValueError("oversized operator page records")
            return decoded
        except OperatorFailure as exc:
            _restore_failure(exc)
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            ValidationError,
            RecursionError,
        ):
            raise OperatorFailure("invalid_response") from None

    async def _managed_original(self, function, parameters, result_type):
        try:
            payload = await self.db.scalar(
                text(
                    f"SELECT {function}("
                    + ", ".join(
                        f"CAST(:{name} AS {'uuid' if name == 'creation_id' else 'jsonb'})"
                        for name in parameters
                    )
                    + ")"
                ),
                parameters,
            )
        except SQLAlchemyError:
            raise OperatorFailure("storage_unavailable") from None
        try:
            if result_type is None:
                require_managed_original_ok(payload)
                return None
            return decode_managed_original_reply(payload, result_type)
        except (TypeError, ValueError):
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
        self,
        dataset_id: UUID,
        entries: Iterable[OperatorSourcePatch],
        *,
        observed_items: Mapping[UUID, OperatorItem] | None = None,
    ) -> int:
        filled = 0
        for batch in _batches(entries):
            request = PatchOperatorItemsRequest(
                operation_id=uuid4(), dataset_id=dataset_id, entries=batch
            )
            if observed_items is not None:
                for entry in batch:
                    before = observed_items.get(entry.id)
                    if (
                        before is None
                        or before.dataset_id != dataset_id
                        or before.source_snapshot != entry.source_snapshot
                    ):
                        raise OperatorFailure("invalid_request")
            saved = await self.patch_operator_items(request)
            if observed_items is not None:
                expected_filled = 0
                for entry, item in zip(batch, saved.items, strict=True):
                    before = observed_items[entry.id]
                    fields = (
                        "id",
                        "dataset_id",
                        "file_name",
                        "file_path",
                        "file_type",
                        "scene_id",
                        "frame_index",
                    )
                    dimensions = (before.width, before.height)
                    if entry.dimensions is not None and not (
                        before.width and before.height
                    ):
                        dimensions = (entry.dimensions.width, entry.dimensions.height)
                        expected_filled += 1
                    metadata = dict(before.metadata)
                    if entry.nuscenes_export is not None:
                        metadata["nuscenes_export"] = entry.nuscenes_export
                    if (
                        any(
                            getattr(item, name) != getattr(before, name)
                            for name in fields
                        )
                        or (item.width, item.height) != dimensions
                        or item.metadata != metadata
                        or item.file_size
                        != (
                            before.file_size
                            if entry.file_size is None
                            else entry.file_size
                        )
                        or item.content_hash
                        != (
                            before.content_hash
                            if entry.content_hash is None
                            else entry.content_hash
                        )
                    ):
                        raise OperatorFailure("invalid_response")
                if saved.dimensions_backfilled != expected_filled:
                    raise OperatorFailure("invalid_response")
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

    async def _managed_original_data(
        self, function: str, parameters: dict, types: dict[str, str]
    ):
        import json

        placeholders = ", ".join(
            f"CAST(:{name} AS {types[name]})" for name in parameters
        )
        bound = {
            name: (_json(value) if types[name] == "jsonb" else value)
            for name, value in parameters.items()
        }
        try:
            raw = await self.db.scalar(
                text(f"SELECT {function}({placeholders})"), bound
            )
        except SQLAlchemyError:
            raise OperatorFailure("storage_unavailable") from None
        try:
            body = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
            if (
                not isinstance(body, dict)
                or body.get("schema") != "aap-managed-original/v1"
            ):
                raise ValueError("unexpected managed original contract")
            if body.get("ok") is not True:
                error = body.get("error") or {}
                raise _operator_failure(error.get("code", "invalid_response"))
            return body.get("data")
        except OperatorFailure:
            raise
        except (TypeError, ValueError, KeyError):
            raise OperatorFailure("invalid_response") from None

    async def _native_data(
        self, function: str, parameters: dict, types: dict[str, str]
    ):
        """Execute a native capability in its own committed transaction."""
        import json

        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.services import storage_access

        engine = storage_access.engine(self.db)
        placeholders = ", ".join(
            f"CAST(:{name} AS {types[name]})" for name in parameters
        )
        bound = {
            name: (_json(value) if types[name] == "jsonb" else value)
            for name, value in parameters.items()
        }
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                raw = await session.scalar(
                    text(f"SELECT {function}({placeholders})"), bound
                )
                await session.commit()
        except SQLAlchemyError:
            raise OperatorFailure("storage_unavailable") from None
        try:
            body = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
            if (
                not isinstance(body, dict)
                or body.get("schema") != "aap-managed-original/v1"
            ):
                raise ValueError("unexpected managed original contract")
            if body.get("ok") is not True:
                error = body.get("error") or {}
                raise _operator_failure(error.get("code", "invalid_response"))
            return body.get("data")
        except OperatorFailure:
            raise
        except (TypeError, ValueError, KeyError):
            raise OperatorFailure("invalid_response") from None

    async def _native_call(
        self, function: str, parameters: dict, types: dict[str, str]
    ):
        await self._native_data(function, parameters, types)

    async def auxiliary_original_id(
        self,
        *,
        dataset_id: UUID,
        item_id: UUID | None = None,
        scene_id: UUID | None = None,
    ) -> UUID | None:
        data = await self._managed_original_data(
            "aap_assets_v1.operator_auxiliary_pointer",
            {
                "dataset_id": dataset_id,
                "item_id": item_id,
                "scene_id": scene_id,
            },
            {"dataset_id": "uuid", "item_id": "uuid", "scene_id": "uuid"},
        )
        if not isinstance(data, dict):
            raise OperatorFailure("invalid_response")
        value = data.get("item" if item_id is not None else "scene")
        return UUID(value) if value else None

    async def item_original_source(self, dataset_id: UUID, item_id: UUID):
        """Return verified canonical facts; absence is explicit, never a caught error."""
        from types import SimpleNamespace

        data = await self._managed_original_data(
            "aap_assets_v1.operator_item_original_material",
            {"dataset_id": dataset_id, "item_id": item_id},
            {"dataset_id": "uuid", "item_id": "uuid"},
        )
        if not isinstance(data, dict) or data.get("found") is not True:
            return None
        source = data.get("source")
        if source is None:
            return None
        creation_id = UUID(source["creation_id"])
        remembered = self._same_uow_receipt(creation_id)
        if remembered is None:
            operation = await self._open_operation(creation_id)
            if operation.mode != "read":
                await self._abandon_read_operation(creation_id, operation)
                raise OperatorFailure("source_changed")
            await self._verify_material(
                source, source["sha256"], source["size_bytes"], operation
            )
            self._remember_receipt(creation_id, source["sha256"], source["size_bytes"])
        elif remembered != (source["sha256"], source["size_bytes"]):
            raise OperatorFailure("source_changed")
        return SimpleNamespace(
            creation_id=UUID(source["creation_id"]),
            sha256=source["sha256"],
            size_bytes=source["size_bytes"],
            object_key=source["object_key"],
            bucket=source["bucket"],
            location_id=UUID(source["location_id"]),
            etag=source["etag"],
            version_id=source.get("version_id"),
            storage_ref=source.get("storage_ref"),
            endpoint_url=source.get("endpoint_url"),
            destination=source.get("destination"),
        )

    def _material_client(self, material):
        import base64

        destination = material.get("destination")
        if not destination:
            from app.services.dataset_assets import LEGACY_LOCATION_ID
            from app.services.storage import storage_service

            from urllib.parse import urlsplit

            from app.config import settings

            expected_endpoint = (
                f"{'https' if settings.minio_use_ssl else 'http'}://"
                f"{settings.minio_endpoint}"
            ).rstrip("/")
            actual_endpoint = str(material.get("endpoint_url") or "").rstrip("/")
            if (
                str(material.get("location_id")) != str(LEGACY_LOCATION_ID)
                or material.get("bucket") != storage_service.datasets_bucket
                or urlsplit(actual_endpoint).netloc
                != urlsplit(expected_endpoint).netloc
            ):
                raise OperatorFailure("creation_target_unavailable")
            return _SharedClient(storage_service.client)
        from urllib.parse import urlsplit

        from app.services.source_readers.s3 import S3Access

        config = destination["config"]
        endpoint = config["endpoint"]
        if "://" not in endpoint:
            endpoint = ("https://" if config.get("use_ssl") else "http://") + endpoint
        access = S3Access(
            dict(config),
            base64.b64decode(destination["secret_enc"]),
            (urlsplit(endpoint).hostname or "",),
        )
        return access.client(read_timeout=60)

    async def _material_attr(self, material, name):
        return material[name] if isinstance(material, dict) else getattr(material, name)

    async def _write_material(
        self, material, operation, data: bytes, content_type: str
    ):
        import io
        from types import SimpleNamespace

        from app.services import managed_original_writer as writer

        bucket = await self._material_attr(material, "bucket")
        key = await self._material_attr(material, "object_key")
        creation_id = str(await self._material_attr(material, "creation_id"))
        if operation.mode != "write":
            raise OperatorFailure("creation_state_conflict")
        prepared = receipt = verified = None
        head_matches: bool | None = None
        completion_started = False
        client_context = self._material_client(material)
        client = None
        failure = None
        unknown = False
        try:
            client = client_context.__enter__()
            prepared = writer.prepare_stream(
                client,
                bucket,
                key,
                io.BytesIO(data),
                content_type=content_type,
                max_bytes=len(data),
            )
            await self._native_call(
                "aap_assets_v1.record_operator_original_prepared",
                {
                    "creation_id": creation_id,
                    "prepared": {
                        "sha256": prepared.sha256,
                        "md5": prepared.md5,
                        "size_bytes": prepared.size_bytes,
                        "upload_id": prepared.upload_id,
                        "parts": list(prepared.parts),
                        "bucket": bucket,
                        "object_key": key,
                        "content_type": content_type,
                    },
                },
                {"creation_id": "uuid", "prepared": "jsonb"},
            )
            await self._native_call(
                "aap_assets_v1.mark_operator_original_writing",
                {"creation_id": creation_id},
                {"creation_id": "uuid"},
            )
            completion_started = True
            receipt = writer.complete(
                client, bucket, key, prepared, content_type=content_type
            )
            await self._native_call(
                "aap_assets_v1.record_operator_original_written",
                {
                    "creation_id": creation_id,
                    "receipt": {
                        "etag": receipt.etag,
                        "version_id": receipt.version_id,
                        "request_id": receipt.request_id,
                        "response": receipt.response,
                    },
                },
                {"creation_id": "uuid", "receipt": "jsonb"},
            )
            verified = writer.verify(
                client,
                bucket,
                key,
                receipt,
                sha256=prepared.sha256,
                size_bytes=prepared.size_bytes,
                max_bytes=len(data),
            )
            if receipt.version_id not in {None, "", "null"}:
                # Attribution needs an observed current HEAD equal to the pinned
                # version, probed while this operation still owns the client.
                from app.services import storage_usage_accounting as accounting

                head_matches = accounting.current_matches(
                    client,
                    bucket,
                    key,
                    receipt.version_id,
                    prepared.size_bytes,
                )
        except BaseException as exc:
            failure = exc
            if not completion_started and prepared is not None:
                try:
                    writer.abort_prepared(client, bucket, key, prepared)
                except BaseException:
                    unknown = True
        finally:
            if client is not None:
                try:
                    client_context.__exit__(None, None, None)
                except BaseException as closing:
                    failure = failure or closing
                    unknown = True
        if failure is None:
            verification = {
                "sha256": verified.sha256,
                "md5": prepared.md5,
                "size_bytes": verified.size_bytes,
                "etag": receipt.etag,
                "version_id": receipt.version_id,
                "observation": verified.observation,
                "physical_settled": True,
            }
            if head_matches is not None:
                verification["current_head_matches"] = head_matches
            try:
                await self._native_call(
                    "aap_assets_v1.record_operator_original_verified",
                    {
                        "creation_id": creation_id,
                        "verification": verification,
                    },
                    {"creation_id": "uuid", "verification": "jsonb"},
                )
            except BaseException as exc:
                # Every handle is drained; keep the native phase failure.
                failure = exc
        if failure is not None:
            completion_unknown = (
                receipt is None and getattr(failure, "outcome", None) == "unknown"
            )
            settled = not (
                unknown
                or completion_unknown
                or isinstance(failure, asyncio.CancelledError)
            )
            if not settled:
                physical_outcome = "unknown"
            elif receipt is not None:
                # The write receipt is known and every handle drained: the bytes
                # exist even though the later ledger transition did not answer.
                physical_outcome = "written"
            else:
                physical_outcome = "not_written"
            evidence = ManagedOriginalFailureEvidence.build(
                physical_settled=settled,
                physical_outcome=physical_outcome,
                stage="verify" if receipt is not None else "write",
                receipt=(
                    {
                        "etag": receipt.etag,
                        "version_id": receipt.version_id,
                        "request_id": receipt.request_id,
                        "response": receipt.response,
                    }
                    if receipt is not None
                    else None
                ),
                observation=(verified.observation if verified is not None else None),
                sha256=(prepared.sha256 if prepared is not None else None),
                md5=(prepared.md5 if prepared is not None else None),
                size_bytes=(prepared.size_bytes if prepared is not None else None),
                current_head_matches=head_matches,
            )
            outcome = (
                "unknown"
                if not settled or getattr(failure, "outcome", None) == "unknown"
                else "conflict"
                if getattr(failure, "outcome", None) == "conflict"
                else "failed"
            )
            code = (
                "creation_close_unknown"
                if unknown
                else getattr(failure, "code", None)
                or getattr(failure, "outcome", None)
                or "creation_io_failed"
            )
            code = re.sub(r"[^a-z0-9_]+", "_", str(code).lower()).strip("_")
            if not code or not code[0].isalpha():
                code = "creation_io_failed"
            code = code[:80]
            try:
                await self._native_call(
                    "aap_assets_v1.fail_operator_original",
                    {
                        "creation_id": creation_id,
                        "outcome": outcome,
                        "error_code": code,
                        "evidence": evidence,
                    },
                    {
                        "creation_id": "uuid",
                        "outcome": "text",
                        "error_code": "text",
                        "evidence": "jsonb",
                    },
                )
            except BaseException:
                pass
            raise failure
        return SimpleNamespace(
            creation_id=UUID(creation_id),
            sha256=prepared.sha256,
            size_bytes=prepared.size_bytes,
            md5=prepared.md5,
        )

    async def _open_operation(self, creation_id):
        data = await self._native_data(
            "aap_assets_v1.open_operator_storage_operation",
            {"creation_id": creation_id},
            {"creation_id": "uuid"},
        )
        try:
            return ManagedOriginalOperation.model_validate(data)
        except (TypeError, ValueError):
            raise OperatorFailure("invalid_response") from None

    async def _verify_material(
        self, material, sha256: str, size_bytes: int, operation
    ) -> None:
        from app.services import managed_original_writer as writer

        bucket = await self._material_attr(material, "bucket")
        key = await self._material_attr(material, "object_key")
        creation_id = str(await self._material_attr(material, "creation_id"))
        etag = await self._material_attr(material, "etag")
        version_id = await self._material_attr(material, "version_id")
        receipt = writer.WriteReceipt(etag, version_id, None, {})
        try:
            with self._material_client(material) as client:
                verified = writer.verify(
                    client,
                    bucket,
                    key,
                    receipt,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    max_bytes=size_bytes,
                )
        except BaseException:
            await self._abandon_read_operation(creation_id, operation)
            raise
        if verified.sha256 != sha256 or verified.size_bytes != size_bytes:
            await self._abandon_read_operation(creation_id, operation)
            raise ValueError("existing scene source drift for canonical media")
        await self._native_call(
            "aap_assets_v1.close_operator_storage_operation",
            {
                "creation_id": creation_id,
                "operation_id": operation.operation_id,
                "physical_settled": True,
            },
            {
                "creation_id": "uuid",
                "operation_id": "uuid",
                "physical_settled": "boolean",
            },
        )

    async def _abandon_read_operation(self, creation_id, operation) -> None:
        """Keep the read permit when closure cannot be proven."""
        try:
            await self._native_call(
                "aap_assets_v1.close_operator_storage_operation",
                {
                    "creation_id": creation_id,
                    "operation_id": operation.operation_id,
                    "physical_settled": True,
                },
                {
                    "creation_id": "uuid",
                    "operation_id": "uuid",
                    "physical_settled": "boolean",
                },
            )
        except BaseException:
            pass

    async def dataset_state(self, dataset_id: UUID) -> dict:
        data = await self._managed_original_data(
            "aap_assets_v1.operator_dataset_state",
            {"dataset_id": dataset_id},
            {"dataset_id": "uuid"},
        )
        if not isinstance(data, dict):
            raise OperatorFailure("invalid_response")
        return data

    async def scene_state(self, dataset_id: UUID, scene_id: UUID) -> dict:
        data = await self._managed_original_data(
            "aap_assets_v1.operator_scene_state",
            {"dataset_id": dataset_id, "scene_id": scene_id},
            {"dataset_id": "uuid", "scene_id": "uuid"},
        )
        if not isinstance(data, dict):
            raise OperatorFailure("invalid_response")
        return data

    async def create_item_original(
        self,
        *,
        dataset_id: UUID,
        item_id: UUID,
        publication_id: UUID,
        operation_id: UUID,
        logical_path: str,
        file_type: str,
        content_type: str,
        data: bytes,
        requested_by: UUID | None = None,
    ):
        """Allocate through the owner, write pinned bytes, then record proof."""
        from types import SimpleNamespace

        await self._ensure_owner_catalog()
        material = await self._native_data(
            "aap_assets_v1.begin_operator_auxiliary",
            {
                "request": {
                    "dataset_id": str(dataset_id),
                    "kind": "item_media",
                    "item_id": str(item_id),
                    "publication_id": str(publication_id),
                    "logical_path": logical_path,
                    "file_type": file_type,
                    "content_type": content_type,
                    "max_bytes": len(data),
                    "requested_by": (str(requested_by) if requested_by else None),
                }
            },
            {"request": "jsonb"},
        )
        creation_id = UUID(material["creation_id"])
        import hashlib

        digest = hashlib.sha256(data).hexdigest()
        remembered = self._same_uow_receipt(creation_id)
        if remembered is not None:
            if remembered != (digest, len(data)):
                raise ValueError("verified operator original does not match source")
            created = SimpleNamespace(
                creation_id=creation_id,
                sha256=digest,
                size_bytes=len(data),
                md5=None,
            )
            return SimpleNamespace(
                object_key=material["object_key"],
                creation_id=creation_id,
            ), created
        operation = await self._open_operation(creation_id)
        if operation.mode == "read":
            if material.get("sha256") != digest or material.get("size_bytes") != len(
                data
            ):
                await self._abandon_read_operation(creation_id, operation)
                raise ValueError("verified operator original does not match source")
            await self._verify_material(material, digest, len(data), operation)
            self._remember_receipt(creation_id, digest, len(data))
            created = SimpleNamespace(
                creation_id=creation_id,
                sha256=digest,
                size_bytes=len(data),
                md5=None,
            )
            return SimpleNamespace(
                object_key=material["object_key"],
                creation_id=creation_id,
            ), created
        created = await self._write_material(material, operation, data, content_type)
        self._remember_receipt(created.creation_id, created.sha256, created.size_bytes)
        return SimpleNamespace(
            object_key=material["object_key"],
            creation_id=creation_id,
        ), created

    async def publish_item_original(
        self,
        *,
        dataset_id: UUID,
        item_id: UUID,
        creation_id: UUID,
        expected_file_path: str,
        expected_file_size: int | None,
        expected_content_hash: str | None,
    ) -> None:
        """CAS-replace an existing item locator only with verified exact content."""
        await self._managed_original(
            "aap_assets_v1.publish_operator_item_original",
            {
                "request": _json(
                    {
                        "dataset_id": str(dataset_id),
                        "item_id": str(item_id),
                        "creation_id": str(creation_id),
                        "expected_file_path": expected_file_path,
                        "expected_file_size": expected_file_size,
                        "expected_content_hash": expected_content_hash,
                    }
                )
            },
            None,
        )
        self.db.expire_all()

    async def ensure_auxiliary_original(
        self,
        *,
        dataset_id: UUID,
        kind: str,
        source_contract: dict,
        logical_path: str,
        data: bytes,
        content_type: str,
        requested_by: UUID | None = None,
        existing_id: UUID | None = None,
    ):
        """Allocate or reuse a verified raw-lidar/map auxiliary original."""
        import hashlib

        from types import SimpleNamespace

        await self._ensure_owner_catalog()

        sha256 = hashlib.sha256(data).hexdigest()
        size = len(data)
        requested = await self._managed_original_data(
            "aap_assets_v1.operator_auxiliary_owner",
            {
                "dataset_id": dataset_id,
                "kind": kind,
                "source_contract": source_contract,
                "existing_id": existing_id,
            },
            {
                "dataset_id": "uuid",
                "kind": "text",
                "source_contract": "jsonb",
                "existing_id": "uuid",
            },
        )
        if requested.get("found") is True:
            if (
                requested.get("kind") != kind
                or requested.get("sha256") != sha256
                or requested.get("size_bytes") != size
            ):
                raise ValueError("existing scene source drift for auxiliary original")
            material = await self._managed_original_data(
                "aap_assets_v1.operator_auxiliary_material",
                {"dataset_id": dataset_id, "owner_id": UUID(requested["id"])},
                {"dataset_id": "uuid", "owner_id": "uuid"},
            )
            creation_id = UUID(material["creation_id"])
            remembered = self._same_uow_receipt(creation_id)
            if remembered is not None:
                # This caller transaction already proved the receipt for this
                # creation; reuse it without a second read or permit.
                if remembered != (sha256, size):
                    raise ValueError(
                        "existing scene source drift for auxiliary original"
                    )
                return SimpleNamespace(**requested)
            operation = await self._open_operation(creation_id)
            if operation.mode != "read":
                await self._abandon_read_operation(creation_id, operation)
                raise ValueError("existing auxiliary original is not verified")
            await self._verify_material(material, sha256, size, operation)
            self._remember_receipt(creation_id, sha256, size)
            return SimpleNamespace(**requested)

        material = await self._native_data(
            "aap_assets_v1.begin_operator_auxiliary",
            {
                "request": {
                    "dataset_id": str(dataset_id),
                    "kind": kind,
                    "source_contract": source_contract,
                    "max_bytes": size,
                    "content_type": content_type,
                    "requested_by": str(requested_by) if requested_by else None,
                }
            },
            {"request": "jsonb"},
        )
        creation_id = UUID(material["creation_id"])
        operation = await self._open_operation(creation_id)
        if operation.mode == "read":
            if material.get("sha256") != sha256 or material.get("size_bytes") != size:
                await self._abandon_read_operation(creation_id, operation)
                raise ValueError("verified auxiliary original does not match source")
            await self._verify_material(material, sha256, size, operation)
            self._remember_receipt(creation_id, sha256, size)
            created = SimpleNamespace(
                creation_id=creation_id,
                sha256=sha256,
                size_bytes=size,
            )
        else:
            created = await self._write_material(
                material, operation, data, content_type
            )
        if created.sha256 != sha256 or created.size_bytes != size:
            raise ValueError("nuScenes auxiliary source changed during creation")
        self._remember_receipt(created.creation_id, created.sha256, created.size_bytes)
        # Business publication belongs to the caller's own transaction so its
        # dataset scope and the caller's bindings commit or roll back together.
        owner = await self._managed_original_data(
            "aap_assets_v1.publish_operator_auxiliary",
            {"creation_id": creation_id},
            {"creation_id": "uuid"},
        )
        self.db.expire_all()
        return SimpleNamespace(**owner)

    async def bind_auxiliary_item(self, item_id: UUID, owner) -> None:
        dataset_id = (
            owner["dataset_id"] if isinstance(owner, dict) else owner.dataset_id
        )
        owner_id = owner["id"] if isinstance(owner, dict) else owner.id
        await self._managed_original_data(
            "aap_assets_v1.bind_operator_auxiliary_item",
            {"dataset_id": dataset_id, "item_id": item_id, "owner_id": owner_id},
            {"dataset_id": "uuid", "item_id": "uuid", "owner_id": "uuid"},
        )

    async def bind_auxiliary_scene(self, scene_id: UUID, owner) -> None:
        dataset_id = (
            owner["dataset_id"] if isinstance(owner, dict) else owner.dataset_id
        )
        owner_id = owner["id"] if isinstance(owner, dict) else owner.id
        await self._managed_original_data(
            "aap_assets_v1.bind_operator_auxiliary_scene",
            {"dataset_id": dataset_id, "scene_id": scene_id, "owner_id": owner_id},
            {"dataset_id": "uuid", "scene_id": "uuid", "owner_id": "uuid"},
        )
