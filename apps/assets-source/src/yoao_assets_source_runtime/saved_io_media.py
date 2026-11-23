"""Registered physical media operations; no request supplies an S3 locator."""

from __future__ import annotations

import base64
import hashlib
import re
from uuid import UUID

from yoao_assets_source import managed_original as writer
from yoao_assets_source.saved_io import SavedIOError

_FAMILY = "media"
_CHUNK = 256 * 1024
_DIGEST = re.compile(r"[a-f0-9]{64}")
_ARTIFACT = "yoao_assets_v1.media_io_artifact"
_CACHE = "yoao_assets_v1.media_io_cache_read"
_ORIGINAL_READ = "yoao_assets_v1.media_io_original_read"
_CREATION = "yoao_assets_v1.media_io_creation"
_CREATION_EVIDENCE = "yoao_assets_v1.media_io_creation_evidence"
_THUMBNAIL = "yoao_assets_v1.media_io_thumbnail"
_MASK_UPLOAD = "yoao_assets_v1.media_io_mask_upload"
_ORIGINAL_CACHE = "yoao_assets_v1.media_io_original_cache"
_BUILTIN = "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7"


def _fields(subject, *required, optional=()):
    if type(subject) is not dict or not set(required) <= subject.keys() <= set(
        required
    ) | set(optional):
        raise SavedIOError("invalid_media_subject", status_code=400)


def _uuid(value):
    try:
        if type(value) is str and str(UUID(value)) == value:
            return UUID(value)
    except ValueError:
        pass
    raise SavedIOError("invalid_media_subject", status_code=400)


def _denied():
    return SavedIOError("media_owner_changed", status_code=409)


def _mismatch():
    return SavedIOError("media_content_mismatch", status_code=409)


def _version(value):
    return None if value in {None, "null"} else value


def _receipt(expected):
    size, digest = expected.get("size"), expected.get("sha256")
    if (
        type(size) is not int
        or not 0 <= size < 2**63
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
    ):
        raise _denied()
    return size, digest


async def _pin(context, expected, *, version=None):
    if expected is None:
        raise _denied()
    if expected["location_id"] == _BUILTIN:
        pin = context.builtin(expected["purpose"], expected["key"], version=version)
        if pin.bucket != expected["bucket"] or context.config.s3_endpoint.rstrip(
            "/"
        ) != expected["endpoint"].rstrip("/"):
            raise _denied()
        return pin
    return await context.s3(
        UUID(expected["location_id"]),
        expected["config_revision"],
        expected["connection_revision"],
        expected["purpose"],
        expected["bucket"],
        expected["key"],
        version=version,
    )


def _identity(result, size, *, etag=None, version=None, content_range=None):
    observed = result.get("ETag")
    if (
        not isinstance(observed, str)
        or not 0 < len(observed) <= 1024
        or any(ord(character) < 33 or ord(character) == 127 for character in observed)
        or etag is not None
        and etag != observed
        or version is not None
        and _version(result.get("VersionId")) != version
        or type(result.get("ContentLength")) is not int
        or result["ContentLength"] != size
        or result.get("DeleteMarker")
        or result.get("ContentRange") != content_range
    ):
        raise _mismatch()
    return observed, _version(result.get("VersionId"))


def _digest(body, size, digest):
    remaining = size
    checksum = hashlib.sha256()
    while remaining:
        chunk = body.read(min(remaining, _CHUNK))
        if not chunk or len(chunk) > remaining:
            raise _mismatch()
        remaining -= len(chunk)
        checksum.update(chunk)
    if body.read(1) or checksum.hexdigest() != digest:
        raise _mismatch()


def _check_upload(body, expected):
    size, digest = _receipt(expected)
    _digest(body, size, digest)
    body.seek(0)


async def _verified(context, pin, expected, *, acknowledged=None):
    size, digest = _receipt(expected)
    version = expected.get("version")
    etag = expected.get("etag")
    if acknowledged is not None:
        version = _version(acknowledged.get("VersionId"))
        etag = acknowledged.get("ETag")
        if not etag:
            raise SavedIOError("media_write_unproven", status_code=503, unsettled=True)
        pin = pin.with_version(version)
    before = await pin.head()
    etag, version = _identity(before, size, etag=etag, version=version)
    exact = pin.with_version(version)
    download = await exact.get(if_match=etag)
    try:
        if _identity(download.metadata, size, etag=etag, version=version) != (
            etag,
            version,
        ):
            raise _mismatch()
        await context.run(_digest, download.body, size, digest)
    finally:
        await context.run(download.close)
    after = await pin.head()
    if _identity(after, size, etag=etag, version=version) != (etag, version):
        raise _mismatch()
    current = False
    if acknowledged is not None and version is not None:
        try:
            current = _identity(
                await pin.with_version(None).head(), size, etag=etag, version=version
            ) == (etag, version)
        except SavedIOError as exc:
            if exc.unsettled:
                raise
    return {"etag": etag, "version_id": version, "current_head_matches": current}


async def _open(
    context, expected, *, selected=None, recheck, strict=False, dynamic=False
):
    pin = await _pin(context, expected, version=expected.get("version"))
    before = await pin.head()
    size = before.get("ContentLength") if dynamic else _receipt(expected)[0]
    if type(size) is not int or size < 0:
        raise _mismatch()
    etag, version = _identity(
        before, size, etag=expected.get("etag"), version=expected.get("version")
    )
    if strict:
        try:
            unexpected = await pin.with_version(version).get(
                if_match='"aap-impossible-etag"'
            )
        except SavedIOError as exc:
            if exc.unsettled or exc.storage_code not in {"412", "PreconditionFailed"}:
                raise SavedIOError(
                    "content_conditions_unverified", unsettled=exc.unsettled
                ) from None
        else:
            await context.run(unexpected.close)
            raise SavedIOError("content_conditions_unsupported")
    length, content_range, byte_range = size, None, None
    if selected is not None:
        if (
            type(selected) is not list
            or len(selected) != 2
            or any(type(value) is not int for value in selected)
            or not 0 <= selected[0] <= selected[1] < size
        ):
            raise SavedIOError("invalid_media_range", status_code=416)
        start, end = selected
        length, content_range = end - start + 1, f"bytes {start}-{end}/{size}"
        byte_range = f"bytes={start}-{end}"
    download = await pin.with_version(version).get(range=byte_range, if_match=etag)
    try:
        if _identity(
            download.metadata,
            length,
            etag=etag,
            version=version,
            content_range=content_range,
        ) != (etag, version):
            raise _mismatch()
        if await recheck() != expected:
            raise _denied()
    except BaseException:
        await context.run(download.close)
        raise
    if selected is None:

        async def after_body():
            after = await pin.head()
            if _identity(after, size, etag=etag, version=version) != (etag, version):
                raise _mismatch()
            if await recheck() != expected:
                raise _denied()

        download.after_body = after_body
    return download


def _artifact(kind, action):
    async def handler(context, subject, body=None):
        _fields(subject, "id", "permit")
        identifier, permit = _uuid(subject["id"]), _uuid(subject["permit"])

        async def proof(claim=False):
            return await context.fetch(
                _ARTIFACT, kind, action, identifier, permit, claim
            )

        expected = await proof()
        pin = await _pin(context, expected)
        if action == "read":
            return await _open(context, expected, recheck=proof)
        if action == "write":
            await context.run(_check_upload, body, expected)
        if await proof(claim=True) != expected:
            raise _denied()
        if action == "delete":
            if kind == "raster":
                try:
                    await _verified(context, pin, expected)
                except SavedIOError as exc:
                    if (
                        exc.storage_code
                        in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}
                        and not exc.unsettled
                    ):
                        return {}
                    raise
                if await proof() != expected:
                    raise _denied()
            try:
                await pin.delete()
            except SavedIOError as exc:
                if (
                    exc.storage_code
                    not in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}
                    or exc.unsettled
                ):
                    raise
            return {}
        try:
            result = await pin.put(
                body,
                length=expected["size"],
                content_type=expected["content_type"],
                if_none_match="*",
            )
        except SavedIOError as exc:
            if (
                kind != "raster"
                or exc.unsettled
                or exc.storage_code not in {"412", "PreconditionFailed"}
            ):
                raise
            await _verified(context, pin, expected)
            if await proof() != expected:
                raise _denied()
            return {"reused": True}
        verified = await _verified(context, pin, expected, acknowledged=result)
        if await proof() != expected:
            raise SavedIOError("media_owner_changed", status_code=409, unsettled=True)
        return {
            "version_id": verified["version_id"],
            "current_head_matches": verified["current_head_matches"],
        }

    return handler


def _cache(action):
    async def handler(context, subject, body=None):
        _fields(
            subject,
            "kind",
            "id",
            "relative",
            "permit",
            optional=("range",) if action == "download" else (),
        )
        kind, relative = subject["kind"], subject["relative"]
        if (
            kind not in ("thumbnail", "pyramid", "video")
            or type(relative) is not str
            or not 0 < len(relative) <= 255
        ):
            raise _denied()
        identifier, permit = _uuid(subject["id"]), _uuid(subject["permit"])

        async def proof():
            return await context.fetch(_CACHE, kind, identifier, relative, permit)

        expected = await proof()
        if expected is None:
            raise _denied()
        if action == "download":
            return await _open(
                context, expected, selected=subject.get("range"), recheck=proof
            )
        pin = await _pin(context, expected, version=expected.get("version"))
        if action == "verify":
            observed = await _verified(context, pin, expected)
            observed.update(sha256=expected["sha256"], size=expected["size"])
        else:
            size, _ = _receipt(expected)
            observed = await pin.head()
            _identity(
                observed,
                size,
                etag=expected.get("etag"),
                version=expected.get("version"),
            )
        if await proof() != expected:
            raise _denied()
        return observed

    return handler


async def _original_read(context, subject, body=None):
    _fields(subject, "kind", "id", "permit")
    identifier, permit = _uuid(subject["id"]), _uuid(subject["permit"])
    kind = subject["kind"]
    if kind not in ("task_original", "dataset_asset", "dataset_auxiliary", "creation"):
        raise _denied()

    async def proof():
        return await context.fetch(_ORIGINAL_READ, kind, identifier, permit)

    expected = await proof()
    if expected is None:
        raise _denied()
    return await _open(context, expected, recheck=proof, strict=True)


async def _operator_verify(context, subject, body=None):
    _fields(subject, "id", "permit")
    identifier, permit = _uuid(subject["id"]), _uuid(subject["permit"])

    async def proof():
        return await context.fetch(_ORIGINAL_READ, "operator", identifier, permit)

    expected = await proof()
    pin = await _pin(
        context, expected, version=expected.get("version") if expected else None
    )
    await _verified(context, pin, expected)
    if await proof() != expected:
        raise _denied()
    return {"sha256": expected["sha256"], "size_bytes": expected["size"]}


def _thumbnail(action):
    async def handler(context, subject, body=None):
        _fields(subject, "id", "permit")
        identifier, permit = _uuid(subject["id"]), _uuid(subject["permit"])

        async def proof(claim=False):
            return await context.fetch(_THUMBNAIL, action, identifier, permit, claim)

        expected = await proof()
        pin = await _pin(
            context, expected, version=expected.get("version") if expected else None
        )
        if action == "write":
            await context.run(_check_upload, body, expected)
            if await proof(claim=True) != expected:
                raise _denied()
            result = await pin.put(
                body,
                length=expected["size"],
                content_type="image/webp",
                if_none_match="*",
            )
            verified = await _verified(context, pin, expected, acknowledged=result)
            if await proof() != expected:
                raise SavedIOError("media_owner_changed", unsettled=True)
            return verified
        try:
            observed = await pin.head(if_match=expected["etag"])
            if _version(observed.get("VersionId")) != expected["version"]:
                raise _mismatch()
            await _verified(context, pin, expected)
        except SavedIOError as exc:
            if not exc.unsettled and exc.storage_code in {
                "404",
                "NoSuchKey",
                "NoSuchVersion",
                "NotFound",
            }:
                return {"deleted": 0}
            raise
        if await proof(claim=True) != expected:
            raise _denied()
        pin = pin.with_version(observed.get("VersionId"))
        await pin.delete(if_match=expected["etag"])
        try:
            await pin.head()
        except SavedIOError as exc:
            if not exc.unsettled and exc.storage_code in {
                "404",
                "NoSuchKey",
                "NoSuchVersion",
                "NotFound",
            }:
                return {"deleted": 1}
        raise SavedIOError(
            "thumbnail_cleanup_unproven", status_code=503, unsettled=True
        )

    return handler


def _mask_upload(action):
    async def handler(context, subject, body=None):
        _fields(subject, "id", "permit")
        identifier, permit = _uuid(subject["id"]), _uuid(subject["permit"])

        async def proof():
            return await context.fetch(_MASK_UPLOAD, identifier, permit, action)

        expected = await proof()
        if expected is None:
            raise _denied()
        if action == "read":
            return await _open(context, expected, recheck=proof, dynamic=True)
        pin = await _pin(context, expected)
        url = await pin.sign(
            "PUT",
            expires_in=expected["expires_in"],
            content_type=expected["content_type"],
        )
        revised = await proof()
        if revised is None:
            raise _denied()
        revised.pop("expires_in")
        expected.pop("expires_in")
        if revised != expected:
            raise _denied()
        return {"url": url}

    return handler


def _original_cache(action):
    async def handler(context, subject, body=None):
        extra = (
            ("lease",)
            if action == "read"
            else ("content_type",)
            if action == "write"
            else ()
        )
        _fields(subject, "id", "permit", *extra)
        identifier, permit = _uuid(subject["id"]), _uuid(subject["permit"])
        lease = _uuid(subject["lease"]) if action == "read" else None

        async def proof(claim=False):
            return await context.fetch(
                _ORIGINAL_CACHE, identifier, permit, lease, action, claim
            )

        expected = await proof()
        if expected is None:
            raise _denied()
        if action == "read":
            return await _open(context, expected, recheck=proof)
        pin = await _pin(context, expected)
        if action == "write":
            content_type = subject["content_type"]
            if type(content_type) is not str or not re.fullmatch(
                r"(?:image/[a-z0-9.+-]{1,64}|application/octet-stream)", content_type
            ):
                raise _denied()
            await context.run(_check_upload, body, expected)
        if await proof(claim=True) != expected:
            raise _denied()
        if action == "delete":
            await pin.delete()
            return {}
        result = await pin.put(
            body,
            length=expected["size"],
            content_type=content_type,
            if_none_match="*",
        )
        verified = await _verified(context, pin, expected, acknowledged=result)
        if await proof() != expected:
            raise SavedIOError("media_owner_changed", unsettled=True)
        return verified

    return handler


def _prepared(evidence):
    return writer.Prepared(
        evidence["sha256"],
        evidence["md5"],
        evidence["size_bytes"],
        b"",
        evidence["upload_id"],
        tuple(evidence["parts"]),
        evidence["bucket"],
        evidence["object_key"],
    )


def _observed_source(observation):
    return writer.Source(
        observation["sha256"],
        observation["md5"],
        observation["size_bytes"],
        b"",
        observation["etag"],
        observation["version_id"],
        observation["observation"],
    )


def _writer_error(exc):
    status = (
        413
        if exc.code == "creation_byte_budget_exceeded"
        else 503
        if exc.outcome == "unknown"
        else 409
    )
    return SavedIOError(
        exc.code,
        status_code=status,
        unsettled=exc.outcome == "unknown",
        storage_code="PreconditionFailed" if exc.outcome == "conflict" else None,
    )


def _creation(step):
    async def handler(context, subject, body=None):
        _fields(subject, "id", "permit", optional=("source_permit",))
        identifier, permit = _uuid(subject["id"]), _uuid(subject["permit"])
        source_permit = (
            _uuid(subject["source_permit"]) if "source_permit" in subject else None
        )
        if step == "sign":
            source_permit, permit = permit, None

        async def proof(claim=False):
            return await context.fetch(
                _CREATION, identifier, permit, source_permit, step, claim
            )

        expected = await proof()
        if expected is None or expected.get("authority_kind") == "dataset_import":
            raise _denied()
        if step == "sign":
            pin = await _pin(context, expected)
            url = await pin.sign(
                "PUT",
                expires_in=expected["expires_in"],
                content_type=expected["content_type"],
            )
            revised = await proof()
            if revised is None or {
                k: v for k, v in revised.items() if k != "expires_in"
            } != {k: v for k, v in expected.items() if k != "expires_in"}:
                raise _denied()
            return {"url": url}

        pin = await _pin(context, expected["target"])
        maximum, timeout = expected["max_bytes"], expected["timeout"]
        content_type = expected["content_type"]
        try:
            if step == "inspect":
                staging = await _pin(context, expected["staging"])
                async with staging.client() as source:
                    observed = await context.run(
                        writer.inspect_object,
                        source,
                        staging.bucket,
                        staging.key,
                        max_bytes=maximum,
                        timeout_seconds=timeout,
                    )
                if await proof() != expected:
                    raise _denied()
                return {
                    "sha256": observed.sha256,
                    "md5": observed.md5,
                    "size_bytes": observed.size_bytes,
                    "etag": observed.etag,
                    "version_id": observed.version_id,
                    "observation": observed.observation,
                }
            if (
                step in ("copy", "stream", "complete", "abort")
                and await proof(claim=True) != expected
            ):
                raise _denied()
            async with pin.client() as client:
                if step in ("copy", "stream"):
                    if step == "copy":
                        staging = await _pin(context, expected["staging"])
                        async with staging.client() as source:
                            prepared = await context.run(
                                writer.prepare_copy,
                                client,
                                staging.bucket,
                                staging.key,
                                pin.bucket,
                                pin.key,
                                _observed_source(expected["source"]),
                                content_type=content_type,
                                timeout_seconds=timeout,
                                source_client=source,
                            )
                    else:
                        prepared = await context.run(
                            writer.prepare_stream,
                            client,
                            pin.bucket,
                            pin.key,
                            body,
                            content_type=content_type,
                            max_bytes=maximum,
                            timeout_seconds=timeout,
                        )
                    try:
                        if not await context.fetch(
                            _CREATION_EVIDENCE,
                            identifier,
                            permit,
                            "prepare",
                            prepared.evidence(),
                        ):
                            raise _denied()
                    except BaseException:
                        await context.run(
                            writer.abort_prepared, client, pin.bucket, pin.key, prepared
                        )
                        raise
                    return {
                        "sha256": prepared.sha256,
                        "md5": prepared.md5,
                        "size_bytes": prepared.size_bytes,
                    }

                prepared = _prepared(expected["prepared"])
                if step == "abort":
                    await context.run(
                        writer.abort_prepared, client, pin.bucket, pin.key, prepared
                    )
                    return {}
                if step == "complete":
                    receipt = await context.run(
                        writer.complete,
                        client,
                        pin.bucket,
                        pin.key,
                        prepared,
                        content_type=content_type,
                    )
                    try:
                        if not await context.fetch(
                            _CREATION_EVIDENCE,
                            identifier,
                            permit,
                            "complete",
                            receipt.evidence(),
                        ):
                            raise _denied()
                    except BaseException:
                        raise SavedIOError(
                            "managed_original_receipt_persistence_unknown",
                            status_code=503,
                            unsettled=True,
                        ) from None
                    return receipt.evidence()

                receipt = writer.WriteReceipt(**expected["receipt"])
                verified = await context.run(
                    writer.verify,
                    client,
                    pin.bucket,
                    pin.key,
                    receipt,
                    sha256=prepared.sha256,
                    size_bytes=prepared.size_bytes,
                    max_bytes=maximum,
                    timeout_seconds=timeout,
                )
            if await proof() != expected:
                raise _denied()
            current = False
            if receipt.version_id is not None:
                try:
                    current = _identity(
                        await pin.head(),
                        prepared.size_bytes,
                        etag=receipt.etag,
                        version=receipt.version_id,
                    ) == (receipt.etag, receipt.version_id)
                except SavedIOError as exc:
                    if exc.unsettled:
                        raise
            return {
                **verified.evidence(),
                "head": base64.b64encode(verified.head_bytes).decode("ascii"),
                "current_head_matches": current,
            }
        except writer.WriterError as exc:
            raise _writer_error(exc) from None

    return handler


def register(registry):
    from . import (
        saved_io_media_derivatives,
        saved_io_media_prediction,
        saved_io_media_retirement,
    )

    saved_io_media_derivatives.register(registry)
    saved_io_media_prediction.register(registry)
    saved_io_media_retirement.register(registry)
    registry.allow(_ARTIFACT + "(text,text,uuid,uuid,boolean)")
    registry.allow(_CACHE + "(text,uuid,text,uuid)")
    registry.allow(_ORIGINAL_READ + "(text,uuid,uuid)")
    registry.allow(_CREATION + "(uuid,uuid,uuid,text,boolean)")
    registry.allow(_CREATION_EVIDENCE + "(uuid,uuid,text,jsonb)")
    registry.allow(_THUMBNAIL + "(text,uuid,uuid,boolean)")
    registry.allow(_MASK_UPLOAD + "(uuid,uuid,text)")
    registry.allow(_ORIGINAL_CACHE + "(uuid,uuid,uuid,text,boolean)")
    for kind in ("guide", "attachment", "raster"):
        registry.add(
            _FAMILY, f"{kind}-read", "download", _artifact(kind, "read"), access="read"
        )
        registry.add(
            _FAMILY, f"{kind}-write", "upload", _artifact(kind, "write"), access="write"
        )
        registry.add(
            _FAMILY,
            f"{kind}-delete",
            "command",
            _artifact(kind, "delete"),
            access="cleanup",
        )
    registry.add(_FAMILY, "cache-head", "command", _cache("head"), access="read")
    registry.add(_FAMILY, "cache-verify", "command", _cache("verify"), access="read")
    registry.add(
        _FAMILY, "cache-download", "download", _cache("download"), access="read"
    )
    registry.add(_FAMILY, "original-read", "download", _original_read, access="read")
    registry.add(_FAMILY, "operator-verify", "command", _operator_verify, access="read")
    registry.add(
        _FAMILY, "thumbnail-write", "upload", _thumbnail("write"), access="write"
    )
    registry.add(
        _FAMILY, "thumbnail-delete", "command", _thumbnail("delete"), access="cleanup"
    )
    registry.add(
        _FAMILY, "mask-format-sign", "command", _mask_upload("sign"), access="write"
    )
    registry.add(
        _FAMILY, "mask-format-read", "download", _mask_upload("read"), access="read"
    )
    registry.add(
        _FAMILY,
        "original-cache-read",
        "download",
        _original_cache("read"),
        access="read",
    )
    registry.add(
        _FAMILY,
        "original-cache-write",
        "upload",
        _original_cache("write"),
        access="write",
    )
    registry.add(
        _FAMILY,
        "original-cache-delete",
        "command",
        _original_cache("delete"),
        access="cleanup",
    )
    for step in ("sign", "inspect", "copy", "stream", "complete", "abort", "verify"):
        registry.add(
            _FAMILY,
            f"creation-{step}",
            "upload" if step == "stream" else "command",
            _creation(step),
            access="write",
        )
