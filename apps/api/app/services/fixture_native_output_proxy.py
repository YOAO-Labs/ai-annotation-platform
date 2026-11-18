"""Private per-run native output proxy with a permit through physical close."""

from __future__ import annotations

import asyncio
import builtins
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import unquote, urlsplit
import uuid

import asyncpg
import boto3
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Request, Response
from starlette.responses import StreamingResponse

from app.services.original_playback_response import select_range
from yoao_assets_media_runtime import fixture_output_capabilities
from yoao_assets_media_runtime.fixture_native_outputs import load_target
from yoao_assets_media_runtime.storage import _run_blocking

_CHUNK_BYTES = 256 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")
_MEDIA_TYPES = {
    "webp": "image/webp",
    "jpeg": "image/jpeg",
    "mp4": "video/mp4",
    "json": "application/json",
}


def _credentials():
    selector = os.environ.get("AAP_PRIVATE_FIXTURE_MEDIA_READER_CREDENTIAL_FILE")
    if not selector or not Path(selector).is_absolute():
        raise RuntimeError("fixture native reader credentials are unavailable")
    try:
        descriptor = os.open(selector, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("invalid reader file")
            encoded = stream.read(8193)
        if len(encoded) > 8192:
            raise ValueError("invalid reader file")
        data = json.loads(encoded)
        if not isinstance(data, dict) or set(data) != {
            "schema",
            "run_id",
            "dsn",
            "database_oid",
            "database_owner_marker",
        }:
            raise ValueError("invalid reader file")
        run = uuid.UUID(data["run_id"])
        dsn = data["dsn"]
        if not isinstance(dsn, str) or not dsn.startswith("postgresql+asyncpg://"):
            raise ValueError("invalid reader file")
        parsed = urlsplit("postgresql://" + dsn.removeprefix("postgresql+asyncpg://"))
        parsed.port
        if (
            data["schema"] != "aap-fixture-native-reader/v1"
            or str(run) != data["run_id"]
            or parsed.scheme != "postgresql"
            or not parsed.hostname
            or unquote(parsed.username or "") != f"aap_fixture_media_read_{run.hex}"
            or not parsed.password
            or not parsed.path.lower().endswith(("_test", "_e2e"))
            or "/" in parsed.path[1:]
            or parsed.query
            or parsed.fragment
            or type(data["database_oid"]) is not int
            or data["database_oid"] <= 0
            or not isinstance(data["database_owner_marker"], str)
            or not data["database_owner_marker"]
        ):
            raise ValueError("invalid reader file")
        target = load_target(
            "AAP_PRIVATE_FIXTURE_MEDIA_OUTPUT_READER_FILE",
            dsn,
            "aap_fixture_media_read_",
        )
        if target["run_id"] != str(run):
            raise ValueError("invalid reader target")
        return data, target
    except (OSError, ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        raise RuntimeError(
            "fixture native reader credentials are unavailable"
        ) from None


class _Reader:
    def __init__(self, credentials, target, run_id, write_id, semaphore):
        self.credentials, self.target = credentials, target
        self.run_id, self.write_id = run_id, write_id
        self.semaphore = semaphore
        self.read_id = uuid.uuid4()
        self.connection = self.client = self.body = None
        self.admitted = self.uncertain = self.acquired = False
        self.reply = None

    async def open(self):
        await self.semaphore.acquire()
        self.acquired = True
        dsn = "postgresql://" + self.credentials["dsn"].removeprefix(
            "postgresql+asyncpg://"
        )
        self.connection = await asyncpg.connect(dsn, command_timeout=30)
        label = f"aap-fixture-native-output-read:{self.read_id}"
        await self.connection.fetchval(
            "SELECT pg_catalog.pg_advisory_lock(pg_catalog.hashtextextended($1,0))",
            label,
        )
        reply = await self.connection.fetchval(
            "SELECT yoao_assets_v1.begin_fixture_native_output_read($1::uuid,$2::uuid,$3::uuid)",
            self.run_id,
            self.write_id,
            self.read_id,
        )
        if isinstance(reply, str):
            reply = json.loads(reply)
        self.admitted = True
        fields = {
            "id",
            "run_id",
            "dataset_id",
            "write_id",
            "endpoint",
            "bucket",
            "storage_identity",
            "receipt_sha256",
            "key",
            "database_oid",
            "database_owner_marker",
            "sha256",
            "size_bytes",
            "etag",
            "version_id",
        }
        if (
            not isinstance(reply, dict)
            or set(reply) != fields
            or reply["id"] != str(self.read_id)
            or reply["run_id"] != str(self.run_id)
            or reply["write_id"] != str(self.write_id)
            or any(
                reply[name] != self.target[name]
                for name in ("endpoint", "bucket", "storage_identity", "receipt_sha256")
            )
            or reply["database_oid"] != self.credentials["database_oid"]
            or reply["database_owner_marker"]
            != self.credentials["database_owner_marker"]
            or not isinstance(reply["key"], str)
            or not reply["key"]
            or not isinstance(reply["sha256"], str)
            or _SHA.fullmatch(reply["sha256"]) is None
            or type(reply["size_bytes"]) is not int
            or reply["size_bytes"] <= 0
            or not isinstance(reply["etag"], str)
            or not reply["etag"]
            or not isinstance(reply["version_id"], str)
            or reply["version_id"] in {"", "null"}
        ):
            raise RuntimeError("fixture native output identity changed")
        uuid.UUID(reply["dataset_id"])
        self.reply = reply
        self.client = boto3.client(
            "s3",
            endpoint_url=self.target["endpoint"],
            region_name="us-east-1",
            aws_access_key_id=self.target["access_key"],
            aws_secret_access_key=self.target["secret_key"],
            config=Config(
                signature_version="s3v4",
                connect_timeout=3,
                read_timeout=30,
                retries={"mode": "standard", "total_max_attempts": 1},
                s3={"addressing_style": "path"},
            ),
        )
        return reply

    def _head(self):
        reply = self.reply
        return self.client.head_object(
            Bucket=reply["bucket"],
            Key=reply["key"],
            VersionId=reply["version_id"],
            IfMatch=reply["etag"],
        )

    def _get(self, selected):
        reply = self.reply
        params = {
            "Bucket": reply["bucket"],
            "Key": reply["key"],
            "VersionId": reply["version_id"],
            "IfMatch": reply["etag"],
        }
        if selected.status_code == 206:
            params["Range"] = (
                f"bytes={selected.start}-{selected.start + selected.length - 1}"
            )
        self.uncertain = True
        response = self.client.get_object(**params)
        self.body = response["Body"]
        self.uncertain = False
        return response

    async def close(self):
        errors = []
        if self.body is not None:
            try:
                await _run_blocking(self.body.close)
            except BaseException as exc:
                self.uncertain = True
                errors.append(exc)
            self.body = None
        if self.client is not None:
            try:
                await _run_blocking(self.client.close)
            except BaseException as exc:
                self.uncertain = True
                errors.append(exc)
            self.client = None
        if self.admitted and not self.uncertain:
            try:
                complete = await self.connection.fetchval(
                    "SELECT yoao_assets_v1.finish_fixture_native_output_read($1::uuid,$2::uuid)",
                    self.run_id,
                    self.read_id,
                )
                if complete is not True:
                    raise RuntimeError("fixture native output read is unsettled")
            except BaseException as exc:
                errors.append(exc)
        elif self.admitted and self.uncertain and not errors:
            errors.append(
                RuntimeError("fixture native output physical close is unproven")
            )
        if self.connection is not None:
            try:
                await self.connection.close()
            except BaseException as exc:
                errors.append(exc)
            self.connection = None
        if self.acquired:
            self.semaphore.release()
            self.acquired = False
        if errors:
            failure = builtins.BaseExceptionGroup(
                "Fixture native output reader close failed", errors
            )
            failure.storage_operation_unsettled = True
            raise failure


async def _finish(reader):
    task = asyncio.create_task(reader.close())
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = exc
        except BaseException:
            break
    try:
        task.result()
    except BaseException as close_error:
        if cancelled is not None:
            cancelled.storage_operation_unsettled = True
            raise cancelled from close_error
        raise
    if cancelled is not None:
        raise cancelled


def _matches(response, expected, length, status, content_range):
    return (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") == status
        and response.get("ETag") == expected["etag"]
        and response.get("VersionId") == expected["version_id"]
        and response.get("ContentLength") == length
        and response.get("ContentRange") == content_range
        and not response.get("DeleteMarker")
    )


def create_app():
    credentials, target = _credentials()
    allowed_run = uuid.UUID(credentials["run_id"])
    slots = asyncio.BoundedSemaphore(4)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/v1/fixture-native-output-ready/{run_id}")
    async def ready(run_id: uuid.UUID, expires: int = 0, signature: str = ""):
        if run_id != allowed_run or not fixture_output_capabilities.verify_ready(
            run_id, expires, signature
        ):
            raise HTTPException(404, detail={"code": "fixture_output_not_found"})
        dsn = "postgresql://" + credentials["dsn"].removeprefix("postgresql+asyncpg://")
        connection = await asyncpg.connect(dsn, command_timeout=30)
        try:
            observed = await connection.fetchval(
                "SELECT yoao_assets_v1.fixture_native_reader_ready($1::uuid)", run_id
            )
        finally:
            await connection.close()
        if isinstance(observed, str):
            observed = json.loads(observed)
        if (
            not isinstance(observed, dict)
            or set(observed)
            != {
                "run_id",
                "endpoint",
                "bucket",
                "storage_identity",
                "receipt_sha256",
                "database_oid",
                "database_owner_marker",
            }
            or any(
                observed[name] != target[name]
                for name in (
                    "run_id",
                    "endpoint",
                    "bucket",
                    "storage_identity",
                    "receipt_sha256",
                )
            )
            or observed["database_oid"] != credentials["database_oid"]
            or observed["database_owner_marker"] != credentials["database_owner_marker"]
        ):
            raise HTTPException(503, detail={"code": "fixture_output_unavailable"})
        return {
            "schema": "aap-fixture-native-output-ready/v1",
            "run_id": str(run_id),
            "receipt_sha256": target["receipt_sha256"],
        }

    @app.api_route(
        "/v1/fixture-native-output/{run_id}/{write_id}", methods=["GET", "HEAD"]
    )
    async def read_output(
        run_id: uuid.UUID,
        write_id: uuid.UUID,
        request: Request,
        expires: int = 0,
        signature: str = "",
    ):
        if run_id != allowed_run or not fixture_output_capabilities.verify(
            run_id, write_id, expires, signature
        ):
            raise HTTPException(404, detail={"code": "fixture_output_not_found"})
        reader = _Reader(credentials, target, run_id, write_id, slots)
        streaming = False
        try:
            reply = await reader.open()
            selected = select_range(
                reply["size_bytes"],
                reply["etag"],
                request_method=request.method,
                range_header=request.headers.get("range"),
                if_range=request.headers.get("if-range"),
            )
            head = await _run_blocking(reader._head)
            if not _matches(head, reply, reply["size_bytes"], 200, None):
                raise RuntimeError("fixture native output identity changed")
            media_type = _MEDIA_TYPES.get(reply["key"].rsplit(".", 1)[-1])
            if media_type is None:
                raise RuntimeError("fixture native output type is invalid")
            headers = {
                "ETag": reply["etag"],
                "Accept-Ranges": "bytes",
                "Content-Length": str(selected.length),
                "x-amz-version-id": reply["version_id"],
                "Cache-Control": "private, no-store",
            }
            if selected.content_range is not None:
                headers["Content-Range"] = selected.content_range
            if request.method == "HEAD":
                await _finish(reader)
                return Response(status_code=200, headers=headers, media_type=media_type)
            response = await _run_blocking(reader._get, selected)
            if not _matches(
                response,
                reply,
                selected.length,
                selected.status_code,
                selected.content_range,
            ):
                raise RuntimeError("fixture native output identity changed")

            async def content():
                remaining = selected.length
                try:
                    while remaining:
                        chunk = await _run_blocking(
                            reader.body.read, min(_CHUNK_BYTES, remaining)
                        )
                        if not chunk or len(chunk) > remaining:
                            raise RuntimeError(
                                "fixture native output transfer ended early"
                            )
                        remaining -= len(chunk)
                        yield chunk
                finally:
                    await _finish(reader)

            streaming = True
            return StreamingResponse(
                content(),
                status_code=selected.status_code,
                headers=headers,
                media_type=media_type,
            )
        finally:
            if not streaming and reader.acquired:
                primary = sys.exception()
                try:
                    await _finish(reader)
                except BaseException as close_error:
                    if primary is not None:
                        combined = builtins.BaseExceptionGroup(
                            "Fixture native output request and close failed",
                            [primary, close_error],
                        )
                        combined.storage_operation_unsettled = True
                        raise combined from None
                    raise

    return app
