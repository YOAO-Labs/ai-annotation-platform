"""Own database, storage, and operation lifetimes for the source service."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
import logging
from collections.abc import Awaitable, Callable
from typing import BinaryIO, TypeVar
from uuid import uuid4

import asyncpg
from pydantic import BaseModel, ValidationError
from yoao_assets_images.contract import ImageFailure
from yoao_assets_images.native_storage_contract import (
    NativePyramidIoReferences,
    NativePyramidPrefixIoReference,
    NativePyramidReadIoRequest,
    NativePyramidRetirementIoRequest,
    NativePyramidRetirementPrefixIoRequest,
    NativePyramidWriteIoRequest,
)

from .config import SourceConfig
from yoao_assets_source.connections_contract import (
    CONNECTION_OPERATIONS,
    CONNECTIONS_SCHEMA,
    ConnectionFailure,
    decode_connection_reply,
    require_connection_scope,
)
from yoao_assets_source.connections_workflow import (
    ConnectionsOptions,
    ConnectionsWorkflow,
)
from yoao_assets_source.contract import (
    SourceDeletionRequest,
    SourceFailure,
    UploadCompleteRequest,
    UploadInitRequest,
    UploadInitResult,
    UploadOutcome,
    decode_source_reply,
)
from .database import SourceDatabase, SourceDataPorts
from yoao_assets_source.deletion_contract import DatasetSourceDeletionReceipt
from yoao_assets_source.ingest_contract import (
    INGEST_SCHEMA,
    IngestCommand,
    IngestFailure,
    IngestReceipt,
    decode_ingest_reply,
)
from yoao_assets_source.import_verification_contract import (
    ImportVerificationReply,
    ImportVerificationRequest,
    ImportVerificationResult,
)
from yoao_assets_source.managed_original_contract import ManagedOriginalMaterial
from .ingest_storage_errors import ManagedStorageError
from .storage import SourceStorage
from .workflow import UploadWorkflow
from .ingest_storage import IngestStorage
from .ingest_workflow import IngestWorkflow
from .image_admission import ImageAdmission
from .native_storage import NativeStorageSigner
from .saved_io import Context, SavedIOService
from .saved_io_import import verify_registered

_Result = TypeVar("_Result", bound=BaseModel)
_SOURCE_SCHEMA = "aap-assets-source/v1"
logger = logging.getLogger("yoao_assets_source_runtime.import_recovery")


class _ExpectedFailure(Exception):
    def __init__(self, body: bytes) -> None:
        self.body = body


class SourceRuntime:
    def __init__(
        self,
        config: SourceConfig,
        database: SourceDatabase | None,
        storage: SourceStorage | None,
        native: NativeStorageSigner | None = None,
        saved_io: SavedIOService | None = None,
    ) -> None:
        self._config = config
        self._database = database
        self._storage = storage
        self._native = native
        self.saved_io = saved_io
        self._recovery_task: asyncio.Task[None] | None = None

    @classmethod
    async def start(cls, config: SourceConfig) -> SourceRuntime:
        database = await SourceDatabase.connect(config)
        saved_io = None
        try:
            native = (
                NativeStorageSigner(config, config.native, database)
                if config.native is not None
                else None
            )
            saved_io = await SavedIOService.start(config)
            runtime = cls(config, database, SourceStorage(config), native, saved_io)
            runtime._recovery_task = asyncio.create_task(
                runtime._recover_import_verifications(),
                name="source-import-verification-recovery",
            )
            return runtime
        except BaseException:
            if saved_io is not None:
                await saved_io.close()
            await database.close()
            raise

    async def _recover_import_verifications(self) -> None:
        cursor = None
        while True:
            try:
                payload = await self._database.import_verification("recover", cursor)
                cursor = payload["cursor"]
                if cursor is None:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Import verification recovery could not advance")
                cursor = None
                await asyncio.sleep(1)

    async def init_upload(self, request: UploadInitRequest) -> bytes:
        async def operation(ports: SourceDataPorts) -> UploadInitResult:
            return await self._workflow(ports).init_upload(request)

        return await self._run(operation, UploadInitResult)

    async def complete_upload(self, request: UploadCompleteRequest) -> bytes:
        async def operation(ports: SourceDataPorts) -> UploadOutcome:
            return await self._workflow(ports).complete_upload(request)

        return await self._run(operation, UploadOutcome)

    async def delete_source(self, request: SourceDeletionRequest) -> bytes:
        async def operation(ports: SourceDataPorts) -> DatasetSourceDeletionReceipt:
            return await self._workflow(ports).delete_source(request)

        return await self._run(operation, DatasetSourceDeletionReceipt)

    async def upload_zip(self, command: IngestCommand, source: BinaryIO) -> bytes:
        async def operation(ports: SourceDataPorts) -> IngestReceipt:
            return await self._ingest_workflow(ports).upload_zip(command, source)

        return await self._run_ingest(operation)

    async def scan(self, command: IngestCommand) -> bytes:
        async def operation(ports: SourceDataPorts) -> IngestReceipt:
            return await self._ingest_workflow(ports).scan(command)

        return await self._run_ingest(operation)

    async def import_verification_status(
        self, request: ImportVerificationRequest
    ) -> dict:
        command = request.model_dump(mode="json")
        payload = await self._database.import_verification("status", command)
        if payload.get("state") == "drained":
            payload = await self._finish_import_verification(
                command, payload["evidence"]["result"]
            )
        return ImportVerificationReply.model_validate(payload).model_dump(mode="json")

    async def _finish_import_verification(self, command: dict, result) -> dict:
        try:
            return await self._database.import_verification("finish", command, result)
        except asyncpg.LockNotAvailableError:
            return {"state": "pending"}

    async def _record_import_read(self, command: dict, result) -> dict:
        payload = await self._database.import_verification("record", command, result)
        if payload.get("state") == "drained":
            return await self._finish_import_verification(command, result)
        return payload

    async def verify_import_original(self, request: ImportVerificationRequest) -> dict:
        command = request.model_dump(mode="json")
        deadline = (
            asyncio.get_running_loop().time()
            + self._config.import_verify_timeout_seconds
        )
        admission = {**command, "admission_id": str(uuid4())}

        async def settle_admission() -> None:
            while True:
                try:
                    recorded = await self._database.import_verification(
                        "record", admission, None
                    )
                    if recorded.get("state") == "drained":
                        await self._finish_import_verification(command, None)
                    return
                except (asyncpg.PostgresConnectionError, OSError, TimeoutError):
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise
                    await asyncio.sleep(min(0.05, remaining))

        uncertain = False
        try:
            while True:
                budget = asyncio.timeout_at(deadline)
                try:
                    async with budget:
                        started = await self._database.import_verification(
                            "begin", admission
                        )
                    break
                except TimeoutError:
                    if (
                        budget.expired()
                        or asyncio.get_running_loop().time() >= deadline
                    ):
                        raise
                    uncertain = True
                except (asyncpg.LockNotAvailableError, asyncpg.ObjectInUseError) as exc:
                    if (
                        isinstance(exc, asyncpg.ObjectInUseError)
                        and exc.message != "import_verification_maintenance_unavailable"
                    ):
                        raise
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("import verification admission deadline")
                await asyncio.sleep(min(0.05, remaining))
            if started.get("state") != "started":
                if uncertain:
                    await settle_admission()
                return ImportVerificationReply.model_validate(started).model_dump(
                    mode="json"
                )
            facts = dict(started["material"])
            image = facts.pop("file_type") == "image"
            expected_md5 = facts.pop("md5")
            material = ManagedOriginalMaterial.model_validate(facts)
            if (
                material.destination is None
                and material.bucket != self._config.datasets_bucket
            ):
                payload = await self._record_import_read(command, None)
                return ImportVerificationReply.model_validate(payload).model_dump(
                    mode="json"
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                payload = await self._record_import_read(command, None)
                return ImportVerificationReply.model_validate(payload).model_dump(
                    mode="json"
                )
            if material.connection_id is None:
                read = self._storage.managed.verify(
                    material,
                    sha256=material.sha256,
                    size_bytes=material.size_bytes,
                    image=image,
                    timeout_seconds=remaining,
                )
            elif self.saved_io is not None:
                read = verify_registered(
                    Context(self.saved_io),
                    material,
                    image=image,
                    timeout_seconds=remaining,
                )
            else:
                raise ManagedStorageError("storage_write_failed")
            task = asyncio.create_task(read)
        except (Exception, asyncio.CancelledError):
            cleanup = asyncio.create_task(settle_admission())
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.done():
                        break
                except Exception:
                    logger.exception("Import verification admission settlement failed")
                    break
            raise
        cancelled = False
        try:
            try:
                verified = await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                verified = await asyncio.shield(task)
        except ManagedStorageError as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            payload = await self._record_import_read(command, None)
        else:
            if verified.md5 != expected_md5:
                payload = await self._record_import_read(command, None)
            else:
                dimensions = verified.dimensions
                receipt = ImportVerificationResult(
                    sha256=verified.sha256,
                    md5=verified.md5,
                    size_bytes=verified.size_bytes,
                    etag=verified.etag,
                    version_id=verified.version_id,
                    observation=verified.observation,
                    physical_settled=True,
                    current_head_matches=True,
                    width=dimensions[0] if dimensions else None,
                    height=dimensions[1] if dimensions else None,
                )
                payload = await self._record_import_read(
                    command, receipt.model_dump(mode="json")
                )
        if cancelled:
            raise asyncio.CancelledError
        return ImportVerificationReply.model_validate(payload).model_dump(mode="json")

    async def manage_connection(self, operation_name: str, request: BaseModel) -> bytes:
        try:
            request_type, result_type = CONNECTION_OPERATIONS[operation_name]
        except KeyError:
            raise ConnectionFailure("invalid_request") from None
        if type(request) is not request_type:
            raise ConnectionFailure("invalid_request")

        async def operation(ports: SourceDataPorts) -> BaseModel:
            workflow = ConnectionsWorkflow(
                native=ports.connections,
                options=ConnectionsOptions(
                    encryption_key=self._config.connector_encryption_key,
                    fallback_allowlist=self._config.connector_host_allowlist,
                    known_hosts_file=self._config.sftp_known_hosts_file,
                    pin_transport=bool(
                        self._config.connections_token or self._config.saved_io
                    ),
                ),
            )
            result = await workflow.invoke(operation_name, request)
            if type(result) is not result_type:
                raise ConnectionFailure("invalid_response")
            return result

        try:
            async with self._database.transaction() as ports:
                try:
                    result = await operation(ports)
                    body = self._encode_connection_success(request, result, result_type)
                except ConnectionFailure as exc:
                    raise _ExpectedFailure(
                        self._encode_connection_failure(exc, result_type)
                    ) from exc
            return body
        except _ExpectedFailure as exc:
            return exc.body

    async def native_write_reference(
        self, request: NativePyramidWriteIoRequest
    ) -> NativePyramidIoReferences:
        return await self._require_native_signer().write_reference(request)

    async def native_verify_reference(
        self, request: NativePyramidWriteIoRequest
    ) -> NativePyramidIoReferences:
        return await self._require_native_signer().verify_reference(request)

    async def native_read_references(
        self, request: NativePyramidReadIoRequest
    ) -> NativePyramidIoReferences:
        return await self._require_native_signer().read_references(request)

    async def native_retirement_references(
        self, request: NativePyramidRetirementIoRequest
    ) -> NativePyramidIoReferences:
        return await self._require_native_signer().retirement_references(request)

    async def native_retirement_prefix_reference(
        self, request: NativePyramidRetirementPrefixIoRequest
    ) -> NativePyramidPrefixIoReference:
        return await self._require_native_signer().retirement_prefix_reference(request)

    def _require_native_signer(self) -> NativeStorageSigner:
        if self._native is None:
            raise ImageFailure("source_unavailable")
        return self._native

    async def ready(self) -> bool:
        if self.saved_io is not None and not await self.saved_io.ready():
            return False
        return await self._database.ready()

    async def close(self) -> None:
        if self._recovery_task is not None:
            self._recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._recovery_task
        try:
            if self.saved_io is not None:
                await self.saved_io.close()
        finally:
            await self._database.close()

    def _workflow(self, ports: SourceDataPorts) -> UploadWorkflow:
        return UploadWorkflow(
            upload_data=ports.uploads,
            legacy_upload_data=ports.legacy_uploads,
            deletion_data=ports.deletions,
            storage=self._storage,
            image_admission=ImageAdmission(ports.images, self._config.image_policy),
            cleanup_bucket=self._config.cleanup_bucket,
        )

    def _ingest_workflow(self, ports: SourceDataPorts) -> IngestWorkflow:
        return IngestWorkflow(
            data=ports.ingest,
            storage=IngestStorage(
                self._storage.client,
                self._storage.datasets_bucket,
                managed=self._storage.managed,
            ),
            image_admission=ImageAdmission(ports.images, self._config.image_policy),
        )

    async def _run(
        self,
        operation: Callable[[SourceDataPorts], Awaitable[_Result]],
        result_model: type[_Result],
    ) -> bytes:
        try:
            async with self._database.transaction() as ports:
                try:
                    result = await operation(ports)
                    body = self._encode_success(result, result_model)
                except SourceFailure as exc:
                    raise _ExpectedFailure(
                        self._encode_failure(exc.code, result_model)
                    ) from exc
            return body
        except _ExpectedFailure as exc:
            return exc.body

    async def _run_ingest(
        self, operation: Callable[[SourceDataPorts], Awaitable[IngestReceipt]]
    ) -> bytes:
        try:
            async with self._database.transaction() as ports:
                try:
                    result = await operation(ports)
                    body = self._encode_ingest_success(result)
                except IngestFailure as exc:
                    raise _ExpectedFailure(self._encode_ingest_failure(exc)) from exc
            return body
        except _ExpectedFailure as exc:
            return exc.body

    def _encode_success(self, result: BaseModel, result_model: type[_Result]) -> bytes:
        payload = {
            "schema": _SOURCE_SCHEMA,
            "ok": True,
            "data": result.model_dump(mode="json"),
        }
        try:
            decoded = decode_source_reply(payload, result_model)
        except SourceFailure:
            raise SourceFailure("invalid_response") from None
        if decoded != result:
            raise SourceFailure("invalid_response")
        return self._serialize(payload)

    def _encode_failure(self, code: str, result_model: type[_Result]) -> bytes:
        payload = {"schema": _SOURCE_SCHEMA, "ok": False, "error": {"code": code}}
        try:
            decode_source_reply(payload, result_model)
        except SourceFailure as exc:
            if exc.code != code:
                raise SourceFailure("invalid_response") from None
        else:
            raise SourceFailure("invalid_response")
        return self._serialize(payload)

    def _serialize(self, payload: dict) -> bytes:
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise SourceFailure("invalid_response") from None
        if len(body) > self._config.max_response_bytes:
            raise SourceFailure("service_unavailable")
        return body

    def _encode_ingest_success(self, result: IngestReceipt) -> bytes:
        payload = {
            "schema": INGEST_SCHEMA,
            "ok": True,
            "data": result.model_dump(mode="json"),
        }
        try:
            decoded = decode_ingest_reply(payload, IngestReceipt)
        except IngestFailure:
            raise IngestFailure("invalid_response") from None
        if decoded != result:
            raise IngestFailure("invalid_response")
        return self._serialize_ingest(payload)

    def _encode_ingest_failure(self, failure: IngestFailure) -> bytes:
        error = {"code": failure.code}
        if failure.detail is not None:
            error["detail"] = failure.detail.model_dump(mode="json")
        payload = {"schema": INGEST_SCHEMA, "ok": False, "error": error}
        try:
            decode_ingest_reply(payload, IngestReceipt)
        except IngestFailure as decoded:
            if decoded.code != failure.code or decoded.detail != failure.detail:
                raise IngestFailure("invalid_response") from None
        else:
            raise IngestFailure("invalid_response")
        return self._serialize_ingest(payload)

    def _serialize_ingest(self, payload: dict) -> bytes:
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise IngestFailure("invalid_response") from None
        if len(body) > self._config.max_response_bytes:
            raise IngestFailure("service_unavailable")
        return body

    def _encode_connection_success(
        self,
        request: BaseModel,
        result: BaseModel,
        result_type: type[BaseModel],
    ) -> bytes:
        payload = {
            "schema": CONNECTIONS_SCHEMA,
            "ok": True,
            "data": result.model_dump(mode="json"),
        }
        try:
            decoded = decode_connection_reply(payload, result_type)
            require_connection_scope(request, decoded)
        except ConnectionFailure:
            raise
        except (TypeError, ValueError, ValidationError):
            raise ConnectionFailure("invalid_response") from None
        if decoded != result:
            raise ConnectionFailure("invalid_response")
        return self._serialize_connection(payload)

    def _encode_connection_failure(
        self, failure: ConnectionFailure, result_type: type[BaseModel]
    ) -> bytes:
        try:
            payload = failure.payload()
            try:
                decode_connection_reply(payload, result_type)
            except ConnectionFailure as decoded:
                if (decoded.code, decoded.message) != (failure.code, failure.message):
                    raise ConnectionFailure("invalid_response") from None
            else:
                raise ConnectionFailure("invalid_response")
        except (TypeError, ValueError):
            payload = ConnectionFailure("invalid_response").payload()
        return self._serialize_connection(payload)

    def _serialize_connection(self, payload: dict) -> bytes:
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ConnectionFailure("invalid_response") from None
        if len(body) > self._config.max_response_bytes:
            raise ConnectionFailure("resource_budget_exceeded")
        return body
