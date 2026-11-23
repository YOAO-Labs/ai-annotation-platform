"""Finite leased import commands. Provider locators are never request fields."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from io import BytesIO
from uuid import UUID

from anyio import CancelScope
from pydantic import ValidationError

from yoao_assets_source import managed_original as writer
from yoao_assets_source.import_contract import ImportSourceObject, ImportState
from yoao_assets_source.saved_io import SavedIOError

from .ingest_storage_errors import ManagedStorageError
from .managed_storage import ManagedRead, _image_dimensions, _map_writer_error
from .saved_io_import_transport import (
    TransferGuard,
    collect_manifest as _collect_manifest,
)
from .saved_io_import_transport import open_observed, source_error

_LIST = "yoao_assets_v1.import_saved_io_list"
_ACTION = "yoao_assets_v1.import_saved_io_action"
_LIST_FIELDS = {"job_id", "lease_token"}
_ENTRY_FIELDS = _LIST_FIELDS | {
    "entry_index",
    "publication_id",
    "item_id",
    "creation_id",
    "operation_id",
}
_RETRYABLE_SQLSTATES = {"40001", "40P01", "55P03"}
_MAX_OBJECT_BYTES = 64 * 1024**2 * writer.MAX_PARTS


def _parse(subject, *, entry=False):
    fields = _ENTRY_FIELDS if entry else _LIST_FIELDS
    if type(subject) is not dict or subject.keys() != fields:
        raise SavedIOError("invalid_request", status_code=400)
    parsed = {}
    for key, value in subject.items():
        if key == "entry_index":
            if type(value) is not int or not 0 <= value <= 2_147_483_647:
                raise SavedIOError("invalid_request", status_code=400)
            parsed[key] = value
        else:
            try:
                parsed[key] = str(UUID(value)) if type(value) is str else None
            except ValueError:
                parsed[key] = None
            if parsed[key] is None:
                raise SavedIOError("invalid_request", status_code=400)
    return parsed


async def _fetch(context, name, *args):
    for attempt in range(5):
        try:
            reply = await context.fetch(name, *args)
            break
        except Exception as exc:
            if getattr(exc, "sqlstate", None) not in _RETRYABLE_SQLSTATES:
                raise SavedIOError(
                    "storage_unavailable", status_code=503, unsettled=True
                ) from None
            if attempt == 4:
                raise SavedIOError("dataset_busy", status_code=409) from None
            await asyncio.sleep(0.025 * (attempt + 1))
    if not isinstance(reply, dict) or reply.get("schema") != "aap-dataset-import/v1":
        raise SavedIOError("invalid_response", status_code=502, unsettled=True)
    if reply.get("ok") is not True:
        code = (reply.get("error") or {}).get("code", "invalid_response")
        raise SavedIOError(code, status_code=400 if code == "invalid_request" else 409)
    if not isinstance(reply.get("data"), dict):
        raise SavedIOError("invalid_response", status_code=502, unsettled=True)
    return reply["data"]


def _policy(context):
    return list(context.config.connector_host_allowlist)


def _adapter(context, facts, path):
    return context.reference(
        facts["connection_id"],
        facts["revision"],
        path=path,
        require_conditional_reads=True,
    )


async def _list(context, subject, body=None):
    ids = _parse(subject)
    args = UUID(ids["job_id"]), UUID(ids["lease_token"]), _policy(context)
    proof = await _fetch(context, _LIST, *args, None)
    transport = proof["transport"]
    try:
        state = ImportState.model_validate(proof["job"])
    except ValidationError:
        raise SavedIOError("invalid_response", status_code=502) from None

    async def check():
        current = await _fetch(context, _LIST, *args, None)
        if current["transport"] != transport:
            raise SavedIOError("connection_changed")

    guard = TransferGuard(check, context.config.import_verify_timeout_seconds)
    try:
        async with _adapter(context, transport, state.source_path) as adapter:
            await check()
            objects, encoded = await context.run(
                _collect_manifest, adapter, state, guard
            )
    except Exception as exc:
        raise source_error(exc, listing=True) from None
    await _fetch(context, _LIST, *args, {"transport": transport, "objects": objects})
    return context.download(BytesIO(encoded), length=len(encoded), metadata={})


class _Action:
    def __init__(self, context, subject, kind) -> None:
        self.context = context
        self.subject = _parse(subject, entry=True)
        self.kind = kind
        self.initial = None
        self.expectation = None

    async def start(self):
        self.initial = await self.call("start")
        self.expectation = {
            field: self.initial[field] for field in ("transport", "target", "source")
        }
        return self.initial

    async def call(self, phase, **evidence):
        return await _fetch(
            self.context,
            _ACTION,
            self.subject,
            f"{self.kind}_{phase}",
            _policy(self.context),
            {**(self.expectation or {}), **evidence},
        )

    async def check(self):
        await self.call("check")

    def guard(self):
        return TransferGuard(self.check, self.initial["timeout"])

    async def open(self, stack, guard):
        try:
            observed = ImportSourceObject.model_validate(self.initial["source"])
            adapter = await stack.enter_async_context(
                _adapter(self.context, self.initial["transport"], self.initial["path"])
            )
            await self.check()
            return await self.context.run(
                open_observed,
                adapter,
                observed,
                self.initial["path"],
                guard,
                digest=self.kind == "read",
                _discard=lambda handle: handle.close(),
            )
        except Exception as exc:
            raise source_error(exc) from None

    @asynccontextmanager
    async def stream(self, guard):
        async with AsyncExitStack() as stack:
            physical = await self.open(stack, guard)
            try:
                yield physical
            finally:
                await self.context.run(physical.close)


async def _read(context, subject, body=None):
    action = _Action(context, subject, "read")
    proof = await action.start()
    stack = AsyncExitStack()
    try:
        stream = await action.open(stack, action.guard())
    except BaseException as exc:
        with CancelScope(shield=True):
            await stack.aclose()
            if isinstance(exc, Exception) and not getattr(
                exc, "storage_operation_unsettled", False
            ):
                await action.call("closed")
        raise

    async def closed():
        await stack.aclose()
        await action.call("closed")

    async def complete():
        await action.call("complete", evidence=stream.evidence())

    return context.download(
        stream,
        length=proof["source"]["size"],
        metadata={},
        after_close=closed,
        after_body=complete,
    )


def _destination_error(exc):
    if isinstance(exc, SavedIOError):
        return exc
    return SavedIOError(
        "destination_write_failed",
        status_code=503,
        unsettled=getattr(exc, "outcome", None) == "unknown"
        or bool(getattr(exc, "storage_operation_unsettled", False)),
    )


async def _copy(context, subject, body=None):
    action = _Action(context, subject, "copy")
    proof = await action.start()
    guard = action.guard()
    target = proof["target"]
    size = proof["source"]["size"]
    prepared = None
    receipt = None
    try:
        if size > _MAX_OBJECT_BYTES:
            raise SavedIOError("destination_write_failed", status_code=413)
        pin = await context.s3(
            target["location_id"],
            target["config_revision"],
            target["connection_revision"],
            "datasets",
            target["bucket"],
            target["key"],
        )
        async with pin.client() as destination:
            completion_started = False

            def abort(result):
                writer.abort_prepared(destination, pin.bucket, pin.key, result)

            try:
                async with action.stream(guard) as source:
                    try:
                        prepared = await context.run(
                            writer.prepare_stream,
                            destination,
                            pin.bucket,
                            pin.key,
                            source,
                            content_type=proof["content_type"],
                            max_bytes=size,
                            _check=guard,
                            _discard=abort,
                        )
                    except writer.WriterError as exc:
                        if exc.outcome != "unknown" and (
                            error := source.error or guard.error
                        ):
                            raise error from None
                        raise
                await action.call("prepare", evidence=prepared.evidence())
                await action.check()
                guard.deadline()
                completion_started = True
                receipt = await context.run(
                    writer.complete,
                    destination,
                    pin.bucket,
                    pin.key,
                    prepared,
                    content_type=proof["content_type"],
                )
            except BaseException as exc:
                if prepared is not None and (
                    not completion_started
                    or isinstance(exc, writer.WriterError)
                    and exc.outcome != "unknown"
                ):
                    await context.run(abort, prepared)
                raise
        await action.call("written", evidence=receipt.evidence())
    except Exception as exc:
        failure = _destination_error(exc)
        if receipt is not None or failure.unsettled:
            raise SavedIOError(
                failure.code, status_code=failure.status_code, unsettled=True
            ) from None
        try:
            code = (
                failure.code
                if failure.code
                in {
                    "source_changed",
                    "source_not_found",
                    "source_read_failed",
                    "invalid_source_object",
                }
                else "destination_write_failed"
            )
            await action.call("failed", code=code)
        except Exception:
            raise SavedIOError(
                failure.code, status_code=failure.status_code, unsettled=True
            ) from None
        raise failure from None
    return {
        "sha256": prepared.sha256,
        "md5": prepared.md5,
        "size_bytes": prepared.size_bytes,
        "etag": receipt.etag,
        "version_id": receipt.version_id,
    }


async def verify_registered(context, original, *, image: bool, timeout_seconds: float):
    """Read the verifier's admitted, saved version with the same pinned transport as copy."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    try:
        policy = await context.connector_allowlist()
        pin = await context.s3(
            original.location_id,
            original.config_revision,
            original.connection_revision,
            "datasets",
            original.bucket,
            original.object_key,
        )
        async with pin.client() as client:
            verified = await context.run(
                writer.verify,
                client,
                pin.bucket,
                pin.key,
                writer.WriteReceipt(original.etag, original.version_id, None, {}),
                sha256=original.sha256,
                size_bytes=original.size_bytes,
                max_bytes=max(original.size_bytes, 1),
                timeout_seconds=deadline - asyncio.get_running_loop().time(),
            )
            head = await context.run(
                client.head_object,
                Bucket=pin.bucket,
                Key=pin.key,
                IfMatch=original.etag,
            )
        version = head.get("VersionId")
        if (
            head.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            or head.get("ETag") != original.etag
            or (None if version == "null" else version) != original.version_id
            or head.get("ContentLength") != original.size_bytes
            or await context.connector_allowlist() != policy
            or asyncio.get_running_loop().time() >= deadline
        ):
            raise ManagedStorageError("source_changed")
    except writer.WriterError as exc:
        raise _map_writer_error(exc) from None
    except Exception as exc:
        if isinstance(exc, ManagedStorageError):
            raise
        raise ManagedStorageError(
            "storage_write_failed",
            uncertain=bool(getattr(exc, "storage_operation_unsettled", False)),
        ) from None
    return ManagedRead(
        sha256=verified.sha256,
        md5=verified.md5,
        size_bytes=verified.size_bytes,
        head_bytes=verified.head_bytes,
        dimensions=await context.run(_image_dimensions, verified.head_bytes)
        if image
        else None,
        etag=original.etag,
        version_id=original.version_id,
        observation=verified.observation,
    )


def register(registry):
    registry.allow(_LIST + "(uuid,uuid,jsonb,jsonb)")
    registry.allow(_ACTION + "(jsonb,text,jsonb,jsonb)")
    copy_slot = asyncio.Semaphore(1)

    async def copy(context, subject, body=None):
        async with copy_slot:
            return await _copy(context, subject, body)

    registry.add("import", "list", "download", _list, access="import_read")
    registry.add("import", "read", "download", _read, access="import_read")
    registry.add("import", "copy", "command", copy, access="import_write")
