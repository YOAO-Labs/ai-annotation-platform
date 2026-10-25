"""Own database, storage, and operation lifetimes for the source service."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
import inspect
from typing import TYPE_CHECKING, BinaryIO, TypeVar

from pydantic import BaseModel, ValidationError

from .config import GuideOnlyConfig, SourceConfig
from aap_assets_source.connections_contract import (
    CONNECTION_OPERATIONS,
    CONNECTIONS_SCHEMA,
    ConnectionFailure,
    decode_connection_reply,
    require_connection_scope,
)
from aap_assets_source.connections_workflow import (
    ConnectionsOptions,
    ConnectionsWorkflow,
)
from aap_assets_source.contract import (
    SourceDeletionRequest,
    SourceFailure,
    UploadCompleteRequest,
    UploadInitRequest,
    UploadInitResult,
    UploadOutcome,
    decode_source_reply,
)
from .database import SourceDatabase, SourceDataPorts
from aap_assets_source.deletion_contract import DatasetSourceDeletionReceipt
from aap_assets_source.ingest_contract import (
    INGEST_SCHEMA,
    IngestCommand,
    IngestFailure,
    IngestReceipt,
    decode_ingest_reply,
)
from .storage import SourceStorage
from .workflow import UploadWorkflow
from .ingest_storage import IngestStorage
from .ingest_workflow import IngestWorkflow
from .image_admission import ImageAdmission

if TYPE_CHECKING:
    from aap_assets_source.guide_object_contract import GuideObjectRequest

    from .guide_storage import GuideStorage

_Result = TypeVar("_Result", bound=BaseModel)
_SOURCE_SCHEMA = "aap-assets-source/v1"


class _ExpectedFailure(Exception):
    def __init__(self, body: bytes) -> None:
        self.body = body


class SourceRuntime:
    def __init__(
        self,
        config: SourceConfig | GuideOnlyConfig,
        database: SourceDatabase | None,
        storage: SourceStorage | None,
        guide_storage: GuideStorage | None = None,
    ) -> None:
        self._config = config
        self._database = database
        self._storage = storage
        self._guide_storage = guide_storage
        self._guide_in_flight = 0

    @classmethod
    async def start(cls, config: SourceConfig | GuideOnlyConfig) -> SourceRuntime:
        if isinstance(config, GuideOnlyConfig):
            from .guide_storage import GuideStorage

            return cls(config, None, None, GuideStorage(config.guide))

        database = await SourceDatabase.connect(config)
        guide_storage = None
        try:
            if config.guide is not None:
                from .guide_storage import GuideStorage

                guide_storage = GuideStorage(config.guide)
            storage = SourceStorage(config)
            return cls(config, database, storage, guide_storage)
        except Exception:
            try:
                if guide_storage is not None:
                    await guide_storage.close()
            finally:
                await database.close()
            raise

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

    async def guide_object(self, request: GuideObjectRequest) -> bytes:
        from aap_assets_source.guide_object_contract import (
            GuideObjectFailure,
            decode_guide_object_reply,
            decode_guide_object_request,
            guide_object_success_reply,
        )

        request = decode_guide_object_request(request)
        if self._guide_storage is None:
            return self._encode_guide_failure(request, "unavailable")
        if self._guide_in_flight >= self._config.guide_max_in_flight:
            return self._encode_guide_failure(request, "busy")

        self._guide_in_flight += 1
        try:
            try:
                result = await self._run_guide_storage(request)
            except GuideObjectFailure as exc:
                return self._encode_guide_failure(request, exc.code)

            try:
                reply = guide_object_success_reply(request, result)
                decoded = decode_guide_object_reply(
                    request, reply.model_dump(mode="json", by_alias=True)
                )
                if decoded != reply:
                    raise ValueError("Guide object reply changed during validation")
                payload = reply.model_dump(mode="json", by_alias=True)
            except (TypeError, ValueError):
                return self._encode_guide_failure(request, "unavailable")
            try:
                return self._serialize_guide_reply(request, payload)
            except (TypeError, ValueError):
                return self._encode_guide_failure(request, "unavailable")
        finally:
            self._guide_in_flight -= 1

    async def _run_guide_storage(self, request: GuideObjectRequest) -> dict:
        if self._guide_storage is None:
            raise RuntimeError("Guide object storage is unavailable")
        if request.operation == "presign_put":
            return {
                "url": await self._guide_storage.generate_upload_url(
                    request.key, content_type=request.content_type
                ),
                "expires_in": 900,
            }
        if request.operation == "head":
            return {
                "content_length": await self._guide_storage.verify_upload(request.key)
            }
        if request.operation == "delete":
            await self._guide_storage.delete_object(request.key)
            return {"deleted": True}
        if request.operation == "presign_get":
            return {
                "url": await self._guide_storage.generate_download_url(request.key),
                "expires_in": 3600,
            }
        raise RuntimeError("Unsupported Guide object operation")

    def _encode_guide_failure(self, request, code: str) -> bytes:
        from aap_assets_source.guide_object_contract import (
            GuideObjectFailure,
            decode_guide_object_reply,
            guide_object_failure_reply,
        )

        reply = guide_object_failure_reply(request, code)
        payload = reply.model_dump(mode="json", by_alias=True)
        try:
            decode_guide_object_reply(request, payload)
        except GuideObjectFailure as decoded:
            if decoded.code != code:
                raise ValueError("Guide failure changed during validation") from None
        else:
            raise ValueError("Guide failure decoded as success")
        return self._serialize_guide_reply(request, payload)

    def _serialize_guide_reply(self, request, payload: dict) -> bytes:
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ValueError("Guide object reply is not serializable") from None
        if len(body) > self._config.max_response_bytes:
            from aap_assets_source.guide_object_contract import (
                GuideObjectFailure,
                decode_guide_object_reply,
                guide_object_failure_reply,
            )

            unavailable = guide_object_failure_reply(request, "unavailable")
            try:
                decode_guide_object_reply(request, unavailable)
            except GuideObjectFailure as exc:
                if exc.code != "unavailable":
                    raise ValueError(
                        "Guide budget failure changed during validation"
                    ) from None
            else:
                raise ValueError("Guide budget failure decoded as success")
            body = json.dumps(
                unavailable.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        return body

    async def ready(self) -> bool:
        if self._database is None:
            return True
        return await self._database.ready()

    async def close(self) -> None:
        try:
            if self._guide_storage is not None:
                closed = self._guide_storage.close()
                if inspect.isawaitable(closed):
                    await closed
        finally:
            if self._database is not None:
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
            storage=IngestStorage(self._storage.client, self._storage.datasets_bucket),
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
