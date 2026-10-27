from __future__ import annotations

from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_assets_source.contract import (
    SourceFailure,
    UploadInitRequest,
    UploadInitResult,
    decode_source_reply,
)
from aap_assets_source.upload_contract import DatasetUploadFailure, DatasetUploadIntent
from aap_assets_source_runtime.config import load_config
from aap_assets_source_runtime.runtime import SourceRuntime


def _config(*, max_response_bytes=16_777_216):
    return load_config(
        {
            "ASSETS_SOURCE_DATABASE_URL": "postgresql://assets_source:secret@db.test/aap",
            "ASSETS_SOURCE_TOKEN": "s" * 40,
            "ASSETS_SOURCE_S3_ENDPOINT": "http://minio.internal:9000",
            "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": "https://storage.example.test",
            "ASSETS_SOURCE_S3_ACCESS_KEY": "source-access",
            "ASSETS_SOURCE_S3_SECRET_KEY": "source-secret",
            "ASSETS_SOURCE_DATASETS_BUCKET": "datasets",
            "ASSETS_SOURCE_IMPORT_BUCKET": "imports",
            "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": "media-cache",
            "ASSETS_SOURCE_MAX_RESPONSE_BYTES": str(max_response_bytes),
        }
    )


class _UploadData:
    def __init__(self, intent, failure=None):
        self.intent = intent
        self.failure = failure

    async def reserve(self, request):
        if self.failure:
            raise self.failure
        return self.intent

    async def allocate(self, request):
        from uuid import uuid4

        from aap_assets_source.managed_original_contract import (
            ManagedOriginalMaterial,
        )

        return ManagedOriginalMaterial(
            creation_id=uuid4(),
            dataset_id=self.intent.dataset_id,
            location_id=uuid4(),
            bucket="datasets",
            object_key=request["file_path"],
            key_prefix="",
            endpoint_url="http://s3",
            storage_identity="a" * 64,
            config_revision=1,
            binding_generation=1,
            max_bytes=request["max_bytes"],
            content_type=request["content_type"],
            origin=request["origin"],
            mode=request["mode"],
            staging={"bucket": "imports", "key": request["staging"]["key"]},
            publication_id=self.intent.publication_id,
            file_path=request["file_path"],
        )


class _Storage:
    def __init__(self, url):
        self.url = url

    async def generate_upload_url(self, key, content_type):
        return self.url


class _Database:
    def __init__(self, ports):
        self.ports = ports
        self.events = []

    @asynccontextmanager
    async def transaction(self):
        self.events.append("begin")
        try:
            yield self.ports
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")


def _request():
    actor_id, dataset_id, item_id = uuid4(), uuid4(), uuid4()
    request = UploadInitRequest(
        actor_id=actor_id,
        dataset_id=dataset_id,
        item_id=item_id,
        file_name="photo.png",
        content_type="image/png",
    )
    intent = DatasetUploadIntent(
        item_id=item_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
        file_name="photo.png",
        file_path=None,
        file_type="image",
        content_type="image/png",
        staging_key=f"dataset-uploads/{item_id}/source",
        publication_id=None,
        state="pending",
        result=None,
    )
    return request, intent


def _runtime(config, request, intent, *, url="https://storage.example.test/signed"):
    ports = SimpleNamespace(
        uploads=_UploadData(intent),
        legacy_uploads=object(),
        deletions=object(),
        images=object(),
    )
    database = _Database(ports)
    runtime = SourceRuntime(config, database, _Storage(url))
    return runtime, database


@pytest.mark.asyncio
async def test_validated_response_is_serialized_before_transaction_commit() -> None:
    request, intent = _request()
    runtime, database = _runtime(_config(), request, intent)

    body = await runtime.init_upload(request)

    result = decode_source_reply(json.loads(body), UploadInitResult)
    assert result.item_id == request.item_id
    assert database.events == ["begin", "commit"]


@pytest.mark.asyncio
async def test_response_budget_failure_rolls_back_and_returns_safe_envelope() -> None:
    request, intent = _request()
    runtime, database = _runtime(
        _config(max_response_bytes=128),
        request,
        intent,
        url="https://storage.example.test/" + "x" * 512,
    )

    body = await runtime.init_upload(request)

    with pytest.raises(SourceFailure) as error:
        decode_source_reply(json.loads(body), UploadInitResult)
    assert error.value.code == "service_unavailable"
    assert database.events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_business_error_is_validated_before_rollback_and_keeps_stable_code() -> (
    None
):
    request, intent = _request()
    ports = SimpleNamespace(
        uploads=_UploadData(intent, DatasetUploadFailure("account_not_active")),
        legacy_uploads=object(),
        deletions=object(),
        images=object(),
    )
    database = _Database(ports)
    runtime = SourceRuntime(
        _config(), database, _Storage("https://storage.example.test/signed")
    )

    body = await runtime.init_upload(request)

    with pytest.raises(SourceFailure) as error:
        decode_source_reply(json.loads(body), UploadInitResult)
    assert error.value.code == "account_not_active"
    assert database.events == ["begin", "rollback"]
