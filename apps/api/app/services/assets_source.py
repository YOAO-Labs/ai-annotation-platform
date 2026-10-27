"""Bind Dataset source operations to the local workflow or independent owner."""

from __future__ import annotations

import asyncio
import json
from typing import BinaryIO, NoReturn, TypeVar
from urllib.parse import urlsplit

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.contract import (
    SourceDeletionRequest,
    SourceFailure,
    UploadCompleteRequest,
    UploadInitRequest,
    UploadInitResult,
    UploadOutcome,
    decode_source_reply,
)
from aap_assets_source.deletion_contract import DatasetSourceDeletionReceipt
from aap_assets_source.legacy_upload_contract import (
    LegacyUploadCompletion,
    LegacyUploadLockRequest,
    LegacyUploadState,
    decode_legacy_upload_reply,
)
from aap_assets_source.ingest_contract import (
    IngestCommand,
    IngestFailure,
    IngestReceipt,
    decode_ingest_reply,
    require_ingest_scope,
)
from aap_assets_source.upload_contract import DatasetUploadFailure
from aap_assets_source_runtime.image_admission import ImageAdmission
from app.config import settings
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.project import Project
from app.db.models.task_batch import TaskBatch
from app.services.dataset_source_deletion_data import DatasetSourceDeletionData
from app.services.dataset_upload_data import DatasetUploadData
from app.services.assets_images import image_gateway, image_policy
from app.services.storage import StorageService, storage_service

_RESULT = TypeVar("_RESULT", bound=BaseModel)
_MAX_REQUEST_BYTES = 1 * 1024 * 1024
MAX_SOURCE_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_RESPONSE_BYTES = MAX_SOURCE_RESPONSE_BYTES
_TRANSIENT_SOURCE_CODES = {"source_busy", "account_busy", "dataset_busy"}
_RETAINED_SOURCE_CODES = {
    "source_in_use",
    "active_learning_reference",
    "creation_in_progress",
}
_MISSING_SOURCE_CODES = {"dataset_missing", "item_missing", "upload_missing"}


class AssetsSourceGatewayError(HTTPException):
    """A stable public HTTP error that never exposes owner internals."""

    def __init__(
        self,
        code: str,
        message: str | dict,
        status_code: int,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        super().__init__(status_code=status_code, detail=message, headers=headers)


def _peer_url() -> str | None:
    value = settings.assets_source_url
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    token = settings.assets_source_token
    if (
        not valid
        or len(token) < 32
        or not token.isascii()
        or any(character.isspace() for character in token)
        or any(ord(character) < 32 or ord(character) == 127 for character in token)
    ):
        raise AssetsSourceGatewayError(
            "service_unavailable", "Assets source service is unavailable", 503
        )
    return value.rstrip("/")


def _encode_request(request: BaseModel) -> bytes:
    body = request.model_dump_json(exclude_unset=True).encode("utf-8")
    if len(body) > _MAX_REQUEST_BYTES:
        raise AssetsSourceGatewayError(
            "invalid_request", "Assets source request is too large", 422
        )
    return body


async def _read_response(response: httpx.Response) -> bytes:
    length = response.headers.get("content-length", "")
    if length.isdecimal() and int(length) > _MAX_RESPONSE_BYTES:
        raise AssetsSourceGatewayError(
            "invalid_response", "Assets source returned an invalid response", 502
        )
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise AssetsSourceGatewayError(
                "invalid_response", "Assets source returned an invalid response", 502
            )
    return bytes(body)


def _raise_failure(exc: SourceFailure, *, operation: str) -> NoReturn:
    code = exc.code
    if code == "account_not_active":
        raise AssetsSourceGatewayError(
            code,
            "账号已停用或不存在",
            401,
            {"WWW-Authenticate": "Bearer"},
        ) from None
    if code in _MISSING_SOURCE_CODES:
        message = "Dataset not found" if code == "dataset_missing" else "Item not found"
        raise AssetsSourceGatewayError(code, message, 404) from None
    if code == "invalid_request":
        message = (
            "上传参数无效"
            if operation.startswith("upload")
            else "Invalid source request"
        )
        raise AssetsSourceGatewayError(code, message, 422) from None
    if code == "upload_scope_mismatch":
        raise AssetsSourceGatewayError(
            code, "上传操作不属于当前账号或素材集", 403
        ) from None
    if code == "catalog_manager_required" or (
        code == "invalid_account_role" and operation == "delete"
    ):
        raise AssetsSourceGatewayError(code, "仅素材库管理员可删除素材", 403) from None
    if code == "invalid_account_role":
        raise AssetsSourceGatewayError(code, "账号角色无效，访问被拒绝", 403) from None
    if code in _RETAINED_SOURCE_CODES:
        raise AssetsSourceGatewayError(
            code,
            {
                "code": code,
                "message": (
                    "关联记录仍引用此来源，请先在对应项目或功能中解除引用；未删除素材或存储对象"
                    if code == "source_in_use"
                    else "关联记录仍引用此来源，未删除素材或存储对象"
                ),
            },
            409,
        ) from None
    if code in _TRANSIENT_SOURCE_CODES or code in {
        "operation_conflict",
        "upload_not_pending",
        "source_changed",
    }:
        raise AssetsSourceGatewayError(code, {"reason": code}, 409) from None
    if code == "service_unavailable" or code == "storage_unavailable":
        raise AssetsSourceGatewayError(
            code, "Assets source service is unavailable", 503
        ) from None
    raise AssetsSourceGatewayError(
        "invalid_response", "Assets source returned an invalid response", 502
    ) from None


def _raise_ingest_failure(exc: IngestFailure, *, kind: str) -> NoReturn:
    code = exc.code
    if code in {"archive_invalid", "archive_unsafe", "resource_budget_exceeded"}:
        detail = (
            exc.detail.model_dump(mode="json", exclude_none=True)
            if exc.detail is not None
            else {"reason": code, "message": "ZIP archive is invalid"}
        )
        status = (
            413
            if detail.get("reason")
            in {"resource_budget_exceeded", "archive_compression_ratio_exceeded"}
            else 400
        )
        raise AssetsSourceGatewayError(code, detail, status) from None
    if code == "account_not_active":
        raise AssetsSourceGatewayError(
            code,
            "账号已停用或不存在",
            401,
            {"WWW-Authenticate": "Bearer"},
        ) from None
    if code == "dataset_missing":
        raise AssetsSourceGatewayError(code, "Dataset not found", 404) from None
    if code == "invalid_account_role":
        raise AssetsSourceGatewayError(code, "账号角色无效，访问被拒绝", 403) from None
    if code == "catalog_manager_required":
        raise AssetsSourceGatewayError(code, "仅素材库管理员可扫描素材", 403) from None
    if code == "reserved_namespace":
        raise AssetsSourceGatewayError(
            code, "Dataset name uses a reserved storage namespace", 422
        ) from None
    if code == "temporal_scenes_missing":
        raise AssetsSourceGatewayError(
            code, "声明为时序数据集但未识别出任何 scene,请检查目录结构", 422
        ) from None
    if code == "invalid_request":
        raise AssetsSourceGatewayError(
            code,
            "上传参数无效" if kind == "zip" else "Invalid source request",
            422,
        ) from None
    if code in {
        "account_busy",
        "dataset_busy",
        "source_busy",
        "operation_conflict",
        "operation_missing",
        "lease_lost",
        "scene_conflict",
    }:
        detail = {"reason": code}
        if exc.detail is not None and exc.detail.conflicts_with is not None:
            detail["conflicts_with"] = exc.detail.conflicts_with
        raise AssetsSourceGatewayError(code, detail, 409) from None
    if code in {"service_unavailable", "storage_unavailable"}:
        raise AssetsSourceGatewayError(
            code, "Assets source service is unavailable", 503
        ) from None
    raise AssetsSourceGatewayError(
        "invalid_response", "Assets source returned an invalid response", 502
    ) from None


async def _stream_binary(source: BinaryIO):
    while True:
        chunk = await asyncio.to_thread(source.read, 64 * 1024)
        if not chunk:
            return
        yield chunk if isinstance(chunk, bytes) else bytes(chunk)


def encode_ingest_public_result(receipt: IngestReceipt) -> bytes:
    try:
        body = json.dumps(
            receipt.public_result(),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise AssetsSourceGatewayError(
            "invalid_response", "Assets source returned an invalid response", 502
        ) from None
    if len(body) > MAX_SOURCE_RESPONSE_BYTES:
        raise AssetsSourceGatewayError(
            "invalid_response", "Assets source returned an invalid response", 502
        )
    return body


def _managed_client(storage: StorageService):
    from botocore.config import Config

    return StorageService(
        client_config=Config(
            connect_timeout=2,
            read_timeout=60,
            retries={"total_max_attempts": 1, "mode": "standard"},
            proxies={},
        )
    ).client


class _LocalStoragePort:
    def __init__(self, storage: StorageService) -> None:
        from aap_assets_source_runtime.managed_storage import ManagedStorage

        self._storage = storage
        self._managed = ManagedStorage(_managed_client(storage), timeout_seconds=60)

    async def generate_upload_url(self, key: str, content_type: str) -> str:
        try:
            return await asyncio.to_thread(
                self._storage.generate_upload_url,
                key,
                content_type,
                bucket=self._storage.import_bucket,
            )
        except (ClientError, BotoCoreError, OSError) as exc:
            raise SourceFailure("storage_unavailable") from exc

    async def verify_staging_upload(self, key: str) -> dict | None:
        return await self._head(key, self._storage.import_bucket)

    async def verify_legacy_source(self, key: str) -> dict | None:
        return await self._head(key, self._storage.datasets_bucket)

    async def _head(self, key: str, bucket: str) -> dict | None:
        try:
            return await asyncio.to_thread(self._storage.verify_upload, key, bucket)
        except (ClientError, BotoCoreError, OSError) as exc:
            raise SourceFailure("storage_unavailable") from exc

    async def read_staging_image_dimensions(
        self, key: str, *, if_match: str
    ) -> tuple[int, int] | None:
        return await self._dimensions(key, self._storage.import_bucket, if_match)

    async def read_legacy_image_dimensions(
        self, key: str, *, if_match: str
    ) -> tuple[int, int] | None:
        return await self._dimensions(key, self._storage.datasets_bucket, if_match)

    async def _dimensions(self, key: str, bucket: str, if_match: str):
        try:
            return await asyncio.to_thread(
                self._storage.read_image_dimensions,
                key,
                bucket=bucket,
                if_match=if_match,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"412", "PreconditionFailed", "404", "NoSuchKey", "NotFound"}:
                raise SourceFailure("source_changed") from exc
            raise SourceFailure("storage_unavailable") from exc
        except (BotoCoreError, OSError) as exc:
            raise SourceFailure("storage_unavailable") from exc

    async def publish_managed_upload(
        self,
        target,
        *,
        staging_key: str,
        staging_etag: str | None,
        staging_version_id: str | None,
        max_bytes: int,
        image: bool,
        content_type: str,
    ):
        from aap_assets_source_runtime.ingest_storage_errors import (
            ManagedStorageError,
        )

        try:
            return await self._managed.publish_object(
                target,
                source_client=self._storage.client,
                source_bucket=self._storage.import_bucket,
                source_key=staging_key,
                source_etag=staging_etag,
                source_version_id=staging_version_id,
                content_type=content_type,
                max_bytes=max_bytes,
                image=image,
            )
        except ManagedStorageError as exc:
            raise SourceFailure(exc.code) from None


class _LegacyUploadData:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def read(self, request: LegacyUploadLockRequest) -> LegacyUploadState:
        payload = await self._db.scalar(
            text(
                "SELECT aap_assets_v1.read_legacy_dataset_upload("
                "CAST(:operation AS uuid), CAST(:actor AS uuid), "
                "CAST(:dataset AS uuid), CAST(:item AS uuid))"
            ),
            {
                "operation": request.operation_id,
                "actor": request.actor_id,
                "dataset": request.dataset_id,
                "item": request.item_id,
            },
        )
        state = self._decode(payload)
        self._check_scope(state, request)
        return state

    async def lock(self, request: LegacyUploadLockRequest) -> LegacyUploadState:
        payload = await self._db.scalar(
            text(
                "SELECT aap_assets_v1.lock_legacy_dataset_upload("
                "CAST(:operation AS uuid), CAST(:actor AS uuid), "
                "CAST(:dataset AS uuid), CAST(:item AS uuid))"
            ),
            {
                "operation": request.operation_id,
                "actor": request.actor_id,
                "dataset": request.dataset_id,
                "item": request.item_id,
            },
        )
        state = self._decode(payload)
        self._check_scope(state, request)
        return state

    async def complete(self, request: LegacyUploadCompletion) -> LegacyUploadState:
        payload = await self._db.scalar(
            text(
                "SELECT aap_assets_v1.complete_legacy_dataset_upload("
                "CAST(:operation AS uuid), CAST(:actor AS uuid), "
                "CAST(:dataset AS uuid), CAST(:item AS uuid), "
                "CAST(:file_path AS text), CAST(:file_size AS bigint), "
                "CAST(:content_hash AS text), CAST(:width AS integer), "
                "CAST(:height AS integer))"
            ),
            {
                "operation": request.operation_id,
                "actor": request.actor_id,
                "dataset": request.dataset_id,
                "item": request.item_id,
                "file_path": request.expected_file_path,
                "file_size": request.file_size,
                "content_hash": request.content_hash,
                "width": request.width,
                "height": request.height,
            },
        )
        state = self._decode(payload)
        self._check_scope(state, request)
        if state.file_path != request.expected_file_path:
            raise SourceFailure("invalid_response")
        return state

    @staticmethod
    def _decode(payload: object) -> LegacyUploadState:
        try:
            return decode_legacy_upload_reply(payload)
        except DatasetUploadFailure:
            raise
        except (ValidationError, ValueError, TypeError) as exc:
            raise SourceFailure("invalid_response") from exc

    @staticmethod
    def _check_scope(
        state: LegacyUploadState,
        request: LegacyUploadLockRequest | LegacyUploadCompletion,
    ) -> None:
        if (
            state.operation_id,
            state.actor_id,
            state.dataset_id,
            state.item_id,
        ) != (
            request.operation_id,
            request.actor_id,
            request.dataset_id,
            request.item_id,
        ):
            raise SourceFailure("invalid_response")


class AssetsSourceGateway:
    """Select local composition once or proxy the whole operation without fallback."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        storage: StorageService | None = None,
        cleanup_bucket: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._db = db
        self._storage = storage or storage_service
        self._cleanup_bucket = cleanup_bucket or self._storage.media_cache_bucket
        self._transport = transport
        self._peer = _peer_url()

    @property
    def is_remote(self) -> bool:
        return self._peer is not None

    async def init_upload(self, request: UploadInitRequest) -> UploadInitResult:
        if self._peer is not None:
            result = await self._remote("upload-init", request, UploadInitResult)
        else:
            try:
                result = await self._local_workflow().init_upload(request)
            except SourceFailure as exc:
                _raise_failure(exc, operation="upload-init")
            except SQLAlchemyError as exc:
                raise self._unavailable() from exc
            except (ValidationError, ValueError, TypeError) as exc:
                raise self._invalid_response() from exc
        if (result.actor_id, result.dataset_id, result.item_id) != (
            request.actor_id,
            request.dataset_id,
            request.item_id,
        ):
            _raise_failure(SourceFailure("invalid_response"), operation="upload-init")
        return result

    async def complete_upload(self, request: UploadCompleteRequest) -> UploadOutcome:
        if self._peer is not None:
            result = await self._remote("upload-complete", request, UploadOutcome)
        else:
            try:
                result = await self._local_workflow().complete_upload(request)
            except SourceFailure as exc:
                _raise_failure(exc, operation="upload-complete")
            except SQLAlchemyError as exc:
                raise self._unavailable() from exc
            except (ValidationError, ValueError, TypeError) as exc:
                raise self._invalid_response() from exc
            if result.result is not None:
                self._invalidate_upload_counters(request.dataset_id)
        if (
            result.operation_id,
            result.actor_id,
            result.dataset_id,
            result.item_id,
        ) != (
            request.operation_id,
            request.actor_id,
            request.dataset_id,
            request.item_id,
        ):
            _raise_failure(
                SourceFailure("invalid_response"), operation="upload-complete"
            )
        return result

    async def delete_source(
        self, request: SourceDeletionRequest
    ) -> DatasetSourceDeletionReceipt:
        if self._peer is not None:
            result = await self._remote("delete", request, DatasetSourceDeletionReceipt)
        else:
            try:
                result = await self._local_workflow().delete_source(request)
            except SourceFailure as exc:
                _raise_failure(exc, operation="delete")
            except SQLAlchemyError as exc:
                raise self._unavailable() from exc
            except (ValidationError, ValueError, TypeError) as exc:
                raise self._invalid_response() from exc
            self._invalidate_source_rows(request.dataset_id, request.item_id)
        if (
            result.operation_id,
            result.actor_id,
            result.dataset_id,
            result.item_id,
        ) != (
            request.operation_id,
            request.actor_id,
            request.dataset_id,
            request.item_id,
        ):
            _raise_failure(SourceFailure("invalid_response"), operation="delete")
        return result

    async def ingest_zip(
        self, command: IngestCommand, source: BinaryIO
    ) -> IngestReceipt:
        if self._peer is not None:
            result = await self._remote_ingest("upload-zip", command, source=source)
        else:
            try:
                result = await self._local_ingest_workflow().upload_zip(command, source)
            except IngestFailure as exc:
                _raise_ingest_failure(exc, kind="zip")
            except SQLAlchemyError as exc:
                raise self._unavailable() from exc
            except (ValidationError, ValueError, TypeError, KeyError):
                raise self._invalid_response() from None
        return self._validate_ingest_result(result, command, kind="zip")

    async def ingest_scan(self, command: IngestCommand) -> IngestReceipt:
        if self._peer is not None:
            result = await self._remote_ingest("scan", command)
        else:
            try:
                result = await self._local_ingest_workflow().scan(command)
            except IngestFailure as exc:
                _raise_ingest_failure(exc, kind="scan")
            except SQLAlchemyError as exc:
                raise self._unavailable() from exc
            except (ValidationError, ValueError, TypeError, KeyError):
                raise self._invalid_response() from None
        return self._validate_ingest_result(result, command, kind="scan")

    async def _remote_ingest(
        self,
        endpoint: str,
        command: IngestCommand,
        *,
        source: BinaryIO | None = None,
    ) -> IngestReceipt:
        kind = "zip" if source is not None else "scan"
        headers = {
            "Authorization": f"Bearer {settings.assets_source_token}",
            "Accept": "application/json",
        }
        params = None
        if source is not None:
            headers["Content-Type"] = "application/zip"
            params = {
                "operation_id": str(command.operation_id),
                "actor_id": str(command.actor_id),
                "dataset_id": str(command.dataset_id),
            }
            content = _stream_binary(source)
        else:
            headers["Content-Type"] = "application/json"
            content = _encode_request(command)
        try:
            async with httpx.AsyncClient(
                timeout=settings.assets_source_timeout_seconds,
                trust_env=False,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._peer}/v1/source/{endpoint}",
                    params=params,
                    headers=headers,
                    content=content,
                ) as response:
                    if response.status_code >= 500:
                        if response.status_code == 502:
                            raise self._invalid_response()
                        raise self._unavailable()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type != "application/json":
                        raise self._invalid_response()
                    try:
                        payload = json.loads(await _read_response(response))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        raise self._invalid_response() from None
                    try:
                        result = decode_ingest_reply(payload, IngestReceipt)
                    except IngestFailure as exc:
                        _raise_ingest_failure(exc, kind=kind)
                    if response.status_code != 200 or result is None:
                        raise self._invalid_response()
        except AssetsSourceGatewayError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise self._unavailable() from exc
        return self._validate_ingest_result(result, command, kind=kind)

    @staticmethod
    def _validate_ingest_result(
        result: IngestReceipt, command: IngestCommand, *, kind: str
    ) -> IngestReceipt:
        try:
            require_ingest_scope(command, result)
        except IngestFailure as exc:
            _raise_ingest_failure(exc, kind=kind)
        if result.kind != kind:
            raise AssetsSourceGatewayError(
                "invalid_response", "Assets source returned an invalid response", 502
            )
        return result

    async def _remote(
        self,
        endpoint: str,
        request: BaseModel,
        result_model: type[_RESULT],
    ) -> _RESULT:
        body = _encode_request(request)
        try:
            async with httpx.AsyncClient(
                timeout=settings.assets_source_timeout_seconds,
                trust_env=False,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self._peer}/v1/source/{endpoint}",
                    headers={
                        "Authorization": f"Bearer {settings.assets_source_token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    content=body,
                ) as response:
                    if response.status_code != 200:
                        raise self._unavailable()
                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    if content_type != "application/json":
                        raise self._invalid_response()
                    payload = json.loads(await _read_response(response))
        except AssetsSourceGatewayError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise self._unavailable() from exc
        try:
            return decode_source_reply(payload, result_model)
        except SourceFailure as exc:
            _raise_failure(exc, operation=endpoint)

    def _local_workflow(self):
        from aap_assets_source_runtime.workflow import UploadWorkflow

        return UploadWorkflow(
            upload_data=DatasetUploadData(self._db),
            legacy_upload_data=_LegacyUploadData(self._db),
            deletion_data=DatasetSourceDeletionData(self._db),
            storage=_LocalStoragePort(self._storage),
            image_admission=ImageAdmission(
                image_gateway(self._db, caller_transaction=True), image_policy()
            ),
            cleanup_bucket=self._cleanup_bucket,
        )

    def _local_ingest_workflow(self):
        from aap_assets_source_runtime.ingest_storage import IngestStorage
        from aap_assets_source_runtime.ingest_workflow import IngestWorkflow
        from aap_assets_source_runtime.managed_storage import ManagedStorage

        from app.services.assets_source_ingest import DatasetSourceIngestData

        return IngestWorkflow(
            data=DatasetSourceIngestData(self._db),
            storage=IngestStorage(
                self._storage.client,
                self._storage.datasets_bucket,
                managed=ManagedStorage(
                    _managed_client(self._storage), timeout_seconds=60
                ),
            ),
            image_admission=ImageAdmission(
                image_gateway(self._db, caller_transaction=True), image_policy()
            ),
        )

    def _invalidate_upload_counters(self, dataset_id) -> None:
        for row in self._db.sync_session.identity_map.values():
            if isinstance(row, Project):
                self._db.expire(row, ["total_tasks", "updated_at"])
            elif isinstance(row, Dataset) and inspect(row).identity == (dataset_id,):
                self._db.expire(row, ["file_count", "updated_at"])

    def _invalidate_source_rows(self, dataset_id, item_id) -> None:
        for row in list(self._db.sync_session.identity_map.values()):
            state = inspect(row)
            if isinstance(row, Dataset) and state.identity == (dataset_id,):
                if item_id is None:
                    self._db.expunge(row)
                else:
                    self._db.expire(row, ["file_count", "updated_at"])
            elif isinstance(row, (DatasetItem, Scene, ProjectDataset)) and (
                state.dict.get("dataset_id") == dataset_id
                and (
                    item_id is None
                    or isinstance(row, DatasetItem)
                    and state.identity == (item_id,)
                )
            ):
                self._db.expunge(row)
            elif (
                item_id is None
                and isinstance(row, TaskBatch)
                and state.dict.get("dataset_id") == dataset_id
            ):
                self._db.expire(row, ["dataset_id"])

    @staticmethod
    def _unavailable() -> AssetsSourceGatewayError:
        return AssetsSourceGatewayError(
            "service_unavailable", "Assets source service is unavailable", 503
        )

    @staticmethod
    def _invalid_response() -> AssetsSourceGatewayError:
        return AssetsSourceGatewayError(
            "invalid_response", "Assets source returned an invalid response", 502
        )
